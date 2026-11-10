# Customer Support — Product Requirements / Architecture Doc

> ⚠️ **STATUS — PROPOSED v2 DESIGN (not yet built).** The *shipped* template
> is the working **17-agent** customer-support desk (see `README.md` + the
> agent table there). This document is the **forward design** for a v2
> live-conversation rebuild (one generic conversation agent + register +
> bulk-comms egress, 16 agents). It is a spec/roadmap, **not** what the
> current configuration contains. Build it in a later release.

> A reactive support desk where the customer can arrive **any way** — a
> phone call, a live chat, a WhatsApp, an email, a web form — and the
> operation talks to them while they're live and reaches back out in bulk
> after. The engine is **one generic conversation agent** that handles every
> *live* channel (voice and chat), and a disciplined pipeline behind it:
> *converse → on end, fill the form → register in the system of record →
> resolve → communicate back (in bulk, via an external comms platform)*.
>
> **The firm rule:** the customer is **never** a mesh member, and **no mesh
> agent ever talks to the customer.** Every contact — in and out — is owned
> by an **external system**. Mesh agents only ever talk to *other agents* or
> to *external systems*. Read this before the config.

---

## 1. The boundary — external systems own all customer contact

```
   CUSTOMER                EXTERNAL SYSTEMS                 │  THE MESH (data + systems only)
   ────────   ◄──talks──►  ─────────────────               │  ────────────────────────────
   voice                   LiveKit            ── mesh call ─┼─► conversation_agent  (LLM)
   live chat               chat platform      ── mesh call ─┼─►   streams reply back to the system
                                              ◄── stream ───┼───  (multi-turn)
   (we dial out)           Twilio             ◄── dial ─────┼───  @pre_compose (outbound)
                                                            │        │ @chain: conversation ended?
                                                            │        ▼  LLM fills the FORM (same session)
                                                            │   register_agent → system of record
                                                            │        ▼
                                                            │   response → QA → human tier
   async / bulk            Instantly /        ◄── upload ───┼───  bulk_comms_agent  (programmatic)
                           EmailBison / CPaaS │   (sends to the customer in bulk
                              │  SENDS         │    once a data threshold is met)
                              ▼                │
   customer's inbox/phone                      │
```

**Mesh agents talk to LiveKit / chat platforms / Twilio / Instantly — not to
the customer.** The customer lives entirely on the far side of those systems.
Continuity across discrete contacts is carried by a **`conversation_id`**,
never an open connection (§5).

---

## 2. The conversation agent — one generic LLM, every live channel, both directions

This is the heart. **One agent**, company-agnostic, handles all *live*
conversation — voice (LiveKit) and chat (chat platform) alike, because in
both cases an external system makes a mesh call and gets a streamed reply.

**Inbound (voice or chat).**
LiveKit / the chat platform makes a **mesh call** with the latest turn →
the agent **streams its reply back to that system** → multi-turn, the system
relays to the customer. The agent answers grounded in the KB; it is
responding to the *external system*, not the person.

**Outbound (we initiate a call).**
The same agent `listen_events` on a **wakeup event**. On wake, its
**`@pre_compose`** places the **Twilio** outbound call **and injects the
call context** into the LLM so it's primed to talk the moment the customer
answers — **same LLM, same session** as inbound. From there it's identical.

**On conversation end (`@chain`, same session).**
When the agent judges the conversation has ended, its `@chain` runs an LLM
pass over the **full history**, **fills the canonical Form** (intent,
priority, identity, the fields downstream needs), and forwards it to the
next relevant agent. The advance edge is gated on the form being complete —
nothing moves on a half-filled form.

