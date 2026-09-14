"""
notifications.py — outbound SMTP notifications for the ingestion pipeline.

Deliberately mirrors the mail helper used by the DRM scripts
(~/repos/scripts/drm/email_server.py) so both code-bases share one mail
configuration and one set of environment variables:

    EMAIL_CONFIG_HOST           — SMTP host
    EMAIL_CONFIG_PORT           — (optional) SMTP port, default 587
    EMAIL_CONFIG_AUTH_USER      — SMTP username; also used as the From address
    EMAIL_CONFIG_AUTH_PASSWORD  — SMTP password
    EMAIL_RECIPIENT             — comma-separated default recipient list

Everything here is best-effort. A mail failure is logged and swallowed: a
broken SMTP config must never turn a successful ingestion run into a failed
one, and must never mask the real error on a failed run.
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

DEFAULT_SMTP_PORT = 587


def get_default_recipient() -> str:
    """Recipient list configured for unattended runs, or '' if unset."""
    return (os.environ.get("EMAIL_RECIPIENT") or "").strip()


def send_email(recipient: str, subject: str, body: str) -> bool:
    """Send one HTML email.

    :param recipient: one address, or several separated by commas
    :param subject:   subject line
    :param body:      HTML body
    :returns:         True if the message was handed to the SMTP server
    """
    if not recipient:
        logger.warning("No recipient configured (EMAIL_RECIPIENT) — email not sent.")
        return False

    smtp_host = os.environ.get("EMAIL_CONFIG_HOST")
    smtp_user = os.environ.get("EMAIL_CONFIG_AUTH_USER")
    smtp_password = os.environ.get("EMAIL_CONFIG_AUTH_PASSWORD")

    missing = [
        name
        for name, value in (
            ("EMAIL_CONFIG_HOST", smtp_host),
            ("EMAIL_CONFIG_AUTH_USER", smtp_user),
            ("EMAIL_CONFIG_AUTH_PASSWORD", smtp_password),
        )
        if not value
    ]
    if missing:
        logger.warning(
            f"SMTP not configured ({', '.join(missing)} missing) — email not sent."
        )
        return False

    # Parsed defensively: an empty or malformed EMAIL_CONFIG_PORT is a typo in
    # .env, not a reason to raise out of a best-effort notification helper.
    raw_port = os.environ.get("EMAIL_CONFIG_PORT") or DEFAULT_SMTP_PORT
    try:
        smtp_port = int(raw_port)
    except (TypeError, ValueError):
        logger.warning(
            f"EMAIL_CONFIG_PORT={raw_port!r} is not a number — falling back to {DEFAULT_SMTP_PORT}."
        )
        smtp_port = DEFAULT_SMTP_PORT

    # Recipients arrive as a single comma-separated string (same convention as
    # the DRM scripts); the header keeps that string, the envelope needs a list.
    recipients = [addr.strip() for addr in recipient.split(",") if addr.strip()]

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipients, msg.as_string())
        logger.info(f"Email sent to {msg['To']}: {subject}")
        return True
    except Exception as exc:
        logger.error(f"Failed to send email to {msg['To']}: {exc}")
        return False
