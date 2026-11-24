# 0005. Hand-written agent loop with a number post-check and one repair turn

- Status: accepted
- Date: 2026-11-24
- Part: 3

## Context

The agent has to guarantee two things the model can't promise on its own: every number in
an answer came from a tool (the model never does arithmetic), and every number is cited.
The loop also needs a hard step cap, a per-step trace with tokens and cost, and a fake LLM
in tests (no network).

## Options

Loop:
1. **SDK tool runner** (`client.beta.messages.tool_runner`): least code, but the loop lives
   inside the SDK, so there's no clean place to stop after `submit_answer`, run the post-check,
   and send back exactly one repair turn. Injecting a scripted fake also means faking the
   SDK client.
2. **Managed Agents**: Anthropic hosts the loop and a sandbox. Our tools need the local
   Postgres and the local embedding model, so every tool would round-trip to this machine
   anyway, and the tests couldn't run offline.
3. **Hand-written loop over an `LLMClient` protocol** (~250 lines): full control of the end
   of the run; the fake is a 60-line class returning real `anthropic.types.Message` objects.

Post-check:
- a. Trust the model's citations.
- b. Match each number against *every* number in the run's tool output text.
- c. Match each number against the *typed values of the ref cited in its sentence*.

## Decision

**Loop option 3 with post-check (c).** The final answer goes through a `submit_answer` tool.
For each number in the answer the post-check requires:
- a value of the same kind (%, x, amount; a bare number may match a multiple),
- within half a unit of its last written digit after scale words,
- with an agreeing sign when the text states one (minus, parentheses, "grew"/"fell" right before it),
- belonging to a ref cited in the same sentence, table row or list item.

Only typed values count: `value`/`display` of items with a `ref` (with their `unit`), their
`inputs`, and numbers written in filing excerpts. Ref ids, dates, accession numbers, CIKs,
notes and error text never count. A failure gets one repair turn (an error result for
`submit_answer` listing the problems), inside the 10-step limit. If it still fails, the answer
is returned with `unverified_numbers` / `miscited_numbers` / `uncited_numbers` and low confidence.

**Why (c), not (b): the first version used (b), and the part-reviewer showed it was hollow.**
It took every digit run in the tool JSON as a verified value, so a `period_end` of
`2025-12-31` or an accession number verified "grew 31%", "grew 12%", and "trades at 31x". The
offline tests missed it because their tool output had no dates. The tests now use realistic
tool output, and a mutation that brings raw digits back is caught.

Live runs (Sonnet 5.5, effort `medium`, 5 sample questions + 2 refusals):

| | run 1 (b) | run 2 (b), marker wording | run 3 (c) |
|---|---|---|---|
| samples passing (cited, verified, spans resolve) | 4 / 5 | 5 / 5 | **5 / 5** |
| refusals correct | 2 / 2 | 2 / 2 | 2 / 2 |
| answers that used the repair turn | 5 / 7 | 4 / 7 | 4 / 7 |
| cost, all 7 questions | $0.166 | $0.156 | $0.150 |
| median latency | 6.0 s | 4.5 s | 4.3 s |

The repair turn does real work. In run 1 it removed an NVIDIA R&D/SG&A split that no tool had
returned, and it added markers to list items whose only citation was in a lead-in line.
Run 1's one failure was a number whose marker was at the end of the *next* sentence. Run 2
says, in the system prompt and the repair message, that the marker goes in the same sentence;
no test or threshold changed.

## Consequences

- A number can only pass if a ref cited next to it really returned it, with the same unit
  and an agreeing sign. 40 mutations of the post-check, loop, tools and metrics are each caught.
- **Not checked:**
  - claims without numbers ("revenue grew faster than costs") are not checked for a citation, beyond requiring the answer to cite something;
  - numbers from filing excerpts have no sign;
  - a direction word only counts right before the number;
  - integers 0-12 followed by a count word ("3 years", "2 segments") are labels;
  - a value within half a unit passes even if a different rounding was meant ("$2 billion" for $1.5B at unit precision).

  Part 5's grader should cover claim-level citation and per-claim value checks.
- `refused` is the model's call. The tools make it easy (resolve_company says "refuse", empty
  series say "not in the filings"), and a non-refused answer that cites nothing is flagged.
- Most answers need two `submit_answer` calls, about +1 model call, at ~$0.02 per question.
  Part 5 can measure whether a stricter first-draft prompt removes the need.
- The first `search_filings` in a process loads the embedding model (~15-35 s on CPU).
