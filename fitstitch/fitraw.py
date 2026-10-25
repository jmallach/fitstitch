"""Minimal lossless FIT reader/writer.

Messages are kept as raw field bytes keyed by field number, so every field
(including undocumented ones and developer fields) survives a read/write
round trip untouched. Only fields explicitly modified via Msg.set() are
re-encoded. The Garmin FIT SDK profile is used only for field names and
scale/offset metadata.
"""
import struct

from garmin_fit_sdk import profile

PROFILE = profile.Profile

# base type id -> (struct code, size, invalid value)
BASE = {
    0x00: ('B', 1, 0xFF), 0x01: ('b', 1, 0x7F), 0x02: ('B', 1, 0xFF),
    0x83: ('h', 2, 0x7FFF), 0x84: ('H', 2, 0xFFFF), 0x85: ('i', 4, 0x7FFFFFFF),
    0x86: ('I', 4, 0xFFFFFFFF), 0x07: ('s', 1, None), 0x88: ('f', 4, None),
    0x89: ('d', 8, None), 0x0A: ('B', 1, 0x00), 0x8B: ('H', 2, 0x0000),
    0x8C: ('I', 4, 0x00000000), 0x0D: ('B', 1, 0xFF), 0x8E: ('q', 8, 0x7FFFFFFFFFFFFFFF),
    0x8F: ('Q', 8, 0xFFFFFFFFFFFFFFFF), 0x90: ('Q', 8, 0x0000000000000000),
}
BASE_BY_NAME = {
    'enum': 0x00, 'sint8': 0x01, 'uint8': 0x02, 'sint16': 0x83, 'uint16': 0x84,
    'sint32': 0x85, 'uint32': 0x86, 'string': 0x07, 'float32': 0x88, 'float64': 0x89,
    'uint8z': 0x0A, 'uint16z': 0x8B, 'uint32z': 0x8C, 'byte': 0x0D, 'sint64': 0x8E,
    'uint64': 0x8F, 'uint64z': 0x90,
}

FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z as a Unix timestamp
TIMESTAMP, MESSAGE_INDEX = 253, 254


class FitError(Exception):
    pass


def crc16(data, crc=0):
    table = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]
    for byte in data:
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[byte & 0xF]
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[(byte >> 4) & 0xF]
    return crc


