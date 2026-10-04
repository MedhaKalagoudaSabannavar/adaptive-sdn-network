import socket
import time

SERVER_IP = "10.0.0.2"
SERVER_PORT = 5000

client_socket = socket.socket(
    socket.AF_INET,
    socket.SOCK_DGRAM
)

client_socket.settimeout(2)

print("UDP Client started.")

sequence_number = 1

while sequence_number <= 50:

    message = f"Packet {sequence_number}"

    try:
        client_socket.sendto(
            message.encode(),
            (SERVER_IP, SERVER_PORT)
        )

        print(f"Sent: {message}")

        data, server_address = client_socket.recvfrom(1024)

        reply = data.decode()

        print(f"Received: {reply}")

    except socket.timeout:

        print(
            f"Packet {sequence_number}: "
            "TIMEOUT - Packet may be lost"
        )

    except Exception as e:

        print("Error:", e)

    sequence_number += 1

    time.sleep(0.5)

client_socket.close()

print("UDP Client finished.")
