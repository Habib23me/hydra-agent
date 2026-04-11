"""
Agent Turn Runner
=================

Runs a single conversational turn: sends a message to the Claude SDK client
and collects the response. Supports streaming callbacks for live updates.
"""

import traceback
from typing import Callable, Awaitable, NamedTuple

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

# Callback type: called with (accumulated_text, is_final)
StreamCallback = Callable[[str, bool], Awaitable[None]]


class TurnResult(NamedTuple):
    """Result of a single conversational turn."""

    response_text: str
    cost_usd: float | None
    error: str | None


async def run_turn(
    client: ClaudeSDKClient,
    message: str,
    on_stream: StreamCallback | None = None,
    stream_interval: float = 2.0,
) -> TurnResult:
    """
    Send a message and collect the full response.

    Args:
        client: Connected ClaudeSDKClient
        message: User message to send
        on_stream: Optional callback called periodically with accumulated text.
                   Signature: async (text, is_final) -> None
        stream_interval: Minimum seconds between stream callbacks

    Returns:
        TurnResult with response text and cost
    """
    try:
        await client.query(message)

        response_text = ""
        cost_usd: float | None = None
        last_stream_len = 0

        import time
        last_stream_time = time.monotonic()

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
                    elif isinstance(block, ToolUseBlock):
                        print(f"  [Tool: {block.name}]", flush=True)

                # Stream update if enough new text and enough time elapsed
                if on_stream and response_text:
                    now = time.monotonic()
                    new_text = len(response_text) - last_stream_len
                    if new_text > 50 and (now - last_stream_time) >= stream_interval:
                        await on_stream(response_text, False)
                        last_stream_len = len(response_text)
                        last_stream_time = now

            elif isinstance(msg, UserMessage):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        is_error = bool(block.is_error) if block.is_error else False
                        if is_error:
                            error_str = str(block.content)[:200]
                            print(f"  [Error] {error_str}", flush=True)

            elif isinstance(msg, ResultMessage):
                cost_usd = msg.total_cost_usd

        # Final stream callback
        if on_stream and response_text:
            await on_stream(response_text, True)

        return TurnResult(response_text=response_text, cost_usd=cost_usd, error=None)

    except Exception as e:
        error_type = type(e).__name__
        error_msg = f"{error_type}: {e}"
        print(f"\nError during turn: {error_msg}")
        traceback.print_exc()
        return TurnResult(response_text="", cost_usd=None, error=error_msg)
