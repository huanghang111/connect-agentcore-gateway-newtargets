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


@mcp.tool(structured_output=False)
def requestRepair(
    productCategory: str,
    productsubCategory: str,
    province: str,
    city: str,
    district: str,
    description: str,
    brand: str,
    productModel: str = "",
    serialNumber: str = "",
) -> str:
    """Create a new repair work order.

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
        productModel: Product model. Optional. If provided, must be validated via interface 9.
        serialNumber: Product serial number. Optional. If provided, must be validated via interface 9.
    """
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
    })
    return json.dumps(result)


@mcp.tool(structured_output=False)
def trackRepair(woNumber: str) -> str:
    """Query the status of an existing repair work order by its work-order number.

    PRECONDITIONS (the caller MUST satisfy before invoking this tool):
      - woNumber must be non-empty and match the work-order number format
        (10-digit numeric string).

    Args:
        woNumber: Work-order number. Required, non-empty, 10-digit numeric.
    """
    err = _validate_wo_number(woNumber)
    if err:
        return json.dumps(err)
    result = _call_api("/repair/track", {"woNumber": woNumber.strip()})
    return json.dumps(result)


@mcp.tool(structured_output=False)
def cancelRepair(woNumber: str) -> str:
    """Cancel an existing repair work order by its work-order number.

    PRECONDITIONS (the caller MUST satisfy before invoking this tool):
      - woNumber must be non-empty and match the work-order number format
        (10-digit numeric string).

    Args:
        woNumber: Work-order number. Required, non-empty, 10-digit numeric.
    """
    err = _validate_wo_number(woNumber)
    if err:
        return json.dumps(err)
    result = _call_api("/repair/cancel", {"woNumber": woNumber.strip()})
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
