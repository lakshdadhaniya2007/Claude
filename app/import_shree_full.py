#!/usr/bin/env python3
"""Import a whole SHREE (FoxPro) installation -- every firm, every year.

    python3 app/import_shree_full.py --source "SHREE BST" --list
    python3 app/import_shree_full.py --source "SHREE BST" --all
    python3 app/import_shree_full.py --source "SHREE BST" --company BST

Where the data lives
--------------------
A SHREE installation keeps one folder per financial year, and inside it one
folder per firm:  SHREE/25-26/ESTI.  The nightly backup writes the same folders
out as ZIPs named <FIRM><DD><MM>.ZIP, and for BST and BSI those ZIPs are the
*only* copy left -- their year folders were emptied.  So both are read, the
ZIPs are unpacked to a work folder, and for each firm-and-year the fullest
snapshot wins (most ledger rows, latest date).

What comes across
-----------------
    CMASBACKUP  -> the firm itself (name, address, GSTIN, PAN, bank)
    HMAS        -> account groups
    AMAS        -> ledgers, opening balances, GSTINs, addresses
    DEPT        -> item categories and their GST rates
    IMAS        -> items, units, HSN, opening stock
    SALE + SITM -> sale bills and challans, with their lines  (and SHREE's
                   credit notes, filed here with a negative total)
    PRCH + SITM -> purchase bills, with their lines           (and returns)
    TMAS        -> receipts, payments and journals, and every ledger posting
    MMAS        -> the next document number for each book

Ledger postings are taken from TMAS verbatim rather than recomputed, so the
statements, trial balance and balance sheet agree with the old system to the
paisa.  They are still tagged with the document they belong to, so a line in a
ledger statement opens the bill behind it.

Years that were never backed up leave a hole -- BST has no 22-23 through 24-25
at all, and some snapshots stop mid-year.  Rather than let that quietly skew
every later balance, the opening balance SHREE itself recorded for each year is
compared with what the imported postings add up to, and the difference is
posted as one dated "carried forward" line.  Every year therefore opens on the
figure the old system opened on.

Login columns in the company master (GST and e-way portal usernames and
passwords) are deliberately never read.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import traceback
import zipfile
import zlib
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api   # noqa: E402
import db    # noqa: E402
import dbf   # noqa: E402

YEAR_RE = re.compile(r'^(\d{2})-(\d{2})')

# SHREE stores amounts credit-positive; this app stores them debit-positive.
CREDIT_POSITIVE = -1

# SHREE firm code -> how it lands here. The order firm keeps its own code so
# the existing books are updated rather than a second copy being made.
# Every firm here keeps two quite different books, on two different machines.
#
#   the accounts book   own bank accounts, receipts, payments, journals, and
#                       untaxed challans -- this is where the money is tracked
#   the bill register   customers, tax ledgers and GST invoices, and nothing
#                       else: no bank, no cash, no receipts
#
# A challan and the tax invoice raised from it are the same goods billed once,
# so the two books can never share a company here -- the turnover would count
# twice. They are kept as separate firms, the way the order firm BSTP has
# always been kept apart from BST and BSI.
#
# Keyed by (which kind of book, SHREE firm code). The same code means different
# things in the two places: PSI on the accounts machine is Patel Steel's own
# cash book, PSI on the billing machine is its GST invoice register.
FIRMS = {
    ('books', 'ESTI'): dict(code='BSTP', name='BST', tax_mode='none'),
    ('books', 'PSI'):  dict(code='PSIA', name='PATEL STEEL INDUSTRIES (Accounts)',
                            tax_mode='none'),
    ('books', 'BST'):  dict(code='BSTA',
                            name='BALKRISHANA STEEL TRADERS (Accounts)',
                            tax_mode='none'),
    ('books', 'VT'):   dict(code='VTA', name='VRAJ TRADERS (Accounts)',
                            tax_mode='none'),
    ('books', '0001'): dict(code='VTA', name='VRAJ TRADERS (Accounts)',
                            tax_mode='none'),
    ('books', '3'):    dict(code='GPE', name='THE GRAND PALACE EXP.',
                            tax_mode='none'),

    ('bills', 'BST'):  dict(code='BST', name='BALKRISHANA STEEL TRADERS',
                            tax_mode='gst'),
    ('bills', 'BSI'):  dict(code='BSI', name='BALKRISHNA STEEL INDUSTRIES',
                            tax_mode='gst'),
    ('bills', 'PSI'):  dict(code='PSI', name='PATEL STEEL INDUSTRIES',
                            tax_mode='gst'),
    # Vraj Traders changed its code as the years went by -- VT, then 0001,
    # then 004, then 7 -- but it is one firm on one PAN throughout.
    ('bills', 'VT'):   dict(code='VT', name='VRAJ TRADERS', tax_mode='gst'),
    ('bills', '0001'): dict(code='VT', name='VRAJ TRADERS', tax_mode='gst'),
    ('bills', '004'):  dict(code='VT', name='VRAJ TRADERS', tax_mode='gst'),
    ('bills', '7'):    dict(code='VT', name='VRAJ TRADERS', tax_mode='gst'),
}

# What each of the app's three firms was given by hand, past what SHREE's
# company master holds -- the jurisdiction and short name the printed bill
# carries, BST's mobile, BSTP's state and its note in place of sale terms --
# copied from the books on 6 Oct 2026. A firm takes them when an update first
# creates it: a new PC started from an empty folder printed bills without
# them. A firm that exists already keeps what it has.
NEW_FIRM_DETAILS = {
    'BSTP': dict(city='Rajkot', state='Gujarat', state_code='24',
                 jurisdiction='RAJKOT', short_name='BST',
                 terms='Purchase record only. No tax charged.'),
    'BST': dict(city='Rajkot', pin='360004', state='Gujarat', state_code='24',
                jurisdiction='RAJKOT', short_name='BST', mobile='9825093021'),
    'BSI': dict(city='Rajkot', state='Gujarat', state_code='24'),
}

# Settings a new book starts with, as chosen on the books it replaces: the
# e-way bill's document number written GT-188 (6 Oct 2026).
NEW_BOOK_META = {'eway_docno': 'hyphen'}

# The company master each firm's details are read from, since a firm's two
# books do not both carry them.
FIRM_MASTER = {'BSTP': 'ESTI', 'PSIA': 'PSI', 'BSTA': 'BST', 'VTA': '7',
               'GPE': '3', 'BST': 'BST', 'BSI': 'BSI', 'PSI': 'PSI', 'VT': '7'}

# Codes that appear in a folder name but are not firms of this business.
SKIP_FIRMS = {'DEMO', 'R', 'OLD', 'NBD', 'PT', 'VBD', 'G1', 'T2', 'S1',
              'E1', 'ES001', 'T101002', 'SHREEXP', 'ADDRESS', 'RUSTDESK',
              'OLDPSI'}

# A bill register has no money in it. Where more than this share of a
# snapshot's vouchers are receipts, payments or journals, it is an accounts
# book. In practice the two sit either side of a wide gap -- every bill
# register measured under 1%, every accounts book over 55% -- so nothing
# lands near the line.
MONEY_SHARE = 0.05

# Where the two halves of an entry cannot both be recovered, the odd half is
# balanced against this account rather than left to unbalance the books --
# the same thing Tally does with a difference in opening balances.
SUSPENSE_NAME = 'Difference in Opening Balances'

VOUCHER_KIND = {'Rcpt': 'RECEIPT', 'Pymt': 'PAYMENT', 'Jrnl': 'JOURNAL',
                'Ctra': 'CONTRA'}

# What a document is, by SHREE's EXT_VHTY. SHREE files its notes in SALE and
# PRCH beside the bills: a credit note (SCnt, e-invoiced as CRN) and a
# purchase return (PDnt) with a negative total, a debit note to a customer
# (SDnt) as a small positive bill. Kept that way here -- the sign is what the
# returns repair, the challan pairing, the GST summary and GSTR-1 all read.
# This app's own DEBIT is a purchase return, so SDnt is not one of those.
DOC_OF = {'Sale': 'SALE', 'SCnt': 'SALE', 'SDnt': 'SALE',
          'Prch': 'PURCHASE', 'PDnt': 'PURCHASE', 'PCnt': 'PURCHASE'}

# BTSALE / BTPRCH is not the kind of document at all but the memo it is
# printed as -- SHREE's own invoice format has
# IIF(SALE.BTSALE='C','CASH MEMO','DEBIT MEMO'). A cash bill posts to CASH IN
# HAND rather than the customer, and the IRP has it as an invoice (GT134 of
# 2026-27, "Typ": "INV"). It was once read as a credit note; see
# rekind_cash_memos for the rows that reading left behind.
MEMO_OF = {'C': 'Cash', 'D': 'Debit'}


def r2(x):
    return round(float(x or 0) + 0.0, 2)


def half_up(x, places=2):
    """To the paisa (or gram) the way SHREE rounds: half away from nought.
    Python's round() goes to the even figure, so a line tax of 6,760.125
    became 6,760.12 where SHREE's ledger says 6,760.13 -- 359 BST and BSI
    bill headers a paisa short on CGST and SGST (2 Oct 2026)."""
    return api.gstcalc.money(x, places)


def norm(name):
    """Ledgers and items are matched between years by their name alone."""
    return re.sub(r'\s+', ' ', (name or '').strip()).upper()


# SHREE names its year folders "25-26"; the app writes "2025-26" everywhere,
# and the two have to agree or a bill saved here cannot find the ones imported
# before it and starts numbering again from one.
def fy_label(date_str):
    """'2025-06-01' -> '2025-26'."""
    y, m = int(date_str[:4]), int(date_str[5:7])
    if m < 4:
        y -= 1
    return '%d-%02d' % (y, (y + 1) % 100)


def fy_full(fy):
    """A folder's '25-26' as the app writes it, '2025-26'."""
    fy = (fy or '').strip()
    return ('20' + fy) if YEAR_RE.match(fy) and len(fy) == 5 else fy


def fy_year(fy):
    """The April this financial year opens in, from either spelling."""
    head = fy.split('-')[0]
    return int(head) if len(head) == 4 else 2000 + int(head)


def fy_start(fy):
    """'2025-26' -> '2025-04-01'."""
    return '%d-04-01' % fy_year(fy)


def fy_next(fy):
    y = fy_year(fy) + 1
    return '%d-%02d' % (y, (y + 1) % 100)


def nature_for(group_code):
    g = (group_code or '').strip()
    if g.startswith('2') or g in ('4001', '5001', '9001'):
        return 'asset'
    if g in ('8201', '8401'):
        return 'income'
    if g.startswith('81') or g in ('8301', '8001'):
        return 'expense'
    return 'liability'


# ------------------------------------------------------------------ screens

def screen_labels(folder, book):
    """Caption of each F1..F60 box on a SHREE entry screen.

    SHREE lets every firm lay its own bill screen out, so which numbered box
    holds CGST, which holds Cutting and which holds Round Off differs between
    BST, BSI and the order firm. The layout is stored in <book>.FKY as a run of
    field descriptors; every sixteen-character one is the caption of the next
    numbered box, so counting them off gives the mapping.
    """
    path = dbf.find(folder, 'f%s.fky' % book.lower())
    if not path:
        return {}
    try:
        data = dbf.read_bytes(path)
    except dbf.Busy:
        raise                   # read wrongly, every charge would be misfiled
    except OSError:
        return {}
    tag = ('_F%s' % book.upper()).encode()
    marks = [m.start() for m in re.finditer(re.escape(tag + b'\x00' * 5), data)]
    out, n = {}, 0
    for i, p in enumerate(marks):
        j = p + len(tag) + 5
        if j + 6 > len(data):
            break
        typ, ln = chr(data[j]), data[j + 5]
        if typ != 'C' or ln != 16:
            continue
        n += 1
        end = (marks[i + 1] if i + 1 < len(marks) else len(data)) - len(tag) - 1
        val = data[max(j, end - ln):end].decode('latin-1')
        val = val.replace('\x00', ' ').strip()
        if val:
            out[n] = val
    return out


# What each money box on a bill screen means. SHREE has been in use since
# before GST, so the older years carry VAT, CST, excise and the education
# cesses instead. They are kept in the nearest GST column so the historical
# registers still add up: state taxes under SGST, inter-state under IGST,
# central levies under CGST, and every cess under Cess. Captions are stored
# truncated, so these are matched as prefixes.
ROLE_RULES = [
    (('CGST',), 'cgst'),
    (('SGST',), 'sgst'),
    (('IGST',), 'igst'),
    (('VAT',), 'sgst'),                 # VAT and its additional tax
    (('CST',), 'igst'),                 # central sales tax, i.e. inter-state
    (('EXCISE', 'EXCISE DU', 'CEN. EXCI', 'CEN.EXCI', 'CENVAT', 'CAPITAL E',
      'SERVICE T', 'TCS'), 'cgst'),
    (('EDU', 'E.CESS', 'S&H', 'CESS', 'CAP.EDU', 'CAP.S&H', 'CEN.S&H'), 'cess'),
    (('ROUND',), 'round_off'),
    (('CUTTING',), 'cutting'),
    (('TRANSPORT', 'TRANSPOTA', 'FREIGHT'), 'transport'),
    (('LOADING',), 'loading'),
    # Packing, forwarding, commission and the like have no column of their own;
    # they join loading, which is already this trade's catch-all handling line.
    (('P & F', 'P&F', 'PACKING', 'OTHER CHA', 'OTHERS', 'COMMISION',
      'MAJURI'), 'loading'),
    (('DISCOUNT', 'LESS DISC'), 'discount'),
]

TAX_ROLES = ('cgst', 'sgst', 'igst', 'cess')
CHARGE_ROLES = ('cutting', 'transport', 'loading')


def charge_map(folder, book):
    """{F-number: role} for the money boxes on a bill screen."""
    roles = {}
    for n, cap in screen_labels(folder, book).items():
        c = cap.upper().replace('.', '.').strip()
        c = re.sub(r'\s+', ' ', c)
        for prefixes, role in ROLE_RULES:
            if any(c.startswith(px) for px in prefixes):
                roles[n] = role
                break
    return roles


def fnum(row, book, n):
    """The numeric value of box F<n><BOOK>, which SHREE stores as text."""
    try:
        return float((row.get('F%d%s' % (n, book)) or '').strip())
    except (TypeError, ValueError):
        return 0.0


# The words on a bill screen, found the way the money boxes are: by caption.
# The challan screen keeps the driver in box 3 and the payment terms in box 4;
# the GST bill screens keep the transporter in box 2, the L.R. number and date
# in 3 and 4, and the payment terms in 7. Read by number, every GST bill came
# in with "/  /" -- its empty L.R. date -- for payment terms and no
# transporter at all.
TEXT_RULES = [
    (('DRIVER',), 'driver_name'),
    (('PAYMENT', 'PAYMNT', 'TERMS'), 'payment_terms'),
    (('TRANSPORT', 'TRANSPOTA'), 'transporter'),
    (('VEHICLE',), 'vehicle_no'),
    (('L.R. NO', 'L.R.NO', 'LR NO', 'L R NO', 'LR.NO'), 'lr_no'),
    (('L.R. DATE', 'L.R.DATE', 'LR DATE', 'L R DATE', 'LR.DATE'), 'lr_date'),
    (('CASES',), 'cases'),
    (('E-WAY', 'EWAY', 'E WAY'), 'eway_no'),
]
# Where the old screens put them, for a year whose layout file is missing.
TEXT_FALLBACK = {'SALE': {'driver_name': [3], 'payment_terms': [4, 7],
                          'vehicle_no': [5]}}
TEXT_WIDTH = dict(driver_name=60, payment_terms=60, transporter=60,
                  vehicle_no=20, lr_no=30, lr_date=10, cases=20, eway_no=20)
BLANK_BOX = re.compile(r'^[\s/.0-]*$')
DATE_BOX = re.compile(r'^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})$')


def box_text(row, book, n):
    """The words in box F<n><BOOK>, or '' for an empty one -- SHREE writes
    an empty date as "/  /" and an empty amount as "0.00"."""
    val = str(row.get('F%d%s' % (n, book)) or '').strip()
    return '' if BLANK_BOX.match(val) else val


def iso_date(val):
    """'11/07/26' -> '2026-07-11', as the app keeps dates; '' if not one."""
    m = DATE_BOX.match((val or '').strip())
    if not m:
        return ''
    d, mo, y = (int(x) for x in m.groups())
    y += 2000 if y < 100 else 0
    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return ''


