# Full-corpus stress test: 3,454 third-party calls

The first stress test (`docs/stress-test.md`) used 23 calls. This one runs every call in the same dataset that carries
CRM IDs, through the production configuration, to see what breaks at the size of one real customer.

Aggregate numbers only; no transcript text is stored in this repository.

## Setup
- **Data.** SalesTranscriptQA (Endgame Labs), built on Salesforce AI Research's CRMArena-Pro, licence CC BY-NC 4.0.
  3,454 of its 4,033 calls carry account and opportunity IDs. They all belong to one seller company, so they form one
  tenant.
- **CRM, rebuilt from the calls' IDs:** 101 accounts, 631 contacts, 1,170 opportunities and 93 sellers.
- **Configuration as designed:**
  - prompt v3;
  - Haiku 5.5, with Opus 5.5 for calls whose candidate pool has a shared first name;
  - prompt caching;
  - no competitor list (an unconfigured tenant);
  - 24 calls in parallel.
- **Reproduce:**
  - `uv run --with pyarrow python -I scripts/stress_full.py` (resumable);
  - `scripts/verify_sample.py` for the quality sample.

## The run
| | |
|---|---|
| Calls extracted | 3,454 of 3,454. **0 failures:** no refusals, cut-offs, parse errors or API errors |
| Model split | Haiku 3,137; Opus 317 (9.2%, the shared-first-name rule) |
| Cost | **$18.87 in total** (Haiku $6.81, Opus $12.06): **$0.0055 per call** |
| Prompt cache | 70% of input tokens read from the cache |
| Time | 46 minutes at about 93 calls a minute; 15 s per call at the median, 23 s at p95 |

## The graph
| | |
|---|---|
| Nodes | **107,505:** 72,715 turns, 15,672 mentions, 13,565 claims, 3,454 calls, 1,170 opportunities, 631 contacts, 101 accounts, 93 sellers, 57 provisional people, 29 competitors, 18 concepts |
| Edges | **203,695** |
| Build from saved extractions | 4.3 s (validation, entity resolution and loading) |
| Busiest nodes | Pain-theme concepts (security 826 claims, integration 824) and competitors (Quantum Circuits Inc 451, CircuitWave 429) |
| Query latency, in-memory, after competitor approval | Deal brief 6 ms at the median, 10 ms at worst; competitor precedents 2 ms and 10 ms (60 deals) |

**Bottleneck found and fixed.** The first full report did not finish in ten minutes: every precedent triggered a scan of
all 107,000 nodes. An index of claims by call brought the whole report down to 19 seconds. A graph database gives that
index for free, but the catalogue's queries must be written to use it.

A second, self-inflicted slowdown: the index's freshness check counted edges, and NetworkX walks every edge to count
them. It now uses the node count.

## Claims and their quality
- **13,565 claims, 3.9 per call:**
  - 4,682 commitments;
  - 3,906 pain points;
  - 2,275 objections;
  - 1,774 competitor stances;
  - 928 committee roles.
- **Quotes not found in the cited turn:** 1 of 13,566.
- **Modality:** 196 claims hypothetical, 9 negated.
- **Sent to review:** 31%. Every competitor name goes to review on an unconfigured tenant (1,774). 485 pain points fell
  outside the seven themes ("other", 12%).

**Verifier sample.** The verifier (`src/kg/verify.py`) is a second model call that asks whether the quote, read with the
turns around it, supports the claim as stated. It was run on 300 random claims, for $1.08:

| | Claims | Fully supported | 95% interval | Partly |
|---|---|---|---|---|
| All | 300 | **69%** | 63-74% | 31% |
| Committed | 198 | **79%** | 73-84% | 21% |
| Sent to review | 102 | 48% | 39-58% | 52% |
| Commitments | 96 | 86% | 78-92% | 14% |
| Competitor stances | 45 | 71% | 57-82% | 29% |
| Pain points | 91 | 68% | 58-77% | 32% |
| Objections | 49 | 59% | 45-72% | 41% |
| Committee roles | 19 | 0% | 0-17% | 100% |

