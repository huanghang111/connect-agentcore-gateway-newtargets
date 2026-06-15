import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from typing import Optional, Tuple

from botocore.config import Config as BotoConfig
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from strands import Agent
from strands.models import BedrockModel

WO_NUMBER_PATTERN = re.compile(r"^WO-\d{4}-\d{4}$")
SMS_TOKEN_PATTERN = re.compile(r"^\d{4}$")

# Industrial-robot product taxonomy. Each top-level category maps to its set of
# valid component-level sub-categories. requestRepair validates that the
# (productCategory, productsubCategory) pair is mutually consistent.
ROBOT_CATEGORIES = {
    "仓储机器人": ("导航传感器", "电池", "驱动电机", "通信模块"),   # WR-500, WR-800
    "巡检机器人": ("热成像模块", "轮组", "气体传感器", "通信"),      # IR-200, IR-400
    "协作机械臂": ("关节电机", "力矩传感器", "控制器", "线缆"),      # CA-100, CA-300
    "服务机器人": ("语音模块", "屏幕", "导航", "电池"),            # SR-50, SR-100
}

# Registered customers: phone number → "公司-联系人". Identity verification
# requires that the caller's phone number is on this list AND the spoken name
# matches the registered customer (company name OR contact person OR the full
# "公司-联系人" string, case/space-insensitive). Until a real CRM is wired in,
# this fixed table is the source of truth for both Flow 1 and Flow 2.
CUSTOMER_REGISTRY = {
    "13800018888": "华创智联-张伟",
    "13688881234": "顺丰物流-李强",
    "13755554321": "中电光伏-王建国",
    "13566667890": "京东亚洲一号-赵明",
    "13322223456": "国药集团-陈芳",
    "13177778901": "万达商管-周鹏",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("mcp_server")

mcp = FastMCP(host="0.0.0.0", stateless_http=True)


@mcp.custom_route("/ping", methods=["GET"])
async def ping(request: Request) -> JSONResponse:
    return JSONResponse({"status": "Healthy"})

API_URL = os.environ.get("REPAIR_API_URL", "")
API_KEY = os.environ.get("REPAIR_API_KEY", "")

NORMALIZE_RESPONSE = os.environ.get("NORMALIZE_RESPONSE", "1").strip().lower() not in (
    "", "0", "false", "no", "off",
)
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-east-1"))
NORMALIZE_MODEL_ID = os.environ.get(
    "NORMALIZE_MODEL_ID",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)
NORMALIZE_TIMEOUT_S = float(os.environ.get("NORMALIZE_TIMEOUT_S", "4"))

# --- Per-call identity verification (stateless) --------------------------------
# Every repair tool (requestRepair / trackRepair / cancelRepair) re-verifies the
# caller on EVERY call from two inline arguments — the caller's spoken name
# (callerName) and the last 4 digits of their phone number (callerPhoneTail) —
# against CUSTOMER_REGISTRY. There is NO token and NO server-side memory of
# previous verifications: identity is established fresh each call, so a caller
# who cannot produce a matching (name, last-4) pair cannot touch any work order.
# The resolved full phone number is the caller's identity, used both as the
# ticket owner on create and as the ownership key on track/cancel.

# Canonical schemas. Every query tool advertises these fields verbatim in its
# docstring so the orchestrator LLM sees one shape regardless of the upstream
# BU's field names. The Pydantic models double as the prompt schema for
# Strands' structured_output() — Strands maps them onto Bedrock tool-use, so
# the model is forced to emit valid JSON matching these types.

class TrackResponse(BaseModel):
    """Canonical track-repair response. Pull values from semantically equivalent
    upstream keys regardless of name (e.g. ticketstatus / tstatus / statusName
    all map to `status`). Use empty strings for unknown fields — never invent
    values that aren't grounded in the input."""

    woNumber: str = Field(default="", description="Work-order number.")
    status: str = Field(default="", description="Current work-order status (free-form string).")
    statusDescription: str = Field(default="", description="Human-readable explanation of the status.")
    scheduledAt: str = Field(default="", description="Scheduled service time, ISO 8601 if known, else empty.")
    technicianName: str = Field(default="", description="Assigned technician's full name; empty if unassigned.")
    technicianPhone: str = Field(default="", description="Assigned technician's contact phone; empty if unknown.")
    address: str = Field(default="", description="Service address.")
    lastUpdatedAt: str = Field(default="", description="Last update timestamp, ISO 8601 if known, else empty.")
    remarks: str = Field(default="", description="Additional notes from the upstream system.")


class CancelResponse(BaseModel):
    """Canonical cancel-repair response. Pull values from semantically equivalent
    upstream keys; use empty strings for unknown text fields. `cancelled` is
    true only when the upstream confirms the work order was cancelled."""

    woNumber: str = Field(default="", description="Work-order number.")
    cancelled: bool = Field(default=False, description="True iff the cancellation succeeded.")
    status: str = Field(default="", description="Resulting work-order status after the cancel call.")
    message: str = Field(default="", description="Human-readable confirmation or failure reason.")


class RequestResponse(BaseModel):
    """Canonical create-repair response. Pull values from semantically equivalent
    upstream keys (e.g. wono / ticketId / orderNumber → woNumber). Use empty
    strings for unknown text fields. `created` is true only when the upstream
    confirms a work order has been opened."""

    woNumber: str = Field(default="", description="Newly created work-order number; \"\" if creation failed.")
    created: bool = Field(default=False, description="True iff the upstream confirms the work order was created.")
    status: str = Field(default="", description="Initial work-order status, e.g. 'open' / 'pending'.")
    scheduledAt: str = Field(default="", description="Initial scheduled service time, ISO 8601 if known, else empty.")
    message: str = Field(default="", description="Human-readable confirmation or failure reason.")


class UpdateResponse(BaseModel):
    """Canonical update-repair response. Pull values from semantically equivalent
    upstream keys; use empty strings for unknown text fields. `updated` is true
    only when the upstream confirms the work order was modified."""

    woNumber: str = Field(default="", description="Work-order number.")
    updated: bool = Field(default=False, description="True iff the upstream confirms the work order was updated.")
    status: str = Field(default="", description="Work-order status after the update.")
    priority: str = Field(default="", description="Work-order priority after the update (P0=urgent, P1=high, P2=medium, P3=low).")
    description: str = Field(default="", description="Fault description after the update.")
    message: str = Field(default="", description="Human-readable confirmation or failure reason.")


_normalize_agent = None


def _get_normalize_agent():
    """Lazily build a single Strands Agent for response normalization."""
    global _normalize_agent
    if _normalize_agent is not None:
        return _normalize_agent
    try:
        boto_cfg = BotoConfig(
            read_timeout=NORMALIZE_TIMEOUT_S,
            connect_timeout=2,
            retries={"max_attempts": 1, "mode": "standard"},
        )
        model = BedrockModel(
            model_id=NORMALIZE_MODEL_ID,
            region_name=BEDROCK_REGION,
            boto_client_config=boto_cfg,
            temperature=0,
            max_tokens=512,
            streaming=False,
        )
        _normalize_agent = Agent(
            model=model,
            system_prompt=(
                "You normalize backend API responses for an MCP repair-service "
                "server. Given a raw JSON payload, fill the requested schema by "
                "mapping semantically equivalent upstream keys onto canonical "
                "field names. Use empty strings (\"\") for unknown text fields "
                "and false for unknown booleans. Never invent values that "
                "aren't present or directly inferable from the input."
            ),
            name="repair-response-normalizer",
            description="Maps heterogeneous upstream API responses onto canonical repair-tool schemas.",
        )
        return _normalize_agent
    except Exception:
        log.exception("failed to init Strands normalize agent; normalization disabled")
        return None


def _normalize_with_llm(raw: dict, schema: type, tool_name: str) -> dict:
    """Coerce a raw upstream response into ``schema`` (a Pydantic model) via
    Strands' structured_output(). Best-effort: any failure (timeout, throttle,
    schema-validation error) returns the raw response unchanged so the
    normalizer is never a hard dependency.

    Fast-path skips:
      - NORMALIZE_RESPONSE flag is off → raw.
      - input is not a dict → raw.
      - input already carries an "error" key → raw (error envelopes pass through).
    """
    if not NORMALIZE_RESPONSE:
        return raw
    if not isinstance(raw, dict):
        return raw
    if "error" in raw:
        return raw

    agent = _get_normalize_agent()
    if agent is None:
        return raw

    prompt = (
        f"Tool: {tool_name}\n"
        f"Map the following upstream JSON onto the requested schema. "
        f"For any canonical field whose value is not present or inferable, "
        f"use \"\" (empty string) for text fields and false for booleans.\n\n"
        f"Upstream JSON:\n{json.dumps(raw, ensure_ascii=False)}"
    )
    start = time.time()
    try:
        result = agent.structured_output(schema, prompt)
        out = result.model_dump()
        log.info(
            "normalize ok tool=%s in %.2fs raw_keys=%s",
            tool_name, time.time() - start, list(raw.keys())[:10],
        )
        return out
    except Exception:
        log.exception("normalize failed tool=%s in %.2fs — returning raw", tool_name, time.time() - start)
        return raw


def _call_api(path: str, payload: dict) -> dict:
    """Call the backend Repair Service API."""
    url = f"{API_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY},
        method="POST",
    )
    start = time.time()
    log.info("POST %s start payload=%s", path, json.dumps(payload)[:300])
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            log.info("POST %s ok in %.2fs resp=%s", path, time.time() - start, body[:500])
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        log.warning("POST %s HTTP %s in %.2fs body=%s", path, e.code, time.time() - start, body[:500])
        return {"error": f"HTTP {e.code}", "message": body}
    except Exception as e:
        log.exception("POST %s exception in %.2fs", path, time.time() - start)
        return {"error": str(e)}


