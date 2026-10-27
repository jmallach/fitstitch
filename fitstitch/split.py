"""Split an activity into consecutive parts, the way a device would have
recorded them had the activity been stopped and a new one started.

This exists to test the stitcher against real files: stitching the parts
back together must reproduce the original data stream exactly. The summary
messages of each part are recomputed only as far as the stitcher needs
(times, distance, laps); proprietary summaries (time in zone, splits, Garmin
metrics) are dropped from the parts.
"""
import argparse
import sys

from . import fitraw, summary
from .fitraw import Msg
from .merge import (Activity, load, utc_offset, FILE_ID, EVENT, RECORD, LAP, SESSION, ACTIVITY,
                    TIME_IN_ZONE, SPLIT, SPLIT_SUMMARY, GARMIN_METRICS, GARMIN_BEST_EFFORT,
                    EVENT_TIMER, EVENT_START, EVENT_STOP_ALL)

DROPPED = {TIME_IN_ZONE, SPLIT, SPLIT_SUMMARY, GARMIN_METRICS, GARMIN_BEST_EFFORT}
ACCUMULATED = [num for num, fi in fitraw.PROFILE['messages'][RECORD]['fields'].items()
               if fi['is_accumulated']]


def _timer_intervals(act):
    """[(start, stop)] intervals during which the timer ran."""
    return summary.timer_intervals(act.of(EVENT), act.start, act.end)


def _overlap(intervals, a, b):
    return sum(max(0, min(e, b) - max(s, a)) for s, e in intervals)


def _set(m, name, value):
    if m.has(name) and value is not None:
        m.set(name, value)


def _summarise(m, recs, t_start, t_end, timer, dist0, orig_total_timer):
    """Recompute the fields of a lap/session that the stitcher relies on."""
    m = m.copy()
    _set(m, 'start_time', t_start)
    if m.has('timestamp') and m.raw('timestamp') != m.raw('start_time'):
        m.set_raw('timestamp', t_end)
    elif m.has('timestamp'):
        m.set_raw('timestamp', t_start)
    _set(m, 'total_elapsed_time', t_end - t_start)
    _set(m, 'total_timer_time', timer)
    dists = [r.get('distance') for r in recs if r.get('distance') is not None]
    if dists:
        _set(m, 'total_distance', max(0.0, dists[-1] - dist0))
    ratio = timer / orig_total_timer if orig_total_timer else 0
    for name in ('total_calories', 'total_cycles', 'total_work', 'total_strides'):
        if m.raw(name) is not None:
            m.set(name, round(m.get(name) * ratio))
    for f, src in (('avg_heart_rate', 'heart_rate'), ('avg_power', 'power'), ('avg_cadence', 'cadence')):
        vals = [r.get(src) for r in recs if r.get(src) is not None]
        if vals and m.raw(f) is not None:
            m.set(f, round(sum(vals) / len(vals)))
            mx = f.replace('avg_', 'max_')
            if m.raw(mx) is not None:
                m.set(mx, max(vals))
    if recs:
        p = next((r for r in recs if r.raw('position_lat') is not None), None)
        if p is not None:
            _set(m, 'start_position_lat', p.get('position_lat'))
            _set(m, 'start_position_long', p.get('position_long'))
        q = next((r for r in reversed(recs) if r.raw('position_lat') is not None), None)
        if q is not None:
            _set(m, 'end_position_lat', q.get('position_lat'))
            _set(m, 'end_position_long', q.get('position_long'))
    if m.raw('total_timer_time') is not None and m.raw('total_distance') is not None:
        speed = m.get('total_distance') / timer if timer else 0
        for name in ('avg_speed', 'enhanced_avg_speed'):
            if m.raw(name) is not None:
                try:
                    m.set(name, speed)
                except fitraw.FitError:
                    pass
    return m


