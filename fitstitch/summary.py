"""Recompute summary messages (session, lap, split, time in zone) from the
records they cover, for activities that were cut (trimmed or split).

What the records can tell is recomputed; what only the watch can compute
(training effect, load, undocumented fields) is kept as it was, and
reported. Averages are weighted by time and only count time when the timer
was running, as watches do.
"""
from collections import deque

RECORD = 20
TIMER_EVENT, EVENT_START, EVENT_STOP, EVENT_STOP_ALL = 0, 0, 1, 4

# structural fields set by the caller, or meaningless to recompute
STRUCTURAL = {'timestamp', 'start_time', 'end_time', 'message_index', 'event', 'event_type',
              'event_group', 'sport', 'sub_sport', 'trigger', 'lap_trigger', 'split_type',
              'num_laps', 'first_lap_index', 'sport_profile_name', 'sport_index',
              'num_splits', 'reference_mesg', 'reference_index', 'intensity'}
# values only the watch can compute: kept unchanged and reported
WATCH_COMPUTED = ('training_effect', 'training_load', 'training_stress', 'intensity_factor',
                  'threshold_power', 'recovery', 'vo2', 'performance_condition', 'stamina',
                  'body_battery', 'grit', 'flow', 'resting_calories')
ZONES = [  # time-in-zone array, its upper boundaries, record fields to classify
    ('time_in_hr_zone', 'hr_zone_high_boundary', ('heart_rate',)),
    ('time_in_power_zone', 'power_zone_high_boundary', ('power',)),
    ('time_in_speed_zone', 'speed_zone_high_boundary', ('enhanced_speed', 'speed')),
    ('time_in_cadence_zone', 'cadence_zone_high_bondary', ('cadence',)),  # sic, in the FIT profile
]


def timer_intervals(events, start, end):
    """[(start, stop)] FIT-timestamp intervals during which the timer ran,
    from timer events; the whole span if there are none."""
    timer = [e for e in events if e.raw('event') == TIMER_EVENT]
    # some devices write no start event: a first stop means it was running
    first = timer[0].raw('event_type') if timer else None
    out, running = [], start if first in (EVENT_STOP, EVENT_STOP_ALL) else None
    for e in timer:
        t, et = e.raw('timestamp'), e.raw('event_type')
        if et == EVENT_START and running is None:
            running = t
        elif et in (EVENT_STOP, EVENT_STOP_ALL) and running is not None:
            out.append((running, t))
            running = None
    if running is not None:
        out.append((running, end))
    return out or [(start, end)]


def running_time(intervals, a, b):
    return sum(max(0, min(e, b) - max(s, a)) for s, e in intervals)


def clip(intervals, a, b):
    return [(max(s, a), min(e, b)) for s, e in intervals if min(e, b) > max(s, a)]


def weights(recs, intervals):
    """Seconds of running timer each record stands for (until the next one)."""
    out = []
    for i, r in enumerate(recs):
        t = r.raw('timestamp')
        nxt = recs[i + 1].raw('timestamp') if i + 1 < len(recs) else t + 1
        out.append(running_time(intervals, t, nxt))
    return out


def _series(recs, names):
    for name in names:
        vals = [r.get(name) for r in recs]
        if any(isinstance(v, (int, float)) for v in vals):
            return [v if isinstance(v, (int, float)) else None for v in vals]
    return None


def _wavg(vals, ws):
    pairs = [(v, w) for v, w in zip(vals, ws) if v is not None and w > 0]
    total = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / total if total else None


def _cadence(recs):
    whole = _series(recs, ('cadence',))
    if whole is None:
        return None
    frac = _series(recs, ('fractional_cadence',)) or [None] * len(recs)
    return [None if c is None else c + (f or 0) for c, f in zip(whole, frac)]