def _validate_wo_number(wo_number: str) -> Optional[dict]:
    """Return an error dict if woNumber is missing or malformed, else None."""
    if not wo_number or not wo_number.strip():
        return {"error": "INVALID_WO_NUMBER", "message": "woNumber must not be empty"}
    if not WO_NUMBER_PATTERN.match(wo_number.strip()):
        return {"error": "INVALID_WO_NUMBER", "message": "woNumber must look like WO-YYYY-NNNN (e.g. WO-2026-0001)"}
    return None


def _norm(s: str) -> str:
    """Normalize a category/enum input: strip + lowercase + collapse internal whitespace."""
    return re.sub(r"\s+", "", (s or "").strip().lower())


# Pre-normalized lookup: norm(category) -> (canonical_category, {norm(sub): canonical_sub})
_CATEGORY_INDEX = {
    _norm(cat): (cat, {_norm(sub): sub for sub in subs})
    for cat, subs in ROBOT_CATEGORIES.items()
}


def _name_matches_registered(spoken: str, registered: str) -> bool:
    """Loose name match for identity verification.

    The registered name is "公司-联系人" (e.g. "华创智联-张伟"). A spoken name
    counts as a match when it equals the company part, the contact-person part,
    or the whole "公司-联系人" string — all compared case/space-insensitively.
    This tolerates customers who say only their company, only the contact name,
    or the full combined form.
    """
    s = _norm(spoken)
    if not s:
        return False
    candidates = {_norm(registered)}
    for part in registered.split("-"):
        if part.strip():
            candidates.add(_norm(part))
    return s in candidates


