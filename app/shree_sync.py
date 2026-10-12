#!/usr/bin/env python3
"""Keep the books in step with SHREE, the old FoxPro software.

Asked for on 16 Sep 2026: *a section which will automatically fetch the data
from the old software files and accordingly update this software's data.*

SHREE is still where most of the day's entries are made, on two machines --
the accounts book (`SHREE BST`) and the GST bill registers (`SHREE BILL`).
`import_shree_full.py` reads both and rebuilds every row it ever brought in;
a handful of other scripts then write what SHREE never recorded. Until now
that chain was run by hand. This runs it, and keeps the app usable while it
does:

  1. The books are copied, and the whole chain runs on the copy. Saving is
     paused meanwhile (a request that writes is answered "try again in a
     minute"); reading carries on against the live books.
  2. Links from this app's own documents to imported ones are written down
     first and put back after -- the import lets go of them.
  3. Decisions taken here about imported rows -- a customer deleted, a bank
     folded into its renamed successor, a party billed by another firm, a
     document edited or deleted -- are replayed (`sync_rule`).
  4. The result is checked against the live books: nothing of this app's own
     may change, SHREE's newest entry may not be older than before, whole
     years may not vanish, and the file has to be sound.
  5. Only then is the live file replaced -- a copy of it is kept first -- by
     SQLite's own backup, which writes into the file the app has open.

Nothing reaches the live books unless every check passes, and a preview does
the whole run without replacing anything.

**An update runs only when somebody asks for it** (24 Sep 2026: *"stop the
auto fetching from SHREE; only when the user commands should it fetch
everything"*) -- Update now on the screen, or --run below. Nothing looks at
SHREE's folders in between; the watch that used to update by itself is gone.

    python3 app/shree_sync.py --status
    python3 app/shree_sync.py --run [--preview]     (stop the app first)

The `--step` forms are what the app runs, each in its own process against the
working copy named by BILLING_DB.
"""
import argparse
from collections import defaultdict
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import api   # noqa: E402
import db    # noqa: E402
import import_shree_full as shree   # noqa: E402

FIRMS = ('BSTP', 'BST', 'BSI')             # the whole app -- see three-firms-only
SETTINGS_KEY = 'shree_sync'
SEED_KEY = 'shree_rules_seeded'
BACKUP_REASON = 'shree-update'
BACKUPS_KEPT = 5
RUNS_KEPT = 40
LOG_LIMIT = 400000                          # characters of log kept per run
DAYS_KEPT = 7                               # and the last copy of each day

HOLD_SECONDS = 240          # a save waits this long for an update to finish
CHANGES_KEPT = 60           # updates remembered for pages opened before them
IDS_KEPT = 20000            # ids remembered per update, per kind of row
SHREE_FILES = ('.dbf', '.fpt', '.cdx', '.zip')
TABLE_NAME = {'invoice': 'document', 'voucher': 'voucher',
              'ledger': 'account', 'item': 'item'}

DEFAULTS = dict(
    sources=[],         # SHREE's folders -- looked for until they are named
    found=False,        # the folders were found, not chosen
    known=[],           # every folder SHREE's books were ever found in here
)

# The SHREE firm codes this app's firms are read from: ESTI in the accounts
# book, BST and BSI in the bill registers.
SHREE_CODES = {code for (_role, code), spec in shree.FIRMS.items()
               if spec['code'] in FIRMS}
BOOK_NAME = {'BSTP': 'accounts book', 'BST': 'bill register',
             'BSI': 'bill register'}

INVOICE_KINDS = ('SALE', 'PURCHASE', 'CREDIT', 'DEBIT')
VOUCHER_KINDS = ('RECEIPT', 'PAYMENT', 'JOURNAL', 'CONTRA')

# What was decided before this screen existed, played after every update
# like anything decided since. See the notes on the CO-OP banks and on the
# renamed bank accounts: SHREE renames an account under the same code, the
# import keys accounts by name, and the old names came back with balances.
DECIDED = [
    ('fold', 'BSTP', 'AUSF BANK : B.S. INDUSTRIES',
     'AUSF BANK : B.S. INDUSTRIES FDR 20 LAKH', 'Renamed bank account, 15 Sep 2026'),
    ('fold', 'BSTP', 'AUSF BANK : B.S. INDUSTRIES FDR 20 LAKH',
     'AUSF BANK : B.S.I. 2121247634598357', 'Renamed bank account, 15 Sep 2026'),
    ('fold', 'BSTP', 'AUSF BANK : B.S. TRADERS',
     'AUSF BANK : B.S. TRADERS FDR 21 LAKH', 'Renamed bank account, 15 Sep 2026'),
    ('fold', 'BSTP', 'AUSF BANK : B.S. TRADERS FDR 21 LAKH',
     'AUSF BANK : B.S.T. 2121247634607682', 'Renamed bank account, 15 Sep 2026'),
    ('fold', 'BSTP', 'KOTAK BANK : BALKRISHNA STEEL',
     'KOTAK BANK : B. S. TRADERS', 'Renamed bank account, 15 Sep 2026'),
] + [
    ('purge_ledger', 'BSTP', name, '',
     'Deleted with its entries, 2 Sep 2026') for name in (
        'THE CO-OP BANK : BALKRISHNA',
        'THE CO-OP BANK : BALKRISHNA STEEL TRADERS',
        'THE CO-OP BANK : PATEL STEEL',
        'THE CO-OP BANK : PATEL STEEL INDUSTRIES',
        'THE CO-OP BANK : PATEL TRADERS',
        'THE CO-OP BANK : VRAJ TRADERS')
] + [
    ('purge_ledger', 'BSTP', 'KOTAK BANK : PATEL STEEL INDUS', '',
     'Deleted with its entries, 15 Sep 2026'),
] + [
    ('move_group', 'BSTP', name, '2201',
     'Personal bank account shown with the bank accounts, 15 Sep 2026')
    for name in (
        'AUSF : 2211247643346281 (VIPULBHAI)',
        'AUSF :2211247642455390 (POONAMBEN)',
        'B.O.B BANK : 2804 (VIPULBHAI)',
        'B.O.B BANK : 9102 (VIPUL HUF)',
        'ICICI BANK (POONAMBEN): 624801051316',
        'KOTAK :5611986866 (POONAMBEN)',
        'KOTAK BANK 48584 (LAKSH)',
        'R.D.C BANK : 9213 (KOLKI)',
        'S.B.I BANK (LAKSH)20413158302',
        'S.B.I BANK : 2802 (POONAMBEN)',
        'S.B.I BANK : 3689 (VIPULBHAI)',
        'SBI POONAMBEN 4951 (PPF)',
        'SBI VIPULBHAI 9717(PPF)')
]

# Deleted in the app before deletions were written down -- kept only where
# these books really are without them.
DECIDED_IF_ABSENT = [
    ('delete_account', 'BSTP', 'PAVAN STEEL', '', 'Deleted in this app, 16 Sep 2026'),
    ('delete_account', 'BSTP', 'YASH ENGINEERS', '', 'Deleted in this app, 16 Sep 2026'),
]

RULE_LABEL = {
    'fold': 'Old account folded into its new name',
    'purge_ledger': 'Account deleted with its entries',
    'move_group': 'Account kept in another group',
    'rename_ledger': 'Account renamed here',
    'merge_ledger': 'Account merged into another here',
    'moved_ledger': 'Customer moved to another firm',
    'billing_firm': 'Billing firm chosen here',
    'delete_account': 'Account deleted here',
    'fold_item': 'Item moved into another here',
    'delete_item': 'Item deleted here',
    'delete_doc': 'Document deleted here',
    'supersede_doc': 'Document edited here',
    'drop_years': 'Old years deleted here',
}
REPLAY_PHASES = {
    # Before pairing, so a challan billed or deleted here is not paired again.
    'docs': ('supersede_doc', 'delete_doc'),
    # After allocation, so a firm chosen here is not overwritten.
    'accounts': ('rename_ledger', 'fold', 'merge_ledger', 'purge_ledger',
                 'move_group',
                 'moved_ledger', 'delete_account', 'billing_firm',
                 'fold_item', 'delete_item',
                 # Last: the old years read in again go once every decision
                 # about their accounts has been played, so what is carried
                 # forward is carried onto the accounts as they now stand.
                 'drop_years'),
}


def now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def norm(name):
    """Accounts are matched the way the import matches them."""
    return re.sub(r'\s+', ' ', (name or '').strip()).upper()


# ------------------------------------------------------------- settings

def settings(conn):
    out = dict(DEFAULTS)
    try:
        saved = json.loads(db.get_meta(conn, SETTINGS_KEY) or '{}')
    except ValueError:
        saved = {}
    # Books saved before 24 Sep 2026 also carry auto, check_sec and
    # quiet_sec -- the update that ran by itself. Nothing reads them now.
    out.update({k: v for k, v in (saved or {}).items() if k in DEFAULTS})
    return out


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _with_known(known, homes):
    """The folders SHREE was found in, newest first, without repeats."""
    out = []
    for p in list(homes or []) + list(known or []):
        if p and not any(_same(p, q) for q in out):
            out.append(p)
    return out[:KNOWN_KEPT]


def remember(conn, homes):
    """Write down folders SHREE's books were just seen in, so a later look
    tries them first -- a drive letter that changes back, a computer that
    comes back on."""
    homes = [h for h in homes or [] if h]
    if not homes:
        return
    cur = settings(conn)
    known = _with_known(cur.get('known'), homes)
    if known != list(cur.get('known') or []):
        cur['known'] = known
        db.set_meta(conn, SETTINGS_KEY,
                    json.dumps({k: cur[k] for k in DEFAULTS}))
        conn.commit()


def save_settings(conn, data):
    cur = settings(conn)
    if 'sources' in data:
        homes = []
        # A folder already on the list is kept as it is, reachable or not.
        # Checking it again is what made the list impossible to change once
        # a folder had gone: SHREE BILL and SHREE BST were moved on 17 Sep
        # 2026, and Change, Remove and Add all failed on the one still
        # listed. (On Windows the same happened whenever the other computer
        # was switched off.) Only a newly named folder has to be there.
        saved = list(cur.get('sources') or [])
        for raw in data.get('sources') or []:
            path = str(raw or '').strip().strip('"').strip()
            if not path:
                continue
            if any(_same(path, old) for old in saved):
                if not any(_same(path, h) for h in homes):
                    homes.append(path)
                continue
            if not os.path.exists(path):
                raise api.ApiError('This folder cannot be reached: %s -- if it '
                                   'is on another computer, is that computer '
                                   'switched on and the folder shared?' % path)
            home = shree.shree_home(path)
            if not home:
                raise api.ApiError(
                    'No SHREE books were found in %s. Choose the SHREE folder '
                    '-- the one holding shree.exe and the year folders '
                    '(25-26, 26-27 ...) -- or the drive it is on.' % path)
            if not any(_same(home, h) for h in homes):
                homes.append(home)
        # None named: the folders are looked for again, as at the start.
        cur['sources'] = homes
        cur['found'] = bool(homes) and bool(data.get('found'))
        cur['known'] = _with_known(cur.get('known'), homes)
        _described.clear()
    keep = {k: cur[k] for k in DEFAULTS}
    db.set_meta(conn, SETTINGS_KEY, json.dumps(keep))
    conn.commit()
    return keep


# ------------------------------------------------------ where SHREE is
#
# SHREE is not kept beside this app. It runs from <drive>:\SHREE on each of
# its two computers -- the accounts book on one, the bill registers on the
# other -- and a folder on the other computer is reached through the network
# (a mapped drive, or \\COMPUTER\share). So the folders are found rather than
# assumed: every drive this computer can see, and SHREE's own shortcuts.

LOOK_TTL = 300                  # seconds a look at a folder is trusted
MISS_TTL = 30                   # ... and a look that found nothing
NET_WAIT = 5                    # seconds a network folder is given to answer
DISCOVER_BUDGET = 45            # seconds one look for SHREE may take in all
KNOWN_KEPT = 20                 # folders SHREE was found in, remembered
_described = {}                 # path -> (when, what it holds)
_found = [0.0, None]            # when, what was found

# GetDriveType's answers, for the drives worth looking at.
_DRIVE_KIND = {2: 'removable', 3: 'fixed', 4: 'network', 6: 'ram'}