def text_map(folder, book, rows):
    """{role: [F-number]} for the boxes on a bill screen that hold words.

    A caption alone is not enough: "Transport" is a charge on the challan
    screen and the transporter's name on the bill screen, and captions past
    the first numeric box drift from their numbers. So a box is taken only
    where what is in it fits -- names and terms that are not amounts, dates
    that are dates.
    """
    roles = {}
    for n, cap in sorted(screen_labels(folder, book).items()):
        c = re.sub(r'\s+', ' ', cap.upper().strip())
        role = next((r for prefixes, r in TEXT_RULES
                     if any(c.startswith(px) for px in prefixes)), None)
        if not role or role in roles:
            continue
        vals = [v for v in (box_text(r, book, n) for r in rows) if v]
        if vals:
            numeric = sum(1 for v in vals if _is_amount(v)) / float(len(vals))
            if role == 'lr_date':
                dated = sum(1 for v in vals if iso_date(v)) / float(len(vals))
                if dated < 0.5:
                    continue
            elif role not in ('lr_no', 'cases', 'eway_no') and numeric >= 0.5:
                continue
        roles[role] = [n]
    if not roles:
        roles = dict(TEXT_FALLBACK.get(book, {}))
    return roles


def _is_amount(val):
    try:
        float(val)
        return True
    except ValueError:
        return False


def header_text(row, book, texts):
    """{role: text} for one bill, from the boxes text_map found."""
    out = {}
    for role, boxes in texts.items():
        val = next((v for v in (box_text(row, book, n) for n in boxes) if v), '')
        if role == 'lr_date':
            val = iso_date(val)
        out[role] = val[:TEXT_WIDTH[role]]
    return out


# ------------------------------------------------------------------ sources

class Source(object):
    __slots__ = ('firm', 'role', 'fy', 'folder', 'rows', 'last', 'origin',
                 'upto', 'saved', 'own')

    def __init__(self, firm, role, fy, folder, rows, last, origin, upto='',
                 saved=''):
        self.firm, self.role, self.fy = firm, role, fy
        self.folder, self.rows, self.last, self.origin = folder, rows, last, origin
        # The newest entry not dated in the future -- a typing slip dated
        # 2027 is in the books, and must not read as how far they run.
        self.upto = upto
        # The day SHREE last wrote the ledger, from the table's own header.
        self.saved = saved
        # SHREE's own year folder -- 26-27\ESTI -- rather than a copy of it
        # or a backup: the one SHREE writes to.
        self.own = False

    @property
    def target(self):
        """The app company this snapshot belongs in, or None."""
        spec = FIRMS.get((self.role, self.firm))
        return spec['code'] if spec else None

    def __repr__(self):
        return '<%s %s %s %d rows %s>' % (self.firm, self.role, self.fy,
                                          self.rows, self.origin)


def _ival(v):
    try:
        return int(float(str(v).strip() or 0))
    except (TypeError, ValueError):
        return 0


# What each snapshot was found to hold, kept in the work folder between runs.
# Three hundred snapshots are looked at on every run and all but the day's
# own are years old, so each is read once and remembered by its files' size
# and time -- and the day, since how far a year runs is counted up to today.
PROBES = {}
PROBES_FILE = 'probes.json'


def load_probes(work):
    import json
    try:
        with open(os.path.join(work, PROBES_FILE), encoding='utf-8') as fh:
            PROBES.update(json.load(fh))
    except (OSError, ValueError):
        pass


def save_probes(work):
    import json
    for key in [k for k in PROBES if not os.path.isdir(k.split('|')[0])]:
        del PROBES[key]
    try:
        with open(os.path.join(work, PROBES_FILE), 'w', encoding='utf-8') as fh:
            json.dump(PROBES, fh)
    except OSError:
        pass


def _sig(path):
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _probe(folder, firm_hint, origin, fy_hint='', fresh=False):
    """What a candidate folder holds, remembered while its files stand still.

    `fresh` reads it again however still its files look: the year SHREE is
    writing to. A table SHREE keeps open all day is written in place, and
    Windows moves its modified time only when SHREE closes it -- a challan
    deleted or a receipt corrected there left size and time as they were.
    """
    apath = dbf.find(folder, 'AMAS.DBF')
    if not apath:
        return None
    tpath = dbf.find(folder, 'TMAS.DBF')
    key = '|'.join((os.path.abspath(folder), firm_hint, fy_hint or ''))
    sig = [_sig(apath), _sig(tpath), datetime.date.today().isoformat()]
    hit = PROBES.get(key)
    if hit and hit[0] == sig and (not hit[1] or len(hit[1]) == 6) \
            and not fresh:
        v = hit[1]
        return Source(firm_hint, v[0], v[1], folder, v[2], v[3], origin,
                      v[4], _century(v[5])) if v else None
    try:
        src = _probe_files(folder, firm_hint, origin, fy_hint, apath, tpath)
    except dbf.Busy as e:
        # SHREE has the year open to itself. What was read last time stands
        # until it lets go. With nothing read before -- the first update on a
        # computer -- the folder is passed over this time and noted: its
        # backups still count, and an update that stopped here fetched
        # nothing at all, for any firm.
        if hit and hit[1] and len(hit[1]) == 6:
            STALE.append((folder, str(e)))
            v = hit[1]
            return Source(firm_hint, v[0], v[1], folder, v[2], v[3], origin,
                          v[4], _century(v[5]))
        BUSY.append((folder, firm_hint, fy_hint, str(e)))
        return None
    PROBES[key] = [sig, [src.role, src.fy, src.rows, src.last, src.upto,
                         src.saved] if src else None]
    return src


def _saved_on(path):
    """The last-update date a DBF header carries, as yyyy-mm-dd."""
    try:
        with open(path, 'rb') as fh:
            h = fh.read(4)
        return '%04d-%02d-%02d' % (_header_year(h[1]), h[2], h[3])
    except (OSError, IndexError):
        return ''


def _header_year(yy):
    """SHREE's FoxPro writes the header year as two digits -- 26 for 2026 --
    where the format means years since 1900. Read as written, every table
    was saved in the 1920s, and no date could be set against the books'."""
    return 1900 + yy if yy >= 80 else 2000 + yy


def _century(day):
    """A header date remembered before _header_year: '1926-09-18' is 2026."""
    if day and day[:2] == '19' and day[2:4].isdigit() and int(day[2:4]) < 80:
        return '20' + day[2:]
    return day or ''


def _probe_files(folder, firm_hint, origin, fy_hint, apath, tpath):
    """Work out what a candidate folder holds, or None if it holds no books.

    Two things are decided here. The year, which a folder named after its year
    states outright and which otherwise comes from the commonest date in the
    file -- a handful of vouchers were typed with the wrong year, 2215 and 2096
    both turn up, so the earliest date cannot be trusted. And which of the two
    kinds of book this is, which is read off the vouchers themselves rather
    than assumed from where the folder sits: the nightly backups moved copies
    of the billing machine's registers onto the accounts machine, so the folder
    a snapshot is found in says nothing about what is inside it.
    """
    # One folder's ledger master has a wrecked header and reads back as rows
    # with no fields at all. Without names nothing can be matched to it, so the
    # snapshot is passed over and the year's opening balance is carried in.
    try:
        sample = dbf.read(apath, limit=40, fields=('ANAM',))
    except dbf.Busy:
        raise
    except Exception:
        return None
    if not any((r.get('ANAM') or '').strip() for r in sample):
        return None

    dates, rows, money, total = [], 0, 0, 0
    if tpath:
        try:
            recs = dbf.read(tpath, fields=('DATE', 'VHTY'))
            rows = len(recs)
            for r in recs:
                d = r.get('DATE')
                if d:
                    dates.append(d)
                if (r.get('VHTY') or '').strip() in VOUCHER_KIND:
                    money += 1
                total += 1
        except dbf.Busy:
            raise
        except Exception:
            return None
    dates = sorted(d for d in dates if '1995' <= d[:4] <= '2035')

    fy = fy_hint if YEAR_RE.match(fy_hint or '') else ''
    if not fy and dates:
        tally = defaultdict(int)
        for d in dates:
            tally[fy_label(d)] += 1
        fy = max(tally, key=lambda k: (tally[k], k))
    if not fy:
        return None

    role = 'books' if (total and money / float(total) > MONEY_SHARE) else 'bills'
    if not total:
        # Nothing posted yet. A bank account still marks it as an accounts book.
        try:
            role = 'books' if any(_ival(r.get('HCOD')) == 2201
                                  for r in dbf.read(apath, fields=('HCOD',))) \
                else 'bills'
        except Exception:
            role = 'bills'

    # Only dates inside the year the books belong to say how far they run.
    fy = fy_full(fy)
    own = [d for d in dates if fy_label(d) == fy]
    today = datetime.date.today().isoformat()
    past = [d for d in own if d <= today]
    return Source(firm_hint, role, fy, folder, rows, own[-1] if own else '',
                  origin, past[-1] if past else '',
                  _saved_on(tpath or apath))


# ------------------------------------------------------ where SHREE lives
#
# SHREE runs from <drive>:\SHREE -- its shortcut points at F:\SHREE\shree.exe
# and its batch file does `cd\shree` -- and it writes its backups beside
# itself on the same drive: SHREEBACKUP\, SHREEBACKUP.zip, Backup.zip,
# backup\. The older years of the accounts book survive only in those, so a
# source is the installation *and* what sits next to it.
#
# The two folders beside this app, `SHREE BST` and `SHREE BILL`, are copies
# of those two drives; a source may be named either way -- the drive (or any
# folder holding SHREE), the SHREE folder itself, or shree.exe.
BESIDE_RE = re.compile(r'(?i)shree|backup')


def has_years(folder):
    """True if `folder` is a SHREE installation: 25-26\\ESTI\\AMAS.DBF."""
    try:
        names = sorted(os.listdir(folder), reverse=True)
    except OSError:
        return False
    for entry in names:
        if not YEAR_RE.match(entry):
            continue
        ydir = os.path.join(folder, entry)
        try:
            subs = os.listdir(ydir)
        except OSError:
            continue
        for sub in subs:
            if os.path.isdir(os.path.join(ydir, sub)) and \
                    dbf.find(os.path.join(ydir, sub), 'AMAS.DBF'):
                return True
    return False


def shree_home(path):
    """The SHREE installation a named folder stands for, or ''."""
    if not path:
        return ''
    path = os.path.abspath(path)
    if os.path.isfile(path):
        path = os.path.dirname(path)
    if has_years(path):
        return path
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return ''
    for name in names:
        cand = os.path.join(path, name)
        if name.upper() == 'SHREE' and has_years(cand):
            return cand
    return ''


def source_places(root):
    """(installation, [folders and archives its backups may be in]).

    Only what is named like SHREE or a backup is looked at beside the
    installation -- the drive it sits on can hold anything else.
    """
    home = shree_home(root) or os.path.abspath(root)
    base = os.path.dirname(home)
    places = [home]
    if base and base != home:
        try:
            names = sorted(os.listdir(base))
        except OSError:
            names = []
        for name in names:
            full = os.path.join(base, name)
            if full == home or not BESIDE_RE.search(name):
                continue
            if os.path.isdir(full) or name.lower().endswith('.zip'):
                places.append(full)
    return home, places


# Fetch everything (--deep): every year is looked into, copied by its
# contents and checked against SHREE -- not only this year and last.
DEEP = []


def recent_years(today=None):
    """This financial year and the one before, '26-27' and '25-26': the ones
    SHREE is still writing to -- last year's stays open until its accounts
    are closed and carried forward."""
    y = fy_year(fy_label(today or datetime.date.today().isoformat()))
    return {'%02d-%02d' % (y % 100, (y + 1) % 100),
            '%02d-%02d' % ((y - 1) % 100, y % 100)}


def scan_year_folders(root, label=''):
    """<root>/SHREE/<FY>/<FIRM> -- an installation as it stands today."""
    out = []
    base = os.path.join(root, 'SHREE')
    if has_years(root) or not os.path.isdir(base):
        base = root
    recent = recent_years()
    for entry in sorted(os.listdir(base)):
        ydir = os.path.join(base, entry)
        m = YEAR_RE.match(entry)
        if not m or not os.path.isdir(ydir):
            continue
        for sub in sorted(os.listdir(ydir)):
            folder = os.path.join(ydir, sub)
            if not os.path.isdir(folder):
                continue
            firm = sub.split(' ')[0].split('_')[0].split('-')[0].upper()
            if firm in SKIP_FIRMS:
                continue
            # 26-27\ESTI itself; "26-27 - Copy" or "ESTI_11.10.25" are copies.
            own = entry == m.group(0) and sub.upper() == firm
            src = _probe(folder, firm, '%s %s/%s' % (label, entry, sub),
                         fy_hint=m.group(0),
                         fresh=own and (m.group(0) in recent or bool(DEEP)))
            if src and src.target:
                src.own = own
                out.append(src)
    return out


# Every unpacked folder this run used, so the ones no archive stands behind
# any more can be cleared out of a work folder kept between runs.
UNPACKED = set()


# A backup of a firm's year is a few megabytes. An archive that would unpack
# to more than this, or into thousands of files, is not one of SHREE's, and
# is left alone rather than allowed to fill the disk.
UNPACK_BYTES = 2 * 1024 ** 3
UNPACK_FILES = 20000


def _check_archive(zf):
    infos = zf.infolist()
    if len(infos) > UNPACK_FILES or \
            sum(i.file_size for i in infos) > UNPACK_BYTES:
        raise ValueError('too large to be a SHREE backup')


def private_dir(path):
    """A folder of copies of the books, readable by this account only."""
    os.makedirs(path, exist_ok=True)
    if os.name != 'nt':                 # a Windows temp folder is already
        try:                            # the user's own
            os.chmod(path, 0o700)
        except OSError:
            pass


def walk_files(place):
    """(path, size, mtime_ns) for every file under `place`, in the order
    os.walk with sorted names would give them.

    The size and time come with the folder listing itself -- on Windows at no
    cost, and over a network share without a round trip per file. Backups
    and archives are never open.
    """
    try:
        entries = sorted(os.scandir(place), key=lambda e: e.name)
    except OSError:
        return
    subdirs = []
    for e in entries:
        try:
            if e.is_dir():
                if not e.is_symlink():
                    subdirs.append(e.path)
                continue
            st = e.stat()
        except OSError:
            continue
        yield e.path, st.st_size, st.st_mtime_ns
    for d in subdirs:
        for item in walk_files(d):
            yield item


def survey(places):
    """The backup archives and company masters in a source's places, found in
    one walk -- a network share is listed once per update, not twice."""
    zips, masters = [], []
    for place in places:
        if os.path.isfile(place):
            if place.lower().endswith('.zip'):
                zips.append(place)
            continue
        for path, size, mtime in walk_files(place):
            low = os.path.basename(path).lower()
            if low.endswith('.zip'):
                zips.append(path)
            elif low in ('cmas.dbf', 'cmasbackup.dbf'):
                masters.append((path, size, mtime))
    return zips, masters


def rmtree(path, passes=8):
    """Delete a folder, clearing the read-only mark Windows leaves on files
    copied off a CD or out of an old backup -- rmtree alone gives up on those
    and leaves the folder half there. A folder marked read-only (a Mac, a
    copied share) is opened up the same way; each pass reaches one level
    further in."""
    def loosen(_func, target, _exc):
        for p in (os.path.dirname(target), target):
            try:
                mode = os.lstat(p).st_mode
                if stat.S_ISLNK(mode):
                    continue            # never change what a link points at
                os.chmod(p, mode | (stat.S_IRWXU if stat.S_ISDIR(mode)
                                    else stat.S_IWRITE))
            except OSError:
                pass
    for _ in range(passes):
        if not os.path.lexists(path):
            return
        if sys.version_info >= (3, 12):     # onerror is deprecated there
            shutil.rmtree(path, onexc=loosen)
        else:
            shutil.rmtree(path, onerror=loosen)


def replace(src, dst, tries=6):
    """os.replace, waited out while a virus scanner on Windows still has the
    file or folder open a moment after it was written."""
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.25 * (attempt + 1))


# What an unpacked archive should hold -- every file and its size -- is
# written beside the files, and checked at every update. Before 17 Sep 2026 a
# folder that existed was trusted, so an unpack cut short (the app closed
# during a first update, a full disk, a file a virus scanner held) was read
# as the whole backup at every update after. Checking once was not enough
# either: the system clears old files out of folders like this one on its
# own -- macOS after a few days unused, Windows' Storage Sense -- and a
# backup with a table gone reads as a backup without that table.
UNPACKED_MARK = '.unpacked-ok'
LEFT_OUT = (UNPACKED_MARK, '.unreadable')


def _listing(dest):
    """{path inside `dest`: size} for every file under it. A folder listing
    carries the sizes on Windows, so this costs next to nothing there."""
    out, stack = {}, ['']
    while stack:
        rel = stack.pop()
        try:
            with os.scandir(os.path.join(dest, rel) if rel else dest) as it:
                for e in it:
                    r = os.path.join(rel, e.name) if rel else e.name
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(r)
                        elif r not in LEFT_OUT:
                            out[r] = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return out


