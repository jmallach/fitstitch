"""Stitch consecutive FIT activity files into one, as if the timer had only
been paused between them."""
import io
import zipfile

from . import fitraw
from .fitraw import Msg, FitError

FILE_ID, FILE_CREATOR, EVENT, RECORD, LAP, SESSION, ACTIVITY = 0, 49, 21, 20, 19, 18, 34
LENGTH, HRV, SET = 101, 78, 225
DEVELOPER_DATA_ID, FIELD_DESCRIPTION = 207, 206
TIME_IN_ZONE, SPLIT, SPLIT_SUMMARY = 216, 312, 313
# Undocumented Garmin messages
GARMIN_METRICS = 140      # end-of-activity physiological snapshot
GARMIN_BEST_EFFORT = 113  # fastest time over 1 km / 1 mile / 5 km ... in this activity

SUMMARY_TYPES = {ACTIVITY, SESSION, LAP, TIME_IN_ZONE, SPLIT, SPLIT_SUMMARY, GARMIN_METRICS,
                 GARMIN_BEST_EFFORT}
# Describe the recording setup (user profile, settings, sport, course...);
# only the first file's copy is kept.
SETUP_ONCE = {FILE_ID, FILE_CREATOR, 2, 3, 7, 12, 13, 72, 79, 141, 147, 394, 499}

SPLIT_TYPE_INTERVAL_ACTIVE = 3
EVENT_TIMER, EVENT_START, EVENT_STOP_ALL = 0, 0, 4
REF_SESSION, REF_LAP, REF_SPLIT = SESSION, LAP, SPLIT


class StitchError(Exception):
    pass


class Activity:
    def __init__(self, name, header, msgs):
        self.name = name
        self.header = header
        self.msgs = msgs
        ft = self.of(FILE_ID)[0].raw('type') if self.of(FILE_ID) else None
        if ft != 4:
            raise StitchError(f'{name}: not an activity file')
        sessions = self.of(SESSION)
        if not sessions:
            raise StitchError(f'{name}: has no session summary (unfinished or partial recording?)')
        if len(sessions) > 1:
            raise StitchError(f'{name}: contains {len(sessions)} sessions; multisport activities '
                              'are not supported')
        if self.of(LENGTH):
            raise StitchError(f'{name}: pool swimming activities are not supported')
        self.session = sessions[0]
        if not self.of(RECORD):
            raise StitchError(f'{name}: contains no record messages')

    def of(self, gnum):
        return [m for m in self.msgs if m.gnum == gnum]

    @property
    def start(self):
        s = self.session.raw('start_time')
        return s if s is not None else self.of(RECORD)[0].raw('timestamp')

    @property
    def end(self):
        """End of the recording, per the session summary (sub-second precision),
        falling back to the last record."""
        last = self.of(RECORD)[-1].raw('timestamp')
        elapsed = self.session.get('total_elapsed_time')
        if elapsed is None:
            return last
        return max(self.start + elapsed, last - 1)

    @property
    def summary_first(self):
        """Newer Garmin devices write the summary messages before the data."""
        idx = self.msgs.index(self.session)
        first_rec = next(i for i, m in enumerate(self.msgs) if m.gnum == RECORD)
        return idx < first_rec


def utc_offset(act):
    """The activity's local time offset in seconds, if recorded plausibly."""
    for m in act.of(ACTIVITY):
        lt, t = m.raw('local_timestamp'), m.raw('timestamp')
        if lt is not None and t is not None and abs(lt - t) <= 15 * 3600:
            return lt - t
    return None


def load(path):
    """Load a .fit file, or the single .fit inside a .zip (e.g. a Garmin Connect export)."""
    with open(path, 'rb') as f:
        data = f.read()
    name = str(path)
    if data[:4] == b'PK\x03\x04':
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            fits = [n for n in z.namelist() if n.lower().endswith('.fit')]
            if len(fits) != 1:
                raise StitchError(f'{path}: expected one .fit file inside the zip, found {len(fits)}')
            data = z.read(fits[0])
            name = f'{path}:{fits[0]}'
    try:
        header, msgs, warns = fitraw.read_bytes(data)
    except FitError as e:
        raise StitchError(f'{name}: {e}') from None
    act = Activity(name, header, msgs)
    act.warnings = [f'{name}: {w}' for w in warns]
    return act


