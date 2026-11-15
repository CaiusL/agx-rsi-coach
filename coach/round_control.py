"""An explicit per-round author reservation, independent of model settings."""
from datetime import datetime, timezone
from coach.sessions import timestamp


def reservation(state, now=None):
    item = state.get('round_authority') or {}
    now = now or datetime.now(timezone.utc)
    if item.get('author') == 'codex' and item.get('round_at') and now < timestamp(item['round_at']):
        return item
    return None


def require_model_allowed(state, now=None):
    item = reservation(state, now)
    if item:
        raise ValueError(f"Round {item['round_at']} is reserved for the direct-authored candidate; model generation is disabled.")


def require_upload_allowed(state, draft, now=None):
    item = reservation(state, now)
    if item and (draft.get('id') != item.get('draft_id') or draft.get('author') != 'codex'):
        raise ValueError('This round only permits the pinned direct-authored draft.')
