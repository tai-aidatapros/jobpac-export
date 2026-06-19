"""
Unit tests for src/notifier.py.

All AWS services are mocked — no real emails are sent.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.config import AppConfig, EmailCredentials, OdbcCredentials
from src.notifier import send_notification


def _make_config(backend: str = "ses", **overrides) -> AppConfig:
    """Build an AppConfig for testing."""
    defaults = dict(
        odbc_creds=OdbcCredentials("host", "DB", "user", "pass", "/fake/jt400.jar"),
        s3_bucket="test-bucket",
        s3_prefix="test/",
        notification_backend=backend,
        notification_recipients=["alice@example.com", "bob@example.com"],
        email_creds=EmailCredentials(
            smtp_server="smtp.example.com",
            smtp_port=587,
            username="sender@example.com",
            password="secret",
            from_address="sender@example.com",
        ),
        sns_topic_arn="arn:aws:sns:ap-southeast-2:123456789:test-topic",
        aws_region="ap-southeast-2",
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


class TestSendNotificationSes:
    """Tests for the SES backend."""

    @patch("src.notifier.boto3")
    def test_sends_via_ses(self, mock_boto3):
        mock_ses = MagicMock()
        mock_ses.send_email.return_value = {"MessageId": "abc123"}
        mock_boto3.client.return_value = mock_ses

        config = _make_config("ses")
        send_notification(config, "Test Subject", "<p>Body</p>")

        mock_boto3.client.assert_called_with("ses", region_name="ap-southeast-2")
        mock_ses.send_email.assert_called_once()

        call_kwargs = mock_ses.send_email.call_args[1]
        assert call_kwargs["Source"] == "sender@example.com"
        assert call_kwargs["Destination"]["ToAddresses"] == [
            "alice@example.com",
            "bob@example.com",
        ]
        assert call_kwargs["Message"]["Subject"]["Data"] == "Test Subject"


class TestSendNotificationSns:
    """Tests for the SNS backend."""

    @patch("src.notifier.boto3")
    def test_sends_via_sns(self, mock_boto3):
        mock_sns = MagicMock()
        mock_sns.publish.return_value = {"MessageId": "def456"}
        mock_boto3.client.return_value = mock_sns

        config = _make_config("sns")
        send_notification(config, "Alert", "Something happened")

        mock_boto3.client.assert_called_with("sns", region_name="ap-southeast-2")
        mock_sns.publish.assert_called_once_with(
            TopicArn="arn:aws:sns:ap-southeast-2:123456789:test-topic",
            Subject="Alert",
            Message="Something happened",
        )

    def test_sns_without_topic_arn_raises(self):
        config = _make_config("sns", sns_topic_arn="")
        with pytest.raises(ValueError, match="SNS_TOPIC_ARN"):
            send_notification(config, "Alert", "Body")


class TestSendNotificationSmtp:
    """Tests for the SMTP backend."""

    @patch("src.notifier.smtplib.SMTP")
    def test_sends_via_smtp(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        config = _make_config("smtp")
        send_notification(config, "Subject", "<p>HTML body</p>")

        mock_smtp_class.assert_called_with("smtp.example.com", 587)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("sender@example.com", "secret")
        mock_server.sendmail.assert_called_once()

    def test_smtp_without_credentials_raises(self):
        config = _make_config(
            "smtp",
            email_creds=EmailCredentials(),
        )
        with pytest.raises(ValueError, match="EMAIL_SECRET_NAME"):
            send_notification(config, "Subject", "Body")


class TestSendNotificationUnknownBackend:
    def test_unknown_backend_raises(self):
        config = _make_config("carrier_pigeon")
        with pytest.raises(ValueError, match="Unknown notification backend"):
            send_notification(config, "Subject", "Body")
