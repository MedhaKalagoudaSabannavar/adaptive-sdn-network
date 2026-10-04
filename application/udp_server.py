import socket

SERVER_IP = "0.0.0.0"
SERVER_PORT = 5000

server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

server_socket.bind((SERVER_IP, SERVER_PORT))

print("UDP Server started...")
print("Waiting for messages...")

while True:
    try:
        data, client_address = server_socket.recvfrom(1024)

        message = data.decode()

        print(f"Received from {client_address}: {message}")

        reply = f"ACK: {message}"
        server_socket.sendto(reply.encode(), client_address)

    except KeyboardInterrupt:
        print("\nServer stopped.")
        break

    except Exception as e:
        print("Error:", e)

server_socket.close()
