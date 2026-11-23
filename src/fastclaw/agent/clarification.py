"""Root-turn clarification control, separate from executable business tools."""

from fastclaw.providers import ToolDefinition, ToolFunction

CLARIFICATION_TOOL = "request_clarification"
CLARIFICATION_PROMPT = """Task clarification:
If missing user intent would materially change the task scope, target, time period,
or authorization, call request_clarification with a concise question before taking
actions that depend on that choice. Do not guess consequential choices. Use existing
conversation context; do not ask again for information already supplied. For minor,
low-risk ambiguities, state your assumption and proceed. After a user replies to a
clarification, assess the reply in context: continue if sufficient, otherwise ask a
focused follow-up. A request_clarification call pauses the turn and cancels all other
tool calls in the same response; it does not undo actions from earlier rounds.
"""

CLARIFICATION_DEFINITION = ToolDefinition(
    function=ToolFunction(
        name=CLARIFICATION_TOOL,
        description="Ask the user to resolve a consequential ambiguity and pause this turn.",
        parameters={
            "type": "object",
            "properties": {"question": {"type": "string", "minLength": 1, "maxLength": 2000}},
            "required": ["question"],
            "additionalProperties": False,
        },
    )
)
