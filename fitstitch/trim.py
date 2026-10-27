"""Trim an activity at the start and/or the end, as if the watch had been
started later or stopped earlier: for a watch left running after the
finish, or started before the real start.

Records, events and other data outside the kept span are removed; laps,
splits and time in zone are cut and recomputed from the records that
remain (see summary.py); the session is rebuilt; accumulated record fields
restart from zero when the start is trimmed. Values only the watch can
compute are kept as they were and reported.
"""
from .fitraw import Msg
from .merge import (Activity, StitchError, utc_offset, FILE_ID, EVENT, RECORD, LAP, SESSION, ACTIVITY,
                    DEVELOPER_DATA_ID, FIELD_DESCRIPTION,
                    TIME_IN_ZONE, SPLIT, SPLIT_SUMMARY, GARMIN_METRICS, GARMIN_BEST_EFFORT,
                    EVENT_TIMER, EVENT_START, EVENT_STOP_ALL)
from . import fitraw
from . import summary

SUMMARY = {SESSION, LAP, ACTIVITY, TIME_IN_ZONE, SPLIT, SPLIT_SUMMARY, GARMIN_METRICS, GARMIN_BEST_EFFORT}
ACCUMULATED = [num for num, fi in fitraw.PROFILE['messages'][RECORD]['fields'].items()
               if fi['is_accumulated']]


class TrimError(StitchError):
    pass


def _span(m):
    """(start, end) FIT timestamps a lap or split covers."""
    start = m.raw('start_time')
    if start is None:
        return None
    if m.raw('end_time') is not None:
        return start, m.raw('end_time')
    return start, start + (m.get('total_elapsed_time') or 0)


def _keep_body(msgs, first_rec, t0, t1):
    """Which messages after the first record survive: those timestamped in
    [t0, t1], and untimestamped ones following a surviving message."""
    keep, cur = {}, True
    for i in range(first_rec, len(msgs)):
        m = msgs[i]
        if m.gnum in SUMMARY:
            continue
        if m.gnum in (DEVELOPER_DATA_ID, FIELD_DESCRIPTION):
            keep[i] = True  # declarations of developer fields used later: always kept
            continue
        ts = m.raw('timestamp')
        if ts is not None:
            cur = t0 <= ts <= t1
        keep[i] = cur
    return keep


