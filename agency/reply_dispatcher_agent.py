"""reply_dispatcher_agent — the EGRESS ACTUATOR / FINISHER.

The mesh DRAFTS (response_agent), CLEARS (qa_agent) and a human APPROVES
(tier_1_agent_human) a reply — but approval is NOT delivery. This is the role
that FINISHES the job: it actually SENDS the approved reply on the channel the
ticket arrived on (slack / email / whatsapp / ...), in-thread, and records the
send so the chain is verifiable day-0.

It is the concrete "@chain fails closed" (doctrine B4), two layers deep:

  a) approval RE-CHECK — reaching this agent does NOT mean approved. An
     allow-token allowlist + a block-token denylist; an UNKNOWN verdict fails
     closed. PLUS deterministic invariants a human can't waive: PII/policy
     concerns must be empty, a real reply body must exist.
  b) render the real artifact (the sent reply) to ./out; on a tripped floor,
     DELETE the partial and record `send_status: blocked` with reasons — never a
     half-sent reply.
  c) dev-store fallback that ANNOUNCES itself — no connector wired => records the
     send to ./data/outbox.json and says so; it never fakes a real ack.
  d) two-layer defense — the body short-circuits before writing; the @chain
     `enforce_send_floor` re-checks independently and deletes on a trip.

Connector: the n8n workflow behind the vault connection 'comms_dispatch_n8n'
(a comms dispatcher / CPaaS — Slack postMessage, your email gateway,
Twilio/EmailBison), picked in the configuration. Inert until wired.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger, chain

try:
    from ._shared import store
    from ._shared.session_stash import resolve_session_id, stash_get, stash_put
except ImportError:  # pragma: no cover — direct-run / test fallback
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore
    from _shared.session_stash import resolve_session_id, stash_get, stash_put  # type: ignore

logger = LeafMeshLogger(__name__)

_OUT_DIR = Path("./out")

# Tier decisions that AUTHORISE an actual send (allowlist).
_SEND_DECISIONS = frozenset({"sent", "send", "edit_and_send", "approve", "approved"})
# Explicit block tokens (denylist) — held / rejected / escalated.
_BLOCK_DECISIONS = frozenset({"held", "held_for_review", "hold", "reject", "rejected",
                              "no", "deny", "denied", "escalate", "escalate_to_tier_2",
                              "escalate_to_sme"})

# Channels with a live in-thread post API vs. async/batched egress.
_BATCHED_CHANNELS = frozenset({"email"})

_DEFAULTS: dict[str, Any] = {
    "dispatch_id": "",
    "ticket_id": "",
    "channel": "",
    "channel_thread_id": "",
    "delivery_mode": "",        # in_thread | batched | blocked
    "sent": False,
    "send_status": "",          # recorded | connector_sent | blocked
    "dispatched_at_utc": "",
    "dispatch_reason": "",
    "artifact_path": "",
    "block_reasons": [],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pick(input_data: dict[str, Any], key: str, default: Any = "") -> Any:
    """Shape-tolerant read: input_data, then upstream_yields (flat or keyed by
    agent), then human_data."""
    if not isinstance(input_data, dict):
        return default
    if input_data.get(key) not in (None, "", [], {}):
        return input_data[key]
    upstream = (input_data.get("upstream_yields")
                or input_data.get("upstream_results") or {})
    if isinstance(upstream, dict):
        if upstream.get(key) not in (None, "", [], {}):
            return upstream[key]
        for v in upstream.values():
            if isinstance(v, dict) and v.get(key) not in (None, "", [], {}):
                return v[key]
    human = input_data.get("human_data") or {}
    if isinstance(human, dict) and human.get(key) not in (None, "", [], {}):
        return human[key]
    return default


def _decision(input_data: dict[str, Any]) -> str:
    """The human verdict, from wherever the SDK delivered it."""
    return str(
        input_data.get("human_message")
        or _pick(input_data, "decision", "")
    ).strip().lower()


def _block_reasons(input_data: dict[str, Any]) -> list[str]:
    """Every deterministic reason this send must NOT go out. Empty = clear.
    Fail closed: an unknown decision is a reason, not a pass."""
    reasons: list[str] = []
    decision = _decision(input_data)
    if decision in _BLOCK_DECISIONS:
        reasons.append(f"not approved (decision: {decision or 'none'})")
    elif decision not in _SEND_DECISIONS:
        reasons.append(f"unknown decision '{decision or 'none'}' — fail closed")

    pii = _pick(input_data, "pii_concerns", [])
    if isinstance(pii, list) and pii:
        reasons.append(f"unresolved PII concerns: {len(pii)}")
    policy = _pick(input_data, "policy_concerns", [])
    if isinstance(policy, list) and policy:
        reasons.append(f"unresolved policy concerns: {len(policy)}")
    # QA's explicit approval flag, when present, must also hold.
    if _pick(input_data, "approved", None) is False:
        reasons.append("qa did not approve")
    if not str(_pick(input_data, "reply_body", "")).strip():
        reasons.append("no reply body to send")
    # edit_and_send means "send MY text": without it, nothing goes out (the
    # draft the person meant to correct is never sent in its place).
    if decision == "edit_and_send" and not str(_pick(input_data, "edited_reply", "")).strip():
        reasons.append("edit_and_send with no edited text in edited_reply")
    return reasons


def _render_artifact(ticket_id: str, channel: str, thread_id: str,
                     delivery_mode: str, reply_body: str) -> str:
    """Render the sent reply to ./out (the verifiable artifact). Returns path."""
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    tid = ticket_id or "ticket"
    path = _OUT_DIR / f"{tid}_reply.md"
    lines = [
        f"# Sent reply — {tid}",
        f"Channel: {channel}   Thread: {thread_id or '(none)'}   Mode: {delivery_mode}",
        f"Dispatched: {_now()}",
        "",
        "---",
        "",
        reply_body,
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _delete_artifact(result: dict[str, Any]) -> None:
    p = result.get("artifact_path")
    if p:
        Path(p).unlink(missing_ok=True)
        result["artifact_path"] = ""


def _shape_connector_ack(raw: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Map the comms-dispatcher connector's send ack into yields."""
    ref = (raw.get("dispatch_id") or raw.get("message_ts") or raw.get("id")
           or raw.get("message_id") or store.new_id("disp"))
    ok = raw.get("sent")
    if ok is None:
        ok = bool(raw.get("ok", raw.get("success", True))) and not raw.get("error")
    return {
        **base,
        "dispatch_id": str(ref),
        "sent": bool(ok),
        "send_status": "connector_sent" if ok else "blocked",
        "dispatch_reason": "" if ok else str(raw.get("error", "connector reported failure")),
    }


