# Task clarification

Root conversations expose a runtime control tool, `request_clarification(question)`.
The model is instructed to use it when missing user intent materially affects scope,
target, time period, or authorization. Clear requests still use the normal execution loop.

Flow:

1. The model asks a focused question with `request_clarification`.
2. The runner pauses the turn before executing any other tools in that response,
   including delegation. Calls receive paired non-execution results in history.
3. The question is saved as an assistant message with metadata
   `status: awaiting_clarification` and displayed through existing content/done events.
4. The user replies in the same session. The next run reads the original request,
   question, and reply, and the model continues or asks a follow-up.

This is a conversational pause, not a blocked worker or a new database state machine.
No frontend change is required to display the question. `done` closes the current
stream; the message metadata distinguishes a clarification from a completed answer.
The control is root-only and can be disabled through
`AgentRunRequest.clarification_enabled=False` for direct runner callers.

Scope: ambiguity recognition remains model-driven. This mechanism does not guarantee
that every ambiguous request triggers a question, validate the user's answer with a
separate classifier, or roll back actions completed in earlier rounds. It adds no
separate model request and no semantic completeness gate. Existing session history
provides continuity; no new tables or migrations are needed.

Local deterministic tests are in `tests/test_clarification.py`; they cover pausing
before sibling tools, saved-history continuation, malformed-question fallback, and
root-only exposure. These tests establish control flow, not real-model recognition
accuracy. No paid provider calls or live-service restart are needed for them.
