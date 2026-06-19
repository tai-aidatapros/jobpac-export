"""
Orchestrator entry point — replaces TaskJobPac.ps1.

Run with:
    python -m src.main

Workflow:
    1. Load configuration (env vars + Secrets Manager)
    2. Test ODBC connectivity → on failure, send alert email & exit(1)
    3. Export all tables to CSV in a temporary directory
    4. Upload all CSVs to S3
    5. Send success notification email
    6. Exit(0)

Any unhandled exception triggers a failure notification before re-raising.
"""

from __future__ import annotations

import logging
import socket
import sys
import tempfile
import traceback

from src.config import load_config
from src.exporter import export_tables
from src.notifier import send_notification
from src.uploader import upload_csvs

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("jobpac-export")

_DB_PORT = 446
_CONNECT_TIMEOUT = 5  # seconds


def _check_db_reachable(host: str, port: int = _DB_PORT, timeout: int = _CONNECT_TIMEOUT) -> bool:
    """TCP handshake to confirm the DB host is reachable before starting the export."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    """
    Main entry point.  Returns 0 on success, 1 on failure.
    """
    config = None
    try:
        # ── 1. Load config ─────────────────────────────────────────────
        logger.info("=== JobPac Data Export started ===")
        config = load_config()
        logger.info(
            "Exporting %d table(s) → s3://%s/%s",
            len(config.tables),
            config.s3_bucket,
            config.s3_prefix,
        )

        # ── 2. Connectivity check ──────────────────────────────────────
        host = config.odbc_creds.host
        logger.info("Checking connectivity to %s:%d …", host, _DB_PORT)
        if not _check_db_reachable(host):
            msg = f"Cannot reach JobPac DB at {host}:{_DB_PORT} — VPN may be down."
            logger.error(msg)
            if config.notification_recipients:
                send_notification(
                    config,
                    subject="Jobpac Refresh — DB Unreachable",
                    body=f"<h3>Jobpac DB Unreachable</h3><p>{msg}</p>",
                )
            return 1
        logger.info("Connectivity OK — proceeding with export.")

        # ── 3. Export tables to temp directory ─────────────────────────
        with tempfile.TemporaryDirectory(prefix="jobpac_") as tmp_dir:
            results, skipped = export_tables(
                connection_string=config.odbc_creds.jdbc_url,
                tables=config.tables,
                output_dir=tmp_dir,
                credentials=(config.odbc_creds.username, config.odbc_creds.password),
                jar_path=config.odbc_creds.jar_path,
                max_workers=config.max_workers,
                backend=config.db_backend,
                host=config.odbc_creds.host,
                database=config.odbc_creds.database,
            )

            total_rows = sum(r.row_count for r in results)
            logger.info(
                "Export complete: %d table(s), %d total row(s), %d skipped.",
                len(results),
                total_rows,
                len(skipped),
            )

            # ── 4. Upload to S3 ────────────────────────────────────────
            uploaded_keys = upload_csvs(
                local_dir=tmp_dir,
                bucket=config.s3_bucket,
                prefix=config.s3_prefix,
                region=config.aws_region,
            )
            logger.info("Uploaded %d file(s) to S3.", len(uploaded_keys))

        # ── 5. Success notification ────────────────────────────────────
        success_lines = "\n".join(
            f"  ✔ {r.table_name}: {r.row_count:,} rows ({r.elapsed_seconds:.1f}s)"
            for r in results
        )
        skipped_lines = "\n".join(f"  ✘ {t}: failed" for t in skipped)

        body = (
            f"<h3>Jobpac Refresh Completed</h3>"
            f"<p>Exported <b>{len(results)}</b> table(s) ({total_rows:,} total rows) "
            f"to <code>s3://{config.s3_bucket}/{config.s3_prefix}</code>"
            + (f" — <b>{len(skipped)} failed</b>" if skipped else "")
            + f"</p>"
            f"<pre>{success_lines}"
            + (f"\n\nFailed:\n{skipped_lines}" if skipped else "")
            + "</pre>"
        )

        if config.notification_recipients:
            send_notification(config, subject="Jobpac Refresh", body=body)

        logger.info("=== JobPac Data Export completed successfully ===")
        return 0

    except Exception:
        # ── Failure notification ───────────────────────────────────────
        error_detail = traceback.format_exc()
        logger.error("=== JobPac Data Export FAILED ===\n%s", error_detail)

        if config and config.notification_recipients:
            try:
                body = (
                    f"<h3>Jobpac Database Access Failed</h3>"
                    f"<pre>{error_detail}</pre>"
                )
                send_notification(config, subject="Jobpac Refresh", body=body)
            except Exception:
                logger.exception("Failed to send failure notification email.")

        return 1


if __name__ == "__main__":
    sys.exit(main())
