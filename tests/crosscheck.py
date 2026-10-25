"""Independent decoders used to cross-check stitched files.

- fitdecode (https://github.com/polyvertex/fitdecode): every data message
  and field value must decode identically to fitstitch's own reader.
- Garmin's FitCSVTool (optional; fetch with tools/fetch_fitcsvtool.py or set
  FITCSVTOOL_JAR): its activity file validation must not report a failure
  the input files did not already have.
"""
import functools
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import fitdecode

from fitstitch import fitraw

JAR = Path(os.environ.get('FITCSVTOOL_JAR',
                          Path(__file__).parent.parent / 'tools' / '.cache' / 'FitCSVTool.jar'))


def _norm(v):
    if isinstance(v, (bytes, bytearray)):
        v = list(v)
    if isinstance(v, (list, tuple)):
        v = [_norm(x) for x in v]
        if all(x is None for x in v):
            return None
        return v[0] if len(v) == 1 else v
    if isinstance(v, float) and v != v:
        return None
    if isinstance(v, str):
        return v.rstrip('\0') or None
    return v


def fitdecode_messages(data):
    out = []
    with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.ENABLED) as fr:
        for frame in fr:
            if isinstance(frame, fitdecode.FitDataMessage):
                fields = {}
                for fd in frame.fields:
                    if getattr(fd, 'is_expanded', False):
                        continue
                    if isinstance(fd.field_def, fitdecode.types.DevFieldDefinition):
                        continue
                    fields[fd.def_num] = _norm(fd.raw_value)
                out.append((frame.global_mesg_num, fields))
    return out


def assert_fitdecode_agrees(data):
    """fitdecode must see the same messages and raw field values."""
    _, msgs, _ = fitraw.read_bytes(data)
    theirs = fitdecode_messages(data)
    assert len(theirs) == len(msgs), f'fitdecode sees {len(theirs)} messages, we wrote {len(msgs)}'
    for i, (m, (gnum, fields)) in enumerate(zip(msgs, theirs)):
        assert m.gnum == gnum, f'message {i}: global number {gnum} != {m.gnum}'
        for num, bt, raw in m.fields:
            size = fitraw.BASE.get(bt, (None, 1, None))[1]
            if len(raw) % size:
                continue  # malformed field (copied byte for byte); decoders disagree on it
            v = _norm(m.raw(num))
            if num not in fields or fields[num] == v:
                continue
            # Decoders differ in how they represent invalid values inside
            # arrays; compare with "invalid" spelled out as the type's sentinel.
            inv = fitraw.BASE.get(bt, (None, None, None))[2]
            spell = lambda x: [inv if y is None else y for y in x] if isinstance(x, list) else x
            if spell(fields[num]) == spell(v):
                continue
            raise AssertionError(f'message {i} ({m.name}) field {num}: fitdecode {fields[num]!r} '
                                 f'!= ours {v!r}')


FAIL_RE = re.compile(r'^(.*) - Level: REQUIRED Status: FAILED', re.M)


def fitcsvtool_failures(data):
    return _fitcsvtool_cached(hashlib.sha1(data).hexdigest(), data)


@functools.lru_cache(maxsize=None)
def _fitcsvtool_cached(_digest, data):
    """Names of required validation checks FitCSVTool reports as failed;
    'decode error' if it cannot decode the file. None if unavailable."""
    if not JAR.exists() or not shutil.which('java'):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, 'in.fit')
        path.write_bytes(data)
        res = subprocess.run(['java', '-jar', str(JAR), '-t', '-i', '-b', str(path), str(Path(tmp, 'out.csv'))],
                             capture_output=True, text=True, timeout=300)
    text = res.stdout + res.stderr
    failures = frozenset(FAIL_RE.findall(text))
    if 'Error:' in text:
        failures |= {'decode error'}
    return failures


def assert_fitcsvtool_agrees(data, input_datas):
    """FitCSVTool must not report failures the inputs did not have."""
    out = fitcsvtool_failures(data)
    if out is None:
        return False
    before = set()
    for d in input_datas:
        before |= fitcsvtool_failures(d)
    new = out - before
    assert not new, f'FitCSVTool validation failures introduced by stitching: {sorted(new)}'
    return True
