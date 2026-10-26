"""Glue between the web page and fitstitch, run inside Pyodide."""
import json
import os
import re

from fitstitch import fitraw, __version__
from fitstitch.cli import ROWS, default_output, _dur
from fitstitch.merge import load, stitch, StitchError
from fitstitch.verify import verify

OUTPUT = '/tmp/stitched.fit'


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
        'columns': [f'Part {i + 1}' for i in range(len(ordered))] + ['Stitched'],
        'parts': [_clean(a.name) for a in ordered],
        'rows': [[label] + [fn(a) for a in ordered + [merged]] for label, fn in ROWS],
        'reordered': [a.name for a in ordered] != [a.name for a in acts],
        'gaps': [_dur(g) for g in merged.gaps],
        'checks': [[c, _clean(d)] for c, d in checks],
        'warnings': [_clean(w) for w in warnings],
        'ok': ok,
        'filename': default_output(ordered),
    })


def version():
    return __version__
