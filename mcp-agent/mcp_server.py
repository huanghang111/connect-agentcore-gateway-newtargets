import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from botocore.config import Config as BotoConfig
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from strands import Agent
from strands.models import BedrockModel

WO_NUMBER_PATTERN = re.compile(r"^\d{10}$")
SMS_TOKEN_PATTERN = re.compile(r"^\d{4}$")
PHONE_NUMBER_PATTERN = re.compile(r"^\d{6,15}$")

SUBCATEGORY_ENUM = ("smart version", "premium version", "elite version")

REGIONS_FILE = Path(__file__).with_name("china_regions_pinyin.json")

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
        return {"error": "INVALID_WO_NUMBER", "message": "woNumber must be a 10-digit number"}
    return None


def _validate_sms_token(sms_token: str) -> Optional[dict]:
    """Return an error dict if smsToken is missing or malformed, else None.

    NOTE: Until the SMS-sending backend is live, `smsToken` is repurposed as
    the last 4 digits of the customer's phone number — collected verbally in
    Connect — and validated as a 4-digit numeric string.
    """
    if not sms_token or not sms_token.strip():
        return {"error": "INVALID_SMS_TOKEN", "message": "Please ask the customer for the last 4 digits of their phone number."}
    if not SMS_TOKEN_PATTERN.match(sms_token.strip()):
        return {"error": "INVALID_SMS_TOKEN", "message": "Input is not 4 digits. Please ask the customer to repeat the last 4 digits of their phone number."}
    return None


def _norm(s: str) -> str:
    """Normalize a region/enum input: strip + lowercase + collapse internal whitespace."""
    return re.sub(r"\s+", "", (s or "").strip().lower())


