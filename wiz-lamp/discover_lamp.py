#!/usr/bin/env python3
"""
Discover WiZ smart lamps on the local network.

WiZ lamps listen on UDP port 38899. We broadcast a registration message and
collect the IP of any lamp that responds. No root/sudo required.
"""

import socket
import json
import time
import os
import sys
import subprocess
from pathlib import Path


WIZMOTE_PORT = 38899
BROADCAST_MSG = json.dumps({
    "method": "registration",
    "params": {
        "phoneMac": "AAAAAAAAAAAA",
        "register": False,
        "phoneIp": "1.2.3.4",
        "id": "1",
    },
}).encode()


def get_local_ip():
    """Return the Pi's primary outbound IP to show the user which interface is active."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


def discover_via_udp(timeout=10):
    """Broadcast to 255.255.255.255 and collect WiZ lamp responses."""
    found = {}  # ip -> mac

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1)
    sock.bind(("", 0))  # OS picks an available port; lamp replies to it

    print(f"Broadcasting on UDP port {WIZMOTE_PORT}  (listening {timeout}s) ...")
    sock.sendto(BROADCAST_MSG, ("255.255.255.255", WIZMOTE_PORT))

    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = sock.recvfrom(1024)
            ip = addr[0]
            if ip in found:
                continue
            try:
                payload = json.loads(data.decode())
                mac = payload.get("result", {}).get("mac", "unknown")
            except (json.JSONDecodeError, AttributeError):
                mac = "unknown"
            found[ip] = mac
            print(f"  ✓  Found WiZ lamp  IP={ip}  MAC={mac}")
        except socket.timeout:
            continue
        except Exception as e:
            print(f"  [warn] {e}", file=sys.stderr)

    sock.close()
    return found


def discover_via_pywizlight(timeout=10):
    """Alternative: use pywizlight's built-in discovery (requires the lib)."""
    try:
        import asyncio
        from pywizlight import discovery as wiz_discovery

        print("Trying pywizlight discovery ...")

        async def _run():
            return await wiz_discovery.find_wizlights(wait_time=timeout)

        bulbs = asyncio.run(_run())
        result = {}
        for b in bulbs:
            result[b.ip] = "unknown"
            print(f"  ✓  Found via pywizlight  IP={b.ip}")
        return result
    except Exception as e:
        print(f"  [pywizlight discovery failed: {e}]", file=sys.stderr)
        return {}


def try_nmap(local_ip):
    """Last-resort: nmap UDP scan. Requires nmap and may need sudo."""
    subnet = ".".join(local_ip.split(".")[:3]) + ".0/24"
    print(f"\nFalling back to nmap scan of {subnet} on UDP 38899 (needs sudo + nmap) ...")
    try:
        result = subprocess.run(
            ["sudo", "nmap", "-sU", "-p", "38899", "--open", "-oG", "-", subnet],
            capture_output=True, text=True, timeout=120,
        )
        ips = []
        for line in result.stdout.splitlines():
            if "Ports: 38899/open" in line:
                ip = line.split()[1]
                ips.append(ip)
                print(f"  ✓  nmap found: {ip}")
        return ips
    except FileNotFoundError:
        print("  nmap not installed. Install with: sudo apt install nmap")
    except Exception as e:
        print(f"  nmap failed: {e}")
    return []


def save_to_config(ip):
    config_path = Path(__file__).parent / "config.env"
    try:
        text = config_path.read_text()
        lines = []
        for line in text.splitlines():
            if line.startswith("LAMP_IP="):
                lines.append(f"LAMP_IP={ip}")
            else:
                lines.append(line)
        config_path.write_text("\n".join(lines) + "\n")
        print(f"\nSaved LAMP_IP={ip} to config.env")
    except Exception as e:
        print(f"\nCould not auto-save to config.env: {e}")
        print(f"Manually set LAMP_IP={ip} in config.env")


def main():
    local_ip = get_local_ip()
    print(f"=== WiZ Lamp Discovery ===")
    print(f"This Pi's outbound IP: {local_ip}")
    print()

    # Primary: raw UDP broadcast (most reliable, no root needed)
    found = discover_via_udp(timeout=10)

    # Fallback 1: pywizlight discovery
    if not found:
        found = discover_via_pywizlight(timeout=10)

    # Fallback 2: nmap
    if not found:
        nmap_ips = try_nmap(local_ip)
        for ip in nmap_ips:
            found[ip] = "unknown"

    print()
    if not found:
        print("No WiZ lamps found.")
        print()
        print("Troubleshooting tips:")
        print("  • Make sure the lamp is powered on and connected to this WiFi network.")
        print("  • WiZ lamps and this Pi must be on the same subnet.")
        print("  • Try opening the WiZ app to confirm the lamp is online.")
        print("  • If on a managed switch/AP, check that multicast/broadcast is not blocked.")
        sys.exit(1)

    ips = list(found.keys())
    print(f"Found {len(ips)} WiZ lamp(s): {', '.join(ips)}")

    if len(ips) == 1:
        ip = ips[0]
        answer = input(f"\nSave {ip} to config.env as LAMP_IP? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            save_to_config(ip)
    else:
        print("\nMultiple lamps found. Which one should be the controller target?")
        for i, ip in enumerate(ips, 1):
            print(f"  {i}) {ip}  (MAC: {found[ip]})")
        choice = input("Enter number (or press Enter to skip): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(ips):
            save_to_config(ips[int(choice) - 1])
        else:
            print(f"Manually set LAMP_IP=<ip> in config.env")

    print()
    print("Next: sudo systemctl start wiz-lamp   (or: python app.py)")


if __name__ == "__main__":
    main()
