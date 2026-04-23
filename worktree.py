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


async def _sync_base_branch(repo_path: Path, base_branch: str) -> None:
    """Fetch the base branch from origin and fast-forward the local ref.

    `git fetch origin {base_branch}` only updates refs/remotes/origin/{base_branch}
    — the local ref stays where it was. Worktrees previously branched from the
    stale local ref, so a primary checkout left sitting on an old main produced
    stale worktrees. Here we also run `git fetch origin {base_branch}:{base_branch}`
    which fast-forwards the local ref to the remote tip. It fails (harmlessly)
    if the local branch is currently checked out in a worktree or has diverged;
    in those cases create_worktree still uses origin/{base_branch} as the
    start point, so the worktree is always fresh regardless.
    """
    rc, _, err = await _run(f"git fetch origin {base_branch}", repo_path)
    if rc != 0:
        # Not fatal — we'll still try to use whatever origin/{base_branch}
        # points at locally, but warn loudly so a stale ref is visible.
        print(f"  [Worktree] WARNING: git fetch origin {base_branch} failed: {err}")
    # Best-effort local fast-forward so the primary checkout doesn't rot either.
    rc, _, err = await _run(
        f"git fetch origin {base_branch}:{base_branch}", repo_path
    )
    if rc != 0 and err:
        # Common causes: branch is currently checked out, or local has diverged.
        # Not fatal — the worktree will still branch from origin/{base_branch}.
        print(f"  [Worktree] Note: could not fast-forward local {base_branch}: {err}")


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

    # Fetch latest base branch and sync the local ref to avoid stale starts.
    await _sync_base_branch(repo_path, base_branch)

    sid = _short_id()
    slug = _slug(task_description) if task_description else "work"
    branch_name = f"bot/{sid}/{slug}"

    # Put worktrees in a sibling directory so they don't clutter the repo
    worktree_root = repo_path.parent / ".worktrees"
    worktree_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_root / f"{sid}-{slug}"

    # Always prefer origin/{base_branch} so the worktree starts from the
    # freshly-fetched remote tip. The local ref may be stale or diverged
    # if the primary checkout was left on base_branch without pulling.
    rc, _, err = await _run(f"git rev-parse --verify origin/{base_branch}", repo_path)
    if rc == 0:
        start_point = f"origin/{base_branch}"
    else:
        # Offline / no remote — fall back to the local ref.
        rc, _, err = await _run(f"git rev-parse --verify {base_branch}", repo_path)
        if rc != 0:
            raise RuntimeError(f"Base branch '{base_branch}' not found locally or on origin: {err}")
        start_point = base_branch

    # Capture the SHA we INTEND to branch from. After worktree creation we'll
    # verify the worktree's HEAD matches this — a tripwire that turns any
    # future regression (stale ref resolution, wrong start_point, etc.) into
    # a loud failure at creation time instead of a silently-broken PR later.
    rc, expected_sha, err = await _run(f"git rev-parse {start_point}", repo_path)
    if rc != 0:
        raise RuntimeError(f"Could not resolve {start_point} to a SHA: {err}")

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

    # Tripwire: verify the worktree actually starts where we expected. If
    # anything in the logic above ever drifts back to using a stale ref,
    # this fires immediately with a clear error instead of producing a
    # polluted PR hours later.
    rc, actual_sha, err = await _run("git rev-parse HEAD", worktree_path)
    if rc != 0 or actual_sha != expected_sha:
        # Tear down the broken worktree so we don't leave it lying around.
        await _run(f"git worktree remove --force {worktree_path}", repo_path)
        raise RuntimeError(
            f"Worktree HEAD mismatch: expected {expected_sha} (from {start_point}) "
            f"but got {actual_sha!r}. Worktree torn down. "
            f"This indicates a stale base branch or a bug in create_worktree."
        )

    # Symlink each entry under .claude/ from the source repo so skills,
    # settings, and custom agents are discoverable from the worktree. We link
    # individual entries (not the whole .claude/ dir) because some projects
    # partially track .claude/ in git — e.g. .claude/worktrees/ committed but
    # .claude/skills/ gitignored. A whole-dir symlink would be skipped when
    # the worktree already has a real .claude/ from tracked content.
    claude_src = repo_path / ".claude"
    if claude_src.is_dir():
        claude_dst = worktree_path / ".claude"
        claude_dst.mkdir(exist_ok=True)
        linked = []
        for entry in claude_src.iterdir():
            target = claude_dst / entry.name
            if target.exists() or target.is_symlink():
                continue
            target.symlink_to(entry.resolve())
            linked.append(entry.name)
        if linked:
            print(f"  [Worktree] Linked .claude/{{{','.join(linked)}}} -> {claude_src.resolve()}")

    print(
        f"  [Worktree] Created {worktree_path} on branch {branch_name} "
        f"(from {start_point} @ {expected_sha[:8]})"
    )
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
