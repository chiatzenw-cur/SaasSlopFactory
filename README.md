# SaasSlopFactory — Descles Corporate Runtime

You are the **shareholder / board**. The agents are **management**.
The company is a durable state machine with capital, authority, a constitution and an audit log —
not a group of chatbots pretending to be a company.

```
                    YOU
             Shareholder / Board
                     │  charter · capital · goals
                     ▼
                CEO Agent
       ┌─────────┬───┴─────┬─────────┐
       ▼         ▼         ▼         ▼
    CTO       COO       CMO       CFO
  engineering  ops     demand   unit economics
       └─────────┴───┬─────┴─────────┘
                     ▼
              Descles kernel
       policy · budgets · approvals · ledger · audit
```

## Run it

```bash
python server.py                 # -> http://127.0.0.1:7310
```

Keys are read from the real environment, `$DESCLES_RUNTIME_ENV`, or `<repo>/.keys.env`
(in that order; values are never printed). With no key the agents run in a
**deterministic fallback mode that labels itself `SIMULATED`** in the event log — it never
silently pretends to be a model.

```bash
python cli.py company create --name "TinyFish Labs" --capital 500 --max-experiment 100
python cli.py status <company>
python cli.py tick <company>
python cli.py board <company>          # pending board requests, with evidence
python cli.py approve <company> <req_id>
python cli.py verify <company>         # recompute the event hash chain
```

## The loop (template: Micro SaaS Studio)

```
CMO  sweep live buying signals (real URLs, real quotes)
  ▼
CMO  discover 3 opportunities, each citing signal ids that must exist
  ▼
CTO  cost the smallest shippable MVP
CFO  unit economics — can veto, cannot allocate
COO  the cheapest experiment that can falsify the hypothesis
  ▼
CEO  pick one move: <= its unilateral limit -> spend it · above -> board request
  ▼
YOU  approve / reject          (money moves only on approval)
```

## What is actually enforced (in code, not in a prompt)

| Role | May do alone | Needs board approval | Never |
|---|---|---|---|
| CEO | allocate ≤ $100 / experiment, kill, spawn | allocate > $100, publish legal terms, hire humans | borrow money, bank transfer |
| CTO | write code, deploy staging, ≤ $50 cloud | deploy production, cloud > $50 | publish legal terms, hire |
| CFO | inspect spend, pause budget, **veto** | — | allocate budget, alter revenue numbers |
| CMO | search, read public forums, ≤ $50/day ads | send email | mass unsolicited email, deceptive marketing |
| COO | run experiment, publish landing page, ≤ $50 ops | change pricing, hire | mass unsolicited email |

Plus charter clauses (`no_debt`, `no_deceptive_marketing`, `no_mass_unsolicited_email`,
`no_legal_commitments_without_approval`, `kill_products_after_validation_failure`) which
**beat every role permit**. And the wall that matters: *cash*. Nobody — not even the board —
can spend money the company does not have.

Every attempt, allowed or denied, is written to the `actions` table with its reason, and every
resulting effect becomes a hash-chained event.

## Verified end to end (this machine, real DeepSeek calls)

```
$ python cli.py tick co_3a9dc87ea6
sweep      68 signals from 10 ok sources
discover   audio-flashcard-app · video-prototype-validation · transcript-video-clipper
evaluate   audio-flashcard-app: CFO veto — expected CAC 800c > price 500c
evaluate   video-prototype-validation: validated, PASS
decide     advance_project ALLOW — $36.00 (inside the CEO's $100 limit)

CEO requests $300   -> APPROVAL, board request filed, cash untouched
CEO requests $900k  -> DENY, insufficient cash ($464.00 available)
CFO requests $50    -> DENY, CFO is forbidden from allocate_budget
board approves $300 -> cash 464.00 -> 164.00, ledger gets a row, chain valid (27 events)
```

## The human boundary

The company runs unattended. A human is asked for exactly two things — **money** and
**identity** — and both arrive as a **setup request**, which is a different object from a
board request:

```
BOARD REQUEST   "may I spend this?"          money does not move until you answer
SETUP REQUEST   "I cannot proceed without you"  an action only a human can take
```

