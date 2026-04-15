"""
Task Listener — Linear Issue Auto-Pickup
=========================================

Polls Linear workspaces for issues assigned to the bot's user account.
When a new assignment is detected, posts a Slack thread and starts a session.

The bot picks up issues in "backlog" or "unstarted" (Todo) states.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from slack_sdk.web.async_client import AsyncWebClient


# How often to poll Linear for new assignments (seconds)
POLL_INTERVAL = int(os.environ.get("TASK_POLL_INTERVAL", "30"))

# File to persist seen issue IDs across restarts
_SEEN_FILE = Path(__file__).parent / ".seen_issues.json"

# Linear states that trigger auto-pickup
_PICKUP_STATES = {"backlog", "unstarted"}

# GraphQL query: issues assigned to a user in backlog/unstarted states
_ISSUES_QUERY = """
query($userId: ID!) {
  issues(
    filter: {
      assignee: { id: { eq: $userId } }
      state: { type: { in: ["backlog", "unstarted"] } }
    }
    orderBy: createdAt
    first: 20
  ) {
    nodes {
      id
      identifier
      title
      description
      url
      state { name type }
      team { key name }
      labels { nodes { name } }
      priority
      project { name }
    }
  }
}
"""


@dataclass
class LinearIssue:
    """A Linear issue ready for pickup."""
    id: str
    identifier: str  # e.g. "GYM-123"
    title: str
    description: str | None
    url: str
    team_key: str  # e.g. "GYM"
    labels: list[str]
    priority: int | None
    project_name: str | None


def _load_seen() -> set[str]:
    """Load previously seen issue IDs from disk."""
    if _SEEN_FILE.exists():
        try:
            return set(json.loads(_SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()


def _save_seen(seen: set[str]) -> None:
    """Persist seen issue IDs to disk."""
    try:
        _SEEN_FILE.write_text(json.dumps(sorted(seen)))
    except Exception as e:
        print(f"  [TaskListener] Failed to save seen issues: {e}")


def _load_projects_config() -> dict:
    """Load projects.json."""
    projects_file = Path(__file__).parent / "projects.json"
    if projects_file.exists():
        return json.loads(projects_file.read_text())
    return {}


def _resolve_channel_for_workspace(workspace: str, config: dict) -> str | None:
    """Find the Slack channel for a Linear workspace."""
    for project in config.get("projects", []):
        if project.get("linear_workspace") == workspace:
            channel = project.get("slack_channel")
            if channel:
                return channel
    return None


async def _fetch_assigned_issues(
    api_key: str, user_id: str
) -> list[LinearIssue]:
    """Query Linear GraphQL API for issues assigned to the bot user."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.linear.app/graphql",
            headers={
                "Content-Type": "application/json",
                "Authorization": api_key,
            },
            json={"query": _ISSUES_QUERY, "variables": {"userId": user_id}},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

    issues = []
    for node in data.get("data", {}).get("issues", {}).get("nodes", []):
        issues.append(LinearIssue(
            id=node["id"],
            identifier=node["identifier"],
            title=node["title"],
            description=node.get("description"),
            url=node["url"],
            team_key=node.get("team", {}).get("key", ""),
            labels=[l["name"] for l in node.get("labels", {}).get("nodes", [])],
            priority=node.get("priority"),
            project_name=node.get("project", {}).get("name") if node.get("project") else None,
        ))
    return issues


