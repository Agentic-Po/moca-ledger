"""Automatic lifecycle and private notification handoff; no identity data here."""
import datetime as dt
import re

FRESH_SECONDS = 86400
TERMINAL = {'closed', 'archived'}
WALLET = re.compile(r'0x[0-9a-f]{40}\Z')


def wallet(f):
    key = str(f.get('key') or '').lower().removeprefix('exit:')
    if WALLET.fullmatch(key):
        return key
    return next((w.lower() for w in f.get("entities", []) if isinstance(w, str) and WALLET.fullmatch(w.lower())), None)


def stamp(value):
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=dt.UTC).timestamp()
    except (ValueError, TypeError):
        return 0


def fresh(f, now):
    return stamp(f.get('ts')) >= now - FRESH_SECONDS


def reopen(cur, candidate, now):
    """New qualifying episode reopens an automatic archive, never a manual close."""
    automatic = cur.get('status') == 'archived' or (cur.get('status') == 'closed' and cur.get('status_by') == 'reward-policy-v2')
    gap = stamp(candidate.get('episode_first')) > stamp(cur.get('episode_last') or cur.get('ts')) + 3600
    if automatic and fresh(candidate, now) and (gap or cur.get('status_by') == 'reward-policy-v2'):
        for key in ('status', 'status_ts', 'status_by', 'status_note', 'ack_role', 'ack_by', 'ack_ts',
                    'ack_note', 'snooze_until', 'last_sent', 'enrich_requested', 'triage_decision',
                    'triage_requested', 'triage_ts', 'needs_decision', 'backfill', 'fallback_sent', 'triage_queued_at'):
            cur.pop(key, None)
        cur['pending_send'] = True
        cur['first_ts'] = candidate.get('episode_first') or candidate.get('first_ts')
        return True
    return False


def prepare(state, now):
    """Archive inactivity; privately verify wallet candidates; keep observations quiet."""
    for f in state.get('open', {}).values():
        if not fresh(f, now):
            if f.get('status') not in TERMINAL:
                f['previous_status'] = f.get('status')
                f.update(status='archived', status_by='automatic-inactivity',
                         status_ts=dt.datetime.fromtimestamp(now, dt.UTC).isoformat(),
                         status_note='no qualifying activity in the last 24 hours; evidence retained')
            f.update(pending_send=False, needs_triage=False, needs_decision=False)
            continue
        if f.get('status') in TERMINAL or ((f.get('ack_role') or (f.get('ack_by') and f.get('ack_by') != 'go-live-seed')) and not (f.get('pending_send') and f.get('escalation') in ('activity after containment','still growing since you reported it'))):
            continue
        if wallet(f):
            # A digest observation is verified once per episode and on material growth;
            # its existence alone never asks a person to act.
            growth = False
            try:
                growth = float(f.get('value') or 0) > float(f.get('triage_value') or 0) * 1.5
            except (ValueError, TypeError):
                pass
            periodic = (stamp(f.get('triage_ts')) > 0 and stamp(f.get('triage_ts')) < now - 6*3600
                        and stamp(f.get('ts')) > stamp(f.get('triage_finding_ts')))
            first = not f.get('triage_generation') and not f.get('backfill')
            if f.get('pending_send') or first or ((growth or periodic) and not f.get('needs_triage')):
                f['triage_generation'] = int(f.get('triage_generation') or 0) + 1
                f['triage_value'] = f.get('value')
                f.update(needs_triage=True, needs_decision=False, triage_decision='pending_verification',
                         delivery_route='private', triage_requested=False, triage_queued_at=dt.datetime.fromtimestamp(now,dt.UTC).isoformat())
            f['pending_send'] = False
            critical = f.get('signal') in {'S-X','4b','S-C'} or (f.get('tier') == 'page' and not f.get('shadow_of'))
            overdue = now - stamp(f.get('triage_queued_at')) > 3600
            if critical and f.get('needs_triage') and overdue and not f.get('fallback_sent'):
                f.update(delivery_route='fallback', pending_send=True, needs_decision=True)
        elif f.get('tier') == 'digest':
            f.update(pending_send=False, needs_decision=False, triage_decision='observe')
    state['triage_version'] = 1


def apply_results(state, receipts):
    """Accept only matching-generation, identity-free private results."""
    for fid, result in receipts.items():
        f = state.get('open', {}).get(fid)
        if not f or not isinstance(result, dict) or f.get('status') in TERMINAL:
            continue
        if result.get('generation') != f.get('triage_generation'):
            continue
        if int(f.get('triage_handled_generation') or 0) >= int(result.get('generation') or 0):
            continue
        decision = result.get('decision')
        if decision not in {'observe', 'expected', 'pending_verification', 'exception'}:
            continue
        f.update(triage_decision=decision, needs_decision=decision == 'exception',
                 needs_triage=bool(result.get('retry')), triage_ts=result.get('at'), triage_finding_ts=result.get('finding_ts'))
        if result.get('message_id'):
            f.update(tg_message_id=int(result['message_id']), last_sent=result.get('at'), send_ok=True, delivery_route='private')
        if decision != 'pending_verification':
            f['pending_send'] = False


def queue_case(f, now):
    if not fresh(f, now) or f.get('status') in TERMINAL or f.get('backfill'):
        return False
    if f.get('delivery_route') == 'private':
        return bool(f.get('needs_decision'))
    return f.get('tier') in {'page', 'notify'} and not f.get('ack_role') and f.get('ack_by') != 'go-live-seed'
