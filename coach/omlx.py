"""Chat Completions client for oMLX and configurable hosted API providers.

The historical module/function names remain compatible with existing callers.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from coach.config import load

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_BLOCK = re.compile(r"```([A-Za-z0-9_+-]*)[ \t]*\n(.*?)```", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)

TRACE = Path(__file__).resolve().parents[1] / "state" / "reasoning.jsonl"
_TRACE_CHARS = 60_000


def _trace(record: dict) -> None:
    """The console reads this file. A logging failure must never break a model call."""
    try:
        TRACE.parent.mkdir(parents=True, exist_ok=True)
        with TRACE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def close_stale_calls(reason: str = "interrupted by coach restart") -> int:
    """Close trace calls left 'running' by a previous (killed) process.

    A restart mid-call leaves a 'start' record with no 'done', which the console
    would otherwise show as an ever-growing 'call in flight'. On startup every
    open call belongs to a dead process, so mark it ended. Only event/id/at/error
    are written so the original label and sampling stay intact.
    """
    try:
        lines = TRACE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    latest: dict = {}
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("id"):
            latest[row["id"]] = row.get("event")
    now = time.time()
    closed = 0
    for cid, event in latest.items():
        if event == "start":
            _trace({"event": "error", "id": cid, "at": now, "error": reason})
            closed += 1
    return closed


def split_thinking(content: str) -> tuple[str, str]:
    body = _THINK.search(content or "")
    thought = body.group(0)[7:-8].strip() if body else ""
    return thought, _THINK.sub("", content or "").strip()


class OmlxError(RuntimeError):
    pass


def request_payload(cfg, model, messages, max_tokens, sampling=None, think=False, stream=False):
    """Send local extensions only to oMLX; use the provider's token field."""
    payload = {'model':model, 'messages':messages, cfg.llm_token_field:max_tokens}
    if stream: payload['stream'] = True
    if cfg.llm_provider == 'omlx':
        payload['chat_template_kwargs'] = {'enable_thinking':think}
    if cfg.llm_send_sampling:
        allowed = {'temperature','top_p'} | ({'top_k'} if cfg.llm_provider == 'omlx' else set())
        payload.update({k:v for k,v in (sampling or {'temperature':.3}).items() if k in allowed and v is not None})
    return payload


def chat_json(
    base_url: str, model: str, messages: list[dict], api_key: str = "",
    timeout: int = 240, max_tokens: int = 1800, sampling: dict | None = None,
    think: bool = False, label: str = "",
) -> dict:
    return _parse_json(chat_text(
        base_url, model, messages, api_key, timeout, max_tokens, sampling, think, label,
    ))


def chat_text(
    base_url: str, model: str, messages: list[dict], api_key: str = "",
    timeout: int = 240, max_tokens: int = 1800, sampling: dict | None = None,
    think: bool = False, label: str = "",
) -> str:
    cfg = load()
    candidates = [model] + [item for item in cfg.llm_fallback_models if item != model]
    last = None
    for candidate in candidates:
        try:
            return _complete(base_url, candidate, messages, api_key, timeout, max_tokens, sampling, think, label)
        except OmlxError as exc:
            last = exc
            if "failed to load" not in str(exc):
                raise
            print(f"Model {candidate} failed to load, trying the configured fallback", flush=True)
    raise last


def _complete(
    base_url: str, model: str, messages: list[dict], api_key: str, timeout: int, max_tokens: int,
    sampling: dict | None = None, think: bool = False, label: str = "",
) -> str:
    payload = request_payload(load(), model, messages, max_tokens, sampling, think)
    call_id = uuid.uuid4().hex[:12]
    started = time.time()
    prompt = "\n\n".join(str(m.get("content") or "") for m in messages)
    _trace({
        "event": "start", "id": call_id, "at": started, "source": "coach", "label": label or "coach",
        "model": model, "think": think, "max_tokens": max_tokens, "sampling": sampling or {},
        "prompt_chars": len(prompt), "prompt": prompt[:_TRACE_CHARS],
    })
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + api_key} if api_key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        _trace({"event": "error", "id": call_id, "at": time.time(), "source": "coach",
                "label": label or "coach", "model": model, "seconds": round(time.time() - started, 1),
                "error": f"HTTP {exc.code}: {detail}"})
        raise OmlxError(f"Model API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, 'reason', str(exc))
        _trace({"event": "error", "id": call_id, "at": time.time(), "source": "coach",
                "label": label or "coach", "model": model, "seconds": round(time.time() - started, 1),
                "error": str(reason)})
        raise OmlxError(f"Model API is not reachable at {base_url}: {reason}") from exc
    try:
        message = body["choices"][0]["message"]
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        if reasoning and "<think>" not in content:
            content = "<think>\n" + reasoning.strip() + "\n</think>\n" + content
    except (KeyError, IndexError, TypeError) as exc:
        _trace({"event": "error", "id": call_id, "at": time.time(), "source": "coach",
                "label": label or "coach", "model": model, "seconds": round(time.time() - started, 1),
                "error": f"no message: {json.dumps(body)[:300]}"})
        raise OmlxError(f"oMLX returned no message: {json.dumps(body)[:400]}") from exc
    thought, visible = split_thinking(content)
    _trace({
        "event": "done", "id": call_id, "at": time.time(), "source": "coach", "label": label or "coach",
        "model": model, "think": think, "sampling": sampling or {}, "max_tokens": max_tokens,
        "seconds": round(time.time() - started, 1),
        "thinking": thought[:_TRACE_CHARS], "thinking_chars": len(thought),
        "content": visible[:_TRACE_CHARS], "content_chars": len(visible),
        "usage": body.get("usage") or {},
    })
    return content


def parse_blocks(content: str) -> tuple[str, dict]:
    """Read one ```python agent.py block and, when present, one ```json strategy block."""
    text = _THINK.sub("", content or "")
    code = ""
    strategy = None
    for language, body in _BLOCK.findall(text):
        tag = language.lower()
        if tag in ("python", "py") and len(body) > len(code):
            code = body
        elif tag == "json" and strategy is None:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                strategy = parsed
    if not code.strip():
        for language, body in _BLOCK.findall(text):
            if not language.strip() and "def act(" in body and len(body) > len(code):
                code = body
    if not code.strip():
        opened = re.search(r"```(?:python|py)[ \t]*\n", text, re.IGNORECASE)
        if opened:
            code = text[opened.end():]
    if not code.strip():
        raise OmlxError(
            "no ```python block was returned. Reply with the complete agent.py inside "
            "a ```python fence. The strategy ```json fence is optional."
        )
    if strategy is None:
        strategy = {}
    return code.strip("\n") + "\n", strategy


def _parse_json(content: str) -> dict:
    text = _THINK.sub("", content or "").strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise OmlxError("oMLX did not return a JSON object: " + text[:300])
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise OmlxError(f"oMLX JSON failed: {exc}: {text[:300]}") from exc
    if not isinstance(parsed, dict):
        raise OmlxError("oMLX JSON was not an object")
    return parsed
