#!/bin/bash

# go2rtc MP4 Snapshot to JPEG Converter
# This script attempts to download a JPEG snapshot from a go2rtc URL
# If the direct JPEG download fails, it will report the file type and exit.
# This version focuses on diagnosing why the direct JPEG download is failing.

# --- Configuration ---

# Default go2rtc URL for the JPEG snapshot.
# Updated to use the .jpeg endpoint you provided.
DEFAULT_GO2RTC_URL="http://cpnz.net:1984/api/frame.jpeg?src=armsatui"

# Output filename for the downloaded file (regardless of its true format)
OUTPUT_SNAPSHOT_FILENAME="cctv_snapshot.downloaded"

# --- Functions ---

# Function to display script usage
display_usage() {
  echo "Usage: $0 [go2rtc_url]"
  echo "  go2rtc_url: (Optional) The URL of the go2rtc snapshot (e.g., .jpeg or .mp4)."
  echo "              If not provided, defaults to: $DEFAULT_GO2RTC_URL"
  echo ""
  echo "Example: $0 \"http://your-go2rtc-ip:1984/api/frame.jpeg?src=mycamera\""
  echo "Output: A file named '$OUTPUT_SNAPSHOT_FILENAME' will be created."
  echo "        The script will attempt to identify its type and suggest next steps."
  echo ""
  echo "Prerequisites: curl and 'file' utility must be installed and available in your PATH."
  echo "               (FFmpeg is only needed if trying to convert an MP4, which this version bypasses for diagnosis)."
}

# --- Main Script Logic ---

# Check if curl is installed
if ! command -v curl &> /dev/null; then
  echo "Error: 'curl' is not installed. Please install it to use this script."
  exit 1
fi

# Check if 'file' utility is installed (common on Linux/macOS, via Git Bash on Windows)
if ! command -v file &> /dev/null; then
  echo "Warning: 'file' utility not found. Cannot determine the actual type of the downloaded file."
  echo "         Please install it for better diagnostics (e.g., 'sudo apt install file' on Debian/Ubuntu)."
fi

# Determine the go2rtc URL
GO2RTC_URL="${1:-$DEFAULT_GO2RTC_URL}" # Use provided argument, or default

echo "--- go2rtc Snapshot Downloader & Analyzer ---"
echo "Source URL: $GO2RTC_URL"
echo "Output File: $OUTPUT_SNAPSHOT_FILENAME"
echo "---------------------------------------------"

# 1. Download the snapshot (whatever format it is) using curl
echo "Downloading snapshot to $OUTPUT_SNAPSHOT_FILENAME..."
# Using -s for silent mode, -S for showing errors even in silent mode
if ! curl -sS -o "$OUTPUT_SNAPSHOT_FILENAME" "$GO2RTC_URL"; then
  echo "Error: Failed to download the snapshot from $GO2RTC_URL"
  rm -f "$OUTPUT_SNAPSHOT_FILENAME" # Clean up any partial download
  exit 1
fi
echo "Download complete."

# Check if the downloaded file is empty
if [ ! -s "$OUTPUT_SNAPSHOT_FILENAME" ]; then
    echo "Error: Downloaded file '$OUTPUT_SNAPSHOT_FILENAME' is empty or invalid."
    rm -f "$OUTPUT_SNAPSHOT_FILENAME"
    exit 1
fi

# 2. Analyze the downloaded file's actual type
if command -v file &> /dev/null; then
  echo "Analyzing downloaded file type:"
  file "$OUTPUT_SNAPSHOT_FILENAME"
  # You can extend this with conditional logic based on 'file' output if needed.
  # For example, if it says "HTML document", it means go2rtc returned an error page.
else
  echo "Skipping file type analysis as 'file' utility is not installed."
fi

echo ""
echo "Troubleshooting steps:"
echo "1. The file '$OUTPUT_SNAPSHOT_FILENAME' has been saved. Please try to open it with an image viewer."
echo "2. The 'file' command output above (if available) might provide clues about its actual content."
echo "3. Since you can view the streams in the go2rtc web UI, the issue is likely how the API serves the snapshot."
echo "4. Double-check your 'go2rtc' server's logs for errors when serving these snapshot requests."
echo "5. In your 'go2rtc.yaml' configuration, verify the settings for the 'armsatui' stream and how it's handled by the 'mp4' and 'mjpeg' modules. Ensure your camera is providing a compatible stream type if you expect JPEG."

echo "Script finished. Further manual investigation of the downloaded file and go2rtc configuration is recommended."

