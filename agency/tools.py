"""Global day-0 tools for the customer-support pod.

These are the pod's day-0 tool surface — the tools the model calls, in the turn,
to PULL what it needs. Each reads the dev store (seed data) so the pod runs with
no connector wired, and **each announces itself**: when there is no real source
and no seed, it returns `available: false` and says why — never a synthetic
success that looks like a real read (doctrine A4/A5).

  - response_agent  -> search_kb, read_past_tickets  (ground the answer)
  - backlog_triage_agent -> claim_ticket_batch       (atomic backlog fan-out)

To go live, wire real RAG / a docs MCP (the commented mcp block on
response_agent) and point the backlog claim at your support desk's queue; these
dev tools are the fallback contract, and the function signatures do not change.
"""
import os

from leafmesh import global_tool, LeafMeshLogger

try:
    from ._shared import store
except ImportError:  # pragma: no cover — direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore

logger = LeafMeshLogger(__name__)


def _kb_connector_wired() -> bool:
    """True if a real KB/docs connector is configured (docs MCP URL set)."""
    return bool(os.getenv("DOCS_MCP_URL"))


@global_tool(category="knowledge")
def search_kb(query: str) -> dict:
    """Search the knowledge base (docs, runbook) for articles matching a query.

    Returns matching articles with id, title, section, url, and body so the reply
    can cite each source. When no real RAG/docs connector is wired, reads the seed
    KB. An empty `hits` with a reason is an honest knowledge gap — NOT an answer.

    Args:
        query: The customer's question or keywords to search for.
    """
    result = store.search_kb(str(query).strip())
    if not result.get("hits") and _kb_connector_wired():
        # A connector IS wired but returned nothing — surface unreachable rather
        # than an empty-but-confident zero.
        return {"available": False,
                "reason": "docs connector wired but returned nothing for this query"}
    logger.info(f"search_kb query={query!r} hits={len(result.get('hits', []))}")
    return result


@global_tool(category="knowledge")
def read_past_tickets(customer_id: str) -> dict:
    """Read a customer's prior resolved tickets, to ground the answer on
    precedent (what actually fixed this before).

    Args:
        customer_id: The customer whose prior tickets to read.
    """
    return store.read_past_tickets(str(customer_id).strip())


@global_tool(category="backlog")
def claim_ticket_batch(count: int = 8) -> dict:
    """Atomically claim a batch of queued backlog tickets to triage.

    The claim is concurrency-safe: when several parallel copies (instances: N)
    call it, each copy receives a DISJOINT batch, so copies never duplicate work.
    Returns the claimed ticket ids and how many remain in the backlog.

    Args:
        count: How many backlog tickets this copy should claim (default 8).
    """
    result = store.claim_batch(count)
    logger.info(f"claim_ticket_batch count={count} "
                f"claimed={result.get('claimed_count')} remaining={result.get('remaining')}")
    return result
