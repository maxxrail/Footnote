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

1. **SDK tool runner** (`client.beta.messages.tool_runner`): least code, but the loop lives
   inside the SDK, so there's no clean place to stop after `submit_answer`, run the post-check,
   and send back exactly one repair turn. Injecting a scripted fake also means faking the
   SDK client.
2. **Managed Agents**: Anthropic hosts the loop and a sandbox. Our tools need the local
   Postgres and the local embedding model, so every tool would round-trip to this machine
   anyway, and the tests couldn't run offline.
3. **Hand-written loop over an `LLMClient` protocol** (~200 lines): full control of the
   end of the run; the fake is a 60-line class returning real `anthropic.types.Message` objects.

For the post-check itself:

a. Trust the model's citations. b. Check numbers against the *cited* ref only. c. Check each
number against *every* number tool outputs returned in the run, within the rounding it was
written with, and require a `[^n]` marker in the same sentence or table row.

## Decision

**Option 3 with post-check (c).** The final answer must be submitted through a
`submit_answer` tool. The post-check extracts numbers (scale words, `%` as a fraction,
`x` multiples; labels like years, FY/Q, form and item names are skipped), matches each within
half a unit of its last written digit, and requires a marker in the same sentence. A failure
gets one repair turn (an error result for `submit_answer` that lists the problems). If it still
fails, the answer is returned with `unverified_numbers` / `uncited_numbers` and low confidence.

What decided it, from the live runs (Sonnet 5.5, effort `medium`, 7 questions):

| | run 1 | run 2 (repair message clarified) |
|---|---|---|
| sample questions passing (cited, verified, filing spans resolve) | 4 / 5 | **5 / 5** |
| refusals correct | 2 / 2 | 2 / 2 |
| answers that used the repair turn | 5 / 7 | 4 / 7 |
| cost, all 7 questions | $0.166 | $0.156 |
| median latency | 6.0 s | 4.5 s |

The repair turn was doing real work. In run 1, it removed two numbers no tool had returned
(an NVIDIA R&D and SG&A split) and added markers to list items whose only citation was in a
lead-in line. The one run 1 failure was a number in a sentence whose marker was at the end of
the *next* sentence, and the model didn't understand the repair message. Run 2 says explicitly,
in both the system prompt and the repair message, that the marker goes in the same sentence;
no threshold or test was changed.

Option (b) would be stronger but needs to know which number each marker supports, which
belongs to Part 5's citation grader.

## Consequences

- The post-check can't be fooled by a plausible-looking number. In tests, 27 mutations of
  the post-check, loop, tools and metrics were each caught.
- It compares **absolute** values, so the sign isn't checked: "fell 12%" passes against a tool's
  +12%. It also accepts a number from any tool output, not necessarily the cited one. Both
  go to Part 5's grader (per-citation value check, sign).
- Bare integers 0-12 without a unit are treated as labels ("3 years"), so a small count stated
  wrongly ("2 of 3 years were losses") isn't checked.
- Most answers need two `submit_answer` calls, about +1 model call. At ~$0.02 per question this
  is cheap; Part 5 can measure whether a stricter first-draft prompt removes the need.
- The first `search_filings` call in a process loads the embedding model (~15-35 s on CPU);
  later calls take well under a second.
