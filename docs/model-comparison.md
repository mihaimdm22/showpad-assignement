# Which model for extraction, at what cost

Question: the LLM is the cost driver, so which model and effort level should production use, and what does a call
cost? Everything runs on prompt `extract_v3` with the same validation, resolution and queries. Only the model or the
effort setting changes.

Two test sets:
- **Mock calls:** the 2 mock calls with the answer key (`data/gold/`). They are hard by design and scored
  automatically.
- **Third-party calls:** 13 calls (11 plus 2 noisy copies) from the stress-test sample (`docs/stress-test.md`). These
  are typical calls without an answer key, so they are compared on proxies and a spot check.

## Mock calls with the answer key
| Setting | People linked or correctly unresolved | Wrong links | Required facts | Precision (strict key) | Traps | Query checks | Cost (2 calls) |
|---|---|---|---|---|---|---|---|
| Opus 5.5, effort high | 11/11 | 0 | 18/18 | 83% | 0 | 21/21 | $0.28 |
| Opus 5.5, high, second run | 11/11 | 0 | 18/18 | 83% | 0 | 21/21 | $0.29 |
| Opus 5.5, medium | 11/11 | 0 | 18/18 | 85% | 0 | 21/21 | $0.25 |
| Opus 5.5, low | 10/11 | **1** | 17/18 | 82% | 0 | 21/21 | $0.17 |
| Sonnet 5.5, high | 8/11 | **1** | 18/18 | 84% | 0 | 21/21 | $0.12 |
| Haiku 5.5, high | 8/11 | 0 | 18/18 | 77% | 0 | 21/21 | $0.01 |
| **Cascade: Haiku, Opus for hard calls** | 11/11 | 0 | 18/18 | 85% | 0 | 21/21 | $0.18 |

**Every setting kept the planted traps out and passed all 21 query checks.** All six runs labelled the negation, the
hypothetical and the seller's opinion correctly, with no trap even in the raw output. The queries then rely on those
labels.

**The difference is the hardest entity case.** "Pieter" is said once for the customer's procurement manager and once
for the seller's engineer.
- Opus at low effort and Sonnet both linked the procurement Pieter to the engineer: the mistake prompt v1 made. The
  model tells the resolver "seller side, solutions engineer". The resolver's same-name rule asks for a second clue, but
  side and role hint come from the same misreading, so one error supplies both.
- Haiku did not guess. It left out the ambiguous mentions, which is safe but loses them.

The two Opus high runs scored identically, so run-to-run noise on this set is small. It is one repeat, not a measured
spread.

## Third-party calls (13 calls, about 500 words each)
| Setting | Claims | Objections | Seller and contact linked | Competitors found | Cost per call | Time per call |
|---|---|---|---|---|---|---|
| Opus 5.5, high | 55 | 14 | 11/11 | 2 of 2 | $0.058 | 13 s |
| Opus 5.5, medium | 55 | 15 | 11/11 | 2 of 2 | $0.049 | 8 s |
| Sonnet 5.5, high | 42 | 6 | 11/11 | 2 of 2 | $0.022 | 4 s |
| Haiku 5.5, high | 49 | 8 | 11/11 | 2 of 2 | $0.0025 | 14 s |

Spot check of three calls, Claude reading each claim against its turn:
- **Haiku** found the same facts as Opus with different wording. On one call it labelled an agreed demo as a real
  commitment, where Opus called it hypothetical.
- **Sonnet** missed real objections: price and data security on one call, data security on another.

## Decision: a cascade, with a trigger written in code
Haiku extracts every call. A call goes to Opus when the CRM already shows the hard case: two people the call could be
about share a first name. The pool is the account's contacts, the people on the call, and the deal owner and team.
- **Known before extraction.** The trigger is checked before the first model call (`shared_first_names()` in
  `src/kg/resolve.py`), so no call is extracted twice.
- **Logged.** The reason is saved with the run (`meta.escalated_for`).
- **Usage:** `kg extract --model claude-haiku-5-5 --escalate-to claude-opus-5-5`.

| | |
|---|---|
| Calls in the third-party corpus that would escalate | 317 of 3,454 with CRM IDs (9.2%) |
| Blended cost per typical call | about **$0.008** (0.908 × $0.0025 + 0.092 × $0.058), against $0.058 for Opus on every call |
| Per 100,000 calls | about $800, against $5,800 |
| Mock calls through the cascade | Aurelia (two Pieters) went to Opus, Brennan to Haiku. Quality is the same as Opus-only (table above) at $0.18 instead of $0.28 |

**Prompt caching, applied afterwards.** The instructions and the output schema form a 5,351-token prefix that is the
same on every call. It is now cached: written once, then read at 5% of the input price on Opus (10% on Haiku).
Measured on the two mock calls with Opus:
- the second call read all 5,351 tokens from the cache;
- quality was unchanged (0 wrong links, 18/18 required facts, 83% strict precision).

Applied to the figures above, from those measured token counts rather than a new run:
- an Opus call on a 500-word transcript falls from $0.058 to about $0.038;
- the cascade falls from about $0.008 to about $0.005 per call, or about $0.18 per seller a month at 8 calls a week.

The full-corpus run then measured it: $0.0055 per call over 3,454 calls, about $0.19 per seller a month
(`docs/stress-test-full.md`).

Still unused: the Batch API at half price, if a brief may be updated hours after the call rather than minutes.

## Limits
- One run per setting; two mock calls; 13 third-party calls; one reviewer.
- The trigger covers one known failure. Others found later need their own rules. Candidates:
  - a mention left unresolved while the CRM has a plausible match;
  - an open deal above a value threshold;
  - a verifier pass disagreeing with a high-stakes claim.
- Haiku's tendency to skip hard mentions means a cascade run keeps fewer "not sure" candidates for review than Opus
  would on calls that do not escalate.
