# Account-intelligence knowledge graph: POC

Unifies **CRM data** (accounts, contacts, deal stages) and **sales-call transcripts** into one knowledge graph, so a
seller can see why a deal is stuck and what other customers said about the competitor they are up against.

> Status: working prototype. Live runs saved under `runs/` for prompts v1, v2 and v3. The main run is `extract_v3__claude-opus-5-5`:
> all 18 required facts found, 11/11 people linked correctly or correctly left unresolved, 0 traps turned into facts,
> 21/21 use-case checks pass, precision 83% against the answer key as written before the prompt (100% after a logged
> review of the extra claims, recorded in the key).
>
> Also measured:
> - the pipeline run unchanged on 23 sales calls written by someone else (`docs/stress-test.md`);
> - Haiku, Sonnet and Opus compared, giving a Haiku-to-Opus cascade with prompt caching (`docs/model-comparison.md`);
> - the whole third-party corpus run through it (`docs/stress-test-full.md`): 3,454 calls, 0 failures, $0.0055 per call,
>   a 107,505-node graph. All 6,909 speakers linked correctly, and a verifier rated 79% of committed claims fully
>   supported, with none unsupported;
> - questions in plain language over the graph (`kg ask`, `docs/ask-examples.md`): 13/13 on a smoke evaluation, including
>   an instruction planted in a quote;
> - a prompt-injection probe: instructions read out in a call stayed data on Haiku and Opus.
>
> What changed between prompt versions, and why: `docs/DESIGN.md` §3 and `docs/stress-test.md`; every run is saved
> under `runs/`.

## Deliverables
| The brief asks for | Here |
|---|---|
| Architecture document or slides: use case, system design, trade-offs | `docs/DESIGN.md`, which follows the brief section by section and maps each part to code; `slides/deck.pdf` |
| Proof of concept with its mock data | this repository: `data/` (mock CRM export, two call transcripts, answer key), `prompts/`, `src/kg/` |
| Supporting evidence | `docs/stress-test.md` (third-party calls), `docs/model-comparison.md` (model and cost choice), `docs/ask-examples.md` (natural-language answers), `scripts/ask_eval.py` (13-question smoke evaluation, results in `runs/ask_eval__*`), `scripts/injection_probe.py` (prompt-injection probe on `data/probes/`) |

## Data boundary: one organisation, its own data
All data in this POC belongs to **one Showpad customer**, the fictional Kestrel Systems (`tenant_id: t_kestrel`): its own
CRM and its own recorded sales calls. "Accounts" (Aurelia Foods, Brennan Packaging) are Kestrel's customers inside
Kestrel's CRM. Rules the design follows and the prototype enforces:
- **Nothing crosses organisations.** Kestrel's data is never used for another Showpad customer. Pooled, cross-company
  benchmarks would be a separate, opt-in product and are out of scope.
- **Inside the organisation, CRM access applies.** A seller sees insights from another account only if the CRM lets them
  see that account (here approximated as: they own a deal on it or are on its deal team). Otherwise the brief shows a
  count, never a name or a quote (`uv run kg brief --as <UserId>`).
- **Internal preparation only.** Insights from one customer's calls help the seller prepare; they are never repeated to
  another customer. Using a customer as a reference needs that customer's permission.
- **Personal data stays deletable.** Every extracted claim points to its call, so deleting a call removes what was
  derived from it.

## The use case in one paragraph
Tom (account executive at Kestrel Systems, which sells quality-management software) has a €210k deal with Aurelia Foods
that has sat at *Proposal* for 58 days with two close-date pushes. The graph shows that "the group CFO", named twice on
the last call (once as "Hendrik Ahlders" by the transcription), is Henrik Aalders, a CRM contact who has never been on a
call and holds no role on the deal. It also shows that Brennan Packaging, another account Tom owns, ran Veritrack (the
cheaper competitor Aurelia is evaluating) for a year: "Implementation took nine months, not the four they sold us, and
we pulled out." Brennan's CRM has no competitor record, so only the transcript knows this. Tom books the CFO with a
payback model, and uses the Brennan insight to prepare: he asks Aurelia about implementation timelines and offers a
reference call with a customer who has agreed to be a reference. He does not quote Brennan to Aurelia.

## How it works
```
data/crm/*.json ─────────────────────────────────────────────┐ (deterministic load)
data/calls/*.json ─► extract (Claude, structured output) ─► validate ─► resolve ─► graph ─► queries ─► deal brief
                       prompts/extract_v3.md   runs/<run>/   gates       ER         NetworkX            eval vs gold
```
1. **Extract** (`src/kg/extract.py`): one call per transcript, constrained to the Pydantic contract in `src/kg/schema.py`
   (person mentions, pain points, competitor mentions, objections, commitments, committee roles). Every item carries a
   verbatim quote, a turn ID and a modality (`asserted` / `negated` / `hypothetical`). Raw outputs are saved under `runs/`
   so every later step can be replayed without the API.
2. **Validate** (`src/kg/validate.py`): the hallucination gate. A quote not found in its cited turn is rejected; unknown
   person references are rejected; near-verbatim quotes, implied claims and out-of-vocabulary competitors are kept but
   marked `needs_review` instead of `committed`.
