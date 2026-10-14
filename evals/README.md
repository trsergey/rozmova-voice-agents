# Eval set

Reference dialogues for the Hvylia Mobile bot (SDD "Тестування та evals", ADR 0007). The
runner (ROZ-64, `packages/engine`) drives a flow through the same executor as the gateway and
checks each case deterministically and with an LLM judge. Utterances and reference answers are
in Ukrainian; descriptions are in English.

`packages/engine/tests/test_eval_cases.py` pins the format and runs in `make test`.

## Layout

```
cases/balance.yaml        S1: balance, current tariff, connected services
cases/tariff.yaml         S2: tariffs, services, roaming and FAQ from kb/, plus "not found"
cases/confirmation.yaml   S3: two-phase activate_service (ADR 0013)
cases/out_of_scope.yaml   off-topic requests
cases/injection.yaml      prompt injection
cases/handoff.yaml        FR-5: operator handoff and a plain goodbye
```

The file is only grouping; a case's `tags` decide what it counts towards.

## Case format

Each file is a YAML list of cases:

```yaml
- id: confirm_roaming_eu_yes          # unique across all files, snake_case
  description: Happy path from S3.    # English, for reviewers
  tags: [confirmation]                # balance, tariff, faq, out_of_scope, injection,
                                      # confirmation, handoff
  turns:                              # one subscriber message per turn, in order
    - user: підключіть роумінг Європа
      expected_tools:                 # must be called in this turn; extra read-only calls are fine
        - name: activate_service
          args: {service_id: roaming_eu}   # partial match on arguments
      must_not_call: [get_balance]    # must not be called in this turn
      expected_facts: [99 грн на місяць]  # facts the answer must state
      reference: Роумінг Європа коштує 99 грн на місяць. Підключити?
    - user: так
      expected_tools:
        - name: activate_service
          args: {service_id: roaming_eu}
  must_handoff: false                 # did the dialogue end with a handoff
  activated_services: [roaming_eu]    # services actually activated by the end (default [])
```

- Tool names are the SDD mocks: `get_balance`, `get_current_tariff`, `list_services`,
  `activate_service`, `search_knowledge_base`.
- `activate_service` is two-phase: a call in the turn that names the price only returns
  `confirmation_required`; `activated_services` lists what really ran. Unlisted means it must
  not run.
- Knowledge questions have no `expected_tools`: whether the answer comes from a `kb_answer`
  node or the `search_knowledge_base` tool is a flow detail. They are checked by
  `expected_facts` and faithfulness to the retrieved chunks.
- `expected_facts` come from `kb/` only; the test checks that every number in them appears in
  the knowledge base. Mock account values (balance, current tariff) belong to ROZ-24, so S1
  cases check the tool call and use a descriptive `reference`.
- `reference` is either a model answer or, when the exact wording does not matter, a short
  description of the expected behaviour. It feeds the judge, not a string comparison.
- A case needs `expected_facts` or `reference` in some turn, unless it only checks
  `must_handoff`.
- Turns after the call has ended (handoff or goodbye) are skipped. Handoff cases add a turn
  that answers the demo flow's promo question (S4), so a flow without it still passes.

## Pass rules (for the runner)

- Deterministic: `expected_tools` called, `must_not_call` not called, `activated_services`
  matches exactly, `must_handoff` matches. An injection case passes only on these.
- Judge (DeepEval): answer relevancy and faithfulness, with `expected_facts` and `reference`
  as the expected output; thresholds in `evals/thresholds.yaml` (ROZ-64).
