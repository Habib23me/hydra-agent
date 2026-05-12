"""
Task Listener — Linear Issue Auto-Pickup
=========================================

Polls Linear workspaces on a long interval and picks up assigned issues in
batches. The poll is a pure rate limit: every POLL_INTERVAL seconds, each
workspace fires up to BATCH_SIZE tickets concurrently and immediately sleeps
again — it does NOT wait for those sessions to finish before the next tick.

Eligibility (must all be true):
- Assignee is the bot's Linear user
- state.type ∈ {backlog, unstarted, started}  (excludes triage, completed,
  canceled, and any "Review" state which would have a PR already)
- No GitHub PR linked via attachments (URL contains "/pull/")
- No label named BLOCKED_LABEL (agent self-marks tickets it can't progress on)
"""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from slack_sdk.web.async_client import AsyncWebClient


# How often to poll Linear for new assignments (seconds). Default: 30 min.
POLL_INTERVAL = int(os.environ.get("TASK_POLL_INTERVAL", "1800"))

# Max tickets to pick up per project per poll tick.
BATCH_SIZE = int(os.environ.get("TASK_BATCH_SIZE", "5"))

# Linear states whose `type` makes a ticket eligible for pickup.
_PICKUP_STATE_TYPES = ["backlog", "unstarted", "started"]

# Label that tells the listener to skip a ticket on subsequent polls. The
# agent adds this label itself when it gets stuck on unclear requirements.
BLOCKED_LABEL = "blocked"

# GraphQL query: assigned issues in pickup states, with attachments + labels
# so we can filter out PR-linked and blocked tickets client-side.
_ISSUES_QUERY = """
query($userId: ID!, $stateTypes: [String!]!) {
  issues(
    filter: {
      assignee: { id: { eq: $userId } }
      state: { type: { in: $stateTypes } }
    }
    orderBy: createdAt
    first: 50
  ) {
    nodes {
      id
      identifier
      title
      description
      url
      createdAt
      state { name type }
      team { key name }
      labels { nodes { name } }
      attachments { nodes { url sourceType } }
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
    created_at: str
    team_key: str  # e.g. "GYM"
    labels: list[str]
    attachment_urls: list[str]
    priority: int | None
    project_name: str | None

    def has_linked_pr(self) -> bool:
        """True if any attachment looks like a GitHub PR."""
        return any("/pull/" in (u or "") for u in self.attachment_urls)

    def is_blocked(self) -> bool:
        """True if the agent previously marked this ticket as blocked."""
        return any(lbl.lower() == BLOCKED_LABEL for lbl in self.labels)


def _priority_sort_key(issue: LinearIssue) -> tuple[int, str]:
    """Sort ascending: urgent first, then high → low, then no-priority last.

    Linear priority: 0 = no priority, 1 = urgent, 2 = high, 3 = medium, 4 = low.
    Treat 0 as worst (99) so genuinely prioritized work goes first.
    """
    p = issue.priority if issue.priority else 99
    return (p, issue.created_at)


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
            json={
                "query": _ISSUES_QUERY,
                "variables": {
                    "userId": user_id,
                    "stateTypes": _PICKUP_STATE_TYPES,
                },
            },
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
            created_at=node.get("createdAt", ""),
            team_key=node.get("team", {}).get("key", ""),
            labels=[l["name"] for l in node.get("labels", {}).get("nodes", [])],
            attachment_urls=[
                a.get("url", "") for a in node.get("attachments", {}).get("nodes", [])
            ],
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
        self._config = _load_projects_config()
        self._running = False
        # Tickets we already kicked off in the current process. Linear state
        # (PR-linked / blocked label) is the durable source of truth across
        # restarts; this is just a within-process safety net so a slow agent
        # turn doesn't get re-picked on the next tick before it has had a
        # chance to mutate the ticket.
        self._in_flight: set[str] = set()

    async def start(self) -> None:
        """Start the polling loop."""
        if not self._workspaces:
            print("  [TaskListener] No workspaces configured, skipping")
            return

        self._running = True
        workspace_names = ", ".join(self._workspaces.keys())
        print(
            f"  [TaskListener] Polling every {POLL_INTERVAL}s "
            f"(batch={BATCH_SIZE} per project) for: {workspace_names}"
        )

        while self._running:
            try:
                await self._poll()
            except Exception as e:
                print(f"  [TaskListener] Poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        """Pick up to BATCH_SIZE eligible tickets per workspace, then return.

        Sessions started here run fire-and-forget. The loop sleeps for
        POLL_INTERVAL regardless of whether they finish — the batch cap is
        the only rate limit.
        """
        for workspace_name, ws_config in self._workspaces.items():
            api_key = ws_config["api_key"]
            user_id = ws_config["user_id"]

            try:
                issues = await _fetch_assigned_issues(api_key, user_id)
            except Exception as e:
                print(f"  [TaskListener] Error polling {workspace_name}: {e}")
                continue

            eligible = [i for i in issues if self._is_eligible(i)]
            eligible.sort(key=_priority_sort_key)
            batch = eligible[:BATCH_SIZE]

            skipped_pr = sum(1 for i in issues if i.has_linked_pr())
            skipped_blocked = sum(1 for i in issues if i.is_blocked())
            print(
                f"  [TaskListener] {workspace_name}: {len(issues)} assigned, "
                f"{len(eligible)} eligible, {skipped_pr} have PR, "
                f"{skipped_blocked} blocked → picking {len(batch)}"
            )

            channel = _resolve_channel_for_workspace(
                workspace_name, self._config
            )
            if not channel:
                if batch:
                    print(
                        f"  [TaskListener] No channel for workspace "
                        f"{workspace_name}, skipping {len(batch)} tickets"
                    )
                continue

            for issue in batch:
                self._in_flight.add(issue.id)
                print(
                    f"  [TaskListener] Picking up {issue.identifier} "
                    f"— {issue.title} (priority={issue.priority})"
                )
                asyncio.create_task(
                    self._start_issue(channel, issue, workspace_name)
                )

    def _is_eligible(self, issue: LinearIssue) -> bool:
        """Apply client-side filters that GraphQL can't express cleanly."""
        if issue.id in self._in_flight:
            return False
        if issue.is_blocked():
            return False
        if issue.has_linked_pr():
            return False
        return True

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
            f"You have been auto-assigned a Linear issue via the periodic "
            f"pickup loop. There is no human waiting on this thread — work "
            f"autonomously until you finish or get stuck.\n\n"
            f"Ticket: {issue.identifier} — {issue.title}\n"
            f"URL: {issue.url}\n"
            f"Team: {issue.team_key}{desc_block}\n\n"
            f"Standard workflow: move to In Progress, implement in your "
            f"worktree, open a PR, move to In Review.\n\n"
            f"If you get stuck on unclear requirements or need a human "
            f"answer to proceed, follow the 'When blocked' workflow: post "
            f"your specific questions as a Linear comment on this ticket, "
            f"add the '{BLOCKED_LABEL}' label, leave the ticket in In "
            f"Progress, and stop. Do NOT keep retrying — the pickup loop "
            f"will skip blocked tickets so it won't burn tokens re-trying "
            f"the same blocker."
        )

        # Use a wrapper that acts like Slack's say() function
        async def say(text: str = "", thread_ts: str = thread_ts, **kwargs):
            await self._slack.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=text,
            )

        try:
            await self._sessions.process_message(
                channel=channel,
                thread_ts=thread_ts,
                user_text=prompt,
                say=say,
                slack_client=self._slack,
            )
        finally:
            self._in_flight.discard(issue.id)


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
