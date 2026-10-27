"""Independent checks of a stitched file against its inputs."""
from collections import Counter

from garmin_fit_sdk import Decoder, Stream

from . import fitraw
from .merge import (Activity, RECORD, LAP, SESSION, EVENT, ACTIVITY, SUMMARY_TYPES, SETUP_ONCE,
                    DEVELOPER_DATA_ID, FIELD_DESCRIPTION)

# Message types whose count must be preserved exactly (data streams).
STREAM_EXEMPT = SUMMARY_TYPES | SETUP_ONCE | {EVENT, DEVELOPER_DATA_ID, FIELD_DESCRIPTION}


def verify(data, inputs):
    """Return a list of (ok, description) checks."""
    checks = []

    def check(ok, desc):
        checks.append((bool(ok), desc))

    # 1. our own parser: CRC and structure
    try:
        header, msgs, _ = fitraw.read_bytes(data)
        out = Activity('output', header, msgs)
        check(True, 'file structure and CRC are valid')
    except Exception as e:  # noqa: BLE001
        check(False, f'file cannot be re-read: {e}')
        return checks

    # 2. the official Garmin FIT SDK
    dec = Decoder(Stream.from_byte_array(bytearray(data)))
    ok = dec.is_fit() and dec.check_integrity()
    _, errors = Decoder(Stream.from_byte_array(bytearray(data))).read()
    check(ok and not errors, 'Garmin FIT SDK decodes the file without errors'
          + (f' ({errors})' if errors else ''))

    # 3. every data-stream message preserved
    cin = Counter(m.gnum for a in inputs for m in a.msgs if m.gnum not in STREAM_EXEMPT)
    cout = Counter(m.gnum for m in out.msgs if m.gnum not in STREAM_EXEMPT)
    missing = {k: cin[k] - cout[k] for k in cin if cin[k] != cout[k]}
    check(not missing, f'all {sum(cin.values())} data messages preserved'
          + (f' (count mismatch per message type: {missing})' if missing else ''))
    ev_in = sum(len(a.of(EVENT)) for a in inputs)
    check(len(out.of(EVENT)) >= ev_in, f'all {ev_in} events preserved')

    # 4. record stream continuity
    recs = out.of(RECORD)
    ts = [r.raw('timestamp') for r in recs]
    check(all(y >= x for x, y in zip(ts, ts[1:])), 'record timestamps never go backwards')
    dist = [r.get('distance') for r in recs if r.get('distance') is not None]
    check(all(y >= x - 0.01 for x, y in zip(dist, dist[1:])), 'record distance never decreases')

    # 5. session totals
    s = out.session
    for field in ('total_distance', 'total_timer_time', 'total_calories'):
        vals = [a.session.get(field) for a in inputs]
        if None in vals or s.get(field) is None:
            continue
        exp = sum(vals)
        check(abs(s.get(field) - exp) <= max(0.01, exp * 1e-6),
              f'session {field} = sum of inputs ({s.get(field):g})')
    exp_elapsed = inputs[-1].end - inputs[0].start
    if s.get('total_elapsed_time') is not None:
        check(abs(s.get('total_elapsed_time') - exp_elapsed) < 1.5,
              f'session elapsed time spans first start to last end ({exp_elapsed:.0f} s)')
    if not any(_undeclared_dev_fields(a) for a in inputs):
        check(not _undeclared_dev_fields(out), 'every developer field used is declared')
    nl = sum(len(a.of(LAP)) for a in inputs)
    if nl or out.of(LAP):
        check(len(out.of(LAP)) == nl and (s.raw('num_laps') in (None, nl)), f'all {nl} laps present')
    idx = [l.raw('message_index') for l in out.of(LAP) if l.has('message_index')]
    check(idx == list(range(len(idx))), 'lap indexes are sequential')
    check(len(out.of(SESSION)) == 1, 'exactly one session')

    # Summary consistency rules from Garmin's activity file validator
    # (ActivityFileValidationPlugin in the FIT Java SDK, run by FitCSVTool -t),
    # with its tolerances. Checked when every input already satisfies them.
    for rule, desc in RULES:
        if all(rule(a) for a in inputs):
            check(rule(out), desc)
    return checks


def _undeclared_dev_fields(act):
    """(developer data index, field number) pairs used without a field description."""
    declared = {(m.raw('developer_data_index'), m.raw('field_definition_number'))
                for m in act.of(FIELD_DESCRIPTION)}
    used = {(idx, num) for m in act.msgs for num, idx, _ in m.devfields}
    return used - declared


def _lap_spans(act):
    spans = []
    for lap in act.of(LAP):
        s, e = lap.raw('start_time'), lap.get('total_elapsed_time')
        if s is None or e is None:
            return None
        spans.append((s, s + e))
    return spans


