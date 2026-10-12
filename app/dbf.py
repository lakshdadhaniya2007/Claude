"""Minimal read-only Visual FoxPro / dBase table reader.

Enough to lift the SHREE masters out of their .DBF files -- fields, records,
and memo (.FPT) text. Deleted records are skipped.
"""
import datetime
import os
import struct
import time


class Busy(OSError):
    """SHREE has the file open in a way that keeps everyone else out.

    FoxPro opens a table exclusively while it packs or reindexes it, and
    Windows then refuses even a read. That is a moment to wait out, not a
    fault in the file -- and never a reason to read the table as empty.
    """


PATIENCE = 3.0          # seconds a file SHREE holds is waited for
_waited_out = []        # files waited for in vain: the rest wait less


def read_bytes(path, patience=None):
    """A whole file, waiting a while SHREE is holding it.

    SHREE holds a table to itself for a moment while it saves, reindexes or
    takes its backup; the update reading SHREE waits up to PATIENCE for it
    (import_shree_full sets it). Once one file has been waited for in vain,
    SHREE is open on that year and the others are not waited for as long.
    `patience` instead waits that long, and is not counted.
    """
    last, waited, pause = None, 0.0, 0.2
    limit = patience if patience is not None else \
        min(PATIENCE, 3.0) if _waited_out else PATIENCE
    while True:
        try:
            with open(path, 'rb') as f:
                return f.read()
        except PermissionError as e:        # sharing or lock violation
            last = e
            if waited >= limit:
                break
            time.sleep(pause)
            waited += pause
            pause = min(pause * 1.5, 2.0)
    if patience is None:
        _waited_out.append(path)
    raise Busy('%s is in use by SHREE' % os.path.basename(path)) from last


def _memo_reader(path):
    """(read, close) for the table's memo file.

    The memo file is read whole the first time a memo is asked for, and not
    held open: SHREE may want it to itself.
    """
    fpt = os.path.splitext(path)[0] + '.fpt'
    if not os.path.exists(fpt):
        fpt = os.path.splitext(path)[0] + '.FPT'
    if not os.path.exists(fpt):
        return (lambda _n: ''), (lambda: None)
    state = {}

    def read(n):
        if not n:
            return ''
        if 'data' not in state:
            state['data'] = read_bytes(fpt)          # Busy goes up
            head = state['data'][:8]
            state['block'] = (struct.unpack('>H', head[6:8])[0]
                              if len(head) == 8 else 0) or 64
        data, block = state['data'], state['block']
        try:
            at = n * block
            length = struct.unpack('>I', data[at + 4:at + 8])[0]
            return data[at + 8:at + 8 + length].decode(
                'latin-1', 'replace').strip()
        except Exception:
            return ''

    def close():
        state.clear()
    return read, close