3. **Resolve** (`src/kg/resolve.py`): links speakers and mentioned people to CRM Contacts and Users. The LLM supplies
   context (which side the person is on, the role the conversation gives them); deterministic scoring does the matching
   (email, name and nickname similarity, title match, attendance), then links, keeps scored candidates for review, or
   creates a provisional person when someone is not in the CRM.
4. **Graph** (`src/kg/graph.py`): CRM layer, transcript layer (Call, Turn), mention layer and claim layer. Claims are
   reified `Assertion` nodes, so each keeps its quote, modality, status and provenance.
5. **Query** (`src/kg/queries.py`): three deterministic competency queries (buying committee and gaps, why the deal is
   stalled, competitor precedents in other accounts), combined into a deal brief. `kg ask` (`src/kg/ask.py`) lets Claude
   answer a question by calling these queries as tools. It never writes a query, and the asking seller's CRM visibility
   is fixed in code.
6. **Evaluate** (`src/kg/evaluate.py`): scores a run against a hand-written answer key (`data/gold/answer_key.json`),
   written before the prompt: entity-resolution accuracy, per-predicate precision and recall, trap violations, and the
   use-case query checks.

## Run it
```bash
uv sync
uv run pytest                          # 81 tests, no API key needed
cp .env.example .env                   # then put your key in .env: ANTHROPIC_API_KEY=... (gitignored)
uv run kg extract                      # writes runs/extract_v3__claude-opus-5-5/
uv run kg demo                         # build + deal brief + evaluation on the saved v3 run
open out/graph.html                    # interactive graph view
uv run kg brief --as 005KS00000PDEWITAA   # the same brief as a seller who cannot see Brennan
uv run kg ask "Why is the Aurelia deal stuck?"                       # Claude answers from the queries
uv run kg extract --model claude-haiku-5-5 --escalate-to claude-opus-5-5 --run my-cascade   # cheap model, hard calls to Opus
```
To exercise the deterministic half without any LLM output, point the commands at the hand-written fixture:
`uv run kg demo --run "$PWD/tests/fixtures/ideal_run"`.

Stress test on third-party calls (downloads a CC BY-NC dataset into `.context/`, never committed; see
`docs/stress-test.md`): `uv run --with pyarrow python -I scripts/stress_test.py --deals 8`.

## Mock data and its traps
Synthetic data only (`.example` domains). The transcripts plant the cases the brief calls hard:

| Trap | Where | Expected handling |
|---|---|---|
| Title-only reference: "the group CFO" | Aurelia u028 | linked to Henrik Aalders by title |
| Transcription misspelling: "Hendrik Ahlders" | Aurelia u030 | linked to Henrik Aalders by fuzzy name |
| Nickname: "Maggie" | Aurelia u002 | linked to Margaret Dekker |
| Same first name, two people: "Pieter" | Aurelia u040 / u041 | customer-side procurement vs the seller's engineer |
| Person not in the CRM: "Ravi" (anonymous dial-in) | Aurelia u031 | provisional person, never forced onto a contact |
| Phonetic misspelling: "Chevonne" | Brennan u008 | never guessed: becomes an unknown person for review |
| Negation: "Budget isn't the problem" | Aurelia u038 | recorded as ruled out, never an open objection |
| Hypothetical: "if we could get you live ... in ninety days" | Aurelia u034 | not a commitment |
| Ruled-out competitor: QualiSphere | Aurelia u021 | stance `rejected` |
| Integration target, not competitor: SAP | Aurelia u031 | not a competitor |
| Seller's opinion of a competitor | Aurelia u048 | kept, but never used as customer evidence |

## Repository layout
```
data/        mock CRM export, two call transcripts, tenant vocabularies, gold answer key, an injection probe
prompts/     versioned extraction prompts
src/kg/      pipeline (data, schema, extract, validate, resolve, graph, queries, evaluate, viz, cli)
runs/        saved raw LLM outputs per run (replayable, committed)
scripts/     stress test on third-party calls (data never committed), prompt-injection probe, kg ask smoke evaluation
docs/        design document (DESIGN.md), stress test, model comparison, recorded `kg ask` answers
tests/       unit and end-to-end tests; fixtures/ideal_run is a hand-written ideal extraction
```

## Known limitations
- One tenant, two mock calls with an answer key, plus 23 third-party calls without one. No incremental re-processing,
  no time-versioned facts.
- CRM visibility is approximated from deal ownership and deal teams; production would sync the CRM's real sharing
  rules, and would only show counts of hidden accounts above a minimum group size.
- The nickname table and title abbreviations are tiny demo lists; production needs a name-variant resource.
- Names are compared by spelling, not sound: a phonetic transcription error such as "Chevonne" for Siobhán becomes an
  unknown person for review instead of a match.
- Quote checking proves words were said, not that they mean what the claim says; a misread negation passes it. The
  verifier pass (`src/kg/verify.py`) catches that case, but does not yet gate claims.
- The same-name rule asks for a second clue, but side and role hint both come from the model. One misreading supplies
  both: Sonnet and Opus at low effort linked the wrong Pieter. The cascade sends such calls to Opus at high effort; a
  sturdier rule would need a clue the model does not produce, such as the speaker's own side.
- Speaker-to-turn assignment is trusted as given by the recorder.
- Production design (streaming ingestion, graph database, multi-tenancy, GraphRAG) is described in `docs/DESIGN.md`, not
  built here.
