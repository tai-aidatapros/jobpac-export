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
import datetime
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _format_value(v: object, force_text: bool = False) -> object:
    # datetime must be checked before date (datetime is a subclass of date)
    if isinstance(v, datetime.datetime):
        return v.strftime("%-m/%-d/%Y %I:%M:%S %p")
    if isinstance(v, datetime.date):
        return v.strftime("%-m/%-d/%Y 12:00:00 AM")
    # jaydebeapi may return IBM i date/timestamp columns as ISO strings
    if isinstance(v, str):
        if _ISO_DATETIME_RE.match(v):
            try:
                return datetime.datetime.fromisoformat(v[:19]).strftime("%-m/%-d/%Y %I:%M:%S %p")
            except ValueError:
                pass
        elif _ISO_DATE_RE.match(v):
            try:
                return datetime.date.fromisoformat(v).strftime("%-m/%-d/%Y 12:00:00 AM")
            except ValueError:
                pass
    if force_text:
        return "" if v is None else str(v)
    return v


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


BATCH_SIZE = 5_000

_RETRYABLE_CONN_PHRASES = (
    "connection does not exist",
    "connection reset",
    "broken pipe",
    "socket closed",
    "communication link failure",
)

_MAX_TABLE_RETRIES = 2
_RETRY_DELAY_SECONDS = 10


def _is_connection_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _RETRYABLE_CONN_PHRASES)


def _export_table_chunked(
    connect_fn: Callable,
    table_name: str,
    output_dir: Path,
    chunk_size: int,
    *,
    force_text: bool = False,
    stop_event: Optional[threading.Event] = None,
    exclude_columns: Optional[list[str]] = None,
) -> TableResult:
    """
    Export a large table by splitting it into RRN-range chunks.

    Each chunk opens its own fresh JDBC connection and queries:
        SELECT * FROM "<table>" WHERE RRN("<table>") BETWEEN <start> AND <end>

    This keeps individual connections short-lived (minutes, not hours), avoiding
    server-side job-timeout drops that kill multi-hour single-query exports.
    """
    t0 = time.monotonic()
    csv_path = output_dir / f"{table_name}.csv"
    quoting = csv.QUOTE_ALL if force_text else csv.QUOTE_MINIMAL

    # Setup connection: discover schema and physical record count.
    # Two separate cursors avoid leaving an open ResultSet when issuing the second query,
    # which some jt400 driver versions reject with "result set already open".
    logger.info("Exporting table %s (chunked, chunk_size=%d) — querying max RRN …", table_name, chunk_size)
    setup_conn = connect_fn()
    try:
        schema_cur = setup_conn.cursor()
        schema_cur.execute(f'SELECT * FROM "{table_name}" FETCH FIRST 1 ROW ONLY WITH UR')
        schema_cur.fetchall()  # consume the result set before closing
        excl = set(c.upper() for c in (exclude_columns or []))
        columns = [desc[0] for desc in schema_cur.description if desc[0].upper() not in excl]
        if excl:
            logger.info("  %s: excluding %d column(s): %s", table_name, len(excl), ", ".join(sorted(excl)))

        rrn_cur = setup_conn.cursor()
        rrn_cur.execute(f'SELECT MAX(RRN("{table_name}")) FROM "{table_name}" WITH UR')
        raw = rrn_cur.fetchone()
        max_rrn = int(raw[0]) if (raw and raw[0] is not None) else 0
    finally:
        setup_conn.close()

    total_chunks = max(1, (max_rrn + chunk_size - 1) // chunk_size) if max_rrn else 0
    logger.info(
        "  %s: max_rrn=%d, chunk_size=%d → %d chunk(s) to export",
        table_name, max_rrn, chunk_size, total_chunks,
    )

    row_count = 0
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, quoting=quoting)
            writer.writerow(columns)

            chunk_num = 0
            start_rrn = 1
            while start_rrn <= max_rrn:
                if stop_event is not None and stop_event.is_set():
                    raise InterruptedError(f"Export of {table_name} cancelled.")

                end_rrn = start_rrn + chunk_size - 1
                chunk_num += 1
                col_list = ", ".join(f'"{c}"' for c in columns)
                query = (
                    f'SELECT {col_list} FROM "{table_name}" '
                    f'WHERE RRN("{table_name}") BETWEEN {start_rrn} AND {end_rrn} WITH UR'
                )
                chunk_rows = 0
                conn = connect_fn()
                try:
                    cur = conn.cursor()
                    cur.execute(query)
                    while True:
                        if stop_event is not None and stop_event.is_set():
                            raise InterruptedError(f"Export of {table_name} cancelled.")
                        batch = cur.fetchmany(BATCH_SIZE)
                        if not batch:
                            break
                        for r in batch:
                            writer.writerow([_format_value(v, force_text) for v in r])
                        chunk_rows += len(batch)
                finally:
                    conn.close()

                row_count += chunk_rows
                logger.info(
                    "  %s chunk %d/%d (RRN %d–%d): %d rows",
                    table_name, chunk_num, total_chunks, start_rrn, end_rrn, chunk_rows,
                )
                start_rrn = end_rrn + 1
    except Exception:
        csv_path.unlink(missing_ok=True)
        raise

    elapsed = time.monotonic() - t0
    logger.info("  → %s: %d rows, %.1fs, saved to %s", table_name, row_count, elapsed, csv_path)
    return TableResult(
        table_name=table_name,
        row_count=row_count,
        csv_path=str(csv_path),
        elapsed_seconds=round(elapsed, 2),
    )


