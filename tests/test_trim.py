from pathlib import Path

import pytest

from fitstitch import fitraw
from fitstitch.cli import main_trim
from fitstitch.merge import Activity, load, StitchError
from fitstitch.summary import resummarise, recompute_zones, timer_intervals
from fitstitch.trim import trim, suggest, point, TrimError
from fitstitch.verify import verify_trim

from crosscheck import assert_fitdecode_agrees, assert_fitcsvtool_agrees
from test_stitch import HEADER, T0, make_activity

ROOT = Path(__file__).resolve().parent.parent
CORPUS = sorted((ROOT / 'samples' / 'corpus').glob('*.fit')) if (ROOT / 'samples' / 'corpus').is_dir() else []
REAL = CORPUS + sorted((ROOT / 'tests' / 'data').glob('*/*.fit'))
# Real files where a suggestion is right: this bike computer recorded 86 s,
# then nothing for 44 minutes before the ride itself started.
SUGGESTED = {'fitdecode--2019-02-17-062644-ELEMNT-297E-195-0.fit': ('start', 2636)}


def activity(msgs):
    return Activity('test', HEADER, msgs)


def with_tail(n=600, tail=300, speed=3.0, tail_speed=0.0, head=0, head_speed=0.0, **kw):
    """A run of n seconds at `speed`; the last `tail` seconds at tail_speed
    and the first `head` seconds at head_speed."""
    msgs = make_activity(T0, n=n, **kw)
    d = 0.0
    for r in (m for m in msgs if m.gnum == 20):
        i = r.raw('timestamp') - T0
        v = head_speed if i < head else tail_speed if i >= n - tail else speed
        d += v
        r.set('distance', d)
    return activity(msgs)


def checked(act, start=None, end=None):
    out, warnings = trim(act, start, end)
    data = fitraw.encode(out.header, out.msgs)
    checks = verify_trim(data, act)
    assert all(ok for ok, _ in checks), [d for ok, d in checks if not ok]
    return out, data


def test_suggest_standing_at_the_end():
    act = with_tail(tail=300, tail_speed=0.0)
    start, end = suggest(act)
    assert start is None
    assert abs((end - T0) - 300) <= 5


def test_suggest_driving_home():
    # 30 minutes running, then 10 minutes in a car
    act = with_tail(n=2400, tail=600, tail_speed=15.0)
    _, end = suggest(act)
    assert abs((end - T0) - 1800) <= 5


def test_suggest_waiting_at_the_start():
    act = with_tail(tail=0, head=240, head_speed=0.0)
    start, end = suggest(act)
    assert end is None and abs((start - T0) - 240) <= 61


def test_no_suggestion_for_short_or_slow_stretches():
    assert suggest(with_tail(tail=120, tail_speed=0.0)) == (None, None)  # under 3 minutes
    assert suggest(with_tail(tail=300, tail_speed=1.3)) == (None, None)  # walking: left to the user


def test_trim_end():
    act = with_tail(n=600, laps=3)
    out, data = checked(act, end=T0 + 450)
    s = out.session
    assert len(out.of(20)) == 451 and s.get('total_elapsed_time') == 450
    assert abs(s.get('total_distance') - out.of(20)[-1].get('distance')) < 0.01
    assert len(out.of(19)) == 3 and s.get('num_laps') == 3
    ev = [e for e in out.of(21) if e.raw('event') == 0]
    assert ev[-1].raw('event_type') == 4 and ev[-1].raw('timestamp') == T0 + 450
    assert out.of(34)[0].get('total_timer_time') == s.get('total_timer_time')
    assert_fitdecode_agrees(data)


def test_trim_start_rebases_accumulated_fields():
    act = with_tail(n=600)
    out, _ = checked(act, start=T0 + 100)
    recs = out.of(20)
    assert recs[0].raw('timestamp') == T0 + 100 and out.session.raw('start_time') == T0 + 100
    assert recs[0].get('distance') == pytest.approx(3.0)  # metres since the record before the cut
    assert recs[0].get('accumulated_power') == 200
    ev = [e for e in out.of(21) if e.raw('event') == 0]
    assert ev[0].raw('event_type') == 0 and ev[0].raw('timestamp') == T0 + 100


def test_trim_summary_first_layout_and_zones():
    act = with_tail(n=600, tail=0, summary_first=True, session_tiz=True)
    tiz = act.of(216)[0]
    # four zone limits; the records' heart rate cycles through 120..149
    tiz.fields.append([tiz.num_of('hr_zone_high_boundary'), 0x02, bytes([125, 135, 145, 200])])
    out, data = checked(act, start=T0 + 60, end=T0 + 500)
    kinds = [m.gnum for m in out.msgs]
    assert kinds.index(18) < kinds.index(20)  # summaries stay before the data
    zones = next(m for m in out.of(216) if m.raw('reference_mesg') == 18).get('time_in_hr_zone')
    # 440 s kept: HR 120-124 → zone 0, 125-134 → 1, 135-144 → 2, 145-149 → 3
    assert sum(zones) == pytest.approx(out.session.get('total_timer_time'), abs=1)
    assert zones[0] == pytest.approx(440 * 5 / 30, abs=6) and zones[4] == 0
    assert_fitdecode_agrees(data)


