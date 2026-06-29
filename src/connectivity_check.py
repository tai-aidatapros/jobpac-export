"""
TCP connectivity probe for the on-prem JobPac DB2 server.

Usage:
    python -m src.connectivity_check [--host HOST] [--port PORT] [--timeout SECONDS]

Defaults to the production DB endpoint: 10.128.13.219:449
"""

import argparse
import socket
import sys
import time
from datetime import datetime, timezone


TARGET_HOST = "10.128.13.219"
TARGET_PORT = 449
DEFAULT_TIMEOUT = 5
PROBE_COUNT = 3


def probe(host: str, port: int, timeout: float) -> tuple[bool, float, str]:
    """Attempt a TCP connection; return (success, latency_ms, error_msg)."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency = (time.perf_counter() - start) * 1000
            return True, latency, ""
    except socket.timeout:
        latency = (time.perf_counter() - start) * 1000
        return False, latency, "Connection timed out"
    except ConnectionRefusedError:
        latency = (time.perf_counter() - start) * 1000
        return False, latency, "Connection refused"
    except OSError as exc:
        latency = (time.perf_counter() - start) * 1000
        return False, latency, str(exc)


def run_report(host: str, port: int, timeout: float, count: int) -> int:
    """Run probes and print a formatted report. Returns exit code (0=all ok)."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print("=" * 60)
    print("  JobPac DB Connectivity Check")
    print("=" * 60)
    print(f"  Target  : {host}:{port}")
    print(f"  Timeout : {timeout}s per probe")
    print(f"  Probes  : {count}")
    print(f"  Time    : {timestamp}")
    print("-" * 60)

    results = []
    for i in range(1, count + 1):
        success, latency_ms, error = probe(host, port, timeout)
        results.append((success, latency_ms, error))
        status = "OK" if success else "FAIL"
        latency_str = f"{latency_ms:.1f} ms"
        detail = "" if success else f"  ({error})"
        print(f"  Probe {i}/{count}: {status:<6}  {latency_str}{detail}")

    print("-" * 60)

    successful = [r for r in results if r[0]]
    failed = [r for r in results if not r[0]]
    success_rate = len(successful) / count * 100

    if successful:
        latencies = [r[1] for r in successful]
        avg_ms = sum(latencies) / len(latencies)
        min_ms = min(latencies)
        max_ms = max(latencies)
        print(f"  Latency : min={min_ms:.1f}ms  avg={avg_ms:.1f}ms  max={max_ms:.1f}ms")
    else:
        print("  Latency : N/A (all probes failed)")

    print(f"  Result  : {len(successful)}/{count} probes succeeded ({success_rate:.0f}%)")

    if len(successful) == count:
        verdict = "REACHABLE"
        verdict_msg = "All probes succeeded — VPN tunnel and DB port are open."
    elif successful:
        verdict = "INTERMITTENT"
        verdict_msg = f"{len(failed)} probe(s) failed — check VPN stability."
    else:
        verdict = "UNREACHABLE"
        first_error = failed[0][2]
        verdict_msg = f"All probes failed ({first_error}) — VPN or firewall issue."

    print(f"  Verdict : {verdict}")
    print(f"            {verdict_msg}")
    print("=" * 60)

    return 0 if verdict == "REACHABLE" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="TCP connectivity check for JobPac DB")
    parser.add_argument("--host", default=TARGET_HOST, help="Target hostname or IP")
    parser.add_argument("--port", type=int, default=TARGET_PORT, help="Target TCP port")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Probe timeout in seconds")
    parser.add_argument("--count", type=int, default=PROBE_COUNT, help="Number of probes to send")
    args = parser.parse_args()

    sys.exit(run_report(args.host, args.port, args.timeout, args.count))


if __name__ == "__main__":
    main()