# ---------------------------------------------------------------- merging

def _weighted(a, b, wa, wb):
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, list):
        return [_weighted(x, y, wa, wb) for x, y in zip(a, b)]
    if wa + wb == 0:
        return (a + b) / 2
    return (a * wa + b * wb) / (wa + wb)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, list):
        return [_add(x, y) for x, y in zip(a, b)]
    return a + b


def _pick(fn, a, b):
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, list):
        return [_pick(fn, x, y) for x, y in zip(a, b)]
    return fn(a, b)


def _with_fraction(m, name, frac):
    v = m.get(name)
    if v is not None and m.has(frac) and m.get(frac) is not None:
        v += m.get(frac)
    return v


# Undocumented Garmin session fields with known semantics.
GARMIN_SESSION_RULES = {
    205: 'first',  # body battery at start
    206: 'last',   # body battery at end
    207: 'last',   # body battery minimum
    215: 'first',  # stamina at start
    216: 'last',   # stamina at end
    178: 'sum',    # estimated sweat loss (ml)
    168: 'last', 211: 'last', 188: 'last', 138: 'last',
}
GARMIN_SPLIT_RULES = {
    101: 'sum',    # a duration in ms
    107: 'first', 108: 'last', 109: 'last',  # body battery start/end/min
}
SUMMARY_RULES = {
    'total_training_effect': 'max',
    'total_anaerobic_training_effect': 'max',
    'training_load_peak': 'sum',
    'first_lap_index': 'first',
    'avg_grade_adjusted_speed': 'avg',
    'avg_vert_speed': 'avg',
    'avg_lap_time': 'avg',
    'num_laps': 'sum',
    'num_splits': 'sum',
    'time_standing': 'sum',
    'stand_count': 'sum',
    'enhanced_avg_respiration_rate': 'avg',
}


def merge_summary(a, b, warnings, extra_rules=None):
    """Merge two summary messages (session/lap/split/split_summary) covering
    consecutive time ranges, a before b. Returns a modified copy of a."""
    rules = dict(SUMMARY_RULES)
    rules.update(extra_rules or {})
    m = a.copy()
    wa = a.get('total_timer_time') or 0
    wb = b.get('total_timer_time') or 0
    for num, _, _ in a.fields:
        name = a.name_of(num)
        sname = name if isinstance(name, str) else ''
        va, vb = a.get(num), b.get(num)
        if name in rules:
            rule = rules[name]
        elif num in rules:
            rule = rules[num]
        elif sname == 'total_elapsed_time':
            rule = 'elapsed'
        elif sname in ('avg_speed', 'enhanced_avg_speed'):
            rule = 'speed'
        elif sname == 'avg_cadence':
            rule = 'cadence'
        elif sname == 'max_cadence':
            rule = 'maxcadence'
        elif sname in ('total_ascent', 'total_descent'):
            rule = 'climb'
        elif sname in ('avg_fractional_cadence', 'max_fractional_cadence',
                       'total_fractional_ascent', 'total_fractional_descent'):
            rule = 'skip'  # handled together with their integer part
        elif sname == 'normalized_power':
            rule = 'np'
        elif sname in ('nec_lat', 'nec_long'):
            rule = 'max'
        elif sname in ('swc_lat', 'swc_long'):
            rule = 'min'
        elif sname.startswith(('total_', 'num_')) or sname == 'metabolic_calories':
            rule = 'sum'
        elif sname.startswith(('max_', 'enhanced_max_')):
            rule = 'max'
        elif sname.startswith(('min_', 'enhanced_min_')):
            rule = 'min'
        elif sname.startswith(('avg_', 'enhanced_avg_')):
            rule = 'avg'
        elif sname.startswith('end_'):
            rule = 'last'
        elif sname.startswith('start_') or sname in ('timestamp', 'message_index'):
            rule = 'first'
        else:
            rule = 'first'
            if va is None and vb is not None:
                rule = 'last'  # only the second part has a value: keep it
            elif vb is not None and va != vb:
                warnings.append(f'{a.name}.{name}: values differ ({va} vs {vb}); kept the first')
        if va is None and vb is None or rule in ('first', 'skip'):
            continue

        if rule == 'last':
            v = vb if vb is not None else va
        elif rule == 'sum':
            v = _add(va, vb)
        elif rule == 'max':
            v = _pick(max, va, vb)
        elif rule == 'min':
            v = _pick(min, va, vb)
        elif rule == 'avg':
            v = _weighted(va, vb, wa, wb)
        elif rule == 'elapsed':
            sa, sb = a.raw('start_time'), b.raw('start_time')
            v = (sb - sa) + (vb or 0) if sa is not None and sb is not None else _add(va, vb)
        elif rule == 'speed':
            da, db = a.get('total_distance'), b.get('total_distance')
            if da is not None and db is not None and wa + wb > 0:
                v = (da + db) / (wa + wb)
            else:
                v = _weighted(va, vb, wa, wb)
        elif rule in ('cadence', 'maxcadence', 'climb'):
            frac = {'cadence': 'avg_fractional_cadence', 'maxcadence': 'max_fractional_cadence',
                    'climb': 'total_fractional_ascent' if sname == 'total_ascent'
                    else 'total_fractional_descent'}[rule]
            fa, fb = _with_fraction(a, name, frac), _with_fraction(b, name, frac)
            c = {'cadence': lambda: _weighted(fa, fb, wa, wb),
                 'maxcadence': lambda: _pick(max, fa, fb),
                 'climb': lambda: _add(fa, fb)}[rule]()
            v = c
            if m.has(frac):
                v = int(c)
                m.set(frac, round(c - v, 4))
        elif rule == 'np':
            v = ((va ** 4 * wa + vb ** 4 * wb) / (wa + wb)) ** 0.25 if va and vb and wa + wb else (va or vb)
        elif callable(rule):
            v = rule(va, vb)
        else:
            raise ValueError(rule)
        m.set(num, v)
    for f in a.devfields:
        warnings.append(f'{a.name}: developer field {f[0]} kept from the first file only')
        break
    return m


