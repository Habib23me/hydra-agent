#!/usr/bin/env python3
"""
Slack Bot — Conversational AI Developer Teammate
=================================================

Listens for @mentions via Socket Mode and manages thread-based conversations.
Each Slack thread gets its own Claude SDK session with preserved context.

Usage:
    pm2 start ecosystem.config.cjs
"""

import asyncio
import os
import re
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

from slack_sdk.web.async_client import AsyncWebClient

from session_manager import SessionManager
from task_listener import TaskListener, build_task_listener_config


# =============================================================================
# Environment
# =============================================================================

SLACK_BOT_TOKEN: str = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN: str = os.environ.get("SLACK_APP_TOKEN", "")

# Default working directory for the agent. Can be overridden per-session later.
DEFAULT_CWD: Path = Path(os.environ.get("DEFAULT_CWD", ".")).resolve()

# How often to clean up idle sessions (seconds)
CLEANUP_INTERVAL = 300  # 5 minutes


# =============================================================================
# App setup
# =============================================================================

app = AsyncApp(token=SLACK_BOT_TOKEN)
sessions = SessionManager(default_cwd=DEFAULT_CWD)


# =============================================================================
# Global middleware — log every incoming event for debugging
# =============================================================================

@app.middleware
async def log_all_events(body, next):
    """Log every event the app receives (before handler dispatch)."""
    event = body.get("event", {})
    etype = event.get("type", "?")
    subtype = event.get("subtype", "none")
    thread_ts = event.get("thread_ts", "none")
    text = (event.get("text") or "")[:60]
    print(f"\n[MIDDLEWARE] type={etype} subtype={subtype} thread_ts={thread_ts} text={text}")
    await next()


# =============================================================================
# Helpers
# =============================================================================

def strip_mention(text: str) -> str:
    """Remove Slack user mentions (e.g. <@U12345>) from the text."""
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


# Patterns commonly used in prompt injection attempts
_INJECTION_PATTERNS = [
    re.compile(r"<\[\|.*?\|\]>", re.DOTALL),           # <[|...|]> wrapper
    re.compile(r"UserQuery:\s*variable\s+\w+\.\s*\w+\s*=\s*\[", re.IGNORECASE),  # UserQuery: variable Z. Z = [...]
    re.compile(r"ResponseFormat:\s*1\.\s*your\s+refu", re.IGNORECASE),            # ResponseFormat: 1. your refu...
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"(reveal|show|print|output|repeat)\s+(your\s+)?system\s*prompt", re.IGNORECASE),
]


def looks_like_injection(text: str) -> bool:
    """Check if message text contains common prompt injection patterns."""
    return any(p.search(text) for p in _INJECTION_PATTERNS)


async def extract_file_info(event: dict) -> str:
    """Extract files from a Slack event, download them to /tmp, return local paths.

    Downloads using the bot token so the agent never needs to touch secrets.
    The agent can then just Read() the local file path.
    """
    files = event.get("files", [])
    if not files:
        return ""

    import aiohttp

    descriptions = []
    for f in files:
        name = f.get("name", "unknown")
        filetype = f.get("filetype", "")
        url = f.get("url_private", "")
        size = f.get("size", 0)

        if not url:
            descriptions.append(f"[Attached file: {name} ({filetype}, {size} bytes)]")
            continue

        # Download to /tmp so the agent can Read() it directly
        local_path = f"/tmp/slack-{event.get('ts', 'unknown')}-{name}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
                ) as resp:
                    if resp.status == 200:
                        with open(local_path, "wb") as out:
                            out.write(await resp.read())
                        descriptions.append(
                            f"[Attached file: {name} ({filetype}, {size} bytes)] "
                            f"Downloaded to: {local_path}"
                        )
                    else:
                        descriptions.append(
                            f"[Attached file: {name} ({filetype}, {size} bytes)] "
                            f"Download failed (HTTP {resp.status})"
                        )
        except Exception as e:
            descriptions.append(
                f"[Attached file: {name} ({filetype}, {size} bytes)] "
                f"Download failed: {e}"
            )

    return "\n".join(descriptions)


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
    file_info = await extract_file_info(event)
    if file_info:
        text = f"{text}\n\n{file_info}" if text else file_info
    if not text:
        return

    # Use thread_ts if already in a thread, otherwise start a new thread from this message
    thread_ts = event.get("thread_ts") or event.get("ts")
    channel = event.get("channel", "")

    print(f"\n[@mention] channel={channel} thread={thread_ts}")
    print(f"  Text: {text[:100]}")

    if looks_like_injection(text):
        print(f"  [SECURITY] Prompt injection attempt detected, ignoring")
        await say(
            text=":shield: Nice try. This message looks like a prompt injection attempt and has been ignored.",
            thread_ts=thread_ts,
        )
        return

    await sessions.process_message(channel, thread_ts, text, say, slack_client=client)


