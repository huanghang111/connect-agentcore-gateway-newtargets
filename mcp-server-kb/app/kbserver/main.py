"""Connect KB MCP Server — self-serve knowledge-base Q&A + info-collection handoff.

Entrypoint for the AgentCore Runtime (Container build, MCP protocol). Exposes
four MCP tools over FastMCP (streamable-http), fronted by an AgentCore Gateway:

  searchKnowledgeBase — Retrieve from the managed Bedrock Knowledge Base and
                        synthesize a grounded Chinese answer + confidence.
  collectCustomerInfo — validate the minimal field set for a handoff, telling
                        the caller which fields are still missing.
  createPreTicket     — persist a pre-ticket (session summary) to DynamoDB.
  getPreTicket        — read a pre-ticket back (for the agent / debugging).

The 1–3 turn / confidence / fallback orchestration lives in the Connect AI
Agent SOP (skills/connect-agent-sop.md), not here — these tools are the
single-step primitives that SOP drives.
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from mcp.server.fastmcp import FastMCP

import kb_search

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("kbserver")

mcp = FastMCP("kbserver", host="0.0.0.0", stateless_http=True)


REGION = os.environ.get("AWS_REGION", "us-east-1")
PRETICKET_TABLE = os.environ.get("PRETICKET_TABLE", "").strip()

# Minimal field set for a handoff pre-ticket. serialNumber is optional.
REQUIRED_PRETICKET_FIELDS = ("productModel", "problemDescription", "contact")

TICKET_ID_PATTERN = re.compile(r"^PT-\d{10}$")

_ddb_table = None


def _get_table():
    """Lazily build the DynamoDB Table resource for pre-tickets."""
    global _ddb_table
    if _ddb_table is None:
        if not PRETICKET_TABLE:
            raise RuntimeError("PRETICKET_TABLE is not set on the Runtime")
        _ddb_table = boto3.resource("dynamodb", region_name=REGION).Table(PRETICKET_TABLE)
    return _ddb_table


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_ticket_id() -> str:
    """PT- + 10 digits derived from the current epoch microseconds. Not
    cryptographic — just a readable, time-sortable, collision-unlikely id."""
    micros = int(time.time() * 1_000_000)
    return f"PT-{micros % 10_000_000_000:010d}"


# ------------------------------------------------------------------ Tool 1: KB

@mcp.tool()
def searchKnowledgeBase(query: str, topK: int = 0) -> str:
    """Search the knowledge base and return a grounded, ready-to-speak Chinese answer.

    Use this FIRST for any customer question. It runs a hybrid search over the
    managed Bedrock Knowledge Base and then synthesizes an answer strictly from
    the retrieved passages, returning a confidence level the orchestrator uses
    to decide whether the question is resolved or should fall back to a human.

    RETURNS — JSON string:
      - answer:             Ready-to-speak Chinese answer (speak this verbatim).
      - confidence:         "HIGH" | "MEDIUM" | "LOW". LOW means no good match —
                            do NOT read a made-up answer; move to info collection.
      - resolvedSuggestion: Boolean hint that the answer likely resolves the ask.
      - citations:          [{text, source, score}] raw supporting passages.
    On a hard retrieval failure returns {"error": "...", "answer": "...", "confidence": "LOW"}.

    Args:
        query: The customer's question, in their own words. Required, non-empty.
        topK:  Optional number of passages to retrieve (1-20). 0 = server default.
    """
    if not query or not query.strip():
        return json.dumps(
            {"error": "INVALID_QUERY", "message": "query must not be empty",
             "answer": "请问您具体想咨询什么问题呢？", "confidence": "LOW",
             "resolvedSuggestion": False, "citations": []},
            ensure_ascii=False,
        )
    k = topK if isinstance(topK, int) and 1 <= topK <= 20 else None
    try:
        hits = kb_search.retrieve(query.strip(), top_k=k)
    except Exception as e:
        log.exception("KB retrieve failed")
        return json.dumps(
            {"error": "KB_RETRIEVE_FAILED", "message": str(e),
             "answer": "抱歉，知识库暂时查询失败。要不要我为您登记信息并转接人工客服？",
             "confidence": "LOW", "resolvedSuggestion": False, "citations": []},
            ensure_ascii=False,
        )
    result = kb_search.synthesize_answer(query.strip(), hits)
    result["citations"] = hits
    return json.dumps(result, ensure_ascii=False)


# ------------------------------------------------------ Tool 2: collect info

@mcp.tool()
def collectCustomerInfo(
    productModel: str = "",
    problemDescription: str = "",
    contact: str = "",
    serialNumber: str = "",
) -> str:
    """Validate the minimal field set needed to hand off to a human agent.

    Call this while collecting information across turns. It does NOT persist
    anything — it tells you which required fields are still missing so you know
    what to ask next. Once it reports complete=true, call createPreTicket.

    RETURNS — JSON string:
      - complete:   Boolean — true when all required fields are present.
      - missing:    List of required field names still empty.
      - normalized: The trimmed field values collected so far.

    Required fields: productModel, problemDescription, contact.
    Optional field:  serialNumber.

    Args:
        productModel:       Product name/model. Required for a complete ticket.
        problemDescription: What's wrong, in the customer's words. Required.
        contact:            Phone/email to reach the customer. Required.
        serialNumber:       Device serial number. Optional.
    """
    normalized = {
        "productModel": (productModel or "").strip(),
        "problemDescription": (problemDescription or "").strip(),
        "contact": (contact or "").strip(),
        "serialNumber": (serialNumber or "").strip(),
    }
    missing = [f for f in REQUIRED_PRETICKET_FIELDS if not normalized.get(f)]
    return json.dumps(
        {"complete": not missing, "missing": missing, "normalized": normalized},
        ensure_ascii=False,
    )


# ------------------------------------------------------ Tool 3: create ticket

@mcp.tool()
def createPreTicket(
    productModel: str,
    problemDescription: str,
    contact: str,
    serialNumber: str = "",
    sessionSummary: str = "",
) -> str:
    """Persist a pre-ticket (session summary) to DynamoDB for a human agent to pick up.

    Call this only after collectCustomerInfo reports complete=true. It writes
    the collected info plus your one-line session summary and returns the
    ticketId to read back to the customer before transferring to a queue.

    RETURNS — JSON string:
      - ticketId:  "PT-##########" — read this to the customer.
      - status:    "OPEN".
      - createdAt: ISO-8601 timestamp.
    On a missing required field returns {"error": "INCOMPLETE_INFO", "missing": [...]}.

    Args:
        productModel:       Product name/model. Required.
        problemDescription: Problem description. Required.
        contact:            Customer contact (phone/email). Required.
        serialNumber:       Device serial number. Optional.
        sessionSummary:     One-line summary of the conversation for the agent. Optional.
    """
    item = {
        "productModel": (productModel or "").strip(),
        "problemDescription": (problemDescription or "").strip(),
        "contact": (contact or "").strip(),
        "serialNumber": (serialNumber or "").strip(),
        "sessionSummary": (sessionSummary or "").strip(),
    }
    missing = [f for f in REQUIRED_PRETICKET_FIELDS if not item.get(f)]
    if missing:
        return json.dumps(
            {"error": "INCOMPLETE_INFO",
             "message": f"Missing required fields: {', '.join(missing)}. Call collectCustomerInfo to gather them first.",
             "missing": missing},
            ensure_ascii=False,
        )

    ticket_id = _gen_ticket_id()
    created_at = _now_iso()
    item.update({"ticketId": ticket_id, "status": "OPEN", "createdAt": created_at})
    try:
        _get_table().put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(ticketId)",
        )
    except ClientError as e:
        log.exception("createPreTicket put_item failed")
        return json.dumps(
            {"error": "WRITE_FAILED", "message": e.response.get("Error", {}).get("Message", str(e))},
            ensure_ascii=False,
        )
    except Exception as e:
        log.exception("createPreTicket failed")
        return json.dumps({"error": "WRITE_FAILED", "message": str(e)}, ensure_ascii=False)

    log.info("pre-ticket created %s", ticket_id)
    return json.dumps(
        {"ticketId": ticket_id, "status": "OPEN", "createdAt": created_at},
        ensure_ascii=False,
    )


# --------------------------------------------------------- Tool 4: get ticket

@mcp.tool()
def getPreTicket(ticketId: str) -> str:
    """Read a previously created pre-ticket back from DynamoDB.

    Useful for the agent to confirm what was captured, or for a human to look up
    a ticket by id. Scoped to a single ticketId.

    RETURNS — JSON string: the stored pre-ticket fields, or
    {"error": "NOT_FOUND"} if no ticket with that id exists.

    Args:
        ticketId: The pre-ticket id ("PT-##########"). Required.
    """
    tid = (ticketId or "").strip()
    if not TICKET_ID_PATTERN.match(tid):
        return json.dumps(
            {"error": "INVALID_TICKET_ID", "message": "ticketId must look like PT-########## (PT- + 10 digits)."},
            ensure_ascii=False,
        )
    try:
        resp = _get_table().get_item(Key={"ticketId": tid})
    except Exception as e:
        log.exception("getPreTicket failed")
        return json.dumps({"error": "READ_FAILED", "message": str(e)}, ensure_ascii=False)
    item = resp.get("Item")
    if not item:
        return json.dumps(
            {"error": "NOT_FOUND", "message": f"No pre-ticket found for {tid}."},
            ensure_ascii=False,
        )
    return json.dumps(item, ensure_ascii=False, default=str)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
