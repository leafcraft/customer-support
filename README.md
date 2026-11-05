# customer-support — Customer Support

Multi-channel reactive ticket triage. Realises the **supp × saas** ROI
cell. Peer-to-peer team — intake owns EVERY channel (voice included):
it normalises AND triages in one stage, fanning out to sentiment in
parallel, and routes voice calls human/callback/self-serve;
KB-grounded responses (retrieval + draft in one agent); QA gate before
customer sees anything; channel-native HITL.

**YAML-first**: `configs/config.yaml` IS the support desk (it seeds the
agency's stored configuration on the first start; after that you change it in
Studio — see *Changing the configuration after the first start*). Every
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

## The team (17 agents)

| Agent | Role | Responsibilities | Can perform |
|---|---|---|---|
| `case_coordinator_agent` | Case coordinator (single point of contact) | Own each ticket end-to-end: open the case, record arrival channel + time, orchestrate the pod, keep live status, answer "where is my ticket?" | LLM; listens on the `support_events` stream (once enabled — see Configuration); opens cases → intake; routes exceptions → queue_manager, security/legal → SME (never triages/drafts/sends itself) |
| `ticket_intake_agent` | Front door for every channel (incl. voice) | Normalise any channel payload → canonical Ticket; classify intent / priority / specialist; scan hard-escalation keywords; route voice calls (human / callback / self-serve; fail-closed on no transcript) | LLM (intent_taxonomy KB); fans out to sentiment; routes to response / queue_manager (escalation or cancel) / tier-1 (voice) |
| `sentiment_agent` | Emotional read | Score frustration / urgency / churn_risk / polite; cite the phrases that drove each score | LLM; runs in parallel with triage; no routing of its own (its scores stay in the session — it has no edge to response) |
| `response_agent` | Knowledge-grounded answer + draft | Retrieve the answer from the KB (cite sources; no source ⇒ confidence ≤ 0.4); draft the tone-aware reply in one pass | LLM (support_kb RAG); fan-in waits for intake and for sentiment to finish; routes to QA (conf ≥ 0.7) or tier-2 (< 0.7) |
| `qa_agent` | Pre-send safety gate | Check accuracy vs cited sources + tone; exhaustive PII sweep; policy-word probe; block on any PII/policy hit; bounded QA-rework back-edge to response (exhaustion → queue_manager) | LLM; routes approved or flagged drafts to tier-1 |
| `backlog_triage_agent` | Bulk triage drainer | Drain the queued-ticket backlog: N parallel copies each atomically CLAIM a disjoint batch (never duplicate work), triage each, escalate the worthy ones | LLM (`scaling.copies.count: 3`, atomic `claim_ticket_batch`); scheduled sweep in the business timezone; routes escalations → queue_manager, drain counts → audit |
| `tier_1_agent_human` | Tier-1 support agent | Review + approve the reply (30-minute window) | Human (Inbox); approve / send / edit_and_send → reply_dispatcher; hold or any other reply → audit; no answer in 30 min → card closes, nothing sent |
| `reply_dispatcher_agent` | Egress actuator | SEND the approved reply on the channel it arrived on (in-thread; batched when async/email), log it to the ticket; fail-closed on unapproved / unresolved PII | Programmatic (n8n via vault connection `comms_dispatch_n8n`; Slack/email/CPaaS), dev-store outbox fallback; terminus → audit |
| `tier_2_specialist_human` | Tier-2 specialist | Billing / security / account depth when the KB had no confident answer (2-hour window) | Human; approve / edit_and_send → reply_dispatcher; hold or any other reply → audit; no answer in 2h → card closes, nothing sent |
| `sme_human` | Tier-3 SME | Engineering / security / legal matters the coordinator routes (4-hour window) | Human; every reply → audit; no answer in 4h → card closes |
| `queue_manager_human` | Queue manager | Own escalations, cancel-intent retention, QA rework limit, thread merges, KB article review, the legal/breach gate; receives the Manager's error alerts (1-hour window) | Human; every reply → audit; no answer in 1h → card closes |
| `support_audit_logger_agent` | WORM audit | Hash-chain every reply sent + every escalation | External (custom sink); terminus |
| `sla_watchdog_agent` | SLA monitor | Find tickets past SLA in your support desk | Programmatic (n8n connector); cron when wired |
| `kb_gap_filler_agent` | Knowledge-debt closer | Draft the missing KB article from the resolution path | LLM (kb_style_guide KB); routes draft to queue_manager review |
| `support_insights_agent` | Insights analyst | CSAT/NPS rollups + trending/spiking ticket clusters | Programmatic (n8n); modes csat_ingest / cluster_ingest / report; spike → kb_gap_filler |
| `cross_channel_agent` | Identity unifier | Resolve the customer across channels; flag thread merges | Programmatic (n8n, CRM identity); routes to response or queue_manager |
| `crm_case_generator_agent` | Case writer | Draft the categorised, push-ready CRM case at resolution | LLM (crm_taxonomy KB); routes to insights |

