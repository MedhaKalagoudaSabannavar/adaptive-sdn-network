from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls
)
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet, ethernet, ether_types


class SimpleController(app_manager.OSKenApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SimpleController, self).__init__(*args, **kwargs)

        # Primary path
        self.primary_path = [1, 2, 4]

        # Backup path
        self.backup_path = [1, 3, 4]

        # Current active path
        self.active_path = self.primary_path

        # Switch-to-switch ports
        self.ports = {
            1: {
                2: 2,
                3: 3
            },
            2: {
                1: 1,
                4: 2
            },
            3: {
                1: 1,
                4: 2
            },
            4: {
                2: 1,
                3: 2
            }
        }

        # Host ports
        self.host_ports = {
            1: 1,
            4: 3
        }

        self.datapaths = {}

    # -------------------------------------------------
    # SWITCH CONNECTED
    # -------------------------------------------------

    @set_ev_cls(
        ofp_event.EventOFPSwitchFeatures,
        CONFIG_DISPATCHER
    )
    def switch_features_handler(self, ev):

        datapath = ev.msg.datapath
        dpid = datapath.id

        self.datapaths[dpid] = datapath

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()

        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER
            )
        ]

        instructions = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS,
                actions
            )
        ]

        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=0,
            match=match,
            instructions=instructions
        )

        datapath.send_msg(flow_mod)

        print("Switch connected: s" + str(dpid))

    # -------------------------------------------------
    # ADD FLOW
    # -------------------------------------------------

    def add_flow(self, datapath, priority, match, actions):

        parser = datapath.ofproto_parser

        instructions = [
            parser.OFPInstructionActions(
                datapath.ofproto.OFPIT_APPLY_ACTIONS,
                actions
            )
        ]

        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions
        )

        datapath.send_msg(flow_mod)

    # -------------------------------------------------
    # DELETE OLD PATH FLOWS
    # -------------------------------------------------

    def delete_path_flows(self):

        print("Deleting old path flows...")

        for dpid, datapath in self.datapaths.items():

            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser

            match = parser.OFPMatch()

            flow_mod = parser.OFPFlowMod(
                datapath=datapath,
                command=ofproto.OFPFC_DELETE,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                priority=10,
                match=match
            )

            datapath.send_msg(flow_mod)

    # -------------------------------------------------
    # LINK FAILURE DETECTION
    # -------------------------------------------------

    @set_ev_cls(
        ofp_event.EventOFPPortStatus,
        MAIN_DISPATCHER
    )
    def port_status_handler(self, ev):

        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id

        ofproto = datapath.ofproto

        port_no = msg.desc.port_no
        port_state = msg.desc.state

        # Check whether the port is down
        link_down = (
            port_state & ofproto.OFPPS_LINK_DOWN
        )

        # s1-eth2 connects s1 to s2
        if dpid == 1 and port_no == 2:

            if link_down:

                print("================================")
                print("LINK FAILURE DETECTED: s1-s2")
                print("Switching to backup path...")
                print("================================")

                self.active_path = self.backup_path

                self.delete_path_flows()

                self.install_path(
                    "00:00:00:00:00:02",
                    self.active_path
                )

                reverse_path = list(
                    reversed(self.active_path)
                )

                self.install_path(
                    "00:00:00:00:00:01",
                    reverse_path
                )

                print(
                    "Active path:",
                    self.active_path
                )

            else:

                print("================================")
                print("LINK RECOVERED: s1-s2")
                print("Switching back to primary path...")
                print("================================")

                self.active_path = self.primary_path

                self.delete_path_flows()

                self.install_path(
                    "00:00:00:00:00:02",
                    self.active_path
                )

                reverse_path = list(
                    reversed(self.active_path)
                )

                self.install_path(
                    "00:00:00:00:00:01",
                    reverse_path
                )

                print(
                    "Active path:",
                    self.active_path
                )

    # -------------------------------------------------
    # PACKET IN
    # -------------------------------------------------

    @set_ev_cls(
        ofp_event.EventOFPPacketIn,
        MAIN_DISPATCHER
    )
    def packet_in_handler(self, ev):

        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id

        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)

        eth = pkt.get_protocol(
            ethernet.ethernet
        )

        if eth is None:
            return

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src = eth.src
        dst = eth.dst

        print(
            "Packet:",
            src,
            "->",
            dst,
            "at s" + str(dpid)
        )

        # Broadcast packets
        if dst == "ff:ff:ff:ff:ff:ff":

            actions = [
                parser.OFPActionOutput(
                    ofproto.OFPP_FLOOD
                )
            ]

            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=msg.buffer_id,
                in_port=in_port,
                actions=actions
            )

            if msg.buffer_id == ofproto.OFP_NO_BUFFER:
                out.data = msg.data

            datapath.send_msg(out)

            return

        # Traffic going to h2
        if dst == "00:00:00:00:00:02":

            self.install_path(
                dst,
                self.active_path
            )

            out_port = self.get_output_port(
                dpid,
                self.active_path
            )

        # Traffic going to h1
        elif dst == "00:00:00:00:00:01":

            reverse_path = list(
                reversed(self.active_path)
            )

            self.install_path(
                dst,
                reverse_path
            )

            out_port = self.get_output_port(
                dpid,
                reverse_path
            )

        else:

            out_port = ofproto.OFPP_FLOOD

        actions = [
            parser.OFPActionOutput(
                out_port
            )
        ]

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions
        )

        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            out.data = msg.data

        datapath.send_msg(out)

    # -------------------------------------------------
    # GET OUTPUT PORT
    # -------------------------------------------------

    def get_output_port(self, dpid, path):

        if dpid not in path:
            return 1

        index = path.index(dpid)

        # Last switch -> host
        if index == len(path) - 1:

            return self.host_ports[dpid]

        next_switch = path[index + 1]

        return self.ports[dpid][next_switch]

    # -------------------------------------------------
    # INSTALL PATH
    # -------------------------------------------------

    def install_path(self, dst_mac, path):

        print(
            "Installing path:",
            path
        )

        # Switch-to-switch rules
        for i in range(len(path) - 1):

            current_switch = path[i]
            next_switch = path[i + 1]

            datapath = self.datapaths.get(
                current_switch
            )

            if datapath is None:
                continue

            out_port = self.ports[
                current_switch
            ][next_switch]

            parser = datapath.ofproto_parser

            match = parser.OFPMatch(
                eth_dst=dst_mac
            )

            actions = [
                parser.OFPActionOutput(
                    out_port
                )
            ]

            self.add_flow(
                datapath,
                10,
                match,
                actions
            )

        # Last switch -> host
        final_switch = path[-1]

        datapath = self.datapaths.get(
            final_switch
        )

        if datapath is None:
            return

        out_port = self.host_ports[
            final_switch
        ]

        parser = datapath.ofproto_parser

        match = parser.OFPMatch(
            eth_dst=dst_mac
        )

        actions = [
            parser.OFPActionOutput(
                out_port
            )
        ]

        self.add_flow(
            datapath,
            10,
            match,
            actions
        )