def normalized_power(recs, ws):
    """30 s rolling average of power, raised to the 4th, averaged, 4th root."""
    power = _series(recs, ('power',))
    if power is None:
        return None
    window, acc, acc_w, fourth, n = deque(), 0.0, 0.0, 0.0, 0
    for p, w, r in zip(power, ws, recs):
        if p is None or w <= 0:
            continue
        window.append((p, w))
        acc, acc_w = acc + p * w, acc_w + w
        while acc_w - window[0][1] >= 30:
            q, qw = window.popleft()
            acc, acc_w = acc - q * qw, acc_w - qw
        if acc_w >= 30:
            fourth += (acc / acc_w) ** 4 * w
            n += w
    return (fourth / n) ** 0.25 if n else None


def climb(recs):
    """(gain, loss) in metres, with 3 m hysteresis as watches use against noise."""
    alt = [a for a in (_series(recs, ('enhanced_altitude', 'altitude')) or []) if a is not None]
    gain = loss = 0.0
    if alt:
        ref = alt[0]
        for a in alt[1:]:
            if a - ref >= 3:
                gain, ref = gain + a - ref, a
            elif ref - a >= 3:
                loss, ref = loss + ref - a, a
    return gain, loss


def _fixes(recs):
    return [(r.raw('position_lat'), r.raw('position_long')) for r in recs
            if r.raw('position_lat') is not None and r.raw('position_long') is not None]


def _set(m, name, value):
    if value is None or not m.has(name):
        return
    try:
        m.set(name, value)
    except Exception:  # noqa: BLE001  out of range for the field's type: leave it
        pass


def _set_with_fraction(m, name, frac, value):
    if value is None or not m.has(name) or m.raw(name) is None:
        return
    if m.has(frac):
        _set(m, name, int(value))
        _set(m, frac, value - int(value))
    else:
        _set(m, name, round(value))


