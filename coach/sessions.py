"""Per-round local improvement sessions with one scheduled release."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import signal
import uuid

LEAD_SECONDS = 12 * 60
MODEL_BUFFER = 60


class Deadline(BaseException):
    """Bypass model repair/retry catches when the release deadline arrives."""


def timestamp(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None: raise ValueError('Round time requires a timezone')
    return dt


def new(round_info, now=None):
    now = now or datetime.now(timezone.utc)
    target = timestamp(round_info['start_at'])
    # Never fabricate a future schedule from a stale response.
    if target <= now: raise ValueError('AGX has not published a future round')
    return {'id': uuid.uuid4().hex, 'enabled': True, 'status': 'improving',
            'round_at': target.isoformat(),
            'upload_at': (target-timedelta(seconds=LEAD_SECONDS)).isoformat(),
            'model_stop_at': (target-timedelta(seconds=LEAD_SECONDS+MODEL_BUFFER)).isoformat(),
            'attempts': 0, 'accepted': 0, 'no_progress': 0, 'candidates': []}


def stage(session, now=None):
    now = now or datetime.now(timezone.utc)
    if not session.get('enabled'): return 'off'
    if session.get('upload_started') or session.get('status') in ('uploaded','upload_failed','no_candidate','missed_window'):
        return 'complete'
    if now >= timestamp(session['round_at'])-timedelta(minutes=10): return 'missed'
    if now >= timestamp(session['upload_at']): return 'upload'
    if now >= timestamp(session['model_stop_at']): return 'settling'
    if session.get('no_progress',0) >= 3: return 'stalled'
    return 'improve'


@contextmanager
def until(deadline):
    """Wall-clock cap for the whole cycle, including retries and network reads."""
    seconds = (timestamp(deadline)-datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0: raise Deadline()
    previous = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    def expire(*_): raise Deadline()
    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try: yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *old_timer)
        signal.signal(signal.SIGALRM, previous)
