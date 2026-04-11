"""
Claude SDK Client Configuration
================================

Creates Claude Agent SDK clients with official GitHub and Linear MCP servers.
"""

import json
import os
from pathlib import Path
from typing import Literal, TypedDict, cast

from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, McpServerConfig
from claude_agent_sdk.types import HookCallback, HookMatcher

from security import bash_security_hook


# Valid permission modes for the Claude SDK
PermissionMode = Literal["acceptEdits", "acceptAll", "reject", "ask"]


class SandboxConfig(TypedDict):
    enabled: bool
    autoAllowBashIfSandboxed: bool


class PermissionsConfig(TypedDict):
    defaultMode: PermissionMode
    allow: list[str]


class SecuritySettings(TypedDict):
    sandbox: SandboxConfig
    permissions: PermissionsConfig


# Playwright MCP tools for browser automation
PLAYWRIGHT_TOOLS: list[str] = [
    "mcp__playwright__browser_navigate",
    "mcp__playwright__browser_take_screenshot",
    "mcp__playwright__browser_click",
    "mcp__playwright__browser_type",
    "mcp__playwright__browser_select_option",
    "mcp__playwright__browser_hover",
    "mcp__playwright__browser_snapshot",
    "mcp__playwright__browser_wait_for",
]

# Built-in tools
BUILTIN_TOOLS: list[str] = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
]

# Prompts directory
PROMPTS_DIR = Path(__file__).parent / "prompts"

# Environment
GITHUB_TOKEN: str = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
LINEAR_API_KEY: str = os.environ.get("LINEAR_API_KEY", "")


def load_projects_registry() -> str:
    """Load the project registry and format it for the system prompt."""
    projects_file = Path(__file__).parent / "projects.json"
    if not projects_file.exists():
        return ""

    data = json.loads(projects_file.read_text())
    projects = data.get("projects", [])
    if not projects:
        return ""

    lines = ["\n## Known Projects\n"]
    lines.append("When the user references a project, use these paths directly. Do not search for them.\n")
    for p in projects:
        aliases = ", ".join(p.get("aliases", []))
        alias_str = f" (also: {aliases})" if aliases else ""
        lines.append(f"- **{p['name']}**{alias_str}: `{p['path']}`")
        if p.get("description"):
            lines.append(f"  {p['description']}")
    return "\n".join(lines)


AGENT_DIR = Path(__file__).parent.resolve()
MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".hydra-memory"))
MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def load_project_memory(project_name: str) -> str:
    """Load memory file for a specific project."""
    memory_file = MEMORY_DIR / f"{project_name}.md"
    if memory_file.exists():
        return memory_file.read_text()
    return ""


def load_all_project_memories() -> str:
    """Load all project memory files and format for the system prompt."""
    if not MEMORY_DIR.exists():
        return ""

    memories = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        content = f.read_text().strip()
        if content:
            memories.append(content)

    if not memories:
        return ""

    return "\n\n## Project Memory\n\nYou have accumulated knowledge about projects you've worked on. Use this to avoid re-exploring things you already know.\n\n" + "\n\n---\n\n".join(memories)


def load_system_prompt() -> str:
    """Load the conversational agent system prompt with project registry and memory."""
    base = (PROMPTS_DIR / "system_prompt.md").read_text()

    # Inject runtime paths into placeholders
    base = base.replace("{{MEMORY_DIR}}", str(MEMORY_DIR.resolve()))
    base = base.replace("{{AGENT_DIR}}", str(AGENT_DIR))

    projects = load_projects_registry()
    memory = load_all_project_memories()
    return base + projects + memory


def create_security_settings() -> SecuritySettings:
    """Create security settings with sandbox and permissions."""
    return SecuritySettings(
        sandbox=SandboxConfig(enabled=True, autoAllowBashIfSandboxed=True),
        permissions=PermissionsConfig(
            defaultMode="acceptEdits",
            allow=[
                "Read(./**)",
                "Write(./**)",
                "Edit(./**)",
                "Glob(./**)",
                "Grep(./**)",
                f"Read({MEMORY_DIR.resolve()}/**)",
                f"Write({MEMORY_DIR.resolve()}/**)",
                f"Edit({MEMORY_DIR.resolve()}/**)",
                "Bash(*)",
                *PLAYWRIGHT_TOOLS,
                "mcp__github__*",
                "mcp__linear__*",
            ],
        ),
    )


def write_security_settings(work_dir: Path, settings: SecuritySettings) -> Path:
    """Write security settings to working directory."""
    work_dir.mkdir(parents=True, exist_ok=True)
    settings_file = work_dir / ".claude_settings.json"
    with open(settings_file, "w") as f:
        json.dump(settings, f, indent=2)
    return settings_file


def get_mcp_servers() -> dict[str, McpServerConfig]:
    """Build MCP server configuration for GitHub, Linear, and Playwright."""
    servers: dict[str, McpServerConfig] = {
        "playwright": cast(
            McpServerConfig,
            {"command": "npx", "args": ["-y", "@playwright/mcp@latest", "--browser", "chromium"]},
        ),
    }

    if GITHUB_TOKEN:
        servers["github"] = cast(
            McpServerConfig,
            {
                "type": "http",
                "url": "https://api.githubcopilot.com/mcp/",
                "headers": {"Authorization": f"Bearer {GITHUB_TOKEN}"},
            },
        )

    if LINEAR_API_KEY:
        servers["linear"] = cast(
            McpServerConfig,
            {
                "type": "http",
                "url": "https://mcp.linear.app/mcp",
                "headers": {"Authorization": f"Bearer {LINEAR_API_KEY}"},
            },
        )

    return servers


def create_session_client(cwd: Path, model: str) -> ClaudeSDKClient:
    """
    Create a Claude Agent SDK client for a conversational session.

    Args:
        cwd: Working directory for the session
        model: Claude model ID to use

    Returns:
        Configured ClaudeSDKClient ready to connect
    """
    security_settings = create_security_settings()
    settings_file = write_security_settings(cwd, security_settings)
    system_prompt = load_system_prompt()
    mcp_servers = get_mcp_servers()

    # Build allowed tools list
    allowed_tools = [*BUILTIN_TOOLS, *PLAYWRIGHT_TOOLS]
    if GITHUB_TOKEN:
        allowed_tools.append("mcp__github__*")
    if LINEAR_API_KEY:
        allowed_tools.append("mcp__linear__*")

    return ClaudeSDKClient(
        options=ClaudeAgentOptions(
            model=model,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher="Bash",
                        hooks=[cast(HookCallback, bash_security_hook)],
                    ),
                ],
            },
            max_turns=200,
            cwd=str(cwd.resolve()),
            settings=str(settings_file.resolve()),
        )
    )
