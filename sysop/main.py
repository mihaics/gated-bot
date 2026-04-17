"""SysOp entry point — startup, health checks, shutdown."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys

from sysop.bot import SysOpBot
from sysop.config import load_config

logger = logging.getLogger("sysop")


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _sync_health_checks(config) -> dict[str, bool]:
    """Runs blocking health probes. Call from a thread via asyncio.to_thread."""
    results: dict[str, bool] = {}

    try:
        env = dict(os.environ, KUBECONFIG=config.kubeconfig)
        r = subprocess.run(
            ["kubectl", "get", "nodes", "--no-headers"],
            capture_output=True, timeout=10, env=env,
        )
        results["kubectl"] = r.returncode == 0
        if r.returncode != 0:
            logger.warning("kubectl health check failed: %s", r.stderr.decode()[:200])
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("kubectl health check failed: %s", e)
        results["kubectl"] = False

    results["git_repo"] = os.path.isdir(config.git_repo_path)
    if not results["git_repo"]:
        logger.warning("Git repo path does not exist: %s", config.git_repo_path)

    persona_dir = config.claude.persona_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "persona"
    )
    results["persona"] = os.path.isfile(os.path.join(persona_dir, "CLAUDE.md"))
    if not results["persona"]:
        logger.warning("Persona CLAUDE.md not found at %s", persona_dir)

    # The Claude Code hook must exist and settings.json must register it; if
    # settings.json is missing, Claude Code runs in bypassPermissions with no
    # gate at all — a known-dangerous configuration worth logging loudly.
    hooks_settings = os.path.join(persona_dir, ".claude", "settings.json")
    results["hook_registered"] = os.path.isfile(hooks_settings)
    if not results["hook_registered"]:
        logger.error(
            "PreToolUse hook is NOT registered — %s is missing. The bot will "
            "run without any gate on Bash commands. Refusing to start silently.",
            hooks_settings,
        )

    return results


async def _run():
    config_path = os.environ.get("SYSOP_CONFIG", "config.yaml")
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Failed to load config: %s", e)
        return 1

    health = await asyncio.to_thread(_sync_health_checks, config)
    health_summary = ", ".join(f"{k}: {'OK' if v else 'FAIL'}" for k, v in health.items())
    logger.info("Health checks: %s", health_summary)

    if not health.get("hook_registered"):
        logger.error("Aborting startup: PreToolUse hook not registered.")
        return 2

    bot = SysOpBot(config)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    force_quit = False

    def _signal_handler():
        nonlocal force_quit
        if stop_event.is_set():
            logger.warning("Second signal — forcing exit")
            force_quit = True
            # Cancel the stop_event waiter to break out immediately.
            for task in asyncio.all_tasks(loop):
                if task is not asyncio.current_task():
                    task.cancel()
            return
        logger.info("Shutdown signal received (press Ctrl+C again to force)")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    logger.info("Starting SysOp bot...")
    await bot.start()
    logger.info("SysOp bot is online")

    notify_channel = os.environ.get("SYSOP_NOTIFY_CHANNEL")
    if notify_channel:
        try:
            from slack_sdk.web.async_client import AsyncWebClient
            client = AsyncWebClient(token=config.slack.bot_token)
            await client.chat_postMessage(
                channel=notify_channel,
                text=f"SysOp online. Health: {health_summary}",
            )
        except Exception as e:
            logger.warning("Failed to post startup message: %s", e)

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass

    if notify_channel:
        try:
            client = AsyncWebClient(token=config.slack.bot_token)
            await client.chat_postMessage(channel=notify_channel, text="SysOp going offline.")
        except Exception:
            pass

    logger.info("Shutting down SysOp bot...")
    try:
        await asyncio.wait_for(bot.stop(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("Shutdown timed out")
    logger.info("SysOp bot stopped")
    return 1 if force_quit else 0


def main():
    from dotenv import load_dotenv
    load_dotenv()
    _setup_logging()
    exit_code = asyncio.run(_run())
    sys.exit(exit_code or 0)


if __name__ == "__main__":
    main()
