#!/usr/bin/env python3
import os
import time

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


APP_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "application"
)

CONTROLLER_IP = "127.0.0.1"
CONTROLLER_PORT = 6653

CONTROL_VIP = "10.0.0.254"
CONTROL_VMAC = "00:00:00:00:00:fe"


def build_network():
    
    net = Mininet(
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=False,
        build=False,
    )

    net.addController(
        "c0",
        controller=RemoteController,
        ip=CONTROLLER_IP,
        port=CONTROLLER_PORT,
    )

    s1 = net.addSwitch("s1", dpid="0000000000000001", protocols="OpenFlow13")
    s2 = net.addSwitch("s2", dpid="0000000000000002", protocols="OpenFlow13")
    s3 = net.addSwitch("s3", dpid="0000000000000003", protocols="OpenFlow13")
    s4 = net.addSwitch("s4", dpid="0000000000000004", protocols="OpenFlow13")

    h1 = net.addHost("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")

    
    net.addLink(h1, s1, port2=1)
    net.addLink(s1, s2, port1=2, port2=1)
    net.addLink(s1, s3, port1=3, port2=1)
    net.addLink(s2, s4, port1=2, port2=1)
    net.addLink(s3, s4, port1=2, port2=2)
    net.addLink(h2, s4, port2=3)

    return net


def banner(text):
    info("\n" + "=" * 60 + "\n")
    info(text + "\n")
    info("=" * 60 + "\n")


def show_log(host, path, title):
    info(host.cmd("tail -n 6 %s" % path))


def run_demo(net):
    h1, h2, s3 = net.get("h1", "h2", "s3")

    server = os.path.join(APP_DIR, "udp_server.py")
    client = os.path.join(APP_DIR, "udp_client.py")

    if not (os.path.exists(server) and os.path.exists(client)):
        info("ERROR: could not find udp_server.py / udp_client.py in %s\n"
             % os.path.abspath(APP_DIR))
        return

    banner("Network works (path h1 - s1 - s2 - s4 - h2)")
    info(h1.cmd("ping -c 3 -W 2 %s" % h2.IP()))

    banner("Starting UDP server (h2) and UDP client (h1)")
    h2.cmd("python3 -u %s > /tmp/server.log 2>&1 &" % server)
    time.sleep(1)
    h1.cmd("python3 -u %s > /tmp/client.log 2>&1 &" % client)
    time.sleep(4)
    show_log(h1, "/tmp/client.log", "UDP client output BEFORE failure")

    if not h1.cmd("pgrep -f udp_client.py").strip():
        info("\nNOTE: the UDP client already finished. For a better demo, "
             "make it send more packets (e.g. 200 packets, 0.1 s apart).\n")

    banner("FAILURE - cutting link s1 <-> s2")
    net.configLinkStatus("s1", "s2", "down")
    time.sleep(4)
    show_log(h1, "/tmp/client.log", "UDP client output AFTER failure (rerouted)")
    info("\n----- s3 flow rules (backup path now carries the traffic) -----\n")
    info(s3.cmd("ovs-ofctl -O OpenFlow13 dump-flows s3 | grep priority=10"))

    banner("RECOVERY - restoring link s1 <-> s2")
    net.configLinkStatus("s1", "s2", "up")
    time.sleep(6)
    show_log(h1, "/tmp/client.log", "UDP client output AFTER recovery")

    h1.cmd("pkill -f udp_client.py")
    h2.cmd("pkill -f udp_server.py")

    


def main():
    setLogLevel("info")

    net = build_network()
    net.build()
    net.start()

    # Static ARP so the UDP app can send reports to the controller's
    # virtual address (10.0.0.254).
    for name in ("h1", "h2"):
        net.get(name).cmd("arp -s %s %s" % (CONTROL_VIP, CONTROL_VMAC))

    info("*** Waiting 5s for the controller to discover the links\n")
    time.sleep(5)

    run_demo(net)

    info("\n*** Mininet CLI (type 'exit' to quit)\n")
    CLI(net)
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    main()
