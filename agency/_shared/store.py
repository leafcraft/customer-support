"""Dev store — a working stand-in for the systems of record, so the pod runs
day-0 with NO connector wired.

This is a DEV STAND-IN, NOT a system of record. In production:
  - the KB reads (search_kb / read_past_tickets) come from your docs + RAG or a
    docs MCP server (the commented mcp block on response_agent),
  - the backlog queue is your support desk's open-ticket queue (Zendesk views),
  - the outbox is your comms platform (Slack postMessage, the email gateway, a
    CPaaS that batch-sends).

**The function signatures are the contract you swap real systems into** — the
mesh does not change. Every read ANNOUNCES ITSELF: when there is no real source
and no seed, it returns `available: false` and says why — never a synthetic
success that looks like a real read (doctrine A4/A5). Nothing here is a system of
record; it holds seed data (so the pod demonstrates end-to-end) and appends the
pod's own outbox under ./data.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_DIR = Path("./data")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _path(kind: str) -> Path:
    _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR / f"{kind}.json"


def _load(kind: str, default: Any) -> Any:
    p = _path(kind)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


def _save(kind: str, data: Any) -> None:
    p = _path(kind)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


# ── Seed KB — a handful of articles so response_agent has something to cite
#    day-0. Replace with real RAG / a docs MCP server (see config.yaml). ──
SEED_KB: list[dict[str, Any]] = [
    {"id": "kb-101", "title": "Reset your password",
     "section": "account", "url": "https://docs.example.com/kb-101",
     "keywords": ["password", "reset", "locked out", "login", "sign in"],
     "body": "Go to Settings > Security > Reset password. A reset link is "
             "emailed to the address on file; it expires in 60 minutes."},
    {"id": "kb-204", "title": "Refund policy and eligibility",
     "section": "billing", "url": "https://docs.example.com/kb-204",
     "keywords": ["refund", "money back", "cancel charge", "billing"],
     "body": "Refunds are available within 30 days of purchase for annual "
             "plans, prorated. Monthly plans are non-refundable but can be "
             "cancelled to stop future charges. Goodwill credits require a "
             "manager's approval."},
    {"id": "kb-310", "title": "Fix a failed payment",
     "section": "billing", "url": "https://docs.example.com/kb-310",
     "keywords": ["payment failed", "card declined", "invoice", "charge"],
     "body": "Update the card under Billing > Payment methods, then click "
             "Retry payment on the failed invoice. Most declines clear on retry."},
    {"id": "kb-415", "title": "Export your data",
     "section": "product", "url": "https://docs.example.com/kb-415",
     "keywords": ["export", "download data", "csv", "backup"],
     "body": "Admins can export from Workspace > Data > Export. The archive is "
             "emailed as a link that expires in 24 hours."},
]

# ── Seed past tickets — prior resolved tickets so response_agent can ground on
#    precedent, and so cross-channel history has something to show. ──
SEED_PAST_TICKETS: dict[str, list[dict[str, Any]]] = {
    "cust-1042": [
        {"ticket_id": "tkt-0001", "channel": "email", "intent": "account",
         "resolution": "Sent password-reset link (kb-101); customer confirmed access.",
         "resolved_at": "2026-07-30T10:14:00Z"},
        {"ticket_id": "tkt-0007", "channel": "chat", "intent": "billing",
         "resolution": "Retried failed payment after card update (kb-310).",
         "resolved_at": "2026-08-11T09:02:00Z"},
    ],
    "cust-2277": [
        {"ticket_id": "tkt-0019", "channel": "slack", "intent": "how_to",
         "resolution": "Walked through data export (kb-415).",
         "resolved_at": "2026-08-18T16:40:00Z"},
    ],
}

# ── Seed backlog — queued ticket ids the backlog_triage copies drain. The claim
#    is atomic so parallel copies (instances: N) get DISJOINT batches. ──
_SEED_BACKLOG: list[str] = [f"tkt-bk-{i:03d}" for i in range(1, 25)]


# ── KB reads (dev-store fallback for the RAG / docs-MCP path) ────────────────
def search_kb(query: str) -> dict[str, Any]:
    q = (query or "").strip().lower()
    if not q:
        return {"available": False, "reason": "empty query"}
    hits = []
    for art in SEED_KB:
        hay = " ".join([art["title"], art["body"], " ".join(art["keywords"])]).lower()
        if any(term and term in hay for term in q.split()):
            hits.append({k: art[k] for k in ("id", "title", "section", "url", "body")})
    if not hits:
        # Announce a genuine miss — a knowledge gap, not a fabricated answer.
        return {"available": True, "query": query, "hits": [],
                "reason": "no seed KB match; wire real RAG / a docs MCP for full coverage"}
    return {"available": True, "query": query, "hits": hits}


def read_past_tickets(customer_id: str) -> dict[str, Any]:
    cid = (customer_id or "").strip()
    if not cid:
        return {"available": False, "reason": "no customer_id"}
    prior = SEED_PAST_TICKETS.get(cid)
    if prior is None:
        return {"available": True, "customer_id": cid, "tickets": [],
                "reason": "no prior tickets for this customer in the seed store"}
    return {"available": True, "customer_id": cid, "tickets": prior}


# ── Backlog claim — atomic pop so parallel copies split the backlog ──────────
def claim_batch(count: int) -> dict[str, Any]:
    """Atomically claim up to `count` backlog ticket ids. Concurrency-safe:
    each caller (instance copy) gets a DISJOINT slice. Dev mirror of an
    `LPOP count` / `UPDATE ... SKIP LOCKED RETURNING` on the real queue."""
    try:
        n = max(0, int(count))
    except (TypeError, ValueError):
        n = 0
    with _LOCK:
        backlog = _load("backlog", None)
        if backlog is None:
            backlog = list(_SEED_BACKLOG)   # seed on first claim
        claimed = backlog[:n]
        remaining = backlog[n:]
        _save("backlog", remaining)
    return {"available": True, "claimed": claimed,
            "claimed_count": len(claimed), "remaining": len(remaining)}


def backlog_len() -> int:
    backlog = _load("backlog", None)
    return len(_SEED_BACKLOG) if backlog is None else len(backlog)


# ── Outbox — recorded sends (the verifiable day-0 artifact) ──────────────────
def record_send(send: dict[str, Any]) -> dict[str, Any]:
    """Persist one dispatched reply. Returns the stored record (with a minted
    dispatch_id + recorded_at). Idempotent on dispatch_id."""
    with _LOCK:
        outbox = _load("outbox", {})
        did = send.get("dispatch_id") or _new_id("disp")
        send["dispatch_id"] = did
        send.setdefault("recorded_at_utc", _now())
        outbox[did] = {**outbox.get(did, {}), **send}
        _save("outbox", outbox)
        return outbox[did]


def get_send(dispatch_id: str) -> dict[str, Any]:
    return _load("outbox", {}).get(dispatch_id, {})


def sends_for_ticket(ticket_id: str) -> list[dict[str, Any]]:
    if not ticket_id:
        return []
    return [r for r in _load("outbox", {}).values()
            if isinstance(r, dict) and r.get("ticket_id") == ticket_id]


new_id = _new_id
now = _now
