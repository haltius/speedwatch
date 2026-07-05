#!/bin/bash
#
# detect_connection.sh — logs whether THIS Mac is currently connected via
# Wi-Fi or Ethernet, and appends the result to a CSV that SpeedWatch's
# analyze.py can read.
#
# WHY THIS RUNS ON THE HOST, NOT IN DOCKER:
# Any containerized process (Docker Desktop, apple/container, anything
# using a VM boundary) only ever sees a *virtual* network interface handed
# to it by the container runtime — never the Mac's real Wi-Fi/Ethernet
# hardware. That distinction only exists in macOS itself, so this has to
# run natively on the host.
#
# WHAT IT ACTUALLY CHECKS (in order):
#   1. Which interface currently holds the default route (i.e. the one
#      actually carrying your traffic right now — not just any interface
#      that happens to be up).
#   2. Cross-references that interface against `networksetup
#      -listallhardwareports`, which maps device names (en0, en5, ...) to
#      human-readable hardware types ("Wi-Fi", "Thunderbolt Ethernet",
#      "USB 10/100/1000 LAN", etc.) — this is macOS's own hardware
#      inventory, not a guess.
#   3. As a second, independent confirmation: asks
#      `networksetup -getairportnetwork` whether that interface is
#      associated with a Wi-Fi network. Wi-Fi interfaces answer with an
#      SSID; Ethernet interfaces refuse the question entirely
#      ("... is not a Wi-Fi interface"). Two independent checks agreeing
#      is stronger evidence than either alone.
#
# USAGE:
#   ./detect_connection.sh                # uses default output path
#   OUTPUT_CSV=/path/to/log.csv ./detect_connection.sh
#
# Designed to be run periodically via launchd — see
# com.speedwatch.connectioncheck.plist in this same folder.

set -euo pipefail

OUTPUT_CSV="${OUTPUT_CSV:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../data" && pwd)/connection_log.csv}"

timestamp_utc() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

# --- 1. Which interface actually carries traffic right now ---
default_iface="$(route get default 2>/dev/null | awk '/interface:/{print $2}')"

if [ -z "${default_iface:-}" ]; then
    echo "$(timestamp_utc),unknown,unknown,no_default_route,,,\"could not determine default route\"" >> "$OUTPUT_CSV"
    echo "No default route found — are you connected to any network?" >&2
    exit 1
fi

# --- 2. Map that interface to its hardware type via macOS's own inventory ---
hw_ports_raw="$(networksetup -listallhardwareports 2>/dev/null)"

hardware_port="$(echo "$hw_ports_raw" | awk -v dev="$default_iface" '
    /^Hardware Port:/ { port = $0; sub(/^Hardware Port: /, "", port) }
    /^Device:/ {
        d = $2
        if (d == dev) { print port; exit }
    }
')"
hardware_port="${hardware_port:-unknown}"

# --- 3. Independent confirmation: does this interface have an associated Wi-Fi network? ---
airport_output="$(networksetup -getairportnetwork "$default_iface" 2>&1 || true)"

if echo "$airport_output" | grep -qi "Current Wi-Fi Network"; then
    airport_says_wifi="yes"
elif echo "$airport_output" | grep -qi "not a Wi-Fi interface"; then
    airport_says_wifi="no"
else
    airport_says_wifi="unclear"
fi

# --- Classify, requiring the two checks to agree before calling it confident ---
hw_is_wifi="no"
echo "$hardware_port" | grep -qi "wi-fi" && hw_is_wifi="yes"

if [ "$hw_is_wifi" = "yes" ] && [ "$airport_says_wifi" = "yes" ]; then
    connection_type="wifi"
    confidence="confirmed"
elif [ "$hw_is_wifi" = "no" ] && [ "$airport_says_wifi" = "no" ]; then
    connection_type="ethernet"
    confidence="confirmed"
elif [ "$hw_is_wifi" = "yes" ] || [ "$airport_says_wifi" = "yes" ]; then
    connection_type="wifi"
    confidence="single_check_only"
else
    connection_type="ethernet_or_other"
    confidence="single_check_only"
fi

# --- Extra corroborating detail, useful in a report even if not load-bearing ---
ip_addr="$(ipconfig getifaddr "$default_iface" 2>/dev/null || echo "")"
media_line="$(ifconfig "$default_iface" 2>/dev/null | awk '/media:/{ $1=""; print; exit }' | sed 's/^ *//')"

mkdir -p "$(dirname "$OUTPUT_CSV")"
if [ ! -f "$OUTPUT_CSV" ]; then
    echo "timestamp_utc,interface,hardware_port,connection_type,confidence,ip_address,media_info" > "$OUTPUT_CSV"
fi

# Escape any commas in free-text fields so the CSV doesn't break
media_line_escaped="$(echo "$media_line" | tr ',' ';')"

echo "$(timestamp_utc),${default_iface},${hardware_port},${connection_type},${confidence},${ip_addr},\"${media_line_escaped}\"" >> "$OUTPUT_CSV"

echo "[$(timestamp_utc)] ${default_iface} -> ${hardware_port} -> ${connection_type} (${confidence})"