def enforce_send_floor(result: Any, context: dict) -> dict:
    """The finisher floor — SEND only on a real approval; delete on a block.

    Re-checks the deterministic invariants independently of the body, so a
    sent=True that slipped through with no approval / unresolved PII is caught
    and its artifact deleted. Belt-and-suspenders over the body's own guard."""
    result = result if isinstance(result, dict) else {}
    result.setdefault("block_reasons", [])
    src = result.pop("_input", {})

    reasons = _block_reasons(src) if isinstance(src, dict) else []
    if result.get("sent") and reasons:
        # Body sent but the floor disagrees — revoke and delete the artifact.
        _delete_artifact(result)
        result.update(sent=False, send_status="blocked", delivery_mode="blocked")
        result["block_reasons"] = sorted(set(result["block_reasons"]) | set(reasons))
        result["dispatch_reason"] = "floor revoked: " + "; ".join(reasons)
        logger.warning(f"[dispatch] send floor REVOKED ticket={result.get('ticket_id')!r}: {reasons}")
        return result

    if not result.get("sent"):
        # A block path — make sure no partial artifact survives.
        _delete_artifact(result)
        result.setdefault("send_status", "blocked")
    return result


@chain(enforce_send_floor)
async def reply_dispatcher_agent(llm_response, input_data, context):
    """Send the approved reply. Fail-closed if not approved / PII unresolved.
    Connector path shapes its ack; dev path records the send to the outbox."""
    input_data = input_data or {}

    ticket_id = _pick(input_data, "ticket_id", "")
    channel = (_pick(input_data, "channel", "") or "web").lower()
    thread_id = _pick(input_data, "channel_thread_id", "")
    reply_body = _pick(input_data, "reply_body", "")
    delivery_mode = "batched" if channel in _BATCHED_CHANNELS else "in_thread"

    # Keep the reply context recoverable across the HITL gate / a re-entry.
    sid = resolve_session_id(input_data, context or {})
    if reply_body and sid:
        stash_put(sid, {"ticket_id": ticket_id, "channel": channel,
                        "channel_thread_id": thread_id, "reply_body": reply_body})
    elif not reply_body and sid:
        recovered = stash_get(sid)
        ticket_id = ticket_id or recovered.get("ticket_id", "")
        channel = channel if channel != "web" else recovered.get("channel", channel)
        thread_id = thread_id or recovered.get("channel_thread_id", "")
        reply_body = recovered.get("reply_body", "")
    # The person's corrected text replaces the draft (the floor blocks an
    # edit_and_send that carries none).
    if _decision(input_data) == "edit_and_send":
        reply_body = str(_pick(input_data, "edited_reply", "")).strip() or reply_body

    base = {
        **_DEFAULTS,
        "ticket_id": ticket_id,
        "channel": channel,
        "channel_thread_id": thread_id,
        "delivery_mode": delivery_mode,
        "dispatched_at_utc": _now(),
        "_input": input_data,          # the floor re-checks against this, then pops it
    }

    # ── THE FAIL-CLOSED FLOOR (layer 1: the body short-circuits) ────────
    reasons = _block_reasons(input_data)
    if reasons:
        logger.warning(
            f"reply_dispatcher_agent — BLOCKED (fail-closed): ticket={ticket_id!r} "
            f"reasons={reasons}; nothing sent, nothing recorded"
        )
        return {
            **base,
            "delivery_mode": "blocked",
            "sent": False,
            "send_status": "blocked",
            "block_reasons": reasons,
            "dispatch_reason": "not approved or unresolved PII/policy — fail-closed",
        }

    # ── Connector path: the comms dispatcher ran and returned an ack ────
    if isinstance(llm_response, dict) and llm_response:
        computed = _shape_connector_ack(llm_response, base)
        if computed["sent"]:
            computed["artifact_path"] = _render_artifact(
                ticket_id, channel, thread_id, delivery_mode, reply_body)
            store.record_send({
                "dispatch_id": computed["dispatch_id"],
                "ticket_id": ticket_id, "channel": channel,
                "channel_thread_id": thread_id, "delivery_mode": delivery_mode,
                "reply_body": reply_body, "source": "comms_connector",
            })
        logger.info(
            f"reply_dispatcher_agent — connector send ticket={ticket_id!r} "
            f"channel={channel} mode={delivery_mode} sent={computed['sent']}"
        )
        return computed

    # ── Dev path: no connector wired -> RECORD the send (real artifact) ──
    artifact_path = _render_artifact(ticket_id, channel, thread_id, delivery_mode, reply_body)
    rec = store.record_send({
        "ticket_id": ticket_id, "channel": channel,
        "channel_thread_id": thread_id, "delivery_mode": delivery_mode,
        "reply_body": reply_body, "artifact_path": artifact_path, "source": "dev_store",
    })
    logger.info(
        f"reply_dispatcher_agent — recorded send (dev_store) ticket={ticket_id!r} "
        f"channel={channel} mode={delivery_mode} dispatch={rec['dispatch_id']}"
    )
    return {
        **base,
        "dispatch_id": rec["dispatch_id"],
        "sent": True,
        "send_status": "recorded",
        "artifact_path": artifact_path,
        "dispatch_reason": f"recorded to dev outbox ({delivery_mode}); "
                           f"wire the comms_dispatch_n8n vault connection to send live",
    }
