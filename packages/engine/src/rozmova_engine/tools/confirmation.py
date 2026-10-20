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

   One exception, once per quote: the subscriber's answer was unclear but short (not a
   no, nothing imitating a system message, e.g. a typo), no tool ran in that turn, and the
   bot's reply named the quoted subject again: the bot re-asked the same question, so the
   answer to it in the next turn still counts. Without it a plain «так» after the re-ask
   was refused (ROZ-88).

So a service cannot be activated in the turn that names its price, a different service
than the one announced cannot be activated, and an old "yes" cannot be reused later.

4. The repeated call also needs the subscriber's message of that turn to be consent by
   ``consent.consent`` (ADR 0015): short, a yes word, no negation, nothing imitating a
   system message. Otherwise the tool quotes again with ``consent_missing`` and the
   pending action moves to this turn, so a clear "так" next turn still works.
"""

import re

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
    subject = quote.details.get("service")
    state.pending_action = PendingAction(
        name=bound.tool.name,
        args=args,
        turn=state.turn,
        subject=subject if isinstance(subject, str) else None,
    )
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
    """Drop a pending action that was not confirmed in the turn that just ended, unless the
    bot only asked about it again (rule 3)."""
    pending = state.pending_action
    if pending is None or pending.turn >= state.turn:
        return
    if not pending.reasked and _reasked(state, pending):
        state.pending_action = pending.model_copy(update={"reasked": True})
    else:
        state.pending_action = None


def _reasked(state: SessionState, pending: PendingAction) -> bool:
    if pending.subject is None:
        return False
    answer = consent(_utterance(state))
    if answer.verdict != "unclear" or not answer.answer_like:
        return False
    turn = [entry.message for entry in state.history if entry.turn == state.turn]
    if any(message.role == "tool" or message.tool_calls for message in turn):
        return False
    reply = " ".join(m.content for m in turn if m.role == "assistant")
    return _mentions(reply, pending.subject)


def _mentions(text: str, subject: str) -> bool:
    """Whether ``text`` names ``subject`` in any case form: every word of the subject, cut
    to its stem, starts a word of the text («Голосова пошта» in «голосову пошту»)."""
    words = re.findall(r"\w+", text.casefold())
    stems = [w[: max(4, len(w) - 2)] for w in re.findall(r"\w+", subject.casefold())]
    return all(any(word.startswith(stem) for word in words) for stem in stems)
