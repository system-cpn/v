#!/bin/bash

if [ $# -ne 3 ]; then
    echo "Usage: $0 <csv_file> <ip_address> <port>"
    echo "Example: $0 aisraw.csv 0.0.0.0 20008"
    exit 1
fi

CSV_FILE="$1"
IP_ADDRESS="$2"
PORT="$3"

if [ ! -f "$CSV_FILE" ]; then
    echo "Error: File '$CSV_FILE' not found"
    exit 1
fi

echo "Starting TCP server on $IP_ADDRESS:$PORT serving data from $CSV_FILE"

# Function to handle client connections
handle_client() {
    local client_fd=$1
    echo "Client connected"
    
    while true; do
        while IFS= read -r line; do
            echo "$line" >&${client_fd}
            if [ $? -ne 0 ]; then
                echo "Client disconnected"
                return
            fi
            sleep 0.5
        done < "$CSV_FILE"
    done
}

# Start TCP server using netcat
while true; do
    echo "Waiting for connections on $IP_ADDRESS:$PORT..."
    nc -l -s "$IP_ADDRESS" -p "$PORT" -e /bin/bash -c "
        echo 'Client connected to AIS data server'
        while true; do
            while IFS= read -r line; do
                echo \"\$line\"
                sleep 0.5
            done < '$CSV_FILE'
        done
    "
    echo "Client disconnected, waiting for new connection..."
done