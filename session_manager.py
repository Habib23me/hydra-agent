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
from client import create_session_client, resolve_cwd_for_channel
from worktree import cleanup_worktree, create_worktree


# Default model for new sessions
DEFAULT_MODEL = "claude-sonnet-4-6"

# Lightweight model for simple Q&A (ticket lookups, status, short answers)
LIGHT_MODEL = "claude-haiku-4-5"

# How long before an idle session is cleaned up (30 minutes)
IDLE_TIMEOUT_SECONDS = 1800

# Max retries when the SDK client fails
MAX_RECONNECT_RETRIES = 2

# Cost guard: max USD spend per session before auto-stopping
MAX_SESSION_COST_USD = float(os.environ.get("MAX_SESSION_COST_USD", "10.0"))

# Warn when session reaches this fraction of the budget (e.g. 80%)
BUDGET_WARN_THRESHOLD = 0.8

# Max consecutive errors before circuit-breaking
MAX_CONSECUTIVE_ERRORS = 5

# Max user turns per session before forcing a new thread
MAX_SESSION_TURNS = int(os.environ.get("MAX_SESSION_TURNS", "30"))

# Turn count threshold: after this many turns, start compacting context
COMPACT_AFTER_TURNS = 15


def _is_lightweight_message(text: str) -> bool:
    """Determine if a message can be handled by the cheaper Haiku model.

    Simple heuristic: short messages without coding intent use Haiku.
    Anything that looks like a coding task gets Sonnet.
    """
    text_lower = text.lower().strip()

    # Short messages (under 100 chars) that are just questions/status
    if len(text_lower) > 300:
        return False

    # Coding keywords → needs Sonnet
    coding_signals = [
        "implement", "build", "create pr", "fix", "refactor", "debug",
        "write code", "add feature", "update the", "change the",
        "commit", "push", "deploy", "migrate", "write test",
        "investigate", "resolve", "PR", "pull request",
        "edit", "modify", "rewrite",
    ]
    if any(signal in text_lower for signal in coding_signals):
        return False

    # Q&A / status patterns → Haiku is fine
    light_signals = [
        "what", "how many", "status", "list", "show me", "tell me",
        "who", "when", "where", "which", "?",
        "yes", "no", "ok", "thanks", "cool", "nice",
        "lol", "haha",
    ]
    if any(signal in text_lower for signal in light_signals):
        return True

    # Default: if it's short enough, use Haiku
    return len(text_lower) < 80


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
    # Turn counter (user messages only)
    turn_count: int = 0
    # Whether we've compacted context in this session
    _compacted: bool = False


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
        is_new_session = key not in self._sessions
        if is_new_session:
            # Resolve CWD from channel → project mapping, fall back to default
            session_cwd = resolve_cwd_for_channel(channel, self._default_cwd)
            self._sessions[key] = ThreadSession(
                channel=channel,
                thread_ts=thread_ts,
                cwd=session_cwd,
            )

        session = self._sessions[key]

        # Auto-create worktree for new sessions in project repos
        # This ensures the agent never commits directly to main
        if is_new_session and session.cwd != self._default_cwd and not session.worktree_path:
            try:
                wt_result = await self.create_session_worktree(
                    channel, thread_ts, task_description=user_text[:60],
                )
                if wt_result:
                    print(f"  [Session {key}] Auto-created worktree at {wt_result}")
            except Exception as e:
                print(f"  [Session {key}] Worktree auto-create failed: {e}")

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

        # Turn limit: force new thread after too many turns
        if session.turn_count >= MAX_SESSION_TURNS:
            await say(
                text=f":warning: Session turn limit reached ({session.turn_count}/{MAX_SESSION_TURNS}). Start a new thread to keep context fresh and costs down.",
                thread_ts=thread_ts,
            )
            return

        # Serialize access to this session (one message at a time)
        async with session._lock:
            session.last_activity = time.time()
            session.turn_count += 1
            session.messages.append({"role": "user", "text": user_text})

            # Context compaction: after many turns, disconnect and reconnect
            # with a summarized history to reduce context size and cost
            if session.turn_count == COMPACT_AFTER_TURNS and not session._compacted:
                print(f"  [Session {key}] Compacting context at turn {session.turn_count}")
                await self._disconnect_session(session)
                session._compacted = True

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
                    # Slack messages have a ~40k char / ~40k byte limit.
                    # For streaming updates, keep it shorter to avoid msg_too_long.
                    if len(display.encode("utf-8")) > 3800:
                        display = display[-3800:]
                    if not is_final:
                        display += "\n\n:writing_hand: _typing..._"
                    await slack_client.chat_update(
                        channel=channel,
                        ts=thinking_ts,
                        text=display,
                    )
                except Exception as e:
                    err_str = str(e)
                    if "msg_too_long" in err_str:
                        # Truncate harder and retry once
                        try:
                            display = text.strip()[-2000:]
                            await slack_client.chat_update(
                                channel=channel, ts=thinking_ts, text=display,
                            )
                        except Exception:
                            pass
                    else:
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

                        # Hard stop: if we've blown past the budget after this turn, lock the session
                        if session.total_cost_usd >= MAX_SESSION_COST_USD:
                            budget_msg = f"\n\n:warning: _Session budget exhausted (${session.total_cost_usd:.2f} / ${MAX_SESSION_COST_USD:.2f}). Start a new thread to continue._"
                            text += budget_msg
                        # Soft warning at threshold
                        elif session.total_cost_usd >= MAX_SESSION_COST_USD * BUDGET_WARN_THRESHOLD:
                            remaining = MAX_SESSION_COST_USD - session.total_cost_usd
                            text += f"\n\n:warning: _Budget warning: ${remaining:.2f} remaining in this session._"
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

        Uses model routing: simple Q&A goes to Haiku, coding tasks to Sonnet.
        If the SDK client crashes, disconnect it, create a fresh one,
        replay conversation history, and retry the current message.
        """
        # Model routing: pick the right model for this message
        use_model = session.model
        if _is_lightweight_message(user_text):
            use_model = LIGHT_MODEL
            print(f"  [Router] Using {LIGHT_MODEL} for lightweight message")

        for attempt in range(1 + MAX_RECONNECT_RETRIES):
            try:
                if session.client is None:
                    # Use the routed model for new clients, but keep session
                    # default for reconnects after compaction
                    model_for_client = use_model if session.turn_count <= 1 else session.model
                    session.client = create_session_client(
                        cwd=session.cwd,
                        model=model_for_client,
                    )
                    await session.client.connect()

                    # Replay history if we have prior messages
                    if len(session.messages) > 1:
                        # After compaction, send a condensed summary instead of full history
                        if session._compacted:
                            history = self._format_compact_history(session.messages[:-1])
                        else:
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
    def _format_compact_history(messages: list[dict]) -> str:
        """Format a condensed version of history for post-compaction replay.

        Keeps the first message (original task), last 4 exchanges, and
        truncates everything in between to save context tokens.
        """
        if len(messages) <= 8:
            # Short enough, no need to compact
            lines = []
            for msg in messages:
                role = "User" if msg["role"] == "user" else "You"
                lines.append(f"[{role}]: {msg['text']}")
            return "\n".join(lines)

        lines = ["[COMPACTED CONTEXT — showing first message and recent exchanges]\n"]

        # First message (original task)
        first = messages[0]
        role = "User" if first["role"] == "user" else "You"
        lines.append(f"[{role} (original request)]: {first['text']}")

        # Middle summary
        skipped = len(messages) - 5
        lines.append(f"\n[... {skipped} earlier messages omitted for context efficiency ...]\n")

        # Last 4 messages (most recent context)
        for msg in messages[-4:]:
            role = "User" if msg["role"] == "user" else "You"
            # Truncate long assistant messages in history
            text = msg["text"]
            if role == "You" and len(text) > 500:
                text = text[:500] + "... [truncated]"
            lines.append(f"[{role}]: {text}")

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
