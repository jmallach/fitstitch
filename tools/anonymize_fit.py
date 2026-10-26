#!/usr/bin/env python3
"""Anonymize FIT activity files so they can be shared as test data.

The files are treated as the parts of one activity. Every message and field
is kept, except that:

- GPS fixes within --radius metres of where the activity starts and ends
  (usually home) are cleared; the records themselves stay, so totals and
  the data stream are unchanged;
- every remaining position, in any message, is rotated by a random angle
  and moved by a random offset, neither saved anywhere, so the track keeps
  its distances but neither its place nor its orientation; bounding boxes
  are recomputed from the moved points;
- serial numbers are cleared and user profile messages (weight, height,
  birth year...) are dropped.

Times, altitude and sensor data are kept. Look at the result before
publishing it.
"""
import argparse
import math
import secrets
from pathlib import Path

from fitstitch import fitraw

SEMICIRCLE = 2 ** 31 / 180
USER_PROFILE = 3


def _deg(sc):
    return sc / SEMICIRCLE


def _distance(a, b):
    """Metres between two (lat, long) points in degrees."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _position_pairs(msg, bbox=False):
    """(lat_num, long_num) pairs of the message's position fields: points,
    or with bbox=True the bounding box corners (nec_*, swc_*)."""
    p = msg.pdef
    if not p:
        return []
    nums = {fi['name']: n for n, fi in p['fields'].items() if fi['units'] == 'semicircles'}
    return [(n, nums[name.replace('_lat', '_long')]) for name, n in nums.items()
            if name.endswith('_lat') and name.replace('_lat', '_long') in nums
            and name.startswith(('nec_', 'swc_')) == bbox]


def _fixes(msgs):
    return [(_deg(m.raw('position_lat')), _deg(m.raw('position_long')))
            for m in msgs if m.gnum == 20 and m.raw('position_lat') is not None
            and m.raw('position_long') is not None]


def _random_transform(points):
    """A function moving a point by a random rotation (30-330 degrees, about
    the track's centre) and a random offset (10-30 degrees of latitude,
    20-60 of longitude), keeping distances: positions are rotated in metres
    around the old centre and laid out again around the new one."""
    lat0 = sum(p[0] for p in points) / len(points)
    long0 = sum(p[1] for p in points) / len(points)
    while True:
        new_lat0 = lat0 + (10 + secrets.randbelow(2000) / 100) * secrets.choice((-1, 1))
        if abs(new_lat0) < 75:
            break
    new_long0 = long0 + (20 + secrets.randbelow(4000) / 100) * secrets.choice((-1, 1))
    angle = math.radians(30 + secrets.randbelow(30000) / 100)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    k_old, k_new = math.cos(math.radians(lat0)), math.cos(math.radians(new_lat0))

    def move(point):
        x, y = (point[1] - long0) * k_old, point[0] - lat0
        x, y = x * cos_a - y * sin_a, x * sin_a + y * cos_a
        return new_lat0 + y, (new_long0 + x / k_new + 180) % 360 - 180
    return move


def _set_point(msg, lat_num, long_num, point):
    if point is None:
        msg.set_raw(lat_num, None)
        msg.set_raw(long_num, None)
    else:
        msg.set_raw(lat_num, round(point[0] * SEMICIRCLE))
        msg.set_raw(long_num, round(point[1] * SEMICIRCLE))


def _bounding_boxes(msgs):
    """Recompute each summary's bounding box from the records in its time span."""
    records = [(m.raw('timestamp'), m.raw('position_lat'), m.raw('position_long'))
               for m in msgs if m.gnum == 20]
    for m in msgs:
        corners = dict((m.name_of(la), (la, lo)) for la, lo in _position_pairs(m, bbox=True))
        if 'nec_lat' not in corners or 'swc_lat' not in corners:
            continue
        start, end = m.raw('start_time'), m.raw('timestamp')
        inside = [(la, lo) for t, la, lo in records if la is not None and lo is not None
                  and t is not None and start is not None and end is not None and start <= t <= end]
        ne = (max(la for la, _ in inside) / SEMICIRCLE, max(lo for _, lo in inside) / SEMICIRCLE) if inside else None
        sw = (min(la for la, _ in inside) / SEMICIRCLE, min(lo for _, lo in inside) / SEMICIRCLE) if inside else None
        if m.raw(corners['nec_lat'][0]) is not None:
            _set_point(m, *corners['nec_lat'], ne)
        if m.raw(corners['swc_lat'][0]) is not None:
            _set_point(m, *corners['swc_lat'], sw)


def anonymize(parts, radius):
    """parts: [(header, msgs)] in time order. Returns them anonymized, and stats."""
    fixes = [f for _, msgs in parts for f in _fixes(msgs)]
    if not fixes:
        raise SystemExit('no GPS fixes found')
    private = [fixes[0], fixes[-1]]
    move = _random_transform(fixes)
    cleared = moved = 0
    out = []
    for header, msgs in parts:
        new = []
        for m in msgs:
            if m.gnum == USER_PROFILE:
                continue
            m = m.copy()
            for lat_num, long_num in _position_pairs(m):
                lat, long = m.raw(lat_num), m.raw(long_num)
                if lat is None or long is None:
                    continue
                point = (_deg(lat), _deg(long))
                if any(_distance(point, p) <= radius for p in private):
                    _set_point(m, lat_num, long_num, None)
                    cleared += 1
                else:
                    _set_point(m, lat_num, long_num, move(point))
                    moved += 1
            if m.has('serial_number') and m.raw('serial_number') is not None:
                m.set_raw('serial_number', None)
            new.append(m)
        _bounding_boxes(new)
        out.append((header, new))
    return out, {'cleared': cleared, 'moved': moved}


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('files', nargs='+', help='the parts of one activity, in time order')
    p.add_argument('-o', '--outdir', required=True, type=Path)
    p.add_argument('--radius', type=float, default=500,
                   help='clear GPS fixes this close (metres) to the start and end (default 500)')
    args = p.parse_args()
    parts = [fitraw.read(f)[:2] for f in args.files]
    anon, stats = anonymize(parts, args.radius)
    args.outdir.mkdir(parents=True, exist_ok=True)
    for f, (header, msgs) in zip(args.files, anon):
        out = args.outdir / Path(f).name
        fitraw.write(out, header, msgs)
        print(f'wrote {out}')
    print(f"{stats['cleared']} positions near the start/end cleared, {stats['moved']} moved")


if __name__ == '__main__':
    main()