def _needed(member):
    """True for what an update reads out of a backup: the tables of a
    snapshot at its top or one folder down -- the only places scan_zips
    looks -- and every archive inside it. SHREE's two whole-installation
    backups hold 10,000 files each, and a first update on a new computer
    unpacked 52,000 files (5.5 GB) where it reads 10,500; on Windows every
    one of them is a file the virus scanner reads as it is written."""
    if member.is_dir():
        return False
    low = member.filename.lower()
    if low.endswith('.zip'):
        return True
    parts = [p for p in member.filename.replace('\\', '/').split('/') if p]
    return len(parts) <= 2 and low.endswith(MIRROR_EXT)


def _expected(zf):
    """{path inside the unpacked folder: size} an archive unpacks to."""
    out = {}
    for i in zf.infolist():
        name = _disk_name(i.filename) if _needed(i) else ''
        if name:
            out[name] = i.file_size
    return out


def _unpacked_whole(dest, path):
    """True when `dest` still holds every file of the archive at its full
    size. A folder unpacked before the list was kept is checked against the
    archive itself, once, and the list written."""
    want = None
    try:
        with open(os.path.join(dest, UNPACKED_MARK), encoding='utf-8') as fh:
            want = json.load(fh)
    except (OSError, ValueError):
        pass
    fresh = not isinstance(want, dict) or not want
    if fresh:
        try:
            with zipfile.ZipFile(path) as zf:
                want = _expected(zf)
        except Exception:                               # noqa: BLE001
            return False
    have = _listing(dest)
    if any(have.get(k) != v for k, v in want.items()):
        return False
    if fresh:
        _mark(dest, want)
    return True


def _disk_name(name):
    """Where zipfile puts an archive member when it unpacks it: the same
    splitting, and on Windows the same replacing of characters a file name
    there cannot hold."""
    arc = name.replace('/', os.path.sep)
    if os.path.altsep:
        arc = arc.replace(os.path.altsep, os.path.sep)
    arc = os.path.splitdrive(arc)[1]
    arc = os.path.sep.join(x for x in arc.split(os.path.sep)
                           if x not in ('', os.path.curdir, os.path.pardir))
    if os.path.sep == '\\':
        clean = getattr(zipfile.ZipFile, '_sanitize_windows_name', None)
        if clean:
            arc = clean(arc, os.path.sep)
    return arc


def _mark(dest, want):
    try:
        with open(os.path.join(dest, UNPACKED_MARK), 'w',
                  encoding='utf-8') as fh:
            json.dump(want, fh)
    except OSError:
        pass


def unpack(path, dest):
    """Unpack an archive whole or not at all: into a folder of its own,
    moved into place only once every file is out. Answers the folder to
    read -- `dest`, or the finished folder itself when Windows will not let
    it be moved yet (a virus scanner reading what was just written); it is
    whole either way, and is moved into place at the next update."""
    part = dest + '.part'
    rmtree(part)
    os.makedirs(part)
    try:
        with zipfile.ZipFile(path) as zf:
            _check_archive(zf)
            zf.extractall(part, members=[i for i in zf.infolist()
                                         if _needed(i)])
            want = _expected(zf)
        _mark(part, want)
    except BaseException:
        rmtree(part)
        raise
    rmtree(dest)
    try:
        replace(part, dest)
    except OSError:
        return part
    return dest


# ------------------------------------------------------------ local copies
#
# Every update reads every year SHREE has kept, and SHREE's folders are often
# on another computer. So each year that is read is first brought up to date
# in a copy on this computer -- only the files that changed since the last
# update cross the network -- and read from there. The copy is also what is
# read while SHREE holds a table to itself: a year is taken whole or not at
# all, so it is never read half old and half new.
MIRROR_EXT = ('.dbf', '.fpt', '.fky')
MIRROR_DIR = 'mirror'
MANIFEST = 'copied.json'
MIRRORED = set()
# (folder, why) where what was read last time stood in for SHREE's own files.
STALE = []
# (folder, firm, year, why) that SHREE held to itself with nothing read before
# to stand in -- passed over this time.
BUSY = []


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def mirror(folder, work, verify=False):
    """The local copy of one snapshot folder, brought up to date.

    A file is copied again when its size or time has moved -- or, with
    `verify`, when its contents have, whatever its size and time say. That
    is for the years SHREE is writing to: it keeps their tables open all
    day and writes into them in place, and Windows moves a file's modified
    time only once SHREE closes it. A challan deleted or a receipt
    corrected in SHREE that morning left both as they were, and the update
    said "Up to date" over last night's copy.
    """
    dest = os.path.join(work, MIRROR_DIR, hashlib.sha1(
        _norm(folder).encode('utf-8')).hexdigest()[:16])
    MIRRORED.add(_norm(dest))
    try:
        with open(os.path.join(dest, MANIFEST), encoding='utf-8') as fh:
            copied = json.load(fh)
    except (OSError, ValueError):
        copied = {}
    want, got, side = {}, {}, set()
    try:
        for e in os.scandir(folder):
            if e.name.lower().endswith(MIRROR_EXT):
                # Asked of the file itself: SHREE keeps these open, and the
                # folder listing can lag behind what it has written.
                st = os.stat(e.path)
                want[e.name] = [st.st_size, st.st_mtime_ns]
        if verify:
            for n in want:
                data = _take(os.path.join(folder, n), n)
                if data is None:
                    side.add(n)
                    continue
                got[n] = data
                want[n] = [len(data), want[n][1],
                           hashlib.sha1(data).hexdigest()]
    except dbf.Busy as e:
        if copied:
            STALE.append((folder, str(e)))
            return dest
        raise
    except OSError:
        if copied:
            STALE.append((folder, 'the folder could not be read'))
            return dest
        raise
    # A copy cleared out of the local folder since (see UNPACKED_MARK) is
    # copied again, not taken on the word of the list. A list written
    # without contents is matched on size and time alone.
    have = _listing(dest) if copied else {}
    changed = []
    for n, sig in want.items():
        prev = copied.get(n) or []
        if n in side:
            continue
        if have.get(n) != sig[0]:
            changed.append(n)
        elif not verify:
            if prev[:2] != sig:
                changed.append(n)
        elif len(prev) == 3:
            if prev[2] != sig[2]:
                changed.append(n)
        elif not _same_bytes(os.path.join(dest, n), got[n]):
            changed.append(n)       # a list from before contents were noted
    gone = [n for n in copied if n not in want]
    stage = dest + '.part'
    if changed or gone:
        rmtree(stage)
        os.makedirs(stage)
    try:
        for n in changed:
            data = got.get(n)
            if data is None:
                data = _take(os.path.join(folder, n), n)
            if data is None:
                side.add(n)
                continue
            with open(os.path.join(stage, n), 'wb') as fh:
                fh.write(data)
    except dbf.Busy as e:
        rmtree(stage)
        if copied:
            STALE.append((folder, str(e)))
            return dest
        raise
    # A file the import never reads (SHREE's macros, its report list) that
    # SHREE is holding stays as the last copy had it; it does not hold the
    # year back.
    changed = [n for n in changed if n not in side]
    for n in side:
        if n in copied:
            want[n] = copied[n]
        else:
            want.pop(n, None)
    os.makedirs(dest, exist_ok=True)
    if not changed and not gone:
        rmtree(stage)
        if want != copied:
            _write_manifest(dest, want)
        return dest
    try:
        for n in changed:
            replace(os.path.join(stage, n), os.path.join(dest, n))
    except OSError:
        # The copy is held by another program (on Windows, a virus scanner)
        # and cannot be brought up to date now. SHREE's own folder is read
        # this once instead; the list of what was copied is left as it was,
        # so the next update copies these files again.
        rmtree(stage)
        return folder
    for n in gone:
        try:
            os.remove(os.path.join(dest, n))
        except OSError:
            pass
    _write_manifest(dest, want)
    rmtree(stage)
    return dest


def _stand_in(src, live, sources, unpacked, stood_in):
    """What is read for a year SHREE held to itself while it was being
    copied: the copy made at the last update, or SHREE's own backup of the
    year when that is more current -- SHREE writes one most days. Neither
    there, the firm is passed over (dbf.Busy goes up)."""
    kept = None
    if src.folder:
        kept = _probe(src.folder, src.firm, src.origin, fy_hint=src.fy[2:],
                      fresh=True)
    backups = [s for s in sources if s.target == src.target
               and s.fy == src.fy and _norm(s.folder).startswith(unpacked)]
    alt = max(backups, key=_newer) if backups else None
    if alt and (kept is None or _newer(alt) > _newer(kept)):
        stood_in[live] = ('read from SHREE\'s backup %s instead, to %s'
                          % (alt.origin.split(' ')[-1], alt.upto or '-'))
        return alt
    if kept is None:
        raise dbf.Busy('%s is in use by SHREE' % os.path.basename(live))
    stood_in[live] = ('read as it was at the last update, to %s'
                      % (kept.upto or '-'))
    return src


# The tables the import reads; their memo files and screen layouts go with
# them. Anything else in a year folder is copied, but not waited for.
READ_TABLES = ('amas', 'tmas', 'hmas', 'dept', 'imas', 'sitm', 'sale',
               'prch', 'mmas')


def _take(path, name):
    """A file's bytes -- or None for one the import does not read that SHREE
    is holding (dbf.Busy goes up for one it does)."""
    stem, ext = os.path.splitext(name.lower())
    if stem in READ_TABLES or (ext == '.fky' and re.match(r'^f[a-z]+$', stem)):
        return dbf.read_bytes(path)
    try:
        return dbf.read_bytes(path, patience=0.5)
    except dbf.Busy:
        return None


