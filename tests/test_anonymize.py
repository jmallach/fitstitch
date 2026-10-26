import sys
from pathlib import Path

from fitstitch import fitraw
from fitstitch.fitraw import Msg
from fitstitch.merge import Activity, stitch

from test_stitch import HEADER, T0, make_activity

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
from anonymize_fit import SEMICIRCLE, USER_PROFILE, _deg, _distance, anonymize  # noqa: E402

HOME = (41.0, 2.0)


def with_track(msgs, start_index, n):
    """Give the records positions along a line heading north-east from HOME,
    about 5 m apart, plus a user profile, serial numbers and a bounding box."""
    out = [msgs[0], Msg.build(USER_PROFILE, weight=70.0, age=40)] + msgs[1:]
    recs = [m for m in out if m.gnum == 20]
    pts = []
    for i, r in enumerate(recs):
        k = start_index + i
        p = (HOME[0] + k * 0.00004, HOME[1] + k * 0.00004)
        pts.append(p)
        r.fields += [[0, 0x85, b''], [1, 0x85, b'']]
        r.fields[-2][2] = round(p[0] * SEMICIRCLE).to_bytes(4, 'little', signed=True)
        r.fields[-1][2] = round(p[1] * SEMICIRCLE).to_bytes(4, 'little', signed=True)
    s = next(m for m in out if m.gnum == 18)
    for name, value in (('nec_long', max(p[1] for p in pts)), ('swc_long', min(p[1] for p in pts))):
        s.fields.append([s.num_of(name), 0x85, round(value * SEMICIRCLE).to_bytes(4, 'little', signed=True)])
    s.set_raw('nec_lat', round(max(p[0] for p in pts) * SEMICIRCLE))
    s.set_raw('swc_lat', round(min(p[0] for p in pts) * SEMICIRCLE))
    return out


def positions(msgs):
    return [(_deg(r.raw('position_lat')), _deg(r.raw('position_long')))
            if r.raw('position_lat') is not None else None for r in msgs if r.gnum == 20]


def test_anonymize():
    # an out-and-back run: part 2 heads back home along the same line
    a = with_track(make_activity(T0, n=60), 0, 60)
    b = with_track(make_activity(T0 + 100, n=60), 60, 60)
    for r, k in zip([m for m in b if m.gnum == 20], range(60)):
        p = (HOME[0] + (59 - k) * 0.00004, HOME[1] + (59 - k) * 0.00004)
        r.set_raw('position_lat', round(p[0] * SEMICIRCLE))
        r.set_raw('position_long', round(p[1] * SEMICIRCLE))
    before = [positions(a), positions(b)]
    (_, a2), (_, b2) = anonymize([(HEADER, a), (HEADER, b)], radius=50)[0]

    home = before[0][0]
    for old, new in zip(before, [positions(a2), positions(b2)]):
        for p, q in zip(old, new):
            assert (q is None) == (_distance(p, home) <= 50)
        kept = [(p, q) for p, q in zip(old, new) if q is not None]
        assert kept and all(_distance(q, home) > 1_000_000 for _, q in kept)  # moved far away
        for (p1, q1), (p2, q2) in zip(kept, kept[1:]):
            assert abs(_distance(p1, p2) - _distance(q1, q2)) < 0.05  # shape kept

    for msgs in (a2, b2):
        assert not any(m.gnum == USER_PROFILE for m in msgs)
        assert msgs[0].raw('serial_number') is None
        s = next(m for m in msgs if m.gnum == 18)
        pts = [p for p in positions(msgs) if p]
        assert abs(_deg(s.raw('nec_lat')) - max(p[0] for p in pts)) < 1e-6
        assert abs(_deg(s.raw('swc_long')) - min(p[1] for p in pts)) < 1e-6

    merged, _, _ = stitch([Activity('a', HEADER, a2), Activity('b', HEADER, b2)])
    assert len(merged.of(20)) == 120
