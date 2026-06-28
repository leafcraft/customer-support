"""Dev-mode outbox for the customer-support egress.

The mesh DRAFTS a reply (response_agent), CLEARS it (qa_agent) and a human
APPROVES it (tier_1_agent_human) — but approval is not delivery. The
reply_dispatcher_agent is the actuator that actually SENDS the approved
reply on the channel the ticket arrived on, in-thread, and records the
send so the chain is verifiable on day 0 — before COMMS_DISPATCH_WEBHOOK_URL
is wired.

This is a dev stand-in, NOT the system of record. The real outbox lives in
your support desk / comms platform (Zendesk replies, Slack postMessage,
the email gateway, a CPaaS like Twilio/EmailBison that batch-sends). In
production the dispatcher posts to that platform via the connector; this
store only records what was sent so the demo produces a real artifact.

Single-process JSON file under ./data/. Swap for Redis/Postgres for a
multi-replica deployment — the function signatures are the contract.
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


def _load(kind: str) -> dict[str, Any]:
    p = _path(kind)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _save(kind: str, data: dict[str, Any]) -> None:
    p = _path(kind)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


# ── Outbox — recorded sends (the verifiable day-0 artifact) ──────────
def record_send(send: dict[str, Any]) -> dict[str, Any]:
    """Persist one dispatched reply. Returns the stored record (with a
    minted dispatch_id + recorded_at). Idempotent on dispatch_id."""
    with _LOCK:
        outbox = _load("outbox")
        did = send.get("dispatch_id") or _new_id("disp")
        send["dispatch_id"] = did
        send.setdefault("recorded_at_utc", _now())
        outbox[did] = {**outbox.get(did, {}), **send}
        _save("outbox", outbox)
        return outbox[did]


def get_send(dispatch_id: str) -> dict[str, Any]:
    return _load("outbox").get(dispatch_id, {})


def sends_for_ticket(ticket_id: str) -> list[dict[str, Any]]:
    if not ticket_id:
        return []
    return [r for r in _load("outbox").values()
            if isinstance(r, dict) and r.get("ticket_id") == ticket_id]


new_id = _new_id
now = _now