def trim(act, start=None, end=None):
    """Keep act between FIT timestamps start and end (None keeps that end as
    it is). Returns (trimmed Activity, warnings)."""
    recs = act.of(RECORD)
    times = [r.raw('timestamp') for r in recs]
    t0 = act.start if start is None else next((t for t in times if t >= start), None)
    t1 = act.end if end is None else next((t for t in reversed(times) if t <= end), None)
    if t0 is None or t1 is None or t1 - t0 < 1 or \
            sum(1 for t in times if t0 <= t <= t1) < 2:
        raise TrimError('nothing would be left of the activity')
    trim_start, trim_end = t0 > act.start, t1 < act.end
    # an end that is not trimmed keeps everything on its side, even records
    # a watch wrote a fraction of a second past the session's own end
    lo = t0 if trim_start else float('-inf')
    hi = t1 if trim_end else float('inf')
    warnings = []
    events = act.of(EVENT)
    whole = summary.timer_intervals(events, act.start, act.end)
    intervals = summary.clip(whole, t0, t1)
    running = lambda t: any(s <= t < e for s, e in whole)
    kept_recs = [r for r in recs if lo <= r.raw('timestamp') <= hi]
    before = [r for r in recs if r.raw('timestamp') < t0]

    # accumulated fields restart from zero at the new start
    base = {}
    if trim_start:
        for num in ACCUMULATED:
            base[num] = next((r.raw(num) for r in reversed(before) if r.raw(num) is not None), None)

    def rebase(r):
        r = r.copy()
        for num, b in base.items():
            if b is not None and r.raw(num) is not None:
                r.set_raw(num, r.raw(num) - b)
        return r

    def cut(m, a, b, orig):
        """Recompute summary m for [a, b]; orig: its original records."""
        span_recs = [r for r in kept_recs if a <= r.raw('timestamp') <= b
                     or (b == t1 and not trim_end and r.raw('timestamp') > b)]
        prev = [r for r in recs if r.raw('timestamp') < a and r.get('distance') is not None]
        return summary.resummarise(m, span_recs, a, b, intervals, orig,
                                   prev[-1].get('distance') if prev else 0.0)

    # laps and splits: keep, cut or drop; renumbered
    def recut(msgs_of_type):
        out, old_index = [], {}  # original position -> (new position, changed)
        for pos, m in enumerate(msgs_of_type):
            sp = _span(m)
            if sp is None:
                continue
            a, b = max(sp[0], t0), min(sp[1], t1)
            if b <= a and not (sp[0] == sp[1] and t0 <= sp[0] <= t1):
                continue
            if (a, b) == sp:
                new = m.copy()
            else:
                orig = [r for r in recs if sp[0] <= r.raw('timestamp') <= sp[1]]
                new, _ = cut(m, a, b, orig)
            old_index[pos] = (len(out), (a, b) != sp)
            if new.has('message_index'):
                new.set('message_index', len(out))
            out.append(new)
        return out, old_index

    laps, lap_index = recut(act.of(LAP))
    splits, split_index = recut(act.of(SPLIT))

    session, kept = cut(act.session, t0, t1, recs)
    # Laps the cut doesn't touch keep the watch's own times, which add up to
    # the session's only approximately; the lap at the cut absorbs the rest,
    # so lap times add up to the session's as Garmin's validator requires.
    cut_laps = [laps[j] for j, was_cut in lap_index.values() if was_cut]
    absorb = (cut_laps[-1] if trim_end else cut_laps[0]) if cut_laps else (laps[-1] if laps else None)
    if absorb is not None:
        for name in ('total_elapsed_time', 'total_timer_time'):
            total, own = session.get(name), absorb.get(name)
            if total is None or own is None or any(l.get(name) is None for l in laps):
                continue
            rest = total - sum(l.get(name) for l in laps) + own
            if rest >= 0:
                summary._set(absorb, name, rest)
    if session.has('num_laps'):
        session.set('num_laps', len(laps))
    if session.has('first_lap_index'):
        session.set('first_lap_index', 0)
    if kept:
        warnings.append(f'{len(kept)} session values that only the watch can compute (training effect, '
                        'load, undocumented fields...) were kept as they were, for the whole activity')

    # time in zone: recomputed wherever its lap, split or session changed
    tiz_out = []
    for m in act.of(TIME_IN_ZONE):
        ref, idx = m.raw('reference_mesg'), m.raw('reference_index')
        if ref == SESSION:
            new = summary.recompute_zones(m, kept_recs, intervals)
        elif ref in (LAP, SPLIT):
            table, msgs_new = (lap_index, laps) if ref == LAP else (split_index, splits)
            if idx not in table:
                continue
            j, was_cut = table[idx]
            if was_cut:
                a, b = _span(msgs_new[j])
                new = summary.recompute_zones(m, [r for r in kept_recs if a <= r.raw('timestamp') <= b], intervals)
            else:
                new = m.copy()
            new.set_raw('reference_index', j)
        else:
            new = m.copy()
        tiz_out.append(new)

    # split summaries: rebuilt per split type from the splits that remain
    summaries = []
    for m in act.of(SPLIT_SUMMARY):
        mine = [s for s in splits if s.raw('split_type') == m.raw('split_type')]
        if not mine:
            continue
        new = m.copy()
        tt = sum(s.get('total_timer_time') or 0 for s in mine)
        for name in ('total_timer_time', 'total_distance', 'total_calories', 'total_ascent', 'total_descent',
                     'metabolic_calories'):
            vals = [s.get(name) for s in mine if s.get(name) is not None]
            if vals and new.raw(name) is not None:
                summary._set(new, name, sum(vals) if name not in ('total_calories', 'metabolic_calories',
                                                                  'total_ascent', 'total_descent')
                             else round(sum(vals)))
        for name in ('max_speed', 'max_heart_rate', 'max_power'):
            vals = [s.get(name) for s in mine if s.get(name) is not None]
            if vals and new.raw(name) is not None:
                summary._set(new, name, max(vals))
        for name in ('avg_heart_rate', 'avg_power'):
            pairs = [(s.get(name), s.get('total_timer_time') or 0) for s in mine if s.get(name) is not None]
            if pairs and new.raw(name) is not None and sum(w for _, w in pairs):
                summary._set(new, name, sum(v * w for v, w in pairs) / sum(w for _, w in pairs))
        dist = new.get('total_distance')
        if new.raw('avg_speed') is not None and dist is not None and tt:
            summary._set(new, 'avg_speed', dist / tt)
        if new.has('num_splits'):
            new.set('num_splits', len(mine))
        summaries.append(new)

    if act.of(GARMIN_BEST_EFFORT):
        warnings.append('best efforts (fastest 1 km, 5 km...) were removed: they may have been set in '
                        'the trimmed part, and the app will work them out again')
    if act.of(GARMIN_METRICS):
        warnings.append('the watch\'s end-of-activity metrics (recovery time, training effect...) were '
                        'kept as they were')

    activity = []
    for a in act.of(ACTIVITY):
        a = a.copy()
        summary._set(a, 'total_timer_time', session.get('total_timer_time'))
        if a.has('timestamp') and a.raw('timestamp') is not None and a.raw('timestamp') >= t1:
            off = (a.raw('local_timestamp') - a.raw('timestamp')) if a.raw('local_timestamp') is not None else None
            a.set_raw('timestamp', t1)
            if off is not None and utc_offset(act) is not None:
                a.set_raw('local_timestamp', t1 + off)
        activity.append(a)

    # rebuild in the original order, replacing summaries where they were
    first_rec = next(i for i, m in enumerate(act.msgs) if m.gnum == RECORD)
    keep_body = _keep_body(act.msgs, first_rec, lo, hi)
    queues = {LAP: laps, SPLIT: splits, TIME_IN_ZONE: tiz_out, SPLIT_SUMMARY: summaries,
              SESSION: [session], ACTIVITY: activity}
    placed = {k: False for k in queues}
    out = []
    start_event = Msg.build(EVENT, timestamp=t0, event=EVENT_TIMER, event_type=EVENT_START, event_group=0)
    stop_event = Msg.build(EVENT, timestamp=t1, event=EVENT_TIMER, event_type=EVENT_STOP_ALL, event_group=0)
    has_start = any(e.raw('event') == EVENT_TIMER and e.raw('event_type') == EVENT_START
                    and e.raw('timestamp') == t0 for e in events)
    need_start = trim_start and running(t0) and not has_start
    need_stop = trim_end and running(t1)
    body_started, end_of_body = False, None
    for i, m in enumerate(act.msgs):
        if m.gnum in queues:
            if not placed[m.gnum]:
                out += queues[m.gnum]
                placed[m.gnum] = True
            continue
        if m.gnum == GARMIN_BEST_EFFORT:
            continue
        if m.gnum in SUMMARY:  # other summaries (e.g. Garmin's metrics) stay as they are
            out.append(m)
            continue
        if m.gnum == EVENT and not lo <= (m.raw('timestamp') or t0) <= hi:
            continue  # e.g. the original timer start, before a trimmed start
        if m.gnum == FILE_ID and trim_start and m.has('time_created'):
            m = m.copy()
            m.set_raw('time_created', t0)
        if i < first_rec:
            out.append(m)
            continue
        if not keep_body.get(i, False):
            continue
        if not body_started:
            body_started = True
            if need_start:
                out.append(start_event)
        out.append(rebase(m) if m.gnum == RECORD else m)
        end_of_body = len(out)
    if need_stop:
        out.insert(end_of_body if end_of_body is not None else len(out), stop_event)
    return Activity(act.name, act.header, out), warnings