def _tiz(msgs, ref_mesg, idx):
    for m in msgs:
        if m.gnum == TIME_IN_ZONE and m.raw('reference_mesg') == ref_mesg and m.raw('reference_index') == idx:
            return m
    return None


def merge_tiz(a, b, warnings):
    m = a.copy()
    for num, _, _ in a.fields:
        n = a.name_of(num)
        if str(n).startswith('time_in_'):
            m.set(num, _add(a.get(num), b.get(num)))
        elif n not in ('timestamp', 'reference_index') and a.get(num) != b.get(num):
            warnings.append(f'time_in_zone.{n}: zone settings differ ({a.get(num)} vs {b.get(num)}); '
                            'kept the first')
    return m


def _dev_key(m):
    """Identity of a developer_data_id message: everything but its index."""
    return tuple((f[0], f[2]) for f in m.fields if f[0] != 3)


def _remap_developer_data(a, b_msgs, warnings):
    """Make the second file's developer data indexes consistent with the first's.
    Returns the second file's messages with duplicates of A's definitions removed."""
    a_ids = {}  # key -> [indexes]
    for m in a.of(DEVELOPER_DATA_ID):
        a_ids.setdefault(_dev_key(m), []).append(m.raw(3))
    a_fields = {(m.raw(0), m.raw(1)): m for m in a.of(FIELD_DESCRIPTION)}
    used = {i for v in a_ids.values() for i in v}
    mapping, dropped = {}, set()
    out = []
    for m in b_msgs:
        if m.gnum == DEVELOPER_DATA_ID:
            old = m.raw(3)
            cands = a_ids.get(_dev_key(m), [])
            if cands:
                new = old if old in cands else cands[0]
                mapping[old] = new
                dropped.add(old)
                continue
            new = old if old not in used else max(used) + 1
            mapping[old] = new
            used.add(new)
            m = m.copy(); m.set_raw(3, new)
        elif m.gnum == FIELD_DESCRIPTION:
            old = m.raw(0)
            new = mapping.get(old, old)
            if old in dropped:
                prev = a_fields.get((new, m.raw(1)))
                if prev is not None:
                    if prev.fields != _with_dev_index(m, new).fields:
                        warnings.append(f'developer field {m.raw(3)!r} is defined differently in the '
                                        'two files; kept the first definition')
                    continue
            m = _with_dev_index(m, new)
        if m.devfields and any(mapping.get(f[1], f[1]) != f[1] for f in m.devfields):
            m = m.copy()
            for f in m.devfields:
                f[1] = mapping.get(f[1], f[1])
        out.append(m)
    return out