def _laps_abut(act):
    spans = _lap_spans(act)
    if not spans:
        return False
    return all(abs(b[0] - a[1]) <= 2 for a, b in zip(spans, spans[1:]))


def _lap_sum(field):
    def rule(act):
        laps = act.of(LAP)
        total = act.session.get(field)
        vals = [lap.get(field) for lap in laps]
        if not laps or total is None or None in vals:
            return False
        return abs(sum(vals) - total) <= 1
    return rule


def _activity_timer(act):
    acts = act.of(ACTIVITY)
    if not acts or acts[0].get('total_timer_time') is None or act.session.get('total_timer_time') is None:
        return False
    return abs(acts[0].get('total_timer_time') - act.session.get('total_timer_time')) <= 1


def _records_within_session(act):
    recs = act.of(RECORD)
    start, elapsed = act.session.raw('start_time'), act.session.get('total_elapsed_time')
    if start is None or elapsed is None:
        return False
    return recs[0].raw('timestamp') >= start and recs[-1].raw('timestamp') <= start + elapsed + 1


RULES = [
    (_laps_abut, 'laps are contiguous (each lap starts where the previous one ended)'),
    (_lap_sum('total_elapsed_time'), 'lap elapsed times add up to the session elapsed time'),
    (_lap_sum('total_timer_time'), 'lap timer times add up to the session timer time'),
    (_activity_timer, 'activity timer time equals the session timer time'),
    (_records_within_session, 'records fall within the session time span'),
]


def verify_trim(data, original):
    """Checks of a trimmed file against the original activity. Returns a
    list of (ok, description), as verify() does."""
    from .trim import ACCUMULATED
    checks = []

    def check(ok, desc):
        checks.append((bool(ok), desc))

    try:
        header, msgs, _ = fitraw.read_bytes(data)
        out = Activity('output', header, msgs)
        check(True, 'file structure and CRC are valid')
    except Exception as e:  # noqa: BLE001
        check(False, f'file cannot be re-read: {e}')
        return checks
    dec = Decoder(Stream.from_byte_array(bytearray(data)))
    ok = dec.is_fit() and dec.check_integrity()
    _, errors = Decoder(Stream.from_byte_array(bytearray(data))).read()
    check(ok and not errors, 'Garmin FIT SDK decodes the file without errors'
          + (f' ({errors})' if errors else ''))

    s = out.session
    t0 = s.raw('start_time')
    t1 = t0 + (s.get('total_elapsed_time') or 0)
    recs = out.of(RECORD)
    # An end that was not trimmed keeps every record on its side (watches
    # may write some past the session's own span); a trimmed one keeps
    # those within the new session span (whose end is a record's time).
    trimmed_start, trimmed_end = t0 > original.start, t1 < original.end - 1
    expected = [r for r in original.of(RECORD)
                if (not trimmed_start or r.raw('timestamp') >= t0)
                and (not trimmed_end or r.raw('timestamp') < t1 + 1)]
    same = [r.raw('timestamp') for r in recs] == [r.raw('timestamp') for r in expected]
    check(same, f'the {len(expected)} records in the kept span are all there, and no others')
    unchanged = same and all(
        [f for f in a.fields if f[0] not in ACCUMULATED] == [f for f in b.fields if f[0] not in ACCUMULATED]
        and a.devfields == b.devfields for a, b in zip(recs, expected))
    check(unchanged, 'record values and developer fields are unchanged')
    if not _undeclared_dev_fields(original):
        check(not _undeclared_dev_fields(out), 'every developer field used is declared')
    ts = [r.raw('timestamp') for r in recs]
    check(all(y >= x for x, y in zip(ts, ts[1:])), 'record timestamps never go backwards')
    dist = [r.get('distance') for r in recs if r.get('distance') is not None]
    check(all(y >= x - 0.01 for x, y in zip(dist, dist[1:])), 'record distance never decreases')
    if dist and s.get('total_distance') is not None:
        check(abs(s.get('total_distance') - dist[-1]) <= max(1.0, dist[-1] * 0.005),
              f'session total_distance = distance in the records ({s.get("total_distance"):g})')
    if s.get('total_timer_time') is not None and s.get('total_elapsed_time') is not None:
        check(s.get('total_timer_time') <= s.get('total_elapsed_time') + 1,
              'timer time is not longer than elapsed time')
    nl = len(out.of(LAP))
    if nl:
        idx = [l.raw('message_index') for l in out.of(LAP) if l.has('message_index')]
        check(idx == list(range(len(idx))), 'lap indexes are sequential')
        check(s.raw('num_laps') in (None, nl), f'session lap count matches the {nl} laps')
    check(len(out.of(SESSION)) == 1, 'exactly one session')
    for rule, desc in RULES:
        if rule(original):
            check(rule(out), desc)
    return checks
