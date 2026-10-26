"""Command-line interface: fit-stitch FILE FILE [FILE...] -o OUTPUT"""
import argparse
import datetime as dt
import os
import sys

from . import fitraw, __version__
from .merge import load, stitch, StitchError, utc_offset
from .verify import verify


def _local(act, fit_ts):
    """Format a FIT timestamp in the activity's local time when known."""
    offset = utc_offset(act) or 0
    t = dt.datetime.fromtimestamp(fit_ts + fitraw.FIT_EPOCH + offset, dt.timezone.utc)
    return t.strftime('%Y-%m-%d %H:%M:%S')


def _dur(s):
    if s is None:
        return '-'
    s = int(round(s))
    return f'{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}'


def _fmt(v, unit='', nd=0):
    if v is None:
        return '-'
    return f'{v:.{nd}f}{unit}'


ROWS = [
    ('Start', lambda a: _local(a, a.start)),
    ('Distance', lambda a: _fmt(_k(a.session.get('total_distance')), ' km', 2)),
    ('Timer time', lambda a: _dur(a.session.get('total_timer_time'))),
    ('Elapsed time', lambda a: _dur(a.session.get('total_elapsed_time'))),
    ('Ascent / descent', lambda a: f"{_fmt(a.session.get('total_ascent'))} / {_fmt(a.session.get('total_descent'), ' m')}"),
    ('Calories', lambda a: _fmt(a.session.get('total_calories'), ' kcal')),
    ('Avg / max HR', lambda a: f"{_fmt(a.session.get('avg_heart_rate'))} / {_fmt(a.session.get('max_heart_rate'), ' bpm')}"),
    ('Avg / max power', lambda a: f"{_fmt(a.session.get('avg_power'))} / {_fmt(a.session.get('max_power'), ' W')}"),
    ('Laps', lambda a: str(len(a.of(19)))),
    ('Records', lambda a: str(len(a.of(20)))),
]


def _k(v):
    return None if v is None else v / 1000


def table(inputs, merged):
    cols = [f'Part {i + 1}' for i in range(len(inputs))] + ['Stitched']
    data = [[fn(a) for a in inputs + [merged]] for _, fn in ROWS]
    w0 = max(len(r[0]) for r in ROWS)
    ws = [max(len(c), *(len(row[i]) for row in data)) for i, c in enumerate(cols)]
    lines = ['  '.join([' ' * w0] + [c.rjust(w) for c, w in zip(cols, ws)])]
    for (label, _), row in zip(ROWS, data):
        lines.append('  '.join([label.ljust(w0)] + [v.rjust(w) for v, w in zip(row, ws)]))
    return '\n'.join(lines)


def default_output(acts):
    return f'stitched-{_local(acts[0], acts[0].start).replace(" ", "_").replace(":", "")}.fit'


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='fit-stitch',
        description='Stitch consecutive FIT activities (e.g. a run accidentally stopped and saved '
                    'halfway) into a single activity, preserving all recorded data. The time '
                    'between parts becomes a pause.')
    p.add_argument('files', nargs='+', metavar='FILE',
                   help='.fit files, or .zip files holding one (e.g. "Export Original" in Garmin Connect)')
    p.add_argument('-o', '--output', help='output .fit file (default: stitched-<start time>.fit)')
    p.add_argument('-n', '--dry-run', action='store_true', help='show the result without writing a file')
    p.add_argument('-f', '--force', action='store_true',
                   help='stitch even if sport or recording device differ')
    p.add_argument('--overwrite', action='store_true', help='overwrite the output file if it exists')
    p.add_argument('-q', '--quiet', action='store_true', help='only print warnings and errors')
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    args = p.parse_args(argv)

    try:
        acts = [load(f) for f in args.files]
        merged, ordered, warnings = stitch(acts, force=args.force)
        data = fitraw.encode(merged.header, merged.msgs)
    except (StitchError, fitraw.FitError, OSError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1

    say = (lambda *a: None) if args.quiet else print
    if [a.name for a in ordered] != [a.name for a in acts]:
        say('Inputs reordered by start time.')
    say(table(ordered, merged))
    for i, g in enumerate(merged.gaps):
        say(f'Pause between part {i + 1} and {i + 2}: {_dur(g)}')
    say()

    checks = verify(data, ordered)
    for ok, desc in checks:
        if not ok or not args.quiet:
            print(f"  [{'ok' if ok else 'FAIL'}] {desc}", file=sys.stdout if ok else sys.stderr)
    for w in warnings:
        print(f'warning: {w}', file=sys.stderr)
    failed = [d for ok, d in checks if not ok]
    if failed:
        print('error: verification failed; no file written', file=sys.stderr)
        return 2

    if args.dry_run:
        say('\nDry run: nothing written.')
        return 0
    out = args.output or default_output(ordered)
    if os.path.exists(out) and not args.overwrite:
        print(f'error: {out} exists (use --overwrite)', file=sys.stderr)
        return 1
    with open(out, 'wb') as f:
        f.write(data)
    say(f'\nWrote {out}')
    say('Before uploading, delete the original parts wherever they were uploaded: Garmin\n'
        'Connect, Strava and most other apps reject an activity with the same start time\n'
        'as an existing one as a duplicate.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