class TaskListener:
    """Polls Linear for assigned issues and kicks off Slack sessions."""

    def __init__(
        self,
        slack_client: AsyncWebClient,
        session_manager,  # SessionManager — avoid circular import
        workspaces: dict[str, dict],  # {name: {api_key, user_id}}
    ):
        self._slack = slack_client
        self._sessions = session_manager
        self._workspaces = workspaces
        self._seen: set[str] = _load_seen()
        self._config = _load_projects_config()
        self._running = False

    async def start(self) -> None:
        """Start the polling loop."""
        if not self._workspaces:
            print("  [TaskListener] No workspaces configured, skipping")
            return

        self._running = True
        workspace_names = ", ".join(self._workspaces.keys())
        print(f"  [TaskListener] Polling every {POLL_INTERVAL}s for: {workspace_names}")

        while self._running:
            try:
                await self._poll()
            except Exception as e:
                print(f"  [TaskListener] Poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        """Check each workspace for newly assigned issues."""
        for workspace_name, ws_config in self._workspaces.items():
            api_key = ws_config["api_key"]
            user_id = ws_config["user_id"]

            try:
                issues = await _fetch_assigned_issues(api_key, user_id)
            except Exception as e:
                print(f"  [TaskListener] Error polling {workspace_name}: {e}")
                continue

            for issue in issues:
                if issue.id in self._seen:
                    continue

                # Mark as seen immediately to avoid duplicate pickups
                self._seen.add(issue.id)
                _save_seen(self._seen)

                channel = _resolve_channel_for_workspace(
                    workspace_name, self._config
                )
                if not channel:
                    print(
                        f"  [TaskListener] No channel for workspace "
                        f"{workspace_name}, skipping {issue.identifier}"
                    )
                    continue

                print(
                    f"\n  [TaskListener] New issue: {issue.identifier} "
                    f"— {issue.title} (workspace: {workspace_name})"
                )

                # Fire-and-forget: don't block the poll loop waiting for
                # the agent to finish its turn
                asyncio.create_task(
                    self._start_issue(channel, issue, workspace_name)
                )

    async def _start_issue(
        self, channel: str, issue: LinearIssue, workspace: str
    ) -> None:
        """Post a Slack thread and start a session for a Linear issue."""
        # Post the initial message — this creates the thread
        label_tags = f" [{', '.join(issue.labels)}]" if issue.labels else ""
        header = (
            f"Picking up <{issue.url}|{issue.identifier}>: "
            f"{issue.title}{label_tags}"
        )

        try:
            result = await self._slack.chat_postMessage(
                channel=channel,
                text=header,
            )
            thread_ts = result.get("ts")
            if not thread_ts:
                print(f"  [TaskListener] Failed to get thread_ts for {issue.identifier}")
                return
        except Exception as e:
            print(f"  [TaskListener] Failed to post message for {issue.identifier}: {e}")
            return

        # Build the initial prompt with issue context
        desc_block = ""
        if issue.description:
            # Truncate very long descriptions
            desc = issue.description[:2000]
            if len(issue.description) > 2000:
                desc += "\n...(truncated, read the full ticket)"
            desc_block = f"\n\nDescription:\n{desc}"

        prompt = (
            f"A new Linear issue has been assigned to you.\n\n"
            f"Ticket: {issue.identifier} — {issue.title}\n"
            f"URL: {issue.url}\n"
            f"Team: {issue.team_key}\n"
            f"Status: {issue.url}{desc_block}\n\n"
            f"Start working on this issue. Follow the standard workflow: "
            f"move to In Progress, create a worktree, implement the changes, "
            f"create a PR, and move to In Review."
        )

        # Use a wrapper that acts like Slack's say() function
        async def say(text: str = "", thread_ts: str = thread_ts, **kwargs):
            await self._slack.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=text,
            )

        await self._sessions.process_message(
            channel=channel,
            thread_ts=thread_ts,
            user_text=prompt,
            say=say,
            slack_client=self._slack,
        )


def build_task_listener_config() -> dict[str, dict]:
    """Build workspace configs for the task listener from projects.json + env.

    Returns {workspace_name: {api_key: str, user_id: str}} for workspaces
    that have both an API key and a bot user ID configured.
    """
    projects_file = Path(__file__).parent / "projects.json"
    if not projects_file.exists():
        return {}

    data = json.loads(projects_file.read_text())
    workspaces = data.get("linear_workspaces", {})
    result = {}

    for name, config in workspaces.items():
        api_key_env = config.get("api_key_env", "")
        api_key = os.environ.get(api_key_env, "")
        user_id = config.get("bot_user_id", "")

        if api_key and user_id:
            result[name] = {"api_key": api_key, "user_id": user_id}
        elif api_key and not user_id:
            print(
                f"  [TaskListener] Workspace '{name}' has API key but no "
                f"bot_user_id in projects.json — skipping auto-pickup"
            )

    return result
