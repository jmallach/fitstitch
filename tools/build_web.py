#!/usr/bin/env python3
"""Build the static web frontend into a directory (default: _site/).

The page runs fitstitch in the browser with Pyodide. fitstitch and
garmin-fit-sdk are bundled as wheels next to the page, so the only thing
loaded from elsewhere is the Pyodide runtime itself.

Serve the result with e.g. `python -m http.server -d _site`.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ['index.html', 'app.js', 'app.py', 'style.css']


def build(out):
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    wheels = out / 'wheels'
    wheels.mkdir(parents=True)
    for name in PAGE:
        shutil.copy(ROOT / 'web' / name, out / name)
    shutil.copytree(ROOT / 'web' / 'locales', out / 'locales')
    pip = [sys.executable, '-m', 'pip', '-q', '--disable-pip-version-check']
    subprocess.run(pip + ['wheel', '--no-deps', '-w', str(wheels), str(ROOT)], check=True)
    subprocess.run(pip + ['download', '--no-deps', '--only-binary', ':all:', '-d', str(wheels),
                          'garmin-fit-sdk'], check=True)
    names = sorted(p.name for p in wheels.glob('*.whl'))
    (out / 'wheels.json').write_text(json.dumps(names) + '\n')
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('out', nargs='?', default=ROOT / '_site')
    out = build(p.parse_args().out)
    print(f'built {out}')


if __name__ == '__main__':
    main()