**Generic by construction:** specialised only by its **KB group** (what it
knows), its **channel connector** (LiveKit/chat/Twilio — how it's reached),
its **form schema** (what it must collect), and its **prompt parts** (tone,
guardrails, escalation keywords). Point those at any company's systems and
it works — no code change. Voice and chat are the *same* agent; only the
external system on the other end differs.

---

## 3. Register — push the form into the system of record

Once the form is complete, **`register_agent`** pushes it into the system of
record (ServiceNow / Zendesk / Salesforce) and returns the external ticket
id. The ticket becomes real in your systems before anything tries to resolve
it — downstream always has its id. State never lives in template storage.

---

## 4. Async / bulk outreach — the programmatic comms agent

Any contact that is **not** a live call/chat — a follow-up after the call, a
resolution notice, a nudge — does **not** go through an LLM talking. It goes
through **`bulk_comms_agent`**, a **programmatic** agent that **uploads the
message + data to an external comms platform** (Instantly / EmailBison /
CPaaS), and that platform sends to the customer directly.

**The clever part — batching.** The platform doesn't fire one-at-a-time; it
holds until a **data threshold** is met and then sends in **bulk**. The
agent is either **called** at the right moment or **`listen_events`** on a
trigger — so outreach goes out efficiently, in batches, not per message.

So there are two egress modes, by nature of the contact:
- **Live** → the conversation agent streams through LiveKit/Twilio/chat (real-time, an LLM talks to the *system*).
- **Async/bulk** → `bulk_comms_agent` uploads to Instantly/EmailBison (batched, the platform sends).

---

## 5. Re-entry — "how does it run when he comes back?"

Not mesh-to-mesh. **One mesh with async edges, stitched by a correlation
id.** The customer replies later (maybe a different channel) → the external
system makes a **fresh mesh call** carrying the same `conversation_id` → the
mesh resolves it to the **open ticket** (Redis session, or
`cross_channel_agent` for a different device/channel) and continues.
**Continuity lives in the id + the ticket store, never a held connection.**

---

## 6. Resolution & the human tiers

For written/async tickets and for drafting outbound content,
**`response_agent`** retrieves the KB answer (cites sources; no-source caps
confidence) and drafts; **`qa_agent`** clears it (PII sweep + policy probe +
tone) — a hit blocks. Low confidence / sensitive / legal routes to the right
human: **tier-1 → tier-2 → SME**, with **queue_manager** owning escalations,
cancel-intent, SLA, and the legal/breach gate. Humans approve; delivery is
the conversation agent (live) or `bulk_comms_agent` (async) — **approval ≠
delivery**.

---

## 7. Connector matrix — three external surfaces, never conflated

| Surface | Direction | Systems | Mesh side |
|---|---|---|---|
| **Live conversation** | in + out | LiveKit (voice), chat platform, Twilio (outbound dial) | `conversation_agent` (mesh call in, stream out; `@pre_compose` dial) |
| **System of record** | out | ServiceNow / Zendesk / Salesforce | `register_agent` (push) |
| **Bulk comms** | out | Instantly / EmailBison / CPaaS | `bulk_comms_agent` (upload; platform batches + sends) |

Supporting reads (CSAT, SLA, identity) come from your support desk/CRM via
the existing n8n connector agents.

---

## 8. Actor inventory (16) — every agent is a job role with responsibilities

Each agent reads like a person on the team — a clear title, a set of
responsibilities, and the things it can actually do. None are thin nodes.

| Agent | Role (job title) | Responsibilities | Can perform |
|---|---|---|---|
| `conversation_agent` | **Frontline support rep — live voice & chat** | Hold the live conversation through the external system (answer each turn, grounded in the KB); place outbound calls when asked; when the conversation ends, write it up into the case Form and hand it on | LLM (KB-grounded) + `@pre_compose` (Twilio outbound dial + context inject) + `@chain` (form-fill + completeness gate); `listen_events` wakeup; streams to LiveKit/chat; routes → register / human |
| `sentiment_agent` | **Customer-sentiment analyst** | Read frustration / urgency / churn risk off the message and cite the phrases that drove the read; advise the rep | LLM; parallel co-input (no routing of its own) |
| `register_agent` | **Records registrar** | File the completed case Form into the system of record and return its official ticket id | Connector (n8n/MCP — ServiceNow/Zendesk/Salesforce); gated on `form_complete`; routes → response |
| `response_agent` | **Knowledge specialist — written support** | Find the grounded answer in the KB (cite sources; flag when there is no good source), draft the written reply / outbound content | LLM (support_kb RAG); routes → QA (conf ≥ 0.7) / tier-2 (< 0.7) |
| `qa_agent` | **Quality & compliance reviewer** | Check every draft for accuracy, tone, PII and policy before it leaves; block anything unsafe | LLM; routes approved/flagged → tier-1 |
| `reply_dispatcher_agent` (shipped) | **Egress actuator / outbound comms coordinator** | After tier-1 approves, actually SEND the reply on the channel it arrived on (in-thread; batched when the session is async/closed); log it to the ticket | Programmatic (n8n via vault connection `comms_dispatch_n8n` — Slack/email/CPaaS), dev-store outbox fallback; deterministic fail-closed floor (no send without approval + clean PII); terminus → audit |
| `tier_1_agent_human` | **Tier-1 support agent** | Review and send replies | Human (Inbox); approve → dispatcher; hold → audit; no answer in 30 min → card closes, nothing sent |
| `tier_2_specialist_human` | **Tier-2 specialist** | Resolve billing / security / account depth | Human; approve → dispatcher; hold → audit; no answer in 2h → card closes |
| `sme_human` | **Subject-matter expert (Tier-3)** | Own engineering / security / legal escalations | Human; → audit; no answer in 4h → card closes |
| `queue_manager_human` | **Support queue manager** | Own escalations, cancellations, SLA breaches and the legal/breach gate | Human; → audit; no answer in 1h → card closes |
| `crm_case_generator_agent` | **Case documenter** | Write up the categorised, push-ready case record at resolution | LLM (crm_taxonomy); feeds register / insights |
| `kb_gap_filler_agent` | **Knowledge-base author** | Turn an unanswered ticket into the article that should have existed | LLM (kb_style_guide); routes draft → queue_manager review |
| `support_insights_agent` | **Support analyst** | Report CSAT/NPS trends and spiking ticket clusters from your analytics | Connector (n8n); modes csat_ingest / cluster_ingest / report; spike → kb_gap_filler |
| `cross_channel_agent` | **Customer-identity resolver** | Stitch a customer's threads across channels into one history | Connector (n8n, CRM identity); routes → response / queue_manager |
| `sla_watchdog_agent` | **SLA monitor** | Watch the support desk for tickets about to breach SLA | Connector (n8n); cron when wired |
| `support_audit_logger_agent` | **Compliance recorder** | Keep the tamper-evident (hash-chained) record of every send + escalation | External (custom sink); terminus |

> **New vs the current 14:** `conversation_agent` (the live voice+chat rep,
> evolving `ticket_intake_agent`), `register_agent`, `bulk_comms_agent`.

---

## 9. The flow

```
 LIVE channel (voice/chat)                          OUTBOUND (we initiate)
   external system ── mesh call ──►┐                wakeup event ─► conversation_agent
                                   ▼                                  @pre_compose: Twilio dial
   conversation_agent  ◄─ stream ─► external system                  + inject context (same session)
        │  (multi-turn; talks to the SYSTEM, never the customer)
        │  @chain: conversation ended → LLM fills FORM (same session)
        │     └─ form_complete? ── no → ask (bounded) / human
        ▼ yes
   register_agent ──► system of record (ServiceNow/Zendesk)
        ▼
   response_agent ─┬─ ≥0.7 → qa_agent ─┬─ clean / flagged → tier_1_agent_human → approve
                   └─ <0.7 → tier_2     └─ (PII/policy hit blocks)
   escalation keyword / cancel ─► queue_manager_human
        │
   approve → reply_dispatcher_agent → SENDS on the arrival channel
             (in-thread; async/email → batched; live → streamed back)
        ▼
   crm_case_generator → register_agent ;  KB gap → kb_gap_filler
        ▼
   support_audit_logger_agent  (WORM, terminus)
```

---

## 10. Controls / trust
- **Form-completeness gate** — nothing registers/resolves on a half-filled form.
- **KB-grounded** answers; no-source ⇒ confidence capped ⇒ escalate.
- **QA gate** before any outbound — PII/policy hit ⇒ blocked.
- **Hard-escalation human gate** — legal/breach/GDPR/harm never reach the bot (edge-enforced).
- **Bulk threshold** — async outreach batches; no accidental per-message spray.
- **Nothing is sent on silence** (a review nobody answers closes its Inbox card;
  nobody is chased automatically); **WORM audit** of every send + escalation.

---

## 11. Reflection / decisions
- **Customer is never a mesh member, and no mesh agent talks to the customer.** Agents talk only to other agents or to external systems (LiveKit, chat, Twilio, Instantly/EmailBison). The customer is always behind an external system.
- **One generic conversation agent for all live channels, both directions** — voice and chat are the same agent (the external system differs); outbound is the *same* session, entered via `@pre_compose` Twilio dial on a wakeup event.
- **`@chain` form-fill at conversation end** + completeness gate is the spine — *this needs Python*, which **retires the template's former YAML-only invariant** (`test_no_per_agent_modules_remain` must change). Deliberate.
- **Register before resolve** — the ticket is real in the system of record before resolution runs.
- **Two egress modes by nature of contact** — live (conversation agent streams via LiveKit/Twilio/chat) vs async **bulk** (programmatic upload to Instantly/EmailBison; the platform batches + sends). Approval ≠ delivery.
- **Continuity = `conversation_id`**, not a held connection.
