# Hydra Agent

An AI developer teammate that lives in your Slack workspace. Tag it, discuss a task, and it reads your codebase, asks clarifying questions, implements features, and creates pull requests — all through thread-based conversations.

Built on the [Claude Agent SDK](https://github.com/anthropics/claude-code/tree/main/agent-sdk-python) with official MCP integrations for GitHub and Linear.

## How It Works

```
You @mention the bot in Slack
  -> Bot starts a thread conversation
  -> Discusses the task, asks clarifying questions
  -> Reads the codebase, plans the approach
  -> Implements the change
  -> Creates a PR, posts the link in the thread
  -> You review and merge
```

Each Slack thread is a persistent conversation. The bot remembers everything discussed in the thread and builds on it — just like chatting with a teammate.

### Model Routing

| Phase | Model | Why |
|-------|-------|-----|
| Chatting / Clarifying | Haiku | Fast, cheap — good for quick back-and-forth |
| Planning / Architecture | Opus | Best reasoning for complex decisions |
| Writing Code | Sonnet | Strong code generation, good balance |

### Integrations

| Service | Method | What it does |
|---------|--------|-------------|
| **GitHub** | Official Copilot MCP | Read repos, create PRs, manage issues, review code |
| **Linear** | Official Linear MCP | Read/create issues, update status, track work |
| **Playwright** | MCP | Browser testing, screenshots for verification |
| **Slack** | slack-bolt (Socket Mode) | Receive messages, post replies — no public URL needed |

## Getting Started

### Prerequisites

- **Python 3.11+**
- **Node.js 18+** (for Claude Code CLI and Playwright)
- **Claude Code CLI** installed and authenticated
- A **Slack workspace** where you can create an app

### 1. Clone and Set Up Python

```bash
git clone https://github.com/your-username/hydra-agent.git
cd hydra-agent

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Install Playwright Browsers

The agent uses Playwright for browser automation and testing. Install the bundled browsers:

```bash
# Install Playwright's bundled Chromium (recommended)
npx playwright install chromium

# Or install all browsers
npx playwright install
```

**Note**: The agent uses Playwright's bundled Chromium, not system Chrome. If you see errors like `Chromium distribution 'chrome' is not found`, run the install command above.

### 3. Install Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code

# Verify installation
claude --version
```

### 4. Create a Slack App

Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app:

#### a) Enable Socket Mode
- Go to **Socket Mode** in the sidebar
- Toggle **Enable Socket Mode**
- Create an **App-Level Token** with the `connections:write` scope
- Copy the `xapp-...` token — this is your `SLACK_APP_TOKEN`

#### b) Set Up Bot Scopes
- Go to **OAuth & Permissions** -> **Scopes** -> **Bot Token Scopes**
- Add these scopes:
  - `app_mentions:read` — to receive @mentions
  - `channels:history` — to read thread replies
  - `channels:read` — to see channel info
  - `chat:write` — to post messages
  - `groups:history` — to read private channel threads (optional)

#### c) Subscribe to Events
- Go to **Event Subscriptions** -> Toggle **Enable Events**
- Under **Subscribe to bot events**, add:
  - `app_mention`
  - `message.channels`
  - `message.groups` (for private channels, optional)

#### d) Install the App
- Go to **Install App** -> **Install to Workspace**
- Copy the `xoxb-...` **Bot User OAuth Token** — this is your `SLACK_BOT_TOKEN`

#### e) Invite the Bot
- In Slack, go to the channel where you want the bot
- Type `/invite @YourBotName`

### 5. Set Up GitHub MCP (Optional)

Create a **Personal Access Token** at [github.com/settings/tokens](https://github.com/settings/tokens):
- Click **Generate new token (classic)**
- Select scopes: `repo`, `read:org`
- Copy the token

### 6. Set Up Linear MCP (Optional)

Create an **API Key** in your Linear workspace:
- Go to **Settings** -> **API** -> **Personal API keys**
- Create a new key and copy it

### 7. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your tokens:

```bash
# Required — Slack
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token

# Optional — GitHub (enables GitHub MCP tools)
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_your_token

# Optional — Linear (enables Linear MCP tools)
LINEAR_API_KEY=lin_api_your_key

# Working directory — where the agent reads/writes code
DEFAULT_CWD=/path/to/your/project
```

### 8. Register Your Projects

The bot needs to know where your projects live so it doesn't waste time searching for them. Copy the example and edit:

```bash
cp projects.example.json projects.json
```

```json
{
  "projects": [
    {
      "name": "my-app",
      "path": "/absolute/path/to/my-app",
      "aliases": ["myapp", "my app"],
      "description": "Short description of the project"
    }
  ]
}
```

Each entry maps a project name (and common aliases) to its absolute path. When you say "work on my-app" in Slack, the agent switches to that directory immediately instead of searching the filesystem.

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Canonical project name |
| `path` | Yes | Absolute path to the project root |
| `aliases` | No | Alternative names the user might say in Slack |
| `description` | No | Short context about the project (injected into the agent's prompt) |

### 9. Run the Bot

The bot is managed with [PM2](https://pm2.keymetrics.io/) for daemonization, auto-restart, and log management.

```bash
# Install PM2 (if not already installed)
npm install -g pm2

# Start the bot
pm2 start ecosystem.config.cjs

# Check status
pm2 status

# View logs
pm2 logs hydra-agent

# Restart after code changes
pm2 restart hydra-agent

# Stop the bot
pm2 stop hydra-agent
```

Logs are written to `./logs/` with timestamps. PM2 auto-restarts the bot on crash (up to 10 times with 3s backoff).

#### Survive reboots

```bash
# Generate the startup script (requires sudo)
pm2 startup

# Run the command it outputs, then save the process list
pm2 save
```

This installs a launchd service (macOS) or systemd unit (Linux) that resurrects your PM2 processes on reboot. Run `pm2 save` again any time you change your process list.

#### Verify it's running

```bash
pm2 logs hydra-agent --lines 20 --nostream
```

You should see:

```
============================================================
  AI Developer Teammate — Slack Bot
============================================================

Default working dir: /path/to/your/project
GitHub MCP: enabled
Linear MCP: enabled

@mention the bot in any channel to start a conversation.
Reply in the thread to continue the conversation.
```

### 10. Test It

In Slack, @mention the bot:

```
@Hydra hey, can you look at the README and tell me what this project does?
```

The bot will reply in a thread. Reply in that thread to continue the conversation.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_BOT_TOKEN` | Yes | Bot OAuth token (`xoxb-...`) from your Slack app |
| `SLACK_APP_TOKEN` | Yes | App-level token (`xapp-...`) for Socket Mode |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | No | GitHub PAT for repo access, PRs, issues |
| `LINEAR_API_KEY` | No | Linear API key for issue tracking |
| `DEFAULT_CWD` | No | Default working directory (default: current dir) |
| `MEMORY_DIR` | No | Agent memory storage path (default: `~/.hydra-memory`) |

## Project Structure

```
hydra-agent/
├── app.py                  # Main entry point — Slack bot with thread routing
├── session_manager.py      # Per-thread Claude SDK session lifecycle
├── agent.py                # Single-turn runner (send message, collect response)
├── client.py               # Claude SDK client config with MCP servers
├── worktree.py             # Git worktree management for isolated coding
├── security.py             # Bash command allowlist and validation
├── projects.example.json     # Project registry template (copy to projects.json)
├── ecosystem.config.cjs      # PM2 process management config
├── prompts/
│   └── system_prompt.md    # Agent personality and instructions
├── logs/                     # PM2 log output (gitignored)
├── requirements.txt
├── LICENSE
└── LICENSE

~/.hydra-memory/              # Agent memory (outside repo, auto-created)
└── {project-name}.md         # One file per project, built by the agent
```

## Security

The agent runs with defense-in-depth security:

1. **OS Sandbox** — Bash commands run in an isolated environment
2. **Filesystem Restrictions** — File operations restricted to the working directory
3. **Bash Allowlist** — Only specific commands permitted (`git`, `npm`, `node`, `python`, `gh`, etc.)
4. **Dangerous Command Validation** — Commands like `rm` are validated to prevent system directory deletion
5. **MCP Permissions** — Tools explicitly allowed in security settings

6. **Self-protection** — The agent cannot modify its own source code directory

See `security.py` for the full command allowlist.

## Troubleshooting

**Bot doesn't respond to @mentions**
- Check that `message.channels` and `app_mention` events are subscribed
- Make sure the bot is invited to the channel (`/invite @BotName`)
- Check that Socket Mode is enabled

**"SSL certificate verify failed"**
- The bot auto-fixes this via `certifi`. If it persists: `pip install --upgrade certifi`

**"GitHub MCP disabled" / "Linear MCP disabled"**
- Set `GITHUB_PERSONAL_ACCESS_TOKEN` or `LINEAR_API_KEY` in `.env`
- These are optional — the bot works without them

**Bot replies but can't read/write files**
- Check that `DEFAULT_CWD` points to a valid directory

**"Command blocked by security hook"**
- The agent tried to run a disallowed command
- Add it to `ALLOWED_COMMANDS` in `security.py` if appropriate

## License

MIT License — see [LICENSE](LICENSE) for details.