def _with_dev_index(m, idx):
    m = m.copy()
    m.set_raw(0, idx)
    return m


def merge_two(a, b, warnings, force=False):
    sa, sb = a.session, b.session
    for f in ('sport', 'sub_sport'):
        if sa.raw(f) != sb.raw(f):
            msg = f'{f} differs: {sa.get(f)} vs {sb.get(f)}'
            if not force:
                raise StitchError(msg + ' (use --force to stitch anyway)')
            warnings.append(msg)
    fa, fb = a.of(FILE_ID)[0], b.of(FILE_ID)[0]
    if (fa.raw('serial_number'), fa.raw('product')) != (fb.raw('serial_number'), fb.raw('product')):
        msg = 'the files were recorded by different devices'
        if not force:
            raise StitchError(msg + ' (use --force to stitch anyway)')
        warnings.append(msg)
    gap = b.start - a.end
    if gap < 0:
        raise StitchError(f'{b.name} starts {-gap:.0f} s before {a.name} ends; the activities overlap')

    t0 = a.start
    out_summary = []

    # --- session ---
    session = merge_summary(sa, sb, warnings, GARMIN_SESSION_RULES)
    laps_a, laps_b = a.of(LAP), b.of(LAP)
    if session.has('num_laps'):
        session.set('num_laps', len(laps_a) + len(laps_b))
    if session.has('total_elapsed_time'):
        session.set('total_elapsed_time', b.end - t0)
    if session.has('timestamp') and sa.raw('timestamp') != t0:
        session.set_raw('timestamp', sb.raw('timestamp'))  # end-of-session convention

    # --- activity ---
    acts_a, acts_b = a.of(ACTIVITY), b.of(ACTIVITY)
    act = None
    if acts_a or acts_b:
        act = (acts_a or acts_b)[0].copy()
        if act.has('total_timer_time'):
            act.set('total_timer_time', session.get('total_timer_time'))
        if acts_a and acts_b and act.raw('timestamp') != t0:
            act.set_raw('timestamp', acts_b[0].raw('timestamp'))
            if act.has('local_timestamp') and acts_b[0].has('local_timestamp'):
                act.set_raw('local_timestamp', acts_b[0].raw('local_timestamp'))

    # --- Garmin metrics snapshot: the latest one, patched with session values ---
    met = None
    if a.of(GARMIN_METRICS) or b.of(GARMIN_METRICS):
        met = (b.of(GARMIN_METRICS) or a.of(GARMIN_METRICS))[0].copy()
        for num, sname in ((60, 'total_ascent'), (61, 'total_descent'), (62, 'avg_power'),
                           (63, 'avg_heart_rate'), (37, 168), (39, 168)):
            if met.has(num) and session.has(sname) and session.raw(sname) is not None:
                try:
                    met.set_raw(num, session.raw(sname))
                except FitError:
                    pass

    # Summary messages written at the session start keep that timestamp.
    def restamp(m, original_first):
        if original_first is not None and original_first.raw('timestamp') == t0 and m.has('timestamp'):
            m.set_raw('timestamp', t0)
        return m

    # --- time in zone for the session ---
    tiz_session = None
    ta, tb = _tiz(a.msgs, REF_SESSION, 0), _tiz(b.msgs, REF_SESSION, 0)
    if ta and tb:
        tiz_session = merge_tiz(ta, tb, warnings)
    elif ta or tb:
        tiz_session = ta or tb
        warnings.append('time in zone for the session was only present in one file; kept as is')

    # --- laps (+ their time in zone) ---
    lap_msgs = []
    n_a = len(laps_a)
    if laps_a and laps_b:
        laps_a = laps_a[:-1] + [_extend_lap(laps_a, b.start - t0, laps_b[0].raw('start_time'), t0)]
    for i, lap in enumerate(laps_a):
        lap_msgs.append(lap)
        t = _tiz(a.msgs, REF_LAP, lap.raw('message_index') if lap.has('message_index') else i)
        if t:
            lap_msgs.append(t)
    for i, lap in enumerate(laps_b):
        old_idx = lap.raw('message_index') if lap.has('message_index') else i
        lap = restamp(lap.copy(), laps_a[0] if laps_a else None)
        if lap.has('message_index'):
            lap.set('message_index', n_a + i)
        lap_msgs.append(lap)
        t = _tiz(b.msgs, REF_LAP, old_idx)
        if t:
            t = restamp(t.copy(), laps_a[0] if laps_a else None)
            t.set('reference_index', n_a + i)
            lap_msgs.append(t)

    # --- splits (+ their time in zone); whole-activity splits are merged ---
    split_msgs = []
    sp_a, sp_b = a.of(SPLIT), b.of(SPLIT)
    whole_a = [i for i, s in enumerate(sp_a) if s.raw('split_type') == SPLIT_TYPE_INTERVAL_ACTIVE]
    whole_b = [i for i, s in enumerate(sp_b) if s.raw('split_type') == SPLIT_TYPE_INTERVAL_ACTIVE]
    merge_whole = len(whole_a) == 1 and len(whole_b) == 1
    idx_of = lambda s, i: s.raw('message_index') if s.has('message_index') else i
    for i, sp in enumerate(sp_a):
        t = _tiz(a.msgs, REF_SPLIT, idx_of(sp, i))
        if merge_whole and i == whole_a[0]:
            j = whole_b[0]
            sp = merge_summary(sp, sp_b[j], warnings, GARMIN_SPLIT_RULES)
            tb = _tiz(b.msgs, REF_SPLIT, idx_of(sp_b[j], j))
            if t and tb:
                t = merge_tiz(t, tb, warnings)
        split_msgs.append(sp)
        if t:
            split_msgs.append(t)
    k = len(sp_a)
    for i, sp in enumerate(sp_b):
        if merge_whole and i == whole_b[0]:
            continue
        t = _tiz(b.msgs, REF_SPLIT, idx_of(sp, i))
        sp = restamp(sp.copy(), sp_a[0] if sp_a else None)
        if sp.has('message_index'):
            sp.set('message_index', k)
        split_msgs.append(sp)
        if t:
            t = restamp(t.copy(), sp_a[0] if sp_a else None)
            t.set('reference_index', k)
            split_msgs.append(t)
        k += 1

    # --- split summaries: one per split type ---
    ss_msgs = []
    ss_b = {s.raw('split_type'): s for s in b.of(SPLIT_SUMMARY)}
    for s in a.of(SPLIT_SUMMARY):
        st = s.raw('split_type')
        if st in ss_b:
            rules = {'num_splits': (lambda x, y: x) if st == SPLIT_TYPE_INTERVAL_ACTIVE and merge_whole
                     else 'sum'}
            ss_msgs.append(merge_summary(s, ss_b.pop(st), warnings, rules))
        else:
            ss_msgs.append(s)
    for s in ss_b.values():
        s = s.copy()
        if s.has('message_index'):
            s.set('message_index', len(ss_msgs))
        ss_msgs.append(s)

    # --- best efforts: keep the fastest per distance ---
    best = {}
    for m in a.of(GARMIN_BEST_EFFORT) + b.of(GARMIN_BEST_EFFORT):
        best.setdefault(m.raw(2), []).append(m)
    best_msgs = [min(v, key=lambda m: m.raw(3) if m.raw(3) is not None else float('inf'))
                 for _, v in sorted(best.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))]

    # Time in zone referring to other message types is kept from both files.
    other_tiz = [m for src in (a, b) for m in src.of(TIME_IN_ZONE)
                 if m.raw('reference_mesg') not in (REF_SESSION, REF_LAP, REF_SPLIT)]
    if other_tiz:
        warnings.append(f'{len(other_tiz)} time-in-zone messages for other message types kept unchanged')

    # --- data streams ---
    offsets = _record_offsets(a)
    b_body = []
    a_setup = {m.gnum for m in a.msgs}
    n_sets = len(a.of(SET))
    for m in b.msgs:
        if m.gnum in SUMMARY_TYPES:
            continue
        if m.gnum in SETUP_ONCE and m.gnum in a_setup:
            continue
        if m.gnum == SET and m.has('message_index'):
            m = m.copy()  # strength sets: numbered on from the first part's
            m.set('message_index', n_sets + (m.raw('message_index') or 0))
        if m.gnum == RECORD and offsets:
            m = m.copy()
            for num, off in offsets.items():
                v = m.raw(num)
                if v is not None:
                    m.set_raw(num, v + off)
        b_body.append(m)
    b_body = _remap_developer_data(a, b_body, warnings)

    a_body = [m for m in a.msgs if m.gnum not in SUMMARY_TYPES and m.gnum not in (FILE_ID, FILE_CREATOR)]
    pause = _pause_events(a, b, t0)

    # --- assemble, following the first file's layout ---
    head = [m for m in a.msgs if m.gnum in (FILE_ID, FILE_CREATOR)]
    session_block = [session] + ([tiz_session] if tiz_session else []) + other_tiz
    if a.summary_first:
        summary = ([act] if act else []) + ([met] if met else []) + session_block + lap_msgs + split_msgs + ss_msgs
        msgs = head + summary + a_body + pause + b_body + best_msgs
    else:
        summary = lap_msgs + split_msgs + ss_msgs + session_block + ([act] if act else []) + ([met] if met else [])
        msgs = head + a_body + pause + b_body + best_msgs + summary
    merged = Activity(f'{a.name} + {b.name}', a.header, msgs)
    merged.gaps = getattr(a, 'gaps', []) + [gap] + getattr(b, 'gaps', [])
    return merged


