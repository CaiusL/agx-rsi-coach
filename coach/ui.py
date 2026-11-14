"""Local console for oMLX reasoning and direct chat. Bind to 127.0.0.1 only.

    python3 -m coach ui
"""

from __future__ import annotations

import hmac
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from coach.config import ROOT, load
from coach.omlx import TRACE, _trace, request_payload
from coach import manual_jobs,series

STATE = ROOT / "state"
WEB = Path(__file__).resolve().parent / "web"
LOG = STATE / "coach.out"
DECISIONS = STATE / "decisions.log"
HOST = "127.0.0.1"
PORT = 8765

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?:([a-z]+): )?(.*)$")
_HEADER = re.compile(
    r"^(?P<at>\S+) (?P<game>\w+) (?P<mode>\w+) generation=(?P<gen>\d+) "
    r"model=(?P<model>\S+) via=(?P<via>.+)$"
)
_RACING_FIELDS = ("kv", "ld_min", "preview", "race_margin", "fuel_load_kg", "ballast_kg", "front_wing", "rear_wing")
_FPS_FIELDS = ("buy_style", "default_site", "expect_site", "execute_live_seconds", "ct_setup", "post_plant")
_BUSY = ("oMLX ", "read the public recording", "reflection accepted", "no improvement")
_IDLE = ("waiting for", "decision logged", "thought ", "rejected", "did not return",
         "unexpected error", "live '", "lost.", "won", "upload failed", "freeze is near")
_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
          ".json": "application/json", ".woff2": "font/woff2"}

_round_cache: dict = {"at": 0.0, "data": {}}
_round_lock = threading.Lock()
_chat_lock = threading.Lock()
_ui_cfg = None

_LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PIT WALL · sign in</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:#040d10;
         color:#e8f4f1; font:14px/1.5 -apple-system, system-ui, sans-serif; }
  form { width:min(360px, 88vw); padding:26px; border:1px solid #17454f; border-radius:14px;
         background:linear-gradient(168deg,#0b262e,#081c22); box-shadow:0 18px 44px -22px #000; }
  h1 { margin:0 0 4px; font:800 15px/1 ui-monospace, Menlo, monospace; letter-spacing:.19em; color:#c9ff3d; }
  p { margin:0 0 16px; color:#8db0b5; font-size:12px; }
  input { width:100%; box-sizing:border-box; padding:10px; border-radius:8px; border:1px solid #17454f;
          background:#06151a; color:#e8f4f1; font:500 13px ui-monospace, Menlo, monospace; }
  input:focus { outline:none; border-color:#c9ff3d; box-shadow:0 0 0 3px rgba(201,255,61,.12); }
  button { margin-top:12px; width:100%; padding:10px; border:0; border-radius:99px; cursor:pointer;
           background:linear-gradient(96deg,#c9ff3d,#59d2ff); color:#06170a;
           font:700 11px ui-monospace, Menlo, monospace; letter-spacing:.12em; text-transform:uppercase; }
</style>
<form method="get" action="/">
  <h1>PIT WALL</h1>
  <p>This console is private. Enter the access token from the host machine's .env (UI_TOKEN).</p>
  <input name="token" type="password" autocomplete="current-password" placeholder="access token" autofocus>
  <button type="submit">Open console</button>
</form>
""".encode("utf-8")


def _supplied_token(handler) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
    token = (query.get("token") or [""])[0]
    if token:
        return token
    for part in (handler.headers.get("Cookie") or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "pitwall_token":
            return value
    header = handler.headers.get("Authorization") or ""
    if header.startswith("Bearer "):
        return header[7:].strip()
    return ""


def _token_ok(handler) -> bool:
    if _ui_cfg is None or not _ui_cfg.ui_token:
        return True
    return hmac.compare_digest(_supplied_token(handler), _ui_cfg.ui_token)


def _tail(path: Path, limit: int = 400_000) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - limit))
        return handle.read().decode("utf-8", "replace")


def _events() -> list[dict]:
    rows = []
    for line in _tail(LOG, 180_000).splitlines():
        match = _LINE.match(line.strip())
        if not match:
            continue
        when, game, text = match.groups()
        rows.append({"at": when, "game": game or "coach", "text": text})
    return rows[-120:]


def _busy(events: list[dict], game: str) -> dict | None:
    own = [row for row in events if row["game"] == game]
    if not own:
        return None
    last = own[-1]
    text = last["text"]
    if any(mark in text for mark in _IDLE):
        return None
    if any(mark in text for mark in _BUSY):
        return {"since": last["at"], "text": text}
    return None


def _slot(game: str, record: dict,series_start=None) -> dict:
    strategy = record.get("strategy") or {}
    last = record.get("last") or {}
    fields = _RACING_FIELDS if game == "racing" else _FPS_FIELDS
    period=None
    if series_start and last.get('created_at'):
        try:period='trial' if series.timestamp(last['created_at'])<series.timestamp(series_start) else 'counted'
        except (ValueError,TypeError):pass
    facts=(last.get('recording') or {}).get('facts') or {};kda=None
    if game=='fps' and facts.get('kill_attribution'):
        version=None
        try:
            import uuid
            mid=str(uuid.UUID(str(last.get('match_id'))))
            played=json.loads((STATE/'logs'/'fps'/f'{mid}.json').read_text())
            version=(played.get('played_strategy') or {}).get('name')
        except (OSError,ValueError,TypeError):pass
        kda={'kills':facts.get('our_kills'),'deaths':facts.get('our_deaths'),'assists':facts.get('our_assists'),
             'kd_ratio':facts.get('our_kd_ratio'),'version':version}
    return {
        "name": strategy.get("name"),
        "thesis": strategy.get("thesis"),
        "live": bool(record.get("live_hash")),
        "hash": (record.get("live_hash") or "")[:12],
        "active_from": record.get("active_from"),
        "fields": {key: strategy.get(key) for key in fields if key in strategy},
        "last_match": last.get("match_id"),
        "last_at": last.get("created_at"),
        "rank": last.get("rank"),
        "score": last.get("seat_score"),
        "rounds": last.get("round_wins"),
        "kda": kda,
        "result_period":period,
    }


def _epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _decisions(limit: int = 14) -> list[dict]:
    out = []
    for chunk in _tail(DECISIONS, 200_000).split("=== ")[1:]:
        lines = chunk.splitlines()
        header = _HEADER.match(lines[0].strip())
        if not header:
            continue
        head = header.groupdict()
        item = {
            "id": "dec-" + head["at"], "kind": "decision", "source": "coach",
            "at": _epoch(head["at"]), "game": head["game"], "mode": head["mode"],
            "label": f"{head['game']} · {head['mode']} decision", "model": head["model"],
            "via": head["via"].strip(), "generation": int(head["gen"]),
            "state": "done", "changed": [], "overridden": [], "thesis": "",
            "sampling": {}, "note": "", "code": "",
        }
        section = None
        for line in lines[1:]:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("SAMPLING:"):
                raw = stripped[len("SAMPLING:"):].strip()
                brace, end = raw.find("{"), raw.rfind("}")
                if brace >= 0 and end > brace:
                    try:
                        item["sampling"] = json.loads(raw[brace:end + 1])
                    except json.JSONDecodeError:
                        pass
                streak = re.search(r"loss_streak=(\d+)", raw)
                if streak:
                    item["loss_streak"] = int(streak.group(1))
                section = None
            elif stripped.startswith("WHAT CHANGED"):
                section = "changed"
            elif stripped.startswith("WHY (oMLX thesis)"):
                section = "thesis"
            elif stripped.startswith("OVERRIDDEN BY THE HARNESS"):
                section = "overridden"
            elif stripped.startswith("LOCAL GATE:"):
                item["gate"] = stripped[len("LOCAL GATE:"):].strip()
                section = None
            elif stripped.startswith("HARNESS:"):
                item["note"] = stripped[len("HARNESS:"):].strip()
                section = None
            elif section == "thesis":
                item["thesis"] = (item["thesis"] + " " + stripped).strip()
            elif section in ("changed", "overridden"):
                if stripped.startswith("agent.py:"):
                    item["code"] = stripped[len("agent.py:"):].strip()
                else:
                    item[section].append(stripped)
        out.append(item)
    return out[-limit:]


def _reasoning(limit: int = 60) -> list[dict]:
    calls: dict[str, dict] = {}
    order: list[str] = []
    for line in _tail(TRACE, 3_000_000).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        cid = row.get("id")
        if not cid:
            continue
        if cid not in calls:
            calls[cid] = {"id": cid, "kind": "call", "state": "running", "thinking": "",
                          "content": "", "source": row.get("source", "coach"), "usage": {}}
            order.append(cid)
        calls[cid].update({k: v for k, v in row.items() if k != "event"})
        calls[cid]["state"] = {"start": "running", "done": "done", "error": "error"}.get(row.get("event"), "running")
        label = calls[cid].get("label") or ""
        calls[cid]["game"] = "racing" if label.startswith("racing") else "fps" if label.startswith("fps") else ""
    items = [calls[cid] for cid in order]
    items.extend(_decisions())
    items.sort(key=lambda row: row.get("at") or 0, reverse=True)
    return items[:limit]


def _coach_pid() -> int | None:
    try:
        out = subprocess.run(["pgrep", "-f", "coach run"], capture_output=True, text=True, timeout=4)
        pids = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
        return pids[0] if pids else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _omlx_models() -> dict:
    cfg = load()
    try:
        request = urllib.request.Request(cfg.omlx_url.rstrip("/") + "/models",
                                         headers={"Accept": "application/json", **({'Authorization':'Bearer '+cfg.omlx_key} if cfg.omlx_key else {})})
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.loads(response.read().decode())
        return {"ok": True, "models": [m.get("id") for m in data.get("data", []) if m.get("id")],
                "url": cfg.omlx_url, "default": cfg.omlx_model, "provider": cfg.llm_provider}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        return {"ok": False, "models": [cfg.omlx_model], "url": cfg.omlx_url, "default": cfg.omlx_model, "provider": cfg.llm_provider, "error": str(exc)[:200]}


def _rounds() -> dict:
    with _round_lock:
        if time.time() - _round_cache["at"] < 20:
            return _round_cache["data"]
    from coach.agx import Agx, AgxError
    from coach.loop import GAMES, loads
    cfg = load()
    client = Agx(cfg.base_url, cfg.api_key)
    data = {}
    for game, meta in GAMES.items():
        try:
            rnd = loads(client.get(f"/api/v1/competitions/{meta['competition_id']}/round")[2])
            comp = loads(client.get(f"/api/v1/competitions/{meta['competition_id']}")[2])
            info = rnd.get("next_round") or {}
            data[game] = {
                "round_state": rnd.get("round_state"),
                "next_start": info.get("start_at"),
                "interval": info.get("interval_seconds") or 7200,
                "scheduling_error": comp.get("scheduling_error"),
                "paused": comp.get("paused"),
                "match": bool(rnd.get("round_match")),
            }
        except (AgxError, OSError, ValueError) as exc:
            data[game] = {"error": str(exc)[:160]}
    with _round_lock:
        _round_cache["at"] = time.time()
        _round_cache["data"] = data
    return data


def draft_view(state: dict) -> dict | None:
    draft = state.get("draft") or {}
    if not draft.get("strategy"):
        return None
    from coach.loop import draft_id
    identity = draft_id(draft)
    report = draft.get("report") or (state.get("code_best") or {}).get("report") or ""
    game = "racing" if draft["strategy"].get("stops") else "fps"
    fields = _RACING_FIELDS if game == "racing" else _FPS_FIELDS
    return {"id": identity, "name": draft["strategy"].get("name"),
            "behavior": {k:v for k,v in (draft.get('behavior_assessment') or {}).items() if k in ('status','calls','changed_actions','gains','baseline','candidate','holdout_traces','evaluation_version')},
            "validation_stage": draft.get('validation_stage'), "match_validated": draft.get('match_validated',False),
            "series_version":draft.get('series_version'),"release_files":draft.get('release_files'),
            "fields": {key: draft["strategy"].get(key) for key in fields if key in draft["strategy"]},
            "experiment": draft.get("experiment"),
            "revision": draft.get("revision") or 1, "ready": draft.get("ready", True),
            "passed": report.count("PASS"), "total": report.count("PASS") + report.count("FAIL"),
            "uploaded": draft.get("uploaded_id") == identity, "at": draft.get("at")}


def board() -> dict:
    events = _events()
    games = {}
    for game in ("racing", "fps"):
        path = STATE / f"{game}.json"
        try:
            state = json.loads(path.read_text()) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        phase = state.get("phase") or "play_A"
        slot = "B" if phase == "play_B" else "A"
        record = state.get(slot) or {}
        games[game] = {
            "phase": phase, "generation": state.get("generation"),
            "watching": slot, "loss_streak": state.get("loss_streak") or 0,
            "loss_streak_info": state.get('loss_streak_info'),
            "counted_series":series.view(state),
            "active_from": record.get("active_from"),
            "last_seen_at": (record.get("last") or {}).get("created_at"),
            "busy": _busy(events, game),
            "draft": draft_view(state), "manual_review": bool(state.get("manual_review")),
            "session": {k:v for k,v in (state.get('session') or {}).items() if k != 'baseline'},
            "manual_pending": (STATE / f"manual_improve_{game}.json").exists(),
            "manual_running": manual_jobs.running(game),
            "manual_result": (state.get("manual_result")
                              if (state.get("manual_result") or {}).get("game") == game else None),
            "A": _slot(game,state.get('A') or {},(state.get('counted_series') or {}).get('starts_at')),
            "B": _slot(game,state.get('B') or {},(state.get('counted_series') or {}).get('starts_at')),
        }
    return {
        "at": time.time(),
        "clock": datetime.now().astimezone().strftime("%H:%M:%S"),
        "events": events, "games": games, "rounds": _rounds(),
        "coach_pid": _coach_pid(), "omlx": _omlx_models(),
    }


def context_bundle() -> dict:
    """Real coach state the chat can attach, so answers are about this run."""
    games = {}
    for game in ("racing", "fps"):
        path = STATE / f"{game}.json"
        try:
            state = json.loads(path.read_text()) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        games[game] = {
            "phase": state.get("phase"), "generation": state.get("generation"),
            "loss_streak": state.get("loss_streak"),
            "loss_streak_info": state.get('loss_streak_info'),
            "draft": draft_view(state), "manual_review": bool(state.get("manual_review")),
            "session": {k:v for k,v in (state.get('session') or {}).items() if k != 'baseline'},
            "racing_experiment": state.get("racing_experiment") if game == "racing" else None,
            "racing_assessment": state.get("racing_assessment") if game == "racing" else None,
            "A": _slot(game, state.get("A") or {}),
            "B": _slot(game, state.get("B") or {}),
        }
    return {"games": games, "decisions": _decisions(3), "rounds": _rounds(), "events": _events()[-25:]}


def _stream_chat(handler, payload: dict) -> None:
    cfg = load()
    model = payload.get("model") or cfg.omlx_model
    messages = payload.get("messages") or []
    sampling = payload.get("sampling") or {}
    think = bool(payload.get("think"))
    max_tokens = int(payload.get("max_tokens") or 1500)
    body = request_payload(cfg, model, messages, max_tokens, sampling, think, stream=True)
    request = urllib.request.Request(
        cfg.omlx_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                 **({"Authorization": "Bearer " + cfg.omlx_key} if cfg.omlx_key else {})},
        method="POST",
    )
    started = time.time()
    thought: list[str] = []
    visible: list[str] = []
    usage: dict = {}
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True

    def emit(event: dict) -> bool:
        try:
            handler.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
            handler.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    emit({"type": "open", "model": model, "think": think})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or choice.get("message") or {}
                    piece = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if piece:
                        thought.append(piece)
                        if not emit({"type": "thinking", "text": piece}):
                            return
                    piece = delta.get("content") or ""
                    if piece:
                        visible.append(piece)
                        if not emit({"type": "content", "text": piece}):
                            return
    except (urllib.error.URLError, OSError, ValueError) as exc:
        emit({"type": "error", "error": f"oMLX stream failed: {str(exc)[:200]}"})
    joined_thought, joined_visible = "".join(thought), "".join(visible)
    if not joined_thought and not joined_visible:
        emit({"type": "error", "error": "oMLX returned no stream content. Turn streaming off and retry."})
    emit({"type": "done", "seconds": round(time.time() - started, 1), "usage": usage,
          "thinking_chars": len(joined_thought), "content_chars": len(joined_visible)})
    _trace({
        "event": "done", "id": "chat-%d" % int(started * 1000), "at": time.time(), "source": "chat",
        "label": "direct chat", "model": model, "think": think, "sampling": sampling,
        "max_tokens": max_tokens, "seconds": round(time.time() - started, 1),
        "thinking": joined_thought[:60000], "thinking_chars": len(joined_thought),
        "content": joined_visible[:60000], "content_chars": len(joined_visible),
        "usage": usage, "prompt_chars": sum(len(str(m.get("content") or "")) for m in messages),
    })


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body: bytes, kind: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data: dict, status: int = 200) -> None:
        self._send(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not _token_ok(self):
            self._send(_LOGIN_PAGE, "text/html; charset=utf-8", 401)
            return
        if path == "/" and "token=" in self.path:
            self.send_response(302)
            self.send_header("Set-Cookie", f"pitwall_token={_supplied_token(self)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/board":
            self._json(board())
            return
        if path == "/api/reasoning":
            self._json({"items": _reasoning(), "at": time.time()})
            return
        if path == "/api/context":
            self._json(context_bundle())
            return
        if path == "/api/models":
            self._json(_omlx_models())
            return
        target = WEB / "index.html" if path in ("/", "/index.html") else (WEB / path.lstrip("/")).resolve()
        if not str(target).startswith(str(WEB.resolve())):
            self._send(b"forbidden", "text/plain; charset=utf-8", 403)
            return
        if not target.is_file():
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return
        self._send(target.read_bytes(), _TYPES.get(target.suffix, "application/octet-stream"))

    def do_POST(self):
        if not _token_ok(self):
            self._json({"error": "unauthorized"}, 401)
            return
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 2_000_000:
            self._json({"error": "empty or oversized request"}, 400)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "invalid JSON"}, 400)
            return
        if path == "/api/improve":
            self._handle_improve(payload)
            return
        if path.startswith("/api/games/") and path.rsplit("/",1)[-1] in ("improve", "upload", "session", "stop-session"):
            game = path.split("/")[3]
            if payload.get("game") != game:
                self._json({"error": "URL game and request game must match"}, 400)
                return
            if path.endswith("/upload"):
                self._handle_upload(payload)
            elif path.endswith('/session') or path.endswith('/stop-session'):
                try:
                    action = 'session_stop' if path.endswith('/stop-session') else 'session_start'
                    req = manual_jobs.enqueue(game, request_id=payload.get('request_id'), action=action)
                    self._json({'ok':True, 'queued':True, **req})
                except ValueError as exc:
                    self._json({'error':str(exc)}, 409)
            else:
                self._handle_improve(payload)
            return
        if path != "/api/chat":
            self._json({"error": "unknown endpoint"}, 404)
            return
        if not isinstance(payload.get("messages"), list) or not payload["messages"]:
            self._json({"error": "messages required"}, 400)
            return
        with _chat_lock:
            _stream_chat(self, payload)

    def _handle_improve(self, payload: dict) -> None:
        """Queue an on-demand sim + improvement. The coach consumes the flag in its
        own tick, so state is never mutated from two processes at once."""
        game = payload.get("game")
        if game not in ("racing", "fps"):
            self._json({"error": "game must be racing or fps"}, 400)
            return
        request_id = payload.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or len(request_id)>100):
            self._json({"error": "invalid request id"}, 400)
            return
        try:
            req = manual_jobs.enqueue(game, False, request_id, action="improve")
        except ValueError as exc:
            self._json({"error": str(exc)}, 409)
            return
        except OSError as exc:
            self._json({"error": f"could not queue request: {exc}"}, 500)
            return
        self._json({"ok": True, "queued": True, **req})

    def _handle_upload(self, payload: dict) -> None:
        game, selected_id = payload.get("game"), payload.get("draft_id")
        if game not in ("racing", "fps"):
            self._json({"error": "game must be racing or fps"}, 400)
            return
        try:
            state = json.loads((STATE / f"{game}.json").read_text())
            draft = draft_view(state)
            if not draft or not draft["ready"] or draft["id"] != selected_id:
                self._json({"error": "Select the current validated draft before uploading."}, 409)
                return
            req = manual_jobs.enqueue(game, True, payload.get("request_id"), action="upload", draft_id=selected_id)
        except ValueError as exc:
            self._json({"error": str(exc)}, 409)
            return
        except OSError as exc:
            self._json({"error": f"could not queue upload: {exc}"}, 500)
            return
        self._json({"ok": True, "queued": True, **req})

    def log_message(self, fmt, *args):
        return


def _lan_ip() -> str:
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def serve(port: int = PORT) -> None:
    global _ui_cfg
    _ui_cfg = load()
    host = _ui_cfg.ui_host
    if host not in ("127.0.0.1", "localhost", "::1") and not _ui_cfg.ui_token:
        raise SystemExit("UI_HOST is not loopback: set UI_TOKEN in .env before exposing the console.")
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    gate = " (access token required)" if _ui_cfg.ui_token else ""
    if host in ("0.0.0.0", "::"):
        print(f"oMLX console at http://127.0.0.1:{port} and http://{_lan_ip()}:{port}{gate}", flush=True)
    else:
        print(f"oMLX console at http://{host}:{port}{gate}", flush=True)
    server.serve_forever()
