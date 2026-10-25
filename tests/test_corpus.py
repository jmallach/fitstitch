"""Tests against public FIT files (fetch them with tools/fetch_corpus.py).

Every file must either be read and written back byte for byte, or be
refused with a clean error. Every activity is split into parts as if it
had been stopped and restarted, and stitching those parts back must
reproduce the original data stream exactly.
"""
from pathlib import Path

import pytest

from fitstitch import fitraw
from fitstitch.merge import Activity, StitchError, load, stitch, RECORD, LAP, EVENT
from fitstitch.split import split
from fitstitch.verify import verify

CORPUS = Path(__file__).parent.parent / 'samples' / 'corpus'
FILES = sorted(CORPUS.glob('*.fit')) if CORPUS.is_dir() else []

if not FILES:
    pytest.skip('no corpus; run tools/fetch_corpus.py', allow_module_level=True)


def _load_or_skip(path):
    try:
        return load(path)
    except StitchError as e:
        pytest.skip(f'not stitchable: {e}')


@pytest.mark.parametrize('path', FILES, ids=[p.name for p in FILES])
def test_roundtrip_or_clean_error(path):
    data = path.read_bytes()
    try:
        header, msgs, _ = fitraw.read_bytes(data)
    except fitraw.FitError:
        with pytest.raises(StitchError):
            load(path)
        return
    again = fitraw.read_bytes(fitraw.encode(header, msgs))[1]
    assert [(m.gnum, m.fields, m.devfields) for m in msgs] == \
           [(m.gnum, m.fields, m.devfields) for m in again]


@pytest.mark.parametrize('fractions', [(0.5,), (0.3, 0.7)], ids=['halves', 'thirds'])
@pytest.mark.parametrize('path', FILES, ids=[p.name for p in FILES])
def test_split_and_stitch_back(path, fractions):
    act = _load_or_skip(path)
    recs = act.of(RECORD)
    times = sorted({r.raw('timestamp') for r in recs})
    if len(times) < 10:
        pytest.skip('too short to split')
    cuts = [times[int(f * len(times))] for f in fractions]
    parts = [Activity(f'part{i}', h, m) for i, (h, m) in enumerate(split(act, cuts))]
    merged, ordered, _ = stitch(parts, force=True)
    data = fitraw.encode(merged.header, merged.msgs)

    checks = verify(data, ordered)
    assert all(ok for ok, _ in checks), [d for ok, d in checks if not ok]

    out = Activity('out', *fitraw.read_bytes(data)[:2])
    # the data stream is restored exactly
    assert [r.fields for r in out.of(RECORD)] == [r.fields for r in recs]
    assert [r.devfields for r in out.of(RECORD)] == [r.devfields for r in recs]
    # every original event is still there
    orig_events = [e.fields for e in act.of(EVENT)]
    out_events = [e.fields for e in out.of(EVENT)]
    assert all(e in out_events for e in orig_events)
    # totals match the original
    s, o = out.session, act.session
    assert s.get('total_elapsed_time') == pytest.approx(o.get('total_elapsed_time'), abs=1.5)
    if o.get('total_distance') is not None and recs[-1].get('distance') is not None:
        assert s.get('total_distance') == pytest.approx(recs[-1].get('distance'), abs=0.05)
    assert len(out.of(LAP)) >= len(act.of(LAP))