def read(path, limit=None, fields=None):
    """Every live record as a dict of python values.

    `fields` names the only columns wanted -- a ledger file has sixty, and
    turning each into a value is most of the cost of reading one. The file
    is read in one go and closed, so SHREE is never kept waiting on it.
    """
    data = read_bytes(path)
    head = data[:32]
    count = struct.unpack('<I', head[4:8])[0]
    hlen = struct.unpack('<H', head[8:10])[0]
    rlen = struct.unpack('<H', head[10:12])[0]
    cols, pos, off = [], 32, 1
    checks = []                 # every date and number column, for _more
    want = {x.upper() for x in fields} if fields else None
    while pos + 32 <= len(data):
        d = data[pos:pos + 32]
        pos += 32
        if d[0] == 0x0D:
            break
        name = d[0:11].split(b'\x00')[0].decode('latin-1').upper()
        size = d[16]
        if want is None or name in want:
            cols.append((name, chr(d[11]), off, off + size))
        if chr(d[11]) in ('D', 'N', 'F'):
            checks.append((chr(d[11]), off, off + size))
        off += size

    # A few tables in the backups carry a wrecked record count in their
    # header -- one claims half a billion rows in a two-megabyte file.
    # Trust the file's own size over the header.
    room = max(0, (len(data) - hlen) // rlen) if rlen else 0
    count = min(count, room)
    if room > count:
        count = _more(data, hlen, rlen, count, room, checks)

    memo, close = _memo_reader(path)

    def memo_text(text):
        return _typed(text.strip(), 'M', memo)
    # Each column's converter is chosen once, not asked of every value --
    # four million values in an update. Text columns first, as before, so a
    # record's keys keep their order. (Checked against the old reader on all
    # 5,231 SHREE tables here, 2.48 million rows: identical, 25% faster.)
    by_type = {'N': _num, 'F': _num, 'D': _date, 'L': _logical,
               'M': memo_text}
    conv = ([(n, a, b, str.strip) for n, t, a, b in cols if t not in _TYPED]
            + [(n, a, b, by_type[t]) for n, t, a, b in cols if t in _TYPED])
    out = []
    try:
        for i in range(count):
            start = hlen + i * rlen
            if data[start] == 42:                       # '*' -- deleted
                continue
            # latin-1 maps byte for byte, so the decoded record can be cut up
            # at the same places as the bytes.
            rec = data[start:start + rlen].decode('latin-1')
            out.append({n: fn(rec[a:b]) for n, a, b, fn in conv})
            if limit and len(out) >= limit:
                break
    finally:
        close()
    return out


_TYPED = ('N', 'F', 'D', 'L', 'M')
_NUMBER_BYTES = frozenset(b' 0123456789.-+')


def _more(data, hlen, rlen, count, room, checks):
    """How many records a table really holds when the file runs on past the
    count in its header.

    FoxPro writes a new record into the file at once, but a table it holds
    to itself has its header's count written back only when the table is
    closed. Read while SHREE is open, the newest entries -- the day's
    challans -- sat past the count and were never seen. A record past it is
    taken while it looks like one: a live or deleted mark first, and every
    date and number column holding a date, a number or nothing. The first
    that does not ends the table; FoxPro's end-of-file mark ends it too.
    """
    n = count
    while n < room:
        rec = data[hlen + n * rlen:hlen + (n + 1) * rlen]
        if rec[0] not in (0x20, 0x2A):
            break
        good = True
        for typ, a, b in checks:
            cell = rec[a:b]
            if typ == 'D':
                text = cell.decode('latin-1').strip()
                if text and _typed(text, 'D', None) == '':
                    good = False
                    break
            elif not _NUMBER_BYTES.issuperset(cell):
                good = False
                break
        if not good:
            break
        n += 1
    return n


def _num(text):
    text = text.strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


# A table repeats the same few thousand dates; each is checked once.
_DATES = {}


def _date(text):
    text = text.strip()
    got = _DATES.get(text)
    if got is None:
        got = _typed(text, 'D', None)
        if len(_DATES) < 200000:
            _DATES[text] = got
    return got


def _logical(text):
    return text.strip().upper() in ('T', 'Y')


def _typed(text, typ, memo):
    if typ in ('N', 'F'):
        try:
            return float(text) if text else 0.0
        except ValueError:
            return 0.0
    if typ == 'D':
        if len(text) == 8 and text.isdigit():
            try:
                datetime.date(int(text[:4]), int(text[4:6]), int(text[6:8]))
                return '%s-%s-%s' % (text[:4], text[4:6], text[6:8])
            except ValueError:
                return ''
        return ''
    if typ == 'L':
        return text.upper() in ('T', 'Y')
    if typ == 'M':
        # memo() answers '' for a pointer it cannot follow; a memo file SHREE
        # is holding (dbf.Busy) goes up to the caller.
        return memo(int(text)) if text.isdigit() else ''
    return text


def count(path):
    with open(path, 'rb') as f:
        return struct.unpack('<I', f.read(32)[4:8])[0]


def find(folder, name):
    """Case-insensitive lookup of a table inside a folder."""
    for cand in (name, name.upper(), name.lower(), name.capitalize()):
        p = os.path.join(folder, cand)
        if os.path.exists(p):
            return p
    target = name.lower()
    try:
        for entry in os.listdir(folder):
            if entry.lower() == target:
                return os.path.join(folder, entry)
    except OSError:
        pass
    return None
