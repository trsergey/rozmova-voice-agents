# ADR 0013: Two-phase confirmation of side-effect tools enforced in engine code

- Status: Accepted
- Date: 2026-10-14

## Context

`activate_service` changes the subscriber's account and costs money. A prompt instruction
"always ask before activating" can be ignored by the model or bypassed by prompt
injection, and a stale "yes" must not activate something later (S3).

## Decision

The engine splits the tool into two phases. The first call in turn N does not execute: it
returns `{"status": "confirmation_required", "service": …, "price": …}` and stores
`pending_action = {name, args, turn: N}`. A call in a later turn with the same arguments
while `pending_action` exists executes and clears it. A turn that ends without executing
clears `pending_action`; different arguments restart the cycle. Whether a reply means
consent is still the LLM's judgement.

## Alternatives considered

- **Trust the prompt.** No guarantee; fails under injection.
- **A separate consent classifier.** Extra model call and latency, and it still needs the
  code-level pending state.

## Consequences

- Guarantees in code: no activation in the turn that names the price, no activation of a
  different service, no reuse of an old consent.
- Eval cases cover "no", "changed my mind", topic change and "activate everything".
- Other side-effect tools must opt into the same mechanism.
