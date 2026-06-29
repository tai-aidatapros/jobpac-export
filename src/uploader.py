"""
S3 uploader — uploads exported CSV files to an S3 bucket.

Each run creates a timestamped prefix so that historical exports are preserved:

    s3://<bucket>/<prefix>/<ISO-timestamp>/<table>.csv
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import boto3

logger = logging.getLogger(__name__)


def make_run_prefix(prefix: str) -> str:
    """Return a timestamped S3 prefix for the current run."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    return f"{prefix.rstrip('/')}/{ts}"


def upload_single_csv(
    csv_path: str | Path,
    bucket: str,
    run_prefix: str,
    region: str = "ap-southeast-2",
) -> str:
    """
    Upload one CSV file to *run_prefix* in S3.

    Returns the S3 key that was uploaded.
    """
    s3 = boto3.client("s3", region_name=region)
    csv_file = Path(csv_path)
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
    return key


def update_latest_pointers(
    csv_paths: Sequence[str | Path],
    bucket: str,
    prefix: str,
    region: str = "ap-southeast-2",
) -> None:
    """Overwrite the 'latest' S3 pointer for each file in *csv_paths*."""
    s3 = boto3.client("s3", region_name=region)
    latest_prefix = f"{prefix.rstrip('/')}/latest"
    for csv_path in csv_paths:
        csv_file = Path(csv_path)
        latest_key = f"{latest_prefix}/{csv_file.name}"
        s3.upload_file(
            Filename=str(csv_file),
            Bucket=bucket,
            Key=latest_key,
            ExtraArgs={"ContentType": "text/csv"},
        )
    logger.info("Latest pointer updated at s3://%s/%s/", bucket, latest_prefix)


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
    local = Path(local_dir)
    csv_files = sorted(local.glob("*.csv"))

    if not csv_files:
        logger.warning("No CSV files found in %s — nothing to upload.", local_dir)
        return []

    run_prefix = make_run_prefix(prefix)
    uploaded_keys = [upload_single_csv(f, bucket, run_prefix, region) for f in csv_files]
    logger.info("Uploaded %d file(s) to s3://%s/%s/", len(uploaded_keys), bucket, run_prefix)

    update_latest_pointers(csv_files, bucket, prefix, region)
    return uploaded_keys
