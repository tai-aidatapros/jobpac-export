"""
Unit tests for src/main.py.

Verifies that the task exits with code 1 and sends an alert (without
starting the export) when any upstream hop is unreachable, and that a
notification email is sent only when something failed.
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

    @patch("src.main.network_diagnostics")
    @patch("src.main.send_notification")
    @patch("src.main.export_tables")
    @patch("src.main._check_s3_reachable", return_value=False)
    @patch("src.main.load_config")
    def test_exits_1_when_s3_unreachable(
        self, mock_cfg, _mock_s3, mock_export, mock_notify, _mock_diag
    ):
        mock_cfg.return_value = _make_config()

        result = main([])

        assert result == 1, "main() must return 1 when S3 is unreachable"
        mock_export.assert_not_called()

    @patch("src.main.network_diagnostics")
    @patch("src.main.send_notification")
    @patch("src.main.export_tables")
    @patch("src.main._check_s3_reachable", return_value=False)
    @patch("src.main.load_config")
    def test_sends_alert_when_s3_unreachable(
        self, mock_cfg, _mock_s3, _mock_export, mock_notify, _mock_diag
    ):
        mock_cfg.return_value = _make_config()

        main([])

        mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# Export run — notification only on failure
# ---------------------------------------------------------------------------

def _run_export(results, skipped, upload_side_effect=None):
    """Run main() with all external calls mocked; return (rc, mock_notify)."""
    with patch("src.main.load_config", return_value=_make_config()), \
         patch("src.main._check_s3_reachable", return_value=True), \
         patch("src.main.network_diagnostics"), \
         patch("src.main.update_latest_pointers"), \
         patch("src.main.upload_single_csv", side_effect=upload_side_effect), \
         patch("src.main.send_notification") as mock_notify, \
         patch("src.main.export_tables") as mock_export:

        def fake_export(**kwargs):
            for r in results:
                kwargs["on_table_complete"](r)
            return results, skipped

        mock_export.side_effect = fake_export
        rc = main([])
    return rc, mock_notify


class TestExportNotifications:
    """Email is sent only when the run has failures."""

    def test_clean_run_returns_0_and_sends_no_email(self):
        from src.exporter import TableResult
        rc, mock_notify = _run_export(
            [TableResult("TABLE_A", 42, "/tmp/TABLE_A.csv", 1.2)], []
        )
        assert rc == 0
        mock_notify.assert_not_called()

    def test_export_failure_sends_email(self):
        from src.exporter import TableResult
        rc, mock_notify = _run_export(
            [TableResult("TABLE_A", 42, "/tmp/TABLE_A.csv", 1.2)], ["TABLE_B"]
        )
        assert rc == 0
        mock_notify.assert_called_once()
        assert "TABLE_B" in mock_notify.call_args.kwargs["body"]
        assert "Failure" in mock_notify.call_args.kwargs["subject"]

    def test_upload_failure_sends_email(self):
        from src.exporter import TableResult

        def fail_table_a(csv_path, **_):
            if str(csv_path).endswith("TABLE_A.csv"):
                raise RuntimeError("boom")

        rc, mock_notify = _run_export(
            [TableResult("TABLE_A", 42, "/tmp/TABLE_A.csv", 1.2)], [],
            upload_side_effect=fail_table_a,
        )
        assert rc == 0
        mock_notify.assert_called_once()
        assert "upload failed" in mock_notify.call_args.kwargs["body"]

    def test_unhandled_exception_sends_failure_email(self):
        with patch("src.main.load_config", return_value=_make_config()), \
             patch("src.main._check_s3_reachable", return_value=True), \
             patch("src.main.network_diagnostics"), \
             patch("src.main.send_notification") as mock_notify, \
             patch("src.main.export_tables", side_effect=RuntimeError("db down")):
            rc = main([])
        assert rc == 1
        mock_notify.assert_called_once()
        assert "FAILED" in mock_notify.call_args.kwargs["subject"]
