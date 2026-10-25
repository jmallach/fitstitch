import os
import struct
from pathlib import Path

import pytest

from fitstitch import fitraw
from fitstitch.fitraw import Msg
from fitstitch.merge import load, stitch, StitchError, Activity
from fitstitch.verify import verify
from fitstitch.cli import main

from crosscheck import assert_fitdecode_agrees, assert_fitcsvtool_agrees

HEADER = bytes([14, 0x20]) + struct.pack('<H', 2132) + b'\0\0\0\0' + b'.FIT' + b'\0\0'
T0 = 1_100_000_000
APP_ID = bytes(range(16))


def make_activity(start, n=60, *, summary_first=False, sport=1, serial=1234, laps=2,
                  dev=False, timer_events=True, session_tiz=False):
    msgs = [Msg.build(0, type=4, manufacturer=1, product=100, serial_number=serial, time_created=start)]
    if dev:
        msgs.append(Msg.build(207, application_id=0, developer_data_index=0))
        msgs[-1].fields[0] = [1, 0x0D, APP_ID]  # application_id is a 16-byte array
        msgs.append(Msg.build(206, developer_data_index=0, field_definition_number=0,
                              fit_base_type_id=0x84, field_name='Power2'))
    if timer_events:
        msgs.append(Msg.build(21, timestamp=start, event=0, event_type=0, event_group=0))
    recs = []
    for i in range(n):
        r = Msg.build(20, timestamp=start + i, distance=i * 3.0, heart_rate=120 + i % 30,
                      accumulated_power=200 * (i + 1), power=200, cadence=80)
        if dev:
            r.devfields.append([0, 0, struct.pack('<H', 300 + i)])
        recs.append(r)
    msgs += recs
    end = start + n - 1
    if timer_events:
        msgs.append(Msg.build(21, timestamp=end, event=0, event_type=4, event_group=0))
    per = n // laps
    lap_msgs = []
    for k in range(laps):
        a, b = k * per, (n - 1 if k == laps - 1 else (k + 1) * per)
        lap_msgs.append(Msg.build(19, timestamp=start + b, start_time=start + a, message_index=k,
                                  total_elapsed_time=b - a, total_timer_time=b - a,
                                  total_distance=(b - a) * 3.0, avg_heart_rate=130,
                                  max_heart_rate=149, event=9, event_type=1))
    session = Msg.build(18, timestamp=end, start_time=start, total_elapsed_time=n - 1,
                        total_timer_time=n - 1, total_distance=(n - 1) * 3.0, total_calories=n,
                        avg_heart_rate=130, max_heart_rate=149, avg_speed=3.0, max_speed=3.5,
                        avg_power=200, max_power=200, total_ascent=10, total_descent=5,
                        num_laps=laps, first_lap_index=0, sport=sport, sub_sport=0,
                        nec_lat=100 + start % 7, swc_lat=50, event=8, event_type=1, message_index=0)
    summary = lap_msgs + [session]
    if session_tiz:
        tiz = Msg.build(216, timestamp=end, reference_mesg=18, reference_index=0)
        tiz.fields.append([2, 0x86, struct.pack('<5I', *(1000 * (j + 1) for j in range(5)))])
        summary.append(tiz)
    activity = Msg.build(34, timestamp=end, total_timer_time=n - 1, num_sessions=1, type=0,
                         event=26, event_type=1)
    summary.append(activity)
    if summary_first:
        msgs = msgs[:1] + summary + msgs[1:]
    else:
        msgs += summary
    return msgs


def save(tmp_path, name, msgs):
    p = tmp_path / name
    fitraw.write(p, HEADER, msgs)
    return p


def run(tmp_path, *parts, **kw):
    paths = [save(tmp_path, f'p{i}.fit', m) for i, m in enumerate(parts)]
    acts = [load(p) for p in paths]
    merged, ordered, warnings = stitch(acts, **kw)
    data = fitraw.encode(merged.header, merged.msgs)
    checks = verify(data, ordered)
    assert all(ok for ok, _ in checks), [d for ok, d in checks if not ok]
    assert_fitdecode_agrees(data)
    assert_fitcsvtool_agrees(data, [p.read_bytes() for p in paths])
    h, msgs, _ = fitraw.read_bytes(data)
    return Activity('out', h, msgs), warnings


