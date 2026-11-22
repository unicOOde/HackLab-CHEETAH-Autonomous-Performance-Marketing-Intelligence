"""Claude helper for the agents: one call -> one JSON object matching a schema.

Credentials: ANTHROPIC_API_KEY (env or .env), or any other credential the SDK resolves.
Model: CLAUDE_MODEL env, default claude-opus-5-5.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

import anthropic

from graph_store import _load_dotenv

DEFAULT_MODEL = "claude-opus-5-5"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
MAX_CONTINUATIONS = 4


class LLMUnavailable(RuntimeError):
    """No credentials / API failure / refusal. Callers must treat this as 'unknown', never act on it."""


def extract_json(text: str) -> Optional[dict]:
    """First JSON object in a model answer (also inside ```json fences)."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            data = json.loads(c)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def _client() -> anthropic.Anthropic:
    _load_dotenv()
    try:
        return anthropic.Anthropic()
    except Exception as e:  # missing credentials raise at construction
        raise LLMUnavailable(f"no Anthropic credentials: {e}") from e


def base_params(effort: str = "medium", model: Optional[str] = None) -> Dict[str, Any]:
    """Shared request params: model, effort, server-side refusal fallback."""
    return dict(
        model=model or os.getenv("CLAUDE_MODEL", DEFAULT_MODEL),
        max_tokens=16000,
        output_config={"effort": effort},
        # on a policy decline the API re-runs the request on a fallback model
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )


def get_client() -> anthropic.Anthropic:
    return _client()


def claude_json(prompt: str, schema: Dict[str, Any], *, system: Optional[str] = None,
                web_search: bool = False, effort: str = "medium",
                model: Optional[str] = None) -> Dict[str, Any]:
    """Ask Claude for a JSON object matching `schema`.

    web_search=True adds the web search server tool; the answer is then parsed from the final
    text (search results and citations come back as content blocks) and `sources` collects the
    URLs Claude actually looked at.
    """
    client = _client()
    params = base_params(effort, model)
    if system:
        params["system"] = system
    if web_search:
        params["tools"] = [WEB_SEARCH_TOOL]
        prompt += ("\n\nAnswer ONLY with one JSON object matching this JSON schema, no other text:\n"
                   + json.dumps(schema))
    else:
        params["output_config"]["format"] = {"type": "json_schema", "schema": schema}

    messages = [{"role": "user", "content": prompt}]
    try:
        for _ in range(MAX_CONTINUATIONS + 1):
            resp = client.beta.messages.create(messages=messages, **params)
            if resp.stop_reason != "pause_turn":
                break
            # server-side search loop hit its limit: resend so it resumes
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
    except anthropic.AuthenticationError as e:
        raise LLMUnavailable(f"Anthropic auth failed: {e}") from e
    except anthropic.APIStatusError as e:
        raise LLMUnavailable(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMUnavailable(f"Anthropic connection error: {e}") from e

    if resp.stop_reason == "refusal":
        raise LLMUnavailable("model declined the request")
    if resp.stop_reason == "max_tokens":
        raise LLMUnavailable("answer truncated (max_tokens)")

    text = "".join(b.text for b in resp.content if b.type == "text")
    data = extract_json(text)
    if data is None:
        raise LLMUnavailable(f"no JSON in answer: {text[:200]!r}")

    if web_search:
        urls = [r.url for b in resp.content if b.type == "web_search_tool_result"
                and isinstance(b.content, list) for r in b.content if getattr(r, "url", None)]
        data["sources"] = list(dict.fromkeys(list(data.get("sources") or []) + urls))
    return data
