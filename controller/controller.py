import os
import re
import time
from collections import defaultdict, deque

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    DEAD_DISPATCHER,
    set_ev_cls,
)
from os_ken.lib import hub
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import (
    packet,
    ethernet,
    ether_types,
    ipv4,
    udp,
)
from os_ken.topology import event
from os_ken.topology.api import get_link, get_switch


class SimpleController(app_manager.OSKenApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    H1_MAC = "00:00:00:00:00:01"
    H2_MAC = "00:00:00:00:00:02"
    HOST_MACS = {H1_MAC, H2_MAC}

    FLOW_PRIORITY = 10

    # Virtual address the UDP application sends its status reports to.
    # topology.py installs a static ARP entry for it on the hosts.
    CONTROL_IP = "10.0.0.254"
    CONTROL_MAC = "00:00:00:00:00:fe"
    CONTROL_PORT = 9999

    # Application-triggered rerouting thresholds.
    LOSS_THRESHOLD = 10.0          # percent
    LATENCY_THRESHOLD_MS = 200.0   # milliseconds
    HOLD_DOWN_SECONDS = 30         # how long the degraded path is avoided

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.mode = os.environ.get("ROUTING_MODE", "dynamic").lower()
        self.event_file = os.environ.get(
            "EVENT_LOG", "controller_events.csv"
        )

        self.primary_path = [1, 2, 4]
        self.backup_path = [1, 3, 4]
        self.active_path = list(self.primary_path)

        # ports[a][b] = port on switch a that leads to switch b.
        # Defaults match topology.py and are refreshed by discovery.
        self.ports = {
            1: {2: 2, 3: 3},
            2: {1: 1, 4: 2},
            3: {1: 1, 4: 2},
            4: {2: 1, 3: 2},
        }

        # Edge switch -> port that faces the host.
        self.host_ports = {
            1: 1,
            4: 3,
        }

        self.datapaths = {}
        self.graph = defaultdict(dict)
        self.failed_links = set()

        self.host_locations = {
            self.H1_MAC: (1, 1),
            self.H2_MAC: (4, 3),
        }

        # Timing of the failure -> reroute reaction.
        self.failure_detected_at = None

        # Links to avoid after an application-triggered reroute.
        self.avoided_edges = set()
        self.avoid_until = 0.0

        self.log_event("controller_start", "mode=%s" % self.mode)
        self.logger.info("Routing mode: %s", self.mode.upper())

        hub.spawn(self._hold_down_monitor)

    # ------------------------------------------------------------------
    # Event log (timestamped CSV for the experiments / report)
    # ------------------------------------------------------------------

    def log_event(self, name, detail=""):
        stamp = time.time()
        detail = str(detail).replace(",", ";")
        self.logger.info("[EVENT] %s %s", name, detail)
        try:
            with open(self.event_file, "a") as handle:
                handle.write("%.6f,%s,%s\n" % (stamp, name, detail))
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Switch lifecycle
    # ------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        datapath = ev.datapath

        if ev.state == MAIN_DISPATCHER:
            self.datapaths[datapath.id] = datapath
            self.logger.info("Switch s%s connected", datapath.id)

        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(datapath.id, None)
            self.logger.warning("Switch s%s disconnected", datapath.id)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Table-miss: send unknown packets to the controller.
        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER,
            )
        ]
        self.add_flow(datapath, 0, parser.OFPMatch(), actions)

        self.logger.info("Switch s%s ready", datapath.id)

    # ------------------------------------------------------------------
    # Flow helpers
    # ------------------------------------------------------------------

    def add_flow(self, datapath, priority, match, actions):
        parser = datapath.ofproto_parser

        instructions = [
            parser.OFPInstructionActions(
                datapath.ofproto.OFPIT_APPLY_ACTIONS,
                actions,
            )
        ]

        datapath.send_msg(
            parser.OFPFlowMod(
                datapath=datapath,
                priority=priority,
                match=match,
                instructions=instructions,
            )
        )

    def delete_path_flows(self):
        """Delete only this controller's host-destination path rules."""
        for datapath in list(self.datapaths.values()):
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser

            for mac in self.HOST_MACS:
                datapath.send_msg(
                    parser.OFPFlowMod(
                        datapath=datapath,
                        command=ofproto.OFPFC_DELETE,
                        out_port=ofproto.OFPP_ANY,
                        out_group=ofproto.OFPG_ANY,
                        priority=self.FLOW_PRIORITY,
                        match=parser.OFPMatch(eth_dst=mac),
                    )
                )

    # ------------------------------------------------------------------
    # Topology discovery
    # ------------------------------------------------------------------

    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        self.refresh_topology()

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        src = ev.link.src
        dst = ev.link.dst
        key = self._link_key(src.dpid, dst.dpid)

        self.graph[src.dpid][dst.dpid] = src.port_no
        self.ports.setdefault(src.dpid, {})[dst.dpid] = src.port_no

        if key in self.failed_links:
            self.failed_links.discard(key)
            self.log_event("link_restored", "s%s-s%s" % key)

        self.logger.info(
            "Discovered link s%s:%s -> s%s:%s",
            src.dpid, src.port_no, dst.dpid, dst.port_no,
        )

        # A restored link may give a better (primary) path again.
        # Stay quiet while the graph is still being built at startup.
        self.recompute_routes(report_missing=False)

    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev):
        src = ev.link.src
        dst = ev.link.dst
        key = self._link_key(src.dpid, dst.dpid)

        if key not in self.failed_links:
            self._note_failure(key)
        self.failed_links.add(key)

        self.graph.get(src.dpid, {}).pop(dst.dpid, None)
        self.graph.get(dst.dpid, {}).pop(src.dpid, None)

        self.logger.warning(
            "Link removed: s%s <-> s%s", src.dpid, dst.dpid
        )
        self.recompute_routes()

    def refresh_topology(self):
        try:
            switches = get_switch(self, None)
            links = get_link(self, None)
        except Exception as exc:
            self.logger.warning("Topology refresh failed: %s", exc)
            return

        self.graph.clear()
        self.ports = {}

        for switch in switches:
            self.graph.setdefault(switch.dp.id, {})

        for link in links:
            src = link.src
            dst = link.dst

            if self._link_key(src.dpid, dst.dpid) in self.failed_links:
                continue

            self.graph[src.dpid][dst.dpid] = src.port_no
            self.ports.setdefault(src.dpid, {})[dst.dpid] = src.port_no

    @staticmethod
    def _link_key(a, b):
        return tuple(sorted((a, b)))

    def _note_failure(self, key):
        if self.failure_detected_at is None:
            self.failure_detected_at = time.time()
        self.log_event("failure_detected", "s%s-s%s" % key)

    # ------------------------------------------------------------------
    # Fast failure detection via port status
    # ------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto

        if msg.reason not in (
            ofproto.OFPPR_ADD,
            ofproto.OFPPR_MODIFY,
            ofproto.OFPPR_DELETE,
        ):
            return

        port_no = msg.desc.port_no
        state = msg.desc.state

        if state & ofproto.OFPPS_LINK_DOWN:
            self.logger.warning(
                "Port down: s%s port %s", datapath.id, port_no
            )

            # Find the neighbour using the known port mapping.
            for neighbor, neighbor_port in list(
                self.ports.get(datapath.id, {}).items()
            ):
                if neighbor_port == port_no:
                    key = self._link_key(datapath.id, neighbor)
                    if key not in self.failed_links:
                        self._note_failure(key)
                    self.failed_links.add(key)
                    self.graph.get(datapath.id, {}).pop(neighbor, None)
                    self.graph.get(neighbor, {}).pop(datapath.id, None)

            self.recompute_routes()

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def shortest_path(self, source, destination, excluded=None):
        """BFS shortest path. Neighbours are visited in sorted order so the
        result is deterministic (s2 is preferred over s3 when both work).
        `excluded` is an optional set of link keys that must not be used."""
        if source == destination:
            return [source]

        queue = deque([(source, [source])])
        visited = {source}

        while queue:
            current, path = queue.popleft()

            for neighbor in sorted(self.graph.get(current, {})):
                if neighbor in visited:
                    continue

                if excluded and self._link_key(current, neighbor) in excluded:
                    continue

                new_path = path + [neighbor]

                if neighbor == destination:
                    return new_path

                visited.add(neighbor)
                queue.append((neighbor, new_path))

        return None

    def static_path(self, source, destination):
        """Baseline: always the fixed primary path (never reroutes)."""
        if source == destination:
            return [source]

        fixed = self.primary_path
        if source in fixed and destination in fixed:
            i = fixed.index(source)
            j = fixed.index(destination)
            if i < j:
                return fixed[i:j + 1]
            return fixed[j:i + 1][::-1]

        return None

    def _exclusions(self):
        if self.avoided_edges and time.time() < self.avoid_until:
            return self.avoided_edges
        return None

    def route(self, source, destination):
        """The one place that decides which path traffic should use."""
        if self.mode == "static":
            return self.static_path(source, destination)

        excluded = self._exclusions()
        path = self.shortest_path(source, destination, excluded)

        # If avoiding the degraded links leaves no route, use them anyway.
        if path is None and excluded:
            path = self.shortest_path(source, destination)

        return path

    @classmethod
    def _path_edges(cls, path):
        return {
            cls._link_key(path[i], path[i + 1])
            for i in range(len(path) - 1)
        }

    def recompute_routes(self, report_missing=True):
        if self.mode == "static":
            return

        path = self.route(1, 4)

        if not path:
            if not report_missing:
                return

            self.logger.error("No route available between s1 and s4")
            self.log_event("no_route", "s1-s4 unreachable")

            if self.active_path is not None:
                self.active_path = None
                self.delete_path_flows()
            return

        if path != self.active_path:
            self._apply_path(path, "topology")
        else:
            self.failure_detected_at = None

    def _apply_path(self, path, reason):
        old = self.active_path

        self.logger.warning(
            "Changing path from %s to %s (%s)", old, path, reason
        )
        self.active_path = path
        self.install_all_host_paths()

        detail = "%s -> %s (%s)" % (old, path, reason)

        if self.failure_detected_at is not None:
            reaction_ms = (time.time() - self.failure_detected_at) * 1000.0
            self.logger.warning(
                "[TIMING] failure -> new flows installed in %.1f ms",
                reaction_ms,
            )
            detail += " reaction_ms=%.1f" % reaction_ms
            self.failure_detected_at = None

        self.log_event("path_changed", detail)

    def install_all_host_paths(self):
        self.delete_path_flows()

        if not self.active_path:
            return

        self.install_path(self.H2_MAC, self.active_path)
        self.install_path(self.H1_MAC, list(reversed(self.active_path)))

    def install_path(self, dst_mac, path):
        if not path:
            return

        self.logger.info("Installing path to %s: %s", dst_mac, path)

        for index, switch_id in enumerate(path):
            datapath = self.datapaths.get(switch_id)

            if datapath is None:
                continue

            if index == len(path) - 1:
                out_port = self.host_ports.get(switch_id)
            else:
                next_switch = path[index + 1]
                out_port = self.ports.get(switch_id, {}).get(next_switch)

            if out_port is None:
                self.logger.warning(
                    "Unknown output port on s%s along %s",
                    switch_id, path,
                )
                continue

            parser = datapath.ofproto_parser
            match = parser.OFPMatch(eth_dst=dst_mac)
            actions = [parser.OFPActionOutput(out_port)]

            self.add_flow(datapath, self.FLOW_PRIORITY, match, actions)

    def _hold_down_monitor(self):
        """After the hold-down expires, allow the primary path again."""
        while True:
            hub.sleep(1)

            if self.avoided_edges and time.time() >= self.avoid_until:
                self.avoided_edges = set()
                self.log_event("hold_down_expired", "primary path allowed")
                self.recompute_routes(report_missing=False)

    # ------------------------------------------------------------------
    # Socket-SDN integration: application status reports
    # ------------------------------------------------------------------

    def handle_control_packet(self, pkt):
        """Handle 'REPORT loss=<pct> latency_ms=<ms>' sent by the UDP app
        to CONTROL_IP:CONTROL_PORT."""
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        udp_pkt = pkt.get_protocol(udp.udp)

        if ip_pkt is None or udp_pkt is None:
            return
        if ip_pkt.dst != self.CONTROL_IP or udp_pkt.dst_port != self.CONTROL_PORT:
            return

        payload = pkt.protocols[-1] if pkt.protocols else b""
        if not isinstance(payload, (bytes, bytearray)):
            return

        text = payload.decode(errors="ignore")

        try:
            loss_match = re.search(r"loss=([0-9]+(?:\.[0-9]+)?)", text)
            lat_match = re.search(r"latency_ms=([0-9]+(?:\.[0-9]+)?)", text)
            loss = float(loss_match.group(1)) if loss_match else 0.0
            latency = float(lat_match.group(1)) if lat_match else 0.0
        except ValueError:
            return

        self.log_event(
            "app_report", "loss=%.1f latency_ms=%.1f" % (loss, latency)
        )

        if self.mode == "static":
            return

        degraded = (
            loss >= self.LOSS_THRESHOLD
            or latency >= self.LATENCY_THRESHOLD_MS
        )
        if not degraded:
            return

        if time.time() < self.avoid_until:
            return  # already reacted recently

        if not self.active_path:
            return

        excluded = self._path_edges(self.active_path)
        alternate = self.shortest_path(1, 4, excluded)

        if not alternate:
            self.logger.warning(
                "App reports degradation but no alternate path exists"
            )
            self.log_event("no_alternate", "loss=%.1f" % loss)
            return

        self.logger.warning(
            "App-reported degradation (loss=%.1f%%, latency=%.1fms): "
            "moving traffic off %s",
            loss, latency, self.active_path,
        )

        self.avoided_edges = excluded
        self.avoid_until = time.time() + self.HOLD_DOWN_SECONDS
        self._apply_path(alternate, "app-report")

    # ------------------------------------------------------------------
    # Packet-in
    # ------------------------------------------------------------------

    def flood_to_hosts(self, datapath, in_port, data):
        """Loop-free replacement for OFPP_FLOOD.

        Broadcast/unknown traffic is accepted only when it enters from a
        host-facing port, and is delivered straight to the host ports of the
        OTHER edge switches. Copies arriving over switch-to-switch links are
        dropped, so the s1-s2-s4-s3 loop can never cause a storm.
        """
        if self.host_ports.get(datapath.id) != in_port:
            return

        for switch_id, host_port in self.host_ports.items():
            if switch_id == datapath.id:
                continue

            dp = self.datapaths.get(switch_id)
            if dp is None:
                continue

            dp.send_msg(
                dp.ofproto_parser.OFPPacketOut(
                    datapath=dp,
                    buffer_id=dp.ofproto.OFP_NO_BUFFER,
                    in_port=dp.ofproto.OFPP_CONTROLLER,
                    actions=[dp.ofproto_parser.OFPActionOutput(host_port)],
                    data=data,
                )
            )

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        # Ignore IPv6 / multicast chatter from the hosts.
        if (eth.ethertype == ether_types.ETH_TYPE_IPV6
                or eth.dst.startswith("33:33")):
            return

        src = eth.src
        dst = eth.dst

        self.logger.debug(
            "Packet %s -> %s at s%s", src, dst, datapath.id
        )

        # Learn host location ONLY on host-facing ports; packets seen on
        # transit switches must not overwrite it.
        if src in self.HOST_MACS and self.host_ports.get(datapath.id) == in_port:
            self.host_locations[src] = (datapath.id, in_port)

        # Application status report addressed to the controller.
        if dst == self.CONTROL_MAC:
            self.handle_control_packet(pkt)
            return

        actions = None

        if dst in self.HOST_MACS:
            destination = self.host_locations.get(dst)

            if destination is not None:
                target_switch, target_port = destination
                path = self.route(datapath.id, target_switch)

                if path:
                    if len(path) == 1:
                        actions = [parser.OFPActionOutput(target_port)]
                    else:
                        out_port = self.ports.get(datapath.id, {}).get(path[1])
                        if out_port is not None:
                            actions = [parser.OFPActionOutput(out_port)]

                    # Install destination-based rules along this route.
                    self.install_path(dst, path)

        if actions is None:
            # Broadcast, unknown destination or no route.
            self.flood_to_hosts(datapath, in_port, msg.data)
            return

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None,
        )
        datapath.send_msg(out)
