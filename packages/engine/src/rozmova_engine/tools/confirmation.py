"""Two-phase, code-enforced confirmation of side-effect tools (ADR 0013, SDD scenario S3).

The LLM decides whether the subscriber agreed; the code decides whether that agreement can
count. A two-phase tool (one with a ``quote``) executes only when it is called again, with
the same arguments, in a later turn than the one in which its price was announced:

1. First call in turn N: nothing is executed. The LLM gets
   ``{"status": "confirmation_required", ...quote}`` and the state records
   ``pending_action = {name, args, turn: N}``.
2. Same call in turn M > N while that ``pending_action`` exists: the tool executes and
   ``pending_action`` is cleared.
3. ``finish_turn`` at the end of every turn drops a ``pending_action`` from an earlier
   turn: a confirmation must come in the very next turn. A call with other arguments
   quotes again and replaces the pending action.

So a service cannot be activated in the turn that names its price, a different service
than the one announced cannot be activated, and an old "yes" cannot be reused later.

4. The repeated call also needs the subscriber's message of that turn to be consent by
   ``consent.consent`` (ADR 0015): short, a yes word, no negation, nothing imitating a
   system message. Otherwise the tool quotes again with ``consent_missing`` and the
   pending action moves to this turn, so a clear "так" next turn still works.
"""

from rozmova_engine.providers import ToolCall
from rozmova_engine.session import PendingAction, SessionState
from rozmova_engine.tools.consent import consent
from rozmova_engine.tools.registry import BoundCall, Quote, ToolContext, ToolRegistry, ToolResult

CONFIRMATION_REQUIRED = "confirmation_required"


async def call_tool(
    registry: ToolRegistry, call: ToolCall, ctx: ToolContext, state: SessionState
) -> ToolResult:
    """Run one LLM tool call, applying the confirmation rule to two-phase tools.

    The executor calls every tool through here, in the turn ``state.turn``.
    """
    bound = registry.bind(call)
    if not isinstance(bound, BoundCall):
        return bound
    if bound.tool.quote is None:
        return await registry.execute(bound, ctx)

    args = bound.normalized_args()
    pending = state.pending_action
    missing: str | None = None
    if (
        pending is not None
        and pending.name == bound.tool.name
        and pending.args == args
        and pending.turn < state.turn
    ):
        verdict = consent(_utterance(state))
        if verdict.given:
            state.pending_action = None
            return await registry.execute(bound, ctx)
        missing = verdict.reason

    quote = await registry.quote(bound, ctx)
    if not isinstance(quote, Quote):
        # Nothing to confirm (unknown service, already active, error): answer as is.
        return quote
    state.pending_action = PendingAction(name=bound.tool.name, args=args, turn=state.turn)
    result: ToolResult = {"status": CONFIRMATION_REQUIRED, **quote.details}
    if missing is not None:
        result["consent_missing"] = (
            f"Not activated: {missing}. Repeat the price and ask the subscriber to answer "
            "yes or no; do not say that an error happened."
        )
    return result


def _utterance(state: SessionState) -> str:
    """The subscriber's message of the current turn; empty if there is none."""
    for entry in reversed(state.history):
        if entry.turn != state.turn:
            break
        if entry.message.role == "user":
            return entry.message.content
    return ""


def finish_turn(state: SessionState) -> None:
    """Drop a pending action that was not confirmed in the turn that just ended."""
    if state.pending_action is not None and state.pending_action.turn < state.turn:
        state.pending_action = None
