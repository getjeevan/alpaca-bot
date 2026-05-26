"""
Secrets management: Load credentials from environment variables or systemd EnvironmentFile.
Never commit .env files — use this instead.
"""

import os
import sys
import logging
from dotenv import load_dotenv

log = logging.getLogger("secrets")


def load_secrets_from_file(env_file: str = ".env") -> None:
    """Load .env file for local development ONLY. Never in production."""
    if os.path.exists(env_file):
        load_dotenv(env_file)
        log.warning(f"⚠ Development mode: loaded secrets from {env_file}")


def validate_required(var: str) -> str:
    """Get required env var or exit with error."""
    val = os.getenv(var, "").strip()
    if not val:
        sys.stderr.write(f"ERROR: Required environment variable '{var}' not set\n")
        sys.stderr.write("Set via: systemctl set-environment, /etc/environment, or .env (dev only)\n")
        sys.exit(1)
    return val


def get_optional(var: str, default: str = "") -> str:
    """Get optional env var with default."""
    return os.getenv(var, default).strip()


class Secrets:
    """All credentials loaded at startup. Validates required vars."""

    def __init__(self, dev_mode: bool = False):
        if dev_mode:
            load_secrets_from_file()

        # ─────────────────────── Alpaca (REQUIRED) ───────────────────────
        self.alpaca_api_key = validate_required("ALPACA_API_KEY")
        self.alpaca_secret_key = validate_required("ALPACA_SECRET_KEY")
        self.alpaca_paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

        # ─────────────────────── Notifications (OPTIONAL) ───────────────────────
        self.slack_webhook_url = get_optional("SLACK_WEBHOOK_URL")
        self.smtp_host = get_optional("SMTP_HOST")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = get_optional("SMTP_USER")
        self.smtp_password = get_optional("SMTP_PASSWORD")
        self.email_from = get_optional("EMAIL_FROM", self.smtp_user)
        self.email_to = get_optional("EMAIL_TO")

        log.info(
            f"Secrets loaded: Alpaca {'paper' if self.alpaca_paper else 'LIVE'} · "
            f"Slack {'enabled' if self.slack_webhook_url else 'disabled'} · "
            f"Email {'enabled' if self.smtp_host else 'disabled'}"
        )

    def validate_environment(self) -> None:
        """Pre-flight checks before trading."""
        if not self.alpaca_api_key or not self.alpaca_secret_key:
            raise ValueError("Alpaca credentials not set")

        log.info(f"✓ Environment validated: Alpaca API key present, paper={self.alpaca_paper}")
