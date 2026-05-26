"""
Notification helpers: Slack webhook + SMTP email.

Both are optional — missing env vars disable the channel silently.
Never raises on delivery failure (logs error, returns False).
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from dotenv import load_dotenv
load_dotenv()
import urllib.request
import urllib.error
import json

log = logging.getLogger("notify")


# ─────────────────────────── Slack ───────────────────────────

def send_slack(text: str, blocks: Optional[list] = None) -> bool:
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return False

    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    try:
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True
            log.warning(f"Slack returned HTTP {resp.status}")
            return False
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        log.error(f"Slack send failed: {e}")
        return False


# ─────────────────────────── Email (SMTP) ───────────────────────────

def send_email(subject: str, body: str, html_body: Optional[str] = None) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("EMAIL_FROM", user).strip()
    recipient = os.getenv("EMAIL_TO", "").strip()

    if not (host and user and password and recipient):
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.sendmail(sender, [recipient], msg.as_string())
        return True
    except Exception as e:
        log.error(f"Email send failed: {e}")
        return False


# ─────────────────────────── Unified broadcaster ───────────────────────────

def broadcast(subject: str, text_body: str, slack_blocks: Optional[list] = None) -> None:
    """Send to all configured channels. Silent if none configured."""
    sent_slack = send_slack(text_body, blocks=slack_blocks)
    sent_email = send_email(subject, text_body)

    if sent_slack or sent_email:
        channels = []
        if sent_slack: channels.append("slack")
        if sent_email: channels.append("email")
        log.info(f"Notification sent to: {', '.join(channels)}")