A missing capability parks **one project**; it never stops the company and it never degrades
into a fake success. With no payment rail connected, the built landing page renders a
**visibly disabled** CTA plus a banner — never a dead link that looks alive.

```
$ python cli.py setup <company>       # what is waiting on you, with numbered steps
$ python cli.py connect <company> <setup_id> --note "paddle sandbox key added"
```

## The money path

A `REVENUE` row is written by a signature-verified payment webhook and nothing else — not a
model, not a fixture, not a summary of a conversation.

- HMAC verified over the **raw body** (`Paddle-Signature: ts=…;h1=…`, `Stripe-Signature: t=…,v1=…`)
- `webhook_events(provider, event_id)` idempotency ledger — Paddle is at-least-once
- stale timestamps and tampered bodies rejected; a duplicate never double-counts MRR
- the amount comes from the provider's own `details.totals` (fee/earnings included), and the
  project is attributed via `custom_data.project_slug` — when the payload carries none, the
  event says `inferred from the most recent build` instead of guessing silently
- `POST /api/webhook/paddle/<company_id>` and `/api/webhook/stripe/<company_id>`

`scripts/test_revenue.py` proves the whole gate **with no Paddle account**, because the
signature is a local HMAC: valid accepted, forged rejected, stale rejected, tampered rejected,
replay deduped, ledger + MRR + attribution + funnel all checked. Revenue tests run against a
throwaway DB — a test payment never touches a real company's books.

## Measurement

`/p/<slug>` is served by the runtime and reports its own traffic (a 1×1 pixel, no JS
dependency) and its CTA clicks. The COO's success criterion is therefore a **measured rate**
against what you asked it to verify:

```
Measured   40 views · 3 pricing-intent (7.5%) · $19.00 revenue   ON TRACK vs 5% criterion
```

## What is real vs. stubbed

| Real | Stubbed in v1 |
|---|---|
| Event-sourced company state, hash-chained audit, verified | Not deploying to a real host |
| Policy/authority engine with real deny paths | No Stripe / Ads connectors (listed as NOT_CONNECTED, never as empty) |
| Ledger with real debits, cash wall | Revenue only arrives if something real produces it — MRR is $0.00 |
| Buying signals fetched from live sources (HN Algolia) with verbatim quotes | Reddit / ProductHunt need OAuth, so they are absent, not faked |
| CMO → CTO → CFO → COO → CEO with real model calls | Build/deploy phase is where a harness plugs in next |
| Build: real files on disk, served, measured | The "product" behind the page is still a landing page, not shipped software |
| Deploy: real Vercel v13 API call when a token exists | Untested against Vercel without a token |
| Revenue: real signature-verified webhook → ledger | Untested against live Paddle without an account |

## Layout

```
descles/
  db.py           sqlite schema; state lives here, never in memory
  events.py       append-only hash-chained event log + verify()
  roles.py        role specs: objectives/metrics/permits/limits
  policy.py       the constitution. authorize() is the only gate
  capabilities.py capability registry + setup requests (the human boundary)
  billing.py      payment rails, HMAC verification, the only REVENUE writer
  metrics.py      views / pricing intent / revenue — what the criteria are judged on
  runtime.py      incorporate / sweep / discover / evaluate / decide / build / deploy / tick
  build.py        the landing page: code writes structure, the model writes copy
  agents.py       the C-suite as pure functions of state -> structured proposals
  signals.py      real signal sources; a dead source reports its status, never a zero
  console.py      localhost:7310 (stdlib http.server) + /p/<slug> + webhooks
  web/index.html
cli.py            descles <command>
server.py         entrypoint
scripts/smoke.py        kernel tests: policy matrix + one full tick
scripts/test_revenue.py money path: signature, idempotency, ledger, funnel
```

## Why the CFO veto is a feature

```
CEO:  Spend $400 on ads.
CFO:  Rejected. Expected CAC 800c exceeds the stated price 500c.
```

Competing incentives inside a company are not a coordination bug to be smoothed away —
they are how a real organisation stops one person steering the ship onto the rocks.
The interesting property here is that the disagreement is *structured*: the CFO cannot
allocate, the CEO cannot touch the books, and neither can lie about the result, because
the ledger and the event chain are written by the kernel.
