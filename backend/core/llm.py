"""Minimal model gateway: health-checked routing over self-hosted OpenAI-compatible
endpoints, with fallback, request IDs, and validated JSON output.

Guided decoding (response_format=json_schema) on the current vLLM build runs away
emitting whitespace on enum fields until max_tokens. So the schema goes in the
prompt and the reply is validated here, with one corrective retry.
"""
from __future__ import annotations

import json
import re
import time
import uuid

import httpx

from backend.core.config import LLM_ENDPOINTS, LLM_TIMEOUT_S, LLMEndpoint


class LLMUnavailable(Exception):
    pass


class SchemaError(ValueError):
    pass


_health_cache: dict[str, tuple[float, bool]] = {}


def _auth(ep: LLMEndpoint) -> dict:
    return {"Authorization": f"Bearer {ep.api_key}"} if ep.api_key else {}


def _healthy(ep: LLMEndpoint) -> bool:
    t, ok = _health_cache.get(ep.name, (0.0, False))
    if time.time() - t < 15:
        return ok
    try:
        r = httpx.get(f"{ep.base_url}/models", timeout=3, headers=_auth(ep))
        ok = r.status_code == 200 and any(m.get("id") == ep.model for m in r.json().get("data", []))
    except Exception:
        ok = False
    _health_cache[ep.name] = (time.time(), ok)
    return ok


def validate(value, schema: dict, path: str = "$") -> None:
    """Validator for the JSON-schema subset used here (object/array/string/enum)."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise SchemaError(f"{path} must be an object")
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                raise SchemaError(f"{path}.{k} is required")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise SchemaError(f"{path} has unexpected keys {sorted(extra)}")
        for k, v in value.items():
            if k in props:
                validate(v, props[k], f"{path}.{k}")
    elif t == "array":
        if not isinstance(value, list):
            raise SchemaError(f"{path} must be an array")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(f"{path} has more than {schema['maxItems']} items")
        for i, v in enumerate(value):
            validate(v, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string":
        if not isinstance(value, str):
            raise SchemaError(f"{path} must be a string")
        if "enum" in schema and value not in schema["enum"]:
            raise SchemaError(f"{path} must be one of {schema['enum']}")


def _extract_json(text: str):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise SchemaError("no JSON object in reply")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise SchemaError(f"invalid JSON: {e}") from e


def chat_json(system: str, user: str, schema: dict, schema_name: str,
              max_tokens: int = 2500) -> tuple[dict, dict]:
    """Returns (validated_json, meta). Tries endpoints in order; on a transport error
    or two invalid replies it marks the endpoint unhealthy and falls through."""
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    sys_full = (system + f"\n\nRespond with ONE JSON object only (no prose, no code fences) that "
                f"matches this JSON schema named {schema_name}:\n{json.dumps(schema)}")
    errors = []
    for ep in LLM_ENDPOINTS:
        if not _healthy(ep):
            errors.append(f"{ep.name}: unhealthy")
            continue
        messages = [{"role": "system", "content": sys_full}, {"role": "user", "content": user}]
        t0 = time.time()
        usage_total = []
        for attempt in range(2):
            try:
                r = httpx.post(f"{ep.base_url}/chat/completions", timeout=LLM_TIMEOUT_S,
                               headers={"X-Request-Id": f"{request_id}-{attempt}", **_auth(ep)},
                               json={"model": ep.model, "messages": messages, "temperature": 0.1,
                                     "max_tokens": max_tokens})
                r.raise_for_status()
                resp = r.json()
            except Exception as e:
                _health_cache[ep.name] = (time.time(), False)
                errors.append(f"{ep.name}: {type(e).__name__}: {str(e)[:200]}")
                break
            usage_total.append(resp.get("usage"))
            content = resp["choices"][0]["message"].get("content") or ""
            try:
                parsed = _extract_json(content)
                validate(parsed, schema)
            except SchemaError as e:
                errors.append(f"{ep.name} attempt {attempt + 1}: {e}")
                messages += [{"role": "assistant", "content": content[:4000]},
                             {"role": "user", "content": f"That reply was rejected: {e}. "
                              "Send the corrected JSON object only."}]
                continue
            meta = {"endpoint": ep.name, "model": ep.model, "request_id": request_id,
                    "latency_s": round(time.time() - t0, 2), "attempts": attempt + 1,
                    "usage": usage_total, "skipped": errors}
            return parsed, meta
    raise LLMUnavailable("; ".join(errors))
