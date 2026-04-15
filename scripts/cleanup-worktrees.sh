#!/usr/bin/env bash
#
# cleanup-worktrees.sh
# ====================
# Resets all project repos to their default branch and removes all worktrees.
# Safe to run anytime — stops hydra-agent first, cleans up, restarts.
#
# Usage:
#   ./scripts/cleanup-worktrees.sh          # full cleanup + restart
#   ./scripts/cleanup-worktrees.sh --dry    # show what would happen
#

set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry" ]] && DRY_RUN=true

# ── Project definitions (repo_path:default_branch) ──────────────────────
PROJECTS=(
    "/Volumes/mac_mini/gymlive/mobile:main"
    "/Volumes/mac_mini/gymlive/backend:main"
    "/Volumes/mac_mini/gymlive/strapi-cloud-template-blog-2ef4165a33:main"
    "/Volumes/mac_mini/viaslim-ag/viaslim-mobile-app:main"
    "/Volumes/mac_mini/viaslim-ag/viaslim-backend:dev/latest"
    "/Volumes/mac_mini/viaslim-ag/viaslim-singlepage-onboarding:main"
)

# Worktree root directories to nuke
WORKTREE_ROOTS=(
    "/Volumes/mac_mini/gymlive/.worktrees"
    "/Volumes/mac_mini/viaslim-ag/.worktrees"
)

echo "============================================"
echo "  Hydra Agent — Worktree Cleanup"
echo "============================================"
echo ""
$DRY_RUN && echo "** DRY RUN — no changes will be made **" && echo ""

# ── Step 1: Stop the agent ──────────────────────────────────────────────
echo "→ Stopping hydra-agent..."
if $DRY_RUN; then
    echo "  [dry] pm2 stop hydra-agent"
else
    pm2 stop hydra-agent 2>/dev/null || true
    sleep 1
fi

# ── Step 2: Remove all worktrees via git ─────────────────────────────────
echo ""
echo "→ Removing git worktrees..."
for entry in "${PROJECTS[@]}"; do
    repo="${entry%%:*}"

    if [[ ! -d "$repo/.git" ]] && [[ ! -f "$repo/.git" ]]; then
        echo "  [skip] $repo — not a git repo"
        continue
    fi

    # List worktrees (skip the main one)
    worktrees=$(git -C "$repo" worktree list --porcelain 2>/dev/null | grep "^worktree " | awk '{print $2}' | tail -n +2)

    if [[ -z "$worktrees" ]]; then
        echo "  [ok] $repo — no worktrees"
        continue
    fi

    while IFS= read -r wt; do
        echo "  [remove] $wt"
        if ! $DRY_RUN; then
            git -C "$repo" worktree remove --force "$wt" 2>/dev/null || {
                echo "  [warn] force remove failed, deleting directory"
                rm -rf "$wt"
            }
        fi
    done <<< "$worktrees"

    # Prune stale references
    if ! $DRY_RUN; then
        git -C "$repo" worktree prune 2>/dev/null || true
    fi
done

# ── Step 3: Nuke leftover worktree directories ──────────────────────────
echo ""
echo "→ Cleaning leftover worktree directories..."
for root in "${WORKTREE_ROOTS[@]}"; do
    if [[ -d "$root" ]]; then
        count=$(ls -1 "$root" 2>/dev/null | wc -l | tr -d ' ')
        echo "  [remove] $root ($count dirs)"
        if ! $DRY_RUN; then
            rm -rf "$root"
        fi
    else
        echo "  [ok] $root — already clean"
    fi
done

# ── Step 4: Reset repos to default branch ───────────────────────────────
echo ""
echo "→ Resetting repos to default branches..."
for entry in "${PROJECTS[@]}"; do
    repo="${entry%%:*}"
    branch="${entry##*:}"

    if [[ ! -d "$repo/.git" ]] && [[ ! -f "$repo/.git" ]]; then
        echo "  [skip] $repo — not a git repo"
        continue
    fi

    current=$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "DETACHED")

    if [[ "$current" == "$branch" ]]; then
        echo "  [ok] $repo — already on $branch"
    else
        echo "  [reset] $repo — $current → $branch"
        if ! $DRY_RUN; then
            git -C "$repo" checkout "$branch" --force 2>/dev/null || {
                echo "  [warn] checkout failed, trying fetch + checkout"
                git -C "$repo" fetch origin "$branch" 2>/dev/null
                git -C "$repo" checkout "$branch" --force 2>/dev/null || echo "  [ERROR] Could not checkout $branch"
            }
            # Pull latest
            git -C "$repo" pull --ff-only 2>/dev/null || true
        fi
    fi
done

# ── Step 5: Clean up stale bot branches ─────────────────────────────────
echo ""
echo "→ Cleaning stale bot/* branches..."
for entry in "${PROJECTS[@]}"; do
    repo="${entry%%:*}"

    if [[ ! -d "$repo/.git" ]] && [[ ! -f "$repo/.git" ]]; then
        continue
    fi

    bot_branches=$(git -C "$repo" branch --list "bot/*" 2>/dev/null | sed 's/^[* ]*//')

    if [[ -z "$bot_branches" ]]; then
        continue
    fi

    while IFS= read -r br; do
        echo "  [delete] $repo — $br"
        if ! $DRY_RUN; then
            git -C "$repo" branch -D "$br" 2>/dev/null || true
        fi
    done <<< "$bot_branches"
done

# ── Step 6: Restart the agent ───────────────────────────────────────────
echo ""
echo "→ Restarting hydra-agent..."
if $DRY_RUN; then
    echo "  [dry] pm2 restart hydra-agent"
else
    pm2 restart hydra-agent
fi

echo ""
echo "✓ Cleanup complete."
