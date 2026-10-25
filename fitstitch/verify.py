"""Independent checks of a stitched file against its inputs."""
from collections import Counter

from garmin_fit_sdk import Decoder, Stream

from . import fitraw
from .merge import (Activity, RECORD, LAP, SESSION, EVENT, SUMMARY_TYPES, SETUP_ONCE,
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
    nl = sum(len(a.of(LAP)) for a in inputs)
    check(len(out.of(LAP)) == nl and (s.raw('num_laps') in (None, nl)), f'all {nl} laps present')
    idx = [l.raw('message_index') for l in out.of(LAP) if l.has('message_index')]
    check(idx == list(range(len(idx))), 'lap indexes are sequential')
    check(len(out.of(SESSION)) == 1, 'exactly one session')
    return checks
