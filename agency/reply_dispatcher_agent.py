"""reply_dispatcher_agent — egress actuator (SYSTEM, connector-backed).

The mesh DRAFTS (response_agent), CLEARS (qa_agent) and a human APPROVES
(tier_1_agent_human) a reply — but approval is not delivery. This is the
role that FINISHES the job: it actually SENDS the approved reply on the
channel the ticket arrived on (slack / email / whatsapp / …), in-thread,
and logs the send to the ticket. An async / closed-session ticket is
dispatched in a batched mode (the comms platform bulk-sends).

Production: the COMMS_DISPATCH_WEBHOOK_URL connector (a comms dispatcher /
CPaaS — Slack postMessage, your email gateway, Twilio/EmailBison) runs and
this shapes its ack into yields. Dev: no URL wired → a dev-store fallback
RECORDS the send to ./data/outbox.json so the full chain runs day-0 and
the artifact is verifiable.

THE FAIL-CLOSED FLOOR (deterministic, not LLM-waivable): never dispatch a
reply that wasn't human-approved, or that still has unresolved PII / policy
concerns. A blocked dispatch returns sent=False with a reason and records
NOTHING to the outbox — it routes on to audit as a non-send.

Yields (config.yaml): dispatch_id, ticket_id, channel, channel_thread_id,
delivery_mode, sent, send_status, dispatched_at_utc, dispatch_reason.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from leafmesh import LeafMeshLogger

from agency._shared import store

logger = LeafMeshLogger(__name__)

# Tier-1 decisions that authorise an actual send. Anything else
# (escalate_to_tier_2, reject, halt, blank) is NOT a send.
_SEND_DECISIONS = frozenset({"sent", "send", "edit_and_send", "approve", "approved"})

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
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pick(input_data: dict[str, Any], key: str, default: Any = "") -> Any:
    """Shape-tolerant read: input_data, then upstream_yields (flat or
    keyed by agent), then human_data."""
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


def _is_approved(input_data: dict[str, Any]) -> bool:
    """A send is authorised only when tier-1 approved AND QA found no PII /
    policy concern. tier-1's decision arrives as human_message (the SDK's
    human-response field) or as a `decision` yield; QA's concerns ride the
    upstream_yields. Fail closed on anything ambiguous."""
    decision = str(
        input_data.get("human_message")
        or _pick(input_data, "decision", "")
    ).strip().lower()
    if decision not in _SEND_DECISIONS:
        return False
    # PII / policy must be resolved — a non-empty concern list blocks even
    # if a human clicked send (the deterministic floor the human can't waive).
    pii = _pick(input_data, "pii_concerns", [])
    policy = _pick(input_data, "policy_concerns", [])
    if (isinstance(pii, list) and pii) or (isinstance(policy, list) and policy):
        return False
    # qa_agent's explicit approval flag, when present, must also hold.
    approved = _pick(input_data, "approved", None)
    if approved is False:
        return False
    return True


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


async def reply_dispatcher_agent(llm_response, input_data, context):
    """Send the approved reply. Fail-closed if not approved / PII unresolved.
    Connector path shapes its ack; dev path records the send to the outbox."""
    input_data = input_data or {}

    ticket_id = _pick(input_data, "ticket_id", "")
    channel = (_pick(input_data, "channel", "") or "web").lower()
    thread_id = _pick(input_data, "channel_thread_id", "")
    reply_body = _pick(input_data, "reply_body", "")
    delivery_mode = "batched" if channel in _BATCHED_CHANNELS else "in_thread"

    base = {
        **_DEFAULTS,
        "ticket_id": ticket_id,
        "channel": channel,
        "channel_thread_id": thread_id,
        "delivery_mode": delivery_mode,
        "dispatched_at_utc": _now(),
    }

    # ── THE FAIL-CLOSED FLOOR ───────────────────────────────────────
    if not _is_approved(input_data):
        logger.warning(
            f"reply_dispatcher_agent — BLOCKED (fail-closed): ticket={ticket_id!r} "
            f"not approved or has unresolved PII/policy concerns; nothing sent"
        )
        return {
            **base,
            "delivery_mode": "blocked",
            "sent": False,
            "send_status": "blocked",
            "dispatch_reason": "not approved or unresolved PII/policy — fail-closed",
        }

    # ── Connector path: the comms dispatcher ran and returned an ack ──
    if isinstance(llm_response, dict) and llm_response:
        computed = _shape_connector_ack(llm_response, base)
        if computed["sent"]:
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

    # ── Dev path: no connector wired → RECORD the send (real artifact) ──
    rec = store.record_send({
        "ticket_id": ticket_id, "channel": channel,
        "channel_thread_id": thread_id, "delivery_mode": delivery_mode,
        "reply_body": reply_body, "source": "dev_store",
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
        "dispatch_reason": f"recorded to dev outbox ({delivery_mode}); "
                           f"wire COMMS_DISPATCH_WEBHOOK_URL to send live",
    }
