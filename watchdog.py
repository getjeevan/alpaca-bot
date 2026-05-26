"""
Alpaca Bot Watchdog: Monitor bot health every 60s, auto-restart if hung or crashed.
Logs to systemd journal. Run as a separate systemd service (alpaca-bot-watchdog.service).
"""

import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

# ─────────────────────── Logging to systemd ───────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("watchdog")

SERVICE_NAME = "alpaca-bot.service"
CHECK_INTERVAL_SECONDS = 60
CONSECUTIVE_FAILURES_BEFORE_RESTART = 3
LOG_PATH = "/var/log/alpaca-bot/bot.log"


def get_service_status() -> dict:
    """Get status of alpaca-bot.service via systemctl."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
        is_active = result.returncode == 0
        status = result.stdout.strip()

        # Get unit info (PID, since, restart count)
        info_result = subprocess.run(
            ["systemctl", "show", SERVICE_NAME, "-p", "MainPID,ActiveEnterTimestamp,NRestarts"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        info = {}
        for line in info_result.stdout.split("\n"):
            if "=" in line:
                key, val = line.split("=", 1)
                info[key] = val

        return {
            "active": is_active,
            "status": status,
            "pid": info.get("MainPID", "?"),
            "restart_count": info.get("NRestarts", "0"),
        }
    except Exception as e:
        log.error(f"Failed to get service status: {e}")
        return {"active": False, "status": "unknown", "pid": "?", "restart_count": "?"}


def check_process_health(pid: str) -> bool:
    """Check if process is responsive (not zombie/hung)."""
    if pid == "?" or not pid:
        return False

    try:
        # Check if process exists
        result = subprocess.run(
            ["ps", "-p", pid, "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            log.warning(f"Process {pid} no longer exists")
            return False

        state = result.stdout.strip()
        # Z = zombie, D = uninterruptible sleep (usually I/O stuck)
        if state and state[0] in ("Z", "D"):
            log.warning(f"Process {pid} in bad state: {state}")
            return False

        return True
    except Exception as e:
        log.warning(f"Failed to check process health: {e}")
        return False


def restart_service() -> bool:
    """Attempt to restart alpaca-bot.service."""
    try:
        log.critical(f"🔴 RESTARTING {SERVICE_NAME}...")
        result = subprocess.run(
            ["systemctl", "restart", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            log.critical(f"✅ {SERVICE_NAME} restarted successfully")
            return True
        else:
            log.error(f"Failed to restart: {result.stderr}")
            return False
    except Exception as e:
        log.error(f"Restart failed: {e}")
        return False


def tail_recent_logs(lines: int = 10) -> str:
    """Return last N lines of bot log."""
    try:
        if not os.path.exists(LOG_PATH):
            return "(no log file yet)"
        result = subprocess.run(
            ["tail", "-n", str(lines), LOG_PATH],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout
    except Exception:
        return "(could not read logs)"


def main():
    log.info(f"🟢 Watchdog started. Monitoring {SERVICE_NAME} every {CHECK_INTERVAL_SECONDS}s")

    consecutive_failures = 0

    while True:
        try:
            time.sleep(CHECK_INTERVAL_SECONDS)

            status = get_service_status()
            is_active = status["active"]
            pid = status["pid"]
            restart_count = status.get("restart_count", "0")

            if is_active:
                is_healthy = check_process_health(pid)
                if is_healthy:
                    log.info(f"✅ {SERVICE_NAME} healthy (PID {pid}, restarts={restart_count})")
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    log.warning(
                        f"⚠ {SERVICE_NAME} unhealthy (PID {pid} in bad state, "
                        f"consecutive failures: {consecutive_failures})"
                    )
                    if consecutive_failures >= CONSECUTIVE_FAILURES_BEFORE_RESTART:
                        restart_service()
                        consecutive_failures = 0
            else:
                consecutive_failures += 1
                log.warning(
                    f"⚠ {SERVICE_NAME} is DOWN (status={status['status']}, "
                    f"consecutive failures: {consecutive_failures})"
                )
                if consecutive_failures >= CONSECUTIVE_FAILURES_BEFORE_RESTART:
                    restart_service()
                    consecutive_failures = 0

        except KeyboardInterrupt:
            log.info("Watchdog stopped by user")
            break
        except Exception as e:
            log.error(f"Watchdog error: {e}")
            consecutive_failures += 1


if __name__ == "__main__":
    main()