def _write_manifest(dest, want):
    tmp = os.path.join(dest, MANIFEST + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(want, fh)
    replace(tmp, os.path.join(dest, MANIFEST))


def _same_bytes(path, data):
    try:
        with open(path, 'rb') as fh:
            return fh.read() == data
    except OSError:
        return False


def prune_work(work, copies=True):
    """Remove unpacked archives -- and, unless `copies` is off, the local
    copies -- this run did not use."""
    for name in os.listdir(work):
        path = _norm(os.path.join(work, name))
        if name == MIRROR_DIR:
            if not copies:
                continue
            for sub in os.listdir(path):
                full = os.path.join(path, sub)
                if os.path.isdir(full) and _norm(full) not in MIRRORED:
                    rmtree(full)
        elif os.path.isdir(path) and path not in UNPACKED:
            rmtree(path)


def scan_zips(root, work, label='', verbose=True, places=None, archives=None):
    """Unpack every backup ZIP once, and probe what came out.

    The nightly backup names its archives <FIRM><DD><MM>.ZIP and rotates a
    second set named by weekday, so most archives appear twice; the file size
    tells the duplicates apart without unpacking both. `archives` are the
    ones to take, as survey() found them; otherwise `places` are searched
    (default: all of `root`).
    """
    out, seen = [], set()
    if archives is not None:
        places, archives = [], list(archives)
    else:
        archives = []
    for place in (places if places is not None else [root]):
        if os.path.isfile(place):
            if place.lower().endswith('.zip'):
                archives.append(place)
            continue
        for dirpath, dirs, files in os.walk(place):
            dirs.sort()
            for name in sorted(files):
                if name.lower().endswith('.zip'):
                    archives.append(os.path.join(dirpath, name))
    pending = list(archives)
    while pending:
        path = pending.pop()
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size < 1000:
            continue
        base = os.path.splitext(os.path.basename(path))[0]
        firm = re.match(r'^([A-Za-z]+|\d+)', base)
        firm = firm.group(1).upper() if firm else ''
        key = '%s__%d' % (base, size)
        if key in seen:
            continue
        seen.add(key)
        # The unpacked copy is kept between runs, so it is named after the
        # archive's time as well: a nightly backup rewritten at the same size
        # must be unpacked again, not read stale.
        try:
            stamp = int(os.path.getmtime(path))
        except OSError:
            stamp = 0
        dest = os.path.join(work, '%s__%d' % (key, stamp))
        UNPACKED.add(os.path.normcase(os.path.abspath(dest)))
        bad = os.path.join(dest, '.unreadable')
        if os.path.isfile(bad):
            continue            # said the first time; the same file again
        if os.path.isdir(dest) and _unpacked_whole(dest, path):
            pass
        else:
            try:
                got = unpack(path, dest)
                if got != dest:
                    UNPACKED.add(os.path.normcase(os.path.abspath(got)))
                    dest = got
            except Exception as exc:                    # noqa: BLE001
                if verbose:
                    print('  could not read %s (%s)'
                          % (os.path.basename(path), exc))
                # A damaged backup is remembered, so it is not unpacked and
                # reported again at every update; an archive rewritten since
                # has another name here and is tried afresh. A full disk or
                # a file held by another program is tried again next time.
                if isinstance(exc, (zipfile.BadZipFile, ValueError, EOFError,
                                    NotImplementedError, RuntimeError,
                                    zlib.error)):
                    try:
                        os.makedirs(dest, exist_ok=True)
                        with open(bad, 'w', encoding='utf-8') as fh:
                            fh.write(str(exc))
                    except OSError:
                        pass
                continue
        inner = []
        for dirpath, _dirs, files in os.walk(dest):
            for name in files:
                if name.lower().endswith('.zip'):
                    inner.append(os.path.join(dirpath, name))
        if inner:
            pending.extend(inner)
        if firm in SKIP_FIRMS:
            continue
        folder = dest
        if not dbf.find(folder, 'AMAS.DBF'):
            for name in sorted(os.listdir(dest)):
                cand = os.path.join(dest, name)
                if os.path.isdir(cand) and dbf.find(cand, 'AMAS.DBF'):
                    folder = cand
                    break
        src = _probe(folder, firm, '%s %s' % (label, os.path.basename(path)))
        if src and src.target:
            out.append(src)
    return out


def _newer(s):
    """How current a snapshot is: the day SHREE last wrote it, then how far
    its entries run, then how many there are."""
    return (s.saved or '', s.upto or '', s.rows, s.last or '')


def pick_best(sources):
    """The most current snapshot of each company-and-year wins.

    Several copies of the same year usually survive -- the live folder, a dated
    copy beside it, and a run of nightly ZIPs -- and they differ in when they
    were taken. The one SHREE wrote last is the year as it stands: 2016-17
    survives in a backup of 8 April 2017 and in several of 30 April, and only
    the later ones carry the narrations typed in between.

    SHREE's own year folder wins over its copies and backups unless one was
    written later and runs at least as far -- the bill registers copied over
    from the other computer. Until 2 Oct 2026 the snapshot with the most
    entries won: on a day more entries were deleted in SHREE than added,
    last night's backup outnumbered the year itself, and was read instead --
    every challan typed since it was missing, and the deleted ones stayed.
    """
    best, own = {}, {}
    for s in sources:
        key = (s.target, s.fy)
        cur = best.get(key)
        if cur is None or _newer(s) > _newer(cur):
            best[key] = s
        if s.own and (s.rows or s.upto):
            cur = own.get(key)
            if cur is None or _newer(s) > _newer(cur):
                own[key] = s
    for key, o in own.items():
        b = best[key]
        if not ((b.saved or '') > (o.saved or '')
                and (b.upto or '') >= (o.upto or '')):
            best[key] = o
    return best


def company_master(roots=None, files=None):
    """Firm details, from whichever company master is the fullest.

    `files` are (path, size, mtime) as survey() found them; each is read
    again only once it has changed."""
    if files is None:
        files = []
        for root in roots or []:
            if os.path.isdir(root):
                files += [f for f in walk_files(root)
                          if os.path.basename(f[0]).lower()
                          in ('cmas.dbf', 'cmasbackup.dbf')]
    out = {}
    for path, size, mtime in files:
        key = 'cmas|' + _norm(path)
        hit = PROBES.get(key)
        if hit and hit[0] == [size, mtime]:
            found = hit[1]
        else:
            try:
                rows = dbf.read(path)
            except Exception:
                continue
            found = []
            for r in rows:
                code = (r.get('CCOD') or '').strip().upper()
                if code:
                    found.append([code, sum(1 for k, v in r.items()
                                            if v not in ('', 0, 0.0, False)),
                                  r])
            PROBES[key] = [[size, mtime], found]
        for code, filled, r in found:
            if filled > out.get(code, (0, None))[0]:
                out[code] = (filled, r)
    return {k: v[1] for k, v in out.items()}


# ------------------------------------------------------------------- import

class Firm(object):
    """One firm's books, built up year by year."""

    def __init__(self, conn, cid, tax_mode, old_ids=None):
        self.conn, self.cid, self.tax_mode = conn, cid, tax_mode
        self.old_ids = old_ids or dict(invoice={}, voucher={}, ledger={},
                                       item={})
        self.ledger = {}        # normalised name -> ledger id
        self.group = {}         # HMAS code    -> account_group id
        self.item = {}          # normalised name -> item id
        self._stock_cats = None  # Billing Stock's categories, read once
        self.opened = set()     # ledgers whose opening balance is already set
        self._susp = None
        self._group_code = {}   # bank ledger id -> its group's code (tmas)
        self.year_group = {}    # (ledger id, year's April) -> its group then
        self.stats = defaultdict(int)

    def old_id(self, table, key):
        """The id this row had last time, if nothing holds it now."""
        oid = self.old_ids[table].pop(key, None)
        if oid and not self.conn.execute('SELECT 1 FROM %s WHERE id=?' % table,
                                         (oid,)).fetchone():
            return oid
        return None

    def suspense(self):
        """The account that takes the other half of an opening adjustment."""
        if self._susp:
            return self._susp
        conn, cid = self.conn, self.cid
        row = conn.execute('SELECT id FROM ledger WHERE company_id=? AND name=?',
                           (cid, SUSPENSE_NAME)).fetchone()
        if row:
            self._susp = row['id']
            return self._susp
        gid = self.group.get('9001')
        if gid is None:
            gid = conn.execute('SELECT id FROM account_group WHERE company_id=? '
                               'ORDER BY id', (cid,)).fetchone()['id']
        cur = conn.execute(
            'INSERT INTO ledger(company_id, group_id, name, notes) '
            'VALUES (?,?,?,?)',
            (cid, gid, SUSPENSE_NAME,
             'Holds the difference where a year had no backup, or where a '
             'voucher survived with only one side.'))
        self._susp = cur.lastrowid
        return self._susp

    # -- masters ------------------------------------------------------
    def groups(self, folder):
        conn, cid = self.conn, self.cid
        if not self.group:
            self.group = {r['code']: r['id'] for r in conn.execute(
                'SELECT code, id FROM account_group WHERE company_id=?', (cid,))}
        path = dbf.find(folder, 'HMAS.DBF')
        if not path:
            return
        for i, r in enumerate(dbf.read(path)):
            code = str(int(r.get('HCOD') or 0))
            name = (r.get('HNAM') or '').strip()
            if not name or code == '0' or code in self.group:
                continue
            parent = str(int(r.get('GCOD') or 0))
            parent = '' if parent == code else parent
            conn.execute(
                'INSERT INTO account_group(company_id, code, name, nature, '
                'parent_code, is_party, sort) VALUES (?,?,?,?,?,?,?)',
                (cid, code, name, nature_for(parent or code), parent,
                 1 if code in ('2101', '3101') else 0, 100 + i))
            self.group[code] = conn.execute(
                'SELECT last_insert_rowid() r').fetchone()['r']
            self.stats['groups'] += 1

    def ledgers(self, folder, fy, first_year):
        """Merge this year's AMAS in; returns {ACOD: ledger id} for the year."""
        conn, cid = self.conn, self.cid
        if not self.ledger:
            for r in conn.execute('SELECT id, name FROM ledger WHERE '
                                  'company_id=?', (cid,)):
                self.ledger.setdefault(norm(r['name']), r['id'])
        fallback = self.group.get('9001') or min(self.group.values())
        by_acod, openings = {}, {}
        for r in dbf.read(dbf.find(folder, 'AMAS.DBF')):
            acod = int(r.get('ACOD') or 0)
            name = (r.get('ANAM') or '').strip()
            if not acod or not name:
                continue
            key = norm(name)
            obal = r2(CREDIT_POSITIVE * float(r.get('OBAL') or 0))
            openings[acod] = obal
            lid = self.ledger.get(key)
            gstin = (r.get('GSTNO') or '').strip().upper()
            if len(gstin) != 15:
                gstin = ''
            hcod = str(int(r.get('HCOD') or 0))
            if lid is None:
                st_code = gstin[:2] if gstin else ''
                st_name = (r.get('STATE') or '').strip().title()
                if st_code and not st_name:
                    st_name = api.STATE_NAME.get(st_code, '')
                atype = (r.get('ATYPE') or '').strip() or (
                    'Regular' if gstin else 'UnRegistered')
                cur = conn.execute(
                    'INSERT INTO ledger(id, company_id, group_id, code, name, '
                    'party_kind, gst_type, address1, address2, city, state, '
                    'state_code, pin, gstin, pan, phone, email, opening_bal, '
                    'credit_limit, credit_days, notes, source) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (self.old_id('ledger', key), cid,
                     self.group.get(hcod, fallback),
                     self._free_code('A%d' % acod), name,
                     'customer' if hcod == '2101' else
                     'supplier' if hcod == '3101' else '',
                     atype if atype in api.GST_TYPES else 'Regular',
                     (r.get('ADD1') or '').strip(),
                     ' '.join(x for x in [(r.get('ADD2') or '').strip(),
                                          (r.get('ADD3') or '').strip()] if x),
                     (r.get('CITY') or '').strip(), st_name, st_code,
                     (r.get('PIN') or '').strip(), gstin,
                     (r.get('PANNO') or '').strip().replace('-', ''),
                     (r.get('MOBILE') or r.get('PHON1') or '').strip(),
                     (r.get('EMAIL') or '').strip(),
                     obal if first_year else 0.0,
                     float(r.get('CREDIT_LMT') or 0), 0,
                     (r.get('DETAILS') or '').strip(), 'SHREE'))
                lid = cur.lastrowid
                self.ledger[key] = lid
                self.stats['ledgers'] += 1
                if first_year:
                    self.opened.add(lid)
            else:
                # A later year usually knows more about a party than the year
                # it was created in -- fill the blanks, never overwrite.
                self._enrich(lid, r, gstin, hcod)
            by_acod[acod] = lid
            self.year_group[(lid, fy_year(fy))] = self.group.get(hcod, fallback)
        return by_acod, openings

    def save_year_groups(self):
        """Where each account sat in the years SHREE had it in another group
        than today's (db.LEDGER_GROUP_YEAR). Only SHREE's own accounts: one
        edited here keeps the group it was given, every year."""
        conn, cid = self.conn, self.cid
        conn.execute('DELETE FROM ledger_group_year WHERE ledger_id IN '
                     '(SELECT id FROM ledger WHERE company_id=?)', (cid,))
        now = {r['id']: r['group_id'] for r in conn.execute(
            "SELECT id, group_id FROM ledger WHERE company_id=? AND "
            "source='SHREE' AND system_key IS NULL", (cid,))}
        rows = sorted((lid, y, gid) for (lid, y), gid in self.year_group.items()
                      if lid in now and gid != now[lid])
        conn.executemany('INSERT OR REPLACE INTO ledger_group_year(ledger_id, '
                         'y, group_id) VALUES (?,?,?)', rows)
        self.stats['year_groups'] = len(rows)

    def _free_code(self, want):
        conn, cid = self.conn, self.cid
        code, n = want, 1
        while conn.execute('SELECT 1 FROM ledger WHERE company_id=? AND code=?',
                           (cid, code)).fetchone():
            n += 1
            code = '%s-%d' % (want, n)
        return code

    def _enrich(self, lid, r, gstin, hcod):
        """Bring an account up to what this year's master says of it.

        Years are read oldest first. An account that is SHREE's alone takes
        each later year's details as they come, so it ends on what SHREE
        shows today -- a corrected GSTIN, a new address or phone, a move to
        another group. Kept to the first year's, as it was until 18 Sep
        2026, 14 GSTINs in BST alone were years out of date. An account
        edited in this app is its own (source ''), and only has its blanks
        filled.
        """
        conn = self.conn
        cur = conn.execute('SELECT * FROM ledger WHERE id=?', (lid,)).fetchone()
        shree_own = cur['source'] == 'SHREE'
        sets, args = [], []
        pairs = [('gstin', gstin),
                 ('pan', (r.get('PANNO') or '').strip().replace('-', '')),
                 ('address1', (r.get('ADD1') or '').strip()),
                 ('address2', ' '.join(x for x in [(r.get('ADD2') or '').strip(),
                                                   (r.get('ADD3') or '').strip()]
                                       if x)),
                 ('city', (r.get('CITY') or '').strip()),
                 ('pin', (r.get('PIN') or '').strip()),
                 ('phone', (r.get('MOBILE') or r.get('PHON1') or '').strip()),
                 ('email', (r.get('EMAIL') or '').strip()),
                 ('state', (r.get('STATE') or '').strip().title()),
                 ('state_code', gstin[:2] if gstin else ''),
                 ('notes', (r.get('DETAILS') or '').strip())]
        atype = (r.get('ATYPE') or '').strip()
        if atype in api.GST_TYPES:
            pairs.append(('gst_type', atype))
        for col, val in pairs:
            have = (cur[col] or '').strip()
            # A blank in a later year is not taken as SHREE clearing it: the
            # older years' masters are often the fuller ones.
            if val and (val != have if shree_own else not have):
                sets.append('%s=?' % col)
                args.append(val)
        limit = float(r.get('CREDIT_LMT') or 0)
        if limit and shree_own and abs(limit - float(cur['credit_limit'] or 0)) > 0.005:
            sets.append('credit_limit=?')
            args.append(limit)
        kind = ('customer' if hcod == '2101' else
                'supplier' if hcod == '3101' else '')
        gid = self.group.get(hcod)
        if shree_own and not cur['system_key'] and gid and gid != cur['group_id']:
            sets.append('group_id=?')
            args.append(gid)
            # Moved into customers or suppliers, it is one now; moved out, it
            # keeps what it was -- its challans still make it a customer.
            if kind and kind != (cur['party_kind'] or ''):
                sets.append('party_kind=?')
                args.append(kind)
        elif kind and not (cur['party_kind'] or ''):
            sets.append('party_kind=?')
            args.append(kind)
        if sets:
            args.append(lid)
            conn.execute('UPDATE ledger SET %s WHERE id=?' % ','.join(sets), args)

    def items(self, folder, first_year):
        conn, cid = self.conn, self.cid
        if not self.item:
            for r in conn.execute('SELECT id, name FROM item WHERE company_id=?',
                                  (cid,)):
                self.item.setdefault(norm(r['name']), r['id'])
        dept = {}
        dpath = dbf.find(folder, 'DEPT.DBF')
        if dpath:
            for r in dbf.read(dpath):
                dept[int(r.get('DCDEPT') or 0)] = dict(
                    name=(r.get('DNDEPT') or '').strip(),
                    gst=float(r.get('IGST') or 0)
                    or float(r.get('CGST') or 0) + float(r.get('SGST') or 0),
                    cess=float(r.get('CESS') or 0),
                    unit=(r.get('CUNIT') or '').split('-')[0].strip())
        by_icod = {}
        ipath = dbf.find(folder, 'IMAS.DBF')
        if not ipath:
            return by_icod, dept
        for r in dbf.read(ipath):
            name = (r.get('INIMAS') or '').strip()
            if not name:
                continue
            icod = int(r.get('ICIMAS') or 0)
            key = norm(name)
            iid = self.item.get(key)
            cat = dept.get(int(r.get('DCIMAS') or 0), {})
            unit = ((r.get('SUIMAS') or '').strip()
                    or cat.get('unit') or 'KGS').upper()
            if iid is None:
                gst = float(r.get('VAT') or 0) or cat.get('gst', 0)
                hsn = (r.get('HSNCODE') or '').strip()
                # A product new to the books starts in the Billing Stock
                # category its name and HSN point to; once here, the
                # category is the user's and no update touches it.
                if self._stock_cats is None:
                    self._stock_cats = db.stock_cats(conn)
                cur = conn.execute(
                    'INSERT INTO item(id, company_id, code, name, category, '
                    'hsn, unit, gst_rate, cess_rate, sale_rate, purchase_rate, '
                    'opening_qty, reorder_level, by_weight, source, stock_cat) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (self.old_id('item', key), cid,
                     self._free_item_code('I%d' % icod), name,
                     cat.get('name', ''), hsn,
                     unit, gst, cat.get('cess', 0),
                     float(r.get('SRIMAS') or 0), float(r.get('PRIMAS') or 0),
                     float(r.get('OSTQTY') or 0) if first_year else 0.0,
                     float(r.get('MIIMAS') or 0),
                     1 if unit in api.WEIGHT_UNITS else 0, 'SHREE',
                     db.guess_stock_cat(name, hsn, self._stock_cats)))
                iid = cur.lastrowid
                self.item[key] = iid
                self.stats['items'] += 1
            else:
                self._refresh_item(iid, r, cat, unit)
            by_icod[icod] = iid
        return by_icod, dept

    def _refresh_item(self, iid, r, cat, unit):
        """An item SHREE alone keeps takes each later year's master, as an
        account does (see _enrich); one edited here only has its HSN filled.
        A blank or a nought in a later year is not read as SHREE clearing
        it."""
        conn = self.conn
        cur = conn.execute('SELECT * FROM item WHERE id=?', (iid,)).fetchone()
        hsn = (r.get('HSNCODE') or '').strip()
        if cur['source'] != 'SHREE':
            if hsn and not (cur['hsn'] or '').strip():
                conn.execute('UPDATE item SET hsn=? WHERE id=?', (hsn, iid))
            return
        want = dict(hsn=hsn, category=cat.get('name', ''), unit=unit,
                    gst_rate=float(r.get('VAT') or 0) or cat.get('gst', 0),
                    cess_rate=cat.get('cess', 0),
                    purchase_rate=float(r.get('PRIMAS') or 0),
                    reorder_level=float(r.get('MIIMAS') or 0))
        if (r.get('SUIMAS') or '').strip() or cat.get('unit'):
            want['by_weight'] = 1 if unit in api.WEIGHT_UNITS else 0
        else:
            del want['unit']            # the KGS default, not SHREE's word
        sets, args = [], []
        for col, val in want.items():
            if col == 'by_weight':
                changed = val != cur[col]
            elif isinstance(val, float) or isinstance(val, int):
                changed = bool(val) and abs(float(val) - float(cur[col] or 0)) > 1e-6
            else:
                changed = bool(val) and val != (cur[col] or '')
            if changed:
                sets.append('%s=?' % col)
                args.append(val)
        if sets:
            args.append(iid)
            conn.execute('UPDATE item SET %s WHERE id=?' % ','.join(sets), args)

    def _free_item_code(self, want):
        conn, cid = self.conn, self.cid
        code, n = want, 1
        while conn.execute('SELECT 1 FROM item WHERE company_id=? AND code=?',
                           (cid, code)).fetchone():
            n += 1
            code = '%s-%d' % (want, n)
        return code

    # -- documents ----------------------------------------------------
    def documents(self, folder, fy, acod, icod, dept):
        """SALE and PRCH headers with their SITM lines."""
        conn, cid = self.conn, self.cid
        lines = defaultdict(list)
        spath = dbf.find(folder, 'SITM.DBF')
        if spath:
            for r in dbf.read(spath):
                vt = (r.get('VHTY') or r.get('EXT_VHTY') or '').strip()
                lines[(vt, int(r.get('VHNO') or 0))].append(r)

        made = {}
        for book, table, party_f, trade_f, no_f, date_f, type_f in (
                ('SALE', 'SALE.DBF', 'CCSALE', 'SASALE', 'BNSALE', 'BDSALE',
                 'BTSALE'),
                ('PRCH', 'PRCH.DBF', 'VCPRCH', 'PAPRCH', 'DOC_NO', 'BDPRCH',
                 'BTPRCH')):
            path = dbf.find(folder, table)
            if not path:
                continue
            roles = charge_map(folder, book)
            vhty = 'Sale' if book == 'SALE' else 'Prch'
            rows = dbf.read(path)
            texts = text_map(folder, book, rows)
            for r in rows:
                iid = self._one_document(r, book, vhty, roles, party_f, trade_f,
                                         no_f, date_f, type_f, fy, acod, icod,
                                         dept, lines, texts)
                if iid:
                    made[(vhty, int(r.get('VHNO') or 0))] = iid
        return made

    def _one_document(self, r, book, vhty, roles, party_f, trade_f, no_f,
                      date_f, type_f, fy, acod, icod, dept, lines, texts):
        conn, cid = self.conn, self.cid
        date = (r.get(date_f) or '').strip()
        if not date:
            return None
        vhno = int(r.get('VHNO') or 0)
        party = acod.get(int(r.get(party_f) or 0))
        if not party:
            self.stats['docs_no_party'] += 1
            return None
        memo = MEMO_OF.get((r.get(type_f) or '').strip().upper(), 'Debit')
        ext = (r.get('EXT_VHTY') or '').strip()
        # The years before SHREE wrote EXT_VHTY leave it blank; the table
        # says the same thing.
        doc_type = DOC_OF.get(ext) or ('SALE' if vhty == 'Sale' else 'PURCHASE')
        if ext and ext not in DOC_OF:
            self.stats['docs_odd_kind'] += 1

        # SALE numbers its own bills; PRCH records the supplier's number in
        # BNPRCH and keeps its own count in DOC_NO.
        raw_no = r.get(no_f)
        try:
            number = str(int(float(raw_no or 0)))
        except (TypeError, ValueError):
            number = str(raw_no or '').strip()
        if not number or number == '0':
            number = str(vhno)
        series = (r.get('BSSALE') or r.get('DOC_S') or '').strip()
        ref_no = (r.get('BNPRCH') or r.get('ORIINVNO') or '').strip()
        # A challan book is a letter and a page. SHREE files the two apart;
        # this app keeps them together in the number, which is what lets the
        # next challan be worked out from the documents alone. Split, the
        # numbering could not see the letter and began again at one.
        if self.tax_mode == 'none' and vhty == 'Sale' and series and number:
            number, series = '%s %s' % (series.upper(), number), ''

        group = lines.get((vhty, vhno)) or []
        # The line detail carries GST per line and is the most reliable source
        # for it. Before GST it carries nothing, and the screen's own tax boxes
        # are all there is.
        taxes = dict(cgst=0.0, sgst=0.0, igst=0.0, cess=0.0)
        for ln in group:
            taxes['cgst'] += abs(float(ln.get('CGSTAMNT') or 0))
            taxes['sgst'] += abs(float(ln.get('SGSTAMNT') or 0))
            taxes['igst'] += abs(float(ln.get('IGSTAMNT') or 0))
            taxes['cess'] += abs(float(ln.get('CESSAMNT') or 0))
        from_lines = sum(taxes.values()) > 0

        charges = dict(cutting=0.0, transport=0.0, loading=0.0)
        box_tax = dict(cgst=0.0, sgst=0.0, igst=0.0, cess=0.0)
        discount = stated_round = 0.0
        for n, role in roles.items():
            val = fnum(r, book, n)
            if not val:
                continue
            if role in charges:
                charges[role] += val
            elif role == 'round_off':
                stated_round += val
            elif role == 'discount':
                discount += val
            elif role in box_tax:
                box_tax[role] += val
        if not from_lines:
            taxes = box_tax
        for k in taxes:
            taxes[k] = half_up(taxes[k])

        goods = r2(r.get('TOTAL1') or 0)
        grand = r2(r.get('TOTAL2') or 0)
        discount = r2(discount)
        # A return is filed with its totals and tax below nought, but its
        # DISCOUNT box as typed: the discount makes the return smaller, so it
        # takes the return's side. Added the other way, BSI's debit note GT/162
        # of 1 Sep 2026 showed 48,524.50 taxable for SHREE's 46,160.50 and a
        # round off of 2,577 (2 Oct 2026). Charges are filed with the return's
        # sign already.
        if goods < 0 < discount:
            discount = -discount
        charge_total = r2(sum(charges.values()))
        tax_total = r2(sum(taxes.values()))

        def settle(tax):
            if self.tax_mode == 'none':
                base = r2(goods - discount)
                return base, r2(grand - r2(base + tax + charge_total))
            base = r2(goods - discount + charge_total)
            return base, r2(grand - r2(base + tax))

        taxable, round_off = settle(tax_total)
        # The order firm's challans are billed with no tax, yet SHREE still
        # records the GST each line would attract once the sister firm bills
        # it. Taking that as charged would inflate every challan, so where
        # dropping the tax is what makes the document add up, it is dropped.
        if tax_total:
            alt_taxable, alt_round = settle(0.0)
            if abs(alt_round) + 0.005 < abs(round_off):
                taxes = dict(cgst=0.0, sgst=0.0, igst=0.0, cess=0.0)
                tax_total = 0.0
                taxable, round_off = alt_taxable, alt_round
                self.stats['docs_tax_free'] += 1
        if abs(round_off - stated_round) <= 0.02:
            pass                # exactly the round off the old screen showed
        elif abs(round_off) > 2.5:
            # A box on this screen was not one we know how to read. The bill is
            # still stored adding up to its own total, and the run reports it.
            self.stats['docs_odd_total'] += 1
            self.stats['docs_odd_value'] += abs(round_off)
        elif stated_round:
            self.stats['docs_odd_round'] += 1

        is_igst = taxes['igst'] > 0
        # The weight goes in with the bill rather than in an UPDATE after it:
        # added up in the same order the lines are written, so it is the same
        # figure to the last bit.
        weight = 0.0
        for ln in group:
            weight += abs(float(ln.get('IQSITM') or 0))
        # SHREE numbers each series from 1, so the series is part of what makes
        # a bill number unique. A number still repeated inside one series is a
        # duplicate the old system let through; keep both, marked.
        number = self._free_doc_no(doc_type, fy, series, number)
        head = header_text(r, book, texts)
        # Columns of these names turn up in some of SHREE's tables; where
        # they do, they say it plainer than any box.
        for role, col in (('transporter', 'TRANNAME'),
                          ('vehicle_no', 'VEHICLENO'), ('lr_no', 'LRNO'),
                          ('eway_no', 'EWAYNO')):
            val = (r.get(col) or '').strip()
            if val:
                head[role] = val[:TEXT_WIDTH[role]]
        if (r.get('LRDATE') or '').strip():
            head['lr_date'] = str(r.get('LRDATE')).strip()[:10]
        cur = conn.execute(
            'INSERT INTO invoice(id, company_id, doc_type, series, invoice_no, '
            'invoice_type, invoice_date, fy, party_id, sales_ac_id, '
            'driver_name, payment_terms, supply_state, is_igst, rcm, ref_no, '
            'subtotal, discount_amt, taxable, cgst, sgst, igst, cess, '
            'weight_total, cutting, transport, loading, round_off, '
            'grand_total, party_amount, transporter, vehicle_no, lr_no, '
            'lr_date, cases, eway_no, remarks, source) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'
            '?,?,?,?,?,?,?,?,?,?)',
            (self.old_id('invoice', (doc_type, fy, series or '', number)),
             cid, doc_type, series, number, memo, date, fy, party,
             acod.get(int(r.get(trade_f) or 0)),
             head.get('driver_name', ''), head.get('payment_terms', ''),
             '', 1 if is_igst else 0,
             1 if (r.get('RCHARGE') or '').strip().upper() == 'Y' else 0,
             ref_no[:40],
             goods, discount, taxable, taxes['cgst'], taxes['sgst'], taxes['igst'],
             taxes['cess'], half_up(weight, 3) if weight else 0.0,
             charges['cutting'], charges['transport'],
             charges['loading'], round_off, grand, grand,
             head.get('transporter', ''), head.get('vehicle_no', ''),
             head.get('lr_no', ''), head.get('lr_date', ''),
             head.get('cases', ''), head.get('eway_no', ''),
             (r.get('NARA') or '').strip()[:200], 'SHREE'))
        iid = cur.lastrowid
        self.stats['doc_' + doc_type.lower()] += 1

        items = []
        for ln in group:
            qty = abs(float(ln.get('IQ1SITM') or 0))
            wt = abs(float(ln.get('IQSITM') or 0))
            rate = abs(float(ln.get('IRSITM') or 0))
            amt = abs(float(ln.get('IASITM') or 0))
            item_id = icod.get(int(ln.get('ICSITM') or 0))
            cat = dept.get(int(ln.get('DCSITM') or 0), {})
            name = (ln.get('INSITM') or '').strip() or cat.get('name', '')
            unit = (cat.get('unit') or 'KGS').upper()
            by_weight = 1 if unit in api.WEIGHT_UNITS else 0
            items.append(
                (iid, item_id, name[:120],
                 self._hsn_of(item_id, name), unit, '', '',
                 qty, wt, by_weight, rate,
                 abs(float(ln.get('IDSITM') or 0)), r2(amt),
                 float(ln.get('VAT') or 0),
                 abs(float(ln.get('CGSTAMNT') or 0)),
                 abs(float(ln.get('SGSTAMNT') or 0)),
                 abs(float(ln.get('IGSTAMNT') or 0)),
                 float(ln.get('CESS') or 0),
                 abs(float(ln.get('CESSAMNT') or 0)),
                 (ln.get('F1ITEM') or '').strip()[:120],
                 r2(amt + abs(float(ln.get('CGSTAMNT') or 0))
                    + abs(float(ln.get('SGSTAMNT') or 0))
                    + abs(float(ln.get('IGSTAMNT') or 0))
                    + abs(float(ln.get('CESSAMNT') or 0)))))
        if items:
            conn.executemany(
                'INSERT INTO invoice_item(invoice_id, item_id, description, '
                'hsn, unit, thickness, cutting_size, qty, weight, by_weight, '
                'rate, discount_pct, taxable, gst_rate, cgst, sgst, igst, '
                'cess_rate, cess, item_detail, line_total) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', items)
            self.stats['doc_lines'] += len(items)
        if not group:
            # A bill whose lines were purged still has to show its value.
            conn.execute(
                'INSERT INTO invoice_item(invoice_id, description, unit, qty, '
                'weight, by_weight, rate, taxable, gst_rate, line_total) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                (iid, 'As per bill', 'KGS', 0.0, 0.0, 1, 0.0, goods, 0.0,
                 r2(goods + tax_total)))
            self.stats['doc_lines_missing'] += 1
        return iid

    def _free_doc_no(self, doc_type, fy, series, want):
        conn, cid = self.conn, self.cid
        number, n = want, 1
        while conn.execute(
                'SELECT 1 FROM invoice WHERE company_id=? AND doc_type=? AND '
                'fy=? AND series=? AND invoice_no=?',
                (cid, doc_type, fy, series, number)).fetchone():
            n += 1
            number = '%s(%d)' % (want, n)
            self.stats['docs_renumbered'] += 1
        return number

    def _free_voucher_no(self, kind, fy, want):
        """SHREE's number, unless a voucher made in the app already holds it.

        A bill raised here posts its GST journal on the next journal number,
        and SHREE, which never hears of it, hands that number out again.
        Without this the second one stopped the whole import on the
        voucher's UNIQUE key.
        """
        conn, cid = self.conn, self.cid
        number, n = want, 1
        while conn.execute(
                'SELECT 1 FROM voucher WHERE company_id=? AND kind=? AND fy=? '
                'AND voucher_no=?', (cid, kind, fy, number)).fetchone():
            n += 1
            number = '%s(%d)' % (want, n)
            self.stats['vouchers_renumbered'] += 1
        return number

    def _hsn_of(self, item_id, name):
        if item_id:
            row = self.conn.execute('SELECT hsn FROM item WHERE id=?',
                                    (item_id,)).fetchone()
            if row and (row['hsn'] or '').strip():
                return row['hsn'].strip()
        m = re.search(r'\b(\d{4,8})\b', name or '')
        return m.group(1) if m else ''

    # -- vouchers and postings ---------------------------------------
    def tmas(self, folder, fy, acod, made):
        """Receipts, payments and journals, and every ledger posting.

        TMAS is SHREE's own ledger: two or more rows per voucher, credit
        positive. Sale and purchase rows already have a bill behind them, so
        they are only tagged onto it; the rest become vouchers here.
        """
        conn, cid = self.conn, self.cid
        path = dbf.find(folder, 'TMAS.DBF')
        if not path:
            return
        groups = defaultdict(list)
        # Written in one batch at the end, in the order they were made -- a
        # quarter of a million single INSERTs was the largest cost of an update.
        postings = []
        doc_posts = {}          # bill id -> SHREE's ledger lines for it
        # Which group each bank sits in, asked once a year: a later year's
        # masters can move a ledger, so this is not kept across years.
        self._group_code = {}
        for r in dbf.read(path, fields=('DATE', 'AMNT', 'VHTY', 'VHNO', 'SRNO',
                                        'COD1', 'NARA', 'REF')):
            date = (r.get('DATE') or '').strip()
            if not date or not (r.get('AMNT') or 0):
                continue
            groups[((r.get('VHTY') or '').strip(),
                    int(r.get('VHNO') or 0))].append(r)

        for (vhty, vhno), rows in sorted(groups.items(),
                                         key=lambda kv: (kv[1][0].get('DATE'),
                                                         kv[0][1])):
            rows.sort(key=lambda x: x.get('SRNO') or 0)
            date = (rows[0].get('DATE') or '').strip()
            entries = []
            for r in rows:
                lid = acod.get(int(r.get('COD1') or 0))
                if not lid:
                    self.stats['post_no_ledger'] += 1
                    continue
                amt = float(r.get('AMNT') or 0)
                entries.append((lid, r2(-amt) if amt < 0 else 0.0,
                                r2(amt) if amt > 0 else 0.0, r))
            if not entries:
                continue

            kind = VOUCHER_KIND.get(vhty)
            if kind:
                doc_id = self._voucher(kind, vhno, date, fy, entries, rows)
                doc_kind, doc_no = kind, str(vhno)
            else:
                doc_id = made.get((vhty, vhno))
                if doc_id:
                    doc_posts[doc_id] = [(e[0], e[1], e[2]) for e in entries]
                    row = conn.execute('SELECT doc_type, invoice_no, series '
                                       'FROM invoice WHERE id=?',
                                       (doc_id,)).fetchone()
                    doc_kind = row['doc_type']
                    doc_no = ((row['series'] + ' ') if row['series'] else '') \
                        + row['invoice_no']
                else:
                    doc_kind, doc_id, doc_no = 'IMPORT', 0, '%s-%d' % (vhty, vhno)
                    self.stats['post_orphan'] += 1

            for lid, dr, cr, r in entries:
                postings.append(
                    (cid, (r.get('DATE') or date).strip(), lid, doc_kind,
                     doc_id, doc_no, dr, cr,
                     ((r.get('NARA') or '').strip()
                      or (r.get('REF') or '').strip())[:200]))
            self.stats['postings'] += len(entries)
        conn.executemany(
            'INSERT INTO posting(company_id, date, ledger_id, '
            'doc_kind, doc_id, doc_no, debit, credit, narration) '
            'VALUES (?,?,?,?,?,?,?,?,?)', postings)
        if self.tax_mode != 'none':
            self._tax_from_ledger(doc_posts)

    def _tax_from_ledger(self, posts):
        """A GST bill's tax as SHREE's own ledger took it.

        The bill's tax is read off its item lines, and SHREE's lines can be
        wrong where its bill is not: BSI's purchase 76 of 19-12-2023 has a
        line typed at 11% IGST, so the lines came to 2,04,336 while the bill's
        IGST box and SHREE's ledger both say 2,76,950 -- and the 72,614
        between them sat in Round Off. Six purchases of BST and BSI read short
        like that ("make sure all the figures match", 6 Oct 2026). Where the
        ledger's tax accounts took something else for the bill, the bill takes
        the ledger's figure, its total as it was; a charge SHREE's screen
        never named (Rs 250 of freight on BST's purchase 130 of 2021-22) is
        put in as Transport where what SHREE put on the trade account is
        what makes the bill add up. Returns are left as they are."""
        conn = self.conn
        names = {}
        for doc_id, entries in posts.items():
            inv = conn.execute(
                'SELECT id, grand_total, taxable, cgst, sgst, igst, cess, '
                'cutting, transport, loading, taxable_adj, discount_amt, '
                'subtotal, sales_ac_id FROM invoice WHERE id=?',
                (doc_id,)).fetchone()
            if not inv or float(inv['grand_total'] or 0) <= 0:
                continue
            got = dict(cgst=0.0, sgst=0.0, igst=0.0, cess=0.0)
            seen = False
            for lid, dr, cr in entries:
                if lid not in names:
                    # A tax account is one under Duties & Taxes: "PURCHASE
                    # A/C - IGST(OGS)" names IGST and is the trade account.
                    row = conn.execute(
                        'SELECT l.name, g.code FROM ledger l JOIN '
                        'account_group g ON g.id=l.group_id WHERE l.id=?',
                        (lid,)).fetchone()
                    names[lid] = (row['name'].upper() if row and
                                  row['code'] == TAX_GROUP else '')
                name = names[lid]
                m = TAX_HEAD.search(name)
                if m and not re.search(r'\bT[CD]S\b', name):
                    got['sgst' if m.group(1) == 'UTGST' else
                        m.group(1).lower()] += cr - dr
                    seen = True
            if not seen:
                continue
            got = {k: r2(abs(v)) for k, v in got.items()}
            if all(abs(got[k] - float(inv[k] or 0)) < 0.005 for k in got):
                continue
            tax = r2(sum(got.values()))
            grand = float(inv['grand_total'])
            taxable = float(inv['taxable'] or 0)
            transport = float(inv['transport'] or 0)
            if abs(grand - taxable - tax) > 2.5 and inv['sales_ac_id']:
                goods = r2(taxable - float(inv['cutting'] or 0) - transport
                           - float(inv['loading'] or 0)
                           - float(inv['taxable_adj'] or 0))
                trade = r2(abs(sum(dr - cr for lid, dr, cr in entries
                                   if lid == inv['sales_ac_id'])))
                extra = r2(trade - goods)
                if extra and abs(grand - (taxable + extra) - tax) < \
                        abs(grand - taxable - tax):
                    taxable, transport = r2(taxable + extra), r2(transport + extra)
            conn.execute(
                'UPDATE invoice SET cgst=?, sgst=?, igst=?, cess=?, is_igst=?, '
                'taxable=?, transport=?, round_off=? WHERE id=?',
                (got['cgst'], got['sgst'], got['igst'], got['cess'],
                 1 if got['igst'] > 0 else 0, taxable, transport,
                 r2(grand - taxable - tax), doc_id))
            self.stats['docs_tax_from_ledger'] += 1

    def _voucher(self, kind, vhno, date, fy, entries, rows):
        conn, cid = self.conn, self.cid
        amount = r2(sum(e[1] for e in entries))
        bank_id = party_id = None
        # On a receipt the money lands in the bank (a debit); on a payment it
        # leaves it (a credit). The other side is the party.
        want_debit_bank = (kind == 'RECEIPT')
        for lid, dr, cr, _r in entries:
            if (dr > 0) == want_debit_bank and bank_id is None:
                bank_id = lid
            elif party_id is None:
                party_id = lid
        if kind in ('JOURNAL', 'CONTRA'):
            bank_id = None
            party_id = entries[0][0] if entries else None
        ref = ''
        for r in rows:
            ref = (r.get('REF') or '').strip()
            if ref:
                break
        mode = 'Cash'
        if bank_id:
            code = self._group_code.get(bank_id)
            if code is None:
                row = conn.execute(
                    'SELECT g.code c FROM ledger l JOIN account_group g '
                    'ON g.id=l.group_id WHERE l.id=?', (bank_id,)).fetchone()
                code = self._group_code[bank_id] = row['c'] if row else ''
            if code == '2201':
                mode = 'Bank'
        remarks = ''
        for r in rows:
            remarks = (r.get('NARA') or '').strip()
            if remarks:
                break
        number = self._free_voucher_no(kind, fy, str(vhno))
        cur = conn.execute(
            'INSERT INTO voucher(id, company_id, kind, voucher_no, voucher_date, '
            'fy, party_id, bank_id, mode, ref_no, amount, remarks, source) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (self.old_id('voucher', (kind, fy, number)), cid, kind, number,
             date, fy, party_id, bank_id, mode, ref[:40], amount,
             remarks[:200], 'SHREE'))
        vid = cur.lastrowid
        conn.executemany(
            'INSERT INTO voucher_line(voucher_id, ledger_id, debit, '
            'credit, remarks) VALUES (?,?,?,?,?)',
            [(vid, lid, dr, cr, (r.get('NARA') or '').strip()[:200])
             for lid, dr, cr, r in entries])
        self.stats['vou_' + kind.lower()] += 1
        return vid

    # -- year openings ------------------------------------------------
    def carry_forward(self, fy, acod, openings, dropped=()):
        """Make each year open on the balance SHREE opened it on.

        Where a year was never backed up, or a snapshot stopped mid-year, the
        postings on file cannot add up to the next year's opening balance. The
        gap is posted once, dated 1 April, rather than left to distort every
        balance after it.

        `dropped` are the accounts an earlier year had and this year's
        masters do not: SHREE renamed them under the same number, or stopped
        carrying them. SHREE opens the year with nothing on that name, so it
        is closed here on 1 April too. Until 6 Oct 2026 they kept the balance
        they stopped at for ever -- a renamed customer owed twice on Who owes
        us, the old Kotak loans stood beside the new ones -- with Difference
        in Opening Balances holding the other side: Rs 23.9 crore of balances
        in BSTP alone that SHREE has no account for ("the software and SHREE
        figures should match").
        """
        conn, cid = self.conn, self.cid
        start = fy_start(fy)
        rows = conn.execute(
            "SELECT ledger_id, SUM(debit-credit) net FROM posting "
            "WHERE company_id=? AND (date<? OR (date=? AND doc_kind='OPENING')) "
            "GROUP BY ledger_id", (cid, start, start)).fetchall()
        sofar = {r['ledger_id']: float(r['net'] or 0) for r in rows}
        base = {r['id']: float(r['opening_bal'] or 0) for r in conn.execute(
            'SELECT id, opening_bal FROM ledger WHERE company_id=?', (cid,))}
        net = 0.0
        for a, want in openings.items():
            lid = acod.get(a)
            if not lid:
                continue
            have = r2(base.get(lid, 0.0) + sofar.get(lid, 0.0))
            diff = r2(want - have)
            if abs(diff) < 0.01:
                continue
            conn.execute(
                'INSERT INTO posting(company_id, date, ledger_id, doc_kind, '
                'doc_id, doc_no, debit, credit, narration) '
                'VALUES (?,?,?,?,0,?,?,?,?)',
                (cid, start, lid, 'OPENING', fy,
                 diff if diff > 0 else 0.0, -diff if diff < 0 else 0.0,
                 'Opening balance carried forward %s' % fy))
            self.stats['carry_forward'] += 1
            self.stats['carry_value'] += abs(diff)
            net += diff
        for lid in sorted(dropped):
            have = r2(base.get(lid, 0.0) + sofar.get(lid, 0.0))
            if abs(have) < 0.01:
                continue
            # Named like the line above, so an account merged into its new
            # name later has one line for the year, not two.
            conn.execute(
                'INSERT INTO posting(company_id, date, ledger_id, doc_kind, '
                'doc_id, doc_no, debit, credit, narration) '
                'VALUES (?,?,?,?,0,?,?,?,?)',
                (cid, start, lid, 'OPENING', fy,
                 -have if have < 0 else 0.0, have if have > 0 else 0.0,
                 'Opening balance carried forward %s -- not in SHREE\'s '
                 'accounts this year' % fy))
            self.stats['closed_dropped'] += 1
            net -= have
        net = r2(net)
        if abs(net) >= 0.01:
            # The other half of every adjustment above, in one line, so the
            # year's books still balance.
            conn.execute(
                'INSERT INTO posting(company_id, date, ledger_id, doc_kind, '
                'doc_id, doc_no, debit, credit, narration) '
                'VALUES (?,?,?,?,0,?,?,?,?)',
                (cid, start, self.suspense(), 'OPENING', fy,
                 0.0 if net > 0 else -net, net if net > 0 else 0.0,
                 'Difference in opening balances %s' % fy))

    def reconcile_line_tax(self):
        """Make each bill's line taxes add up to the tax on the bill itself.

        Three things pull them apart. A challan carries no tax, but SHREE still
        records on every line the GST it would attract once the sister firm
        bills it -- left alone, that tax would show on the challan's own lines
        and in every item-wise report. Before GST the tax was typed on the
        screen and never reached the lines at all. And where both do carry it,
        SHREE rounded each line separately, so thirteen lines could come to a
        paisa more than the bill they belong to.

        The bill's own figure is the one that was charged, so the lines are
        made to agree with it -- shared out in proportion to what each line is
        worth, with the last line taking whatever the rounding leaves.
        """
        conn, cid = self.conn, self.cid
        cols = ('cgst', 'sgst', 'igst', 'cess')
        fixed = 0
        for inv in conn.execute(
                'SELECT id, cgst, sgst, igst, cess FROM invoice '
                "WHERE company_id=? AND source='SHREE'", (cid,)).fetchall():
            lines = conn.execute(
                'SELECT id, taxable, cgst, sgst, igst, cess FROM invoice_item '
                'WHERE invoice_id=? ORDER BY id', (inv['id'],)).fetchall()
            if not lines:
                continue
            want = {c: r2(inv[c]) for c in cols}
            have = {c: r2(sum(float(l[c] or 0) for l in lines)) for c in cols}
            # SHREE keeps tax to three decimals, so a line can hold 10,094.895
            # -- not an amount anyone can pay. Those are brought to the paisa
            # as well, even where the rounded totals already agree.
            exact = all(abs(float(l[c] or 0) - r2(l[c])) < 0.0005
                        for l in lines for c in cols)
            if exact and all(abs(want[c] - have[c]) < 0.005 for c in cols):
                continue
            base = sum(abs(float(l['taxable'] or 0)) for l in lines)
            share = [abs(float(l['taxable'] or 0)) / base if base else
                     1.0 / len(lines) for l in lines]
            for n, line in enumerate(lines):
                last = (n == len(lines) - 1)
                vals = {}
                for c in cols:
                    if last:
                        vals[c] = r2(want[c] - sum(
                            r2(want[c] * share[k]) for k in range(len(lines) - 1)))
                    else:
                        vals[c] = r2(want[c] * share[n])
                conn.execute(
                    'UPDATE invoice_item SET cgst=?, sgst=?, igst=?, cess=? '
                    'WHERE id=?',
                    (vals['cgst'], vals['sgst'], vals['igst'], vals['cess'],
                     line['id']))
            fixed += 1
        if fixed:
            self.stats['lines_retaxed'] = fixed
        return fixed

    def balance_books(self):
        """Leave the trial balance balancing to the paisa.

        Two things stop it on their own. SHREE's own opening balances do not
        add to nil where a year's profit was never closed to capital, and a
        handful of vouchers survive in the backups with one side missing. Both
        are parked on the suspense account, where they are visible and named,
        rather than left to make every report a little wrong.
        """
        conn, cid = self.conn, self.cid
        sid = self.suspense()
        ob = conn.execute('SELECT coalesce(sum(opening_bal),0) s FROM ledger '
                          'WHERE company_id=?', (cid,)).fetchone()['s']
        po = conn.execute('SELECT coalesce(sum(debit-credit),0) s FROM posting '
                          'WHERE company_id=?', (cid,)).fetchone()['s']
        resid = r2(float(ob) + float(po))
        if abs(resid) < 0.01:
            return 0.0
        cur = conn.execute('SELECT opening_bal FROM ledger WHERE id=?',
                           (sid,)).fetchone()
        conn.execute('UPDATE ledger SET opening_bal=? WHERE id=?',
                     (r2(float(cur['opening_bal'] or 0) - resid), sid))
        self.stats['opening_difference'] = resid
        return resid

    # -- counters -----------------------------------------------------
    DOC_COUNTERS = [('SALE', 'SALE', 'invoice', 'doc_type', 'invoice_no'),
                    ('PRCH', 'PURCHASE', 'invoice', 'doc_type', 'invoice_no'),
                    ('CNT', 'CREDIT', 'invoice', 'doc_type', 'invoice_no'),
                    ('DNT', 'DEBIT', 'invoice', 'doc_type', 'invoice_no'),
                    ('RCPT', 'RECEIPT', 'voucher', 'kind', 'voucher_no'),
                    ('PYMT', 'PAYMENT', 'voucher', 'kind', 'voucher_no'),
                    ('JRNL', 'JOURNAL', 'voucher', 'kind', 'voucher_no'),
                    ('CTRA', 'CONTRA', 'voucher', 'kind', 'voucher_no')]

    def counters(self, folder, fy):
        """Set the next document number for the year.

        SHREE's own counter is the starting point, but it counts vouchers while
        this app numbers bills, and the challan books run several series at
        once. Whichever is higher -- SHREE's counter or the highest number
        actually imported -- decides, so the next document can never land on a
        number already used.
        """
        conn, cid = self.conn, self.cid
        path = dbf.find(folder, 'MMAS.DBF')
        rows = dbf.read(path) if path else []
        m = rows[0] if rows else {}
        for field, key, table, kind_col, no_col in self.DOC_COUNTERS:
            try:
                nxt = int(float(m.get(field) or 0))
            except (TypeError, ValueError):
                nxt = 0
            used = conn.execute(
                'SELECT max(CAST(%s AS INTEGER)) m FROM %s WHERE company_id=? '
                'AND fy=? AND %s=?' % (no_col, table, kind_col),
                (cid, fy, key)).fetchone()['m']
            # For receipts and journals SHREE's counter numbers the very same
            # documents, so it can be trusted. For bills it counts vouchers
            # while the books are numbered per challan series, so once any bill
            # has been imported the books themselves are the only guide.
            nxt = int(used) if used else nxt
            if key == 'SALE' and self.tax_mode == 'none':
                self._challan_book(fy)
                continue
            if nxt <= 0:
                continue
            row = conn.execute('SELECT 1 FROM counter WHERE company_id=? AND '
                               'key=? AND fy=?', (cid, key, fy)).fetchone()
            if row:
                conn.execute('UPDATE counter SET next_no=?, prefix=?, pad=0 '
                             'WHERE company_id=? AND key=? AND fy=?',
                             (nxt + 1, '', cid, key, fy))
            else:
                conn.execute('INSERT INTO counter(company_id, key, fy, prefix, '
                             'next_no, pad) VALUES (?,?,?,?,?,0)',
                             (cid, key, fy, '', nxt + 1))

    def _challan_book(self, fy):
        """Carry on in whichever challan book the last one came out of."""
        conn, cid = self.conn, self.cid
        last = conn.execute(
            "SELECT invoice_no FROM invoice WHERE company_id=? AND "
            "doc_type='SALE' AND fy=? ORDER BY invoice_date DESC, id DESC "
            'LIMIT 1', (cid, fy)).fetchone()
        if not last:
            return
        m = re.match(r'^([A-Za-z]+)\s+(\d+)$', (last['invoice_no'] or '').strip())
        if m:
            prefix = m.group(1).upper() + ' '
            hi = conn.execute(
                'SELECT max(CAST(substr(invoice_no, ?) AS INTEGER)) m FROM '
                "invoice WHERE company_id=? AND doc_type='SALE' AND fy=? AND "
                'invoice_no LIKE ?',
                (len(prefix) + 1, cid, fy, prefix + '%')).fetchone()['m'] or 0
        else:
            prefix = ''
            hi = conn.execute(
                'SELECT max(CAST(invoice_no AS INTEGER)) m FROM invoice WHERE '
                "company_id=? AND doc_type='SALE' AND fy=?",
                (cid, fy)).fetchone()['m'] or 0
        conn.execute(
            'INSERT INTO counter(company_id, key, fy, prefix, next_no, pad) '
            'VALUES (?,?,?,?,?,0) ON CONFLICT(company_id, key, fy) DO UPDATE '
            'SET prefix=excluded.prefix, next_no=excluded.next_no, pad=0',
            (cid, 'SALE', fy, prefix, hi + 1))

    # -- stock --------------------------------------------------------
    def anchor_stock(self, folder):
        """Set opening stock so today's figure matches SHREE's closing stock.

        SHREE keeps a running quantity per item. Whole years are missing from
        the backups, so adding up only the bills that survive would understate
        what is actually on the floor. The opening quantity is therefore set to
        whatever makes the closing figure agree with the last year on file.

        Only SHREE's own documents are set against its figure. A bill made in
        this app is not in SHREE's closing stock, so it moves the stock on
        from there -- counted here too, every update put the stock back to
        SHREE's figure as though the bill had never gone out. The quantity
        is the one the stock screens add up (api.STOCK_QTY), or the two
        would part on every return.
        """
        conn, cid = self.conn, self.cid
        path = dbf.find(folder, 'IMAS.DBF')
        if not path:
            return []
        report = []
        moved = defaultdict(float)
        for r in conn.execute(
                'SELECT ii.item_id, i.doc_type, SUM(' + api.STOCK_QTY + ') q '
                'FROM invoice_item ii JOIN invoice i ON i.id=ii.invoice_id '
                "WHERE i.company_id=? AND i.source='SHREE' "
                'AND ii.item_id IS NOT NULL GROUP BY ii.item_id, i.doc_type',
                (cid,)):
            moved[r['item_id']] += api.DOC_STOCK.get(r['doc_type'], 0) \
                * float(r['q'] or 0)
        # The last year's item master is the authority on what is on the floor.
        # Anything it no longer lists is out of stock as far as it is
        # concerned, so those are brought to nil rather than left drifting on
        # whatever the years that did survive happen to add up to.
        closing = {}
        for r in dbf.read(path, fields=('INIMAS', 'CSTQTY')):
            iid = self.item.get(norm(r.get('INIMAS')))
            if iid:
                closing[iid] = (float(r.get('CSTQTY') or 0),
                                (r.get('INIMAS') or '').strip())
        for iid, name, source in [
                (r['id'], r['name'], r['source']) for r in conn.execute(
                    'SELECT id, name, source FROM item WHERE company_id=?',
                    (cid,))]:
            if iid not in closing and source != 'SHREE':
                # Made in this app and unknown to SHREE: its opening stock
                # is whatever was typed for it here.
                continue
            want, label = closing.get(iid, (0.0, name))
            # To the gram, as SHREE keeps it: to the paisa, BSI's stock sat
            # up to 5 grams off SHREE's on nine items (2 Oct 2026).
            opening = half_up(want - moved.get(iid, 0.0), 3)
            conn.execute('UPDATE item SET opening_qty=? WHERE id=?',
                         (opening, iid))
            if want or moved.get(iid) or opening:
                report.append((label, want, moved.get(iid, 0.0), opening))
        report.sort(key=lambda x: -abs(x[1]))
        return report


