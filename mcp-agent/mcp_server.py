import json
import os
import time
import urllib.request
import urllib.error
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP(host="0.0.0.0", stateless_http=True)


@mcp.custom_route("/ping", methods=["GET"])
async def ping(request: Request) -> JSONResponse:
    return JSONResponse({"status": "Healthy"})

API_URL = os.environ.get("REPAIR_API_URL", "")
API_KEY = os.environ.get("REPAIR_API_KEY", "")


def _log(msg: str) -> None:
    print(f"[tool] {msg}", flush=True)


def _call_api(path: str, payload: dict) -> dict:
    """Call the backend Repair Service API."""
    url = f"{API_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": API_KEY,
        },
        method="POST",
    )
    start = time.time()
    _log(f"POST {path} start payload={json.dumps(payload)[:300]}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
            _log(f"POST {path} ok in {time.time()-start:.2f}s resp={body[:500]}")
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        _log(f"POST {path} HTTP {e.code} in {time.time()-start:.2f}s body={body[:500]}")
        return {"error": f"HTTP {e.code}", "message": body}
    except Exception as e:
        _log(f"POST {path} exception in {time.time()-start:.2f}s: {e}")
        return {"error": str(e)}


@mcp.tool()
def requestRepair(
    product_model: str,
    serial_number: str,
    purchase_date: str,
    issue_description: str,
    full_name: str,
    phone: str,
    service_address: str,
    preferred_time: str,
    warranty_status: str,
) -> str:
    """Create a new repair ticket for a product. Records customer information, generates a repair ticket, and returns a unique 10-digit ticket number.

    Args:
        product_model: The model name or number of the product needing repair
        serial_number: The unique serial number of the product
        purchase_date: When the product was purchased (YYYY-MM-DD format)
        issue_description: A detailed description of the problem or malfunction
        full_name: Customer's complete name
        phone: Customer's contact phone number
        service_address: The address where repair service should be performed or product picked up
        preferred_time: Customer's preferred date and time for service
        warranty_status: Whether the product is still under warranty (yes, no, or unknown)
    """
    result = _call_api("/repair/request", {
        "product_model": product_model,
        "serial_number": serial_number,
        "purchase_date": purchase_date,
        "issue_description": issue_description,
        "full_name": full_name,
        "phone": phone,
        "service_address": service_address,
        "preferred_time": preferred_time,
        "warranty_status": warranty_status,
    })
    return json.dumps(result)


@mcp.tool()
def trackRepair(
    repair_notice_or_work_order_number: str,
    full_name: str,
    phone: str,
    need_to_reschedule_or_missed_visit: str,
    waiting_for_spare_part: str,
) -> str:
    """Check the status of an existing repair ticket. Requires the ticket number and customer verification information.

    Args:
        repair_notice_or_work_order_number: The repair notice or work order number
        full_name: Customer's complete name
        phone: Customer's contact phone number
        need_to_reschedule_or_missed_visit: Whether customer needs to reschedule or missed a visit (yes or no)
        waiting_for_spare_part: Whether customer is waiting for a spare part (yes or no)
    """
    result = _call_api("/repair/track", {
        "repair_notice_or_work_order_number": repair_notice_or_work_order_number,
        "full_name": full_name,
        "phone": phone,
        "need_to_reschedule_or_missed_visit": need_to_reschedule_or_missed_visit,
        "waiting_for_spare_part": waiting_for_spare_part,
    })
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
