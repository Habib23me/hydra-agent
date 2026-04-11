"""
Session Manager
===============

Manages per-Slack-thread Claude SDK sessions. Each thread gets a long-lived
ClaudeSDKClient that preserves conversation context across messages.
"""

import asyncio
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient

from agent import TurnResult, run_turn
from client import create_session_client


# Default model for new sessions (haiku for conversational chat)
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# How long before an idle session is cleaned up (30 minutes)
IDLE_TIMEOUT_SECONDS = 1800


@dataclass
class ThreadSession:
    """State for a single Slack thread conversation."""

    channel: str
    thread_ts: str
    client: ClaudeSDKClient | None = None
    cwd: Path = field(default_factory=lambda: Path.cwd())
    model: str = DEFAULT_MODEL
    messages: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionManager:
    """Manages Claude SDK sessions per Slack thread."""

    def __init__(self, default_cwd: Path | None = None):
        self._sessions: dict[str, ThreadSession] = {}
        self._default_cwd = default_cwd or Path.cwd()

    def _key(self, channel: str, thread_ts: str) -> str:
        return f"{channel}:{thread_ts}"

    def get_session(self, channel: str, thread_ts: str) -> ThreadSession | None:
        return self._sessions.get(self._key(channel, thread_ts))

    def has_session(self, channel: str, thread_ts: str) -> bool:
        return self._key(channel, thread_ts) in self._sessions

    async def process_message(
        self,
        channel: str,
        thread_ts: str,
        user_text: str,
        say,
    ) -> None:
        """
        Process a user message in a Slack thread.

        Gets or creates a session for the thread, sends the message to the
        Claude SDK client, and posts the response back to the thread.

        Args:
            channel: Slack channel ID
            thread_ts: Thread timestamp (identifies the thread)
            user_text: The user's message text
            say: Slack say() function for posting replies
        """
        key = self._key(channel, thread_ts)

        # Get or create session
        if key not in self._sessions:
            self._sessions[key] = ThreadSession(
                channel=channel,
                thread_ts=thread_ts,
                cwd=self._default_cwd,
            )

        session = self._sessions[key]

        # Serialize access to this session (one message at a time)
        async with session._lock:
            session.last_activity = time.time()
            session.messages.append({"role": "user", "text": user_text})

            try:
                # Create client if needed
                if session.client is None:
                    session.client = create_session_client(
                        cwd=session.cwd,
                        model=session.model,
                    )
                    await session.client.connect()

                    # If we have prior messages, inject history as context
                    if len(session.messages) > 1:
                        history = self._format_history(session.messages[:-1])
                        context_msg = (
                            f"Here is the conversation so far in this Slack thread:\n"
                            f"{history}\n---\n"
                            f"The user just said: {user_text}"
                        )
                        result = await run_turn(session.client, context_msg)
                    else:
                        result = await run_turn(session.client, user_text)
                else:
                    # Session exists — just send the follow-up message
                    result = await run_turn(session.client, user_text)

                # Post response to Slack thread
                if result.error:
                    await say(
                        text=f"Something went wrong: {result.error}",
                        thread_ts=thread_ts,
                    )
                elif result.response_text:
                    # Slack has a 4000 char limit per message — split if needed
                    text = result.response_text.strip()
                    if len(text) <= 3900:
                        await say(text=text, thread_ts=thread_ts)
                    else:
                        # Split into chunks at paragraph boundaries
                        for chunk in self._split_text(text, 3900):
                            await say(text=chunk, thread_ts=thread_ts)

                    session.messages.append(
                        {"role": "assistant", "text": result.response_text}
                    )

                    if result.cost_usd is not None:
                        print(
                            f"  [Session {key}] Turn cost: ${result.cost_usd:.4f}"
                        )
                else:
                    await say(
                        text="(No response generated)",
                        thread_ts=thread_ts,
                    )

            except Exception as e:
                print(f"Error in session {key}: {e}")
                traceback.print_exc()
                await say(
                    text=f"Hit an error: {type(e).__name__}: {e}",
                    thread_ts=thread_ts,
                )
                # Kill the broken client so next message creates a fresh one
                await self._disconnect_session(session)

    async def close_session(self, channel: str, thread_ts: str) -> None:
        """Close and remove a session."""
        key = self._key(channel, thread_ts)
        session = self._sessions.pop(key, None)
        if session:
            await self._disconnect_session(session)

    async def cleanup_idle(self, max_idle_seconds: int = IDLE_TIMEOUT_SECONDS) -> None:
        """Close sessions that have been idle too long."""
        now = time.time()
        stale_keys = [
            key
            for key, session in self._sessions.items()
            if now - session.last_activity > max_idle_seconds
        ]
        for key in stale_keys:
            session = self._sessions.pop(key)
            print(f"  [Cleanup] Closing idle session {key}")
            await self._disconnect_session(session)

    async def close_all(self) -> None:
        """Close all active sessions."""
        for key in list(self._sessions.keys()):
            session = self._sessions.pop(key)
            await self._disconnect_session(session)

    @staticmethod
    async def _disconnect_session(session: ThreadSession) -> None:
        if session.client:
            try:
                await session.client.disconnect()
            except Exception:
                pass
            session.client = None

    @staticmethod
    def _format_history(messages: list[dict]) -> str:
        lines = []
        for msg in messages:
            role = "User" if msg["role"] == "user" else "You"
            lines.append(f"[{role}]: {msg['text']}")
        return "\n".join(lines)

    @staticmethod
    def _split_text(text: str, max_len: int) -> list[str]:
        """Split text into chunks, preferring paragraph boundaries."""
        if len(text) <= max_len:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break

            # Find a good split point (paragraph, then sentence, then hard cut)
            split_at = text.rfind("\n\n", 0, max_len)
            if split_at == -1:
                split_at = text.rfind("\n", 0, max_len)
            if split_at == -1:
                split_at = text.rfind(". ", 0, max_len)
                if split_at != -1:
                    split_at += 1  # Include the period
            if split_at == -1:
                split_at = max_len

            chunks.append(text[:split_at].rstrip())
            text = text[split_at:].lstrip()

        return chunks