`backlog_triage_agent` runs a fixed 3 copies (`scaling.copies: {mode: fixed, count: 3, handoff: last}`) that split the backlog through `claim_ticket_batch`. leafmesh 2.4.484's `copies.mode: auto` with `claim_tool:` could replace that manual split; that is an owner decision and is not done here.

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
- **Silence never sends** — on leafmesh 2.4.486, a review nobody answers in
  time closes its Inbox card and the ticket records that no reply came.
  Nothing is sent, and nobody is chased automatically: there is no
  tier-1 → tier-2 hand-off on silence. Which tier a ticket reaches is decided
  up front by the routing (confidence, keywords, intent). Each seat still
  declares `fallback_response: held_for_review` (→ audit) as the intended
  timeout answer. The Manager raises only errors (hard errors, repeated agent
  failures) to the queue manager.
- **Enterprise state stays enterprise** — CSAT/SLA/identity are read
  from Zendesk/Intercom + Salesforce/Segment via n8n connectors; the
  template keeps no shadow SQLite ledgers

## Configuration — what to set, in three tiers

Everything is set in `.env` (`cp env .env`). The **whole LLM chain (intake →
sentiment → response → QA) runs on Tier 1 alone** against the seed KB / backlog
store (`./data`); KB reads, the egress send, and the CSAT / SLA / cross-channel
connectors use dev stand-ins that **announce themselves** until you wire Tiers 2–3.

### Tier 1 — required to boot (dev mode)
| Setting | For | Notes |
|---|---|---|
| `LEAFMESH_LICENSE_KEY` | LeafMesh runtime | **Required.** |
| `OPENAI_API_KEY` | Coordinator + intake + QA + backlog + Manager (`gpt-4o-mini`) | **Required.** |
| `ANTHROPIC_API_KEY` | response_agent (Claude) | **Required** unless you change the model. |
| `REDIS_HOST` / `PORT` / `PASSWORD` | State store | Defaults to `localhost:6379`. `docker run -d -p 6379:6379 redis:7-alpine`. |
| *(vault connection `support_events`)* | Async inbound stream the Coordinator listens on | Off on day 0: create the connection (its URL), then uncomment the broker and `listen_events` — in `configs/config.yaml` before the first start, in Studio's YAML editor after it (see *Changing the configuration after the first start*). |

### Tier 2 — real connectors (vault connections; inert until wired)
Addresses and tokens live **only in the vault** — a URL or token written in
`configs/config.yaml` is refused at load. Create each connection, then
uncomment the block that names it — in `configs/config.yaml` before the first
start, in Studio's YAML editor (or `PUT /api/yaml/raw`) after it; it applies
without a restart:

