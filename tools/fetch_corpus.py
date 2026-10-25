#!/usr/bin/env python3
"""Download public FIT test files from open-source projects into samples/corpus/.

The files are only used by the test suite and are not part of this
repository. Requires git.
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPOS = [
    'https://github.com/polyvertex/fitdecode.git',
    'https://github.com/dtcooper/python-fitparse.git',
    'https://github.com/garmin/fit-python-sdk.git',
    'https://github.com/garmin/fit-javascript-sdk.git',
]


def main():
    dest = Path(__file__).resolve().parent.parent / 'samples' / 'corpus'
    dest.mkdir(parents=True, exist_ok=True)
    seen = {hashlib.sha1(p.read_bytes()).hexdigest() for p in dest.glob('*.fit')}
    count = 0
    with tempfile.TemporaryDirectory() as tmp:
        for url in REPOS:
            name = url.rstrip('/').split('/')[-1].removesuffix('.git')
            print(f'cloning {url}')
            subprocess.run(['git', 'clone', '-q', '--depth', '1', url, f'{tmp}/{name}'], check=True)
            for p in sorted(Path(tmp, name).rglob('*')):
                if p.suffix.lower() != '.fit' or '.git' in p.parts:
                    continue
                digest = hashlib.sha1(p.read_bytes()).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                shutil.copy(p, dest / f'{name}--{p.name}')
                count += 1
    print(f'{count} new files in {dest}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