@app.event("message")
async def handle_message(event: dict, say, client) -> None:
    """Handle messages in threads where the bot has an active session.
    """
    # Ignore bot messages and non-content subtypes (edits, deletes, etc.)
    if event.get("bot_id"):
        return
    subtype = event.get("subtype")
    if subtype and subtype not in ("file_share",):
        return

    # Only handle thread replies
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    channel = event.get("channel", "")

    # Only respond if we have an active session for this thread
    if not sessions.has_session(channel, thread_ts):
        return

    # Skip @mentions (handled by handle_app_mention) unless it's a file_share
    text_raw = event.get("text", "")
    subtype = event.get("subtype")
    if re.search(r"<@[A-Z0-9]+>", text_raw) and subtype != "file_share":
        return

    text = strip_mention(text_raw) if re.search(r"<@[A-Z0-9]+>", text_raw) else text_raw.strip()
    file_info = await extract_file_info(event)
    if file_info:
        text = f"{text}\n\n{file_info}" if text else file_info

    if not text:
        return

    print(f"\n[Thread reply] channel={channel} thread={thread_ts}")
    print(f"  Text: {text[:100]}")

    if looks_like_injection(text):
        print(f"  [SECURITY] Prompt injection attempt detected, ignoring")
        await say(
            text=":shield: Nice try. This message looks like a prompt injection attempt and has been ignored.",
            thread_ts=thread_ts,
        )
        return

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
    from client import _load_linear_workspaces, _load_sentry_orgs
    linear_ws = _load_linear_workspaces()
    if not linear_ws:
        print("Warning: No Linear API keys configured — Linear MCP disabled")
    sentry_orgs = _load_sentry_orgs()
    if not sentry_orgs:
        print("Warning: No Sentry auth tokens configured — Sentry MCP disabled")

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
    from client import _load_linear_workspaces, _load_sentry_orgs
    linear_ws = _load_linear_workspaces()
    if linear_ws:
        print(f"Linear MCP: enabled ({', '.join(linear_ws.keys())})")
    else:
        print("Linear MCP: disabled")
    sentry_orgs = _load_sentry_orgs()
    if sentry_orgs:
        print(f"Sentry MCP: enabled ({', '.join(sentry_orgs.keys())})")
    else:
        print("Sentry MCP: disabled")
    print()
    print("@mention the bot in any channel to start a conversation.")
    print("Reply in the thread to continue the conversation.")
    print()
    print("Press Ctrl+C to stop.\n")

    # Start idle session cleanup task
    asyncio.create_task(cleanup_loop())

    # Start Linear task listener (auto-pickup assigned issues)
    listener_config = build_task_listener_config()
    if listener_config:
        slack_web_client = AsyncWebClient(token=SLACK_BOT_TOKEN)
        listener = TaskListener(
            slack_client=slack_web_client,
            session_manager=sessions,
            workspaces=listener_config,
        )
        asyncio.create_task(listener.start())
        print(f"Task listener: enabled ({', '.join(listener_config.keys())})")
    else:
        print("Task listener: disabled (no bot_user_id configured)")

    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    if not validate_env():
        sys.exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nStopped by user.")
        # Clean up all sessions to avoid "Unclosed client session" warnings
        try:
            asyncio.run(sessions.close_all())
        except Exception:
            pass
        sys.exit(0)