class Msg:
    def __init__(self, gnum, fields, arch=0, devfields=None):
        self.gnum = gnum
        self.arch = arch
        self.fields = fields  # list of [num, base_type, raw_bytes]
        self.devfields = devfields or []  # list of [num, dev_data_index, raw_bytes]

    @classmethod
    def build(cls, gnum, **values):
        """Create a message from scaled values, using profile base types."""
        m = cls(gnum, [])
        for name, value in values.items():
            num = m.num_of(name)
            fi = m.finfo(num)
            bt = BASE_BY_NAME[fi['base_type']] if fi else BASE_BY_NAME['uint32']
            if isinstance(value, str):
                m.fields.append([num, bt, value.encode('utf-8') + b'\0'])
                continue
            m.fields.append([num, bt, b'\0' * BASE[bt][1]])
            m.set(num, value)
        return m

    def copy(self):
        return Msg(self.gnum, [list(f) for f in self.fields], self.arch,
                   [list(f) for f in self.devfields])

    @property
    def pdef(self):
        return PROFILE['messages'].get(self.gnum)

    @property
    def name(self):
        p = self.pdef
        return p['name'] if p else f'unknown_{self.gnum}'

    def finfo(self, num):
        p = self.pdef
        if p and num in p['fields']:
            return p['fields'][num]
        return None

    def name_of(self, num):
        if num == TIMESTAMP:
            return 'timestamp'
        if num == MESSAGE_INDEX:
            return 'message_index'
        fi = self.finfo(num)
        return fi['name'] if fi else num

    def num_of(self, name):
        if isinstance(name, int):
            return name
        if name == 'timestamp':
            return TIMESTAMP
        if name == 'message_index':
            return MESSAGE_INDEX
        p = self.pdef
        if p:
            for n, fi in p['fields'].items():
                if fi['name'] == name:
                    return n
        raise KeyError(name)

    def _field(self, num):
        for f in self.fields:
            if f[0] == num:
                return f
        return None

    def has(self, key):
        try:
            num = self.num_of(key)
        except KeyError:
            return False
        f = self._field(num)
        return f is not None

    def _scale(self, num):
        fi = self.finfo(num)
        if not fi or num in (TIMESTAMP, MESSAGE_INDEX):
            return 1, 0
        s = fi['scale'][0] if fi['scale'] and len(fi['scale']) == 1 else 1
        o = fi['offset'][0] if fi['offset'] and len(fi['offset']) == 1 else 0
        return s, o

    def raw(self, key):
        """Decoded raw (unscaled) value; list for arrays; None if absent or invalid."""
        try:
            num = self.num_of(key)
        except KeyError:
            return None
        f = self._field(num)
        if f is None:
            return None
        code, size, inv = BASE.get(f[1], ('B', 1, None))
        data = f[2]
        if code == 's':
            return data.split(b'\0')[0].decode('utf-8', 'replace')
        end = '<' if self.arch == 0 else '>'
        n = len(data) // size
        if n == 0:
            return None  # field shorter than its type: invalid
        vals = list(struct.unpack(end + code * n, data[:n * size]))
        vals = [None if (inv is not None and v == inv) or (code in 'fd' and v != v) else v for v in vals]
        return vals if n > 1 else vals[0]

    def get(self, key):
        """Scaled value (e.g. metres, seconds), None if absent or invalid."""
        try:
            num = self.num_of(key)
        except KeyError:
            return None
        v = self.raw(num)
        if v is None or isinstance(v, str):
            return v
        s, o = self._scale(num)
        conv = lambda x: None if x is None else (x / s - o if (s, o) != (1, 0) else x)
        return [conv(x) for x in v] if isinstance(v, list) else conv(v)

    def set_raw(self, key, value):
        num = self.num_of(key)
        f = self._field(num)
        if f is None:
            raise KeyError(f'field {key} not present in {self.name}')
        code, size, inv = BASE[f[1]]
        end = '<' if self.arch == 0 else '>'
        vals = value if isinstance(value, list) else [value]
        if len(vals) * size != len(f[2]):
            raise FitError(f'{self.name}.{key}: {len(vals)} values do not fit {len(f[2])} bytes')
        lo, hi = _range(code)
        out = []
        for v in vals:
            if v is None:
                v = inv if inv is not None else float('nan')
            elif code not in 'fd':
                v = int(round(v))
                if not lo <= v <= hi:
                    raise FitError(f'{self.name}.{key}: value {v} out of range for its type')
            out.append(v)
        f[2] = struct.pack(end + code * len(out), *out)

    def set(self, key, value):
        num = self.num_of(key)
        s, o = self._scale(num)
        conv = lambda x: None if x is None else (x + o) * s
        if isinstance(value, list):
            self.set_raw(num, [conv(x) for x in value])
        else:
            self.set_raw(num, conv(value))

    def remove(self, key):
        num = self.num_of(key)
        self.fields = [f for f in self.fields if f[0] != num]

    def as_dict(self):
        return {self.name_of(f[0]): self.get(f[0]) for f in self.fields}

    def __repr__(self):
        return f'<{self.name} {self.as_dict()}>'


def _range(code):
    return {
        'B': (0, 0xFF), 'b': (-0x80, 0x7F), 'H': (0, 0xFFFF), 'h': (-0x8000, 0x7FFF),
        'I': (0, 0xFFFFFFFF), 'i': (-0x80000000, 0x7FFFFFFF),
        'Q': (0, 0xFFFFFFFFFFFFFFFF), 'q': (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF),
    }.get(code, (None, None))