# -------------------------------------------------------------------- driver

def ensure_company(conn, spec, cm):
    """Find or create the firm, and top up whatever its record is missing."""
    code = spec['code']
    name = (cm.get('CNAM') or '').strip().strip('*').strip() or spec['name']
    gstin = (cm.get('GSTNO') or '').strip().upper()
    if len(gstin) != 15:
        gstin = ''
    state = (cm.get('STATE') or '').strip().title()
    state_code = gstin[:2] if gstin else ''
    if state_code and not state:
        state = api.STATE_NAME.get(state_code, '')
    fields = dict(
        name=spec['name'] if spec['name'] else name,
        address1=(cm.get('ADD1') or '').strip(),
        address2=' '.join(x for x in [(cm.get('ADD2') or '').strip(),
                                      (cm.get('ADD3') or '').strip()] if x),
        city=(cm.get('CITY') or '').strip(), state=state,
        state_code=state_code, gstin=gstin,
        pan=(cm.get('PAN_NO') or '').strip().replace('-', ''),
        phone=(cm.get('PHONE') or '').strip(),
        mobile=(cm.get('MOBILE') or '').strip(),
        email=(cm.get('EMAIL') or '').strip(),
        bank_name=(cm.get('BANKNAME') or '').strip(),
        bank_ac=(cm.get('BANKACNO') or '').strip(),
        bank_ifsc=(cm.get('IFSCNO') or '').strip(),
        bank_branch=(cm.get('BANKADD') or '').strip())

    row = conn.execute('SELECT * FROM company WHERE code=?', (code,)).fetchone()
    if row:
        cid = row['id']
        sets, args = [], []
        for col, val in fields.items():
            if val and not (row[col] or '').strip():
                sets.append('%s=?' % col)
                args.append(val)
        # The name and the tax setting say what kind of book this firm is, so
        # they are corrected rather than merely filled in -- an accounts book
        # wrongly set up as a GST firm would add tax to every challan.
        if (row['name'] or '').strip() != spec['name']:
            sets.append('name=?')
            args.append(spec['name'])
        if (row['tax_mode'] or 'gst') != spec['tax_mode']:
            sets.append('tax_mode=?')
            args.append(spec['tax_mode'])
        if sets:
            args.append(cid)
            conn.execute('UPDATE company SET %s WHERE id=?' % ','.join(sets),
                         args)
        return cid, False
    made = dict(fields, code=code, tax_mode=spec['tax_mode'],
                terms=db.DEFAULT_TERMS)
    made.update(NEW_FIRM_DETAILS.get(code) or {})
    cid = db.create_company(conn, made)
    drop_placeholder(conn)
    if not db.get_meta(conn, 'shree_last_update'):
        # A new book's first update: it opens on the accounts book, where
        # the day's challans are made, whichever firm happened to be read
        # first -- and takes the settings the old books had.
        if code == 'BSTP':
            db.set_meta(conn, 'active_company', str(cid))
        for key, value in NEW_BOOK_META.items():
            if db.get_meta(conn, key) is None:
                db.set_meta(conn, key, value)
    return cid, True