def _export_single_table(
    connect_fn: Callable,
    table_name: str,
    output_dir: Path,
    *,
    force_text: bool = False,
    stop_event: Optional[threading.Event] = None,
    chunk_size: Optional[int] = None,
    exclude_columns: Optional[list[str]] = None,
) -> TableResult:
    if chunk_size is not None:
        return _export_table_chunked(
            connect_fn, table_name, output_dir, chunk_size,
            force_text=force_text, stop_event=stop_event,
            exclude_columns=exclude_columns,
        )

    t0 = time.monotonic()
    csv_path = output_dir / f"{table_name}.csv"

    excl = set(c.upper() for c in (exclude_columns or []))
    if excl:
        # Discover all column names first so we can build an explicit SELECT list.
        _setup = connect_fn()
        try:
            _cur = _setup.cursor()
            _cur.execute(f'SELECT * FROM "{table_name}" FETCH FIRST 1 ROW ONLY WITH UR')
            _cur.fetchall()
            all_cols = [desc[0] for desc in _cur.description]
        finally:
            _setup.close()
        kept = [c for c in all_cols if c.upper() not in excl]
        logger.info("  %s: excluding %d column(s): %s", table_name, len(excl), ", ".join(sorted(excl)))
        col_list = ", ".join(f'"{c}"' for c in kept)
        query = f'SELECT {col_list} FROM "{table_name}" WITH UR'  # noqa: S608
    else:
        query = f'SELECT * FROM "{table_name}" WITH UR'  # noqa: S608

    logger.info("Exporting table %s …", table_name)

    last_exc: BaseException | None = None
    for attempt in range(1, _MAX_TABLE_RETRIES + 2):  # attempts: 1, 2, 3
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError(f"Export of {table_name} cancelled by user.")
        if attempt > 1:
            logger.warning(
                "Retrying %s (attempt %d/%d) after connection error: %s",
                table_name, attempt, _MAX_TABLE_RETRIES + 1, last_exc,
            )
            time.sleep(_RETRY_DELAY_SECONDS)

        conn = connect_fn()
        try:
            cursor = conn.cursor()
            cursor.execute(query)
            columns = [desc[0] for desc in cursor.description]

            row_count = 0
            quoting = csv.QUOTE_ALL if force_text else csv.QUOTE_MINIMAL
            with open(csv_path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh, quoting=quoting)
                writer.writerow(columns)
                while True:
                    if stop_event is not None and stop_event.is_set():
                        raise InterruptedError(f"Export of {table_name} cancelled by user.")
                    batch = cursor.fetchmany(BATCH_SIZE)
                    if not batch:
                        break
                    for row in batch:
                        writer.writerow([_format_value(v, force_text) for v in row])
                    row_count += len(batch)
        except InterruptedError:
            csv_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            csv_path.unlink(missing_ok=True)  # don't leave partial CSV for the uploader to pick up
            if _is_connection_error(exc) and attempt <= _MAX_TABLE_RETRIES:
                last_exc = exc
                continue
            raise
        finally:
            conn.close()
        break  # success

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
    force_text: bool = False,
    on_table_complete: Optional[Callable[[TableResult], None]] = None,
    table_chunk_sizes: Optional[dict[str, int]] = None,
    table_exclude_columns: Optional[dict[str, list[str]]] = None,
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
    on_table_complete:
        Optional callback invoked after each table is successfully exported to CSV.
        Called with the TableResult in the main thread (the as_completed loop).
        Exceptions raised by the callback are logged and suppressed.
    table_chunk_sizes:
        Optional mapping of table_name → chunk_size. Tables listed here are exported
        using RRN-based chunked queries instead of a single long-running SELECT *.
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
    exported_names: set[str] = set()
    stop_event = threading.Event()

    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        _chunk_sizes = table_chunk_sizes or {}
        _excl_cols = table_exclude_columns or {}
        futures = {
            pool.submit(
                _export_single_table,
                connect_fn,
                table_name,
                out,
                force_text=force_text,
                stop_event=stop_event,
                chunk_size=_chunk_sizes.get(table_name),
                exclude_columns=_excl_cols.get(table_name),
            ): table_name
            for table_name in tables
        }
        for future in as_completed(futures):
            table_name = futures[future]
            completed += 1
            try:
                result = future.result(timeout=600)
                results.append(result)
                exported_names.add(table_name)
                if on_table_complete is not None:
                    try:
                        on_table_complete(result)
                    except Exception:
                        logger.exception("on_table_complete callback failed for %s", table_name)
            except FutureTimeoutError:
                logger.error("Skipping table %s — timed out after 600s (connection likely stalled)", table_name)
                skipped.append(table_name)
            except InterruptedError:
                logger.warning("Skipping table %s — cancelled.", table_name)
                skipped.append(table_name)
            except Exception:
                logger.exception("Skipping table %s — full traceback:", table_name)
                skipped.append(table_name)
            logger.info("Progress: %d/%d tables completed", completed, total)
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt received — signalling workers to stop …")
        stop_event.set()
        for f in futures:
            f.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        uncompleted = [t for t in tables if t not in exported_names and t not in skipped]
        logger.warning(
            "Export interrupted — exported: %d, skipped: %d, uncompleted: %d%s",
            len(results),
            len(skipped),
            len(uncompleted),
            f" ({', '.join(uncompleted)})" if uncompleted else "",
        )
        raise
    except BaseException:
        stop_event.set()
        pool.shutdown(wait=False, cancel_futures=True)
        uncompleted = [t for t in tables if t not in exported_names and t not in skipped]
        logger.warning(
            "Export interrupted — exported: %d, skipped: %d, uncompleted: %d%s",
            len(results),
            len(skipped),
            len(uncompleted),
            f" ({', '.join(uncompleted)})" if uncompleted else "",
        )
        raise
    else:
        pool.shutdown(wait=True)

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
