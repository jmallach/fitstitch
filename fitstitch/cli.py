"""Command-line interface: fit-stitch FILE FILE [FILE...] -o OUTPUT"""
import argparse
import datetime as dt
import os
import sys

from . import fitraw, __version__
from .merge import load, stitch, StitchError, utc_offset
from .verify import verify, verify_trim
from . import trim as trimming


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


def table(inputs, merged, cols=None):
    cols = cols or [f'Part {i + 1}' for i in range(len(inputs))] + ['Stitched']
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



def _offset(act, ts):
    return _dur(ts - act.start)


def main_trim(argv=None):
    p = argparse.ArgumentParser(
        prog='fit-trim',
        description='Trim a FIT activity at the start and/or the end, as if the watch had been '
                    'started later or stopped earlier (e.g. it kept recording after the finish). '
                    'Without --start, --end or --auto, only shows where it would suggest cutting.')
    p.add_argument('file', metavar='FILE', help='.fit file, or .zip file holding one')
    p.add_argument('--start', metavar='WHEN',
                   help='new start: elapsed time from the start (1:02:30, 45:00, 90s) or distance '
                        '(10.4km, 850m); with a leading "-", counted back from the end')
    p.add_argument('--end', metavar='WHEN', help='new end, in the same forms (e.g. -23:00 or -1.2km)')
    p.add_argument('--auto', action='store_true',
                   help='cut where fitstitch suggests, for the ends not given explicitly')
    p.add_argument('-o', '--output', help='output .fit file (default: trimmed-<start time>.fit)')
    p.add_argument('-n', '--dry-run', action='store_true', help='show the result without writing a file')
    p.add_argument('--overwrite', action='store_true', help='overwrite the output file if it exists')
    p.add_argument('-q', '--quiet', action='store_true', help='only print warnings and errors')
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    # "--end -23:00": before Python 3.14 argparse takes a value starting with
    # "-" for an option, so join it to its option ("--end=-23:00")
    argv = list(sys.argv[1:] if argv is None else argv)
    for i in range(len(argv) - 1, 0, -1):
        if argv[i - 1] in ('--start', '--end') and argv[i].startswith('-'):
            argv[i - 1:i + 1] = [f'{argv[i - 1]}={argv[i]}']
    args = p.parse_args(argv)
    say = (lambda *a: None) if args.quiet else print

    try:
        act = load(args.file)
        start = trimming.point(act, args.start) if args.start else None
        end = trimming.point(act, args.end) if args.end else None
        s_start, s_end = trimming.suggest(act)
        if not (args.start or args.end or args.auto):
            if s_start is None and s_end is None:
                say('Nothing to suggest: no stretch of standing still or vehicle speed at either end.\n'
                    'Give --start and/or --end to trim anyway.')
            for label, ts, opt in (('start', s_start, '--start'), ('end', s_end, '--end')):
                if ts is not None:
                    say(f'Suggested {label}: {_offset(act, ts)} into the activity '
                        f'({_local(act, ts)}); {opt} {_offset(act, ts)} or --auto to apply.')
            return 0
        if args.auto:
            start = start if args.start else s_start
            end = end if args.end else s_end
            if start is None and end is None:
                say('Nothing to trim: no suggestion at either end.')
                return 0
        trimmed, warnings = trimming.trim(act, start, end)
        data = fitraw.encode(trimmed.header, trimmed.msgs)
    except (StitchError, fitraw.FitError, OSError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1

    say(table([act], trimmed, cols=['Original', 'Trimmed']))
    s = trimmed.session
    cut_start = s.raw('start_time') - act.start
    cut_end = act.end - (s.raw('start_time') + (s.get('total_elapsed_time') or 0))
    if cut_start > 0.5:
        say(f'Removed from the start: {_dur(cut_start)}')
    if cut_end > 0.5:
        say(f'Removed from the end: {_dur(cut_end)}')
    say()
    checks = verify_trim(data, act)
    for ok, desc in checks:
        if not ok or not args.quiet:
            print(f"  [{'ok' if ok else 'FAIL'}] {desc}", file=sys.stdout if ok else sys.stderr)
    for w in warnings:
        print(f'warning: {w}', file=sys.stderr)
    if not all(ok for ok, _ in checks):
        print('error: verification failed; no file written', file=sys.stderr)
        return 2
    if args.dry_run:
        say('\nDry run: nothing written.')
        return 0
    out = args.output or default_output([trimmed]).replace('stitched-', 'trimmed-')
    if os.path.exists(out) and not args.overwrite:
        print(f'error: {out} exists (use --overwrite)', file=sys.stderr)
        return 1
    with open(out, 'wb') as f:
        f.write(data)
    say(f'\nWrote {out}')
    say('Before uploading, delete the original wherever it was uploaded: Garmin Connect,\n'
        'Strava and most other apps reject an activity with the same start time as an\n'
        'existing one as a duplicate.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
