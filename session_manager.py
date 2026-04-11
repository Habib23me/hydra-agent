"""
Session Manager
===============

Manages per-Slack-thread Claude SDK sessions. Each thread gets a long-lived
ClaudeSDKClient that preserves conversation context across messages.

Features:
- Streaming: Posts a "thinking..." message and updates it live as the response arrives.
- Worktrees: Optionally creates git worktrees for isolated coding work.
- Error recovery: Graceful handling of SDK failures with session reconnect.
"""

import asyncio
import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from slack_sdk.web.async_client import AsyncWebClient

from claude_agent_sdk import ClaudeSDKClient

from agent import TurnResult, run_turn
from client import create_session_client
from worktree import cleanup_worktree, create_worktree


# Default model for new sessions (haiku for conversational chat)
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# How long before an idle session is cleaned up (30 minutes)
IDLE_TIMEOUT_SECONDS = 1800

# Max retries when the SDK client fails
MAX_RECONNECT_RETRIES = 2

# Cost guard: max USD spend per session before auto-stopping
MAX_SESSION_COST_USD = float(os.environ.get("MAX_SESSION_COST_USD", "1.0"))

# Max consecutive errors before circuit-breaking
MAX_CONSECUTIVE_ERRORS = 5


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
    # Worktree state
    worktree_path: Path | None = None
    worktree_branch: str | None = None
    # Streaming state — ts of the live-updating message
    _stream_msg_ts: str | None = None
    # Track consecutive errors for circuit-breaking
    _consecutive_errors: int = 0
    # Cost tracking
    total_cost_usd: float = 0.0


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
        slack_client: AsyncWebClient | None = None,
    ) -> None:
        """
        Process a user message in a Slack thread.

        Posts a "thinking..." indicator, streams the response via chat.update,
        and handles errors with automatic reconnect.
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

        # Budget guard: stop if session has spent too much
        if session.total_cost_usd >= MAX_SESSION_COST_USD:
            await say(
                text=f":warning: Session budget reached (${session.total_cost_usd:.2f} / ${MAX_SESSION_COST_USD:.2f}). Start a new thread to continue.",
                thread_ts=thread_ts,
            )
            return

        # Circuit breaker: stop if too many consecutive errors
        if session._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            await say(
                text=f":warning: Too many errors in a row ({session._consecutive_errors}). Start a new thread to reset.",
                thread_ts=thread_ts,
            )
            return

        # Serialize access to this session (one message at a time)
        async with session._lock:
            session.last_activity = time.time()
            session.messages.append({"role": "user", "text": user_text})

            # Post a "thinking..." message that we'll update with streamed content
            thinking_ts = None
            if slack_client:
                try:
                    result = await slack_client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=":hourglass_flowing_sand: Thinking...",
                    )
                    thinking_ts = result.get("ts")
                    session._stream_msg_ts = thinking_ts
                except Exception as e:
                    print(f"  [Stream] Could not post thinking message: {e}")

            # Build the streaming callback
            async def on_stream(text: str, is_final: bool) -> None:
                if not slack_client or not thinking_ts:
                    return
                try:
                    display = text.strip()
                    if len(display) > 3900:
                        display = display[-3900:]
                    if not is_final:
                        display += "\n\n:writing_hand: _typing..._"
                    await slack_client.chat_update(
                        channel=channel,
                        ts=thinking_ts,
                        text=display,
                    )
                except Exception as e:
                    print(f"  [Stream] Update failed: {e}")

            try:
                result = await self._run_with_recovery(
                    session, user_text, key,
                    on_stream=on_stream if slack_client else None,
                )

                # Post response to Slack thread
                if result.error:
                    error_text = f":warning: Something went wrong: {result.error}"
                    if thinking_ts and slack_client:
                        await slack_client.chat_update(
                            channel=channel, ts=thinking_ts, text=error_text,
                        )
                    else:
                        await say(text=error_text, thread_ts=thread_ts)
                    session._consecutive_errors += 1
                elif result.response_text:
                    text = result.response_text.strip()
                    session._consecutive_errors = 0

                    if thinking_ts and slack_client:
                        # Update the thinking message with the final text
                        if len(text.encode("utf-8")) <= 3800:
                            try:
                                await slack_client.chat_update(
                                    channel=channel, ts=thinking_ts, text=text,
                                )
                            except Exception:
                                # Fallback: delete thinking msg and post as new
                                await slack_client.chat_delete(
                                    channel=channel, ts=thinking_ts,
                                )
                                await say(text=text, thread_ts=thread_ts)
                        else:
                            # Delete thinking msg and post chunks
                            try:
                                await slack_client.chat_delete(
                                    channel=channel, ts=thinking_ts,
                                )
                            except Exception:
                                pass
                            for chunk in self._split_text(text, 3800):
                                await say(text=chunk, thread_ts=thread_ts)
                    else:
                        # Fallback: no slack_client, use say()
                        if len(text) <= 3900:
                            await say(text=text, thread_ts=thread_ts)
                        else:
                            for chunk in self._split_text(text, 3900):
                                await say(text=chunk, thread_ts=thread_ts)

                    session.messages.append(
                        {"role": "assistant", "text": result.response_text}
                    )

                    if result.cost_usd is not None:
                        session.total_cost_usd += result.cost_usd
                        print(f"  [Session {key}] Turn: ${result.cost_usd:.4f} | Total: ${session.total_cost_usd:.4f} / ${MAX_SESSION_COST_USD:.2f}")
                else:
                    no_resp = "(No response generated)"
                    if thinking_ts and slack_client:
                        await slack_client.chat_update(
                            channel=channel, ts=thinking_ts, text=no_resp,
                        )
                    else:
                        await say(text=no_resp, thread_ts=thread_ts)

            except Exception as e:
                print(f"Error in session {key}: {e}")
                traceback.print_exc()
                error_text = f":x: Hit an error: {type(e).__name__}: {e}"
                if thinking_ts and slack_client:
                    try:
                        await slack_client.chat_update(
                            channel=channel, ts=thinking_ts, text=error_text,
                        )
                    except Exception:
                        await say(text=error_text, thread_ts=thread_ts)
                else:
                    await say(text=error_text, thread_ts=thread_ts)
                session._consecutive_errors += 1
                await self._disconnect_session(session)

    async def _run_with_recovery(
        self,
        session: ThreadSession,
        user_text: str,
        key: str,
        on_stream=None,
    ) -> TurnResult:
        """
        Run a turn with automatic reconnect on failure.

        If the SDK client crashes, disconnect it, create a fresh one,
        replay conversation history, and retry the current message.
        """
        for attempt in range(1 + MAX_RECONNECT_RETRIES):
            try:
                if session.client is None:
                    session.client = create_session_client(
                        cwd=session.cwd,
                        model=session.model,
                    )
                    await session.client.connect()

                    # Replay history if we have prior messages
                    if len(session.messages) > 1:
                        history = self._format_history(session.messages[:-1])
                        context_msg = (
                            f"Here is the conversation so far in this Slack thread:\n"
                            f"{history}\n---\n"
                            f"The user just said: {user_text}"
                        )
                        return await run_turn(
                            session.client, context_msg, on_stream=on_stream,
                        )
                    else:
                        return await run_turn(
                            session.client, user_text, on_stream=on_stream,
                        )
                else:
                    return await run_turn(
                        session.client, user_text, on_stream=on_stream,
                    )

            except Exception as e:
                print(f"  [Recovery] Attempt {attempt + 1} failed: {e}")
                traceback.print_exc()
                await self._disconnect_session(session)

                if attempt >= MAX_RECONNECT_RETRIES:
                    return TurnResult(
                        response_text="",
                        cost_usd=None,
                        error=f"Failed after {attempt + 1} attempts: {e}",
                    )

                print(f"  [Recovery] Reconnecting (attempt {attempt + 2})...")

        # Should not reach here
        return TurnResult(response_text="", cost_usd=None, error="Recovery exhausted")

    # ── Worktree management ──────────────────────────────────────────────

    async def create_session_worktree(
        self, channel: str, thread_ts: str, task_description: str = "",
    ) -> Path | None:
        """Create a worktree for a session and switch its cwd."""
        session = self.get_session(channel, thread_ts)
        if not session:
            return None

        if session.worktree_path:
            return session.worktree_path  # Already has one

        try:
            wt_path, branch = await create_worktree(
                repo_path=session.cwd, task_description=task_description,
            )
            session.worktree_path = wt_path
            session.worktree_branch = branch
            # Point the session's working directory at the worktree
            session.cwd = wt_path
            return wt_path
        except Exception as e:
            print(f"  [Worktree] Failed to create: {e}")
            return None

    async def cleanup_session_worktree(
        self, channel: str, thread_ts: str,
    ) -> None:
        """Clean up the worktree for a session."""
        session = self.get_session(channel, thread_ts)
        if not session or not session.worktree_path:
            return

        try:
            await cleanup_worktree(self._default_cwd, session.worktree_path)
        except Exception as e:
            print(f"  [Worktree] Cleanup failed: {e}")
        finally:
            session.worktree_path = None
            session.worktree_branch = None
            session.cwd = self._default_cwd

    # ── Session lifecycle ────────────────────────────────────────────────

    async def close_session(self, channel: str, thread_ts: str) -> None:
        """Close and remove a session, cleaning up worktree if any."""
        key = self._key(channel, thread_ts)
        session = self._sessions.pop(key, None)
        if session:
            if session.worktree_path:
                try:
                    await cleanup_worktree(self._default_cwd, session.worktree_path)
                except Exception:
                    pass
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
            if session.worktree_path:
                try:
                    await cleanup_worktree(self._default_cwd, session.worktree_path)
                except Exception:
                    pass
            await self._disconnect_session(session)

    async def close_all(self) -> None:
        """Close all active sessions."""
        for key in list(self._sessions.keys()):
            session = self._sessions.pop(key)
            if session.worktree_path:
                try:
                    await cleanup_worktree(self._default_cwd, session.worktree_path)
                except Exception:
                    pass
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

            split_at = text.rfind("\n\n", 0, max_len)
            if split_at == -1:
                split_at = text.rfind("\n", 0, max_len)
            if split_at == -1:
                split_at = text.rfind(". ", 0, max_len)
                if split_at != -1:
                    split_at += 1
            if split_at == -1:
                split_at = max_len

            chunks.append(text[:split_at].rstrip())
            text = text[split_at:].lstrip()

        return chunks
