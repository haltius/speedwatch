"""
network_check.py — independent, third-party evidence of which network the
container's public IP actually belongs to.

This deliberately does NOT rely on self-reported labels. It gathers three
things you can't fabricate by typing a value into an env var:

1. WHOIS on the public IP — the regional internet registry's (ARIN's)
   authoritative record of who that IP block is allocated to.
2. Reverse DNS (PTR) — the ISP's own DNS answering "whose IP is this".
3. Traceroute — the actual network path, whose early hops are normally
   inside the ISP's own infrastructure and often named accordingly.

Each is a fact reported by a system outside your control (the registry,
the DNS, the network path itself) — which is what makes it usable as
evidence, unlike a field you set yourself.
"""

import subprocess
import socket
import re


def reverse_dns(ip: str) -> str | None:
    try:
        hostname, _, _ = socket.gethostbyaddr(ip)
        return hostname
    except Exception:
        return None


def run_whois(ip: str, timeout: int = 15) -> dict:
    """Run whois against the public IP and pull out the org/net fields."""
    try:
        proc = subprocess.run(
            ["whois", ip], capture_output=True, text=True, timeout=timeout
        )
        raw = proc.stdout.strip()
    except Exception as e:
        return {"raw": None, "org": None, "error": str(e)}

    org = None
    for pattern in (r"OrgName:\s*(.+)", r"org-name:\s*(.+)", r"descr:\s*(.+)", r"owner:\s*(.+)"):
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            org = m.group(1).strip()
            break

    return {"raw": raw, "org": org, "error": None}


def run_traceroute(target: str = "1.1.1.1", max_hops: int = 8, timeout: int = 30) -> dict:
    """
    Traceroute out from the container. The first 1-3 hops (after the local
    Docker gateway) are normally inside the ISP's own network and often
    carry hostnames identifying it.
    """
    try:
        proc = subprocess.run(
            ["traceroute", "-m", str(max_hops), "-q", "1", "-w", "2", target],
            capture_output=True, text=True, timeout=timeout,
        )
        raw = proc.stdout.strip()
    except Exception as e:
        return {"raw": None, "error": str(e)}
    return {"raw": raw, "error": None}


def gather_network_evidence(public_ip: str | None) -> dict:
    """Collect all three evidence sources for the given public IP."""
    result = {
        "public_ip": public_ip,
        "reverse_dns": None,
        "whois_org": None,
        "whois_raw": None,
        "traceroute_raw": None,
    }
    if not public_ip:
        return result

    result["reverse_dns"] = reverse_dns(public_ip)

    whois_res = run_whois(public_ip)
    result["whois_org"] = whois_res.get("org")
    result["whois_raw"] = whois_res.get("raw")

    tr_res = run_traceroute()
    result["traceroute_raw"] = tr_res.get("raw")

    return result