def split(act, cuts):
    """Split at the given FIT timestamps. Returns a list of (header, msgs)."""
    recs = act.of(RECORD)
    times = sorted(set(r.raw('timestamp') for r in recs))
    bounds = []
    for c in cuts:
        before = [t for t in times if t < c]
        after = [t for t in times if t >= c]
        if not before or not after:
            raise ValueError(f'cut {c} is outside the recording')
        bounds.append((before[-1], after[0]))
    first_rec = next(i for i, m in enumerate(act.msgs) if m.gnum == RECORD)
    setup = [m for m in act.msgs[:first_rec]
             if m.gnum not in (FILE_ID, SESSION, LAP, ACTIVITY) and m.gnum not in DROPPED]
    timer = _timer_intervals(act)
    starts = [act.start] + [b for _, b in bounds]
    ends = [a for a, _ in bounds] + [act.end]

    # Assign every body message to a part: by timestamp, or by the preceding
    # message for messages without one.
    part_of, cur = [], 0
    for m in act.msgs[first_rec:]:
        ts = m.raw('timestamp')
        if ts is not None and m.gnum not in (SESSION, LAP, ACTIVITY):
            cur = sum(1 for s in starts[1:] if ts >= s)
        part_of.append(cur)

    parts = []
    orig_timer = act.session.get('total_timer_time') or (act.end - act.start)
    for k, (t0, t1) in enumerate(zip(starts, ends)):
        body = [m for m, p in zip(act.msgs[first_rec:], part_of) if p == k
                and m.gnum not in (SESSION, LAP, ACTIVITY) and m.gnum not in DROPPED]
        prec = [m for m in body if m.gnum == RECORD]
        # rebase accumulated fields so each part starts from zero
        base = {}
        if k > 0:
            prev = [r for r in recs if r.raw('timestamp') <= ends[k - 1]]
            for num in ACCUMULATED:
                base[num] = next((r.raw(num) for r in reversed(prev) if r.raw(num) is not None), None)
        new_body = []
        for m in body:
            if m.gnum == RECORD and k > 0:
                m = m.copy()
                for num, b in base.items():
                    if b is not None and m.raw(num) is not None:
                        m.set_raw(num, m.raw(num) - b)
            new_body.append(m)
        prec = [m for m in new_body if m.gnum == RECORD]
        # timer events at the part boundaries
        ev = [m for m in new_body if m.gnum == EVENT and m.raw('event') == EVENT_TIMER]
        if not ev or ev[0].raw('event_type') != EVENT_START:
            new_body.insert(0, Msg.build(EVENT, timestamp=t0, event=EVENT_TIMER,
                                         event_type=EVENT_START, event_group=0))
        if not ev or ev[-1].raw('event_type') not in (1, EVENT_STOP_ALL):
            new_body.append(Msg.build(EVENT, timestamp=t1, event=EVENT_TIMER,
                                      event_type=EVENT_STOP_ALL, event_group=0))
        part_timer = _overlap(timer, t0, t1)
        dist0 = 0.0

        laps = []
        for lap in act.of(LAP):
            ls = lap.raw('start_time')
            le = ls + (lap.get('total_elapsed_time') or 0) if ls is not None else None
            if ls is None or le <= t0 or ls > t1:
                continue
            a, b = max(ls, t0), min(le, t1)
            lrecs = [r for r in prec if a <= r.raw('timestamp') <= b]
            prev = [r for r in prec if r.raw('timestamp') < a and r.get('distance') is not None]
            ld0 = prev[-1].get('distance') if prev else dist0
            if (a, b) != (ls, le):
                lap = _summarise(lap, lrecs, a, b, _overlap(timer, a, b), ld0,
                                 lap.get('total_timer_time') or 1)
            else:
                lap = lap.copy()
            if lap.has('message_index'):
                lap.set('message_index', len(laps))
            laps.append(lap)

        session = _summarise(act.session, prec, t0, t1, part_timer, dist0, orig_timer)
        _set(session, 'num_laps', len(laps))
        _set(session, 'first_lap_index', 0)
        activity = []
        if act.of(ACTIVITY):
            a = act.of(ACTIVITY)[0].copy()
            _set(a, 'total_timer_time', part_timer)
            if a.has('timestamp'):
                a.set_raw('timestamp', t0 if a.raw('timestamp') == act.start else t1)
            off = utc_offset(act)
            if a.has('local_timestamp') and off is not None:
                a.set_raw('local_timestamp', a.raw('timestamp') + off)
            activity = [a]
        fid = act.of(FILE_ID)[0].copy()
        if fid.has('time_created'):
            fid.set_raw('time_created', t0)
        summary = laps + [session] + activity
        if act.summary_first:
            msgs = [fid] + activity + [session] + laps + setup + new_body
        else:
            msgs = [fid] + setup + new_body + summary
        parts.append((act.header, msgs))
    return parts


def main(argv=None):
    p = argparse.ArgumentParser(prog='fit-split', description=(
        'Split a FIT activity into parts at fractions of its duration, as if it had been stopped '
        'and restarted. Meant for testing fit-stitch.'))
    p.add_argument('file')
    p.add_argument('fractions', nargs='+', type=float, help='cut points, e.g. 0.5 or 0.33 0.66')
    p.add_argument('-o', '--prefix', default='part', help='output prefix (default: part → part1.fit…)')
    args = p.parse_args(argv)
    act = load(args.file)
    cuts = [act.start + f * (act.end - act.start) for f in sorted(args.fractions)]
    for i, (h, msgs) in enumerate(split(act, cuts)):
        out = f'{args.prefix}{i + 1}.fit'
        fitraw.write(out, h, msgs)
        print(f'wrote {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
