# Building Hydra Agent: A Dev Log

## The Mac Mini and the Idea

I recently got a Mac Mini. One of those "always on" machines sitting in the corner, humming quietly, waiting to be useful. And I had this idea that had been bouncing around for a while: what if I could set up an autonomous agent that actually matches how we work at the agency?

Because here's the thing. The way most AI coding tools work right now, you're sitting there, typing prompts, waiting, copying code, pasting it, running it, going back. It's still you doing the work, just with a fancy autocomplete. That's not how a team works. When you have a developer on your team, you tag them in Slack, you say "hey can you look at this," you discuss it in the thread, they go off and do the work, they come back with a PR. You review it, give feedback, they iterate. That's the flow.

So I wanted to build that. An AI developer teammate that lives in Slack, works in threads, has access to the same tools we use (GitHub, Linear, browser), and can actually go off and write code in isolation without messing up the main branch.

## What It Actually Is

Hydra Agent is a Slack bot built on Anthropic's Claude Agent SDK. You @mention it in any channel, it starts a thread, and that thread becomes a persistent conversation with a Claude session behind it. Every reply in the thread goes to the same session, context preserved. No copy-pasting, no context windows resetting. Just a running conversation, like talking to a teammate.

It has access to:
- **GitHub** via the official Copilot MCP server. It can read repos, create PRs, check issues.
- **Linear** via Linear's MCP server. It can look up tickets, update status, create issues.
- **Playwright** for browser automation. It can navigate pages, take screenshots, click around.
- **File system tools**. Read, write, edit, search. The basics.
- **Bash**. With a security layer so it doesn't `rm -rf` your life.

When it needs to write code, it creates a git worktree. A separate copy of the repo on its own branch, so whatever it does doesn't touch your working directory. When it's done, it creates a PR. Clean.

## How the Build Actually Went

Honestly? It was a ride.

Started this morning. The initial version was a batch-mode autonomous builder. You'd give it an app spec and it would just go, creating Linear tickets, writing code, looping until done. It worked in a demo sense but it wasn't what I actually wanted. Too autonomous, no conversation, no collaboration. It was a bot that does its own thing, not a teammate.

So I scrapped most of it. Rewrote the core to be conversational. New session manager, new event handlers, new system prompt. The Claude SDK makes this surprisingly clean. You create a client, connect it, call `query()` for each message, iterate `receive_response()` for the reply. The SDK handles tool use internally. The agent can decide to read files, run commands, check GitHub, whatever, and you just get the final text response.

The first real bug was fun. Thread replies weren't being dispatched. I spent a while looking at slack-bolt's event matching, tried three different handler patterns, read the docs, nothing worked. Turns out it wasn't a code problem at all. I had zombie bot processes. Multiple instances of the app running, all connected to Socket Mode, and Slack was randomly distributing events across them. Some events went to dead processes and vanished. Killed everything, ran one clean instance, worked immediately.

That's when I added zombie prevention. PID file on startup, checks for existing instances, kills them before starting. Simple but necessary when you're developing a long-running process and restarting it fifty times.

Then streaming. Without it, you send a message and just wait. Could be 10 seconds, could be 30 if the agent is running tools. Feels broken. So now it posts a "Thinking..." message immediately and live-updates it as the response comes in, using Slack's `chat.update()`. Shows a typing indicator while generating. Small thing but it completely changes how it feels.

Error recovery was the last piece. The SDK client can crash. Network issues, token expiration, whatever. Without recovery, a crashed session means the whole thread is dead. Now it catches failures, disconnects the broken client, creates a fresh one, replays the conversation history as context, and retries. Up to two retries before giving up. The user sees a brief hiccup, not a dead thread.

## Where It Is Right Now

It works. The base conversational loop is solid. You can tag it, have a multi-turn conversation, it remembers context, it can use tools. Streaming makes it feel responsive. Zombie prevention means I can restart it without hunting for orphan processes.

The PR for streaming, worktrees, zombie prevention, and error recovery is open. The main branch has the stable conversational version tagged as `v0.1.0-rc1`.

What's not done:
- **Model routing**. The plan was to dynamically switch models. Haiku for quick chat, Opus for planning, Sonnet for coding. The SDK supports `set_model()` so this is straightforward, just haven't built the heuristics yet.
- **Repo discovery**. Right now you configure a working directory. Eventually it should be able to find repos on the machine when you say "work on project X."
- **Battle testing**. It's been tested in one Slack workspace with one user. That's not real testing. Real testing is when someone asks it something I didn't anticipate and it handles it gracefully (or doesn't, and I learn why).

## The Honest Take

This is early. Very early. The bones are right but there's a lot of rough edges. The system prompt needs iteration based on real usage. The worktree flow needs to be triggered more intelligently (right now it's manual). Error messages could be more helpful. The streaming sometimes flickers if the response is short.

But the core idea works. Having a developer teammate in Slack that you can just talk to, that has access to your tools, that works in isolation and comes back with a PR. That flow is real and it's good. It's how I want to work with AI, not as a fancy autocomplete but as a collaborator that fits into the workflow we already have.

If you want to try it or build on it, it's MIT licensed and on GitHub. Set up the env vars, point it at your Slack workspace, and start a conversation. Just know you're signing up for the early days.

---

*April 11, 2026. Built in a single session on a Mac Mini that finally earned its spot on the desk.*
