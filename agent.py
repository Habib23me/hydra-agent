"""
Agent Turn Runner
=================

Runs a single conversational turn: sends a message to the Claude SDK client
and collects the response.
"""

import traceback
from typing import NamedTuple

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


class TurnResult(NamedTuple):
    """Result of a single conversational turn.

    Attributes:
        response_text: Collected text from the assistant's response
        cost_usd: Total cost of this turn (if reported)
        error: Error message if the turn failed, None otherwise
    """

    response_text: str
    cost_usd: float | None
    error: str | None


async def run_turn(client: ClaudeSDKClient, message: str) -> TurnResult:
    """
    Send a message and collect the full response.

    Args:
        client: Connected ClaudeSDKClient (must already be in async context)
        message: User message to send

    Returns:
        TurnResult with response text and cost
    """
    try:
        await client.query(message)

        response_text = ""
        cost_usd: float | None = None

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
                    elif isinstance(block, ToolUseBlock):
                        print(f"  [Tool: {block.name}]", flush=True)

            elif isinstance(msg, UserMessage):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        is_error = bool(block.is_error) if block.is_error else False
                        if is_error:
                            error_str = str(block.content)[:200]
                            print(f"  [Error] {error_str}", flush=True)

            elif isinstance(msg, ResultMessage):
                cost_usd = msg.total_cost_usd

        return TurnResult(response_text=response_text, cost_usd=cost_usd, error=None)

    except Exception as e:
        error_type = type(e).__name__
        error_msg = f"{error_type}: {e}"
        print(f"\nError during turn: {error_msg}")
        traceback.print_exc()
        return TurnResult(response_text="", cost_usd=None, error=error_msg)
