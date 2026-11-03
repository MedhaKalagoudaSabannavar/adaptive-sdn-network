#!/usr/bin/env python3
r"""Diamond Mininet topology for the Adaptive Communication Network project.

            s2   (primary path)
          /    \
 h1 - s1          s4 - h2
          \    /
            s3   (backup path)

Port numbering is fixed explicitly so it always matches controller.py:
    s1: 1=h1, 2=s2, 3=s3
    s2: 1=s1, 2=s4
    s3: 1=s1, 2=s4
    s4: 1=s2, 2=s3, 3=h2

Run with the SDN controller (start controller.py first):
    sudo python3 topology.py --controller remote
"""
import argparse
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

# Virtual address the UDP app uses to send status reports to the controller.
# No real host owns it: the packet misses every flow and reaches the
# controller as a packet-in (see controller.py).
CONTROL_VIP = "10.0.0.254"
CONTROL_VMAC = "00:00:00:00:00:fe"


def build_network(controller_mode="standalone", controller_ip="127.0.0.1",
                  controller_port=6653):
    # TCLink allows delay / loss / bandwidth limits on links (experiments).
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink,
                  autoSetMacs=False, build=False)
    if controller_mode == "remote":
        info("*** Adding remote SDN controller\n")
        net.addController("c0", controller=RemoteController,
                          ip=controller_ip, port=controller_port)

    info("*** Adding hosts\n")
    h1 = net.addHost("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")

    info("*** Adding OpenFlow 1.3 switches\n")
    switch_args = {"protocols": "OpenFlow13"}
    if controller_mode == "standalone":
        switch_args["failMode"] = "standalone"
    s1 = net.addSwitch("s1", dpid="0000000000000001", **switch_args)
    s2 = net.addSwitch("s2", dpid="0000000000000002", **switch_args)
    s3 = net.addSwitch("s3", dpid="0000000000000003", **switch_args)
    s4 = net.addSwitch("s4", dpid="0000000000000004", **switch_args)

    info("*** Adding primary and backup paths (ports fixed)\n")
    net.addLink(h1, s1, port2=1)
    net.addLink(s1, s2, port1=2, port2=1)  # Primary path
    net.addLink(s2, s4, port1=2, port2=1)
    net.addLink(s1, s3, port1=3, port2=1)  # Backup path
    net.addLink(s3, s4, port1=2, port2=2)
    net.addLink(h2, s4, port2=3)

    info("*** Building and starting network\n")
    net.build()
    net.start()

    # Static ARP so hosts can send status reports to the controller's
    # virtual IP (used by the Socket-SDN integration).
    for name in ("h1", "h2"):
        net.get(name).cmd("arp -s %s %s" % (CONTROL_VIP, CONTROL_VMAC))

    return net


def main():
    parser = argparse.ArgumentParser(description="Run the diamond Mininet topology.")
    parser.add_argument("--controller", choices=["standalone", "remote"],
                        default="standalone",
                        help="standalone for topology tests; remote for SDN integration")
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--controller-port", type=int, default=6653)
    args = parser.parse_args()

    setLogLevel("info")
    net = None
    try:
        net = build_network(args.controller, args.controller_ip, args.controller_port)
        info("*** Try: net, links, pingall, h1 ping -c 4 h2, iperf h1 h2\n")
        info("*** Simulate primary-link failure with: link s1 s2 down\n")
        info("*** Restore primary link with: link s1 s2 up\n")
        CLI(net)
    finally:
        if net is not None:
            info("*** Stopping network\n")
            net.stop()


if __name__ == "__main__":
    main()