def _validate_category_pair(category: str, sub_category: str) -> Optional[dict]:
    """Validate that productCategory is a known robot category AND productsubCategory
    is a component that belongs to that category (case/space insensitive).

    Returns an error dict pinpointing the offending level, else None.
    """
    entry = _CATEGORY_INDEX.get(_norm(category))
    if entry is None:
        return {
            "error": "INVALID_CATEGORY",
            "message": f"productCategory must be one of: {', '.join(ROBOT_CATEGORIES.keys())}.",
            "allowed": list(ROBOT_CATEGORIES.keys()),
        }
    canonical_cat, sub_index = entry
    if _norm(sub_category) not in sub_index:
        return {
            "error": "INVALID_SUB_CATEGORY",
            "message": f"For productCategory '{canonical_cat}', productsubCategory must be one of: {', '.join(ROBOT_CATEGORIES[canonical_cat])}.",
            "allowed": list(ROBOT_CATEGORIES[canonical_cat]),
        }
    return None


def _authenticate_caller(caller_name: str, caller_phone_tail: str) -> Tuple[Optional[str], Optional[dict]]:
    """Verify the caller from an inline (name, last-4-digits) pair on EVERY call.

    Looks up CUSTOMER_REGISTRY for the unique customer whose phone number ends in
    ``caller_phone_tail`` AND whose registered name matches ``caller_name``
    (company OR contact person OR full "公司-联系人" string, case/space
    insensitive). No token, no cached state — this runs fresh for every repair
    tool invocation.

    Returns ``(full_phone_number, None)`` on success (the caller's verified
    identity, used as the ticket-owner / ownership key), or
    ``(None, error_dict)`` on any failure so the tool returns it verbatim:
      - INVALID_NAME           : caller_name empty
      - INVALID_SMS_TOKEN      : caller_phone_tail not exactly 4 digits
      - CUSTOMER_NOT_FOUND     : no registered customer matches (name, last-4)

    The 6 registered customers all have distinct last-4 digits, so a matching
    (name, last-4) pair identifies exactly one customer.
    """
    name = (caller_name or "").strip()
    if not name:
        return None, {
            "error": "INVALID_NAME",
            "message": "callerName must not be empty. Ask the customer for their name (company name or contact person) and call again.",
        }
    tail = (caller_phone_tail or "").strip()
    if not SMS_TOKEN_PATTERN.match(tail):
        return None, {
            "error": "INVALID_SMS_TOKEN",
            "message": "callerPhoneTail must be exactly the last 4 digits of the customer's phone number. Ask the customer to repeat them.",
        }
    for phone, registered in CUSTOMER_REGISTRY.items():
        if phone[-4:] == tail and _name_matches_registered(name, registered):
            return phone, None
    return None, {
        "error": "CUSTOMER_NOT_FOUND",
        "message": "No registered customer matches that name and last-4 digits. Ask the customer to confirm their name (company or contact person) and the last 4 digits of their phone number.",
    }


