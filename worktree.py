"""
Git Worktree Manager
====================

Creates and manages git worktrees for isolated agent work.
Each implementation session gets its own worktree so the main branch stays clean.
"""

import asyncio
import hashlib
import re
import time
from pathlib import Path


def _slug(text: str, max_len: int = 30) -> str:
    """Turn arbitrary text into a branch-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-")


def _short_id() -> str:
    """Generate a short unique ID from current time."""
    return hashlib.sha1(str(time.time()).encode()).hexdigest()[:6]


async def _run(cmd: str, cwd: Path) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


async def create_worktree(
    repo_path: Path,
    task_description: str = "",
) -> tuple[Path, str]:
    """
    Create a git worktree for isolated work.

    Args:
        repo_path: Path to the main git repo.
        task_description: Short description used to name the branch.

    Returns:
        (worktree_path, branch_name)

    Raises:
        RuntimeError: If worktree creation fails.
    """
    sid = _short_id()
    slug = _slug(task_description) if task_description else "work"
    branch_name = f"bot/{sid}/{slug}"

    # Put worktrees in a sibling directory so they don't clutter the repo
    worktree_root = repo_path.parent / ".worktrees"
    worktree_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_root / f"{sid}-{slug}"

    # Get the current branch to base the worktree on
    rc, current_branch, err = await _run("git rev-parse --abbrev-ref HEAD", repo_path)
    if rc != 0:
        raise RuntimeError(f"Not a git repo or git error: {err}")

    # Create the worktree with a new branch
    cmd = f"git worktree add -b {branch_name} {worktree_path} {current_branch}"
    rc, out, err = await _run(cmd, repo_path)
    if rc != 0:
        raise RuntimeError(f"Failed to create worktree: {err}")

    print(f"  [Worktree] Created {worktree_path} on branch {branch_name}")
    return worktree_path, branch_name


async def cleanup_worktree(repo_path: Path, worktree_path: Path) -> None:
    """
    Remove a git worktree and prune.

    Args:
        repo_path: Path to the main git repo.
        worktree_path: Path to the worktree to remove.
    """
    if not worktree_path.exists():
        return

    # Force-remove the worktree
    rc, _, err = await _run(f"git worktree remove --force {worktree_path}", repo_path)
    if rc != 0:
        print(f"  [Worktree] Warning: could not remove {worktree_path}: {err}")
        return

    # Prune stale worktree references
    await _run("git worktree prune", repo_path)
    print(f"  [Worktree] Removed {worktree_path}")


async def list_worktrees(repo_path: Path) -> list[str]:
    """List active worktrees for the repo."""
    rc, out, _ = await _run("git worktree list --porcelain", repo_path)
    if rc != 0:
        return []
    return [
        line.split(" ", 1)[1]
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]
