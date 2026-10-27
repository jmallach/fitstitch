"""Glue between the web page and fitstitch, run inside Pyodide."""
import json
import os
import re

from fitstitch import fitraw, summary, __version__
from fitstitch import trim as trimming
from fitstitch.cli import ROWS, default_output, _dur
from fitstitch.merge import load, stitch, StitchError
from fitstitch.verify import verify, verify_trim

OUTPUT = '/tmp/stitched.fit'
TRIMMED = '/tmp/trimmed.fit'
POINTS = 1500  # chart and track points sent to the page


def _clean(text):
    """Hide the browser-side upload paths (/tmp/in/<n>-<name>) from messages."""
    return re.sub(r'/tmp/in/\d+-', '', text)


def run(paths, force=False):
    """Stitch the files at paths. Returns a JSON report; the stitched file
    is left at OUTPUT when every check passed."""
    if os.path.exists(OUTPUT):
        os.remove(OUTPUT)
    try:
        acts = [load(p) for p in paths]
        merged, ordered, warnings = stitch(acts, force=force)
        data = fitraw.encode(merged.header, merged.msgs)
    except (StitchError, fitraw.FitError, OSError) as e:
        return json.dumps({'error': _clean(str(e))})

    checks = verify(data, ordered)
    ok = all(c for c, _ in checks)
    if ok:
        with open(OUTPUT, 'wb') as f:
            f.write(data)
    return json.dumps({
        'parts': [_clean(a.name) for a in ordered],
        'rows': [[label] + [fn(a) for a in ordered + [merged]] for label, fn in ROWS],
        'reordered': [a.name for a in ordered] != [a.name for a in acts],
        'gaps': [_dur(g) for g in merged.gaps],
        'checks': [[c, _clean(d)] for c, d in checks],
        'warnings': [_clean(w) for w in warnings],
        'ok': ok,
        'filename': default_output(ordered),
    })


def prepare(path):
    """Everything the page needs to draw an activity for trimming: speed,
    heart rate and distance over time, the track, pauses and the suggested
    cuts. Times are seconds from the start."""
    try:
        act = load(path)
    except (StitchError, fitraw.FitError, OSError) as e:
        return json.dumps({'error': _clean(str(e))})
    recs = act.of(20)
    whole = summary.timer_intervals(act.of(21), act.start, act.end)
    speed = trimming._smoothed_speed(recs, whole, window=30)
    step = max(1, -(-len(recs) // POINTS))
    t, v, hr, dist, track = [], [], [], [], []
    for i in range(0, len(recs), step):
        r = recs[i]
        t.append(r.raw('timestamp') - act.start)
        v.append(None if speed[i] is None else round(speed[i] * 3.6, 2))
        hr.append(r.get('heart_rate'))
        dist.append(None if r.get('distance') is None else round(r.get('distance'), 1))
        lat, long = r.raw('position_lat'), r.raw('position_long')
        track.append(None if lat is None or long is None else
                     [round(lat * 180 / 2 ** 31, 6), round(long * 180 / 2 ** 31, 6)])
    pauses, prev = [], act.start
    for a, b in whole:
        if a - prev >= 1:
            pauses.append([prev - act.start, a - act.start])
        prev = b
    if act.end - prev >= 1:
        pauses.append([prev - act.start, act.end - act.start])
    start, end = trimming.suggest(act)
    # series with nothing to show (no movement, no heart rate) are left out
    flat = lambda s: None if not any(x for x in s) else s
    moved = flat(v)
    return json.dumps({
        'duration': act.end - act.start,
        't': t, 'speed': moved, 'hr': flat(hr), 'distance': dist if moved else None,
        'track': track if any(track) else None,
        'pauses': pauses,
        'suggest': [None if start is None else start - act.start, None if end is None else end - act.start],
    })


def run_trim(path, start, end):
    """Trim the file at path to [start, end] seconds from its start (None
    keeps that end). Returns a JSON report like run(); the trimmed file is
    left at TRIMMED when every check passed."""
    if os.path.exists(TRIMMED):
        os.remove(TRIMMED)
    try:
        act = load(path)
        number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        trimmed, warnings = trimming.trim(act, act.start + start if number(start) else None,
                                          act.start + end if number(end) else None)
        data = fitraw.encode(trimmed.header, trimmed.msgs)
    except (StitchError, fitraw.FitError, OSError) as e:
        return json.dumps({'error': _clean(str(e))})
    checks = verify_trim(data, act)
    ok = all(c for c, _ in checks)
    if ok:
        with open(TRIMMED, 'wb') as f:
            f.write(data)
    s = trimmed.session
    return json.dumps({
        'rows': [[label] + [fn(a) for a in (act, trimmed)] for label, fn in ROWS],
        'removed': [_dur(s.raw('start_time') - act.start),
                    _dur(act.end - s.raw('start_time') - (s.get('total_elapsed_time') or 0))],
        'checks': [[c, _clean(d)] for c, d in checks],
        'warnings': [_clean(w) for w in warnings],
        'ok': ok,
        'filename': default_output([trimmed]).replace('stitched-', 'trimmed-'),
    })


def version():
    return __version__
