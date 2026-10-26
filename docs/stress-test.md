# Stress test on third-party sales calls

The two mock calls in `data/calls/` were written for this project, with traps chosen in advance. They test judgment on
known hard cases. They cannot show what breaks on calls nobody tuned for. This test runs the unchanged pipeline on sales
calls written by someone else, for a different seller, with a CRM slice rebuilt from their record IDs.

Aggregate numbers only. No transcript text is stored in this repository.

## Data
**SalesTranscriptQA** (Endgame Labs), built on Salesforce AI Research's **CRMArena-Pro**
([paper](https://arxiv.org/abs/2505.18878), [dataset](https://huggingface.co/datasets/EndgameLabs/SalesTranscriptQA)).
Licence CC BY-NC 4.0. It holds 4,033 synthetic B2B voice-call transcripts for a fictional seller, TechPulse Solutions
(electronic design automation software). Each call carries Salesforce IDs for its account, contact and opportunity.
The data is downloaded at run time into `.context/external/` (gitignored).

## Setup
- **Sample:** deals with several calls, one deal per account, preferring deals whose calls mention competition. The
  sample is deterministic. 8 deals, 23 calls, about 500 words each.
- **CRM slice:** accounts, contacts and opportunities rebuilt from the IDs on every corpus call of those accounts
  (51 contacts, 91 opportunities, 51 sellers). There are no titles, emails or stage history.
- **Tenant configuration, as on day one:** no competitor list, so every competitor name goes to review. Seven generic
  pain themes, written from the product description, not from the calls. The seller's product list stands in for the
  CRM product catalogue, which the dataset does not ship. It holds the names sellers call "our X" in at least 10 of the
  4,033 calls.
- **Speech-to-text noise:** two calls copied with lower case, no punctuation, filler words, anonymous speaker labels
  ("Speaker 1") and the contact's first name misspelled.
- **Model:** `claude-opus-5-5`, effort high. The validator, resolver, graph and queries are the same code as for the
  mock calls.

Reproduce: `uv run --with pyarrow python -I scripts/stress_test.py --deals 8`. Use `--prompt extract_v2` to re-score
the saved v2 outputs without calling the model.

## What broke on the first run (prompt v2), and what changed
| # | Finding | Change | Guarded by |
|---|---|---|---|
| 1 | `deal_brief` crashed on an opportunity with no stage history: the CRM slice has none | Stall query and brief accept a missing stage date ("in stage for an unknown time") | `test_brief_survives_an_opportunity_without_stage_history` |
| 2 | The seller's own product was stored as a competitor: the customer said they already use it | The tenant config lists the seller's products. The validator rejects them as competitors, and prompt v3 shows the list | `test_own_products_and_generic_vendors_are_never_competitors` |
| 3 | "another provider" was stored as a competitor name | The validator rejects names made only of generic words. Prompt v3: named vendors only | same test |
| 4 | Name variants of one vendor: possessives, punctuation, a short form of a longer name | Names are folded (case, punctuation, possessive). A unique word-prefix matches the full name. Unknown short forms merge into the one longer name they start | `test_competitor_names_fold_punctuation_and_unique_word_prefixes`, `test_a_short_unknown_vendor_name_merges_into_the_one_longer_name_it_starts` |
| 5 | The competitor look-up never fired: it required "evaluating" or "current vendor", but these customers mostly compare | Any competitor the customer brings up opens a look-up, unless they ruled it out. Evidence shown from other accounts must still be a committed fact | `test_any_customer_mention_puts_a_competitor_in_play` |
| 6 | Over-extraction: one sentence stored as both a pain point and an objection, and preferences filed as objections | Prompt v3: record each fact once. An objection is a concern or condition, not a preference | measured below |
| 7 | 41% of claims went to review on a tenant with no configuration | Kept on purpose (see "Review queue" below) | n/a |

## Prompt v2 against v3, same 13 calls (4 deals, including the 2 noisy copies)
| | v2 | v3 |
|---|---|---|
| Claims kept | 63 | 55 |
| Objections | 19 | 14 |
| Pain points | 19 | 16 |
| Commitments | 23 | 23 |
| Turns stored as both pain and objection | 5 | 1 |
| Own products or generic names proposed as competitors | 2 | 0 |
| Quotes not found in the cited turn | 0 | 0 |
| Cost for the 13 calls | $0.72 | $0.76 |

**Manual review, 5 calls where v2 had problems.** One reviewer: Claude, reading each claim against its turn.
- **v2:** 26 of 32 claims correct, with 3 duplicates, 2 wrong (the own product and "another provider"; both are now
  rejected by the validator) and 1 weak (an objection read into the customer saying they were still looking at several options).
- **v3:** 23 of 24 correct. The remaining one is a judgment call: the seller's offer of a demo once the customer has more
  clarity is labelled hypothetical, although the customer agreed to it.
- **What v3 lost:** one genuine pain point (the customer needs something that fits their current systems)
  disappeared along with the duplicate objection on the same sentence.

On the mock calls with the answer key, v3 keeps all 18 required facts and all 21 query checks. Precision rises from
75% to 83%.

## Prompt v3 on the full sample (8 deals, 23 clean calls plus 2 noisy copies)
| | |
|---|---|
| Claims kept / quotes not found | 101 / 0 |
| Seller and customer contact linked to the CRM, clean calls | 23 / 23 and 23 / 23 |
| Noisy calls | seller linked from their self-introduction (2/2); misspelled contact left unresolved, not mislinked (2/2) |
| Claims sent to review | 40 of 101: 28 marked "implied" by the model, 8 unknown competitor, 4 unknown pain theme |
| Competitor names proposed for approval | 3 distinct names, 8 mentions; no own products or generic names |
| Cost and time per call (about 500 words) | $0.058, 12 s; whole run $1.44, 5 minutes sequential |

**Approval simulation.** An admin approves the 3 proposed names, and the graph is rebuilt.
- Three deals get a competitor in play. One gets a precedent: on AquaGuard Solutions, Quantum Circuits Inc is a past
  vendor, and the brief now cites CyberWave Security, where the customer said it is their current vendor.
- The reverse direction stays empty on purpose. AquaGuard's statement was marked "implied", so it waits in review and
  is not shown to other deals.
- Under the earlier rule (only "evaluating" or "current vendor" opened a look-up), AquaGuard's past-vendor mention
  would not have triggered it.

**Coverage against the dataset's own questions.** 51 transcript lines are marked as evidence by the dataset's
question set; 16 of them carry an extracted claim. The dataset asks about everything said, such as products pitched and
testimonials. The ontology stores deal-risk facts only, so this is a coverage indicator, not recall.

## Review queue
On the configured mock tenant, 12% of claims go to review (5 of 40). Here the figure is 40%.
- 12 points of that come from vocabulary the tenant has not configured yet. Approving the 3 competitor names clears 8
  of them.
- The larger part is claims the model itself marked "implied". These calls are vague by construction: customers
  mostly say they are reviewing options.

The queue is the price of the precision-first rule. A day-one tenant should be seeded before the first call: the
competitor list from the CRM's competitor field, products from the product catalogue, pain themes from the customer's
own messaging.

## Cost
- About 7,600 input tokens per call. About 2,200 are the call and its context (participants, pain themes). The rest is
  the instructions (1,800) and the output schema (about 3,500), identical on every call.
- That fixed part is now cached. Measured afterwards on the mock calls: 5,351 tokens are read from the cache on every
  call after the first, at 5% of the input price on Opus. That cuts an Opus call on a 500-word transcript from $0.058
  to about $0.038 (`docs/model-comparison.md`).
- Output is about 1,400 tokens for a 500-word call and about 8,000 for the 1,000-word mock call (effort high includes
  reasoning).

## What this does not show
- The calls are generated by an LLM: tidy grammar, two speakers, few names, few competitors. Real calls are longer and
  messier.
- No labelled answer key exists for these calls. Precision here is one reviewer's judgment on 5 calls, not a measured
  rate.
- One run per prompt version; run-to-run variation is not measured.
