"""
Docker-based Watchdog: Monitor alpaca-bot container health every 60s, auto-restart if unhealthy.
Runs as a sidecar service in docker-compose.
"""

import logging
import os
import subprocess
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("watchdog-docker")

BOT_CONTAINER_NAME = os.getenv("BOT_CONTAINER_NAME", "alpaca-bot")
CHECK_INTERVAL_SECONDS = 60
CONSECUTIVE_FAILURES_BEFORE_RESTART = 3


def get_container_status() -> dict:
    """Get status of bot container via Docker API."""
    try:
        result = subprocess.run(
            ["docker", "inspect", BOT_CONTAINER_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {"running": False, "status": "not_found"}

        # Parse Docker inspect output
        import json
        data = json.loads(result.stdout)
        if data:
            container = data[0]
            state = container.get("State", {})
            return {
                "running": state.get("Running", False),
                "status": state.get("Status", "unknown"),
                "pid": state.get("Pid", 0),
            }
        return {"running": False, "status": "parse_error"}
    except Exception as e:
        log.error(f"Failed to inspect container: {e}")
        return {"running": False, "status": "error"}


def check_container_health() -> bool:
    """Check if container is healthy (has healthcheck)."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={BOT_CONTAINER_NAME}", "--format", "{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = result.stdout.strip()
        # Status examples: "Up 2 hours (healthy)", "Up 2 hours (unhealthy)", "Up 2 hours"
        if "unhealthy" in status:
            return False
        if "Up" in status:
            return True
        return False
    except Exception as e:
        log.warning(f"Failed to check container health: {e}")
        return False


def restart_container() -> bool:
    """Restart bot container."""
    try:
        log.critical(f"🔴 RESTARTING {BOT_CONTAINER_NAME}...")
        result = subprocess.run(
            ["docker", "restart", BOT_CONTAINER_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            log.critical(f"✅ {BOT_CONTAINER_NAME} restarted successfully")
            return True
        else:
            log.error(f"Failed to restart: {result.stderr}")
            return False
    except Exception as e:
        log.error(f"Restart failed: {e}")
        return False


def main():
    log.info(f"🟢 Watchdog started. Monitoring {BOT_CONTAINER_NAME} every {CHECK_INTERVAL_SECONDS}s")

    consecutive_failures = 0

    while True:
        try:
            time.sleep(CHECK_INTERVAL_SECONDS)

            status = get_container_status()
            is_running = status.get("running", False)

            if is_running:
                is_healthy = check_container_health()
                if is_healthy:
                    log.info(f"✅ {BOT_CONTAINER_NAME} healthy (status={status.get('status', '?')})")
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    log.warning(
                        f"⚠ {BOT_CONTAINER_NAME} unhealthy (consecutive failures: {consecutive_failures})"
                    )
                    if consecutive_failures >= CONSECUTIVE_FAILURES_BEFORE_RESTART:
                        restart_container()
                        consecutive_failures = 0
            else:
                consecutive_failures += 1
                log.warning(
                    f"⚠ {BOT_CONTAINER_NAME} is DOWN (status={status.get('status', '?')}, "
                    f"consecutive failures: {consecutive_failures})"
                )
                if consecutive_failures >= CONSECUTIVE_FAILURES_BEFORE_RESTART:
                    restart_container()
                    consecutive_failures = 0

        except KeyboardInterrupt:
            log.info("Watchdog stopped by user")
            break
        except Exception as e:
            log.error(f"Watchdog error: {e}")
            consecutive_failures += 1


if __name__ == "__main__":
    main()
