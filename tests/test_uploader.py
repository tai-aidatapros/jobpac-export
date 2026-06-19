"""
Unit tests for src/uploader.py.

Uses moto to mock S3 interactions.
"""

import csv
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.uploader import upload_csvs


class TestUploadCsvs:
    """Tests for the upload_csvs function."""

    @patch("src.uploader.boto3")
    def test_uploads_csv_files(self, mock_boto3):
        """All CSV files in the directory should be uploaded to S3."""
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create test CSV files
            for name in ["TABLE_A.csv", "TABLE_B.csv"]:
                path = Path(tmp_dir) / name
                with open(path, "w", newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["col1", "col2"])
                    writer.writerow(["val1", "val2"])

            keys = upload_csvs(
                local_dir=tmp_dir,
                bucket="test-bucket",
                prefix="exports/",
                region="ap-southeast-2",
            )

            # Each CSV should be uploaded twice: once with timestamp, once as "latest"
            assert mock_s3.upload_file.call_count == 4  # 2 timestamped + 2 latest
            assert len(keys) == 2

            # Verify the keys contain the table names
            assert any("TABLE_A.csv" in k for k in keys)
            assert any("TABLE_B.csv" in k for k in keys)

    @patch("src.uploader.boto3")
    def test_no_csv_files_returns_empty(self, mock_boto3):
        """An empty directory should result in no uploads."""
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3

        with tempfile.TemporaryDirectory() as tmp_dir:
            keys = upload_csvs(
                local_dir=tmp_dir,
                bucket="test-bucket",
                prefix="exports/",
            )

            assert keys == []
            mock_s3.upload_file.assert_not_called()

    @patch("src.uploader.boto3")
    def test_timestamp_prefix_is_applied(self, mock_boto3):
        """Uploaded keys should contain a timestamp-based sub-prefix."""
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "DATA.csv"
            path.write_text("col\nval\n")

            keys = upload_csvs(
                local_dir=tmp_dir,
                bucket="bucket",
                prefix="prefix/",
            )

            assert len(keys) == 1
            # Key should look like: prefix/2026-06-15T07-30-00/DATA.csv
            key = keys[0]
            parts = key.split("/")
            assert parts[0] == "prefix"
            assert len(parts) == 3  # prefix / timestamp / filename
            assert parts[2] == "DATA.csv"
