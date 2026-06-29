"""
Unit tests for src/main.py pre-flight connectivity checks.

Verifies that the task exits with code 1 and sends an alert (without
starting the export) when any upstream hop is unreachable.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.main import main


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_config(host: str = "10.128.13.219") -> MagicMock:
    cfg = MagicMock()
    cfg.odbc_creds.host = host
    cfg.s3_bucket = "test-bucket"
    cfg.aws_region = "ap-southeast-2"
    cfg.notification_recipients = ["ops@example.com"]
    cfg.tables = ["TABLE_A"]
    cfg.s3_prefix = "current/"
    cfg.max_workers = 1
    cfg.db_backend = "jdbc"
    cfg.odbc_creds.jdbc_url = f"jdbc:as400://{host}/TESTDB"
    cfg.odbc_creds.username = "user"
    cfg.odbc_creds.password = "pass"
    cfg.odbc_creds.jar_path = "/fake/jt400.jar"
    cfg.odbc_creds.database = "TESTDB"
    return cfg


# ---------------------------------------------------------------------------
# S3 connectivity
# ---------------------------------------------------------------------------

class TestS3ConnectivityCheck:
    """Task must stop before exporting when S3 is unreachable."""

    @patch("src.main.send_notification")
    @patch("src.main.export_tables")
    @patch("src.main._check_s3_reachable", return_value=False)
    @patch("src.main.load_config")
    def test_exits_1_when_s3_unreachable(
        self, mock_cfg, _mock_s3, mock_export, mock_notify
    ):
        mock_cfg.return_value = _make_config()

        result = main()

        assert result == 1, "main() must return 1 when S3 is unreachable"
        mock_export.assert_not_called()

    @patch("src.main.send_notification")
    @patch("src.main.export_tables")
    @patch("src.main._check_s3_reachable", return_value=False)
    @patch("src.main.load_config")
    def test_sends_alert_when_s3_unreachable(
        self, mock_cfg, _mock_s3, _mock_export, mock_notify
    ):
        mock_cfg.return_value = _make_config()

        main()

        mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# Happy path — both hops reachable
# ---------------------------------------------------------------------------

class TestHappyPath:
    """When all connectivity checks pass, the export runs to completion."""

    @patch("src.main.send_notification")
    @patch("src.main.upload_csvs", return_value=["current/2026-01-01/TABLE_A.csv"])
    @patch("src.main.export_tables")
    @patch("src.main._check_s3_reachable", return_value=True)
    @patch("src.main.load_config")
    def test_returns_0_on_success(
        self, mock_cfg, _mock_s3, mock_export, mock_upload, mock_notify
    ):
        cfg = _make_config()
        mock_cfg.return_value = cfg

        from src.exporter import TableResult
        mock_export.return_value = (
            [TableResult("TABLE_A", 42, "/tmp/TABLE_A.csv", 1.2)],
            [],
        )

        result = main()

        assert result == 0
        mock_export.assert_called_once()
        mock_upload.assert_called_once()
