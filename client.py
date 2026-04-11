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


def load_system_prompt() -> str:
    """Load the conversational agent system prompt."""
    return (PROMPTS_DIR / "system_prompt.md").read_text()


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
            {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
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
            max_turns=1000,
            cwd=str(cwd.resolve()),
            settings=str(settings_file.resolve()),
        )
    )
