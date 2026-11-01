# SDN Project

## Running the Project

Follow the steps below in order to run the SDN controller and Mininet demo.

### T1 – Controller Terminal

Open the first terminal and run:

```bash
cd ~/sdn-project/controller
source ../.venv/bin/activate
osken-manager --observe-links controller.py
```

Leave this terminal running. The controller will remain active and wait for connections from Mininet.

### T2 – Mininet Demo Terminal

Open a second terminal and run:

```bash
sudo service openvswitch-switch start
sudo mn -c
cd ~/sdn-project/demo
sudo python3 demo.py
```

### Important Notes

- Start the controller in T1 before running the Mininet demo in T2.
- Keep the controller terminal (T1) running throughout the demo.
- Run the Mininet commands in the second terminal (T2).
- `sudo mn -c` cleans up any previous Mininet sessions before starting the demo.
