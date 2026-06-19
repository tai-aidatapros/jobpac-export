"""
JDBC/ODBC data exporter — replaces JobPacGetCurrentData.ps1.

Connects to the JobPac IBM i database, runs SELECT * for each table listed in
the configuration, and writes the results as CSV files to a local directory.
Also generates a processing-info summary CSV identical to the original
JobPacTableProcessingInfo.csv.

Backend is selected at runtime via the DB_BACKEND env var ("jdbc" or "odbc").
"""

from __future__ import annotations

import csv
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class TableResult:
    """Result metadata for a single exported table."""

    table_name: str
    row_count: int
    csv_path: str
    elapsed_seconds: float


def _make_connect_fn(
    backend: str,
    host: str,
    database: str,
    username: str,
    password: str,
    jar_path: str,
) -> Callable:
    """Return a zero-argument callable that opens a DB-API 2.0 connection."""
    if backend == "odbc":
        from src.drivers import odbc as _driver
    else:
        from src.drivers import jdbc as _driver

    def _connect():
        return _driver.connect(
            host=host,
            database=database,
            username=username,
            password=password,
            jar_path=jar_path,
        )

    return _connect


def _test_connection(connect_fn: Callable, backend: str) -> None:
    logger.info("Testing %s connectivity …", backend.upper())
    conn = connect_fn()
    conn.close()
    logger.info("%s connectivity OK.", backend.upper())


def _export_single_table(
    connect_fn: Callable,
    table_name: str,
    output_dir: Path,
) -> TableResult:
    t0 = time.monotonic()
    query = f'SELECT * FROM "{table_name}"'  # noqa: S608 — quoted to handle names with # and other special chars
    csv_path = output_dir / f"{table_name}.csv"

    logger.info("Exporting table %s …", table_name)

    BATCH_SIZE = 5_000

    conn = connect_fn()
    try:
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [desc[0] for desc in cursor.description]

        row_count = 0
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            while True:
                batch = cursor.fetchmany(BATCH_SIZE)
                if not batch:
                    break
                writer.writerows(batch)
                row_count += len(batch)
    except Exception:
        csv_path.unlink(missing_ok=True)  # don't leave partial CSV for the uploader to pick up
        raise
    finally:
        conn.close()

    elapsed = time.monotonic() - t0
    logger.info("  → %s: %d rows, %.1fs, saved to %s", table_name, row_count, elapsed, csv_path)

    return TableResult(
        table_name=table_name,
        row_count=row_count,
        csv_path=str(csv_path),
        elapsed_seconds=round(elapsed, 2),
    )


def export_tables(
    connection_string: str,
    tables: list[str],
    output_dir: str,
    *,
    credentials: tuple[str, str],
    jar_path: str,
    max_workers: int = 2,
    backend: str = "jdbc",
    host: str = "",
    database: str = "",
) -> tuple[list[TableResult], list[str]]:
    """
    Export all tables to CSV in parallel and write a processing-info summary.

    Parameters
    ----------
    connection_string:
        JDBC URL — kept for backwards compatibility, not used by the ODBC backend.
    tables:
        List of table names to export.
    output_dir:
        Directory where CSV files will be written.
    credentials:
        (username, password) tuple.
    jar_path:
        Filesystem path to jt400.jar (JDBC only).
    max_workers:
        Number of concurrent connections. Each worker opens its own connection.
    backend:
        "jdbc" or "odbc".
    host:
        IBM i hostname/IP — required for ODBC; JDBC derives it from connection_string.
    database:
        IBM i database/library — required for ODBC; JDBC derives it from connection_string.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # For JDBC, parse host/database out of the JDBC URL if not supplied explicitly.
    if backend == "jdbc" and not host:
        # jdbc:as400://host/database
        parts = connection_string.replace("jdbc:as400://", "").split("/", 1)
        host = parts[0]
        database = parts[1] if len(parts) > 1 else ""

    username, password = credentials
    connect_fn = _make_connect_fn(backend, host, database, username, password, jar_path)

    _test_connection(connect_fn, backend)

    logger.info(
        "Exporting %d table(s) with backend=%s max_workers=%d …",
        len(tables), backend, max_workers,
    )

    results: list[TableResult] = []
    skipped: list[str] = []
    total = len(tables)
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_export_single_table, connect_fn, table_name, out): table_name
            for table_name in tables
        }
        for future in as_completed(futures):
            table_name = futures[future]
            completed += 1
            try:
                results.append(future.result())
            except Exception:
                logger.exception("Skipping table %s — full traceback:", table_name)
                skipped.append(table_name)
            logger.info("Progress: %d/%d tables completed", completed, total)

    if skipped:
        logger.warning("Skipped %d table(s): %s", len(skipped), ", ".join(skipped))

    # Sort results to match original table order for deterministic summary output
    order = {t: i for i, t in enumerate(tables)}
    results.sort(key=lambda r: order[r.table_name])

    summary_path = out / "JobPacTableProcessingInfo.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["TableName", "RowCount", "ElapsedSeconds"])
        for r in results:
            writer.writerow([r.table_name, r.row_count, r.elapsed_seconds])
    logger.info("Processing summary written to %s", summary_path)

    return results, skipped
