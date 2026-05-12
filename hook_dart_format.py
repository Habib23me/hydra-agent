"""
Dart Format Pre-Push Hook
==========================

PreToolUse hook for the Bash tool. Before allowing `git push` to run in a
Dart project worktree (detected by a top-level `pubspec.yaml`), runs
`dart format --set-exit-if-changed lib/`. If anything would be reformatted,
the push is blocked and the agent is told to format + commit + retry.

The hook is best-effort: if the `dart` toolchain is not installed on the
host, the push proceeds. The factory captures the worktree cwd at
client-creation time, so it always points at the right project root.
"""

import asyncio
import re
from pathlib import Path

from claude_agent_sdk import PreToolUseHookInput
from claude_agent_sdk.types import HookContext, SyncHookJSONOutput


# Matches `git push` anywhere in the command, including chained forms like
# `git commit -m '...' && git push origin HEAD` and `git push --force-with-lease`.
_GIT_PUSH_RE = re.compile(r"\bgit\s+push\b")

# Cap on dart format runtime so a hung subprocess can't stall the agent loop.
_DART_FORMAT_TIMEOUT_SECONDS = 30


def make_dart_format_hook(cwd: Path):
    """Build a hook bound to a specific session worktree.

    Args:
        cwd: The session's working directory (typically the worktree path).

    Returns:
        An async PreToolUse hook callback.
    """
    pubspec = cwd / "pubspec.yaml"
    lib_dir = cwd / "lib"

    async def dart_format_hook(
        input_data: PreToolUseHookInput,
        tool_use_id: str | None = None,
        context: HookContext | None = None,
    ) -> SyncHookJSONOutput:
        if input_data.get("tool_name") != "Bash":
            return {}

        command: str = input_data.get("tool_input", {}).get("command", "")
        if not command or not _GIT_PUSH_RE.search(command):
            return {}

        # Not a Dart project, or no lib/ to format — nothing to do.
        if not pubspec.exists() or not lib_dir.exists():
            return {}

        try:
            proc = await asyncio.create_subprocess_exec(
                "dart", "format", "--set-exit-if-changed", "lib/",
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_DART_FORMAT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            # dart toolchain not installed — let the push through. This hook
            # is a safety net, not a hard gate; the project's CI should also
            # enforce formatting.
            return {}
        except asyncio.TimeoutError:
            return SyncHookJSONOutput(
                decision="block",
                reason=(
                    f"`dart format --set-exit-if-changed lib/` timed out after "
                    f"{_DART_FORMAT_TIMEOUT_SECONDS}s. Run it manually and retry the push."
                ),
            )

        if proc.returncode == 0:
            return {}

        output = (
            stdout.decode("utf-8", errors="replace")
            + stderr.decode("utf-8", errors="replace")
        ).strip()
        snippet = output[:600] if output else "(no output)"

        return SyncHookJSONOutput(
            decision="block",
            reason=(
                "Dart formatting check failed before `git push`. "
                "Run `dart format lib/` in your worktree, review and commit "
                "the formatting changes, then push again.\n\n"
                f"dart format output:\n{snippet}"
            ),
        )

    return dart_format_hook
