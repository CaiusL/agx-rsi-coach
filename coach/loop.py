"""Watch both competitions, keep A as the best, and submit B as its counter."""

from __future__ import annotations

import hashlib
import ast
import fcntl
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coach.agx import Agx, AgxError, loads, new_key
from coach.bundle import RUNTIME, build
from coach.config import ROOT, Config
from coach.omlx import OmlxError, OmlxTransportError, _parse_json, chat_json, chat_text, parse_blocks
from coach.prompts import book_messages, code_messages, plan_messages, repair_messages, behavior_messages
from coach import recording, manual_jobs, training, sessions, outcomes, fps_evaluation, fps_combat, fps_workflow, fps_reference, series, release
from coach.sim import sim_insight, racing_assessment
from coach import racing_workflow
from coach.validate import check
from coach.strategies import (
    GAMES,
    canonical,
    caused_change,
    clamp,
    fps_measurement_text,
    fps_score,
    fps_snapshot,
    seed,
    stop_kind,
)

STATE = ROOT / "state"
LEDGER = STATE / "uploads.json"
DECISIONS = STATE / "decisions.log"


def log(message: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    line = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S ") + message
    print(line, flush=True)
    with (STATE / "coach.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def run_forever(cfg: Config) -> None:
    coach_lock=acquire_coach_lock()  # Keep the descriptor alive for the entire loop.
    manual_jobs.recover_interrupted()
    from coach.omlx import close_stale_calls
    closed = close_stale_calls()
    if closed:
        log(f"closed {closed} interrupted model call(s) left open by a previous process")
    log(
        f"coach running, poll {cfg.poll_seconds}s, oMLX {cfg.omlx_model}. "
        "Rounds are read from AGX (currently a 2-hour window)."
    )
    while True:
        # A manual click runs its requested game before unrelated automatic polling.
        requested = [g for g in GAMES if (STATE / f"manual_improve_{g}.json").exists()]
        # Releases run before local generation in either game.
        games = game_order(requested)
        for game in games:
            try:
                log(f"{game}: {tick(cfg, game)}")
            except (AgxError, OmlxError) as exc:
                log(f"{game}: {exc}")
            except Exception as exc:
                log(f"{game}: unexpected error: {exc}")
        active = any((load_state(g).get('session') or {}).get('enabled') for g in GAMES)
        time.sleep(min(cfg.poll_seconds, 5) if active else cfg.poll_seconds)


def acquire_coach_lock():
    STATE.mkdir(parents=True,exist_ok=True)
    handle=(STATE/'coach.lock').open('a+')
    try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError('Another coach already owns this state directory') from None
    (STATE/'coach.pid').write_text(str(os.getpid()))
    return handle


def game_order(requested,now=None):
    states={game:load_state(game) for game in GAMES}
    due=[game for game,state in states.items() if (state.get('session') or {}).get('enabled') and
        sessions.stage(state['session'],now) in ('upload','settling')]
    games=list(dict.fromkeys(due+list(requested or GAMES)))
    return sorted(games,key=lambda game:(game not in due,(states[game].get('session') or {}).get('upload_at','9999')))


def tick_all(cfg: Config) -> None:
    for game in GAMES:
        try:
            log(f"{game}: {tick(cfg, game)}")
        except (AgxError, OmlxError) as exc:
            log(f"{game}: {exc}")
        except Exception as exc:
            log(f"{game}: unexpected error: {exc}")


def tick(cfg: Config, game: str) -> str:
    key_a = cfg.account_key(game, "A")
    key_b = cfg.account_key(game, "B")
    if not key_a:
        return "no API key. Copy .env.example to .env and set AGX_API_KEY."
    state = load_state(game)
    from coach import match_retention
    if match_retention.latest_only():
        state['data_retention'] = match_retention.prune(game)
    if getattr(cfg,'counted_start_at','') and series.configure(state,cfg.counted_start_at):save_state(game,state)
    state['outcome_account'] = _ident(key_a)
    slots = ('A',) if key_b and key_b != key_a else ('A','B')
    if outcomes.bootstrap(state,game,STATE/'logs'/game,slots):
        save_state(game,state)
    flag = STATE / f"manual_improve_{game}.json"
    if flag.exists():
        req = manual_jobs.claim(game)
        if req is None:
            return "manual request rejected: its game did not match the queue"
        log(f"{game}: manual request {req['request_id']} started (game={req['game']})")
        error = None
        try:
            client = Agx(cfg.base_url, key_a)
            action = req.get("action", "improve")
            if action == "upload":
                upload_draft(cfg, game, state, client, req.get("draft_id"), request_id=req['request_id'])
                if state.get('session'):
                    state['session'].update(enabled=False, status='stopped_by_manual_upload')
                    save_state(game, state)
            elif action in ('session_start', 'session_stop'):
                if action == 'session_stop':
                    state.setdefault('session', {}).update(enabled=False, status='stopped')
                else:
                    from coach.round_control import require_model_allowed
                    require_model_allowed(state)
                    info = round_payload(client, GAMES[game]['competition_id']).get('next_round') or {}
                    current = state.get('session') or {}
                    if not current.get('enabled'):
                        state['session'] = sessions.new(info)
                        state['session']['baseline'] = session_baseline(state)
                        watched = 'B' if state.get('phase') == 'play_B' else 'A'
                        state['session']['source_match'] = (state[watched].get('last') or {}).get('match_id')
                    state['manual_review'] = True
                save_state(game, state)
            elif action == "improve":
                deadlines=[]
                now=datetime.now(timezone.utc)
                for peer_game in GAMES:
                    peer=load_state(peer_game).get('session') or {}
                    if peer.get('enabled') and sessions.stage(peer,now) in ('improve','stalled','ready','settling','upload'):
                        deadlines.append(peer['model_stop_at'])
                try:
                    if deadlines:
                        with sessions.until(min(deadlines)):
                            manual_improve(cfg,game,state,client,upload=False,request_id=req['request_id'])
                    else:manual_improve(cfg,game,state,client,upload=False,request_id=req['request_id'])
                except sessions.Deadline:
                    from coach.omlx import close_stale_calls
                    close_stale_calls('manual generation stopped for scheduled release')
                    state=load_state(game)
                    state['manual_result']={'game':game,'action':'improve','request_id':req['request_id'],
                        'status':'release_cutoff','at':datetime.now(timezone.utc).isoformat(),
                        'error':'Model work stopped to protect scheduled release; selected draft retained.'}
                    save_state(game,state)
            else:
                raise ValueError("Unknown manual action")
            return f"manual request {req['request_id']} complete (game={game}, action={action})"
        except Exception as exc:
            error = exc
            current = load_state(game)
            if req.get('action')=='upload' and (current.get('manual_result') or {}).get('status')=='confirmation_required':
                raise
            current["manual_result"] = {"game": game, "action": req.get("action", "improve"),
                "request_id": req["request_id"], "status": "data_wait" if isinstance(exc,(AgxError,OmlxTransportError,fps_evaluation.EvidenceUnavailable)) else "upload_failed" if req.get("action") == "upload" else "improve_failed",
                "error": str(exc), "at": datetime.now(timezone.utc).isoformat()}
            save_state(game, current)
            raise
        finally:
            manual_jobs.finish(req, error)
    from coach.round_control import reservation
    reserved = reservation(state)
    if reserved:
        # Watching and a pinned timed release remain available; never run a model.
        if (state.get('session') or {}).get('enabled'):
            state['session']['readiness_locked'] = True
            return session_step(cfg, game, state, Agx(cfg.base_url, key_a))
        return watch_manual_game(cfg, game, state, Agx(cfg.base_url, key_a))
    if (state.get('session') or {}).get('enabled'):
        return session_step(cfg, game, state, Agx(cfg.base_url, key_a))
    if state.get("manual_review"):
        return watch_manual_game(cfg, game, state, Agx(cfg.base_url, key_a))
    if key_b and key_b != key_a:
        return tick_dual(cfg, game, state, key_a, key_b)
    return tick_single(cfg, game, state, key_a)


def dry_run(cfg: Config) -> None:
    """Ask oMLX for a counter and save it locally. Does not upload."""
    for game in GAMES:
        state = load_state(game)
        best = state["A"]["strategy"]
        proposal = propose(cfg, game, best, report(state))
        state["B"]["strategy"] = proposal
        save_state(game, state)
        log(f"{game} dry-run B '{proposal['name']}': {proposal['thesis']}")


def status(cfg: Config) -> None:
    public = Agx(cfg.base_url)
    for game, meta in GAMES.items():
        state = load_state(game) if (STATE / f"{game}.json").is_file() else None
        try:
            window = public_round(public, meta["competition_id"])
        except AgxError as exc:
            window = str(exc)
        phase = state.get("phase") if state else "no local state"
        generation = state.get("generation") if state else "-"
        print(f"{meta['title']}: phase={phase} generation={generation}")
        print(f"  next round: {window}")
        if not state:
            continue
        ours = (state.get("checkpoint") or {}).get("ours") or {}
        if ours:
            print(
                f"  checkpoint: rank {ours.get('rank')} "
                f"rating {ours.get('rating')} of {state['checkpoint'].get('field_size')}"
            )
        for slot in ("A", "B"):
            last = state[slot].get("last")
            name = (state[slot].get("strategy") or {}).get("name")
            rank = None if not last else last.get("rank", last.get("seat_score"))
            code = state[slot].get("code")
            written = f" code={len(code.encode())}B" if code else ""
            print(f"  {slot} {name} live={bool(state[slot].get('live_hash'))} last={rank}{written}")
        gate = state.get("gate")
        if gate:
            summary = (gate.get("report") or "").splitlines()[-1]
            print(f"  local gate: {'pass' if gate.get('ok') else 'fail'} — {summary}")


def tick_single(cfg: Config, game: str, state: dict, api_key: str) -> str:
    client = Agx(cfg.base_url, api_key)
    studied = ensure_checkpoint(cfg, game, state, client)
    ensure_plan_b(cfg, game, state, client)
    promote_passing(cfg, game, state, client)
    phase = state.get("phase", "play_A")
    if phase == "refine":
        return refine_step(cfg, game, state, client)
    if phase == "play_A":
        if not ensure_live(cfg, game, state, client, "A", shared=True):
            save_state(game, state)
            return "best is not live yet"
        if state.get("pending_b") and quota_open(cfg, client.api_key):
            if ensure_live(cfg, game, state, client, "B", shared=True):
                state["pending_b"] = False
                state["phase"] = "play_B"
                save_state(game, state)
                return f"Plan B '{state['B']['strategy']['name']}' is now live for the next round"
        match, rnd = fresh_match(client, game, state["A"], state)
        if not match:
            save_state(game, state)
            return _with_checkpoint(studied, state, waiting(rnd, "best"))
        remember(state["A"], match, game)
        attach_standings(client, game, state["A"]["last"])
        played = _copy(state["A"].get("strategy"))
        save_state(game, state)
        if we_won(game, state["A"]["last"]):
            note_history(state, "A")
            outcomes.sync(state,game,[state['A']['last']])
            report = write_plan(cfg, game, state, client, "win", state["A"]["last"], counter=state["B"].get("last"))
            state["generation"] = state.get("generation", 1) + 1
            if game == "racing":
                uploaded = stage_plan_b(cfg, game, state, client, shared=True)
                action = "submitted a faster revision" if uploaded else "faster revision is written and the upload is waiting"
                log_game(
                    cfg, game, state, "A", match, client,
                    played=played, decision="win", mode="win", evidence_text=report, action=action,
                )
                if not uploaded:
                    return "won. The faster revision is written; the upload is waiting"
                return (
                    f"won with '{state['A']['strategy']['name']}'. "
                    f"The faster revision '{state['B']['strategy']['name']}' runs next."
                )
            state["phase"] = "play_A"
            save_state(game, state)
            log_game(
                cfg, game, state, "A", match, client,
                played=played, decision="win", mode="win", evidence_text=report,
                action="kept the best",
            )
            return (
                f"won with '{state['A']['strategy']['name']}'. "
                f"Plan B '{state['B']['strategy']['name']}' is ready if the next one loses."
            )
        note_history(state, "A")
        outcomes.sync(state,game,[state['A']['last']])
        report = write_plan(cfg, game, state, client, "loss", state["A"]["last"])
        if game == "fps":
            rnd = round_payload(client, GAMES[game]["competition_id"])
            deadline = refine_deadline(rnd)
            state["refine"] = {
                "deadline": deadline.isoformat() if deadline else None,
                "focus": _copy(state["A"]["last"]),
                "mode": "loss",
                "iters": 0,
            }
            state["phase"] = "refine"
            save_state(game, state)
            log_game(
                cfg, game, state, "A", match, client,
                played=played, decision="loss", mode="loss", evidence_text=report,
                action="refining the FPS candidate before the submit cutoff",
            )
            until = deadline.isoformat() if deadline else "the gate passes"
            return f"lost. Refining the FPS candidate until {until}."
        uploaded = stage_plan_b(cfg, game, state, client, shared=True)
        action = "submitted Plan B" if uploaded else "Plan B is written and the upload is waiting"
        log_game(
            cfg, game, state, "A", match, client,
            played=played, decision="loss", mode="loss", evidence_text=report, action=action,
        )
        if not uploaded:
            return "lost. Plan B is written; the upload is waiting"
        return f"lost. Submitted Plan B '{state['B']['strategy']['name']}' for the next round"
    if not ensure_live(cfg, game, state, client, "B", shared=True):
        save_state(game, state)
        return "Plan B is not live yet"
    match, rnd = fresh_match(client, game, state["B"], state)
    if not match:
        save_state(game, state)
        return waiting(rnd, "Plan B")
    remember(state["B"], match, game)
    attach_standings(client, game, state["B"]["last"])
    return finish_plan_b(cfg, game, state, client, shared=True, match=match)


def tick_dual(cfg: Config, game: str, state: dict, key_a: str, key_b: str) -> str:
    client_a = Agx(cfg.base_url, key_a)
    client_b = Agx(cfg.base_url, key_b)
    studied = ensure_checkpoint(cfg, game, state, client_a)
    ensure_plan_b(cfg, game, state, client_a)
    if not ensure_live(cfg, game, state, client_a, "A", shared=False):
        save_state(game, state)
        return "account A is not live yet"
    if not ensure_live(cfg, game, state, client_b, "B", shared=False):
        save_state(game, state)
        return "account B is not live yet"
    match_a, rnd = fresh_match(client_a, game, state["A"], state)
    match_b, _rnd = fresh_match(client_b, game, state["B"], state)
    if not match_a or not match_b:
        save_state(game, state)
        missing = "best" if not match_a else "Plan B"
        return _with_checkpoint(studied, state, waiting(rnd, missing) + " (both accounts must finish a round)")
    remember(state["A"], match_a, game)
    remember(state["B"], match_b, game)
    attach_standings(client_a, game, state["A"]["last"])
    attach_standings(client_b, game, state["B"]["last"])
    outcomes.sync(state,game,[state['A']['last']])
    played_a = _copy(state["A"].get("strategy"))
    played_b = _copy(state["B"].get("strategy"))
    a_won = we_won(game, state["A"]["last"])
    b_won = we_won(game, state["B"]["last"])
    mode = "win" if a_won or b_won else "loss"
    if b_won and not a_won:
        focus, counter = state["B"]["last"], state["A"].get("last")
    else:
        focus, counter = state["A"]["last"], state["B"].get("last")
    if choose(game, state["A"]["last"], state["B"]["last"]) == "B":
        adopt(state, "B")
    note_history(state, "B" if b_won else "A")
    report = write_plan(cfg, game, state, client_a, mode, focus, counter=counter)
    state["phase"] = "play_A"
    state["generation"] = state.get("generation", 1) + 1
    save_state(game, state)
    ensure_live(cfg, game, state, client_a, "A", shared=False)
    ensure_live(cfg, game, state, client_b, "B", shared=False)
    save_state(game, state)
    action = f"generation {state['generation']}: best is '{state['A']['strategy']['name']}'"
    log_game(
        cfg, game, state, "A", match_a, client_a,
        played=played_a, decision="win" if a_won else "loss", mode=mode,
        evidence_text=report, action=action,
    )
    log_game(
        cfg, game, state, "B", match_b, client_b,
        played=played_b, decision="win" if b_won else "loss", mode=mode,
        evidence_text=report, action=action,
    )
    return (
        f"generation {state['generation']}: best is '{state['A']['strategy']['name']}', "
        f"Plan B is '{state['B']['strategy']['name']}' ({mode})"
    )


def finish_plan_b(cfg, game, state, client, shared: bool, match: dict) -> str:
    """Plan B has a result. A win keeps it. A loss restores the best and writes a new Plan B."""
    played = _copy(state["B"].get("strategy"))
    focus = state["B"]["last"]
    previous_best = json.loads(json.dumps(state["A"]["last"])) if state["A"].get("last") else None
    won = we_won(game, focus)
    outcomes.sync(state,game,[focus])
    better = choose(game, state["A"]["last"], focus)
    if better == "B":
        adopt(state, "B")
    else:
        state["A"]["live_hash"] = None
    note_history(state, "B" if won or better == "B" else "A")
    state["generation"] = state.get("generation", 1) + 1
    if won:
        counter = previous_best if better == "B" else state["B"].get("last")
        report = write_plan(cfg, game, state, client, "win", focus, counter=counter, played=played)
        if game == "racing":
            uploaded = stage_plan_b(cfg, game, state, client, shared=shared)
            action = "submitted a faster revision" if uploaded else "faster revision is written and the upload is waiting"
            log_game(
                cfg, game, state, "B", match, client,
                played=played, decision="win", mode="win", evidence_text=report, action=action,
            )
            if not uploaded:
                return "won. The faster revision is written; the upload is waiting"
            return (
                f"won with '{state['A']['strategy']['name']}'. "
                f"The faster revision '{state['B']['strategy']['name']}' runs next."
            )
        state["phase"] = "play_A"
        save_state(game, state)
        if state["A"].get("live_hash") != digest(state["A"]["strategy"], state["A"].get("code")):
            ensure_live(cfg, game, state, client, "A", shared=shared)
        save_state(game, state)
        log_game(
            cfg, game, state, "B", match, client,
            played=played, decision="win", mode="win", evidence_text=report,
            action="Plan B is now the best" if better == "B" else "kept the previous best",
        )
        return (
            f"Plan B won and is now the best ('{state['A']['strategy']['name']}'). "
            f"The next Plan B '{state['B']['strategy']['name']}' waits for a loss."
        )
    report = write_plan(cfg, game, state, client, "loss", focus, played=played)
    if game == "fps":
        deadline = refine_deadline(round_payload(client, GAMES[game]["competition_id"]))
        state["refine"] = {"deadline": deadline.isoformat() if deadline else None,
                           "focus": _copy(focus), "mode": "loss", "iters": 0}
        state["phase"] = "refine"
        save_state(game, state)
        log_game(cfg, game, state, "B", match, client, played=played, decision="loss",
                 mode="loss", evidence_text=report, action="refining FPS candidate")
        return "Plan B lost; refining a replacement against the expanded gate."
    state["phase"] = "play_A"
    save_state(game, state)
    ensure_live(cfg, game, state, client, "A", shared=shared)
    save_state(game, state)
    log_game(
        cfg, game, state, "B", match, client,
        played=played, decision="loss", mode="loss", evidence_text=report,
        action="Plan B scored better and is now the best" if better == "B" else "restored the previous best",
    )
    return (
        f"Plan B lost. Restored '{state['A']['strategy']['name']}'. "
        f"New Plan B '{state['B']['strategy']['name']}' waits for the next loss."
    )


def stage_plan_b(cfg, game, state, client, shared: bool) -> bool:
    """Put the written Plan B live for the next window. A stays the restore point."""
    if game == "fps" and state.get("plan_ready") is False:
        log("fps: no validated replacement; no upload")
        return False
    state["phase"] = "play_B"
    state["B"]["agent_id"] = state["A"].get("agent_id")
    save_state(game, state)
    uploaded = ensure_live(cfg, game, state, client, "B", shared=shared)
    if not uploaded:
        # A is still the live bundle. Keep reading its results instead of stalling
        # in play_B until the upload cap resets.
        state["phase"] = "play_A"
        state["pending_b"] = True
    save_state(game, state)
    return uploaded


def draft_id(draft: dict) -> str:
    return digest(draft["strategy"], draft.get("code"))


def session_baseline(state):
    if state.get('draft'): return _copy(state['draft'])
    slot = 'B' if state.get('phase') == 'play_B' else 'A'
    result = {'strategy':_copy(state[slot]['strategy']), 'code':state[slot].get('code'), 'ready':True}
    result['id'] = draft_id(result)
    return result


def functional_draft_key(draft):
    book = {k:v for k,v in (draft.get('strategy') or {}).items() if k not in ('name','thesis')}
    return digest(book, draft.get('code'))


def session_step(cfg, game, state, client):
    """One local cycle, or one immutable scheduled upload. No auto submission mid-cycle."""
    session = state['session']
    now = datetime.now(timezone.utc)
    watched = 'B' if state.get('phase') == 'play_B' else 'A'
    session.setdefault('source_match', (state[watched].get('last') or {}).get('match_id'))
    prompt_version = hashlib.sha256(Path(__file__).with_name('prompts.py').read_bytes()).hexdigest()[:16]
    if session.get('prompt_version') != prompt_version:
        session.update(prompt_version=prompt_version, no_progress=0)
    focus=(state.get(watched) or {}).get('last') or {}
    if focus.get('match_id'):
        focus['training']=training.read(client,game,focus,background=True)
        from coach import fps_match_data, racing_match_data
        collector=fps_match_data if game=='fps' else racing_match_data
        focus['public_analysis']=collector.read(client,focus,background=True)
        analysis=focus.get('public_analysis') or {}
        if analysis.get('status')=='verified':
            version=f'{analysis.get("match_id")}:{analysis.get("schema")}'
            if session.get('public_analysis_version')!=version:
                session.update(public_analysis_version=version,no_progress=0)
                session.pop('retry_at',None)
    if game == 'fps':
        corpus_version=hashlib.sha256(json.dumps([(str(p),sha) for _,p,sha in fps_evaluation.corpus(focus)]).encode()).hexdigest()[:16]
        if session.get('corpus_version') not in (None,corpus_version):
            session['no_progress']=0
            session.pop('retry_at',None)
        session['corpus_version']=corpus_version
    stage = sessions.stage(session, now)
    if stage == 'off': return 'Session stopped.'
    if stage == 'complete':
        if session.get('status') == 'uploading':
            confirmed = (state.get('draft') or {}).get('uploaded_id') == session.get('selected_id')
            session.update(status='uploaded' if confirmed else 'upload_failed')
            if not confirmed: session['error'] = 'Release was interrupted; review AGX admission before retrying.'
            save_state(game, state)
        if session.get('next_watch_at','') > now.isoformat():
            return f"Session {session['status']}; waiting for a new played result."
        session['next_watch_at'] = (now+timedelta(seconds=60)).isoformat()
        # Wait for a NEW played result before starting another round's session.
        message = watch_manual_game(cfg, game, state, client)
        result = state['B' if state.get('phase') == 'play_B' else 'A'].get('last') or {}
        mid = result.get('match_id')
        observed=state.get('round_observation') or {}
        if game=='fps' and observed.get('round_state')=='failed' and not observed.get('match_assigned'):
            try:
                current_observation=sessions.timestamp(observed['observed_at'])>=sessions.timestamp(session['round_at'])
                newer_window=sessions.timestamp(observed['next_round']['start_at'])>sessions.timestamp(session['round_at'])
                if current_observation and newer_window and now>=sessions.timestamp(session['round_at']):
                    session['void_round_observed']=True
            except (KeyError,TypeError,ValueError):pass
        try:belongs_to_round=sessions.timestamp(result.get('created_at','')) >= sessions.timestamp(session['round_at'])
        except (ValueError,TypeError):belongs_to_round=False
        if mid and mid != session.get('source_match') and belongs_to_round and now >= sessions.timestamp(session['round_at']):
            info = round_payload(client, GAMES[game]['competition_id']).get('next_round') or {}
            fresh = sessions.new(info)
            fresh['baseline'] = session_baseline(state)
            fresh['source_match'] = mid
            state['session'] = fresh; save_state(game, state)
        elif game=='fps' and session.get('void_round_observed'):
            # A failed scheduler round has no played result. Wait for a genuinely
            # published future window instead of stalling forever or inventing a loss.
            info=observed.get('next_round') or round_payload(client,GAMES[game]['competition_id']).get('next_round') or {}
            if info.get('start_at') and sessions.timestamp(info['start_at']) > now:
                fresh=sessions.new(info)
                if sessions.timestamp(fresh['model_stop_at']) > now:
                    fresh.update(baseline=session_baseline(state),source_match=mid,previous_void_session=session['id'])
                    state['session']=fresh
                    message=f"AGX failed to assign the preceding FPS round; re-armed for published round {fresh['round_at']} without recording a loss."
            save_state(game,state)
        else: save_state(game, state)
        return message
    if stage == 'missed':
        if game=='fps':
            info=round_payload(client,GAMES[game]['competition_id'])
            if info.get('round_state')=='failed':session['void_round_observed']=True
        session['status'] = 'missed_window'; save_state(game, state)
        return 'Session missed the 10–15 minute release window; retained draft without late upload.'
    if stage == 'upload':
        draft = state.get('draft') or {}
        session.update(status='uploading', upload_started=now.isoformat(), selected_id=draft.get('id'))
        save_state(game, state)  # Persist intent before network: restarts cannot choose a second candidate.
        if not draft.get('ready') or draft.get('uploaded_id') == draft.get('id'):
            if draft.get('ready') and draft.get('uploaded_id')==draft.get('id') and state[watched].get('live_hash')==draft.get('id'):
                series.admitted(state,draft)
            session['status'] = 'no_candidate'; save_state(game, state)
            return 'Session ended; no new validated draft to upload.'
        try:
            # Reserve time for the other game's release on the single coach.
            deadline = min(now+timedelta(seconds=50), sessions.timestamp(session['round_at'])-timedelta(minutes=10)).isoformat()
            with sessions.until(deadline):
                success = upload_draft(cfg, game, state, client, draft['id'], request_id=session['id'])
            session['status'] = 'uploaded' if success else 'upload_failed'
        except sessions.Deadline:
            session.update(status='confirmation_required', error='Release deadline interrupted confirmation. Remote admission/publication may have completed; inspect the saved release attempt before retrying.')
        except Exception as exc:
            uncertain=(state.get('manual_result') or {}).get('status')=='confirmation_required'
            session.update(status='confirmation_required' if uncertain else 'upload_failed',error=str(exc))
        save_state(game, state)
        return f"Session release {session['status']}; selected one draft for {session['round_at']}."
    if stage in ('settling', 'stalled','ready'):
        session['status'] = stage; save_state(game, state)
        return f"Session {stage}; selected draft waits for {session['upload_at']}."
    if session.get('retry_at') and now < sessions.timestamp(session['retry_at']):
        return f"Waiting to retry infrastructure at {session['retry_at']}; experiment budget unchanged."
    # Both games share a model. The earliest stop time prevents one game blocking the other's release.
    stops = [session['model_stop_at']]
    for other in GAMES:
        peer = load_state(other).get('session') or {}
        if peer.get('enabled') and sessions.stage(peer, now) in ('improve','stalled','ready','settling','upload'):
            stops.append(peer['model_stop_at'])
    deadline = min(stops)
    previous_draft = _copy(state.get('draft') or {})
    before = previous_draft.get('id')
    interrupted = False
    session['attempts'] += 1; session['status'] = 'improving'; save_state(game, state)
    try:
        with sessions.until(deadline):
            watch_manual_game(cfg, game, state, client)
            # The independent racing baseline belongs only to the work copy.
            # Keep the selected release draft durable even during generation.
            if game=='racing':
                manual_improve(cfg,game,state,client,request_id=session['id'],baseline_override=session.get('baseline'))
            else:manual_improve(cfg,game,state,client,request_id=session['id'])
    except sessions.Deadline:
        from coach.omlx import close_stale_calls
        close_stale_calls('session model cutoff reached')
        session['status'] = 'settling'
        interrupted = True
    except (AgxError, OmlxTransportError, fps_evaluation.EvidenceUnavailable, OSError) as exc:
        session['infrastructure_errors']=session.get('infrastructure_errors',0)+1
        delay=min(300,30*2**min(session['infrastructure_errors']-1,4))
        session.update(status='retrying_data',last_error=str(exc),retry_at=(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat())
        save_state(game,state)
        return f"Infrastructure retry scheduled in {delay}s; no-progress count unchanged."
    except Exception as exc:
        session['last_error'] = str(exc)
    after = (state.get('draft') or {}).get('id')
    seen = set(session.get('functional_keys') or [functional_draft_key(session.get('baseline') or {})])
    functional_key = functional_draft_key(state.get('draft') or {})
    if after and after != before and functional_key in seen:
        rejected_name=(state.get('draft') or {}).get('strategy',{}).get('name')
        state['draft'] = previous_draft
        after = before
        session['last_error'] = 'Duplicate functional proposal; retained selected draft.'
        state['manual_result']={'game':game,'action':'improve','request_id':session['id'],'status':'no_candidate',
            'name':(previous_draft.get('strategy') or {}).get('name'),'draft_id':before,
            'revision':previous_draft.get('revision'),'candidate_ready':bool(previous_draft.get('ready')),
            'rejected_candidate_name':rejected_name,'blockers':['duplicate_functional'],
            'at':datetime.now(timezone.utc).isoformat()}
    if after and after != before:
        session.pop('retry_at',None);session['infrastructure_errors']=0
        session['accepted'] += 1; session['no_progress'] = 0
        session['candidates'] = (session['candidates'] + [{'id':after, 'name':state['draft']['strategy']['name'],
            'at':datetime.now(timezone.utc).isoformat(), 'experiment':state['draft'].get('experiment'),
            'book':{k:v for k,v in state['draft']['strategy'].items() if k not in ('name','thesis')},
            'selection':'latest offline-tested experiment; no measured win-rate ranking'}])[-100:]
        session['functional_keys'] = sorted(seen | {functional_key})
        archive = STATE/'sessions'/session['id']; archive.mkdir(parents=True,exist_ok=True)
        (archive/f"{session['attempts']:04d}.json").write_text(json.dumps(state['draft'],ensure_ascii=False))
    elif not interrupted: session['no_progress'] += 1
    save_state(game, state)
    return f"Session cycle {session['attempts']}: {session['accepted']} validated revisions; release {session['upload_at']}."


def watch_manual_game(cfg, game, state, client) -> str:
    """Record live results without generating or submitting revisions during manual control."""
    slot = "B" if state.get("phase") == "play_B" else "A"
    match, rnd = fresh_match(client, game, state[slot], state)
    rnd=rnd or {}
    state['round_observation']={'observed_at':datetime.now(timezone.utc).isoformat(),
        'round_state':rnd.get('round_state'),'match_assigned':bool(rnd.get('round_match')),
        'next_round':_copy(rnd.get('next_round') or {})}
    if not match:
        save_state(game,state)
        control = ' Timed session controls the next upload.' if (state.get('session') or {}).get('enabled') else ' Manual control: uploads require Upload to AGX.'
        return waiting(rnd, slot) + control
    played = _copy(state[slot]["strategy"])
    remember(state[slot], match, game)
    attach_standings(client, game, state[slot]["last"])
    outcomes.sync(state,game,[state[slot]['last']])
    if game=='fps':fps_workflow.observe(state,state[slot]['last'],played)
    save_state(game, state)
    result = state[slot]["last"]
    log_game(cfg, game, state, slot, match, client, played=played,
             decision="win" if we_won(game, result) else "loss", mode="manual",
             evidence_text=situation_text(game, result, played),
             action="recorded result; draft and upload remain under manual control")
    return "Recorded match result; waiting for manual improve or upload."


def manual_improve(cfg, game, state, client, upload: bool = False, request_id=None, baseline_override=None) -> bool:
    """Improve a local working copy only. Legacy upload arguments cannot submit it."""
    from coach.round_control import require_model_allowed
    require_model_allowed(state)
    state["manual_review"] = True
    save_state(game, state)
    watched = "B" if state.get("phase") == "play_B" else "A"
    previous = state.get("draft") or {}
    previous_best=_copy(state.get('code_best'))
    optimization_goal='quality' if game=='fps' else 'balanced'
    generation_base=baseline_override or previous
    base = generation_base.get("strategy") or state[watched].get("strategy") or state["A"]["strategy"]
    base_code = generation_base.get("code") if generation_base else state[watched].get("code")
    if game == 'fps':
        # Independent hypotheses start from the live version. Unplayed rewrites
        # must not accumulate as though each one had won a real match.
        fixed = fps_workflow.anchor(state)
        base, base_code = fixed['strategy'] or base, fixed['code'] or runtime_code(game)
        repair=state.get('fps_contract_base') or {}
        if repair.get('anchor_id')==fixed.get('id') and repair.get('action_equivalent'):
            # An independently verified contract repair with identical replay
            # actions can supply a safe generation base without stacking tactics.
            base,base_code=repair['strategy'],repair['code']
    focus = state[watched].get("last") or state["A"].get("last") or {}
    work = _copy(state)
    if game == 'fps':
        # gate_best must not silently resume a different unplayed policy.
        stored=work.get('code_best') or {}
        if stored.get('ok') or stored.get('anchor_id') != fixed.get('id'):
            work['code_best'] = None
        work.pop('repair_diagnosis', None)
        audit=fps_evaluation.assess(base_code,base,base_code,base,focus,goal='quality') if (focus.get('training') or {}).get('status')=='verified' else {}
        work['fps_baseline_audit']=[{'trace':t['trace'],'calibration':t.get('calibration'),'quality':t.get('quality',{}).get('baseline')}
            for t in audit.get('traces',[]) if t.get('split')!='holdout']
    work["_draft_workspace"] = True
    work["A"]["strategy"], work["A"]["code"], work["A"]["last"] = _copy(base), base_code, _copy(focus)
    work["_candidate_focus"] = _copy(focus)
    work["refine"] = None
    work["draft_context_note"] = (
        "The current baseline is the selected draft. Improve exactly one behavior/lever relative "
        "to it. Match statistics belong to the recorded played version; check its provenance "
        "before attributing them to this baseline. The new revision is unplayed. Keep every "
        "functional gate check passing; name the next hypothesis."
    )
    if work.get("code_best"):
        work["code_best"]["stalls"] = 0
    mode = "win" if we_won(game, focus) else "loss"
    try:
        write_plan(cfg, game, work, client, mode, focus)
    finally:
        # Naming/protocol/network errors must not discard a gate-passing body.
        # Live slots and selected drafts remain unchanged until all acceptance checks pass.
        for key in ("code_best", "code_log", "gate", "repair_diagnosis", "repair_blockers", "decisions", "racing_experiments", "racing_assessment", "racing_experiment"):
            if key in work:
                state[key] = work[key]
        if game=='fps' and state.get('code_best'):state['code_best']['anchor_id']=fixed.get('id')
        save_state(game,state)
    accepted = False
    report = (work.get("code_best") or {}).get("report") or ""
    if work.get("plan_ready"):
        proposed = work["B"]
        accepted, report = check(game, proposed.get("code") or runtime_code(game), proposed["strategy"])
        if accepted:
            replay_ok, replay_report = training.check_replay(game, proposed.get('code') or runtime_code(game), proposed['strategy'], work.get('_candidate_focus'))
            report += '\n' + replay_report
            state['replay_feedback'] = '' if replay_ok else replay_report
            accepted = replay_ok
        assessment=None
        if accepted and game == 'fps':
            assessment=fps_evaluation.assess(proposed.get('code') or runtime_code(game),proposed['strategy'],base_code or runtime_code(game),base,focus,goal=optimization_goal)
            assessment=fps_evaluation.contract_repair(assessment,(work.get('gate') or {}).get('ok',True),True)
            behavior_report=fps_evaluation.report(assessment);report+='\n'+behavior_report
            state['behavior_feedback']='' if assessment['ok'] else behavior_report
            if not assessment['ok']:
                if previous_best is not None:state['code_best']=previous_best
                accepted=False
                if assessment.get('status') in ('evidence_unavailable','evaluation_unavailable'):
                    save_state(game,state)
                    raise fps_evaluation.EvidenceUnavailable(behavior_report)
            if assessment['ok'] and previous.get('ready') and previous.get('validation_stage') == 'offline_experiment' and previous.get('id') != previous.get('uploaded_id'):
                incumbent=fps_evaluation.assess(proposed.get('code') or runtime_code(game),proposed['strategy'],previous.get('code') or runtime_code(game),previous['strategy'],focus,goal='quality')
                if not incumbent['ok']:
                    report+='\nFAIL incumbent_selection: Candidate did not improve on the selected offline experiment.'
                    state['behavior_feedback']=fps_evaluation.report(incumbent)
                    accepted=False
            fps_workflow.record(state, proposed, assessment, accepted)
        if accepted:
            new = {"strategy": _copy(proposed["strategy"]), "code": proposed.get("code"),
                   "ready": True, "report": report, "upload_requested": False,
                   "context": candidate_context(work), "revision": int(previous.get("revision") or 0) + 1,
                   "at": datetime.now(timezone.utc).isoformat(),
                   "experiment": work.get("racing_experiment"), "assessment": work.get("racing_assessment")}
            if assessment is not None:new['behavior_assessment']=assessment
            new['optimization_goal']=optimization_goal
            if game=='fps':
                new.update(validation_stage='offline_experiment',match_validated=False,
                    anchor_id=fps_workflow.anchor(state).get('id'),evaluation_version=fps_evaluation.VERSION)
                if assessment.get('status')=='offline_contract_repair' and assessment.get('changed_actions')==0:
                    state['fps_contract_base']={'strategy':_copy(new['strategy']),'code':new['code'],
                        'anchor_id':new['anchor_id'],'action_equivalent':True}
            new["id"] = draft_id(new)
            series.stamp(state,new)
            if state.get('counted_series'):new['release_files']=release.prepare(game,state,new)
            state["draft"] = new
    selected = state.get("draft") or {}
    passes, total = report.count("PASS"), report.count("PASS") + report.count("FAIL")
    blockers = sorted(failed_checks(report))
    state["manual_result"] = {
        "game": game, "action": "improve", "request_id": request_id,
        "status": "no_upload" if accepted else "no_candidate",
        "name": (selected.get("strategy") or {}).get("name"), "active_from": None,
        "candidate_passed": passes, "candidate_total": total, "blockers": blockers,
        "candidate_ready": bool(selected.get("ready", bool(selected))),
        "candidate_source": (work.get("code_best") or {}).get("source"),
        "draft_id": selected.get("id"), "revision": selected.get("revision"),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(game, state)
    log(f"{game}: local draft {'updated' if accepted else 'retained'}; no upload; "
        f"draft {(selected.get('strategy') or {}).get('name')}, blockers {blockers}")
    return False


def upload_draft(cfg, game, state, client, selected_id, request_id=None) -> bool:
    """Validate and submit exactly the selected snapshot. Never call oMLX or regenerate."""
    draft = state.get("draft") or {}
    from coach.round_control import require_upload_allowed
    require_upload_allowed(state, draft)
    error = None
    if not draft.get("strategy") or not draft.get("ready", True):
        error = "No validated draft is ready. Run sim + improve first."
    elif not selected_id or draft_id(draft) != selected_id:
        error = "The draft changed after this upload was requested. Select the current draft."
    elif game == "racing" and draft.get("code") != runtime_code(game):
        error = "Racing assets changed; validate a fresh draft before uploading."
    report = draft.get("report") or ""
    if error is None:
        valid, report = check(game, draft.get("code") or runtime_code(game), draft["strategy"])
        if valid:
            watched = 'B' if state.get('phase') == 'play_B' else 'A'
            valid, replay_report = training.check_replay(game, draft.get('code') or runtime_code(game), draft['strategy'], state[watched].get('last'))
            report += '\n'+replay_report
        if valid and game == 'fps':
            baseline=state[watched]
            assessment=fps_evaluation.assess(draft.get('code') or runtime_code(game),draft['strategy'],baseline.get('code') or runtime_code(game),baseline['strategy'],baseline.get('last'),goal=draft.get('optimization_goal','balanced'))
            baseline_passed,_=check('fps',baseline.get('code') or runtime_code(game),baseline['strategy'])
            assessment=fps_evaluation.contract_repair(assessment,baseline_passed,True)
            report+='\n'+fps_evaluation.report(assessment)
            valid=assessment['ok'];draft['behavior_assessment']=assessment
            if valid:draft.update(validation_stage='offline_experiment',match_validated=False,evaluation_version=fps_evaluation.VERSION)
            if not valid:draft['ready']=False
        if not valid:
            error = "Draft failed current validation: " + ", ".join(sorted(failed_checks(report)))
    previous_b, previous_phase = _copy(state["B"]), state.get("phase")
    uploaded = False
    if error is None:
        state["B"].update(strategy=_copy(draft["strategy"]), code=draft.get("code"),
                          live_hash=previous_b.get("live_hash") if previous_b.get("live_hash") == selected_id else None,
                          blocked_hash=None, reject_count=0, seen=[])
        try:
            uploaded = ensure_live(cfg, game, state, client, "B", shared=True, regenerate_on_rejection=False)
        except BaseException as exc:
            state["B"], state["phase"] = previous_b, previous_phase
            remote_stage=(state.get('release_attempt') or {}).get('stage')
            uncertain=isinstance(exc,AgxError) and exc.status in (None,429,502,503,504) and remote_stage in ('uploading_bundle','publishing')
            if isinstance(exc,sessions.Deadline) or uncertain:
                state['manual_result']={'game':game,'action':'upload','status':'confirmation_required',
                    'draft_id':selected_id,'error':'Release interrupted; local live slot restored. Check remote confirmation before retrying.',
                    'at':datetime.now(timezone.utc).isoformat()}
            save_state(game, state)
            raise
        if uploaded:
            state["phase"] = "play_B"
            state["pending_b"] = False
            state["plan_ready"] = True
            draft["upload_requested"] = True
            draft["uploaded_id"] = selected_id
            draft["active_from"] = state["B"].get("active_from")
            series.admitted(state,draft)
        else:
            attempt=state.get('release_attempt') or {}
            if attempt.get('draft_id')==selected_id and attempt.get('admission_passed'):
                error='Bundle passed admission, but AGX publication failed: '+str(attempt.get('error') or 'publication was not confirmed')
            else:error = "Upload was not admitted. See the coach log; the selected draft is retained."
    if not uploaded:
        state["B"], state["phase"] = previous_b, previous_phase
    state["manual_review"] = True
    state["manual_result"] = {
        "game": game, "action": "upload", "request_id": request_id,
        "status": "uploaded" if uploaded else "upload_failed", "error": error,
        "name": (draft.get("strategy") or {}).get("name"), "draft_id": selected_id,
        "active_from": state["B"].get("active_from") if uploaded else None,
        "candidate_passed": report.count("PASS"),
        "candidate_total": report.count("PASS") + report.count("FAIL"),
        "blockers": sorted(failed_checks(report)), "at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(game, state)
    log(f"{game}: explicit upload {'completed' if uploaded else 'failed'} for draft {selected_id}: {error or 'admitted'}")
    return uploaded


def ensure_checkpoint(cfg, game, state, client) -> bool:
    """Study the current leaderboard once, before our own match has a result."""
    if state.get("checkpoint"):
        return False
    snap = field_snapshot(client, game)
    if not snap.get("ours") and not snap.get("latest_public_result"):
        return False
    write_plan(cfg, game, state, client, "init", snap)
    ours = snap.get("ours") or {}
    state["checkpoint"] = {
        "at": snap.get("at"),
        "field_size": snap.get("field_size"),
        "ours": ours,
        "top": snap.get("top") or [],
        "immediately_ahead": snap.get("immediately_ahead") or [],
        "latest_public_result": snap.get("latest_public_result"),
        "plan_b": (state["B"].get("strategy") or {}).get("name"),
    }
    save_state(game, state)
    log(
        f"{game}: initial checkpoint rank {ours.get('rank')} "
        f"rating {ours.get('rating')} of {snap.get('field_size')}. "
        f"Plan B '{state['checkpoint']['plan_b']}' studies that field and stays in reserve."
    )
    return True


def _with_checkpoint(studied: bool, state: dict, message: str) -> str:
    if not studied:
        return message
    ours = (state.get("checkpoint") or {}).get("ours") or {}
    return f"studied checkpoint rank {ours.get('rank')} rating {ours.get('rating')}. {message}"


def ensure_plan_b(cfg, game, state, client) -> None:
    if state["B"].get("strategy"):
        return
    write_plan(cfg, game, state, client, "loss", state["A"].get("last") or {})


def _named_loss(game: str, name: str | None) -> bool:
    """A gate-passing file that already lost its match is not a promotion."""
    if not name:
        return False
    folder = STATE / "logs" / game
    if not folder.exists():
        return False
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        played = (data.get("played_strategy") or {}).get("name")
        if played == name and data.get("decision") == "loss":
            return True
    return False


def promote_passing(cfg, game: str, state: dict, client) -> bool:
    """Put a gate-passing rewrite live even when the best won.

    FPS scores the team, so a seat that never moves can ride teammates to a win and
    the loss path would never replace it.
    """
    if game != "fps" or state.get("phase", "play_A") != "play_A":
        return False
    if (state.get("draft") or {}).get("upload_requested") is False:
        return False
    stored = state.get("code_best") or {}
    code, book = stored.get("code"), stored.get("book") or {}
    if not code or not (stored.get("ok") or gate_score(stored.get("report") or "") >= 1.0):
        return False
    live_name = (state["A"].get("strategy") or {}).get("name")
    if stored.get("context") != candidate_context(state):
        return False
    if _named_loss(game, book.get("name")):
        return False
    if digest(book, code) == state["A"].get("live_hash"):
        return False
    passed, _report = gate_best(game, state)
    if passed:
        return False
    if not quota_open(cfg, client.api_key):
        log(f"{game}: the passing rewrite is ready but the daily upload cap is reached")
        return False
    code_sha = hashlib.sha256(code.encode()).hexdigest()
    if not book.get("name") or book.get("name") == live_name:
        if stored.get("book_for") == code_sha:
            return False
        book = code_book(cfg, game, state["A"]["strategy"], code, stored.get("report") or "")
        stored["book"], stored["book_for"] = book, code_sha
        state["code_best"] = stored
        save_state(game, state)
        if book.get("name") == live_name:
            log(f"{game}: oMLX kept the live name '{live_name}', not resubmitting it")
            return False
    state["A"]["strategy"] = _copy(book)
    state["A"]["code"] = code
    state["A"]["live_hash"] = None
    state["B"]["strategy"] = _copy(book)
    state["B"]["code"] = code
    save_state(game, state)
    log(f"{game}: the live best fails the local gate, promoting the passing rewrite '{book['name']}'")
    return ensure_live(cfg, game, state, client, "A", shared=True)


def write_plan(cfg, game, state, client, mode: str, focus: dict, counter=None, played=None) -> str:
    from coach.round_control import require_model_allowed
    require_model_allowed(state)
    state["plan_ready"] = False
    if focus:
        attach_standings(client, game, focus)
    state["_candidate_focus"] = _copy(focus)
    state["_played_focus"] = {"match_id": (focus or {}).get("match_id"),
                              "strategy": _copy(played) if played else played_for_result(state, focus)}
    report = evidence(state, mode, focus, counter, client)
    state["plan_mode"] = mode
    before = _copy(state["A"]["strategy"])
    before_code = state["A"].get("code")
    trace: dict = {}
    book_mode = "pace" if game == "racing" and mode == "win" else mode
    book_sampling = sampling_for(cfg, state, book_mode, "book")
    code_sampling = sampling_for(cfg, state, mode, "code")
    trace["sampling"] = book_sampling
    proposal, code = None, None
    if game == "fps":
        passed, gate_report = gate_best(game, state)
        guide = 'FPS workflow v2: test one independent, source-grounded experiment against the live anchor.'
        if guide:
            log(f"{game}: no improvement across the last results, guiding oMLX")
        if not passed or guide:
            proposal, code = improve_code(
                cfg, game, state, report, gate_report, code_sampling,
                force=bool(guide and passed),
            )
            trace["source"] = "agent.py rewrite"
            trace["sampling"] = code_sampling
    if proposal is None and game == "fps" and (not passed or guide):
        # Retain both the best partial repair and the submitted slot's identity.
        save_state(game, state)
        return report
    if proposal is None:
        code = state["A"].get("code")
        trace["source"] = "book experiment"
        proposal = propose(cfg, game, state["A"]["strategy"], report, mode, trace, book_sampling,
                           live_code=state["A"].get("code"),
                           exploration=racing_workflow.coverage(state, STATE / "logs" / "racing") if game == "racing" else None)
    if game == "racing":
        experiment = trace.get("experiment") or {}
        state.setdefault("racing_experiments", []).append({"at": datetime.now(timezone.utc).isoformat(),
            "baseline": before, "candidate": _copy(proposal), "changed": racing_workflow.changes(before, proposal),
            "experiment": experiment})
        state["racing_experiments"] = state["racing_experiments"][-100:]
        track = (((focus or {}).get("recording") or {}).get("facts") or {}).get("track_id")
        try:
            state["racing_assessment"] = racing_assessment(track, before, proposal) if track else None
        except Exception as exc:
            state["racing_assessment"] = {"rankable_by_simulation": False, "reason": str(exc)}
        state["racing_experiment"] = experiment
    note_decision(cfg, game, state, mode, before, before_code, proposal, code, trace)
    state["plan_ready"] = True
    state["B"]["strategy"] = proposal
    state["B"]["code"] = code
    state["B"]["live_hash"] = None
    state["B"]["seen"] = []
    state["B"]["active_from"] = None
    state["B"]["reject_count"] = 0
    state["B"]["blocked_hash"] = None
    save_state(game, state)
    return report


def candidate_context(state: dict) -> str:
    book = state["A"]["strategy"]
    code = state["A"].get("code") or runtime_code("fps")
    fixture = (Path(__file__).parent / "gate_fps.py").read_bytes()
    focus = (state.get("refine") or {}).get("focus") or state.get("_candidate_focus") or state["A"].get("last") or {}
    prompt_version = Path(__file__).with_name('prompts.py').read_bytes()
    trace_digest = (focus.get('training') or {}).get('sha256','')
    behavior_feedback=state.get('behavior_feedback') or ''
    goal=json.dumps(state.get('fps_combat_focus') or {},sort_keys=True)
    return hashlib.sha256((digest(book, code) + str(focus.get("match_id")) + trace_digest + behavior_feedback + goal).encode() + fixture + prompt_version).hexdigest()


def diagnose(cfg, game: str, messages: list, sampling=None, fixture=False) -> str:
    """Produce a visible decision artifact that the implementation call receives."""
    if game == "racing" and not fixture:
        messages = [{"role": "system", "content":
            "You diagnose racing evidence, not generate a setup yet. Return JSON with comparison, change, hypothesis, evidence, plan, uncertainty, alternatives. "
            "First identify the logged retirement reason and phase, remaining fuel and preceding telemetry if available. "
            "Prioritize completing the race after a DNF. A damage retirement alone does not prove fuel starvation or a wing defect. "
            "Use actual played setup fields, never infer them from strategy names. Treat cross-track fuel comparisons as confounded hypotheses. "
            "Compare BOTH dry and wet rows on matched tracks. Consider fuel_load_kg, front_wing and rear_wing alongside controller gains. "
            "alternatives is a short list of candidate levers and what is known/untested about them. Follow any exploration requirement. "
            "A/B are baseline/trial, not wet/dry presets. Simulation does not model book effects; do not invent predicted time gains."},
            {"role": "user", "content": messages[-1]["content"]}]
    if fixture:
        messages = [{"role": "system", "content": "Diagnose Python fixture failures. Return ONLY a JSON object with comparison, change, hypothesis strings."},
                    {"role": "user", "content": messages[-1]["content"]}]
    diagnosis_messages=messages + [{"role": "user", "content":
            ("Return JSON with comparison, change, hypothesis. Diagnose ONLY the current failed "
             "fixture checks and exceptions. Quote the exact exception, the function and missing "
             "state initialization if present. Passing checks are constraints to preserve, not "
             "problems to fix. Inspect initialization and every caller of the failing helper. "
             "Do not reuse a diagnosis about checks that now PASS. Do not redesign tactics. "
             "Under 180 words." if fixture else
             "Before implementing, return only a concise JSON decision rationale with keys comparison, "
             "change, hypothesis, evidence, plan, uncertainty. evidence contains at most three measured facts; "
             "plan contains at most three implementation and validation steps; uncertainty states what the logs cannot prove. "
             "Give a short dashboard explanation, without a private reasoning transcript. "
             "Compare two actual evidence rows with their measured numbers; "
             "name ONE function or live lever to change. Distinguish a hypothesis from a measured "
             "cause. No code or strategy yet. Keep the whole diagnosis under 180 words; each string "
             "under 400 characters. If alternatives are requested, at most two short items. Close all JSON strings and braces.")}]
    if not fixture:
        analysis=chat_text(cfg.omlx_url,cfg.omlx_model,diagnosis_messages,cfg.omlx_key,
            timeout=150,max_tokens=3000,sampling=sampling,think=True,label=f'{game} · diagnosis and strategy planning')
        try:raw=_parse_json(analysis)
        except OmlxError:
            # A reasoning-only response is useful work, not a reason to repeat
            # the entire model call or discard the diagnosis. Reserve a separate
            # direct-output call for the small machine-readable decision.
            raw=chat_json(cfg.omlx_url,cfg.omlx_model,
                diagnosis_messages+[{'role':'user','content':'Use this analysis artifact as a hypothesis, verify it against the current source/evidence, and return only the requested concise JSON decision:\n'+analysis}],
                cfg.omlx_key,timeout=90,max_tokens=1600,sampling=sampling,think=False,label=f'{game} · planning summary')
    else:
        raw=chat_json(cfg.omlx_url,cfg.omlx_model,diagnosis_messages,cfg.omlx_key,
            timeout=120,max_tokens=2200,sampling=sampling,think=False,label=f'{game} · diagnosis')
    if not all(isinstance(raw.get(k), str) and raw[k].strip()
               for k in ("comparison", "change", "hypothesis")):
        raise OmlxError("Model returned no actionable diagnosis")
    if game=='racing' and not fixture:
        valid_lists=all(isinstance(raw.get(k),list) and 1<=len(raw[k])<=3 and
                        all(isinstance(x,str) and x.strip() for x in raw[k]) for k in ('evidence','plan'))
        if not valid_lists or not isinstance(raw.get('uncertainty'),str) or not raw['uncertainty'].strip():
            raise OmlxError('Racing planning needs measured evidence, a validation plan, and explicit uncertainty')
    return json.dumps(raw, ensure_ascii=False)


def refine_step(cfg, game, state, client) -> str:
    """Non-blocking FPS improvement: one gate-scored rewrite per tick until the gate
    fully passes or the submit cutoff, then upload the best candidate."""
    refine = state.get("refine") or {}
    cb = state.get("code_best") or {}
    score = gate_score(cb.get("report") or "")
    full_pass = bool(cb.get("ok") and cb.get("context") == candidate_context(state))
    deadline_raw = refine.get("deadline")
    deadline = datetime.fromisoformat(deadline_raw) if deadline_raw else None
    now = datetime.now(timezone.utc)
    iters = int(refine.get("iters") or 0)
    stalled = cb.get("context") == candidate_context(state) and int(cb.get("stalls") or 0) >= 2
    expired = (deadline is not None and now >= deadline) or iters >= REFINE_MAX_ITERS or stalled
    if full_pass or expired or not refine:
        if not full_pass:
            state["refine"] = None
            state["phase"] = "play_A"
            save_state(game, state)
            return "FPS refinement ended without a valid candidate; keeping the current best."
        if full_pass and cb.get("code"):
            final = _passed_book(cfg, game, state["A"]["strategy"],
                                 cb.get("book") or state["B"]["strategy"],
                                 cb["code"], cb.get("report") or "")
            state["B"]["strategy"] = final
            state["B"]["code"] = cb["code"]
            state["plan_ready"] = True
        state["refine"] = None
        uploaded = stage_plan_b(cfg, game, state, client, shared=True)
        save_state(game, state)
        why = "the gate fully passes" if full_pass else "the submit cutoff was reached"
        tail = "Plan B is live for the next round." if uploaded else "the upload is waiting for the window."
        return f"FPS refinement finished ({why}); best gate {score:.0%}. {tail}"
    focus = refine.get("focus") or state["A"].get("last") or {}
    mode = refine.get("mode") or "loss"
    report = evidence(state, mode, focus, client=client)
    _passed, gate_report = gate_best(game, state)
    sampling = sampling_for(cfg, state, mode, "code")
    improve_code(cfg, game, state, report, gate_report, sampling, force=True, max_attempts=1)
    cb = state.get("code_best") or {}
    score = gate_score(cb.get("report") or "")
    refine["iters"] = iters + 1
    state["refine"] = refine
    save_state(game, state)
    return f"refining FPS candidate: best gate {score:.0%}, iteration {refine['iters']}, submit by {deadline_raw}"


CODE_ATTEMPTS = 3
CODE_TIMEOUT = 420
CODE_TOKENS = 12000


def runtime_code(game: str) -> str:
    return (RUNTIME / game / "agent.py").read_text()


def gate_best(game: str, state: dict) -> tuple[bool, str]:
    """Run the local gate on the live agent, cached until the code or book changes."""
    if game != "fps":
        return True, ""
    code = state["A"].get("code") or runtime_code(game)
    fixture = (Path(__file__).resolve().parent / "gate_fps.py").read_bytes()
    key = digest(state["A"]["strategy"], code) + hashlib.sha256(fixture).hexdigest()[:16]
    cached = state.get("gate") or {}
    if cached.get("key") == key:
        return bool(cached.get("ok")), cached.get("report") or ""
    ok, report = check(game, code, state["A"]["strategy"])
    state["gate"] = {"key": key, "ok": ok, "report": report, "at": datetime.now(timezone.utc).isoformat()}
    save_state(game, state)
    log(f"{game}: local gate {'passed' if ok else 'failed'} on the live agent")
    return ok, report


def gate_score(report: str) -> float:
    passed = report.count("PASS")
    total = passed + report.count("FAIL")
    return passed / total if total else 0.0


def code_reads(code: str) -> list[str]:
    """The strategy fields this agent actually reads. Unread fields are inert levers."""
    return sorted(set(re.findall(r"""STRATEGY\.get\(\s*["']([a-z_]+)["']""", code or "")))


def admitted_names(game: str) -> set[str]:
    names = set()
    path = STATE / "coach.log"
    if path.is_file():
        names.update(re.findall(r"\b" + re.escape(game) + r" [AB]: live '([^']+)'", path.read_text()))
    for path in (STATE / "logs" / game).glob("*.json"):
        try:
            name = (json.loads(path.read_text()).get("played_strategy") or {}).get("name")
            if name:
                names.add(name)
        except (OSError, ValueError):
            pass
    return names


def code_book(cfg, game: str, best: dict, code: str, gate_report: str) -> dict:
    """Name a passing rewrite without reusing a previously admitted revision name."""
    reads = code_reads(code)
    forbidden = admitted_names(game) | {best.get("name") or ""}
    for _ in range(2):
        raw = chat_json(
            cfg.omlx_url, cfg.omlx_model,
            book_messages(best, code, gate_report, reads, sorted(forbidden)),
            cfg.omlx_key, timeout=CODE_TIMEOUT,
            sampling={"temperature": cfg.omlx_temp, "top_p": cfg.omlx_top_p, "top_k": cfg.omlx_top_k},
            label=f"{game} · book for rewrite",
        )
        final = clamp(game, raw, best)
        if final.get("name") and final["name"] not in forbidden:
            return final
        log(f"{game}: rejected previously admitted revision name '{final.get('name')}'")
    # Keep naming as a small model-authored metadata task instead of another code rewrite.
    raw=chat_json(cfg.omlx_url,cfg.omlx_model,[{'role':'system','content':'Name a validated artifact. Return only JSON with one key: name.'},
        {'role':'user','content':'Choose a NEW short lowercase hyphenated revision name. It must differ from every forbidden name. Forbidden: '+json.dumps(sorted(forbidden))}],
        cfg.omlx_key,timeout=60,max_tokens=100,sampling={'temperature':.6},think=False,label=f'{game} · revision name')
    name=raw.get('name')
    if isinstance(name,str) and re.fullmatch(r'[a-z][a-z0-9-]{2,47}',name) and name not in forbidden:
        return {**final,'name':name}
    raise OmlxError("Passing code retained; waiting for a new, never-admitted revision name")


def _passed_book(cfg, game: str, best: dict, merged, code: str, gate_report: str) -> dict:
    merged = clamp(game, merged, best)
    if merged.get("name") and merged.get("name") not in admitted_names(game) | {best.get("name")}:
        return merged
    return code_book(cfg, game, best, code, gate_report)


_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def _cause_text(text: str) -> str:
    """The prose before the code fence, after any thinking block."""
    head = _THINK_BLOCK.sub("", text or "")
    fence = head.find("```")
    if fence >= 0:
        head = head[:fence]
    return head.strip()


def _thought_body(text: str) -> str:
    match = _THINK_BLOCK.search(text or "")
    return match.group(1).strip() if match else ""


def _reflection_ok(cause: str, thought: str) -> bool:
    """Two match numbers, in the visible cause or in the thinking."""
    cause = (cause or "").strip()
    thought = (thought or "").strip()
    numbers = set(re.findall(r"\d+", thought + "\n" + cause))
    if len(numbers) < 2:
        return False
    return len(thought) >= 400 or len(cause) >= 160


def failed_checks(report: str) -> set[str]:
    return set(re.findall(r"^FAIL ([a-zA-Z_0-9]+):", report or "", re.MULTILINE))


def apply_code_edits(code: str, payload: dict) -> str:
    """Apply exact, unique model-authored edits; never guess where a patch belongs."""
    edits = payload.get("edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 8:
        raise OmlxError("Expected 1 to 8 exact source edits")
    original = code
    for edit in edits:
        if not isinstance(edit, dict):
            raise OmlxError("Each edit must contain old and new source strings")
        old, new = edit.get("old"), edit.get("new")
        if not isinstance(old, str) or not old or not isinstance(new, str) or code.count(old) != 1:
            raise OmlxError("Patch old source must match exactly once; no fuzzy application")
        code = code.replace(old, new, 1)
    if code == original:
        raise OmlxError("Patch was a no-op")
    try:
        compile(code, "candidate_agent.py", "exec")
    except SyntaxError as exc:
        raise OmlxError(f"Patch introduced syntax error at line {exc.lineno}: {exc.msg}") from exc
    return code


def repair_signature(context: str, report: str) -> str:
    failures = "\n".join(line for line in report.splitlines() if line.startswith("FAIL"))
    return hashlib.sha256(("function-repair-v3\n" + context + "\n" + failures).encode()).hexdigest()


def apply_function_replacements(code: str, reply: str) -> str:
    """Install model-authored function definitions, preserving all other source."""
    blocks = re.findall(r"```(?:python|py)\s*\n(.*?)```", reply, re.DOTALL)
    if not blocks:
        raise OmlxError("Expected replacement functions in a Python fence")
    replacement = "\n\n".join(blocks)
    try:
        tree = ast.parse(replacement)
        original = ast.parse(code)
    except SyntaxError as exc:
        raise OmlxError(f"Invalid replacement syntax: {exc}") from exc
    if not 1 <= len(tree.body) <= 3 or any(not isinstance(n, ast.FunctionDef) or n.decorator_list for n in tree.body):
        raise OmlxError("Return 1 to 3 existing functions only; no imports, globals, or decorators")
    names = [n.name for n in tree.body]
    if len(set(names)) != len(names):
        raise OmlxError("Duplicate replacement function")
    old = {n.name: n for n in original.body if isinstance(n, ast.FunctionDef)}
    if any(name not in old for name in names):
        raise OmlxError("A replacement names a function that does not exist")
    lines = code.splitlines(keepends=True)
    for node in sorted(tree.body, key=lambda n: old[n.name].lineno, reverse=True):
        prior = old[node.name]
        if prior.decorator_list:
            raise OmlxError("Cannot replace a decorated function")
        source = ast.get_source_segment(replacement, node)
        lines[prior.lineno-1:prior.end_lineno] = [source + "\n"]
    result = "".join(lines)
    if result == code:
        raise OmlxError("Replacement changed nothing")
    try:
        compile(result, "candidate_agent.py", "exec")
    except SyntaxError as exc:
        raise OmlxError(f"Replacement did not compile: {exc}") from exc
    return result


def improve_code(cfg, game: str, state: dict, report: str, gate_report: str,
                 sampling: dict | None = None, force: bool = False,
                 max_attempts: int = CODE_ATTEMPTS):
    """Ask oMLX for a new agent.py and keep it only if the local gate passes."""
    book = state["A"]["strategy"]
    base_code = state["A"].get("code") or runtime_code(game)
    candidate = base_code
    best_score = gate_score(gate_report)
    best_code = base_code
    best_report, candidate_book = gate_report, book
    stored = state.get("code_best") or {}
    mode = "repair"
    same_context = stored.get("context") == candidate_context(state)
    if stored.get("code") and not same_context:
        # A fixture upgrade must recheck partial work, not discard it and restart from live code.
        valid, fresh_report = check(game, stored["code"], stored.get("book") or book)
        stored = {**stored, "ok": valid, "report": fresh_report, "score": gate_score(fresh_report), "stalls": 0}
    if stored.get("code"):
        stored_score = gate_score(stored.get("report") or "")
        if stored.get("ok") and not force:
            log(f"{game}: reusing the rewrite that already passed the local gate")
            final = _passed_book(cfg, game, book, stored.get("book"), stored["code"], stored.get("report") or "")
            stored["book"] = final
            state["code_best"] = stored
            save_state(game, state)
            return final, stored["code"]
        if stored_score >= best_score:
            candidate = best_code = stored["code"]
            candidate_book = stored.get("book") or book
            gate_report = stored.get("report") or gate_report
            best_score = stored_score
            best_report = gate_report
            log(f"{game}: resuming from the best stored rewrite, {best_score:.0%} of checks passed")
    if force:
        mode = "improve" if best_score >= 1.0 else "repair"
    context = candidate_context(state)
    stalls = int(stored.get("stalls") or 0)
    diagnosis = ""
    for attempt in range(1, max_attempts + 1):
        log(f"{game}: oMLX {mode} of agent.py, attempt {attempt}/{max_attempts}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        folder = STATE / "candidates" / game
        folder.mkdir(parents=True, exist_ok=True)
        started = time.time()
        # Existing FPS policies are revised by bounded function edits from the
        # first attempt; do not spend thousands of tokens repeating the file.
        incremental = bool(candidate)
        signature = repair_signature(context, gate_report)
        cached_diagnosis = state.get("repair_diagnosis") or {}
        if cached_diagnosis.get("signature") == signature:
            diagnosis = cached_diagnosis["text"]
        else:
            fixture_repair = bool(failed_checks(gate_report))
            messages = (repair_messages(candidate_book, candidate, gate_report, "", state.get('behavior_feedback') or '') if fixture_repair else
                        code_messages(candidate_book, candidate, report, gate_report, attempt, "improve"))
            diagnosis = diagnose(cfg, game, messages, sampling, fixture=fixture_repair)
            state["repair_diagnosis"] = {"context": context, "signature": signature, "text": diagnosis}
            save_state(game, state)
        try:
            if incremental:
                patch_messages = (repair_messages(candidate_book, candidate, gate_report, diagnosis, state.get('behavior_feedback') or '')
                                  if failed_checks(gate_report) else
                                  behavior_messages(candidate_book, candidate, report, gate_report, diagnosis))
                text = chat_text(
                    cfg.omlx_url, cfg.omlx_model,
                    patch_messages,
                    cfg.omlx_key, timeout=180, max_tokens=5000, sampling=sampling,
                    think=False, label=f"{game} · function {'repair' if failed_checks(gate_report) else 'improve'} {attempt}",
                )
                new_code = apply_function_replacements(candidate, text)
                raw_book = candidate_book
            else:
                text = chat_text(
                    cfg.omlx_url, cfg.omlx_model,
                    code_messages(candidate_book, candidate, report + "\nCurrent fixture diagnosis: " + diagnosis, gate_report, attempt, mode),
                    cfg.omlx_key, timeout=CODE_TIMEOUT, max_tokens=CODE_TOKENS + 8000,
                    sampling=sampling, think=False, label=f"{game} · agent.py attempt {attempt}",
                )
                new_code, raw_book = parse_blocks(text)
            (folder / f"{stamp}-a{attempt}.txt").write_text(text)
            if new_code.strip() == candidate.strip():
                raise OmlxError("Reply did not change the candidate")
        except OmlxError as exc:
            stalls += 1
            if state.get("code_best"):
                state["code_best"]["stalls"] = stalls
            save_state(game, state)
            gate_report = best_report + "\nPrevious reply rejected: " + str(exc)
            log(f"{game}: repair reply rejected: {exc}")
            if stalls >= 2:
                break
            continue
        merged = clamp(game, raw_book, book)
        ok, gate_report = check(game, new_code, merged)
        score = gate_score(gate_report)
        path = folder / f"{stamp}-a{attempt}.py"
        path.write_text(new_code)
        state.setdefault("code_log", []).append({
            "at": datetime.now(timezone.utc).isoformat(),
            "attempt": attempt, "ok": ok, "bytes": len(new_code.encode()),
            "name": merged.get("name"), "report": gate_report, "source": str(path.relative_to(ROOT)),
        })
        state["code_log"] = state["code_log"][-12:]
        failures = failed_checks(gate_report)
        previous_failures = failed_checks(best_report)
        no_regression = not (failures - previous_failures)
        progress = score > best_score and no_regression
        stalls = 0 if progress else stalls + 1
        if score >= best_score and no_regression:
            best_score, best_code, candidate = score, new_code, new_code
            best_report, candidate_book = gate_report, merged
            state["code_best"] = {
                "code": new_code, "score": score, "report": gate_report,
                "ok": ok, "book": merged, "context": context, "stalls": stalls,
                "source": str(path.relative_to(ROOT)),
            }
        else:
            candidate = best_code
            gate_report = best_report
            if state.get("code_best"):
                state["code_best"]["stalls"] = stalls
            log(f"{game}: regression rejected; keeping the {best_score:.0%} candidate and its failure report")
        save_state(game, state)
        if ok:
            final = _passed_book(cfg, game, book, merged, new_code, gate_report)
            final_ok, final_report = check(game, new_code, final)
            state["code_best"].update(book=final, ok=final_ok, report=final_report,
                                      score=gate_score(final_report))
            save_state(game, state)
            if not final_ok:
                gate_report = final_report
                continue
            log(f"{game}: oMLX rewrite passed the local gate, {len(new_code.encode())} bytes, book '{final.get('name')}'")
            return final, new_code
        log(f"{game}: repair incomplete, best {best_score:.0%}; stalls {stalls}/2")
        if stalls >= 2:
            log(f"{game}: stopping repeated failures; partial candidate retained for new evidence or manual retry")
            break
    if state.get("code_best"):
        state["code_best"]["report"] = best_report
        state["code_best"]["score"] = best_score
    state["repair_blockers"] = sorted(failed_checks(best_report))
    save_state(game, state)
    log(f"{game}: no validated replacement; blockers {', '.join(state['repair_blockers'])}; partial repair retained")
    return None, None


def _copy(value):
    return json.loads(json.dumps(value)) if value is not None else None


def _public_match(match: dict) -> dict:
    saved = {key: value for key, value in match.items() if not str(key).startswith("_")}
    saved["seat"] = match.get("_seat")
    saved["team"] = match.get("_team")
    return saved


def _slot_view(state: dict, slot: str) -> dict:
    record = state.get(slot) or {}
    code = record.get("code")
    return {
        "strategy": _copy(record.get("strategy")),
        "agent_id": record.get("agent_id"),
        "submission_id": record.get("submission_id"),
        "active_from": record.get("active_from"),
        "live": bool(record.get("live_hash")),
        "last": _copy(record.get("last")),
        "code_bytes": len(code.encode()) if code else None,
        "code_sha": hashlib.sha256(code.encode()).hexdigest()[:12] if code else None,
    }


def _safe_board(client, game: str):
    try:
        data = leaderboard(client, GAMES[game]["competition_id"])
    except AgxError:
        return None
    items = [item for item in (data.get("items") or []) if isinstance(item.get("rank"), int)]
    items.sort(key=lambda item: item["rank"])
    mine = next((item for item in items if item.get("is_mine")), None)
    return {
        "field_size": data.get("total") or len(items),
        "ours": _board_row(mine) if mine else None,
        "rows": [_board_row(item) for item in items],
    }


def log_game(cfg, game, state, slot, match, client, *, played, decision, mode, evidence_text, action) -> Path:
    """Write the full match record once the result and the next plan are both known."""
    summary = (state.get(slot) or {}).get("last") or {}
    match_id = str(match.get("id") or "unknown")
    record = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "game": game,
        "slot": slot,
        "generation": state.get("generation"),
        "phase": state.get("phase"),
        "decision": decision,
        "mode": mode,
        "action": action,
        "won": we_won(game, summary),
        "model": cfg.omlx_model,
        "played_strategy": played,
        "result": summary,
        "field": _field_rows(match),
        "match": _public_match(match),
        "leaderboard": _safe_board(client, game),
        "evidence": evidence_text,
        "next_best": _slot_view(state, "A"),
        "next_plan_b": _slot_view(state, "B"),
        "checkpoint": _copy(state.get("checkpoint")),
        "faults": state.get("faults") or [],
        "gate": _copy(state.get("gate")),
        "code_log": _copy(state.get("code_log")),
        "decisions": _copy(state.get("decisions")),
    }
    folder = STATE / "logs" / game
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{match_id}.json"
    text = json.dumps(record, indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
    with (STATE / "games.log").open("a", encoding="utf-8") as handle:
        handle.write(text + "\n\n")
    log(
        f"{game}: logged {slot} match {match_id} rank={summary.get('rank')} "
        f"score={summary.get('seat_score')} {decision}. {action}. {path.relative_to(ROOT)}"
    )
    return path


def _book_diff(before, after, skip_thesis: bool = False) -> list[str]:
    before, after = before or {}, after or {}
    rows = []
    for key in sorted(set(before) | set(after)):
        if skip_thesis and key == "thesis":
            continue
        old, new = before.get(key), after.get(key)
        if old != new:
            rows.append(
                f"  {key}: {json.dumps(old, ensure_ascii=False)} -> {json.dumps(new, ensure_ascii=False)}"
            )
    return rows


def note_decision(cfg, game, state, mode, before, before_code, proposal, code, trace) -> None:
    """Append what changed and why, including anything the harness overrode."""
    asked = clamp(game, trace["raw"], before) if trace.get("raw") else None
    diff = _book_diff(before, proposal, skip_thesis=True)
    override = _book_diff(asked, proposal) if asked else []
    old_bytes = len(before_code.encode()) if before_code else 0
    new_bytes = len(code.encode()) if code else 0
    if new_bytes != old_bytes or (code or "") != (before_code or ""):
        code_line = f"  agent.py: rewritten, {old_bytes} -> {new_bytes} bytes"
    else:
        code_line = f"  agent.py: unchanged ({new_bytes} bytes)" if new_bytes else "  agent.py: the frozen runtime file"
    gate_line = ""
    if game == "fps":
        entries = state.get("code_log") or []
        if entries and trace.get("source") == "agent.py rewrite":
            last = entries[-1]
            gate_line = f"LOCAL GATE: {'passed' if last.get('ok') else 'rejected'} — {last.get('report', '').strip().splitlines()[-1]}"
    lines = [
        f"=== {datetime.now(timezone.utc).isoformat()} {game} {mode} "
        f"generation={state.get('generation', 1)} model={cfg.omlx_model} via={trace.get('source', 'book experiment')}",
        f"SAMPLING: {json.dumps(trace.get('sampling') or {})} loss_streak={state.get('loss_streak', 0)}",
        "WHAT CHANGED (live best -> Plan B):",
        *(diff or ["  nothing: the whole book is copied"]),
        code_line,
        "WHY (oMLX thesis):",
        "  " + (proposal.get("thesis") or "(no thesis returned)"),
    ]
    if override:
        lines += ["OVERRIDDEN BY THE HARNESS (asked -> applied):", *override]
    if asked is not None and not override and _book_diff(before, asked):
        lines.append("HARNESS: applied exactly what oMLX asked for.")
    if gate_line:
        lines.append(gate_line)
    if trace.get('diagnosis'):
        lines.append('PLANNING: '+json.dumps(trace['diagnosis'],ensure_ascii=False))
    lines.append("")
    DECISIONS.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    state.setdefault("decisions", []).append({
        "at": datetime.now(timezone.utc).isoformat(),
        "game": game, "mode": mode, "via": trace.get("source", "book experiment"),
        "name": proposal.get("name"), "thesis": proposal.get("thesis"),
        "changed": diff, "overridden": override,
        "code_bytes": new_bytes, "code_changed": (code or "") != (before_code or ""),
        "diagnosis":trace.get('diagnosis'),
    })
    state["decisions"] = state["decisions"][-10:]
    log(f"{game}: decision logged — {proposal.get('name')} ({len(diff)} field changes)")


def adopt(state: dict, slot: str) -> None:
    state["A"]["strategy"] = json.loads(json.dumps(state[slot]["strategy"]))
    state["A"]["code"] = state[slot].get("code")
    state["A"]["last"] = state[slot].get("last")
    state["A"]["live_hash"] = state[slot].get("live_hash")
    state["A"]["active_from"] = state[slot].get("active_from")
    state["A"]["seen"] = list(dict.fromkeys(state["A"].get("seen", []) + state[slot].get("seen", [])))[-40:]


def note_history(state: dict, winner: str) -> None:
    state.setdefault("history", []).append({
        "at": datetime.now(timezone.utc).isoformat(),
        "generation": state.get("generation", 1),
        "winner": winner,
        "a": state["A"].get("last"),
        "b": state["B"].get("last"),
        "a_name": (state["A"].get("strategy") or {}).get("name"),
        "b_name": (state["B"].get("strategy") or {}).get("name"),
        "a_stop": stop_kind(state["A"].get("strategy")),
        "b_stop": stop_kind(state["B"].get("strategy")),
        "a_plan": fps_snapshot(state["A"].get("strategy")),
        "b_plan": fps_snapshot(state["B"].get("strategy")),
    })
    state["history"] = state["history"][-12:]


def ensure_live(cfg, game, state, client, slot: str, shared: bool, regenerate_on_rejection: bool = True) -> bool:
    record = state[slot]
    if game == "racing":
        # Asset-dispatch code is harness infrastructure; frozen controller is packaged unchanged.
        record["code"] = runtime_code(game)
    if not record.get("strategy"):
        return False
    current = digest(record["strategy"], record.get("code"))
    if record.get("live_hash") == current:
        return True
    if record.get("blocked_hash") == current and record.get("reject_count", 0) >= 2:
        log(f"{game} {slot}: this strategy was rejected twice, waiting for a new one")
        return False
    if game == "fps":
        passed, gate_report = check(game, record.get("code") or runtime_code(game), record["strategy"])
        if not passed:
            log(f"fps {slot}: upload blocked by local validation: {gate_report.splitlines()[-1]}")
            return False
    if not quota_open(cfg, client.api_key):
        log(f"{game} {slot}: daily upload cap reached, keeping the live version")
        return False
    rnd = round_payload(client, GAMES[game]["competition_id"])
    agent_id = ensure_agent(client, game, state, slot, shared)
    register(client, game, agent_id)
    try:
        def progress(update):
            if update.get('stage')=='uploading_bundle':
                selected=state.get('draft') or {}
                expected=(selected.get('release_files') or {}).get('bundle_sha256') if selected.get('id')==current else None
                if expected and expected!=update['bundle_sha256']:
                    raise ValueError('Submission bytes differ from the reviewed release files; prepare this draft again')
            if update.get('stage')=='uploading_bundle':state['release_attempt']={}
            state.setdefault('release_attempt',{}).update(update)
            state['release_attempt']['draft_id']=current
            save_state(game,state)
        submission_id, fault = upload(client, game, agent_id, record["strategy"], record.get("code"),on_progress=progress)
    except AgxError as exc:
        if (state.get('release_attempt') or {}).get('stage')=='publishing':
            state['release_attempt'].update(error=str(exc),http_status=exc.status)
            save_state(game,state)
        if exc.status in (None,429,502,503,504) and (state.get('release_attempt') or {}).get('stage') in ('uploading_bundle','publishing'):
            raise
        log(f"{game} {slot}: upload failed: {exc}")
        return False
    if not submission_id:
        state.setdefault("faults", []).append({"slot": slot, "fault": fault, "name": record["strategy"]["name"]})
        state["faults"] = state["faults"][-8:]
        record["reject_count"] = record.get("reject_count", 0) + 1
        record["blocked_hash"] = current
        log(f"{game} {slot}: admission rejected ({fault})")
        if regenerate_on_rejection and slot == "B" and record["reject_count"] <= 2:
            mode = state.get("plan_mode") or "loss"
            focus = state["B"].get("last") or state["A"].get("last") or {}
            record["strategy"] = propose(
                cfg,
                game,
                state["A"]["strategy"],
                evidence(state, mode, focus, client=client) + f"\nRejected: {fault}",
                mode,
            )
            save_state(game, state)
            return ensure_live(cfg, game, state, client, slot, shared)
        return False
    record["live_hash"] = digest(record["strategy"], record.get("code"))
    if slot == "A" and record.get("code"):
        (RUNTIME / game / "agent.py").write_text(record["code"])
    record["active_from"] = activation_time(rnd)
    record["submission_id"] = submission_id
    record["reject_count"] = 0
    record["blocked_hash"] = None
    if shared:
        state["A"]["agent_id"] = agent_id
        state["B"]["agent_id"] = agent_id
    log(f"{game} {slot}: live '{record['strategy']['name']}' from {record['active_from']}")
    save_state(game, state)
    return True


def ensure_agent(client: Agx, game: str, state: dict, slot: str, shared: bool) -> str:
    existing = state["A"].get("agent_id") if shared else state[slot].get("agent_id")
    if existing:
        state[slot]["agent_id"] = existing
        return existing
    _status, _headers, body = client.post_json(
        "/api/v1/python-submissions/agents",
        {"game_id": GAMES[game]["game_id"]},
        idempotency=new_key(),
    )
    data = loads(body)
    agent_id = data.get("agent_id") or data.get("id") or (data.get("agent") or {}).get("id")
    if not agent_id:
        raise AgxError(f"create agent returned no id: {json.dumps(data)[:300]}")
    state[slot]["agent_id"] = agent_id
    if shared:
        state["A"]["agent_id"] = agent_id
        state["B"]["agent_id"] = agent_id
    register(client, game, agent_id)
    return agent_id


def register(client: Agx, game: str, agent_id: str) -> None:
    path = f"/api/v1/python-submissions/competitions/{GAMES[game]['competition_id']}/registration"
    try:
        client.post_json(path, {"agent_id": agent_id}, idempotency=new_key())
    except AgxError as exc:
        if exc.status in (409,):
            return
        if exc.status in (401, 403):
            log(f"{game}: this API key cannot register. Open the competition in the browser and click 报名 once.")
            return
        raise


def upload(client: Agx, game: str, agent_id: str, strategy: dict, code: str | None = None, *, on_progress=None) -> tuple[str | None, str]:
    blob = build(game, strategy, code)
    note_upload(client.api_key)
    idem = new_key()
    if on_progress:on_progress({'stage':'uploading_bundle','agent_id':agent_id,'idempotency_key':idem,
        'bundle_sha256':hashlib.sha256(blob).hexdigest(),'started_at':datetime.now(timezone.utc).isoformat()})
    _status, _headers, body = client.post_zip(
        f"/api/v1/python-submissions/agents/{agent_id}/bundles",
        blob,
        idem,
    )
    data = loads(body)
    submission_id = find_id(data)
    if on_progress:on_progress({'stage':'admission','submission_id':submission_id})
    state_name, fault = admission_of(data)
    if state_name not in ("passed", "failed", "rejected") and submission_id:
        state_name, fault = poll_admission(client, submission_id)
    if state_name != "passed" or not submission_id:
        return None, fault or state_name or "admission did not pass"
    publish_key=new_key()
    if on_progress:on_progress({'stage':'publishing','publish_idempotency_key':publish_key,'admission_passed':True})
    result=client.post_json(
        f"/api/v1/python-submissions/competitions/{GAMES[game]['competition_id']}/versions",
        {"submission_id": submission_id, "auto_match": True},
        idempotency=publish_key,
    )
    if on_progress:on_progress({'stage':'published','confirmed_at':datetime.now(timezone.utc).isoformat(),
        'activation_response':loads(result[2])})
    return submission_id, ""


def poll_admission(client: Agx, submission_id: str) -> tuple[str, str]:
    for _ in range(30):
        _status, headers, body = client.get(f"/api/v1/python-submissions/bundles/{submission_id}")
        data = loads(body)
        state_name, fault = admission_of(data)
        if state_name in ("passed", "failed", "rejected"):
            return state_name, fault
        time.sleep(_retry_header(headers))
    return "running", "admission still running"


def fresh_match(client: Agx, game: str, record: dict, state=None):
    rows, rnd = my_matches(client, GAMES[game]["competition_id"], GAMES[game]["game_id"])
    # Count every returned match, including rounds missed between polls or before a new upload.
    # Secondary accounts retain independent results; the board reports the primary account.
    if state is not None and state.get('outcome_account') in (None, _ident(client.api_key)):
        summaries = [summarize(game,row) for row in rows if outcomes.eligible(row)]
        if outcomes.sync(state,game,summaries): save_state(game,state)
    seen = set(record.get("seen") or [])
    active = record.get("active_from") or ""
    chosen = None
    for match in rows:
        identity = str(match.get("id") or "")
        created = str(match.get("created_at") or "")
        if not identity or identity in seen or not created:
            continue
        if active and created < active:
            continue
        if match.get("status") not in ("succeeded", "failed"):
            continue
        if chosen is None or created > str(chosen.get("created_at") or ""):
            chosen = match
    return chosen, rnd


def my_matches(client: Agx, competition_id: str, game_id: str):
    # An account key plus competition_id returns 404 on this host. Filter by game, then by competition.
    _status, _headers, body = client.get(
        "/api/v1/matches",
        {"game_id": game_id, "limit": 100},
    )
    rows = [
        row for row in (loads(body).get("items") or [])
        if row.get("competition_id") in (None, competition_id)
    ]
    try:
        _job_status, _job_headers, job_body = client.get(
            "/api/v1/jobs",
            {"mine": "true", "game_id": game_id, "limit": 20},
        )
        rows.extend(
            row for row in (loads(job_body).get("items") or [])
            if row.get("competition_id") in (None, competition_id)
        )
    except AgxError:
        pass
    rnd = round_payload(client, competition_id)
    current = rnd.get("round_match")
    if isinstance(current, dict):
        current = dict(current)
        current["_caller_match"] = True
        rows.append(current)
    owned = []
    seen = set()
    for row in rows:
        identity = str(row.get("id") or "")
        if identity and identity in seen:
            continue
        participants = [
            p for p in (row.get("participants") or [])
            if p.get("is_mine") or p.get("mine")
        ]
        caller = bool(row.get("_caller_match") or row.get("is_mine") or row.get("mine"))
        if not participants and not caller:
            continue
        seen.add(identity)
        copied = dict(row)
        if participants:
            copied["_seat"] = participants[0].get("seat")
            copied["_team"] = participants[0].get("team")
        else:
            copied["_seat"] = row.get("seat")
            copied["_team"] = row.get("team")
        owned.append(copied)
    return owned, rnd


def remember(record: dict, match: dict, game: str) -> None:
    record["last"] = summarize(game, match)
    record.setdefault("seen", []).append(match.get("id"))
    record["seen"] = record["seen"][-40:]


def _field_rows(match: dict) -> list[dict]:
    result = match.get("result") or {}
    ranks = result.get("ranks") or []
    scores = result.get("scores") or []
    rows = []
    for person in match.get("participants") or []:
        seat = person.get("seat")
        if not isinstance(seat, int):
            continue
        rows.append({
            "name": person.get("display_name"),
            "seat": seat,
            "team": person.get("team"),
            "rank": ranks[seat] if seat < len(ranks) else None,
            "score": scores[seat] if seat < len(scores) else None,
        })
    return rows


def _slim(row: dict) -> dict:
    return {"name": row.get("name"), "rank": row.get("rank"), "score": row.get("score")}


def _ahead_behind(game: str, rows: list[dict], seat, team) -> tuple[list[dict], list[dict]]:
    me = next((row for row in rows if row["seat"] == seat), None)
    if me is None:
        return [], []
    others = [row for row in rows if row["seat"] != seat]
    if game == "racing" and me.get("rank") is not None:
        ahead = sorted((row for row in others if row.get("rank") is not None and row["rank"] < me["rank"]), key=lambda row: row["rank"])
        behind = sorted((row for row in others if row.get("rank") is not None and row["rank"] > me["rank"]), key=lambda row: row["rank"])
        return [_slim(row) for row in ahead], [_slim(row) for row in behind[:3]]
    my_score = me.get("score")
    if my_score is None:
        return [_slim(row) for row in others[:3]], []
    ahead = sorted((row for row in others if (row.get("score") or 0) > my_score), key=lambda row: row["score"], reverse=True)
    worse = [row for row in others if (row.get("score") or 0) < my_score]
    if worse:
        behind = sorted(worse, key=lambda row: row["score"], reverse=True)
    else:
        opponents = [row for row in others if row.get("team") != team]
        behind = opponents or others
    return [_slim(row) for row in ahead], [_slim(row) for row in behind[:3]]


def summarize(game: str, match: dict) -> dict:
    result = match.get("result") or {}
    ranks = result.get("ranks") or []
    scores = result.get("scores") or []
    wins = result.get("round_wins") or []
    seat = match.get("_seat")
    team = match.get("_team")
    rank = ranks[seat] if isinstance(seat, int) and seat < len(ranks) else None
    seat_score = scores[seat] if isinstance(seat, int) and seat < len(scores) else None
    team_rounds = wins[team] if isinstance(team, int) and team < len(wins) else None
    ahead, behind = _ahead_behind(game, _field_rows(match), seat, team)
    if match.get("status") == "failed":
        rank = 99 if rank is None else rank
        seat_score = -1
    return {
        "match_id": match.get("id"),
        "status": match.get("status"),
        "created_at": match.get("created_at"),
        "finished_at": match.get("finished_at"),
        "seat": seat,
        "team": team,
        "rank": rank,
        "seat_score": seat_score,
        "team_rounds": team_rounds,
        "round_wins": wins,
        "ahead": ahead,
        "behind": behind,
        "error": match.get("error"),
        "game": game,
    }


def we_won(game: str, summary: dict | None) -> bool:
    if not summary or summary.get("status") == "failed":
        return False
    if game == "racing":
        return _finish_place(summary) == 1
    score = summary.get("seat_score")
    return score is not None and float(score) >= 1


def leaderboard(client: Agx, competition_id: str) -> dict:
    _status, _headers, body = client.get(
        f"/api/v1/competitions/{competition_id}/leaderboard",
        {"limit": "100"},
    )
    data = loads(body)
    return data if isinstance(data, dict) else {}


def _board_row(item: dict) -> dict:
    return {
        "name": item.get("display_name"),
        "rank": item.get("rank"),
        "rating": item.get("rating"),
        "win_rate_percent": item.get("win_rate_percent"),
    }


def field_snapshot(client: Agx, game: str) -> dict:
    data = leaderboard(client, GAMES[game]["competition_id"])
    items = [item for item in (data.get("items") or []) if isinstance(item.get("rank"), int)]
    items.sort(key=lambda item: item["rank"])
    mine = next((item for item in items if item.get("is_mine")), None)
    mine_rank = mine.get("rank") if mine else None
    ahead = [item for item in items if mine_rank is not None and item["rank"] < mine_rank][-3:]
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "field_size": data.get("total") or len(items),
        "ours": _board_row(mine) if mine else None,
        "top": [_board_row(item) for item in items[:5]],
        "immediately_ahead": [_board_row(item) for item in ahead],
        "latest_public_result": latest_public_result(client, game),
    }


def latest_public_result(client: Agx, game: str) -> dict | None:
    _status, _headers, body = client.get(
        "/api/v1/matches",
        {"game_id": GAMES[game]["game_id"], "limit": 5},
    )
    rows = loads(body).get("items") or []
    match = next((row for row in rows if row.get("status") == "succeeded"), None)
    if not match:
        return None
    result = match.get("result") or {}
    ranks = result.get("ranks") or []
    scores = result.get("scores") or []
    order = []
    for person in match.get("participants") or []:
        seat = person.get("seat")
        if not isinstance(seat, int):
            continue
        order.append({
            "name": person.get("display_name"),
            "rank": ranks[seat] if seat < len(ranks) else None,
            "score": scores[seat] if seat < len(scores) else None,
        })
    order.sort(key=lambda row: (row["rank"] is None, row["rank"] if row["rank"] is not None else 99))
    return {
        "match_id": match.get("id"),
        "created_at": match.get("created_at"),
        "winner": result.get("winner"),
        "order": order[:10],
    }


def standings_behind(client: Agx, competition_id: str) -> list[dict]:
    items = leaderboard(client, competition_id).get("items") or []
    mine = next((item for item in items if item.get("is_mine")), None)
    if not mine or not isinstance(mine.get("rank"), int):
        return []
    rank = mine["rank"]
    behind = [item for item in items if isinstance(item.get("rank"), int) and item["rank"] > rank]
    behind.sort(key=lambda item: item["rank"])
    return [
        {"name": item.get("display_name"), "rank": item.get("rank"), "rating": item.get("rating")}
        for item in behind[:3]
    ]


def attach_standings(client: Agx, game: str, summary: dict) -> None:
    if not isinstance(summary, dict):
        return
    summary['training'] = training.read(client, game, summary, background=True)
    from coach import fps_match_data, racing_match_data
    collector=fps_match_data if game=='fps' else racing_match_data
    summary['public_analysis'] = collector.read(client,summary,background=True)
    try:
        summary["standings_behind"] = standings_behind(client, GAMES[game]["competition_id"])
    except AgxError:
        summary["standings_behind"] = []
    cached = summary.get("recording") or {}
    stale = cached.get("schema") != recording.schema_for(game)
    if summary.get("status") == "succeeded" and (summary.get("recording") is None or stale):
        summary["recording"] = recording.read(
            client, game, summary.get("match_id"), summary.get("seat"), summary.get("team")
        )
        if summary["recording"]:
            log(f"{game}: read the public recording for {summary.get('match_id')}")


def fps_records(state: dict) -> list:
    """Each measured shooter book and its best team score."""
    found = []

    def add(plan, result):
        score = fps_score(result)
        if plan and score:
            found.append((plan, score))

    for row in state.get("history") or []:
        for slot in ("a", "b"):
            add(row.get(f"{slot}_plan"), row.get(slot))
    for slot in ("A", "B"):
        add(fps_snapshot(state[slot].get("strategy")), state[slot].get("last"))
    return found


def tried_stops(state: dict) -> dict:
    """Only actual dry stop executions can measure a stop experiment."""
    found = {}
    for path in (STATE / "logs" / "racing").glob("*.json"):
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        result = row.get("result") or {}
        facts = (result.get("recording") or {}).get("facts") or {}
        if facts.get("wet_race") is not False or not facts.get("pit_stops"):
            continue
        kind = stop_kind(row.get("played_strategy"))
        place = facts.get("finish_position")
        if kind and isinstance(place, int):
            found[kind] = min(found.get(kind, 99), place - 1)
    return found


def played_for_result(state: dict, focus: dict | None) -> dict:
    focus = focus or {}
    mid = focus.get("match_id")
    for game in ("racing", "fps"):
        path = STATE / "logs" / game / f"{mid}.json"
        if path.is_file():
            try:
                return json.loads(path.read_text()).get("played_strategy") or {}
            except (OSError, ValueError):
                pass
    remembered = state.get("_played_focus") or {}
    if mid and remembered.get("match_id") == mid:
        return remembered.get("strategy") or {}
    for slot in ("B", "A"):
        if mid and (state[slot].get("last") or {}).get("match_id") == mid:
            return _copy(state[slot].get("strategy") or {})
    return _copy(state["A"].get("strategy") or {})


def situation_text(game: str, focus: dict, played: dict | None) -> str:
    """State only what this result can support. The model was inventing the rest."""
    played = played or {}
    rec = focus.get("recording") or {}
    if game == "racing":
        rank = focus.get("rank")
        place = _finish_place(focus)
        stop = (played.get("stops") or [{}])[0]
        lines = [
            "Situation:",
            f"Recorded finish place {place}; rank-array rank {rank}. A retirement is not a classified finish.",
            "Exact book that raced: " + json.dumps(played, ensure_ascii=False),
            (
                f"Strategy that raced: start {played.get('start_compound')}, "
                f"lap {stop.get('lap')} for {stop.get('compound')}."
            ),
        ]
        if rec.get("text"):
            lines.append(rec["text"])
            recorded = (rec.get("facts") or {}).get("finish_position")
            if isinstance(recorded, int) and isinstance(rank, int) and recorded != rank + 1:
                lines.append(
                    f"The match ranks array says place {place}. The recording says place {recorded}. "
                    "Use the recording."
                )
        else:
            lines.append("No recording is available for this match. No lap times and no gaps were returned.")
            lines.append("That finish order does not say whether the stop was early or late.")
        return "\n".join(lines)
    wins = focus.get("round_wins") or []
    setup = played.get("ct_setup")
    defense = {
        "retake": "On defense this seat pushes the expected site and shoots the first enemy it sees.",
        "stack_expect": "On defense this seat walks to the expected site and holds.",
        "mid": "On defense this seat walks toward mid when the map has that landmark.",
    }.get(setup, "Defense setup is unset.")
    lines = [
        "Situation:",
        (
            f"Team rounds {wins}. Seat score {focus.get('seat_score')}. "
            f"Rank {focus.get('rank')}."
        ),
    ]
    if rec.get("text"):
        lines.append(rec["text"])
    else:
        lines.append("No recording is available for this match. The score is the team result only.")
    lines += [
        (
            f"Strategy that played: attack site {played.get('default_site')}, "
            f"buy {played.get('buy_style')}, plant delay {played.get('execute_live_seconds')}s, "
            f"defense {setup}."
        ),
        defense,
        "A visible enemy within 25m also makes this seat stop before it shoots.",
        "Freeze is 6s, live is 115s, and the first 25s of live are still the buy window.",
    ]
    return "\n".join(lines)


def match_series(game: str, focus=None, played=None) -> str:
    """Every finished match on one line, oldest first. This is the reflection input."""
    folder = STATE / "logs" / game
    if not folder.exists():
        return ""
    rows = []
    seen = set()
    data_rows = []
    for path in folder.glob("*.json"):
        try:
            data_rows.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    if focus and focus.get("match_id"):
        data_rows = [d for d in data_rows if (d.get("result") or {}).get("match_id") != focus["match_id"]]
        data_rows.append({"result": focus, "played_strategy": played or {}})
    for data in data_rows:
        result = data.get("result") or {}
        identity = result.get("match_id") or str(id(data))
        if identity in seen:
            continue
        seen.add(identity)
        played = data.get("played_strategy") or {}
        facts = ((result.get("recording") or {}).get("facts") or {})
        created = result.get("created_at") or ""
        when = created
        if created:
            try:
                when = datetime.fromisoformat(created).astimezone().strftime("%m-%d %H:%M")
            except ValueError:
                when = created[:16]
        if game == "racing":
            place = facts.get("finish_position")
            if place is None and facts.get("finished") is not False and isinstance(result.get("rank"), int) and result["rank"] < 90:
                place = result["rank"] + 1
            gap = facts.get("gap_to_winner_s")
            qual = facts.get("qualifying_s")
            rows.append((created, (
                f"{when} id {str(identity)[:8]} track {facts.get('track_id', 'unknown')} {played.get('name')} place {place} "
                f"finished {facts.get('finished', facts.get('finish_position') is not None)} "
                f"retirements {json.dumps(facts.get('retirements') or [])} "
                f"gap {None if gap is None else round(gap, 1)}s "
                f"qual {None if qual is None else round(qual, 1)}s "
                f"start {facts.get('weather_start')} wet {facts.get('wet_race')} peak {facts.get('max_track_wetness')} "
                f"kv {played.get('kv')} ld_min {played.get('ld_min')} "
                f"preview {played.get('preview')} margin {played.get('race_margin')} "
                f"fuel {played.get('fuel_load_kg')} ballast {played.get('ballast_kg')} "
                f"wings {played.get('front_wing')}/{played.get('rear_wing')} "
                f"stops {len(facts['pit_stops']) if 'pit_stops' in facts else 'unknown'}"
            )))
        else:
            reasons = facts.get("round_end_reasons") or {}
            reason_text = ", ".join(f"{count} {name}" for name, count in sorted(reasons.items())) or "no recording"
            weapons = ",".join(facts.get("our_weapons") or []) or "none"
            rows.append((created, (
                f"{when} {played.get('name')} buy {played.get('buy_style')} site {played.get('default_site')} "
                f"K {facts.get('our_kills')} D {facts.get('our_deaths')} "
                f"shots {facts.get('our_shots')} hits {facts.get('our_hits')} "
                f"weapons {weapons} plants {facts.get('our_plants')} "
                f"reasons {reason_text} rounds {result.get('round_wins')} score {result.get('seat_score')}"
            )))
    rows.sort(key=lambda item: item[0])
    if not rows:
        return ""
    lines = [
        "Series: every finished match, oldest first. Compare these rows before using the latest one.",
    ]
    if game == "racing":
        lines.append("Compare only matched tracks/conditions; car fields and controller fields are both live levers. Place alone does not identify a cause.")
    else:
        lines.append("A team score of 1 can hide a seat that did not play. Deaths, weapons and plants are the measured result.")
    lines.extend(text for _created, text in rows[-36:])
    return "\n".join(lines)


def evidence(state: dict, mode: str, focus: dict | None, counter=None, client=None) -> str:
    focus = focus or {}
    plan = state["B"].get("strategy") or {}
    played = played_for_result(state, focus)
    if mode == "init":
        return "\n".join([
            "Outcome: initial checkpoint. Our own match has not finished.",
            "A leaderboard rating is not a description of how those players play.",
            "Field: " + json.dumps(focus, ensure_ascii=False),
            "Previous Plan B: " + json.dumps({"name": plan.get("name"), "thesis": plan.get("thesis")}, ensure_ascii=False),
        ])
    game = "racing" if played.get("stops") else "fps"
    if game=='fps':
        if client:
            try:fps_reference.refresh(client,GAMES['fps']['competition_id'])
            except (AgxError,OSError,ValueError) as exc:log(f'fps: public reference unavailable; using cached research ({type(exc).__name__})')
        return fps_workflow.evidence(state,focus,played)
    reading = situation_text(game, focus, played)
    trace_evidence = training.text(focus)
    if trace_evidence:
        reading += '\n' + trace_evidence
    if state.get('replay_feedback'):
        reading += '\nRecorded-input regression failure in the draft:\n' + state['replay_feedback']
    if state.get('behavior_feedback'):
        reading += '\nPaired real-input behavior assessment:\n'+state['behavior_feedback']
    if game=='fps' and (state.get('fps_combat_focus') or {}).get('active'):
        reading += '\n'+fps_combat.focus_text(focus,state['fps_combat_focus'])
    session = state.get('session') or {}
    if session.get('enabled'):
        reading += ('\nTIMED LOCAL SESSION: these are unplayed hypotheses, not new match results. '
                    'Do not claim the last local revision won. Avoid functionally repeated proposals. '
                    'Recent session revisions: '+json.dumps(session.get('candidates',[])[-10:]))
    if state.get("draft_context_note"):
        reading += "\n" + state["draft_context_note"]
    series = match_series(game, focus, played)
    if series:
        reading = series + "\n" + reading
    if played.get("stops"):
        reading += "\n" + racing_workflow.coverage_text(racing_workflow.coverage(state, STATE / "logs" / "racing"))
        reading += "\n" + racing_workflow.replay_evidence(focus,STATE/'logs'/'racing')
        if state.get("asset_repair_note"):
            reading += "\nAsset binding context: " + state["asset_repair_note"]
        tried = tried_stops(state)
        if tried:
            places = {kind: rank + 1 for kind, rank in tried.items()}
            missing = [kind for kind in ("undercut", "overcut") if kind not in tried]
            reading += "\nMeasured stop places: " + json.dumps(places)
            if missing:
                reading += f"\nThe missing experiment is {missing[0]}."
            else:
                best_rank = min(tried.values())
                tied = [kind for kind, rank in tried.items() if rank == best_rank]
                if len(tied) > 1:
                    reading += f"\nBoth stops are tied at place {best_rank + 1}. The stop is not the measured difference."
                else:
                    reading += f"\nThe better stop so far is {tied[0]}."
    elif played.get("ct_setup"):
        reading += "\n" + fps_measurement_text(fps_records(state))
    guide = stall_guide(game, state)
    if game == "fps":
        fault = combat_fault(focus)
        if fault:
            guide = f"{guide}\n{fault}" if guide else fault
        block = body_block_fault(state)
        if block:
            guide = f"{guide}\n{block}" if guide else block
    if guide:
        reading += "\n" + guide
    sim = sim_insight(client, game, focus)
    if sim:
        reading += "\n" + sim
    if mode == "win":
        behind = list(focus.get("behind") or [])
        if len(behind) < 3:
            for person in focus.get("standings_behind") or []:
                if len(behind) >= 3:
                    break
                behind.append(person)
        counter_result = counter if counter is not None else state["B"].get("last")
        lines = [
            "Outcome: win.",
            reading,
            "Also read the counter result and these three players behind us.",
            "Counter result: " + json.dumps(counter_result or "Plan B has not raced yet", ensure_ascii=False),
            "Three players immediately behind us: " + json.dumps(behind[:3], ensure_ascii=False),
            "Recent admission faults: " + json.dumps(state.get("faults") or [], ensure_ascii=False),
        ]
        if played.get("stops"):
            lines.insert(
                1,
                "The win stays the baseline. The next race still runs one faster revision. "
                "The target is a lower finish time.",
            )
        return "\n".join(lines)
    best_last = state["A"].get("last") or {}
    return "\n".join([
        "Outcome: loss.",
        reading,
        "Base the next Plan B on this situation, the best result, and the previous Plan B.",
        "Best result: " + json.dumps({
            "name": (state["A"].get("strategy") or {}).get("name"),
            "rank": best_last.get("rank"),
            "seat_score": best_last.get("seat_score"),
            "team_rounds": best_last.get("team_rounds"),
        }, ensure_ascii=False),
        "Who finished ahead this time: " + json.dumps(focus.get("ahead") or best_last.get("ahead") or [], ensure_ascii=False),
        "Previous Plan B: " + json.dumps({"name": plan.get("name"), "thesis": plan.get("thesis")}, ensure_ascii=False),
        "Plan B result: " + json.dumps(state["B"].get("last") or "not raced yet", ensure_ascii=False),
        "Recent admission faults: " + json.dumps(state.get("faults") or [], ensure_ascii=False),
    ])


def _measured_results(state: dict) -> list[dict]:
    """Finished matches in time order, one row per match."""
    rows = []
    for entry in state.get("history") or []:
        for slot in ("a", "b"):
            result = entry.get(slot)
            if isinstance(result, dict) and result.get("match_id"):
                rows.append(result)
    rows.sort(key=lambda result: result.get("created_at") or "")
    unique = []
    seen = set()
    for result in rows:
        identity = result["match_id"]
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(result)
    return unique


def _finish_place(result: dict):
    """Recording position when it exists. Lower is better."""
    facts = ((result.get("recording") or {}).get("facts") or {})
    position = facts.get("finish_position")
    if facts.get("finished") is False:
        return None
    if isinstance(position, int):
        return position
    rank = result.get("rank")
    if isinstance(rank, int) and rank < 90:
        return rank + 1
    return None


def combat_fault(focus: dict | None) -> str:
    """A measured combat defect the strategy book cannot fix, so the code must change."""
    facts = ((focus or {}).get("recording") or {}).get("facts") or {}
    if not facts:
        return ""
    faults = []
    shots, hits = facts.get("our_shots") or 0, facts.get("our_hits") or 0
    if shots and not hits:
        faults.append(
            f"This seat fired {shots} shots and landed 0 hits, using only "
            f"{', '.join(facts.get('our_weapons') or []) or 'no weapon'}. "
            "The local gate's aim check passed, so the gate does not reproduce the real engine. "
            "Compare visible target range, weapon readiness, alignment and fire timing before choosing a cause."
        )
    if not shots and (facts.get("our_deaths") or 0) >= 5:
        faults.append(
            f"This seat fired 0 shots and died {facts.get('our_deaths')} times. Inspect visible targets, "
            "range cutoffs, weapon readiness and blocked movement; zero shots alone does not identify the cause."
        )
    trace=(focus or {}).get('training') or {};counts=trace.get('counts') or {}
    if facts.get("attack_rounds") and not facts.get("our_plants") and trace.get('status')=='verified' and counts.get('carrier_calls',0)>0:
        faults.append(
            f"This seat attacked {facts['attack_rounds']} rounds and planted 0 times; the own-seat trace "
            f"contains {counts['carrier_calls']} carrier decisions. Check site proximity, time remaining, "
            "threats and use=plant opportunities before attributing this to routing. Field plants include opponents."
        )
    weapons = {weapon for weapon in (facts.get("our_weapons") or []) if weapon}
    shots = facts.get("our_shots") or 0
    if shots and weapons and weapons <= {"pistol", "usp"}:
        faults.append(
            f"Every one of the {shots} shots was a pistol ({', '.join(sorted(weapons))}). "
            "Inspect available cash, purchase receipts and active-slot selection before attributing this to a failed rifle buy."
        )
    deaths = facts.get("our_deaths") or 0
    rounds = facts.get("rounds") or 0
    if rounds and deaths >= 10:
        faults.append(
            f"This seat died {deaths} times in {rounds} rounds. Review per-round engagement and objective opportunities; "
            "deaths alone do not identify aiming, buying or navigation as the cause."
        )
    if not faults:
        return ""
    return (
        "Coach guidance: investigate these measured contribution signals. "
        + " ".join(faults)
        + " A team win does not excuse it. Rewrite agent.py and keep the book unless a book value "
        "is the measured cause."
    )


def body_block_fault(state: dict) -> str:
    """Describe trace-backed immobilization without inventing its collision cause."""
    focus=state.get('_candidate_focus') or (state.get('A') or {}).get('last') or {}
    trace=focus.get('training') or {}
    episodes=((trace.get('behavior') or {}).get('opportunities') or {}).get('blocked_responses',0)
    if trace.get('status')=='verified' and episodes:
        return (f'Coach guidance: own-seat inputs contain {episodes} immobilization episodes with commanded movement, '
                'unchanged position and no visible threat. Inspect waypoint elevation, graph traversal and recovery. '
                'These observations do not distinguish geometry collision from teammate obstruction.')
    return ''


def stall_guide(game: str, state: dict) -> str:
    """Tell oMLX the last change failed. Empty when the results improved."""
    results = _measured_results(state)
    if game == "racing":
        places = [(result, _finish_place(result)) for result in results]
        places = [(result, place) for result, place in places if place is not None]
        if len(places) < 2:
            return ""
        previous, latest = places[-2][1], places[-1][1]
        if latest < previous:
            return ""
        change = "stayed the same" if latest == previous else "got worse"
        return (
            "Coach guidance: no improvement. "
            f"The previous finish was place {previous} and this finish is place {latest} ({change}). "
            "These places alone do not identify a cause; track, field and weather may differ. "
            "Read the coverage ledger and compare live car fields as well as controller gains. "
            "Read actual stop events; do not assume every wet race has no stops. "
            "Change one informative lever as a hypothesis, not a claimed isolated cause."
        )
    if len(results) < 2:
        return ""
    if we_won("fps", results[-1]) or we_won("fps", results[-2]):
        return ""
    return (
        "Coach guidance: two matches in a row were losses. They may have used the same uploaded version. "
        "Compare source/version provenance and this seat's opportunities; team outcomes alone do not establish "
        "that a local revision failed. Use paired response checks to choose one measured behavior change."
    )


def choose(game: str, left: dict, right: dict) -> str:
    """Return the slot letter that scored better. A tie stays with A."""
    if not right:
        return "A"
    if not left:
        return "B"
    if game == "racing":
        left_rank = _finish_place(left)
        right_rank = _finish_place(right)
        if right_rank is None:
            return "A"
        if left_rank is None:
            return "B"
        return "B" if right_rank < left_rank else "A"
    def fps_key(item):
        score = item.get("seat_score")
        rounds = item.get("team_rounds")
        facts = (item.get("recording") or {}).get("facts") or {}
        healthy = 0 if combat_fault(item) else 1
        n = max(1, facts.get("rounds") or 1)
        return (healthy, score if score is not None else -1, rounds if rounds is not None else -1,
                (facts.get("our_kills") or 0) / n, (facts.get("our_hits") or 0) / n,
                -(facts.get("our_deaths") or 0) / n)
    return "B" if fps_key(right) > fps_key(left) else "A"


def sampling_for(cfg: Config, state: dict, mode: str, kind: str) -> dict:
    """Explore harder after a loss, consolidate after a win.

    Code stays cool: a hot agent.py fails the gate on syntax rather than on strategy.
    """
    streak = int(state.get("loss_streak") or 0)
    if kind == "code":
        temp = cfg.omlx_temp + 0.10 * streak
        cap = 0.6
    elif mode == "win":
        temp = cfg.omlx_temp - 0.10
        cap = 1.0
    elif mode == "pace":
        temp = cfg.omlx_temp
        cap = 1.0
    else:
        temp = cfg.omlx_temp + 0.25 + 0.15 * streak
        cap = 1.0
    temp = round(max(0.1, min(cap, temp)), 2)
    top_k = cfg.omlx_top_k if temp < 0.7 else max(cfg.omlx_top_k, 40)
    return {"temperature": temp, "top_p": cfg.omlx_top_p, "top_k": top_k}


def _same_book(a: dict, b: dict) -> bool:
    """True when two books differ only in name/thesis: no functional lever changed."""
    def strip(d):
        return {k: v for k, v in (d or {}).items() if k not in ("name", "thesis")}
    return strip(a) == strip(b)


def effective_proposal(game: str, raw: dict, best: dict, live_code: str | None = None) -> dict:
    if game == "fps":
        reads = set(code_reads(live_code or runtime_code("fps")))
        raw = {k: v for k, v in raw.items() if k in reads | {"name", "thesis"}}
    candidate = clamp(game, raw, best)
    return candidate if game == "racing" else caused_change(game, candidate, best)


def proposal_problem(game: str, candidate: dict, best: dict) -> str:
    changed = {k for k in set(candidate) | set(best)
               if k not in ("name", "thesis") and candidate.get(k) != best.get(k)}
    if not changed:
        return "No functional change. Choose one actual live lever."
    if game == "racing" and len(changed) > 1 and not changed <= {"start_compound", "stops"}:
        return "Multiple independent fields changed: " + ", ".join(sorted(changed)) + ". Change ONE scalar field or the coupled tyre/stop schedule; keep the rest copied."
    return ""


def propose(cfg: Config, game: str, best: dict, text: str, mode: str = "loss", trace: dict | None = None,
            sampling: dict | None = None, live_code: str | None = None, exploration=None) -> dict:
    messages = plan_messages(game, best, text, mode, exploration=exploration)
    if mode != "init":
        diagnosis = diagnose(cfg, game, messages, sampling)
        if trace is not None:trace['diagnosis']=json.loads(diagnosis)
        messages += [{"role": "user", "content": "Implement this accepted diagnosis:\n" + diagnosis}]
    envelope = chat_json(cfg.omlx_url, cfg.omlx_model, messages, cfg.omlx_key,
        timeout=120, max_tokens=2400 if exploration is not None else 2000,
        sampling=sampling, think=False, label=f"{game} · {mode} artifact")
    def normalize(raw):
        raw = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else raw
        return effective_proposal(game, raw, best, live_code)
    candidate = normalize(envelope)
    def problem():
        basic = proposal_problem(game, candidate, best)
        if basic: return basic
        if game == "racing" and exploration is not None:
            return racing_workflow.experiment_problem(envelope, candidate, best, exploration)
        return ""
    for _ in range(2):
        issue = problem()
        if not issue: break
        log(f"{game}: rejected proposal: {issue}")
        reply = chat_text(cfg.omlx_url, cfg.omlx_model,
            messages + [{"role": "assistant", "content": json.dumps(envelope, ensure_ascii=False)},
                        {"role": "user", "content": issue +
                         " Correct the proposal. Preserve every other baseline field. " +
                         ("Return strategy AND experiment with BOTH dry_hypothesis and wet_hypothesis; simulation_effect_s=null." if exploration is not None else "Return one JSON object only.")}],
            cfg.omlx_key, timeout=120, max_tokens=2400 if exploration is not None else 1500,
            sampling=sampling, think=False,label=f"{game} · {mode} proposal repair")
        try:
            envelope = _parse_json(reply)
        except OmlxError:
            break
        candidate = normalize(envelope)
    issue = problem()
    if issue:
        raise OmlxError(f"{game}: rejected proposal after retries: {issue}")
    if trace is not None:
        trace["raw"] = candidate
        if game == "racing": trace["experiment"] = envelope.get("experiment") or {}
    return candidate


def report(state: dict) -> str:
    lines = []
    for slot in ("A", "B"):
        last = state[slot].get("last")
        if last:
            lines.append(f"Slot {slot} last result: {json.dumps(last, ensure_ascii=False)}")
    if state.get("history"):
        lines.append("Earlier decisions: " + json.dumps(state["history"][-3:], ensure_ascii=False))
    if state.get("faults"):
        lines.append("Recent admission faults: " + json.dumps(state["faults"][-3:], ensure_ascii=False))
    return "\n".join(lines) or "No finished match yet. Counter the written plan."


def waiting(rnd: dict, slot: str) -> str:
    info = (rnd or {}).get("next_round") or {}
    start = info.get("start_at", "unknown")
    interval = info.get("interval_seconds", 7200)
    note = " round_state=failed." if (rnd or {}).get("round_state") == "failed" else ""
    return f"waiting for {slot}'s match.{note} next window {start} ({int(interval) // 60} min)"


def round_payload(client: Agx, competition_id: str) -> dict:
    _status, _headers, body = client.get(f"/api/v1/competitions/{competition_id}/round")
    data = loads(body)
    return data if isinstance(data, dict) else {}


def public_round(client: Agx, competition_id: str) -> str:
    info = round_payload(client, competition_id).get("next_round") or {}
    if not info:
        return "unavailable"
    minutes = int(info.get("interval_seconds") or 0) // 60
    return f"{info.get('start_at')} for {minutes} min ({info.get('state') or info.get('timezone') or 'UTC'})"


FREEZE_GUARD_SECONDS = 1800
REFINE_MAX_ITERS = 12


def refine_deadline(rnd: dict):
    """Stop refinement early enough to leave time for submission."""
    info = (rnd or {}).get("next_round") or {}
    start_raw = info.get("start_at")
    if not start_raw:
        return None
    return datetime.fromisoformat(start_raw) - timedelta(seconds=FREEZE_GUARD_SECONDS + 120)


def activation_time(rnd: dict) -> str:
    info = (rnd or {}).get("next_round") or {}
    start_raw = info.get("start_at")
    if not start_raw:
        return datetime.now(timezone.utc).isoformat()
    start = datetime.fromisoformat(start_raw)
    interval = timedelta(seconds=int(info.get("interval_seconds") or 7200))
    end_raw = info.get("end_at")
    end = datetime.fromisoformat(end_raw) if end_raw else start + interval
    now = datetime.now(timezone.utc)
    if start <= now < end or now >= start - timedelta(seconds=90):
        return (start + interval).isoformat()
    return start.isoformat()


def quota_open(cfg: Config, api_key: str) -> bool:
    """AGX_MAX_UPLOADS_PER_DAY=0 means unlimited: full automation, no local cap."""
    if cfg.max_uploads <= 0:
        return True
    ledger = _ledger()
    day = datetime.now(timezone.utc).date().isoformat()
    return int(ledger.get(day, {}).get(_ident(api_key), 0)) < cfg.max_uploads


def note_upload(api_key: str) -> None:
    ledger = _ledger()
    day = datetime.now(timezone.utc).date().isoformat()
    bucket = ledger.setdefault(day, {})
    ident = _ident(api_key)
    bucket[ident] = int(bucket.get(ident, 0)) + 1
    # Keep only the last two days.
    for old in list(ledger):
        if old not in {day, (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()}:
            ledger.pop(old, None)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, indent=2))


def _ledger() -> dict:
    if not LEDGER.is_file():
        return {}
    try:
        return json.loads(LEDGER.read_text())
    except json.JSONDecodeError:
        return {}


def _ident(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:8]


def digest(strategy: dict, code: str | None = None) -> str:
    return hashlib.sha256((canonical(strategy) + "\n" + (code or "")).encode()).hexdigest()


def find_id(data: dict) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("submission_id", "id", "bundle_id"):
        if data.get(key):
            return str(data[key])
    for nested in ("submission", "bundle", "admission"):
        child = data.get(nested)
        if isinstance(child, dict):
            found = find_id(child)
            if found and nested != "admission":
                return found
    return None


def admission_of(data: dict) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "", ""
    admission = data.get("admission")
    fault = str(data.get("fault_code") or data.get("error") or "")
    if isinstance(admission, str):
        return admission, fault
    if isinstance(admission, dict):
        state_name = str(admission.get("status") or admission.get("state") or "")
        fault = str(admission.get("fault_code") or fault)
        return state_name, fault
    for key in ("status", "state"):
        if data.get(key) in ("passed", "failed", "rejected", "queued", "running"):
            return str(data[key]), fault
    return "", fault


def _retry_header(headers) -> int:
    value = headers.get("Retry-After", "")
    if str(value).isdigit():
        return max(1, int(value))
    return 3


def load_state(game: str) -> dict:
    path = STATE / f"{game}.json"
    if path.is_file():
        state = json.loads(path.read_text())
        state.setdefault("phase", "play_A")
        state.setdefault("generation", 1)
        state.setdefault("history", [])
        state.setdefault("faults", [])
        for slot in ("A", "B"):
            state.setdefault(slot, {})
            state[slot].setdefault("seen", [])
        if not state["A"].get("strategy"):
            state["A"]["strategy"] = seed(game)
        return state
    return {
        "phase": "play_A",
        "manual_review": True,
        "generation": 1,
        "history": [],
        "faults": [],
        "A": {"strategy": seed(game), "seen": []},
        "B": {"strategy": None, "seen": []},
    }


def save_state(game: str, state: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    name = f"draft_work_{game}.json" if state.get("_draft_workspace") else f"{game}.json"
    target = STATE / name
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    temporary.replace(target)
