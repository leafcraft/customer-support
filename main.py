"""customer-support — Customer-Support Pod (LeafMesh).

Boot order matters (SDK 2.4.131):
  1. import agency.tools  → the @global_tool decorators run BEFORE from_store/start,
     so response_agent's search_kb / read_past_tickets and backlog_triage's
     claim_ticket_batch are registered.
  2. LeafMesh.from_store() → auto_discover wires the finisher
     agency/reply_dispatcher_agent.py to the YAML.
  3. audit_logger.register(...) → attach the WORM sink to the custom external
     support_audit_logger_agent (after from_store, before start).
  4. leafmesh.start().

YAML-FIRST: every agent is defined in the agency's configuration (in the store;
see Studio); the only Python is the
finisher (reply_dispatcher_agent.py), the WORM sink (agency/_shared), and the
day-0 dev-store tools (agency/tools.py + agency/_shared/store.py).
"""
import asyncio
import signal
import sys

from dotenv import load_dotenv
from leafmesh import EventType, LeafMesh, LeafMeshLogger

import agency.tools  # noqa: F401 — registers @global_tool tools before start()
from agency._shared import audit_logger as _audit_logger

load_dotenv()
logger = LeafMeshLogger(__name__)
leafmesh = LeafMesh.from_store()

# Register the WORM audit sink as the intelligence function for the
# custom external support_audit_logger_agent (SDK 2.4+ API).
_audit_logger.register(leafmesh, "support_audit_logger_agent")


async def _on_agent_error(event):
    logger.warning(f"[support-audit] AGENT_ERROR session={event.session_id}")

async def _on_manager_intervention(event):
    logger.warning(f"[support-ops] MANAGER_INTERVENTION session={event.session_id}")

async def _on_intervention_needed(event):
    logger.error(f"[support-ops] INTERVENTION_NEEDED session={event.session_id}")

async def _on_human_input_timeout(event):
    logger.warning(f"[support-audit] HITL TIMEOUT session={event.session_id}")


async def main():
    await leafmesh.start()
    leafmesh.event_bus.subscribe(EventType.AGENT_ERROR,           _on_agent_error)
    leafmesh.event_bus.subscribe(EventType.MANAGER_INTERVENTION,  _on_manager_intervention)
    leafmesh.event_bus.subscribe(EventType.INTERVENTION_NEEDED,   _on_intervention_needed)
    leafmesh.event_bus.subscribe(EventType.HUMAN_INPUT_TIMEOUT,   _on_human_input_timeout)
    # Self-healing is automatic — the SDK enables it during start() on
    # every operator-facing mesh. No call is needed.

    logger.info("customer-support — Customer Support mesh running")
    logger.info("API: http://127.0.0.1:18820/docs")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
    else:
        try: await stop.wait()
        except KeyboardInterrupt: pass

    await leafmesh.stop()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