def drop_placeholder(conn):
    """The "My Company" a new Billing starts with, gone once a real firm is in.

    db.init makes it so the screens have a firm to stand on before the first
    update. Left behind it was a fourth firm, empty, and the one the app
    opened on (6 Oct 2026, a PC set up from an empty folder) -- the app holds
    BSTP, BST and BSI and nothing else. Only while it holds nothing: no
    document, voucher, order or posting, and no account of its own beyond
    the ones every firm is given. Nothing is committed; the import does that.
    """
    for r in conn.execute("SELECT id FROM company WHERE code='MAIN' "
                          "AND name='My Company'").fetchall():
        pid = r['id']
        if any(conn.execute('SELECT 1 FROM %s WHERE company_id=? LIMIT 1' % t,
                            (pid,)).fetchone()
               for t in ('invoice', 'voucher', 'sales_order', 'posting')):
            continue
        if conn.execute('SELECT 1 FROM ledger WHERE company_id=? AND '
                        'system_key IS NULL LIMIT 1', (pid,)).fetchone():
            continue
        tables = [t for (t,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
            if t != 'company' and any(c['name'] == 'company_id' for c in
                                      conn.execute('PRAGMA table_info(%s)' % t))]
        # Accounts point at their group, so they go first.
        for t in sorted(tables, key=lambda t: t != 'ledger'):
            conn.execute('DELETE FROM %s WHERE company_id=?' % t, (pid,))
        conn.execute('DELETE FROM company WHERE id=?', (pid,))
        if db.get_meta(conn, 'active_company') in (None, str(pid)):
            first = conn.execute("SELECT id FROM company ORDER BY code<>'BSTP', "
                                 'id').fetchone()
            if first:
                db.set_meta(conn, 'active_company', str(first['id']))


INV_KINDS = ('SALE', 'PURCHASE', 'CREDIT', 'DEBIT')
VOU_KINDS = ('RECEIPT', 'PAYMENT', 'JOURNAL', 'CONTRA')


def remembered_ids(conn, cid):
    """The ids this import gave its rows last time, by what each row is.

    Every run replaces the rows it wrote. They used to come back under new
    ids, so after an update a bill open on a screen named some other bill,
    and every page had to be reloaded. Each row now gets its old id back --
    a document by its kind, year and number, an account or an item by its
    name -- and only what SHREE really added or took away comes and goes.
    """
    ids = dict(invoice={}, voucher={}, ledger={}, item={})
    for r in conn.execute(
            'SELECT id, doc_type, fy, series, invoice_no FROM invoice '
            "WHERE company_id=? AND source='SHREE'", (cid,)):
        ids['invoice'][(r['doc_type'], r['fy'], r['series'] or '',
                        r['invoice_no'])] = r['id']
    for r in conn.execute(
            'SELECT id, kind, fy, voucher_no FROM voucher '
            "WHERE company_id=? AND source='SHREE'", (cid,)):
        ids['voucher'][(r['kind'], r['fy'], r['voucher_no'])] = r['id']
    for table in ('ledger', 'item'):
        for r in conn.execute(
                "SELECT id, name FROM %s WHERE company_id=? AND source='SHREE' "
                'ORDER BY id' % table, (cid,)):
            ids[table].setdefault(norm(r['name']), r['id'])
    return ids


REKIND = {'CREDIT': 'SALE', 'DEBIT': 'PURCHASE'}

# SHREE's GST accounts, by name: TAX - CGST A/C, TAX - IGST A/C ...
TAX_HEAD = re.compile(r'\b(CGST|SGST|UTGST|IGST|CESS)\b')
TAX_GROUP = '3201'                      # Duties & Taxes


def rekind_cash_memos(conn, cid=None):
    """Turn the cash memos an earlier import took for notes back into bills.

    Until 19 Sep 2026 a bill SHREE prints as a CASH MEMO (BTSALE 'C') came
    in as a credit note -- 150 in BST and 98 in BSI -- so its goods went back
    into stock, and it stayed out of the GST summary and the sales registers
    and started its own run of credit-note numbers. The import now files them
    as the bills they are (see MEMO_OF). They are turned round here, in place
    and under the ids they already have, before anything compares the books
    by kind, year and number. Left to the import, each would read as one bill
    gone and another come: a SHREE update would refuse to go on, and a page
    open on one would be told it had been deleted.

    Each document takes with it its postings' kind, the rules written for it
    (a deletion kept here, an edit), the credit-note counters numbered off it
    and its share of the opening stock -- so stock still closes where SHREE
    closes it until the next import anchors it again. Running it again finds
    nothing to do. Nothing is committed; the caller does that.
    """
    out = dict(documents=0, rules=0, counters=0, clashes=[], edited=[])
    code = {r['id']: r['code'] for r in conn.execute(
        'SELECT id, code FROM company')}
    where, args = ("source='SHREE' AND invoice_type='Credit' "
                   "AND doc_type IN ('CREDIT','DEBIT')"), []
    if cid:
        where += ' AND company_id=?'
        args.append(cid)
    opening = defaultdict(float)    # item -> added back to its opening
    top = defaultdict(int)          # (firm, old kind, fy) -> highest number
    for d in conn.execute('SELECT id, company_id, doc_type, fy, series, '
                          'invoice_no FROM invoice WHERE ' + where,
                          args).fetchall():
        old, new = d['doc_type'], REKIND[d['doc_type']]
        if conn.execute(
                'SELECT 1 FROM invoice WHERE company_id=? AND doc_type=? AND '
                "fy=? AND COALESCE(series,'')=? AND invoice_no=?",
                (d['company_id'], new, d['fy'], d['series'] or '',
                 d['invoice_no'])).fetchone():
            # The number is taken in the bill run; the import renumbers one
            # of the two, as it does any number SHREE let through twice.
            out['clashes'].append('%s %s' % (code.get(d['company_id']),
                                             api.doc_key(old, d['fy'],
                                                         d['series'],
                                                         d['invoice_no'])))
            continue
        turn = api.DOC_STOCK[new] - api.DOC_STOCK[old]
        for ln in conn.execute(
                'SELECT item_id, ' + api.STOCK_QTY + ' q FROM invoice_item ii '
                'WHERE invoice_id=? AND item_id IS NOT NULL', (d['id'],)):
            opening[ln['item_id']] -= turn * float(ln['q'] or 0)
        conn.execute("UPDATE invoice SET doc_type=?, invoice_type='Cash' "
                     'WHERE id=?', (new, d['id']))
        conn.execute('UPDATE posting SET doc_kind=? WHERE doc_kind=? AND '
                     'doc_id=?', (new, old, d['id']))
        m = re.match(r'^\d+$', (d['invoice_no'] or '').strip())
        if m:
            k = (d['company_id'], old, d['fy'])
            top[k] = max(top[k], int(m.group(0)))
        out['documents'] += 1
    for iid, q in opening.items():
        if abs(q) > 1e-9:
            conn.execute('UPDATE item SET opening_qty=ROUND(opening_qty+?, 3) '
                         'WHERE id=?', (q, iid))

    # A document deleted or edited here is known to the update by its key,
    # and every SHREE document ever filed as a note was one of these.
    firm = code.get(cid) if cid else None
    for r in conn.execute(
            "SELECT id, kind, firm, name FROM sync_rule WHERE kind IN "
            "('delete_doc','supersede_doc') AND (name LIKE 'CREDIT|%' OR "
            "name LIKE 'DEBIT|%')").fetchall():
        if firm and r['firm'] != firm:
            continue
        kind, rest = r['name'].split('|', 1)
        if r['kind'] == 'supersede_doc':
            # Edited here, it became this app's own and was posted as a note
            # -- a return, the wrong way round. That is the user's own entry
            # to put right; it is only named.
            fy, series, no = (rest.split('|') + ['', ''])[:3]
            own = conn.execute(
                "SELECT i.id FROM invoice i JOIN company c ON "
                "c.id=i.company_id WHERE c.code=? AND i.doc_type=? AND i.fy=? "
                "AND COALESCE(i.series,'')=? AND i.invoice_no=? AND "
                "COALESCE(i.source,'')<>'SHREE'",
                (r['firm'], kind, fy, series, no)).fetchone()
            if own:
                out['edited'].append('%s %s' % (r['firm'], r['name']))
        conn.execute('UPDATE sync_rule SET name=? WHERE id=?',
                     ('%s|%s' % (REKIND[kind], rest), r['id']))
        out['rules'] += 1

    # The counters the import and the repairs numbered off these documents:
    # nothing of that kind is left in the year, so they would start the
    # firm's first real credit note at the next cash memo's number.
    for (co, kind, fy), hi in top.items():
        if conn.execute('SELECT 1 FROM invoice WHERE company_id=? AND '
                        'doc_type=? AND fy=? LIMIT 1',
                        (co, kind, fy)).fetchone():
            continue
        out['counters'] += conn.execute(
            "DELETE FROM counter WHERE company_id=? AND key=? AND fy=? AND "
            "prefix='' AND next_no=?", (co, kind, fy, hi + 1)).rowcount
    return out


def wipe(conn, cid):
    """Clear what a previous run of this import left, and nothing else.

    Documents billed in the app itself carry no source, so they survive; only
    rows marked SHREE are replaced. A challan imported here may already be
    linked to a bill in another firm, so those links are let go first --
    otherwise the delete trips the foreign keys.
    """
    inv = [r['id'] for r in conn.execute(
        "SELECT id FROM invoice WHERE company_id=? AND source='SHREE'", (cid,))]
    vou = [r['id'] for r in conn.execute(
        "SELECT id FROM voucher WHERE company_id=? AND source='SHREE'", (cid,))]
    orders = [r['id'] for r in conn.execute(
        "SELECT id FROM sales_order WHERE company_id=? AND source='SHREE'",
        (cid,))]
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS _gone(id INTEGER PRIMARY KEY)')
    conn.execute('DELETE FROM _gone')
    conn.executemany('INSERT OR IGNORE INTO _gone(id) VALUES (?)',
                     [(i,) for i in inv])

    for table, col in (('invoice', 'from_challan'), ('invoice', 'billed_invoice'),
                       ('sales_order', 'billed_invoice')):
        conn.execute('UPDATE %s SET %s=NULL WHERE %s IN (SELECT id FROM _gone)'
                     % (table, col, col))
    conn.execute('UPDATE invoice SET order_id=NULL WHERE order_id IN '
                 "(SELECT id FROM sales_order WHERE company_id=? AND "
                 "source='SHREE')", (cid,))

    conn.execute(
        "DELETE FROM posting WHERE company_id=? AND (doc_kind IN "
        "('IMPORT','OPENING') OR (doc_kind IN (%s) AND doc_id IN "
        "(SELECT id FROM _gone)))" % ','.join('?' * len(INV_KINDS)),
        (cid,) + INV_KINDS)
    if vou:
        conn.execute('DELETE FROM _gone')
        conn.executemany('INSERT OR IGNORE INTO _gone(id) VALUES (?)',
                         [(i,) for i in vou])
        conn.execute(
            'DELETE FROM posting WHERE company_id=? AND doc_kind IN (%s) AND '
            'doc_id IN (SELECT id FROM _gone)'
            % ','.join('?' * len(VOU_KINDS)), (cid,) + VOU_KINDS)

    conn.executemany('DELETE FROM invoice_item WHERE invoice_id=?',
                     [(i,) for i in inv])
    conn.executemany('DELETE FROM invoice WHERE id=?', [(i,) for i in inv])
    conn.executemany('DELETE FROM voucher_line WHERE voucher_id=?',
                     [(i,) for i in vou])
    conn.executemany('DELETE FROM voucher WHERE id=?', [(i,) for i in vou])
    conn.executemany('DELETE FROM sales_order_item WHERE order_id=?',
                     [(i,) for i in orders])
    conn.executemany('DELETE FROM sales_order WHERE id=?',
                     [(i,) for i in orders])
    conn.execute('DROP TABLE IF EXISTS _gone')
    _drop_orphan_masters(conn, cid)


def _drop_orphan_masters(conn, cid):
    """Clear away imported accounts and items nothing points at any more.

    A firm's chart of accounts belongs to whichever book it came from, and the
    two books of one firm share almost nothing. Without this, re-pointing a
    firm at a different book would leave the old book's customers sitting
    beside the new ones -- two sets of accounts inside one firm, which is
    exactly what must not happen. Anything still referenced stays, so an
    account a hand-entered bill uses is never removed.
    """
    # Everything that points at an account or an item, gathered once: asked
    # as eight NOT IN lists it took eleven seconds a firm.
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS _used(id INTEGER PRIMARY KEY)')
    conn.execute('DELETE FROM _used')
    for sql in ('SELECT party_id FROM invoice',
                'SELECT sales_ac_id FROM invoice',
                'SELECT party_id FROM sales_order',
                'SELECT party_id FROM voucher',
                'SELECT bank_id FROM voucher',
                'SELECT ledger_id FROM voucher_line',
                'SELECT ledger_id FROM posting',
                'SELECT linked_ledger FROM ledger'):
        conn.execute('INSERT OR IGNORE INTO _used(id) %s WHERE %s IS NOT NULL'
                     % (sql, sql.split()[1]))
    conn.execute(
        "DELETE FROM ledger WHERE company_id=? AND source='SHREE' "
        'AND system_key IS NULL AND id NOT IN (SELECT id FROM _used)', (cid,))
    conn.execute('DELETE FROM _used')
    # Stock added or removed by hand in this app names its item too, and an
    # item it names is kept (27 Sep 2026).
    for sql in ('SELECT item_id FROM invoice_item',
                'SELECT item_id FROM sales_order_item',
                'SELECT item_id FROM stock_entry_line'):
        conn.execute('INSERT OR IGNORE INTO _used(id) %s WHERE item_id IS NOT NULL'
                     % sql)
    conn.execute(
        "DELETE FROM item WHERE company_id=? AND source='SHREE' "
        'AND id NOT IN (SELECT id FROM _used)', (cid,))
    conn.execute('DROP TABLE IF EXISTS _used')


def import_firm(conn, spec, years, cm, verbose=True):
    cid, fresh = ensure_company(conn, spec, cm)
    if verbose:
        print('\n%-5s %-42s %s(id %d)%s'
              % (spec['code'], spec['name'],
                 'no tax  ' if spec['tax_mode'] == 'none' else 'GST     ',
                 cid, '  [created]' if fresh else ''))
    # Before the ids are remembered, so the cash memos keep theirs.
    turned = rekind_cash_memos(conn, cid)
    if verbose and turned['documents']:
        print('   %d cash memos filed as credit notes turned back into bills'
              % turned['documents'])
    old_ids = remembered_ids(conn, cid)
    wipe(conn, cid)
    firm = Firm(conn, cid, spec['tax_mode'], old_ids)
    order = sorted(years)
    last_folder = None
    had = set()                 # every account an earlier year's masters had
    for n, fy in enumerate(order):
        src = years[fy]
        last_folder = src.folder
        firm.groups(src.folder)
        acod, openings = firm.ledgers(src.folder, fy, first_year=(n == 0))
        icod, dept = firm.items(src.folder, first_year=(n == 0))
        if n:
            firm.carry_forward(fy, acod, openings,
                               dropped=had - set(acod.values()))
        had |= set(acod.values())
        made = firm.documents(src.folder, fy, acod, icod, dept)
        firm.tmas(src.folder, fy, acod, made)
        firm.counters(src.folder, fy)      # after the year's documents exist
        if verbose:
            print('   %-6s %-26s %5d ledgers  %5d bills  %5d postings'
                  % (fy, src.origin[:26],
                     len(acod), len(made), firm.stats['postings']))
    stock = firm.anchor_stock(last_folder) if last_folder else []
    if spec['tax_mode'] == 'none':
        # Each challan line the steel type its words name (4 Oct 2026).
        db.fill_steel_types(conn, cid)
    firm.reconcile_line_tax()
    firm.balance_books()
    firm.save_year_groups()
    conn.commit()
    return cid, firm.stats, stock


# Which kind of SHREE book each app firm is: a challan in the accounts book,
# a bill in a register.
FIRM_BOOK = {spec['code']: role for (role, _c), spec in FIRMS.items()}


def check_against(conn, cid, fy, folder):
    """What of SHREE's own year folder is not in the books just written:
    ([(kind, date, amount) of documents], [... of vouchers], totals).

    Read straight from SHREE's folder, not from the copy or backup the
    import read: on 2 Oct 2026 the import read last night's backup in place
    of the year itself and said "Updated" while that day's challans were
    missing. Bills and challans are matched by date and total, vouchers by
    kind, date and amount -- numbers can be moved to keep them unique."""
    want_d, want_v, party = Counter(), Counter(), {}
    path = dbf.find(folder, 'AMAS.DBF')
    names = {_ival(r.get('ACOD')): (r.get('ANAM') or '').strip()
             for r in (dbf.read(path, fields=('ACOD', 'ANAM')) if path else [])}
    for table, date_f, party_f in (('SALE', 'BDSALE', 'CCSALE'),
                                   ('PRCH', 'BDPRCH', 'VCPRCH')):
        path = dbf.find(folder, table + '.DBF')
        if not path:
            continue
        for r in dbf.read(path, fields=(date_f, 'TOTAL2', party_f)):
            if r.get(date_f):
                key = (table, r[date_f], abs(round(float(r.get('TOTAL2') or 0))))
                want_d[key] += 1
                party.setdefault(key, names.get(_ival(r.get(party_f)), ''))
    path = dbf.find(folder, 'TMAS.DBF')
    groups = defaultdict(list)
    for r in (dbf.read(path, fields=('DATE', 'VHTY', 'VHNO', 'AMNT'))
              if path else []):
        kind = VOUCHER_KIND.get((r.get('VHTY') or '').strip())
        if kind and (r.get('DATE') or '').strip() and (r.get('AMNT') or 0):
            groups[(kind, int(r.get('VHNO') or 0))].append(r)
    for (kind, _no), rows in groups.items():
        want_v[(kind, rows[0]['DATE'].strip(),
                r2(sum(-float(r['AMNT']) for r in rows if r['AMNT'] < 0)))] += 1
    have_d, have_v = Counter(), Counter()
    for r in conn.execute("SELECT doc_type, invoice_date, grand_total FROM "
                          "invoice WHERE company_id=? AND fy=? AND "
                          "source='SHREE'", (cid, fy)):
        have_d[('PRCH' if r[0] in ('PURCHASE', 'DEBIT') else 'SALE', r[1],
                abs(round(r[2] or 0)))] += 1
    for r in conn.execute("SELECT kind, voucher_date, amount FROM voucher "
                          "WHERE company_id=? AND fy=? AND source='SHREE'",
                          (cid, fy)):
        have_v[(r[0], r[1], r2(r[2]))] += 1
    return ([k + (party.get(k, ''),) for k in
             sorted((want_d - have_d).elements())],
            sorted((want_v - have_v).elements()),
            (sum(want_d.values()), sum(want_v.values())))


def _check_line(code, fy, folder, conn, cid):
    """One line of the check for the update to read: 'check: ... all in'
    or 'check: ... N not in: ...'."""
    where = os.sep.join(os.path.normpath(folder).split(os.sep)[-2:])
    try:
        docs, vous, (nd, nv) = check_against(conn, cid, fy, folder)
    except dbf.Busy as e:
        return ('  check: %s %s not checked against SHREE\'s own %s -- %s'
                % (code, fy, where, e))
    if not docs and not vous:
        return ('  check: %s %s all in -- all %d documents and %d vouchers '
                'in SHREE\'s own %s' % (code, fy, nd, nv, where))
    sale = 'challan' if FIRM_BOOK.get(code) == 'books' else 'bill'

    def day(d):
        return '-'.join(reversed(d.split('-')))
    said = ['%s %s %s %s' % (sale if k == 'SALE' else 'purchase', day(d),
                             who, '{:,}'.format(a))
            for k, d, a, who in docs[:10]]
    said += ['%s %s %s' % (k.lower(), day(d), '{:,.2f}'.format(a))
             for k, d, a in vous[:10]]
    more = len(docs) + len(vous) - len(said)
    return ('  check: %s %s %d not in -- of SHREE\'s own %s: %s%s'
            % (code, fy, len(docs) + len(vous), where, '; '.join(said),
               ' and %d more' % more if more > 0 else ''))


def main():
    db.quiet_windows_errors()
    # Run by hand in a Windows console (cp437 / cp1252), a SHREE name with a
    # byte that code page lacks would stop the import in the middle of a
    # print. Run by the app, the output is UTF-8 already.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', action='append', default=None,
                    help='a SHREE folder; repeat for each machine')
    ap.add_argument('--work', help='where to unpack the ZIPs (kept between runs)')
    ap.add_argument('--company', action='append',
                    help='app firm code to import; repeatable (default: all)')
    ap.add_argument('--from-year', help='earliest financial year, e.g. 20-21')
    ap.add_argument('--list', action='store_true',
                    help='show what was found and import nothing')
    ap.add_argument('--json', help='with --list: also write what was found, '
                                   'firm by firm, to this file')
    ap.add_argument('--keep-work', action='store_true',
                    help='leave the unpacked ZIPs behind')
    ap.add_argument('--direct', action='store_true',
                    help="read SHREE's folders where they are, without "
                         'bringing a copy up to date first')
    ap.add_argument('--deep', action='store_true',
                    help='every year looked into, copied by its contents and '
                         'checked against SHREE -- Fetch everything')
    ap.add_argument('--prune-work', action='store_true',
                    help='with --keep-work: clear unpacked ZIPs no archive '
                         'stands behind any more')
    args = ap.parse_args()
    if args.deep:
        DEEP.append(True)

    roots = args.source or ['SHREE BST', 'SHREE BILL']
    for r in roots:
        if not os.path.isdir(r):
            raise SystemExit('Source folder not found: %s' % r)
    work = args.work or os.path.join(tempfile.gettempdir(), 'shree_unpack')
    private_dir(work)

    load_probes(work)
    # A table SHREE holds for a moment -- saving, reindexing, its backup --
    # is waited for rather than read from an older copy.
    try:
        dbf.PATIENCE = float(os.environ.get('BILLING_SHREE_WAIT') or 15)
    except ValueError:
        dbf.PATIENCE = 15.0
    sources = []
    masters = []
    for root in roots:
        label = os.path.basename(root.rstrip('/\\')) or root
        home, places = source_places(root)
        print('Reading %s ...' % home)
        found = scan_year_folders(home, label)
        print('  %d year folders' % len(found))
        sources += found
        zips, cmas = survey(places)
        masters += cmas
        found = scan_zips(home, work, label, archives=zips)
        print('  %d snapshots in its backup archives (%s)'
              % (len(found), ', '.join(os.path.basename(p) for p in places[1:])
                 or 'none beside it'))
        sources += found

    best = pick_best(sources)
    cm = company_master(files=masters)
    save_probes(work)

    by_code = defaultdict(dict)
    for (code, fy), src in best.items():
        if args.from_year and fy < args.from_year:
            continue
        by_code[code][fy] = src

    specs = {}
    for spec in FIRMS.values():
        specs[spec['code']] = spec

    print('\nFirms found')
    print('  %-6s %-42s %-8s %-6s %s' % ('code', 'firm', 'book', 'years', 'range'))
    for code in sorted(by_code):
        spec = specs[code]
        yrs = sorted(by_code[code])
        kind = 'accounts' if spec['tax_mode'] == 'none' else 'GST bills'
        print('  %-6s %-42s %-8s %-6d %s .. %s'
              % (code, spec['name'][:42], kind, len(yrs), yrs[0], yrs[-1]))
        for fy in yrs:
            s = by_code[code][fy]
            print('        %-6s %-38s %6d rows  to %s'
                  % (fy, s.origin[:38], s.rows, s.last or '-'))
    for folder, firm, fy, why in BUSY:
        print('  %s: SHREE is holding it to itself (%s); passed over this '
              'time' % (folder, why))
    if args.json:
        found = {}
        for code, years in by_code.items():
            found[code] = dict(
                newest=max((s.upto or '' for s in years.values()), default=''),
                # The day SHREE last wrote its books: a folder written on or
                # after the day the books run to is not an old copy, even
                # when its newest entry was re-dated or deleted.
                saved=max((s.saved or '' for s in years.values()), default=''),
                years={fy: dict(origin=s.origin, rows=s.rows, upto=s.upto,
                                backup=_norm(s.folder).startswith(
                                    _norm(work) + os.sep), own=s.own)
                       for fy, s in years.items()})
        # A year SHREE is holding could not be looked into, so which kind of
        # book it is is not known; the code SHREE files it under says.
        busy = [dict(folder=f, year=fy_full(y), why=w,
                     firm=(FIRMS.get(('bills', c)) or FIRMS.get(('books', c))
                           or {}).get('code', c))
                for f, c, y, w in BUSY]
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(dict(firms=found, busy=busy), fh)
    if args.list:
        return

    wanted = [c.upper() for c in (args.company or [])] or sorted(by_code)

    # Every year to be read, brought up to date on this computer first. A
    # firm whose year SHREE snatched back between the look and the copy,
    # with no copy from before to stand in, is left as it is this time --
    # the other firms still come in.
    unpacked = _norm(work) + os.sep
    passed_over = set()
    read_now = set()            # held while looked into, let go by the copy
    recent = recent_years()
    stood_in = {}               # folder -> what was read in its place
    # SHREE's own folder of this year and last, firm by firm, as it is:
    # what the books are checked against once the import is done.
    own_of = {}
    for s in sources:
        if s.own and s.target in wanted and (s.fy[2:] in recent or DEEP):
            cur = own_of.get((s.target, s.fy))
            if cur is None or _newer(s) > _newer(cur[0]):
                own_of[(s.target, s.fy)] = (s, s.folder)
    # A year SHREE held while it was looked into is checked all the same --
    # or said to be unchecked -- never left out of the check unsaid.
    for folder, firm, fy, _why in BUSY:
        code = (FIRMS.get(('bills', firm)) or FIRMS.get(('books', firm))
                or {}).get('code')
        if code in wanted and (fy in recent or DEEP) and \
                (code, fy_full(fy)) not in own_of:
            own_of[(code, fy_full(fy))] = (None, folder)
    # A year SHREE held while it was looked into, with nothing read of it
    # before: one of SHREE's own backups was picked in its place. Said, so
    # the update does not claim to be up to date with SHREE.
    for folder, firm, fy, why in BUSY:
        code = (FIRMS.get(('bills', firm)) or FIRMS.get(('books', firm))
                or {}).get('code')
        src = by_code.get(code, {}).get(fy_full(fy))
        if code in wanted and src and _norm(src.folder).startswith(unpacked):
            STALE.append((folder, why))
            stood_in[folder] = ('read from SHREE\'s backup %s instead, to %s'
                                % (src.origin.split(' ')[-1], src.upto or '-'))
    for code in ([] if args.direct else list(wanted)):
        own = {_norm(s.folder) for s in by_code.get(code, {}).values()}
        try:
            for fy, src in list(by_code.get(code, {}).items()):
                if _norm(src.folder).startswith(unpacked):
                    continue
                live, held = src.folder, len(STALE)
                try:
                    src.folder = mirror(live, work,
                                        verify=fy[2:] in recent or bool(DEEP))
                except dbf.Busy as e:
                    # Never copied before: one of SHREE's own backups of the
                    # year is read rather than nothing at all.
                    STALE.append((live, str(e)))
                    src.folder = None
                if len(STALE) > held:
                    by_code[code][fy] = _stand_in(src, live, sources,
                                                  unpacked, stood_in)
                else:
                    read_now.add(_norm(live))
        except dbf.Busy as e:
            wanted.remove(code)
            passed_over |= own
            print('%s: skipped -- SHREE is holding its books to itself (%s)'
                  % (code, e))
    # A year read from what was kept last time is said so -- but not for a
    # firm passed over, of which nothing was read at all: on the update's
    # screen the two sat side by side, "not updated" and "read as it was".
    for folder, why in sorted(set(STALE)):
        if _norm(folder) in passed_over or _norm(folder) in read_now:
            continue
        print('  %s: SHREE is using it (%s); %s'
              % (folder, why, stood_in.get(folder)
                 or 'read as it was at the last update'))
    if args.keep_work and args.prune_work:
        prune_work(work, copies=not args.direct)
    db.init()
    conn = db.connect()
    try:
        totals = defaultdict(int)
        for code in wanted:
            if code not in by_code:
                print('\n%s: nothing found, skipped.' % code)
                continue
            spec = specs[code]
            # A firm is one transaction: anything that stops it takes back
            # what it had written, and the other firms still come in. One
            # odd record used to stop the update for all three.
            try:
                cid, stats, stock = import_firm(
                    conn, spec, by_code[code],
                    cm.get(FIRM_MASTER.get(code, code), {}))
            except Exception as e:                 # noqa: BLE001
                conn.rollback()
                traceback.print_exc(file=sys.stdout)
                print('%s: skipped -- its books could not be read (%s: %s)'
                      % (code, type(e).__name__, str(e)[:200]))
                continue
            for (c, fy), (_s, folder) in sorted(own_of.items()):
                if c == code and fy in by_code[code]:
                    print(_check_line(code, fy, folder, conn, cid))
            print('   ' + '-' * 62)
            money = ('carry_value', 'docs_odd_value', 'opening_difference')
            for k in sorted(stats):
                print('   %-22s %s' % (k, ('%.2f' % stats[k])
                                       if k in money else stats[k]))
                totals[k] += stats[k]
            if stock:
                print('   closing stock anchored to SHREE (%d items, top 6):'
                      % len(stock))
                for name, closing, moved, opening in stock[:6]:
                    print('     %-28s closing %12.2f  from bills %12.2f  '
                          'opening set %12.2f'
                          % (name.strip()[:28], closing, moved, opening))
        print('\n' + '=' * 66)
        print('Totals across every firm')
        money = ('carry_value', 'docs_odd_value', 'opening_difference')
        for k in sorted(totals):
            print('  %-24s %s' % (k, ('%.2f' % totals[k])
                                  if k in money else totals[k]))
    finally:
        conn.close()
    if not args.keep_work:
        rmtree(work)
    # Both of these read what has just landed and write what SHREE never
    # recorded, and a re-import drops what they wrote along with the rows it
    # replaces -- so they belong at the end of every fetch, in this order.
    print('\nDone. Two things to run now that the data is in:\n'
          '  python3 app/allocate_customers.py --apply'
          '     which firm bills each customer\n'
          '  python3 app/link_challans.py --apply'
          '        which bill came from which challan\n'
          'Then start the app and pick the firm from the top-left selector.')


if __name__ == '__main__':
    main()
