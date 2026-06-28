# customer-support — Customer Support

Multi-channel reactive ticket triage. Realises the **supp × saas** ROI
cell. Peer-to-peer team — intake owns EVERY channel (voice included):
it normalises AND triages in one stage, fanning out to sentiment in
parallel, and routes voice calls human/callback/self-serve;
KB-grounded responses (retrieval + draft in one agent); QA gate before
customer sees anything; channel-native HITL.

**YAML-first**: `configs/config.yaml` IS the support desk. Every
business rule lives in the agent prompts; ticket / CSAT / SLA /
call-record state lives in YOUR support desk + telephony + CRM
(Zendesk/Intercom, Twilio/Aircall, Salesforce) behind n8n connector
agents. The only Python is the WORM audit sink
(`agency/_shared/audit_logger.py`) and the egress actuator
(`agency/reply_dispatcher_agent.py`) — the role that actually *sends* the
approved reply, with a deterministic fail-closed floor.

## Why it works in an organization

Support is a **cost-out** function: roughly 70% of inbound tickets are
routine and repetitive. This desk resolves that routine majority
**agent-side** — KB-grounded, QA-gated, audited — and escalates only the
remainder to the right human tier *with full context*, across **every
channel a customer actually uses** (call, web form, WhatsApp, Telegram,
chat, email). It plugs into the systems the org already runs
(Zendesk/Intercom, Salesforce/Segment, Twilio/Aircall) rather than
replacing them, and **every customer-facing reply clears a PII + policy
gate and a human review** before it sends — so deflection never comes at
the cost of trust or compliance. The payoff is measurable: tickets
deflected, faster first response, fewer escalations, and a self-healing
knowledge base (gaps become drafted articles).

## The team (15 agents)

| Agent | Role | Responsibilities | Can perform |
|---|---|---|---|
| `ticket_intake_agent` | Front door for every channel (incl. voice) | Normalise any channel payload → canonical Ticket; classify intent / priority / specialist; scan hard-escalation keywords; route voice calls (human / callback / self-serve; fail-closed on no transcript) | LLM (intent_taxonomy KB); listens on the `support_events` stream; fans out to sentiment; routes to response / queue_manager (escalation or cancel) / tier-1 (voice) |
| `sentiment_agent` | Emotional read | Score frustration / urgency / churn_risk / polite; cite the phrases that drove each score | LLM; parallel co-input to response (no routing of its own) |
| `response_agent` | Knowledge-grounded answer + draft | Retrieve the answer from the KB (cite sources; no source ⇒ confidence ≤ 0.4); draft the tone-aware reply in one pass | LLM (support_kb RAG); fan-in waits for intake (+ sentiment); routes to QA (conf ≥ 0.7) or tier-2 (< 0.7) |
| `qa_agent` | Pre-send safety gate | Check accuracy vs cited sources + tone; exhaustive PII sweep; policy-word probe; block on any PII/policy hit | LLM; routes approved or flagged drafts to tier-1 |
| `tier_1_agent_human` | Tier-1 operator | Review + approve the reply; escalate when needed | Human (channel-native HITL); approve → reply_dispatcher; default-deny fallback → tier-2 |
| `reply_dispatcher_agent` | Egress actuator | SEND the approved reply on the channel it arrived on (in-thread; batched when async/email), log it to the ticket; fail-closed on unapproved / unresolved PII | Programmatic (COMMS_DISPATCH_WEBHOOK_URL; Slack/email/CPaaS), dev-store outbox fallback; terminus → audit |
| `tier_2_specialist_human` | Tier-2 specialist | Billing / security / account depth; escalate to SME | Human; fallback → SME |
| `sme_human` | Tier-3 SME | Engineering / security / legal escalations | Human; fallback → halt |
| `queue_manager_human` | Queue owner | Own escalations, cancel-intent retention, SLA breaches, the legal/breach gate | Human; fallback → halt (default-deny) |
| `support_audit_logger_agent` | WORM audit | Hash-chain every reply sent + every escalation | External (custom sink); terminus |
| `sla_watchdog_agent` | SLA monitor | Find tickets past SLA in your support desk | Programmatic (n8n connector); cron when wired |
| `kb_gap_filler_agent` | Knowledge-debt closer | Draft the missing KB article from the resolution path | LLM (kb_style_guide KB); routes draft to queue_manager review |
| `support_insights_agent` | Insights analyst | CSAT/NPS rollups + trending/spiking ticket clusters | Programmatic (n8n); modes csat_ingest / cluster_ingest / report; spike → kb_gap_filler |
| `cross_channel_agent` | Identity unifier | Resolve the customer across channels; flag thread merges | Programmatic (n8n, CRM identity); routes to response or queue_manager |
| `crm_case_generator_agent` | Case writer | Draft the categorised, push-ready CRM case at resolution | LLM (crm_taxonomy KB); routes to insights |

> **Egress (now real):** `reply_dispatcher_agent` is the one connector
> actor that finishes the job — channel chosen at runtime
> (Slack/email/WhatsApp/…), in-thread, batched when the session is
> async/closed. Approval is not delivery: tier-1 signs off, the dispatcher
> sends, audit records it. The inbound adapters (LiveKit, channel
> webhooks) sit *outside* the mesh and make the inbound mesh calls.

## Entry points (Rule 1)

8 channels: `slack_message`, `email_inbound`, `chat_widget`,
`phone_transcribed`, `web_form`, `social_channel`, `manual_ticket`,
`callback_followup` — plus `phone_call_intake` (live voice routing),
`post_resolution_csat` / `csat_rollup` / `ticket_pattern_scan`
(support-desk insights via n8n), KB-gap signals, and per-agent
direct calls.

## Trust mechanisms

- **No-source-no-answer** — response_agent's KB retrieval caps
  confidence at 0.4 if it returns zero cited sources (low confidence
  routes to tier-2 instead of the QA gate)
- **PII sweep** — qa_agent's prompt mandates an exhaustive scan for
  emails + account/customer IDs in the draft body; any hit blocks
  approval (cardinal rule: non-empty pii_concerns ⇒ approved=false)
- **Policy-word probe** — discount / refund / free upgrade / roadmap
  commit / guarantee in the body blocks approval
- **Hard-escalation keywords force-bump priority** — legal / regulator
  / GDPR / breach / harm / etc.
- **Cancel intent skips agent draft** — routes directly to queue
  manager (retention path)
- **Voice fail-closed** — a call with no transcript (failed STT) always
  routes `requires_human_now=true`; urgency is scored strictly on a
  0..1 scale so it can't blow past routing thresholds
- **Approval ≠ delivery, fail-closed send** — tier-1 approves, then
  `reply_dispatcher_agent` actually sends (channel chosen at runtime,
  in-thread, batched when async). It re-checks the floor in deterministic
  Python: never dispatch a reply that wasn't approved or that still has
  unresolved PII/policy — blocked dispatches record nothing and route to
  audit as a non-send
- **HITL fallback chain** — tier_1 → tier_2 → sme → queue_manager → halt
- **Enterprise state stays enterprise** — CSAT/SLA/identity are read
  from Zendesk/Intercom + Salesforce/Segment via n8n connectors; the
  template keeps no shadow SQLite ledgers

## Quick start

```bash
cp env .env
pip install -r requirements.txt
docker run -d -p 6379:6379 redis:7-alpine
python main.py
```
