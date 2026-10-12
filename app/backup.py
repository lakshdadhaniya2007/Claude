"""Everything, backed up at once (5 Oct 2026: "Add a option to backup
everything at once").

One .zip file that is the whole of Billing as it stands: the books -- every
firm, the personal accounts, the settings, the passcode and delete password
(kept as hashes) -- and the program that reads them. Unzipped on any PC it is
a Billing folder ready to run; to put only the books back, the one file
app/data/billing.db is copied into place.

The books are copied the way every other copy here is (api.copy_books), so
the copy is whole even while someone is saving, and it is checked
(PRAGMA quick_check) before it goes into the file. What is left out is what
is no use in a backup or made on the PC itself: the automatic copies taken
before a delete or an update, the log, Python's caches, update packages,
other .zip files (earlier backups saved into the folder would otherwise be
carried inside every later one) and Billing.exe, which install.bat builds.

SHREE's own data is not in it: that is SHREE's, on the accounts computer,
and Billing reads it again from there.
"""
import datetime
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile

import api
import db

APP = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.dirname(APP)                     # the Billing folder
DATA = os.path.join(APP, 'data')
LAST = os.path.join(os.path.dirname(db.DB_PATH), 'last-backup.json')
ROOT = 'Billing'                                # the folder inside the zip

SKIP_DIRS = {'__pycache__', 'pending-install', '.git', '.svn', '.idea',
             '.vscode', 'node_modules'}
SKIP_NAMES = {'Billing.exe', '.DS_Store', 'Thumbs.db', 'desktop.ini'}
SKIP_ENDINGS = ('.pyc', '.pyo', '.zip', '.tmp', '.log', '.pid')
BIG = 50 * 1024 * 1024          # a stray file this size is not the program

README = """Billing -- backup of everything
===============================

Made {when}.

What is in it
-------------
  app\\data\\billing.db   the books: {firms},
                        the personal accounts, the settings, the passcode
                        and delete password. Checked before it was packed.
  everything else       the Billing program as it was that day.

SHREE's own data is not here -- it stays in SHREE, on the accounts
computer, and Billing reads it again from there.

To put the books back on this PC
--------------------------------
  1. Double-click stop.bat in the Billing folder.
  2. Copy app\\data\\billing.db from this backup into the Billing folder's
     app\\data folder, replacing the file there. (Rename the one there
     first instead, if you want to keep it.)
  3. Open Billing.

To set Billing up on another PC
-------------------------------
  1. Install Python from python.org (tick "Add python.exe to PATH").
  2. Unzip this file anywhere -- it makes a folder called Billing.
  3. Double-click install.bat in that folder.
"""


def _counts(path):
    """What the copied books hold, firm by firm -- read off the copy itself,
    so the summary is of what was saved."""
    out = {'firms': [], 'personal': 0}
    c = sqlite3.connect(path)
    try:
        rows = c.execute('SELECT id, name, code FROM company ORDER BY id').fetchall()
        for cid, name, code in rows:
            docs = c.execute('SELECT COUNT(*) FROM invoice WHERE company_id=?',
                             (cid,)).fetchone()[0]
            vch = c.execute('SELECT COUNT(*) FROM voucher WHERE company_id=?',
                            (cid,)).fetchone()[0]
            out['firms'].append({'code': code or '', 'name': name,
                                 'documents': docs, 'vouchers': vch})
        try:
            out['personal'] = c.execute(
                'SELECT COUNT(*) FROM personal_entry').fetchone()[0]
        except sqlite3.Error:
            pass
    finally:
        c.close()
    return out


def _check(path):
    c = sqlite3.connect(path)
    try:
        got = c.execute('PRAGMA quick_check').fetchone()[0]
    finally:
        c.close()
    if got != 'ok':
        raise api.ApiError('The copy of the books did not check out (%s). '
                           'Nothing was saved -- try again.' % got, 500)


def _program_files():
    """(path on disk, name in the zip) for every file of the program."""
    data = os.path.normcase(os.path.abspath(DATA))
    for top, dirs, files in os.walk(HOME):
        here = os.path.normcase(os.path.abspath(top))
        # The books go in as their checked copy, never from the live folder.
        dirs[:] = sorted(d for d in dirs
                         if d not in SKIP_DIRS and not d.startswith('.')
                         and os.path.normcase(os.path.abspath(
                             os.path.join(top, d))) != data)
        if here == data:
            continue
        for f in sorted(files):
            if (f in SKIP_NAMES or f.endswith(SKIP_ENDINGS)
                    or '.before-' in f or f.startswith('~$')):
                continue
            full = os.path.join(top, f)
            try:
                if not os.path.isfile(full) or os.path.getsize(full) > BIG:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(full, HOME).replace(os.sep, '/')
            yield full, ROOT + '/' + rel


def make(conn):
    """The backup: (zip bytes, file name). Records when it was made."""
    now = datetime.datetime.now()
    name = 'Billing-backup-%s.zip' % now.strftime('%Y-%m-%d-%H%M')
    work = tempfile.mkdtemp(prefix='billing-backup-')
    try:
        books = os.path.join(work, 'billing.db')
        api.copy_books(conn, books)
        _check(books)
        held = _counts(books)
        firms = ', '.join('%s (%s)' % (f['name'], f['code']) if f['code']
                          else f['name'] for f in held['firms']) or 'every firm'
        packed = os.path.join(work, name)
        n = 0
        with zipfile.ZipFile(packed, 'w', zipfile.ZIP_DEFLATED,
                             compresslevel=6) as z:
            z.writestr(ROOT + '/RESTORE - read me.txt',
                       README.format(when=now.strftime('%d-%m-%Y at %H:%M'),
                                     firms=firms).replace('\n', '\r\n'))
            z.write(books, ROOT + '/app/data/billing.db')
            for full, arc in _program_files():
                z.write(full, arc)
                n += 1
            z.writestr(ROOT + '/backup-info.json', json.dumps({
                'made': now.isoformat(timespec='seconds'),
                'books_bytes': os.path.getsize(books),
                'program_files': n, 'held': held}, indent=1))
        with open(packed, 'rb') as f:
            blob = f.read()
        _remember({'made': now.isoformat(timespec='seconds'), 'name': name,
                   'bytes': len(blob), 'held': held})
        return blob, name
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _remember(info):
    """When the last backup was made, for the Settings screen. A small file
    beside the books, not a row in them: a backup changes nothing in the
    books, and writing there would make every screen work its answers out
    again."""
    tmp = LAST + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(info, f)
        os.replace(tmp, LAST)
    except OSError:
        pass


def last():
    """The last backup made, or nothing."""
    try:
        with open(LAST, encoding='utf-8') as f:
            info = json.load(f)
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}