def _smoothed_speed(recs, intervals, window=60):
    """Speed (m/s) at each record, averaged over the previous `window`
    seconds of running timer, from the records' distance: 0 while the timer
    is stopped, None where the records carry no distance."""
    out = []
    pts = [(r.raw('timestamp'), r.get('distance')) for r in recs]
    j = 0
    for i, (t, d) in enumerate(pts):
        while j < i and pts[j][0] < t - window:
            j += 1
        run = summary.running_time(intervals, pts[j][0], t)
        if run < window / 2:
            out.append(0.0 if i else None)  # timer stopped: not part of the activity
            continue
        if d is None or pts[j][1] is None:
            out.append(None)
            continue
        out.append((d - pts[j][1]) / run)
    return out


def suggest(act, min_tail=180):
    """Suggest where the activity really started and ended: the stretches at
    either end, of at least min_tail seconds, spent standing still or moving
    at vehicle speed. Deliberately conservative: slower stretches (walking
    home after a run, or uphill on a trail run) look alike, so they are left
    for the user to judge. Returns (start, end) FIT timestamps, each None
    when nothing needs trimming at that end."""
    recs = act.of(RECORD)
    if len(recs) < 10:
        return None, None
    intervals = summary.timer_intervals(act.of(EVENT), act.start, act.end)
    speed = _smoothed_speed(recs, intervals)
    moving = sorted(s for s in speed if s is not None and s > 0.5)
    if len(moving) < 10:
        return None, None
    typical = moving[len(moving) // 2]
    times = [r.raw('timestamp') for r in recs]

    vehicle = max(2 * typical, typical + 4)

    def unlike(s):
        return s < 0.4 or s > vehicle

    def tail(indices):
        """First index (walking from the end inwards) where the activity's
        own pace holds for a minute. Records without distance don't count
        either way."""
        streak = None
        for i in indices:
            if speed[i] is None:
                continue
            if unlike(speed[i]):
                streak = None
                continue
            streak = i if streak is None else streak
            if abs(times[i] - times[streak]) >= 60:
                return streak
        return None

    # The smoothed speed finds the stretches robustly but lags a minute
    # behind; the exact moment comes from the speed between records.
    dist = [r.get('distance') for r in recs]
    inst = [None] + [(dist[i] - dist[i - 1]) / (times[i] - times[i - 1])
                     if None not in (dist[i], dist[i - 1]) and times[i] > times[i - 1] else None
                     for i in range(1, len(recs))]

    def moving(i):
        return inst[i] is not None and 0.4 <= inst[i] <= vehicle

    end_i = tail(range(len(recs) - 1, -1, -1))
    if end_i is not None:
        end_i = next((i for i in range(end_i, -1, -1) if moving(i)), end_i)
    start_i = tail(range(len(recs)))
    if start_i is not None:
        back = next((i for i in range(start_i, -1, -1) if times[start_i] - times[i] >= 90), 0)
        start_i = next((i for i in range(back, start_i + 1) if moving(i)), start_i)
        start_i = max(0, start_i - 1)  # the record the first movement is measured from
    end = times[end_i] if end_i is not None and times[-1] - times[end_i] >= min_tail else None
    start = times[start_i] if start_i is not None and times[start_i] - times[0] >= min_tail else None
    return start, end


def point(act, spec):
    """FIT timestamp for a cut point given as elapsed time from the start
    ('1:02:30', '45:00', '90s'), or distance from the start ('10.4km',
    '850m'); with a leading '-', counted back from the end instead."""
    s = spec.strip().lower()
    back = s.startswith('-')
    s = s.lstrip('-+').strip()
    recs = act.of(RECORD)
    if s.endswith(('km', 'm')) and not s.endswith('mm'):
        try:
            metres = float(s[:-2]) * 1000 if s.endswith('km') else float(s[:-1])
        except ValueError:
            raise TrimError(f'not a distance: {spec}') from None
        dist = [(r.raw('timestamp'), r.get('distance')) for r in recs if r.get('distance') is not None]
        if not dist:
            raise TrimError('this activity has no distance data; give a time instead')
        target = dist[-1][1] - metres if back else metres
        return next((t for t, d in dist if d >= target), dist[-1][0])
    try:
        if s.endswith('s'):
            seconds = float(s[:-1])
        else:
            parts = [float(x) for x in s.split(':')]
            if not 1 <= len(parts) <= 3:
                raise ValueError
            seconds = 0
            for x in parts:
                seconds = seconds * 60 + x
            if len(parts) == 1:
                seconds *= 60  # a bare number is minutes
    except ValueError:
        raise TrimError(f'not a time or distance: {spec} (try 1:02:30, -15:00 or 10.4km)') from None
    return act.end - seconds if back else act.start + seconds