| Vault connection | Enables |
|---|---|
| `docs_mcp` (Infrastructure → Integrations → MCP: the server's URL + token) | Real docs/KB retrieval for response_agent (its `mcp` block) |
| `comms_dispatch_n8n` (Infrastructure → Integrations → n8n, or an "Add custom" record with service type Tools: base URL = the workflow's webhook URL) | The egress send — Slack postMessage / email gateway / CPaaS (Twilio/EmailBison). Then uncomment `integration` + `connector_config` on `reply_dispatcher_agent` (they ship commented: an n8n agent with no address fails every call and is quarantined after 5) |
| `support_insights_n8n` (same shape) | CSAT/NPS rollups + trending/spiking clusters |
| `sla_watchdog_n8n` (same shape) | Tickets-past-SLA scan |
| `cross_channel_n8n` (same shape) | Cross-channel identity resolution |

> Create the connection **before** you uncomment its block: an agent whose
> named connection does not exist is switched off (with the reason) and works
> as soon as the connection exists — no restart.
> The day-0 `search_kb` tool checks whether the `docs_mcp` record exists, only
> to word an empty KB answer. Nothing in `.env` switches it.

### Manager escalation surfaces (optional)
| Setting | For | Notes |
|---|---|---|
| *(vault connection `escalation_webhook`)* | PagerDuty-style webhook target | Create it (an "Add custom" record on the Infrastructure page, service type 'Outbound webhook'), then uncomment the webhook target in `manager.escalation`. |
| `SLACK_SUPPORT_CHANNEL` + a Slack adapter | Slack escalation target | Shipped commented. Posting needs a Slack channel adapter: connect Slack on a human seat (a vault Slack record — Infrastructure → Messaging → Slack), set the channel id in `.env`, then uncomment the target. |

### Tier 3 — CRM push at resolution (later)
`crm_case_generator_agent` drafts the push-ready CRM case; wire it to
Salesforce / HubSpot / Zendesk when you are ready to write cases back.

## Run it

```bash
cp env .env
pip install -r requirements.txt          # core LeafMesh runs the pod day-0
docker run -d -p 6379:6379 redis:7-alpine
python main.py
```

## Changing the configuration after the first start

`configs/config.yaml` is only the first-start seed. On the first start (an empty store) the agency imports it as version 1 and from then on keeps its configuration in the store. After that the file is not read again: an edit to it is **not** applied, even after a restart (the boot log says "that edit was NOT applied").

To change a running agency:

- **Studio** — its settings pages, or the raw YAML editor.
- **API** — `PUT /api/yaml/raw` with an `X-API-Key` header and the body `{"yaml_content": "<the whole YAML>", "save_to_disk": true}`, or `PATCH /api/yaml/config` with `{"updates": {...}, "save_to_disk": true}`.

Each save is a new version and applies live on every pod — no restart. (A few settings, such as brokers, make the agency reload itself in place within seconds.) So wherever this README or a comment says "uncomment X in `configs/config.yaml`", do it in the file before the first start, or in Studio's YAML editor after it.

## Changing a running project (Docker)

What reaches a project that is already running (leafmesh 2.4.497). The
image holds the Python code and the first-start `configs/config.yaml`;
`.env` is passed in when the container is created; the store (Redis) keeps
its data on its own volume.

| You change | When it applies |
|---|---|
| An agent in Studio (prompt, model, tools, routes, human seats, schedules, …) | At once, no restart |
| A human seat's `channels` (Slack, email, …) | The agency reloads itself in place within seconds |
| Add or delete an agent in Studio | At once, including routes and schedules |
| A connection in the vault (model keys, MCP, webhooks, connectors, Slack, email) | At once. An agent switched off for a missing connection or address switches back on by itself |
| Anything in `.env` (licence, agency name, store, ports) | After `docker compose up -d` (the container is recreated) |
| Python code (`agency/*.py`: programmatic agents, tools, `@pre_compose`) | After a rebuild: `docker compose up -d --build` |
| `configs/config.yaml` | Never after the first start. It is read only when the store is empty |
| Several app containers on one store | A Studio save reaches every one within about 15 seconds |

- Keep model keys in the vault. A key there wins over the same key in
  `.env` (the log warns), and a change applies at once. From 2.4.497 that
  holds for every value the vault holds (endpoints, organisation / project
  ids, cloud identities); the `.env` value returns when the record goes.
- Code the image does not have: an agent marked pure Python
  (`integration: python`) is switched off until its function is in the image;
  any other programmatic agent stays on, and each call to code it does not
  have fails. Rebuild the image with the code first.
- A new Redis volume is an empty store, so `configs/config.yaml` is imported
  again as version 1.
- **If the agency refuses to start after an upgrade** (a new leafmesh pin,
  then a rebuild: "Configuration has N error(s) in the stored
  configuration"), repair the stored configuration with the agency stopped:

  ```bash
  docker compose run --rm customer-support leafmesh config check      # each problem, with its line
  docker compose run --rm customer-support leafmesh config versions   # kept versions: which pass, which is in use
  docker compose run --rm customer-support leafmesh config rollback   # back to the newest version that passes (saved as a new version)
  ```

  Or `leafmesh config export > config.yaml`, fix it by hand, then
  `leafmesh config import config.yaml` (it is checked before it is saved).

## Where the data store is set

From leafmesh 2.4.482 the store (Redis or Postgres) is set **only in `.env`** — never in `configs/config.yaml`. A `persistence:` block in the YAML is ignored with a warning on leafmesh 2.4.482–2.4.485; from 2.4.486 the agency refuses to start if that block differs from `.env`, and names each `.env` line to add. The project's `.env` sets the store explicitly (`LEAFMESH_STORE`, `LEAFMESH_AGENCY_NAME`, `REDIS_*`).

- Redis (default): `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB`; optional `REDIS_DEFAULT_TTL`, `REDIS_SESSION_TTL`, `REDIS_SSL*`, `REDIS_CLUSTER_NODES`.
- Postgres instead: `LEAFMESH_STORE=postgres` plus `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (optional `POSTGRES_SSLMODE`).
- Or the whole server in one value: `REDIS_URL=redis://user:pw@host:6379/0` (`rediss://` turns TLS on) or `DATABASE_URL=postgresql://user:pw@host:5432/db`. The single names fill in what the URL leaves out.
- With Docker Compose the app reaches Redis as `redis` (the service name); compose sets that for you.
- Check it in Studio → **Infrastructure → Datastores**: it shows whether the store is reachable (never the address or password).

## This mesh's sign-in (leafmesh 2.4.497)

From leafmesh 2.4.497 each mesh signs in to LeafCraft with its own key, not
a key shared by every install.

- **First start.** The mesh makes a key pair and registers it with
  `LEAFMESH_LICENSE_KEY` and `LEAFMESH_ENV_TOKEN` from `.env` (copy the
  environment token from Studio — a placeholder such as `development` is
  refused). With both valid it works at once. If your organisation turned on
  "require approval", the first start stops and says so: approve it in
  Studio → **Keys → SDK access**, then start again. Organisation admins get an
  email for every new registration.
- **Where the key lives.** In the vault, in the store, encrypted with
  `LEAFMESH_VAULT_KEY`: it survives rebuilds and every copy of the app shares
  it. A new Redis volume, or a new vault key, makes a new key pair that
  registers again — revoke the old one in Studio.
- **Check or change it** (in Docker: `docker compose run --rm customer-support leafmesh auth status`):
  - `leafmesh auth status` — credential id, key fingerprint, registration and
    mode, never a key; `--check` signs in now and reports the tokens.
  - `leafmesh auth register` — register now.
  - `leafmesh auth rotate` — a new key pair: once it is active the mesh
    switches to it and revokes the old one. A revoked or expired key is never
    revived; rotate instead.
- **Lost the environment token?** From 5 Dec 2026 Studio shows the licence
  key and environment token only once, when they are made (then only a hint).
  Rotate the environment token in Studio and put the new one in `.env`.
  Rotating it changes where the agency's data is kept in the store, so do it
  before the agency holds data.
- **An existing project whose `.env` says `LEAFMESH_ENV_TOKEN=development`.**
  2.4.497 needs the real token. The token also names where the agency's data
  sits in the store, so with the real token the agency starts on an empty
  store (vault, sessions, board) — the old data stays under the old name. Set
  the real token before you put data in the agency, or contact us to move it.

## Your vault key — back it up

`create-leafmesh` put a `LEAFMESH_VAULT_KEY` in your `.env` when it made this project. It is the master key that encrypts every connection and credential you store in the vault (in Redis / Postgres).

- **Back it up like a master password.** Lose it and every stored connection has to be entered again.
- **Never change or regenerate it** for an existing project — a new key locks the existing vault. To change it, use `leafmesh vault rotate-key`.
- Every copy of the app (every replica) must get the **same** value. Docker Compose passes it in from `.env` as an environment variable.
- Never commit `.env` (it is already in `.gitignore`).