def resummarise(msg, recs, t0, t1, intervals, orig_recs, base_distance=0.0):
    """Return a copy of summary msg recomputed for the records recs, which
    span [t0, t1] with the timer running during intervals. orig_recs are the
    records msg originally covered, for values that can only be rescaled
    (calories, ascent). base_distance is the record distance at which the
    span starts. Returns (msg, names of fields kept as the watch wrote them)."""
    m = msg.copy()
    kept = []
    ws = weights(recs, intervals)
    timer = running_time(intervals, t0, t1)
    old_timer = msg.get('total_timer_time') or 0
    ratio = timer / old_timer if old_timer else 1.0

    if m.has('start_time'):
        same = m.raw('timestamp') == m.raw('start_time')
        m.set_raw('start_time', t0)
        if m.has('timestamp'):
            m.set_raw('timestamp', t0 if same else t1)
    elif m.has('timestamp'):
        m.set_raw('timestamp', t1)
    if m.has('end_time'):
        m.set_raw('end_time', t1)
    _set(m, 'total_elapsed_time', t1 - t0)
    _set(m, 'total_timer_time', timer)
    dist = [r.get('distance') for r in recs if r.get('distance') is not None]
    distance = max(0.0, dist[-1] - base_distance) if dist else None
    _set(m, 'total_distance', distance)

    fixes = _fixes(recs)
    if fixes:
        for prefix, (lat, long) in (('start_position', fixes[0]), ('end_position', fixes[-1])):
            if m.raw(f'{prefix}_lat') is not None:
                m.set_raw(f'{prefix}_lat', lat)
                m.set_raw(f'{prefix}_long', long)
        for name, value in (('nec_lat', max(f[0] for f in fixes)), ('nec_long', max(f[1] for f in fixes)),
                            ('swc_lat', min(f[0] for f in fixes)), ('swc_long', min(f[1] for f in fixes))):
            if m.raw(name) is not None:
                m.set_raw(name, value)

    old_gain, old_loss = climb(orig_recs)
    gain, loss = climb(recs)
    cadence = _cadence(recs)
    handled = set()
    for num, _, _ in list(m.fields):
        name = m.name_of(num)
        if not isinstance(name, str):
            if m.raw(num) is not None:
                kept.append(str(name))
            continue
        if name in STRUCTURAL or name in handled or m.raw(num) is None:
            continue
        value = None
        if name in ('total_elapsed_time', 'total_timer_time', 'total_distance') \
                or name.startswith(('start_position', 'end_position', 'nec_', 'swc_')):
            continue
        if any(w in name for w in WATCH_COMPUTED):
            kept.append(name)
            continue
        if name in ('avg_speed', 'enhanced_avg_speed'):
            value = distance / timer if distance is not None and timer else None
        elif name in ('avg_cadence', 'max_cadence'):
            frac = 'avg_fractional_cadence' if name == 'avg_cadence' else 'max_fractional_cadence'
            if cadence is not None:
                c = _wavg(cadence, ws) if name == 'avg_cadence' else \
                    max((v for v, w in zip(cadence, ws) if v is not None), default=None)
                _set_with_fraction(m, name, frac, c)
            handled.update({name, frac})
            continue
        elif name in ('total_ascent', 'total_descent'):
            frac = 'total_fractional_ascent' if name == 'total_ascent' else 'total_fractional_descent'
            old = (msg.get(name) or 0) + (msg.get(frac) or 0)
            part, whole = (gain, old_gain) if name == 'total_ascent' else (loss, old_loss)
            _set_with_fraction(m, name, frac, old * part / whole if whole else old * ratio)
            handled.update({name, frac})
            continue
        elif name == 'normalized_power':
            value = normalized_power(recs, ws)
        elif name == 'total_work':
            power = _series(recs, ('power',))
            value = sum(p * w for p, w in zip(power, ws) if p is not None) if power else None
        elif name == 'total_cycles' and cadence is not None:
            value = sum(c / 60 * w for c, w in zip(cadence, ws) if c is not None)
        elif name == 'avg_grade_adjusted_speed':
            old_speed = msg.get('enhanced_avg_speed') or msg.get('avg_speed')
            new_speed = distance / timer if distance is not None and timer else None
            value = msg.get(name) * new_speed / old_speed if old_speed and new_speed is not None else None
        elif name.startswith(('avg_', 'max_', 'min_', 'enhanced_avg_', 'enhanced_max_', 'enhanced_min_')):
            kind = 'avg' if 'avg_' in name else 'max' if 'max_' in name else 'min'
            src = name.replace(f'{kind}_', '', 1)
            if isinstance(m.raw(num), list):
                kept.append(name)
                continue
            vals = _series(recs, (src,) if src.startswith('enhanced_') else (src, f'enhanced_{src}'))
            if vals is None:
                kept.append(name)
                continue
            running = [v for v, w in zip(vals, ws) if v is not None and w > 0]
            value = _wavg(vals, ws) if kind == 'avg' else \
                (max(running) if kind == 'max' else min(running)) if running else None
        elif name.startswith('total_') or name == 'metabolic_calories':
            if not isinstance(msg.get(name), (int, float)):
                kept.append(name)
                continue
            value = msg.get(name) * ratio
            if name in ('total_calories', 'metabolic_calories', 'total_strides', 'total_cycles'):
                value = round(value)
        else:
            kept.append(name)
            continue
        if value is not None:
            _set(m, name, value)
    return m, kept


def recompute_zones(tiz, recs, intervals):
    """Copy of a time_in_zone message with its times recomputed from recs."""
    m = tiz.copy()
    ws = weights(recs, intervals)
    for times, bounds, sources in ZONES:
        if m.raw(times) is None or m.raw(bounds) is None:
            continue
        limits = m.get(bounds)
        limits = [b for b in (limits if isinstance(limits, list) else [limits]) if b is not None]
        vals = _series(recs, sources)
        old = m.get(times)
        n = len(old) if isinstance(old, list) else 1
        acc = [0.0] * n
        if vals is not None:
            for v, w in zip(vals, ws):
                if v is None or w <= 0:
                    continue
                # a value equal to a zone's "high boundary" belongs to the
                # next zone up, as Garmin watches count it
                zone = sum(1 for b in limits if v >= b)
                acc[min(zone, n - 1)] += w
        _set(m, times, acc if n > 1 else acc[0])
    return m
