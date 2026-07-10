"""Knowledge Base retrieval + answer synthesis for the searchKnowledgeBase tool.

Split out from main.py so the retrieval/generation logic stays isolated and
testable: `retrieve()` does the pure Bedrock `Retrieve` call, and
`synthesize_answer()` turns raw hits into a ready-to-speak Chinese answer plus a
confidence level.

KB id resolution: the AgentCore CLI creates the managed KB and its id is not
known until deploy time. Rather than thread the id through CDK outputs into the
runtime env, the runtime is given KNOWLEDGE_BASE_NAME and resolves the id once
at first use via bedrock-agent.list_knowledge_bases. KNOWLEDGE_BASE_ID may still
be set directly to skip the lookup.
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from pydantic import BaseModel, Field
from strands import Agent
from strands.models import BedrockModel

log = logging.getLogger("kb_search")

KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "").strip()
KNOWLEDGE_BASE_NAME = os.environ.get("KNOWLEDGE_BASE_NAME", "").strip()
KB_TOP_K = int(os.environ.get("KB_TOP_K", "5") or "5")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-east-1"))
KB_ANSWER_MODEL_ID = os.environ.get(
    "KB_ANSWER_MODEL_ID",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)
KB_ANSWER_TIMEOUT_S = float(os.environ.get("KB_ANSWER_TIMEOUT_S", "8"))

SKILLS_DIR = Path(__file__).with_name("skills")

_agent_runtime = None
_bedrock_agent = None
_answer_agent = None
_sop_cache: dict = {}
_resolved_kb_id: Optional[str] = None


def _get_agent_runtime():
    """Lazily build a single bedrock-agent-runtime client (short timeouts so a
    slow retrieval never hangs the MCP call)."""
    global _agent_runtime
    if _agent_runtime is None:
        cfg = BotoConfig(
            read_timeout=10,
            connect_timeout=3,
            retries={"max_attempts": 2, "mode": "standard"},
        )
        _agent_runtime = boto3.client(
            "bedrock-agent-runtime", region_name=BEDROCK_REGION, config=cfg
        )
    return _agent_runtime


def _resolve_kb_id() -> str:
    """Return the KB id, resolving it from KNOWLEDGE_BASE_NAME on first use.

    KNOWLEDGE_BASE_ID (if set) wins. Otherwise look the KB up by name via
    bedrock-agent.list_knowledge_bases and cache the result for the process.
    """
    global _bedrock_agent, _resolved_kb_id
    if KNOWLEDGE_BASE_ID:
        return KNOWLEDGE_BASE_ID
    if _resolved_kb_id:
        return _resolved_kb_id
    if not KNOWLEDGE_BASE_NAME:
        raise RuntimeError("Neither KNOWLEDGE_BASE_ID nor KNOWLEDGE_BASE_NAME is set on the Runtime")
    if _bedrock_agent is None:
        _bedrock_agent = boto3.client("bedrock-agent", region_name=BEDROCK_REGION)
    # The AgentCore CLI names the managed KB "<project>_<kbName>", so accept both
    # the exact name and a "*_<name>" suffix match — tolerant of whether the env
    # var carries the short or fully-qualified name.
    want = KNOWLEDGE_BASE_NAME
    exact = suffix = None
    for page in _bedrock_agent.get_paginator("list_knowledge_bases").paginate():
        for kb in page.get("knowledgeBaseSummaries", []):
            name = kb.get("name", "")
            if name == want:
                exact = kb["knowledgeBaseId"]
            elif name.endswith(f"_{want}"):
                suffix = kb["knowledgeBaseId"]
    _resolved_kb_id = exact or suffix
    if _resolved_kb_id:
        log.info("resolved KB '%s' -> %s", want, _resolved_kb_id)
        return _resolved_kb_id
    raise RuntimeError(f"No Knowledge Base named '{want}' (or '*_{want}') found in {BEDROCK_REGION}")


def retrieve(query: str, top_k: Optional[int] = None) -> list[dict]:
    """Run a single hybrid search against the managed Knowledge Base.

    Returns a list of ``{text, source, score}`` dicts (possibly empty). Raises
    on a hard client error (KB not found, throttling after retries) — the caller
    is expected to catch and degrade gracefully.
    """
    kb_id = _resolve_kb_id()
    k = top_k or KB_TOP_K
    client = _get_agent_runtime()
    start = time.time()
    # Fully-managed KBs (AgentCore CLI creates these) require
    # `managedSearchConfiguration`; the classic `vectorSearchConfiguration` is
    # rejected with a ValidationException for managed KBs.
    resp = client.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "managedSearchConfiguration": {"numberOfResults": k}
        },
    )
    hits = []
    for r in resp.get("retrievalResults", []):
        content = r.get("content", {}) or {}
        loc = r.get("location", {}) or {}
        # location shape varies by source type (S3/WEB/…); pull a human URI best-effort.
        source = (
            loc.get("s3Location", {}).get("uri")
            or loc.get("webLocation", {}).get("url")
            or loc.get("type", "")
        )
        hits.append({
            "text": content.get("text", ""),
            "source": source,
            "score": r.get("score", 0.0),
        })
    log.info(
        "kb retrieve ok kb=%s k=%d hits=%d in %.2fs",
        kb_id, k, len(hits), time.time() - start,
    )
    return hits


def _load_sop(sop_name: str) -> Optional[str]:
    if sop_name in _sop_cache:
        return _sop_cache[sop_name]
    try:
        text = (SKILLS_DIR / f"{sop_name}.md").read_text(encoding="utf-8")
    except Exception:
        log.exception("failed to read SOP %s", sop_name)
        text = None
    _sop_cache[sop_name] = text
    return text


class _KbAnswer(BaseModel):
    """The structured answer the synthesis agent must produce."""

    answer: str = Field(default="", description="Ready-to-speak Chinese answer.")
    confidence: str = Field(default="LOW", description="HIGH | MEDIUM | LOW")
    resolvedSuggestion: bool = Field(
        default=False, description="Whether this answer likely resolves the question."
    )


def _get_answer_agent():
    """Lazily build the Strands agent whose system prompt is skills/kb_answer.md."""
    global _answer_agent
    if _answer_agent is not None:
        return _answer_agent
    sop = _load_sop("kb_answer")
    if not sop:
        _answer_agent = None
        return None
    try:
        cfg = BotoConfig(
            read_timeout=KB_ANSWER_TIMEOUT_S,
            connect_timeout=2,
            retries={"max_attempts": 1, "mode": "standard"},
        )
        model = BedrockModel(
            model_id=KB_ANSWER_MODEL_ID,
            region_name=BEDROCK_REGION,
            boto_client_config=cfg,
            temperature=0,
            max_tokens=700,
            streaming=False,
        )
        _answer_agent = Agent(
            model=model,
            system_prompt=sop,
            name="kb-answer",
            description="Synthesizes a grounded Chinese answer + confidence from KB hits.",
        )
    except Exception:
        log.exception("failed to init KB answer agent; using deterministic fallback")
        _answer_agent = None
    return _answer_agent


def _fallback_answer(query: str, hits: list[dict]) -> dict:
    """Deterministic answer used when the synthesis agent is unavailable.

    No LLM: if there are hits, stitch the top passage in; if not, return the
    LOW-confidence handoff message. Kept intentionally simple — its job is to
    keep the tool working during a Bedrock outage, not to be eloquent.
    """
    if not hits:
        return {
            "answer": "抱歉，我暂时没有找到与您问题相关的信息。要不要我为您登记信息并转接人工客服？",
            "confidence": "LOW",
            "resolvedSuggestion": False,
        }
    top = hits[0].get("text", "").strip()
    return {
        "answer": f"根据资料：{top}",
        "confidence": "MEDIUM",
        "resolvedSuggestion": False,
    }


def synthesize_answer(query: str, hits: list[dict]) -> dict:
    """Turn raw KB hits into ``{answer, confidence, resolvedSuggestion}``.

    Best-effort: on missing SOP / model failure / timeout / bad output, fall
    back to a deterministic answer so the tool never hard-fails.
    """
    agent = _get_answer_agent()
    if agent is None:
        return _fallback_answer(query, hits)
    context = {"query": query, "results": hits, "count": len(hits)}
    prompt = (
        "请根据下面的数据，按你的 SOP 生成 JSON（answer/confidence/resolvedSuggestion）。\n\n"
        f"数据(JSON)：\n{json.dumps(context, ensure_ascii=False)}"
    )
    start = time.time()
    try:
        result = agent.structured_output(_KbAnswer, prompt)
        out = result.model_dump()
        conf = (out.get("confidence") or "LOW").upper()
        if conf not in ("HIGH", "MEDIUM", "LOW"):
            conf = "LOW"
        out["confidence"] = conf
        if not (out.get("answer") or "").strip():
            log.warning("kb answer empty in %.2fs — using fallback", time.time() - start)
            return _fallback_answer(query, hits)
        log.info("kb answer ok conf=%s in %.2fs", conf, time.time() - start)
        return out
    except Exception:
        log.exception("kb answer failed in %.2fs — using fallback", time.time() - start)
        return _fallback_answer(query, hits)