@mcp.tool()
def requestRepair(
    productCategory: str,
    productsubCategory: str,
    description: str,
    brand: str,
    callerName: str,
    callerPhoneTail: str,
    productModel: str = "",
    serialNumber: str = "",
) -> str:
    """Create a new industrial-robot repair work order.

    Identity is re-verified on EVERY call: ask for callerName + callerPhoneTail
    each time; never reuse values from a previous work order.

    productCategory must be 仓储机器人 / 巡检机器人 / 协作机械臂 / 服务机器人, and
    productsubCategory a component of it (e.g. 仓储机器人: 导航传感器/电池/驱动电机/
    通信模块), else INVALID_CATEGORY / INVALID_SUB_CATEGORY.

    Returns {woNumber, created, status, scheduledAt, message}. Identity errors:
    INVALID_NAME / INVALID_SMS_TOKEN / CUSTOMER_NOT_FOUND.

    Args:
        productCategory: 仓储机器人, 巡检机器人, 协作机械臂, or 服务机器人.
        productsubCategory: Faulty component of productCategory.
        description: AI summary plus dialog transcript.
        brand: Product brand / manufacturer.
        callerName: Company or contact name. Ask each call; never reuse.
        callerPhoneTail: Last 4 phone digits. Ask each call; never reuse.
        productModel: Robot model, e.g. WR-500. Optional.
        serialNumber: Unit serial number. Optional.
    """
    customer_phone, err = _authenticate_caller(callerName, callerPhoneTail)
    if err:
        return json.dumps(err)
    err = _validate_category_pair(productCategory, productsubCategory)
    if err:
        return json.dumps(err)
    result = _call_api("/repair/request", {
        "productCategory": productCategory,
        "productsubCategory": productsubCategory,
        "productModel": productModel,
        "serialNumber": serialNumber,
        "description": description,
        "brand": brand,
        "customerId": customer_phone,
    })
    result = _normalize_with_llm(result, RequestResponse, "requestRepair")
    return json.dumps(result)


@mcp.tool()
def trackRepair(woNumber: str, callerName: str, callerPhoneTail: str) -> str:
    """Query the status of an existing repair work order by its work-order number.

    Identity is re-verified on EVERY call: ask the customer for callerName +
    callerPhoneTail each time and never reuse values from a previous work order.
    A customer may only see their OWN company's work orders — a work order owned
    by a different customer (or one that does not exist) returns HTTP 404.

    Returns {woNumber, status, statusDescription, scheduledAt, technicianName,
    technicianPhone, address, lastUpdatedAt, remarks}; empty strings mean
    "unknown" — do not invent values. Errors: INVALID_WO_NUMBER (bad format),
    INVALID_NAME / INVALID_SMS_TOKEN / CUSTOMER_NOT_FOUND (identity).

    Args:
        woNumber: Work-order number, a WO-YYYY-NNNN string (e.g. WO-2026-0001).
        callerName: Customer's company name OR contact person's name. Ask every call; never reuse.
        callerPhoneTail: Last 4 digits of the customer's phone number. Ask every call; never reuse.
    """
    err = _validate_wo_number(woNumber)
    if err:
        return json.dumps(err)
    customer_phone, err = _authenticate_caller(callerName, callerPhoneTail)
    if err:
        return json.dumps(err)
    result = _call_api("/repair/track", {
        "woNumber": woNumber.strip(),
        "customerId": customer_phone,
    })
    result = _normalize_with_llm(result, TrackResponse, "trackRepair")
    return json.dumps(result)


