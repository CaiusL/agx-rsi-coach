"""Idempotent match-result accounting, independent of proposals and A/B slots."""
from datetime import datetime, timezone
import json

SCHEMA = 1


def outcome(game, summary):
    if summary.get('status') != 'succeeded':
        return 'void'
    if game == 'fps':
        rounds, team = summary.get('round_wins') or [], summary.get('team')
        if type(team) is int and len(rounds) >= 2 and 0 <= team < len(rounds) and all(type(n) in (int,float) for n in rounds):
            ours, theirs = rounds[team], max(n for i,n in enumerate(rounds) if i != team)
            return 'win' if ours > theirs else 'loss' if ours < theirs else 'draw'
        score = summary.get('seat_score')
        if score == 1: return 'win'
        if score == 0: return 'loss'
        if score == .5: return 'draw'
        return 'unknown'
    facts = (summary.get('recording') or {}).get('facts') or {}
    if facts.get('finished') is False: return 'loss'
    place = facts.get('finish_position')
    if type(place) is int and place >= 1: return 'win' if place == 1 else 'loss'
    rank = summary.get('rank')
    if type(rank) is int and 0 <= rank < 90: return 'win' if rank == 0 else 'loss'
    return 'unknown'


def _time(row):
    value = row.get('created_at') or row.get('finished_at') or ''
    try:
        return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError):
        return float('-inf')


def sync(state, game, summaries):
    """Merge all completed matches by ID; late arrivals recompute chronological streaks."""
    ledger = {row['match_id']:row for row in state.get('match_outcomes',[]) if row.get('match_id')}
    for summary in summaries:
        mid = summary.get('match_id')
        if not mid or summary.get('status') != 'succeeded': continue
        row = dict(ledger.get(mid) or {})
        for key in ('match_id','status','created_at','finished_at','seat','team','rank','seat_score','round_wins'):
            if summary.get(key) is not None: row[key] = summary[key]
        facts = (summary.get('recording') or {}).get('facts') or {}
        if any(k in facts for k in ('finished','finish_position')):
            row['recording'] = {'facts':{k:facts[k] for k in ('finished','finish_position') if k in facts}}
        row['outcome'] = outcome(game,row)
        ledger[mid] = row
    rows = sorted(ledger.values(),key=lambda r:(_time(r),r['match_id']))
    count = 0; boundary = None
    for row in reversed(rows):
        if row['outcome'] != 'loss':
            boundary = row; break
        count += 1
    info = {'schema':SCHEMA, 'scope':'primary account, completed competition matches',
            'exact':bool(boundary and boundary['outcome'] in ('win','draw')),
            'counted_matches':len(rows), 'last_match_id':rows[-1]['match_id'] if rows else None,
            'last_result_at':(rows[-1].get('created_at') or rows[-1].get('finished_at')) if rows else None,
            'boundary_match_id':boundary['match_id'] if boundary else None,
            'boundary_outcome':boundary['outcome'] if boundary else None}
    changed = state.get('match_outcomes') != rows or state.get('loss_streak') != count or state.get('loss_streak_info') != info
    state.update(match_outcomes=rows,loss_streak=count,loss_streak_info=info)
    return changed


def eligible(match):
    return (match.get('status') == 'succeeded' and not match.get('is_canary')
            and match.get('rated') is not False and match.get('mode') in (None,'ranked'))


def bootstrap(state, game, folder, slots=('A','B')):
    if (state.get('loss_streak_info') or {}).get('schema') == SCHEMA: return False
    summaries = []
    for history in state.get('history') or []:
        summaries.extend(history.get(slot.lower()) or {} for slot in slots)
    summaries.extend((state.get(slot) or {}).get('last') or {} for slot in slots)
    for path in folder.glob('*.json'):
        try: record = json.loads(path.read_text())
        except (OSError,ValueError): continue
        if record.get('slot') not in (None,*slots): continue
        match, result = record.get('match') or {}, record.get('result') or {}
        if match and not eligible(match): continue
        summaries.append(result)
    return sync(state,game,summaries)
