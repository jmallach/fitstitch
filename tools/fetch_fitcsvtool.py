#!/usr/bin/env python3
"""Download Garmin's FitCSVTool.jar (from github.com/garmin/fit-sdk-tools)
into tools/.cache/, so the test suite can validate stitched files with it.

FitCSVTool is distributed under Garmin's FIT Protocol License Agreement,
not this project's license; it is downloaded for local use only.
Running it requires Java.
"""
import sys
import urllib.request
from pathlib import Path

URL = 'https://github.com/garmin/fit-sdk-tools/raw/main/FitCSVTool/FitCSVTool.jar'
LICENSE = 'https://github.com/garmin/fit-sdk-tools/blob/main/LICENSE.txt'


def main():
    dest = Path(__file__).resolve().parent / '.cache' / 'FitCSVTool.jar'
    dest.parent.mkdir(exist_ok=True)
    print(f'FitCSVTool is licensed under the Garmin FIT Protocol License Agreement:\n  {LICENSE}')
    print(f'downloading {URL}')
    with urllib.request.urlopen(URL) as r:
        data = r.read()
    if data[:2] != b'PK':
        print('error: download is not a jar file', file=sys.stderr)
        return 1
    dest.write_bytes(data)
    print(f'saved {dest} ({len(data)} bytes)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