def _extend_lap(laps, span, next_start, t0):
    """Stretch a part's last lap over the pause before the next part, as a
    real pause inside a lap would: elapsed time grows, timer time does not.

    Preferably the lap also absorbs the rounding residue of the part's laps,
    so lap elapsed times keep adding up to the session's (FIT validators allow
    at most 1 s difference, which would otherwise accumulate over the parts).
    If that would make it overlap the next part's first lap, it ends exactly
    where that lap starts instead, keeping laps contiguous."""
    lap = laps[-1]
    elapsed = [l.get('total_elapsed_time') for l in laps]
    if None in elapsed or lap.raw('start_time') is None:
        return lap
    old_end = lap.raw('start_time') + elapsed[-1]
    delta = span - sum(elapsed)
    if next_start is not None and abs(old_end + delta - next_start) > 1:
        delta = next_start - old_end
    if delta <= 0:
        return lap
    lap = lap.copy()
    lap.set('total_elapsed_time', elapsed[-1] + delta)
    ts = lap.raw('timestamp')
    if ts is not None and ts != t0 and abs(ts - old_end) <= 1:
        lap.set_raw('timestamp', round(old_end + delta))  # end-of-lap convention
    return lap


def _record_offsets(a):
    """Accumulating record fields: the second file's values continue from the
    first file's final values."""
    offsets = {}
    recs = a.of(RECORD)
    for num, fi in fitraw.PROFILE['messages'][RECORD]['fields'].items():
        if not fi['is_accumulated'] and fi['name'] != 'calories':
            continue
        last = next((r.raw(num) for r in reversed(recs) if r.raw(num) is not None), None)
        if last is not None:
            offsets[num] = last
    # distance: prefer the session total in case the last record lags behind it
    if 5 in offsets and a.session.raw('total_distance') is not None:
        offsets[5] = max(offsets[5], a.session.raw('total_distance'))
    return offsets


