"""
S3 uploader — uploads exported CSV files to an S3 bucket.

Each run creates a timestamped prefix so that historical exports are preserved:

    s3://<bucket>/<prefix>/<ISO-timestamp>/<table>.csv
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import boto3

logger = logging.getLogger(__name__)


def upload_csvs(
    local_dir: str,
    bucket: str,
    prefix: str,
    region: str = "ap-southeast-2",
) -> list[str]:
    """
    Upload all CSV files from *local_dir* to S3.

    Parameters
    ----------
    local_dir:
        Local directory containing .csv files.
    bucket:
        S3 bucket name.
    prefix:
        Base key prefix (e.g. "current/").  A timestamp sub-prefix is appended
        automatically so each run is isolated.
    region:
        AWS region for the S3 client.

    Returns
    -------
    List of S3 keys that were uploaded.
    """
    s3 = boto3.client("s3", region_name=region)

    # Timestamp sub-prefix for run isolation
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    run_prefix = f"{prefix.rstrip('/')}/{ts}"

    local = Path(local_dir)
    csv_files = sorted(local.glob("*.csv"))

    if not csv_files:
        logger.warning("No CSV files found in %s — nothing to upload.", local_dir)
        return []

    uploaded_keys: list[str] = []

    for csv_file in csv_files:
        key = f"{run_prefix}/{csv_file.name}"
        file_size = csv_file.stat().st_size
        logger.info(
            "Uploading %s (%s bytes) → s3://%s/%s",
            csv_file.name,
            f"{file_size:,}",
            bucket,
            key,
        )
        s3.upload_file(
            Filename=str(csv_file),
            Bucket=bucket,
            Key=key,
            ExtraArgs={"ContentType": "text/csv"},
        )
        uploaded_keys.append(key)

    logger.info(
        "Uploaded %d file(s) to s3://%s/%s/",
        len(uploaded_keys),
        bucket,
        run_prefix,
    )

    # Also upload a "latest" pointer — overwrite on every run so consumers can
    # always find the most recent export without knowing the timestamp.
    latest_prefix = f"{prefix.rstrip('/')}/latest"
    for csv_file in csv_files:
        latest_key = f"{latest_prefix}/{csv_file.name}"
        s3.upload_file(
            Filename=str(csv_file),
            Bucket=bucket,
            Key=latest_key,
            ExtraArgs={"ContentType": "text/csv"},
        )
    logger.info("Latest pointer updated at s3://%s/%s/", bucket, latest_prefix)

    return uploaded_keys
