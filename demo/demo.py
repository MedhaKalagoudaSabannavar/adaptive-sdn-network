#!/usr/bin/env python3
import os
import sys
import time

from mininet.cli import CLI
from mininet.log import setLogLevel, info

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(HERE, "..", "application")
sys.path.insert(0, os.path.join(HERE, "..", "topology"))

from topology import build_network  # noqa: E402  (Person 2's topology)


def banner(text):
    info("\n" + "=" * 60 + "\n")
    info(text + "\n")
    info("=" * 60 + "\n")


def show_log(host, path, title):
    info("\n----- %s -----\n" % title)
    info(host.cmd("tail -n 6 %s" % path))


def show_rules(net, switch, title):
    info("\n----- %s -----\n" % title)
    info(net.get(switch).cmd(
        "ovs-ofctl -O OpenFlow13 dump-flows %s | grep priority=10" % switch
    ))


def run_demo(net):
    h1, h2 = net.get("h1", "h2")

    server = os.path.join(APP_DIR, "udp_server.py")
    client = os.path.join(APP_DIR, "udp_client.py")

    if not (os.path.exists(server) and os.path.exists(client)):
        info("ERROR: could not find udp_server.py / udp_client.py in %s\n"
             % os.path.abspath(APP_DIR))
        return

    banner("1. Network works (path h1 - s1 - s2 - s4 - h2)")
    info(h1.cmd("ping -c 3 -W 2 %s" % h2.IP()))
    show_rules(net, "s2", "s2 flow rules (primary path carries the traffic)")

    banner("2. Starting UDP server (h2) and UDP client (h1)")
    h2.cmd("python3 -u %s > /tmp/server.log 2>&1 &" % server)
    time.sleep(1)
    h1.cmd("python3 -u %s > /tmp/client.log 2>&1 &" % client)
    time.sleep(4)
    show_log(h1, "/tmp/client.log", "UDP client output BEFORE failure")

    if not h1.cmd("pgrep -f udp_client.py").strip():
        info("\nNOTE: the UDP client already finished. For a better demo, "
             "make it send more packets (e.g. 200 packets, 0.1 s apart).\n")

    banner("3. FAILURE - cutting link s1 <-> s2")
    net.configLinkStatus("s1", "s2", "down")
    time.sleep(4)
    show_log(h1, "/tmp/client.log", "UDP client output AFTER failure (rerouted)")
    show_rules(net, "s3", "s3 flow rules (backup path now carries the traffic)")

    banner("4. RECOVERY - restoring link s1 <-> s2")
    net.configLinkStatus("s1", "s2", "up")
    time.sleep(6)
    show_log(h1, "/tmp/client.log", "UDP client output AFTER recovery")

    h1.cmd("pkill -f udp_client.py")
    h2.cmd("pkill -f udp_server.py")


def main():
    setLogLevel("info")

    # Person 2's topology: builds, starts and adds the static ARP entries.
    net = build_network("remote")

    info("*** Waiting 5s for the controller to discover the links\n")
    time.sleep(5)

    try:
        run_demo(net)
        info("\n*** Mininet CLI (type 'exit' to quit)\n")
        CLI(net)
    finally:
        net.stop()


if __name__ == "__main__":
    main()