@pytest.mark.parametrize('summary_first', [False, True])
def test_basic_two_parts(tmp_path, summary_first):
    a = make_activity(T0, 60, summary_first=summary_first)
    b = make_activity(T0 + 100, 40, summary_first=summary_first)
    out, _ = run(tmp_path, a, b)
    s = out.session
    assert s.get('total_distance') == pytest.approx(59 * 3 + 39 * 3)
    assert s.get('total_timer_time') == pytest.approx(59 + 39)
    assert s.get('total_elapsed_time') == pytest.approx(100 + 39)
    assert s.get('total_calories') == 100
    assert s.get('total_ascent') == 20
    assert s.get('num_laps') == 4
    assert s.get('avg_speed') == pytest.approx((59 * 3 + 39 * 3) / 98, abs=0.001)
    assert out.summary_first == summary_first
    recs = out.of(20)
    assert len(recs) == 100
    # second part continues distance and accumulated power
    assert recs[60].get('distance') == pytest.approx(59 * 3)
    assert recs[60].raw('accumulated_power') == 200 * 60 + 200
    assert [l.raw('message_index') for l in out.of(19)] == [0, 1, 2, 3]
    timer = [(e.raw('timestamp') - T0, e.raw('event_type')) for e in out.of(21) if e.raw('event') == 0]
    assert timer == [(0, 0), (59, 4), (100, 0), (139, 4)]
    assert out.of(34)[0].get('total_timer_time') == pytest.approx(98)


def test_three_parts_out_of_order(tmp_path):
    a = make_activity(T0, 30)
    b = make_activity(T0 + 50, 30)
    c = make_activity(T0 + 100, 30)
    out, _ = run(tmp_path, c, a, b)
    assert len(out.of(20)) == 90
    assert out.session.get('total_distance') == pytest.approx(3 * 29 * 3)
    assert out.session.get('total_elapsed_time') == pytest.approx(129)
    d = [r.get('distance') for r in out.of(20)]
    assert d == sorted(d)
    assert [l.raw('message_index') for l in out.of(19)] == list(range(6))


def test_missing_timer_events_are_added(tmp_path):
    a = make_activity(T0, 30, timer_events=False)
    b = make_activity(T0 + 60, 30, timer_events=False)
    out, _ = run(tmp_path, a, b)
    timer = [(e.raw('timestamp') - T0, e.raw('event_type')) for e in out.of(21) if e.raw('event') == 0]
    assert timer == [(29, 4), (60, 0)]


def test_developer_fields_deduplicated(tmp_path):
    out, _ = run(tmp_path, make_activity(T0, 30, dev=True), make_activity(T0 + 60, 30, dev=True))
    assert len(out.of(207)) == 1 and len(out.of(206)) == 1
    vals = [struct.unpack('<H', r.devfields[0][2])[0] for r in out.of(20)]
    assert vals == [300 + i for i in range(30)] * 2


def test_time_in_zone_merged(tmp_path):
    a = make_activity(T0, 30, session_tiz=True)
    b = make_activity(T0 + 60, 30, session_tiz=True)
    out, _ = run(tmp_path, a, b)
    tiz = [m for m in out.of(216) if m.raw('reference_mesg') == 18]
    assert len(tiz) == 1
    assert tiz[0].get('time_in_hr_zone') == pytest.approx([2.0, 4.0, 6.0, 8.0, 10.0])


def test_undocumented_messages_preserved(tmp_path):
    a = make_activity(T0, 30)
    b = make_activity(T0 + 60, 30)
    a.insert(5, Msg(60000, [[253, 0x86, struct.pack('<I', T0 + 3)], [0, 0x0D, b'abc']]))
    b.insert(5, Msg(60000, [[253, 0x86, struct.pack('<I', T0 + 63)], [0, 0x0D, b'xyz']]))
    out, _ = run(tmp_path, a, b)
    assert [m.fields[1][2] for m in out.of(60000)] == [b'abc', b'xyz']