def test_trim_keeps_developer_fields():
    act = with_tail(n=300, tail=0, dev=True)
    out, _ = checked(act, end=T0 + 200)
    assert all(r.devfields for r in out.of(20))
    assert [m.gnum for m in out.msgs].count(206) == 1


def test_nothing_left():
    act = with_tail(n=300, tail=0)
    with pytest.raises(TrimError):
        trim(act, start=T0 + 200, end=T0 + 100)


def test_point():
    act = with_tail(n=600, tail=0)
    assert point(act, '2:00') == T0 + 120
    assert point(act, '90s') == T0 + 90
    assert point(act, '-1:00') == act.end - 60
    assert point(act, '300m') == T0 + 99  # 3 m per second, from the first record
    assert point(act, '-0.3km') == next(r.raw('timestamp') for r in act.of(20)
                                        if r.get('distance') >= act.of(20)[-1].get('distance') - 300)
    with pytest.raises(StitchError):
        point(act, 'soon')


def test_cli(tmp_path, capsys):
    act = with_tail(n=600, tail=300)
    src = tmp_path / 'run.fit'
    fitraw.write(src, HEADER, act.msgs)
    assert main_trim([str(src)]) == 0
    # the last record still moving is the one 4:59 in
    assert 'Suggested end: 0:04:59' in capsys.readouterr().out
    out = tmp_path / 'out.fit'
    assert main_trim([str(src), '--auto', '-o', str(out), '-q']) == 0
    assert len(load(out).of(20)) == 300
    assert main_trim([str(src), '--end', '-1:00', '-o', str(out), '-q']) == 1  # exists
    assert main_trim([str(src), '--end', '-1:00', '-o', str(out), '-q', '--overwrite']) == 0


@pytest.mark.parametrize('path', REAL, ids=[p.name for p in REAL])
def test_trim_real_files(path):
    try:
        act = load(path)
    except StitchError:
        pytest.skip('not stitchable, so not trimmable either')
    span = act.end - act.start
    if len(act.of(20)) < 20:
        pytest.skip('too short')
    # real files show no false suggestions
    start, end = suggest(act)
    if path.name in SUGGESTED:
        which, at = SUGGESTED[path.name]
        assert (start - act.start if which == 'start' else act.end - end) == pytest.approx(at, abs=5)
    else:
        assert (start, end) == (None, None)
    for start, end in ((None, act.end - span * 0.2), (act.start + span * 0.15, None),
                       (act.start + span * 0.15, act.end - span * 0.2)):
        out, data = checked(act, start, end)
        assert_fitdecode_agrees(data)
        assert_fitcsvtool_agrees(data, [fitraw.encode(act.header, act.msgs)])


@pytest.mark.parametrize('path', REAL, ids=[p.name for p in REAL])
def test_summary_matches_the_watch(path):
    """Recomputing a whole, untrimmed activity must give what the watch wrote."""
    try:
        act = load(path)
    except StitchError:
        pytest.skip('not an activity we handle')
    s, recs = act.session, act.of(20)
    iv = timer_intervals(act.of(21), act.start, act.end)
    m, _ = resummarise(s, recs, s.raw('start_time') or act.start, act.end, iv, recs)
    if s.get('max_heart_rate') is not None and any(r.get('heart_rate') for r in recs):
        # with smart recording the watch may see a peak between stored records
        assert 0 <= s.get('max_heart_rate') - m.get('max_heart_rate') <= 3
    if s.get('avg_heart_rate') is not None and m.get('avg_heart_rate') is not None:
        assert abs(m.get('avg_heart_rate') - s.get('avg_heart_rate')) <= 2
    if s.get('total_timer_time') and s.get('total_elapsed_time'):
        assert abs(m.get('total_timer_time') - s.get('total_timer_time')) <= max(2, 0.01 * s.get('total_timer_time'))
    if s.get('normalized_power') and m.get('normalized_power'):
        assert abs(m.get('normalized_power') - s.get('normalized_power')) <= 0.03 * s.get('normalized_power')
    for tiz in (t for t in act.of(216) if t.raw('reference_mesg') == 18):
        z = recompute_zones(tiz, recs, iv)
        for field in ('time_in_hr_zone', 'time_in_power_zone'):
            if tiz.raw(field) is not None and isinstance(tiz.get(field), list):
                total = sum(x for x in tiz.get(field) if x)
                if sum(x for x in z.get(field) if x) < 0.9 * total:
                    continue  # zones from data the records don't carry (e.g. an HRM plugin)
                for a, b in zip(tiz.get(field), z.get(field)):
                    assert abs((a or 0) - (b or 0)) <= max(10, 0.02 * total), (field, tiz.get(field), z.get(field))
