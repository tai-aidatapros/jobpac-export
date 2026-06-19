"""
Email / notification module — replaces Send-MailMessage from TaskJobPac.ps1.

Supports three pluggable backends:
  - **ses**  — Amazon Simple Email Service (default, recommended)
  - **sns**  — Amazon Simple Notification Service
  - **smtp** — Direct SMTP (e.g. Office 365, for backward compatibility)
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

import boto3

from src.config import AppConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_notification(
    config: AppConfig,
    subject: str,
    body: str,
) -> None:
    """
    Send a notification using the backend configured in *config*.

    Parameters
    ----------
    config:
        Application config (contains backend choice, recipients, credentials).
    subject:
        Email subject line.
    body:
        Message body (HTML for SES/SMTP, plain text for SNS).
    """
    backend = config.notification_backend
    logger.info("Sending notification via %s: subject=%r", backend, subject)

    if backend == "ses":
        _send_via_ses(config, subject, body)
    elif backend == "sns":
        _send_via_sns(config, subject, body)
    elif backend == "smtp":
        _send_via_smtp(config, subject, body)
    else:
        logger.error("Unknown notification backend: %s", backend)
        raise ValueError(f"Unknown notification backend: {backend}")


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------

def _send_via_ses(config: AppConfig, subject: str, body: str) -> None:
    """Send email through Amazon SES."""
    ses = boto3.client("ses", region_name=config.aws_region)

    # SES requires a verified "From" address.  If email_creds has a
    # from_address configured, use it; otherwise fall back to the first
    # recipient (common for internal alerts).
    from_address = config.email_creds.from_address or config.notification_recipients[0]

    response = ses.send_email(
        Source=from_address,
        Destination={"ToAddresses": config.notification_recipients},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Html": {"Data": body, "Charset": "UTF-8"}},
        },
    )
    logger.info("SES MessageId: %s", response["MessageId"])


def _send_via_sns(config: AppConfig, subject: str, body: str) -> None:
    """Publish a message to an SNS topic."""
    sns = boto3.client("sns", region_name=config.aws_region)

    if not config.sns_topic_arn:
        raise ValueError("SNS_TOPIC_ARN is required when using the SNS backend")

    response = sns.publish(
        TopicArn=config.sns_topic_arn,
        Subject=subject[:100],  # SNS subject max 100 chars
        Message=body,
    )
    logger.info("SNS MessageId: %s", response["MessageId"])


def _send_via_smtp(config: AppConfig, subject: str, body: str) -> None:
    """Send email via SMTP (e.g. Office 365) — mirrors the original Send-MailMessage."""
    creds = config.email_creds
    if not creds.smtp_server or not creds.username:
        raise ValueError("EMAIL_SECRET_NAME is required when using the SMTP backend")

    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = creds.from_address or creds.username
    msg["To"] = ", ".join(config.notification_recipients)

    logger.info(
        "Connecting to %s:%d as %s …",
        creds.smtp_server,
        creds.smtp_port,
        creds.username,
    )
    with smtplib.SMTP(creds.smtp_server, creds.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(creds.username, creds.password)
        server.sendmail(
            creds.from_address or creds.username,
            config.notification_recipients,
            msg.as_string(),
        )
    logger.info("SMTP email sent successfully.")