def read_bytes(b):
    """Parse a FIT file. Returns (header bytes, list of Msg, warnings)."""
    warnings = []
    if len(b) < 12:
        raise FitError('file too short to be a FIT file')
    hs = b[0]
    if hs not in (12, 14) or b[8:12] != b'.FIT':
        raise FitError('not a FIT file')
    ds = struct.unpack('<I', b[4:8])[0]
    if len(b) < hs + ds + 2:
        raise FitError('file is truncated')
    if crc16(b[:hs + ds + 2]) != 0:
        raise FitError('file CRC check failed (corrupted file)')
    if len(b) > hs + ds + 2:
        warnings.append('file contains more than one chained FIT stream; only the first was read')
    header = b[:hs]
    i, defs, msgs = hs, {}, []
    last_ts = None
    while i < hs + ds:
        h = b[i]; i += 1
        if h & 0x80:  # compressed timestamp header
            lt = (h >> 5) & 0x03
            offset = h & 0x1F
            if last_ts is None:
                raise FitError('compressed timestamp before any full timestamp')
            ts = (last_ts & ~0x1F) + offset
            if offset < (last_ts & 0x1F):
                ts += 0x20
            last_ts = ts
            msg = _read_data(b, i, defs, lt)
            i += _data_size(defs[lt])
            msg.fields.insert(0, [TIMESTAMP, 0x86, struct.pack('<I' if msg.arch == 0 else '>I', ts)])
            msgs.append(msg)
            continue
        lt = h & 0x0F
        if h & 0x40:  # definition
            arch = b[i + 1]
            end = '<' if arch == 0 else '>'
            g = struct.unpack(end + 'H', b[i + 2:i + 4])[0]
            n = b[i + 4]; i += 5
            fl = [(b[i + 3 * k], b[i + 3 * k + 1], b[i + 3 * k + 2]) for k in range(n)]
            i += 3 * n
            dl = []
            if h & 0x20:
                nd = b[i]; i += 1
                dl = [(b[i + 3 * k], b[i + 3 * k + 1], b[i + 3 * k + 2]) for k in range(nd)]
                i += 3 * nd
            defs[lt] = (g, arch, fl, dl)
        else:
            if lt not in defs:
                raise FitError(f'data message for undefined local type {lt}')
            msg = _read_data(b, i, defs, lt)
            i += _data_size(defs[lt])
            ts = msg.raw(TIMESTAMP)
            if ts is not None:
                last_ts = ts
            msgs.append(msg)
    return header, msgs, warnings


def _data_size(d):
    return sum(f[1] for f in d[2]) + sum(f[1] for f in d[3])


def _read_data(b, i, defs, lt):
    g, arch, fl, dl = defs[lt]
    fields, devfields = [], []
    for num, size, bt in fl:
        fields.append([num, bt, b[i:i + size]]); i += size
    for num, size, dev in dl:
        devfields.append([num, dev, b[i:i + size]]); i += size
    return Msg(g, fields, arch, devfields)


def read(path):
    with open(path, 'rb') as f:
        return read_bytes(f.read())


def encode(header, msgs):
    body = bytearray()
    slots = {}  # definition key -> local type
    order = []  # LRU of definition keys
    for m in msgs:
        key = (m.gnum, m.arch, tuple((f[0], len(f[2]), f[1]) for f in m.fields),
               tuple((f[0], len(f[2]), f[1]) for f in m.devfields))
        if key not in slots:
            if len(slots) < 16:
                lt = len(slots)
            else:
                lt = slots.pop(order.pop(0))
            slots[key] = lt
            end = '<' if m.arch == 0 else '>'
            flag = 0x20 if m.devfields else 0
            body += bytes([0x40 | flag | lt, 0, m.arch]) + struct.pack(end + 'H', m.gnum) + bytes([len(m.fields)])
            for num, size, bt in key[2]:
                body += bytes([num, size, bt])
            if m.devfields:
                body += bytes([len(m.devfields)])
                for num, size, dev in key[3]:
                    body += bytes([num, size, dev])
        else:
            order.remove(key)
        order.append(key)
        body += bytes([slots[key]])
        for f in m.fields:
            body += f[2]
        for f in m.devfields:
            body += f[2]
    hdr = bytearray(header)
    hdr[4:8] = struct.pack('<I', len(body))
    if len(hdr) >= 14:
        hdr[12:14] = struct.pack('<H', crc16(hdr[:12]))
    out = bytes(hdr) + bytes(body)
    return out + struct.pack('<H', crc16(out))


def write(path, header, msgs):
    data = encode(header, msgs)
    with open(path, 'wb') as f:
        f.write(data)
    return data
