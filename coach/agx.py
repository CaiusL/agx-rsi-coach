"""AGX account client. The key is sent as Authorization: Bearer."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

TRANSIENT = {429, 502, 503, 504}


class AgxError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class Agx:
    def __init__(self, base_url: str, api_key: str | None = None):
        self.base = base_url.rstrip("/")
        self.api_key = api_key or ""

    def get(self, path: str, params: dict | None = None):
        return self._request("GET", path, params=params)

    def post_json(self, path: str, payload: dict, idempotency: str | None = None):
        return self._request("POST", path, json_body=payload, idempotency=idempotency)

    def post_zip(self, path: str, blob: bytes, idempotency: str):
        return self._request(
            "POST",
            path,
            data=blob,
            content_type="application/zip",
            idempotency=idempotency,
            timeout=120,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        idempotency: str | None = None,
        timeout: int = 60,
    ):
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        url = self.base + path + query
        raw = data if data is not None else (json.dumps(json_body).encode() if json_body is not None else None)
        headers = {"Accept": "application/json, application/x-ndjson, */*"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        if content_type:
            headers["Content-Type"] = content_type
        elif json_body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency:
            headers["Idempotency-Key"] = idempotency
        delay = 2
        last = None
        for _ in range(4):
            request = urllib.request.Request(url, data=raw, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.status, response.headers, response.read()
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                last = AgxError(f"{method} {path} -> HTTP {exc.code}: {payload[:300].decode('utf-8', 'replace')}", exc.code, payload.decode("utf-8", "replace"))
                if exc.code not in TRANSIENT:
                    raise last
                time.sleep(_retry_after(exc.headers, delay))
                delay = min(delay * 2, 30)
            except urllib.error.URLError as exc:
                last = AgxError(f"{method} {path} failed: {exc.reason}")
                time.sleep(delay)
        raise last or AgxError(f"{method} {path} failed")


def _retry_after(headers, default: int) -> int:
    value = headers.get("Retry-After", "")
    if str(value).isdigit():
        return max(1, int(value))
    return default


def loads(blob: bytes):
    if not blob:
        return {}
    try:
        return json.loads(blob.decode())
    except json.JSONDecodeError:
        return {"raw": blob.decode("utf-8", "replace")[:500]}


def new_key() -> str:
    return str(uuid.uuid4())
