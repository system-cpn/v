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

echo "Connecting to $IP_ADDRESS:$PORT and sending data from $CSV_FILE"

while IFS= read -r line; do
    echo "$line" | nc "$IP_ADDRESS" "$PORT"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to send data to $IP_ADDRESS:$PORT"
        exit 1
    fi
    sleep 0.5
done < "$CSV_FILE"

echo "All data sent successfully"