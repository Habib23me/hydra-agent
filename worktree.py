"""
Git Worktree Manager
====================

Creates and manages git worktrees for isolated agent work.
Each implementation session gets its own worktree so the main branch stays clean.

IMPORTANT: Worktrees are always based off the project's configured base_branch
(from projects.json), NOT whatever HEAD happens to be. This prevents work from
one agent session bleeding into another.
"""

import asyncio
import hashlib
import json
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


def _get_base_branch_for_repo(repo_path: Path) -> str:
    """Look up the base branch for a repo from projects.json.

    Falls back to 'main' if the repo isn't in the registry or has no base_branch.
    """
    projects_file = Path(__file__).parent / "projects.json"
    if not projects_file.exists():
        return "main"

    try:
        data = json.loads(projects_file.read_text())
    except (json.JSONDecodeError, OSError):
        return "main"

    resolved = str(repo_path.resolve())
    for project in data.get("projects", []):
        project_path = project.get("path", "")
        if not project_path:
            continue
        # Match if repo_path is the project path or a parent of it
        if resolved == project_path or resolved.startswith(project_path + "/") or project_path.startswith(resolved + "/"):
            return project.get("base_branch", "main")

    return "main"


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


async def _fetch_base_branch(repo_path: Path, base_branch: str) -> None:
    """Fetch the latest base branch from origin so worktrees start fresh."""
    await _run(f"git fetch origin {base_branch}", repo_path)


async def create_worktree(
    repo_path: Path,
    task_description: str = "",
    base_branch: str | None = None,
) -> tuple[Path, str]:
    """
    Create a git worktree for isolated work.

    Always branches from the project's base_branch (looked up from projects.json),
    NOT from the current HEAD. This ensures clean worktrees without other agents' work.

    Args:
        repo_path: Path to the main git repo.
        task_description: Short description used to name the branch.
        base_branch: Override base branch (defaults to project config or 'main').

    Returns:
        (worktree_path, branch_name)

    Raises:
        RuntimeError: If worktree creation fails.
    """
    if base_branch is None:
        base_branch = _get_base_branch_for_repo(repo_path)

    # Fetch latest base branch to avoid stale starts
    await _fetch_base_branch(repo_path, base_branch)

    sid = _short_id()
    slug = _slug(task_description) if task_description else "work"
    branch_name = f"bot/{sid}/{slug}"

    # Put worktrees in a sibling directory so they don't clutter the repo
    worktree_root = repo_path.parent / ".worktrees"
    worktree_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_root / f"{sid}-{slug}"

    # Verify the base branch exists
    rc, _, err = await _run(f"git rev-parse --verify {base_branch}", repo_path)
    if rc != 0:
        # Try origin/base_branch
        rc, _, err = await _run(f"git rev-parse --verify origin/{base_branch}", repo_path)
        if rc != 0:
            raise RuntimeError(f"Base branch '{base_branch}' not found locally or on origin: {err}")
        # Use the remote ref
        start_point = f"origin/{base_branch}"
    else:
        start_point = base_branch

    # Create the worktree with a new branch from the base branch
    cmd = f"git worktree add -b {branch_name} {worktree_path} {start_point}"
    rc, out, err = await _run(cmd, repo_path)

    if rc != 0:
        # Handle branch name collision: append short id
        if "already exists" in err:
            branch_name = f"bot/{sid}/{slug}-{_short_id()}"
            cmd = f"git worktree add -b {branch_name} {worktree_path} {start_point}"
            rc, out, err = await _run(cmd, repo_path)
            if rc != 0:
                raise RuntimeError(f"Failed to create worktree (retry): {err}")
        else:
            raise RuntimeError(f"Failed to create worktree: {err}")

    print(f"  [Worktree] Created {worktree_path} on branch {branch_name} (from {start_point})")
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