def test_compressed_timestamps(tmp_path):
    # Hand-encode a part whose records use compressed timestamp headers.
    def encode_compressed(start, n, dist0):
        body = bytearray()
        fid = Msg.build(0, type=4, manufacturer=1, product=100, serial_number=1234, time_created=start)
        body += bytes(fitraw.encode(HEADER, [fid])[14:-2])
        # local type 1: record without timestamp; local type 2: event with timestamp
        body += bytes([0x41, 0, 0]) + struct.pack('<H', 20) + bytes([1, 5, 4, 0x86])
        body += bytes([0x42, 0, 0]) + struct.pack('<H', 21) + bytes([3, 253, 4, 0x86, 0, 1, 0, 1, 1, 0])
        body += bytes([0x02]) + struct.pack('<I', start) + bytes([0, 0])
        for i in range(n):
            ts = start + i
            body += bytes([0x80 | (1 << 5) | (ts & 0x1F)]) + struct.pack('<I', i * 100)
        session = Msg.build(18, timestamp=start + n - 1, start_time=start, total_elapsed_time=n - 1,
                            total_timer_time=n - 1, total_distance=(n - 1), sport=1, sub_sport=0)
        rest = fitraw.encode(HEADER, [session])[14:-2]  # redefines local type 0
        body += rest
        hdr = bytearray(HEADER)
        hdr[4:8] = struct.pack('<I', len(body))
        hdr[12:14] = struct.pack('<H', fitraw.crc16(hdr[:12]))
        out = bytes(hdr) + bytes(body)
        return out + struct.pack('<H', fitraw.crc16(out))

    pa, pb = tmp_path / 'a.fit', tmp_path / 'b.fit'
    pa.write_bytes(encode_compressed(T0, 40, 0))
    pb.write_bytes(encode_compressed(T0 + 50, 40, 0))
    merged, ordered, _ = stitch([load(pa), load(pb)])
    recs = merged.of(20)
    assert [r.raw('timestamp') - T0 for r in recs] == list(range(40)) + list(range(50, 90))
    assert recs[40].get('distance') == pytest.approx(39)
    data = fitraw.encode(merged.header, merged.msgs)
    assert all(ok for ok, _ in verify(data, ordered))


def test_overlap_rejected(tmp_path):
    with pytest.raises(StitchError, match='overlap'):
        run(tmp_path, make_activity(T0, 60), make_activity(T0 + 30, 60))


def test_sport_mismatch(tmp_path):
    a, b = make_activity(T0, 30), make_activity(T0 + 60, 30, sport=2)
    with pytest.raises(StitchError, match='sport'):
        run(tmp_path, a, b)
    out, warnings = run(tmp_path, a, b, force=True)
    assert any('sport' in w for w in warnings)


def test_device_mismatch(tmp_path):
    with pytest.raises(StitchError, match='different devices'):
        run(tmp_path, make_activity(T0, 30), make_activity(T0 + 60, 30, serial=999))


def test_cli(tmp_path, capsys):
    a = save(tmp_path, 'a.fit', make_activity(T0, 30))
    b = save(tmp_path, 'b.fit', make_activity(T0 + 60, 30))
    out = tmp_path / 'out.fit'
    assert main([str(a), str(b), '-o', str(out)]) == 0
    assert out.exists()
    assert main([str(a), str(b), '-o', str(out)]) == 1  # refuses to overwrite
    assert main([str(a), str(b), '-o', str(out), '--overwrite', '-q']) == 0
    assert main([str(a), '-n']) == 1
    assert 'need at least two' in capsys.readouterr().err


def test_zip_input(tmp_path):
    import zipfile
    for i, start in enumerate((T0, T0 + 60)):
        p = save(tmp_path, f'{i}_ACTIVITY.fit', make_activity(start, 30))
        with zipfile.ZipFile(tmp_path / f'{i}.zip', 'w') as z:
            z.write(p, p.name)
    merged, _, _ = stitch([load(tmp_path / '0.zip'), load(tmp_path / '1.zip')])
    assert len(merged.of(20)) == 60


# Optional: real-world specimens in samples/<case>/*.fit|zip (not distributed).
SAMPLES = Path(__file__).parent.parent / 'samples'
CASES = sorted(d for d in SAMPLES.iterdir() if d.is_dir() and d.name != 'corpus') if SAMPLES.is_dir() else []


@pytest.mark.parametrize('case', CASES, ids=[c.name for c in CASES])
def test_real_samples(case):
    files = sorted(p for p in case.iterdir() if p.suffix.lower() in ('.fit', '.zip'))
    if len(files) < 2:
        pytest.skip('needs at least two parts')
    merged, ordered, _ = stitch([load(p) for p in files], force=True)
    data = fitraw.encode(merged.header, merged.msgs)
    checks = verify(data, ordered)
    assert all(ok for ok, _ in checks), [d for ok, d in checks if not ok]
    assert_fitdecode_agrees(data)
    assert_fitcsvtool_agrees(data, [fitraw.encode(a.header, a.msgs) for a in ordered])


def test_roundtrip_real_samples():
    files = [p for c in CASES for p in c.iterdir() if p.suffix.lower() in ('.fit', '.zip')]
    if not files:
        pytest.skip('no samples')
    for p in files:
        act = load(p)
        h, msgs = act.header, act.msgs
        h2, msgs2, _ = fitraw.read_bytes(fitraw.encode(h, msgs))
        assert [(m.gnum, m.fields, m.devfields) for m in msgs] == \
               [(m.gnum, m.fields, m.devfields) for m in msgs2]
