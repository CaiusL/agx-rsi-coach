"""Combat attribution from public events; never invent assists from shared credit."""
import json
import math


def summarize(events,seat,team):
    kills=[e for e in events if e.get('type')=='kill']
    ends=[e for e in events if e.get('type')=='round_end'];rounds=len(ends)
    def killer(event):
        explicit=event.get('killer',event.get('killer_seat'))
        if type(explicit) is int:return explicit
        attackers=event.get('attackers') or []
        return attackers[0] if len(attackers)==1 else None
    ours=[e for e in kills if killer(e)==seat and e.get('target')!=seat]
    shared=[e for e in kills if killer(e) is None and seat in (e.get('attackers')or[]) and e.get('target')!=seat]
    deaths=[e for e in kills if e.get('target')==seat]
    assists=None
    if kills and all(isinstance(e.get('assists',e.get('assisters')),list) for e in kills):
        assists=sum(seat in e.get('assists',e.get('assisters',[])) and killer(e)!=seat for e in kills)
    hits=[e for e in events if e.get('type')=='hit' and e.get('player')==seat and e.get('target')!=seat]
    damage=sum(e['hp_damage'] for e in hits) if all(type(e.get('hp_damage')) in (int,float) and math.isfinite(e['hp_damage']) for e in hits) else None
    first={}
    for e in sorted(kills,key=lambda e:e.get('match_tick',0)):first.setdefault(e.get('round_index'),e)
    opening_kills=sum(killer(e)==seat and e.get('target')!=seat for e in first.values())
    opening_deaths=sum(e.get('target')==seat for e in first.values())
    fires=[e for e in events if e.get('type')=='fire' and e.get('player')==seat]
    no_shot_deaths=sum(not any(f.get('round_index')==e.get('round_index') and f.get('match_tick',0)<=e.get('match_tick',0) for f in fires) for e in deaths)
    return {'our_kills':len(ours),'our_deaths':len(deaths),'our_assists':assists,
        'our_shared_kill_involvements':len(shared),'our_kill_involvements':sum(seat in (e.get('attackers')or[]) and e.get('target')!=seat for e in kills),
        'our_kd_ratio':round(len(ours)/len(deaths),3) if deaths else None,
        'our_kda_ratio':round((len(ours)+assists)/len(deaths),3) if assists is not None and deaths else None,
        'our_kills_per_round':round(len(ours)/rounds,3) if rounds else None,
        'our_deaths_per_round':round(len(deaths)/rounds,3) if rounds else None,
        'our_hp_damage':round(damage,2) if damage is not None else None,
        'our_damage_per_round':round(damage/rounds,2) if damage is not None and rounds else None,
        'our_opening_kills':opening_kills,'our_opening_deaths':opening_deaths,'our_deaths_without_firing':no_shot_deaths,
        'kill_attribution':'explicit killer or sole attacker; unordered shared contributors are not kills or assists',
        'assist_attribution':'explicit' if assists is not None else 'not published; unknown'}


def focus_text(focus,request=None):
    facts=((focus or {}).get('recording')or{}).get('facts')or{}
    keys=('our_kills','our_deaths','our_assists','our_kd_ratio','our_kda_ratio','our_kills_per_round','our_deaths_per_round','our_damage_per_round','our_opening_kills','our_opening_deaths','our_deaths_without_firing','our_shared_kill_involvements','our_weapons','our_shots','our_hits','our_accuracy','assist_attribution')
    return ('COMBAT GOAL REQUESTED BY USER: improve useful kills and damage per round while avoiding needless deaths. '
        'Do not inflate K/D by hiding, abandoning the bomb, or withholding useful engagements. '
        'Assists and KDA ratios stay unknown when attribution is absent. '
        'Recorded statistics belong to the played version, not the current unplayed candidate. '
        'Choose ONE combat change supported by target range, readiness, recoil/alignment and timing evidence. '
        'Preserve navigation recovery and objective checks. The candidate must improve ready aligned engagement-window '
        'coverage or rifle engagement coverage on paired inputs without per-trace regressions; this is not proof of more kills. '
        'Latest recorded match '+str((focus or {}).get('match_id'))+' combat facts: '+json.dumps({k:facts[k] for k in keys if k in facts},ensure_ascii=False)+
        '\nCurrent baseline response audit (not a played result): '+json.dumps((request or {}).get('baseline_audit') or {},ensure_ascii=False))