def within(seconds, fn, *args, **kw):
    """fn(*args), or `default` if it has not answered in `seconds`.

    A network drive whose computer is switched off can hold a folder listing
    for half a minute or more. The question is asked on a thread of its own
    and, if it takes too long, answered as "not there"; the thread is left to
    give up in its own time.
    """
    default = kw.pop('default', None)
    box = {}

    def run():
        try:
            box['value'] = fn(*args)
        except Exception as e:                     # noqa: BLE001
            box['error'] = e
    t = threading.Thread(target=run, name='slow-folder', daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive() or 'error' in box:
        return default
    return box.get('value', default)


def drive_list():
    """(root, kind) for every drive worth looking at on this computer.

    CD drives are left out -- asking an empty one brings up a "no disk" box on
    some machines -- and so are A: and B:.
    """
    if os.name != 'nt':
        return []
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
    except Exception:                              # noqa: BLE001
        return []
    out = []
    for i in range(2, 26):
        if not mask >> i & 1:
            continue
        root = '%s:\\' % chr(65 + i)
        kind = _DRIVE_KIND.get(k32.GetDriveTypeW(ctypes.c_wchar_p(root)))
        if kind:
            out.append((root, kind))
    return out


def _drives():
    return [root for root, _kind in drive_list()]


quiet_windows_errors = db.quiet_windows_errors


def network_shares(server):
    """The shared folders of another computer, for \\\\COMPUTER typed alone."""
    if os.name != 'nt':
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class SHARE_INFO_1(ctypes.Structure):
            _fields_ = [('netname', wintypes.LPWSTR),
                        ('type', wintypes.DWORD),
                        ('remark', wintypes.LPWSTR)]
        netapi = ctypes.windll.netapi32
        buf = ctypes.POINTER(SHARE_INFO_1)()
        read, total = wintypes.DWORD(), wintypes.DWORD()
        resume = wintypes.DWORD()
        rc = netapi.NetShareEnum(ctypes.c_wchar_p(server), 1,
                                 ctypes.byref(buf), wintypes.DWORD(-1),
                                 ctypes.byref(read), ctypes.byref(total),
                                 ctypes.byref(resume))
        if rc != 0:
            return []
        try:
            out = []
            for i in range(read.value):
                item = buf[i]
                # Disk shares only, and not the hidden C$ / ADMIN$ kind.
                if item.type & 0xFF == 0 and not item.type & 0x80000000:
                    out.append(item.netname)
            return sorted(out, key=str.lower)
        finally:
            netapi.NetApiBufferFree(buf)
    except Exception:                              # noqa: BLE001
        return []


_EXE_PATH = re.compile(r'((?:[A-Za-z]:|\\\\[^\\\x00-\x1f"*?<>|]+)'
                       r'(?:\\[^\\\x00-\x1f"*?<>|]+)*?)\\shree\w*\.exe',
                       re.IGNORECASE)
_CD_SHREE = re.compile(r'(?im)^\s*cd\s*\\\s*(shree)\b')


def _shortcut_places():
    """Where SHREE's shortcuts and batch files say it is.

    The desktop shortcut on the accounts computer reads F:\\SHREE\\shree.exe;
    its batch file does `cd\\shree` on the drive it sits on.
    """
    if os.name != 'nt':
        return []
    env = os.environ.get
    folders = [os.path.join(env('USERPROFILE', ''), 'Desktop'),
               os.path.join(env('USERPROFILE', ''), 'OneDrive', 'Desktop'),
               os.path.join(env('PUBLIC', ''), 'Desktop'),
               os.path.join(env('APPDATA', ''), 'Microsoft', 'Windows',
                            'Start Menu', 'Programs'),
               os.path.join(env('PROGRAMDATA', ''), 'Microsoft', 'Windows',
                            'Start Menu', 'Programs')]
    out = []
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for dirpath, dirs, files in os.walk(folder):
            if dirpath.count(os.sep) - folder.count(os.sep) >= 2:
                dirs[:] = []
            for name in files:
                low = name.lower()
                if 'shree' not in low or not low.endswith(
                        ('.lnk', '.pif', '.bat', '.cmd', '.url')):
                    continue
                out += shortcut_targets(os.path.join(dirpath, name))
    return out


_RUNNING_EXE = re.compile(r'(?i)^shree\w*\.exe$')


def _running_places():
    """The folders SHREE is running from on this computer right now.

    SHREE open on this computer says exactly where its books are, whatever
    drive letter its USB drive was given today and however deep the folder
    sits. Only programs of whoever is signed in can be asked, which is
    where SHREE runs.
    """
    if os.name != 'nt':
        return []
    try:
        import ctypes
        from ctypes import wintypes
        psapi, k32 = ctypes.windll.psapi, ctypes.windll.kernel32
        ids = (wintypes.DWORD * 4096)()
        used = wintypes.DWORD()
        if not psapi.EnumProcesses(ids, ctypes.sizeof(ids),
                                   ctypes.byref(used)):
            return []
        out = []
        for pid in ids[:used.value // ctypes.sizeof(wintypes.DWORD)]:
            # PROCESS_QUERY_LIMITED_INFORMATION
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                continue
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if k32.QueryFullProcessImageNameW(h, 0, buf,
                                                  ctypes.byref(size)):
                    folder, name = os.path.split(buf.value)
                    if _RUNNING_EXE.match(name) and folder not in out:
                        out.append(folder)
            finally:
                k32.CloseHandle(h)
        return out
    except Exception:                              # noqa: BLE001
        return []


def shortcut_targets(path):
    """The SHREE folders one shortcut or batch file names."""
    try:
        with open(path, 'rb') as fh:
            raw = fh.read(262144)
    except OSError:
        return []
    found = []
    for text in (raw.decode('latin-1'), raw.decode('utf-16-le', 'ignore')):
        for m in _EXE_PATH.finditer(text):
            found.append(m.group(1))
        if _CD_SHREE.search(text):
            drive = os.path.splitdrive(os.path.abspath(path))[0]
            found += [d + '\\SHREE' for d in
                      re.findall(r'(?im)^\s*([A-Za-z]:)\s*$', text)] or \
                     ([drive + '\\SHREE'] if drive else [])
    return found


_probes_read = []


def _books_in(home):
    """Which of this app's firms the newest years of an installation feed,
    and how far each runs."""
    # What the last update read of each year, as the importer keeps it: a
    # year SHREE holds to itself still says how far it ran then. Without
    # it, the first look after the app started -- with SHREE already open
    # on the accounts computer, as on any morning -- passed over this year,
    # and the update said BSTP's newest entry was the 31st of March
    # (27 Sep 2026).
    if not _probes_read:
        _probes_read.append(True)
        try:
            shree.load_probes(unpack_dir())
        except Exception:                          # noqa: BLE001
            pass
    today = datetime.date.today().isoformat()
    years = []
    for entry in os.listdir(home):
        m = shree.YEAR_RE.match(entry)
        if m and os.path.isdir(os.path.join(home, entry)):
            years.append((m.group(0), -len(entry), entry))
    books = {}
    looked = set()
    for fy, _l, entry in sorted(years, reverse=True):
        if len(looked) >= 3 and fy not in looked:
            break
        looked.add(fy)
        ydir = os.path.join(home, entry)
        for sub in sorted(os.listdir(ydir)):
            code = sub.split(' ')[0].split('_')[0].split('-')[0].upper()
            if code not in SHREE_CODES or not os.path.isdir(
                    os.path.join(ydir, sub)):
                continue
            try:
                src = shree._probe(os.path.join(ydir, sub), code, entry,
                                   fy_hint=fy)
            except Exception:                      # noqa: BLE001
                continue
            if not src or src.target not in FIRMS:
                continue
            upto = src.upto if src.upto <= today else ''
            have = books.get(src.target)
            if have is None or (src.fy, upto) > (have['fy'], have['upto']):
                books[src.target] = dict(
                    firm=src.target, fy=src.fy, upto=upto,
                    saved=src.saved if src.saved <= today else '',
                    kind='accounts' if src.role == 'books' else 'bills')
        if len(books) == len(FIRMS):
            break
    return [books[f] for f in FIRMS if f in books]


def describe(path):
    """What one SHREE folder holds, for the screen."""
    hit = _described.get(path)
    if hit and time.time() - hit[0] < (LOOK_TTL if hit[1]['found']
                                       else MISS_TTL):
        return hit[1]
    out = dict(path=path, found=False, home='', books=[], beside=[],
               network=is_network(path), note='')

    def look():
        home = shree.shree_home(path)
        if home:
            home, places = shree.source_places(home)
            out.update(found=True, home=home, books=_books_in(home),
                       beside=[os.path.basename(p) for p in places[1:]])
        return True
    # A folder on another computer that is switched off is not waited for.
    if within(NET_WAIT * 4 if out['network'] else 120, look,
              default=False) is False:
        out.update(found=False, books=[], beside=[],
                   note='did not answer')
    _described[path] = (time.time(), out)
    return out


def is_network(path):
    """True for a folder on another computer: \\\\COMPUTER\\share, or a
    drive letter mapped to one."""
    if str(path).startswith(('\\\\', '//')):
        return True
    path = os.path.abspath(path)
    if os.name != 'nt':
        # A drive mounted on a Mac -- most often the Windows computer's --
        # is given the short wait a network folder gets.
        return path.startswith('/Volumes/')
    try:
        import ctypes
        root = os.path.splitdrive(path)[0] + '\\'
        if ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 4:
            return True
    except Exception:                              # noqa: BLE001
        return False
    # A remembered drive of another computer's, not connected yet.
    return _unc_of(path) != path


# Folders at the top of a drive that hold Windows or its programs' data --
# never SHREE's books -- and are not looked into.
SYSTEM_DIRS = {'windows', 'windows.old', 'programdata', 'perflogs',
               'recovery', 'system volume information', '$recycle.bin',
               'msocache', 'boot', 'efi', 'config.msi', 'onedrivetemp',
               'intel', 'amd', 'nvidia', 'drivers', 'appdata'}
SHREE_NAME = re.compile(r'(?i)shree')
UNC_RE = re.compile(r'\\\\([A-Za-z0-9][A-Za-z0-9._-]{0,62})\\([^\\/:*?"<>|\r\n]+)'
                    r'((?:\\[^\\/:*?"<>|\r\n]+)*)')
_looked = []                    # where the last look went, for the screen
_refound = []                   # what the last refind put right, to be said
_servers = [0.0, None]          # when, the other computers last named


def _names(path, wait=NET_WAIT):
    """The names in one folder, or [] if it does not answer in time."""
    return within(wait, lambda: sorted(os.listdir(path), key=str.lower),
                  default=[]) or []


def _unc_of(path):
    """F:\\SHREE as \\\\ACCOUNTS\\F\\SHREE, where F: is another computer's drive.

    A drive letter mapped to another computer is put back by Windows only
    once someone opens it after signing in, and Billing starts before that:
    named by its network name, the folder is there from the start. Anything
    else is answered as it came.
    """
    if os.name != 'nt' or not re.match(r'^[A-Za-z]:', str(path or '')):
        return path
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        rc = ctypes.windll.mpr.WNetGetConnectionW(
            ctypes.c_wchar_p(path[:2]), buf, ctypes.byref(size))
        # 1201: a remembered drive not connected yet -- its name still comes.
        if rc not in (0, 1201) or not buf.value.startswith('\\\\'):
            return path
        return buf.value.rstrip('\\') + path[2:]
    except Exception:                              # noqa: BLE001
        return path


def _place_key(path):
    """One key for one folder, however it was reached."""
    return os.path.normcase(os.path.realpath(_unc_of(path)))


def _unc_servers(texts):
    """The computers named in `\\\\COMPUTER\\share...` strings, in order."""
    out = []
    for text in texts:
        for m in UNC_RE.finditer(str(text or '')):
            name = m.group(1).upper()
            if name not in out:
                out.append(name)
    return out


def _registry_texts():
    """Network folders Windows remembers for whoever is signed in: mapped
    drives (connected or not), and what was typed into Map network drive,
    the Explorer address bar and Run."""
    if os.name != 'nt':
        return []
    try:
        import winreg
    except ImportError:
        return []
    out = []

    def values(key):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                i = 0
                while True:
                    try:
                        _n, v, _t = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    if isinstance(v, str):
                        out.append(v)
                    i += 1
        except OSError:
            pass

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Network') as net:
            i = 0
            while True:
                try:
                    letter = winreg.EnumKey(net, i)
                except OSError:
                    break
                values('Network\\' + letter)
                i += 1
    except OSError:
        pass
    ex = 'Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\'
    for key in ('Map Network Drive MRU', 'TypedPaths', 'RunMRU'):
        values(ex + key)
    return [v for v in out if v.startswith('\\\\') or '\\\\' in v]


def _lan_computers():
    """The computers this one sees on the network, as Explorer's Network
    lists them. Often empty on a newer Windows; the folders Windows
    remembers are what finds the other computer then."""
    if os.name != 'nt':
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class SERVER_INFO_100(ctypes.Structure):
            _fields_ = [('platform_id', wintypes.DWORD),
                        ('name', wintypes.LPWSTR)]
        netapi = ctypes.windll.netapi32
        buf = ctypes.POINTER(SERVER_INFO_100)()
        read, total = wintypes.DWORD(), wintypes.DWORD()
        rc = netapi.NetServerEnum(None, 100, ctypes.byref(buf),
                                  wintypes.DWORD(-1), ctypes.byref(read),
                                  ctypes.byref(total), wintypes.DWORD(0x3),
                                  None, None)
        if rc not in (0, 234) or not buf:
            return []
        try:
            return [buf[i].name.upper() for i in range(read.value)
                    if buf[i].name]
        finally:
            netapi.NetApiBufferFree(buf)
    except Exception:                              # noqa: BLE001
        return []


def network_computers(extra=(), fresh=False):
    """The other computers SHREE may be on: the ones Windows remembers a
    folder of, any named in the folders already listed, and the ones on the
    network -- this computer left out, it is looked at drive by drive."""
    if not fresh and _servers[1] is not None and \
            time.time() - _servers[0] < LOOK_TTL:
        names = list(_servers[1])
    else:
        names = _unc_servers(_registry_texts())
        for drive, kind in drive_list():
            if kind == 'network':
                names += _unc_servers([_unc_of(drive)])
        names += within(NET_WAIT, _lan_computers, default=[]) or []
        _servers[:] = [time.time(), names]
    names = _unc_servers(extra) + names
    me = (os.environ.get('COMPUTERNAME') or '').upper()
    out = []
    for n in names:
        if n and n != me and n not in out:
            out.append(n)
    return out


def discover(fresh=False, known=(), extra=()):
    """The SHREE installations this computer can see, best first.

    Where it has been found before first, then SHREE's own shortcuts, every
    drive -- SHREE at its top, any folder named like SHREE one or two levels
    down (SHREE 2026, D:\\DATA\\SHREE) -- the Desktop and Documents, and the
    other computers on the network with every folder they share. A folder is
    taken by what is in it, not by its name. The whole look is held to
    DISCOVER_BUDGET, a computer that is switched off to NET_WAIT.
    """
    if not fresh and _found[1] is not None and \
            time.time() - _found[0] < LOOK_TTL:
        return _found[1]
    started = time.time()
    seen, out, looked = set(), [], []

    def left():
        return DISCOVER_BUDGET - (time.time() - started)

    def add(path, how, wait=NET_WAIT):
        if left() <= 0 or not path:
            return
        home = within(max(1, min(wait, left())),
                      lambda: shree.shree_home(path)
                      if os.path.exists(path) else '', default='')
        if not home:
            return
        # Another computer's drive is kept by its network name when that
        # answers as well: the letter may not be there after a restart.
        unc = _unc_of(home)
        if unc != home and within(NET_WAIT, shree.has_years, unc):
            home = unc
        key = _place_key(home)
        if key in seen:
            return
        seen.add(key)
        d = describe(home)
        if d['books']:
            out.append(dict(d, path=home, how=how))

    def inside(folder, how, depth):
        """SHREE-named folders in `folder`, and SHREE in each one below."""
        for name in _names(folder):
            if left() <= 0:
                return
            if name.startswith(('$', '.')) or name.lower() in SYSTEM_DIRS:
                continue
            full = os.path.join(folder, name)
            if SHREE_NAME.search(name):
                if within(2, os.path.isdir, full):
                    add(full, how)
            elif depth > 1 and within(2, os.path.isdir, full):
                add(os.path.join(full, 'SHREE'), how)
                for sub in _names(full, 2):
                    if SHREE_NAME.search(sub):
                        add(os.path.join(full, sub), how)

    for path in _running_places():
        add(path, 'running')
    for path in known or ():
        add(path, 'copy' if _copy_like(path) else 'found here before')
    for path in _shortcut_places():
        add(path, 'SHREE\'s shortcut')
    for drive, kind in drive_list():
        how = 'drive %s' % drive[:2]
        n = len(out)
        add(os.path.join(drive, 'SHREE'), how)
        if kind == 'network':
            # Another computer's drive: SHREE at its top, the share being
            # SHREE's own folder, or a folder named like it at the top.
            add(drive, how)
            inside(drive, how, 1)
        else:
            inside(drive, how, 2)
        looked.append(dict(where=drive[:2], kind=kind, found=len(out) - n))
    if os.name == 'nt':
        home = os.environ.get('USERPROFILE') or ''
        for sub in ('Desktop', os.path.join('OneDrive', 'Desktop'),
                    'Documents', 'Downloads') if home else ():
            inside(os.path.join(home, sub), 'copy', 1)
        for name in network_computers(extra, fresh):
            if left() <= 0:
                looked.append(dict(where='\\\\' + name, kind='network',
                                   found=0, note='not reached in time'))
                continue
            n = len(out)
            shares = within(NET_WAIT, network_shares, '\\\\' + name,
                            default=[]) or []
            for share in shares:
                base = '\\\\%s\\%s' % (name, share)
                add(os.path.join(base, 'SHREE'), 'on %s' % name)
                add(base, 'on %s' % name)
                inside(base, 'on %s' % name, 1)
            looked.append(dict(where='\\\\' + name, kind='network',
                               found=len(out) - n,
                               note=('shares ' + ', '.join(shares[:8]))
                               if shares else 'did not answer'))
    else:
        # A Mac has no drive letters. A Windows computer's drive shared to it
        # is mounted under /Volumes: SHREE at its top, the share being
        # SHREE's own folder, or a folder named like it on it.
        for vol in within(NET_WAIT, _volumes, default=[]):
            how = 'drive %s' % os.path.basename(vol)
            n = len(out)
            add(os.path.join(vol, 'SHREE'), how)
            add(vol, how)
            inside(vol, how, 1)
            looked.append(dict(where=vol, kind='drive', found=len(out) - n))
    # Copies of the two computers' drives, as this app was built from: in
    # the app's own folder, or beside it -- where they were moved to on
    # 17 Sep 2026 -- or in the home folder.
    for place in _copy_places():
        for name in _names(place):
            full = os.path.join(place, name)
            if SHREE_NAME.search(name) and within(2, os.path.isdir, full):
                add(full, 'copy')
    _looked[:] = looked
    _found[:] = [time.time(), out]
    return out


def _volumes():
    """The drives mounted on a Mac, other than its own disk."""
    try:
        names = os.listdir('/Volumes')
    except OSError:
        return []
    out = []
    for name in sorted(names):
        full = os.path.join('/Volumes', name)
        if name.startswith('.') or os.path.realpath(full) == '/':
            continue
        if os.path.isdir(full):
            out.append(full)
    return out


def _copy_places():
    """Folders that may hold copies of SHREE's drives, nearest first.

    Only folders this app can already read are asked about: on a Mac,
    listing Documents or Downloads from a process that has not been given
    them brings up a permission box.
    """
    out = []
    for place in (ROOT, os.path.dirname(ROOT), os.path.expanduser('~')):
        if place and not any(_same(place, p) for p in out):
            out.append(place)
    return out


def _copy_like(path):
    """True for a folder where copies of SHREE's drives are kept rather than
    SHREE itself: named like SHREE directly inside this app's folder, the one
    it sits in, or the home folder -- or anywhere in a Windows user's Desktop,
    Documents or Downloads. The top of a drive is never such a place: with
    the app in C:\\Billing, C:\\SHREE is SHREE itself."""
    p = os.path.normcase(os.path.abspath(path))
    places = [x for x in _copy_places() if os.path.dirname(x) != x]
    for place in places:
        base = os.path.normcase(os.path.abspath(place)) + os.sep
        if p.startswith(base) and SHREE_NAME.search(p[len(base):].split(os.sep)[0]):
            return True
    if os.name == 'nt':
        home = os.environ.get('USERPROFILE') or ''
        for sub in ('Desktop', 'Documents', 'Downloads'):
            base = os.path.normcase(os.path.join(home, sub)) + os.sep
            if home and p.startswith(base):
                return True
    return False


def from_elsewhere(path):
    """A folder written the other kind of computer's way, which this one
    cannot reach: the Mac's /Users/... on a Windows PC, or F:\\SHREE and
    \\\\ACCOUNTS\\F on a Mac.

    The books are carried from one computer to the other with their settings
    in them (README: "Moving it to a Windows PC"). A folder somebody chose on
    the Mac is still a folder on the Mac, and no choice made there can be
    honoured here. On Windows /Users/... is also a folder on the current
    drive, so it only counts as elsewhere when it is not there.
    """
    p = str(path or '')
    if os.name == 'nt':
        other = p.startswith('/') and not p.startswith('//')
    else:
        other = bool(re.match(r'[A-Za-z]:[\\/]', p)) or p.startswith('\\\\')
    return other and not within(NET_WAIT, shree.shree_home, p)


def _current(book, newest):
    """A book found somewhere is not behind these books: its newest entry,
    or the day SHREE last wrote it, is no earlier than they run to."""
    have = newest.get(book['firm'], '')
    return not have or (book.get('upto') or '') >= have or \
        (book.get('saved') or '') >= have


def refind(conn, fresh=True):
    """Put right a list of SHREE's folders that no longer reads every firm.

    A folder written the other kind of computer's way goes (`from_elsewhere`).
    A folder on this computer that has gone -- moved, renamed, the drive
    letter changed -- is replaced by SHREE where it is now, chosen or not;
    one on another computer is kept while that computer is off. A firm no
    folder on the list holds is looked for. Only SHREE at least as current as
    these books is taken without asking, so an old copy never slips in in
    its place. Answers the new list, or None if nothing changed; what was
    done is left in `_refound` for the update to say.
    """
    st = settings(conn)
    saved = list(st.get('sources') or [])
    elsewhere = [x for x in saved if from_elsewhere(x)]
    rest, renamed = [], []
    for x in saved:
        if x in elsewhere:
            continue
        # Another computer's drive letter, not back yet after a restart, is
        # read by the network name it stands for.
        unc = _unc_of(x)
        if unc != x and not within(NET_WAIT, shree.has_years, x) and \
                within(NET_WAIT, shree.has_years, unc):
            renamed.append((x, unc))
            x = unc
        if not any(_place_key(x) == _place_key(k) for k in rest):
            rest.append(x)
    looks = {x: describe(x) for x in rest}
    have = coverage([looks[x] for x in rest if looks[x]['found']])
    gone = [x for x in rest if not looks[x]['found'] and not is_network(x)]
    _refound[:] = []
    if saved and not elsewhere and not gone and not renamed and \
            len(have) == len(FIRMS):
        return None
    newest = books_newest(conn)
    add, behind = [], []
    for c in chosen(discover(fresh, known=st.get('known'), extra=saved)):
        if any(_place_key(c['path']) == _place_key(k) for k in rest + add):
            continue
        useful = [b for b in c['books']
                  if b['firm'] not in have and _current(b, newest)]
        # Found, but behind these books: said, not taken. It is somebody's
        # to choose, knowing it stops where it does.
        behind += [(b, c['path']) for b in c['books']
                   if b['firm'] not in have and not _current(b, newest)]
        if useful:
            add.append(c['path'])
            for b in useful:
                have[b['firm']] = dict(b, path=c['path'])
    # A folder that has gone is let go once every firm is read from
    # somewhere again. Until then it stays on the list: SHREE run from a USB
    # drive comes back when the drive is plugged in.
    drop = gone if len(have) == len(FIRMS) else []
    late = ['%s\'s %s in %s stops at %s, before these books (%s), so it was '
            'not used. If it is SHREE as it is now, choose it under "Where '
            'SHREE is".' % (b['firm'], 'accounts book' if b['kind'] ==
                            'accounts' else 'bill register', path,
                            _ddmmyyyy(b.get('upto')) or 'nothing',
                            _ddmmyyyy(newest.get(b['firm'])))
            for b, path in behind if b['firm'] not in have]
    if not add and not elsewhere and not drop and not renamed:
        _refound[:] = late
        return None
    kept = [x for x in rest if x not in drop]
    new = kept + add
    notes = []
    for path in add:
        firms = sorted({f for f, b in have.items() if _same(b['path'], path)})
        notes.append('SHREE\'s books for %s are now read from %s%s.' % (
            ', '.join(firms), path,
            ' -- %s is not there any more' % ', '.join(drop) if drop else ''))
    for path in elsewhere:
        notes.append('%s was a folder on the other kind of computer and is '
                     'no longer read.' % path)
    for was, unc in renamed:
        notes.append('%s is read as %s -- its network name, there before the '
                     'drive letter comes back after a restart.' % (was, unc))
    # What somebody chose here is still theirs: the list stays "chosen"
    # unless none of it is left.
    save_settings(conn, dict(sources=new, found=bool(st.get('found'))
                             or not kept))
    remember(conn, add)
    _refound[:] = notes + late
    return new


def _norm_path(p):
    return os.path.normcase(os.path.abspath(p))


def unreadable(path):
    """Why an update cannot read one of its folders, in words that point at
    the fix."""
    if is_network(path):
        return ('SHREE\'s books cannot be read in %s -- is that computer '
                'switched on, and its drive shared?' % path)
    if not os.path.exists(path):
        return ('%s is not there any more. If SHREE\'s folder was moved or '
                'renamed, choose it again under "Where SHREE is" -- Change '
                'or Find SHREE.' % path)
    return ('No SHREE books could be read in %s. Choose the SHREE folder -- '
            'the one holding the year folders (25-26, 26-27 ...) -- under '
            '"Where SHREE is".' % path)


def chosen(found):
    """The found folders an update would read: SHREE itself where it can be
    seen, and a copy only for a firm SHREE itself was found for nowhere --
    firm by firm, so the accounts computer switched off does not leave its
    firm out when a copy of its drive is here."""
    out = [c for c in found if c['how'] != 'copy']
    have = {b['firm'] for c in out for b in c['books']}
    for c in found:
        if c['how'] == 'copy' and any(b['firm'] not in have
                                      for b in c['books']):
            out.append(c)
            have |= {b['firm'] for b in c['books']}
    return out


def sources_of(conn, fresh=False):
    """The folders to read, and whether they were found rather than named.

    Found folders are written down the first time, so an update does not go
    looking on every run and the screen shows what will be read.
    """
    st = settings(conn)
    if st['sources']:
        return st['sources']
    picked = [c['path'] for c in chosen(discover(fresh, known=st['known']))]
    if picked:
        save_settings(conn, dict(sources=picked, found=True))
    return picked


def coverage(folders):
    """Which folder each of this app's firms comes from."""
    out = {}
    for f in folders:
        for b in f['books']:
            have = out.get(b['firm'])
            if have is None or (b['fy'], b['upto']) > (have['fy'], have['upto']):
                out[b['firm']] = dict(b, path=f['path'])
    return out


def browse(path):
    """The folders inside one folder, for choosing SHREE's."""
    if not path:
        drives = _drives()
        if drives:
            # The other computers too, so the accounts computer's drive is a
            # click away rather than a name to be typed.
            return dict(path='', parent=None, home='', entries=[
                dict(name=d, path=d, shree=False) for d in drives] + [
                dict(name='\\\\' + n, path='\\\\' + n, shree=False,
                     computer=True) for n in network_computers()])
        path = ROOT
    path = path.strip().strip('"').rstrip('\\/') if path.strip().startswith(
        ('\\\\', '//')) else os.path.abspath(path.strip().strip('"'))
    server = re.match(r'^[\\/]{2}([^\\/]+)$', path)
    if server:
        # A computer on its own: offer the folders it shares.
        name = server.group(1)
        shares = within(NET_WAIT * 2, network_shares, '\\\\' + name,
                        default=[])
        if not shares:
            raise api.ApiError(
                'No shared folders could be read on %s. Is the computer '
                'switched on, and is its SHREE drive shared?' % name)
        return dict(path='\\\\' + name, parent='', home='', entries=[
            dict(name=sh, path='\\\\%s\\%s' % (name, sh), shree=False)
            for sh in shares])
    path = os.path.abspath(path)
    if not is_network(path):
        # A folder that has been moved or renamed opens where it used to be,
        # at the nearest folder still there -- Change on a folder that had
        # gone used to open on an error and an empty list.
        while not os.path.exists(path) and os.path.dirname(path) != path:
            path = os.path.dirname(path)
    if os.path.isfile(path):
        path = os.path.dirname(path)
    names = within(NET_WAIT * 2 if is_network(path) else 60,
                   lambda: sorted(os.listdir(path), key=lambda n: n.lower()))
    if names is None:
        raise api.ApiError(
            'This folder cannot be opened: %s. A folder on another computer is '
            'named like \\\\COMPUTER\\F -- the computer and its shared folder '
            '-- and the computer has to be switched on.' % path)
    entries = []
    for name in names:
        full = os.path.join(path, name)
        if name.startswith(('$', '.')) or name == 'System Volume Information':
            continue
        try:
            if not os.path.isdir(full):
                continue
        except OSError:
            continue
        # A copy of a SHREE drive (SHREE BILL, holding SHREE) is marked as
        # well as SHREE's own folder.
        entries.append(dict(name=name, path=full,
                            shree=name.upper().startswith('SHREE')
                            and bool(shree.shree_home(full))))
    parent = os.path.dirname(path)
    if parent == path:
        # The top of a drive goes up to the list of drives; the top of a share
        # to the list of that computer's shares.
        unc = re.match(r'^\\\\([^\\]+)\\[^\\]+\\?$', path)
        parent = ('\\\\' + unc.group(1)) if unc else \
            ('' if os.name == 'nt' else None)
    return dict(path=path, parent=parent, home=shree.shree_home(path),
                entries=entries[:1000])


def fingerprint(sources):
    """What SHREE's files look like: every book and backup, by size and time.

    About a second for the two computers' folders. Written down with each
    update (its report, and `shree_last_print`), so what an update read can
    be told apart from what the next one reads.
    """
    h = hashlib.sha1()
    count = 0
    for root in sources:
        home, places = shree.source_places(root)
        if not os.path.isdir(home):
            h.update(('missing:%s' % root).encode('utf-8'))
            continue
        for place in places:
            if os.path.isfile(place):
                try:
                    st = os.stat(place)
                except OSError:
                    continue
                files = [(place, st.st_size, st.st_mtime_ns)]
            else:
                # The sizes and times the folder listings carry: over a
                # network share, thousands of files cost a few listings
                # rather than a question each.
                files = shree.walk_files(place)
            for path, size, mtime in files:
                if not path.lower().endswith(SHREE_FILES):
                    continue
                h.update(('%s|%d|%d\n' % (path, size, mtime)
                          ).encode('utf-8', 'replace'))
                count += 1
    return '%s:%d' % (h.hexdigest()[:16], count)


# --------------------------------------------------------------- rules

def seed_rules(conn):
    """Write down what was decided before decisions were written down.

    Only the decisions Fetch everything keeps. Since 6 Oct 2026 the books
    are to match SHREE's figures, and Fetch everything lets the rest go --
    the CO-OP banks deleted with their entries among them. Books started
    from nothing (a new PC, an emptied folder) were still given those, so
    their first update deleted the CO-OP banks, left 201 BSTP entries with
    one side, and was refused for it: the accounts book never came in. They
    now start where Fetch everything ends."""
    if db.get_meta(conn, SEED_KEY):
        return
    for kind, firm, name, value, note in DECIDED:
        if kind in AWAY_FROM_SHREE:
            continue
        conn.execute('INSERT INTO sync_rule(kind, firm, name, value, note) '
                     'VALUES (?,?,?,?,?)', (kind, firm, name, value, note))
    for kind, firm, name, value, note in DECIDED_IF_ABSENT:
        cid = _cid(conn, firm)
        if kind in AWAY_FROM_SHREE:
            continue
        if cid and not _find_ledger(conn, cid, name):
            conn.execute('INSERT INTO sync_rule(kind, firm, name, value, note) '
                         'VALUES (?,?,?,?,?)', (kind, firm, name, value, note))
    db.set_meta(conn, SEED_KEY, now())
    conn.commit()


def list_rules(conn):
    out = []
    for r in conn.execute('SELECT * FROM sync_rule ORDER BY id'):
        d = dict(r)
        d['label'] = RULE_LABEL.get(d['kind'], d['kind'])
        d['what'] = _describe(d)
        out.append(d)
    return out


def _describe(rule):
    kind, name, value = rule['kind'], rule['name'], rule['value']
    if kind in ('delete_doc', 'supersede_doc'):
        parts = name.split('|')
        try:
            fp = json.loads(value or '{}')
        except ValueError:
            fp = {}
        number = ' '.join(x for x in parts[2:4] if x)
        return '%s %s (%s) -- %s, %s, %s' % (
            api.DOC_LABEL.get(parts[0], parts[0].title()), number, parts[1],
            fp.get('party', ''), fp.get('date', ''), fp.get('total', ''))
    if kind in ('fold', 'rename_ledger', 'merge_ledger', 'fold_item'):
        return '%s  ->  %s' % (name, value)
    if kind == 'move_group':
        return '%s  (group %s)' % (name, value)
    if kind in ('billing_firm', 'moved_ledger'):
        return '%s  ->  %s' % (name, value)
    if kind == 'drop_years':
        try:
            last = api._day_before(name)
        except ValueError:
            last = name
        return 'every entry up to %s, balances carried forward' % (
            '-'.join(reversed(last.split('-'))))
    return name


def release_rules(conn, data):
    """Stop keeping every decision of one kind -- most often the documents
    deleted here, which the next update then brings back from SHREE."""
    kind = str((data or {}).get('kind') or '')
    if kind not in RULE_LABEL:
        raise api.ApiError('Unknown kind of decision: %s.' % kind)
    firm = str((data or {}).get('firm') or '')
    sql, args = 'DELETE FROM sync_rule WHERE kind=?', [kind]
    if firm:
        sql += ' AND firm=?'
        args.append(firm)
    n = conn.execute(sql, args).rowcount
    conn.commit()
    return dict(ok=True, released=n)


def delete_rule(conn, rid):
    row = conn.execute('SELECT id FROM sync_rule WHERE id=?', (rid,)).fetchone()
    if not row:
        raise api.ApiError('That decision is not on the list.', 404)
    conn.execute('DELETE FROM sync_rule WHERE id=?', (rid,))
    conn.commit()
    return dict(ok=True)


def _cid(conn, firm):
    row = conn.execute('SELECT id FROM company WHERE code=?', (firm,)).fetchone()
    return row['id'] if row else None


def _find_ledger(conn, cid, name):
    want = norm(name)
    for r in conn.execute('SELECT * FROM ledger WHERE company_id=? AND '
                          'upper(trim(name))=?', (cid, want)):
        return dict(r)
    for r in conn.execute('SELECT * FROM ledger WHERE company_id=?', (cid,)):
        if norm(r['name']) == want:
            return dict(r)
    return None


def _repoint(conn, old, new):
    """Everything that names account `old` names `new` instead."""
    for sql in ('UPDATE posting SET ledger_id=? WHERE ledger_id=?',
                'UPDATE voucher_line SET ledger_id=? WHERE ledger_id=?',
                'UPDATE voucher SET bank_id=? WHERE bank_id=?',
                'UPDATE voucher SET party_id=? WHERE party_id=?',
                'UPDATE invoice SET party_id=? WHERE party_id=?',
                'UPDATE invoice SET sales_ac_id=? WHERE sales_ac_id=?',
                'UPDATE sales_order SET party_id=? WHERE party_id=?',
                'UPDATE ledger SET linked_ledger=? WHERE linked_ledger=?'):
        conn.execute(sql, (new, old))


def _referenced(conn, lid):
    return conn.execute(
        'SELECT (SELECT COUNT(*) FROM posting WHERE ledger_id=?) '
        '     + (SELECT COUNT(*) FROM voucher_line WHERE ledger_id=?) '
        '     + (SELECT COUNT(*) FROM invoice WHERE party_id=? OR sales_ac_id=?) '
        '     + (SELECT COUNT(*) FROM sales_order WHERE party_id=?)',
        (lid, lid, lid, lid, lid)).fetchone()[0]


def _rule_fold(conn, rule):
    """An old account's history onto the account it was renamed to.

    Its entries move across, the successor's first "carried forward" line
    after the old account stops is reduced by what the old account came to --
    that line was standing in for it -- and the same amount goes onto
    Difference in Opening Balances, so neither today's balance nor the trial
    balance moves. This is exactly what was done by hand on 15 Sep 2026.
    """
    cid = _cid(conn, rule['firm'])
    old = _find_ledger(conn, cid, rule['name']) if cid else None
    if not old:
        return 'skipped', 'not in the books'
    new = _find_ledger(conn, cid, rule['value'])
    if not new:
        return 'warn', 'the account it folds into, %s, is not there' % rule['value']
    if old['id'] == new['id']:
        return 'skipped', 'same account'
    bal = float(old['opening_bal'] or 0) + float(conn.execute(
        'SELECT COALESCE(SUM(debit-credit),0) FROM posting WHERE ledger_id=?',
        (old['id'],)).fetchone()[0] or 0)
    last = conn.execute(
        "SELECT MAX(date) FROM posting WHERE ledger_id=? AND doc_kind<>'OPENING'",
        (old['id'],)).fetchone()[0] or ''
    line = conn.execute(
        "SELECT * FROM posting WHERE ledger_id=? AND doc_kind='OPENING' "
        'AND date>? ORDER BY date, id LIMIT 1', (new['id'], last)).fetchone()
    diff = _find_ledger(conn, cid, api.OPENING_DIFF_LEDGER)
    _repoint(conn, old['id'], new['id'])
    note = ''
    if abs(bal) > 0.005:
        if line and diff:
            net = api.r2(line['debit'] - line['credit'] - bal)
            if abs(net) < 0.005:
                conn.execute('DELETE FROM posting WHERE id=?', (line['id'],))
            else:
                conn.execute('UPDATE posting SET debit=?, credit=? WHERE id=?',
                             (max(net, 0.0), max(-net, 0.0), line['id']))
            conn.execute(
                'INSERT INTO posting(company_id, date, ledger_id, doc_kind, '
                'doc_id, doc_no, debit, credit, narration) '
                "VALUES (?,?,?,'OPENING',0,?,?,?,?)",
                (cid, line['date'], diff['id'], line['doc_no'],
                 max(bal, 0.0), max(-bal, 0.0),
                 'Renamed account folded: %s -> %s'
                 % (old['name'].strip(), new['name'].strip())))
        else:
            note = ('; no carried-forward line to take its %.2f, so %s moved '
                    'by it' % (bal, new['name']))
    conn.execute('UPDATE ledger SET opening_bal=opening_bal+? WHERE id=?',
                 (float(old['opening_bal'] or 0), new['id']))
    conn.execute('DELETE FROM ledger WHERE id=?', (old['id'],))
    # Since 6 Oct 2026 the import closes the old name on the day SHREE
    # stopped carrying it, beside the new name's carried line: one a year.
    api._tidy_carried(conn, new['id'])
    return ('warn' if note else 'done'), 'folded%s' % note


def _rule_purge(conn, rule):
    """An account deleted together with its entries -- their other halves
    stay, which is the difference BST's trial balance carries by decision."""
    cid = _cid(conn, rule['firm'])
    led = _find_ledger(conn, cid, rule['name']) if cid else None
    if not led:
        return 'skipped', 'not in the books'
    used = conn.execute(
        'SELECT (SELECT COUNT(*) FROM invoice WHERE party_id=? OR sales_ac_id=?) '
        '     + (SELECT COUNT(*) FROM sales_order WHERE party_id=?) '
        '     + (SELECT COUNT(*) FROM ledger WHERE linked_ledger=?)',
        (led['id'],) * 4).fetchone()[0]
    if used:
        return 'warn', 'a bill, an order or another account names it -- kept'
    n = conn.execute('DELETE FROM posting WHERE ledger_id=?', (led['id'],)).rowcount
    conn.execute('DELETE FROM voucher_line WHERE ledger_id=?', (led['id'],))
    conn.execute('UPDATE voucher SET bank_id=NULL WHERE bank_id=?', (led['id'],))
    conn.execute('UPDATE voucher SET party_id=NULL WHERE party_id=?', (led['id'],))
    conn.execute('DELETE FROM ledger WHERE id=?', (led['id'],))
    return 'done', 'deleted with %d entries' % n


def _rule_move_group(conn, rule):
    cid = _cid(conn, rule['firm'])
    led = _find_ledger(conn, cid, rule['name']) if cid else None
    if not led:
        return 'skipped', 'not in the books'
    grp = conn.execute('SELECT id FROM account_group WHERE company_id=? AND '
                       'code=?', (cid, rule['value'])).fetchone()
    if not grp:
        return 'warn', 'group %s is not in this firm' % rule['value']
    # A group chosen here is the account's group in every year.
    conn.execute('DELETE FROM ledger_group_year WHERE ledger_id=?', (led['id'],))
    if led['group_id'] == grp['id']:
        return 'skipped', 'already there'
    conn.execute('UPDATE ledger SET group_id=? WHERE id=?', (grp['id'], led['id']))
    return 'done', 'moved'


def _rule_rename(conn, rule):
    """SHREE still calls the account by its old name; fold that into the
    account as it was renamed here."""
    cid = _cid(conn, rule['firm'])
    old = _find_ledger(conn, cid, rule['name']) if cid else None
    if not old:
        return 'skipped', 'not in the books'
    new = _find_ledger(conn, cid, rule['value'])
    if not new:
        conn.execute("UPDATE ledger SET name=?, source='' WHERE id=?",
                     (rule['value'], old['id']))
        return 'done', 'renamed'
    if new['id'] == old['id']:
        return 'skipped', 'same account'
    _repoint(conn, old['id'], new['id'])
    conn.execute('UPDATE ledger SET opening_bal=opening_bal+? WHERE id=?',
                 (float(old['opening_bal'] or 0), new['id']))
    conn.execute('DELETE FROM ledger WHERE id=?', (old['id'],))
    return 'done', 'joined to the renamed account'


def _named(conn, cid, name):
    """The account of exactly this name, else of this name read as meant --
    two spellings that differ only in their spaces can be the two merged."""
    for r in conn.execute('SELECT * FROM ledger WHERE company_id=? AND '
                          'trim(name)=?', (cid, (name or '').strip())):
        return dict(r)
    return _find_ledger(conn, cid, name)


def _rule_merge(conn, rule):
    """An account merged into another here. SHREE brings it back under its
    own name with its entries, and they go onto the kept account again."""
    cid = _cid(conn, rule['firm'])
    old = _named(conn, cid, rule['name']) if cid else None
    if not old:
        return 'skipped', 'not in the books'
    new = _named(conn, cid, rule['value'])
    if new and new['id'] == old['id']:
        return 'skipped', 'same account'
    if not new:
        api.rename_for_merge(conn, old, rule['value'])
        return 'done', 'renamed'
    api.merge_ledger_into(conn, old, new)
    return 'done', 'merged'


def _rule_moved(conn, rule):
    cid = _cid(conn, rule['firm'])
    led = _find_ledger(conn, cid, rule['name']) if cid else None
    if not led:
        return 'skipped', 'not in the books'
    if _referenced(conn, led['id']):
        return 'warn', 'SHREE has entries for it in %s again -- kept' % rule['firm']
    conn.execute('UPDATE ledger SET linked_ledger=NULL WHERE linked_ledger=?',
                 (led['id'],))
    conn.execute('DELETE FROM ledger WHERE id=?', (led['id'],))
    return 'done', 'left in %s' % rule['value']


def _rule_billing(conn, rule):
    cid = _cid(conn, rule['firm'])
    led = _find_ledger(conn, cid, rule['name']) if cid else None
    target = _cid(conn, rule['value'])
    if not led:
        return 'skipped', 'not in the books'
    if not target:
        return 'warn', 'firm %s is not in the app' % rule['value']
    if led['home_company'] == target:
        return 'skipped', 'already so'
    api.set_billing_firm(conn, led['id'], target)
    return 'done', 'billed by %s' % rule['value']


def _rule_delete_account(conn, rule):
    cid = _cid(conn, rule['firm'])
    led = _find_ledger(conn, cid, rule['name']) if cid else None
    if not led:
        return 'skipped', 'not in the books'
    try:
        # An account that is not a party, deleted here with its entries,
        # has them deleted again; any other is only ever deleted alone.
        got = api.delete_account(conn, led['id'], cascade=True, backup=False,
                                 entries=rule['value'] == 'entries')
    except api.ApiError as e:
        conn.rollback()
        return 'warn', 'could not be deleted again: %s' % e.message
    return 'done', 'deleted with %d document(s)' % got['documents']


def _find_item(conn, cid, name):
    want = norm(name)
    for r in conn.execute('SELECT * FROM item WHERE company_id=?', (cid,)):
        if norm(r['name']) == want:
            return dict(r)
    return None


def _rule_fold_item(conn, rule):
    """An item deleted here with its lines moved onto another: SHREE's bills
    come back naming it, and are moved across again, stock with them."""
    cid = _cid(conn, rule['firm'])
    old = _find_item(conn, cid, rule['name']) if cid else None
    if not old:
        return 'skipped', 'not in the books'
    new = _find_item(conn, cid, rule['value'])
    if not new:
        return 'warn', 'the item it moves into, %s, is not there' % rule['value']
    if new['id'] == old['id']:
        return 'skipped', 'same item'
    for table in ('invoice_item', 'sales_order_item', 'stock_entry_line'):
        conn.execute('UPDATE %s SET item_id=? WHERE item_id=?' % table,
                     (new['id'], old['id']))
    conn.execute('UPDATE item SET opening_qty=ROUND(opening_qty+?, 3) '
                 'WHERE id=?', (float(old['opening_qty'] or 0), new['id']))
    # The one that stays keeps its Billing Stock category, or takes the
    # folded one's if it had none.
    conn.execute("UPDATE item SET stock_cat=? WHERE id=? AND stock_cat=''",
                 (old.get('stock_cat') or '', new['id']))
    conn.execute('DELETE FROM item WHERE id=?', (old['id'],))
    return 'done', 'moved into %s' % new['name']


def _rule_delete_item(conn, rule):
    """An item deleted here: SHREE's lines naming it keep their figures,
    without an item."""
    cid = _cid(conn, rule['firm'])
    old = _find_item(conn, cid, rule['name']) if cid else None
    if not old:
        return 'skipped', 'not in the books'
    for table in ('invoice_item', 'sales_order_item'):
        conn.execute('UPDATE %s SET item_id=NULL WHERE item_id=?' % table,
                     (old['id'],))
    # Its stock added or removed by hand is its own and goes with it.
    conn.execute('DELETE FROM stock_entry_line WHERE item_id=?', (old['id'],))
    conn.execute('DELETE FROM stock_entry WHERE id NOT IN '
                 '(SELECT entry_id FROM stock_entry_line)')
    conn.execute('DELETE FROM item WHERE id=?', (old['id'],))
    return 'done', 'deleted'


def _matches(conn, table, row, fp):
    if table == 'invoice':
        party = conn.execute('SELECT name FROM ledger WHERE id=?',
                             (row['party_id'],)).fetchone()
        date, total = row['invoice_date'], row['grand_total']
    else:
        party = conn.execute('SELECT name FROM ledger WHERE id=?',
                             (row['party_id'],)).fetchone() \
            if row['party_id'] else None
        date, total = row['voucher_date'], row['amount']
    return (norm(party['name'] if party else '') == norm(fp.get('party'))
            and date == fp.get('date')
            and abs(float(total or 0) - float(fp.get('total') or 0)) < 0.01)


def _rule_doc(conn, rule):
    """Take SHREE's copy of a document away again -- deleted here, or edited
    here and kept as this app's own.

    Found by its paper number, then checked against what it said when it was
    deleted or edited: a document SHREE has changed since is a different
    document now, and is left for a person to look at.
    """
    cid = _cid(conn, rule['firm'])
    if not cid:
        return 'skipped', 'firm not in the app'
    kind, fy, series, number = (rule['name'].split('|') + ['', '', '', ''])[:4]
    try:
        fp = json.loads(rule['value'] or '{}')
    except ValueError:
        fp = {}
    renumbered = re.escape(number) + r'\(\d+\)'
    if kind in INVOICE_KINDS:
        table = 'invoice'
        found = [dict(r) for r in conn.execute(
            "SELECT * FROM invoice WHERE company_id=? AND source='SHREE' AND "
            "doc_type=? AND fy=? AND COALESCE(series,'')=? AND "
            '(invoice_no=? OR invoice_no LIKE ?)',
            (cid, kind, fy, series, number, number + '(%'))]
        found = [r for r in found if r['invoice_no'] == number
                 or re.fullmatch(renumbered, r['invoice_no'])]
    else:
        table = 'voucher'
        found = [dict(r) for r in conn.execute(
            "SELECT * FROM voucher WHERE company_id=? AND source='SHREE' AND "
            'kind=? AND fy=? AND (voucher_no=? OR voucher_no LIKE ?)',
            (cid, kind, fy, number, number + '(%'))]
        found = [r for r in found if r['voucher_no'] == number
                 or re.fullmatch(renumbered, r['voucher_no'])]
    if not found:
        return 'skipped', 'not in SHREE any more'
    same = [r for r in found if _matches(conn, table, r, fp)]
    if not same:
        return 'warn', ('SHREE has changed it since -- both kept, look at it'
                        if rule['kind'] == 'supersede_doc'
                        else 'SHREE has changed it since -- kept')
    for r in same:
        if table == 'invoice':
            api.purge_chain(conn, r['id'])
        else:
            conn.execute('UPDATE invoice SET gst_voucher=NULL WHERE gst_voucher=?',
                         (r['id'],))
            api._drop_voucher(conn, r['id'])
    return 'done', 'taken away'


def _rule_drop_years(conn, rule):
    """The old years an update has just read back in from SHREE go again,
    their balances carried forward as when they were first deleted."""
    cid = _cid(conn, rule['firm'])
    if not cid:
        return 'skipped', 'no such firm'
    try:
        extra = json.loads(rule['value'] or '{}')
    except ValueError:
        extra = {}
    got = api._drop_before(conn, cid, rule['name'], extra=extra)
    if not (got['documents'] or got['entries']):
        return 'skipped', 'nothing before %s' % rule['name']
    return 'done', '%d documents and %d entries taken out again' % (
        got['documents'], got['entries'])


HANDLERS = {
    'drop_years': _rule_drop_years,
    'fold': _rule_fold, 'purge_ledger': _rule_purge,
    'move_group': _rule_move_group, 'rename_ledger': _rule_rename,
    'merge_ledger': _rule_merge,
    'moved_ledger': _rule_moved, 'billing_firm': _rule_billing,
    'delete_account': _rule_delete_account,
    'fold_item': _rule_fold_item, 'delete_item': _rule_delete_item,
    'delete_doc': _rule_doc, 'supersede_doc': _rule_doc,
}


def replay(conn, phase):
    """Play every decision of one phase, oldest first. Returns what happened."""
    api.RECORD_RULES = False
    kinds = REPLAY_PHASES[phase]
    out = []
    # Kind by kind, in the phase's order, and oldest first within a kind. The
    # bank folds were done before any deletion was written down; played after
    # one, a fold would measure an old account a deleted customer's receipt
    # had already left, and carry a different figure forward.
    rules = sorted((dict(r) for r in conn.execute(
        'SELECT * FROM sync_rule WHERE kind IN (%s)'
        % ','.join('?' * len(kinds)), kinds)),
        key=lambda r: (kinds.index(r['kind']), r['id']))
    for rule in rules:
        try:
            before = _shree_moves(conn) if rule['kind'] in KEEP_STOCK else None
            status, note = HANDLERS[rule['kind']](conn, rule)
            if before is not None:
                _keep_stock(conn, before)
            conn.commit()
        except Exception as e:                     # noqa: BLE001
            conn.rollback()
            status, note = 'warn', '%s: %s' % (type(e).__name__, e)
        out.append(dict(id=rule['id'], kind=rule['kind'], firm=rule['firm'],
                        what=_describe(rule), status=status, note=note))
    if phase == 'docs':
        n = drop_shadowed_gst_journals(conn)
        conn.commit()
        if n:
            out.append(dict(id=0, kind='gst_journal', firm='', status='done',
                            what='%d GST journal(s) of SHREE\'s' % n,
                            note='a bill edited here carries its own'))
    return out


# Decisions that take SHREE's documents out of the books. The import set
# each item's opening so stock ends on SHREE's own closing figure, those
# documents included -- the goods did leave. Taken out afterwards, their
# kilos came back into stock: BSTP's CRCA stood 31,313 kg above SHREE's, the
# challans of the four customers deleted on 16 Sep 2026 (2 Oct 2026).
KEEP_STOCK = ('delete_doc', 'delete_account')


def _shree_moves(conn):
    """{item id: what SHREE's documents in the books move it by}."""
    out = defaultdict(float)
    for r in conn.execute(
            'SELECT ii.item_id, i.doc_type, SUM(' + api.STOCK_QTY + ') q '
            'FROM invoice_item ii JOIN invoice i ON i.id=ii.invoice_id '
            "WHERE i.source='SHREE' AND ii.item_id IS NOT NULL "
            'GROUP BY ii.item_id, i.doc_type'):
        out[r['item_id']] += api.DOC_STOCK.get(r['doc_type'], 0) * \
            float(r['q'] or 0)
    return out


def _keep_stock(conn, before):
    """Each item's opening takes up what the documents just taken out
    moved, so its stock stays on SHREE's figure."""
    after = _shree_moves(conn)
    for iid in set(before) | set(after):
        d = before.get(iid, 0.0) - after.get(iid, 0.0)
        if abs(d) > 1e-9:
            conn.execute('UPDATE item SET opening_qty=ROUND(opening_qty+?, 3) '
                         'WHERE id=?', (d, iid))


def drop_shadowed_gst_journals(conn):
    """SHREE's GST journal for a bill this app keeps its own copy of, which
    carries its own journal: taken out, or the customer pays the tax twice.
    BST GT 200, edited here on 2 Oct 2026 before the app took SHREE's
    journal over, had both -- Rs 42,364 of SHREE's and Rs 42,283 of its own."""
    n = 0
    for b in conn.execute(
            "SELECT b.id, b.series, b.invoice_no, b.invoice_date, "
            "b.gst_voucher, c.company_id, c.party_id FROM invoice b JOIN "
            "invoice c ON c.id=b.from_challan WHERE b.source='' AND "
            "b.gst_voucher IS NOT NULL").fetchall():
        series = (b['series'] or '').strip()
        ref = '%s/%s' % (series, b['invoice_no']) if series else b['invoice_no']
        for v in conn.execute(
                "SELECT v.id FROM voucher v WHERE v.company_id=? AND "
                "v.kind='JOURNAL' AND v.source='SHREE' AND v.voucher_date=? "
                "AND (v.ref_no=? OR v.ref_no LIKE ?) AND v.id<>? AND EXISTS "
                "(SELECT 1 FROM voucher_line l WHERE l.voucher_id=v.id AND "
                "l.ledger_id=?)", (b['company_id'], b['invoice_date'], ref,
                                    '% ' + ref, b['gst_voucher'],
                                    b['party_id'])).fetchall():
            api._drop_voucher(conn, v['id'])
            n += 1
    return n


# ------------------------------------------------------ fetch everything
#
# 6 Oct 2026, the user: *"This 'fetch everything' command should delete
# everything from the software and fetch everything from SHREE to the
# software ... basically the software and SHREE figures should match."*
# Until then it only let go of the documents kept deleted and edited here;
# the CO-OP banks, the accounts deleted here and every entry made here
# stayed, and the figures still differed from SHREE's.
#
# Now every entry made in this app goes -- documents, vouchers, orders,
# stock added or taken out by hand -- with every decision that keeps the
# books away from SHREE's figures, groups chosen here among them (a group
# total is a figure too; SHREE's PERSONAL BANK ACCOUNT group still shows on
# Bank & Cash, api.MONEY_SQL). What is kept changes no figure: banks folded
# into their new names and accounts merged here (SHREE shows each as one
# account), names and billing firms chosen here, and the old years deleted
# on Settings (their balances are carried forward). Settings,
# Personal Accounts and the staff list are not the books and stay as they
# are. All of it happens on the working copy, firm by firm for the firms
# SHREE could be read for, and the books are copied first as at every update.

# The decisions that make a figure differ from SHREE's.
AWAY_FROM_SHREE = ('delete_doc', 'supersede_doc', 'purge_ledger',
                   'delete_account', 'delete_item', 'move_group')


def _firm_ids(conn, firms):
    return {c: i for c, i in ((f, _cid(conn, f)) for f in firms) if i}


def _in(ids):
    return ','.join('?' * len(ids))


def _edited_keys(conn, firms):
    return {(r['firm'], r['name']) for r in conn.execute(
        "SELECT firm, name FROM sync_rule WHERE kind='supersede_doc'")
        if r['firm'] in firms}


def _own_rows(conn, firms):
    """This app's own documents and vouchers in `firms`, and the GST journals
    that only go with one of its bills."""
    ids = _firm_ids(conn, firms)
    code = {i: c for c, i in ids.items()}
    if not ids:
        return code, [], []
    cids = list(ids.values())
    docs = [dict(r) for r in conn.execute(
        'SELECT i.id, i.company_id, i.doc_type, i.fy, i.series, i.invoice_no, '
        'i.invoice_date, i.grand_total, i.gst_voucher, l.name AS party '
        'FROM invoice i LEFT JOIN ledger l ON l.id=i.party_id '
        "WHERE i.company_id IN (%s) AND COALESCE(i.source,'')<>'SHREE' "
        'ORDER BY i.invoice_date, i.id' % _in(cids), cids)]
    journals = {d['gst_voucher'] for d in docs if d['gst_voucher']}
    vouchers = [dict(r) for r in conn.execute(
        'SELECT v.id, v.company_id, v.kind, v.fy, v.voucher_no, v.voucher_date, '
        'v.amount, l.name AS party FROM voucher v '
        'LEFT JOIN ledger l ON l.id=v.party_id '
        "WHERE v.company_id IN (%s) AND COALESCE(v.source,'')<>'SHREE' "
        'ORDER BY v.voucher_date, v.id' % _in(cids), cids)
        if r['id'] not in journals]
    return code, docs, vouchers


def everything_plan(conn, firms=FIRMS):
    """What Fetch everything takes away besides SHREE's own rows, for the
    question asked before it runs. Reads; never writes."""
    code, docs, vouchers = _own_rows(conn, firms)
    edited = _edited_keys(conn, firms)
    cids = list(code)
    made, back = [], []
    for d in docs:
        firm = code[d['company_id']]
        key = api.doc_key(d['doc_type'], d['fy'], d['series'], d['invoice_no'])
        row = dict(firm=firm, key=key, date=d['invoice_date'],
                   party=d['party'] or '', amount=api.r2(d['grand_total']))
        (back if (firm, key) in edited else made).append(row)
    for v in vouchers:
        made.append(dict(firm=code[v['company_id']],
                         key=api.doc_key(v['kind'], v['fy'], '', v['voucher_no']),
                         date=v['voucher_date'], party=v['party'] or '',
                         amount=api.r2(v['amount'])))
    made.sort(key=lambda r: (r['date'], r['firm']))

    def count(sql):
        return conn.execute(sql % _in(cids), cids).fetchone()[0] if cids else 0
    orders = count("SELECT COUNT(*) FROM sales_order WHERE company_id IN (%s) "
                   "AND COALESCE(source,'')<>'SHREE'")
    stock = count('SELECT COUNT(*) FROM stock_entry WHERE company_id IN (%s)')
    steel = count('SELECT COUNT(*) FROM steel_entry WHERE company_id IN (%s)')
    drops, kept = [], {}
    for r in list_rules(conn):
        if r['firm'] in firms and r['kind'] in AWAY_FROM_SHREE:
            if r['kind'] != 'supersede_doc':     # said as the documents
                drops.append(dict(firm=r['firm'], kind=r['kind'],
                                  label=r['label'], what=r['what']))
        else:
            kept[r['label']] = kept.get(r['label'], 0) + 1
    return dict(firms=list(firms), made=made, edited=back, orders=orders,
                stock=stock, steel=steel, drops=drops,
                kept=[dict(label=k, count=n) for k, n in sorted(kept.items())],
                # Entries made or changed here go: the delete password is
                # asked for, as for any delete of a document.
                password=bool(made or back or orders or stock or steel))


def _drop_own_masters(conn, cids):
    """Accounts and items made here that nothing names any more. The
    app's own accounts it cannot work without -- system accounts, the one
    that holds opening differences, a staff member's -- stay."""
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS _used(id INTEGER PRIMARY KEY)')
    conn.execute('DELETE FROM _used')
    for sql in ('SELECT party_id FROM invoice', 'SELECT sales_ac_id FROM invoice',
                'SELECT party_id FROM sales_order', 'SELECT party_id FROM voucher',
                'SELECT bank_id FROM voucher', 'SELECT ledger_id FROM voucher_line',
                'SELECT ledger_id FROM posting', 'SELECT linked_ledger FROM ledger'):
        conn.execute('INSERT OR IGNORE INTO _used(id) %s WHERE %s IS NOT NULL'
                     % (sql, sql.split()[1]))
    staff = {(r['company_id'], norm(r['name'])) for r in conn.execute(
        "SELECT company_id, name FROM staff UNION ALL SELECT company_id, "
        "loose_account FROM staff WHERE loose_account<>''")}
    gone = [r['id'] for r in conn.execute(
        "SELECT id, company_id, name FROM ledger WHERE company_id IN (%s) "
        "AND COALESCE(source,'')<>'SHREE' AND system_key IS NULL "
        'AND id NOT IN (SELECT id FROM _used)' % _in(cids), cids)
        if norm(r['name']) != norm(api.OPENING_DIFF_LEDGER)
        and (r['company_id'], norm(r['name'])) not in staff]
    conn.executemany('DELETE FROM ledger WHERE id=?', [(i,) for i in gone])
    conn.execute('DELETE FROM _used')
    for sql in ('SELECT item_id FROM invoice_item',
                'SELECT item_id FROM sales_order_item',
                'SELECT item_id FROM stock_entry_line'):
        conn.execute('INSERT OR IGNORE INTO _used(id) %s WHERE item_id IS NOT '
                     'NULL' % sql)
    items = conn.execute(
        "DELETE FROM item WHERE company_id IN (%s) AND COALESCE(source,'')"
        "<>'SHREE' AND id NOT IN (SELECT id FROM _used)" % _in(cids),
        cids).rowcount
    conn.execute('DROP TABLE IF EXISTS _used')
    return len(gone), items


def wipe_for_everything(conn, firms=FIRMS):
    """Fetch everything, before SHREE is read: this app's own entries in
    `firms` go, and the decisions that keep the books away from SHREE's
    figures, so what the import writes next is the books as SHREE has them.
    Run on the working copy only. Answers what went."""
    api.RECORD_RULES = False           # nothing here is a decision to keep
    ids = _firm_ids(conn, firms)
    out = dict(edited=0, made=0, vouchers=0, orders=0, stock=0, steel=0,
               decisions=0, accounts=0, items=0)
    if not ids:
        return out
    cids = list(ids.values())
    code, docs, vouchers = _own_rows(conn, firms)
    edited = _edited_keys(conn, firms)
    for d in docs:
        # Read again: deleting a bill has already changed its challan.
        r = conn.execute('SELECT * FROM invoice WHERE id=?',
                         (d['id'],)).fetchone()
        if not r:
            continue
        key = api.doc_key(d['doc_type'], d['fy'], d['series'], d['invoice_no'])
        out['edited' if (code[d['company_id']], key) in edited else 'made'] += 1
        api._purge_invoice(conn, dict(r))
    for v in vouchers:
        conn.execute('UPDATE invoice SET gst_voucher=NULL WHERE gst_voucher=?',
                     (v['id'],))
        api._drop_voucher(conn, v['id'])
        out['vouchers'] += 1
    for r in conn.execute("SELECT id FROM sales_order WHERE company_id IN (%s) "
                          "AND COALESCE(source,'')<>'SHREE'" % _in(cids),
                          cids).fetchall():
        conn.execute('UPDATE invoice SET order_id=NULL WHERE order_id=?',
                     (r['id'],))
        conn.execute('DELETE FROM sales_order_item WHERE order_id=?', (r['id'],))
        conn.execute('DELETE FROM sales_order WHERE id=?', (r['id'],))
        out['orders'] += 1
    for table in ('stock_entry', 'steel_entry'):
        conn.execute('DELETE FROM %s_line WHERE entry_id IN (SELECT id FROM %s '
                     'WHERE company_id IN (%s))' % (table, table, _in(cids)),
                     cids)
        out[table.split('_')[0]] = conn.execute(
            'DELETE FROM %s WHERE company_id IN (%s)' % (table, _in(cids)),
            cids).rowcount
    out['decisions'] = conn.execute(
        'DELETE FROM sync_rule WHERE kind IN (%s) AND firm IN (%s)'
        % (_in(AWAY_FROM_SHREE), _in(list(ids))),
        list(AWAY_FROM_SHREE) + list(ids)).rowcount
    out['accounts'], out['items'] = _drop_own_masters(conn, cids)
    conn.commit()
    return out


# --------------------------------------------------------------- links

def _key_of(conn):
    return {r['id']: [r['code'], r['doc_type'], r['fy'], r['series'] or '',
                      r['invoice_no']]
            for r in conn.execute(
                'SELECT i.id, c.code, i.doc_type, i.fy, i.series, i.invoice_no '
                'FROM invoice i JOIN company c ON c.id=i.company_id')}


def save_links(conn):
    """The ties between this app's documents and imported ones, by number.

    The import takes every imported row away and brings it back under a new
    id, letting go of any bill made here from an imported challan -- which
    would then offer "Bill it" a second time.
    """
    key = _key_of(conn)
    src = {r['id']: r['source'] for r in conn.execute(
        'SELECT id, source FROM invoice')}
    code = {r['id']: r['code'] for r in conn.execute('SELECT id, code FROM company')}
    links = []
    for r in conn.execute('SELECT id, source, from_challan, billed_invoice, '
                          'billed_company, order_id FROM invoice'):
        own = r['source'] != 'SHREE'
        if own and r['from_challan'] and src.get(r['from_challan']) == 'SHREE':
            links.append(dict(kind='own_from', own=r['id'],
                              key=key[r['from_challan']]))
        if own and r['billed_invoice'] and src.get(r['billed_invoice']) == 'SHREE':
            links.append(dict(kind='own_billed', own=r['id'],
                              key=key[r['billed_invoice']],
                              firm=code.get(r['billed_company'])))
        if not own and r['billed_invoice'] and src.get(r['billed_invoice']) != 'SHREE':
            links.append(dict(kind='shree_billed', key=key[r['id']],
                              own=r['billed_invoice'],
                              firm=code.get(r['billed_company'])))
        if not own and r['order_id']:
            links.append(dict(kind='shree_order', key=key[r['id']],
                              own=r['order_id']))
    for r in conn.execute('SELECT id, billed_invoice, billed_company FROM '
                          "sales_order WHERE COALESCE(source,'')<>'SHREE'"):
        if r['billed_invoice'] and src.get(r['billed_invoice']) == 'SHREE':
            links.append(dict(kind='order_billed', own=r['id'],
                              key=key[r['billed_invoice']],
                              firm=code.get(r['billed_company'])))
    return links


def restore_links(conn, links):
    ids = {tuple(v): k for k, v in _key_of(conn).items()}
    code = {r['code']: r['id'] for r in conn.execute('SELECT id, code FROM company')}
    done = lost = 0
    for l in links:
        target = ids.get(tuple(l['key']))
        own_ok = True
        if l['kind'] in ('own_from', 'own_billed', 'shree_billed'):
            own_ok = conn.execute('SELECT 1 FROM invoice WHERE id=?',
                                  (l['own'],)).fetchone() is not None
        elif l['kind'] in ('shree_order', 'order_billed'):
            own_ok = conn.execute('SELECT 1 FROM sales_order WHERE id=?',
                                  (l['own'],)).fetchone() is not None
        if not target or not own_ok:
            lost += 1
            continue
        if l['kind'] == 'own_from':
            conn.execute('UPDATE invoice SET from_challan=? WHERE id=?',
                         (target, l['own']))
        elif l['kind'] == 'own_billed':
            conn.execute('UPDATE invoice SET billed_invoice=?, billed_company=? '
                         'WHERE id=?', (target, code.get(l.get('firm')), l['own']))
        elif l['kind'] == 'shree_billed':
            conn.execute('UPDATE invoice SET billed_invoice=?, billed_company=? '
                         'WHERE id=?', (l['own'], code.get(l.get('firm')), target))
        elif l['kind'] == 'shree_order':
            conn.execute('UPDATE invoice SET order_id=? WHERE id=?',
                         (l['own'], target))
        elif l['kind'] == 'order_billed':
            conn.execute('UPDATE sales_order SET billed_invoice=?, '
                         'billed_company=? WHERE id=?',
                         (target, code.get(l.get('firm')), l['own']))
        done += 1
    conn.commit()
    return dict(restored=done, lost=lost)


# -------------------------------------------------------------- census

def census(conn):
    """What the books hold, firm by firm, in a form two copies can be
    compared by."""
    out = {}
    # A date SHREE was given by mistake in the future (2027-03-10 is in the
    # books) must not decide what "newest" is -- corrected, it would read as
    # the books going backwards. Today is the limit, as it is for the folders
    # (import_shree_full) and for books_newest: allowing tomorrow, a challan
    # dated 23-09 on the 22nd was BSTP's newest, and once SHREE re-dated it
    # every update on the Windows PC was refused (22 Sep 2026).
    today = datetime.date.today().isoformat()
    codes = {r['id']: r['code'] for r in conn.execute('SELECT id, code FROM company')}
    names = {r['id']: r['name'] for r in conn.execute('SELECT id, name FROM ledger')}
    for firm in FIRMS:
        out[firm] = dict(docs={}, vouchers={}, ledgers={}, own={},
                         ledger_ids={}, item_ids={}, newest='', out_by=0.0,
                         lines={}, faults={})
    # What each document's lines come to: how many, the kilos, the money.
    # A SHREE file read damaged or half written keeps its bills' headers and
    # spoils their lines -- a test of 22 Sep 2026 lost every BSI 2026-27 line
    # that way, and nothing else the update looked at had moved.
    sig = {r[0]: [r[1], r[2] or 0.0, r[3] or 0.0] for r in conn.execute(
        'SELECT invoice_id, COUNT(*), ROUND(SUM(weight), 3), '
        'ROUND(SUM(line_total), 2) FROM invoice_item GROUP BY invoice_id')}
    for r in conn.execute(
            'SELECT id, company_id, doc_type, fy, series, invoice_no, '
            'invoice_date, party_id, grand_total, source FROM invoice'):
        firm = codes.get(r['company_id'])
        if firm not in out:
            continue
        k = api.doc_key(r['doc_type'], r['fy'], r['series'], r['invoice_no'])
        row = [names.get(r['party_id'], ''), api.r2(r['grand_total']),
               r['invoice_date'], r['source'] or '', r['id']]
        out[firm]['docs'][k] = row
        if r['source'] == 'SHREE':
            out[firm]['lines'][k] = sig.get(r['id'], [0, 0.0, 0.0])
        if r['source'] != 'SHREE':
            out[firm]['own']['i%d' % r['id']] = [k] + row
        elif out[firm]['newest'] < r['invoice_date'] <= today:
            out[firm]['newest'] = r['invoice_date']
    for r in conn.execute(
            'SELECT id, company_id, kind, fy, voucher_no, voucher_date, '
            'party_id, amount, source FROM voucher'):
        firm = codes.get(r['company_id'])
        if firm not in out:
            continue
        k = api.doc_key(r['kind'], r['fy'], '', r['voucher_no'])
        row = [names.get(r['party_id'], ''), api.r2(r['amount']),
               r['voucher_date'], r['source'] or '', r['id']]
        out[firm]['vouchers'][k] = row
        if r['source'] != 'SHREE':
            out[firm]['own']['v%d' % r['id']] = [k] + row
        elif out[firm]['newest'] < r['voucher_date'] <= today:
            out[firm]['newest'] = r['voucher_date']
    for r in conn.execute('SELECT id, company_id, name, source FROM ledger '
                          'WHERE system_key IS NULL'):
        firm = codes.get(r['company_id'])
        if firm in out:
            out[firm]['ledgers'][norm(r['name'])] = r['source'] or ''
            out[firm]['ledger_ids'][str(r['id'])] = r['name']
    for r in conn.execute('SELECT id, company_id, name FROM item'):
        firm = codes.get(r['company_id'])
        if firm in out:
            out[firm]['item_ids'][str(r['id'])] = r['name']
    totals = api._book_totals(conn, [cid for cid, c in codes.items() if c in out])
    for cid, v in totals.items():
        out[codes[cid]]['out_by'] = api.r2(v)
    import verify
    for cid, code in codes.items():
        if code in FIRMS:
            out[code]['faults'] = {c.key: [c.title, c.count] for c in
                                   verify.run(conn, cid, serious_only=True)
                                   if c.level == verify.BAD}
    out['_faults'] = _faults(conn)
    return out


def _faults(conn):
    import verify
    return {c.key: [c.title, c.count, api.r2(c.amount)]
            for c in verify.run(conn, serious_only=True)
            if c.level == verify.BAD}


def _by_id(side):
    """{table: {id: (firm, key)}} for one census."""
    out = dict(invoice={}, voucher={})
    for firm in FIRMS:
        c = side.get(firm) or {}
        for table, part in (('invoice', 'docs'), ('voucher', 'vouchers')):
            for k, row in c.get(part, {}).items():
                out[table][row[4]] = (firm, part, k)
    return out


def _diff_lists(report, before, after, diff):
    """Put the diff's rows under their firms, named as the paper names them."""
    b_ids, a_ids = _by_id(before), _by_id(after)
    for firm, f in report['firms'].items():
        f.update(added=[], removed=[], changed=[])
        for part in ('docs', 'vouchers'):
            f['counts'][part].update(added=0, removed=0, changed=0)
    for table in ('invoice', 'voucher'):
        for what, ids, side in (('added', diff['added'][table], a_ids),
                                ('removed', diff['removed'][table], b_ids),
                                ('changed', diff['changed'][table], a_ids)):
            for rid in ids:
                hit = side[table].get(rid)
                if not hit or hit[0] not in report['firms']:
                    continue
                firm, part, k = hit
                f = report['firms'][firm]
                f['counts'][part][what] += 1
                row = (after if side is a_ids else before)[firm][part][k]
                line = [k] + row[:3]
                if what == 'changed':
                    was = before[firm][part].get(k) or row
                    line += was[:3]
                f[what].append(line)
    for firm, f in report['firms'].items():
        for kind, table, names in (('accounts', 'ledger', 'ledger_ids'),
                                   ('items', 'item', 'item_ids')):
            got = {}
            for what, side in (('added', after), ('removed', before),
                               ('changed', after)):
                ids = [str(i) for i in diff[what][table]]
                got[what] = sorted(side[firm][names][i] for i in ids
                                   if i in side[firm][names])
            f['counts'][kind] = dict(
                f['counts'].get(kind, {}), added=len(got['added']),
                removed=len(got['removed']), changed=len(got['changed']))
            f['%s_added' % kind] = got['added'][:200]
            f['%s_removed' % kind] = got['removed'][:200]
            f['%s_changed' % kind] = got['changed'][:200]
        f['counts']['items']['total'] = len(after[firm]['item_ids'])
        for key in ('added', 'removed', 'changed'):
            f[key] = sorted(f[key])[:300]


def _paper_no(key):
    """'G 75' out of 'SALE|2026-27||G 75' -- the number on the paper."""
    parts = key.split('|')
    return ' '.join(p for p in parts[2:] if p) or key


# What a firm is held back for, and which of those the user may overrule once
# shown what would go: that SHREE's files really are what they say. A
# document made in this app changing is never theirs to wave through.
ACCEPTABLE = ('gone', 'newest', 'lines', 'health', 'accounts')


def _fail(report, firm, kind, text, accept=()):
    """One firm's refusal. The rest of the update still goes ahead without it
    (see Manager._chain); one the user accepted is only said."""
    if firm in accept and kind in ACCEPTABLE:
        report['warnings'].append(text + ' -- updated anyway, as you chose')
        return
    report['failures'].append(text)
    h = report['held'].setdefault(firm, dict(why=[], kinds=[]))
    h['why'].append(text)
    h['kinds'].append(kind)
    h['can_accept'] = all(k in ACCEPTABLE for k in h['kinds'])


def _newest_back(report, firm, b, a, accept=()):
    """SHREE's newest entry for a firm is earlier than the books' was.

    A folder older than the books looks like that, and must not be read: the
    entries made since it was copied would be taken out. But so does SHREE
    correcting the date of the newest entry, and refusing that held back every
    firm's challans and bills on the Windows PC (22 Sep 2026). So look at the
    entries that ran further than SHREE now does: still in SHREE under their
    own numbers, they were re-dated there. Gone, the folder is older -- unless
    only a few went and SHREE brings entries these books never had, which no
    older copy of the books' own source could (they were deleted in SHREE).
    """
    late = [(part, k, row[2]) for part in ('docs', 'vouchers')
            for k, row in b[part].items()
            if row[3] == 'SHREE' and a['newest'] < row[2] <= b['newest']]
    lost = [(part, k, day) for part, k, day in late if k not in a[part]]
    moved = [(part, k, day) for part, k, day in late if k in a[part]]
    new = sum(1 for part in ('docs', 'vouchers')
              for k, row in a[part].items()
              if row[3] == 'SHREE' and k not in b[part])
    said = ', '.join('%s of %s' % (_paper_no(k), _ddmmyyyy(day))
                     for _p, k, day in (lost or moved)[:5])
    if lost and (len(lost) > 3 or not new):
        _fail(report, firm, 'newest',
              '%s: SHREE\'s newest entry would go back from %s to %s -- the '
              'folder looks older than the books (%s %s no longer in it)'
              % (firm, _ddmmyyyy(b['newest']),
                 _ddmmyyyy(a['newest']) or 'nothing', said,
                 'is' if len(lost) == 1 else 'are'), accept)
    elif lost:
        report['warnings'].append(
            '%s: %s %s no longer in SHREE -- deleted there, so taken out here '
            'too' % (firm, said, 'is' if len(lost) == 1 else 'are'))
    else:
        report['warnings'].append(
            '%s: SHREE has re-dated %s -- now %s'
            % (firm, said, ', '.join(sorted({_ddmmyyyy(a[p][k][2])
                                             for p, k, _d in moved[:5]}))))


def _lines_spoilt(report, firm, b, a, accept=(), restated=()):
    """Documents SHREE still has as they were -- party, amount, date -- whose
    lines came back different. SHREE changing a bill changes its total too;
    lines moving by themselves, by the year-full, is a file read wrong.

    A bill restate_bills rebuilt from its challan this time is left out of
    both counts: its lines are the challan's, checked against the bill's own
    taxable value, not SHREE's. On books from before 19 Sep 2026, never
    restated, some 4,300 BST and BSI bills change at once, every update was
    refused as "SHREE's bill lines look damaged" -- and, with the firm left
    out still restated, tried again for ever (27 Sep 2026)."""
    bl, al = b.get('lines') or {}, a.get('lines') or {}
    if not bl or not al:
        return                    # a census from before lines were counted
    moved, per_year, in_year = [], {}, {}
    for k, was in bl.items():
        if k in restated:
            continue
        fy = k.split('|')[1] if k.count('|') >= 3 else ''
        in_year[fy] = in_year.get(fy, 0) + 1
        now = al.get(k)
        if now is None or now == was:
            continue
        if b['docs'].get(k, [None] * 3)[:3] != a['docs'].get(k, [None] * 3)[:3]:
            continue              # SHREE changed the document itself
        moved.append(k)
        per_year[fy] = per_year.get(fy, 0) + 1
    worst = [fy for fy, n in per_year.items()
             if n >= 5 and n > in_year[fy] * 0.5]
    if worst or len(moved) > max(25, len(bl) * 0.02):
        years = worst or sorted(per_year, key=per_year.get)[-1:]
        _fail(report, firm, 'lines',
              '%s: the lines of %d documents would change while the documents '
              'themselves did not (%s) -- SHREE\'s bill lines look damaged or '
              'half written' % (firm, len(moved), ', '.join(
                  '%s: %d of %d' % (fy, per_year[fy], in_year[fy])
                  for fy in sorted(years))), accept)


def _worse_health(report, firm, b, a, accept=()):
    """A kind of fault Data health reports, grown by more than an odd
    document's worth in this firm: the books would come out worse."""
    fb, fa = b.get('faults'), a.get('faults')
    if fb is None or fa is None:
        return
    docs = sum(1 for v in b['docs'].values() if v[3] == 'SHREE')
    if not docs:
        return      # its first read: nothing here yet that could get worse
    for key, (title, n) in sorted(fa.items()):
        was = (fb.get(key) or [title, 0])[1]
        if n - was > max(5, docs * 0.01):
            _fail(report, firm, 'health',
                  '%s: Data health would get worse -- %s: %d, was %d'
                  % (firm, title, n, was), accept)


def compare(before, after, faults_before, faults_after, import_notes,
            diff=None, accept=(), restated=()):
    """What an update changed, and whether it is safe to keep.

    A refusal names its firm in `held`; only what no firm can be blamed for
    (a file that fails its own check) is left in `failures` alone.
    """
    report = dict(firms={}, failures=[], warnings=[], held={})
    for firm in FIRMS:
        b, a = before.get(firm), after.get(firm)
        if not b or not a:
            continue
        f = dict(added=[], removed=[], changed=[], counts={})
        for table, label in (('docs', 'document'), ('vouchers', 'voucher')):
            bk, ak = set(b[table]), set(a[table])
            add = sorted(ak - bk)
            gone = sorted(bk - ak)
            # Party, amount or date -- what the paper says.
            chg = sorted(k for k in bk & ak if b[table][k][:3] != a[table][k][:3])
            f['counts'][table] = dict(added=len(add), removed=len(gone),
                                      changed=len(chg), total=len(ak))
            f['added'] += [[k] + a[table][k][:3] for k in add]
            f['removed'] += [[k] + b[table][k][:3] for k in gone]
            f['changed'] += [[k] + a[table][k][:3] + b[table][k][:3]
                             for k in chg]
            shree_before = sum(1 for v in b[table].values() if v[3] == 'SHREE')
            shree_gone = sum(1 for k in gone if b[table][k][3] == 'SHREE')
            if shree_gone > max(25, shree_before * 0.005):
                _fail(report, firm, 'gone',
                      '%s: %d imported %ss would disappear -- SHREE\'s files '
                      'look older or incomplete' % (firm, shree_gone, label),
                      accept)
        la, lb = set(a['ledgers']), set(b['ledgers'])
        f['counts']['accounts'] = dict(added=len(la - lb), removed=len(lb - la),
                                       total=len(la))
        f['accounts_added'] = sorted(la - lb)[:200]
        f['accounts_removed'] = sorted(lb - la)[:200]
        for key in ('added', 'removed', 'changed'):
            f[key] = f[key][:300]
        f['newest_before'], f['newest_after'] = b['newest'], a['newest']
        if a['newest'] < b['newest']:
            _newest_back(report, firm, b, a, accept)
        _lines_spoilt(report, firm, b, a, accept, restated)
        _worse_health(report, firm, b, a, accept)
        gone_acc = len(lb - la)
        if gone_acc > max(25, len(lb) * 0.05):
            _fail(report, firm, 'accounts',
                  '%s: %d accounts would disappear -- SHREE\'s accounts file '
                  'looks damaged or incomplete' % (firm, gone_acc), accept)
        if b['own'] != {k: v for k, v in a['own'].items() if k in b['own']} \
                or set(b['own']) - set(a['own']):
            _fail(report, firm, 'own',
                  '%s: a document made in this app would change' % firm)
        f['out_by_before'], f['out_by_after'] = b['out_by'], a['out_by']
        if abs(a['out_by'] - b['out_by']) > 0.5:
            report['warnings'].append(
                '%s: the trial balance difference moves from %.2f to %.2f'
                % (firm, b['out_by'], a['out_by']))
        report['firms'][firm] = f
    for note in import_notes:
        report['failures'].append(note)
    new_faults = {k: v for k, v in faults_after.items()
                  if k not in faults_before or faults_before[k][1] != v[1]}
    for k, v in sorted(new_faults.items()):
        was = faults_before.get(k)
        report['warnings'].append(
            'Data health: %s -- %d%s' % (v[0], v[1],
                                        ' (was %d)' % was[1] if was else ' (new)'))
    report['faults_before'], report['faults_after'] = faults_before, faults_after
    if diff is not None:
        _diff_lists(report, before, after, diff)
        report['diff'] = dict(rows=diff['rows'], tables=diff['tables'])
        report['touched'] = {t: _bounded(sorted(set(diff['changed'][t])
                                                | set(diff['removed'][t])))
                             for t in ('invoice', 'voucher', 'ledger', 'item')}
        report['gone'] = {t: _bounded(diff['removed'][t])
                          for t in ('invoice', 'voucher', 'ledger', 'item')}
    return report


def soundness(conn):
    bad = []
    ok = conn.execute('PRAGMA quick_check').fetchone()[0]
    if ok != 'ok':
        bad.append('The updated copy failed its own check: %s' % ok)
    fk = conn.execute('PRAGMA foreign_key_check').fetchall()
    if fk:
        bad.append('%d row(s) in the updated copy point at nothing (%s)'
                   % (len(fk), fk[0][0]))
    return bad


# -------------------------------------------------------------- the diff
#
# What an update changed, row by row and column by column, read by SQLite
# itself from the two files side by side. It is what decides whether the
# books are replaced at all, what the report lists, and which open screens
# are now behind.

# Kept under the same id from one update to the next.
DIFF_KEPT = ('company', 'account_group', 'ledger', 'item', 'invoice',
             'voucher', 'sales_order')
# Rebuilt under new ids each time: compared by what they say, and put down
# to the row they belong to.
DIFF_LINES = {'invoice_item': ('invoice_id', 'invoice'),
              'voucher_line': ('voucher_id', 'voucher'),
              'sales_order_item': ('order_id', 'sales_order'),
              # Which bills a challan went out on, when not simply one made
              # from it (link_challans.py): put down to the challan.
              'challan_bill': ('challan_id', 'invoice'),
              # The group an account was in, in the years SHREE had it in
              # another (db.LEDGER_GROUP_YEAR): put down to the account.
              'ledger_group_year': ('ledger_id', 'ledger'),
              'posting': (None, None),
              'counter': (None, None)}
DIFF_SKIP_COLS = {'id', 'created_at', 'updated_at'}


def _cols(conn, table, schema='main'):
    return [r[1] for r in conn.execute('PRAGMA %s.table_info(%s)'
                                       % (schema, table))]


def _file_uri(path, mode):
    from urllib.request import pathname2url
    return 'file:%s?%s' % (pathname2url(os.path.abspath(path)), mode)


def book_diff(new_path, old_path):
    """Every row that differs between two copies of the books.

    Both are opened as files nobody is writing -- the caller sees to that --
    so neither needs its write-ahead log.
    """
    conn = sqlite3.connect(_file_uri(new_path, 'immutable=1'), uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS o", (_file_uri(old_path, 'immutable=1'),))
        out = dict(added={}, removed={}, changed={}, rows=0, tables={})
        for t in DIFF_KEPT:
            cols = [c for c in _cols(conn, t) if c not in DIFF_SKIP_COLS]
            if set(cols) != set(c for c in _cols(conn, t, 'o')
                                if c not in DIFF_SKIP_COLS):
                cols = [c for c in cols if c in _cols(conn, t, 'o')]
            sel = ', '.join(['id'] + cols)
            newer = {r[0] for r in conn.execute(
                'SELECT id FROM (SELECT %s FROM main.%s EXCEPT SELECT %s FROM o.%s)'
                % (sel, t, sel, t))}
            older = {r[0] for r in conn.execute(
                'SELECT id FROM (SELECT %s FROM o.%s EXCEPT SELECT %s FROM main.%s)'
                % (sel, t, sel, t))}
            new_ids = {r[0] for r in conn.execute('SELECT id FROM main.%s' % t)} \
                if newer or older else set()
            old_ids = {r[0] for r in conn.execute('SELECT id FROM o.%s' % t)} \
                if newer or older else set()
            out['added'][t] = sorted(newer - old_ids)
            out['removed'][t] = sorted(older - new_ids)
            out['changed'][t] = set((newer | older) & new_ids & old_ids)
        for t, (fk, parent) in DIFF_LINES.items():
            have_new, have_old = _cols(conn, t), _cols(conn, t, 'o')
            if not have_new and not have_old:
                out['tables'][t] = 0
                continue
            cols = [c for c in have_new or have_old if c not in DIFF_SKIP_COLS]
            if have_new and have_old and set(cols) != set(
                    c for c in have_old if c not in DIFF_SKIP_COLS):
                cols = [c for c in cols if c in have_old]
            sel = ', '.join(cols)
            # A table one copy does not have yet -- books from before the
            # app that made it -- reads as an empty one.
            new_src = 'main.%s' % t if have_new else \
                '(SELECT %s FROM o.%s WHERE 0)' % (sel, t)
            old_src = 'o.%s' % t if have_old else \
                '(SELECT %s FROM main.%s WHERE 0)' % (sel, t)
            # Counted, so a line that appears twice and loses one copy shows.
            # Both copies are grouped in one pass, each distinct line with how
            # many times each copy holds it, and kept where the counts differ.
            # A line comes out once per copy that holds it, with that copy's
            # count -- the rows the two-way EXCEPT of per-side counts gave,
            # found with one sort of the ledger instead of four (posting:
            # 1.44 s -> 0.57 s, identical results on three pairs of books).
            diff = []
            for row in conn.execute(
                    'SELECT %s, sum(in_new__), sum(in_old__) FROM ('
                    'SELECT %s, 1 in_new__, 0 in_old__ FROM %s UNION ALL '
                    'SELECT %s, 0, 1 FROM %s) GROUP BY %s '
                    'HAVING sum(in_new__) <> sum(in_old__)'
                    % (sel, sel, new_src, sel, old_src, sel)):
                line, in_new, in_old = tuple(row[:-2]), row[-2], row[-1]
                if in_new:
                    diff.append(line + (in_new,))
                if in_old:
                    diff.append(line + (in_old,))
            out['tables'][t] = len(diff)
            out['rows'] += len(diff)
            if parent:
                i = cols.index(fk)
                out['changed'][parent].update(r[i] for r in diff if r[i])
            elif t == 'posting':
                k, d = cols.index('doc_kind'), cols.index('doc_id')
                for r in diff:
                    if r[k] in INVOICE_KINDS and r[d]:
                        out['changed']['invoice'].add(r[d])
                    elif r[k] in VOUCHER_KINDS and r[d]:
                        out['changed']['voucher'].add(r[d])
        for t in DIFF_KEPT:
            # A row added or removed is not also "changed".
            out['changed'][t] = sorted(out['changed'][t]
                                       - set(out['added'][t])
                                       - set(out['removed'][t]))
            n = (len(out['added'][t]) + len(out['removed'][t])
                 + len(out['changed'][t]))
            out['tables'][t] = n
            out['rows'] += n
        return out
    finally:
        conn.close()


# --------------------------------------------------------------- runner

def _script(name):
    return os.path.join(BASE, name)


def _python():
    """The interpreter the steps run under.

    On Windows the app itself runs under pythonw.exe, which has no console of
    its own. The steps are run under python.exe beside it -- their output is
    what the log shows -- with no window (CREATE_NO_WINDOW, below).
    """
    exe = sys.executable
    folder, name = os.path.split(exe)
    if name.lower().startswith('pythonw'):
        plain = os.path.join(folder, 'python' + name[len('pythonw'):])
        if os.path.isfile(plain):
            return plain
    return exe


def _bounded(ids):
    """Ids worth remembering, or None for "too many -- assume any"."""
    ids = list(ids)
    return ids if len(ids) <= IDS_KEPT else None


class StepFailed(RuntimeError):
    """A script of the update stopped with an error, or ran too long."""


class Manager(object):
    """One update at a time, run from a thread inside the app when somebody
    asks for one."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cond = threading.Condition()
        self.readers = 0
        self.writers = 0
        self.waiting = 0            # saves held until an update is in
        self.swapping = False
        self.blocking_writes = False
        self.state = dict(running=False, mode='', started='', started_by='',
                          step='', step_no=0, steps=0, log=[], result=None)
        self.books_updated = ''
        # What each recent update changed, oldest first, so that a page
        # opened before it is only stopped from saving what really moved.
        self.changes = []
        self.known_since = None
        self.deep = False           # Fetch everything: every year read again
        self.wiped = {}             # ... and what it cleared away first

    def load(self):
        """What the app starts with: when the books were last replaced, and
        what the recent updates changed."""
        conn = db.connect()
        try:
            self.books_updated = db.get_meta(conn, 'shree_last_update') or ''
            found, since = [], self.books_updated
            for r in conn.execute(
                    "SELECT summary FROM sync_run WHERE mode='apply' AND "
                    "status='done' AND backup<>'' ORDER BY id DESC LIMIT ?",
                    (CHANGES_KEPT,)):
                try:
                    s = json.loads(r['summary'] or '{}')
                except ValueError:
                    break
                if 'stamp' not in s or 'touched' not in s:
                    break
                found.append(dict(stamp=s['stamp'], touched=s['touched'],
                                  gone=s['gone']))
                since = s.get('prev_stamp', '')
            self.changes = found[::-1]
            self.known_since = since
        finally:
            conn.close()

    # -- the app asks these on every request ------------------------------
    def touch(self):
        """Kept for the server's sake; updates no longer wait for quiet."""

    BLOCKED = ('The books are still being updated from SHREE. Nothing was '
               'saved -- try again in a minute.')

    def enter(self, writing=False):
        """Called before every API request. Answers why it may not go ahead.

        An update works on a copy of the books taken when it starts, and the
        copy then replaces them, so a save made meanwhile would be lost. It
        is held instead, and goes in the moment the update is done -- the
        screen only sees a save that took a little longer. Only one held for
        more than HOLD_SECONDS is turned away. Every request waits while the
        books are being replaced, which takes a second or two.
        """
        with self.cond:
            while self.swapping:
                self.cond.wait(1.0)
            if writing and self.blocking_writes:
                self.waiting += 1
                end = time.time() + HOLD_SECONDS
                try:
                    while ((self.blocking_writes or self.swapping)
                           and time.time() < end):
                        self.cond.wait(1.0)
                finally:
                    self.waiting -= 1
                if self.blocking_writes or self.swapping:
                    return self.BLOCKED
            self.readers += 1
            self.writers += 1 if writing else 0
            return ''

    def leave(self, writing=False):
        with self.cond:
            self.readers -= 1
            self.writers -= 1 if writing else 0
            self.cond.notify_all()

    def _block_writes(self, on):
        with self.cond:
            self.blocking_writes = on
            self.cond.notify_all()
            if not on:
                return
            # A save already under way finishes before the copy is taken.
            waited = 0
            while self.writers and waited < 120:
                self.cond.wait(0.5)
                waited += 0.5

    def stale_for(self, seen, target, refs):
        """Why a save from a page opened at `seen` may not go ahead, or ''.

        `target` is the (table, id) the save rewrites or deletes, `refs` the
        rows it points at, {table: ids}. Every update keeps each row's id, so
        only a save touching what an update changed or removed is stopped.
        """
        if seen == self.books_updated:
            return ''
        with self.lock:
            changes = list(self.changes)
            since = self.known_since
        if since is None or seen < since:
            return ('The books were updated from SHREE after this page was '
                    'opened. Reload the page, then save again.')
        later = [c for c in changes if c['stamp'] > seen]
        if target:
            table, rid = target
            for c in later:
                ids = c['touched'].get(table, [])
                if ids is None or rid in ids:
                    return ('This %s was changed in SHREE after the page was '
                            'opened. Reload the page to see SHREE\'s version, '
                            'then make the change again.'
                            % TABLE_NAME.get(table, 'entry'))
        for table, ids in (refs or {}).items():
            for c in later:
                gone = c['gone'].get(table, [])
                if gone is None or set(ids) & set(gone):
                    return ('The %s this entry uses was removed in SHREE after '
                            'the page was opened. Reload the page and choose '
                            'it again.' % TABLE_NAME.get(table, 'entry'))
        return ''

    # -- status -----------------------------------------------------------
    def status(self):
        with self.lock:
            st = dict(self.state)
            st['log'] = list(self.state['log'][-60:])
            st.pop('step_at', None)
        st['waiting'] = self.waiting
        return st

    def _log(self, text):
        with self.lock:
            for line in str(text).splitlines():
                self.state['log'].append(line)
            if len(self.state['log']) > 4000:
                self.state['log'] = self.state['log'][-3000:]

    def _step(self, label):
        t = time.time()
        with self.lock:
            prev = self.state.get('step_at')
            if prev and self.state['step']:
                self.state['timings'].append(
                    [self.state['step'], round(t - prev, 1)])
            self.state['step'] = label
            self.state['step_no'] += 1
            self.state['step_at'] = t
        self._log('== %s' % label)

    def _again(self):
        """Before another try: saves held for the last one go in, and the
        steps are counted from the start again."""
        self._block_writes(False)
        with self.lock:
            self.state['step_no'] = 1

    # -- starting ---------------------------------------------------------
    def start(self, mode='apply', started_by='you', accept=(),
              everything=False):
        """`everything` is Fetch everything: every firm taken as SHREE has
        it, every year read again by its contents -- asked for on 2 Oct 2026,
        "fetch everything no questions asked" -- and, since 6 Oct 2026, the
        books cleared first of everything made or decided here that SHREE
        does not have (wipe_for_everything), so their figures are SHREE's.
        A copy of the books is still kept first."""
        everything = bool(everything) and mode == 'apply'
        with self.lock:
            if self.state['running']:
                raise api.ApiError('An update is already running.', 409)
            self.state = dict(running=True, mode=mode, started=now(),
                              started_by=started_by, step='Starting',
                              step_no=0, steps=(15 if everything else 14)
                              if mode == 'apply' else 12,
                              log=[], result=None, timings=[],
                              everything=everything)
        accept = FIRMS if everything else \
            tuple(f for f in (accept or ()) if f in FIRMS)
        t = threading.Thread(target=self._run,
                             args=(mode, started_by, accept, everything),
                             name='shree-update', daemon=True)
        t.start()
        return self.status()

    def _run(self, mode, started_by, accept=(), everything=False):
        result = dict(status='failed', message='', summary={}, backup='')
        conn = db.connect()
        _clear_old_work()
        work_dir = tempfile.mkdtemp(prefix=WORK_PREFIX)
        work = os.path.join(work_dir, 'work.db')
        started = time.time()
        try:
            result.update(self._chain(conn, mode, work, work_dir, accept,
                                      everything))
        except Exception as e:                     # noqa: BLE001
            self._log(traceback.format_exc())
            result['message'] = '%s: %s' % (type(e).__name__, e)
        finally:
            self._block_writes(False)
            self._step('Finished')
            with self.lock:
                timings = list(self.state['timings'])
            if isinstance(result.get('summary'), dict):
                result['summary']['timings'] = timings
                result['summary']['seconds'] = round(time.time() - started, 1)
            try:
                self._record(conn, mode, started_by, result)
            except Exception:                      # noqa: BLE001
                self._log(traceback.format_exc())
            conn.close()
            keep = os.environ.get('BILLING_SYNC_KEEP')
            if keep and os.path.isfile(work):
                shutil.copyfile(work, keep)       # for looking into a run
            _remove_tree(work_dir)
            with self.lock:
                self.state['running'] = False
                self.state['result'] = dict(result, summary=None)

    def _sub(self, args, work, label, timeout=1800):
        """Run one script on the working copy, its output into the log."""
        env = dict(os.environ, BILLING_DB=work, PYTHONIOENCODING='utf-8',
                   PYTHONUNBUFFERED='1')
        flags = 0x08000000 if os.name == 'nt' else 0     # CREATE_NO_WINDOW
        proc = subprocess.Popen([_python(), '-u'] + args, cwd=ROOT, env=env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                creationflags=flags)
        lines = []
        started = time.time()
        for raw in proc.stdout:
            line = raw.decode('utf-8', 'replace').rstrip()
            lines.append(line)
            self._log(line)
            if time.time() - started > timeout:
                proc.kill()
                raise StepFailed('%s took longer than %d minutes'
                                 % (label, timeout // 60))
        code = proc.wait()
        self._log('-- %s: %.1f s' % (label, time.time() - started))
        if code:
            raise StepFailed('%s stopped with an error' % label)
        return lines

    def _pair(self, work, strict=False):
        """link_challans until it finds nothing more.

        `strict` before the repairs: a bill only when it is the challan plus
        GST. The looser shapes -- billed in parts, re-priced -- add up kilos
        and money, and a return whose lines repair.py has not yet turned round
        adds up the wrong way; they wait for the second pass.
        """
        for _ in range(4):
            out = self._sub([_script('link_challans.py'), '--apply']
                            + (['--strict'] if strict else []), work,
                            'Challan pairing')
            if any(l.startswith('0 pairs') or l.startswith('No challans')
                   for l in out):
                break

    def _plan(self, conn, sources, left_out, work, work_dir, accept=()):
        """Which firms this update brings in, and why any other is left.

        Every backup is unpacked and every year looked into here, before
        saving is held. Then a firm is taken only if its books were found and
        run at least as far as the books here already do -- a copy of SHREE
        from last week, or a backup standing in for a year SHREE is holding
        to itself, would take entries away. Each firm is judged on its own:
        one computer switched off no longer stops the other's firms.
        """
        found_file = os.path.join(work_dir, 'found.json')
        args = [_script('import_shree_full.py'), '--list',
                '--json', found_file, '--work', unpack_dir(), '--keep-work']
        if self.deep:
            args.append('--deep')
        for s in sources:
            args += ['--source', s]
        self._sub(args, work, 'Looking into SHREE\'s years', timeout=3600)
        with open(found_file, encoding='utf-8') as fh:
            found = json.load(fh)
        have = books_newest(conn)
        firms, skipped, older, newest = [], [], {}, {}
        for firm in FIRMS:
            info = found['firms'].get(firm)
            busy = [b for b in found.get('busy', []) if b['firm'] == firm]
            why = ''
            if not info and left_out:
                # A folder on the list did not answer -- most often the other
                # computer switched off. Asking for the folder to be added,
                # when it already is, sent people looking in the wrong place.
                said = ' '.join(left_out).rstrip('.')
                why = ('its %s is in none of the folders that answered, and '
                       'one did not -- %s It will be at the next update once '
                       'that folder answers'
                       % (BOOK_NAME[firm],
                          said if said.endswith(('?', '!')) else said + '.'))
                if busy:
                    why = ('SHREE is holding %s %s to itself. Close SHREE on '
                           'that computer, or wait for the next update'
                           % (firm, busy[0]['year']))
            elif not info:
                why = ('none of the folders that could be read holds SHREE\'s '
                       '%s. Add the SHREE folder of the computer it is kept on '
                       'under "Where SHREE is"' % BOOK_NAME[firm])
                if busy:
                    why = ('SHREE is holding %s %s to itself. Close SHREE on '
                           'that computer, or wait for the next update'
                           % (firm, busy[0]['year']))
            elif info['newest'] < have.get(firm, '') and \
                    (info.get('saved') or '') < have[firm] and not busy and \
                    not any(y.get('own') for y in info['years'].values()):
                # Only SHREE's backups of the firm were found -- its own year
                # folders are in a SHREE these folders are not. The backup of
                # some months ago is never offered in place of the books.
                last = info['years'][max(info['years'])]
                why = ('none of the folders read holds SHREE\'s own %s %s -- '
                       'only SHREE\'s backup of it, %s, which stops at %s; the '
                       'books here run to %s. %s is kept in another SHREE '
                       '(on this computer or the other one): add that SHREE\'s '
                       'folder under "Where SHREE is"'
                       % (firm, BOOK_NAME[firm],
                          last['origin'].split(' ')[-1],
                          _ddmmyyyy(info['newest']) or 'none',
                          _ddmmyyyy(have[firm]), firm))
            elif info['newest'] < have.get(firm, '') and \
                    (info.get('saved') or '') < have[firm] and \
                    firm not in accept:
                # Behind the books, and last written before the day they run
                # to: a copy from before. Written since, SHREE is as current
                # as the books -- its newest entry was re-dated or deleted
                # there -- and the check after the import looks at what went.
                why = ('the newest %s entry SHREE\'s folders give is %s, and '
                       'the books here already run to %s'
                       % (firm, _ddmmyyyy(info['newest']) or 'none',
                          _ddmmyyyy(have[firm])))
                if busy:
                    why += (' -- SHREE is holding %s %s to itself, so only an '
                            'older backup could be read. Close SHREE on that '
                            'computer, or wait for the next update'
                            % (firm, busy[0]['year']))
                else:
                    why += ' -- the folder looks older than the books'
                    # Whether it is a copy, or SHREE as it really is now, is
                    # the user's to say: asked on the screen, not skipped
                    # for ever.
                    older[firm] = '%s was left as it was: %s.' % (firm, why)
                    self._log(older[firm])
                    continue
            if why:
                skipped.append('%s was not updated: %s.' % (firm, why))
                self._log(skipped[-1])
            else:
                firms.append(firm)
                newest[firm] = dict(upto=info['newest'])
        return firms, skipped, older, newest

    def _step_json(self, work, work_dir, args, label):
        out = os.path.join(work_dir, 'out-%d.json' % int(time.time() * 1000))
        self._sub([_script('shree_sync.py'), '--step'] + args + ['--out', out],
                  work, label)
        with open(out, encoding='utf-8') as fh:
            return json.load(fh)

    def _attempt(self, conn, mode, firms, sources, skipped, left_out, work,
                 work_dir, accept, prune):
        """One pass of the update over `firms`, on a fresh copy of the books:
        everything up to and including the check. Nothing live is touched."""
        skipped = list(skipped)
        # Saving is held from here: the copy is the books as they stand now.
        if mode == 'apply':
            self._block_writes(True)
        self._step('Copying the books to work on')
        # Each try works on files of its own: the last try's are not
        # deleted first, which Windows refuses while its virus scanner is
        # still reading a file just written. The folder goes at the end.
        before_db = os.path.join(work_dir, 'before.db')
        n = 1
        while os.path.exists(before_db) or os.path.exists(work):
            n += 1
            before_db = os.path.join(work_dir, 'before-%d.db' % n)
            work = os.path.join(work_dir, 'work-%d.db' % n)
        conn.execute('VACUUM INTO ?', (before_db,))
        shutil.copyfile(before_db, work)
        prev_stamp = self.books_updated

        self._step('Noting what the books hold now')
        # Documents an earlier import filed under the wrong kind are turned
        # round first, in place and under their ids, so this count and the
        # one after the import name them alike. Counted as they were, the
        # 150 cash memos BST's earlier imports filed as credit notes read as
        # 150 documents gone, and every update from 19 Sep 2026 was refused.
        turned = self._step_json(work, work_dir, ['rekind'], 'Cash memos')
        if turned.get('documents'):
            self._log('%d cash memos filed as credit notes turned back into '
                      'bills' % turned['documents'])
        if self.deep:
            # Fetch everything: what was made or decided here goes first,
            # from the firms about to be read -- a firm SHREE could not be
            # read for keeps its entries.
            self._step('Clearing the books of what SHREE does not have')
            self.wiped = self._step_json(
                work, work_dir, ['wipe', '--firms', ','.join(firms)],
                'Clearing the books')
            self._log('cleared for Fetch everything: %s' % json.dumps(
                self.wiped, sort_keys=True))
        before = self._step_json(work, work_dir, ['census'], 'Counting')
        links = self._step_json(work, work_dir, ['save-links'], 'Links')
        links_file = os.path.join(work_dir, 'links.json')
        with open(links_file, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(links))

        self._step('Reading SHREE')
        unpack = unpack_dir()
        args = [_script('import_shree_full.py')]
        for s in sources:
            args += ['--source', s]
        for f in firms:
            args += ['--company', f]
        args += ['--work', unpack, '--keep-work']
        if self.deep:
            args.append('--deep')
        # What one computer's backups left in the work folder is cleared
        # only when both were read: with the other one switched off, its
        # unpacked backups and copies are what the next update stands on.
        if prune and not left_out and not skipped:
            args += ['--prune-work']
        lines = self._sub(args, work, 'Reading SHREE', timeout=3600)
        # A firm none of the folders held this time was not read, so nothing
        # of it changed: it is said, not refused.
        unread = {}
        for m in (re.match(r'^(\w+): nothing found', l) for l in lines):
            if m:
                skipped.append('%s was not updated: none of the folders read '
                               'holds its books this time.' % m.group(1))
                unread[m.group(1)] = 'none of the folders read holds its books'
        # Years SHREE was holding to itself, read as they were last time.
        held_back = [l.strip() for l in lines if 'SHREE is using it' in l]
        # The books just written, set against SHREE's own folders of this
        # year and last (import_shree_full.check_against).
        checks = [l.strip()[len('check: '):] for l in lines
                  if l.strip().startswith('check: ')]
        # A firm whose books SHREE snatched back while they were being copied,
        # or that could not be read at all, is left as it was; the others
        # came in.
        for l in lines:
            m = re.match(r'^(\w+): skipped -- (.*)$', l)
            if m:
                skipped.append('%s was not updated: %s. It will be at the '
                               'next update.' % (m.group(1), m.group(2)))
                unread[m.group(1)] = m.group(2)

        self._step('Putting back this app\'s links to imported documents')
        got = self._step_json(work, work_dir, ['restore-links', '--in',
                                               links_file], 'Links')
        self._log('links restored %(restored)d, lost %(lost)d' % got)

        self._step('Replaying documents deleted or edited here')
        done_docs = self._step_json(work, work_dir,
                                    ['replay', '--phase', 'docs'], 'Replay')

        self._step('Deciding which firm bills each customer')
        self._sub([_script('allocate_customers.py'), '--apply'], work,
                  'Customer allocation')

        self._step('Pairing challans with their bills')
        self._pair(work, strict=True)

        self._step('Putting right what the import misreads')
        self._sub([_script('repair.py'), '--apply'], work, 'Repairs')
        self._sub([_script('fill_hsn.py'), '--apply', '--lines'], work,
                  'HSN codes')
        # Once more, now both sides are put right. A firm left as it was keeps
        # its returns turned negative by the last update's repairs, and they
        # cannot be matched to the other firm's credit bills until those are
        # turned the same way: ten return pairs were lost like that.
        self._pair(work)

        # SHREE wrote a challan's cutting, transport and loading into the
        # bill's rate; each bill paired with its challan is shown as the
        # challan plus GST again, its amounts left exactly as filed.
        self._step('Showing each bill as its challan plus GST')
        # A file of this try's own, like the copies above.
        restated_file = work + '.restated.json'
        self._sub([_script('restate_bills.py'), '--apply', '--ids',
                   restated_file], work, 'Bills as their challans')

        self._step('Replaying the decisions taken here')
        done_accounts = self._step_json(work, work_dir,
                                        ['replay', '--phase', 'accounts'],
                                        'Replay')

        self._step('Checking the result against the books')
        before_file = os.path.join(work_dir, 'before.json')
        with open(before_file, 'w', encoding='utf-8') as fh:
            # dumps, not dump: the same text, by the C encoder (the census
            # is 10 MB; dump to a file encodes it in Python, 3x slower)
            fh.write(json.dumps(before))
        notes_file = os.path.join(work_dir, 'notes.json')
        with open(notes_file, 'w', encoding='utf-8') as fh:
            json.dump([], fh)
        args = ['check', '--in', before_file, '--notes', notes_file,
                '--before', before_db, '--restated', restated_file]
        if accept:
            args += ['--accept', ','.join(accept)]
        report = self._step_json(work, work_dir, args, 'Check')
        if self.deep:
            # Fetch everything cleared these firms for SHREE's books, which
            # then could not be read after all: the run goes again without
            # them, so their entries stay rather than go with nothing in
            # their place.
            for f, why in sorted(unread.items()):
                if f not in firms:
                    continue
                text = '%s: SHREE\'s books could not be read after all -- %s' \
                    % (f, why)
                report['failures'].append(text)
                h = report['held'].setdefault(f, dict(why=[], kinds=[]))
                h['why'].append(text)
                h['kinds'].append('unread')
                h['can_accept'] = False
        touched = report.pop('touched', {})
        gone = report.pop('gone', {})
        report['replayed'] = [r for r in done_docs + done_accounts
                              if r['status'] != 'skipped']
        for r in report['replayed']:
            if r['status'] == 'warn':
                report['warnings'].append('%s: %s -- %s'
                                          % (RULE_LABEL.get(r['kind'], r['kind']),
                                             r['what'], r['note']))
        return dict(report=report, skipped=skipped, before_db=before_db,
                    prev_stamp=prev_stamp, touched=touched, gone=gone,
                    links=got, held_back=held_back, work=work,
                    checks=checks)

    def _chain(self, conn, mode, work, work_dir, accept=(), everything=False):
        seed_rules(conn)
        self.deep = everything
        # What Fetch everything clears is cleared on the working copy
        # (wipe_for_everything), never on the live books ahead of the run:
        # an update that stops leaves every decision as it was.
        self.wiped = {}
        sources = sources_of(conn, fresh=True)
        if not sources:
            return dict(message='SHREE could not be found on this computer. '
                                'Name its folder under "Where SHREE is".')
        _described.clear()
        _refound[:] = []
        looks = [describe(s) for s in sources]
        lost = not all(d['found'] for d in looks)
        if lost or everything or \
                len(coverage([d for d in looks if d['found']])) < len(FIRMS):
            again = refind(conn, fresh=lost or everything)
            if again:
                self._log('SHREE is not where it was found before; reading '
                          + ', '.join(again))
                sources = again
                _described.clear()
            elif again is not None:
                # Only folders from the other computer were named, and this
                # one shows no SHREE of its own.
                return dict(message='SHREE could not be found on this '
                                    'computer. Name its folder under "Where '
                                    'SHREE is".')
        # Whatever can be read is read. The accounts book and the bill
        # registers are on two computers; with one of them switched off, not
        # shared, or named by a folder from another machine, the update used
        # to stop before reading anything at all -- for every firm.
        folders, left_out = [], []
        for s in sources:
            d = describe(s)
            if d['found']:
                folders.append(d)
            else:
                left_out.append(unreadable(s))
                self._log(left_out[-1])
        held = coverage(folders)
        remember(conn, [d['path'] for d in folders if d['books']])
        refound = list(_refound)
        if not folders:
            return dict(message=left_out[0] if left_out else
                        'None of the folders named holds SHREE\'s books. '
                        'Choose SHREE\'s folder under "Where SHREE is".')
        sources = [d['path'] for d in folders]
        self._step('Looking at SHREE\'s files')
        for s in sources:
            self._log('reading %s' % s)
        print_ = fingerprint(sources)
        self._log('fingerprint %s' % print_)
        # Every backup unpacked and every year looked into now, while saving
        # still goes on: on a computer that has never updated, that is
        # minutes of work, and saves are held only from the copy onwards.
        firms, skipped, older, newest = self._plan(conn, sources, left_out,
                                                   work, work_dir, accept)
        held_firms = {f: dict(why=[why], kinds=['newest'], can_accept=True)
                      for f, why in older.items()}
        if not firms:
            said = skipped + list(older.values())
            return dict(message='Not updated: ' + ' '.join(said),
                        summary=dict(warnings=said, held=held_firms))

        # One firm that cannot be taken no longer stops the others. The
        # whole run is tried again without it -- a fresh copy, so nothing of
        # the first try is kept -- and it is named, with what held it back.
        # Until 22 Sep 2026 one refusal about BSTP kept every firm's challans
        # and bills out of the books on the Windows PC.
        planned = list(firms)
        crash = crash_firms = None
        queue = []
        while True:
            try:
                att = self._attempt(conn, mode, firms, sources, skipped,
                                    left_out, work, work_dir, accept,
                                    prune=firms == planned)
            except StepFailed as e:
                self._log('%s -- with %s in' % (e, ', '.join(firms)))
                if crash is None:
                    crash, crash_firms = e, list(firms)
                    # Which firm's entries a script fell over is not known,
                    # so each is left out in turn until one run goes through.
                    queue = [[g for g in firms if g != f] for f in firms] \
                        if len(firms) > 1 else []
                if not queue:
                    raise crash
                firms = queue.pop(0)
                self._log('Trying again without %s'
                          % ', '.join(f for f in crash_firms if f not in firms))
                self._again()
                continue
            if crash is not None:
                for f in crash_firms:
                    if f not in firms:
                        held_firms[f] = dict(
                            why=['%s was not updated: %s while its new entries '
                                 'were in (the full log has it)' % (f, crash)],
                            kinds=['error'], can_accept=False)
                crash = crash_firms = None
                queue = []
            report = att['report']
            blamed = report.get('held') or {}
            loose = [x for x in report['failures']
                     if not any(x in h['why'] for h in blamed.values())]
            keep = [f for f in firms if f not in blamed]
            # Blame that falls only on firms already left out came from a
            # step every firm goes through, not from reading SHREE: another
            # try would end the same way. Until 27 Sep 2026 it tried again
            # for ever.
            if loose or not blamed or not keep or keep == firms:
                break
            for f, h in blamed.items():
                fr = report['firms'].get(f) or {}
                counts = fr.get('counts') or {}
                held_firms[f] = dict(
                    h, removed=(fr.get('removed') or [])[:300], counts=counts,
                    gone_total=sum((counts.get(p) or {}).get('removed', 0)
                                   for p in ('docs', 'vouchers')))
                self._log('%s is left as it was: %s' % (f, '; '.join(h['why'])))
            firms = keep
            self._log('Updating %s without it' % ', '.join(firms))
            self._again()

        report, skipped, work = att['report'], att['skipped'], att['work']
        before_db, prev_stamp = att['before_db'], att['prev_stamp']
        touched, gone, got = att['touched'], att['gone'], att['links']
        held_back = att['held_back']
        checks = [c for c in att['checks'] if c.split(' ')[0] in firms]
        missed = [c for c in checks if ' not in -- ' in c
                  or ' not checked ' in c]
        report['checks'] = checks
        # BSTP's older years are only in the backups beside SHREE: shared
        # without them, they read as documents gone, and the reason says so
        # in the words that point at the fix.
        acc = held.get('BSTP', {}).get('path')
        hint = ('the backups kept beside SHREE\'s folder in %s cannot be seen, '
                'and the older years are only in them. Share the whole drive '
                'SHREE is on, not just its SHREE folder' % acc) \
            if acc and not describe(acc)['beside'] else ''
        for f, h in held_firms.items():
            report['held'].setdefault(f, h)
        for f, h in report['held'].items():
            if f == 'BSTP' and hint and 'gone' in h.get('kinds', []):
                h['why'].insert(0, 'BSTP: ' + hint)
            if f in held_firms:
                why = h['why'][0]
                said = why if why.startswith(f + ' was ') else \
                    '%s was left as it was -- %s' % (f, why.split(': ', 1)[-1])
                skipped.append(said if said.endswith('.') else said + '.')
        report['links'] = got
        report['fingerprint'] = print_
        report['warnings'] = refound + report['warnings'] + held_back + \
            ['Checked against SHREE: ' + c for c in missed]
        # First, and in the message too: a firm left as it was is the one
        # thing on this screen that needs somebody to do something.
        report['warnings'] = skipped + [w for w in left_out
                                        if not any(w in s for s in skipped)] \
            + report['warnings']
        report['not_updated'] = [f for f in FIRMS if f not in firms
                                 or any(s.startswith(f + ' ') for s in skipped)]
        if hint and any('would disappear' in f for f in report['failures']):
            report['failures'].insert(0, hint[0].upper() + hint[1:] + '.')
        said = _what_changed(report)
        # With a firm left out, the headline names the ones that came in:
        # "Up to date" over bills that never arrived read as all is well.
        came = [f for f in FIRMS if f not in report['not_updated']]
        who = ' ' + ', '.join(came) if came and report['not_updated'] else ''
        if everything:
            # Said first: what Fetch everything did beyond an update.
            refound = [_wiped_note(self.wiped, came)] + refound
            report['wiped'] = self.wiped
        kept = ''.join(' ' + s for s in refound + skipped) + \
            _busy_note(held_back) + _check_note(checks, missed) + \
            _kept_note(report) + \
            _age_note({f: d for f, d in newest.items() if f in firms})
        if report['failures']:
            return dict(status='failed', summary=report,
                        message='Not applied: ' + report['failures'][0])
        if mode == 'preview':
            return dict(status='preview', summary=report,
                        message='Preview only%s -- %s. Nothing was written.%s'
                                % (who and ' for' + who,
                                   said or 'nothing in SHREE has changed', kept))
        if not report['diff']['rows']:
            # The books would come back exactly as they are: nothing to
            # replace, and no open page is behind.
            db.set_meta(conn, 'shree_last_print', print_)
            conn.commit()
            return dict(status='done', summary=report,
                        message=('Nothing new in what could be read.'
                                 if held_back else '%s -- nothing in SHREE '
                                 'has changed.' % ((who.strip() + ' up to date')
                                                   if who else 'Up to date'))
                        + kept)

        self._step('Keeping a copy of the books')
        backup = api.backup_books(conn, BACKUP_REASON)
        _prune_backups()

        self._step('Replacing the books with the updated copy')
        # To the microsecond: two updates a second apart must not share one.
        stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
        wconn = sqlite3.connect(work)
        try:
            wconn.execute('INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)',
                          ('shree_last_print', print_))
            wconn.execute('INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)',
                          ('shree_last_update', stamp))
            wconn.commit()
            # The planner's figures travel with the copy; bring them up to
            # date with what the update changed.
            db.optimize(wconn)
            live = db.connect()
            with self.cond:
                self.swapping = True
                waited = 0
                while self.readers and waited < 60:
                    self.cond.wait(0.5)
                    waited += 0.5
            try:
                # Switching firm is let through while saving is held; the
                # firm chosen meanwhile is the one kept.
                # Not a firm the update took away -- a new book's "My
                # Company" -- or the app would open on a firm that is gone.
                row = live.execute("SELECT value FROM meta WHERE "
                                   "key='active_company'").fetchone()
                if row and wconn.execute('SELECT 1 FROM company WHERE id=?',
                                         (row[0],)).fetchone():
                    wconn.execute('INSERT OR REPLACE INTO meta(key, value) '
                                  'VALUES (?,?)', ('active_company', row[0]))
                    wconn.commit()
                wconn.backup(live)
                # The copy arrives as one big write-ahead log; fold it into
                # the file now rather than leave it the size of the books.
                try:
                    live.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                except sqlite3.Error:
                    pass
                # Written down before anything held is let through, so a
                # held save is judged against this update.
                with self.lock:
                    self.changes.append(dict(stamp=stamp, touched=touched,
                                             gone=gone))
                    if len(self.changes) > CHANGES_KEPT:
                        self.known_since = self.changes[0]['stamp']
                        self.changes = self.changes[-CHANGES_KEPT:]
                    if self.known_since is None:
                        self.known_since = prev_stamp
                self.books_updated = stamp
            finally:
                live.close()
                with self.cond:
                    self.swapping = False
                    self.cond.notify_all()
        finally:
            wconn.close()
        report.update(stamp=stamp, prev_stamp=prev_stamp, touched=touched,
                      gone=gone)
        return dict(status='done', summary=report, backup=backup,
                    message='Updated%s -- %s.%s'
                    % (who, said or 'SHREE\'s entries were put in again as '
                       'they are', kept))

    def _record(self, conn, mode, started_by, result):
        """Write the run down."""
        summary = result.get('summary') or {}
        with self.lock:
            log = '\n'.join(self.state['log'])[-LOG_LIMIT:]
            started = self.state['started']
        status, message = result.get('status', ''), result.get('message', '')
        conn.execute(
            'INSERT INTO sync_run(started, finished, started_by, mode, '
            'status, message, summary, log, backup) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (started, now(), started_by, mode, status, message,
             json.dumps(summary), log, result.get('backup') or ''))
        conn.execute('DELETE FROM sync_run WHERE id NOT IN (SELECT id FROM '
                     'sync_run ORDER BY id DESC LIMIT ?)', (RUNS_KEPT,))
        conn.commit()


def books_newest(conn):
    """{firm: newest date of an entry brought in from SHREE}, no later than
    today -- the yardstick a folder has to reach to be read."""
    today = datetime.date.today().isoformat()
    out = {}
    for sql in ("SELECT c.code, MAX(i.invoice_date) FROM invoice i JOIN "
                "company c ON c.id=i.company_id WHERE i.source='SHREE' AND "
                "i.invoice_date<=? GROUP BY c.code",
                "SELECT c.code, MAX(v.voucher_date) FROM voucher v JOIN "
                "company c ON c.id=v.company_id WHERE v.source='SHREE' AND "
                "v.voucher_date<=? GROUP BY c.code"):
        for code, day in conn.execute(sql, (today,)):
            if day and day > out.get(code, ''):
                out[code] = day
    return out


def _ddmmyyyy(day):
    try:
        return datetime.date(*map(int, day.split('-'))).strftime('%d-%m-%Y')
    except (AttributeError, TypeError, ValueError):
        return ''


def _age_note(held, today=None):
    """' SHREE's newest entries are from 10-09-2026 (BSTP, BST) ...' when
    they are more than two days old: an update brings in what SHREE's
    folders hold, and a copy of SHREE's drive stops on the day it was made."""
    today = today or datetime.date.today()
    by_day = {}
    for firm in FIRMS:
        upto = (held.get(firm) or {}).get('upto') or ''
        try:
            day = datetime.date(*map(int, upto.split('-')))
        except (TypeError, ValueError):
            continue
        by_day.setdefault(day, []).append(firm)
    if not by_day or (today - min(by_day)).days <= 2:
        return ''
    parts = ['%s (%s)' % (d.strftime('%d-%m-%Y'), ', '.join(f))
             for d, f in sorted(by_day.items())]
    return (' SHREE\'s newest entries are from %s -- anything entered in SHREE '
            'after that is not in the folders read.' % ' and '.join(parts))


def _busy_note(held_back):
    """' SHREE had 26-27\\ESTI open, so it was read as it was at the last
    update ...' -- said in the message itself, not only under Details: on
    the Windows computers SHREE holds a year's files while it is in use, and
    "Up to date" alone claimed what the update could not see (27 Sep 2026)."""
    said = []
    for line in held_back:
        folder, _, rest = line.partition(': SHREE is using it')
        parts = [p for p in re.split(r'[\\/]+', folder.strip()) if p]
        name = '\\'.join(parts[-2:]) if len(parts) >= 2 else folder.strip()
        # "...); read from SHREE's backup X instead, to 2026-10-01"
        how = rest.split('); ', 1)[-1].strip() if '); ' in rest else \
            'read as it was at the last update'
        m = re.search(r', to (\d{4}-\d{2}-\d{2})$', how)
        if m:
            how = '%s (to %s)' % (how[:m.start()], _ddmmyyyy(m.group(1)))
        how = how.replace(' instead', '')
        text = '%s, so it was %s' % (name, how)
        if text not in said:
            said.append(text)
    if not said:
        return ''
    return (' SHREE had %s -- anything entered there since is not in yet. '
            'Close SHREE on that computer and press Update now again.'
            % '; and '.join(t.replace(', so', ' open, so', 1) for t in said))


def _check_note(checks, missed):
    """' Checked against SHREE: every bill, challan and voucher of 2025-26
    and 2026-27 is in.' -- or what is not. Read from SHREE's own folders
    after the import, so an update that read the wrong copy cannot say
    "Updated" over entries it left out (2 Oct 2026)."""
    if missed:
        return ''.join(' Checked against SHREE: %s.' % c.rstrip('.')
                       for c in missed[:3]) + \
            (' (%d more under Details.)' % (len(missed) - 3)
             if len(missed) > 3 else '')
    years = sorted({c.split(' ')[1] for c in checks})
    if not years:
        return ''
    firms = [f for f in FIRMS if any(c.startswith(f + ' ') for c in checks)]
    span = ' and '.join(years) if len(years) <= 2 else \
        'every year from %s to %s' % (years[0], years[-1])
    return (' Checked against SHREE: every bill, challan and voucher of %s '
            'is in (%s).' % (span, ', '.join(firms)))


def _kept_note(report):
    """' 1116 documents in SHREE are kept deleted here ...' -- said on every
    update, so documents deleted in this app are never silently missing."""
    n = sum(1 for r in report.get('replayed') or []
            if r.get('kind') == 'delete_doc' and r.get('status') == 'done')
    if not n:
        return ''
    return (' %d document%s in SHREE %s kept deleted here -- see Kept here '
            'to bring %s back.' % (n, '' if n == 1 else 's',
                                  'is' if n == 1 else 'are',
                                  'it' if n == 1 else 'them'))


def _plural(n, one, many=None):
    return '%d %s' % (n, one if n == 1 else (many or one + 's'))


def _wiped_note(w, firms):
    """What Fetch everything cleared before reading SHREE, in a sentence."""
    w = w or {}
    gone = []
    if w.get('made') or w.get('vouchers'):
        gone.append(_plural(w.get('made', 0) + w.get('vouchers', 0),
                            'entry', 'entries') + ' made here')
    if w.get('edited'):
        gone.append(_plural(w['edited'], 'document') + ' edited here (now as '
                    'SHREE has them)')
    if w.get('orders'):
        gone.append(_plural(w['orders'], 'order'))
    if w.get('stock') or w.get('steel'):
        gone.append(_plural(w.get('stock', 0) + w.get('steel', 0),
                            'stock entry', 'stock entries'))
    if w.get('decisions'):
        gone.append(_plural(w['decisions'], 'decision') + ' that kept '
                    'figures apart from SHREE\'s')
    if w.get('accounts') or w.get('items'):
        gone.append(_plural(w.get('accounts', 0), 'account') + ' and '
                    + _plural(w.get('items', 0), 'item') + ' made here')
    return ('Fetch everything: %s cleared and read again from SHREE%s.'
            % (', '.join(firms) or 'no firm',
               ' -- taken out: ' + '; '.join(gone) if gone else ''))


def _what_changed(report):
    """"3 new, 1 changed, 2 accounts changed" -- or '' for nothing."""
    n = dict(added=0, changed=0, removed=0)
    acc = dict(added=0, changed=0, removed=0)
    for f in report['firms'].values():
        for part in ('docs', 'vouchers'):
            for k in n:
                n[k] += f['counts'][part][k]
        for k in acc:
            acc[k] += f['counts']['accounts'].get(k, 0) \
                + f['counts'].get('items', {}).get(k, 0)
    bits = []
    if n['added']:
        bits.append('%d new' % n['added'])
    if n['changed']:
        bits.append('%d changed' % n['changed'])
    if n['removed']:
        bits.append('%d gone' % n['removed'])
    moved = sum(acc.values())
    if moved:
        bits.append('%d account%s or item%s changed'
                    % (moved, '' if moved == 1 else 's', '' if moved == 1 else 's'))
    return ', '.join(bits)


WORK_PREFIX = 'billing-shree-'
OLD_UNPACK = 'billing_shree_unpack'


def unpack_dir():
    """Where unpacked backups and the local copies of SHREE's years are kept
    between updates.

    Not the temporary folder, where they were until 17 Sep 2026: macOS clears
    files there that have not been used for a few days and Windows' Storage
    Sense clears it too, so a kept backup lost tables between updates. The
    one kept there before is moved over -- the same disk, so nothing is
    copied -- and anything that goes missing is noticed and unpacked again.
    """
    old = os.path.join(tempfile.gettempdir(), OLD_UNPACK)
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or \
            os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
        path = os.path.join(base, 'Billing', 'shree_unpack')
    elif sys.platform == 'darwin':
        path = os.path.expanduser('~/Library/Caches/Billing/shree_unpack')
    else:
        path = os.path.join(os.environ.get('XDG_CACHE_HOME')
                            or os.path.expanduser('~/.cache'),
                            'billing', 'shree_unpack')
    try:
        if not os.path.isdir(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.isdir(old):
                try:
                    os.replace(old, path)
                except OSError:
                    pass
        shree.private_dir(path)
        return path
    except OSError:
        return old


def _remove_tree(path, tries=5):
    """Delete a working folder. On Windows a virus scanner can still be
    reading a copy of the books a moment after it is closed; a working folder
    left behind each time would fill the disk with them."""
    for attempt in range(tries):
        shree.rmtree(path, passes=2)
        if not os.path.exists(path):
            return True
        time.sleep(0.5 * (attempt + 1))
    return False


def _clear_old_work(hours=6):
    """Working folders an earlier run could not remove."""
    base = tempfile.gettempdir()
    try:
        names = os.listdir(base)
    except OSError:
        return
    cut = time.time() - hours * 3600
    for name in names:
        full = os.path.join(base, name)
        if name.startswith(WORK_PREFIX) and os.path.isdir(full):
            try:
                if os.path.getmtime(full) < cut:
                    _remove_tree(full, tries=1)
            except OSError:
                pass


def _prune_backups():
    """The newest few copies, and the last of each of the past week's days."""
    import glob
    mine = sorted(f for f in glob.glob('%s.before-%s-*'
                                       % (glob.escape(db.DB_PATH), BACKUP_REASON))
                  if not f.endswith(('-wal', '-shm', '-journal')))
    keep = set(mine[-BACKUPS_KEPT:])
    by_day = {}
    for f in mine:
        m = re.search(r'-(\d{8})-\d{6}', f)
        if m:
            by_day[m.group(1)] = f
    keep.update(by_day[d] for d in sorted(by_day)[-DAYS_KEPT:])
    for old in mine:
        if old in keep:
            continue
        try:
            os.remove(old)
        except OSError:
            pass


MANAGER = Manager()


def overview(conn):
    """Everything the screen shows."""
    seed_rules(conn)
    sources = sources_of(conn)
    st = settings(conn)
    runs = []
    for r in conn.execute('SELECT id, started, finished, started_by, mode, '
                          'status, message, backup, repeats FROM sync_run '
                          'ORDER BY id DESC LIMIT 20'):
        runs.append(dict(r))
    folders = [describe(s) for s in sources]
    held = coverage(folders)
    missing = [dict(firm=f, book=BOOK_NAME[f]) for f in FIRMS if f not in held]
    # A firm the last update left as it was, and why -- the one thing on the
    # screen somebody has to decide about.
    left = {}
    if runs:
        row = conn.execute('SELECT summary FROM sync_run WHERE id=?',
                           (runs[0]['id'],)).fetchone()
        try:
            left = (json.loads(row['summary'] or '{}') or {}).get('held') or {}
        except (TypeError, ValueError):
            left = {}
        left = {f: dict(why=h.get('why') or [],
                        can_accept=bool(h.get('can_accept')),
                        removed=(h.get('removed') or [])[:50],
                        gone_total=h.get('gone_total') or
                        len(h.get('removed') or []),
                        run=runs[0]['id'])
                for f, h in left.items() if f in FIRMS}
    return dict(settings=st, folders=folders, runs=runs, held=left,
                coverage=held, missing=missing,
                rules=list_rules(conn), state=MANAGER.status(),
                last_update=db.get_meta(conn, 'shree_last_update') or '',
                has_run=bool(conn.execute(
                    "SELECT 1 FROM sync_run WHERE mode='apply' AND "
                    "status='done' LIMIT 1").fetchone()))


def find_doc(conn, firm, key):
    """A document named the way an update report names it, by the numbers on
    its paper -- ids do not survive an update, so a report cannot keep them."""
    parts = (key or '').split('|')
    cid = _cid(conn, firm)
    if len(parts) != 4 or not cid:
        raise api.ApiError('That document is not in the books.', 404)
    kind, fy, series, number = parts
    if kind in VOUCHER_KINDS:
        row = conn.execute(
            "SELECT id FROM voucher WHERE company_id=? AND kind=? AND fy=? "
            "AND TRIM(voucher_no)=? ORDER BY source='SHREE' LIMIT 1",
            (cid, kind, fy, number)).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM invoice WHERE company_id=? AND doc_type=? AND fy=? "
            "AND TRIM(COALESCE(series,''))=? AND TRIM(invoice_no)=? "
            "ORDER BY source='SHREE' LIMIT 1",
            (cid, kind, fy, series, number)).fetchone()
    if not row:
        raise api.ApiError('That document is not in the books any more.', 404)
    return dict(kind=kind, id=row['id'], company_id=cid)


def run_detail(conn, rid):
    row = conn.execute('SELECT * FROM sync_run WHERE id=?', (rid,)).fetchone()
    if not row:
        raise api.ApiError('That update is not on the list.', 404)
    d = dict(row)
    try:
        d['summary'] = json.loads(d['summary'] or '{}')
    except ValueError:
        d['summary'] = {}
    return d


# ----------------------------------------------------------------- CLI

def _step_main(a):
    conn = db.connect()
    try:
        if a.step == 'census':
            out = census(conn)
        elif a.step == 'rekind':
            import import_shree_full
            out = import_shree_full.rekind_cash_memos(conn)
            conn.commit()
        elif a.step == 'wipe':
            out = wipe_for_everything(conn, [f for f in (a.firms or '').split(',')
                                             if f in FIRMS])
        elif a.step == 'save-links':
            out = save_links(conn)
        elif a.step == 'restore-links':
            with open(a.inp, encoding='utf-8') as fh:
                out = restore_links(conn, json.load(fh))
        elif a.step == 'replay':
            out = replay(conn, a.phase)
        elif a.step == 'check':
            with open(a.inp, encoding='utf-8') as fh:
                before = json.load(fh)
            notes = []
            if a.notes:
                with open(a.notes, encoding='utf-8') as fh:
                    notes = json.load(fh)
            after = census(conn)
            # Bills rebuilt from their challans this run, by the keys the
            # census names them by.
            restated = set()
            if a.restated and os.path.isfile(a.restated):
                with open(a.restated, encoding='utf-8') as fh:
                    ids = [int(i) for i in json.load(fh)]
                for n in range(0, len(ids), 500):
                    chunk = ids[n:n + 500]
                    for r in conn.execute(
                            'SELECT doc_type, fy, series, invoice_no FROM '
                            'invoice WHERE id IN (%s)' % ','.join('?' * len(chunk)),
                            chunk):
                        restated.add(api.doc_key(r['doc_type'], r['fy'],
                                                 r['series'], r['invoice_no']))
            diff = None
            if a.before:
                # Everything this run wrote is in the file itself before the
                # two are laid side by side.
                conn.commit()
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                diff = book_diff(db.DB_PATH, a.before)
            out = compare(before, after, before.get('_faults', {}),
                          after.get('_faults', {}), notes + soundness(conn),
                          diff, [f for f in (a.accept or '').split(',') if f],
                          restated)
        else:
            raise SystemExit('unknown step %s' % a.step)
    finally:
        conn.close()
    with open(a.out, 'w', encoding='utf-8') as fh:
        fh.write(json.dumps(out))


def main(argv=None):
    db.quiet_windows_errors()
    # A SHREE name printed to a Windows console must not stop a run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--run', action='store_true',
                    help='update the books now (stop the app first)')
    ap.add_argument('--preview', action='store_true',
                    help='with --run: do everything but replace the books')
    ap.add_argument('--everything', action='store_true',
                    help='with --run: Fetch everything -- the books cleared '
                         'of everything made or decided here and every firm '
                         'read again as SHREE has it')
    ap.add_argument('--step')
    ap.add_argument('--phase')
    ap.add_argument('--in', dest='inp')
    ap.add_argument('--notes')
    ap.add_argument('--before')
    ap.add_argument('--out')
    ap.add_argument('--accept', help='firms whose refusal the user overruled')
    ap.add_argument('--firms', help='with --step wipe: the firms to clear')
    ap.add_argument('--restated', help='ids of the bills restate_bills rebuilt')
    a = ap.parse_args(argv)
    if a.step:
        return _step_main(a)
    db.init()
    conn = db.connect()
    try:
        seed_rules(conn)
        if a.status:
            o = overview(conn)
            print('SHREE folders:')
            for f in o['folders']:
                print('  %s %s' % ('ok     ' if f['found'] else 'MISSING', f['path']))
                for b in f['books']:
                    print('          %-5s %-14s to %s' % (b['firm'], BOOK_NAME[b['firm']],
                                                     b['upto'] or '-'))
            for m in o['missing']:
                print('  no folder holds the %s (%s)' % (m['book'], m['firm']))
            print('Last update: %s' % (o['last_update'] or 'never'))
            print('Kept decisions: %d' % len(o['rules']))
            for r in o['runs'][:5]:
                print('  %s  %-9s %-8s %s' % (r['started'], r['started_by'],
                                              r['status'], r['message']))
            return
    finally:
        conn.close()
    if a.run:
        import socket
        try:
            socket.create_connection(('127.0.0.1', 8800), 0.5).close()
            raise SystemExit('The app is running. Start the update from its '
                             'SHREE update screen, or stop the app first.')
        except OSError:
            pass
        MANAGER.start('preview' if a.preview else 'apply', 'command line',
                      everything=a.everything)
        shown = 0
        while True:
            time.sleep(1)
            st = MANAGER.state
            for line in st['log'][shown:]:
                print(line)
            shown = len(st['log'])
            if not st['running']:
                break
        print('\n%s: %s' % (st['result']['status'], st['result']['message']))


if __name__ == '__main__':
    main()
