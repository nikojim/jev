"""OpenRouter client: streaming chat completion with latency, TTFT, token and cost metrics."""

import json
import math
import re
import ssl
import time
from dataclasses import dataclass, field

import httpx
import truststore

from app.schemas import RunMetrics


class LLMError(RuntimeError):
    request: dict | None = None  # payload of the failed request, for display


@dataclass
class Completion:
    text: str
    metrics: RunMetrics
    # (token, logprob) pairs, when the provider returns logprobs
    logprobs: list[tuple[str, float]] = field(default_factory=list)
    # exact JSON body sent to OpenRouter (the API key travels in a header and is never included)
    request: dict = field(default_factory=dict)


def build_payload(model: str, messages: list[dict], response_format: dict | None) -> dict:
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "stream": True,
        "usage": {"include": True},
        "logprobs": True,
    }
    if response_format:
        payload["response_format"] = response_format
    return payload


class OpenRouterClient:
    def __init__(self, api_key: str, base_url: str, timeout: float):
        self.base_url = base_url
        self.http = httpx.AsyncClient(
            # Use the OS certificate store (not certifi) so TLS-inspecting proxies/antivirus
            # whose root CA is installed in Windows (e.g. Kaspersky) are trusted.
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            timeout=httpx.Timeout(timeout, connect=15),
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "http://localhost:8000",
                "X-Title": "jev ticket triage",
            },
        )
        self.pricing: dict[str, dict[str, float]] = {}

    async def aclose(self) -> None:
        await self.http.aclose()

    async def load_pricing(self) -> None:
        """Fetch per-token prices for every model (used when OpenRouter does not return usage.cost)."""
        try:
            r = await self.http.get(f"{self.base_url}/models")
            r.raise_for_status()
        except httpx.HTTPError:
            return
        for m in r.json().get("data", []):
            p = m.get("pricing") or {}
            try:
                self.pricing[m["id"]] = {"prompt": float(p.get("prompt", 0)), "completion": float(p.get("completion", 0))}
            except (TypeError, ValueError):
                continue

    @property
    def decisions_url(self) -> str:
        # https://openrouter.ai/api/v1 -> https://openrouter.ai/api/alpha/decisions
        root = self.base_url[: -len("/v1")] if self.base_url.endswith("/v1") else self.base_url
        return f"{root}/alpha/decisions"

    async def decisions(self, model: str, payload: dict) -> tuple[dict, RunMetrics]:
        """Call the Decisions API (no streaming: TTFT is not available)."""
        start = time.perf_counter()
        try:
            r = await self.http.post(self.decisions_url, json=payload)
        except httpx.TimeoutException:
            raise _with_request(LLMError("Request timed out"), payload)
        except httpx.HTTPError as e:
            raise _with_request(LLMError(f"Network error: {e}"), payload)
        latency = time.perf_counter() - start
        if r.status_code >= 400:
            raise _with_request(LLMError(f"HTTP {r.status_code}: {_error_message(r.text)}"), payload)
        data = r.json()
        u = data.get("usage") or {}
        usage = {"prompt_tokens": u.get("input_tokens"), "completion_tokens": u.get("output_tokens"), "cost": u.get("cost")}
        return data, self._metrics(model, usage, latency, None)

    async def chat(self, model: str, messages: list[dict], json_schema: dict) -> Completion:
        """Try strict JSON schema output first, then plain JSON mode, then no response_format."""
        formats = [{"type": "json_schema", "json_schema": json_schema}, {"type": "json_object"}, None]
        last_error: LLMError | None = None
        for fmt in formats:
            try:
                return await self._stream(model, messages, fmt)
            except LLMError as e:
                last_error = e
                if not _is_format_error(e):
                    raise
        raise last_error  # type: ignore[misc]

    async def _stream(self, model: str, messages: list[dict], response_format: dict | None) -> Completion:
        payload = build_payload(model, messages, response_format)
        try:
            completion = await self._send(model, payload)
        except LLMError as e:
            e.request = payload
            raise
        completion.request = payload
        return completion

    async def _send(self, model: str, payload: dict) -> Completion:
        text_parts: list[str] = []
        logprobs: list[tuple[str, float]] = []
        usage: dict = {}
        ttft: float | None = None
        start = time.perf_counter()

        try:
            async with self.http.stream("POST", f"{self.base_url}/chat/completions", json=payload) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode(errors="replace")
                    raise LLMError(f"HTTP {r.status_code}: {_error_message(body)}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # SSE comments such as ": OPENROUTER PROCESSING"
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if "error" in chunk:
                        raise LLMError(_error_message(json.dumps(chunk)))
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            if ttft is None:
                                ttft = time.perf_counter() - start
                            text_parts.append(content)
                        for lp in ((choice.get("logprobs") or {}).get("content") or []):
                            if "token" in lp and "logprob" in lp:
                                logprobs.append((lp["token"], lp["logprob"]))
        except httpx.TimeoutException:
            raise LLMError("Request timed out")
        except httpx.HTTPError as e:
            raise LLMError(f"Network error: {e}")

        latency = time.perf_counter() - start
        text = "".join(text_parts)
        if not text.strip():
            raise LLMError("Empty response from model")
        return Completion(text=text, metrics=self._metrics(model, usage, latency, ttft), logprobs=logprobs)

    def _metrics(self, model: str, usage: dict, latency: float, ttft: float | None) -> RunMetrics:
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        m = RunMetrics(latency_s=latency, ttft_s=ttft, prompt_tokens=pt, completion_tokens=ct,
                       total_tokens=int(usage.get("total_tokens") or pt + ct))
        if usage.get("cost") is not None:
            m.cost_usd, m.cost_source = float(usage["cost"]), "openrouter"
        elif model in self.pricing and (pt or ct):
            p = self.pricing[model]
            m.cost_usd, m.cost_source = pt * p["prompt"] + ct * p["completion"], "pricing"
        return m


def _with_request(e: LLMError, payload: dict) -> LLMError:
    e.request = payload
    return e


def is_decisions_model_error(e: LLMError) -> bool:
    return "decisions model" in str(e).lower()


def _error_message(body: str) -> str:
    try:
        err = json.loads(body).get("error", {})
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raw = (err.get("metadata") or {}).get("raw") if isinstance(err, dict) else None
        return f"{msg} ({raw})" if raw else (msg or body[:300])
    except (json.JSONDecodeError, AttributeError):
        return body[:300]


def _is_format_error(e: LLMError) -> bool:
    msg = str(e).lower()
    return msg.startswith("http 400") and any(k in msg for k in ("response_format", "json_schema", "json", "schema", "structured"))


def extract_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences or surrounding prose."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("No JSON object found in response")
        obj = json.loads(cleaned[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("Response JSON is not an object")
    return obj


def team_logprob(text: str, logprobs: list[tuple[str, float]], team: str) -> float | None:
    """Joint probability of the tokens that spell the chosen team in the "team" field, if logprobs exist."""
    if not logprobs or "".join(t for t, _ in logprobs) != text:
        return None
    m = re.search(r'"team"\s*:\s*"', text)
    if not m or not text[m.end():].startswith(team):
        return None
    span_start, span_end = m.end(), m.end() + len(team)
    total, pos = 0.0, 0
    for token, lp in logprobs:
        tok_start, tok_end = pos, pos + len(token)
        if tok_end > span_start and tok_start < span_end:
            total += lp
        pos = tok_end
        if pos >= span_end:
            break
    return math.exp(total)