def _pause_events(a, b, t0):
    """Timer stop/start events marking the gap as a pause, if the files lack them."""
    out = []
    timer_a = [e for e in a.of(EVENT) if e.raw('event') == EVENT_TIMER]
    timer_b = [e for e in b.of(EVENT) if e.raw('event') == EVENT_TIMER]
    if not timer_a or timer_a[-1].raw('event_type') not in (1, EVENT_STOP_ALL):
        out.append(Msg.build(EVENT, timestamp=a.of(RECORD)[-1].raw('timestamp'),
                             event=EVENT_TIMER, event_type=EVENT_STOP_ALL, event_group=0))
    if not timer_b or timer_b[0].raw('event_type') != EVENT_START:
        out.append(Msg.build(EVENT, timestamp=b.start, event=EVENT_TIMER,
                             event_type=EVENT_START, event_group=0))
    return out


def stitch(activities, force=False):
    """Stitch activities (any order; sorted by start time). Returns (Activity, warnings)."""
    if len(activities) < 2:
        raise StitchError('need at least two activities to stitch')
    acts = sorted(activities, key=lambda x: x.start)
    warnings = [w for x in acts for w in getattr(x, 'warnings', [])]
    merged = acts[0]
    for nxt in acts[1:]:
        merged = merge_two(merged, nxt, warnings, force=force)
    return merged, acts, warnings
