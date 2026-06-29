#!/usr/bin/env python3
"""
Diagnose one or all JobPac AS/400 tables: row count, column schema, max RRN,
and a throughput probe to suggest a table-specific ChunkSize.

Usage (requires the same env vars as src/main.py):
    PYTHONPATH=. python3 scripts/diagnose_table.py CCTRNCTP
    PYTHONPATH=. python3 scripts/diagnose_table.py --all
    make diagnose TABLE=CCTRNCTP
    make diagnose-all
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROBE_ROWS = 100      # rows fetched in the throughput probe (kept small for wide/LOB tables)
TARGET_SECS = 300     # desired max seconds per chunk (5 minutes)
MIN_CHUNK   = 1_000
ROUND_TO    = 1_000


def _round_to(n: int, multiple: int) -> int:
    return max(multiple, (n // multiple) * multiple)


@dataclass
class TableStats:
    name: str
    row_count: int | None = None
    max_rrn: int | None = None
    rows_per_sec: float | None = None
    col_count: int | None = None
    suggested_chunk: int | None = None
    error: str | None = None


def _probe_table(table_name: str, connect) -> TableStats:
    stats = TableStats(name=table_name)
    try:
        # Row count — WITH UR skips lock waits (uncommitted read)
        print("  [1/4] connecting …", flush=True)
        conn = connect()
        try:
            print(f"  [1/4] SELECT COUNT(*) FROM \"{table_name}\" WITH UR …", flush=True)
            cur = conn.cursor()
            cur.execute(f'SELECT COUNT(*) FROM "{table_name}" WITH UR')
            stats.row_count = int(cur.fetchone()[0])
            print(f"  [1/4] done — {stats.row_count:,} rows", flush=True)
        finally:
            conn.close()

        # Schema + max RRN
        print("  [2/4] connecting …", flush=True)
        conn = connect()
        try:
            print(f"  [2/4] FETCH FIRST 1 ROW (schema) …", flush=True)
            schema_cur = conn.cursor()
            schema_cur.execute(f'SELECT * FROM "{table_name}" FETCH FIRST 1 ROW ONLY WITH UR')
            schema_cur.fetchall()
            stats.col_count = len(schema_cur.description)
            print(f"  [2/4] done — {stats.col_count} columns:", flush=True)
            for desc in schema_cur.description:
                print(f"         {str(desc[0]):<40} {desc[1]}", flush=True)

            print(f"  [3/4] MAX(RRN) …", flush=True)
            rrn_cur = conn.cursor()
            rrn_cur.execute(f'SELECT MAX(RRN("{table_name}")) FROM "{table_name}" WITH UR')
            raw = rrn_cur.fetchone()
            stats.max_rrn = int(raw[0]) if (raw and raw[0] is not None) else 0
            print(f"  [3/4] done — max_rrn={stats.max_rrn:,}", flush=True)
        finally:
            conn.close()

        # Throughput probe
        probe = min(PROBE_ROWS, stats.row_count or 0)
        if probe > 0 and stats.max_rrn:
            print(f"  [4/4] throughput probe ({probe:,} rows) …", flush=True)
            print(f"  [4/4] connecting …", flush=True)
            conn = connect()
            try:
                cur = conn.cursor()
                print(f"  [4/4] executing query …", flush=True)
                cur.execute(
                    f'SELECT * FROM "{table_name}" '
                    f'WHERE RRN("{table_name}") BETWEEN 1 AND {stats.max_rrn} '
                    f'FETCH FIRST {probe} ROWS ONLY WITH UR'
                )
                print(f"  [4/4] fetching first batch …", flush=True)
                t0 = time.monotonic()
                fetched = 0
                while fetched < probe:
                    batch = cur.fetchmany(10)
                    if not batch:
                        break
                    fetched += len(batch)
                    print(f"  [4/4] fetched {fetched:,} rows so far …", flush=True)
                elapsed = time.monotonic() - t0
                if elapsed > 0:
                    stats.rows_per_sec = fetched / elapsed
                print(f"  [4/4] done — {stats.rows_per_sec:,.0f} rows/sec", flush=True)
            finally:
                conn.close()

        if stats.rows_per_sec and stats.rows_per_sec > 0:
            raw_chunk = int(stats.rows_per_sec * TARGET_SECS)
            stats.suggested_chunk = _round_to(raw_chunk, ROUND_TO)

    except Exception as exc:
        stats.error = str(exc)

    return stats


def _print_detail(stats: TableStats) -> None:
    print(f"\n=== {stats.name} ===")
    if stats.error:
        print(f"  ERROR: {stats.error}")
        return
    print(f"  Rows        : {stats.row_count:>12,}")
    print(f"  Max RRN     : {stats.max_rrn:>12,}")
    print(f"  Columns     : {stats.col_count:>12,}")
    if stats.rows_per_sec:
        chunk = stats.suggested_chunk or 0
        mins = chunk / stats.rows_per_sec / 60 if stats.rows_per_sec else 0
        print(f"  Rows/sec    : {stats.rows_per_sec:>12,.0f}  (probe of {PROBE_ROWS:,} rows)")
        print(f"  Chunk size  : {chunk:>12,}  (~{mins:.1f} min/chunk)")
    print()


def _print_summary(all_stats: list[TableStats]) -> None:
    col = [35, 12, 12, 12, 12, 12]
    header = (
        f"{'Table':<{col[0]}} {'Rows':>{col[1]}} {'MaxRRN':>{col[2]}} "
        f"{'Cols':>{col[3]}} {'Rows/sec':>{col[4]}} {'ChunkSize':>{col[5]}}"
    )
    sep = "-" * sum(col + [5])
    print(f"\n{'='*len(sep)}")
    print("SUMMARY — suggested ChunkSize per table")
    print(f"{'='*len(sep)}")
    print(header)
    print(sep)

    csv_lines: list[str] = ["TableName,ChunkSize,,,"]
    for s in all_stats:
        if s.error:
            row = f"{'  ' + s.name:<{col[0]}} {'ERROR':>{col[1]}}"
            csv_lines.append(f"{s.name},,,,  # ERROR: {s.error}")
        else:
            rps = f"{s.rows_per_sec:,.0f}" if s.rows_per_sec else "—"
            chunk = f"{s.suggested_chunk:,}" if s.suggested_chunk else "—"
            row = (
                f"  {s.name:<{col[0]-2}} {(s.row_count or 0):>{col[1]},} "
                f"{(s.max_rrn or 0):>{col[2]},} {(s.col_count or 0):>{col[3]},} "
                f"{rps:>{col[4]}} {chunk:>{col[5]}}"
            )
            chunk_val = s.suggested_chunk or ""
            csv_lines.append(f"{s.name},{chunk_val},,,")
        print(row)

    print(sep)
    print("\nSuggested config/tables.csv entries:\n")
    for line in csv_lines:
        print(f"  {line}")
    print()


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} TABLE_NAME | --all", file=sys.stderr)
        sys.exit(1)

    from src.config import load_config
    from src.drivers import jdbc as _jdbc

    config = load_config()
    creds = config.odbc_creds

    def connect():
        return _jdbc.connect(
            host=creds.host,
            database=creds.database,
            username=creds.username,
            password=creds.password,
            jar_path=creds.jar_path,
        )

    if sys.argv[1] == "--all":
        tables = config.tables
        if not tables:
            print("No tables found in config.", file=sys.stderr)
            sys.exit(1)
        print(f"\nProbing {len(tables)} table(s) — this may take a few minutes …")
        all_stats = []
        for i, table_name in enumerate(tables, 1):
            print(f"  [{i}/{len(tables)}] {table_name} …", end=" ", flush=True)
            s = _probe_table(table_name, connect)
            rps = f"{s.rows_per_sec:,.0f} rows/s" if s.rows_per_sec else (s.error or "empty")
            print(rps)
            all_stats.append(s)
        _print_summary(all_stats)
    else:
        table_name = sys.argv[1]
        print(f"\nProbing {table_name} …")
        stats = _probe_table(table_name, connect)
        _print_detail(stats)
        if stats.suggested_chunk:
            print(f"  Add to config/tables.csv:  {table_name},{stats.suggested_chunk},,,")
        elif not stats.error:
            print(f"  Table is small/empty — no ChunkSize needed.")
        print()


if __name__ == "__main__":
    main()
