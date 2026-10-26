# `kg ask`: recorded answers

Real outputs, unedited, from `claude-opus-5-5` at effort medium with prompt `ask_v1` (2026-10-25; the current
prompt `ask_v2` adds one rule about instructions inside quotes, tested in `scripts/ask_eval.py`). The graph was built from the
`extract_v3__cascade-haiku-opus` run: Aurelia extracted by Opus, Brennan by Haiku (`docs/model-comparison.md`).
The first line of each answer lists the tools Claude called. Each tool runs one fixed query with the asking seller's
CRM visibility (`src/kg/ask.py`). Answers vary from run to run.

## Tom Okafor (owns both deals)

`uv run kg ask "Why is the Aurelia deal stuck, and what should I do before my next call?"`

```text
Asked as Tom Okafor. Tools called: find_opportunities({"text": "Aurelia"}); stall_signals({"opportunity_id": "006KS00000AURQMSAA"}); buying_committee({"opportunity_id": "006KS00000AURQMSAA"}); competitor_precedents({"opportunity_id": "006KS00000AURQMSAA"})

**Why it's stuck:** It has been in Proposal/Price Quote for 58 days and the close date has moved twice (now 2026-12-18). Budget isn't the problem. Margaret Dekker said "Budget isn't the problem, it's in this year's plan" (call_aur_20261014/u038). Four things are blocking it:

1. **The person who signs hasn't been involved.** Deals above 200K go to the Group CFO, Henrik Aalders: "it goes to the group CFO, he signs, not me" (u028). He hasn't been on any call and has no role on the deal in the CRM.
2. **Veritrack is about 30% cheaper:** "it's about thirty percent below yours" (u018). Margaret says what matters is "Proving payback to group" (u038).
3. **Security review:** "everything has to stay in the EU and we'll need your SOC 2 report and a pen-test summary" (Sanne Visser, u024).
4. **Our side is behind on what we promised:**
   - Your recap ("I'll send a recap this afternoon," u045) was due 10/14 and is 9 days overdue.
   - Pieter de Wit's security pack (SOC 2 Type II, pen-test summary, EU hosting addendum) was due 10/16. It's overdue and the CRM task is still "Not Started" (u025).

There's also time pressure. Joris van den Berg said if group pushes this to next year, "we'd miss the spring audit window" (u036).

**Before your next call:**
- **Today:** send the recap. Also send the two pricing options and payback model you promised "by Friday the twenty-third" (u039), which is today.
- **Make the payback model about the CFO**, since he signs and price is the objection. Joris offered CAPA numbers per plant, but only tentatively: "I'll try to pull the CAPA numbers per plant" (u044). Treat that as unconfirmed and follow up.
- **Get Pieter de Wit to send the security pack now.** It should also include the SAP connector and REST API documentation he promised (u032). He also said he'd chase Sanne's security questionnaire this week (u042).
- **Check which Pieter gets the pricing.** Margaret said "loop in Pieter on those, procurement will want both options" (u040). That most likely means Pieter Jansen in procurement, not Pieter de Wit. His procurement role is unconfirmed (needs_review). Make sure he gets both options.
- **Lock in the CFO meeting.** Margaret offered "thirty minutes with the CFO in the week of the ninth of November" (u043). Confirm the date with her.
- **Update the CRM:**
  - Mark Aalders as economic buyer.
  - Mark Jansen as procurement.
  - Add Ravi, the enterprise architect handling the SAP integration: "I'd need your API documentation, because we'd integrate with our SAP" (u031). He isn't in the CRM yet, and his technical evaluator role is unconfirmed.

**Competition (for your prep only):** QualiSphere is out: "group IT killed that last year" (u021). On Veritrack, another customer said they ran it for a year at one site, the nine-month rollout was slow, and they pulled out (call_brn_20260930/u004). That's a single example. You could use it to frame time-to-value and the spring audit deadline against Veritrack's lower price. Don't name or quote that customer to Aurelia.
```

## Pieter de Wit (on the Aurelia deal team only)

`uv run kg ask --as 005KS00000PDEWITAA "What have other customers said about Veritrack?"`

```text
Asked as Pieter de Wit. Tools called: find_opportunities({}); competitor_precedents({"opportunity_id": "006KS00000AURQMSAA"})

I can't show you anything other customers said about Veritrack. Your permissions don't give you access to any of it.

I could only check this through a deal that faces Veritrack. The only deal you can see is **Aurelia Benelux - Kestrel QMS (3 plants)** (006KS00000AURQMSAA, Proposal/Price Quote, owned by Tom Okafor). For Veritrack on that deal:

- **Accounts you can see:** none have anything recorded about Veritrack.
- **Accounts you can't see:** 1 has something on Veritrack. You don't have access to that account, so I can't tell you which one it is or what was said.

If you need that view, the deal owner (Tom Okafor) or someone with access to that account could share it. Use it only for your own preparation, not in conversations with Aurelia.
```

## Pieter de Wit, asking about a deal he cannot see

`uv run kg ask --as 005KS00000PDEWITAA "What's going on with the Brennan deal?"`

```text
Asked as Pieter de Wit. Tools called: find_opportunities({"text": "Brennan"}); find_opportunities({})

I can't find a Brennan deal. Searching for "Brennan" returned nothing, so either no deal or account by that name exists, or your CRM permissions don't let you see it. I can't tell which from here.

The only deal visible to you is:

- **Aurelia Benelux - Kestrel QMS (3 plants)**: Aurelia Foods Benelux B.V., at the Proposal/Price Quote stage, owned by Tom Okafor

If Brennan is a contact's name and not the account's, it might belong to a deal under a different account name. Do you want me to look at Aurelia? Otherwise, check the exact account name, or ask the deal owner or your CRM admin about access.
```

## Tom Okafor, asking something the graph does not hold

`uv run kg ask "What is Aurelia's annual revenue?"`

```text
Asked as Tom Okafor. Tools called: find_opportunities({"text": "Aurelia"})

I can't answer that with these tools. None of them return account financials like annual revenue.

The only Aurelia record I can see is your deal **Aurelia Benelux - Kestrel QMS (3 plants)**. The account is Aurelia Foods Benelux B.V., and the deal is in the Proposal/Price Quote stage. I only get the deal name, account, stage, owner and whether it's closed, not company details.

To find the revenue, check the account record in the CRM directly or a source like the company's filed accounts. Benelux B.V. companies usually file annual accounts with the Dutch Chamber of Commerce (KvK). I can help with what I do have on this deal: the buying committee, why it might be stuck, or what other customers have said about competitors you're up against.
```
