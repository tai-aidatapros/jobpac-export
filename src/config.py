"""
Configuration and secrets loading.

All runtime configuration is driven by environment variables (12-factor style).
Secrets are fetched from AWS Secrets Manager at startup.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_TABLES_PATH = str(Path(__file__).resolve().parent.parent / "config" / "tables.csv")
_DEFAULT_S3_PREFIX = "current/"
_DEFAULT_NOTIFICATION_BACKEND = "ses"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class OdbcCredentials:
    """JDBC connection credentials for IBM i (AS/400) via jt400."""

    host: str
    database: str
    username: str
    password: str
    jar_path: str  # path to jt400.jar

    @property
    def jdbc_url(self) -> str:
        return f"jdbc:as400://{self.host}/{self.database};secure=true"


@dataclass
class EmailCredentials:
    """SMTP / SES email credentials (optional, only needed for SMTP backend)."""

    smtp_server: str = ""
    smtp_port: int = 587
    username: str = ""
    password: str = ""
    from_address: str = ""


@dataclass
class AppConfig:
    """Top-level application configuration."""

    # DB
    odbc_creds: OdbcCredentials = field(default_factory=lambda: OdbcCredentials("", "", "", "", ""))

    # S3
    s3_bucket: str = ""
    s3_prefix: str = _DEFAULT_S3_PREFIX

    # Notification
    notification_backend: str = _DEFAULT_NOTIFICATION_BACKEND  # "ses" | "sns" | "smtp"
    notification_recipients: list[str] = field(default_factory=list)
    email_creds: EmailCredentials = field(default_factory=EmailCredentials)
    sns_topic_arn: str = ""

    # Tables
    tables: list[str] = field(default_factory=list)
    table_chunk_sizes: dict[str, int] = field(default_factory=dict)
    table_exclude_columns: dict[str, list[str]] = field(default_factory=dict)
    max_workers: int = 2

    # DB backend: "jdbc" (default) or "odbc"
    db_backend: str = "jdbc"

    # AWS region (used for boto3 clients)
    aws_region: str = "ap-southeast-2"

    # When True, all CSV fields are written as quoted strings (text type)
    csv_force_text: bool = True


# ---------------------------------------------------------------------------
# Secrets Manager helper
# ---------------------------------------------------------------------------
def _get_secret(secret_name: str, region: str) -> dict:
    """Retrieve a JSON secret from AWS Secrets Manager."""
    client = boto3.client("secretsmanager", region_name=region)
    try:
        response = client.get_secret_value(SecretId=secret_name)
        return json.loads(response["SecretString"])
    except ClientError:
        logger.exception("Failed to retrieve secret '%s'", secret_name)
        raise


# ---------------------------------------------------------------------------
# Table list loader
# ---------------------------------------------------------------------------
def _load_tables(path: str) -> tuple[list[str], dict[str, int], dict[str, list[str]]]:
    """
    Load the list of table names and optional per-table settings from a CSV file.

    Expected columns (only TableName is required):
        TableName      — IBM i table name
        ChunkSize      — optional int; when set, the table is exported in RRN-based
                         chunks of this size to avoid long-lived connections dropping
        ExcludeColumns — optional semicolon-separated list of column names to skip
                         (e.g. "LONGDESC;BINARYDATA") — useful to drop LOB columns

    Returns a (tables, chunk_sizes, exclude_columns) tuple.
    """
    tables: list[str] = []
    chunk_sizes: dict[str, int] = {}
    exclude_columns: dict[str, list[str]] = {}
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return tables, chunk_sizes, exclude_columns
        normalized = [h.strip().lower() for h in header]
        chunk_col = normalized.index("chunksize") if "chunksize" in normalized else -1
        excl_col = normalized.index("excludecolumns") if "excludecolumns" in normalized else -1
        for row in reader:
            if not row:
                continue
            name = row[0].strip()
            if not name:
                continue
            tables.append(name)
            if chunk_col >= 0 and len(row) > chunk_col:
                raw = row[chunk_col].strip()
                if raw:
                    try:
                        chunk_sizes[name] = int(raw)
                    except ValueError:
                        logger.warning("Invalid ChunkSize %r for table %s — ignored", raw, name)
            if excl_col >= 0 and len(row) > excl_col:
                raw = row[excl_col].strip()
                if raw:
                    cols = [c.strip() for c in raw.split(";") if c.strip()]
                    if cols:
                        exclude_columns[name] = cols
    logger.info("Loaded %d table(s) from %s", len(tables), path)
    return tables, chunk_sizes, exclude_columns


# ---------------------------------------------------------------------------
# Build configuration
# ---------------------------------------------------------------------------
def load_config() -> AppConfig:
    """
    Build an AppConfig from environment variables and Secrets Manager.

    Required env vars:
        JOBPAC_SECRET_NAME   — Secrets Manager secret holding ODBC creds
        S3_BUCKET            — Target S3 bucket for CSV uploads

    Optional env vars:
        JOBPAC_HOST          — Override IBM i host/IP (default: from secret)
        JOBPAC_DB            — Override database name (default: from secret)
        JT400_JAR            — Path to jt400.jar (required)
        S3_PREFIX            — Key prefix (default: "current/")
        TABLES_PATH          — Path to tables CSV (default: bundled config/tables.csv)
        NOTIFICATION_BACKEND — "ses", "sns", or "smtp" (default: "ses")
        NOTIFICATION_RECIPIENTS — Comma-separated email list
        EMAIL_SECRET_NAME    — Secrets Manager secret for SMTP creds (only for smtp backend)
        SNS_TOPIC_ARN        — SNS topic ARN (only for sns backend)
        DB_BACKEND           — "jdbc" (default) or "odbc"
        AWS_REGION           — AWS region (default: ap-southeast-2)
    """
    region = os.environ.get("AWS_REGION", "ap-southeast-2")

    # --- ODBC credentials ---------------------------------------------------
    odbc_secret_name = os.environ["JOBPAC_SECRET_NAME"]
    odbc_secret = _get_secret(odbc_secret_name, region)

    odbc_creds = OdbcCredentials(
        host=os.environ.get("JOBPAC_HOST", odbc_secret.get("host", "")),
        database=os.environ.get("JOBPAC_DB", odbc_secret.get("database", "JDNWCDTA01")),
        username=odbc_secret["username"],
        password=odbc_secret["password"],
        jar_path=os.environ["JT400_JAR"],
    )

    # --- S3 ------------------------------------------------------------------
    s3_bucket = os.environ["S3_BUCKET"]
    s3_prefix = os.environ.get("S3_PREFIX", _DEFAULT_S3_PREFIX)

    # --- Notification --------------------------------------------------------
    backend = os.environ.get("NOTIFICATION_BACKEND", _DEFAULT_NOTIFICATION_BACKEND).lower()
    recipients_raw = os.environ.get("NOTIFICATION_RECIPIENTS", "")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    email_creds = EmailCredentials()
    sns_topic_arn = ""

    if backend == "smtp":
        email_secret_name = os.environ.get("EMAIL_SECRET_NAME", "")
        if email_secret_name:
            email_secret = _get_secret(email_secret_name, region)
            email_creds = EmailCredentials(
                smtp_server=email_secret.get("smtp_server", "smtp.office365.com"),
                smtp_port=int(email_secret.get("smtp_port", 587)),
                username=email_secret["username"],
                password=email_secret["password"],
                from_address=email_secret.get("from_address", email_secret["username"]),
            )
    elif backend == "sns":
        sns_topic_arn = os.environ.get("SNS_TOPIC_ARN", "")

    # --- Tables --------------------------------------------------------------
    tables_path = os.environ.get("TABLES_PATH", _DEFAULT_TABLES_PATH)
    tables, table_chunk_sizes, table_exclude_columns = _load_tables(tables_path)
    max_workers = int(os.environ.get("MAX_WORKERS", "2"))
    db_backend = os.environ.get("DB_BACKEND", "jdbc").lower()
    csv_force_text = os.environ.get("CSV_FORCE_TEXT", "true").lower() in ("true", "1", "yes")

    config = AppConfig(
        odbc_creds=odbc_creds,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        notification_backend=backend,
        notification_recipients=recipients,
        email_creds=email_creds,
        sns_topic_arn=sns_topic_arn,
        tables=tables,
        table_chunk_sizes=table_chunk_sizes,
        table_exclude_columns=table_exclude_columns,
        max_workers=max_workers,
        db_backend=db_backend,
        aws_region=region,
        csv_force_text=csv_force_text,
    )

    logger.info(
        "Config loaded: %d tables, backend=%s, bucket=%s",
        len(config.tables),
        config.notification_backend,
        config.s3_bucket,
    )
    return config
