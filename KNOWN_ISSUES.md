# Known Issues

## EISDIR on directory Read
The agent frequently tries `Read` on a directory path instead of using `Glob` or `Bash(ls)`. The SDK returns `EISDIR: illegal operation on a directory`. Non-fatal -- the agent recovers and retries with a different tool, but it wastes a tool call each time.

**Fix**: Intercept EISDIR in `agent.py` and return a helpful message like "This is a directory. Use Glob or Bash(ls) to list contents."

## Bash cwd tracking error
Every Bash command logs `zsh:1: operation not permitted: /tmp/claude-501/cwd-*`. The SDK sandbox tries to write a temp file to track working directory changes but the macOS sandbox blocks it. Commands still execute fine.

**Fix**: Likely needs a fix upstream in the Claude Agent SDK sandbox layer.

## Sibling tool call cascades
When the agent fires parallel tool calls and one fails (e.g., EISDIR), the SDK fails all sibling calls in that batch with `Sibling tool call errored`. This multiplies a single error into 3-4 errors.

**Fix**: SDK-level. Could also be mitigated by reducing parallel tool calls in the system prompt, but that would slow down the agent.

## msg_too_long on Slack responses
Long responses can exceed Slack's message limit (~4000 bytes). The streaming callback truncates to 3800 bytes during streaming, and the final post chunks long messages. Edge cases may still slip through if the byte count is close to the limit.

**Fix**: Currently handled with try/except fallback to chunked posting. Could be more robust with consistent byte-length checking everywhere.

## No dynamic model routing
The plan was to swap models based on task type: Haiku for quick chat, Opus for planning/architecture, Sonnet for coding. Currently all sessions use Sonnet for everything. This means simple questions ("what's the status?") cost more than they should, and complex planning tasks don't get Opus-level reasoning.

The SDK supports `client.set_model()` mid-session, so the infrastructure is there. What's missing is the routing logic -- heuristics or classifier to detect task phase and switch models accordingly.

**Fix**: Build `model_router.py` with keyword/intent-based routing. Call `client.set_model()` before each `client.query()`. Phases: chat -> Haiku, plan/design -> Opus, code/implement -> Sonnet.

## Session loss on restart
PM2 restart kills the Python process, which drops all in-memory sessions. Active Slack threads lose their conversation context. The agent reconnects on the next message via history replay, but the first message after restart may lack context.

**Fix**: Persist session state to disk (e.g., SQLite or JSON) so sessions survive restarts.
