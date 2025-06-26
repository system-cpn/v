#!/bin/bash

# Configuration
IP="154.26.138.64"
PORT="5006"
FILTER_PREFIX="!AIVDM"

echo "Attempting to connect to $IP on port $PORT..."
echo "Filtering for lines starting with '$FILTER_PREFIX'..."
echo "Press Ctrl+C to stop."

# Use netcat to connect to the TCP server
# -N: Shut down the network connection after EOF on the input.
#     (This is useful if you just want to read once and exit, but for a continuous stream, it might not be strictly necessary if stdin is not closed).
#     However, netcat will keep running and printing until the connection is closed or killed.
# The output of netcat is then piped to grep.
nc $IP $PORT | while IFS= read -r line; do
  # Check if the line starts with the desired prefix
  if [[ "$line" == "$FILTER_PREFIX"* ]]; then
    echo "$line"
  fi
done

echo "Connection closed or script terminated."