def _load_regions() -> dict:
    """Build a province-level lookup table.

    Schema:
      {
        norm(province_variant): {
          "self": frozenset(norm(province_variant), ...),   # to detect city==province
          "cities": { norm(city_variant): frozenset(norm(district), ...) },
          "all_districts": frozenset(norm(district), ...),  # union across cities
        }
      }
    """
    with open(REGIONS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    index: dict = {}
    for prov in data["regions"]:
        cities_idx: dict = {}
        all_dists: set = set()
        for city in prov["cities"]:
            dist_set = set()
            for dist in city["districts"]:
                for v in dist["d"]:
                    dist_set.add(_norm(v))
            dist_fset = frozenset(dist_set)
            for v in city["c"]:
                cities_idx[_norm(v)] = dist_fset
            all_dists |= dist_set
        prov_self = frozenset(_norm(v) for v in prov["p"])
        prov_entry = {
            "self": prov_self,
            "cities": cities_idx,
            "all_districts": frozenset(all_dists),
        }
        for v in prov["p"]:
            index[_norm(v)] = prov_entry
    log.info("loaded china regions: %d provinces", len(index))
    return index


_REGION_INDEX: dict = _load_regions()

# Virtual "市辖区" city layer that the official dataset interposes between
# direct-administered municipalities (Beijing/Shanghai/Tianjin/Chongqing) and
# their districts. Customers/LLMs never actually say this — they say
# "Beijing, Chaoyang" — so we accept either pattern.
_MUNICIPALITY_PROXY_CITY = frozenset({"shixiaqu", "shixia", "市辖区", "市辖"})

# Direct-administered municipalities (直辖市). For these provinces only, we
# also accept city == province name (the way users/LLMs naturally phrase it).
_MUNICIPALITY_PROVINCES = frozenset({
    "beijingshi", "beijing", "北京市", "北京",
    "shanghaishi", "shanghai", "上海市", "上海",
    "tianjinshi", "tianjin", "天津市", "天津",
    "chongqingshi", "chongqing", "重庆市", "重庆",
})


_SUBCATEGORY_NORMALIZED = {_norm(s) for s in SUBCATEGORY_ENUM}


def _validate_subcategory(value: str) -> Optional[dict]:
    """Reject sub-category values outside the fixed enum (case/space insensitive)."""
    if _norm(value) not in _SUBCATEGORY_NORMALIZED:
        return {
            "error": "INVALID_SUB_CATEGORY",
            "message": f"productsubCategory must be one of: {', '.join(SUBCATEGORY_ENUM)}.",
            "allowed": list(SUBCATEGORY_ENUM),
        }
    return None


def _validate_region(province: str, city: str, district: str) -> Optional[dict]:
    """Validate the (province, city, district) triple against the China admin-division pinyin list.

    Accepts either Chinese names or pinyin (with/without administrative suffix), case-insensitive.

    Special case for direct-administered municipalities (Beijing/Shanghai/
    Tianjin/Chongqing): the official dataset puts a virtual "市辖区" city between
    the municipality and its districts; users say "Beijing, Chaoyang" instead.
    We accept three city patterns for these provinces:
      - city == province name (e.g., province=Beijing, city=Beijing)
      - city == "市辖区" / "shixiaqu"
      - the literal city in the dataset

    Returns an error dict pinpointing the first level that fails, else None.
    """
    p, c, d = _norm(province), _norm(city), _norm(district)
    prov = _REGION_INDEX.get(p)
    if prov is None:
        return {
            "error": "INVALID_PROVINCE",
            "message": f"Unknown province '{province}'. Please ask the customer for the Chinese-administrative province name (in Chinese or pinyin).",
        }

    is_municipality = p in _MUNICIPALITY_PROVINCES
    if is_municipality and (c in prov["self"] or c in _MUNICIPALITY_PROXY_CITY):
        districts = prov["all_districts"]
    else:
        districts = prov["cities"].get(c)
        if districts is None:
            return {
                "error": "INVALID_CITY",
                "message": f"City '{city}' is not part of province '{province}'. Please ask the customer to confirm the city.",
            }

    if d not in districts:
        return {
            "error": "INVALID_DISTRICT",
            "message": f"District '{district}' is not part of city '{city}'. Please ask the customer to confirm the district.",
        }
    return None


def _validate_customer_id(customer_id: str) -> Optional[dict]:
    """Return an error dict if customerId is missing, else None."""
    if not customer_id or not customer_id.strip():
        return {
            "error": "MISSING_CUSTOMER_ID",
            "message": "customerId is required. If unknown, call verifyCustomer first with the last 4 digits of the customer's phone number; if that returns CUSTOMER_NOT_FOUND, fall back to verifyCustomerByPhoneAndName with the customer's full phone number and full name.",
        }
    return None


def _verify_phone_tail_to_customer_id(phone_tail: str) -> Optional[str]:
    """Resolve a 4-digit phone tail into a customerId, or ``None`` if no match.

    STUB: Until the real identity API is live this returns
    ``"0000" + phone_tail`` for any 4-digit input EXCEPT the magic value
    ``"0000"``, which simulates a "customer not found" outcome so the
    fallback (full phone number + full name) flow can be exercised end-to-end
    on Connect.
    """
    if phone_tail == "0000":
        return None
    return f"0000{phone_tail}"


def _verify_phone_and_name_to_customer_id(phone_number: str, full_name: str) -> Optional[str]:
    """Resolve a (full phone number, full name) pair into a customerId, or ``None``.

    STUB: Until the real identity API is live, return ``"PHN" + last 4 digits
    of phone_number`` for any input where the phone number does NOT end in
    ``0000``. A phone number ending in ``0000`` simulates a still-not-found
    outcome so the agent can fall back to a human handoff during testing.
    """
    _ = full_name  # reserved for the real identity API; stub keys only on phone tail
    tail = phone_number[-4:]
    if tail == "0000":
        return None
    return f"PHN{tail}"


@mcp.tool(structured_output=False)
def verifyCustomer(smsToken: str) -> str:
    """Verify a customer by the last 4 digits of their phone number and return a customerId.

    HOW TO USE — read this BEFORE doing anything else:
      This is the PRIMARY identity check. Call it ONCE per conversation when,
      and only when, the conversation context does NOT already contain a
      customerId. After this call succeeds, save the returned `customerId` for
      the rest of the conversation and pass it to requestRepair / trackRepair
      / cancelRepair. Do NOT call this tool again in the same conversation if
      a customerId is already known.

      Workflow:
        1. Ask the customer in plain language for the last 4 digits of their
           phone number (e.g. "For verification, could you tell me the last
           four digits of your phone number?"). Never tell the customer you
           are sending or have sent an SMS / verification code.
        2. Pass exactly those 4 digits as `smsToken`.
        3. On success the result is {"customerId": "..."} — remember this
           customerId and reuse it for every subsequent repair tool call.
        4. If the value is not exactly 4 digits, this tool returns
           {"error": "INVALID_SMS_TOKEN"}. Apologize briefly and ask again
           for the last 4 digits — do NOT retry with the same bad value,
           and do NOT mention SMS.
        5. If the lookup succeeds in format but no customer matches, this
           tool returns {"error": "CUSTOMER_NOT_FOUND"}. In that case tell
           the customer "Sorry, we couldn't find an account with that phone
           number. Could you provide a different full phone number along
           with the account holder's full name so we can look it up?" and
           then call `verifyCustomerByPhoneAndName` with the values they
           supply. Do NOT keep retrying verifyCustomer with new last-4
           guesses, and do NOT ask for the last 4 digits a second time.

    Args:
        smsToken: Last 4 digits of the customer's phone number (NOT an SMS verification code). Required, exactly 4 digits.
    """
    err = _validate_sms_token(smsToken)
    if err:
        return json.dumps(err)
    phone_tail = smsToken.strip()
    customer_id = _verify_phone_tail_to_customer_id(phone_tail)
    if customer_id is None:
        log.info("verifyCustomer not_found phone_tail=%s", phone_tail)
        return json.dumps({
            "error": "CUSTOMER_NOT_FOUND",
            "message": "No customer matches the given last 4 digits. Ask the customer for a full phone number and full name, then call verifyCustomerByPhoneAndName.",
        })
    log.info("verifyCustomer ok phone_tail=%s customerId=%s", phone_tail, customer_id)
    return json.dumps({"customerId": customer_id})


@mcp.tool(structured_output=False)
def verifyCustomerByPhoneAndName(phoneNumber: str, fullName: str) -> str:
    """Fallback identity check — verify a customer by FULL phone number plus full name.

    HOW TO USE — read this BEFORE doing anything else:
      Call this tool ONLY after `verifyCustomer` has already returned
      {"error": "CUSTOMER_NOT_FOUND"} earlier in this same conversation, AND
      the customer has supplied BOTH a different full phone number AND the
      account holder's full name. Do NOT call this tool as the first identity
      check — start with `verifyCustomer` instead.

      Workflow:
        1. After verifyCustomer returns CUSTOMER_NOT_FOUND, ask: "Could you
           provide a different full phone number, and the full name on the
           account?" Collect BOTH values before calling this tool.
        2. Pass the full digits-only phone number as `phoneNumber` and the
           full name as `fullName`.
        3. On success the result is {"customerId": "..."} — remember this
           customerId and reuse it for every subsequent repair tool call.
        4. On {"error": "INVALID_PHONE_NUMBER"} or {"error": "INVALID_NAME"},
           apologize briefly and ask the customer to repeat the offending
           field — do NOT retry with the same bad value.
        5. On {"error": "CUSTOMER_NOT_FOUND"} the second time, do NOT loop.
           Apologize and offer to transfer the customer to a human agent.

    Args:
        phoneNumber: Customer's full phone number, digits only (no spaces, dashes, or country-code prefix symbols). Required.
        fullName: Account holder's full name. Required, non-empty.
    """
    phone = (phoneNumber or "").strip()
    name = (fullName or "").strip()
    if not phone or not PHONE_NUMBER_PATTERN.match(phone):
        return json.dumps({
            "error": "INVALID_PHONE_NUMBER",
            "message": "phoneNumber must be a digits-only string of 6–15 digits (no spaces, dashes, or '+'). Ask the customer to repeat the full phone number.",
        })
    if not name:
        return json.dumps({
            "error": "INVALID_NAME",
            "message": "fullName must not be empty. Ask the customer for the full name on the account.",
        })
    customer_id = _verify_phone_and_name_to_customer_id(phone, name)
    if customer_id is None:
        log.info("verifyCustomerByPhoneAndName not_found phone=%s name=%s", phone, name)
        return json.dumps({
            "error": "CUSTOMER_NOT_FOUND",
            "message": "No customer matches the given phone number and name. Do NOT loop — apologize and offer to transfer to a human agent.",
        })
    log.info("verifyCustomerByPhoneAndName ok phone=%s name=%s customerId=%s", phone, name, customer_id)
    return json.dumps({"customerId": customer_id})


@mcp.tool(structured_output=False)
def requestRepair(
    productCategory: str,
    productsubCategory: str,
    province: str,
    city: str,
    district: str,
    description: str,
    brand: str,
    customerId: str,
    productModel: str = "",
    serialNumber: str = "",
) -> str:
    """Create a new repair work order.

    IDENTITY — read this BEFORE doing anything else:
      `customerId` authorizes this call. Resolve it in this order:
        1. If the conversation already has a customerId (from earlier in the
           conversation, or already provided in the agent's context), pass
           that. Do NOT ask the customer for verification again.
        2. Otherwise call verifyCustomer first with the last 4 digits of the
           customer's phone number, store the returned customerId, and then
           call this tool with it.
        3. If verifyCustomer returns {"error": "CUSTOMER_NOT_FOUND"}, ask
           the customer for a different full phone number AND the account
           holder's full name, then call verifyCustomerByPhoneAndName with
           those values. Use the customerId it returns.
      If `customerId` is missing/empty this tool returns
      {"error": "MISSING_CUSTOMER_ID"}; in that case run the appropriate
      verify tool and retry — do NOT retry with an empty customerId.

    PRECONDITIONS (the caller MUST satisfy before invoking this tool):
      - productCategory: validated via the product category lookup API
        (interface 7); only values returned by that API are accepted.
      - productsubCategory: MUST be one of the fixed enum values
        "smart version", "premium version", or "elite version"
        (case-insensitive). Any other value is rejected with
        {"error": "INVALID_SUB_CATEGORY"}.
      - province / city / district: validated server-side against the official
        China administrative-division list (Chinese name or pinyin accepted,
        case-insensitive, suffix optional). The triple must be hierarchically
        consistent — district must belong to city, city must belong to
        province. Failures return {"error": "INVALID_PROVINCE" |
        "INVALID_CITY" | "INVALID_DISTRICT"}; ask the customer to clarify the
        offending level and retry — do NOT retry with the same bad value.

        DIRECT-ADMINISTERED MUNICIPALITIES — Beijing, Shanghai, Tianjin,
        Chongqing have NO real city layer. When the customer mentions one of
        these four, do NOT ask which city — pass the municipality name as
        BOTH `province` and `city`, e.g. province="Beijing", city="Beijing",
        district="Chaoyang". This is accepted by the validator. Same for
        北京/上海/天津/重庆.
      - productModel / serialNumber (optional): if provided, validated via the
        product model/SN API (interface 9).
      - brand: extracted from the dialog or the product library (recommended to
        reuse the extended response of interface 9).
      - description: AI-generated summary plus the dialog transcript, produced
        from the conversation context.

    RETURNS — canonical schema (upstream field names like `wono` / `ticketId` /
    `orderNumber` are normalized onto `woNumber`):
      - woNumber:    Newly created work-order number (string, "" on failure).
      - created:     Boolean — true iff upstream confirms the work order was created.
      - status:      Initial work-order status (string, e.g. "open" / "pending").
      - scheduledAt: Initial scheduled service time (ISO 8601 string, "" if unknown).
      - message:     Human-readable confirmation or failure reason (string).
    Empty strings mean "unknown" — phrase them to the customer accordingly.
    Error envelopes ({"error": "..."}) are returned unchanged.

    Args:
        productCategory: Top-level product category (e.g., "Refrigerator"). Required. Must be validated via interface 7.
        productsubCategory: Product sub-category. Required. MUST be one of: "smart version", "premium version", "elite version" (case-insensitive).
        province: Province / state. Required. Chinese name or pinyin; validated against China admin divisions.
        city: City. Required. Chinese name or pinyin; must belong to the given province.
        district: District / street. Required. Chinese name or pinyin; must belong to the given city.
        description: Work-order remark — AI summary plus dialog transcript. Required.
        brand: Product brand. Required. Extracted from dialog or product library (interface 9 extension recommended).
        customerId: Authenticated customer identifier. Required. Reuse the value already in the conversation; otherwise obtain via verifyCustomer first.
        productModel: Product model. Optional. If provided, must be validated via interface 9.
        serialNumber: Product serial number. Optional. If provided, must be validated via interface 9.
    """
    err = _validate_customer_id(customerId)
    if err:
        return json.dumps(err)
    err = _validate_subcategory(productsubCategory)
    if err:
        return json.dumps(err)
    err = _validate_region(province, city, district)
    if err:
        return json.dumps(err)
    result = _call_api("/repair/request", {
        "productCategory": productCategory,
        "productsubCategory": productsubCategory,
        "productModel": productModel,
        "serialNumber": serialNumber,
        "province": province,
        "city": city,
        "district": district,
        "description": description,
        "brand": brand,
        "customerId": customerId.strip(),
    })
    result = _normalize_with_llm(result, RequestResponse, "requestRepair")
    return json.dumps(result)


@mcp.tool(structured_output=False)
def trackRepair(woNumber: str, customerId: str) -> str:
    """Query the status of an existing repair work order by its work-order number.

    IDENTITY — read this BEFORE doing anything else:
      `customerId` authorizes this call. Resolve it in this order:
        1. If the conversation already has a customerId (from earlier in the
           conversation, or already provided in the agent's context), pass
           that. Do NOT ask the customer for verification again.
        2. Otherwise call verifyCustomer first with the last 4 digits of the
           customer's phone number, store the returned customerId, and then
           call this tool with it.
        3. If verifyCustomer returns {"error": "CUSTOMER_NOT_FOUND"}, ask
           the customer for a different full phone number AND the account
           holder's full name, then call verifyCustomerByPhoneAndName with
           those values. Use the customerId it returns.
      If `customerId` is missing/empty this tool returns
      {"error": "MISSING_CUSTOMER_ID"}; in that case run the appropriate
      verify tool and retry — do NOT retry with an empty customerId.

    PRECONDITIONS (the caller MUST satisfy before invoking this tool):
      - woNumber must be non-empty and match the work-order number format
        (10-digit numeric string).

    RETURNS — canonical schema (the upstream API may use different field names
    such as `ticketstatus`, `tstatus`, `statusName`; this tool normalizes them
    so you can rely on these exact keys):
      - woNumber:           Work-order number (string).
      - status:             Current work-order status (string).
      - statusDescription:  Human-readable status explanation (string).
      - scheduledAt:        Scheduled service time (ISO 8601 string, "" if unknown).
      - technicianName:     Technician's full name (string, "" if unassigned).
      - technicianPhone:    Technician's contact phone (string, "" if unknown).
      - address:            Service address (string).
      - lastUpdatedAt:      Last update timestamp (ISO 8601 string, "" if unknown).
      - remarks:            Additional notes (string).
    Empty strings mean "unknown" — phrase them to the customer accordingly
    (e.g. "no technician has been assigned yet"), do NOT invent values.
    Error envelopes ({"error": "..."}) are returned unchanged.

    Args:
        woNumber: Work-order number. Required, non-empty, 10-digit numeric.
        customerId: Authenticated customer identifier. Required. Reuse the value already in the conversation; otherwise obtain via verifyCustomer first.
    """
    err = _validate_wo_number(woNumber)
    if err:
        return json.dumps(err)
    err = _validate_customer_id(customerId)
    if err:
        return json.dumps(err)
    result = _call_api("/repair/track", {
        "woNumber": woNumber.strip(),
        "customerId": customerId.strip(),
    })
    result = _normalize_with_llm(result, TrackResponse, "trackRepair")
    return json.dumps(result)


@mcp.tool(structured_output=False)
def cancelRepair(woNumber: str, customerId: str) -> str:
    """Cancel an existing repair work order by its work-order number.

    IDENTITY — read this BEFORE doing anything else:
      `customerId` authorizes this call. Resolve it in this order:
        1. If the conversation already has a customerId (from earlier in the
           conversation, or already provided in the agent's context), pass
           that. Do NOT ask the customer for verification again.
        2. Otherwise call verifyCustomer first with the last 4 digits of the
           customer's phone number, store the returned customerId, and then
           call this tool with it.
        3. If verifyCustomer returns {"error": "CUSTOMER_NOT_FOUND"}, ask
           the customer for a different full phone number AND the account
           holder's full name, then call verifyCustomerByPhoneAndName with
           those values. Use the customerId it returns.
      If `customerId` is missing/empty this tool returns
      {"error": "MISSING_CUSTOMER_ID"}; in that case run the appropriate
      verify tool and retry — do NOT retry with an empty customerId.

    PRECONDITIONS (the caller MUST satisfy before invoking this tool):
      - woNumber must be non-empty and match the work-order number format
        (10-digit numeric string).

    RETURNS — canonical schema (upstream field names are normalized):
      - woNumber:   Work-order number (string).
      - cancelled:  Boolean — true if the cancellation succeeded.
      - status:     Resulting work-order status after the cancel call (string).
      - message:    Human-readable confirmation or failure reason (string).
    Error envelopes ({"error": "..."}) are returned unchanged.

    Args:
        woNumber: Work-order number. Required, non-empty, 10-digit numeric.
        customerId: Authenticated customer identifier. Required. Reuse the value already in the conversation; otherwise obtain via verifyCustomer first.
    """
    err = _validate_wo_number(woNumber)
    if err:
        return json.dumps(err)
    err = _validate_customer_id(customerId)
    if err:
        return json.dumps(err)
    result = _call_api("/repair/cancel", {
        "woNumber": woNumber.strip(),
        "customerId": customerId.strip(),
    })
    result = _normalize_with_llm(result, CancelResponse, "cancelRepair")
    return json.dumps(result)


@mcp.tool(structured_output=False)
def faqSearch(query: str) -> str:
    """Search the FAQ knowledge base using natural language queries. Returns relevant FAQ entries about product usage, troubleshooting, warranty, and repair services.

    Args:
        query: Natural language question or search query
    """
    result = _call_api("/faq/simple", {"query": query})
    return json.dumps(result)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
