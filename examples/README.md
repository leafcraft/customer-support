# customer-support — example scenarios

Four realistic scenarios that exercise the major paths through this mesh.

## How to run

```bash
create-leafmesh --load .   # once: loads the team into the Redis in .env (or use create-leafmesh --start .)
python examples/run_examples.py                 # all scenarios
python examples/run_examples.py 01_             # one scenario (substring match)
```

## Scenario map

- **01_password_reset_routine** — `manual_ticket` — Routine support ticket — password reset request via web channel. Should triage as how_to, low priority, no specialist; sentiment neutral; bot drafts response → human QA approves.
- **02_billing_dispute_escalation** — `manual_ticket` — Enterprise customer disputing a billing line — triage routes to billing specialist, sentiment is negative, hard escalation keyword 'regulator' triggers urgent priority.
- **03_security_incident** — `manual_ticket` — Suspected account takeover — hard-escalation keyword 'breach' force-bumps priority to urgent, requires specialist (security topic), Tier-3 SME is on the call.
- **04_kb_gap_signal** — `repeat_question_burst` — Triggered when 3+ tickets in 24h ask the same novel question — kb_gap_filler_agent drafts a missing KB article for the support manager to publish.

## Audit trail

Every scenario appends one hash-chained entry to the template's
`logs/<audit>.jsonl` file. Tail it during a run to see the
ledger build up across scenarios.
