from collections import defaultdict, deque

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    DEAD_DISPATCHER,
    set_ev_cls,
)
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import (
    packet,
    ethernet,
    ether_types,
    lldp,
)
from os_ken.topology import event
from os_ken.topology.api import get_link, get_switch


class SimpleController(app_manager.OSKenApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    HOST_MACS = {
        "00:00:00:00:00:01",
        "00:00:00:00:00:02",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.primary_path = [1, 2, 4]
        self.backup_path = [1, 3, 4]
        self.active_path = list(self.primary_path)

        
        self.ports = {
            1: {2: 2, 3: 3},
            2: {1: 1, 4: 2},
            3: {1: 1, 4: 2},
            4: {2: 1, 3: 2},
        }

        self.host_ports = {
            1: 1,
            4: 3,
        }

        self.datapaths = {}
        self.graph = defaultdict(dict)
        self.failed_links = set()
        self.host_locations = {
            "00:00:00:00:00:01": (1, 1),
            "00:00:00:00:00:02": (4, 3),
        }


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

        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER,
            )
        ]

        self.add_flow(
            datapath,
            0,
            parser.OFPMatch(),
            actions,
        )

        self.logger.info("Switch s%s ready", datapath.id)


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
                        priority=10,
                        match=parser.OFPMatch(eth_dst=mac),
                    )
                )

    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        self.refresh_topology()

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        src = ev.link.src
        dst = ev.link.dst

        self.graph[src.dpid][dst.dpid] = src.port_no
        self.ports.setdefault(src.dpid, {})[dst.dpid] = src.port_no

        self.failed_links.discard(
            self._link_key(src.dpid, dst.dpid)
        )

        self.logger.info(
            "Discovered link s%s:%s -> s%s:%s",
            src.dpid, src.port_no, dst.dpid, dst.port_no,
        )

    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev):
        src = ev.link.src
        dst = ev.link.dst

        self.failed_links.add(
            self._link_key(src.dpid, dst.dpid)
        )

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

            # Find the neighbor using the known port mapping.
            for neighbor, neighbor_port in list(
                self.ports.get(datapath.id, {}).items()
            ):
                if neighbor_port == port_no:
                    self.failed_links.add(
                        self._link_key(datapath.id, neighbor)
                    )
                    self.graph.get(datapath.id, {}).pop(neighbor, None)
                    self.graph.get(neighbor, {}).pop(datapath.id, None)

            self.recompute_routes()

    

    def shortest_path(self, source, destination):
        if source == destination:
            return [source]

        queue = deque([(source, [source])])
        visited = {source}

        while queue:
            current, path = queue.popleft()

            for neighbor in self.graph.get(current, {}):
                if neighbor in visited:
                    continue

                new_path = path + [neighbor]

                if neighbor == destination:
                    return new_path

                visited.add(neighbor)
                queue.append((neighbor, new_path))

        return None

    def recompute_routes(self):
        path = self.shortest_path(1, 4)

        if not path:
            self.logger.error("No route available between s1 and s4")
            return

        if path != self.active_path:
            self.logger.warning(
                "Changing path from %s to %s",
                self.active_path, path,
            )
            self.active_path = path
            self.install_all_host_paths()

   

    def install_all_host_paths(self):
        self.delete_path_flows()

        forward = self.shortest_path(1, 4)
        if forward:
            self.install_path("00:00:00:00:00:02", forward)

        reverse = self.shortest_path(4, 1)
        if reverse:
            self.install_path("00:00:00:00:00:01", reverse)

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

            self.add_flow(datapath, 10, match, actions)

    

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

        src = eth.src
        dst = eth.dst

        self.logger.info(
            "Packet %s -> %s at s%s", src, dst, datapath.id
        )

        # Learn the host's current switch and port.
        if src in self.HOST_MACS:
            self.host_locations[src] = (datapath.id, in_port)

        # Broadcast packets are flooded for discovery/ARP.
        if dst == "ff:ff:ff:ff:ff:ff":
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        elif dst in self.HOST_MACS:
            destination = self.host_locations.get(dst)

            if destination is None:
                actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            else:
                target_switch, target_port = destination
                path = self.shortest_path(datapath.id, target_switch)

                if not path:
                    actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
                elif len(path) == 1:
                    actions = [parser.OFPActionOutput(target_port)]
                else:
                    next_switch = path[1]
                    out_port = self.ports.get(datapath.id, {}).get(next_switch)

                    if out_port is None:
                        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
                    else:
                        actions = [parser.OFPActionOutput(out_port)]

                        # Install destination-based rules on this route.
                        self.install_path(dst, path)
        else:
            actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None,
        )
        datapath.send_msg(out)

