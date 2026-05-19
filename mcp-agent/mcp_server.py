import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from typing import Optional
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

WO_NUMBER_PATTERN = re.compile(r"^\d{10}$")
SMS_TOKEN_PATTERN = re.compile(r"^\d{4}$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("mcp_server")

mcp = FastMCP(host="0.0.0.0", stateless_http=True)


@mcp.custom_route("/ping", methods=["GET"])
async def ping(request: Request) -> JSONResponse:
    return JSONResponse({"status": "Healthy"})

API_URL = os.environ.get("REPAIR_API_URL", "")
API_KEY = os.environ.get("REPAIR_API_KEY", "")


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


def _validate_customer_id(customer_id: str) -> Optional[dict]:
    """Return an error dict if customerId is missing, else None."""
    if not customer_id or not customer_id.strip():
        return {
            "error": "MISSING_CUSTOMER_ID",
            "message": "customerId is required. If unknown, call verifyCustomer first with the last 4 digits of the customer's phone number to obtain a customerId.",
        }
    return None


def _verify_phone_tail_to_customer_id(phone_tail: str) -> str:
    """Resolve a 4-digit phone tail into a customerId.

    STUB: Until the real identity API is live, return ``"0000" + phone_tail``
    so end-to-end tests have a stable, deterministic customerId.
    """
    return f"0000{phone_tail}"


@mcp.tool(structured_output=False)
def verifyCustomer(smsToken: str) -> str:
    """Verify a customer by the last 4 digits of their phone number and return a customerId.

    HOW TO USE — read this BEFORE doing anything else:
      Call this tool ONCE per conversation when, and only when, the
      conversation context does NOT already contain a customerId. After this
      call succeeds, save the returned `customerId` for the rest of the
      conversation and pass it to requestRepair / trackRepair / cancelRepair.
      Do NOT call this tool again in the same conversation if a customerId
      is already known.

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

    Args:
        smsToken: Last 4 digits of the customer's phone number (NOT an SMS verification code). Required, exactly 4 digits.
    """
    err = _validate_sms_token(smsToken)
    if err:
        return json.dumps(err)
    customer_id = _verify_phone_tail_to_customer_id(smsToken.strip())
    log.info("verifyCustomer ok phone_tail=%s customerId=%s", smsToken.strip(), customer_id)
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
      If `customerId` is missing/empty this tool returns
      {"error": "MISSING_CUSTOMER_ID"}; in that case run verifyCustomer and
      retry — do NOT retry with an empty customerId.

    PRECONDITIONS (the caller MUST satisfy before invoking this tool):
      - productCategory / productsubCategory: validated via the product category
        lookup API (interface 7); only values returned by that API are accepted.
      - province / city / district: validated via the address mapping API
        (interface 8); the triple must form a valid administrative region.
      - productModel / serialNumber (optional): if provided, validated via the
        product model/SN API (interface 9).
      - brand: extracted from the dialog or the product library (recommended to
        reuse the extended response of interface 9).
      - description: AI-generated summary plus the dialog transcript, produced
        from the conversation context.

    Args:
        productCategory: Top-level product category (e.g., "Refrigerator"). Required. Must be validated via interface 7.
        productsubCategory: Product sub-category. Required. Must be validated via interface 7.
        province: Province / state. Required. Must be validated via interface 8.
        city: City. Required. Must be validated via interface 8.
        district: District / street. Required. Must be validated via interface 8.
        description: Work-order remark — AI summary plus dialog transcript. Required.
        brand: Product brand. Required. Extracted from dialog or product library (interface 9 extension recommended).
        customerId: Authenticated customer identifier. Required. Reuse the value already in the conversation; otherwise obtain via verifyCustomer first.
        productModel: Product model. Optional. If provided, must be validated via interface 9.
        serialNumber: Product serial number. Optional. If provided, must be validated via interface 9.
    """
    err = _validate_customer_id(customerId)
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
      If `customerId` is missing/empty this tool returns
      {"error": "MISSING_CUSTOMER_ID"}; in that case run verifyCustomer and
      retry — do NOT retry with an empty customerId.

    PRECONDITIONS (the caller MUST satisfy before invoking this tool):
      - woNumber must be non-empty and match the work-order number format
        (10-digit numeric string).

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
      If `customerId` is missing/empty this tool returns
      {"error": "MISSING_CUSTOMER_ID"}; in that case run verifyCustomer and
      retry — do NOT retry with an empty customerId.

    PRECONDITIONS (the caller MUST satisfy before invoking this tool):
      - woNumber must be non-empty and match the work-order number format
        (10-digit numeric string).

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
