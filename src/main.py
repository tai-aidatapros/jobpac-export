"""
Orchestrator entry point — replaces TaskJobPac.ps1.

Run with:
    python -m src.main

Workflow:
    1. Load configuration (env vars + Secrets Manager)
    2. Test DB TCP connectivity → on failure, send alert email & exit(1)
    3. Test S3 bucket accessibility → on failure, send alert email & exit(1)
    4. Export all tables to CSV in a temporary directory
    5. Upload all CSVs to S3
    6. Send success notification email
    7. Exit(0)

Any unhandled exception triggers a failure notification before re-raising.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.config import load_config
from src.exporter import TableResult, export_tables
from src import network_diagnostics
from src.notifier import send_notification
from src.uploader import make_run_prefix, update_latest_pointers, upload_single_csv

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

_DB_PORT = 449


def _check_s3_reachable(bucket: str, region: str) -> bool:
    """HeadBucket call to confirm S3 bucket is accessible before starting the export."""
    try:
        boto3.client("s3", region_name=region).head_bucket(Bucket=bucket)
        return True
    except (BotoCoreError, ClientError):
        return False


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export JobPac IBM i tables to S3.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        metavar="N",
        help="Number of parallel DB connections (overrides MAX_WORKERS env var).",
    )
    parser.add_argument(
        "--tables",
        type=str,
        default=None,
        metavar="TABLE[,TABLE...]",
        help="Comma-separated list of tables to export (overrides tables.csv).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """
    Main entry point.  Returns 0 on success, 1 on failure.
    """
    args = _parse_args(argv)
    config = None
    uploaded_results: list[TableResult] = []
    upload_failed: list[str] = []
    job_start = datetime.now(timezone.utc)
    t0 = time.monotonic()
    try:
        # ── 1. Load config ─────────────────────────────────────────────
        logger.info("=== JobPac Data Export started at %s ===", job_start.strftime("%Y-%m-%dT%H:%M:%S UTC"))
        config = load_config()
        if args.max_workers is not None:
            config.max_workers = args.max_workers
            logger.info("max_workers overridden by CLI: %d", config.max_workers)
        if args.tables is not None:
            override = [t.strip() for t in args.tables.split(",") if t.strip()]
            logger.info("Tables overridden by CLI: %s", ", ".join(override))
            config.tables = override
        logger.info(
            "Exporting %d table(s) → s3://%s/%s",
            len(config.tables),
            config.s3_bucket,
            config.s3_prefix,
        )

        # ── 2. Network diagnostics — trace path through VPN ───────────────
        host = config.odbc_creds.host
        network_diagnostics.run(db_host=host, db_port=_DB_PORT)

        # ── 3. DB connection (AS/400 drops bare TCP so no pre-check) ──────
        logger.info("Connecting to DB at %s:%d …", host, _DB_PORT)

        # ── 4. S3 connectivity check ───────────────────────────────────
        logger.info("Checking S3 bucket s3://%s …", config.s3_bucket)
        if not _check_s3_reachable(config.s3_bucket, config.aws_region):
            msg = (
                f"Cannot access S3 bucket '{config.s3_bucket}' — "
                "check IAM permissions, bucket name, or network connectivity."
            )
            logger.error(msg)
            if config.notification_recipients:
                send_notification(
                    config,
                    subject="Jobpac Refresh — S3 Unreachable",
                    body=f"<h3>S3 Unreachable</h3><p>{msg}</p>",
                )
            return 1
        logger.info("S3 connectivity OK — proceeding with export.")

        # ── 4. Export tables, uploading each one to S3 as it completes ─
        run_prefix = make_run_prefix(config.s3_prefix)

        def _upload_table(result: TableResult) -> None:
            try:
                upload_single_csv(
                    csv_path=result.csv_path,
                    bucket=config.s3_bucket,
                    run_prefix=run_prefix,
                    region=config.aws_region,
                )
                uploaded_results.append(result)
            except Exception:
                logger.exception("Failed to upload %s to S3.", result.table_name)
                upload_failed.append(result.table_name)

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
                force_text=config.csv_force_text,
                on_table_complete=_upload_table,
                table_chunk_sizes=config.table_chunk_sizes,
                table_exclude_columns=config.table_exclude_columns,
            )

            total_rows = sum(r.row_count for r in results)
            logger.info(
                "Export complete: %d table(s), %d total row(s), %d skipped.",
                len(results),
                total_rows,
                len(skipped),
            )

            # ── 5. Write summary CSV and upload it ─────────────────────
            summary_path = Path(tmp_dir) / "JobPacTableProcessingInfo.csv"
            try:
                upload_single_csv(
                    csv_path=summary_path,
                    bucket=config.s3_bucket,
                    run_prefix=run_prefix,
                    region=config.aws_region,
                )
            except Exception:
                logger.warning("Failed to upload summary CSV — table data already in S3.", exc_info=True)

            # ── 6. Update 'latest' pointer for all successfully uploaded tables ─
            all_uploaded_paths = [r.csv_path for r in uploaded_results]
            try:
                update_latest_pointers(
                    csv_paths=all_uploaded_paths,
                    bucket=config.s3_bucket,
                    prefix=config.s3_prefix,
                    region=config.aws_region,
                )
            except Exception:
                logger.warning("Failed to update latest/ pointers — run data already in S3.", exc_info=True)

            uploaded_count = len(uploaded_results) + 1  # +1 for summary
            logger.info("Uploaded %d file(s) to S3 (run prefix: %s).", uploaded_count, run_prefix)
            if upload_failed:
                logger.warning("Failed to upload %d table(s) to S3: %s", len(upload_failed), ", ".join(upload_failed))

        # ── 7. Success notification ────────────────────────────────────
        success_lines = "\n".join(
            f"  ✔ {r.table_name}: {r.row_count:,} rows ({r.elapsed_seconds:.1f}s)"
            for r in results
        )
        skipped_lines = "\n".join(f"  ✘ {t}: export failed" for t in skipped)
        upload_fail_lines = "\n".join(f"  ✘ {t}: upload failed" for t in upload_failed)

        body = (
            f"<h3>Jobpac Refresh Completed</h3>"
            f"<p>Exported <b>{len(results)}</b> table(s) ({total_rows:,} total rows) "
            f"to <code>s3://{config.s3_bucket}/{run_prefix}</code>"
            + (f" — <b>{len(skipped)} export failed</b>" if skipped else "")
            + (f" — <b>{len(upload_failed)} upload failed</b>" if upload_failed else "")
            + f"</p>"
            f"<pre>{success_lines}"
            + (f"\n\nExport failures:\n{skipped_lines}" if skipped else "")
            + (f"\n\nUpload failures:\n{upload_fail_lines}" if upload_failed else "")
            + "</pre>"
        )

        if config.notification_recipients:
            send_notification(config, subject="Jobpac Refresh", body=body)

        job_end = datetime.now(timezone.utc)
        elapsed = time.monotonic() - t0
        logger.info(
            "=== JobPac Data Export completed successfully | start=%s end=%s elapsed=%.1fs ===",
            job_start.strftime("%Y-%m-%dT%H:%M:%S UTC"),
            job_end.strftime("%Y-%m-%dT%H:%M:%S UTC"),
            elapsed,
        )
        return 0

    except BaseException:
        # ── Failure / interrupt notification ──────────────────────────
        error_detail = traceback.format_exc()
        job_end = datetime.now(timezone.utc)
        elapsed = time.monotonic() - t0
        logger.error(
            "=== JobPac Data Export FAILED | start=%s end=%s elapsed=%.1fs ===\n%s",
            job_start.strftime("%Y-%m-%dT%H:%M:%S UTC"),
            job_end.strftime("%Y-%m-%dT%H:%M:%S UTC"),
            elapsed,
            error_detail,
        )

        # Report how far S3 uploads got before the failure/interrupt.
        if uploaded_results or upload_failed:
            logger.warning(
                "S3 upload status at time of failure — uploaded: %d, upload_failed: %d",
                len(uploaded_results),
                len(upload_failed),
            )
            if uploaded_results:
                logger.info(
                    "Tables uploaded to S3: %s",
                    ", ".join(r.table_name for r in uploaded_results),
                )

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
    rc = main(sys.argv[1:])
    # jaydebeapi starts a JVM via JPype that prevents normal interpreter shutdown;
    # os._exit bypasses the JVM's thread linger without affecting the exit code.
    os._exit(rc)