- **No claim was judged unsupported.**
- **What "partly" means.** It marks category boundaries (a stated requirement filed as an objection, a forward-looking
  concern filed as a pain) and thin role inferences ("take it back to my team" read as an influencer).
- **Review status works.** It separates the weaker claims (48% fully supported) from the committed ones (79%). All the
  role claims read were already held for review.
- **The verifier's own reliability.** Claude read 30 of the sampled claims in context and agreed with the verifier's
  "fully supported or not" call on 29. This is the same model family, so a human sample is the real calibration.

**What the corpus contains:**
- An economic buyer is named in only 9 of 1,170 deals. Most role claims are influencer (551) or champion (260). These
  calls rarely say who signs, so the "economic buyer never met" insight needs calls that discuss it.
- 1,187 of the 4,010 transcript lines cited by the dataset's own questions carry a claim (30%). Those questions ask
  about everything said; the claims cover deal risk only.

## Entity resolution: what scale exposed, and the fixes
Same extractions, no new model calls (resolution replays from saved outputs):

| | Before | After |
|---|---|---|
| Seller speakers not linked | 197 | 0 |
| Customer speakers not linked | 79 | 0 |
| Speaker links correct, checked against the dataset's own IDs | not measured | **100% (6,909 / 6,909)** |
| Person mentions linked | 73% | **95%** |
| Person mentions "not sure" | 26% | **4%** |
| Person mentions not in the CRM | 1% | 1% (57 provisional people) |

Four causes, none visible on the two mock calls:
1. **The candidate pool held all 93 sellers.** "Thanks, Liam" tied with two sellers named Liam who were never on the
   call. The fix is blocking: match the people the call is plausibly about first (the account's contacts, everyone on
   the call, the deal's owner and team), and widen only when nobody there fits. Widening never breaks a tie.
2. **The recorder's side was not used for speakers.** A customer and a seller elsewhere shared a name. Side is a fact
   for speakers, so it now filters the pool. For mentioned people side is the model's guess, so it still never breaks
   a tie.
3. **Surnames compared by Jaro-Winkler.** That rewards shared letters, so "Lucas Almeida" and "Lucas Moreira" scored
   0.86 and an exact full name could not clear the margin. Surnames now use edit distance. The transcription variant
   "Ahlders / Aalders" still matches, and an initial ("Sanne V.") matches the surname's first letter.
4. **A bug in my reconstruction of the data.** It marked 83 calls' customer contacts as sellers whenever a seller
   elsewhere had the same name. The check against the dataset's IDs found it: after fixes 1 and 2, 83 speaker links
   were still wrong, and all were these. Fixed in `scripts/stress_test.py`, and those 82 calls were re-extracted. None was in the
   23-call test.

Claude read a sample of 25 mentions that blocking newly linked; all were plausible, mostly greetings that name the
call's contact. Each fix has a test.

## Competitors and cross-account precedents
- **Vocabulary discovered on an unconfigured tenant:** 25 distinct competitor names. Five carry most customer mentions:
  - Quantum Circuits Inc, 365;
  - NanoDesign Systems, 291;
  - CircuitWave, 283;
  - Adaptive Design Solutions, 167;
  - FlexEDA, 72.
- **One leak:** "Workflow Genius" (16) is TechPulse's own product, missing from my stand-in catalogue. That is why the
  product list must come from the CRM's catalogue, not from a scan of calls.
- **Approval round:** an admin approves every name customers raised in at least three calls (16 names). **712 of 1,170
  deals** then get precedents from other accounts. Quantum Circuits Inc appears in 87 of 101 accounts.
- **Precedent volume:** **188 per deal at the median, up to 626.** A brief cannot list them all. It now picks one per
  account, ranked by shared pains, then the most telling stance (left it, ruled it out, uses it), then recency. It shows
  three per competitor and "N more". `kg ask` gets the best ten and the total. A brief is now 22 lines at the median and
  31 at most; it was 33 and 137.

## What this does not show
- The calls are LLM-generated: about 500 words, tidy, few names. Real calls are longer and noisier, and per-call cost
  grows with length.
- There is no answer key. Precision comes from a model verifier and one same-family reader.
- One run.

Spend for this test: about $20 (extraction $18.87, re-extraction of 82 calls, verifier sample $1.08).
