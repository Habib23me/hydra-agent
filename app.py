#!/usr/bin/env python3
"""
Slack Bot — Conversational AI Developer Teammate
=================================================

Listens for @mentions via Socket Mode and manages thread-based conversations.
Each Slack thread gets its own Claude SDK session with preserved context.

Usage:
    python app.py
"""

import asyncio
import atexit
import os
import re
import signal
import sys
from pathlib import Path

import certifi
from dotenv import load_dotenv

# Fix SSL certificate verification on macOS with Homebrew Python.
if not os.environ.get("SSL_CERT_FILE"):
    os.environ["SSL_CERT_FILE"] = certifi.where()

load_dotenv()

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from session_manager import SessionManager


# =============================================================================
# Environment
# =============================================================================

SLACK_BOT_TOKEN: str = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN: str = os.environ.get("SLACK_APP_TOKEN", "")

# Default working directory for the agent. Can be overridden per-session later.
DEFAULT_CWD: Path = Path(os.environ.get("DEFAULT_CWD", ".")).resolve()

# How often to clean up idle sessions (seconds)
CLEANUP_INTERVAL = 300  # 5 minutes

# PID file to prevent zombie instances
PID_FILE = Path(__file__).parent / ".hydra-agent.pid"


# =============================================================================
# Zombie prevention
# =============================================================================

def _kill_existing_instance() -> None:
    """Kill any existing bot instance using the PID file."""
    if not PID_FILE.exists():
        return

    try:
        old_pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return

    if old_pid == os.getpid():
        return

    # Check if the old process is still running
    try:
        os.kill(old_pid, 0)  # Signal 0 = just check if alive
    except ProcessLookupError:
        # Process is dead, clean up stale PID file
        PID_FILE.unlink(missing_ok=True)
        return
    except PermissionError:
        # Process exists but we can't signal it — leave it
        print(f"Warning: existing instance (PID {old_pid}) is running but we can't stop it")
        return

    # Kill the old instance
    print(f"Stopping existing instance (PID {old_pid})...")
    try:
        os.kill(old_pid, signal.SIGTERM)
        # Give it a moment to clean up
        import time
        for _ in range(10):
            time.sleep(0.2)
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                break
        else:
            # Still alive, force kill
            os.kill(old_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception as e:
        print(f"Warning: could not stop old instance: {e}")

    PID_FILE.unlink(missing_ok=True)


def _write_pid_file() -> None:
    """Write current PID to the PID file."""
    PID_FILE.write_text(str(os.getpid()))


def _cleanup_pid_file() -> None:
    """Remove the PID file on exit."""
    PID_FILE.unlink(missing_ok=True)


# =============================================================================
# App setup
# =============================================================================

app = AsyncApp(token=SLACK_BOT_TOKEN)
sessions = SessionManager(default_cwd=DEFAULT_CWD)


# =============================================================================
# Helpers
# =============================================================================

def strip_mention(text: str) -> str:
    """Remove Slack user mentions (e.g. <@U12345>) from the text."""
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


# =============================================================================
# Event handlers
# =============================================================================

@app.event("app_mention")
async def handle_app_mention(event: dict, say, client) -> None:
    """
    Handle @mentions of the bot.

    If this is a top-level message (no thread_ts), create a new thread.
    If this is inside an existing thread, route to that thread's session.
    """
    if event.get("bot_id"):
        return

    text = strip_mention(event.get("text", ""))
    if not text:
        return

    # Use thread_ts if already in a thread, otherwise start a new thread from this message
    thread_ts = event.get("thread_ts") or event.get("ts")
    channel = event.get("channel", "")

    print(f"\n[@mention] channel={channel} thread={thread_ts}")
    print(f"  Text: {text[:100]}")

    await sessions.process_message(channel, thread_ts, text, say, slack_client=client)


@app.event("message")
async def handle_message(event: dict, say, client) -> None:
    """
    Handle messages in threads where the bot has an active session.
    """
    # Ignore bot messages and subtypes (edits, deletes, etc.)
    if event.get("bot_id") or event.get("subtype"):
        return

    # Only handle thread replies
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    channel = event.get("channel", "")

    # Only respond if we have an active session for this thread
    if not sessions.has_session(channel, thread_ts):
        return

    # Skip @mentions (handled by handle_app_mention)
    text_raw = event.get("text", "")
    if re.search(r"<@[A-Z0-9]+>", text_raw):
        return

    text = text_raw.strip()
    if not text:
        return

    print(f"\n[Thread reply] channel={channel} thread={thread_ts}")
    print(f"  Text: {text[:100]}")

    await sessions.process_message(channel, thread_ts, text, say, slack_client=client)


# =============================================================================
# Startup
# =============================================================================

def validate_env() -> bool:
    """Validate required environment variables."""
    errors: list[str] = []

    if not SLACK_BOT_TOKEN:
        errors.append("SLACK_BOT_TOKEN is not set")
    elif not SLACK_BOT_TOKEN.startswith("xoxb-"):
        errors.append("SLACK_BOT_TOKEN should start with 'xoxb-'")

    if not SLACK_APP_TOKEN:
        errors.append("SLACK_APP_TOKEN is not set")
    elif not SLACK_APP_TOKEN.startswith("xapp-"):
        errors.append("SLACK_APP_TOKEN should start with 'xapp-'")

    if errors:
        print("Missing or invalid environment variables:\n")
        for err in errors:
            print(f"  - {err}")
        return False

    # Warnings for optional integrations
    if not os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN"):
        print("Warning: GITHUB_PERSONAL_ACCESS_TOKEN not set — GitHub MCP disabled")
    if not os.environ.get("LINEAR_API_KEY"):
        print("Warning: LINEAR_API_KEY not set — Linear MCP disabled")

    return True


async def cleanup_loop() -> None:
    """Periodically clean up idle sessions."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            await sessions.cleanup_idle()
        except Exception as e:
            print(f"Cleanup error: {e}")


async def main() -> None:
    """Start the Slack Socket Mode listener."""
    print("\n" + "=" * 60)
    print("  AI Developer Teammate — Slack Bot")
    print("=" * 60)
    print(f"\nDefault working dir: {DEFAULT_CWD}")
    print(f"GitHub MCP: {'enabled' if os.environ.get('GITHUB_PERSONAL_ACCESS_TOKEN') else 'disabled'}")
    print(f"Linear MCP: {'enabled' if os.environ.get('LINEAR_API_KEY') else 'disabled'}")
    print()
    print("@mention the bot in any channel to start a conversation.")
    print("Reply in the thread to continue the conversation.")
    print()
    print("Press Ctrl+C to stop.\n")

    # Start idle session cleanup task
    asyncio.create_task(cleanup_loop())

    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    if not validate_env():
        sys.exit(1)

    # Kill any existing instance before starting
    _kill_existing_instance()
    _write_pid_file()
    atexit.register(_cleanup_pid_file)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nStopped by user.")
        sys.exit(0)