@mcp.tool()
def cancelRepair(woNumber: str, callerName: str, callerPhoneTail: str) -> str:
    """Cancel an existing repair work order by its work-order number.

    Cancellation is destructive: obtain explicit customer confirmation first.
    Identity is re-verified on EVERY call: ask the customer for callerName +
    callerPhoneTail each time and never reuse values from a previous work order.
    A customer may only cancel their OWN company's work orders — a work order
    owned by a different customer (or one that does not exist) returns HTTP 404.

    Returns {woNumber, cancelled, status, message}. Already cancelled/completed
    orders return HTTP 409. Errors: INVALID_WO_NUMBER (bad format),
    INVALID_NAME / INVALID_SMS_TOKEN / CUSTOMER_NOT_FOUND (identity).

    Args:
        woNumber: Work-order number, a WO-YYYY-NNNN string (e.g. WO-2026-0001).
        callerName: Customer's company name OR contact person's name. Ask every call; never reuse.
        callerPhoneTail: Last 4 digits of the customer's phone number. Ask every call; never reuse.
    """
    err = _validate_wo_number(woNumber)
    if err:
        return json.dumps(err)
    customer_phone, err = _authenticate_caller(callerName, callerPhoneTail)
    if err:
        return json.dumps(err)
    result = _call_api("/repair/cancel", {
        "woNumber": woNumber.strip(),
        "customerId": customer_phone,
    })
    result = _normalize_with_llm(result, CancelResponse, "cancelRepair")
    return json.dumps(result)


# Mutable-field value ranges for updateRepair. status EXCLUDES "cancelled":
# cancelling is done via cancelRepair, not this tool.
UPDATE_PRIORITY_ENUM = ("P0", "P1", "P2", "P3")
UPDATE_STATUS_ENUM = ("pending", "scheduled", "in_progress", "completed")


@mcp.tool()
def updateRepair(
    woNumber: str,
    callerName: str,
    callerPhoneTail: str,
    description: str = "",
    priority: str = "",
    status: str = "",
) -> str:
    """Update a repair work order's fault description, priority, and/or status.

    Identity is re-verified on EVERY call: ask for callerName + callerPhoneTail
    each time; never reuse values from a previous work order. A customer may only
    update their OWN company's orders (else HTTP 404).

    Provide at least one of description / priority / status. priority: P0/P1/P2/P3.
    status: pending/scheduled/in_progress/completed (to CANCEL use cancelRepair).
    Returns {woNumber, updated, status, priority, description, message}.
    Already cancelled/completed orders return HTTP 409. Errors: INVALID_WO_NUMBER,
    INVALID_PRIORITY, INVALID_STATUS, NOTHING_TO_UPDATE, INVALID_NAME /
    INVALID_SMS_TOKEN / CUSTOMER_NOT_FOUND.

    Args:
        woNumber: Work-order number, a WO-YYYY-NNNN string (e.g. WO-2026-0001).
        callerName: Customer's company OR contact name. Ask every call; never reuse.
        callerPhoneTail: Last 4 phone digits. Ask every call; never reuse.
        description: New fault description. Optional.
        priority: New priority P0/P1/P2/P3 (P0=urgent/紧急, P1=high/高, P2=medium/中, P3=low/低). Optional.
        status: New status pending/scheduled/in_progress/completed. Optional.
    """
    err = _validate_wo_number(woNumber)
    if err:
        return json.dumps(err)
    desc = (description or "").strip()
    prio = (priority or "").strip()
    stat = (status or "").strip()
    if not desc and not prio and not stat:
        return json.dumps({
            "error": "NOTHING_TO_UPDATE",
            "message": "Provide at least one field to change: description, priority, or status.",
        })
    if prio and prio not in UPDATE_PRIORITY_ENUM:
        return json.dumps({
            "error": "INVALID_PRIORITY",
            "message": f"priority must be one of: {', '.join(UPDATE_PRIORITY_ENUM)}.",
            "allowed": list(UPDATE_PRIORITY_ENUM),
        })
    if stat and stat not in UPDATE_STATUS_ENUM:
        return json.dumps({
            "error": "INVALID_STATUS",
            "message": f"status must be one of: {', '.join(UPDATE_STATUS_ENUM)} (to cancel, use cancelRepair).",
            "allowed": list(UPDATE_STATUS_ENUM),
        })
    customer_phone, err = _authenticate_caller(callerName, callerPhoneTail)
    if err:
        return json.dumps(err)
    payload = {"woNumber": woNumber.strip(), "customerId": customer_phone}
    if desc:
        payload["description"] = desc
    if prio:
        payload["priority"] = prio
    if stat:
        payload["status"] = stat
    result = _call_api("/repair/update", payload)
    result = _normalize_with_llm(result, UpdateResponse, "updateRepair")
    return json.dumps(result)


@mcp.tool()
def faqSearch(query: str) -> str:
    """Search the FAQ knowledge base using natural language queries. Returns relevant FAQ entries about product usage, troubleshooting, warranty, and repair services.

    Args:
        query: Natural language question or search query
    """
    result = _call_api("/faq/simple", {"query": query})
    return json.dumps(result)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
