"""
Network diagnostics — traces the path from the Fargate container to the on-prem
JobPac DB through the Site-to-Site VPN.

Steps logged:
  1. Container network interfaces and assigned IPs
  2. VPC routing table (ip route) — confirms VPN routes are present
  3. TCP port probe to DB (confirms firewall/SG allows port 449)
"""

from __future__ import annotations

import logging
import re
import socket
import subprocess
import time

logger = logging.getLogger(__name__)

_HEADER = "=" * 56
_DIVIDER = "-" * 56


# ── helpers ────────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 15) -> str:
    """Run a shell command and return combined stdout+stderr."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return f"[timed out after {timeout}s]"
    except FileNotFoundError:
        return f"[command not found: {cmd[0]}]"


def _tcp_probe(host: str, port: int, timeout: float = 5.0) -> tuple[bool, float, str]:
    """Attempt a TCP connection; return (success, latency_ms, error)."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.perf_counter() - start) * 1000, ""
    except socket.timeout:
        return False, (time.perf_counter() - start) * 1000, "timed out"
    except ConnectionRefusedError:
        return False, (time.perf_counter() - start) * 1000, "connection refused"
    except OSError as exc:
        return False, (time.perf_counter() - start) * 1000, str(exc)


# ── parsers ────────────────────────────────────────────────────────────────────

def _parse_interfaces(raw: str) -> list[dict]:
    """Parse `ip addr show` into a list of interface summaries."""
    interfaces: list[dict] = []
    current: dict | None = None

    for line in raw.splitlines():
        # New interface block: "3: eth0@if5: <FLAGS> mtu 1500 ..."
        m = re.match(r"^\d+:\s+(\S+?)(?:@\S+)?:\s+<([^>]*)>.*mtu\s+(\d+)", line)
        if m:
            current = {
                "name": m.group(1),
                "flags": m.group(2).split(","),
                "mtu": int(m.group(3)),
                "ipv4": [],
                "ipv6": [],
            }
            interfaces.append(current)
            continue

        if current is None:
            continue

        m4 = re.match(r"^\s+inet\s+(\S+)\s+scope\s+(\S+)", line)
        if m4:
            current["ipv4"].append({"addr": m4.group(1), "scope": m4.group(2)})

        m6 = re.match(r"^\s+inet6\s+(\S+)\s+scope\s+(\S+)", line)
        if m6:
            current["ipv6"].append({"addr": m6.group(1), "scope": m6.group(2)})

    return interfaces



def _parse_ping(raw: str) -> dict:
    """Extract packet loss and rtt from ping output."""
    result: dict = {"transmitted": 0, "received": 0, "loss_pct": 100, "avg_ms": None}
    m = re.search(r"(\d+) packets transmitted,\s*(\d+) received,\s*([\d.]+)%", raw)
    if m:
        result["transmitted"] = int(m.group(1))
        result["received"] = int(m.group(2))
        result["loss_pct"] = float(m.group(3))
    rtt = re.search(r"rtt [^=]+ = [\d.]+/([\d.]+)/", raw)
    if rtt:
        result["avg_ms"] = float(rtt.group(1))
    return result


# ── steps ──────────────────────────────────────────────────────────────────────

def _step_interfaces() -> None:
    logger.info(_DIVIDER)
    logger.info("STEP 1  Container network interfaces")
    logger.info(_DIVIDER)

    raw = _run(["ip", "addr", "show"])
    interfaces = _parse_interfaces(raw)

    if not interfaces:
        logger.warning("  (no interfaces found — ip addr output was empty)")
        return

    logger.info("  %-12s  %-6s  %-22s  %s", "INTERFACE", "STATE", "IPv4 ADDRESS", "MTU")
    for iface in interfaces:
        state = "UP" if "UP" in iface["flags"] else "DOWN"
        ipv4_addrs = [e["addr"] for e in iface["ipv4"] if e["scope"] != "host"] or [
            e["addr"] for e in iface["ipv4"]
        ]
        addr_str = ", ".join(ipv4_addrs) if ipv4_addrs else "(none)"
        loopback = "LOOPBACK" in iface["flags"]
        note = "[loopback]" if loopback else f"MTU {iface['mtu']}"
        logger.info("  %-12s  %-6s  %-22s  %s", iface["name"], state, addr_str, note)

        for v6 in iface["ipv6"]:
            if v6["scope"] == "host":
                continue
            logger.info("  %-12s  %-6s  %-22s", "", "", v6["addr"])


def _step_routes() -> None:
    logger.info(_DIVIDER)
    logger.info("STEP 2  Default gateway")
    logger.info(_DIVIDER)

    raw = _run(["ip", "route"])
    default = next((l.strip() for l in raw.splitlines() if l.startswith("default")), None)
    if default:
        logger.info("  %s", default)
    else:
        logger.warning("  [WARN] No default gateway found")


def _step_ping(db_host: str) -> None:
    logger.info(_DIVIDER)
    logger.info("STEP 3  Ping %s", db_host)
    logger.info(_DIVIDER)

    raw = _run(["ping", "-c", "3", "-W", "2", db_host], timeout=15)
    p = _parse_ping(raw)

    loss = p["loss_pct"]
    rx, tx = p["received"], p["transmitted"]

    if loss == 0 and p["avg_ms"] is not None:
        logger.info(
            "  [OK]   %d/%d packets received  —  avg RTT %.1f ms",
            rx, tx, p["avg_ms"],
        )
    elif loss == 100:
        logger.warning(
            "  [FAIL] %d/%d packets received  —  100%% packet loss  "
            "(host unreachable or ICMP blocked)",
            rx, tx,
        )
    else:
        logger.warning(
            "  [WARN] %d/%d packets received  —  %.0f%% packet loss",
            rx, tx, loss,
        )


def _step_tcp(db_host: str, db_port: int) -> None:
    logger.info(_DIVIDER)
    logger.info("STEP 3  TCP probe  %s:%d", db_host, db_port)
    logger.info(_DIVIDER)

    success, latency_ms, error = _tcp_probe(db_host, db_port)
    if success:
        logger.info("  [OK]   Port %d reachable  —  %.1f ms", db_port, latency_ms)
    else:
        # AS/400 drops bare TCP (no DDM handshake), so a timeout here is
        # expected even when VPN and firewall are healthy; JDBC will confirm.
        logger.warning(
            "  [WARN] Port %d %s (%.0f ms)  "
            "—  normal for AS/400; JDBC will confirm actual DB access",
            db_port, error, latency_ms,
        )


# ── public entry point ─────────────────────────────────────────────────────────

def run(db_host: str, db_port: int = 449) -> None:
    """
    Run all diagnostics and log results.  Never raises — failures are logged
    as warnings so the main export can still proceed.
    """
    logger.info(_HEADER)
    logger.info("  NETWORK DIAGNOSTICS  —  target: %s:%d", db_host, db_port)
    logger.info(_HEADER)

    _step_interfaces()
    _step_routes()
    _step_tcp(db_host, db_port)

    logger.info(_DIVIDER)
    logger.info("  Network diagnostics complete")
    logger.info(_HEADER)
