"""Business logic + JSON API handlers.

Documents never store balances. Saving a document rewrites its double-entry
rows in `posting`, and every ledger report is derived from that one table.
"""
import bisect
import collections
import csv
import datetime
import functools
import glob
import io
import itertools
import json
import os
import re
import sqlite3
import threading

import db
import gst as gstcalc
import verify
from allocate_customers import BRIDGE_HINTS

STATES = [
    ('01', 'Jammu & Kashmir'), ('02', 'Himachal Pradesh'), ('03', 'Punjab'),
    ('04', 'Chandigarh'), ('05', 'Uttarakhand'), ('06', 'Haryana'),
    ('07', 'Delhi'), ('08', 'Rajasthan'), ('09', 'Uttar Pradesh'),
    ('10', 'Bihar'), ('11', 'Sikkim'), ('12', 'Arunachal Pradesh'),
    ('13', 'Nagaland'), ('14', 'Manipur'), ('15', 'Mizoram'),
    ('16', 'Tripura'), ('17', 'Meghalaya'), ('18', 'Assam'),
    ('19', 'West Bengal'), ('20', 'Jharkhand'), ('21', 'Odisha'),
    ('22', 'Chhattisgarh'), ('23', 'Madhya Pradesh'), ('24', 'Gujarat'),
    ('26', 'Dadra & Nagar Haveli and Daman & Diu'), ('27', 'Maharashtra'),
    ('29', 'Karnataka'), ('30', 'Goa'), ('31', 'Lakshadweep'),
    ('32', 'Kerala'), ('33', 'Tamil Nadu'), ('34', 'Puducherry'),
    ('35', 'Andaman & Nicobar Islands'), ('36', 'Telangana'),
    ('37', 'Andhra Pradesh'), ('38', 'Ladakh'), ('97', 'Other Territory'),
]
STATE_NAME = dict(STATES)

UNITS = ['NOS', 'PCS', 'KGS', 'MTS', 'QTL', 'LTR', 'MTR', 'SQF', 'BOX',
         'BAG', 'BDL', 'SET', 'PAIR', 'ROLL', 'TON', 'UNT']

# Matches the SHREE AMAS "ATYPE" column.
GST_TYPES = ['Regular', 'Composition', 'UnRegistered', 'SEZ',
             'Not Applicable']

DOC_TYPES = ('SALE', 'PURCHASE', 'CREDIT', 'DEBIT')
VOUCHER_KINDS = ('RECEIPT', 'PAYMENT', 'JOURNAL', 'CONTRA')

DOC_LABEL = {'SALE': 'Tax Invoice', 'PURCHASE': 'Purchase Bill',
             'CREDIT': 'Credit Note', 'DEBIT': 'Debit Note'}

# What an entry is called on an account. SHREE writes these to four letters --
# Jrnl, Pymt, Rcpt -- because a DOS column was that wide; the words are the
# same. A sale in a no-tax book is a challan and is named so there.
POSTING_LABEL = {'SALE': 'Bill', 'PURCHASE': 'Purchase',
                 'CREDIT': 'Credit Note', 'DEBIT': 'Debit Note',
                 'RECEIPT': 'Receipt', 'PAYMENT': 'Payment',
                 'JOURNAL': 'Journal', 'CONTRA': 'Contra',
                 'OPENING': 'Opening'}

# The line SHREE prints under each entry, from its own ledger:
#     20/05/24  SALES A/C                              700.00
#               Sale Bill No.: B100
#     28/05/24  KOTAK BANK : B. S. TRADERS   50,000.00
#               Rcpt Chq/Ref No:IB
#     30/05/24  BALKRISHNA STEEL TRADERS (GST)      12,456.00
#               Jrnl Reference:GT/77
# The account on the other side is the particulars; the number and the
# reference sit beneath it.
POSTING_REF = {'SALE': 'Sale Bill No.: %s', 'PURCHASE': 'Prch Bill No.: %s',
               'CREDIT': 'Cr Note No.: %s', 'DEBIT': 'Dr Note No.: %s',
               'RECEIPT': 'Rcpt Chq/Ref No: %s', 'PAYMENT': 'Pymt %s',
               'JOURNAL': 'Jrnl Reference: %s', 'CONTRA': 'Ctra %s'}


def _posting_ref(kind, no, narration, challan):
    """SHREE's own second line for an entry, as near as this app can say it."""
    if kind == 'SALE':
        shape = 'Challan No.: %s' if challan else POSTING_REF['SALE']
    else:
        shape = POSTING_REF.get(kind, (kind.title() + ' %s') if kind else '%s')
    body = no if kind in ('SALE', 'PURCHASE', 'CREDIT', 'DEBIT') else narration
    # SHREE leaves the bare word behind -- "Jrnl", "Pymt" -- but the Type
    # column already says that, so an entry with no reference gets no line.
    return (shape % body) if body else ''

# FoxPro narrations came across with stray control bytes in them.
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def txt_or_blank(value):
    return _CONTROL.sub('', str(value or '')).strip()

# Stock effect: +1 receives goods, -1 issues goods.
DOC_STOCK = {'PURCHASE': 1, 'CREDIT': 1, 'SALE': -1, 'DEBIT': -1}

# How much one line moves stock: a weighed line by its weight, anything else
# by its quantity. A return's weight is negative -- SHREE files a sales return
# as a bill with its lines turned round -- and counts as it stands; asked as
# `weight>0` the return fell through to its quantity, nought on every weighed
# line, and the goods never came back into stock. The SHREE import anchors
# opening stock on this same figure.
STOCK_QTY = 'CASE WHEN ii.weight<>0 THEN ii.weight ELSE ii.qty END'

GSTIN_RE = re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z][Z][0-9A-Z]$')


def docno(inv):
    sr = (inv.get('series') or '').strip()
    return (sr + ' ' + inv['invoice_no']) if sr else inv['invoice_no']


# ------------------------------------------- what the SHREE update keeps ---
#
# The SHREE update rebuilds every imported row from the old software's files
# (`shree_sync.py`), which would quietly undo a decision taken here about one
# of those rows. So each such decision is written down as it is made, by name
# rather than by id -- the rows come back with new ids -- and the update plays
# it again. The update itself turns this off while it replays them.
RECORD_RULES = True

# The fields a person actually edits on a document. Totals, tax and postings
# are worked out again on every save, and an imported document saved with no
# change would otherwise count as edited.
DOC_EDIT_FIELDS = ('party_id', 'invoice_date', 'cutting', 'transport',
                   'loading', 'driver_name', 'payment_terms', 'vehicle_no',
                   'transporter', 'lr_no', 'lr_date', 'eway_no', 'remarks',
                   'ref_no', 'cases',
                   # Everything else the form can change on a saved document:
                   # since 2 Oct 2026 a save that changes none of these leaves
                   # an imported document exactly as SHREE wrote it. The e-way
                   # boxes are not among them -- the form fills their defaults
                   # in ("1" for the sub-supply type where SHREE has none), so
                   # a plain Ctrl+S read as an edit -- and they never change
                   # the money; they are kept by themselves (EWAY_ONLY).
                   'series', 'invoice_type', 'dept', 'order_id', 'round_off',
                   'tcs')
EWAY_ONLY = ('transporter_gstin', 'transport_mode', 'transport_distance',
             'vehicle_type', 'sub_supply_type', 'sub_supply_desc',
             'transaction_type')
LINE_EDIT_FIELDS = ('item_id', 'description', 'item_detail', 'hsn', 'unit',
                    'qty', 'weight', 'by_weight', 'rate', 'incl_price',
                    'discount_pct', 'gst_rate', 'thickness', 'cutting_size')
VOUCHER_EDIT_FIELDS = ('party_id', 'bank_id', 'voucher_date', 'amount',
                       'ref_no', 'remarks')


def _firm_code(conn, cid):
    row = conn.execute('SELECT code FROM company WHERE id=?', (cid,)).fetchone()
    return row['code'] if row else ''


def record_rule(conn, kind, cid, name, value='', note=''):
    """Write down a decision about SHREE's data for the update to replay."""
    if not RECORD_RULES:
        return
    firm = _firm_code(conn, cid)
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True)
    if kind in ('billing_firm', 'rename_ledger', 'merge_ledger'):
        # One answer per account: the latest replaces the one before.
        conn.execute('DELETE FROM sync_rule WHERE kind=? AND firm=? AND name=?',
                     (kind, firm, name))
    conn.execute('INSERT INTO sync_rule(kind, firm, name, value, note) '
                 'VALUES (?,?,?,?,?)', (kind, firm, name, value, note))


def doc_key(kind, fy, series, number):
    """How a document is known across SHREE updates: its kind, year and
    number -- the three things its paper carries."""
    return '|'.join((kind, fy or '', (series or '').strip(),
                     (number or '').strip()))


def _doc_fingerprint(conn, inv):
    party = conn.execute('SELECT name FROM ledger WHERE id=?',
                         (inv['party_id'],)).fetchone()
    return dict(party=party['name'] if party else '',
                date=inv['invoice_date'], total=r2(inv['grand_total']))


def _voucher_fingerprint(conn, v):
    party = conn.execute('SELECT name FROM ledger WHERE id=?',
                         (v['party_id'],)).fetchone() if v['party_id'] else None
    return dict(party=party['name'] if party else '',
                date=v['voucher_date'], total=r2(v['amount']))


def _edit_signature(conn, table, key, fields, line_sql=None, line_fields=()):
    """The part of a saved row a person typed, to tell an edit from a resave."""
    row = one(conn.execute('SELECT * FROM %s WHERE id=?' % table, (key,)))
    if not row:
        return None

    def norm_v(v):
        if isinstance(v, float):
            return round(v, 2)
        return '' if v is None else v
    head = tuple(norm_v(row.get(f)) for f in fields)
    lines = ()
    if line_sql:
        lines = tuple(tuple(norm_v(l.get(f)) for f in line_fields)
                      for l in rows(conn.execute(line_sql, (key,))))
    return head, lines


def _invoice_signature(conn, iid):
    # A challan carries no tax, and saving one sets every line's rate to
    # nought whatever SHREE stored there -- not an edit anybody made.
    untaxed = conn.execute(
        "SELECT c.tax_mode='none' FROM invoice i JOIN company c "
        'ON c.id=i.company_id WHERE i.id=?', (iid,)).fetchone()
    fields = tuple(f for f in LINE_EDIT_FIELDS
                   if not (untaxed and untaxed[0] and f == 'gst_rate'))
    return _edit_signature(
        conn, 'invoice', iid, DOC_EDIT_FIELDS,
        'SELECT * FROM invoice_item WHERE invoice_id=? ORDER BY id', fields)


def _voucher_signature(conn, vid):
    # A receipt or payment is saved from its party, bank and amount, and its
    # lines are rebuilt from those -- only a journal is edited line by line.
    # A journal's party, bank and amount are read off its lines when saved.
    kind = conn.execute('SELECT kind FROM voucher WHERE id=?', (vid,)).fetchone()
    by_lines = kind and kind[0] in ('JOURNAL', 'CONTRA')
    return _edit_signature(
        conn, 'voucher', vid,
        ('voucher_date', 'ref_no', 'remarks') if by_lines
        else VOUCHER_EDIT_FIELDS,
        'SELECT * FROM voucher_line WHERE voucher_id=? ORDER BY id'
        if by_lines else None, ('ledger_id', 'debit', 'credit'))


def _adopt_edited(conn, table, row, kind, key, fingerprint):
    """An imported row edited here becomes this app's own.

    The update leaves the app's own rows alone, so the edit survives; SHREE's
    unedited copy comes in again beside it and the `supersede_doc` rule takes
    that copy away once more.
    """
    if row.get('source') != 'SHREE':
        return
    conn.execute("UPDATE %s SET source='' WHERE id=?" % table, (row['id'],))
    record_rule(conn, 'supersede_doc', row['company_id'], key,
                dict(fingerprint, table=table),
                'Edited in this app -- its own copy is kept')


class ApiError(Exception):
    def __init__(self, message, status=400, extra=None):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        # Merged into the JSON error body. A blocked delete uses it to say
        # the block can be lifted by taking the dependent document too.
        self.extra = extra or {}


def r2(x):
    return round(float(x or 0) + 1e-9, 2)


def r3(x):
    return round(float(x or 0) + 1e-9, 3)


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


# Results worked out from the books, kept until anything is committed to them
# (db.generation). Never used inside a write that has not been committed yet --
# that write is not in the generation, and would not be in the answer.
_MEMO = {}
_MEMO_LOCK = threading.Lock()
MEMO_SIZE = 64


def memo(conn, key, compute):
    if conn.in_transaction:
        return compute()
    gen = db.generation()
    with _MEMO_LOCK:
        hit = _MEMO.get(key)
    if hit is not None and hit[0] == gen:
        return hit[1]
    value = compute()
    with _MEMO_LOCK:
        if len(_MEMO) >= MEMO_SIZE:
            for k in list(_MEMO)[:MEMO_SIZE // 4]:
                del _MEMO[k]
        _MEMO[key] = (gen, value)
    return value


def one(cur):
    r = cur.fetchone()
    return dict(r) if r else None


def _chunks(seq, size):
    """Slice a list of ids so an IN (...) never outruns SQLite's parameter cap."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


BOOL_DEFAULT_ON = ('active', 'track_stock', 'by_weight')
NULLABLE = {'code'}      # stored as NULL when blank so UNIQUE stays usable


def _apply_defaults(data):
    """A field the client never sent must keep its default, not become 0."""
    for f in BOOL_DEFAULT_ON:
        if f not in data or data[f] in ('', None):
            data[f] = 1
    return data


def _coerce(data, fields, numeric):
    out = []
    for f in fields:
        v = data.get(f)
        if f in numeric:
            try:
                out.append(float(v or 0))
            except (TypeError, ValueError):
                out.append(0.0)
            continue
        text = '' if v is None else str(v).strip()
        out.append(None if (f in NULLABLE and not text) else text)
    return out


# ------------------------------------------------------------------ search

# Party names are not punctuated consistently -- "J.K.ENG.ENTERPRISE" and
# "SHREE NATHJI IND." sit in the same book as "PVT LTD" and "JK ENG" -- so a
# plain substring search misses the name somebody is actually looking at.
# Squashing both sides down to letters and digits makes the two forms equal.
_SQUASH_CHARS = ".,-/&()'\" "


def squash(text):
    t = (text or '').upper()
    for ch in _SQUASH_CHARS:
        t = t.replace(ch, '')
    return t


def _squash_sql(col):
    expr = 'upper(%s)' % col
    for ch in _SQUASH_CHARS:
        expr = "replace(%s,'%s','')" % (expr, "''" if ch == "'" else ch)
    return expr


def like_arg(text):
    r"""A LIKE pattern matching `text` literally.

    Without this a typed % matches every row and _ matches any character, so
    the two keys most likely to be hit by accident quietly return the whole
    ledger instead of nothing.
    """
    return '%' + (text or '').replace('\\', '\\\\').replace('%', '\\%') \
        .replace('_', '\\_') + '%'


def contains(cols, term):
    r"""(sql, args) for a plain "any of these columns holds it" filter.

    The pattern is escaped, so a typed % or _ is looked for as itself rather
    than matching every row -- searching a document list for "50%" used to
    return the whole register.
    """
    pattern = like_arg(term)
    return ('(' + ' OR '.join("%s LIKE ? ESCAPE '\\'" % c for c in cols) + ')',
            [pattern] * len(cols))


def qint(value, what='number', default=None):
    """An integer out of a query string or a request body.

    int() on whatever the client happened to send raises ValueError, which
    comes back as a 500 with a traceback in the log. Every id, limit and
    company that reaches a query goes through here instead, so a mistyped one
    is answered as the bad request it is.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is not None:
            return default
        raise ApiError('A %s is required.' % what)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ApiError('"%s" is not a valid %s.' % (value, what))


def qlimit(value, default, cap=5000):
    """A row count that is always positive and never unbounded."""
    return max(1, min(cap, qint(value, 'limit', default)))


NAME_MAX = 120


def check_name(data, what):
    """The name a save was sent: text, not blank, and a name's length.

    The longest in the books is 47 letters. A 10,000-letter one used to be
    taken without a word and drew every list it was in 100,000px wide; a
    number sent as the name came back as "Something went wrong" (29 Sep 2026).
    """
    name = data.get('name')
    if name is not None and not isinstance(name, str):
        if isinstance(name, (dict, list, bool)):
            raise ApiError('%s name should be text.' % what)
        name = str(name)
    name = (name or '').strip()
    if not name:
        raise ApiError('%s name is required.' % what)
    if len(name) > NAME_MAX:
        raise ApiError('That name is %d letters long; a name can be at most %d.'
                       % (len(name), NAME_MAX))
    return name


def name_search(term, cols, name_col):
    """(sql, args) matching a typed search term, or ('', []) for a blank one.

    Written for how the names are really typed: the term is trimmed and its
    spacing collapsed, a difference in punctuation or spacing does not matter,
    and the words may be given in any order.
    """
    term = ' '.join((term or '').split())
    if not term:
        return '', []
    sql = ["%s LIKE ? ESCAPE '\\'" % _squash_sql(name_col)]
    args = [like_arg(squash(term))]
    words = []
    for word in term.split(' '):
        words.append('(' + ' OR '.join("%s LIKE ? ESCAPE '\\'" % c
                                       for c in cols) + ')')
        args.extend([like_arg(word)] * len(cols))
    # Either the whole term read straight through the punctuation, or every
    # word landed somewhere -- which is what lets the words be reordered.
    return '(' + sql[0] + ' OR (' + ' AND '.join(words) + '))', args


def name_rank(term, name_col):
    r"""(sql, args) for an ORDER BY putting the closest reading first.

    The word-by-word half of the match is deliberately loose -- it is what
    lets the words be typed in any order -- but on a short term it can bury
    the name somebody actually meant under everything else carrying the same
    letters: "J K ENG" turned up six engineering works before
    J.K.ENG.ENTERPRISE. Ranking pulls it back to the top, which matters most
    where the list is then cut to a handful of rows.
    """
    term = ' '.join((term or '').split())
    if not term:
        return '', []
    col = _squash_sql(name_col)
    whole = like_arg(squash(term))
    return ("CASE WHEN %s LIKE ? ESCAPE '\\' THEN 0 "
            "WHEN %s LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END" % (col, col),
            [whole[1:], whole])          # starts with it, then merely holds it


def gstin_ok(value):
    return bool(GSTIN_RE.match((value or '').strip().upper()))


def clean_gstin(value):
    g = (value or '').strip().upper()
    if g and not GSTIN_RE.match(g):
        raise ApiError('GSTIN "%s" is not a valid 15-character GST number.' % g)
    return g


# --------------------------------------------------------------- companies

COMPANY_NUM = {'default_gst', 'active'}
COMPANY_FIELDS = ['code', 'name', 'address1', 'address2', 'city', 'state',
                  'state_code', 'pin', 'gstin', 'pan', 'phone', 'mobile', 'email',
                  'bank_name', 'bank_ac', 'bank_ifsc', 'bank_branch',
                  'terms', 'books_from',
                  'logo', 'jurisdiction', 'short_name', 'tax_mode',
                  'default_gst', 'active']


def list_companies(conn):
    return rows(conn.execute(
        'SELECT c.*, '
        ' (SELECT COUNT(*) FROM invoice i WHERE i.company_id=c.id) AS doc_count '
        'FROM company c ORDER BY c.active DESC, c.name'))


def get_company(conn, cid):
    c = one(conn.execute('SELECT * FROM company WHERE id=?', (cid,)))
    if not c:
        raise ApiError('Company not found.', 404)
    return c


def _meta_int(value):
    """An id remembered in the meta table, or None if it is no longer one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def active_company_id(conn, requested=None):
    """Resolve the company to work in: explicit id, else the remembered one."""
    if requested:
        cid = qint(requested, 'company')
        if conn.execute('SELECT 1 FROM company WHERE id=?', (cid,)).fetchone():
            return cid
        raise ApiError('Company not found.', 404)
    stored = _meta_int(db.get_meta(conn, 'active_company'))
    if stored and conn.execute('SELECT 1 FROM company WHERE id=?',
                               (stored,)).fetchone():
        return stored
    first = conn.execute('SELECT id FROM company ORDER BY id').fetchone()
    if not first:
        raise ApiError('No company exists yet.', 404)
    db.set_meta(conn, 'active_company', str(first['id']))
    conn.commit()
    return first['id']


def switch_company(conn, cid):
    get_company(conn, cid)
    db.set_meta(conn, 'active_company', str(cid))
    conn.commit()
    return {'active_company': cid}


def _normalise_place(data):
    data = dict(data)
    data['gstin'] = clean_gstin(data.get('gstin'))
    if data['gstin'] and not (data.get('state_code') or '').strip():
        data['state_code'] = data['gstin'][:2]
    if data.get('state_code') and not (data.get('state') or '').strip():
        data['state'] = STATE_NAME.get(data['state_code'], '')
    return data


# A logo is kept in the books as a data: address and drawn by the browser, so
# it has to be a picture and nothing else -- no SVG, which can carry script --
# and of a size a letterhead needs.
LOGO_RE = re.compile(r'^data:image/(png|jpeg|jpg|gif|webp);base64,'
                     r'[A-Za-z0-9+/=\s]+$')
LOGO_MAX = 2 * 1024 * 1024
COMPANY_TEXT_MAX = 400
COMPANY_LONG = {'terms': 4000}


def _check_company(data):
    logo = (data.get('logo') or '').strip()
    if logo and (len(logo) > LOGO_MAX or not LOGO_RE.match(logo)):
        raise ApiError('The logo has to be a PNG, JPEG, GIF or WebP picture '
                       'under 1.5 MB.')
    for f in COMPANY_FIELDS:
        v = data.get(f)
        if f != 'logo' and isinstance(v, str) and \
                len(v) > COMPANY_LONG.get(f, COMPANY_TEXT_MAX):
            raise ApiError('%s is too long.' % f.replace('_', ' ').capitalize())


def save_company(conn, cid, data):
    data = dict(data, name=check_name(data, 'Company'))
    _check_company(data)
    data = _normalise_place(data)
    if (data.get('tax_mode') or 'gst') not in ('gst', 'none'):
        data['tax_mode'] = 'gst'
    if not str(data.get('default_gst') or '').strip():
        data['default_gst'] = 18
    if not (data.get('code') or '').strip():
        data['code'] = re.sub(r'[^A-Z0-9]', '',
                              data['name'].upper())[:8] or 'CO'
    try:
        if cid:
            vals = _coerce(_apply_defaults(data), COMPANY_FIELDS, COMPANY_NUM)
            sets = ', '.join('%s=?' % f for f in COMPANY_FIELDS)
            conn.execute('UPDATE company SET %s WHERE id=?' % sets,
                         vals + [cid])
        else:
            if not (data.get('terms') or '').strip():
                data['terms'] = db.DEFAULT_TERMS
            cid = db.create_company(conn, data)
    except sqlite3.IntegrityError:
        raise ApiError('Company code "%s" is already used.' % data.get('code'))
    conn.commit()
    return get_company(conn, cid)


def delete_company(conn, cid):
    if conn.execute('SELECT COUNT(*) c FROM company').fetchone()['c'] <= 1:
        raise ApiError('At least one company must remain.')
    used = conn.execute('SELECT COUNT(*) n FROM invoice WHERE company_id=?',
                        (cid,)).fetchone()['n']
    if used:
        raise ApiError('This company has %d document(s). Mark it inactive '
                       'instead of deleting.' % used)
    conn.execute('DELETE FROM counter WHERE company_id=?', (cid,))
    conn.execute('DELETE FROM ledger WHERE company_id=?', (cid,))
    conn.execute('DELETE FROM account_group WHERE company_id=?', (cid,))
    conn.execute('DELETE FROM item WHERE company_id=?', (cid,))
    conn.execute('DELETE FROM company WHERE id=?', (cid,))
    if db.get_meta(conn, 'active_company') == str(cid):
        first = conn.execute('SELECT id FROM company ORDER BY id').fetchone()
        db.set_meta(conn, 'active_company', str(first['id']))
    conn.commit()
    return {'ok': True}


# ------------------------------------------------------------------ groups

def list_groups(conn, cid):
    return rows(conn.execute(
        'SELECT * FROM account_group WHERE company_id=? ORDER BY sort, code',
        (cid,)))


def save_group(conn, cid, gid, data):
    name = check_name(data, 'Group')
    nature = (data.get('nature') or '').strip()
    if nature not in ('asset', 'liability', 'income', 'expense'):
        raise ApiError('Nature must be asset, liability, income or expense.')
    code = (data.get('code') or '').strip()
    fields = ('code', 'name', 'nature', 'parent_code', 'is_party')
    vals = [code, name, nature, (data.get('parent_code') or '').strip(),
            1 if data.get('is_party') else 0]
    try:
        if gid:
            sets = ', '.join('%s=?' % f for f in fields)
            conn.execute('UPDATE account_group SET %s WHERE id=? AND '
                         'company_id=?' % sets, vals + [gid, cid])
        else:
            if not code:
                raise ApiError('Group code is required.')
            conn.execute(
                'INSERT INTO account_group(company_id, code, name, nature, '
                'parent_code, is_party, sort) VALUES (?,?,?,?,?,?,999)',
                [cid] + vals)
    except sqlite3.IntegrityError:
        raise ApiError('Group code "%s" is already used.' % code)
    conn.commit()
    return list_groups(conn, cid)


# ----------------------------------------------------------------- ledgers

LEDGER_FIELDS = ['group_id', 'code', 'name', 'party_kind', 'gst_type',
                 'address1', 'address2', 'city', 'state', 'state_code', 'pin',
                 'gstin', 'pan', 'phone', 'email', 'opening_bal',
                 'credit_limit', 'credit_days', 'notes', 'active']
LEDGER_NUM = {'group_id', 'opening_bal', 'credit_limit', 'credit_days', 'active'}


def sys_ledger(conn, cid, key):
    r = conn.execute('SELECT id FROM ledger WHERE company_id=? AND system_key=?',
                     (cid, key)).fetchone()
    if not r:
        raise ApiError('System ledger "%s" is missing for this company.' % key,
                       500)
    return r['id']


def list_ledgers(conn, cid, q):
    sql = ('SELECT l.*, g.name AS group_name, g.nature, g.is_party AS in_party_group '
           'FROM ledger l JOIN account_group g ON g.id=l.group_id '
           'WHERE l.company_id=?')
    args = [cid]
    where, wargs = name_search(q.get('search'),
                               ['l.name', 'l.code', 'l.city', 'l.gstin'],
                               'l.name')
    if where:
        sql += ' AND ' + where
        args += wargs
    if q.get('group_id'):
        sql += ' AND l.group_id=?'
        args.append(qint(q['group_id'], 'group'))
    if q.get('kind'):
        sql += " AND (l.party_kind=? OR l.party_kind='both')"
        args.append(q['kind'])
    if q.get('parties_only'):
        sql += " AND l.party_kind<>''"
    if q.get('cash_bank'):
        sql += ' AND ' + MONEY_SQL
    if q.get('active_only'):
        sql += ' AND l.active=1'
    if q.get('user_only'):
        sql += ' AND l.system_key IS NULL'
    # Searching is looking for one party, so the closest reading leads. An
    # unfiltered list has no rank and stays in name order.
    rank, rargs = name_rank(q.get('search'), 'l.name')
    sql += ' ORDER BY %sl.name' % (rank + ', ' if rank else '')
    args += rargs
    data = rows(conn.execute(sql, args))
    bal = ledger_balances(conn, cid)
    for l in data:
        l['balance'] = r2(bal.get(l['id'], l['opening_bal']))
        l['is_system'] = bool(l['system_key'])
    return data


def save_ledger(conn, cid, lid, data):
    data = dict(data, name=check_name(data, 'Ledger'))
    if not data.get('group_id'):
        raise ApiError('Choose an account group.')
    grp = one(conn.execute('SELECT * FROM account_group WHERE id=? AND '
                           'company_id=?', (qint(data['group_id'], 'group'), cid)))
    if not grp:
        raise ApiError('That account group does not belong to this company.')
    data = _normalise_place(data)
    if not (data.get('code') or '').strip():
        data['code'] = None
    if (data.get('gst_type') or '') not in GST_TYPES:
        data['gst_type'] = 'Regular'
    # A ledger in a receivable/payable group is a trading party by default.
    if grp['is_party'] and not (data.get('party_kind') or '').strip():
        data['party_kind'] = 'customer' if grp['code'] == '2101' else 'supplier'
    vals = _coerce(_apply_defaults(data), LEDGER_FIELDS, LEDGER_NUM)
    before = one(conn.execute('SELECT * FROM ledger WHERE id=? AND company_id=?',
                              (lid, cid))) if lid else None
    try:
        if lid:
            sets = ', '.join('%s=?' % f for f in LEDGER_FIELDS)
            conn.execute('UPDATE ledger SET %s WHERE id=? AND company_id=?'
                         % sets, vals + [lid, cid])
            # A group chosen here is the account's group in every year.
            if before and before['group_id'] != grp['id']:
                conn.execute('DELETE FROM ledger_group_year WHERE ledger_id=?',
                             (lid,))
            _adopt_edited_ledger(conn, before)
            # How a person is paid is kept by their account's name.
            if before and staff_key(before['name']) != staff_key(data['name']):
                conn.execute('UPDATE OR IGNORE staff SET name=? WHERE company_id=? AND '
                             'name=?', (staff_key(data['name']), cid,
                                        staff_key(before['name'])))
            # An account another was merged into is found by its name when
            # the SHREE update merges them again, so the name it is found by
            # follows it.
            if before and before['name'].strip() != data['name'].strip():
                conn.execute("UPDATE sync_rule SET value=? WHERE "
                             "kind='merge_ledger' AND firm=? AND value=?",
                             (data['name'].strip(), _firm_code(conn, cid),
                              before['name'].strip()))
        else:
            cols = ','.join(['company_id'] + LEDGER_FIELDS)
            ph = ','.join('?' * (len(LEDGER_FIELDS) + 1))
            cur = conn.execute('INSERT INTO ledger(%s) VALUES (%s)' % (cols, ph),
                               [cid] + vals)
            lid = cur.lastrowid
    except sqlite3.IntegrityError:
        raise ApiError('Ledger code "%s" is already used.' % data.get('code'))
    conn.commit()
    return one(conn.execute('SELECT * FROM ledger WHERE id=?', (lid,)))


def _adopt_edited_ledger(conn, before):
    """An imported account edited here stays as edited through SHREE updates.

    Made this app's own, the update leaves it be and fills only what it lacks.
    A new name is written down: SHREE still calls it by the old one and will
    bring that back, and the update folds it into the renamed account.
    """
    if not before or before.get('source') != 'SHREE':
        return
    after = one(conn.execute('SELECT * FROM ledger WHERE id=?', (before['id'],)))
    changed = [f for f in LEDGER_FIELDS
               if (before.get(f) if before.get(f) is not None else '')
               != (after.get(f) if after.get(f) is not None else '')]
    if not changed:
        return
    conn.execute("UPDATE ledger SET source='' WHERE id=?", (before['id'],))
    if before['name'].strip() != after['name'].strip():
        # A second rename keeps SHREE's original name as the key.
        firm = _firm_code(conn, before['company_id'])
        prior = conn.execute(
            "SELECT name FROM sync_rule WHERE kind='rename_ledger' AND firm=? "
            'AND value=?', (firm, before['name'].strip())).fetchone()
        record_rule(conn, 'rename_ledger', before['company_id'],
                    prior['name'] if prior else before['name'].strip(),
                    after['name'].strip(), 'Renamed in this app')


def list_allocation(conn, q):
    """Customers across every firm, with the firm that bills each one."""
    firms = [c for c in list_companies(conn) if c['active']]
    names = {f['id']: f for f in firms}
    # One count for the whole book. This used to be a query per customer, and
    # with three thousand of them on screen that was three thousand round
    # trips to answer one page.
    doc_counts = {r['party_id']: r['n'] for r in conn.execute(
        'SELECT party_id, COUNT(*) n FROM invoice GROUP BY party_id')}
    rows_out = []
    for f in firms:
        sql = ("SELECT l.*, g.name AS group_name FROM ledger l "
               'JOIN account_group g ON g.id=l.group_id '
               "WHERE l.company_id=? AND l.party_kind IN ('customer','both')")
        args = [f['id']]
        where, wargs = name_search(q.get('search'),
                                   ['l.name', 'l.city', 'l.gstin'], 'l.name')
        if where:
            sql += ' AND ' + where
            args += wargs
        if q.get('active_only'):
            sql += ' AND l.active=1'
        bal = ledger_balances(conn, f['id'])
        for l in rows(conn.execute(sql + ' ORDER BY l.name', args)):
            bill = names.get(l['home_company'])
            rows_out.append(dict(
                id=l['id'], name=l['name'], city=l['city'], state=l['state'],
                gstin=l['gstin'], phone=l['phone'], active=l['active'],
                company_id=f['id'], company_code=f['code'],
                company_name=f['name'],
                bill_company_id=l['home_company'],
                bill_company_name=bill['name'] if bill else '',
                bill_company_code=bill['code'] if bill else '',
                balance=r2(bal.get(l['id'], 0)),
                docs=doc_counts.get(l['id'], 0)))
    rows_out.sort(key=lambda x: (x['name'].lower(), x['company_code']))
    return dict(firms=[dict(id=f['id'], code=f['code'], name=f['name'],
                            tax_mode=f['tax_mode']) for f in firms],
                billing_firms=billing_firms(conn),
                order_company=order_company_id(
                    conn, qint(q.get('company'), 'company')
                    if q.get('company') else None),
                rows=rows_out)


def allocate_ledger(conn, lid, target_cid):
    """Put a customer under a different firm.

    With no documents behind it the party simply moves. Once it has history the
    history has to stay where it is, so the master is copied into the new firm
    and the old record is made inactive -- new bills go to the new firm, old
    ones stay readable in the old one.
    """
    src = one(conn.execute('SELECT * FROM ledger WHERE id=?', (lid,)))
    if not src:
        raise ApiError('Party not found.', 404)
    if src['system_key']:
        raise ApiError('Built-in ledgers cannot be moved between firms.')
    if src['company_id'] == target_cid:
        return dict(moved=False, ledger_id=lid, message='Already in that firm.')
    get_company(conn, target_cid)

    grp = one(conn.execute('SELECT code FROM account_group WHERE id=?',
                           (src['group_id'],)))
    tgt_grp = one(conn.execute(
        'SELECT id FROM account_group WHERE company_id=? AND code=?',
        (target_cid, grp['code'] if grp else '2101')))
    if not tgt_grp:
        tgt_grp = one(conn.execute(
            "SELECT id FROM account_group WHERE company_id=? AND code='2101'",
            (target_cid,)))
    if not tgt_grp:
        raise ApiError('The target firm has no receivables group.')

    used = conn.execute('SELECT COUNT(*) n FROM posting WHERE ledger_id=?',
                        (lid,)).fetchone()['n']
    if not used:
        # Both firms were imported from the same AMAS numbering, so the code
        # may already be taken over there. Codes are optional -- drop it.
        code = src['code']
        if code and conn.execute(
                'SELECT 1 FROM ledger WHERE company_id=? AND code=?',
                (target_cid, code)).fetchone():
            code = None
        conn.execute('UPDATE ledger SET company_id=?, group_id=?, code=?, '
                     "home_company=?, source='' WHERE id=?",
                     (target_cid, tgt_grp['id'], code, target_cid, lid))
        # SHREE still lists it in the old firm; the update takes that copy
        # away again as long as nothing has been written to it there.
        if src.get('source') == 'SHREE':
            record_rule(conn, 'moved_ledger', src['company_id'],
                        src['name'].strip(), _firm_code(conn, target_cid),
                        'Moved to another firm under Customer allocation')
        conn.commit()
        return dict(moved=True, ledger_id=lid,
                    message='Moved to the new firm.'
                            + ('' if code else ' Its old code was already in '
                               'use there, so the code was cleared.'))

    twin = same_name(conn, 'ledger', target_cid, src['name'], 'id')
    if twin:
        new_id = twin['id']
        conn.execute('UPDATE ledger SET active=1, home_company=? WHERE id=?',
                     (target_cid, new_id))
    else:
        copy = {k: src[k] for k in src.keys()
                if k not in ('id', 'company_id', 'group_id', 'code',
                             'system_key', 'opening_bal', 'home_company')}
        copy.update(company_id=target_cid, group_id=tgt_grp['id'], code=None,
                    system_key=None, opening_bal=0, home_company=target_cid,
                    active=1, source='')
        cols = ','.join(copy.keys())
        ph = ','.join('?' * len(copy))
        cur = conn.execute('INSERT INTO ledger(%s) VALUES (%s)' % (cols, ph),
                           list(copy.values()))
        new_id = cur.lastrowid
    # The old master is kept, inactive, as this app's own -- rebuilt from
    # SHREE it would come back active.
    conn.execute("UPDATE ledger SET active=0, home_company=?, source='' "
                 'WHERE id=?', (target_cid, lid))
    conn.commit()
    return dict(
        moved=True, ledger_id=new_id, copied=True,
        message=('This party has %d posting(s), so its history stays in the old '
                 'firm. A fresh master was opened in the new firm and the old '
                 'one made inactive.' % used))


ORDER_FIRM_KEY = 'order_company'      # meta key: the firm orders are taken in


def order_company_id(conn, cid=None):
    """The firm orders are taken in.

    Every family firm keeps its own no-tax book for orders and challans, and
    bills them out through its own GST firm, so there is no longer a single
    answer to this. When the caller is already sitting in a no-tax firm that
    firm is the one -- raising its challan anywhere else would put one firm's
    document in another firm's book. Only when the question is asked from a
    GST firm does the configured default apply.
    """
    if cid:
        row = conn.execute('SELECT tax_mode FROM company WHERE id=? AND '
                           'active=1', (cid,)).fetchone()
        if row and (row['tax_mode'] or 'gst') == 'none':
            return cid
    stored = _meta_int(db.get_meta(conn, ORDER_FIRM_KEY))
    if stored and conn.execute('SELECT 1 FROM company WHERE id=?',
                               (stored,)).fetchone():
        return stored
    row = conn.execute("SELECT id FROM company WHERE tax_mode='none' AND "
                       'active=1 ORDER BY id').fetchone()
    return row['id'] if row else None


def billing_firms(conn):
    """The official firms a customer can be billed from."""
    return rows(conn.execute(
        "SELECT id, code, name FROM company WHERE tax_mode='gst' AND active=1 "
        'ORDER BY name'))


_WORD_RE = re.compile(r'[A-Z0-9]+')


def _edits(a, b, most):
    """Letters apart, two swapped counting as one; gives up past `most`."""
    if abs(len(a) - len(b)) > most:
        return most + 1
    before, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i]
        for j in range(1, len(b) + 1):
            v = min(prev[j] + 1, cur[j - 1] + 1,
                    prev[j - 1] + (a[i - 1] != b[j - 1]))
            if (before is not None and i > 1 and j > 1
                    and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]):
                v = min(v, before[j - 2] + 1)
            cur.append(v)
        if min(cur) > most:
            return most + 1
        before, prev = prev, cur
    return prev[-1]


def near_name(term, name):
    """Is `name` what was typed, but for a slip of the finger?

    Each typed word has to start one of the name's words, or come within a
    letter of doing so -- one wrong, missing, extra, or two swapped; two in a
    word of six letters or more -- its first letter as typed. "rikia" is
    RIKAI INDUSTRIES. A word of one
    or two letters is too short to guess at and has to be right. The same
    reading as `nameRank` in app.js, which orders what comes back.
    """
    words = _WORD_RE.findall((term or '').upper())
    names = _WORD_RE.findall((name or '').upper())
    if not words or not names:
        return False

    def fits(w):
        if any(n.startswith(w) for n in names):
            return True
        if len(w) < 3:
            return False
        most = 1 if len(w) <= 5 else 2
        # The first letter is taken as typed right: a slip lands later in a
        # word, and without it SANTSH read as GANESH.
        for n in (n for n in names if n[0] == w[0]):
            for size in range(len(w) - 1, len(w) + 2):
                if _edits(w, n[:size], most) <= most:
                    return True
                if size >= len(n):
                    break
        return False
    return all(fits(w) for w in words)


def search_customers(conn, q):
    """Customers across every firm, for picking while taking an order.

    `fuzzy=1` (the Accounts box) adds, when nothing is spelt the way it was
    typed, the names within a slip of it -- `near_name`.
    """
    base = ("SELECT l.id, l.name, l.city, l.state, l.state_code, l.gstin, "
            '  l.phone, l.company_id, l.home_company, l.linked_ledger, '
            '  c.code AS company_code, c.name AS company_name, c.tax_mode, '
            '  h.code AS bill_code, h.name AS bill_name '
            'FROM ledger l JOIN company c ON c.id=l.company_id '
            'LEFT JOIN company h ON h.id=l.home_company '
            "WHERE l.party_kind IN ('customer','both') AND l.active=1")
    sql = base
    args = []
    term = q.get('search') or ''
    # A GST number is searched only for something that could be part of one:
    # a digit, or five letters running (the PAN inside it). Three letters of a
    # name -- "egf" -- used to turn up customers through their GSTINs alone.
    cols = ['l.name', 'l.city']
    if re.search(r'\d', term) or re.search(r'[A-Za-z]{5}', term):
        cols.append('l.gstin')
    where, wargs = name_search(term, cols, 'l.name')
    if where:
        sql += ' AND ' + where
        args += wargs
    if q.get('company'):
        sql += ' AND l.company_id=?'
        args.append(qint(q['company'], 'company'))
    # Rank before the cut, not after: the row somebody meant has to survive
    # the LIMIT to be worth ordering at all.
    limit = qlimit(q.get('limit'), 40, 500)
    rank, rargs = name_rank(q.get('search'), 'l.name')
    sql += ' ORDER BY %sl.name LIMIT %d' % (rank + ', ' if rank else '',
                                            limit * 4)
    args += rargs
    found = rows(conn.execute(sql, args))
    if not found and q.get('fuzzy') and len(squash(term)) >= 3:
        # Nothing spelt that way: every customer read against it instead. A
        # few thousand short names, and only when the plain search came back
        # empty, so the typing never waits on it otherwise.
        near_sql, near_args = base, []
        if q.get('company'):
            near_sql += ' AND l.company_id=?'
            near_args.append(qint(q['company'], 'company'))
        found = [r for r in rows(conn.execute(near_sql + ' ORDER BY l.name', near_args))
                 if near_name(term, r['name'])][:limit * 4]

    # The same customer can have a master in more than one firm. Show one row
    # per name: the local copy if there is one, else the billing firm's.
    prefer = qint(q.get('prefer_company'), 'company', 0)
    best = {}
    for r in found:
        # Spaces too: BSI's UMIYA ENTERPRISE   ATULBHAI is BST's customer.
        key = name_key(r['name'])
        cur = best.get(key)
        if cur is None:
            best[key] = r
            continue
        def score(x):
            if prefer and x['company_id'] == prefer:
                return 2
            if x['home_company'] and x['company_id'] == x['home_company']:
                return 1
            return 0
        if score(r) > score(cur):
            best[key] = r
    # Keep the order the query came back in. Re-sorting by name here undid the
    # ranking and then cut the list, so on a short term the name somebody had
    # typed could be dropped for spelling further down the alphabet. With no
    # search term that order is alphabetical anyway.
    return list(best.values())[:limit]


def _copy_master(conn, src, target_cid, group_code, extra=None):
    """Reproduce a party master inside another firm."""
    grp = one(conn.execute(
        'SELECT id FROM account_group WHERE company_id=? AND code=?',
        (target_cid, group_code)))
    if not grp:
        raise ApiError('The target firm has no %s group.' % group_code)
    copy = {k: src[k] for k in src.keys()
            if k not in ('id', 'company_id', 'group_id', 'code', 'system_key',
                         'opening_bal', 'home_company', 'linked_ledger')}
    # Made here, so a SHREE update does not take it for one of its own.
    copy.update(company_id=target_cid, group_id=grp['id'], code=None,
                system_key=None, opening_bal=0, active=1, source='')
    copy.update(extra or {})
    cols = ','.join(copy.keys())
    ph = ','.join('?' * len(copy))
    cur = conn.execute('INSERT INTO ledger(%s) VALUES (%s)' % (cols, ph),
                       list(copy.values()))
    return cur.lastrowid


def adopt_customer(conn, cid, source_lid):
    """Make sure a customer exists in company `cid`, copying it if needed.

    Used when an order is taken in the order firm for a customer whose master
    lives in one of the billing firms.
    """
    src = one(conn.execute('SELECT * FROM ledger WHERE id=?', (source_lid,)))
    if not src:
        raise ApiError('Customer not found.', 404)
    if src['company_id'] == cid:
        return one(conn.execute('SELECT * FROM ledger WHERE id=?', (source_lid,)))

    # The account here that already points at this one is its twin, however
    # the two names are spelt; failing that, the same name.
    twin = one(conn.execute(
        'SELECT * FROM ledger WHERE company_id=? AND linked_ledger=? '
        'ORDER BY active DESC, id LIMIT 1', (cid, src['id']))) \
        or same_name(conn, 'ledger', cid, src['name'])
    if twin:
        # A firm already chosen to bill this customer stays chosen: picking
        # the customer's copy in the other GST firm used to move them there
        # without a word. Customer allocation is where that is changed.
        conn.execute('UPDATE ledger SET active=1, home_company=COALESCE('
                     'home_company, ?), linked_ledger=COALESCE(linked_ledger, ?) '
                     'WHERE id=?',
                     (src['home_company'] or src['company_id'], src['id'],
                      twin['id']))
        conn.commit()
        return one(conn.execute('SELECT * FROM ledger WHERE id=?', (twin['id'],)))

    # Same rule as counterpart(): only a firm that raises tax bills may be
    # recorded as the one that bills this customer.
    src_firm = get_company(conn, src['company_id'])
    fallback = src['company_id'] if src_firm.get('tax_mode') != 'none' else None
    new_id = _copy_master(conn, src, cid, '2101', dict(
        party_kind='customer',
        home_company=src['home_company'] or fallback,
        linked_ledger=src['id']))
    conn.commit()
    return one(conn.execute('SELECT * FROM ledger WHERE id=?', (new_id,)))


def set_billing_firm(conn, lid, company_id):
    """Choose which official firm bills this customer."""
    led = one(conn.execute('SELECT * FROM ledger WHERE id=?', (lid,)))
    if not led:
        raise ApiError('Customer not found.', 404)
    firm = get_company(conn, company_id)
    if firm.get('tax_mode') == 'none':
        raise ApiError('"%s" does not raise tax bills, so it cannot be a '
                       'billing firm.' % firm['name'])
    link = None
    if led['company_id'] != company_id:
        twin = same_name(conn, 'ledger', company_id, led['name'], 'id')
        link = twin['id'] if twin else None
    conn.execute('UPDATE ledger SET home_company=?, linked_ledger=COALESCE(?, '
                 'linked_ledger) WHERE id=?', (company_id, link, lid))
    record_rule(conn, 'billing_firm', led['company_id'], led['name'].strip(),
                firm['code'], 'Chosen under Customer allocation')
    conn.commit()
    return dict(ok=True, ledger_id=lid, billing_company=company_id,
                message='%s will be billed from %s.' % (led['name'], firm['name']))


def name_key(name):
    """A name as it is meant: case and runs of spaces make no difference."""
    return re.sub(r'\s+', ' ', str(name or '')).strip().lower()


def same_name(conn, table, cid, name, cols='*'):
    """The row in firm `cid` that carries this name, read as it is meant.

    SHREE keeps each firm's masters apart, and 24 customers are typed with
    different spacing in two of them -- UMIYA ENTERPRISE   ATULBHAI in BSI is
    UMIYA ENTERPRISE ATULBHAI in BST. Matched only letter for letter, taking
    an order or billing a challan for one of them opened a second account
    for the same customer (29 Sep 2026). The exact name still wins.
    """
    exact = one(conn.execute(
        'SELECT %s FROM %s WHERE company_id=? AND lower(name)=lower(?)'
        % (cols, table), (cid, name)))
    if exact:
        return exact
    key = name_key(name)
    if not key:
        return None
    for r in conn.execute(
            "SELECT %s, name AS _name FROM %s WHERE company_id=? AND "
            "lower(replace(name, ' ', ''))=? ORDER BY id" % (cols, table),
            (cid, key.replace(' ', ''))):
        if name_key(r['_name']) == key:
            found = dict(r)
            found.pop('_name', None)
            return found
    return None


def counterpart(conn, led, target_cid):
    """The same customer's record inside the billing firm, made if missing."""
    if led['company_id'] == target_cid:
        return led
    if led['linked_ledger']:
        twin = one(conn.execute(
            'SELECT * FROM ledger WHERE id=? AND company_id=?',
            (led['linked_ledger'], target_cid)))
        if twin:
            return twin
    twin = same_name(conn, 'ledger', target_cid, led['name'])
    if twin:
        conn.execute('UPDATE ledger SET linked_ledger=? WHERE id=?',
                     (twin['id'], led['id']))
        return twin
    # A copy pulled into the order firm is not billed *by* it -- that firm
    # raises no tax bills at all. Marking it so is what left customers
    # pointed at a firm that could never bill them, and the bill draft then
    # failed with nothing to fall back on.
    firm = get_company(conn, target_cid)
    home = target_cid if firm.get('tax_mode') != 'none' else led['home_company']
    new_id = _copy_master(conn, led, target_cid, '2101',
                          dict(party_kind='customer', home_company=home))
    conn.execute('UPDATE ledger SET linked_ledger=? WHERE id=?',
                 (new_id, led['id']))
    return one(conn.execute('SELECT * FROM ledger WHERE id=?', (new_id,)))


def party_in_firm(conn, lid, firm):
    """A customer's own record in another firm's books, made if missing --
    for a challan or a bill raised from the customer's account (4 Oct 2026),
    as billing a challan already does."""
    led = one(conn.execute('SELECT * FROM ledger WHERE id=?',
                           (qint(lid, 'account'),)))
    if not led:
        raise ApiError('That account is no longer in the books.', 404)
    if not led.get('party_kind'):
        raise ApiError('Only a customer or a supplier is kept in more than '
                       'one firm.')
    target = qint(firm, 'firm')
    if not conn.execute('SELECT 1 FROM company WHERE id=?', (target,)).fetchone():
        raise ApiError('That firm is not in the books.', 404)
    twin = counterpart(conn, led, target)
    conn.commit()
    return dict(id=twin['id'], name=twin['name'], company_id=target)


def counterpart_item(conn, item_id, target_cid):
    """The same item inside another firm, made if missing."""
    if not item_id:
        return None
    src = one(conn.execute('SELECT * FROM item WHERE id=?', (item_id,)))
    if not src:
        return None
    if src['company_id'] == target_cid:
        return src
    twin = same_name(conn, 'item', target_cid, src['name'])
    if twin:
        return twin
    copy = {k: src[k] for k in src.keys()
            if k not in ('id', 'company_id', 'code', 'opening_qty')}
    # Made here, so a SHREE update does not take it for one of its own -- as
    # the copy of an imported item it was stamped SHREE, and the next update
    # deleted it the moment no bill used it.
    copy.update(company_id=target_cid, code=None, opening_qty=0, source='')
    cols = ','.join(copy.keys())
    ph = ','.join('?' * len(copy))
    cur = conn.execute('INSERT INTO item(%s) VALUES (%s)' % (cols, ph),
                       list(copy.values()))
    return one(conn.execute('SELECT * FROM item WHERE id=?', (cur.lastrowid,)))


# ------------------------------------------------- deleting an account ---
#
# Asked for on 16 Sep 2026: *add an option to delete customer accounts*.
# Nine customers in ten have history, and the user chose that it goes with
# the account -- every challan, bill, order, receipt and journal, in all three
# firms -- shown first, with a copy of the books taken before anything moves.
#
# Three rules keep that from reaching past the one account:
#
# * **Another party's balance never moves.** A receipt that also names a
#   second customer, a transfer journal between two of them, a bill raised
#   for someone else off this customer's challan, a bill with a line posted
#   to another customer -- deleting any of those would change an account
#   nobody asked about. They block the delete and are listed, so they can be
#   edited or deleted on their own first. A round off is the one exception.
# * **The books stay as balanced as they were.** Whole documents go, so each
#   firm's trial balance is unchanged. An opening balance has no document to
#   go with; it is moved onto Difference in Opening Balances, where every
#   year SHREE never backed up already sits.
# * **An account that is not a party** -- a bank, an expense head -- only
#   ever goes while nothing is written against it. Deleting a bank with its
#   entries would take every customer's receipts along.

# SHREE files the firms' UPI collection accounts -- BST UPI, BSI UPI -- under
# Accounts Receivable. A journal moving a customer's payment onto one is a
# receipt, not a second customer, so it is not a reason to refuse.
COLLECTION_ACCOUNT = re.compile(r'\bUPI\b', re.I)
KIND_LABEL = {'PURCHASE': 'Purchase', 'CREDIT': 'Credit note',
              'DEBIT': 'Debit note', 'RECEIPT': 'Receipt', 'PAYMENT': 'Payment',
              'JOURNAL': 'Journal', 'CONTRA': 'Contra', 'IMPORT': 'Entry',
              'OPENING': 'Opening balance'}
# A line smaller than this on another party's account is a round off.
ROUND_OFF_LINE = 1.0
# The line a deleted account's part of a kept entry is carried on, and how
# the cash and bank books read the name back off it.
DELETED_PART = "%s's part, account deleted"
DELETED_PART_RE = re.compile(r"^(.*)'s part, account deleted$")
# Kept this many copies of the books taken before a delete; older ones go.
DELETE_BACKUPS_KEPT = 10


def _in(ids):
    return ','.join('?' * len(ids))


def _each_chunk(conn, sql, ids, size=500):
    """Run `sql` (with one `IN (%s)`) over ids, a slice at a time."""
    out = []
    ids = list(ids)
    for part in _chunks(ids, size):
        out += rows(conn.execute(sql % _in(part), part))
    return out


def _is_party(led):
    return bool(led['party_kind'] or led['is_party'])


def _account_family(conn, led):
    """The ledgers that are this account -- one per book it is written in.

    The same reading the Accounts screen draws the account from, so what is
    deleted is exactly what was on screen. Built-ins and anything that is not
    a party are never swept in by a matching name.
    """
    ids = {led['id']}
    if _is_party(led):
        keys, _ = customer_keys(conn)
        key = keys.get((led['company_id'], led['id']))
        if key:
            ids |= {lid for _, lid in _ledgers_of(conn, key, keys)}
    return _each_chunk(
        conn,
        'SELECT l.id, l.company_id, l.name, l.code, l.system_key, l.party_kind, '
        '       l.opening_bal, g.is_party, c.name AS company, c.code AS firm_code '
        'FROM ledger l JOIN account_group g ON g.id=l.group_id '
        'JOIN company c ON c.id=l.company_id WHERE l.id IN (%s) '
        'ORDER BY c.name, l.id', sorted(ids))


def _is_other_party(row, family):
    """A party outside this account -- a UPI collection account is not one."""
    return (row['ledger_id'] not in family
            and bool(row['party_kind'] or row['is_party'])
            and not COLLECTION_ACCOUNT.search(row['name']))


def _is_money_line(row, family):
    """A line on the firm's own cash, bank or UPI collection account.

    Money that came in or went out did so whatever becomes of the customer
    it came from. Deleting PAVAN STEEL, YASH ENGINEERS and PARESHBHAI STAR
    ENG on 16-17 Sep 2026 took fifteen 2015-16 cash receipts out whole, and
    BSTP's cash book has stood Rs 9,23,600 below SHREE's on every day since
    (3 Oct 2026: "the cash book isn't correct ... the numbers aren't
    matching").
    """
    return (row['ledger_id'] not in family
            and (row['gcode'] in MONEY_GROUPS or row['gparent'] in MONEY_GROUPS
                 or bool(COLLECTION_ACCOUNT.search(row['name']))))


def _take_out_plan(voucher, lines, family):
    """How this account's part comes out of an entry it shares.

    Its lines go. The entry is then short on the other side by what they
    came to, and that is taken off the other non-party lines there -- the
    line of the same amount first, then the largest -- the discount on a
    receipt, say. **A cash or bank line is never taken down**: the money
    moved, and the cash and bank books stay on SHREE's figures. What cannot
    be taken off stood against the money or against another party (a
    transfer between two customers), and is carried on Difference in Opening
    Balances, so neither moves.
    """
    mine = [l for l in lines if l['ledger_id'] in family]
    net = r2(sum(l['debit'] - l['credit'] for l in mine))
    field = 'debit' if net < 0 else 'credit'
    need = abs(net)
    sizes = [r2(abs(l['debit'] - l['credit'])) for l in mine]
    spare = [l for l in lines if l['ledger_id'] not in family
             and not _is_other_party(l, family)
             and not _is_money_line(l, family) and l[field] > 0.004]
    spare.sort(key=lambda l: (r2(l[field]) not in sizes,
                              l['ledger_id'] != voucher['bank_id'], -l[field]))
    shrink = {}
    for l in spare:
        if need <= 0.004:
            break
        take = r2(min(need, l[field]))
        shrink[l['ledger_id']] = r2(shrink.get(l['ledger_id'], 0) + take)
        need = r2(need - take)
    others = []
    for l in lines:
        if _is_other_party(l, family) and l['name'] not in others:
            others.append(l['name'])
    return dict(id=voucher['id'], company_id=voucher['company_id'],
                kind=voucher['kind'], no=voucher['voucher_no'],
                date=voucher['voucher_date'], bank_id=voucher['bank_id'],
                party_id=voucher['party_id'], field=field, sizes=sizes,
                shrink=shrink, carry=need,
                money=r2(sum(l['debit'] - l['credit'] for l in lines
                             if _is_money_line(l, family))),
                party_after=next((l['ledger_id'] for l in lines
                                  if _is_other_party(l, family)), None),
                others=others)


def _shrink_rows(conn, table, where, args, field, by, sizes):
    """Take `by` off `field` across the rows `where` picks. Emptied rows go."""
    found = rows(conn.execute('SELECT id, debit, credit FROM %s WHERE %s AND %s>0'
                              % (table, where, field), args))
    found.sort(key=lambda r: (r2(r[field]) not in sizes, -r[field]))
    for r in found:
        if by <= 0.004:
            break
        take = min(by, r[field])
        left = r2(r[field] - take)
        other = r['credit' if field == 'debit' else 'debit']
        if left <= 0.004 and other <= 0.004:
            conn.execute('DELETE FROM %s WHERE id=?' % table, (r['id'],))
        else:
            conn.execute('UPDATE %s SET %s=? WHERE id=?' % (table, field),
                         (left, r['id']))
        by = r2(by - take)
    return by


def _take_out(conn, plan, family, diff_ledger, name):
    """Apply a `_take_out_plan` to the entry's lines and its postings."""
    vid, kind, fam = plan['id'], plan['kind'], sorted(family)
    ph = _in(fam)
    stamp = one(conn.execute(
        'SELECT date, doc_no, narration FROM posting WHERE doc_kind=? AND '
        'doc_id=? LIMIT 1', (kind, vid)))
    conn.execute('DELETE FROM voucher_line WHERE voucher_id=? AND ledger_id IN (%s)'
                 % ph, [vid] + fam)
    conn.execute('DELETE FROM posting WHERE doc_kind=? AND doc_id=? AND '
                 'ledger_id IN (%s)' % ph, [kind, vid] + fam)
    for lid, by in plan['shrink'].items():
        for table, where, args in (
                ('voucher_line', 'voucher_id=? AND ledger_id=?', (vid, lid)),
                ('posting', 'doc_kind=? AND doc_id=? AND ledger_id=?',
                 (kind, vid, lid))):
            if _shrink_rows(conn, table, where, args, plan['field'], by,
                            plan['sizes']) > 0.004:
                raise ApiError('%s %s could not be reduced as planned, so '
                               'nothing was deleted.'
                               % (KIND_LABEL.get(kind, kind), plan['no']), 500)
    if plan['carry'] > 0.004:
        dr = plan['carry'] if plan['field'] == 'credit' else 0.0
        cr = plan['carry'] if plan['field'] == 'debit' else 0.0
        note = DELETED_PART % name
        conn.execute('INSERT INTO voucher_line(voucher_id, ledger_id, debit, '
                     'credit, remarks) VALUES (?,?,?,?,?)',
                     (vid, diff_ledger, dr, cr, note))
        conn.execute('INSERT INTO posting(company_id, date, ledger_id, doc_kind, '
                     'doc_id, doc_no, debit, credit, narration) '
                     'VALUES (?,?,?,?,?,?,?,?,?)',
                     (plan['company_id'], stamp['date'] if stamp else plan['date'],
                      diff_ledger, kind, vid,
                      stamp['doc_no'] if stamp else plan['no'], dr, cr,
                      stamp['narration'] if stamp else note))
    total = one(conn.execute(
        'SELECT COALESCE(SUM(debit), 0) d, COALESCE(SUM(credit), 0) c '
        'FROM voucher_line WHERE voucher_id=?', (vid,)))
    conn.execute('UPDATE voucher SET amount=?, party_id=CASE WHEN party_id IN '
                 '(%s) THEN ? ELSE party_id END, bank_id=CASE WHEN bank_id IN '
                 '(%s) THEN NULL ELSE bank_id END WHERE id=?' % (ph, ph),
                 [r2(max(total['d'], total['c']))] + fam
                 + [plan['party_after']] + fam + [vid])


def _account_footprint(conn, led):
    """Everything that goes when this account goes. Reads; never writes."""
    fam_rows = _account_family(conn, led)
    family = {r['id'] for r in fam_rows}
    fam = sorted(family)
    firms = {c['id']: c for c in list_companies(conn)}
    blockers = []

    def blocker(company_id, kind, no, date, doc_id, why):
        firm = firms.get(company_id, {})
        label = ('Challan' if firm.get('tax_mode') == 'none' else 'Bill') \
            if kind == 'SALE' else KIND_LABEL.get(kind, 'Entry')
        blockers.append(dict(company_id=company_id, firm=firm.get('name', ''),
                             kind=kind, label=label, no=no, date=date,
                             doc_id=doc_id, why=why))

    for r in fam_rows:
        if r['system_key']:
            raise ApiError('"%s" is a built-in account and cannot be deleted.'
                           % r['name'])

    # Every document written to the account, and every document its postings
    # point at -- the two are the same today, and reading both keeps it so.
    invoices = {r['id'] for r in _each_chunk(
        conn, 'SELECT id FROM invoice WHERE party_id IN (%s)', fam)}
    posted = _each_chunk(
        conn, 'SELECT doc_kind, doc_id, company_id, doc_no, date, narration, '
              '       id, ledger_id, debit, credit '
              'FROM posting WHERE ledger_id IN (%s)', fam)
    invoices |= {p['doc_id'] for p in posted if p['doc_kind'] in DOC_TYPES}

    # A bill raised from one of these challans goes with it, however far the
    # chain runs -- the same walk a single challan's delete makes.
    frontier = set(invoices)
    while frontier:
        made = {r['id'] for r in _each_chunk(
            conn, 'SELECT id FROM invoice WHERE from_challan IN (%s)',
            sorted(frontier))} - invoices
        invoices |= made
        frontier = made
    docs = _each_chunk(
        conn, 'SELECT i.id, i.company_id, i.doc_type, i.series, i.invoice_no, '
              '       i.invoice_date, i.party_id, i.order_id, i.gst_voucher, '
              '       i.eway_no, i.grand_total, p.name AS party '
              'FROM invoice i JOIN ledger p ON p.id=i.party_id '
              'WHERE i.id IN (%s)', sorted(invoices))

    # A bill raised off one of these challans is the same customer, often
    # under another spelling in the GST firm -- "SHREE VALLABH IND" billed as
    # "SHREE VALLABH INDUSTRIES", which is why 193 customers were refused
    # before. The bill goes with its challan. When that other name is left
    # holding nothing at all, it goes too; when it still holds other
    # documents (AVADH ENTERPRISE is billed off two accounts' challans) it
    # stays, and is shown with what it loses.
    renamed, added = [], []
    for vid_ in sorted({d['party_id'] for d in docs} - family):
        mine = [d for d in docs if d['party_id'] == vid_]
        v = one(conn.execute(
            'SELECT l.id, l.company_id, l.name, l.code, l.system_key, '
            '       l.party_kind, l.opening_bal, g.is_party, '
            '       c.name AS company, c.code AS firm_code '
            'FROM ledger l JOIN account_group g ON g.id=l.group_id '
            'JOIN company c ON c.id=l.company_id WHERE l.id=?', (vid_,)))
        own_docs = {r['id'] for r in conn.execute(
            'SELECT id FROM invoice WHERE party_id=?', (vid_,))}
        # Its other entries: bills not going (counted once each), and any
        # posting that is not one of its bills at all.
        stray = sum(1 for r in conn.execute(
            'SELECT doc_kind, doc_id FROM posting WHERE ledger_id=?', (vid_,))
            if not (r['doc_kind'] in DOC_TYPES and r['doc_id'] in own_docs))
        busy = conn.execute(
            'SELECT (SELECT COUNT(*) FROM voucher_line WHERE ledger_id=?) '
            '     + (SELECT COUNT(*) FROM sales_order WHERE party_id=?)',
            (vid_, vid_)).fetchone()[0]
        left = len(own_docs - invoices)
        if (not v['system_key'] and not left and not stray and not busy
                and abs(float(v['opening_bal'] or 0)) <= 0.004):
            fam_rows.append(v)
            family.add(vid_)
            added.append(vid_)
        else:
            renamed.append(dict(ledger_id=vid_, name=v['name'],
                                firm=v['company'], documents=len(mine),
                                kept=left + stray, change=0.0))
    fam = sorted(family)
    posted += _each_chunk(
        conn, 'SELECT doc_kind, doc_id, company_id, doc_no, date, narration, '
              '       id, ledger_id, debit, credit '
              'FROM posting WHERE ledger_id IN (%s)', added)

    # A document's own lines can land on another party's account. SHREE
    # posts the round off on BST's bills to A B C INDUSTRIES (never more
    # than 50 paise a line) and stands PARMAR INDUSTRIES as the sales account
    # on eleven BSI bills. Deleting the document takes those lines too, as
    # deleting it on its own always has, so the accounts they move are shown.
    by_doc = {d['id']: d for d in docs}
    by_name = {r['ledger_id']: r for r in renamed}
    touches = {}
    for kind in DOC_TYPES:
        ids = sorted(d['id'] for d in docs if d['doc_type'] == kind)
        for r in _each_chunk(
                conn, 'SELECT p.doc_id, p.ledger_id, p.company_id, l.name, '
                      '       p.debit - p.credit AS amount '
                      'FROM posting p JOIN ledger l ON l.id=p.ledger_id '
                      'JOIN account_group g ON g.id=l.group_id '
                      "WHERE p.doc_kind='" + kind + "' AND p.doc_id IN (%s) "
                      "AND (l.party_kind<>'' OR g.is_party=1)", ids):
            if r['ledger_id'] in family or COLLECTION_ACCOUNT.search(r['name']):
                continue
            if r['ledger_id'] == by_doc[r['doc_id']]['party_id']:
                by_name[r['ledger_id']]['change'] = r2(
                    by_name[r['ledger_id']]['change'] - r['amount'])
                continue
            t = touches.setdefault(r['ledger_id'], dict(
                ledger_id=r['ledger_id'], name=r['name'],
                firm=firms.get(r['company_id'], {}).get('name', ''),
                entries=0, change=0.0, round_off=True))
            t['entries'] += 1
            t['change'] = r2(t['change'] - r['amount'])
            t['round_off'] = t['round_off'] and abs(r['amount']) < ROUND_OFF_LINE

    # Orders: this account's own, and any left with nothing once its
    # documents have gone.
    orders = {r['id'] for r in _each_chunk(
        conn, 'SELECT id FROM sales_order WHERE party_id IN (%s)', fam)}
    behind = {d['order_id'] for d in docs if d['order_id']}
    behind |= {r['id'] for r in _each_chunk(
        conn, 'SELECT id FROM sales_order WHERE billed_invoice IN (%s)',
        sorted(invoices))}
    behind -= orders
    # One still answered for by a document that stays is only re-pointed;
    # the rest are stranded and go, as they would with a single challan.
    kept = {r['order_id'] for r in _each_chunk(
        conn, 'SELECT id, order_id FROM invoice WHERE order_id IN (%s)',
        sorted(behind)) if r['id'] not in invoices}
    stranded = behind - kept

    # Vouchers: every one with a line on the account, every one its postings
    # point at, and the GST journal of every bill that is going.
    vouchers = {r['voucher_id'] for r in _each_chunk(
        conn, 'SELECT DISTINCT voucher_id FROM voucher_line '
              'WHERE ledger_id IN (%s)', fam)}
    vouchers |= {p['doc_id'] for p in posted if p['doc_kind'] in VOUCHER_KINDS}
    vouchers |= {d['gst_voucher'] for d in docs if d['gst_voucher']}
    vouchers = {v['id'] for v in _each_chunk(
        conn, 'SELECT id FROM voucher WHERE id IN (%s)', sorted(vouchers))}
    vrows = _each_chunk(
        conn, 'SELECT id, company_id, kind, voucher_no, voucher_date, '
              '       bank_id, party_id '
              'FROM voucher WHERE id IN (%s)', sorted(vouchers))
    # An entry that also names another party is not this account's alone:
    # only its part comes out, and the other party's balance stays. Nor is
    # one that moved cash or money in a bank: the receipt stays in the cash
    # book, and only the customer's side of it goes.
    lines = {}
    for r in _each_chunk(
            conn, 'SELECT vl.id, vl.voucher_id, vl.ledger_id, vl.debit, '
                  '       vl.credit, l.name, l.party_kind, g.is_party, '
                  '       g.code AS gcode, g.parent_code AS gparent '
                  'FROM voucher_line vl JOIN ledger l ON l.id=vl.ledger_id '
                  'JOIN account_group g ON g.id=l.group_id '
                  'WHERE vl.voucher_id IN (%s)', sorted(vouchers)):
        lines.setdefault(r['voucher_id'], []).append(r)
    takeouts = [_take_out_plan(v, lines[v['id']], family) for v in vrows
                if any(_is_other_party(l, family) or _is_money_line(l, family)
                       for l in lines.get(v['id'], []))]
    shared_ids = {t['id'] for t in takeouts}
    vrows = [v for v in vrows if v['id'] not in shared_ids]
    vouchers -= shared_ids

    # Postings with no document behind them: opening balances carried from a
    # year never backed up, and the few pre-GST sales lifted as bare entries.
    opening = [p for p in posted if p['doc_kind'] == 'OPENING']
    bare = {}
    for p in posted:
        if p['doc_kind'] == 'IMPORT':
            bare[(p['company_id'], p['doc_no'], p['date'], p['narration'])] = p
    bare_ids = []
    for (co, no, date, narr), p in sorted(bare.items()):
        grp = rows(conn.execute(
            'SELECT p.id, p.ledger_id, l.name, l.party_kind, g.is_party '
            'FROM posting p JOIN ledger l ON l.id=p.ledger_id '
            'JOIN account_group g ON g.id=l.group_id '
            "WHERE p.company_id=? AND p.doc_kind='IMPORT' AND p.doc_no=? "
            'AND p.date=? AND p.narration=?', (co, no, date, narr)))
        others = sorted({g['name'] for g in grp if g['ledger_id'] not in family
                         and (g['party_kind'] or g['is_party'])
                         and not COLLECTION_ACCOUNT.search(g['name'])})
        if others:
            blocker(co, 'IMPORT', no, date, None,
                    'also names ' + ', '.join(others))
        bare_ids += [g['id'] for g in grp]
    strange = sorted({p['doc_kind'] for p in posted
                      if p['doc_kind'] not in DOC_TYPES + VOUCHER_KINDS
                      + ('OPENING', 'IMPORT')})
    for k in strange:
        blocker(led['company_id'], k, '', '', None,
                'entries of a kind this delete does not know how to undo')

    opening_bal = [r for r in fam_rows if abs(float(r['opening_bal'] or 0)) > 0.004]
    diff = {}
    for co in ({p['company_id'] for p in opening}
               | {r['company_id'] for r in opening_bal}
               | {t['company_id'] for t in takeouts if t['carry'] > 0.004}):
        d = one(conn.execute(
            'SELECT id FROM ledger WHERE company_id=? AND lower(name)=lower(?)',
            (co, OPENING_DIFF_LEDGER)))
        if d:
            diff[co] = d['id']
        else:
            blocker(co, 'OPENING', '', '', None, 'this firm has no "%s" account '
                    'to carry balances to' % OPENING_DIFF_LEDGER)

    # Not a party: only ever deleted while nothing at all is written to it.
    if not _is_party(led):
        used = len(posted) or len(invoices) + len(vouchers) + len(takeouts)
        if used:
            blockers[:] = [dict(company_id=led['company_id'],
                                firm=firms.get(led['company_id'], {}).get('name', ''),
                                kind='', label='', no='', date='',
                                doc_id=None, why='%d entr%s written against it'
                                % (used, 'y is' if used == 1 else 'ies are'))]

    # What the screen shows: a line per firm.
    bal = {}
    for p in posted:
        bal[p['ledger_id']] = bal.get(p['ledger_id'], 0.0) + p['debit'] - p['credit']
    per = {}

    def firm_row(co):
        if co not in per:
            per[co] = dict(company_id=co, firm=firms.get(co, {}).get('name', ''),
                           tax_mode=firms.get(co, {}).get('tax_mode') or 'gst',
                           challans=0, bills=0, purchases=0, credit_notes=0,
                           debit_notes=0, orders=0, receipts=0, payments=0,
                           journals=0, contras=0, opening=0, shared=0, eway=0,
                           value=0.0)
        return per[co]
    names = {'PURCHASE': 'purchases', 'CREDIT': 'credit_notes',
             'DEBIT': 'debit_notes', 'RECEIPT': 'receipts',
             'PAYMENT': 'payments', 'JOURNAL': 'journals', 'CONTRA': 'contras'}
    for d in docs:
        f = firm_row(d['company_id'])
        if d['doc_type'] == 'SALE':
            f['challans' if f['tax_mode'] == 'none' else 'bills'] += 1
            f['value'] = r2(f['value'] + float(d['grand_total'] or 0))
            if f['tax_mode'] != 'none' and (d['eway_no'] or '').strip():
                f['eway'] += 1
        else:
            f[names[d['doc_type']]] += 1
    for v in vrows:
        firm_row(v['company_id'])[names[v['kind']]] += 1
    for t in takeouts:
        if t['others']:
            firm_row(t['company_id'])['shared'] += 1
    order_firms = _each_chunk(
        conn, 'SELECT company_id FROM sales_order WHERE id IN (%s)',
        sorted(orders | stranded))
    for o in order_firms:
        firm_row(o['company_id'])['orders'] += 1
    for p in opening:
        firm_row(p['company_id'])['opening'] += 1
    for r in opening_bal:
        firm_row(r['company_id'])['opening'] += 1
    for r in bare.values():
        firm_row(r['company_id'])['journals'] += 1

    # Documents already one-sided: mostly the other half went when a bank
    # account was deleted with its entries (the CO-OP banks, Patel Steel's
    # Kotak); challan I 68 came from SHREE that way.
    # Taking the rest away moves what that firm's trial balance is out by,
    # by exactly what they carried -- said here, and held to in the delete.
    carried = {}

    def carry(company_id, net):
        if abs(net) > 0.05:
            c = carried.setdefault(company_id, [0, 0.0])
            c[0] += 1
            c[1] += net
    kind_of = [(d['id'], d['doc_type']) for d in docs] \
        + [(v['id'], v['kind']) for v in vrows]
    for kind in DOC_TYPES + VOUCHER_KINDS:
        ids = sorted(i for i, k in kind_of if k == kind)
        for r in _each_chunk(
                conn, 'SELECT company_id, SUM(debit - credit) AS net '
                      "FROM posting WHERE doc_kind='" + kind + "' "
                      'AND doc_id IN (%s) GROUP BY doc_id, company_id', ids):
            carry(r['company_id'], r['net'])
    for (co, no, date, narr) in bare:
        net = conn.execute(
            'SELECT COALESCE(SUM(debit - credit), 0) FROM posting '
            "WHERE company_id=? AND doc_kind='IMPORT' AND doc_no=? AND date=? "
            'AND narration=?', (co, no, date, narr)).fetchone()[0]
        carry(co, float(net or 0))
    out_by = _book_totals(conn, sorted(carried))
    lopsided = [dict(company_id=co, firm=firms.get(co, {}).get('name', ''),
                     documents=n, change=r2(-net), out_by=r2(out_by[co]),
                     then=r2(out_by[co] - net))
                for co, (n, net) in sorted(carried.items())]

    # Receipts and payments that moved money stay where SHREE has them, in
    # the cash and bank books; only this account's side of them goes.
    kept = {}
    for t in takeouts:
        if t['others']:
            continue
        k = kept.setdefault(t['company_id'], dict(
            company_id=t['company_id'],
            firm=firms.get(t['company_id'], {}).get('name', ''),
            entries=0, money=0.0, carried=0.0))
        k['entries'] += 1
        k['money'] = r2(k['money'] + abs(t['money']))
        k['carried'] = r2(k['carried'] + t['carry'])
    # A cash bill posts its money to the cash account itself and goes whole
    # with its customer, goods, tax and all -- so what that takes out of the
    # cash and bank books is said first.
    cash_bills = {}
    for kind in DOC_TYPES:
        ids = sorted(d['id'] for d in docs if d['doc_type'] == kind)
        for r in _each_chunk(
                conn, 'SELECT p.company_id, p.doc_id, '
                      '       SUM(p.debit - p.credit) AS amount '
                      'FROM posting p JOIN ledger l ON l.id=p.ledger_id '
                      'JOIN account_group g ON g.id=l.group_id '
                      "WHERE p.doc_kind='" + kind + "' AND p.doc_id IN (%s) "
                      'AND ' + MONEY_SQL + ' '
                      'GROUP BY p.company_id, p.doc_id', ids):
            c = cash_bills.setdefault(r['company_id'], dict(
                company_id=r['company_id'],
                firm=firms.get(r['company_id'], {}).get('name', ''),
                documents=0, money=0.0))
            c['documents'] += 1
            c['money'] = r2(c['money'] + float(r['amount'] or 0))

    dates = [d['invoice_date'] for d in docs] + [v['voucher_date'] for v in vrows]
    ledgers = [dict(id=r['id'], company_id=r['company_id'], firm=r['company'],
                    name=r['name'], code=r['code'] or '',
                    balance=r2(float(r['opening_bal'] or 0) + bal.get(r['id'], 0)))
               for r in fam_rows]
    # What is owed is read the way the Accounts screen reads it: from the
    # accounts book alone, since a GST firm's ledger repeats the challans.
    held, known = _account_refs([(x['company_id'], x['id']) for x in ledgers],
                                money_books(conn))
    if not _is_party(led):
        held, known = [(led['company_id'], led['id'])], True
    held = {lid for _, lid in held}
    return dict(
        ledger_id=led['id'], name=led['name'], is_party=_is_party(led),
        ledgers=ledgers,
        balance=r2(sum(x['balance'] for x in ledgers if x['id'] in held))
        if known else None,
        firms=[per[k] for k in sorted(per, key=lambda c: per[c]['firm'])],
        documents=len(invoices) + len(vouchers) + len(orders | stranded)
        + len(bare),
        entries=len(posted), first=min(dates) if dates else '',
        last=max(dates) if dates else '',
        blocked=blockers,
        touches=sorted(touches.values(), key=lambda t: -abs(t['change'])),
        renamed=renamed,
        shared=[dict(company_id=t['company_id'],
                     firm=firms.get(t['company_id'], {}).get('name', ''),
                     kind=t['kind'], label=KIND_LABEL.get(t['kind'], 'Entry'),
                     no=t['no'], date=t['date'], doc_id=t['id'],
                     others=t['others'], carried=t['carry'])
                for t in takeouts if t['others']],
        carried=r2(sum(t['carry'] for t in takeouts if t['others'])),
        kept=[kept[k] for k in sorted(kept, key=lambda c: kept[c]['firm'])],
        cash_bills=[cash_bills[k] for k in
                    sorted(cash_bills, key=lambda c: cash_bills[c]['firm'])],
        lopsided=lopsided,
        # The working sets, kept off the wire by account_delete_check.
        _family=fam, _invoices=invoices, _orders=orders, _stranded=stranded,
        _behind=behind, _vouchers=vouchers, _takeouts=takeouts,
        _opening=opening, _opening_bal=opening_bal, _bare=bare_ids, _diff=diff)


def account_delete_check(conn, lid):
    """What deleting this account would take with it -- for the screen."""
    led = _ledger_for_delete(conn, lid)
    fp = (_account_footprint if _is_party(led) else _nonparty_plan)(conn, led)
    return {k: v for k, v in fp.items() if not k.startswith('_')}


# ------------------------------------- deleting an account that is not a party
#
# 26 Sep 2026, the user: *there should always be a way to delete anything and
# everything*. A bank, cash, expense or tax account with entries used to be
# refused ("only deleted while nothing is written against it") -- 316 of them
# in these books. Asked what should become of its entries, the user chose to
# be offered both each time:
#
#   - **moved** onto another account picked then (an old bank into the one in
#     use, a duplicate expense head into the right one). Every document and
#     every other balance stays; the account picked takes the history.
#   - **deleted with it**: every bill, challan, purchase and voucher with a
#     line on it goes whole, the way a customer's go with the customer. Other
#     accounts on those entries -- customers too -- move, and are shown first.
#
# Either way what has no document of its own (an opening balance, a bare
# entry lifted from SHREE) is carried onto Difference in Opening Balances, a
# copy of the books is taken first, and the trial balance is held to exactly
# what the screen said. Built-in accounts still stay: every bill posts to
# them, and the app makes them again when it starts.

def _nonparty_plan(conn, led):
    """What deleting an account that is not a party touches, both ways."""
    if led['system_key']:
        raise ApiError('"%s" is a built-in account and cannot be deleted.'
                       % led['name'])
    lid, cid = led['id'], led['company_id']
    firm = get_company(conn, cid)
    posted = rows(conn.execute(
        'SELECT id, doc_kind, doc_id, debit, credit, date FROM posting '
        'WHERE ledger_id=?', (lid,)))
    invoices = {r['id'] for r in conn.execute(
        'SELECT id FROM invoice WHERE party_id=? OR sales_ac_id=?', (lid, lid))}
    invoices |= {p['doc_id'] for p in posted if p['doc_kind'] in DOC_TYPES}
    frontier = set(invoices)
    while frontier:                     # the bills raised off its challans
        made = {r['id'] for r in _each_chunk(
            conn, 'SELECT id FROM invoice WHERE from_challan IN (%s)',
            sorted(frontier))} - invoices
        invoices |= made
        frontier = made
    docs = _each_chunk(
        conn, 'SELECT id, company_id, doc_type, invoice_date, grand_total, '
              '       gst_voucher, eway_no FROM invoice WHERE id IN (%s)',
        sorted(invoices))
    vouchers = {r['voucher_id'] for r in conn.execute(
        'SELECT DISTINCT voucher_id FROM voucher_line WHERE ledger_id=?', (lid,))}
    vouchers |= {r['id'] for r in conn.execute(
        'SELECT id FROM voucher WHERE bank_id=? OR party_id=?', (lid, lid))}
    vouchers |= {p['doc_id'] for p in posted if p['doc_kind'] in VOUCHER_KINDS}
    vouchers |= {d['gst_voucher'] for d in docs if d['gst_voucher']}
    vrows = _each_chunk(
        conn, 'SELECT id, company_id, kind, voucher_date FROM voucher '
              'WHERE id IN (%s)', sorted(vouchers))
    orders = [r['id'] for r in conn.execute(
        'SELECT id FROM sales_order WHERE party_id=?', (lid,))]
    loose = [p['id'] for p in posted
             if p['doc_kind'] not in DOC_TYPES + VOUCHER_KINDS]

    # What deleting the entries does to every other account on them.
    change, net = {}, 0.0
    for kind in DOC_TYPES + VOUCHER_KINDS:
        ids = sorted(d['id'] for d in docs if d['doc_type'] == kind) \
            if kind in DOC_TYPES else sorted(v['id'] for v in vrows
                                             if v['kind'] == kind)
        for r in _each_chunk(
                conn, 'SELECT ledger_id, SUM(debit - credit) AS n FROM posting '
                      "WHERE doc_kind='" + kind + "' AND doc_id IN (%s) "
                      'GROUP BY ledger_id', ids):
            net += float(r['n'] or 0)
            if r['ledger_id'] != lid:
                change[r['ledger_id']] = change.get(r['ledger_id'], 0.0) \
                    - float(r['n'] or 0)
    moved = [k for k, v in change.items() if abs(v) > 0.004]
    others = _each_chunk(
        conn, 'SELECT l.id, l.name, l.party_kind, g.is_party FROM ledger l '
              'JOIN account_group g ON g.id=l.group_id WHERE l.id IN (%s)',
        sorted(moved))
    others = sorted((dict(ledger_id=o['id'], name=o['name'],
                          party=_is_party(o), change=r2(change[o['id']]))
                     for o in others), key=lambda o: -abs(o['change']))

    names = {'PURCHASE': 'purchases', 'CREDIT': 'credit_notes',
             'DEBIT': 'debit_notes', 'RECEIPT': 'receipts',
             'PAYMENT': 'payments', 'JOURNAL': 'journals', 'CONTRA': 'contras'}
    row = dict(company_id=cid, firm=firm['name'],
               tax_mode=firm.get('tax_mode') or 'gst', challans=0, bills=0,
               purchases=0, credit_notes=0, debit_notes=0, orders=len(orders),
               receipts=0, payments=0, journals=0, contras=0,
               opening=len(loose) + (1 if abs(float(led['opening_bal'] or 0))
                                     > 0.004 else 0),
               shared=0, eway=0, value=0.0)
    for d in docs:
        if d['doc_type'] == 'SALE':
            row['challans' if row['tax_mode'] == 'none' else 'bills'] += 1
            row['value'] = r2(row['value'] + float(d['grand_total'] or 0))
            if row['tax_mode'] != 'none' and (d['eway_no'] or '').strip():
                row['eway'] += 1
        else:
            row[names[d['doc_type']]] += 1
    for v in vrows:
        row[names[v['kind']]] += 1

    bal = r2(float(led['opening_bal'] or 0)
             + sum(p['debit'] - p['credit'] for p in posted))
    lines = conn.execute('SELECT COUNT(*) FROM voucher_line WHERE ledger_id=?',
                         (lid,)).fetchone()[0]
    history = bool(posted or docs or vrows or orders or lines
                   or abs(float(led['opening_bal'] or 0)) > 0.004)
    diff = one(conn.execute(
        'SELECT id FROM ledger WHERE company_id=? AND lower(name)=lower(?)',
        (cid, OPENING_DIFF_LEDGER)))
    out_by = _book_totals(conn, [cid])[cid]
    dates = [d['invoice_date'] for d in docs] + [v['voucher_date'] for v in vrows]
    return dict(
        ledger_id=lid, name=led['name'], is_party=False, company_id=cid,
        firm=firm['name'], from_shree=led['source'] == 'SHREE',
        ledgers=[dict(id=lid, company_id=cid, firm=firm['name'],
                      name=led['name'], code=led['code'] or '', balance=bal)],
        balance=bal, firms=[row] if history else [],
        documents=len(docs) + len(vrows) + len(orders), entries=len(posted),
        first=min(dates) if dates else '', last=max(dates) if dates else '',
        blocked=[], touches=[], renamed=[], shared=[], carried=0.0,
        lopsided=[dict(company_id=cid, firm=firm['name'],
                       documents=len(docs) + len(vrows), change=r2(-net),
                       out_by=r2(out_by), then=r2(out_by - net))]
        if abs(net) > 0.05 else [],
        choose=history, others=others[:12], others_count=len(others),
        parties_moved=sum(1 for o in others if o['party']),
        move_default=diff['id'] if diff else None,
        _docs=[d['id'] for d in docs], _vouchers=sorted(v['id'] for v in vrows),
        _orders=orders, _loose=loose, _net=net,
        _diff=diff['id'] if diff else None)


def _repoint_ledger(conn, old, new):
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


def _delete_nonparty(conn, led, move_to=None, entries=False, backup=True):
    """Delete an account that is not a party: its entries moved onto
    `move_to`, or deleted with it (`entries`), or -- with none -- just it."""
    plan = _nonparty_plan(conn, led)
    lid, cid = led['id'], led['company_id']
    target = None
    if move_to:
        target = one(conn.execute('SELECT * FROM ledger WHERE id=? AND '
                                  'company_id=?', (qint(move_to, 'account'), cid)))
        if not target:
            raise ApiError('The account to move the entries to is not in %s.'
                           % plan['firm'])
        if target['id'] == lid:
            raise ApiError('Pick another account to move the entries to.')
    if plan['choose'] and not target and not entries:
        raise ApiError('%s has entries written against it. Choose where they '
                       'go: onto another account, or deleted with it.'
                       % led['name'], 409, {'cascade': True, 'choose': True})
    carry = plan['choose'] and not target and bool(
        plan['_loose'] or abs(float(led['opening_bal'] or 0)) > 0.004)
    if carry and not plan['_diff']:
        raise ApiError('%s has no "%s" account to carry its opening balance '
                       'to.' % (plan['firm'], OPENING_DIFF_LEDGER))

    conn.commit()
    copy = backup_books(conn, 'delete') if (plan['choose'] and backup) else None
    try:
        before = _book_totals(conn, [cid])[cid]
        expect = 0.0
        if target:
            _repoint_ledger(conn, lid, target['id'])
            conn.execute('UPDATE ledger SET opening_bal=opening_bal+? '
                         'WHERE id=?', (float(led['opening_bal'] or 0),
                                        target['id']))
        elif plan['choose']:
            behind = set()
            for iid in plan['_docs']:
                behind |= orders_behind(conn, iid)
            for iid in sorted(plan['_docs'], reverse=True):
                inv = one(conn.execute('SELECT * FROM invoice WHERE id=?',
                                       (iid,)))
                if inv:
                    _purge_invoice(conn, inv)
            for oid in sorted(behind):
                settle_order(conn, oid)
            for oid in plan['_orders']:
                conn.execute('UPDATE invoice SET order_id=NULL WHERE order_id=?',
                             (oid,))
                conn.execute('DELETE FROM sales_order_item WHERE order_id=?',
                             (oid,))
                conn.execute('DELETE FROM sales_order WHERE id=?', (oid,))
            for vid in plan['_vouchers']:
                conn.execute('UPDATE invoice SET gst_voucher=NULL '
                             'WHERE gst_voucher=?', (vid,))
                _drop_voucher(conn, vid)
            if carry:
                # An opening balance or a bare entry has no document to go
                # with; it is carried, so the year it opened still adds up.
                conn.execute('UPDATE posting SET ledger_id=? WHERE ledger_id=?',
                             (plan['_diff'], lid))
                conn.execute('UPDATE ledger SET opening_bal=opening_bal+? '
                             'WHERE id=?', (float(led['opening_bal'] or 0),
                                            plan['_diff']))
            expect = -plan['_net']

        conn.execute('UPDATE voucher SET party_id=NULL WHERE party_id=?', (lid,))
        conn.execute('UPDATE voucher SET bank_id=NULL WHERE bank_id=?', (lid,))
        conn.execute('UPDATE invoice SET sales_ac_id=NULL WHERE sales_ac_id=?',
                     (lid,))
        conn.execute('UPDATE ledger SET linked_ledger=NULL WHERE linked_ledger=?',
                     (lid,))
        left = conn.execute(
            'SELECT (SELECT COUNT(*) FROM posting WHERE ledger_id=?) '
            '     + (SELECT COUNT(*) FROM voucher_line WHERE ledger_id=?) '
            '     + (SELECT COUNT(*) FROM invoice WHERE party_id=?) '
            '     + (SELECT COUNT(*) FROM sales_order WHERE party_id=?)',
            (lid,) * 4).fetchone()[0]
        if left:
            raise ApiError('%d entr%s would still point at %s after the '
                           'delete, so nothing was deleted.'
                           % (left, 'y' if left == 1 else 'ies', led['name']),
                           500)
        conn.execute('DELETE FROM ledger WHERE id=?', (lid,))
        # For the SHREE update: the account comes back from SHREE, and this
        # is done to it again.
        if target:
            record_rule(conn, 'rename_ledger', cid, led['name'].strip(),
                        target['name'].strip(),
                        'Entries moved onto %s and deleted here'
                        % target['name'].strip())
        else:
            record_rule(conn, 'delete_account', cid, led['name'].strip(),
                        'entries' if plan['choose'] else '',
                        'Deleted with its entries in this app'
                        if plan['choose'] else 'Deleted in this app')
        after = _book_totals(conn, [cid])[cid]
        if abs(after - before - expect) > 0.05:
            raise ApiError('Deleting %s would have put the books out by %.2f, '
                           'so nothing was deleted.'
                           % (led['name'], after - before - expect), 500)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    gone = 0 if target else plan['documents']
    return dict(ok=True, name=led['name'], documents=gone,
                moved_to=target['name'] if target else '',
                entries=plan['entries'], shared=0, ledgers=1,
                firms=plan['firms'], backup=copy)


def _ledger_for_delete(conn, lid):
    led = one(conn.execute(
        'SELECT l.*, g.is_party FROM ledger l '
        'JOIN account_group g ON g.id=l.group_id WHERE l.id=?', (lid,)))
    if not led:
        raise ApiError('Account not found.', 404)
    return led


def _book_totals(conn, cids):
    """company -> what its trial balance is out by (0 when it squares)."""
    out = {}
    for co in cids:
        p = conn.execute('SELECT COALESCE(SUM(debit - credit), 0) FROM posting '
                         'WHERE company_id=?', (co,)).fetchone()[0]
        o = conn.execute('SELECT COALESCE(SUM(opening_bal), 0) FROM ledger '
                         'WHERE company_id=?', (co,)).fetchone()[0]
        out[co] = float(p or 0) + float(o or 0)
    return out


def copy_books(conn, path):
    """The whole database copied to `path`, whole even while the app is in use.

    The copy is a file to keep, not a live database: one file, with no -wal
    or -shm beside it, which is what VACUUM INTO writes. SQLite older than
    3.27 has no VACUUM INTO -- nor will it run inside a transaction -- and the
    backup API then does the copying."""
    try:
        conn.execute('VACUUM INTO ?', (path,))
    except sqlite3.OperationalError:
        dst = sqlite3.connect(path)
        try:
            conn.backup(dst)
            dst.execute('PRAGMA journal_mode=DELETE')
        finally:
            dst.close()
        for tail in ('-wal', '-shm'):
            try:
                os.remove(path + tail)
            except OSError:
                pass


def backup_books(conn, reason):
    """Copy the whole database beside itself before something is undone.

    SQLite makes the copy, so it is whole even while the app is in use. Only
    the newest few taken for the same reason are kept.
    """
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    path = '%s.before-%s-%s' % (db.DB_PATH, reason, stamp)
    n = 1
    while os.path.exists(path):         # two deletes in the same second
        n += 1
        path = '%s.before-%s-%s-%d' % (db.DB_PATH, reason, stamp, n)
    copy_books(conn, path)
    if os.name != 'nt':
        try:
            os.chmod(path, db.FILE_MODE)
        except OSError:
            pass
    mine = sorted(f for f in glob.glob('%s.before-%s-*'
                                       % (glob.escape(db.DB_PATH), reason))
                  if not f.endswith(('-wal', '-shm', '-journal')))
    for old in mine[:-DELETE_BACKUPS_KEPT]:
        for f in (old, old + '-wal', old + '-shm'):
            try:
                os.remove(f)
            except OSError:
                pass
    return os.path.basename(path)


def delete_account(conn, lid, cascade=False, backup=True, move_to=None,
                   entries=False):
    """Delete an account, and -- for a party -- everything written to it.

    Any other account goes the way asked: its entries moved onto `move_to`,
    or deleted with it (`entries`)."""
    led = _ledger_for_delete(conn, lid)
    if not _is_party(led):
        return _delete_nonparty(conn, led, move_to, entries, backup)
    fp = _account_footprint(conn, led)
    b = fp['blocked']
    if b and _is_party(led):
        raise ApiError('%s cannot be deleted yet: %d entr%s also concern%s '
                       'someone else.' % (led['name'], len(b),
                                          'y' if len(b) == 1 else 'ies',
                                          's' if len(b) == 1 else ''),
                       409, {'blocked': b})
    if b:
        raise ApiError('%s cannot be deleted: %s. Mark it inactive instead.'
                       % (led['name'], b[0]['why']), 409, {'blocked': b})
    family = fp['_family']
    has_history = bool(fp['documents'] or fp['entries'] or fp['shared']
                       or fp['_opening_bal'])
    if has_history and not cascade:
        # Say what is there: an account with only an opening balance used to
        # be said to have "0 document(s)" to delete with it.
        held = []
        if fp['documents']:
            held.append('%d document%s' % (fp['documents'],
                                           '' if fp['documents'] == 1 else 's'))
        if fp['entries'] and not fp['documents']:
            held.append('%d entr%s' % (fp['entries'],
                                       'y' if fp['entries'] == 1 else 'ies'))
        if fp['_opening_bal'] and not held:
            held.append('an opening balance')
        raise ApiError('%s has %s against it. Confirm to delete %s with the '
                       'account.' % (led['name'], ' and '.join(held) or 'history',
                                     'it' if len(held) == 1 and not
                                     fp['documents'] else 'them'),
                       409, {'cascade': True})

    # The copy is taken before the first write, while no transaction is open.
    conn.commit()
    backup = backup_books(conn, 'delete') if (has_history and backup) else None
    firm_ids = sorted({r['company_id'] for r in fp['ledgers']}
                      | {f['company_id'] for f in fp['firms']}
                      | {l['company_id'] for l in fp['lopsided']})
    try:
        before = _book_totals(conn, firm_ids)
        for iid in sorted(fp['_invoices'], reverse=True):
            inv = one(conn.execute('SELECT * FROM invoice WHERE id=?', (iid,)))
            if inv:
                _purge_invoice(conn, inv)
        own = sorted(fp['_orders'] | fp['_stranded'])
        for part in _chunks(own, 500):
            conn.execute('UPDATE invoice SET order_id=NULL WHERE order_id IN (%s)'
                         % _in(part), part)
            conn.execute('DELETE FROM sales_order_item WHERE order_id IN (%s)'
                         % _in(part), part)
            conn.execute('DELETE FROM sales_order WHERE id IN (%s)' % _in(part),
                         part)
        for oid in sorted(fp['_behind'] - fp['_stranded']):
            settle_order(conn, oid)
        for plan in fp['_takeouts']:
            _take_out(conn, plan, family, fp['_diff'].get(plan['company_id']),
                      led['name'])
        vids = sorted(fp['_vouchers'])
        for part in _chunks(vids, 500):
            conn.execute('UPDATE invoice SET gst_voucher=NULL '
                         'WHERE gst_voucher IN (%s)' % _in(part), part)
        for vid in vids:
            _drop_voucher(conn, vid)
        for part in _chunks(fp['_bare'], 500):
            conn.execute('DELETE FROM posting WHERE id IN (%s)' % _in(part), part)

        # An opening balance has no document to go with. It is carried onto
        # Difference in Opening Balances on the same day, so the year it
        # opened still adds up.
        for p in fp['_opening']:
            conn.execute('DELETE FROM posting WHERE id=?', (p['id'],))
            conn.execute(
                'INSERT INTO posting(company_id, date, ledger_id, doc_kind, '
                'doc_id, doc_no, debit, credit, narration) '
                "VALUES (?,?,?,'OPENING',0,?,?,?,?)",
                (p['company_id'], p['date'], fp['_diff'][p['company_id']],
                 p['doc_no'], p['debit'], p['credit'],
                 'Opening balance of %s, account deleted' % led['name']))
        for r in fp['_opening_bal']:
            conn.execute('UPDATE ledger SET opening_bal=opening_bal+? WHERE id=?',
                         (float(r['opening_bal']), fp['_diff'][r['company_id']]))

        # Nothing may be left pointing at the account.
        for part in _chunks(family, 500):
            ph = _in(part)
            conn.execute('UPDATE voucher SET party_id=NULL WHERE party_id IN (%s)'
                         % ph, part)
            conn.execute('UPDATE voucher SET bank_id=NULL WHERE bank_id IN (%s)'
                         % ph, part)
            conn.execute('UPDATE invoice SET sales_ac_id=NULL '
                         'WHERE sales_ac_id IN (%s)' % ph, part)
            conn.execute('UPDATE ledger SET linked_ledger=NULL '
                         'WHERE linked_ledger IN (%s)' % ph, part)
            left = conn.execute(
                'SELECT (SELECT COUNT(*) FROM posting WHERE ledger_id IN (%s)) '
                '     + (SELECT COUNT(*) FROM voucher_line WHERE ledger_id IN (%s)) '
                '     + (SELECT COUNT(*) FROM invoice WHERE party_id IN (%s)) '
                '     + (SELECT COUNT(*) FROM sales_order WHERE party_id IN (%s))'
                % (ph, ph, ph, ph), part * 4).fetchone()[0]
            if left:
                raise ApiError('%d entr%s would still point at %s after the '
                               'delete, so nothing was deleted.'
                               % (left, 'y' if left == 1 else 'ies',
                                  led['name']), 500)
            conn.execute('DELETE FROM ledger WHERE id IN (%s)' % ph, part)

        # Every name the account went by, so a SHREE update that brings it
        # back finds it and deletes it again.
        for r in fp['ledgers']:
            record_rule(conn, 'delete_account', r['company_id'],
                        r['name'].strip(), '', 'Deleted in this app')
        after = _book_totals(conn, firm_ids)
        allowed = {l['company_id']: l['change'] for l in fp['lopsided']}
        moved = {co: r2(after[co] - before[co] - allowed.get(co, 0))
                 for co in firm_ids}
        moved = {co: v for co, v in moved.items() if abs(v) > 0.05}
        if moved:
            raise ApiError('Deleting %s would have put the books out by %s, so '
                           'nothing was deleted.' % (led['name'], ', '.join(
                               '%.2f' % v for v in moved.values())), 500)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return dict(ok=True, name=led['name'], documents=fp['documents'],
                shared=len(fp['shared']),
                ledgers=len(family), firms=fp['firms'], backup=backup)


# ------------------------------------------------------ merging two accounts
#
# 3 Oct 2026, the user: *sometimes by mistake the user has created two
# different accounts for one customer and they both have transactions for
# different years. So add a option to merge accounts.*
#
# One of the two is kept. Everything written to the other -- challans, bills,
# orders, receipts, journals, its opening balance -- is moved onto it, in
# every firm the customer is written in, and the emptied account is deleted.
# Nothing is deleted or re-dated, so no firm's trial balance moves and the
# kept account ends on exactly what the two came to together. Where only the
# account going is written in a firm (the old spelling billed by BSI, say),
# its record there takes the kept name instead.
#
# Each move is written down (`merge_ledger`, firm by firm): a SHREE update
# brings the old account back under its old name, and merges it again.

# The copy of the books taken first is named for it (DELETE_BACKUPS_KEPT).
MERGE_REASON = 'merge'
# The year SHREE carries a balance into, as the import writes it.
CARRIED_LINE = 'Opening balance carried forward%'
# What a merged account brings from its record, where the kept one is blank.
MERGE_FILL = ('address1', 'address2', 'city', 'state', 'state_code', 'pin',
              'gstin', 'pan', 'phone', 'email', 'notes')


def _first_day(conn, cid):
    """1 April of the first year a firm's books hold."""
    first = conn.execute('SELECT MIN(date) FROM posting WHERE company_id=?',
                         (cid,)).fetchone()[0] or _today()
    y, m = int(first[:4]), int(first[5:7])
    return '%d-04-01' % (y if m >= 4 else y - 1)


def _opening_as_entry(conn, led, onto):
    """An imported account's opening balance, as an opening line on `onto`.

    The import gives an account its opening balance only in the first year it
    reads, and writes every carried-forward line afresh. Left in the opening
    balance of the account it went into, it would be counted again each
    update that kept that account; as a line, the update wipes it and the
    merge, played again, writes it once more.
    """
    ob = r2(led['opening_bal'] or 0)
    if abs(ob) <= 0.004:
        return
    day = _first_day(conn, led['company_id'])
    conn.execute(
        'INSERT INTO posting(company_id, date, ledger_id, doc_kind, doc_id, '
        "doc_no, debit, credit, narration) VALUES (?,?,?,'OPENING',0,?,?,?,?)",
        (led['company_id'], day, onto, db.fy_of(day), max(ob, 0.0),
         max(-ob, 0.0), 'Opening balance of %s' % led['name'].strip()))
    conn.execute('UPDATE ledger SET opening_bal=0 WHERE id=?', (led['id'],))


def _tidy_carried(conn, lid):
    """One carried-forward line a year: the two accounts each had one."""
    for r in conn.execute(
            "SELECT date, doc_no, COUNT(*) n, SUM(debit - credit) net, "
            "MIN(narration) narr, MIN(company_id) co FROM posting "
            "WHERE ledger_id=? AND doc_kind='OPENING' AND narration LIKE ? "
            "GROUP BY date, doc_no HAVING COUNT(*) > 1",
            (lid, CARRIED_LINE)).fetchall():
        conn.execute("DELETE FROM posting WHERE ledger_id=? AND "
                     "doc_kind='OPENING' AND date=? AND doc_no=? AND "
                     "narration LIKE ?", (lid, r['date'], r['doc_no'],
                                          CARRIED_LINE))
        net = r2(r['net'] or 0)
        if abs(net) > 0.004:
            conn.execute(
                'INSERT INTO posting(company_id, date, ledger_id, doc_kind, '
                'doc_id, doc_no, debit, credit, narration) '
                "VALUES (?,?,?,'OPENING',0,?,?,?,?)",
                (r['co'], r['date'], lid, r['doc_no'], max(net, 0.0),
                 max(-net, 0.0), r['narr']))


_SHREE_CODE = re.compile(r'^A(\d+)(?:-\d+)?$')


# The kinds of entry a document or voucher of its own stands behind. An
# opening line or a bare imported entry has no number to be shared by.
SHARED_KINDS = DOC_TYPES + VOUCHER_KINDS


def _shared_entries(conn, a, b):
    """The entries written to both accounts -- a journal moving a balance
    from one to the other ("HAWALO TRANSFER FROM SHREE ENTER TO SHREE ENT
    KAUSHIKBHAI"), a TDS journal with a line for each. Merged, each is one
    entry of one account and must count once: its lines there are added
    together, and one that only moved money between the two comes out.

    Answers [{kind, id, no, date, net, mine, others, gst}]: `net` is what its
    lines on the two come to, `mine` how many lines it has on them, `others`
    how many on any other account."""
    out = []
    for r in conn.execute(
            'SELECT p.doc_kind, p.doc_id, MIN(p.doc_no) no, MIN(p.date) day, '
            '       SUM(p.debit - p.credit) net, COUNT(*) n, '
            '       SUM(p.debit) dr FROM posting p '
            'WHERE p.ledger_id IN (?, ?) AND p.doc_kind IN (%s) '
            'GROUP BY p.doc_kind, p.doc_id '
            'HAVING COUNT(DISTINCT p.ledger_id) = 2' % _in(SHARED_KINDS),
            (a, b) + SHARED_KINDS).fetchall():
        others = conn.execute(
            'SELECT COUNT(*) FROM posting WHERE doc_kind=? AND doc_id=? AND '
            'ledger_id NOT IN (?, ?)', (r['doc_kind'], r['doc_id'], a, b)
        ).fetchone()[0]
        gst = r['doc_kind'] in VOUCHER_KINDS and bool(conn.execute(
            'SELECT 1 FROM invoice WHERE gst_voucher=?', (r['doc_id'],)
        ).fetchone())
        out.append(dict(kind=r['doc_kind'], id=r['doc_id'], no=r['no'] or '',
                        date=r['day'], net=r2(r['net'] or 0), mine=r['n'],
                        amount=r2(r['dr'] or 0), others=others, gst=gst))
    return out


def _once_each(conn, shared, lid):
    """After the move, each shared entry's lines on the kept account `lid`
    made one line; a transfer between the two, which nets to nothing and
    names no one else, taken out whole. Nothing else moves: the lines added
    together come to what they came to apart."""
    gone = 0
    for e in shared:
        kind, did = e['kind'], e['id']
        rows_ = conn.execute(
            'SELECT * FROM posting WHERE doc_kind=? AND doc_id=? AND '
            'ledger_id=? ORDER BY id', (kind, did, lid)).fetchall()
        if len(rows_) > 1 or (rows_ and abs(r2(rows_[0]['debit']
                                                - rows_[0]['credit'])) < 0.005):
            net = r2(sum(r['debit'] - r['credit'] for r in rows_))
            first = rows_[0]
            conn.execute('DELETE FROM posting WHERE doc_kind=? AND doc_id=? '
                         'AND ledger_id=?', (kind, did, lid))
            if abs(net) >= 0.005:
                conn.execute(
                    'INSERT INTO posting(company_id, date, ledger_id, doc_kind, '
                    'doc_id, doc_no, debit, credit, narration) '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    (first['company_id'], first['date'], lid, kind, did,
                     first['doc_no'], max(net, 0.0), max(-net, 0.0),
                     first['narration']))
        if kind not in VOUCHER_KINDS:
            continue
        lines = conn.execute('SELECT * FROM voucher_line WHERE voucher_id=? '
                             'AND ledger_id=? ORDER BY id', (did, lid)).fetchall()
        if lines:
            net = r2(sum(l['debit'] - l['credit'] for l in lines))
            conn.execute('DELETE FROM voucher_line WHERE voucher_id=? AND '
                         'ledger_id=?', (did, lid))
            if abs(net) >= 0.005:
                conn.execute('INSERT INTO voucher_line(voucher_id, ledger_id, '
                             'debit, credit, remarks) VALUES (?,?,?,?,?)',
                             (did, lid, max(net, 0.0), max(-net, 0.0),
                              lines[0]['remarks']))
        left = conn.execute('SELECT COUNT(*) FROM posting WHERE doc_kind=? AND '
                            'doc_id=?', (kind, did)).fetchone()[0]
        if not left:
            # Money moved from one to the other and nowhere else: on one
            # account it is nothing at all.
            conn.execute('UPDATE invoice SET gst_voucher=NULL WHERE '
                         'gst_voucher=?', (did,))
            _drop_voucher(conn, did)
            gone += 1
            continue
        total = one(conn.execute(
            'SELECT COALESCE(SUM(debit), 0) d, COALESCE(SUM(credit), 0) c '
            'FROM voucher_line WHERE voucher_id=?', (did,)))
        conn.execute('UPDATE voucher SET amount=? WHERE id=?',
                     (r2(max(total['d'], total['c'])), did))
    return gone


def _look_alike(conn, a, b):
    """Entries on the two that look the same -- one day, one kind, one
    amount -- but are separate documents, each with its own number. They are
    shown, never taken out: the BST and BSI Kotak loans pay the same EMI on
    the same day, and those are two payments. One entered twice is for a
    person to delete. Answers [(posting on a, posting on b)]."""
    sql = ('SELECT * FROM posting p WHERE ledger_id=? AND doc_kind IN (%s) '
           'AND NOT EXISTS (SELECT 1 FROM posting q WHERE q.doc_kind=p.doc_kind '
           'AND q.doc_id=p.doc_id AND q.ledger_id=?) ORDER BY date, id'
           % _in(SHARED_KINDS))
    theirs = {}
    for r in conn.execute(sql, (b,) + SHARED_KINDS + (a,)):
        theirs.setdefault((r['date'], r['doc_kind'], r2(r['debit']),
                           r2(r['credit'])), []).append(dict(r))
    out = []
    for r in conn.execute(sql, (a,) + SHARED_KINDS + (b,)):
        twin = theirs.get((r['date'], r['doc_kind'], r2(r['debit']),
                           r2(r['credit'])))
        if twin:
            out.append((dict(r), twin.pop(0)))
    return out


def _span(conn, led, other=None):
    """An account's balance, and its first and last entry -- not counting
    the carried-forward lines, nor an entry it shares with `other`, which
    belongs to neither one's own run of years."""
    shared = ''
    args = [led['id']]
    if other:
        shared = (' AND NOT EXISTS (SELECT 1 FROM posting q WHERE '
                  'q.doc_kind=posting.doc_kind AND q.doc_id=posting.doc_id '
                  'AND q.ledger_id=?)')
        args.append(other['id'])
    r = one(conn.execute(
        "SELECT MIN(date) first, MAX(date) last FROM posting "
        "WHERE ledger_id=? AND doc_kind<>'OPENING'" + shared, args))
    net = conn.execute('SELECT COALESCE(SUM(debit - credit), 0) FROM posting '
                       'WHERE ledger_id=?', (led['id'],)).fetchone()[0]
    return (r2(float(led['opening_bal'] or 0) + float(net or 0)),
            r['first'] or '', r['last'] or '')


def _stand_in(conn, old, new):
    """The line that already carries one account's balance into the other.

    SHREE renames an account and keeps its number; the import keys accounts
    by name, so a customer renamed in 2022-23 -- BALVIKRUPA INDUSTRIES to
    BALVIKRUPA INDUSTRIES JAGDISHBHAI, A117 and A117-2 -- came in as two.
    The old name stops with the balance it had, and the new name's first
    "Opening balance carried forward" line is that same balance, so the two
    added together count it twice. Recognised by the shared number, or by
    the line being exactly what the earlier account stopped at, with the
    earlier account's last entry before the later one's first. Returns
    (line, amount, the earlier account) or None. Two accounts that run side
    by side, or whose balances SHREE kept apart, are simply added together.
    """
    spans = {old['id']: _span(conn, old, new), new['id']: _span(conn, new, old)}
    for x, y in ((old, new), (new, old)):
        bal, _f, last = spans[x['id']]
        first_y = spans[y['id']][1]
        if abs(bal) <= 0.005 or not last or (first_y and first_y <= last):
            continue
        line = one(conn.execute(
            "SELECT * FROM posting WHERE ledger_id=? AND doc_kind='OPENING' "
            'AND narration LIKE ? AND date>? ORDER BY date, id LIMIT 1',
            (y['id'], CARRIED_LINE, last)))
        if not line:
            continue
        ca = _SHREE_CODE.match(x.get('code') or '')
        cb = _SHREE_CODE.match(y.get('code') or '')
        renamed = bool(ca and cb and ca.group(1) == cb.group(1))
        if renamed or abs(r2(line['debit'] - line['credit']) - bal) < 0.5:
            return line, bal, x
    return None


def _count_once(conn, line, amount, old, new):
    """Take the balance carried twice off the carried-forward line, onto
    Difference in Opening Balances -- the way a renamed bank was folded on
    15 Sep 2026. Neither the trial balance nor any other account moves."""
    diff = one(conn.execute('SELECT id FROM ledger WHERE company_id=? AND '
                            'lower(name)=lower(?)',
                            (line['company_id'], OPENING_DIFF_LEDGER)))
    if not diff:
        return 0.0
    net = r2(line['debit'] - line['credit'] - amount)
    if abs(net) < 0.005:
        conn.execute('DELETE FROM posting WHERE id=?', (line['id'],))
    else:
        conn.execute('UPDATE posting SET debit=?, credit=? WHERE id=?',
                     (max(net, 0.0), max(-net, 0.0), line['id']))
    conn.execute(
        'INSERT INTO posting(company_id, date, ledger_id, doc_kind, doc_id, '
        "doc_no, debit, credit, narration) VALUES (?,?,?,'OPENING',0,?,?,?,?)",
        (line['company_id'], line['date'], diff['id'], line['doc_no'],
         max(amount, 0.0), max(-amount, 0.0),
         'Accounts merged: %s and %s' % (old['name'].strip(),
                                         new['name'].strip())))
    return amount


def merge_ledger_into(conn, old, new):
    """Every entry on account `old` onto `new`, in the same firm, and `old`
    deleted. The SHREE update plays it again through this (`merge_ledger`).

    Answers the balance that was counted in both and is now counted once."""
    stand = _stand_in(conn, old, new)
    shared = _shared_entries(conn, old['id'], new['id'])
    _repoint_ledger(conn, old['id'], new['id'])
    _once_each(conn, shared, new['id'])
    if old.get('source') == 'SHREE':
        _opening_as_entry(conn, old, new['id'])
    else:
        conn.execute('UPDATE ledger SET opening_bal=opening_bal+? WHERE id=?',
                     (float(old['opening_bal'] or 0), new['id']))
    once = _count_once(conn, stand[0], stand[1], old, new) if stand else 0.0
    # The kept record keeps what it says; what it leaves blank, the other
    # one fills -- an address, a GSTIN, the firm that bills them.
    sets, args = [], []
    for f in MERGE_FILL:
        if not str(new.get(f) or '').strip() and str(old.get(f) or '').strip():
            sets.append('%s=?' % f)
            args.append(old[f])
            if f == 'gstin':
                sets.append('gst_type=?')
                args.append(old['gst_type'])
    for f in ('credit_limit', 'credit_days'):
        if not new.get(f) and old.get(f):
            sets.append('%s=?' % f)
            args.append(old[f])
    if not new.get('home_company') and old.get('home_company'):
        sets.append('home_company=?')
        args.append(old['home_company'])
    if (new.get('linked_ledger') in (None, old['id'])
            and old.get('linked_ledger') and old['linked_ledger'] != new['id']):
        sets.append('linked_ledger=?')
        args.append(old['linked_ledger'])
    kinds = {new.get('party_kind') or '', old.get('party_kind') or ''} - {''}
    if len(kinds) > 1:
        sets.append("party_kind='both'")
    elif kinds and not new.get('party_kind'):
        sets.append('party_kind=?')
        args.append(kinds.pop())
    if old.get('active') and not new.get('active'):
        sets.append('active=1')
    if sets:
        conn.execute('UPDATE ledger SET %s WHERE id=?' % ', '.join(sets),
                     args + [new['id']])
    conn.execute('UPDATE ledger SET linked_ledger=NULL WHERE id=? AND '
                 'linked_ledger=?', (new['id'], new['id']))
    _tidy_carried(conn, new['id'])
    # How a person is paid is kept by their account's name.
    conn.execute('UPDATE OR IGNORE staff SET name=? WHERE company_id=? AND '
                 'name=?', (staff_key(new['name']), new['company_id'],
                            staff_key(old['name'])))
    conn.execute('DELETE FROM ledger WHERE id=?', (old['id'],))
    return once


def rename_for_merge(conn, led, name):
    """The account going is the only one written in its firm: its record
    there takes the kept name, and so becomes the kept account's."""
    conn.execute('UPDATE ledger SET name=? WHERE id=?', (name, led['id']))
    if led.get('source') == 'SHREE':
        _opening_as_entry(conn, led, led['id'])
    conn.execute('UPDATE OR IGNORE staff SET name=? WHERE company_id=? AND '
                 'name=?', (staff_key(name), led['company_id'],
                            staff_key(led['name'])))


def _merge_side(conn, led):
    """One of the two accounts: its record in each firm, with what is
    written there and what it stands at."""
    fam = _account_family(conn, led)
    ids = [r['id'] for r in fam]
    full = {r['id']: r for r in _each_chunk(
        conn, 'SELECT * FROM ledger WHERE id IN (%s)', ids)}
    stats = {r['ledger_id']: r for r in _each_chunk(
        conn, "SELECT ledger_id, COUNT(*) n, SUM(debit - credit) net, "
              "MIN(CASE WHEN doc_kind<>'OPENING' THEN date END) first, "
              "MAX(CASE WHEN doc_kind<>'OPENING' THEN date END) last, "
              "SUM(doc_kind<>'OPENING') entries FROM posting "
              "WHERE ledger_id IN (%s) GROUP BY ledger_id", ids)}
    orders = {r['party_id']: r['n'] for r in _each_chunk(
        conn, 'SELECT party_id, COUNT(*) n FROM sales_order '
              'WHERE party_id IN (%s) GROUP BY party_id', ids)}
    ledgers = []
    for r in fam:
        s = stats.get(r['id']) or {}
        f = full[r['id']]
        ledgers.append(dict(
            id=r['id'], company_id=r['company_id'], firm=r['company'],
            firm_code=r['firm_code'], name=r['name'], code=r['code'] or '',
            system_key=r['system_key'], is_party=_is_party(r),
            source=f['source'] or '', gstin=(f['gstin'] or '').strip().upper(),
            city=f['city'] or '', entries=int(s.get('entries') or 0),
            orders=orders.get(r['id'], 0), first=s.get('first') or '',
            last=s.get('last') or '',
            balance=r2(float(f['opening_bal'] or 0) + float(s.get('net') or 0))))
    held, known = _account_refs([(x['company_id'], x['id']) for x in ledgers],
                                money_books(conn))
    if not _is_party(led):
        held, known = [(led['company_id'], led['id'])], True
    held = {lid for _, lid in held}
    firsts = [x['first'] for x in ledgers if x['first']]
    lasts = [x['last'] for x in ledgers if x['last']]
    return dict(
        ledger_id=led['id'], name=led['name'], is_party=_is_party(led),
        company_id=led['company_id'], ledgers=ledgers,
        entries=sum(x['entries'] for x in ledgers),
        orders=sum(x['orders'] for x in ledgers),
        first=min(firsts) if firsts else '', last=max(lasts) if lasts else '',
        balance=r2(sum(x['balance'] for x in ledgers if x['id'] in held))
        if known else None,
        gstins=sorted({x['gstin'] for x in ledgers if x['gstin']}),
        from_shree=any(x['source'] == 'SHREE' for x in ledgers),
        _full=full)


def _merge_plan(conn, gone, kept):
    """Firm by firm, which record of `gone` goes onto which of `kept`."""
    a, b = gone, kept
    if a['ledger_id'] == b['ledger_id']:
        raise ApiError('Pick another account to merge with -- this is the '
                       'same one.')
    if a['is_party'] != b['is_party']:
        raise ApiError('%s is %s and %s is not, so they cannot be merged. '
                       'A customer or supplier merges only with another '
                       'customer or supplier.'
                       % ((a['name'], 'a customer or supplier', b['name'])
                          if a['is_party'] else
                          (b['name'], 'a customer or supplier', a['name'])))
    in_b = {x['id'] for x in b['ledgers']}
    if a['ledger_id'] in in_b:
        # Already read as one account (a spelling twin in the same book):
        # only the record picked goes.
        moving = [x for x in a['ledgers'] if x['id'] == a['ledger_id']]
    else:
        moving = [x for x in a['ledgers'] if x['id'] not in in_b]
    if not a['is_party'] and moving[0]['company_id'] != b['company_id']:
        raise ApiError('%s and %s are in different firms. An account that is '
                       'not a customer or supplier merges only with another '
                       'in its own firm.' % (a['name'], b['name']))
    for x in moving:
        if x['system_key']:
            raise ApiError('"%s" is a built-in account and cannot be merged '
                           'into another. Merge the other one into it '
                           'instead.' % x['name'])

    # The kept account's record in each firm: the one picked, else the one
    # spelt as it is, else the busiest.
    target = {}
    for x in sorted(b['ledgers'], key=lambda x: (
            x['id'] != b['ledger_id'], name_key(x['name']) != name_key(b['name']),
            -x['entries'])):
        if x['id'] not in {m['id'] for m in moving}:
            target.setdefault(x['company_id'], x)
    steps = []
    for x in sorted(moving, key=lambda x: (
            x['firm'], name_key(x['name']) != name_key(b['name']), x['id'])):
        t = target.get(x['company_id'])
        if t:
            steps.append(dict(how='move', ledger=x, into=t,
                              firm=x['firm'], firm_code=x['firm_code']))
            continue
        clash = same_name(conn, 'ledger', x['company_id'], b['name'],
                          'id, name')
        if clash and clash['id'] != x['id']:
            raise ApiError('%s already has an account called %s that is not '
                           'this customer, so %s cannot take that name there.'
                           ' Rename one of them first.'
                           % (x['firm'], clash['name'], x['name']))
        steps.append(dict(how='rename', ledger=x, into=dict(x, name=b['name']),
                          firm=x['firm'], firm_code=x['firm_code']))
        target[x['company_id']] = dict(x, name=b['name'])
    return steps


def merge_check(conn, lid, into):
    """What merging account `lid` into `into` would do -- and the other way
    round, so the screen can let either name be the one kept. Reads only."""
    a = _ledger_for_delete(conn, lid)
    b = _ledger_for_delete(conn, qint(into, 'account'))
    sa, sb = _merge_side(conn, a), _merge_side(conn, b)

    firms = {c['id']: c for c in list_companies(conn)}

    def label(kind, company_id):
        if kind == 'SALE':
            return 'Challan' if firms.get(company_id, {}).get('tax_mode') \
                == 'none' else 'Bill'
        return KIND_LABEL.get(kind, 'Entry')

    def way(gone, kept):
        try:
            steps = _merge_plan(conn, gone, kept)
        except ApiError as e:
            return dict(blocked=e.message)
        # Entries written to both count once (see _shared_entries), and
        # entries that only look alike are shown for a person to judge.
        shared, alike = [], []
        for s in steps:
            if s['how'] != 'move':
                continue
            co = s['ledger']['company_id']
            for e in _shared_entries(conn, s['ledger']['id'], s['into']['id']):
                shared.append(dict(
                    e, firm_code=s['firm_code'], label=label(e['kind'], co),
                    out=not e['others'] and abs(e['net']) < 0.005))
            for x, y in _look_alike(conn, s['ledger']['id'], s['into']['id']):
                alike.append(dict(
                    firm_code=s['firm_code'], date=x['date'],
                    label=label(x['doc_kind'], co), gone_no=x['doc_no'],
                    kept_no=y['doc_no'], amount=r2(x['debit'] or x['credit']),
                    side='Dr' if x['debit'] else 'Cr'))
        # A balance SHREE carried from one name into the other is in both;
        # merged, it is counted once.
        full = dict(gone['_full'])
        full.update(kept['_full'])
        books = money_books(conn)
        once, out = 0.0, []
        for s in steps:
            st = _stand_in(conn, full[s['ledger']['id']],
                           full[s['into']['id']]) if s['how'] == 'move' else None
            if st and (s['ledger']['company_id'] in books
                       or not gone['is_party']):
                once += st[1]
            out.append(dict(how=s['how'], firm=s['firm'],
                            firm_code=s['firm_code'], name=s['ledger']['name'],
                            into=s['into']['name'],
                            entries=s['ledger']['entries'],
                            orders=s['ledger']['orders'],
                            balance=s['ledger']['balance'],
                            shree=s['ledger']['source'] == 'SHREE',
                            once=r2(st[1]) if st else 0.0,
                            once_date=st[0]['date'] if st else '',
                            once_from=st[2]['name'] if st else ''))
        bal = [s['balance'] for s in (gone, kept) if s['balance'] is not None]
        shared.sort(key=lambda e: e['date'])
        return dict(
            gone=gone['ledger_id'], kept=kept['ledger_id'], steps=out,
            between=len(shared),
            between_out=sum(1 for e in shared if e['out']),
            shared=shared[:8], alike=len(alike), alike_rows=alike[:8],
            balance=r2(sum(bal) - once) if bal else None,
            gstin_changes=bool(gone['gstins']) and bool(kept['gstins'])
            and gone['gstins'] != kept['gstins'])

    def clean(side):
        return {k: v for k, v in side.items() if not k.startswith('_')}
    # The one still in use is kept unless chosen otherwise: the name SHREE
    # and the bills are written under now.
    keep_b = (sb['last'] or '') >= (sa['last'] or '')
    return dict(a=clean(sa), b=clean(sb),
                a_into_b=way(sa, sb), b_into_a=way(sb, sa),
                keep=sb['ledger_id'] if keep_b else sa['ledger_id'])


def merge_accounts(conn, lid, into, backup=True):
    """Merge account `lid` into account `into`: one account, kept under the
    second one's name, holding every entry both had."""
    a = _ledger_for_delete(conn, lid)
    b = _ledger_for_delete(conn, qint(into, 'account'))
    sa, sb = _merge_side(conn, a), _merge_side(conn, b)
    steps = _merge_plan(conn, sa, sb)
    firm_ids = sorted({s['ledger']['company_id'] for s in steps})
    every = [x['id'] for x in sa['ledgers'] + sb['ledgers']]

    conn.commit()
    copy = backup_books(conn, MERGE_REASON) if backup else None
    try:
        before = _book_totals(conn, firm_ids)
        held = {}
        for x in sa['ledgers'] + sb['ledgers']:
            if x['company_id'] in firm_ids and x['id'] not in held:
                held[x['id']] = x
        want = {}
        for x in held.values():
            want[x['company_id']] = want.get(x['company_id'], 0.0) + x['balance']
        moved, counted_once, shared, shared_out = 0, 0.0, 0, 0
        for s in steps:
            old = one(conn.execute('SELECT * FROM ledger WHERE id=?',
                                   (s['ledger']['id'],)))
            if s['how'] == 'move':
                new = one(conn.execute('SELECT * FROM ledger WHERE id=?',
                                       (s['into']['id'],)))
                both = _shared_entries(conn, old['id'], new['id'])
                shared += len(both)
                shared_out += sum(1 for e in both if not e['others']
                                  and abs(e['net']) < 0.005)
                once = merge_ledger_into(conn, old, new)
                want[old['company_id']] = want.get(old['company_id'], 0.0) \
                    - once
                counted_once += once
            else:
                rename_for_merge(conn, old, s['into']['name'])
            moved += s['ledger']['entries']
            record_rule(conn, 'merge_ledger', old['company_id'],
                        old['name'].strip(), s['into']['name'].strip(),
                        'Merged into %s in this app'
                        % s['into']['name'].strip())
        gone = [s['ledger']['id'] for s in steps if s['how'] == 'move']
        if gone and conn.execute('SELECT COUNT(*) FROM ledger WHERE id IN (%s)'
                                 % _in(gone), gone).fetchone()[0]:
            raise ApiError('An account would still be there after the merge, '
                           'so nothing was merged.', 500)
        after = _book_totals(conn, firm_ids)
        off = {co: r2(after[co] - before[co]) for co in firm_ids}
        off = {co: v for co, v in off.items() if abs(v) > 0.05}
        if off:
            raise ApiError('Merging would have put the books out by %s, so '
                           'nothing was merged.' % ', '.join(
                               '%.2f' % v for v in off.values()), 500)
        # The two together, firm by firm, are exactly what is left.
        still = [i for i in every if one(conn.execute(
            'SELECT id FROM ledger WHERE id=?', (i,)))]
        got = {}
        for r in _each_chunk(
                conn, 'SELECT l.id, l.company_id, l.opening_bal + COALESCE(('
                      'SELECT SUM(debit - credit) FROM posting p '
                      'WHERE p.ledger_id=l.id), 0) AS bal FROM ledger l '
                      'WHERE l.id IN (%s)', still):
            if r['company_id'] in firm_ids:
                got[r['company_id']] = got.get(r['company_id'], 0.0) \
                    + float(r['bal'] or 0)
        for co in firm_ids:
            if abs(got.get(co, 0.0) - want.get(co, 0.0)) > 0.05:
                raise ApiError('The merged account would not have come to '
                               'what the two came to together, so nothing '
                               'was merged.', 500)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(ok=True, name=a['name'], into=b['name'], ledger_id=b['id'],
                entries=moved, firms=[s['firm'] for s in steps],
                counted_once=r2(counted_once), shared=shared,
                shared_out=shared_out,
                renamed=[s['firm'] for s in steps if s['how'] == 'rename'],
                backup=copy)


# ------------------------------------------------------ deleting in bulk
#
# Asked for on 17 Sep 2026: deleting one thing or many has to be easy. The
# lists tick rows and send them here together. Every row is deleted the way
# its own Delete button deletes it -- the same checks, the same bills taken
# along, the same record kept for the SHREE update -- inside one transaction:
# each row in a savepoint of its own, so one that cannot go is left out and
# the rest still go, and the whole lot is committed once. `check` does all of
# it and rolls it back, which is how the screen can say beforehand exactly
# what will happen.

BULK_MAX = 5000
BULK_KINDS = ('invoice', 'order', 'voucher', 'ledger', 'item', 'steel_entry',
              'stock_entry', 'personal', 'personal_entry')


class _OneTransaction(object):
    """The connection as the delete functions see it inside a bulk delete:
    their own commits and rollbacks are left to the bulk delete."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self):
        pass

    def rollback(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _bulk_row(conn, kind, rid):
    """(company id, label, amount, own number) for one ticked row, or None if
    it is gone. The own number is how the delete functions name it among
    what they took along."""
    if kind == 'invoice':
        r = one(conn.execute(
            'SELECT i.company_id, i.doc_type, i.series, i.invoice_no, '
            'i.grand_total, c.code, c.tax_mode, p.name party FROM invoice i '
            'JOIN company c ON c.id=i.company_id '
            'LEFT JOIN ledger p ON p.id=i.party_id WHERE i.id=?', (rid,)))
        if not r:
            return None
        what = ('Challan' if r['doc_type'] == 'SALE' and r['tax_mode'] == 'none'
                else DOC_LABEL.get(r['doc_type'], r['doc_type'].title()))
        return (r['company_id'], '%s %s (%s) %s' % (what, docno(r), r['code'],
                                                    r['party'] or ''),
                r2(r['grand_total']), docno(r))
    if kind == 'order':
        r = one(conn.execute(
            'SELECT o.company_id, o.order_no, o.grand_total, p.name party '
            'FROM sales_order o LEFT JOIN ledger p ON p.id=o.party_id '
            'WHERE o.id=?', (rid,)))
        return r and (r['company_id'], 'Order %s %s' % (r['order_no'],
                                                        r['party'] or ''),
                      r2(r['grand_total']), 'order %s' % r['order_no'])
    if kind == 'voucher':
        r = one(conn.execute(
            'SELECT v.company_id, v.kind, v.voucher_no, v.amount, c.code, '
            'p.name party FROM voucher v JOIN company c ON c.id=v.company_id '
            'LEFT JOIN ledger p ON p.id=v.party_id WHERE v.id=?', (rid,)))
        return r and (r['company_id'], '%s %s (%s) %s' % (
            KIND_LABEL.get(r['kind'], r['kind'].title()), r['voucher_no'],
            r['code'], r['party'] or ''), r2(r['amount']), r['voucher_no'])
    if kind == 'ledger':
        r = one(conn.execute(
            'SELECT l.company_id, l.name, c.code FROM ledger l '
            'JOIN company c ON c.id=l.company_id WHERE l.id=?', (rid,)))
        return r and (r['company_id'], '%s (%s)' % (r['name'], r['code']), 0.0,
                      r['name'])
    if kind == 'steel_entry':
        r = one(conn.execute(
            'SELECT e.company_id, e.kind, e.entry_date, e.party, '
            '  (SELECT COALESCE(SUM(weight), 0) FROM steel_entry_line '
            '   WHERE entry_id=e.id) kg FROM steel_entry e WHERE e.id=?', (rid,)))
        if not r:
            return None
        kg = ('%.3f' % float(r['kg'] or 0)).rstrip('0').rstrip('.')
        return (r['company_id'], '%s %s%s, %s kg' % (
            STEEL_ENTRY_KINDS.get(r['kind'], r['kind']),
            '-'.join(reversed((r['entry_date'] or '').split('-'))),
            ' ' + r['party'] if r['party'] else '', kg), 0.0,
            'stock entry %d' % rid)
    if kind == 'stock_entry':
        r = one(conn.execute(
            'SELECT e.company_id, e.kind, e.entry_date, e.reason, c.code, '
            '  (SELECT COALESCE(SUM(qty), 0) FROM stock_entry_line '
            '   WHERE entry_id=e.id) kg FROM stock_entry e '
            'JOIN company c ON c.id=e.company_id WHERE e.id=?', (rid,)))
        if not r:
            return None
        kg = ('%+.3f' % float(r['kg'] or 0)).rstrip('0').rstrip('.')
        return (r['company_id'], '%s %s (%s)%s, %s kg' % (
            STOCK_ENTRY_KINDS.get(r['kind'], r['kind']),
            '-'.join(reversed((r['entry_date'] or '').split('-'))), r['code'],
            ' ' + r['reason'] if r['reason'] else '', kg), 0.0,
            'stock entry %d' % rid)
    if kind == 'personal':
        # Somebody off Personal Accounts goes with every entry they have, so
        # the confirmation names how many and where they stood.
        r = one(conn.execute(
            'SELECT p.name, COUNT(e.id) n, COALESCE(SUM(CASE e.direction '
            "WHEN 'GIVEN' THEN e.amount ELSE -e.amount END), 0) bal "
            'FROM personal_person p LEFT JOIN personal_entry e '
            'ON e.person_id=p.id WHERE p.id=? GROUP BY p.id', (rid,)))
        if not r:
            return None
        return (None, '%s — %s%s' % (r['name'], _entries_text(r['n']),
                                      _personal_stands(r['bal'])),
                0.0, 'person %d' % rid)
    if kind == 'personal_entry':
        r = one(conn.execute(
            'SELECT e.*, p.name FROM personal_entry e '
            'JOIN personal_person p ON p.id=e.person_id WHERE e.id=?', (rid,)))
        if not r:
            return None
        return (None, '%s %s %s%s' % (
            '-'.join(reversed(r['entry_date'].split('-'))),
            'given to' if r['direction'] == 'GIVEN' else 'received from',
            r['name'], ' (%s)' % r['note'] if r['note'] else ''),
            r2(r['amount']), 'personal entry %d' % rid)
    r = one(conn.execute(
        'SELECT i.company_id, i.name, c.code FROM item i '
        'JOIN company c ON c.id=i.company_id WHERE i.id=?', (rid,)))
    return r and (r['company_id'], '%s (%s)' % (r['name'], r['code']), 0.0,
                  r['name'])


def _from_shree(conn, kind, rid):
    """True for a document or voucher that came in from SHREE."""
    table = {'invoice': 'invoice', 'voucher': 'voucher'}.get(kind)
    if not table:
        return False
    row = conn.execute('SELECT source FROM %s WHERE id=?' % table,
                       (rid,)).fetchone()
    return bool(row) and row[0] == 'SHREE'


def _bulk_one(tx, kind, cid, rid, cascade, keep=False):
    """Delete one row; answers what went with it."""
    if kind == 'invoice':
        return delete_invoice(tx, cid, rid, cascade, keep).get('deleted') or []
    if kind == 'order':
        return delete_order(tx, cid, rid, cascade, keep).get('deleted') or []
    if kind == 'voucher':
        delete_voucher(tx, cid, rid, keep)
        return []
    if kind == 'ledger':
        # A customer goes with its documents; any other account, ticked in
        # bulk, with its entries -- both only once the opt-in says so.
        r = delete_account(tx, rid, cascade, backup=False, entries=cascade)
        return ['%d document%s' % (r['documents'], '' if r['documents'] == 1
                                   else 's')] if r['documents'] else []
    if kind == 'steel_entry':
        delete_steel_entry(tx, cid, rid)
        return []
    if kind == 'stock_entry':
        delete_stock_entry(tx, rid, cid)
        return []
    if kind == 'personal':
        # Their entries go too; the row's own label already counts them.
        delete_personal_person(tx, rid)
        return []
    if kind == 'personal_entry':
        delete_personal_entry(tx, rid)
        return []
    # An item on bills leaves its lines behind, without an item, once asked.
    r = delete_item(tx, cid, rid, unlink=cascade, backup=False)
    return ['%d line%s' % (r['lines'], '' if r['lines'] == 1 else 's')] \
        if r.get('lines') else []


def bulk_delete(conn, data):
    """Delete many rows at once; with `check`, only say what would happen."""
    check = bool(data.get('check'))
    cascade = bool(data.get('cascade'))
    keep = bool(data.get('keep'))
    items = []
    ticked = data.get('items') or []
    if not isinstance(ticked, list):
        raise ApiError('Tick at least one row first.')
    for it in ticked:
        if not isinstance(it, dict):
            raise ApiError('Unknown kind of row.')
        kind = str(it.get('kind') or '')
        if kind not in BULK_KINDS:
            raise ApiError('Unknown kind of row: %s.' % kind)
        items.append((kind, qint(it.get('id'), kind)))
    if not items:
        raise ApiError('Tick at least one row first.')
    if len(items) > BULK_MAX:
        raise ApiError('%d rows at once is more than can be deleted together '
                       '(%d). Narrow the list first.' % (len(items), BULK_MAX))
    items = list(dict.fromkeys(items))       # the same row ticked twice

    conn.commit()
    backup = None
    if not check:
        # One copy of the books for the whole lot, taken before the first
        # write while no transaction is open.
        backup = backup_books(conn, 'bulk-delete')
    tx = _OneTransaction(conn)
    rows_out, gone_labels = [], set()
    firms = {r['id']: r['code'] for r in conn.execute(
        'SELECT id, code FROM company')}
    conn.execute('SAVEPOINT bulk_delete')
    try:
        # An entry whose other side is already gone -- most often with a bank
        # account deleted with its entries -- moves what a firm's trial
        # balance is out by when it goes. The screen says so beforehand.
        before = _book_totals(conn, list(firms))
        for n, (kind, rid) in enumerate(items):
            row = _bulk_row(conn, kind, rid)
            if row is None:
                # Already taken along by an earlier row (a bill with its
                # challan), or deleted meanwhile.
                rows_out.append(dict(kind=kind, id=rid, label='', amount=0.0,
                                     status='gone', why='', also=[]))
                continue
            cid, label, amount, own = row
            out = dict(kind=kind, id=rid, label=label, amount=amount,
                       status='ok', why='', also=[],
                       shree=_from_shree(conn, kind, rid))
            sp = 'bulk_%d' % n
            conn.execute('SAVEPOINT %s' % sp)
            try:
                try:
                    went = _bulk_one(tx, kind, cid, rid, False, keep)
                except ApiError as e:
                    if not (e.extra or {}).get('cascade'):
                        raise
                    # It takes other documents along. Said either way; done
                    # only when that was asked for.
                    conn.execute('ROLLBACK TO %s' % sp)
                    if not cascade:
                        out.update(status='needs', why=e.message,
                                   also=list((e.extra or {}).get('blocked_by')
                                             or []))
                        rows_out.append(out)
                        conn.execute('RELEASE %s' % sp)
                        continue
                    went = _bulk_one(tx, kind, cid, rid, True, keep)
                    out['status'] = 'with'
                out['also'] = [g for g in went if g != own and g not in gone_labels]
                gone_labels.update(went)
                conn.execute('RELEASE %s' % sp)
            except ApiError as e:
                conn.execute('ROLLBACK TO %s' % sp)
                conn.execute('RELEASE %s' % sp)
                out.update(status='blocked', why=e.message)
            rows_out.append(out)
        after = _book_totals(conn, list(firms))
        moved = [dict(firm=firms[co], before=r2(before[co]), after=r2(after[co]))
                 for co in firms if abs(after[co] - before[co]) > 0.05]
        if check:
            conn.execute('ROLLBACK TO bulk_delete')
        conn.execute('RELEASE bulk_delete')
        if check:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise

    done = [r for r in rows_out if r['status'] in ('ok', 'with')]
    by_kind = {}
    for r in done:
        by_kind[r['kind']] = by_kind.get(r['kind'], 0) + 1
    return dict(check=check, rows=rows_out, deleted=len(done),
                by_kind=by_kind, amount=r2(sum(r['amount'] for r in done)),
                also=sorted({a for r in done for a in r['also']}),
                needs=[r for r in rows_out if r['status'] == 'needs'],
                blocked=[r for r in rows_out if r['status'] == 'blocked'],
                moved=moved, backup=backup, keep=keep,
                from_shree=sum(1 for r in done if r.get('shree')))


# ------------------------------------------------- old years, deleted ---
#
# 5 Oct 2026: "Add option to delete account transactions year-wise, for all
# accounts, because the old data is of no use." Every document of the years
# chosen goes -- challans, bills, purchases, notes, receipts, payments,
# journals, contras -- with their entries. **What they added up to does
# not**: each account is given one "Balance brought forward" line on the last
# day deleted (31 March), so every balance, the trial balance, ageing and the
# stock of every item stand exactly where they stood. Dated 31 March and not
# 1 April because the Profit & Loss adds up whatever is dated inside its
# dates: on 1 April the old years' takings would have landed in the first
# year kept.
#
# Always from the oldest year up to the one chosen -- a year in the middle
# cannot go, or the years either side of it would not join -- and never the
# year we are in. The SHREE update would read the old years back in from
# SHREE's files, so the deletion is written down (`drop_years`) and played
# again after every update, like any other decision taken here.

YEAR_INV_KINDS = ('SALE', 'PURCHASE', 'CREDIT', 'DEBIT')
YEAR_VOU_KINDS = ('RECEIPT', 'PAYMENT', 'JOURNAL', 'CONTRA')
BF_NO = 'B/F'                      # doc_no of the brought-forward line


def _fy_label(start_year):
    return '%d-%02d' % (start_year, (start_year + 1) % 100)


def _fy_cut(fy):
    """'2022-23' -> '2023-04-01', the first day after the year."""
    m = re.match(r'^(\d{4})-(\d{2})$', str(fy or '').strip())
    if not m:
        raise ApiError('Choose a year, such as 2022-23.')
    start = int(m.group(1))
    if (start + 1) % 100 != int(m.group(2)):
        raise ApiError('%s is not a financial year.' % fy)
    return '%d-04-01' % (start + 1)


def _day_before(date):
    d = datetime.date.fromisoformat(date) - datetime.timedelta(days=1)
    return d.isoformat()


# A date's financial year, worked out in SQL: its starting year.
_FY_START_SQL = ("CAST(substr(%s,1,4) AS INTEGER) - "
                 "(CAST(substr(%s,6,2) AS INTEGER) < 4)")


def book_years(conn, q=None):
    """Each firm's years, oldest first, and what each holds -- for the screen
    that deletes them. Years already deleted show what is left of them: the
    brought-forward lines."""
    firms = [dict(id=r['id'], code=r['code'], name=r['name'])
             for r in conn.execute('SELECT id, code, name FROM company '
                                   'WHERE active=1 ORDER BY id')]
    current = db.fy_of(_today())
    years = {}

    def add(code, start, key, n):
        if start is None:
            return
        fy = _fy_label(int(start))
        y = years.setdefault(fy, {})
        y.setdefault(code, dict(documents=0, entries=0, lines=0))[key] += n

    code_of = {f['id']: f['code'] for f in firms}
    for r in conn.execute(
            'SELECT company_id, %s y, COUNT(*) n FROM invoice GROUP BY 1, 2'
            % (_FY_START_SQL % ('invoice_date', 'invoice_date'))):
        if r['company_id'] in code_of:
            add(code_of[r['company_id']], r['y'], 'documents', r['n'])
    for r in conn.execute(
            'SELECT company_id, %s y, COUNT(*) n FROM voucher GROUP BY 1, 2'
            % (_FY_START_SQL % ('voucher_date', 'voucher_date'))):
        if r['company_id'] in code_of:
            add(code_of[r['company_id']], r['y'], 'entries', r['n'])
    for r in conn.execute(
            'SELECT company_id, %s y, COUNT(*) n FROM posting GROUP BY 1, 2'
            % (_FY_START_SQL % ('date', 'date'))):
        if r['company_id'] in code_of:
            add(code_of[r['company_id']], r['y'], 'lines', r['n'])
    gone = {}
    for r in conn.execute("SELECT firm, name FROM sync_rule "
                          "WHERE kind='drop_years'"):
        if r['name'] > gone.get(r['firm'], ''):
            gone[r['firm']] = r['name']
    out = []
    for fy in sorted(years):
        out.append(dict(fy=fy, deletable=fy < current, firms=years[fy],
                        deleted=[c for c, cut in gone.items()
                                 if _fy_cut(fy) <= cut]))
    return dict(current=current, firms=firms, years=out,
                deleted={c: _day_before(cut) for c, cut in gone.items()})


def _drop_before(conn, cid, cut, extra=None):
    """Take out of firm `cid` every document dated before `cut`, carrying
    what they added up to onto each account as one line the day before.

    `extra` is what was written down when the deletion was first made: the
    accounts and items moved by documents made in this app, which an update
    cannot bring back (by name -- ids change). It is added only when this
    firm's brought-forward lines are not already there, that is, when the
    update has just read the old years in again."""
    eve = _day_before(cut)
    inv_k, vou_k = YEAR_INV_KINDS, YEAR_VOU_KINDS
    ik = ','.join('?' * len(inv_k))
    vk = ','.join('?' * len(vou_k))
    for t in ('_yr_inv', '_yr_vou'):
        conn.execute('CREATE TEMP TABLE IF NOT EXISTS %s(id INTEGER PRIMARY KEY)' % t)
        conn.execute('DELETE FROM %s' % t)
    conn.execute('INSERT INTO _yr_inv(id) SELECT id FROM invoice '
                 'WHERE company_id=? AND invoice_date<?', (cid, cut))
    conn.execute('INSERT INTO _yr_vou(id) SELECT id FROM voucher '
                 'WHERE company_id=? AND voucher_date<?', (cid, cut))
    n_inv = conn.execute('SELECT COUNT(*) FROM _yr_inv').fetchone()[0]
    n_vou = conn.execute('SELECT COUNT(*) FROM _yr_vou').fetchone()[0]
    # Who was sold goods in the years going: a challan is what makes an
    # account in the accounts book a customer (`_trade_keys`), and with the
    # challans gone a customer still owing from those years would drop off
    # Who owes us and the customer list.
    sold = sorted({r['name'] for r in conn.execute(
        "SELECT DISTINCT l.name FROM invoice i JOIN ledger l ON l.id=i.party_id "
        "WHERE i.id IN (SELECT id FROM _yr_inv) AND i.doc_type IN "
        "('SALE','DEBIT')")})
    had_bf = conn.execute(
        "SELECT 1 FROM posting WHERE company_id=? AND doc_kind='OPENING' "
        "AND doc_no=? AND date=? LIMIT 1", (cid, BF_NO, eve)).fetchone()

    # The entries going: every entry of a document going, whatever its own
    # date, and every other entry dated before the cut -- the openings SHREE
    # gave each year -- but never one of a document that stays.
    go_sql = ('company_id=? AND ((doc_kind IN (%s) AND doc_id IN '
              '(SELECT id FROM _yr_inv)) OR (doc_kind IN (%s) AND doc_id IN '
              '(SELECT id FROM _yr_vou)) OR (date<? AND NOT ('
              '(doc_kind IN (%s) AND doc_id IN (SELECT id FROM invoice WHERE '
              'company_id=? AND invoice_date>=?)) OR (doc_kind IN (%s) AND '
              'doc_id IN (SELECT id FROM voucher WHERE company_id=? AND '
              'voucher_date>=?)))))' % (ik, vk, ik, vk))
    args = ((cid,) + inv_k + vou_k + (cut,) + inv_k + (cid, cut) + vou_k
            + (cid, cut))
    carry = collections.defaultdict(float)
    n_post = 0
    for r in conn.execute('SELECT ledger_id, SUM(debit-credit) net, COUNT(*) n '
                          'FROM posting WHERE ' + go_sql + ' GROUP BY ledger_id',
                          args):
        carry[r['ledger_id']] += float(r['net'] or 0)
        n_post += r['n']
    # The opening balance an account was given when the books began goes
    # into the same line. Left on the account undated, a debt from before the
    # first year kept read as owed since today -- "not due yet" on Who owes us,
    # its challans being gone.
    for r in conn.execute('SELECT id, opening_bal FROM ledger WHERE '
                          'company_id=? AND ABS(opening_bal)>0.004', (cid,)):
        carry[r['id']] += float(r['opening_bal'] or 0)
    conn.execute('UPDATE ledger SET opening_bal=0 WHERE company_id=? AND '
                 'ABS(opening_bal)>0.004', (cid,))

    # What this app's own documents among them came to, for the decision.
    own, own_stock = collections.defaultdict(float), collections.defaultdict(float)
    for r in conn.execute(
            'SELECT l.name, SUM(p.debit-p.credit) net FROM posting p '
            'JOIN ledger l ON l.id=p.ledger_id WHERE p.company_id=? AND ('
            "(p.doc_kind IN (%s) AND p.doc_id IN (SELECT id FROM invoice WHERE "
            "id IN (SELECT id FROM _yr_inv) AND COALESCE(source,'')<>'SHREE')) "
            "OR (p.doc_kind IN (%s) AND p.doc_id IN (SELECT id FROM voucher "
            "WHERE id IN (SELECT id FROM _yr_vou) AND "
            "COALESCE(source,'')<>'SHREE'))) GROUP BY l.name" % (ik, vk),
            (cid,) + inv_k + vou_k):
        if abs(float(r['net'] or 0)) >= 0.005:
            own[r['name']] += float(r['net'])

    # Stock: each item's opening takes up what the documents going moved,
    # so its stock stays where it is.
    moved = collections.defaultdict(float)
    for r in conn.execute(
            'SELECT ii.item_id, it.name, i.doc_type, i.source, SUM(' + STOCK_QTY
            + ') q FROM invoice_item ii JOIN invoice i ON i.id=ii.invoice_id '
            'JOIN item it ON it.id=ii.item_id WHERE i.id IN (SELECT id FROM '
            '_yr_inv) GROUP BY ii.item_id, i.doc_type, i.source'):
        q = DOC_STOCK.get(r['doc_type'], 0) * float(r['q'] or 0)
        moved[r['item_id']] += q
        if (r['source'] or '') != 'SHREE':
            own_stock[r['name']] += q

    if extra and not had_bf:
        names = collections.defaultdict(list)
        for r in conn.execute('SELECT id, name FROM ledger WHERE company_id=?',
                              (cid,)):
            names[' '.join((r['name'] or '').upper().split())].append(r['id'])
        for name, amount in (extra.get('own') or {}).items():
            ids = names.get(' '.join(str(name).upper().split()))
            if ids:
                carry[ids[0]] += float(amount or 0)
        items = {}
        for r in conn.execute('SELECT id, name FROM item WHERE company_id=?',
                              (cid,)):
            items.setdefault(' '.join((r['name'] or '').upper().split()), r['id'])
        for name, q in (extra.get('stock') or {}).items():
            iid = items.get(' '.join(str(name).upper().split()))
            if iid:
                moved[iid] += float(q or 0)

    # Links from what stays to what goes are let go: a bill kept whose
    # challan went, an order whose challan went, a bill's GST journal.
    conn.execute('UPDATE invoice SET from_challan=NULL WHERE from_challan IN '
                 '(SELECT id FROM _yr_inv)')
    conn.execute('UPDATE invoice SET billed_invoice=NULL, billed_company=NULL '
                 'WHERE billed_invoice IN (SELECT id FROM _yr_inv)')
    conn.execute('UPDATE sales_order SET billed_invoice=NULL, '
                 'billed_company=NULL WHERE billed_invoice IN '
                 '(SELECT id FROM _yr_inv)')
    conn.execute('UPDATE invoice SET gst_voucher=NULL WHERE gst_voucher IN '
                 '(SELECT id FROM _yr_vou)')
    conn.execute('DELETE FROM challan_bill WHERE challan_id IN (SELECT id FROM '
                 '_yr_inv) OR bill_id IN (SELECT id FROM _yr_inv)')

    conn.execute('DELETE FROM posting WHERE ' + go_sql, args)
    conn.execute('DELETE FROM invoice_item WHERE invoice_id IN '
                 '(SELECT id FROM _yr_inv)')
    conn.execute('DELETE FROM doc_history WHERE invoice_id IN '
                 '(SELECT id FROM _yr_inv)')
    conn.execute('DELETE FROM invoice WHERE id IN (SELECT id FROM _yr_inv)')
    conn.execute('DELETE FROM voucher_line WHERE voucher_id IN '
                 '(SELECT id FROM _yr_vou)')
    conn.execute('DELETE FROM voucher WHERE id IN (SELECT id FROM _yr_vou)')

    said = '-'.join(reversed(eve.split('-')))
    carried = 0
    for lid, net in carry.items():
        net = r2(net)
        if abs(net) < 0.005:
            continue
        conn.execute(
            'INSERT INTO posting(company_id, date, ledger_id, doc_kind, doc_id, '
            'doc_no, debit, credit, narration) VALUES (?,?,?,?,0,?,?,?,?)',
            (cid, eve, lid, 'OPENING', BF_NO, net if net > 0 else 0.0,
             -net if net < 0 else 0.0,
             'Balance brought forward — the entries up to %s were deleted'
             % said))
        carried += 1
    for iid, q in moved.items():
        if abs(q) > 1e-9:
            conn.execute('UPDATE item SET opening_qty=ROUND(opening_qty+?, 3) '
                         'WHERE id=?', (q, iid))
    return dict(documents=n_inv, entries=n_vou, lines=n_post, carried=carried,
                sold=sold,
                own={k: r2(v) for k, v in own.items() if abs(v) >= 0.005},
                stock={k: round(v, 3) for k, v in own_stock.items()
                       if abs(v) > 1e-9})


def drop_years(conn, data):
    """Delete the years up to and including `upto` in the firms named (all
    of them when none are); with `check`, only count what would go."""
    data = data or {}
    upto = str(data.get('upto') or '').strip()
    cut = _fy_cut(upto)
    current = db.fy_of(_today())
    if upto >= current:
        raise ApiError('%s is the year we are in; only the years before it '
                       'can be deleted.' % upto)
    want = data.get('firms') or []
    if not isinstance(want, list):
        raise ApiError('Choose the firms.')
    firms = [dict(r) for r in conn.execute(
        'SELECT id, code, name FROM company WHERE active=1 ORDER BY id')]
    if want:
        firms = [f for f in firms if f['code'] in want]
        if not firms:
            raise ApiError('None of those firms is in the books.')
    check = bool(data.get('check'))
    eve = _day_before(cut)

    if check:
        out = []
        for f in firms:
            n_inv = conn.execute('SELECT COUNT(*) FROM invoice WHERE '
                                 'company_id=? AND invoice_date<?',
                                 (f['id'], cut)).fetchone()[0]
            n_vou = conn.execute('SELECT COUNT(*) FROM voucher WHERE '
                                 'company_id=? AND voucher_date<?',
                                 (f['id'], cut)).fetchone()[0]
            own = conn.execute(
                "SELECT (SELECT COUNT(*) FROM invoice WHERE company_id=? AND "
                "invoice_date<? AND COALESCE(source,'')<>'SHREE') + (SELECT "
                "COUNT(*) FROM voucher WHERE company_id=? AND voucher_date<? "
                "AND COALESCE(source,'')<>'SHREE')",
                (f['id'], cut, f['id'], cut)).fetchone()[0]
            out.append(dict(firm=f['code'], name=f['name'], documents=n_inv,
                            entries=n_vou, own=own))
        return dict(check=True, upto=upto, last_day=eve, firms=out,
                    documents=sum(x['documents'] for x in out),
                    entries=sum(x['entries'] for x in out))

    conn.commit()
    backup = backup_books(conn, 'drop-years')
    done = []
    try:
        cids = [f['id'] for f in firms]
        before = _book_totals(conn, cids)
        for f in firms:
            got = _drop_before(conn, f['id'], cut)
            # Written down for the SHREE update, which would read the years
            # back in: one decision per firm, the latest date, and what this
            # app's own documents added up to so far.
            old = conn.execute("SELECT name, value FROM sync_rule WHERE "
                               "kind='drop_years' AND firm=?",
                               (f['code'],)).fetchall()
            own, stock, keep = (collections.defaultdict(float),
                                collections.defaultdict(float), cut)
            sold = set(got['sold'])
            for r in old:
                keep = max(keep, r['name'])
                try:
                    v = json.loads(r['value'] or '{}')
                except ValueError:
                    v = {}
                sold.update(v.get('sold') or [])
                for k, x in (v.get('own') or {}).items():
                    own[k] += float(x or 0)
                for k, x in (v.get('stock') or {}).items():
                    stock[k] += float(x or 0)
            for k, x in got['own'].items():
                own[k] += x
            for k, x in got['stock'].items():
                stock[k] += x
            if RECORD_RULES:
                conn.execute("DELETE FROM sync_rule WHERE kind='drop_years' "
                             "AND firm=?", (f['code'],))
                conn.execute(
                    'INSERT INTO sync_rule(kind, firm, name, value, note) '
                    'VALUES (?,?,?,?,?)',
                    ('drop_years', f['code'], keep, json.dumps(dict(
                        sold=sorted(sold),
                        own={k: r2(v) for k, v in own.items() if abs(v) >= 0.005},
                        stock={k: round(v, 3) for k, v in stock.items()
                               if abs(v) > 1e-9}), sort_keys=True),
                     'every entry before %s deleted here'
                     % '-'.join(reversed(keep.split('-')))))
            done.append(dict(firm=f['code'], name=f['name'],
                             documents=got['documents'], entries=got['entries'],
                             lines=got['lines'], carried=got['carried']))
        after = _book_totals(conn, cids)
        moved = [f['code'] for f in firms
                 if abs(after[f['id']] - before[f['id']]) > 0.05]
        if moved:
            raise ApiError('Nothing was deleted: the books of %s would not '
                           'have stayed balanced.' % ', '.join(moved))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(ok=True, upto=upto, last_day=eve, firms=done, backup=backup,
                documents=sum(x['documents'] for x in done),
                entries=sum(x['entries'] for x in done))


# ------------------------------------------------------------------- items

ITEM_FIELDS = ['code', 'name', 'category', 'hsn', 'unit', 'gst_rate',
               'cess_rate', 'sale_rate', 'purchase_rate', 'opening_qty',
               'reorder_level', 'track_stock', 'by_weight', 'active']
ITEM_NUM = {'gst_rate', 'cess_rate', 'sale_rate', 'purchase_rate',
            'opening_qty', 'reorder_level', 'track_stock', 'by_weight',
            'active'}

# Units that mean the line is sold by weight.
WEIGHT_UNITS = {'', 'KG', 'KGS', 'MT', 'MTS', 'QTL', 'TON', 'TONNE'}


def list_items(conn, cid, q):
    sql = 'SELECT * FROM item WHERE company_id=?'
    args = [cid]
    if q.get('search'):
        where, wargs = contains(['name', 'code', 'hsn', 'category', 'stock_cat'],
                                q['search'])
        sql += ' AND ' + where
        args += wargs
    if q.get('active_only'):
        sql += ' AND active=1'
    sql += ' ORDER BY name'
    data = rows(conn.execute(sql, args))
    inward = set()
    st = stock_map(conn, cid, inward)
    for it in data:
        it['stock'] = r3(st.get(it['id'], 0))
        # Whether this firm has ever bought the thing. The entry screens show
        # what a line leaves in stock, and "nothing left" and "never stocked
        # here" are not the same warning.
        it['stocked'] = bool(it['track_stock'] and it['id'] in inward)
        it['low'] = bool(it['track_stock'] and it['reorder_level'] > 0
                         and it['stock'] <= it['reorder_level'])
    return data


def save_item(conn, cid, iid, data):
    data = dict(data, name=check_name(data, 'Item'))
    data = dict(data)
    # The Billing Stock category is set only when the form sends it, so a
    # screen without the box leaves it as it was; a new product sent
    # without one is given the likeliest.
    cat = (_stock_cat_name(conn, data.get('stock_cat'))
           if 'stock_cat' in data else None)
    if not (data.get('code') or '').strip():
        data['code'] = None
    if not (data.get('unit') or '').strip():
        data['unit'] = 'KGS'
    if 'by_weight' not in data or data['by_weight'] in ('', None):
        data['by_weight'] = 1 if data['unit'].strip().upper() in WEIGHT_UNITS else 0
    vals = _coerce(_apply_defaults(data), ITEM_FIELDS, ITEM_NUM)
    before = one(conn.execute('SELECT * FROM item WHERE id=? AND company_id=?',
                              (iid, cid))) if iid else None
    try:
        if iid:
            sets = ', '.join('%s=?' % f for f in ITEM_FIELDS)
            conn.execute('UPDATE item SET %s WHERE id=? AND company_id=?' % sets,
                         vals + [iid, cid])
            # An imported item edited here -- its HSN, most often -- is this
            # app's own from now on, so a SHREE update fills it but never
            # replaces it.
            after = one(conn.execute('SELECT * FROM item WHERE id=?', (iid,)))
            if before and before.get('source') == 'SHREE' and any(
                    (before.get(f) or '') != (after.get(f) or '')
                    for f in ITEM_FIELDS):
                conn.execute("UPDATE item SET source='' WHERE id=?", (iid,))
        else:
            cols = ','.join(['company_id'] + ITEM_FIELDS)
            ph = ','.join('?' * (len(ITEM_FIELDS) + 1))
            cur = conn.execute('INSERT INTO item(%s) VALUES (%s)' % (cols, ph),
                               [cid] + vals)
            iid = cur.lastrowid
            if cat is None:
                cat = db.guess_stock_cat(data['name'], data.get('hsn'),
                                         db.stock_cats(conn))
        # Not one of ITEM_FIELDS: putting a SHREE product in a category is
        # not editing it, and it stays SHREE's to update.
        if cat is not None:
            conn.execute('UPDATE item SET stock_cat=? WHERE id=?', (cat, iid))
    except sqlite3.IntegrityError:
        raise ApiError('Item code "%s" is already used.' % data.get('code'))
    conn.commit()
    return one(conn.execute('SELECT * FROM item WHERE id=?', (iid,)))


def item_delete_check(conn, cid, iid):
    """What deleting an item touches -- for the screen to offer both ways."""
    it = one(conn.execute('SELECT * FROM item WHERE id=? AND company_id=?',
                          (iid, cid)))
    if not it:
        raise ApiError('Item not found.', 404)
    n = conn.execute('SELECT COUNT(*) n, COUNT(DISTINCT invoice_id) d '
                     'FROM invoice_item WHERE item_id=?', (iid,)).fetchone()
    o = conn.execute('SELECT COUNT(*) FROM sales_order_item WHERE item_id=?',
                     (iid,)).fetchone()[0]
    stock = stock_map(conn, cid).get(iid) if it['track_stock'] else None
    hand = conn.execute('SELECT COUNT(*) FROM stock_entry_line WHERE item_id=?',
                        (iid,)).fetchone()[0]
    return dict(id=iid, name=it['name'], unit=it['unit'], hsn=it['hsn'] or '',
                lines=n['n'] + o, documents=n['d'], order_lines=o,
                stock_lines=hand,
                stock=None if stock is None else r3(stock),
                from_shree=it['source'] == 'SHREE')


def delete_item(conn, cid, iid, move_to=None, unlink=False, backup=True):
    """Delete an item. One on bills and challans goes the way asked
    (26 Sep 2026: "offer both each time"): its lines moved onto `move_to`,
    whose stock takes them -- a duplicate into the right one -- or, with
    `unlink`, left on their documents without an item, every figure kept,
    out of stock. It used to be refused, 195 of 224 items with it."""
    it = one(conn.execute('SELECT * FROM item WHERE id=? AND company_id=?',
                          (iid, cid)))
    if not it:
        raise ApiError('Item not found.', 404)
    used = conn.execute(
        'SELECT (SELECT COUNT(*) FROM invoice_item WHERE item_id=?) '
        '     + (SELECT COUNT(*) FROM sales_order_item WHERE item_id=?) AS n',
        (iid, iid)).fetchone()['n']
    target = None
    if move_to:
        target = one(conn.execute('SELECT * FROM item WHERE id=? AND '
                                  'company_id=?', (qint(move_to, 'item'), cid)))
        if not target:
            raise ApiError('The item to move the lines to is not in this firm.')
        if target['id'] == iid:
            raise ApiError('Pick another item to move the lines to.')
    if used and not target and not unlink:
        raise ApiError('"%s" is on %d bill or challan line%s. Choose where '
                       '%s: onto another item, or kept without an item.'
                       % (it['name'], used, '' if used == 1 else 's',
                          'it goes' if used == 1 else 'they go'),
                       409, {'cascade': True, 'choose': True, 'lines': used})
    conn.commit()
    copy = backup_books(conn, 'delete') if (used and backup) else None
    try:
        for table in ('invoice_item', 'sales_order_item'):
            conn.execute('UPDATE %s SET item_id=? WHERE item_id=?' % table,
                         (target['id'] if target else None, iid))
        if target:
            # Its opening stock goes across with its lines, so what the two
            # held adds up on the one that stays.
            conn.execute('UPDATE item SET opening_qty=ROUND(opening_qty+?, 3) '
                         'WHERE id=?', (float(it['opening_qty'] or 0),
                                        target['id']))
        # Stock added or removed by hand goes the same way; with no item to
        # go to, it is that item's stock and goes with it.
        drop_hand_lines(conn, iid, target['id'] if target else None)
        conn.execute('DELETE FROM item WHERE id=? AND company_id=?', (iid, cid))
        # SHREE brings its items back at every update; this is done again.
        if it['source'] == 'SHREE':
            if target:
                record_rule(conn, 'fold_item', cid, it['name'].strip(),
                            target['name'].strip(), 'Lines moved onto %s and '
                            'deleted here' % target['name'].strip())
            else:
                record_rule(conn, 'delete_item', cid, it['name'].strip(), '',
                            'Deleted in this app')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(ok=True, name=it['name'], lines=used,
                moved_to=target['name'] if target else '', backup=copy)


# ------------------------------------------------------------ computation

def compute_lines(lines, is_igst, tax_mode='gst'):
    """Recompute every line total server-side; never trust the client.

    A line is priced on its weight when one is given, otherwise on quantity.
    Filling in `incl_price` means "this is the agreed price with tax in it" --
    the tax is stripped back out and divided by the weight to get the per-kg
    rate that goes on the bill.
    """
    taxed = tax_mode != 'none'
    out = []
    tot = dict(subtotal=0.0, discount=0.0, taxable=0.0, cgst=0.0, sgst=0.0,
               igst=0.0, cess=0.0, weight=0.0, units=0.0)
    for ln in lines or []:
        desc = (ln.get('description') or '').strip()
        qty = float(ln.get('qty') or 0)
        weight = float(ln.get('weight') or 0)
        rate = float(ln.get('rate') or 0)
        incl = float(ln.get('incl_price') or 0)
        if not desc and not qty and not weight:
            continue
        disc_pct = float(ln.get('discount_pct') or 0)
        gst = float(ln.get('gst_rate') or 0) if taxed else 0.0
        cess_rate = float(ln.get('cess_rate') or 0) if taxed else 0.0

        # The rate is per kilo, so weight is what prices the line -- the
        # quantity column is the piece count and never multiplies the rate.
        by_weight = 1 if ln.get('by_weight', 1) in (1, '1', True, None) else 0
        # Kilos x rate to the paisa, half going up -- SHREE's way, in gst.py.
        # A price with the tax in it has the tax backed out and the rate
        # rounded to the paisa first, so rate x weight is the amount printed.
        rate, gross, disc, taxable = gstcalc.line_amount(dict(
            ln, weight=weight, qty=qty, rate=rate, incl_price=incl,
            by_weight=by_weight, gst_rate=gst, cess_rate=cess_rate,
            discount_pct=disc_pct), taxed)
        # The line's own tax, until the bill is worked out as a whole.
        cgst, sgst, igst, cess = gstcalc.tax_on(taxable, gst, cess_rate,
                                                bool(is_igst))

        out.append(dict(
            item_id=ln.get('item_id') or None, charge_share=0.0,
            description=desc, hsn=(ln.get('hsn') or '').strip(),
            item_detail=(ln.get('item_detail') or '').strip(),
            thickness=(ln.get('thickness') or '').strip(),
            cutting_size=(ln.get('cutting_size') or '').strip(),
            steel_type=steel_word(ln.get('steel_type')),
            steel_quality=steel_word(ln.get('steel_quality')),
            steel_thick=steel_word(ln.get('steel_thick')),
            unit=(ln.get('unit') or '').strip(), qty=r3(qty), weight=r3(weight),
            by_weight=by_weight, incl_price=r2(incl),
            rate=round(rate + 1e-9, 3),
            discount_pct=r2(disc_pct), taxable=taxable, gst_rate=gst,
            cgst=cgst, sgst=sgst, igst=igst, cess_rate=cess_rate, cess=cess,
            line_total=gstcalc.money(taxable + cgst + sgst + igst + cess)))
        tot['subtotal'] += gross
        tot['discount'] += disc
        tot['taxable'] += taxable
        tot['cgst'] += cgst
        tot['sgst'] += sgst
        tot['igst'] += igst
        tot['cess'] += cess
        tot['weight'] += weight
        tot['units'] += qty
    for k in tot:
        tot[k] = r3(tot[k]) if k in ('weight', 'units') else gstcalc.money(tot[k])
    return out, tot


def state_of(state_code='', gstin='', state_name=''):
    """A state's two-digit code: from the GSTIN when there is one -- its first
    two digits are the state, and it is what the portal checks -- then the
    state code typed, then the state's name. '' when none says."""
    g = re.sub(r'\s', '', str(gstin or '')).upper()
    if len(g) == 15 and g[:2].isdigit() and g[:2] != '00':
        return g[:2]
    d = re.sub(r'\D', '', str(state_code or ''))
    if d and int(d):
        return '%02d' % int(d)
    want = str(state_name or '').strip().lower()
    for code, name in STATES:
        if want and name.lower() == want:
            return code
    return ''


def resolve_igst(conn, cid, party_id, override=None, supply_state=''):
    """Inter-state supply => IGST. Intra-state => CGST + SGST.

    The firm's state against where the goods are supplied: the bill's place
    of supply when one is given, otherwise the customer's state -- each read
    off its GSTIN first, as the e-invoice reads them, so a customer entered
    with a GSTIN but no state code is no longer taken for a local one.
    """
    comp = get_company(conn, cid)
    if comp.get('tax_mode') == 'none':
        return 0
    if override is not None and override != '':
        return 1 if int(override) else 0
    party = one(conn.execute('SELECT * FROM ledger WHERE id=? AND company_id=?',
                             (party_id, cid)))
    if not party:
        raise ApiError('Party not found in this company.', 404)
    home = state_of(comp.get('state_code'), comp.get('gstin'), comp.get('state'))
    away = state_of('', '', supply_state) or state_of(
        party.get('state_code'), party.get('gstin'), party.get('state'))
    if not home or not away:
        return 0
    return 1 if home != away else 0


def tax_mode(conn, cid):
    return get_company(conn, cid).get('tax_mode') or 'gst'


# --------------------------------------------------------- posting engine

def repost(conn, cid, doc_kind, doc_id, date, doc_no, entries, narration=''):
    """Replace this document's double-entry rows. entries = [(ledger, dr, cr)]."""
    conn.execute('DELETE FROM posting WHERE doc_kind=? AND doc_id=?',
                 (doc_kind, doc_id))
    dr_total = cr_total = 0.0
    for ledger_id, dr, cr in entries:
        dr, cr = r2(dr), r2(cr)
        if not dr and not cr:
            continue
        dr_total += dr
        cr_total += cr
        conn.execute(
            'INSERT INTO posting(company_id, date, ledger_id, doc_kind, '
            'doc_id, doc_no, debit, credit, narration) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (cid, date, ledger_id, doc_kind, doc_id, doc_no, dr, cr, narration))
    if abs(r2(dr_total) - r2(cr_total)) > 0.05:
        raise ApiError('Internal posting is unbalanced (Dr %.2f vs Cr %.2f). '
                       'The document was not saved.' % (dr_total, cr_total), 500)


def invoice_entries(conn, cid, inv):
    """Double-entry rows for one invoice, following normal GST treatment."""
    S = lambda k: sys_ledger(conn, cid, k)
    doc = inv['doc_type']
    tax = [(inv['cgst'], 'CGST'), (inv['sgst'], 'SGST'),
           (inv['igst'], 'IGST'), (inv['cess'], 'CESS')]
    # Cutting / Transport / Loading, exactly as the entry screen names them.
    charges = [(S('CUTTING'), inv['cutting']), (S('TRANSPORT'), inv['transport']),
               (S('LOADING'), inv['loading'])]
    # The "Sales A/c" box lets the user post to a specific revenue ledger.
    trade = inv.get('sales_ac_id') or None
    # `taxable` now includes the charges, but those are posted to their own
    # ledgers below -- the sales/purchase account only takes the goods.
    goods = inv.get('goods')
    if goods is None:
        goods = r2(inv['taxable'] - inv['cutting'] - inv['transport']
                   - inv['loading'] - inv.get('taxable_adj', 0))
    e = []

    adj = inv.get('taxable_adj', 0)
    if doc == 'SALE':
        e.append((inv['party_id'], inv['party_amount'], 0))
        e.append((trade or S('SALES'), 0, goods))
        for amt, name in tax:
            e.append((S('OUT_' + name), 0, amt))
        for led, amt in charges:
            e.append((led, 0, amt))
        rounding = r2(inv['round_off'] + adj)
        e.append((S('ROUNDOFF'), max(0, -rounding), max(0, rounding)))

    elif doc == 'CREDIT':          # sales return
        e.append((inv['party_id'], 0, inv['party_amount']))
        e.append((trade or S('SALES_RETURN'), goods, 0))
        for amt, name in tax:
            e.append((S('OUT_' + name), amt, 0))
        for led, amt in charges:
            e.append((led, amt, 0))
        rounding = r2(inv['round_off'] + adj)
        e.append((S('ROUNDOFF'), max(0, rounding), max(0, -rounding)))

    elif doc == 'PURCHASE':
        e.append((inv['party_id'], 0, inv['party_amount']))
        e.append((trade or S('PURCHASE'), goods, 0))
        for led, amt in charges:
            e.append((led, amt, 0))
        # TCS the supplier charged: owed to them, and ours to claim back.
        if r2(inv.get('tcs') or 0):
            e.append((tcs_ledger(conn, cid), r2(inv['tcs']), 0))
        rounding = r2(inv['round_off'] + adj)
        e.append((S('ROUNDOFF'), max(0, rounding), max(0, -rounding)))
        for amt, name in tax:
            # Input credit is always claimed; under reverse charge the same
            # amount is also raised as an output liability, so the supplier is
            # billed net of tax.
            e.append((S('IN_' + name), amt, 0))
            if inv['rcm']:
                e.append((S('OUT_' + name), 0, amt))

    elif doc == 'DEBIT':           # purchase return
        e.append((inv['party_id'], inv['party_amount'], 0))
        e.append((trade or S('PURCHASE_RETURN'), 0, goods))
        for led, amt in charges:
            e.append((led, 0, amt))
        rounding = r2(inv['round_off'] + adj)
        e.append((S('ROUNDOFF'), max(0, -rounding), max(0, rounding)))
        for amt, name in tax:
            e.append((S('IN_' + name), 0, amt))
            if inv['rcm']:
                e.append((S('OUT_' + name), amt, 0))
    return e


def tcs_ledger(conn, cid):
    """The account a purchase's TCS is posted to: the firm's own TCS A/C
    PURCHASE, as SHREE named it in both GST firms (8 Oct 2026), found by name
    so an update from SHREE and the app keep one account between them. A
    firm without one gets it under Duties & Taxes."""
    r = conn.execute(
        "SELECT id FROM ledger WHERE company_id=? AND "
        "UPPER(REPLACE(name, ' ', ''))='TCSA/CPURCHASE' ORDER BY id LIMIT 1",
        (cid,)).fetchone()
    if r:
        return r['id']
    g = conn.execute("SELECT id FROM account_group WHERE company_id=? AND "
                     "code='3201'", (cid,)).fetchone()
    if not g:
        raise ApiError('There is no Duties & Taxes group to keep TCS in.', 500)
    return conn.execute('INSERT INTO ledger(company_id, group_id, name) '
                        "VALUES (?,?,'TCS A/C PURCHASE')",
                        (cid, g['id'])).lastrowid


# --------------------------------------------------------------- invoices

def last_rates(conn, cid, party_id, doc_type='SALE'):
    """The rates per kilo on this party's last challan, for a new one.

    Asked on 23 Sep 2026: "whenever I make a new challan for any existing
    customer, the price per kilo in the last challan should automatically be
    fetched and autofilled." The last one is the latest by date that has a
    line priced by the kilo -- a challan of nothing but "NILL" lines at 0 is
    stepped over, as it says nothing about the price.

    Every priced line comes back with what it says about itself (the item
    detail, which starts with the thickness -- "0.60 CRCA 80 MM PATO") and
    its kilos: a challan can carry two prices, one a thickness, and the
    screen chooses between them. Read only.
    """
    if not party_id:
        return dict(found=False)
    pid = qint(party_id, 'party')
    kind = (doc_type or 'SALE').upper()
    row = conn.execute(
        'SELECT i.id, i.series, i.invoice_no, i.invoice_date FROM invoice i '
        'WHERE i.company_id=? AND i.party_id=? AND i.doc_type=? '
        '  AND EXISTS (SELECT 1 FROM invoice_item ii WHERE ii.invoice_id=i.id '
        '              AND ii.rate>0 AND ii.by_weight=1) '
        'ORDER BY i.invoice_date DESC, i.id DESC LIMIT 1',
        (cid, pid, kind)).fetchone()
    if not row:
        return dict(found=False)
    lines = conn.execute(
        'SELECT item_detail, steel_thick, rate, weight FROM invoice_item '
        'WHERE invoice_id=? AND rate>0 AND by_weight=1 ORDER BY id',
        (row['id'],)).fetchall()
    return dict(found=True, id=row['id'], company_id=cid,
                series=row['series'] or '', invoice_no=row['invoice_no'],
                invoice_date=row['invoice_date'],
                # A challan made here names its steel's thickness; SHREE's
                # wrote it at the front of the item detail.
                lines=[dict(item_detail=l['item_detail'] or '',
                            steel_thick=l['steel_thick'] or '',
                            rate=l['rate'], weight=l['weight'] or 0)
                       for l in lines])


_DOC_NO = re.compile(r'^(.*?)(\d+)\s*$')


def number_order(docs):
    """Challans and bills by their number, the latest on top (4 Oct 2026:
    "Show challan and bill based on their number and not date. The recent
    on the top").

    A number only counts within its own run: a challan book (the letter in
    "G 12" -- A, B, C ... and R running beside them), a bill series (GT, R),
    one firm, one year. Inside a run the documents go strictly by number,
    highest first, whatever dates they carry. The runs are then woven
    together by time, so the book in use now is on top, an older book below
    it, and BST's and BSI's bills alternate as they were raised. A run's
    place in time at each number is the latest date on or below that number
    -- so one page dated wrong cannot lift or bury the pages above it -- and
    on a day two books share, the one in use more lately goes first: the
    last page of G and the first of H, both written on the 26th, read
    H 1, G 109, not G 109, H 1.
    """
    runs = {}
    for d in docs:
        m = _DOC_NO.match(str(d.get('invoice_no') or '').strip())
        book, n = ((m.group(1).strip(' -/'), int(m.group(2))) if m
                   else (str(d.get('invoice_no') or '').strip(), -1))
        d['_n'] = n
        runs.setdefault((d.get('company_id'), d.get('fy') or '',
                         d.get('series') or '', book), []).append(d)
    for run in runs.values():
        run.sort(key=lambda d: (d['_n'], str(d.get('invoice_no') or ''), d['id']))
        latest = ''
        for d in run:
            latest = max(latest, d.get('invoice_date') or '')
            d['_when'] = latest
        for d in run:
            d['_run'] = latest
    docs.sort(key=lambda d: (d['_when'], d['_run'], d['_n'],
                             str(d.get('invoice_no') or ''), d['id']),
              reverse=True)
    for d in docs:
        del d['_n'], d['_when'], d['_run']
    return docs


def list_invoices(conn, cid, q):
    sql = ('SELECT i.*, p.name AS party_name, p.city AS party_city, '
           '       p.gstin AS party_gstin, p.home_company AS bill_company, '
           '       bc.code AS bill_firm_code, bd.code AS billed_firm_code, '
           "       TRIM(COALESCE(b.series,'') || ' ' || b.invoice_no) "
           '         AS billed_invoice_no, '
           # The other bills a challan billed in parts went out on.
           '       (SELECT COUNT(*) FROM challan_bill cb WHERE '
           '        cb.challan_id=i.id AND cb.bill_id<>COALESCE(i.billed_invoice, 0)) '
           '         AS billed_more '
           'FROM invoice i JOIN ledger p ON p.id=i.party_id '
           'LEFT JOIN company bc ON bc.id=p.home_company '
           'LEFT JOIN company bd ON bd.id=i.billed_company '
           'LEFT JOIN invoice b ON b.id=i.billed_invoice '
           'WHERE i.company_id=?')
    args = [cid]
    if q.get('doc_type'):
        sql += ' AND i.doc_type=?'
        args.append(q['doc_type'])
    if q.get('party_id'):
        sql += ' AND i.party_id=?'
        args.append(qint(q['party_id'], 'party'))
    if q.get('from'):
        sql += ' AND i.invoice_date>=?'
        args.append(q['from'])
    if q.get('to'):
        sql += ' AND i.invoice_date<=?'
        args.append(q['to'])
    if q.get('search'):
        where, wargs = contains(['i.invoice_no', 'p.name'], q['search'])
        sql += ' AND ' + where
        args += wargs
    # Dates are stored yyyy-mm-dd, so they sort correctly as text. Wrapping the
    # column in date() only stopped ix_inv_co being used for the ordering.
    sql += ' ORDER BY i.invoice_date DESC, i.id DESC'
    # Challans and bills go by their number instead; a purchase's number is
    # the supplier's, so purchases (and notes) stay in date order.
    by_number = (q.get('doc_type') or '').upper() == 'SALE'
    if q.get('limit') and not by_number:
        sql += ' LIMIT %d' % qlimit(q['limit'], 200)
    got = rows(conn.execute(sql, args))
    if by_number:
        number_order(got)
        if q.get('limit'):
            got = got[:qlimit(q['limit'], 200)]
    return got


def get_invoice(conn, cid, iid):
    inv = one(conn.execute(
        'SELECT i.*, p.name AS party_name, p.address1 AS party_address1, '
        '  p.address2 AS party_address2, p.city AS party_city, '
        '  p.state AS party_state, p.state_code AS party_state_code, '
        '  p.pin AS party_pin, p.gstin AS party_gstin, p.pan AS party_pan, '
        '  p.phone AS party_phone, p.email AS party_email, '
        '  p.gst_type AS party_gst_type, p.home_company AS party_home_company, '
        '  hc.name AS party_home_firm, sa.name AS sales_ac_name '
        'FROM invoice i JOIN ledger p ON p.id=i.party_id '
        'LEFT JOIN company hc ON hc.id=p.home_company '
        'LEFT JOIN ledger sa ON sa.id=i.sales_ac_id '
        'WHERE i.id=? AND i.company_id=?', (iid, cid)))
    if not inv:
        raise ApiError('Document not found.', 404)
    inv['lines'] = rows(conn.execute(
        'SELECT * FROM invoice_item WHERE invoice_id=? ORDER BY id', (iid,)))
    inv['company'] = get_company(conn, cid)
    inv['history_count'] = conn.execute(
        'SELECT COUNT(*) FROM doc_history WHERE invoice_id=?', (iid,)).fetchone()[0]

    # "Opening Bal. + This Bill = Closing Bal." on the rough challan.
    opening = conn.execute(
        'SELECT COALESCE(SUM(p.debit - p.credit), 0) b FROM posting p '
        'WHERE p.company_id=? AND p.ledger_id=? AND NOT (p.doc_kind=? AND '
        'p.doc_id=?)', (cid, inv['party_id'], inv['doc_type'], iid)
    ).fetchone()['b']
    base = one(conn.execute('SELECT opening_bal FROM ledger WHERE id=?',
                            (inv['party_id'],))) or {}
    inv['party_opening'] = r2(float(base.get('opening_bal') or 0) + opening)
    inv['party_closing'] = r2(inv['party_opening'] + inv['party_amount'])

    # The other half of the hand-off: a challan says which bill went out on
    # it, and in whose books that bill sits. The screen has always shown this
    # and had nothing to show -- until the imported ten years were joined up,
    # only a challan billed inside the app carried it.
    if inv.get('billed_invoice'):
        made = one(conn.execute(
            'SELECT i.invoice_no, i.series, c.name AS firm, c.code AS firm_code '
            'FROM invoice i JOIN company c ON c.id=i.company_id WHERE i.id=?',
            (inv['billed_invoice'],)))
        if made:
            inv['billed_invoice_no'] = docno(made)
            inv['billed_firm_name'] = made['firm']
            inv['billed_firm_code'] = made['firm_code']

    # Every bill a challan went out on and every challan a bill came from,
    # when that is more than the one pair above: SHREE billed some challans
    # in parts, several together, or re-priced (link_challans.py).
    linked = rows(conn.execute(
        'SELECT d.id, d.company_id, d.series, d.invoice_no, d.invoice_date, '
        '  d.weight_total, d.taxable, d.grand_total, c.code AS firm_code, '
        '  MAX(x.how) AS how FROM ('
        '  SELECT bill_id AS id, how FROM challan_bill WHERE challan_id=:i '
        "  UNION ALL SELECT billed_invoice, '' FROM invoice "
        '    WHERE id=:i AND billed_invoice IS NOT NULL '
        '  UNION ALL SELECT challan_id, how FROM challan_bill WHERE bill_id=:i '
        "  UNION ALL SELECT id, '' FROM invoice WHERE billed_invoice=:i "
        "  UNION ALL SELECT from_challan, '' FROM invoice "
        '    WHERE id=:i AND from_challan IS NOT NULL) x '
        'JOIN invoice d ON d.id=x.id JOIN company c ON c.id=d.company_id '
        'GROUP BY d.id ORDER BY d.invoice_date, d.id', dict(i=iid)))
    for d in linked:
        d['no'] = docno(d)
    no_tax = (inv['company'].get('tax_mode') or 'gst') == 'none'
    inv['bills' if no_tax else 'challans'] = linked

    # Rate-wise GST summary block printed on the tax invoice.
    if inv.get('from_challan'):
        src = one(conn.execute(
            'SELECT invoice_no, series, grand_total, company_id FROM invoice '
            'WHERE id=?', (inv['from_challan'],)))
        if src:
            inv['challan_no'] = docno(src)
            inv['challan_total'] = r2(src['grand_total'])
            inv['challan_company'] = src['company_id']
    if inv.get('order_id'):
        o = one(conn.execute('SELECT order_no FROM sales_order WHERE id=?',
                             (inv['order_id'],)))
        inv['order_no'] = o['order_no'] if o else ''
    if inv.get('gst_voucher'):
        j = one(conn.execute('SELECT voucher_no, company_id FROM voucher '
                             'WHERE id=?', (inv['gst_voucher'],)))
        if j:
            inv['gst_voucher_no'] = j['voucher_no']
            inv['gst_voucher_company'] = j['company_id']

    inv['gst_summary'] = rows(conn.execute(
        'SELECT gst_rate rate, SUM(taxable + charge_share) taxable, '
        '  SUM(cgst) cgst, SUM(sgst) sgst, SUM(igst) igst, SUM(cess) cess '
        'FROM invoice_item WHERE invoice_id=? GROUP BY gst_rate '
        'ORDER BY gst_rate', (iid,)))
    inv['goods_total'] = r2(sum(float(l['taxable'] or 0) for l in inv['lines']))
    for g in inv['gst_summary']:
        for k in ('taxable', 'cgst', 'sgst', 'igst', 'cess'):
            g[k] = r2(g[k])
        g['total_gst'] = r2(g['cgst'] + g['sgst'] + g['igst'] + g['cess'])
    return inv


def eway_fields(data):
    """The extra transport details the e-way bill portal asks for.

    They only matter when a JSON file is being made, so they are kept out of
    the way of the rest of the document -- but they belong to the bill and
    are saved and edited with it like any other transport box.
    """
    try:
        distance = float(str(data.get('transport_distance') or '').strip() or 0)
    except ValueError:
        raise ApiError('Distance has to be a number of kilometres.')
    return dict(
        transporter_gstin=clean_gstin(data.get('transporter_gstin')) or '',
        transport_mode=(str(data.get('transport_mode') or '').strip()),
        transport_distance=max(0.0, distance),
        vehicle_type=(str(data.get('vehicle_type') or 'R').strip().upper()
                      or 'R'),
        sub_supply_type=(str(data.get('sub_supply_type') or '').strip()),
        sub_supply_desc=(str(data.get('sub_supply_desc') or '').strip())[:20],
        transaction_type=(str(data.get('transaction_type') or '1').strip()
                          or '1'))


def _purchase_items(conn, cid, lines):
    """Give every line of a purchase a product of this firm, so the goods go
    into Billing Stock. Returns the names of the products it had to make.

    Asked on 27 Sep 2026: *"as a new purchase is entered it should
    automatically update the billing stock accordingly."* Stock is counted
    product by product, and a purchase line only ever named one when it was
    picked off the list. One typed instead -- a size bought for the first
    time, most often -- saved with no product at all: the money was booked,
    and the stock never moved and nothing said so (1,000 kg picked went in on
    a copy of the books; 500 kg typed did not).

    So a line with no product takes this firm's product of the same name, or
    a new one made from the line itself -- its HSN, unit, GST and rate,
    counted in stock -- and a product of the other firm, its twin here.
    Made here, the product is the user's (`source=''`), so a SHREE update
    neither takes it for its own nor removes it while a purchase names it.
    """
    made = []
    # Names compared as they read: SHREE's carry double spaces ("CRCA
    # 72091520") that nobody types.
    squash = lambda s: ' '.join((s or '').split()).upper()
    known = {}
    for r in conn.execute('SELECT id, name FROM item WHERE company_id=? '
                          'ORDER BY active, id DESC', (cid,)):
        known[squash(r['name'])] = r['id']      # active, then oldest, wins
    for ln in lines:
        name = ' '.join((ln.get('description') or '').split())
        it = (one(conn.execute('SELECT id, company_id FROM item WHERE id=?',
                               (ln['item_id'],))) if ln.get('item_id') else None)
        if it:
            if it['company_id'] != cid:
                twin = counterpart_item(conn, it['id'], cid)
                ln['item_id'] = twin['id'] if twin else None
            continue
        ln['item_id'] = None
        if not name:
            continue
        if squash(name) in known:
            ln['item_id'] = known[squash(name)]
            continue
        unit = (ln.get('unit') or '').strip() or 'KGS'
        new = dict(code=None, name=name, category='', hsn=ln.get('hsn') or '',
                   stock_cat=db.guess_stock_cat(name, ln.get('hsn'),
                                                db.stock_cats(conn)),
                   unit=unit, gst_rate=ln.get('gst_rate') or 0,
                   cess_rate=ln.get('cess_rate') or 0, sale_rate=0,
                   purchase_rate=ln.get('rate') or 0, opening_qty=0,
                   reorder_level=0, track_stock=1,
                   by_weight=1 if ln.get('by_weight', 1) else 0, active=1,
                   source='')
        cur = conn.execute('INSERT INTO item(company_id, %s) VALUES (?%s)'
                           % (', '.join(new), ', ?' * len(new)),
                           [cid] + list(new.values()))
        ln['item_id'] = known[squash(name)] = cur.lastrowid
        made.append(name)
    return made


def save_invoice(conn, cid, iid, data, who='', action=None, note=''):
    """Save a bill, challan, purchase or note, new (`iid` None) or edited.

    `who` and `action`/`note` are for its history (`_keep_history`): which
    device saved it, and -- for a bill redone from its edited challan --
    why it changed when nobody opened it.
    """
    # `iid` is reassigned to the new row's id when creating, so whether this
    # was an edit has to be remembered before that happens.
    updating = bool(iid)
    doc_type = (data.get('doc_type') or 'SALE').upper()
    if doc_type not in DOC_TYPES:
        raise ApiError('Unknown document type "%s".' % doc_type)
    if not data.get('party_id'):
        raise ApiError('Select a party for this document.')
    party_id = qint(data['party_id'], 'party')
    date = (data.get('invoice_date') or '').strip()
    if not date:
        raise ApiError('Document date is required.')

    mode = tax_mode(conn, cid)
    is_igst = resolve_igst(conn, cid, party_id, data.get('is_igst'),
                           data.get('supply_state') or '')
    rcm = 1 if (data.get('rcm') and mode != 'none'
                and doc_type in ('PURCHASE', 'DEBIT')) else 0
    lines, tot = compute_lines(data.get('lines'), is_igst, mode)
    if not lines:
        raise ApiError('Add at least one line item.')
    # Only a challan takes steel out of the yard's stock (Challan Stock). A
    # line names all three -- type, quality, thickness -- or none of them,
    # and each off the lists (or as the challan already said it). A SHREE
    # challan's line names only its type (4 Oct 2026) and is saved again as
    # it stood.
    steel_was = None
    for n, ln in enumerate(lines, 1):
        if mode != 'none' or doc_type != 'SALE':
            ln.update(steel_type='', steel_quality='', steel_thick='')
            continue
        parts = (ln['steel_type'], ln['steel_quality'], ln['steel_thick'])
        if any(parts) and steel_was is None:
            steel_was = [tuple(r) for r in conn.execute(
                'SELECT steel_type, steel_quality, steel_thick FROM '
                'invoice_item WHERE invoice_id=?', (iid or 0,))]
        if any(parts) and not all(parts) and parts not in steel_was:
            raise ApiError('Line %d: choose the steel type, quality and '
                           'thickness -- all three, or none.' % n)
        if all(parts):
            odd = steel_unknown(conn, parts, steel_was)
            if odd:
                raise ApiError('Line %d: "%s" is not on the steel list.' % (n, odd))

    cutting = float(data.get('cutting') or 0)
    transport = float(data.get('transport') or data.get('freight') or 0)
    loading = float(data.get('loading') or 0)
    charges = gstcalc.money(cutting + transport + loading)
    # TCS is typed on a purchase only (8 Oct 2026).
    tcs = gstcalc.money(data.get('tcs') or 0) if doc_type == 'PURCHASE' else 0.0

    # The bill as a whole, the way SHREE worked every GST bill out (gst.py):
    # cutting, transport and loading are part of the value of supply, so each
    # line takes a share by value and is taxed with it; a bill made from a
    # challan carries that challan's round off into the taxable value; CGST
    # and SGST are each the half rate on each rate's taxable value, IGST the
    # whole rate. There is no automatic round off since 2 Oct 2026 ("remove
    # auto round off"): the total stays to the paisa unless a Round Off was
    # typed. SHREE rounded all 12,155 of its GST bills; those keep the round
    # off they came with.
    adj = gstcalc.money(data.get('taxable_adj')) if mode != 'none' else 0.0
    worked = gstcalc.bill(lines, charges=charges, adj=adj,
                          is_igst=bool(is_igst), taxed=mode != 'none',
                          round_off=data.get('round_off'), tcs=tcs)
    for k in ('cgst', 'sgst', 'igst', 'cess'):
        tot[k] = worked[k]
    goods = worked['goods']
    taxable_value = worked['taxable']
    tax_total = worked['tax']
    round_off = worked['round_off']
    grand = worked['grand_total']
    # Under reverse charge the supplier bills no tax; we self-assess it.
    party_amount = r2(grand - tax_total) if rcm else r2(grand)

    party = one(conn.execute('SELECT * FROM ledger WHERE id=? AND company_id=?',
                             (party_id, cid)))
    if not party:
        raise ApiError('Party not found in this company.', 404)

    sales_ac_id = data.get('sales_ac_id') or None
    if sales_ac_id:
        sales_ac_id = qint(sales_ac_id, 'ledger')
        if not conn.execute('SELECT 1 FROM ledger WHERE id=? AND company_id=?',
                            (sales_ac_id, cid)).fetchone():
            raise ApiError('That Sales A/c is not in this company.')

    head = dict(
        company_id=cid, doc_type=doc_type,
        series=(data.get('series') or '').strip(),
        invoice_type=(data.get('invoice_type') or 'Debit').strip() or 'Debit',
        invoice_date=date, fy=db.fy_of(date), party_id=party_id,
        sales_ac_id=sales_ac_id,
        driver_name=(data.get('driver_name') or '').strip(),
        payment_terms=(data.get('payment_terms') or '').strip(),
        dept=(data.get('dept') or '').strip(),
        order_id=data.get('order_id') or None,
        supply_state=(data.get('supply_state') or party.get('state') or ''),
        is_igst=is_igst, rcm=rcm, ref_no=(data.get('ref_no') or '').strip(),
        from_challan=data.get('from_challan') or None,
        subtotal=tot['subtotal'], discount_amt=tot['discount'],
        taxable=taxable_value, cgst=tot['cgst'], sgst=tot['sgst'],
        igst=tot['igst'], cess=tot['cess'], weight_total=tot['weight'],
        cutting=r2(cutting), transport=r2(transport), loading=r2(loading),
        tcs=r2(tcs), taxable_adj=adj,
        round_off=round_off, grand_total=r2(grand), party_amount=party_amount,
        transporter=(data.get('transporter') or '').strip(),
        vehicle_no=(data.get('vehicle_no') or '').strip(),
        lr_no=(data.get('lr_no') or '').strip(),
        lr_date=(data.get('lr_date') or '').strip(),
        cases=(data.get('cases') or '').strip(),
        eway_no=(data.get('eway_no') or '').strip(),
        remarks=(data.get('remarks') or '').strip(),
        **eway_fields(data))

    # A purchase into a GST firm always lands in Billing Stock -- see
    # _purchase_items. The challan firm keeps no item stock.
    new_items = (_purchase_items(conn, cid, lines)
                 if doc_type == 'PURCHASE' and mode != 'none' else [])

    was = None
    before = None
    if iid:
        existing = one(conn.execute(
            'SELECT * FROM invoice WHERE id=? AND company_id=?', (iid, cid)))
        if not existing:
            raise ApiError('Document not found.', 404)
        was = _invoice_signature(conn, iid)
        before = _history_before(conn, iid)
        # An imported document saved without a change is left exactly as it
        # came: its tax, totals, lines and SHREE's own ledger postings. Saved
        # unchanged it used to be worked out again the app's way -- challan
        # G 34's sale split into Sales and Loading, and its bill's GST journal
        # posted a second time beside SHREE's (2 Oct 2026).
        if existing.get('source') == 'SHREE':
            conn.execute('SAVEPOINT resave')
        head['invoice_no'] = existing['invoice_no']
        sets = ', '.join('%s=?' % k for k in head)
        conn.execute('UPDATE invoice SET %s WHERE id=?' % sets,
                     list(head.values()) + [iid])
        conn.execute('DELETE FROM invoice_item WHERE invoice_id=?', (iid,))
    else:
        no = (data.get('invoice_no') or '').strip()
        # Numbered inside its own run: GT and R are numbered separately, so
        # the series decides which number is next.
        head['invoice_no'] = no or db.next_number(conn, cid, doc_type,
                                                  db.fy_of(date),
                                                  head['series'])
        cols = ','.join(head.keys())
        ph = ','.join('?' * len(head))
        try:
            cur = conn.execute('INSERT INTO invoice(%s) VALUES (%s)' % (cols, ph),
                               list(head.values()))
        except sqlite3.IntegrityError:
            raise ApiError('Document number "%s" already exists.'
                           % head['invoice_no'])
        iid = cur.lastrowid
        if no:
            db.sync_counter(conn, cid, doc_type, db.fy_of(date), no)

    if lines:
        cols = list(lines[0].keys()) + ['invoice_id']
        conn.executemany(
            'INSERT INTO invoice_item(%s) VALUES (%s)'
            % (','.join(cols), ','.join('?' * len(cols))),
            [[ln[c] for c in cols[:-1]] + [iid] for ln in lines])
    edited = was is not None and _invoice_signature(conn, iid) != was
    if updating and not edited and existing.get('source') == 'SHREE':
        conn.execute('ROLLBACK TO resave')
        conn.execute('RELEASE resave')
        # Only e-way details typed for the portal, if any: written alone,
        # as the e-way screen's own save does.
        ew = {k: v for k, v in eway_fields(data).items()
              if k in data and (existing.get(k) or '') != (v or '')
              and not (k in ('sub_supply_type', 'transaction_type')
                       and v == '1' and not existing.get(k))}
        if ew:
            conn.execute('UPDATE invoice SET %s WHERE id=?'
                         % ', '.join('%s=?' % k for k in ew),
                         list(ew.values()) + [iid])
        _keep_history(conn, iid, before, action or 'edited', note, who)
        conn.commit()
        return get_invoice(conn, cid, iid)
    if edited:
        _adopt_edited(conn, 'invoice', existing, doc_type,
                      doc_key(existing['doc_type'], existing['fy'],
                              existing['series'], existing['invoice_no']),
                      _doc_fingerprint(conn, existing))

    head['party_id'] = party_id
    repost(conn, cid, doc_type, iid, date, head['invoice_no'],
           invoice_entries(conn, cid, dict(head, goods=goods)),
           '%s - %s' % (DOC_LABEL[doc_type], party['name']))

    if head.get('order_id'):
        # A challan fulfils the order; the tax bill closes it.
        state = 'challaned' if mode == 'none' else 'invoiced'
        conn.execute('UPDATE sales_order SET status=?, '
                     'billed_company=?, billed_invoice=? WHERE id=?',
                     (state, cid, iid, head['order_id']))
    # A bill puts its GST on the challan's customer in the order firm, the
    # way SHREE did -- and an edit, or a bill no longer from a challan, keeps
    # that journal in step. See _sync_gst_journal.
    if mode != 'none' and doc_type == 'SALE':
        _sync_gst_journal(conn, iid)
    if head.get('from_challan'):
        conn.execute('UPDATE invoice SET billed_company=?, billed_invoice=? '
                     'WHERE id=?', (cid, iid, head['from_challan']))
        # Billing the challan closes the order it came from, if there was one.
        conn.execute(
            "UPDATE sales_order SET status='invoiced', billed_company=?, "
            'billed_invoice=? WHERE id=(SELECT order_id FROM invoice WHERE id=?)',
            (cid, iid, head['from_challan']))
    _keep_history(conn, iid, before,
                  action or ('edited' if updating else 'created'), note, who)
    conn.commit()
    out = get_invoice(conn, cid, iid)
    if new_items:
        out['new_items'] = new_items

    # Editing a challan that has already been billed has to carry through to
    # the bill, or the two stop agreeing. Only on an edit: a document just
    # created has nothing hanging off it yet. The challan is already saved by
    # this point, so a bill that cannot be rebuilt is reported rather than
    # allowed to fail the save and hide itself.
    # Only when something was changed: rebuilt for nothing, an imported
    # bill lost its own item descriptions to the challan's.
    if updating and edited:
        try:
            again = resync_bill(conn, cid, iid, who)
            if again:
                out['resynced_bill'] = again
        except ApiError as e:
            out['resync_error'] = e.message
    return out


# ------------------------------------------ what each save changed ---
#
# 6 Oct 2026: "add a history feature in all the bills and challans so I can
# see all the changes done in any particular detail. Whenever the bill or
# challan is saved the history should be kept, if anything changes then it
# should show." Every save that leaves the document different from the last
# one kept writes the whole document down again (`doc_history`), and the
# History window compares each with the one before it.

# The boxes of the form, in the order the window lists what changed: the
# header, then the money worked out from the lines, then the transport card.
HISTORY_HEAD = ('no', 'invoice_date', 'party', 'sales_ac', 'invoice_type',
                'driver_name', 'payment_terms', 'ref_no', 'is_igst', 'rcm',
                'dept', 'remarks')
HISTORY_MONEY = ('weight_total', 'cutting', 'transport', 'loading', 'taxable',
                 'cgst', 'sgst', 'igst', 'cess', 'tcs', 'round_off',
                 'grand_total')
HISTORY_TRANSPORT = ('transporter', 'vehicle_no', 'lr_no', 'lr_date', 'cases',
                     'eway_no', 'transporter_gstin', 'transport_mode',
                     'transport_distance', 'vehicle_type', 'sub_supply_type')
HISTORY_LINE = ('description', 'item_detail', 'steel', 'hsn', 'qty', 'weight',
                'rate', 'amount', 'gst_rate')


def _doc_snapshot(conn, iid):
    """The document as its screen shows it, for the history -- or None.

    Names rather than ids, so a version says who the bill was made out to on
    the day. Figures to the paisa (kilos to the gram), so that working the
    same bill out again never reads as a change. What the form fills in by
    itself -- the e-way boxes' defaults -- is kept the way the form fills it,
    or the first plain save of an old bill would list them as edits.
    """
    inv = one(conn.execute(
        'SELECT i.*, p.name AS party_name, sa.name AS sales_ac_name, '
        '  c.tax_mode FROM invoice i JOIN company c ON c.id=i.company_id '
        'LEFT JOIN ledger p ON p.id=i.party_id '
        'LEFT JOIN ledger sa ON sa.id=i.sales_ac_id WHERE i.id=?', (iid,)))
    if not inv:
        return None
    taxed = (inv['tax_mode'] or 'gst') != 'none'
    text = lambda v: ' '.join(str(v or '').split())
    head = dict(no=docno(inv), invoice_date=inv['invoice_date'] or '',
                party=inv['party_name'] or '', sales_ac=inv['sales_ac_name'] or '',
                invoice_type=text(inv['invoice_type']),
                driver_name=text(inv['driver_name']),
                payment_terms=text(inv['payment_terms']),
                ref_no=text(inv['ref_no']), dept=text(inv['dept']),
                remarks=(inv['remarks'] or '').strip(),
                is_igst=1 if inv['is_igst'] else 0, rcm=1 if inv['rcm'] else 0,
                weight_total=r3(inv['weight_total']))
    for k in HISTORY_MONEY[1:]:
        head[k] = r2(inv[k])
    for k in HISTORY_TRANSPORT:
        head[k] = text(inv.get(k))
    head['transport_distance'] = r2(inv.get('transport_distance'))
    head['vehicle_type'] = head['vehicle_type'] or 'R'
    head['sub_supply_type'] = head['sub_supply_type'] or '1'
    lines = []
    for l in conn.execute('SELECT * FROM invoice_item WHERE invoice_id=? '
                          'ORDER BY id', (iid,)):
        ln = dict(description=text(l['description']),
                  item_detail=text(l['item_detail']), hsn=text(l['hsn']),
                  qty=r3(l['qty']), weight=r3(l['weight']),
                  rate=round(float(l['rate'] or 0) + 1e-9, 4),
                  amount=r2(l['taxable']))
        steel = ' · '.join(x for x in (l['steel_type'], l['steel_quality'],
                                       l['steel_thick']) if x)
        if steel:
            ln['steel'] = steel
        # A challan carries no tax: its lines' GST is nought whatever SHREE
        # stored. A line's stored total is left out: it carries its share of
        # the charges, which the Total on the screen does not, and the
        # bill's own totals already say what the charges came to.
        if taxed:
            ln['gst_rate'] = r2(l['gst_rate'])
        lines.append(ln)
    return dict(head=head, lines=lines)


def _snap_text(snap):
    return json.dumps(snap, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'))


def _keep_history(conn, iid, before, action, note='', who=''):
    """Write this save into the document's history, if it changed anything.

    `before` is the document as it stood before the save (None for a new
    one). A document saved for the first time since history began first gets
    that state written down, so its first change has something to be compared
    with; one changed between two saves by something other than its own
    screen -- a SHREE update, a merge of accounts, a repair -- gets that
    written down as such, so the change is not put on the save that followed.
    """
    after = _doc_snapshot(conn, iid)
    if after is None:
        return
    cid = conn.execute('SELECT company_id FROM invoice WHERE id=?',
                       (iid,)).fetchone()
    last = conn.execute('SELECT snapshot FROM doc_history WHERE invoice_id=? '
                        'ORDER BY id DESC LIMIT 1', (iid,)).fetchone()
    last = last[0] if last else None
    now_text = _snap_text(after)
    put = []
    if before is not None:
        was_text = _snap_text(dict(head=before['head'], lines=before['lines']))
        if last is None:
            if was_text != now_text:
                put.append(('before', 'SHREE' if (before.get('source')
                                                  == 'SHREE') else '',
                            '', was_text, ''))
        elif was_text != last:
            put.append(('elsewhere', '', '', was_text, ''))
        if was_text != now_text:
            put.append((action, note, who, now_text, None))
    elif last != now_text:
        put.append((action, note, who, now_text, None))
    for act, nt, wh, snap, at in put:
        conn.execute(
            'INSERT INTO doc_history(invoice_id, company_id, action, note, who, '
            "snapshot, saved_at) VALUES (?,?,?,?,?,?,COALESCE(?, "
            "datetime('now','localtime')))",
            (iid, cid['company_id'], act, nt, wh, snap, at))


def _history_before(conn, iid):
    """The document before a save, for `_keep_history` -- with where it came
    from, so the first version of an imported one can say so."""
    snap = _doc_snapshot(conn, iid)
    if snap is not None:
        src = conn.execute('SELECT source FROM invoice WHERE id=?',
                           (iid,)).fetchone()
        snap = dict(snap, source=(src[0] or '') if src else '')
    return snap


def _history_changes(old, new):
    """What differs between two versions: the header boxes, each line, the
    totals and the transport card, in the order the form shows them.

    Lines are matched by what they are -- product and item detail -- so a
    line put in or taken out in the middle reads as just that, and not as
    every line below it changed. Lines that stand in the same place but were
    renamed are compared field by field.
    """
    import difflib
    out = []
    oh, nh = old['head'], new['head']

    # A version kept before a box existed reads as that box's empty value:
    # TCS came in on 8 Oct 2026, and every older bill has none.
    since = dict(tcs=0.0)

    def head(keys):
        for k in keys:
            was, now = oh.get(k, since.get(k)), nh.get(k, since.get(k))
            if was != now:
                out.append(dict(at='head', field=k, old=was, new=now))

    head(HISTORY_HEAD)
    ol, nl = old['lines'], new['lines']
    key = lambda l: (l.get('description'), l.get('item_detail'), l.get('steel', ''))
    sm = difflib.SequenceMatcher(None, [key(l) for l in ol],
                                 [key(l) for l in nl], autojunk=False)

    def same_place(i, j):
        a, b = ol[i], nl[j]
        diffs = [dict(field=f, old=a.get(f), new=b.get(f)) for f in HISTORY_LINE
                 if a.get(f) != b.get(f) and (f in a or f in b)]
        if diffs:
            out.append(dict(at='line', line=j + 1, was_line=i + 1, now=b,
                            fields=diffs))

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for n in range(i2 - i1):
                same_place(i1 + n, j1 + n)
            continue
        paired = min(i2 - i1, j2 - j1) if tag == 'replace' else 0
        for n in range(paired):
            same_place(i1 + n, j1 + n)
        for i in range(i1 + paired, i2):
            out.append(dict(at='removed', was_line=i + 1, now=ol[i]))
        for j in range(j1 + paired, j2):
            out.append(dict(at='added', line=j + 1, now=nl[j]))
    head(HISTORY_MONEY)
    head(HISTORY_TRANSPORT)
    return out


def invoice_history(conn, cid, iid):
    """Every version of one bill or challan, newest first, each with what
    changed from the one before it -- what the History window shows."""
    inv = one(conn.execute(
        'SELECT i.id, i.doc_type, i.series, i.invoice_no, i.source, '
        '  i.created_at, c.tax_mode FROM invoice i '
        'JOIN company c ON c.id=i.company_id WHERE i.id=? AND i.company_id=?',
        (iid, cid)))
    if not inv:
        raise ApiError('Document not found.', 404)
    got = rows(conn.execute(
        'SELECT id, action, note, who, snapshot, saved_at FROM doc_history '
        'WHERE invoice_id=? ORDER BY id', (iid,)))
    prev = None
    for n, v in enumerate(got, 1):
        snap = json.loads(v.pop('snapshot'))
        v['version'] = n
        v['doc'] = snap
        v['changes'] = _history_changes(prev, snap) if prev else []
        prev = snap
    got.reverse()
    return dict(id=iid, no=docno(inv), doc_type=inv['doc_type'],
                taxed=(inv['tax_mode'] or 'gst') != 'none',
                source=inv['source'] or '', created_at=inv['created_at'],
                versions=got)


# ------------------------------------------------ the GST on a challan ---

def _drop_voucher(conn, vid):
    """Remove one voucher and everything it posted."""
    v = one(conn.execute('SELECT kind FROM voucher WHERE id=?', (vid,)))
    if not v:
        return
    conn.execute('DELETE FROM posting WHERE doc_kind=? AND doc_id=?',
                 (v['kind'], vid))
    conn.execute('DELETE FROM voucher_line WHERE voucher_id=?', (vid,))
    conn.execute('DELETE FROM voucher WHERE id=?', (vid,))


def _gst_bridge_ledger(conn, order_cid, firm_code, firm_name):
    """The account in the order firm a billing firm's GST is put against.

    SHREE credits the tax on a challan to "BALKRISHNA STEEL INDUSTRIES (GST)"
    or "BALKRISHNA STEEL TRADERS (GST)" inside the order firm -- the accounts
    `allocate_customers.py` reads a customer's firm off. They are already in
    these books; a fresh set gets one made the first time a bill needs it.
    """
    names = [n for code, hints in BRIDGE_HINTS if code == firm_code
             for n in hints]
    names = sorted(names, key=lambda n: '(GST)' not in n.upper()) \
        + ['%s (GST)' % firm_name]
    for n in names:
        r = one(conn.execute(
            'SELECT l.id FROM ledger l JOIN account_group g ON g.id=l.group_id '
            "WHERE l.company_id=? AND upper(trim(l.name))=upper(?) "
            "AND g.code<>'2101'", (order_cid, n)))
        if r:
            return r['id']
    grp = one(conn.execute(
        "SELECT id FROM account_group WHERE company_id=? AND code='9008'",
        (order_cid,))) or one(conn.execute(
            "SELECT id FROM account_group WHERE company_id=? "
            "AND nature='liability' ORDER BY sort, code LIMIT 1", (order_cid,)))
    if not grp:
        raise ApiError('The order firm has no account group to hold GST.', 500)
    return conn.execute(
        'INSERT INTO ledger(company_id, group_id, name) VALUES (?,?,?)',
        (order_cid, grp['id'], '%s (GST)' % firm_name)).lastrowid


def _sync_gst_journal(conn, bill_id):
    """Keep the GST journal a bill puts on its challan's customer in step.

    **A challan carries no tax.** When SHREE billed one it posted a journal in
    the order firm on the bill's date -- the customer debited with the bill's
    tax to the rupee, the billing firm's (GST) account credited, the bill's own
    number as the reference ("GT/182") -- so the customer's account there
    reads challan plus GST, which is what they owe. A bill raised from a
    challan in this app posted no such journal, and the account was short by
    the tax. Asked for on 15 Sep 2026.

    Called every time a bill is saved -- raised, edited, or rebuilt from an
    edited challan by `resync_bill` -- and it makes, rewrites or removes the
    journal to match. A bill with no challan behind it, or no tax on it, has
    none. `invoice.gst_voucher` is how the bill knows which journal is its
    own; deleting the bill deletes it (`_purge_invoice`).
    """
    bill = one(conn.execute(
        'SELECT i.*, c.code AS firm_code, c.name AS firm_name, c.tax_mode '
        'FROM invoice i JOIN company c ON c.id=i.company_id WHERE i.id=?',
        (bill_id,)))
    if not bill:
        return None
    challan = one(conn.execute(
        'SELECT id, company_id, party_id FROM invoice WHERE id=?',
        (bill['from_challan'],))) if bill['from_challan'] else None
    old = one(conn.execute("SELECT * FROM voucher WHERE id=? AND kind='JOURNAL'",
                           (bill['gst_voucher'],))) if bill['gst_voucher'] else None
    if old is None and challan:
        # An imported bill edited here already has SHREE's own GST journal
        # on the challan's customer -- same day, the bill's number as its
        # reference. That one is taken over, not posted a second time: BST
        # GT 200 edited on 2 Oct 2026 put the tax on BALAJI twice.
        old = _shree_gst_journal(conn, bill, challan)
    # The bill over its challan: the tax, and any Round Off typed on the bill
    # -- so the customer's account here reads exactly the bill. Until 2 Oct
    # 2026 it was the tax to the rupee, as SHREE posts it, which matched only
    # while every bill was rounded to the rupee too.
    tax = sum(float(bill[k] or 0) for k in ('cgst', 'sgst', 'igst', 'cess'))
    amount = r2(tax + float(bill['round_off'] or 0)) if tax > 0 else 0.0

    if (bill['doc_type'] != 'SALE' or (bill['tax_mode'] or 'gst') == 'none'
            or not challan or amount < 1):
        if old:
            _drop_voucher(conn, old['id'])
        if bill['gst_voucher']:
            conn.execute('UPDATE invoice SET gst_voucher=NULL WHERE id=?',
                         (bill_id,))
        return None

    order_cid, party = challan['company_id'], challan['party_id']
    bridge = _gst_bridge_ledger(conn, order_cid, bill['firm_code'],
                                bill['firm_name'])
    date = bill['invoice_date']
    fy = db.fy_of(date)
    series = (bill['series'] or '').strip()
    ref = '%s/%s' % (series, bill['invoice_no']) if series else bill['invoice_no']
    head = dict(company_id=order_cid, kind='JOURNAL', voucher_date=date, fy=fy,
                party_id=party, bank_id=None, mode='Cash', ref_no=ref,
                amount=amount, remarks='')
    if old and old['company_id'] == order_cid and old['fy'] == fy:
        vid = old['id']
        head['voucher_no'] = old['voucher_no']
        conn.execute('UPDATE voucher SET %s WHERE id=?'
                     % ', '.join('%s=?' % k for k in head),
                     list(head.values()) + [vid])
        conn.execute('DELETE FROM voucher_line WHERE voucher_id=?', (vid,))
    else:
        if old:
            _drop_voucher(conn, old['id'])
        head['voucher_no'] = db.next_number(conn, order_cid, 'JOURNAL', fy)
        vid = conn.execute('INSERT INTO voucher(%s) VALUES (%s)'
                           % (','.join(head), ','.join('?' * len(head))),
                           list(head.values())).lastrowid
        conn.execute('UPDATE invoice SET gst_voucher=? WHERE id=?',
                     (vid, bill_id))
    conn.executemany(
        'INSERT INTO voucher_line(voucher_id, ledger_id, debit, credit, remarks) '
        'VALUES (?,?,?,?,?)', [(vid, party, amount, 0.0, ''),
                               (vid, bridge, 0.0, amount, '')])
    # The narration is the reference, as on SHREE's own: the account reads
    # "Jrnl Reference: GT/182" beneath the (GST) account.
    repost(conn, order_cid, 'JOURNAL', vid, date, head['voucher_no'],
           [(party, amount, 0), (bridge, 0, amount)], ref)
    return vid


def _shree_gst_journal(conn, bill, challan):
    """SHREE's GST journal for `bill`, made this app's own so it can be
    rewritten in place -- or None. It is SHREE's: dated the bill's day in the
    challan's firm, its reference the bill's number ("GT/200", or "KB/09
    GT/200"), the challan's customer on one side, and no bill of this app's
    pointing at it already."""
    series = (bill['series'] or '').strip()
    ref = '%s/%s' % (series, bill['invoice_no']) if series else bill['invoice_no']
    for v in rows(conn.execute(
            "SELECT * FROM voucher WHERE company_id=? AND kind='JOURNAL' AND "
            "source='SHREE' AND voucher_date=? AND (ref_no=? OR ref_no LIKE ?) "
            'AND NOT EXISTS (SELECT 1 FROM invoice i WHERE i.gst_voucher='
            'voucher.id) ORDER BY id',
            (challan['company_id'], bill['invoice_date'], ref, '% ' + ref))):
        if not conn.execute('SELECT 1 FROM voucher_line WHERE voucher_id=? '
                            'AND ledger_id=?',
                            (v['id'], challan['party_id'])).fetchone():
            continue
        _adopt_edited(conn, 'voucher', v, 'JOURNAL',
                      doc_key('JOURNAL', v['fy'], '', v['voucher_no']),
                      _voucher_fingerprint(conn, v))
        conn.execute('UPDATE invoice SET gst_voucher=? WHERE id=?',
                     (v['id'], bill['id']))
        return v
    return None


def dependent_bill(conn, iid):
    """The GST bill, if any, that was raised from this challan.

    The bill sits in one of the official firms, so it is looked up across
    every set of books rather than inside the active one.
    """
    return one(conn.execute(
        'SELECT i.id, i.series, i.invoice_no, i.company_id, c.name AS firm '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        'WHERE i.from_challan=?', (iid,)))


def _purge_invoice(conn, inv, record=False):
    """Undo one document and everything it set off, in whichever firm's books
    it sits. Whether it is allowed to go is the caller's decision."""
    iid = inv['id']
    # Deleted by hand, an imported document is written down, or the next
    # SHREE update brings it straight back.
    if record and inv.get('source') == 'SHREE':
        record_rule(conn, 'delete_doc', inv['company_id'],
                    doc_key(inv['doc_type'], inv['fy'], inv['series'],
                            inv['invoice_no']),
                    dict(_doc_fingerprint(conn, inv), table='invoice'),
                    'Deleted in this app')

    # A bill's GST journal on its challan's customer goes with it.
    if inv.get('gst_voucher'):
        _drop_voucher(conn, inv['gst_voucher'])

    # Deleting a bill frees the challan it was made from to be billed again.
    if inv['from_challan']:
        conn.execute('UPDATE invoice SET billed_company=NULL, '
                     'billed_invoice=NULL WHERE id=?', (inv['from_challan'],))

    # The order this document answered for is dealt with once the whole chain
    # has gone -- see settle_order. It is deliberately not stepped back to
    # open here: a deleted challan should not put the work back in the order
    # book as though it had never been done.

    # Nothing may be left pointing at the row, and the links have to be cut
    # BEFORE it goes: sales_order.billed_invoice and invoice_item.invoice_id
    # are real foreign keys, so a stray reference makes the delete fail
    # outright rather than merely leaving a dangling id behind.
    conn.execute('UPDATE invoice SET from_challan=NULL WHERE from_challan=?', (iid,))
    # A challan billed in parts still went out on its other bills: it moves
    # on to the next of them, and is free to be billed again only when none
    # is left. Its challan_bill rows go with the bill (ON DELETE CASCADE).
    for r in rows(conn.execute('SELECT id FROM invoice WHERE billed_invoice=?',
                               (iid,))):
        nxt = one(conn.execute(
            'SELECT b.id, b.company_id FROM challan_bill cb '
            'JOIN invoice b ON b.id=cb.bill_id '
            'WHERE cb.challan_id=? AND cb.bill_id<>? '
            'ORDER BY b.invoice_date, b.id LIMIT 1', (r['id'], iid)))
        conn.execute('UPDATE invoice SET billed_company=?, billed_invoice=? '
                     'WHERE id=?', (nxt['company_id'] if nxt else None,
                                    nxt['id'] if nxt else None, r['id']))
    conn.execute('UPDATE sales_order SET billed_company=NULL, billed_invoice=NULL '
                 'WHERE billed_invoice=?', (iid,))

    conn.execute('DELETE FROM posting WHERE doc_kind=? AND doc_id=?',
                 (inv['doc_type'], iid))
    conn.execute('DELETE FROM invoice_item WHERE invoice_id=?', (iid,))
    conn.execute('DELETE FROM doc_history WHERE invoice_id=?', (iid,))
    conn.execute('DELETE FROM invoice WHERE id=?', (iid,))


def orders_behind(conn, iid):
    """The orders a document answers for -- the one it was raised from, and
    any that point back at it."""
    found = set()
    row = one(conn.execute('SELECT order_id FROM invoice WHERE id=?', (iid,)))
    if row and row['order_id']:
        found.add(row['order_id'])
    for r in rows(conn.execute('SELECT id FROM sales_order WHERE billed_invoice=?',
                               (iid,))):
        found.add(r['id'])
    return found


def settle_order(conn, oid):
    """Decide what becomes of an order once documents raised from it have gone.

    While anything still answers for it the order stays, pointed at whatever
    survives. Once the last one has gone there is nothing left for the order to
    track, so it goes too -- deleting a challan must not hand the work back to
    the order book as an open order. Returns its number if it was deleted.
    """
    left = one(conn.execute(
        'SELECT i.id, i.company_id, c.tax_mode FROM invoice i '
        'JOIN company c ON c.id=i.company_id '
        'WHERE i.order_id=? ORDER BY i.id LIMIT 1', (oid,)))
    if left:
        conn.execute('UPDATE sales_order SET status=?, billed_company=?, '
                     'billed_invoice=? WHERE id=?',
                     ('challaned' if left['tax_mode'] == 'none' else 'invoiced',
                      left['company_id'], left['id'], oid))
        return None

    o = one(conn.execute('SELECT order_no FROM sales_order WHERE id=?', (oid,)))
    if not o:
        return None
    conn.execute('DELETE FROM sales_order_item WHERE order_id=?', (oid,))
    conn.execute('DELETE FROM sales_order WHERE id=?', (oid,))
    return o['order_no']


def purge_chain(conn, iid, seen=None, keep_orders=False, record=False):
    """Delete a document together with the bill raised from it, and the bill
    raised from that, however far the chain runs. The bill goes first so the
    challan is never left with a tax invoice pointing at nothing.

    The order behind the chain goes with it unless the caller is deleting that
    order itself and will clear it up.

    Returns the numbers of everything that went, newest link first.
    """
    seen = set() if seen is None else seen
    if iid in seen:
        return []
    seen.add(iid)
    inv = one(conn.execute('SELECT * FROM invoice WHERE id=?', (iid,)))
    if not inv:
        return []
    gone = []
    behind = orders_behind(conn, iid)
    made = dependent_bill(conn, iid)
    if made:
        # Read the bill's own order link first -- once it is deleted there is
        # nothing left to read it from.
        behind |= orders_behind(conn, made['id'])
        gone += purge_chain(conn, made['id'], seen, keep_orders=True,
                            record=record)
    _purge_invoice(conn, inv, record)
    gone.append(docno(inv))
    if not keep_orders:
        for oid in sorted(behind):
            no = settle_order(conn, oid)
            if no:
                gone.append('order %s' % no)
    return gone


def delete_invoice(conn, cid, iid, cascade=False, keep=False):
    """Delete a document and undo everything it set off.

    A challan that has already been billed cannot go on its own -- the tax
    invoice would be left pointing at nothing. Rather than making the user
    switch firms and hunt the bill down, the block says so and `cascade`
    takes the bill along, whichever firm holds it.

    A document that came from SHREE comes back at the next SHREE update
    unless `keep` asks for it to be kept deleted here. Until 17 Sep 2026
    every such delete was kept: 1,116 bills and challans deleted in the app
    that afternoon were deleted again by every update, and SHREE's data
    could not be brought in at all.
    """
    inv = one(conn.execute('SELECT * FROM invoice WHERE id=? AND company_id=?',
                           (iid, cid)))
    if not inv:
        raise ApiError('Document not found.', 404)

    made = dependent_bill(conn, iid)
    if made and not cascade:
        raise ApiError(
            '%s has already been billed as %s by %s.'
            % (docno(inv), docno(made), made['firm']), 409,
            {'cascade': True,
             'blocked_by': ['%s (%s)' % (docno(made), made['firm'])]})

    gone = purge_chain(conn, iid, record=keep)
    conn.commit()
    return {'ok': True, 'deleted': gone}


# ----------------------------------------------------------------- orders

def list_orders(conn, cid, q):
    sql = ('SELECT o.*, p.name AS party_name, p.city AS party_city, '
           '  p.home_company AS party_bill_company, '
           '  bc.code AS bill_firm_code, bc.name AS bill_firm_name, '
           "  bd.code AS billed_firm_code, "
           "  TRIM(COALESCE(i.series,'') || ' ' || i.invoice_no) "
           '    AS billed_invoice_no '
           'FROM sales_order o JOIN ledger p ON p.id=o.party_id '
           'LEFT JOIN company bc ON bc.id=p.home_company '
           'LEFT JOIN company bd ON bd.id=o.billed_company '
           'LEFT JOIN invoice i ON i.id=o.billed_invoice '
           'WHERE o.company_id=?')
    args = [cid]
    if q.get('status'):
        sql += ' AND o.status=?'
        args.append(q['status'])
    if q.get('party_id'):
        sql += ' AND o.party_id=?'
        args.append(qint(q['party_id'], 'party'))
    if q.get('from'):
        sql += ' AND o.order_date>=?'
        args.append(q['from'])
    if q.get('to'):
        sql += ' AND o.order_date<=?'
        args.append(q['to'])
    if q.get('search'):
        where, wargs = contains(['o.order_no', 'p.name'], q['search'])
        sql += ' AND ' + where
        args += wargs
    sql += ' ORDER BY o.order_date DESC, o.id DESC'
    if q.get('limit'):
        sql += ' LIMIT %d' % qlimit(q['limit'], 200)
    return rows(conn.execute(sql, args))


def get_order(conn, cid, oid):
    o = one(conn.execute(
        'SELECT o.*, p.name AS party_name, p.city AS party_city, '
        '  p.state AS party_state, p.gstin AS party_gstin, '
        '  p.phone AS party_phone, p.home_company AS party_bill_company, '
        '  bc.name AS bill_firm_name, bc.code AS bill_firm_code, '
        "  bd.name AS billed_firm_name, "
        "  TRIM(COALESCE(i.series,'') || ' ' || i.invoice_no) "
        '    AS billed_invoice_no '
        'FROM sales_order o JOIN ledger p ON p.id=o.party_id '
        'LEFT JOIN company bc ON bc.id=p.home_company '
        'LEFT JOIN company bd ON bd.id=o.billed_company '
        'LEFT JOIN invoice i ON i.id=o.billed_invoice '
        'WHERE o.id=? AND o.company_id=?', (oid, cid)))
    if not o:
        raise ApiError('Order not found.', 404)
    o['lines'] = rows(conn.execute(
        'SELECT * FROM sales_order_item WHERE order_id=? ORDER BY id', (oid,)))
    o['company'] = get_company(conn, cid)
    # Every challan and bill raised from it, wherever it sits.
    o['documents'] = [dict(r, no=docno(r)) for r in rows(conn.execute(
        'SELECT i.id, i.company_id, i.doc_type, i.series, i.invoice_no, '
        '  i.invoice_date, i.grand_total, c.code AS firm_code, '
        '  c.name AS firm_name, c.tax_mode FROM invoice i '
        'JOIN company c ON c.id=i.company_id WHERE i.order_id=? '
        '   OR i.id=? ORDER BY i.invoice_date, i.id',
        (oid, o['billed_invoice'] or 0)))]
    return o


def save_order(conn, cid, oid, data):
    if not data.get('party_id'):
        raise ApiError('Select a party for this order.')
    party_id = qint(data['party_id'], 'party')

    # The order book belongs to one firm, but the customer may have been
    # picked while another firm was on screen. Follow that customer over to
    # the order firm's own record so the order never points at a party in
    # somebody else's books.
    party = one(conn.execute('SELECT * FROM ledger WHERE id=?', (party_id,)))
    if not party:
        raise ApiError('The customer on this order no longer exists.', 404)
    if party['company_id'] != cid:
        party_id = counterpart(conn, party, cid)['id']
    date = (data.get('order_date') or '').strip()
    if not date:
        raise ApiError('Order date is required.')

    is_igst = resolve_igst(conn, cid, party_id)
    lines, tot = compute_lines(data.get('lines'), is_igst, tax_mode(conn, cid))
    if not lines:
        raise ApiError('Add at least one line item.')
    tax_total = r2(tot['cgst'] + tot['sgst'] + tot['igst'] + tot['cess'])

    head = dict(
        company_id=cid, order_date=date, party_id=party_id,
        due_date=(data.get('due_date') or '').strip(),
        status=(data.get('status') or 'open'),
        remarks=(data.get('remarks') or '').strip(),
        subtotal=tot['taxable'], tax_total=tax_total,
        grand_total=r2(tot['taxable'] + tax_total))

    if oid:
        sets = ', '.join('%s=?' % k for k in head)
        conn.execute('UPDATE sales_order SET %s WHERE id=? AND company_id=?'
                     % sets, list(head.values()) + [oid, cid])
        conn.execute('DELETE FROM sales_order_item WHERE order_id=?', (oid,))
    else:
        no = (data.get('order_no') or '').strip()
        head['order_no'] = no or db.next_number(conn, cid, 'ORDER',
                                                db.fy_of(date))
        cols = ','.join(head.keys())
        ph = ','.join('?' * len(head))
        try:
            cur = conn.execute('INSERT INTO sales_order(%s) VALUES (%s)'
                               % (cols, ph), list(head.values()))
        except sqlite3.IntegrityError:
            raise ApiError('Order number "%s" already exists.' % head['order_no'])
        oid = cur.lastrowid
        if no:
            db.sync_counter(conn, cid, 'ORDER', db.fy_of(date), no)

    conn.executemany(
        'INSERT INTO sales_order_item(order_id, item_id, description, hsn,'
        ' unit, thickness, cutting_size, item_detail,'
        ' qty, weight, by_weight, incl_price, rate, discount_pct,'
        ' gst_rate, taxable, tax_amt, line_total) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        [(oid, ln['item_id'], ln['description'], ln['hsn'], ln['unit'],
          ln['thickness'], ln['cutting_size'], ln['item_detail'],
          ln['qty'], ln['weight'], ln['by_weight'], ln['incl_price'],
          ln['rate'], ln['discount_pct'], ln['gst_rate'], ln['taxable'],
          r2(ln['cgst'] + ln['sgst'] + ln['igst'] + ln['cess']),
          ln['line_total']) for ln in lines])
    conn.commit()
    return get_order(conn, cid, oid)


def delete_order(conn, cid, oid, cascade=False, keep=False):
    """Delete an order.

    One that has already been challaned or billed cannot go on its own.
    `cascade` takes the challan and the bill made from it along, wherever
    they sit -- that is the only way to undo an order end to end without
    switching firms twice.
    """
    o = one(conn.execute('SELECT * FROM sales_order WHERE id=? AND company_id=?',
                         (oid, cid)))
    if not o:
        raise ApiError('Order not found.', 404)
    docs = rows(conn.execute(
        'SELECT i.id, i.series, i.invoice_no, c.name AS firm, c.tax_mode '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        'WHERE i.order_id=? ORDER BY i.id', (oid,)))
    if docs and not cascade:
        named = ['%s %s in %s'
                 % ('challan' if d['tax_mode'] == 'none' else 'bill',
                    docno(d), d['firm']) for d in docs]
        raise ApiError(
            'Order %s already has %s.' % (o['order_no'], ' and '.join(named)),
            409, {'cascade': True, 'blocked_by': named})

    gone = []
    for d in docs:
        # A challan takes its bill with it, so by the time the loop reaches
        # the bill its row is already gone and the call is a no-op. The order
        # is this function's own to delete, so the chain leaves it alone.
        gone += purge_chain(conn, d['id'], keep_orders=True, record=keep)
    conn.execute('DELETE FROM sales_order_item WHERE order_id=?', (oid,))
    conn.execute('DELETE FROM sales_order WHERE id=? AND company_id=?',
                 (oid, cid))
    conn.commit()
    return {'ok': True, 'deleted': gone}


def order_to_challan(conn, cid, oid):
    """Turn an order into a challan draft, in the firm orders are taken in.

    Orders and challans both belong to the no-tax order firm. The GST bill
    comes later, off the challan, from whichever official firm owns the
    customer -- so the challan never leaves that firm.
    """
    o = get_order(conn, cid, oid)
    if o['status'] == 'cancelled':
        raise ApiError('Order %s is cancelled.' % o['order_no'])
    if o['status'] in ('challaned', 'invoiced'):
        raise ApiError('Order %s already has a challan.' % o['order_no'])

    target = order_company_id(conn, cid)
    firm = get_company(conn, target)
    party = one(conn.execute('SELECT * FROM ledger WHERE id=?', (o['party_id'],)))
    if not party:
        raise ApiError('The customer on this order no longer exists.', 404)
    tgt_party = party if target == cid else counterpart(conn, party, target)

    lines = []
    for l in o['lines']:
        twin = (one(conn.execute('SELECT * FROM item WHERE id=?', (l['item_id'],)))
                if (target == cid and l['item_id'])
                else counterpart_item(conn, l['item_id'], target))
        lines.append(dict(
            item_id=twin['id'] if twin else None, description=l['description'],
            hsn=l['hsn'] or (twin['hsn'] if twin else ''), unit=l['unit'],
            qty=l['qty'], weight=l['weight'], by_weight=l['by_weight'],
            incl_price=l['incl_price'], rate=l['rate'],
            discount_pct=l['discount_pct'],
            # A challan carries no tax; the rate is settled on the bill.
            gst_rate=0.0, cess_rate=0.0, item_detail=l.get('item_detail') or '',
            # How the sheet is cut is decided on the order and has to reach
            # the workshop, so it rides along to the challan and the bill.
            thickness=l.get('thickness') or '',
            cutting_size=l.get('cutting_size') or ''))
    conn.commit()
    return dict(
        doc_type='SALE', company_id=target, company_code=firm['code'],
        company_name=firm['name'], party_id=tgt_party['id'],
        party_name=tgt_party['name'], order_id=o['id'], order_company=cid,
        order_no=o['order_no'], ref_no=o['order_no'], remarks=o['remarks'],
        lines=lines)


def order_to_invoice(conn, cid, oid, target_cid=None):
    """Turn an order into an invoice draft for the customer's billing firm.

    Orders are taken in the order firm; the tax bill is raised by whichever
    official firm owns that customer, so the draft comes back pointed at a
    different company than the order lives in.
    """
    o = get_order(conn, cid, oid)
    if o['status'] == 'invoiced':
        raise ApiError('Order %s has already been billed.' % o['order_no'])
    if o['status'] == 'challaned':
        raise ApiError('Order %s already has a challan -- bill that challan '
                       'rather than the order.' % o['order_no'])
    if o['status'] == 'cancelled':
        raise ApiError('Order %s is cancelled.' % o['order_no'])

    party = one(conn.execute('SELECT * FROM ledger WHERE id=?', (o['party_id'],)))
    if not party:
        raise ApiError('The customer on this order no longer exists.', 404)

    target = qint(target_cid, 'firm') if target_cid else party['home_company']
    if not target:
        raise ApiError('No billing firm is set for "%s". Set one on the '
                       'Customer Allocation screen, then bill the order.'
                       % party['name'])
    firm = get_company(conn, target)
    if firm.get('tax_mode') == 'none':
        raise ApiError('"%s" does not raise tax bills. Pick one of the '
                       'official firms.' % firm['name'])

    tgt_party = counterpart(conn, party, target)
    order_firm = get_company(conn, cid)
    lines = []
    for l in o['lines']:
        twin = counterpart_item(conn, l['item_id'], target)
        # An order taken in the no-tax firm carries no GST rate, so pick the
        # billing firm's rate for the item back up.
        gst = l['gst_rate']
        cess = 0.0
        if not gst and order_firm.get('tax_mode') == 'none' and twin:
            gst = twin['gst_rate']
            cess = twin['cess_rate']
        lines.append(dict(
            item_id=twin['id'] if twin else None, description=l['description'],
            hsn=l['hsn'] or (twin['hsn'] if twin else ''), unit=l['unit'],
            qty=l['qty'], weight=l['weight'], by_weight=l['by_weight'],
            incl_price=l['incl_price'], rate=l['rate'],
            discount_pct=l['discount_pct'], gst_rate=gst, cess_rate=cess,
            item_detail=l.get('item_detail') or '',
            thickness=l.get('thickness') or '',
            cutting_size=l.get('cutting_size') or ''))
    conn.commit()
    return dict(
        doc_type='SALE', company_id=target, company_code=firm['code'],
        company_name=firm['name'], party_id=tgt_party['id'],
        party_name=tgt_party['name'], order_id=o['id'], order_company=cid,
        order_no=o['order_no'], ref_no=o['order_no'], remarks=o['remarks'],
        lines=lines)


def _bill_lines(conn, ch, firm, prior=None):
    """A challan's lines repeated as bill lines, each with a GST rate found.

    The challan carries no rate -- its firm is outside GST -- so for a line the
    bill has not seen before the rate comes from the item as the billing firm
    holds it, then as the order firm holds it, then that firm's default.

    Where the bill already has that line, its own rate is kept exactly as it
    stands, nil included: a stored rate is a decision somebody made on the
    bill, and nil-rated and exempt goods are real. Reading a zero as "nothing
    set" and re-deriving would quietly put 18% on an exempt line.

    Lines are matched to the bill's by description so inserting or reordering
    one does not shift every rate along, and by position when that fails, so
    renaming a line on the bill does not lose its rate either.
    """
    target = firm['id']
    prior = list(prior or [])
    by_name = {}
    for l in prior:
        by_name.setdefault((l['description'] or '').strip().lower(), l)
    lines = []
    for i, l in enumerate(ch['lines']):
        twin = counterpart_item(conn, l['item_id'], target)
        src = one(conn.execute('SELECT gst_rate, cess_rate FROM item WHERE id=?',
                               (l['item_id'],))) if l['item_id'] else None
        was = by_name.get((l['description'] or '').strip().lower())
        if was is None and len(prior) == len(ch['lines']):
            was = prior[i]
        if was is not None:
            gst = float(was['gst_rate'] or 0)
            cess = float(was['cess_rate'] or 0)
        else:
            gst = ((twin or {}).get('gst_rate') or 0) \
                or ((src or {}).get('gst_rate') or 0) \
                or float(firm.get('default_gst') or 18)
            cess = ((twin or {}).get('cess_rate') or 0) \
                or ((src or {}).get('cess_rate') or 0)
        lines.append(dict(
            item_id=twin['id'] if twin else None, description=l['description'],
            hsn=l['hsn'] or (twin['hsn'] if twin else ''), unit=l['unit'],
            qty=l['qty'], weight=l['weight'], by_weight=l['by_weight'],
            incl_price=0, rate=l['rate'], discount_pct=l['discount_pct'],
            gst_rate=gst, cess_rate=cess, item_detail=l['item_detail'] or '',
            thickness=l['thickness'] or '', cutting_size=l['cutting_size'] or ''))
    return lines


def resync_bill(conn, challan_cid, challan_id, who=''):
    """Carry an edited challan through to the GST bill raised from it.

    The bill is the challan plus tax. A challan changed after it has been
    billed would otherwise leave the two disagreeing, and the invariant the
    whole hand-off rests on quietly stops holding -- the bill would still say
    it came from this challan while no longer being 1.18 times it.

    The bill keeps what is its own -- its number, its series, its date, the
    firm it was raised by -- and takes everything that decides the money from
    the challan again. Returns what was updated, or None if nothing was.
    """
    bill = dependent_bill(conn, challan_id)
    if not bill:
        return None
    tgt = bill['company_id']
    firm = get_company(conn, tgt)
    ch = get_invoice(conn, challan_cid, challan_id)
    old = get_invoice(conn, tgt, bill['id'])

    party = one(conn.execute('SELECT * FROM ledger WHERE id=?', (ch['party_id'],)))
    if not party:
        raise ApiError('The customer on this challan no longer exists.', 404)
    # Following the customer keeps the bill pointed at the right party when the
    # challan is moved to another one. It stays in the firm that raised it --
    # moving a bill between firms means deleting it and billing again.
    tgt_party = counterpart(conn, party, tgt)

    fresh = save_invoice(conn, tgt, bill['id'], dict(
        doc_type=old['doc_type'], series=old['series'],
        invoice_type=old['invoice_type'], invoice_date=old['invoice_date'],
        party_id=tgt_party['id'], sales_ac_id=old['sales_ac_id'],
        dept=ch['dept'] or old['dept'],
        eway_no=ch['eway_no'] or old['eway_no'], rcm=old['rcm'],
        order_id=ch['order_id'] or old['order_id'],
        from_challan=ch['id'], ref_no=docno(ch),
        driver_name=ch['driver_name'], payment_terms=ch['payment_terms'],
        transporter=ch['transporter'], vehicle_no=ch['vehicle_no'],
        lr_no=ch['lr_no'], lr_date=ch['lr_date'], cases=ch['cases'],
        remarks=ch['remarks'], cutting=ch['cutting'],
        transport=ch['transport'], loading=ch['loading'],
        # The challan's own round off rides into the taxable value, and the
        # bill is left unrounded, so it lands on the challan total plus GST.
        taxable_adj=ch['round_off'], round_off='',
        lines=_bill_lines(conn, ch, firm, old['lines'])),
        who=who, action='rebuilt', note=docno(ch))
    return dict(id=bill['id'], company_id=tgt, firm=firm['name'],
                number=docno(fresh), grand_total=r2(fresh['grand_total']),
                challan_total=r2(ch['grand_total']))


def challan_to_bill(conn, cid, iid, target_cid=None):
    """Turn a challan into the GST bill draft.

    The bill repeats the challan exactly -- same weights, same per-kilo rates,
    same cutting, transport and loading -- and then adds GST on the whole
    thing. So the bill is simply the challan plus tax, with nothing re-derived.
    """
    ch = get_invoice(conn, cid, iid)
    if ch['billed_invoice']:
        raise ApiError('Challan %s has already been billed.' % ch['invoice_no'])

    party = one(conn.execute('SELECT * FROM ledger WHERE id=?', (ch['party_id'],)))
    if not party:
        raise ApiError('The customer on this challan no longer exists.', 404)
    target = qint(target_cid, 'firm') if target_cid else party['home_company']
    if not target:
        raise ApiError('No billing firm is set for "%s". Set one on the '
                       'Customer Allocation screen, then bill the challan.'
                       % party['name'])
    firm = get_company(conn, target)
    if firm.get('tax_mode') == 'none':
        raise ApiError('"%s" does not raise tax bills. Pick one of the '
                       'official firms.' % firm['name'])
    if target == cid:
        raise ApiError('This challan is already in %s.' % firm['name'])

    tgt_party = counterpart(conn, party, target)
    lines = _bill_lines(conn, ch, firm)
    conn.commit()
    return dict(
        doc_type='SALE', company_id=target, company_code=firm['code'],
        company_name=firm['name'], party_id=tgt_party['id'],
        party_name=tgt_party['name'], from_challan=ch['id'],
        order_id=ch['order_id'],
        challan_company=cid, challan_no=docno(ch), ref_no=docno(ch),
        challan_total=r2(ch['grand_total']), remarks=ch['remarks'],
        # Everything the challan recorded about the movement of the goods
        # belongs on the bill for them too. The department it was cut in and
        # the e-way number were being dropped here and typed again.
        dept=ch['dept'], eway_no=ch['eway_no'],
        invoice_type=ch['invoice_type'],
        driver_name=ch['driver_name'], payment_terms=ch['payment_terms'],
        transporter=ch['transporter'], vehicle_no=ch['vehicle_no'],
        lr_no=ch['lr_no'], lr_date=ch['lr_date'], cases=ch['cases'],
        # The charges come across untouched and get taxed with the goods.
        cutting=ch['cutting'], transport=ch['transport'],
        loading=ch['loading'], taxable_adj=ch['round_off'],
        lines=lines)


# --------------------------------------------------------------- vouchers

def list_vouchers(conn, cid, q):
    sql = ('SELECT v.*, p.name AS party_name, b.name AS bank_name '
           'FROM voucher v LEFT JOIN ledger p ON p.id=v.party_id '
           'LEFT JOIN ledger b ON b.id=v.bank_id WHERE v.company_id=?')
    args = [cid]
    if q.get('kind'):
        sql += ' AND v.kind=?'
        args.append(q['kind'])
    if q.get('party_id'):
        sql += ' AND v.party_id=?'
        args.append(qint(q['party_id'], 'party'))
    if q.get('from'):
        sql += ' AND v.voucher_date>=?'
        args.append(q['from'])
    if q.get('to'):
        sql += ' AND v.voucher_date<=?'
        args.append(q['to'])
    if q.get('search'):
        where, wargs = contains(['v.voucher_no', 'p.name', 'v.ref_no',
                                 'v.remarks'], q['search'])
        sql += ' AND ' + where
        args += wargs
    sql += ' ORDER BY v.voucher_date DESC, v.id DESC'
    if q.get('limit'):
        sql += ' LIMIT %d' % qlimit(q['limit'], 200)
    data = rows(conn.execute(sql, args))
    # Journals and contras carry their lines on the list screen. Fetching them
    # per voucher meant a query per row; one pass over the whole page's lines
    # costs a single read however long the list is.
    wants = [v['id'] for v in data if v['kind'] in ('JOURNAL', 'CONTRA')]
    if wants:
        by_voucher = {}
        for chunk in _chunks(wants, 400):
            for r in rows(conn.execute(
                    'SELECT vl.*, l.name AS ledger_name FROM voucher_line vl '
                    'JOIN ledger l ON l.id=vl.ledger_id WHERE vl.voucher_id '
                    'IN (%s) ORDER BY vl.id' % ','.join('?' * len(chunk)),
                    chunk)):
                by_voucher.setdefault(r['voucher_id'], []).append(r)
        for v in data:
            if v['kind'] in ('JOURNAL', 'CONTRA'):
                v['lines'] = by_voucher.get(v['id'], [])
    return data


def get_voucher(conn, cid, vid):
    v = one(conn.execute(
        'SELECT v.*, p.name AS party_name, b.name AS bank_name '
        'FROM voucher v LEFT JOIN ledger p ON p.id=v.party_id '
        'LEFT JOIN ledger b ON b.id=v.bank_id '
        'WHERE v.id=? AND v.company_id=?', (vid, cid)))
    if not v:
        raise ApiError('Voucher not found.', 404)
    v['lines'] = rows(conn.execute(
        'SELECT vl.*, l.name AS ledger_name FROM voucher_line vl '
        'JOIN ledger l ON l.id=vl.ledger_id WHERE vl.voucher_id=? '
        'ORDER BY vl.id', (vid,)))
    return v


def recent_money(conn, cid):
    """The cash and bank accounts most lately written to, latest first (4
    Oct 2026: "while doing bank entry always autofill it with recent entry
    ... the entries are done bank account-wise"). A new receipt or payment
    starts on the first of them that suits the screen. Entries made in this
    app come first, in the order they were made; an account only SHREE has
    written to follows, by its latest date."""
    got = conn.execute(
        "SELECT bank_id, MAX(CASE WHEN COALESCE(source,'')='' THEN id END) mine, "
        '  MAX(voucher_date) dated FROM voucher '
        "WHERE company_id=? AND kind IN ('RECEIPT','PAYMENT') "
        '  AND bank_id IS NOT NULL GROUP BY bank_id', (cid,)).fetchall()
    got = sorted(got, key=lambda r: (r['mine'] is not None, r['mine'] or 0,
                                     r['dated'] or ''), reverse=True)
    return dict(accounts=[r['bank_id'] for r in got[:30]])


def save_voucher(conn, cid, vid, data, commit=True):
    kind = (data.get('kind') or 'RECEIPT').upper()
    if kind not in VOUCHER_KINDS:
        raise ApiError('Voucher kind must be one of %s.'
                       % ', '.join(VOUCHER_KINDS))
    date = (data.get('voucher_date') or '').strip()
    if not date:
        raise ApiError('Voucher date is required.')

    entries = []
    head = dict(company_id=cid, kind=kind, voucher_date=date, fy=db.fy_of(date),
                ref_no=(data.get('ref_no') or '').strip(),
                remarks=(data.get('remarks') or '').strip(),
                mode=(data.get('mode') or 'Cash'))
    v_lines = []

    if kind in ('RECEIPT', 'PAYMENT'):
        if not data.get('party_id'):
            raise ApiError('Select a party.')
        amount = r2(data.get('amount'))
        if amount <= 0:
            raise ApiError('Amount must be greater than zero.')
        party_id = qint(data['party_id'], 'party')
        bank_id = qint(data['bank_id'], 'ledger') if data.get('bank_id') \
            else sys_ledger(conn, cid, 'CASH')
        if not conn.execute('SELECT 1 FROM ledger WHERE id=? AND company_id=?',
                            (bank_id, cid)).fetchone():
            raise ApiError('That cash/bank ledger is not in this company.')
        # The other side can be any account of this firm -- a customer, a
        # supplier, another bank, salary, an expense -- but not one of another
        # firm's books, and not the account the money moves through: that
        # posts a debit and a credit to one ledger and moves nothing.
        if not conn.execute('SELECT 1 FROM ledger WHERE id=? AND company_id=?',
                            (party_id, cid)).fetchone():
            raise ApiError('That account is not in this company.')
        if party_id == bank_id:
            raise ApiError('The money cannot come from and go to the same '
                           'account. Pick the account on the other side.')
        head.update(party_id=party_id, bank_id=bank_id, amount=amount)
        if kind == 'RECEIPT':
            entries = [(bank_id, amount, 0), (party_id, 0, amount)]
        else:
            entries = [(party_id, amount, 0), (bank_id, 0, amount)]
    else:
        raw = data.get('lines') or []
        total_dr = total_cr = 0.0
        for ln in raw:
            if not ln.get('ledger_id'):
                continue
            dr, cr = r2(ln.get('debit')), r2(ln.get('credit'))
            if not dr and not cr:
                continue
            if dr and cr:
                raise ApiError('A line can be either a debit or a credit, '
                               'not both.')
            v_lines.append((qint(ln['ledger_id'], 'ledger'), dr, cr,
                            (ln.get('remarks') or '').strip()))
            total_dr += dr
            total_cr += cr
        if len(v_lines) < 2:
            raise ApiError('A %s voucher needs at least two lines.'
                           % kind.lower())
        if r2(total_dr) != r2(total_cr):
            raise ApiError('Debit (%.2f) and credit (%.2f) must be equal.'
                           % (total_dr, total_cr))
        ids = [l[0] for l in v_lines]
        found = conn.execute(
            'SELECT COUNT(*) n FROM ledger WHERE company_id=? AND id IN (%s)'
            % ','.join('?' * len(ids)), [cid] + ids).fetchone()['n']
        if found != len(set(ids)):
            raise ApiError('One of those ledgers is not in this company.')
        head.update(party_id=None, bank_id=None, amount=r2(total_dr))
        entries = [(l[0], l[1], l[2]) for l in v_lines]

    was = None
    if vid:
        existing = one(conn.execute(
            'SELECT * FROM voucher WHERE id=? AND company_id=?', (vid, cid)))
        if not existing:
            raise ApiError('Voucher not found.', 404)
        was = _voucher_signature(conn, vid) \
            if existing.get('source') == 'SHREE' else None
        # Saved unchanged, an imported voucher is left as SHREE wrote it --
        # a receipt of several lines was rebuilt into one (2 Oct 2026).
        if was is not None:
            conn.execute('SAVEPOINT resave_v')
        head['voucher_no'] = existing['voucher_no']
        sets = ', '.join('%s=?' % k for k in head)
        conn.execute('UPDATE voucher SET %s WHERE id=?' % sets,
                     list(head.values()) + [vid])
        conn.execute('DELETE FROM voucher_line WHERE voucher_id=?', (vid,))
    else:
        supplied = (data.get('voucher_no') or '').strip()
        head['voucher_no'] = supplied or db.next_number(conn, cid, kind,
                                                        db.fy_of(date))
        cols = ','.join(head.keys())
        ph = ','.join('?' * len(head))
        try:
            cur = conn.execute('INSERT INTO voucher(%s) VALUES (%s)'
                               % (cols, ph), list(head.values()))
        except sqlite3.IntegrityError:
            raise ApiError('Voucher number "%s" already exists.'
                           % head['voucher_no'])
        vid = cur.lastrowid
        if supplied:
            db.sync_counter(conn, cid, kind, db.fy_of(date), supplied)

    conn.executemany('INSERT INTO voucher_line(voucher_id, ledger_id, debit, '
                     'credit, remarks) VALUES (?,?,?,?,?)',
                     [(vid, lid_, dr, cr, note)
                      for lid_, dr, cr, note in v_lines])
    if was is not None and _voucher_signature(conn, vid) == was:
        conn.execute('ROLLBACK TO resave_v')
        conn.execute('RELEASE resave_v')
        if commit:
            conn.commit()
        return get_voucher(conn, cid, vid)
    if was is not None and _voucher_signature(conn, vid) != was:
        _adopt_edited(conn, 'voucher', existing, existing['kind'],
                      doc_key(existing['kind'], existing['fy'], '',
                              existing['voucher_no']),
                      _voucher_fingerprint(conn, existing))

    # SHREE's narration: the remarks, else the Ch/DD reference, else nothing.
    # The word "Receipt" used to stand in, and the account then read
    # "Rcpt Chq/Ref No: Receipt" with the cheque or NEFT typed nowhere on it.
    repost(conn, cid, kind, vid, date, head['voucher_no'], entries,
           head['remarks'] or head.get('ref_no') or '')
    # A month's salary is a journal per person, saved together or not at all.
    if commit:
        conn.commit()
    return get_voucher(conn, cid, vid)


def delete_voucher(conn, cid, vid, keep=False):
    v = one(conn.execute('SELECT * FROM voucher WHERE id=? AND company_id=?',
                         (vid, cid)))
    if not v:
        raise ApiError('Voucher not found.', 404)
    # From SHREE: back at the next update, unless asked to be kept deleted.
    if keep and v.get('source') == 'SHREE':
        record_rule(conn, 'delete_doc', cid,
                    doc_key(v['kind'], v['fy'], '', v['voucher_no']),
                    dict(_voucher_fingerprint(conn, v), table='voucher'),
                    'Deleted in this app')
    # A bill's GST journal deleted by hand: the bill stops pointing at it,
    # and makes a fresh one the next time it is saved.
    conn.execute('UPDATE invoice SET gst_voucher=NULL WHERE gst_voucher=?', (vid,))
    conn.execute('DELETE FROM posting WHERE doc_kind=? AND doc_id=?',
                 (v['kind'], vid))
    conn.execute('DELETE FROM voucher_line WHERE voucher_id=?', (vid,))
    conn.execute('DELETE FROM voucher WHERE id=?', (vid,))
    conn.commit()
    return {'ok': True}


# --------------------------------------------------------- staff & salary
#
# SHREE keeps everyone paid a salary as an account under SALARY PAYABLE --
# LABOUR BHOLA, SAHIL SALARY MONTHLY -- and the shop keeps it one way:
#
#   * through the month, advances ("UPAD") go out as payments against the
#     person's account, from cash or the bank;
#   * on the last day, a journal books what the month earned: SALARY EXP.
#     debited, the person's account credited, the working written as the
#     narration -- "284 X 71.25 =20235 AUG 2026" for hours at a rate,
#     "FIX 21000" for a fixed month.
#
# So the account's balance says everything. In credit, salary is still owed
# to them; in debit, they have had more than they have earned. The staff
# table holds only how each person is paid; every figure is read from the
# books, so an entry made in SHREE counts exactly as one made here.

PAY_BASES = ('month', 'day', 'hour')
STAFF_TEXT = ('mobile', 'joined', 'left_on', 'notes')
STAFF_IDLE_DAYS = 365       # no entry for this long and nothing owed: former
_RATE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*[xX×*/]\s*(\d+(?:\.\d+)?)')
_FIX_RE = re.compile(r'\bFIX\w*\D{0,3}(\d+(?:\.\d+)?)', re.I)


def _today():
    return datetime.date.today().isoformat()


def _amount(v):
    """A number from a form box; blank or nonsense is nought."""
    try:
        return float(str(v).replace(',', '').strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def staff_key(name):
    """A person is known by their account's name, as a SHREE update knows
    an account."""
    return re.sub(r'\s+', ' ', (name or '').strip()).upper()


def month_bounds(month):
    """'2026-09' -> ('2026-09-01', '2026-09-30', 30)."""
    m = re.match(r'^(\d{4})-(\d{2})$', (month or '').strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ApiError('Give the month as 2026-09.')
    first = datetime.date(int(m.group(1)), int(m.group(2)), 1)
    after = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    last = after - datetime.timedelta(days=1)
    return first.isoformat(), last.isoformat(), last.day


def _month_name(month):
    y, m = month.split('-')
    return '%s %s' % (datetime.date(int(y), int(m), 1).strftime('%b').upper(), y)


def _is_staff_account(l):
    """On the list without being put there: filed under SALARY PAYABLE, as
    SHREE files everyone it books a salary for. The family's "... (SALARY)"
    accounts sit among the receivables and carry the family's own money in
    and out -- sixty-six lakh on one of them -- so they are offered first
    when somebody is added, never counted in by themselves."""
    return 'SALARY PAYABLE' in (l['group_name'] or '').upper()


def _staff_list(conn, cid, everyone=False):
    """[(account, settings or None)] for the people on the staff list, or
    with `everyone` for every account that could be put on it."""
    kept = {r['name']: dict(r) for r in conn.execute(
        'SELECT * FROM staff WHERE company_id=?', (cid,))}
    out = []
    for l in rows(conn.execute(
            'SELECT l.id, l.name, l.phone, l.city, l.active, l.source, '
            'l.group_id, g.name AS group_name, g.nature FROM ledger l '
            'JOIN account_group g ON g.id=l.group_id '
            'WHERE l.company_id=? AND l.system_key IS NULL ORDER BY l.name',
            (cid,))):
        s = kept.get(staff_key(l['name']))
        listed = s['listed'] if s else _is_staff_account(l)
        if listed or everyone:
            out.append((l, s if listed or s else None))
    return out


def _staff_moves(conn, cid, ids, frm, to):
    """What moved on each account between two dates, sorted the way the
    shop reads it: salary booked (journal credits), paid out (payments),
    handed back (receipts) and anything else, net. The balance carried into
    a year that was never backed up is not a movement; it is opening."""
    out = {i: dict(salary=0.0, paid=0.0, back=0.0, other=0.0, carried=0.0,
                   entries=0) for i in ids}
    for chunk in _chunks(list(ids), 400):
        for r in conn.execute(
                'SELECT ledger_id, doc_kind, SUM(debit) d, SUM(credit) c, '
                'COUNT(*) n FROM posting WHERE company_id=? AND date>=? AND '
                'date<=? AND ledger_id IN (%s) GROUP BY ledger_id, doc_kind'
                % ','.join('?' * len(chunk)), [cid, frm, to] + chunk):
            m = out[r['ledger_id']]
            d, c = float(r['d'] or 0), float(r['c'] or 0)
            kind = r['doc_kind']
            if kind == 'OPENING':
                m['carried'] += d - c
                continue
            m['entries'] += r['n']
            if kind == 'JOURNAL':
                m['salary'] += c
                m['other'] += d
            elif kind == 'PAYMENT':
                m['paid'] += d
                m['other'] -= c
            elif kind == 'RECEIPT':
                m['back'] += c
                m['other'] += d
            else:
                m['other'] += d - c
    return out


def _salary_journals(conn, cid, ids, frm, to):
    """{account: [salary journals between the dates, newest first]}."""
    out = {}
    for chunk in _chunks(list(ids), 400):
        for r in conn.execute(
                'SELECT p.ledger_id, p.date, p.credit, p.doc_id, p.doc_no, '
                "  COALESCE(NULLIF(p.narration, ''), v.remarks, '') note, "
                "  COALESCE(v.source, '') source "
                'FROM posting p LEFT JOIN voucher v ON v.id=p.doc_id '
                "WHERE p.company_id=? AND p.doc_kind='JOURNAL' AND p.credit>0 "
                'AND p.date>=? AND p.date<=? AND p.ledger_id IN (%s) '
                'ORDER BY p.date DESC, p.id DESC' % ','.join('?' * len(chunk)),
                [cid, frm or '0000', to] + chunk):
            out.setdefault(r['ledger_id'], []).append(dict(
                date=r['date'], amount=r2(r['credit']), voucher_id=r['doc_id'],
                no=r['doc_no'], note=(r['note'] or '').strip(),
                shree=r['source'] == 'SHREE'))
    return out


def _usual_bank(conn, cid, ids):
    """{account: the cash or bank account its payments of the last year
    came from most often} -- where the next advance is likely paid from."""
    since = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
    out, best = {}, {}
    for chunk in _chunks(list(ids), 400):
        for r in conn.execute(
                "SELECT party_id, bank_id, COUNT(*) n FROM voucher WHERE "
                "company_id=? AND kind='PAYMENT' AND voucher_date>=? AND "
                'bank_id IS NOT NULL AND party_id IN (%s) GROUP BY party_id, '
                'bank_id' % ','.join('?' * len(chunk)), [cid, since] + chunk):
            if r['n'] > best.get(r['party_id'], 0):
                best[r['party_id']] = r['n']
                out[r['party_id']] = r['bank_id']
    return out


def _pay_from_books(last):
    """(basis, rate, units, how) read off the latest salary journals.

    The narration carries the working -- 284 X 71.25 = 20235 -- and the two
    numbers are taken only where they multiply out to what was booked. More
    than 31 of something in a month is hours; up to 31, days. "FIX 21000",
    or the same amount twice running, is a fixed month.
    """
    if not last:
        return '', 0.0, 0.0, ''
    amt, note = last[0]['amount'], last[0]['note']
    y, m, d = last[0]['date'].split('-')
    when = 'salary of %s-%s-%s' % (d, m, y)
    for m in _RATE_RE.finditer(note):
        units, rate = float(m.group(1)), float(m.group(2))
        if units and rate and abs(units * rate - amt) <= max(2.0, amt * 0.005):
            return ('day' if units <= 31 else 'hour'), rate, units, when
    m = _FIX_RE.search(note)
    if m:
        return 'month', float(m.group(1)), 0.0, when
    same = sum(1 for x in last[:3] if abs(x['amount'] - amt) < 0.5)
    return 'month', amt, 0.0, when + (' (same as the month before)'
                                      if same >= 2 else ' (a guess)')


def _staff_row(l, s, last):
    """One person: their account, how they are paid, and whether that was
    set here or read off the books."""
    b_basis, b_rate, b_units, how = _pay_from_books(last)
    basis = (s or {}).get('pay_basis') or b_basis or 'month'
    rate = r2((s or {}).get('rate')) if (s or {}).get('pay_basis') else r2(b_rate)
    return dict(
        ledger_id=l['id'], name=l['name'], group_name=l['group_name'],
        source=l['source'] or '', active=l['active'],
        pay_basis=basis, rate=rate,
        pay_from='set here' if (s or {}).get('pay_basis') else (how or ''),
        last_units=b_units,
        mobile=(s or {}).get('mobile') or l['phone'] or '',
        joined=(s or {}).get('joined') or '', left_on=(s or {}).get('left_on') or '',
        notes=(s or {}).get('notes') or '',
        added=bool(s) and not _is_staff_account(l))


def _day_before(iso):
    return (datetime.date(*map(int, iso.split('-')))
            - datetime.timedelta(days=1)).isoformat()


def report_staff(conn, cid, q):
    """Everyone on the staff list, and one month of their account."""
    month = (q.get('month') or '').strip() or _today()[:7]
    frm, to, days = month_bounds(month)
    people = _staff_list(conn, cid)
    ids = [l['id'] for l, _ in people]
    opening = ledger_balances(conn, cid, _day_before(frm))
    closing = ledger_balances(conn, cid, to)
    moves = _staff_moves(conn, cid, ids, frm, to)
    loose = {k: v for k, v in _loose_ids(conn, cid).items() if k in set(ids)}
    loose_moves = _staff_moves(conn, cid, list(loose.values()), frm, to)
    booked = _salary_journals(conn, cid, ids, frm, to)
    history = _salary_journals(conn, cid, ids, '', to)
    last_move = {}
    for chunk in _chunks(ids, 400):
        for r in conn.execute(
                "SELECT ledger_id, MAX(date) d FROM posting WHERE company_id=? "
                "AND doc_kind<>'OPENING' AND date<=? AND ledger_id IN (%s) "
                'GROUP BY ledger_id' % ','.join('?' * len(chunk)),
                [cid, to] + chunk):
            last_move[r['ledger_id']] = r['d']
    banks = _usual_bank(conn, cid, ids)
    idle_since = (datetime.date(*map(int, to.split('-')))
                  - datetime.timedelta(days=STAFF_IDLE_DAYS)).isoformat()
    show_former = bool(q.get('former'))
    out, hidden = [], 0
    tot = dict(salary=0.0, paid=0.0, back=0.0, due=0.0, advance=0.0,
               due_count=0, advance_count=0, posted=0, loose_given=0.0,
               loose_taken=0.0, loose_out=0.0, loose_out_count=0,
               loose_owed=0.0, loose_owed_count=0)
    for l, s in people:
        row = _staff_row(l, s, history.get(l['id'], [])[:3])
        m = moves[l['id']]
        row.update(
            opening=r2(opening.get(l['id'], 0.0) + m['carried']),
            salary=r2(m['salary']), paid=r2(m['paid']), back=r2(m['back']),
            other=r2(m['other']), closing=r2(closing.get(l['id'], 0.0)),
            entries=m['entries'], booked=booked.get(l['id'], []),
            bank_id=banks.get(l['id']),
            loose_id=loose.get(l['id']),
            last_salary=(history.get(l['id']) or [None])[0],
            last_entry=last_move.get(l['id'], ''))
        # Loose money: given is what went out to them, taken is what came
        # back -- in cash, or cut from their salary.
        lm = loose_moves.get(row['loose_id']) if row['loose_id'] else None
        row.update(
            loose_opening=r2(opening.get(row['loose_id'], 0.0)
                             + (lm['carried'] if lm else 0.0)),
            loose_given=r2(lm['paid'] if lm else 0.0),
            loose_taken=r2((lm['back'] + lm['salary']) if lm else 0.0),
            loose_closing=r2(closing.get(row['loose_id'], 0.0)
                             if row['loose_id'] else 0.0))
        # Former: left, or long quiet with nothing owed either way. Somebody
        # just taken on has no entries at all yet, and is not former.
        row['former'] = bool(row['left_on'] and row['left_on'] <= to) or (
            abs(row['closing']) < 0.5 and abs(row['loose_closing']) < 0.5
            and not m['entries'] and not (lm and lm['entries'])
            and bool(row['last_entry']) and row['last_entry'] < idle_since
            and not (row['joined'] and row['joined'] >= idle_since))
        if row['former'] and not show_former:
            hidden += 1
            continue
        out.append(row)
        tot['salary'] += row['salary']
        tot['paid'] += row['paid']
        tot['back'] += row['back']
        tot['posted'] += 1 if row['booked'] else 0
        # Owed to somebody and owed by somebody are two different facts,
        # and never netted -- as an overdraft is never netted against cash.
        if row['closing'] < -0.005:
            tot['due'] += -row['closing']
            tot['due_count'] += 1
        elif row['closing'] > 0.005:
            tot['advance'] += row['closing']
            tot['advance_count'] += 1
        tot['loose_given'] += row['loose_given']
        tot['loose_taken'] += row['loose_taken']
        if row['loose_closing'] > 0.005:
            tot['loose_out'] += row['loose_closing']
            tot['loose_out_count'] += 1
        elif row['loose_closing'] < -0.005:
            tot['loose_owed'] += -row['loose_closing']
            tot['loose_owed_count'] += 1
    for k in ('salary', 'paid', 'back', 'due', 'advance', 'loose_given',
              'loose_taken', 'loose_out', 'loose_owed'):
        tot[k] = r2(tot[k])
    return dict(month=month, month_name=_month_name(month), start=frm, end=to,
                days=days, rows=out, former_hidden=hidden, totals=tot,
                expense=_salary_expense(conn, cid, ids))


def _salary_expense(conn, cid, ids):
    """The accounts a month's salary can be booked to, the usual one first:
    whichever the salary journals of the last year debited most often, else
    an expense account named for salary or wages."""
    since = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
    used = {}
    if ids:
        for chunk in _chunks(list(ids), 400):
            for r in conn.execute(
                    "SELECT o.ledger_id, COUNT(*) n FROM posting p JOIN posting o "
                    "ON o.doc_kind=p.doc_kind AND o.doc_id=p.doc_id "
                    "AND o.company_id=p.company_id AND o.debit>0 "
                    "WHERE p.company_id=? AND p.doc_kind='JOURNAL' AND "
                    "p.credit>0 AND p.date>=? AND p.ledger_id IN (%s) "
                    "GROUP BY o.ledger_id" % ','.join('?' * len(chunk)),
                    [cid, since] + chunk):
                used[r['ledger_id']] = used.get(r['ledger_id'], 0) + r['n']
    staff = set(ids)
    out = []
    for r in conn.execute(
            'SELECT l.id, l.name FROM ledger l JOIN account_group g '
            "ON g.id=l.group_id WHERE l.company_id=? AND g.nature='expense' "
            'ORDER BY l.name', (cid,)):
        name = r['name'].upper()
        if r['id'] in staff:
            continue
        if used.get(r['id']) or re.search(r'SALAR|WAGE|LABOUR|BONUS|STAFF',
                                          name):
            out.append(dict(id=r['id'], name=r['name'],
                            used=used.get(r['id'], 0)))
    out.sort(key=lambda x: (-x['used'], x['name']))
    return out


def staff_detail(conn, cid, lid, q):
    """One person over a year, month by month, with every entry."""
    found = [(l, s) for l, s in _staff_list(conn, cid, everyone=True)
             if l['id'] == lid]
    if not found:
        raise ApiError('That account is not in this firm.', 404)
    l, s = found[0]
    fy = (q.get('fy') or '').strip() or db.fy_of(_today())
    if not re.match(r'^\d{4}-\d{2}$', fy):
        raise ApiError('Give the year as 2026-27.')
    y = int(fy[:4])
    start, end = '%d-04-01' % y, '%d-03-31' % (y + 1)
    history = _salary_journals(conn, cid, [lid], '', end).get(lid, [])
    row = _staff_row(l, s, history[:3])
    row['listed'] = bool(s['listed']) if s else _is_staff_account(l)
    row['bank_id'] = _usual_bank(conn, cid, [lid]).get(lid)
    bal = ledger_balances(conn, cid, _day_before(start)).get(lid, 0.0)
    months = []
    cur = datetime.date(y, 4, 1)
    last_day = min(end, _today()) if y <= int(db.fy_of(_today())[:4]) else end
    while cur.isoformat() <= last_day:
        month = cur.isoformat()[:7]
        frm, to, _days = month_bounds(month)
        m = _staff_moves(conn, cid, [lid], frm, to)[lid]
        opening = r2(bal + m['carried'])
        bal = opening + m['paid'] - m['salary'] - m['back'] + m['other']
        months.append(dict(month=month, name=_month_name(month),
                           opening=opening, salary=r2(m['salary']),
                           paid=r2(m['paid']), back=r2(m['back']),
                           other=r2(m['other']), closing=r2(bal),
                           entries=m['entries']))
        cur = (cur.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    entries = rows(conn.execute(
        'SELECT p.id, p.date, p.doc_kind, p.doc_id, p.doc_no, p.debit, '
        "p.credit, p.narration, COALESCE(v.source, i.source, '') source "
        'FROM posting p LEFT JOIN voucher v ON v.id=p.doc_id AND p.doc_kind '
        "IN ('RECEIPT','PAYMENT','JOURNAL','CONTRA') LEFT JOIN invoice i ON "
        "i.id=p.doc_id AND p.doc_kind IN ('SALE','PURCHASE','CREDIT','DEBIT') "
        "WHERE p.company_id=? AND p.ledger_id=? AND p.doc_kind<>'OPENING' "
        'AND p.date>=? AND p.date<=? ORDER BY p.date, p.id',
        (cid, lid, start, end)))
    # The account on the other side of each entry -- the cash or bank it
    # was paid from, the expense a salary was booked to -- in one read.
    other = {}
    docs = sorted({e['doc_id'] for e in entries})
    for chunk in _chunks(docs, 400):
        for r in conn.execute(
                'SELECT p.doc_kind, p.doc_id, l.id, l.name, '
                'p.debit + p.credit amt FROM posting p JOIN ledger l '
                'ON l.id=p.ledger_id WHERE p.company_id=? AND p.ledger_id<>? '
                'AND p.doc_id IN (%s)' % ','.join('?' * len(chunk)),
                [cid, lid] + chunk):
            key = (r['doc_kind'], r['doc_id'])
            if key not in other or r['amt'] > other[key][2]:
                other[key] = (r['id'], r['name'], r['amt'])
    for e in entries:
        o = other.get((e['doc_kind'], e['doc_id']))
        e['other_id'], e['other'] = (o[0], o[1]) if o else (None, '')
        e['debit'], e['credit'] = r2(e['debit']), r2(e['credit'])
    loose = None
    loose_id = _loose_ids(conn, cid).get(lid)
    if loose_id:
        run = r2(ledger_balances(conn, cid, _day_before(start)).get(loose_id, 0.0))
        opening = run
        lines = rows(conn.execute(
            'SELECT date, doc_kind, doc_id, doc_no, debit, credit, narration '
            'FROM posting WHERE company_id=? AND ledger_id=? AND date>=? AND '
            'date<=? ORDER BY date, id', (cid, loose_id, start, end)))
        for e in lines:
            if e['doc_kind'] == 'OPENING':
                opening = r2(opening + e['debit'] - e['credit'])
            run = r2(run + e['debit'] - e['credit'])
            e['balance'] = run
            e['debit'], e['credit'] = r2(e['debit']), r2(e['credit'])
        loose = dict(ledger_id=loose_id, opening=opening,
                     entries=[e for e in lines if e['doc_kind'] != 'OPENING'],
                     given=r2(sum(e['debit'] for e in lines
                                  if e['doc_kind'] != 'OPENING')),
                     taken=r2(sum(e['credit'] for e in lines
                                  if e['doc_kind'] != 'OPENING')),
                     closing=r2(ledger_balances(conn, cid).get(loose_id, 0.0)))
    return dict(staff=row, fy=fy, months=months, entries=entries,
                closing=r2(ledger_balances(conn, cid).get(lid, 0.0)),
                loose=loose)


def _staff_settings(data, have=None):
    """What a save may set, checked."""
    have = dict(have or {})
    if 'pay_basis' in data:
        basis = (data.get('pay_basis') or '').strip().lower()
        if basis and basis not in PAY_BASES:
            raise ApiError('Pay is by the month, the day or the hour.')
        have['pay_basis'] = basis
    if 'rate' in data:
        rate = r2(data.get('rate'))
        if rate < 0:
            raise ApiError('The rate cannot be below nought.')
        have['rate'] = rate
    if have.get('pay_basis') and not have.get('rate'):
        raise ApiError('Give the rate: %s.' % dict(
            month='the salary for a whole month', day="a day's pay",
            hour="an hour's pay")[have['pay_basis']])
    for k in STAFF_TEXT:
        if k in data:
            have[k] = (data.get(k) or '').strip()[:200]
    for k in ('joined', 'left_on'):
        if have.get(k) and not re.match(r'^\d{4}-\d{2}-\d{2}$', have[k]):
            raise ApiError('A date is written 2026-09-30.')
    return have


def save_staff(conn, cid, lid, data, commit=True):
    """How one person is paid; also puts them back on the list."""
    l = one(conn.execute('SELECT id, name FROM ledger WHERE id=? AND '
                         'company_id=?', (lid, cid)))
    if not l:
        raise ApiError('That account is not in this firm.', 404)
    key = staff_key(l['name'])
    have = one(conn.execute('SELECT * FROM staff WHERE company_id=? AND '
                            'name=?', (cid, key)))
    s = _staff_settings(data, have)
    conn.execute(
        'INSERT INTO staff(company_id, name, pay_basis, rate, mobile, joined, '
        'left_on, notes, listed) VALUES (?,?,?,?,?,?,?,?,1) '
        'ON CONFLICT(company_id, name) DO UPDATE SET pay_basis=excluded.'
        'pay_basis, rate=excluded.rate, mobile=excluded.mobile, '
        'joined=excluded.joined, left_on=excluded.left_on, '
        'notes=excluded.notes, listed=1',
        (cid, key, s.get('pay_basis') or '', r2(s.get('rate')),
         s.get('mobile') or '', s.get('joined') or '', s.get('left_on') or '',
         s.get('notes') or ''))
    if commit:
        conn.commit()
    return dict(ok=True, ledger_id=lid)


def add_staff(conn, cid, data):
    """Put somebody on the staff list: an account the books already have,
    or a new one opened under SALARY PAYABLE the way SHREE files them."""
    if data.get('ledger_id'):
        return save_staff(conn, cid, qint(data['ledger_id'], 'account'), data)
    name = re.sub(r'\s+', ' ', (data.get('name') or '').strip())
    if not name:
        raise ApiError('Give the name.')
    same = one(conn.execute(
        'SELECT id FROM ledger WHERE company_id=? AND upper(trim(name))=?',
        (cid, staff_key(name))))
    if same:
        return save_staff(conn, cid, same['id'], data)
    grp = one(conn.execute(
        "SELECT id FROM account_group WHERE company_id=? AND "
        "upper(name) LIKE '%SALARY PAYABLE%' ORDER BY code LIMIT 1", (cid,)))
    if not grp:
        raise ApiError('This firm has no SALARY PAYABLE group to open the '
                       'account in. Add one under Chart of Accounts.')
    _staff_settings(data)           # checked before anything is written
    made = save_ledger(conn, cid, None, dict(
        name=name.upper(), group_id=grp['id'], phone=data.get('mobile') or '',
        notes='Staff'))
    return save_staff(conn, cid, made['id'], data)


def staff_candidates(conn, cid):
    """Accounts that could be put on the staff list -- anyone not already
    on it, outside income and expense."""
    out = [dict(id=l['id'], name=l['name'], group_name=l['group_name'])
           for l, s in _staff_list(conn, cid, everyone=True)
           if not (s['listed'] if s else _is_staff_account(l))
           and l['nature'] not in ('expense', 'income')
           and not l['name'].upper().endswith(LOOSE_TAG.strip())]
    # Accounts named for a salary or a labourer lead the list.
    out.sort(key=lambda x: (not re.search(r'SALAR|LABOUR|LEADER|STAFF',
                                          x['name'].upper()), x['name']))
    return out


def unlist_staff(conn, cid, lid):
    """Take somebody off the list. Their account and its entries stay."""
    l = one(conn.execute('SELECT name FROM ledger WHERE id=? AND company_id=?',
                         (lid, cid)))
    if not l:
        raise ApiError('That account is not in this firm.', 404)
    conn.execute(
        'INSERT INTO staff(company_id, name, listed) VALUES (?,?,0) '
        'ON CONFLICT(company_id, name) DO UPDATE SET listed=0',
        (cid, staff_key(l['name'])))
    conn.commit()
    return dict(ok=True)


# Loose money: cash handed to somebody in an emergency -- a hospital bill,
# a journey home -- and taken back later, in cash or cut from a salary. It
# is kept on an account of its own beside theirs, "LABOUR BHOLA (LOOSE
# MONEY)" under Loans & Advances, so it never hides inside the salary
# account's balance: what is owed on salary and what is out as loose money
# are two different sums, and each is seen for what it is. The account is
# opened the first time it is used. Taken when nothing is out, the account
# goes into credit -- money they lent -- and reads as owed to them.
LOOSE_TAG = ' (LOOSE MONEY)'


def _loose_ids(conn, cid):
    """{person's account: their loose-money account} for this firm."""
    by_key = {staff_key(r['name']): r['id'] for r in conn.execute(
        'SELECT id, name FROM ledger WHERE company_id=?', (cid,))}
    out = {}
    for r in conn.execute("SELECT name, loose_account FROM staff WHERE "
                          "company_id=? AND loose_account<>''", (cid,)):
        person, loose = by_key.get(r['name']), by_key.get(r['loose_account'])
        if person and loose:
            out[person] = loose
    return out


def _ensure_loose(conn, cid, lid):
    """Their loose-money account, opened under Loans & Advances if they
    have none yet. Nothing is committed here."""
    have = _loose_ids(conn, cid).get(lid)
    if have:
        return have
    l = one(conn.execute('SELECT id, name FROM ledger WHERE id=? AND '
                         'company_id=?', (lid, cid)))
    if not l:
        raise ApiError('That account is not in this firm.', 404)
    name = (l['name'].strip() + LOOSE_TAG)[:120]
    found = one(conn.execute('SELECT id FROM ledger WHERE company_id=? AND '
                             'upper(trim(name))=?', (cid, staff_key(name))))
    if found:
        loose = found['id']
    else:
        grp = one(conn.execute(
            "SELECT id FROM account_group WHERE company_id=? AND "
            "(code='2501' OR upper(name) LIKE 'LOANS & ADVANCES%') AND "
            "nature='asset' ORDER BY code='2501' DESC, code LIMIT 1", (cid,)))
        if not grp:
            raise ApiError('This firm has no Loans & Advances group to keep '
                           'loose money in. Add one under Chart of Accounts.')
        loose = conn.execute(
            'INSERT INTO ledger(company_id, group_id, name, notes) '
            'VALUES (?,?,?,?)',
            (cid, grp['id'], name,
             'Loose money given to %s -- kept apart from salary.'
             % l['name'].strip())).lastrowid
    conn.execute(
        'INSERT INTO staff(company_id, name, loose_account) VALUES (?,?,?) '
        'ON CONFLICT(company_id, name) DO UPDATE SET '
        'loose_account=excluded.loose_account',
        (cid, staff_key(l['name']), staff_key(name)))
    return loose


def loose_money(conn, cid, lid, data):
    """Loose money given to somebody, or taken -- back from them in cash, or
    cut from their salary."""
    person = one(conn.execute('SELECT id, name FROM ledger WHERE id=? AND '
                              'company_id=?', (lid, cid)))
    if not person:
        raise ApiError('That account is not in this firm.', 404)
    way = (data.get('direction') or '').strip().lower()
    if way not in ('given', 'taken'):
        raise ApiError('Say whether the money was given or taken.')
    how = (data.get('how') or 'cash').strip().lower()
    if how not in ('cash', 'salary') or (way == 'given' and how == 'salary'):
        raise ApiError('Loose money is given in cash or through the bank.')
    amount = r2(_amount(data.get('amount')))
    if amount <= 0:
        raise ApiError('Give the amount.')
    date = (data.get('date') or '').strip() or _today()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        raise ApiError('A date is written 2026-09-30.')
    note = re.sub(r'\s+', ' ', (data.get('remarks') or '').strip())
    try:
        loose = _ensure_loose(conn, cid, lid)
        if how == 'salary':
            text = 'LOOSE MONEY CUT FROM SALARY' + (' - ' + note if note else '')
            v = save_voucher(conn, cid, None, dict(
                kind='JOURNAL', voucher_date=date, remarks=text[:200],
                lines=[dict(ledger_id=lid, debit=amount, remarks=text[:200]),
                       dict(ledger_id=loose, credit=amount,
                            remarks=text[:200])]), commit=False)
        else:
            bank = qint(data.get('bank_id'), 'account', 0) or \
                sys_ledger(conn, cid, 'CASH')
            code = one(conn.execute(
                'SELECT g.code, g.parent_code FROM ledger l JOIN account_group '
                'g ON g.id=l.group_id WHERE l.id=? AND l.company_id=?',
                (bank, cid)))
            if not code or (code['code'] not in MONEY_GROUPS
                            and code['parent_code'] not in MONEY_GROUPS):
                raise ApiError('Choose the cash or bank account.')
            text = ('LOOSE MONEY GIVEN' if way == 'given'
                    else 'LOOSE MONEY TAKEN') + (' - ' + note if note else '')
            v = save_voucher(conn, cid, None, dict(
                kind='PAYMENT' if way == 'given' else 'RECEIPT',
                voucher_date=date, party_id=loose, bank_id=bank,
                amount=amount, remarks=text[:200],
                mode='Cash' if code['code'] == '2301' else 'Bank'),
                commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(ok=True, voucher_id=v['id'], kind=v['kind'],
                no=v['voucher_no'], loose_id=loose,
                balance=r2(ledger_balances(conn, cid).get(loose, 0.0)))


def _salary_note(basis, units, rate, amount, days, month):
    """The working, as the shop writes it: "284 X 71.25 = 20235 SEP 2026"."""
    tail = _month_name(month)
    n = lambda x: ('%.2f' % x).rstrip('0').rstrip('.')
    if basis == 'month':
        if not units or abs(units - days) < 0.001:
            return 'FIX %s %s' % (n(amount), tail)
        return '%s / %d X %s = %s %s' % (n(rate), days, n(units), n(amount),
                                         tail)
    return '%s X %s = %s %s' % (n(units), n(rate), n(amount), tail)


def post_salary(conn, cid, data):
    """A month's salary: one journal per person, as SHREE books it --
    the salary account debited, theirs credited, on the month's last day.
    All of them go in together or none does."""
    month = (data.get('month') or '').strip()
    frm, to, days = month_bounds(month)
    date = (data.get('date') or '').strip() or to
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        raise ApiError('A date is written 2026-09-30.')
    people = {l['id']: l for l, _ in _staff_list(conn, cid, everyone=True)}
    staff_ids = {l['id'] for l, _ in _staff_list(conn, cid)}
    exp = one(conn.execute(
        'SELECT l.id, l.name FROM ledger l JOIN account_group g ON '
        'g.id=l.group_id WHERE l.id=? AND l.company_id=?',
        (qint(data.get('expense_id'), 'account', 0), cid)))
    if not exp:
        raise ApiError('Choose the account the salary is booked to.')
    if exp['id'] in staff_ids:
        raise ApiError('%s is on the staff list; choose the salary expense '
                       'account instead.' % exp['name'])
    lines, seen = [], set()
    for ln in data.get('lines') or []:
        lid = qint(ln.get('ledger_id'), 'account')
        amount = r2(ln.get('amount'))
        if not amount:
            continue
        if lid not in people:
            raise ApiError('One of these is not an account in this firm.')
        if lid in seen:
            raise ApiError('%s is in the list twice.' % people[lid]['name'])
        if amount < 0:
            raise ApiError('%s: the salary cannot be below nought.'
                           % people[lid]['name'])
        seen.add(lid)
        basis = (ln.get('basis') or 'month').lower()
        if basis not in PAY_BASES:
            raise ApiError('Pay is by the month, the day or the hour.')
        lines.append((lid, amount, basis, _amount(ln.get('units')),
                      _amount(ln.get('rate')), bool(ln.get('again')),
                      (ln.get('note') or '').strip()))
    if not lines:
        raise ApiError('Nothing to post -- no one has an amount.')
    # A month already booked, in SHREE or here, is not booked twice unless
    # that was asked for outright.
    done = _salary_journals(conn, cid, [l[0] for l in lines], frm, to)
    twice = [people[l[0]]['name'] for l in lines if done.get(l[0]) and not l[5]]
    if twice:
        raise ApiError('Salary for %s is already booked for %s. Untick them, '
                       'or tick "again" to book it a second time.'
                       % (_month_name(month), ', '.join(twice)))
    made = []
    try:
        for lid, amount, basis, units, rate, _again, note in lines:
            text = _salary_note(basis, units, rate, amount, days, month)
            if note:
                text += ' ' + note
            v = save_voucher(conn, cid, None, dict(
                kind='JOURNAL', voucher_date=date, remarks=text[:200],
                lines=[dict(ledger_id=exp['id'], debit=amount,
                            remarks=people[lid]['name']),
                       dict(ledger_id=lid, credit=amount, remarks=text[:200])]),
                commit=False)
            made.append(dict(voucher_id=v['id'], no=v['voucher_no'],
                             name=people[lid]['name'], amount=amount))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(ok=True, made=made, total=r2(sum(m['amount'] for m in made)))


# ---------------------------------------------------------------- reports

def ledger_balances(conn, cid, upto=None):
    """ledger_id -> signed balance. Positive = debit (they owe us / asset).

    Half the screens ask for this, some more than once, so it is worked out
    once per state of the books. The caller gets its own copy to change.
    """
    return dict(memo(conn, ('balances', cid, upto or ''),
                     lambda: _ledger_balances(conn, cid, upto)))


def _ledger_balances(conn, cid, upto=None):
    bal = {}
    for r in conn.execute('SELECT id, opening_bal FROM ledger WHERE company_id=?',
                          (cid,)):
        bal[r['id']] = float(r['opening_bal'] or 0)
    sql = ('SELECT ledger_id, SUM(debit) d, SUM(credit) c FROM posting '
           'WHERE company_id=?')
    args = [cid]
    if upto:
        sql += ' AND date<=?'
        args.append(upto)
    sql += ' GROUP BY ledger_id'
    for r in conn.execute(sql, args):
        bal[r['ledger_id']] = bal.get(r['ledger_id'], 0.0) + \
            float(r['d'] or 0) - float(r['c'] or 0)
    return bal


def stock_map(conn, cid, inward=None):
    """item_id -> quantity on hand. Items with track_stock off are skipped.

    Pass a `set` as `inward` and it is filled with the items this firm has
    ever actually bought -- an opening quantity, or a purchase or credit line.
    An item with none of those is not a stock position that has run down to
    nothing; it is a name that was never stocked here at all. The two read the
    same as a bare zero and mean quite different things, and the challan
    book's one generic product -- which every bill made from a challan starts
    out naming -- is the second kind.
    """
    st = {}
    for r in conn.execute('SELECT id, opening_qty, track_stock FROM item '
                          'WHERE company_id=?', (cid,)):
        if r['track_stock']:
            st[r['id']] = float(r['opening_qty'] or 0)
            if inward is not None and float(r['opening_qty'] or 0):
                inward.add(r['id'])
    # A weighed line moves stock by its weight, matching the item's unit.
    q = ('SELECT ii.item_id, i.doc_type, '
         '  SUM(' + STOCK_QTY + ') q '
         'FROM invoice_item ii JOIN invoice i ON i.id=ii.invoice_id '
         'WHERE i.company_id=? AND ii.item_id IS NOT NULL '
         'GROUP BY ii.item_id, i.doc_type')
    for r in conn.execute(q, (cid,)):
        if r['item_id'] not in st:
            continue
        sign = DOC_STOCK.get(r['doc_type'], 0)
        st[r['item_id']] += sign * float(r['q'] or 0)
        if inward is not None and sign > 0 and float(r['q'] or 0):
            inward.add(r['item_id'])
    # Stock added, removed or counted by hand: each line is its own change.
    for r in conn.execute(
            'SELECT l.item_id, SUM(l.qty) q, MAX(l.qty) most '
            'FROM stock_entry_line l JOIN stock_entry e ON e.id=l.entry_id '
            'WHERE e.company_id=? GROUP BY l.item_id', (cid,)):
        if r['item_id'] not in st:
            continue
        st[r['item_id']] += float(r['q'] or 0)
        if inward is not None and float(r['most'] or 0) > 0:
            inward.add(r['item_id'])
    return st


def _resolve_account(conn, led, books, one_book=False):
    """The ledgers that make up this account, and which of them hold the money.

    A party is followed through every book they are written in -- one
    customer, one account -- unless the caller wants the firm on screen alone.
    Anything that is not a party is only ever itself.
    """
    refs = [(led['company_id'], led['id'])]
    if not one_book and (led['party_kind'] or led['is_party']):
        keys, _ = customer_keys(conn)
        key = keys.get((led['company_id'], led['id']))
        if key:
            found = _ledgers_of(conn, key, keys)
            if found:
                return (found,) + _account_refs(found, books)
    return refs, refs, True


def report_ledger(conn, cid, q):
    """One account, the same one screen Customer Accounts drills into.

    SHREE has a single *Account Ledger*, read either as a monthly summary or
    entry by entry, and it is the same screen for a customer, a bank or an
    expense head. This app had two -- a Ledger Statement and a Customer
    Account -- built on two readings of the same postings, which is how they
    came to disagree. There is one now.

    A party is followed through **every** book they are written in, because a
    customer with a ledger in the accounts book and another in a GST firm has
    one account between them. `scope=book` keeps to the firm on screen, which
    is what the chart of accounts wants.
    """
    if not q.get('ledger_id') and not q.get('party_id'):
        raise ApiError('Select a ledger to view its statement.')
    lid = qint(q.get('ledger_id') or q.get('party_id'), 'ledger')
    led = one(conn.execute(
        'SELECT l.*, g.name AS group_name, g.is_party, c.name AS company_name '
        'FROM ledger l JOIN account_group g ON g.id=l.group_id '
        'JOIN company c ON c.id=l.company_id WHERE l.id=?', (lid,)))
    if not led:
        raise ApiError('Ledger not found.', 404)
    frm, to = q.get('from') or '', q.get('to') or ''
    books = money_books(conn)
    one_book = (q.get('scope') or '').lower() in ('book', 'ledger', 'firm')
    refs, account, accounted = _resolve_account(conn, led, books, one_book)
    d = _account_report(conn, refs, account, accounted, frm, to, led, books)
    _entry_bills(conn, d.get('entries') or [])
    d['scope'] = 'book' if one_book else 'customer'
    d['is_party'] = bool(led['party_kind'] or led['is_party'])
    return d


def _entry_bills(conn, entries):
    """The GST bill each challan on an account went out on.

    An account is read in the accounts book, where the goods are a challan
    and the tax a journal; the bill itself stands in whichever GST firm raised
    it. Asked on 23 Sep 2026 for "the complete account, all bills, challans,
    payments, everything" -- so each challan line names its bill, and the
    screen opens it from there, in its own firm. The same lookup the ageing
    screens use (`_bill_links`). A month's lines are these same entries, so
    they carry it too.
    """
    claims = [dict(kind='SALE', doc_id=e['doc_id'], entry=e) for e in entries
              if e.get('doc_kind') == 'SALE' and e.get('doc_id')]
    _bill_links(conn, claims)
    for c in claims:
        if c.get('bill_id'):
            c['entry'].update(bill_id=c['bill_id'], bill_company=c['bill_company'],
                              bill_no=c['bill_no'])


# The April a day's financial year opens in, worked out in SQL from a
# posting's date -- the year ledger_group_year is kept by.
YEAR_OF = "(CAST(substr(%s,1,4) AS INTEGER) - (substr(%s,6,2) < '04'))"


def _april_of(day):
    day = day or datetime.date.today().isoformat()
    try:
        y, m = int(day[:4]), int(day[5:7])
    except ValueError:
        return datetime.date.today().year
    return y if m >= 4 else y - 1


def _ledger_rows_with_groups(conn, cid, upto=None):
    """Every account with its balance on `upto`, under the group it was in
    that year -- SHREE's group of the year where it had it elsewhere
    (db.LEDGER_GROUP_YEAR), so an old year's trial balance and balance sheet
    read as SHREE's did."""
    bal = ledger_balances(conn, cid, upto)
    out = []
    for l in conn.execute(
            'SELECT l.id, l.name, l.system_key, g.code AS gcode, '
            '       g.name AS gname, g.nature, g.sort '
            'FROM ledger l LEFT JOIN ledger_group_year yg ON '
            '  yg.ledger_id=l.id AND yg.y=? '
            'JOIN account_group g ON g.id=COALESCE(yg.group_id, l.group_id) '
            'WHERE l.company_id=? ORDER BY g.sort, g.code, l.name',
            (_april_of(upto), cid)):
        d = dict(l)
        d['balance'] = r2(bal.get(l['id'], 0))
        out.append(d)
    return out


def report_trial_balance(conn, cid, q):
    data = [l for l in _ledger_rows_with_groups(conn, cid, q.get('to'))
            if abs(l['balance']) > 0.004]
    dr = r2(sum(l['balance'] for l in data if l['balance'] > 0))
    cr = r2(-sum(l['balance'] for l in data if l['balance'] < 0))
    for l in data:
        l['debit'] = l['balance'] if l['balance'] > 0 else 0
        l['credit'] = -l['balance'] if l['balance'] < 0 else 0
    return dict(rows=data, total_debit=dr, total_credit=cr,
                difference=r2(dr - cr), upto=q.get('to') or '')


def _group_totals(conn, cid, natures, frm, to):
    """Group-wise totals for P&L: movement inside the period only.

    Not the opening lines. SHREE opens every income and expense account at
    nil each year, and the import does the same with a line on 1 April that
    takes away what the year before ran up -- counted here, it subtracted
    last year's sales from this year's, income printed negative and
    profitable years read NET LOSS (found 10 Sep 2026, put right 6 Oct 2026:
    "make sure all the figures match"). A year's P&L is that year's entries,
    as in SHREE."""
    # Each entry under the group its account was in that year.
    sql = ('SELECT g.code, g.name, g.nature, SUM(p.debit) d, SUM(p.credit) c '
           'FROM posting p JOIN ledger l ON l.id=p.ledger_id '
           'LEFT JOIN ledger_group_year yg ON yg.ledger_id=l.id AND yg.y='
           + YEAR_OF % ('p.date', 'p.date') + ' '
           'JOIN account_group g ON g.id=COALESCE(yg.group_id, l.group_id) '
           "WHERE p.company_id=? AND p.doc_kind<>'OPENING' AND g.nature IN (%s)"
           % ','.join('?' * len(natures)))
    args = [cid] + list(natures)
    if frm:
        sql += ' AND p.date>=?'
        args.append(frm)
    if to:
        sql += ' AND p.date<=?'
        args.append(to)
    sql += ' GROUP BY g.code ORDER BY g.sort, g.code'
    out = []
    for r in conn.execute(sql, args):
        net = float(r['d'] or 0) - float(r['c'] or 0)
        out.append(dict(code=r['code'], name=r['name'], nature=r['nature'],
                        amount=r2(net if r['nature'] == 'expense' else -net)))
    return [g for g in out if abs(g['amount']) > 0.004]


def report_pnl(conn, cid, q):
    frm, to = q.get('from') or '', q.get('to') or ''
    expense = _group_totals(conn, cid, ('expense',), frm, to)
    income = _group_totals(conn, cid, ('income',), frm, to)
    exp_total = r2(sum(g['amount'] for g in expense))
    inc_total = r2(sum(g['amount'] for g in income))
    return dict(expense=expense, income=income, expense_total=exp_total,
                income_total=inc_total, profit=r2(inc_total - exp_total),
                **{'from': frm, 'to': to})


def report_balance_sheet(conn, cid, q):
    to = q.get('to') or ''
    ledgers = _ledger_rows_with_groups(conn, cid, to)
    groups = {}
    for l in ledgers:
        if l['nature'] in ('income', 'expense'):
            continue                     # these roll up into net profit below
        g = groups.setdefault(l['gcode'], dict(
            code=l['gcode'], name=l['gname'], nature=l['nature'],
            amount=0.0, sort=l['sort'], ledgers=[]))
        g['amount'] += l['balance']
        if abs(l['balance']) > 0.004:
            g['ledgers'].append(dict(name=l['name'], amount=r2(l['balance'])))
    # Income carries a credit (negative) balance and expense a debit (positive)
    # one, so negating their combined balance gives income - expense.
    profit = r2(-sum(l['balance'] for l in ledgers
                     if l['nature'] in ('income', 'expense')))

    assets, liabilities = [], []
    for g in sorted(groups.values(), key=lambda x: (x['sort'], x['code'])):
        g['amount'] = r2(g['amount'])
        if abs(g['amount']) < 0.005:
            continue
        (assets if g['nature'] == 'asset' else liabilities).append(
            dict(g, amount=abs(g['amount']),
                 contra=(g['amount'] < 0 if g['nature'] == 'asset'
                         else g['amount'] > 0)))
    asset_total = r2(sum(l['balance'] for l in ledgers if l['nature'] == 'asset'))
    liab_total = r2(-sum(l['balance'] for l in ledgers
                         if l['nature'] == 'liability'))
    diff = r2(asset_total - (liab_total + profit))
    return dict(assets=assets, liabilities=liabilities,
                asset_total=asset_total, liability_total=liab_total,
                profit=profit, difference=diff, upto=to)


# ------------------------------------------------------- bank and cash

# Every group that holds the firm's own money: the bank accounts, the cash
# box, and an overdraft account, which is the same thing carrying a credit
# balance. list_ledgers filters on the same three codes.
MONEY_GROUPS = ('2201', '2301', '6101')
# ... and the groups SHREE keeps under them: BSTP's PERSONAL BANK ACCOUNT
# (9010) sits under Bank Accounts there. Read that way since 6 Oct 2026, the
# personal banks keep SHREE's group -- its balance sheet's figures -- and
# still show on Bank & Cash.
MONEY_SQL = ("(g.code IN ('2201','2301','6101') OR "
             "COALESCE(g.parent_code,'') IN ('2201','2301','6101'))")


def bank_accounts(conn, cid, frm='', to=''):
    """Each cash and bank account with what it held and what moved.

    `opening` is the balance the account carried into `frm`, `received` and
    `paid` are the debits and credits inside the window, and `closing` is what
    it ends on. A statement for one account totals to exactly these figures --
    they are read from the same postings.
    """
    accounts = rows(conn.execute(
        'SELECT l.id, l.name, l.code, l.active, l.opening_bal, '
        '       g.code AS gcode, g.name AS group_name '
        'FROM ledger l JOIN account_group g ON g.id=l.group_id '
        'WHERE l.company_id=? AND ' + MONEY_SQL + ' '
        'ORDER BY g.code, l.name', (cid,)))
    if not accounts:
        return []
    ids = set(a['id'] for a in accounts)

    opening = dict((a['id'], float(a['opening_bal'] or 0)) for a in accounts)
    if frm:
        for r in conn.execute(
                'SELECT ledger_id, COALESCE(SUM(debit - credit), 0) b '
                'FROM posting WHERE company_id=? AND date<? GROUP BY ledger_id',
                (cid, frm)):
            if r['ledger_id'] in ids:
                opening[r['ledger_id']] += float(r['b'] or 0)

    sql = ('SELECT ledger_id, COALESCE(SUM(debit), 0) d, '
           '       COALESCE(SUM(credit), 0) c, COUNT(*) n '
           'FROM posting WHERE company_id=?')
    args = [cid]
    if frm:
        sql += ' AND date>=?'
        args.append(frm)
    if to:
        sql += ' AND date<=?'
        args.append(to)
    moved = {}
    for r in conn.execute(sql + ' GROUP BY ledger_id', args):
        if r['ledger_id'] in ids:
            moved[r['ledger_id']] = (float(r['d'] or 0), float(r['c'] or 0),
                                     r['n'])

    out = []
    for a in accounts:
        d, c, n = moved.get(a['id'], (0.0, 0.0, 0))
        o = opening.get(a['id'], 0.0)
        out.append(dict(
            id=a['id'], name=a['name'], code=a['code'] or '',
            group_name=a['group_name'], active=bool(a['active']),
            kind='cash' if a['gcode'] == '2301' else 'bank',
            opening=r2(o), received=r2(d), paid=r2(c), entries=n,
            closing=r2(o + d - c)))
    return out


def report_bank(conn, cid, q):
    """The bank book or the cash book: every account of that kind over a
    window, and -- when one is named -- its statement, in the
    deposit/withdrawal wording a bank uses.

    `kind` is 'bank' or 'cash' and is what tells the two books apart (they
    are two screens since 22 Sep 2026; left out, this answers for both, as
    the one screen did). The statement is looked up before the filter, so a
    link to an account of the other kind still answers -- the screen reads
    `statement.account.kind` and hands over to the other book.
    """
    frm, to = q.get('from') or '', q.get('to') or ''
    want = (q.get('kind') or '').strip().lower()
    if want and want not in ('bank', 'cash'):
        raise ApiError('Unknown kind "%s" -- bank or cash.' % want)
    accounts = bank_accounts(conn, cid, frm, to)

    statement = None
    if q.get('account'):
        lid = qint(q['account'], 'account')
        if not any(a['id'] == lid for a in accounts):
            raise ApiError('That is not a cash or bank account of this firm.',
                           404)
        st = report_ledger(conn, cid, dict(q, ledger_id=lid, scope='book'))
        # A bank reads its own book the other way round from the ledger: money
        # into the account is a deposit, money out a withdrawal. The balance
        # runs down the page the way a passbook does -- the account ledger
        # itself does not carry one, because there it is struck at the foot.
        # **A passbook runs in the order things happened.** The account ledger
        # sorts its rows by kind and number within a day, which is right for a
        # ledger and wrong here: it gathers a day's payments above its
        # receipts, and every running balance down the page is then a figure
        # the account never actually stood at. SHREE's bank book reads in
        # entry order, so this puts them back in it. A day's closing balance
        # is the same either way.
        st['entries'].sort(key=lambda e: (e['date'] or '', e['doc_id'] or 0))
        run = float(st['opening'] or 0)
        for e in st['entries']:
            e['deposit'] = e.pop('debit')
            e['withdrawal'] = e.pop('credit')
            run = r2(run + e['deposit'] - e['withdrawal'])
            e['balance'] = run
        _bank_book_fields(conn, st['entries'])
        statement = dict(
            account=next(a for a in accounts if a['id'] == lid),
            opening=st['opening'], closing=st['closing'],
            deposits=st['total_debit'], withdrawals=st['total_credit'],
            entries=st['entries'])

    # An account with nothing in it and nothing through it is not worth a
    # line: this firm carries twenty-two bank accounts and works two of them.
    for a in accounts:
        a['dormant'] = not a['entries'] and abs(a['closing']) < 0.005 \
            and abs(a['opening']) < 0.005
        a['overdrawn'] = a['kind'] == 'bank' and a['closing'] < -0.005
    show_all = str(q.get('dormant') or '').lower() in ('1', 'true', 'yes', 'on')
    # One book at a time: the totals, the count of dormant accounts and the
    # money moved between the firm's own accounts are all this book's own.
    # Cash paid into the bank is money leaving the cash book and arriving in
    # the bank book, so it is only left out where both sides are in the book
    # being read -- Kotak to AUSF on the bank page, one cash box to another.
    if want:
        accounts = [a for a in accounts if a['kind'] == want]
    asleep = sum(1 for a in accounts if a['dormant'])
    if not show_all:
        accounts = [a for a in accounts if not a['dormant']]

    banks = [a for a in accounts if a['kind'] == 'bank']
    cash = [a for a in accounts if a['kind'] == 'cash']
    # Money in the bank and money owed to it are two different figures, and
    # netting them says neither. An overdrawn account is a debt.
    held = r2(sum(a['closing'] for a in banks if a['closing'] > 0))
    overdrawn = r2(-sum(a['closing'] for a in banks if a['closing'] < 0))
    moved = _own_transfers(conn, cid, [a['id'] for a in accounts], frm, to)
    return dict(
        rows=accounts, statement=statement, **{'from': frm}, to=to,
        bank_total=r2(sum(a['closing'] for a in banks)),
        bank_held=held, bank_overdrawn=overdrawn,
        overdrawn_count=sum(1 for a in banks if a['closing'] < -0.005),
        cash_total=r2(sum(a['closing'] for a in cash)),
        dormant_hidden=0 if show_all else asleep, dormant=show_all,
        received=r2(sum(a['received'] for a in accounts)),
        paid=r2(sum(a['paid'] for a in accounts)),
        transfers=moved,
        opening_total=r2(sum(a['opening'] for a in accounts)),
        closing_total=r2(sum(a['closing'] for a in accounts)))


def _own_transfers(conn, cid, ids, frm='', to=''):
    """Money moved between two of the firm's own cash and bank accounts.

    Cash paid into the bank is a withdrawal from the cash book and a deposit
    in the bank book, so every such move is in both `received` and `paid`.
    Added up across the accounts, that said ₹38.7 crore came in to BSTP this
    year when ₹22.6 crore of it had only gone from one of its own accounts to
    another (Kotak to AUSF, cash into Kotak -- SHREE books these as receipts
    and payments against the other bank). A document counts for what it moved
    between the accounts: the smaller of what went in and what came out.
    Opening balances all sit on document 0 and are never a move.
    """
    if not ids:
        return 0.0
    sql = ('SELECT SUM(debit) d, SUM(credit) c FROM posting '
           "WHERE company_id=? AND doc_kind<>'OPENING' AND doc_id<>0 "
           'AND ledger_id IN (%s)' % ','.join('?' * len(ids)))
    args = [cid] + list(ids)
    if frm:
        sql += ' AND date>=?'
        args.append(frm)
    if to:
        sql += ' AND date<=?'
        args.append(to)
    sql += ' GROUP BY doc_kind, doc_id HAVING COUNT(DISTINCT ledger_id) > 1'
    return r2(sum(min(float(r['d'] or 0), float(r['c'] or 0))
                  for r in conn.execute(sql, args)))


# SHREE writes the voucher type and the instrument together in the Ch/DD
# column -- "Rcpt CLG", "Pymt AU", "Pymt KB/141", "Rcpt UPI" -- so the type is
# abbreviated the way its own book abbreviates it.
BOOK_KIND = {'RECEIPT': 'Rcpt', 'PAYMENT': 'Pymt', 'JOURNAL': 'Jrnl',
             'CONTRA': 'Ctra', 'SALE': 'Sale', 'PURCHASE': 'Prch',
             'CREDIT': 'CrNt', 'DEBIT': 'DrNt', 'OPENING': ''}


def _bank_book_fields(conn, entries):
    """Fill in the three things SHREE's bank book shows and a ledger does not.

    Its columns are **Date | Particulars | City | Ch/DD No. | Withdraw |
    Deposit | Balance**, and it says more per line than the account ledger it
    is built on:

      * the **Ch/DD No.** column carries the voucher type and the instrument
        together -- `Pymt AU`, `Rcpt CLG`, `Pymt KB/141` -- not the voucher's
        own serial number, which is what this screen used to print there;
      * the **city** of the party on the other side, blank for a bank or an
        expense head, which is how you tell one ANJANAI from another;
      * the **narration** on a second line under the particulars, where this
        screen was printing the reference instead -- so the reference showed
        twice and the note somebody typed showed not at all.

    All three live on the voucher, so they are read in one pass for the page
    being drawn rather than a query a row.
    """
    ids = sorted({e['doc_id'] for e in entries
                  if e['doc_id'] and e['doc_kind'] in VOUCHER_KINDS})
    found, gone = {}, {}
    for chunk in (ids[i:i + 400] for i in range(0, len(ids), 400)):
        ph = ','.join('?' * len(chunk))
        for r in conn.execute(
                'SELECT v.id, v.ref_no, v.remarks, v.mode, l.city '
                'FROM voucher v LEFT JOIN ledger l ON l.id=v.party_id '
                'WHERE v.id IN (%s)' % ph, chunk):
            found[r['id']] = r
        # A receipt from a customer since deleted stays in the book, its
        # other side carried on Difference in Opening Balances; the line
        # there still knows whose it was, and the book says that name, as
        # SHREE's own book does.
        for r in conn.execute(
                "SELECT voucher_id, remarks FROM voucher_line WHERE "
                "voucher_id IN (%s) AND remarks LIKE '%%account deleted'"
                % ph, chunk):
            m = DELETED_PART_RE.match(r['remarks'] or '')
            if m:
                gone.setdefault(r['voucher_id'], []).append(m.group(1))
    for e in entries:
        v = found.get(e['doc_id']) if e['doc_kind'] in VOUCHER_KINDS else None
        names = gone.get(e['doc_id']) if v else None
        if names and squash(e['particulars']) == squash(OPENING_DIFF_LEDGER):
            e['particulars'] = ', '.join(names) + ' (account deleted)'
        short = BOOK_KIND.get(e['doc_kind'], e['doc_kind'].title())
        ref = txt_or_blank(v['ref_no']) if v else ''
        e['city'] = txt_or_blank(v['city']) if v else ''
        e['note'] = txt_or_blank(v['remarks']) if v else ''
        e['mode'] = txt_or_blank(v['mode']) if v else ''
        # A document that is not a voucher has no instrument, so its own
        # number is the only reference it has to give.
        e['chq'] = (short + (' ' + ref if ref else '')).strip() \
            or (e['no'] or '')


# The account every unbacked-up year was carried onto. It is a bookkeeping
# entry, not a party, and belongs at the foot of a statement rather than in it.
OPENING_DIFF_LEDGER = 'Difference in Opening Balances'


def report_outstanding(conn, cid, q):
    """Who owes us and what we owe, the way SHREE lays it out.

    Its *Trial Balance (Groupwise (Vertical))* is the screen this firm reads
    to see where it stands: two columns side by side -- **Liabilities** on the
    left, **Assets** on the right -- each grouped under its account group,
    each group footed with `Sum of: <group>`, and each side footed with how
    many lines it holds and what they come to.

    The same group appears on **both** sides and that is the point of it. A
    customer in credit is an advance held and belongs among the liabilities;
    the rest of Accounts Receivables is money owed to us. Splitting by the
    sign of the balance rather than by the kind of account is what puts each
    one where it belongs.
    """
    upto = q.get('to') or q.get('upto') or ''
    hunt = squash(q.get('search') or '')
    rows_ = [l for l in _ledger_rows_with_groups(conn, cid, upto)
             if abs(l['balance']) > 0.004]

    # The years SHREE never backed up are carried on one account, and it is
    # nobody's debt. SHREE keeps it off the two columns and prints it at the
    # foot -- "Difference in opening Balance: ... DB" -- and so does this;
    # left in the list it reads as the largest creditor in the book.
    # See the note on missing years in the import.
    opening_diff = 0.0
    keep = []
    for l in rows_:
        if squash(l['name']) == squash(OPENING_DIFF_LEDGER):
            opening_diff += l['balance']
        else:
            keep.append(l)
    rows_ = keep
    if hunt:
        rows_ = [l for l in rows_ if hunt in squash(l['name'])]

    def side(want_debit):
        groups, order = {}, []
        for l in rows_:
            if (l['balance'] > 0) != want_debit:
                continue
            key = (l['gcode'], l['gname'])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(dict(id=l['id'], name=l['name'],
                                    amount=r2(abs(l['balance'])),
                                    system_key=l['system_key'] or ''))
        out = []
        for code, name in order:
            got = sorted(groups[(code, name)], key=lambda x: x['name'])
            out.append(dict(code=code, name=name, rows=got,
                            sum=r2(sum(x['amount'] for x in got))))
        return dict(groups=out,
                    count=sum(len(g['rows']) for g in out),
                    total=r2(sum(g['sum'] for g in out)))

    assets, liabilities = side(True), side(False)
    return dict(
        upto=upto, search=q.get('search') or '',
        assets=assets, liabilities=liabilities,
        # SHREE prints this at the foot and so does this: what the two sides
        # differ by, and which way round.
        difference=r2(assets['total'] - liabilities['total']),
        opening_difference=r2(-opening_diff),
        # One flat list behind the two columns, for the CSV.
        rows=[dict(name=x['name'], group_name=g['name'], balance=x['amount'],
                   side=label)
              for label, sd in (('Assets', assets), ('Liabilities', liabilities))
              for g in sd['groups'] for x in g['rows']],
        total=r2(assets['total'] - liabilities['total']))


# ---------------------------------------------------------- customers
#
# A customer is one person to the man who sells to them and three ledgers to
# these books, and the three are not interchangeable. The GST firms hold the
# tax bills and almost no money -- twelve receipts in BSI against thirty
# thousand sales -- because the receipts were always written in the no-tax
# accounts book, where each customer's whole account lives: every sale, the
# tax on it as a journal, and every rupee that came back. So a balance read
# off a GST firm is not what a customer owes, it is everything ever billed to
# them; what they owe is in the accounts book and nowhere else.
#
# This report is built on that split rather than around it. What they were
# charged comes from the bills, what they paid and what is left comes from the
# accounts book, and every figure says which book it came from.

MONTH_SHORT = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def add_days(date_str, days):
    """A date, so many days on. '2026-08-01' + 7 -> '2026-08-08'."""
    try:
        d = datetime.date(int(date_str[0:4]), int(date_str[5:7]),
                          int(date_str[8:10]))
    except (TypeError, ValueError, IndexError):
        return date_str or ''
    return (d + datetime.timedelta(days=int(days or 0))).isoformat()


@functools.lru_cache(maxsize=16384)
def _day_number(s):
    try:
        return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10])).toordinal()
    except (TypeError, ValueError, IndexError):
        return None


def _day(s):
    """A date's day number, or None where days_between would give up."""
    try:
        return _day_number(s)
    except TypeError:
        return None


def days_between(frm, to):
    """Whole days from one date to another; negative when `to` is earlier.

    Matching challans to their GST journals asks this some 200,000 times over
    a few hundred distinct dates, so each date is read once."""
    try:
        a, b = _day_number(frm), _day_number(to)
    except TypeError:                      # something unhashable
        return 0
    if a is None or b is None:
        return 0
    return b - a


def month_label(ym):
    """'2026-08' -> 'Aug 2026'."""
    year, _, mon = str(ym or '').partition('-')
    try:
        return '%s %s' % (MONTH_SHORT[int(mon) - 1], year)
    except (ValueError, IndexError):
        return ym or ''


def money_books(conn):
    """The books the receipts are written in -- every no-tax accounts book.

    One firm per family here, but SHREE keeps an accounts book for each of
    them, so this is a list and not the one order firm: a customer of Patel
    Steel is settled in Patel Steel's book, not in this one's.
    """
    return [c['id'] for c in list_companies(conn)
            if (c['tax_mode'] or 'gst') == 'none']


def customer_keys(conn):
    """(company_id, ledger_id) -> one key for the same customer everywhere.

    The books were written by hand over thirteen years, so the same customer
    is "PARTH INDUSTRIES   NIMISHBHAI" in one and "PARTH INDUSTRIES
    NIMISHBHAI" in the next. The name with its spaces and case taken out
    joins those two; `linked_ledger`, where an allocation has already set it,
    joins the ones whose names never did agree.
    """
    parent, key_of = {}, {}

    def find(k):
        while parent.get(k, k) != k:
            parent[k] = parent.get(parent[k], parent[k])
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    rows_ = list(conn.execute(
        'SELECT id, company_id, name, linked_ledger FROM ledger '
        "WHERE party_kind<>'' OR linked_ledger IS NOT NULL"))
    by_id = {r['id']: r for r in rows_}
    for r in rows_:
        k = squash(r['name'])
        key_of[(r['company_id'], r['id'])] = k
        parent.setdefault(k, k)
    for r in rows_:
        twin = by_id.get(r['linked_ledger'])
        if twin:
            union(squash(r['name']), squash(twin['name']))
    return {ref: find(k) for ref, k in key_of.items()}, by_id


def _ledgers_of(conn, key, keys):
    """Every ledger, in every book, that is this customer."""
    return [ref for ref, k in keys.items() if k == key]


def report_customers(conn, cid, q):
    """Customers with their bills and their payments, a month at a time.

    Two questions, one screen. With nobody named it answers the wide one:
    over these months, who was billed how many times and for how much, what
    did they pay, and what is still owed. Name one customer and the same
    figures come back a row per month with the documents behind each --
    five bills and three payments in August is the thing this exists to
    show, and the count matters as much as the amount.

    What is owed is read to the end of the window and from the whole of the
    account, not from the window alone: money is owed from before it too.
    """
    frm, to = q.get('from') or '', q.get('to') or ''
    # One customer is one account, and the account has one screen. Naming a
    # customer here answers with exactly what Account Ledger answers with.
    if q.get('ledger_id') or q.get('party_id'):
        return report_ledger(conn, cid, q)
    keys, _ = customer_keys(conn)
    return _customer_totals(conn, cid, keys, frm, to, q)


def _sale_docs(conn, frm, to, party_ids=None):
    """Every challan -- and only challans.

    The goods go out once and are written down twice: as a challan in the
    accounts book, and again as a tax bill in whichever GST firm billed that
    customer. Counting both would count every load twice. The challan is the
    one that counts here, because this screen is the customer's account, and
    the account is the accounts book -- the same book the balance and the
    receipts come from, and the same documents the ledger statement shows.
    A challan that was never billed on counts too; the goods went out just
    the same.
    """
    books = money_books(conn)
    if not books:
        return
    sql = ("SELECT i.id, i.company_id, i.doc_type, i.invoice_date, i.series, "
           "       i.invoice_no, i.party_id, i.grand_total, i.billed_invoice "
           "FROM invoice i WHERE i.company_id IN (%s) "
           "  AND i.doc_type IN ('SALE','DEBIT','CREDIT')"
           % ','.join('?' * len(books)))
    args = list(books)
    if frm:
        sql += ' AND i.invoice_date>=?'
        args.append(frm)
    if to:
        sql += ' AND i.invoice_date<=?'
        args.append(to)
    if party_ids:
        sql += ' AND i.party_id IN (%s)' % ','.join('?' * len(party_ids))
        args += list(party_ids)
    for r in conn.execute(sql, args):
        yield r


# The kinds of entry the bank and cash books are written in -- a receipt is
# the customer paying, a payment is the firm paying them. Used only to say how
# much of a month's credit was money rather than an adjustment.
MONEY_KINDS = ('RECEIPT', 'PAYMENT', 'CONTRA')


def _trade_keys(conn, keys):
    """The accounts that are customers, as against accounts money passed through.

    **The accounts book holds more than customers.** Land bought between the
    brothers, a salary account, a builder paid in instalments: they are marked
    as parties there because that is where the money went, and on the strength
    of a payment passing through they would sit at the top of any screen
    sorted by what it is owed. `PRAVINBHAI CONSTRUCTION A/C.` is one of them,
    and it is owed more than any real customer on these books.

    A customer is someone the firms have **sent goods to**: a ledger in one of
    the GST firms, or a challan at any time. Money moving on its own does not
    make an account a customer.

    This is the one place that decides it, because the customer list and the
    ageing screen have to agree about who is on them -- and the totals have to
    agree too, or neither figure gets believed.
    """
    firms = {f['id'] for f in billing_firms(conn)}
    trade = {k for (co, _), k in keys.items() if co in firms}
    books = money_books(conn)
    if books:
        for r in conn.execute(
                "SELECT DISTINCT i.party_id, i.company_id FROM invoice i "
                "WHERE i.company_id IN (%s) AND i.doc_type IN ('SALE','DEBIT')"
                % ','.join('?' * len(books)), books):
            key = keys.get((r['company_id'], r['party_id']))
            if key:
                trade.add(key)
        # A customer whose every challan was in the years deleted in Settings
        # is still one: those deletions name who had been sold goods.
        codes = {r['code'] for r in conn.execute(
            'SELECT code FROM company WHERE id IN (%s)'
            % ','.join('?' * len(books)), books)}
        sold = set()
        for r in conn.execute("SELECT firm, value FROM sync_rule "
                              "WHERE kind='drop_years'"):
            if r['firm'] not in codes:
                continue
            try:
                names = (json.loads(r['value'] or '{}') or {}).get('sold') or []
            except ValueError:
                names = []
            sold.update(squash(n) for n in names)
        if sold:
            for r in conn.execute('SELECT id, company_id, name FROM ledger '
                                  'WHERE company_id IN (%s)'
                                  % ','.join('?' * len(books)), books):
                key = keys.get((r['company_id'], r['id']))
                if key and squash(r['name']) in sold:
                    trade.add(key)
    return trade


def _account_refs(refs, books):
    """The ledgers a customer's balance is read from, and whether it is one.

    Only the accounts book holds both sides of the trade, so only it can say
    what is owed. Three hundred and sixty-five customers the GST firms bill
    have never been written there, and for them the honest answer is that
    nobody knows: their register balance is everything ever billed to them,
    never reduced by a rupee, and printing it as a receivable would be worse
    than printing nothing. They come back marked, not guessed at.
    """
    mine = [r for r in refs if r[0] in books]
    return (mine, True) if mine else (list(refs), False)


def _book_names(conn, books):
    """The accounts books, named the way the screen has to name them."""
    names = [c['name'] for c in list_companies(conn) if c['id'] in set(books)]
    return ' and '.join(names) if len(names) < 3 else ', '.join(names)


def _balances_by_book(conn, to=None):
    """company_id -> {ledger_id: balance}, for the books a customer lives in."""
    wanted = [c['id'] for c in list_companies(conn)]
    return {cid: ledger_balances(conn, cid, to or None) for cid in wanted}


def _account_sums(conn, books, frm, to):
    """Debit, credit and how many entries, per ledger, over the window.

    One pass for every party at once -- reading a customer's account one at a
    time is what made this screen slow enough to notice.
    """
    if not books:
        return []
    sql = ('SELECT company_id, ledger_id, COUNT(*) n, SUM(debit) d, '
           '       SUM(credit) c FROM posting WHERE company_id IN (%s)'
           % ','.join('?' * len(books)))
    args = list(books)
    if frm:
        sql += ' AND date>=?'
        args.append(frm)
    if to:
        sql += ' AND date<=?'
        args.append(to)
    return conn.execute(sql + ' GROUP BY company_id, ledger_id', args)


def _customer_totals(conn, cid, keys, frm, to, q):
    """Every customer over the window, one row each."""
    ledgers = {r['id']: r for r in conn.execute(
        "SELECT l.id, l.company_id, l.name, l.city, l.phone, l.gstin, "
        "       l.credit_limit, l.credit_days FROM ledger l "
        "JOIN account_group g ON g.id=l.group_id "
        "WHERE l.party_kind='customer' OR l.party_kind='' AND g.is_party=1")}

    agg = {}

    def slot(key, led):
        s = agg.get(key)
        if not s:
            s = agg[key] = dict(key=key, name=led['name'], city=led['city'] or '',
                                phone=led['phone'] or '', gstin=led['gstin'] or '',
                                credit_days=led['credit_days'] or 0,
                                credit_limit=r2(led['credit_limit'] or 0),
                                ledger_id=led['id'], company_id=led['company_id'],
                                bills=dict(count=0, amount=0.0),
                                debit=0.0, credit=0.0, entries=0,
                                returns=dict(count=0, amount=0.0), owed=0.0)
        # The GST books hold the fuller record of a customer, so their
        # spelling of the name and their GSTIN are the ones worth showing.
        if led['gstin'] and not s['gstin']:
            s['gstin'], s['name'] = led['gstin'], led['name']
        return s

    books = money_books(conn)
    # The two figures SHREE puts against a customer for a year -- everything
    # debited to them and everything credited -- summed from the same entries
    # its own screen sums, so a row here and the months behind it agree. The
    # challan count rides alongside: the goods that went out, counted once
    # each however many lines the challan hit.
    for p in _account_sums(conn, books, frm, to):
        led = ledgers.get(p['ledger_id'])
        if not led:
            continue
        key = keys.get((led['company_id'], led['id']))
        if not key:
            continue
        s = slot(key, led)
        s['debit'] += float(p['d'] or 0)
        s['credit'] += float(p['c'] or 0)
        s['entries'] += p['n']

    for d in _sale_docs(conn, frm, to):
        led = ledgers.get(d['party_id'])
        if not led:
            continue
        key = keys.get((led['company_id'], led['id']))
        if not key:
            continue
        s = slot(key, led)
        where = 'returns' if d['doc_type'] == 'CREDIT' else 'bills'
        s[where]['count'] += 1
        s[where]['amount'] += float(d['grand_total'] or 0)

    bals = _balances_by_book(conn, to)
    refs_of = {}
    for (co, lid), key in keys.items():
        if lid in ledgers:
            refs_of.setdefault(key, []).append((co, lid))
    for key, refs in refs_of.items():
        account, accounted = _account_refs(refs, books)
        owing = sum(bals.get(co, {}).get(lid, 0) for co, lid in account)
        if not accounted and key not in agg:
            continue                       # nothing billed, nothing to say
        if accounted and abs(owing) < 0.005 and key not in agg:
            continue
        s = slot(key, ledgers[account[0][1]])
        s['accounted'] = accounted
        s['owed'] = (s['owed'] or 0) + owing if accounted else None
        s['billed_to_date'] = r2(owing)

    only_owing = str(q.get('owing') or '').lower() in ('1', 'true', 'yes', 'on')
    # The accounts book holds more than customers -- land bought between the
    # brothers, a salary account, a builder paid in instalments -- and they
    # are marked as parties there because that is where the money went. They
    # were sitting at the top of a screen called Customer Accounts, by the
    # size of their balances, on the strength of a payment passing through.
    #
    # A customer is someone the firms have sent goods to: a ledger in one of
    # the GST firms, or a challan at any time -- not only in the window, or a
    # challan-only customer paying off an old balance would drop out of a
    # month in which they bought nothing. Money moving on its own does not
    # make an account a customer, which is the whole difference between this
    # screen and the outstanding report.
    scope = (q.get('scope') or 'trade').lower()
    trade = _trade_keys(conn, keys)
    out = []
    for s in agg.values():
        for k in ('bills', 'returns'):
            s[k]['amount'] = r2(s[k]['amount'])
        s['debit'], s['credit'] = r2(s['debit']), r2(s['credit'])
        # What the account stood at when the window opened, so the row reads
        # the way the months behind it do: opening, debit, credit, closing.
        s['opening'] = (None if s['owed'] is None
                        else r2(s['owed'] - s['debit'] + s['credit']))
        s.setdefault('accounted', True)
        s.setdefault('billed_to_date', 0.0)
        s['owed'] = r2(s['owed']) if s['owed'] is not None else None
        s['over_limit'] = bool(s['credit_limit'] > 0 and s['owed']
                               and s['owed'] > s['credit_limit'])
        moved = s['entries'] or s['bills']['count'] or s['returns']['count']
        if scope != 'all' and s['key'] not in trade:
            continue
        if only_owing and not (s['owed'] and s['owed'] > 0.005):
            continue
        if not moved and not (s['owed'] and abs(s['owed']) >= 0.005):
            continue
        s['trade'] = s['key'] in trade
        out.append(s)
    out.sort(key=lambda x: (-(x['owed'] or 0), -x['bills']['amount'], x['name']))
    return dict(mode='all',
                unaccounted=sum(1 for x in out if not x['accounted']), from_=frm, to=to, rows=out, count=len(out),
                scope=scope, money_book=books,
                money_book_name=_book_names(conn, books),
                billed=r2(sum(x['bills']['amount'] for x in out)),
                debit=r2(sum(x['debit'] for x in out)),
                credit=r2(sum(x['credit'] for x in out)),
                bill_count=sum(x['bills']['count'] for x in out),
                entry_count=sum(x['entries'] for x in out),
                owed=r2(sum(x['owed'] for x in out
                            if x['owed'] and x['owed'] > 0)),
                advance=r2(-sum(x['owed'] for x in out
                                if x['owed'] and x['owed'] < 0)))


def _months_between(first, last):
    """Every month from one to the other, the empty ones included.

    SHREE draws all twelve rows of a year whether anything happened in them
    or not, and a month with nothing in it is worth seeing.
    """
    out, y, m = [], int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    while (y, m) <= (ly, lm) and len(out) < 600:
        out.append('%04d-%02d' % (y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _account_entries(conn, account, frm, to):
    """Every posting of the account, in SHREE's own ledger columns.

    Its *Ledger of ...* screen is `Date | Particulars | Credit | Debit`, where
    the particulars is the **account on the other side** -- SALES A/C, KOTAK
    BANK : B. S. TRADERS, BALKRISHNA STEEL TRADERS (GST) -- and the number and
    reference sit on a second line beneath it. There is no running balance
    column there: the opening is the first row and the closing is struck at
    the foot, the way a T account balances.
    """
    companies = {c['id']: c for c in list_companies(conn)}
    firms = {i: c['name'] for i, c in companies.items()}
    no_tax = {i for i, c in companies.items()
              if (c['tax_mode'] or 'gst') == 'none'}
    rows = []
    for co, lid in account:
        where, args = '', [co, lid]
        if frm:
            where += ' AND p.date>=?'
            args.append(frm)
        if to:
            where += ' AND p.date<=?'
            args.append(to)
        # The other side of each entry, gathered in one pass rather than a
        # query per row -- a long account is thousands of them.
        against, ids = {}, {}
        for r in conn.execute(
                'SELECT p.id pid, l2.name nm, l2.id lid FROM posting p '
                'JOIN posting p2 ON p2.company_id=p.company_id '
                '  AND p2.doc_kind=p.doc_kind AND p2.doc_id=p.doc_id '
                '  AND p2.ledger_id<>p.ledger_id '
                'JOIN ledger l2 ON l2.id=p2.ledger_id '
                'WHERE p.company_id=? AND p.ledger_id=? AND p.doc_id>0'
                + where, args):
            seen = against.setdefault(r['pid'], [])
            if r['nm'] not in seen:
                seen.append(r['nm'])
            ids.setdefault(r['pid'], set()).add(r['lid'])
        for p in conn.execute(
                'SELECT p.id, p.date, p.doc_kind, p.doc_id, p.doc_no, '
                '       p.debit, p.credit, p.narration FROM posting p '
                'WHERE p.company_id=? AND p.ledger_id=?' + where
                + ' ORDER BY p.date, p.id', args):
            kind = p['doc_kind'] or 'OPENING'
            challan = kind == 'SALE' and co in no_tax
            names = against.get(p['id']) or []
            rows.append(dict(
                date=p['date'], doc_kind=kind,
                kind='Challan' if challan else POSTING_LABEL.get(kind,
                                                                 kind.title()),
                doc_id=p['doc_id'] or 0, no=p['doc_no'] or '',
                particulars=', '.join(names[:3])
                            + (' +%d' % (len(names) - 3) if len(names) > 3
                               else ''),
                # The one account on the other side, when there is only one --
                # the screen opens it from the particulars.
                other_id=(next(iter(ids[p['id']]))
                          if len(ids.get(p['id']) or ()) == 1 else None),
                ref=_posting_ref(kind, p['doc_no'] or '',
                                 txt_or_blank(p['narration']), challan),
                narration=txt_or_blank(p['narration']),
                firm=firms.get(co, ''), company_id=co,
                debit=r2(p['debit']), credit=r2(p['credit'])))
    rows.sort(key=lambda r: (r['date'] or '', r['kind'], r['no']))
    return rows


def _entries_with_gst(conn, entries):
    """The account's lines, each challan and its GST journal as one.

    Asked for on 15 Sep 2026: a challan and the GST raised on it are one sale,
    so the account shows them as one line -- "Challan + GST", the two added,
    the challan's date and number -- paired the way the ageing screens pair
    them (`_join_gst`). **Only where both fall in the same month.** The
    monthly summary is SHREE's own month totals to the rupee; tax written on
    1 August cannot be pulled back onto a challan of 31 July without moving
    it between months, so those few stay two lines.
    """
    claims, line_of, journal_line = [], {}, {}
    for e in entries:
        if e['debit'] <= 0.004 or not e['doc_id']:
            continue
        if e['kind'] == 'Challan' or e['doc_kind'] == 'JOURNAL':
            c = dict(kind=e['doc_kind'], doc_id=e['doc_id'], date=e['date'],
                     no=e['no'], amount=e['debit'], open=e['debit'],
                     ref=e.get('narration') or '')
            claims.append(c)
            line_of[id(c)] = e
            if e['doc_kind'] == 'JOURNAL':
                journal_line.setdefault(e['doc_id'], []).append(e)
    if not journal_line or not any(c['kind'] == 'SALE' for c in claims):
        return entries
    gone = set()
    for c in _join_gst(conn, claims):
        if not c.get('gst_ids'):
            continue
        line = line_of[id(c)]
        parts = [journal_line.get(d, []) for d in c['gst_ids']]
        if any(len(p) != 1 for p in parts):
            continue
        parts = [p[0] for p in parts]
        if any((p['date'] or '')[:7] != (line['date'] or '')[:7] for p in parts):
            continue                  # a month apart: kept as two lines
        gst = r2(sum(p['debit'] for p in parts))
        line.update(kind='Challan + GST', challan_amount=line['debit'], gst=gst,
                    gst_ref=' + '.join(p['narration'] or p['no'] for p in parts),
                    debit=r2(line['debit'] + gst))
        gone.update(id(p) for p in parts)
    return [e for e in entries if id(e) not in gone]


def _opening_before(conn, account, frm):
    """What the account stood at before the window opened."""
    total = 0.0
    for co, lid in account:
        total += float(one(conn.execute(
            'SELECT COALESCE(opening_bal, 0) b FROM ledger WHERE id=?',
            (lid,)))['b'] or 0)
        if frm:
            total += float(one(conn.execute(
                'SELECT COALESCE(SUM(debit - credit), 0) b FROM posting '
                'WHERE company_id=? AND ledger_id=? AND date<?',
                (co, lid, frm)))['b'] or 0)
    return total


def _account_report(conn, refs, account, accounted, frm, to, led, books):
    """One account, as SHREE's two ledger views: the months and the entries.

    SHREE has one *Account Ledger* with two ways of reading it -- *Monthly
    Summary*, which is `Month | Credit | Debit | Balance | C/D` with every
    month of the window drawn, and the datewise ledger, which is every entry
    with the account on the other side of it. Both are the same postings, so
    both are built here at once and the screen chooses.

    The month totals are not worked out from the documents. `AMAS` stores them
    on the ledger's own record (`APCR`/`APDB` through `MRCR`/`MRDB`) and they
    are the totals of every entry, so totalling the entries reproduces SHREE
    to the rupee. Splitting them into challans and receipts does not: the tax
    on a challan is a journal, and it would have nowhere to go.
    """
    firms = {c['id']: c['name'] for c in list_companies(conn)}
    bals = _balances_by_book(conn, to)
    opening = _opening_before(conn, account, frm)
    entries = _entries_with_gst(conn, _account_entries(conn, account, frm, to))

    dated = sorted(e['date'] for e in entries if e['date'])
    now = datetime.date.today().isoformat()
    first = (frm or (dated[0] if dated else now))[:7]
    last = (to or (dated[-1] if dated else now))[:7]
    if last < first:
        last = first

    months = {ym: dict(month=ym, mon=ym[5:7], label=month_label(ym),
                       credit=0.0, debit=0.0, kinds={}, docs=[])
              for ym in _months_between(first + '-01', last + '-01')}
    stray = []
    for e in entries:
        m = months.get((e['date'] or '')[:7])
        if m is None:                      # outside the drawn range
            stray.append(e)
            continue
        m['credit'] += e['credit']
        m['debit'] += e['debit']
        m['kinds'][e['kind']] = m['kinds'].get(e['kind'], 0) + 1
        m['docs'].append(e)

    running, out = r2(opening), []
    for ym in sorted(months):
        m = months[ym]
        m['opening'] = running
        m['credit'], m['debit'] = r2(m['credit']), r2(m['debit'])
        running = r2(running + m['debit'] - m['credit'])
        m['balance'] = running
        m['dc'] = 'DB' if running >= 0 else 'CR'
        m['count'] = len(m['docs'])
        out.append(m)

    # A challan is a document, not a posting, so it is counted once however
    # many lines it hit -- the figure the user checks against the challan book.
    challans = {(e['company_id'], e['doc_id']) for e in entries
                if e['kind'] in ('Challan', 'Challan + GST') and e['doc_id']}
    if not accounted:
        # No account in the book the money is written in, so what is owed is
        # not knowable here. The months still show what the register holds;
        # the balance column is left empty rather than filled with a figure
        # that only adds up because nothing was ever taken off it.
        for m in out:
            m['opening'] = m['balance'] = None
            m['dc'] = ''
    return dict(
        mode='one', from_=frm, to=to, months=out, entries=entries,
        party=dict(led), ledger=dict(led), accounted=accounted,
        stray=len(stray),
        opening=r2(opening) if accounted else None,
        closing=r2(running) if accounted else None,
        owed=r2(running) if accounted else None,
        billed_to_date=r2(running),
        money_book=books, money_book_name=_book_names(conn, books),
        account_book=', '.join(sorted({firms.get(co, '') for co, _ in account})),
        in_books=[dict(company_id=co, company=firms.get(co, ''), ledger_id=lid,
                       balance=r2(bals.get(co, {}).get(lid, 0)),
                       is_account=((co, lid) in account))
                  for co, lid in sorted(refs)],
        debit=r2(sum(m['debit'] for m in out)),
        credit=r2(sum(m['credit'] for m in out)),
        total_debit=r2(sum(m['debit'] for m in out)),
        total_credit=r2(sum(m['credit'] for m in out)),
        entry_count=len(entries),
        billed=r2(sum(e.get('challan_amount', e['debit']) for e in entries
                      if e['kind'] in ('Challan', 'Challan + GST'))),
        bill_count=len(challans),
        paid=r2(sum(e['credit'] for e in entries
                    if e['doc_kind'] in MONEY_KINDS)),
        paid_out=r2(sum(e['debit'] for e in entries
                        if e['doc_kind'] in MONEY_KINDS)))


# How long a document allows before it is due. The terms are typed by hand
# and have been for ten years -- '7 DAYS', '30days', '30DAYS', 'SAME DAY',
# 'CASH', and a hundred and sixty other spellings -- so the number is read
# out of whatever was written and anything without one is taken as due at
# once, which is what 'CASH', 'SAME DAYS' and every misspelling of them mean.
# This reads 15,251 of the 15,303 documents that carry terms at all.
_DAYS_RE = re.compile(r'(\d+)')


def terms_days(text, fallback=0):
    """The credit period a document allows, in days."""
    said = (text or '').strip()
    if not said:
        return fallback
    hit = _DAYS_RE.search(said)
    return int(hit.group(1)) if hit else 0


def _usual_terms(conn, party_ids):
    """The period this customer is usually given, for entries that carry none.

    The tax on a challan is a journal and carries no terms of its own; it
    falls due when the challan it belongs to falls due, and the customer's
    usual period is the closest this data can come to saying when that is.
    """
    if not party_ids:
        return 0, ''
    tally = {}
    for r in conn.execute(
            "SELECT payment_terms t, COUNT(*) n FROM invoice "
            "WHERE doc_type IN ('SALE','DEBIT') AND party_id IN (%s) "
            "AND COALESCE(payment_terms,'')<>'' GROUP BY payment_terms"
            % ','.join('?' * len(party_ids)), list(party_ids)):
        tally[r['t']] = tally.get(r['t'], 0) + r['n']
    if not tally:
        return 0, ''
    said = max(tally, key=lambda t: tally[t])
    return terms_days(said), said


AGE_BUCKETS = ((1, 30), (31, 60), (61, 90), (91, 0))

# What each kind of open item is called where a customer can read it. A challan
# is the goods going out; the tax on it is a journal beside it.
DUE_KIND = {'SALE': 'Challan', 'DEBIT': 'Debit note', 'JOURNAL': 'GST',
            'OPENING': 'Opening balance', 'PURCHASE': 'Purchase',
            'RECEIPT': 'Receipt', 'PAYMENT': 'Payment', 'CONTRA': 'Contra'}


def _band(late):
    """Which ageing band a debt this many days past due falls in."""
    for lo, hi in AGE_BUCKETS:
        if late >= lo and (hi == 0 or late <= hi):
            return '%d-%d' % (lo, hi) if hi else '%d+' % lo
    return ''


def _band_name(b):
    return '%d-%d' % b if b[1] else '%d+' % b[0]


def _bill_links(conn, claims):
    """Say which bill each open challan went out on, and in whose books.

    The screens open a challan, its GST journal and its bill from the line,
    and a bill sits in a different firm from the challan it came from.
    """
    ids = sorted({c['doc_id'] for c in claims
                  if c.get('kind') == 'SALE' and c.get('doc_id')})
    found = {}
    for part in _chunks(ids, 500):
        for r in conn.execute(
                'SELECT ch.id, b.id AS bill_id, b.company_id AS bill_company, '
                '       b.series, b.invoice_no FROM invoice ch '
                'JOIN invoice b ON b.id=ch.billed_invoice '
                'WHERE ch.id IN (%s)' % ','.join('?' * len(part)), part):
            found[r['id']] = r
    for c in claims:
        r = found.get(c.get('doc_id')) if c.get('kind') == 'SALE' else None
        if r:
            c.update(bill_id=r['bill_id'], bill_company=r['bill_company'],
                     bill_no=docno(dict(r)))


def _settle(claims, credits, as_on):
    """Apply what was paid to the oldest debt first, then age what is left.

    **The arithmetic both ageing screens run** -- one customer's own page and
    the whole book -- because two tellings of it would come to disagree about
    what a customer owes, and either way the figure has to tie to the account
    balance. The gathering differs (one account, or every account that owes in
    one pass); the settling does not.

    Two ages come out of it, and they are different questions:

      * **`age_days`** -- how long ago the bill was written. That is how old
        the money is, whatever was agreed about paying it.
      * **`overdue_days`** -- how long ago it fell *due*, which is nothing at
        all until the credit period on the document has run out.

    A customer on 90 days with a two-month-old bill is holding 60-day-old
    money and is not late by a single day, and a screen that shows only one of
    those two numbers cannot say so.
    """
    claims.sort(key=lambda c: (c['date'], c['no']))
    # Oldest first: the way a payment is applied when nobody says otherwise.
    pot = sum(credits)
    for c in claims:
        if pot <= 0:
            break
        take = min(pot, c['open'])
        c['open'] -= take
        pot -= take

    owed = due_now = not_due = 0.0
    buckets, oldest, oldest_bill, openlist = {}, '', '', []
    for c in claims:
        if c['open'] <= 0.004:
            continue
        owed += c['open']
        late = days_between(c['due'], as_on)
        c['open'] = r2(c['open'])
        c['overdue_days'] = late if late > 0 else 0
        c['age_days'] = max(0, days_between(c['date'], as_on))
        if late > 0:
            due_now += c['open']
            band = _band(late)
            buckets[band] = r2(buckets.get(band, 0) + c['open'])
            if not oldest or c['due'] < oldest:
                oldest = c['due']
        else:
            not_due += c['open']
        if not oldest_bill or c['date'] < oldest_bill:
            oldest_bill = c['date']
        openlist.append(c)

    return dict(
        owed=r2(owed), due_now=r2(due_now), not_due=r2(not_due),
        buckets=[dict(band=_band_name(b), amount=buckets.get(_band_name(b), 0.0))
                 for b in AGE_BUCKETS],
        oldest_due=oldest,
        oldest_overdue_days=days_between(oldest, as_on) if oldest else 0,
        # The oldest rupee still standing, and how long it has been standing.
        oldest_bill=oldest_bill,
        owed_for_days=(max(0, days_between(oldest_bill, as_on))
                       if oldest_bill else 0),
        credited=r2(sum(credits)),
        claims=openlist, claim_count=len(openlist),
        settled=len(claims) - len(openlist))


# ---------------------------------------------- a challan and its GST ---

# SHREE writes the bill a GST journal belongs to as its reference: "GT/14",
# "GT=26", "T-165" -- a series and a number with anything between them.
_GST_REF = re.compile(r'^\s*([A-Z]{1,4})\s*[/=\- ]\s*0*(\d+)(?:\s|$)', re.I)
# The rates a steel bill is taxed at, for a journal no bill can be found for.
_GST_RATES = (0.18, 0.12, 0.05, 0.28)


def _taxes_to(total, tax):
    """Whether this much tax is what a challan of this total would carry."""
    return any(abs(total * rate - tax) <= 2.0 for rate in _GST_RATES)


def _tax_journals(conn, voucher_ids):
    """{journal id: billing firm} for the journals among these that carry tax.

    A GST journal credits the account standing for the billing firm -- the
    same accounts `allocate_customers.py` reads a customer's firm off, which is
    also how the firm is known here -- or, in the VAT years, a VAT or TAX
    account, where the firm is left blank. A late-payment charge or a
    two-rupee discount is a journal too, and is neither tax nor any challan's.
    """
    ids = sorted({i for i in voucher_ids if i})
    if not ids:
        return {}
    firm_of = {re.sub(r'\s+', ' ', n).upper(): code
               for code, names in BRIDGE_HINTS for n in names}
    out = {}
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        for r in conn.execute(
                'SELECT vl.voucher_id, l.name, g.code gcode FROM voucher_line vl '
                'JOIN ledger l ON l.id=vl.ledger_id '
                'JOIN account_group g ON g.id=l.group_id '
                'WHERE vl.voucher_id IN (%s) AND vl.credit>0'
                % ','.join('?' * len(chunk)), chunk):
            if r['gcode'] == '2101':
                continue                  # a customer, not a tax account
            name = re.sub(r'\s+', ' ', r['name'] or '').strip().upper()
            if name in firm_of:
                out[r['voucher_id']] = firm_of[name]
            elif re.search(r'\((GST|VAT)\)|\bVAT\b|\bGST\b|\bTAX A/?C\b', name):
                out.setdefault(r['voucher_id'], '')
    return out


def _challan_bills(conn, challan_ids):
    """{challan id: (series, number, fy)} of the bill each challan went out on."""
    ids = sorted({i for i in challan_ids if i})
    out = {}
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        for r in conn.execute(
                'SELECT ch.id, b.series, b.invoice_no, b.fy FROM invoice ch '
                'JOIN invoice b ON b.id=ch.billed_invoice '
                'WHERE ch.id IN (%s)' % ','.join('?' * len(chunk)), chunk):
            no = re.sub(r'\D', '', r['invoice_no'] or '')
            if no:
                out[r['id']] = ((r['series'] or '').strip().upper(),
                                str(int(no)), r['fy'])
    return out


def _challan_gst_vouchers(conn, challan_ids):
    """{challan id: journal id} -- the GST journal a bill raised in this app
    posted for the challan it came from (`invoice.gst_voucher`)."""
    ids = sorted({i for i in challan_ids if i})
    out = {}
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        for r in conn.execute(
                'SELECT from_challan, gst_voucher FROM invoice '
                'WHERE gst_voucher IS NOT NULL AND from_challan IN (%s)'
                % ','.join('?' * len(chunk)), chunk):
            out[r['from_challan']] = r['gst_voucher']
    return out


def _gst_bills(conn):
    """{(firm, series, number, fy): [(taxable, tax, date)]} -- every GST sale bill."""
    out = {}
    for r in conn.execute(
            "SELECT c.code, i.series, i.invoice_no, i.fy, i.taxable, "
            "       COALESCE(i.cgst,0)+COALESCE(i.sgst,0)+COALESCE(i.igst,0)"
            "       +COALESCE(i.cess,0) tax, i.invoice_date "
            "FROM invoice i JOIN company c ON c.id=i.company_id "
            "WHERE c.tax_mode='gst' AND i.doc_type='SALE'"):
        no = re.sub(r'\D', '', r['invoice_no'] or '')
        if no:
            out.setdefault((r['code'], (r['series'] or '').strip().upper(),
                            str(int(no)), r['fy']), []).append(
                (float(r['taxable'] or 0), float(r['tax'] or 0),
                 r['invoice_date']))
    return out


def _join_gst(conn, claims, links=None, taxed=None, bills=None, owned=None):
    """Put each GST journal back on the challan it was raised on.

    **There is one challan for every GST journal** -- the same sale written
    twice: the goods without tax on the challan, then the tax on them as a
    journal when the bill went out. Listed apart, one load read as two debts,
    one of them nothing but tax, and the tax fell due on the customer's usual
    terms instead of with its own challan. Joined, the pair is one item: the
    challan's date, number and terms, the amounts added. A challan that was
    never billed stays on its own, and so does any journal that is not tax.
    Asked for on 15 Sep 2026.

    A journal is joined by the first of these that names exactly one challan
    -- and where several fit, only if one is plainly the nearest in date:

      1. **the bill link** -- the journal names its bill ("GT/14") and the
         challan carries the bill it went out on (`billed_invoice`);
      2. **the bill's value** -- that bill, in the firm the journal's other
         side stands for, has a taxable value equal to a challan's total;
      3. **the tax itself** -- 5, 12, 18 or 28 per cent of a challan's total
         is the journal to two rupees;
      4. **a challan billed in parts** -- two or three named bills whose
         values add up to one challan's total;
      5. **a bill re-priced** -- the named bill is 90 to 102 per cent of a
         challan's total, the few per cent a bill is sometimes knocked down.

    The challan has to fall from thirty days before the bill to two days
    after it, the window `link_challans.py` pairs in. What none of these
    places -- tax on a bill written in somebody else's name, a VAT-year entry
    with no bill number -- stays a GST item of its own.

    Both ageing screens call this before `_settle`, so a customer's own page
    and the whole book still come to the same figures. `links`, `taxed` and
    `bills` can be read once for the whole book and passed in.
    """
    for c in claims:
        c['item'] = DUE_KIND.get(c['kind'], (c['kind'] or '').title())
    chals = [c for c in claims if c['kind'] == 'SALE' and c['doc_id']]
    jours = [c for c in claims if c['kind'] == 'JOURNAL' and c['doc_id']]
    if not jours:
        return claims
    if taxed is None:
        taxed = _tax_journals(conn, [j['doc_id'] for j in jours])
    if links is None:
        links = _challan_bills(conn, [c['doc_id'] for c in chals])
    if bills is None:
        bills = _gst_bills(conn)
    tax = []
    for j in jours:
        if j['doc_id'] in taxed:
            tax.append(j)
        else:
            j['item'] = 'Journal'
    used, gone = set(), set()

    def join(ch, j, how):
        if 'gst' not in ch:
            ch.update(challan_amount=ch['amount'], gst=0.0, gst_ref='',
                      gst_match=how, gst_ids=[])
        ch['gst_ids'].append(j['doc_id'])
        ch['gst'] = r2(ch['gst'] + j['amount'])
        ref = j['ref'] or j['no']
        ch['gst_ref'] = ch['gst_ref'] + ' + ' + ref if ch['gst_ref'] else ref
        ch['amount'] = r2(ch['amount'] + j['amount'])
        ch['open'] += j['open']
        ch['item'] = 'Challan + GST'
        used.add(id(ch))
        gone.add(id(j))

    def free():
        return [c for c in chals if id(c) not in used]

    def waiting():
        return [j for j in tax if id(j) not in gone]

    # Each challan's date read once, and the challans in date order, so a
    # journal is only compared with the ones inside its thirty-two days --
    # not with every challan the customer ever had.
    dayof = {id(c): _day(c['date']) for c in chals}
    dated = sorted((c for c in chals if dayof[id(c)] is not None),
                   key=lambda c: dayof[id(c)])
    days = [dayof[id(c)] for c in dated]
    undated = [c for c in chals if dayof[id(c)] is None]

    def near(c, date):
        a, b = dayof[id(c)], _day(date)
        gap = 0 if a is None or b is None else b - a   # as days_between
        return -2 <= gap <= 30, abs(gap)

    def around(date):
        """(gap, challan) for every free challan `near` accepts."""
        t = _day(date)
        if t is None:
            return [(0, c) for c in chals if id(c) not in used]
        lo = bisect.bisect_left(days, t - 30)
        hi = bisect.bisect_right(days, t + 2)
        found = [(t - days[i], dated[i]) for i in range(lo, hi)
                 if id(dated[i]) not in used]
        found += [(0, c) for c in undated if id(c) not in used]
        return found

    def nearest(cands):
        cands.sort(key=lambda x: x[0])
        if cands and (len(cands) == 1 or cands[0][0] < cands[1][0]):
            return cands[0][1]
        return None

    # The bill each journal names, looked up in the firm its other side
    # stands for -- or in both GST firms, keeping the one whose tax it is.
    named = {}
    for j in tax:
        m = _GST_REF.match(j['ref'] or '')
        if not m:
            continue
        key = (m.group(1).upper(), str(int(m.group(2))), db.fy_of(j['date']))
        firms = [taxed[j['doc_id']]] if taxed[j['doc_id']] else ['BST', 'BSI']
        found = [b for f in firms for b in bills.get((f,) + key, ())]
        if len(found) > 1:
            found = [b for b in found if abs(b[1] - j['amount']) <= 2.0]
        if len(found) == 1:
            named[id(j)] = found[0]

    # 0. a bill raised in this app names its own journal outright
    if owned is None:
        owned = _challan_gst_vouchers(conn, [c['doc_id'] for c in chals])
    if owned:
        by_id = {j['doc_id']: j for j in jours}
        for c in chals:
            j = by_id.get(owned.get(c['doc_id']))
            if j is not None and id(j) not in gone and id(c) not in used:
                join(c, j, 'bill link')

    # 1. the bill link
    by_bill = {}
    for c in chals:
        if c['doc_id'] in links:
            by_bill.setdefault(links[c['doc_id']], []).append(c)
    for j in tax:
        m = _GST_REF.match(j['ref'] or '')
        if not m or id(j) in gone:
            continue
        key = (m.group(1).upper(), str(int(m.group(2))), db.fy_of(j['date']))
        cands = [c for c in by_bill.get(key, ()) if id(c) not in used]
        if len(cands) > 1:                # the same number in both GST firms
            cands = [c for c in cands if _taxes_to(c['amount'], j['amount'])]
        if len(cands) == 1:
            join(cands[0], j, 'bill link')

    # 2. the named bill's value is a challan's total
    for j in waiting():
        if id(j) not in named:
            continue
        taxable, _tax, bdate = named[id(j)]
        hit = nearest([(abs(gap), c) for gap, c in around(bdate)
                       if abs(c['amount'] - taxable) <= 20])
        if hit:
            join(hit, j, 'bill value')

    # 3. the tax is a rate on a challan's total
    for j in waiting():
        hit = nearest([(abs(gap), c) for gap, c in around(j['date'])
                       if _taxes_to(c['amount'], j['amount'])])
        if hit:
            join(hit, j, 'tax rate')

    # 4. one challan billed in two or three parts
    for c in free():
        parts = [j for j in waiting()
                 if id(j) in named and near(c, named[id(j)][2])[0]]
        fits = [combo for k in (2, 3)
                for combo in itertools.combinations(parts, k)
                if abs(sum(named[id(j)][0] for j in combo) - c['amount']) <= 20 * k]
        if len(fits) == 1:
            for j in fits[0]:
                join(c, j, 'billed in parts')

    # 5. a bill knocked down a few per cent from its challan
    for j in waiting():
        if id(j) not in named:
            continue
        taxable, _tax, bdate = named[id(j)]
        hit = nearest([(abs(gap), c) for gap, c in around(bdate)
                       if c['amount'] > 0
                       and 0.90 <= taxable / c['amount'] <= 1.02])
        if hit:
            join(hit, j, 'bill re-priced')

    return [c for c in claims if id(c) not in gone]


def report_due(conn, cid, q):
    """What a customer owes on a given day, and how much of it has come due.

    **Some customers pay after a few months**, and a single balance cannot
    say whether that is late. This settles the account the way a ledger clerk
    would -- every credit against the oldest debt first -- and then asks of
    each rupee still standing: was it due by this date, and if so, how long
    ago?

    Every debit is a claim from the day it was written: the challan, the tax
    journal beside it, anything else charged. Its due date is the challan's
    own payment terms where it has them, and the customer's usual terms where
    it does not -- a tax journal carries none of its own but falls due with
    the challan it belongs to.
    """
    if not q.get('ledger_id') and not q.get('party_id'):
        raise ApiError('Select a customer.')
    lid = qint(q.get('ledger_id') or q.get('party_id'), 'customer')
    led = one(conn.execute(
        'SELECT l.*, g.name AS group_name, g.is_party, c.name AS company_name '
        'FROM ledger l JOIN account_group g ON g.id=l.group_id '
        'JOIN company c ON c.id=l.company_id WHERE l.id=?', (lid,)))
    if not led:
        raise ApiError('Customer not found.', 404)
    as_on = q.get('as_on') or q.get('to') or datetime.date.today().isoformat()
    books = money_books(conn)
    one_book = (q.get('scope') or '').lower() in ('book', 'ledger', 'firm')
    refs, account, accounted = _resolve_account(conn, led, books, one_book)

    # The terms written on each of this customer's own documents, and the
    # period they are usually given.
    ids = [lid for _, lid in refs]
    doc_terms = {}
    for r in conn.execute(
            "SELECT id, payment_terms FROM invoice WHERE party_id IN (%s)"
            % ','.join('?' * len(ids)), ids):
        if (r['payment_terms'] or '').strip():
            doc_terms[r['id']] = r['payment_terms']
    # Counted over the books the money is in, as Who owes us counts it. Since
    # the bills in the GST firms were given their terms (19 Sep 2026) they
    # outvote the challans -- SHREE SANTOSH read "7 DAYS" here and "90 DAYS"
    # there, and the two screens that say they agree did not (24 Sep 2026).
    usual_days, usual_said = _usual_terms(conn, [lid for _, lid in account])

    # Every entry on the account up to the day asked about, oldest first.
    claims, credits = [], []
    opening = 0.0
    for co, led_id in account:
        opening += float(one(conn.execute(
            'SELECT COALESCE(opening_bal, 0) b FROM ledger WHERE id=?',
            (led_id,)))['b'] or 0)
        for p in conn.execute(
                'SELECT date, doc_kind, doc_id, doc_no, debit, credit, '
                '       narration FROM posting WHERE company_id=? '
                'AND ledger_id=? AND date<=? ORDER BY date, id',
                (co, led_id, as_on)):
            if float(p['debit'] or 0) > 0.004:
                said = doc_terms.get(p['doc_id']) if p['doc_kind'] == 'SALE' \
                    else None
                days = terms_days(said, usual_days) if said else usual_days
                claims.append(dict(
                    company_id=co,
                    date=p['date'], kind=p['doc_kind'] or 'OPENING',
                    doc_id=p['doc_id'] or 0, no=p['doc_no'] or '',
                    ref=txt_or_blank(p['narration']),
                    amount=r2(p['debit']), open=float(p['debit'] or 0),
                    terms=said or usual_said, days=days,
                    due=add_days(p['date'], days)))
            elif float(p['credit'] or 0) > 0.004:
                credits.append(float(p['credit'] or 0))
    # An opening balance owed from before the books begin is the oldest claim
    # of all, and nothing says when it fell due.
    if opening > 0.004:
        first = claims[0]['date'] if claims else as_on
        claims.insert(0, dict(date=first, kind='OPENING', doc_id=0, no='',
                              ref='Opening balance', amount=r2(opening),
                              open=opening, terms='', days=0, due=first))
    elif opening < -0.004:
        credits.insert(0, -opening)

    claims = _join_gst(conn, claims)
    got = _settle(claims, credits, as_on)
    if q.get('payments'):
        # The customer's page off Who owes us shows every transaction, not
        # only what is still owed (6 Oct 2026: "make sure each and every
        # transaction with that customer is shown"): every bill, paid off or
        # not, with what is left on it, beside every payment.
        for c in claims:
            c['open'] = r2(max(0.0, c['open']))
        _bill_links(conn, claims)
        got['bills'] = claims
        got['payments'] = _payments_of(conn, account, as_on, opening)
    else:
        _bill_links(conn, got['claims'])
    return dict(
        got,
        party=dict(led), as_on=as_on, accounted=accounted,
        usual_terms=usual_said, usual_days=usual_days)


def _payments_of(conn, account, as_on, opening=0.0):
    """Everything that took money off what a customer owes, up to a day.

    Asked on 24 Sep 2026, on Who owes us: *"show any kind of payment done by
    the customer ... the newest payment or bill on the top."* The unpaid bills
    alone say what is left and nothing of what came in. So every credit on the
    account comes back -- money received into a bank or the cash, and the
    other ways a balance comes down (a return, a journal, an advance brought
    in as the opening balance) -- each with the account on the other side, the
    same reading the account's own page gives it. Oldest first; the screen
    turns it round.
    """
    out = []
    if opening < -0.004:
        out.append(dict(date='', doc_kind='OPENING', kind='Opening balance',
                        doc_id=0, no='', company_id=0, particulars='',
                        other_id=None, narration='Paid in advance before the '
                        'books begin', amount=r2(-opening)))
    for e in _account_entries(conn, account, '', as_on):
        if e['credit'] > 0.004:
            out.append(dict(
                date=e['date'], doc_kind=e['doc_kind'], kind=e['kind'],
                doc_id=e['doc_id'], no=e['no'], company_id=e['company_id'],
                particulars=e['particulars'], other_id=e['other_id'],
                narration=e['narration'], amount=e['credit']))
    return out


def report_ageing(conn, cid, q):
    """Every customer who owes us money, oldest money first.

    **Due & ageing answers for one customer; this asks the same question of
    the whole book.** *Who owes us, how old is the money, and how long have
    they been holding it* is not a question a balance can answer -- and it
    was only answerable here one customer at a time, which means knowing
    which customer to ask about first.

    Each account is settled exactly as its own page settles it -- every credit
    against the oldest debt first, each debit due by the terms written on the
    document -- through the one `_settle` both screens call. So a row here and
    that customer's own page cannot disagree, and both tie to the balance.

    **Only the accounts book can say what is owed.** A customer the GST firms
    bill who was never written in an accounts book has no receivable anybody
    can work out: their register balance is everything ever billed to them,
    never reduced by a rupee. They are counted and named as not known, never
    added in -- the same rule `_account_refs` states for the customer list.
    """
    as_on = q.get('as_on') or q.get('to') or datetime.date.today().isoformat()
    books = money_books(conn)
    keys, _ = customer_keys(conn)
    ledgers = {r['id']: r for r in conn.execute(
        "SELECT l.id, l.company_id, l.name, l.city, l.phone, l.gstin, "
        "       l.credit_limit FROM ledger l "
        "JOIN account_group g ON g.id=l.group_id "
        "WHERE l.party_kind='customer' OR l.party_kind='' AND g.is_party=1")}

    refs_of = {}
    for (co, lid), key in keys.items():
        if lid in ledgers:
            refs_of.setdefault(key, []).append((co, lid))

    # Customers, not every account a rupee has passed through -- the same test
    # the customer list applies, so the two screens name the same people and
    # come to the same total. `scope=all` asks for the rest as well.
    scope = (q.get('scope') or 'trade').lower()
    trade = _trade_keys(conn, keys)

    # Who owes anything at all, before any of it is aged. Settling an account
    # is cheap but not free, and on these books a hundred and fifty owe out of
    # three thousand: the balance decides who is worth the work.
    bals = {co: ledger_balances(conn, co, as_on) for co in books}
    owing, unknown = {}, []
    for key, refs in refs_of.items():
        if scope != 'all' and key not in trade:
            continue
        account, accounted = _account_refs(refs, books)
        if not accounted:
            led = ledgers[account[0][1]]
            unknown.append(dict(name=led['name'], city=led['city'] or '',
                                ledger_id=led['id'],
                                company_id=led['company_id']))
            continue
        if sum(bals.get(co, {}).get(lid, 0) for co, lid in account) > 0.005:
            owing[key] = account
    unknown.sort(key=lambda u: u['name'])

    lids = sorted({lid for account in owing.values() for _, lid in account})
    if not lids:
        return dict(as_on=as_on, rows=[], unknown=unknown,
                    unknown_count=len(unknown), buckets=[], totals=dict(
                        count=0, owed=0.0, due_now=0.0, not_due=0.0,
                        claims=0), money_book=_book_names(conn, books))

    # Everything the settlement needs, in one pass each rather than the four
    # queries an account costs on its own page.
    ph = ','.join('?' * len(lids))
    doc_terms = {}
    for r in conn.execute(
            "SELECT id, payment_terms FROM invoice WHERE party_id IN (%s) "
            "AND COALESCE(payment_terms,'')<>''" % ph, lids):
        doc_terms[r['id']] = r['payment_terms']

    tally = {}
    for r in conn.execute(
            "SELECT party_id, payment_terms t, COUNT(*) n FROM invoice "
            "WHERE doc_type IN ('SALE','DEBIT') AND party_id IN (%s) "
            "AND COALESCE(payment_terms,'')<>'' "
            'GROUP BY party_id, payment_terms' % ph, lids):
        tally.setdefault(r['party_id'], {})[r['t']] = r['n']

    opening = {r['id']: float(r['opening_bal'] or 0) for r in conn.execute(
        'SELECT id, opening_bal FROM ledger WHERE id IN (%s)' % ph, lids)}

    entries = {}
    for p in conn.execute(
            'SELECT company_id, ledger_id, date, doc_kind, doc_id, doc_no, '
            '       debit, credit, narration FROM posting '
            'WHERE company_id IN (%s) AND ledger_id IN (%s) AND date<=? '
            'ORDER BY date, id'
            % (','.join('?' * len(books)), ph), books + lids + [as_on]):
        entries.setdefault(p['ledger_id'], []).append(p)

    # Which bill each challan went out on, and which journals are tax -- read
    # once for the whole book, then every customer's GST is joined to its
    # challan before the account is settled.
    links = _challan_bills(conn, [
        p['doc_id'] for got in entries.values() for p in got
        if p['doc_kind'] == 'SALE' and float(p['debit'] or 0) > 0.004])
    taxed = _tax_journals(conn, [
        p['doc_id'] for got in entries.values() for p in got
        if p['doc_kind'] == 'JOURNAL' and float(p['debit'] or 0) > 0.004])
    bills = _gst_bills(conn)
    owned = _challan_gst_vouchers(conn, [
        p['doc_id'] for got in entries.values() for p in got
        if p['doc_kind'] == 'SALE' and float(p['debit'] or 0) > 0.004])

    rows, open_items = [], []
    for key, account in owing.items():
        mine = [lid for _, lid in account]
        # The usual period is counted over every ledger this one customer is
        # written in, the way `_usual_terms` counts it for their own page.
        counted = {}
        for lid in mine:
            for t, n in tally.get(lid, {}).items():
                counted[t] = counted.get(t, 0) + n
        usual_said = max(counted, key=lambda t: counted[t]) if counted else ''
        usual_days = terms_days(usual_said) if usual_said else 0

        claims, credits, opens = [], [], 0.0
        for lid in mine:
            opens += opening.get(lid, 0.0)
            for p in entries.get(lid, ()):
                if float(p['debit'] or 0) > 0.004:
                    said = (doc_terms.get(p['doc_id'])
                            if p['doc_kind'] == 'SALE' else None)
                    days = terms_days(said, usual_days) if said else usual_days
                    claims.append(dict(
                        company_id=p['company_id'],
                        date=p['date'], kind=p['doc_kind'] or 'OPENING',
                        doc_id=p['doc_id'] or 0, no=p['doc_no'] or '',
                        ref=txt_or_blank(p['narration']),
                        amount=r2(p['debit']), open=float(p['debit'] or 0),
                        terms=said or usual_said, days=days,
                        due=add_days(p['date'], days)))
                elif float(p['credit'] or 0) > 0.004:
                    credits.append(float(p['credit'] or 0))
        # An opening balance owed from before the books begin is the oldest
        # claim of all, and nothing says when it fell due.
        if opens > 0.004:
            first = claims[0]['date'] if claims else as_on
            claims.insert(0, dict(date=first, kind='OPENING', doc_id=0, no='',
                                  ref='Opening balance', amount=r2(opens),
                                  open=opens, terms='', days=0, due=first))
        elif opens < -0.004:
            credits.insert(0, -opens)

        claims = _join_gst(conn, claims, links, taxed, bills, owned)
        got = _settle(claims, credits, as_on)
        if got['owed'] <= 0.004:
            continue                # paid up by the time it was settled
        open_items += got['claims']
        led = ledgers[account[0][1]]
        name, gstin = led['name'], led['gstin'] or ''
        for _, lid in account:
            other = ledgers.get(lid)
            if other and other['gstin'] and not gstin:
                gstin, name = other['gstin'], other['name']
        # When the money was last seen, which is the other half of "how long
        # have they owed it" -- a customer paying every week is not the same
        # as one who has paid nothing since March.
        paid = [p['date'] for lid in mine for p in entries.get(lid, ())
                if float(p['credit'] or 0) > 0.004]
        rows.append(dict(
            got, key=key, name=name, city=led['city'] or '',
            phone=led['phone'] or '', gstin=gstin,
            ledger_id=led['id'], company_id=led['company_id'],
            credit_limit=r2(led['credit_limit'] or 0),
            over_limit=bool(led['credit_limit']
                            and got['owed'] > float(led['credit_limit'])),
            usual_terms=usual_said, usual_days=usual_days,
            last_paid=max(paid) if paid else '',
            quiet_days=days_between(max(paid), as_on) if paid else 0))

    # Oldest money at the top: the whole point of asking the book at once is
    # to be told who to chase before being told what the figures are.
    rows.sort(key=lambda r: (-r['owed_for_days'], -r['owed']))

    bands = [_band_name(b) for b in AGE_BUCKETS]
    totals = dict(
        count=len(rows), claims=sum(r['claim_count'] for r in rows),
        owed=r2(sum(r['owed'] for r in rows)),
        due_now=r2(sum(r['due_now'] for r in rows)),
        not_due=r2(sum(r['not_due'] for r in rows)),
        overdue_count=sum(1 for r in rows if r['due_now'] > 0.004))
    _bill_links(conn, open_items)
    return dict(
        as_on=as_on, scope=scope, rows=rows, totals=totals,
        buckets=[dict(band=b, amount=r2(sum(
            x['amount'] for r in rows for x in r['buckets'] if x['band'] == b)),
            count=sum(1 for r in rows for x in r['buckets']
                      if x['band'] == b and x['amount'] > 0.004))
            for b in bands],
        unknown=unknown, unknown_count=len(unknown),
        money_book=_book_names(conn, books))


# Units that count pieces, lengths or volumes rather than weigh. SHREE's own
# labels -- UNT, SHEE, S PI -- are none of these: its bill has one quantity
# column, headed "Quantity Kgs", so every line it wrote is a weight whatever
# the item's unit box happens to say.
COUNT_UNITS = {'NOS', 'NO', 'PCS', 'PC', 'PIECE', 'PIECES', 'BAG', 'BAGS',
               'SET', 'SETS', 'PAIR', 'PAIRS', 'BOX', 'PKT', 'DOZ', 'ROLL',
               'BTL', 'CAN', 'LTR', 'MTR', 'SQF', 'SQM', 'SQFT'}


def weighed_item(it):
    """Whether an item's stock figure is kilos of steel.

    A weight unit says so outright and a counting unit says it is not. Past
    that, the code decides: iron and steel are chapters 72 and 73, and an
    item with no code at all is one of SHREE's old steel names.
    """
    unit = (it['unit'] or '').strip().upper()
    if unit in WEIGHT_UNITS:
        return True
    if unit in COUNT_UNITS:
        return False
    hsn = (it['hsn'] or '').strip()
    return not hsn or hsn[:2] in ('72', '73')


def _cat_of(it, cats):
    """A product's Billing Stock category, '' when it has none -- or names
    one no longer on the list."""
    cat = (it['stock_cat'] or '') if 'stock_cat' in it.keys() else ''
    return cat if cat in cats else ''


def _stock_key(it, cats):
    """The line a product is counted on with both firms together: its HSN
    (its name, with none), within its category. The same code is the same
    steel whichever book bought it -- but HSN 72082790 is SHREE's code for
    plate and for hot-rolled sheet alike, so the category splits it."""
    base = (it['hsn'] or '').strip() or ('name:' + it['name'].strip().lower())
    cat = _cat_of(it, cats)
    return 'cat:%s|%s' % (cat, base) if cat else base


def _stock_of(conn, cid, q):
    """One firm's stock, item by item, between the dates asked for.

    Opening is what stood when `from` began, purchased and sold are what
    moved between the two dates, and in stock is what stood when `to` ended
    -- so each line adds up across. (In stock used to be today's figure
    whatever the dates said, and opening the item's own book figure, which
    for a SHREE item is whatever makes it come out at SHREE's closing stock
    and is often far below nothing.)
    """
    frm, to = q.get('from') or '', q.get('to') or ''
    # Blank `from` puts nothing before it and everything after it.
    sql = ('SELECT ii.item_id, i.doc_type, '
           '  SUM(CASE WHEN i.invoice_date<? THEN ' + STOCK_QTY + ' END) b, '
           '  SUM(CASE WHEN i.invoice_date>=? THEN ' + STOCK_QTY + ' END) q, '
           '  SUM(CASE WHEN i.invoice_date>=? THEN ii.taxable END) v '
           'FROM invoice_item ii JOIN invoice i ON i.id=ii.invoice_id '
           'WHERE i.company_id=? AND ii.item_id IS NOT NULL')
    args = [frm, frm, frm, cid]
    if to:
        sql += ' AND i.invoice_date<=?'
        args.append(to)
    moved = {}
    for r in conn.execute(sql + ' GROUP BY ii.item_id, i.doc_type', args):
        moved.setdefault(r['item_id'], {})[r['doc_type']] = (
            float(r['b'] or 0), float(r['q'] or 0), float(r['v'] or 0))
    # Stock added and removed by hand, the same way round: before the dates
    # into the opening, between them in columns of their own.
    sql = ('SELECT l.item_id, '
           '  SUM(CASE WHEN e.entry_date<? THEN l.qty END) b, '
           '  SUM(CASE WHEN e.entry_date>=? AND l.qty>0 THEN l.qty END) plus, '
           '  SUM(CASE WHEN e.entry_date>=? AND l.qty<0 THEN -l.qty END) minus '
           'FROM stock_entry_line l JOIN stock_entry e ON e.id=l.entry_id '
           'WHERE e.company_id=?')
    args = [frm, frm, frm, cid]
    if to:
        sql += ' AND e.entry_date<=?'
        args.append(to)
    hand = {r['item_id']: (float(r['b'] or 0), float(r['plus'] or 0),
                           float(r['minus'] or 0))
            for r in conn.execute(sql + ' GROUP BY l.item_id', args)}

    out = []
    cats = set(db.stock_cats(conn))
    for it in conn.execute('SELECT * FROM item WHERE company_id=? ORDER BY name',
                           (cid,)):
        m = moved.get(it['id'], {})
        tracked = bool(it['track_stock'])
        opening = float(it['opening_qty'] or 0) if tracked else 0.0
        came = went = 0.0
        for doc, (before, during, _v) in m.items():
            sign = DOC_STOCK.get(doc, 0)
            if tracked:
                opening += sign * before
            if sign > 0:
                came += during
            elif sign < 0:
                went += during
        before, added, removed = hand.get(it['id'], (0.0, 0.0, 0.0))
        if tracked:
            opening += before
        onhand = r3(opening + came - went + added - removed) if tracked else 0.0
        kg = weighed_item(it)
        rate = it['purchase_rate'] or it['sale_rate']
        out.append(dict(
            id=it['id'], tracked=tracked, code=it['code'] or '',
            name=it['name'], category=it['category'] or '', hsn=it['hsn'] or '',
            stock_cat=_cat_of(it, cats),
            unit=it['unit'], kg=kg, stock_unit='kg' if kg else it['unit'],
            opening=r3(opening), purchased=r3(came), sold=r3(went),
            added=r3(added), removed=r3(removed),
            stock=onhand, reorder_level=r3(it['reorder_level']),
            rate=r2(rate), value=r2(onhand * rate),
            sales_value=r2(m.get('SALE', (0, 0, 0))[2]),
            purchase_value=r2(m.get('PURCHASE', (0, 0, 0))[2]),
            low=bool(tracked and it['reorder_level'] > 0
                     and onhand <= it['reorder_level'])))
    return out


def report_stock(conn, cid, q):
    """The stock, held by the firms that buy it -- and what it weighs.

    **Every purchase is bought into one of the two GST firms and every sale is
    billed out of one of them**, so the material stands in their books and
    nowhere else. The order firm raises the challan that takes goods out of
    the yard, but it buys nothing and bills nothing, and a quantity in its
    name is a quantity in no one's stock.

    So this reads the billing firms, not whichever firm the screen happens to
    be in. Read together they are one pile split across two sets of books --
    which is what an empty `firm` asks for -- and read alone, one firm's own
    holding.

    Together, the rows are gathered **by HSN**: the code is what says what the
    material is, the same code is the same steel whichever book it was bought
    into, and it is the question the firms are divided along in the first
    place. An item carrying no HSN can only stand for itself, so it is
    gathered under its own name.

    **The pile is steel, and steel is counted in kilos.** `kg` is the weight
    in stock -- the weighed items only, so a machine or a box of screws is
    never added to it -- and `short` names what has gone below nothing, since
    those lines take away from the total rather than add to it.
    """
    firms = billing_firms(conn)
    wanted = firms
    if q.get('firm'):
        want = qint(q['firm'], 'firm')
        wanted = [f for f in firms if f['id'] == want]
        if not wanted:
            raise ApiError('That firm does not raise bills, so it holds no '
                           'stock.')

    rows, per_firm = [], []
    for f in wanted:
        got = _stock_of(conn, f['id'], q)
        for r in got:
            r.update(firm_id=f['id'], firm_code=f['code'], firm_name=f['name'])
        rows += got
        per_firm.append(dict(
            f, items=len([x for x in got if x['stock']]),
            purchased=r3(sum(x['purchased'] for x in got)),
            sold=r3(sum(x['sold'] for x in got)),
            added=r3(sum(x['added'] for x in got)),
            removed=r3(sum(x['removed'] for x in got)),
            stock=r3(sum(x['stock'] for x in got)),
            kg=r3(sum(x['stock'] for x in got if x['kg'])),
            value=r2(sum(x['value'] for x in got))))

    cat_list = db.stock_cats(conn)
    groups, at = [], {}
    for r in rows:
        code = (r['hsn'] or '').strip()
        key = _stock_key(r, cat_list)
        g = at.get(key)
        if g is None:
            g = at[key] = dict(key=key, hsn=code, name=r['name'], unit=r['unit'],
                               stock_cat=r['stock_cat'], ids=[],
                               names=[], units=[], kg=True, opening=0.0,
                               purchased=0.0, sold=0.0, added=0.0,
                               removed=0.0, stock=0.0,
                               value=0.0, low=False, firms={})
            groups.append(g)
        g['ids'].append(r['id'])
        if r['name'] not in g['names']:
            g['names'].append(r['name'])
        if r['stock_unit'] not in g['units']:
            g['units'].append(r['stock_unit'])
        g['kg'] = g['kg'] and r['kg']
        for k in ('opening', 'purchased', 'sold', 'added', 'removed',
                  'stock', 'value'):
            g[k] += r[k]
        g['low'] = g['low'] or r['low']
        held = g['firms'].setdefault(str(r['firm_id']), 0.0)
        g['firms'][str(r['firm_id'])] = held + r['stock']
        # The item a line's own Add / Remove / Correct opens in each firm:
        # the one holding the most of the code there.
        best = g.setdefault('pick', {}).get(str(r['firm_id']))
        if best is None or abs(r['stock']) > best[1]:
            g['pick'][str(r['firm_id'])] = (r['id'], abs(r['stock']))
    for g in groups:
        for k in ('opening', 'purchased', 'sold', 'added', 'removed', 'stock'):
            g[k] = r3(g[k])
        g['value'] = r2(g['value'])
        g['firms'] = {k: r3(v) for k, v in g['firms'].items()}
        g['pick'] = {k: v[0] for k, v in g.get('pick', {}).items()}
        # A code bought into both books under two names says so, rather than
        # showing whichever of them happened to sort first.
        g['name'] = ' / '.join(g['names'])
        g['unit'] = 'kg' if g['kg'] else ' / '.join(g['units'])
    groups.sort(key=lambda g: (-abs(g['stock']), g['name']))

    # What has gone below nothing, by the lines the screen draws.
    shown = rows if len(wanted) == 1 else groups
    short = [dict(name=x['name'], names=x.get('names') or [x['name']],
                  kg=r3(-x['stock']))
             for x in sorted(shown, key=lambda x: x['stock'])
             if x['kg'] and x['stock'] < -0.0005]
    others = [dict(name=x['name'], stock=x['stock'], unit=x['stock_unit'])
              for x in rows if not x['kg'] and abs(x['stock']) > 0.0005]

    # Each category's weight, in the list's order, and what has none last.
    # A line below nothing counts against its category as it does against
    # the whole, so the categories add up to the weight at the top.
    cats = []
    for name in cat_list + ['']:
        mine = [x for x in rows if x['stock_cat'] == name]
        if not name and not mine:
            continue
        weighed = [x for x in mine if x['kg']]
        cats.append(dict(
            name=name, products=len(mine),
            items=len([x for x in mine if abs(x['stock']) > 0.0005]),
            kg=r3(sum(x['stock'] for x in weighed)),
            firms={str(f['id']): r3(sum(x['stock'] for x in weighed
                                        if x['firm_id'] == f['id']))
                   for f in wanted},
            value=r2(sum(x['value'] for x in mine))))

    return dict(rows=rows, groups=groups, firms=firms, per_firm=per_firm,
                cats=cats, cat_list=cat_list,
                one_firm=len(wanted) == 1,
                in_billing_firm=any(f['id'] == cid for f in firms),
                date_from=q.get('from') or '', as_on=q.get('to') or '',
                kg=r3(sum(x['stock'] for x in rows if x['kg'])),
                short=short, short_kg=r3(sum(x['kg'] for x in short)),
                others=others,
                # Whether anything was added or removed by hand in the dates,
                # so the screen gives it a column only when there is some.
                by_hand=any(x['added'] or x['removed'] for x in rows),
                # How many were made in these dates -- the count on the tab.
                entries=_hand_entries(conn, [f['id'] for f in wanted], q),
                total_value=r2(sum(x['value'] for x in rows)))


# ------------------------------------------- Billing Stock categories ---
#
# Asked on 4 Oct 2026: "Make categories for Billing Stock - GP SHEET, HR
# SHEET, CRCA SHEET, COLOUR COATED SHEET, MS PLATE, MS ANGLE, MS PIPE". The
# list is the user's (db.stock_cat, seeded once with those seven); each
# product names one in item.stock_cat, or none. Nothing here moves stock.

def _cat_word(name):
    return ' '.join(str(name or '').split()).upper()


def _stock_cat_name(conn, name):
    """A category as sent, checked against the list -- '' for none."""
    want = _cat_word(name)
    if not want:
        return ''
    if want not in db.stock_cats(conn):
        raise ApiError('"%s" is not one of the Billing Stock categories.'
                       % want[:40])
    return want


def stock_cat_list(conn, q):
    """The categories in order, with how many products of the billing firms
    each holds -- and, given a product's name and HSN, the one it most
    likely belongs in (for a new product's form)."""
    firms = [f['id'] for f in billing_firms(conn)]
    count = {}
    if firms:
        for r in conn.execute(
                'SELECT stock_cat, COUNT(*) n FROM item WHERE company_id IN '
                '(%s) GROUP BY stock_cat' % ','.join('?' * len(firms)), firms):
            count[r['stock_cat'] or ''] = r['n']
    names = db.stock_cats(conn)
    out = dict(cats=[dict(name=n, products=count.get(n, 0)) for n in names],
               none=count.get('', 0))
    if q.get('name') or q.get('hsn'):
        out['guess'] = db.guess_stock_cat(q.get('name'), q.get('hsn'), names)
    return out


def save_stock_cats(conn, data):
    """The list as the screen sends it, in order: each entry its name now
    and the name it had (`old`, '' for a new one). A renamed category takes
    its products with it; one left out of the list leaves them with none."""
    got = data.get('cats')
    if not isinstance(got, list):
        raise ApiError('Send the list of categories.')
    have = db.stock_cats(conn)
    names, renamed, seen = [], {}, set()
    for c in got:
        if not isinstance(c, dict):
            raise ApiError('Each category should be a name.')
        name = _cat_word(c.get('name'))
        old = _cat_word(c.get('old'))
        if not name:
            continue
        if len(name) > 60:
            raise ApiError('"%s…" is too long for a category name.' % name[:30])
        if name in seen:
            raise ApiError('"%s" is in the list twice.' % name)
        seen.add(name)
        names.append(name)
        if old in have:
            renamed[old] = name
    moved = dropped = 0
    for r in conn.execute("SELECT id, stock_cat FROM item WHERE stock_cat<>''"
                          ).fetchall():
        now = renamed.get(r['stock_cat'], '')
        if now != r['stock_cat']:
            conn.execute('UPDATE item SET stock_cat=? WHERE id=?', (now, r['id']))
            if now:
                moved += 1
            else:
                dropped += 1
    conn.execute('DELETE FROM stock_cat')
    conn.executemany('INSERT INTO stock_cat(name, sort) VALUES (?,?)',
                     [(n, i) for i, n in enumerate(names)])
    conn.commit()
    out = stock_cat_list(conn, {})
    out.update(renamed_products=moved, uncategorised=dropped)
    return out


def set_stock_cat(conn, data):
    """Put products in a category (or in none) -- one line of Billing Stock
    is a product in each firm, and they all move together."""
    ids = data.get('item_ids')
    if not isinstance(ids, list) or not ids:
        raise ApiError('Say which products.')
    ids = [qint(i, 'product') for i in ids]
    cat = _stock_cat_name(conn, data.get('stock_cat'))
    found = conn.execute('SELECT COUNT(*) FROM item WHERE id IN (%s)'
                         % ','.join('?' * len(ids)), ids).fetchone()[0]
    if found != len(set(ids)):
        raise ApiError('A product to move is no longer in the books.', 404)
    conn.execute('UPDATE item SET stock_cat=? WHERE id IN (%s)'
                 % ','.join('?' * len(ids)), [cat] + ids)
    conn.commit()
    return dict(stock_cat=cat, products=len(set(ids)))


# ------------------------------------------------ stock added by hand ---
#
# Stock put into or taken out of BST's or BSI's books by hand. A purchase
# and a bill move the stock by themselves; this is for the rest -- steel
# found or lost at a count, damaged or scrapped, an opening typed in. It
# posts to no account. Three kinds, each line one item:
#
#     ADD     so many kilos more           line qty = +kilos
#     REMOVE  so many kilos less           line qty = -kilos
#     COUNT   this is what is there        line qty = found - books that day
#
# A count is kept as the change it made on the day it was saved, like any
# other entry; opening it again works the change out afresh from the books.

STOCK_ENTRY_KINDS = {'ADD': 'Stock added', 'REMOVE': 'Stock removed',
                     'COUNT': 'Stock corrected'}


def _stock_firm(conn, firm):
    """The billing firm an entry is for -- never whichever the screen is in."""
    if firm in (None, ''):
        raise ApiError('Choose the firm: the stock is held by BST or BSI.')
    fid = qint(firm, 'firm')
    for f in billing_firms(conn):
        if f['id'] == fid:
            return f
    raise ApiError('Only the billing firms hold stock.')


def stock_on(conn, cid, as_on='', skip=None):
    """item_id -> what the books held at the end of `as_on` (every date when
    blank), leaving out one hand entry -- the one being opened again."""
    st = {}
    for r in conn.execute('SELECT id, opening_qty FROM item WHERE company_id=? '
                          'AND track_stock=1', (cid,)):
        st[r['id']] = float(r['opening_qty'] or 0)
    sql = ('SELECT ii.item_id, i.doc_type, SUM(' + STOCK_QTY + ') q '
           'FROM invoice_item ii JOIN invoice i ON i.id=ii.invoice_id '
           'WHERE i.company_id=? AND ii.item_id IS NOT NULL')
    args = [cid]
    if as_on:
        sql += ' AND i.invoice_date<=?'
        args.append(as_on)
    for r in conn.execute(sql + ' GROUP BY ii.item_id, i.doc_type', args):
        if r['item_id'] in st:
            st[r['item_id']] += DOC_STOCK.get(r['doc_type'], 0) * float(r['q'] or 0)
    sql = ('SELECT l.item_id, SUM(l.qty) q FROM stock_entry_line l '
           'JOIN stock_entry e ON e.id=l.entry_id WHERE e.company_id=?')
    args = [cid]
    if as_on:
        sql += ' AND e.entry_date<=?'
        args.append(as_on)
    if skip:
        sql += ' AND e.id<>?'
        args.append(skip)
    for r in conn.execute(sql + ' GROUP BY l.item_id', args):
        if r['item_id'] in st:
            st[r['item_id']] += float(r['q'] or 0)
    return st


def _hand_entries(conn, firm_ids, q):
    if not firm_ids:
        return 0
    sql = ('SELECT COUNT(*) FROM stock_entry WHERE company_id IN (%s)'
           % ','.join('?' * len(firm_ids)))
    args = list(firm_ids)
    if q.get('from'):
        sql += ' AND entry_date>=?'
        args.append(q['from'])
    if q.get('to'):
        sql += ' AND entry_date<=?'
        args.append(q['to'])
    return conn.execute(sql, args).fetchone()[0]


def stock_items(conn, q):
    """What an entry can be made against in one firm, with what each holds
    on the entry's date -- for the material box and the figures beside it."""
    f = _stock_firm(conn, q.get('firm'))
    as_on = _steel_day(q.get('as_on'), 'Date')
    skip = qint(q['skip'], 'entry') if q.get('skip') else None
    st = stock_on(conn, f['id'], as_on, skip)
    out = []
    for it in conn.execute('SELECT * FROM item WHERE company_id=? AND '
                           'track_stock=1 ORDER BY name', (f['id'],)):
        kg = weighed_item(it)
        out.append(dict(id=it['id'], name=it['name'], hsn=it['hsn'] or '',
                        code=it['code'] or '', kg=kg,
                        unit='kg' if kg else it['unit'],
                        stock=r3(st.get(it['id'], 0))))
    # What is on the floor first: it is what gets counted and taken out.
    out.sort(key=lambda x: (abs(x['stock']) < 0.0005, x['name']))
    return dict(firm=f, as_on=as_on, items=out)


def _day_before(day):
    return (datetime.date.fromisoformat(day) - datetime.timedelta(days=1)).isoformat()


def stock_moves(conn, q):
    """One material's history in the billing firms -- the item details other
    billing programs open on a click: the stock when `from` began, then every
    purchase, bill, return and change made by hand up to `to`, each with the
    stock it left. `item` names one firm's item; `key` a line of both firms
    together (an HSN, or "name:" and a name for an item with none)."""
    firms = billing_firms(conn)
    wanted = firms
    if q.get('firm'):
        wanted = [_stock_firm(conn, q['firm'])]
    fids = [f['id'] for f in wanted]
    code = {f['id']: f['code'] for f in firms}
    items = []
    if q.get('item'):
        it = one(conn.execute('SELECT * FROM item WHERE id=?',
                              (qint(q['item'], 'item'),)))
        if not it or it['company_id'] not in code:
            raise ApiError('That material is not in the billing firms.', 404)
        items = [it]
        fids = [it['company_id']]
    else:
        key = str(q.get('key') or '').strip()
        if not key:
            raise ApiError('Say which material.')
        cats = db.stock_cats(conn)
        for it in conn.execute(
                'SELECT * FROM item WHERE company_id IN (%s) ORDER BY name'
                % ','.join('?' * len(fids)), fids):
            if _stock_key(it, cats) == key:
                items.append(dict(it))
    items = [it for it in items if it['track_stock']]
    if not items:
        raise ApiError('No material of that name keeps stock here.', 404)
    frm = _steel_day(q.get('from'), 'From')
    to = _steel_day(q.get('to'), 'To')
    ids = [it['id'] for it in items]
    marks = ','.join('?' * len(ids))

    opening = 0.0
    for it in items:
        if frm:
            opening += stock_on(conn, it['company_id'], _day_before(frm)).get(it['id'], 0.0)
        else:
            opening += float(it['opening_qty'] or 0)

    def dated(col):
        w, a = '', []
        if frm:
            w += ' AND %s>=?' % col
            a.append(frm)
        if to:
            w += ' AND %s<=?' % col
            a.append(to)
        return w, a

    moves = []
    w, a = dated('i.invoice_date')
    for r in conn.execute(
            'SELECT i.id, i.company_id, i.doc_type, i.invoice_date d, i.series, '
            '  i.invoice_no, i.source, p.name party, SUM(' + STOCK_QTY + ') q '
            'FROM invoice_item ii JOIN invoice i ON i.id=ii.invoice_id '
            'LEFT JOIN ledger p ON p.id=i.party_id '
            'WHERE ii.item_id IN (%s)%s GROUP BY i.id' % (marks, w), ids + a):
        change = DOC_STOCK.get(r['doc_type'], 0) * float(r['q'] or 0)
        what = {'PURCHASE': 'Purchase', 'SALE': 'Bill', 'CREDIT': 'Credit note',
                'DEBIT': 'Debit note'}.get(r['doc_type'], r['doc_type'].title())
        if r['doc_type'] == 'SALE' and change > 0:
            what = 'Sales return'
        elif r['doc_type'] == 'PURCHASE' and change < 0:
            what = 'Purchase return'
        moves.append(dict(date=r['d'], what=what, kind=r['doc_type'],
                          no=docno(dict(r)), party=r['party'] or '',
                          firm_id=r['company_id'], firm=code.get(r['company_id'], ''),
                          doc_id=r['id'], change=r3(change), order=1))
    w, a = dated('e.entry_date')
    for r in conn.execute(
            'SELECT e.id, e.company_id, e.kind, e.entry_date d, e.reason, e.note, '
            '  SUM(l.qty) q, MAX(l.counted) found FROM stock_entry_line l '
            'JOIN stock_entry e ON e.id=l.entry_id '
            'WHERE l.item_id IN (%s)%s GROUP BY e.id' % (marks, w), ids + a):
        moves.append(dict(date=r['d'], what=STOCK_ENTRY_KINDS.get(r['kind'], r['kind']),
                          kind=r['kind'], no='', party=r['reason'] or '',
                          note=r['note'] or '',
                          counted=None if r['found'] is None else r3(r['found']),
                          firm_id=r['company_id'], firm=code.get(r['company_id'], ''),
                          entry_id=r['id'], change=r3(float(r['q'] or 0)),
                          order=3 if r['kind'] == 'COUNT' else 2))
    moves.sort(key=lambda m: (m['date'] or '', m['order'],
                              m.get('doc_id') or m.get('entry_id') or 0))
    left = opening
    for m in moves:
        left += m['change']
        m['balance'] = r3(left)
        m['kg_in'] = r3(max(m['change'], 0.0))
        m['kg_out'] = r3(max(-m['change'], 0.0))
        del m['order']
    kg = all(weighed_item(it) for it in items)
    return dict(name=' / '.join(dict.fromkeys(it['name'] for it in items)),
                hsn=(items[0]['hsn'] or '').strip(),
                unit='kg' if kg else items[0]['unit'], kg=kg,
                firms=[dict(id=f, code=code[f]) for f in dict.fromkeys(
                    it['company_id'] for it in items)],
                date_from=frm, as_on=to, opening=r3(opening), moves=moves,
                closing=r3(left),
                total_in=r3(sum(m['kg_in'] for m in moves)),
                total_out=r3(sum(m['kg_out'] for m in moves)))


def _stock_entry_lines(conn, ids):
    got = {}
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        for r in conn.execute(
                'SELECT l.*, i.name, i.hsn, i.unit, i.code, i.category '
                'FROM stock_entry_line l JOIN item i ON i.id=l.item_id '
                'WHERE l.entry_id IN (%s) ORDER BY l.id'
                % ','.join('?' * len(part)), part):
            d = dict(r)
            kg = weighed_item(r)
            d.update(kg=kg, unit='kg' if kg else r['unit'],
                     qty=r3(d['qty']),
                     counted=None if d['counted'] is None else r3(d['counted']))
            del d['category']
            got.setdefault(r['entry_id'], []).append(d)
    return got


def _stock_entry_out(conn, e, lines):
    e = dict(e)
    e['lines'] = lines
    e['what'] = STOCK_ENTRY_KINDS.get(e['kind'], e['kind'])
    e['added'] = r3(sum(l['qty'] for l in lines if l['qty'] > 0))
    e['removed'] = r3(-sum(l['qty'] for l in lines if l['qty'] < 0))
    e['change'] = r3(e['added'] - e['removed'])
    return e


def list_stock_entries(conn, q):
    """Every hand entry in the billing firms, newest first."""
    firms = {f['id']: f for f in billing_firms(conn)}
    sql = ('SELECT e.*, c.code firm_code, c.name firm_name FROM stock_entry e '
           'JOIN company c ON c.id=e.company_id WHERE 1=1')
    args = []
    if q.get('firm'):
        sql += ' AND e.company_id=?'
        args.append(_stock_firm(conn, q['firm'])['id'])
    else:
        sql += ' AND e.company_id IN (%s)' % ','.join('?' * len(firms))
        args += list(firms)
    frm = _steel_day(q.get('from'), 'From')
    to = _steel_day(q.get('to'), 'To')
    if frm:
        sql += ' AND e.entry_date>=?'
        args.append(frm)
    if to:
        sql += ' AND e.entry_date<=?'
        args.append(to)
    kind = (q.get('kind') or '').upper()
    if kind in STOCK_ENTRY_KINDS:
        sql += ' AND e.kind=?'
        args.append(kind)
    got = rows(conn.execute(sql + ' ORDER BY e.entry_date DESC, e.id DESC',
                            args)) if firms else []
    lines = _stock_entry_lines(conn, [e['id'] for e in got])
    out = [_stock_entry_out(conn, e, lines.get(e['id'], [])) for e in got]
    return dict(rows=out, count=len(out), firms=list(firms.values()),
                added=r3(sum(e['added'] for e in out)),
                removed=r3(sum(e['removed'] for e in out)))


def get_stock_entry(conn, eid, cid=None):
    e = one(conn.execute(
        'SELECT e.*, c.code firm_code, c.name firm_name FROM stock_entry e '
        'JOIN company c ON c.id=e.company_id WHERE e.id=?', (eid,)))
    if not e or (cid and e['company_id'] != cid):
        raise ApiError('That stock entry is not there any more.', 404)
    return _stock_entry_out(conn, e, _stock_entry_lines(conn, [eid]).get(eid, []))


def _kilos(v, n):
    try:
        return float(str(v if v is not None else '').replace(',', '').strip() or 'nan')
    except ValueError:
        raise ApiError('Line %d: the weight is not a number.' % n)


def save_stock_entry(conn, eid, data):
    """Stock added, removed or counted: a firm, a date, why, and a line for
    each item with its weight."""
    f = _stock_firm(conn, data.get('firm'))
    kind = (data.get('kind') or 'ADD').upper()
    if kind not in STOCK_ENTRY_KINDS:
        raise ApiError('An entry adds stock, removes it, or counts it.')
    day = _steel_day(data.get('entry_date'), 'Date')
    if not day:
        raise ApiError('Give the date.')
    if eid:
        get_stock_entry(conn, eid)                  # it is still there
    items = {r['id']: r for r in conn.execute(
        'SELECT * FROM item WHERE company_id=?', (f['id'],))}
    book = stock_on(conn, f['id'], day, eid) if kind == 'COUNT' else {}
    lines, seen = [], set()
    for n, ln in enumerate(data.get('lines') or [], 1):
        raw = ln.get('weight')
        blank = str(raw if raw is not None else '').strip() == ''
        iid = ln.get('item_id')
        if not iid and blank:
            continue                      # a blank line left at the foot
        if not iid:
            raise ApiError('Line %d: choose the material.' % n)
        it = items.get(qint(iid, 'item'))
        if not it:
            raise ApiError('Line %d: that material is not in %s\'s books.'
                           % (n, f['code']))
        if not it['track_stock']:
            raise ApiError('Line %d: %s does not keep stock (Track stock is '
                           'off on the item).' % (n, it['name']))
        if blank:
            raise ApiError('Line %d: give the weight for %s.' % (n, it['name']))
        w = _kilos(raw, n)
        if w != w:                        # NaN: nothing usable typed
            raise ApiError('Line %d: give the weight for %s.' % (n, it['name']))
        if kind == 'COUNT':
            if w < 0:
                raise ApiError('Line %d: a count cannot be below nothing.' % n)
            if it['id'] in seen:
                raise ApiError('Line %d: %s is counted twice. Put the whole '
                               'count on one line.' % (n, it['name']))
            seen.add(it['id'])
            lines.append((it['id'], r3(w - book.get(it['id'], 0.0)), r3(w)))
        else:
            if w <= 0:
                raise ApiError('Line %d: give the weight (more than 0).' % n)
            lines.append((it['id'], r3(w if kind == 'ADD' else -w), None))
    if not lines:
        raise ApiError('Add at least one line: the material and its weight.')
    reason = re.sub(r'\s+', ' ', str(data.get('reason') or '')).strip()[:80]
    note = str(data.get('note') or '').strip()[:300]
    if eid:
        conn.execute('UPDATE stock_entry SET company_id=?, kind=?, entry_date=?, '
                     'reason=?, note=? WHERE id=?',
                     (f['id'], kind, day, reason, note, eid))
        conn.execute('DELETE FROM stock_entry_line WHERE entry_id=?', (eid,))
    else:
        eid = conn.execute(
            'INSERT INTO stock_entry(company_id, kind, entry_date, reason, note) '
            'VALUES (?,?,?,?,?)', (f['id'], kind, day, reason, note)).lastrowid
    conn.executemany(
        'INSERT INTO stock_entry_line(entry_id, item_id, qty, counted) '
        'VALUES (?,?,?,?)', [(eid,) + ln for ln in lines])
    conn.commit()
    return get_stock_entry(conn, eid)


def drop_hand_lines(conn, iid, move_to=None):
    """An item going: its hand-entry lines onto `move_to`, or away with it --
    and an entry left with no line at all goes too."""
    if move_to:
        conn.execute('UPDATE stock_entry_line SET item_id=? WHERE item_id=?',
                     (move_to, iid))
        return
    conn.execute('DELETE FROM stock_entry_line WHERE item_id=?', (iid,))
    conn.execute('DELETE FROM stock_entry WHERE id NOT IN '
                 '(SELECT entry_id FROM stock_entry_line)')


def delete_stock_entry(conn, eid, cid=None):
    get_stock_entry(conn, eid, cid)
    conn.execute('DELETE FROM stock_entry_line WHERE entry_id=?', (eid,))
    conn.execute('DELETE FROM stock_entry WHERE id=?', (eid,))
    conn.commit()
    return dict(deleted=eid)


def list_bills(conn, q):
    """Every GST firm's sales bills in one list, newest first -- or, with
    `doc_type=PURCHASE`, every purchase bought into them (6 Oct 2026: "show
    purchase bills for both the firms together").

    Each firm keeps its own books, so a list scoped to one company can only
    ever show half of what was billed. This reads the billing firms together,
    says which one raised each bill, and totals them separately as well as
    together -- which is the whole point of looking at both at once.
    """
    kind = 'PURCHASE' if (q.get('doc_type') or '').upper() == 'PURCHASE' else 'SALE'
    firms = billing_firms(conn)
    wanted = firms
    if q.get('firm'):
        wanted = [f for f in firms if f['id'] == qint(q['firm'], 'firm')]

    out = []
    for f in wanted:
        for r in list_invoices(conn, f['id'], dict(q, doc_type=kind)):
            r['firm_id'] = f['id']
            r['firm_code'] = f['code']
            r['firm_name'] = f['name']
            out.append(r)
    if kind == 'SALE':
        # By number within each firm's run, the two firms woven by time.
        number_order(out)
    else:
        # A purchase carries the supplier's number, so purchases go by date,
        # as each firm's own list has them.
        out.sort(key=lambda r: (r['invoice_date'] or '', r['id']), reverse=True)

    def totals(group):
        tax = sum(float(r['cgst'] or 0) + float(r['sgst'] or 0)
                  + float(r['igst'] or 0) + float(r['cess'] or 0) for r in group)
        return dict(count=len(group),
                    taxable=r2(sum(float(r['taxable'] or 0) for r in group)),
                    tax=r2(tax),
                    total=r2(sum(float(r['grand_total'] or 0) for r in group)))

    per_firm = []
    for f in wanted:
        per_firm.append(dict(f, **totals([r for r in out if r['firm_id'] == f['id']])))
    return dict(firms=firms, rows=out, per_firm=per_firm, totals=totals(out))


# ------------------------------------------------------------- e-way bill

def eway_settings(conn):
    """The portal's own dropdowns, so the screen and the file cannot drift."""
    import eway
    pair = lambda rows: [{'code': c, 'name': n} for c, n in rows]  # noqa: E731
    return dict(sub_supply=pair(eway.SUB_SUPPLY),
                trans_modes=pair(eway.TRANS_MODES),
                vehicle_types=pair(eway.VEHICLE_TYPES),
                transaction_types=pair(eway.TRANSACTION_TYPES),
                doc_types=pair(eway.DOC_TYPES),
                schemas=pair(eway.SCHEMAS),
                docno_styles=pair(eway.DOCNO_STYLES),
                docno_style=eway.docno_style(conn),
                billing_firms=billing_firms(conn),
                consignor=eway.consignor_id(conn),
                threshold=eway.THRESHOLD)


def save_eway_settings(conn, data):
    """The two answers the screen asks for once and then remembers.

    How the series and the number are joined has to match what the same bill
    is filed as in GSTR-1, and that does not change from bill to bill. Which
    firm a challan moves under is the same kind of answer: it is asked only
    for the customers whose billing firm the books never recorded, and it is
    the same firm every time for those.
    """
    import eway
    if 'docno_style' in data:
        style = (data.get('docno_style') or '').strip()
        if style not in dict(eway.DOCNO_STYLES):
            raise ApiError('Unknown document number style "%s".' % style)
        db.set_meta(conn, 'eway_docno', style)
    if 'consignor' in data:
        want = str(data.get('consignor') or '').strip()
        if not want:
            db.set_meta(conn, eway.CONSIGNOR_KEY, '')
        else:
            cid = qint(want, 'firm')
            if cid not in {f['id'] for f in billing_firms(conn)}:
                raise ApiError('A challan can only move under a GST firm.')
            db.set_meta(conn, eway.CONSIGNOR_KEY, str(cid))
    conn.commit()
    return eway_settings(conn)


def save_transport(conn, cid, iid, data, who=''):
    """Just the transport boxes, without re-saving the whole document.

    Filling in a vehicle number on the e-way screen must not re-run the tax
    working of a bill that has already gone out, so this writes those columns
    and nothing else.
    """
    if not one(conn.execute('SELECT 1 FROM invoice WHERE id=? AND company_id=?',
                            (iid, cid))):
        raise ApiError('Document not found.', 404)
    fields = dict(
        transporter=(data.get('transporter') or '').strip(),
        vehicle_no=(data.get('vehicle_no') or '').strip().upper(),
        lr_no=(data.get('lr_no') or '').strip(),
        lr_date=(data.get('lr_date') or '').strip(),
        eway_no=(data.get('eway_no') or '').strip(),
        **eway_fields(data))
    # Only what was actually sent. A screen that shows the vehicle and the
    # transporter but not the lorry receipt would otherwise blank the lorry
    # receipt -- and the e-way number with it -- every time it saved.
    fields = {k: v for k, v in fields.items() if k in data}
    if not fields:
        return _transport_preview(conn, cid, iid, data)
    before = _history_before(conn, iid)
    sets = ', '.join('%s=?' % k for k in fields)
    conn.execute('UPDATE invoice SET %s WHERE id=? AND company_id=?' % sets,
                 list(fields.values()) + [iid, cid])
    # The vehicle or the e-way number filled in for the portal is a change
    # to the bill like any other, and goes into its history as one.
    _keep_history(conn, iid, before, 'edited', 'e-way screen', who)
    conn.commit()
    return _transport_preview(conn, cid, iid, data)


def _transport_preview(conn, cid, iid, data):
    """The document back as whichever portal asked for it."""
    if (data.get('portal') or '').lower() == 'einvoice':
        import einvoice
        return einvoice.preview(conn, cid, iid, data.get('docno'))
    import eway
    return eway.preview(conn, cid, iid, data.get('schema') or 'bulk',
                        data.get('docno'))


def eway_candidates(conn, q):
    """Bills to choose from on the e-way screen, with what is obviously off.

    The heavy check runs per bill once one is ticked; this pass only reads
    what the list already has, so opening the screen stays cheap.
    """
    import eway
    style = eway.docno_style(conn, q.get('docno'))
    doc = (q.get('doc_type') or 'SALE').upper()
    firms = billing_firms(conn)
    if q.get('firm') == 'order':
        firms = [get_company(conn, order_company_id(conn))]
    elif q.get('firm'):
        want = qint(q['firm'], 'firm')
        firms = [f for f in firms if f['id'] == want] \
            or [get_company(conn, want)]

    out = []
    fallback = eway.consignor_id(conn)
    known = {x['id'] for x in billing_firms(conn)}
    for f in firms:
        # A no-tax book has no GSTIN, so its challans move under the firm that
        # bills the customer -- and when nothing records which firm that is,
        # the row has nothing to go up under at all.
        own = (f.get('tax_mode') or 'gst') != 'none'
        pins = {r['id']: eway.pin_of(r['pin'], r['address1'], r['address2'],
                                     r['city'])[0]
                for r in conn.execute(
                    'SELECT id, pin, address1, address2, city FROM ledger '
                    'WHERE company_id=?', (f['id'],))}
        for r in list_invoices(conn, f['id'], dict(q, doc_type=doc)):
            missing = []
            if not own and not (r.get('bill_company') in known or fallback):
                missing.append('firm to move under')
            if not gstin_ok(r.get('party_gstin')):
                missing.append('party GSTIN')
            if not pins.get(r['party_id']):
                missing.append('party PIN')
            if not (r.get('vehicle_no') or '').strip() \
                    and not (r.get('transporter_gstin') or '').strip():
                missing.append('vehicle')
            out.append(dict(
                id=r['id'], firm_id=f['id'], firm_code=f['code'],
                firm_name=f['name'], invoice_date=r['invoice_date'],
                doc_no=eway.doc_number(r, style), party_name=r['party_name'],
                party_id=r['party_id'], party_city=r.get('party_city') or '',
                party_gstin=r.get('party_gstin') or '',
                grand_total=r2(r['grand_total']),
                vehicle_no=r.get('vehicle_no') or '',
                eway_no=r.get('eway_no') or '',
                below_threshold=float(r['grand_total'] or 0) < eway.THRESHOLD,
                missing=missing))
    out.sort(key=lambda r: (r['invoice_date'], r['id']), reverse=True)
    return dict(firms=firms, rows=out,
                total=r2(sum(r['grand_total'] for r in out)))


def einvoice_candidates(conn, q):
    """Bills to choose from on the e-invoice screen, with what is obviously off.

    The heavy check runs per bill once one is ticked; this pass only reads what
    the list already has, so opening the screen stays cheap. An e-invoice is a
    B2B document, so a buyer with no GSTIN is the one thing that keeps a bill
    off the list altogether rather than merely flagging it.
    """
    import eway
    style = eway.docno_style(conn, q.get('docno'))
    doc = (q.get('doc_type') or 'SALE').upper()
    firms = billing_firms(conn)
    if q.get('firm'):
        want = qint(q['firm'], 'firm')
        firms = [f for f in firms if f['id'] == want] \
            or [get_company(conn, want)]

    out = []
    for f in firms:
        pins = {r['id']: eway.pin_of(r['pin'], r['address1'], r['address2'],
                                     r['city'])[0]
                for r in conn.execute(
                    'SELECT id, pin, address1, address2, city FROM ledger '
                    'WHERE company_id=?', (f['id'],))}
        for r in list_invoices(conn, f['id'], dict(q, doc_type=doc)):
            missing = []
            registered = gstin_ok(r.get('party_gstin'))
            if not registered:
                missing.append('party GSTIN')
            if not pins.get(r['party_id']):
                missing.append('party PIN')
            out.append(dict(
                id=r['id'], firm_id=f['id'], firm_code=f['code'],
                firm_name=f['name'], invoice_date=r['invoice_date'],
                doc_no=eway.doc_number(r, style), party_name=r['party_name'],
                party_id=r['party_id'], party_city=r.get('party_city') or '',
                party_gstin=r.get('party_gstin') or '',
                grand_total=r2(r['grand_total']),
                # A bill with transport on it asks for the e-way bill along
                # with the IRN; without, the IRN comes back on its own.
                with_ewb=bool((r.get('vehicle_no') or '').strip()
                              or (r.get('transporter') or '').strip()
                              or (r.get('transporter_gstin') or '').strip()),
                eway_no=r.get('eway_no') or '',
                registered=registered, missing=missing))
    out.sort(key=lambda r: (r['invoice_date'], r['id']), reverse=True)
    return dict(firms=firms, rows=out,
                with_ewb=sum(1 for r in out if r['with_ewb']),
                total=r2(sum(r['grand_total'] for r in out)))


def report_register(conn, cid, q):
    doc = (q.get('doc_type') or 'SALE').upper()
    data = list_invoices(conn, cid, dict(q, doc_type=doc))
    tot = dict(taxable=0.0, cgst=0.0, sgst=0.0, igst=0.0, cess=0.0,
               weight_total=0.0, cutting=0.0, transport=0.0, loading=0.0,
               grand_total=0.0)
    for d in data:
        for k in tot:
            tot[k] += float(d.get(k) or 0)
    for k in tot:
        tot[k] = r2(tot[k])
    return dict(doc_type=doc, rows=data, totals=tot, count=len(data))


def report_gst(conn, cid, q):
    doc = (q.get('doc_type') or 'SALE').upper()
    where, args = ['i.company_id=?', 'i.doc_type=?'], [cid, doc]
    if q.get('from'):
        where.append('i.invoice_date>=?')
        args.append(q['from'])
    if q.get('to'):
        where.append('i.invoice_date<=?')
        args.append(q['to'])
    w = ' AND '.join(where)

    # Cutting, transport and loading are part of the value of the supply, so
    # each line's share of them is taxable value like any other. The document's
    # own GST summary counts it, and so must this: leaving it out made the
    # rate-wise and HSN grids come up short of the bills they are built from.
    rate_wise = rows(conn.execute(
        'SELECT ii.gst_rate rate, SUM(ii.taxable + ii.charge_share) taxable, '
        '  SUM(ii.cgst) cgst, '
        '  SUM(ii.sgst) sgst, SUM(ii.igst) igst, SUM(ii.cess) cess '
        'FROM invoice_item ii JOIN invoice i ON i.id=ii.invoice_id '
        'WHERE ' + w + ' GROUP BY ii.gst_rate ORDER BY ii.gst_rate', args))

    hsn_wise = rows(conn.execute(
        "SELECT COALESCE(NULLIF(ii.hsn,''),'-') hsn, ii.unit, "
        '  SUM(CASE WHEN ii.weight>0 THEN ii.weight ELSE ii.qty END) qty, '
        '  SUM(ii.taxable + ii.charge_share) taxable, SUM(ii.cgst) cgst, '
        '  SUM(ii.sgst) sgst, SUM(ii.igst) igst, SUM(ii.cess) cess, '
        '  SUM(ii.line_total) total '
        'FROM invoice_item ii JOIN invoice i ON i.id=ii.invoice_id '
        'WHERE ' + w + ' GROUP BY hsn, ii.unit ORDER BY hsn', args))

    b2b = rows(conn.execute(
        'SELECT p.id party_id, p.name party, p.gstin, p.state, '
        '  COUNT(*) invoices, '
        '  SUM(i.taxable) taxable, SUM(i.cgst) cgst, SUM(i.sgst) sgst, '
        '  SUM(i.igst) igst, SUM(i.cess) cess, SUM(i.grand_total) total '
        'FROM invoice i JOIN ledger p ON p.id=i.party_id '
        "WHERE " + w + " AND TRIM(COALESCE(p.gstin,''))<>'' "
        'GROUP BY p.id ORDER BY total DESC', args))

    b2c = one(conn.execute(
        'SELECT COUNT(*) invoices, COALESCE(SUM(i.taxable),0) taxable, '
        '  COALESCE(SUM(i.cgst),0) cgst, COALESCE(SUM(i.sgst),0) sgst, '
        '  COALESCE(SUM(i.igst),0) igst, COALESCE(SUM(i.cess),0) cess, '
        '  COALESCE(SUM(i.grand_total),0) total '
        'FROM invoice i JOIN ledger p ON p.id=i.party_id '
        "WHERE " + w + " AND TRIM(COALESCE(p.gstin,''))=''", args))

    rcm = one(conn.execute(
        'SELECT COUNT(*) invoices, COALESCE(SUM(i.taxable),0) taxable, '
        '  COALESCE(SUM(i.cgst+i.sgst+i.igst+i.cess),0) tax '
        'FROM invoice i WHERE ' + w + ' AND i.rcm=1', args))

    for group in (rate_wise, hsn_wise, b2b):
        for r in group:
            for k, v in list(r.items()):
                if isinstance(v, float):
                    r[k] = r3(v) if k == 'qty' else r2(v)
    for d in (b2c, rcm):
        for k, v in list((d or {}).items()):
            if isinstance(v, float):
                d[k] = r2(v)

    totals = dict(taxable=0.0, cgst=0.0, sgst=0.0, igst=0.0, cess=0.0)
    for r in rate_wise:
        for k in totals:
            totals[k] += r[k]
    for k in totals:
        totals[k] = r2(totals[k])
    totals['tax_total'] = r2(totals['cgst'] + totals['sgst']
                             + totals['igst'] + totals['cess'])
    return dict(doc_type=doc, rate_wise=rate_wise, hsn_wise=hsn_wise,
                b2b=b2b, b2c=b2c, rcm=rcm, totals=totals)


def report_health(conn, cid, q):
    """Data health: every check in verify.py, for one firm or all of them.

    `scope=all` asks the same questions of every firm at once, which is how a
    fresh import wants looking at. The default is the firm you are standing in.
    """
    everywhere = (q.get('scope') or '') == 'all'
    checks = verify.run(conn, None if everywhere else cid)
    firm = None if everywhere else get_company(conn, cid)
    return dict(
        scope='all' if everywhere else 'company',
        company=dict(id=firm['id'], code=firm['code'], name=firm['name'])
        if firm else None,
        checks=[c.as_dict() for c in checks],
        bad=sum(1 for c in checks if c.level == verify.BAD),
        warn=sum(1 for c in checks if c.level == verify.WARN),
        ok=sum(1 for c in checks if c.level == verify.OK))


def report_daybook(conn, cid, q):
    frm, to = q.get('from') or '', q.get('to') or ''

    def window(col):
        clause, args = '', []
        if frm:
            clause += ' AND %s>=?' % col
            args.append(frm)
        if to:
            clause += ' AND %s<=?' % col
            args.append(to)
        return clause, args

    out = []
    clause, args = window('i.invoice_date')
    for r in conn.execute(
            'SELECT i.id, i.invoice_date d, i.doc_type, i.series, '
            '  i.invoice_no, i.grand_total amt, i.party_id, p.name party '
            'FROM invoice i JOIN ledger p ON p.id=i.party_id '
            'WHERE i.company_id=?' + clause, [cid] + args):
        out.append(dict(id=r['id'], company_id=cid, date=r['d'],
                        type=r['doc_type'], no=docno(dict(r)), party=r['party'],
                        party_id=r['party_id'], amount=r2(r['amt']), mode=''))

    clause, args = window('v.voucher_date')
    for r in conn.execute(
            'SELECT v.id, v.voucher_date d, v.kind, v.voucher_no no, '
            '  v.amount amt, v.mode, v.party_id, '
            '  COALESCE(p.name, v.remarks) party FROM voucher v '
            'LEFT JOIN ledger p ON p.id=v.party_id '
            'WHERE v.company_id=?' + clause, [cid] + args):
        out.append(dict(id=r['id'], company_id=cid, date=r['d'],
                        type=r['kind'], no=r['no'], party=r['party'] or '',
                        party_id=r['party_id'], amount=r2(r['amt']),
                        mode=r['mode'] or ''))

    out.sort(key=lambda x: (x['date'], x['type'], x['no']))
    return dict(rows=out, count=len(out),
                total=r2(sum(x['amount'] for x in out)))


def report_dashboard(conn, cid, q):
    today = q.get('today') or ''
    month = today[:7] if today else ''

    def total(doc, where='', args=()):
        sql = ('SELECT COALESCE(SUM(grand_total),0) t FROM invoice '
               'WHERE company_id=? AND doc_type=? ' + where)
        return r2(conn.execute(sql, (cid, doc) + tuple(args)).fetchone()['t'])

    bal = ledger_balances(conn, cid)
    party_ids = [r['id'] for r in conn.execute(
        "SELECT l.id FROM ledger l JOIN account_group g ON g.id=l.group_id "
        "WHERE l.company_id=? AND (l.party_kind<>'' OR g.is_party=1)", (cid,))]
    receivable = r2(sum(bal.get(i, 0) for i in party_ids if bal.get(i, 0) > 0))
    payable = r2(-sum(bal.get(i, 0) for i in party_ids if bal.get(i, 0) < 0))

    st = stock_map(conn, cid)
    low = []
    for it in conn.execute('SELECT * FROM item WHERE company_id=? AND active=1 '
                           'AND track_stock=1', (cid,)):
        onhand = r3(st.get(it['id'], 0))
        if it['reorder_level'] > 0 and onhand <= it['reorder_level']:
            low.append(dict(id=it['id'], name=it['name'], unit=it['unit'],
                            stock=onhand, reorder_level=it['reorder_level']))
    low.sort(key=lambda x: x['stock'])

    trend = rows(conn.execute(
        "SELECT substr(invoice_date,1,7) ym, COALESCE(SUM(grand_total),0) t, "
        "  COUNT(*) n FROM invoice WHERE company_id=? AND doc_type='SALE' "
        'GROUP BY ym ORDER BY ym DESC LIMIT 12', (cid,)))
    trend.reverse()

    top = rows(conn.execute(
        'SELECT p.id, p.name, COALESCE(SUM(i.grand_total),0) t, COUNT(*) n '
        'FROM invoice i JOIN ledger p ON p.id=i.party_id '
        "WHERE i.company_id=? AND i.doc_type='SALE' "
        'GROUP BY p.id ORDER BY t DESC LIMIT 8', (cid,)))

    cash_bank = rows(conn.execute(
        "SELECT l.id, l.name FROM ledger l JOIN account_group g ON g.id=l.group_id "
        'WHERE l.company_id=? AND ' + MONEY_SQL + ' '
        'ORDER BY l.name', (cid,)))
    for c in cash_bank:
        c['balance'] = r2(bal.get(c['id'], 0))

    return dict(
        sales_today=total('SALE', 'AND invoice_date=?', (today,)) if today else 0,
        sales_month=(total('SALE', 'AND substr(invoice_date,1,7)=?', (month,))
                     if month else 0),
        sales_total=total('SALE'),
        purchase_month=(total('PURCHASE', 'AND substr(invoice_date,1,7)=?',
                              (month,)) if month else 0),
        receivable=receivable, payable=payable,
        cash_bank=cash_bank,
        open_orders=conn.execute(
            "SELECT COUNT(*) n FROM sales_order WHERE company_id=? AND "
            "status='open'", (cid,)).fetchone()['n'],
        open_order_value=r2(conn.execute(
            "SELECT COALESCE(SUM(grand_total),0) t FROM sales_order "
            "WHERE company_id=? AND status='open'", (cid,)).fetchone()['t']),
        invoice_count=conn.execute(
            "SELECT COUNT(*) n FROM invoice WHERE company_id=? AND "
            "doc_type='SALE'", (cid,)).fetchone()['n'],
        party_count=len(party_ids),
        item_count=conn.execute('SELECT COUNT(*) n FROM item WHERE company_id=?',
                                (cid,)).fetchone()['n'],
        low_stock=low[:10], low_stock_count=len(low),
        trend=trend, top_parties=top,
        imported=conn.execute(
            "SELECT COUNT(*) n FROM posting WHERE company_id=? AND "
            "doc_kind='IMPORT'", (cid,)).fetchone()['n'],
        recent=list_invoices(conn, cid, {'limit': 8}))


# ------------------------------------------------------------- csv export

# Excel and LibreOffice run a cell opening with any of these as a formula
# rather than showing it as text. Party names come out of an imported DBF and
# are typed by hand, so a name beginning "=" or "@" would be executed by
# whoever opens the export. Prefixing an apostrophe is the standard defence:
# the cell reads the same on screen and is never evaluated.
# A str rather than a tuple: one C-level scan per cell, and an export runs
# this over every cell of a few thousand rows.
_CSV_TRIGGERS = '=+@\t\r'
# A leading minus is only worth escaping when the cell is not simply a
# negative amount -- and "-" on its own is how a missing HSN is printed.
_CSV_NUMBER = re.compile(r'^-([\d,]+(\.\d*)?)?$')


def csv_cell(value):
    # Numbers are the common case and can never be read as a formula.
    if type(value) is not str or not value:
        return value
    head = value[0]
    if head in _CSV_TRIGGERS:
        return "'" + value
    if head == '-' and not _CSV_NUMBER.match(value):
        return "'" + value
    return value


def to_csv(header, records, keys):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for rec in records:
        w.writerow([csv_cell(rec.get(k, '')) for k in keys])
    return buf.getvalue()


def _account_csv(rep, q):
    """An account, in whichever of SHREE's two views the screen is showing.

    Both come off the same report, so there is one writer rather than one per
    screen -- they used to disagree about what a row was.
    """
    name = re.sub(r'[^A-Za-z0-9]+', '-',
                  rep['party']['name']).strip('-').lower() or 'account'
    if (q.get('view') or '').lower() in ('entries', 'datewise', 'detail'):
        return (to_csv(['Date', 'Type', 'No', 'Particulars', 'Reference',
                        'Firm', 'Credit', 'Debit'], rep['entries'],
                       ['date', 'kind', 'no', 'particulars', 'ref', 'firm',
                        'credit', 'debit']),
                'ledger-%s.csv' % name)
    rows = [dict(m, month=m['label'],
                 challans=(m['kinds'].get('Challan', 0)
                           + m['kinds'].get('Challan + GST', 0)),
                 balance=('' if m['balance'] is None else abs(m['balance'])))
            for m in rep['months']]
    return (to_csv(['#', 'Month', 'Challans', 'Credit', 'Debit', 'Balance',
                    'C/D'], rows,
                   ['mon', 'month', 'challans', 'credit', 'debit', 'balance',
                    'dc']),
            'account-%s.csv' % name)


def export_csv(conn, cid, what, q):
    if what == 'register':
        rep = report_register(conn, cid, q)
        return (to_csv(['Date', 'Doc No', 'Party', 'GSTIN', 'Quantity', 'Taxable',
                        'CGST', 'SGST', 'IGST', 'Cess', 'Cutting', 'Transport',
                        'Loading', 'Round Off', 'Total'],
                       rep['rows'],
                       ['invoice_date', 'invoice_no', 'party_name',
                        'party_gstin', 'weight_total', 'taxable', 'cgst',
                        'sgst', 'igst', 'cess', 'cutting', 'transport',
                        'loading', 'round_off', 'grand_total']),
                '%s-register.csv' % rep['doc_type'].lower())
    if what == 'customers':
        rep = report_customers(conn, cid, q)
        if rep['mode'] == 'one':
            return _account_csv(rep, q)
        # SHREE's yearly summary of a customer: what the account stood at,
        # what was debited and credited, and what it stands at now.
        rows = [dict(r, bills=r['bills']['count'],
                     opening=('' if r['opening'] is None else r['opening']),
                     owed=('' if r['owed'] is None else r['owed']))
                for r in rep['rows']]
        return (to_csv(['Customer', 'City', 'Phone', 'GSTIN', 'Challans',
                        'Opening', 'Debit', 'Credit', 'Balance'], rows,
                       ['name', 'city', 'phone', 'gstin', 'bills', 'opening',
                        'debit', 'credit', 'owed']),
                'customers.csv')
    if what == 'outstanding':
        rep = report_outstanding(conn, cid, q)
        return (to_csv(['Side', 'Group', 'Ledger', 'Amount'], rep['rows'],
                       ['side', 'group_name', 'name', 'balance']),
                'outstanding.csv')
    if what == 'stock':
        rep = report_stock(conn, cid, q)
        # The rows span both billing firms, so which one holds each line is
        # part of the answer and not something to work out afterwards.
        return (to_csv(['Firm', 'Code', 'Item', 'Stock category',
                        'SHREE department', 'HSN', 'Unit',
                        'Opening', 'Purchased', 'Sold', 'Added by hand',
                        'Removed by hand', 'In Stock', 'Rate', 'Value'],
                       rep['rows'],
                       ['firm_code', 'code', 'name', 'stock_cat', 'category', 'hsn',
                        'stock_unit', 'opening', 'purchased', 'sold', 'added',
                        'removed', 'stock', 'rate', 'value']),
                'stock-summary.csv')
    if what == 'receivables':
        rep = report_ageing(conn, cid, q)
        # Flattened for a spreadsheet: a row per open item, each carrying the
        # customer it belongs to, so the file can be sorted and totalled any
        # way round without the grouping the screen does for you.
        flat = []
        for r in rep['rows']:
            for c in r['claims']:
                flat.append(dict(
                    c, name=r['name'], city=r['city'], phone=r['phone'],
                    gstin=r['gstin'], owed_total=r['owed'],
                    owed_for_days=r['owed_for_days'],
                    last_paid=r['last_paid'],
                    kind_said=c.get('item') or DUE_KIND.get(
                        c['kind'], c['kind'].title()),
                    challan_part=c.get('challan_amount', ''),
                    gst_part=c.get('gst', ''), gst_bill=c.get('gst_ref', '')))
        return (to_csv(['Customer', 'City', 'Phone', 'GSTIN', 'Owed in total',
                        'Owing for (days)', 'Last paid', 'Item', 'Date', 'No',
                        'Terms', 'Due on', 'Age (days)', 'Days late',
                        'Challan', 'GST', 'GST bill', 'Still owed'],
                       flat,
                       ['name', 'city', 'phone', 'gstin', 'owed_total',
                        'owed_for_days', 'last_paid', 'kind_said', 'date',
                        'no', 'terms', 'due', 'age_days', 'overdue_days',
                        'challan_part', 'gst_part', 'gst_bill', 'open']),
                'who-owes-us-%s.csv' % rep['as_on'])
    if what == 'gst-hsn':
        rep = report_gst(conn, cid, q)
        return (to_csv(['HSN', 'Unit', 'Qty', 'Taxable', 'CGST', 'SGST',
                        'IGST', 'Cess', 'Total'], rep['hsn_wise'],
                       ['hsn', 'unit', 'qty', 'taxable', 'cgst', 'sgst',
                        'igst', 'cess', 'total']), 'gst-hsn-summary.csv')
    if what == 'gst-b2b':
        rep = report_gst(conn, cid, q)
        return (to_csv(['Party', 'GSTIN', 'State', 'Invoices', 'Taxable',
                        'CGST', 'SGST', 'IGST', 'Cess', 'Total'], rep['b2b'],
                       ['party', 'gstin', 'state', 'invoices', 'taxable',
                        'cgst', 'sgst', 'igst', 'cess', 'total']),
                'gst-b2b-summary.csv')
    if what == 'ledger':
        return _account_csv(report_ledger(conn, cid, q), q)
    if what == 'bank':
        rep = report_bank(conn, cid, q)
        if rep['statement']:
            st = rep['statement']
            # The bank book's own columns, the ones the screen shows.
            return (to_csv(['Date', 'Particulars', 'City', 'Ch/DD No.',
                            'Narration', 'Voucher', 'Withdraw', 'Deposit',
                            'Balance'],
                           st['entries'],
                           ['date', 'particulars', 'city', 'chq', 'note',
                            'no', 'withdrawal', 'deposit', 'balance']),
                    'bank-%s.csv'
                    % re.sub(r'\W+', '-', st['account']['name']).lower())
        return (to_csv(['Account', 'Group', 'Entries', 'Opening', 'Received',
                        'Paid', 'Closing'], rep['rows'],
                       ['name', 'group_name', 'entries', 'opening', 'received',
                        'paid', 'closing']), 'bank-and-cash.csv')
    if what == 'trial-balance':
        rep = report_trial_balance(conn, cid, q)
        return (to_csv(['Group', 'Ledger', 'Debit', 'Credit'], rep['rows'],
                       ['gname', 'name', 'debit', 'credit']),
                'trial-balance.csv')
    if what == 'bills':
        rep = list_bills(conn, q)
        buying = (q.get('doc_type') or '').upper() == 'PURCHASE'
        return (to_csv(['Date', 'Firm', 'Bill No',
                        'Supplier' if buying else 'Party', 'GSTIN', 'Taxable',
                        'CGST', 'SGST', 'IGST', 'Total'], rep['rows'],
                       ['invoice_date', 'firm_name', 'invoice_no', 'party_name',
                        'party_gstin', 'taxable', 'cgst', 'sgst', 'igst',
                        'grand_total']),
                'purchases-all-firms.csv' if buying else 'bills-all-firms.csv')
    if what == 'allocation':
        rep = list_allocation(conn, q)
        rows_out = []
        for r in rep['rows']:
            rows_out.append(dict(r, master_in=r['company_name'],
                                 billed_by=r['bill_company_name'] or ''))
        return (to_csv(['Customer', 'City', 'State', 'GSTIN', 'Master in',
                        'Billed by', 'Balance'], rows_out,
                       ['name', 'city', 'state', 'gstin', 'master_in',
                        'billed_by', 'balance']), 'customer-allocation.csv')
    if what == 'ledgers':
        return (to_csv(['Code', 'Name', 'Group', 'Party type', 'GST type',
                        'City', 'State', 'GSTIN', 'Phone', 'Balance'],
                       list_ledgers(conn, cid, q),
                       ['code', 'name', 'group_name', 'party_kind', 'gst_type',
                        'city', 'state', 'gstin', 'phone', 'balance']),
                'ledgers.csv')
    if what == 'items':
        return (to_csv(['Code', 'Name', 'Category', 'HSN', 'Unit', 'GST %',
                        'Sale Rate', 'Purchase Rate', 'In Stock'],
                       list_items(conn, cid, q),
                       ['code', 'name', 'category', 'hsn', 'unit', 'gst_rate',
                        'sale_rate', 'purchase_rate', 'stock']), 'items.csv')
    if what == 'daybook':
        rep = report_daybook(conn, cid, q)
        return (to_csv(['Date', 'Type', 'No', 'Party', 'Mode', 'Amount'],
                       rep['rows'],
                       ['date', 'type', 'no', 'party', 'mode', 'amount']),
                'daybook.csv')
    raise ApiError('Unknown export "%s".' % what, 404)


# --------------------------------------------------------------- counters

def get_counters(conn, cid, fy=''):
    data = rows(conn.execute(
        'SELECT * FROM counter WHERE company_id=? AND fy=? ORDER BY key',
        (cid, fy)))
    if not data and fy:
        for key, _prefix in db.DEFAULT_COUNTERS:
            db._counter_row(conn, cid, key, fy)
        conn.commit()
        data = rows(conn.execute(
            'SELECT * FROM counter WHERE company_id=? AND fy=? ORDER BY key',
            (cid, fy)))
    return data


def save_counters(conn, cid, data):
    fy = str(data.get('fy') or '')
    counters = data.get('counters') or []
    if not isinstance(counters, list) or not all(
            isinstance(c, dict) and str(c.get('key') or '').strip()
            for c in counters):
        raise ApiError('Send the numbering as a list, each with its key.')
    for c in counters:
        try:
            next_no = max(1, int(c.get('next_no') or 1))
        except (TypeError, ValueError):
            raise ApiError('"%s" is not a number to carry on from.'
                           % c.get('next_no'))
        c = dict(c, key=str(c['key']).strip(), next_no=next_no)
        db._counter_row(conn, cid, c['key'], fy)
        # `pad` is how many digits the number is written to: 4 gives
        # INV-0007, 0 gives a plain 7 for a challan book.
        try:
            pad = max(0, min(8, int(c.get('pad', 4))))
        except (TypeError, ValueError):
            pad = 4
        conn.execute('UPDATE counter SET prefix=?, next_no=?, pad=? '
                     'WHERE company_id=? AND key=? AND fy=?',
                     (str(c.get('prefix') or ''), c['next_no'], pad,
                      cid, c['key'], fy))
    conn.commit()
    return get_counters(conn, cid, fy)


def voucher_lookups(conn, cid):
    """Sales/Purchase account options and departments for the entry screen."""
    trade = rows(conn.execute(
        'SELECT l.id, l.name, l.system_key, g.code FROM ledger l '
        'JOIN account_group g ON g.id=l.group_id '
        "WHERE l.company_id=? AND g.code IN ('8401','8301') AND l.active=1 "
        'ORDER BY g.code DESC, l.name', (cid,)))
    depts = [r['dept'] for r in conn.execute(
        "SELECT DISTINCT dept FROM invoice WHERE company_id=? AND dept<>'' "
        'ORDER BY dept', (cid,))]
    if 'Stores Department' not in depts:
        depts.insert(0, 'Stores Department')
    return dict(
        sales_accounts=[t for t in trade if t['code'] == '8401'],
        purchase_accounts=[t for t in trade if t['code'] == '8301'],
        departments=depts,
        invoice_types=['Debit', 'Credit'])


def list_fys(conn, cid):
    """Financial years that hold anything at all, newest first.

    Postings are included so imported history shows up even though it has no
    invoice or voucher record behind it.
    """
    seen = set()
    for table in ('invoice', 'voucher'):
        for r in conn.execute("SELECT DISTINCT fy FROM %s WHERE company_id=? "
                              "AND fy<>''" % table, (cid,)):
            seen.add(r['fy'])
    # `GROUP BY substr(date,1,7)` used to be here, to collapse the dates to one
    # a month. It bought nothing -- DISTINCT had already deduped them and the
    # financial year is worked out per row either way -- and it cost a temp
    # B-tree over every posting the firm has, on the one call every screen
    # makes. Left as a plain DISTINCT it is answered from `ix_post_co_date`
    # alone: 6.9ms to 1.5ms on these books, and more than that where the disk
    # is slower.
    for r in conn.execute('SELECT DISTINCT date FROM posting '
                          'WHERE company_id=?', (cid,)):
        fy = db.fy_of(r['date'])
        if fy:
            seen.add(fy)
    return sorted(seen, reverse=True)


def first_fy(conn):
    """The oldest financial year anything in any firm is dated in: '2013-14'.

    The Year box offers every year from here to now whichever firm is open,
    because the Bills screen reads both GST firms at once and BST's bills
    begin two years before the accounts book does. A few index seeks.
    """
    oldest = ''
    for c in conn.execute('SELECT id FROM company').fetchall():
        for sql in ('SELECT MIN(date) FROM posting WHERE company_id=?',
                    'SELECT MIN(invoice_date) FROM invoice WHERE company_id=?',
                    'SELECT MIN(voucher_date) FROM voucher WHERE company_id=?',
                    'SELECT MIN(order_date) FROM sales_order '
                    'WHERE company_id=?'):
            d = conn.execute(sql, (c[0],)).fetchone()[0]
            if d and db.fy_of(d) and (not oldest or d < oldest):
                oldest = d
    return db.fy_of(oldest)


# ================================================================ challan stock
#
# Asked for on 26 Sep 2026: *"create a stock management for challans as well.
# These are the different types of steels the user will have in stock. For
# example CRCA - REGULAR - 0.50 ... a stock management system for just
# challans under BST."* Answered, when asked: the challans are raised in this
# app, steel comes in by a stock-in entry made here, and the count starts
# from the stock in hand today.
#
# A piece of stock is (type, quality, thickness), each off the lists in
# `steel_list` -- the three columns of the user's sheet. It is kept in the
# challan firm (`order_company_id`), whichever firm the screen is in, and in
# kilograms:
#
#     in hand = opening stock + stock in - challans
#
# The opening and the stock in are `steel_entry` rows; the challans are their
# own lines (`invoice_item.steel_*`), so a challan edited or deleted moves the
# stock with it and nothing is ever posted twice. SHREE's challans name no
# steel and move none: the update from SHREE replaces only its own documents,
# so the challans made here, and these entries, come through it untouched.

STEEL_LIST_KEYS = (('type', 'types', 'steel types'),
                   ('quality', 'qualities', 'qualities'),
                   ('thickness', 'thicknesses', 'thicknesses'))
STEEL_ENTRY_KINDS = {'IN': 'Stock in', 'OPENING': 'Opening stock',
                     'OUT': 'Stock removed', 'COUNT': 'Stock corrected'}
# Taken out or counted by hand (27 Sep 2026: "repeat the same for BST stock
# management"): steel gone without a challan -- damaged, scrap, short -- and a
# count of what is really there. Their lines carry the change they make,
# signed: minus for removed, and for a count, the kilos found less what the
# books held that day (`counted` keeps what was found).
STEEL_BY_HAND = ('OUT', 'COUNT')
_ISO_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def steel_word(v):
    """One of the three parts, as the lists hold them: one space between
    words, upper case, and short."""
    return re.sub(r'\s+', ' ', str(v or '')).strip().upper()[:30]


def steel_name(t, q, th):
    return '%s - %s - %s' % (t, q, th)


def _steel_day(v, what, default=''):
    v = str(v or '').strip() or default
    if not v:
        return ''
    if not _ISO_DAY.match(v):
        raise ApiError('%s is not a date: %s' % (what, v))
    try:
        datetime.date.fromisoformat(v)
    except ValueError:
        raise ApiError('%s is not a date: %s' % (what, v))
    return v


def _steel_firm(conn, cid):
    fid = order_company_id(conn, cid)
    if not fid:
        raise ApiError('There is no challan firm -- a firm set to No tax -- '
                       'for the steel to be kept in.')
    return fid


def steel_lists(conn):
    out = {k: [] for k, _, _ in STEEL_LIST_KEYS}
    for r in conn.execute('SELECT kind, name FROM steel_list ORDER BY sort, id'):
        if r['kind'] in out:
            out[r['kind']].append(r['name'])
    return {key: out[kind] for kind, key, _ in STEEL_LIST_KEYS}


def steel_unknown(conn, trio, allowed=()):
    """The part of (type, quality, thickness) that is not on the lists, or
    None. A trio in `allowed` -- what the entry or challan already said
    before this save -- stands even if its name has since left a list."""
    if tuple(trio) in set(allowed):
        return None
    have = steel_lists(conn)
    for value, key in zip(trio, ('types', 'qualities', 'thicknesses')):
        if value not in have[key]:
            return value
    return None


def save_steel_lists(conn, data):
    """The three lists as the screen sends them, in the order to show them.
    A name taken off a list stays on whatever already names it."""
    lists = {}
    for kind, key, label in STEEL_LIST_KEYS:
        seen, names = set(), []
        for n in data.get(key) or []:
            n = steel_word(n)
            if n and n not in seen:
                seen.add(n)
                names.append(n)
        if not names:
            raise ApiError('The list of %s cannot be empty.' % label)
        lists[kind] = names
    conn.execute('DELETE FROM steel_list')
    for kind, names in lists.items():
        conn.executemany('INSERT INTO steel_list(kind, name, sort) VALUES (?,?,?)',
                         [(kind, n, i) for i, n in enumerate(names)])
    conn.commit()
    return steel_lists(conn)


def _steel_order(conn):
    """Where each name stands in its list, for sorting the stock the way the
    lists read -- CRCA before PICKLE, 0.50 before 0.55."""
    pos = {}
    for r in conn.execute('SELECT kind, name, sort FROM steel_list'):
        pos[(r['kind'], r['name'])] = r['sort']
    return lambda t, q, th: (pos.get(('type', t), 999), t,
                             pos.get(('quality', q), 999), q,
                             pos.get(('thickness', th), 999), th)


def steel_stock(conn, cid, q):
    """Every piece of steel with anything against it, as on a day. `skip`
    leaves one entry out -- the one a form has open again."""
    fid = _steel_firm(conn, cid)
    as_on = _steel_day(q.get('as_on'), 'As on',
                       datetime.date.today().isoformat())
    want_t, want_q = steel_word(q.get('type')), steel_word(q.get('quality'))
    skip = qint(q['skip'], 'entry') if q.get('skip') else 0
    rows_ = {}

    def row(t, qu, th):
        return rows_.setdefault((t, qu, th), dict(
            steel_type=t, steel_quality=qu, steel_thick=th,
            name=steel_name(t, qu, th), opening=0.0, stock_in=0.0, out=0.0,
            by_hand=0.0, challans=0, last=''))

    for r in conn.execute(
            'SELECT l.steel_type t, l.steel_quality q, l.steel_thick th, '
            "  SUM(CASE WHEN e.kind='OPENING' THEN l.weight ELSE 0 END) op, "
            "  SUM(CASE WHEN e.kind='IN' THEN l.weight ELSE 0 END) inw, "
            "  SUM(CASE WHEN e.kind IN ('OUT','COUNT') THEN l.weight ELSE 0 END) hand, "
            '  MAX(e.entry_date) last '
            'FROM steel_entry_line l JOIN steel_entry e ON e.id=l.entry_id '
            'WHERE e.company_id=? AND e.entry_date<=? AND e.id<>? '
            'GROUP BY l.steel_type, l.steel_quality, l.steel_thick',
            (fid, as_on, skip)):
        x = row(r['t'], r['q'], r['th'])
        x['opening'] += r['op'] or 0
        x['stock_in'] += r['inw'] or 0
        x['by_hand'] += r['hand'] or 0
        x['last'] = max(x['last'], r['last'] or '')
    for r in conn.execute(
            'SELECT ii.steel_type t, ii.steel_quality q, ii.steel_thick th, '
            '  SUM(ii.weight) w, COUNT(DISTINCT i.id) n, MAX(i.invoice_date) last '
            'FROM invoice_item ii JOIN invoice i ON i.id=ii.invoice_id '
            "WHERE i.company_id=? AND i.doc_type='SALE' AND ii.steel_type<>'' "
            # A SHREE challan names only its type (4 Oct 2026): no whole
            # steel, so nothing out of stock.
            "  AND ii.steel_quality<>'' AND ii.steel_thick<>'' "
            '  AND i.invoice_date<=? '
            'GROUP BY ii.steel_type, ii.steel_quality, ii.steel_thick',
            (fid, as_on)):
        x = row(r['t'], r['q'], r['th'])
        x['out'] += r['w'] or 0
        x['challans'] += r['n'] or 0
        x['last'] = max(x['last'], r['last'] or '')

    key = _steel_order(conn)
    out = []
    for (t, qu, th), x in sorted(rows_.items(), key=lambda kv: key(*kv[0])):
        if (want_t and t != want_t) or (want_q and qu != want_q):
            continue
        for k in ('opening', 'stock_in', 'out', 'by_hand'):
            x[k] = r3(x[k])
        x['balance'] = r3(x['opening'] + x['stock_in'] + x['by_hand'] - x['out'])
        out.append(x)
    tot = {k: r3(sum(x[k] for x in out))
           for k in ('opening', 'stock_in', 'out', 'by_hand', 'balance')}
    tot['removed'] = r3(-sum(x['by_hand'] for x in out if x['by_hand'] < 0))
    tot['found'] = r3(sum(x['by_hand'] for x in out if x['by_hand'] > 0))
    firm = get_company(conn, fid)
    return dict(as_on=as_on, firm_id=fid, firm=firm['name'], rows=out,
                totals=tot, short=sum(1 for x in out if x['balance'] < -0.0005),
                # Whether anything was removed or counted by hand, so the
                # screen gives it a column only when there is some.
                by_hand=any(abs(x['by_hand']) > 0.0005 for x in out),
                started=bool(conn.execute(
                    'SELECT 1 FROM steel_entry WHERE company_id=? LIMIT 1',
                    (fid,)).fetchone()),
                lists=steel_lists(conn))


def steel_item(conn, cid, q):
    """One piece of steel: every movement in and out, oldest first, with what
    was in hand after each."""
    fid = _steel_firm(conn, cid)
    t, qu, th = (steel_word(q.get('type')), steel_word(q.get('quality')),
                 steel_word(q.get('thick')))
    if not (t and qu and th):
        raise ApiError('Say which steel: type, quality and thickness.')
    as_on = _steel_day(q.get('as_on'), 'As on',
                       datetime.date.today().isoformat())
    moves = []
    for r in conn.execute(
            'SELECT e.id, e.kind, e.entry_date d, e.party, e.ref_no, e.note, '
            '  SUM(l.weight) w, MAX(l.counted) found FROM steel_entry e '
            'JOIN steel_entry_line l ON l.entry_id=e.id '
            'WHERE e.company_id=? AND l.steel_type=? AND l.steel_quality=? '
            '  AND l.steel_thick=? AND e.entry_date<=? GROUP BY e.id',
            (fid, t, qu, th, as_on)):
        w = float(r['w'] or 0)
        moves.append(dict(date=r['d'], kind=r['kind'], entry_id=r['id'],
                          what=STEEL_ENTRY_KINDS.get(r['kind'], r['kind']),
                          party=r['party'] or '', ref=r['ref_no'] or '',
                          note=r['note'] or '',
                          kg_in=r3(max(w, 0.0)), kg_out=r3(max(-w, 0.0)),
                          counted=None if r['found'] is None else r3(r['found']),
                          by_hand=r['kind'] in STEEL_BY_HAND,
                          order=0 if r['kind'] == 'OPENING' else
                          3 if r['kind'] == 'COUNT' else 1))
    for r in conn.execute(
            'SELECT i.id, i.invoice_date d, i.series, i.invoice_no, '
            '  p.name party, SUM(ii.weight) w FROM invoice_item ii '
            'JOIN invoice i ON i.id=ii.invoice_id '
            'JOIN ledger p ON p.id=i.party_id '
            "WHERE i.company_id=? AND i.doc_type='SALE' AND ii.steel_type=? "
            '  AND ii.steel_quality=? AND ii.steel_thick=? AND i.invoice_date<=? '
            'GROUP BY i.id',
            (fid, t, qu, th, as_on)):
        moves.append(dict(date=r['d'], kind='CHALLAN', doc_id=r['id'],
                          company_id=fid, what='Challan', no=docno(dict(r)),
                          party=r['party'] or '', ref='', note='',
                          kg_in=0.0, kg_out=r3(r['w']), order=2))
    moves.sort(key=lambda m: (m['date'] or '', m['order'],
                              m.get('entry_id') or m.get('doc_id') or 0))
    left = 0.0
    for m in moves:
        left = r3(left + m['kg_in'] - m['kg_out'])
        m['balance'] = left
        del m['order']
    return dict(steel_type=t, steel_quality=qu, steel_thick=th,
                name=steel_name(t, qu, th), as_on=as_on, moves=moves,
                balance=left,
                total_in=r3(sum(m['kg_in'] for m in moves)),
                total_out=r3(sum(m['kg_out'] for m in moves)))


def _steel_entry_lines(conn, ids):
    got = {}
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        for r in conn.execute(
                'SELECT * FROM steel_entry_line WHERE entry_id IN (%s) ORDER BY id'
                % ','.join('?' * len(part)), part):
            got.setdefault(r['entry_id'], []).append(dict(r))
    return got


def list_steel_entries(conn, cid, q):
    fid = _steel_firm(conn, cid)
    frm = _steel_day(q.get('from'), 'From')
    to = _steel_day(q.get('to'), 'To')
    sql, args = 'SELECT * FROM steel_entry WHERE company_id=?', [fid]
    if frm:
        sql += ' AND entry_date>=?'
        args.append(frm)
    if to:
        sql += ' AND entry_date<=?'
        args.append(to)
    kind = (q.get('kind') or '').upper()
    if kind in STEEL_ENTRY_KINDS:
        sql += ' AND kind=?'
        args.append(kind)
    sql += ' ORDER BY entry_date DESC, id DESC'
    out = rows(conn.execute(sql, args))
    lines = _steel_entry_lines(conn, [e['id'] for e in out])
    for e in out:
        e['lines'] = lines.get(e['id'], [])
        e['what'] = STEEL_ENTRY_KINDS.get(e['kind'], e['kind'])
        e['weight'] = r3(sum(l['weight'] for l in e['lines']))
    return dict(rows=out, count=len(out),
                weight=r3(sum(e['weight'] for e in out)))


def get_steel_entry(conn, cid, eid):
    fid = _steel_firm(conn, cid)
    e = one(conn.execute('SELECT * FROM steel_entry WHERE id=? AND company_id=?',
                         (eid, fid)))
    if not e:
        raise ApiError('That stock entry is not there any more.', 404)
    e['lines'] = _steel_entry_lines(conn, [eid]).get(eid, [])
    e['what'] = STEEL_ENTRY_KINDS.get(e['kind'], e['kind'])
    return e


def save_steel_entry(conn, cid, eid, data):
    """Opening stock or steel come in: a date, who it came from, and a line
    for each piece of steel with its kilograms."""
    fid = _steel_firm(conn, cid)
    kind = (data.get('kind') or 'IN').upper()
    if kind not in STEEL_ENTRY_KINDS:
        raise ApiError('A stock entry is Stock in, Opening stock, Stock '
                       'removed or Stock counted.')
    day = _steel_day(data.get('entry_date'), 'Date')
    if not day:
        raise ApiError('Give the date.')
    book = {}
    if kind == 'COUNT':
        for x in steel_stock(conn, cid, dict(as_on=day, skip=eid or ''))['rows']:
            book[(x['steel_type'], x['steel_quality'], x['steel_thick'])] = x['balance']
    before = [(l['steel_type'], l['steel_quality'], l['steel_thick'])
              for l in (get_steel_entry(conn, cid, eid)['lines'] if eid else [])]
    lines = []
    for n, ln in enumerate(data.get('lines') or [], 1):
        t, qu, th = (steel_word(ln.get('steel_type')),
                     steel_word(ln.get('steel_quality')),
                     steel_word(ln.get('steel_thick')))
        typed = str(ln.get('weight') if ln.get('weight') is not None
                    else '').replace(',', '').strip()
        try:
            kg = float(typed or 0)
        except ValueError:
            raise ApiError('Line %d: the kilograms are not a number.' % n)
        if not (t or qu or th or typed):
            continue                      # a blank line left at the foot
        if not (t and qu and th):
            raise ApiError('Line %d: choose the type, quality and thickness.' % n)
        odd = steel_unknown(conn, (t, qu, th), before)
        if odd:
            raise ApiError('Line %d: "%s" is not on the steel list.' % (n, odd))
        if kind == 'COUNT':
            # A count of nought is a steel cleared; only a blank is no count.
            if not typed:
                raise ApiError('Line %d: give the kilos you have of %s.'
                               % (n, steel_name(t, qu, th)))
            if kg < 0:
                raise ApiError('Line %d: a count cannot be below nothing.' % n)
            if any(x[:3] == (t, qu, th) for x in lines):
                raise ApiError('Line %d: %s is counted twice. Put the whole '
                               'count on one line.' % (n, steel_name(t, qu, th)))
            lines.append((t, qu, th, r3(kg - book.get((t, qu, th), 0.0)), r3(kg)))
            continue
        if kg <= 0:
            raise ApiError('Line %d: give the kilograms (more than 0).' % n)
        lines.append((t, qu, th, r3(-kg if kind == 'OUT' else kg), None))
    if not lines:
        raise ApiError('Add at least one line of steel.')
    head = dict(kind=kind, entry_date=day,
                party=re.sub(r'\s+', ' ', str(data.get('party') or '')).strip()[:120],
                ref_no=str(data.get('ref_no') or '').strip()[:60],
                note=str(data.get('note') or '').strip()[:300])
    if eid:
        get_steel_entry(conn, cid, eid)          # it is there, and it is ours
        conn.execute('UPDATE steel_entry SET kind=?, entry_date=?, party=?, '
                     'ref_no=?, note=? WHERE id=?',
                     (head['kind'], head['entry_date'], head['party'],
                      head['ref_no'], head['note'], eid))
        conn.execute('DELETE FROM steel_entry_line WHERE entry_id=?', (eid,))
    else:
        eid = conn.execute(
            'INSERT INTO steel_entry(company_id, kind, entry_date, party, '
            'ref_no, note) VALUES (?,?,?,?,?,?)',
            (fid, head['kind'], head['entry_date'], head['party'],
             head['ref_no'], head['note'])).lastrowid
    conn.executemany(
        'INSERT INTO steel_entry_line(entry_id, steel_type, steel_quality, '
        'steel_thick, weight, counted) VALUES (?,?,?,?,?,?)',
        [(eid,) + ln for ln in lines])
    conn.commit()
    return get_steel_entry(conn, cid, eid)


def delete_steel_entry(conn, cid, eid):
    get_steel_entry(conn, cid, eid)
    conn.execute('DELETE FROM steel_entry_line WHERE entry_id=?', (eid,))
    conn.execute('DELETE FROM steel_entry WHERE id=?', (eid,))
    conn.commit()
    return dict(deleted=eid)


# ----------------------------------------------------- personal accounts
#
# The user's own money, kept apart from the business (30 Sep 2026: "a new
# page named Personal Accounts where I will be managing my personal accounts
# which aren't related to the business ... the money given or received from
# different people"). A person, and each sum given to them or received from
# them. Given counts towards what they owe you, received towards what you
# owe them; the balance is the one less the other. Nothing here belongs to
# a firm or touches an account, and SHREE's update never writes here.

PERSONAL_WAYS = {'GIVEN': 'Given', 'RECEIVED': 'Received'}
PERSONAL_MAX = 1e11                  # ten thousand crore: a slip, not a loan


def _inr(v):
    """1234567.5 -> ₹12,34,567.50, the Indian way the screens write it."""
    v = abs(float(v or 0))
    whole, paise = ('%.2f' % v).split('.')
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return '₹' + ','.join(groups + [tail]) + '.' + paise


def _entries_text(n):
    return '%d entr%s' % (n, 'y' if n == 1 else 'ies')


def _personal_stands(bal):
    """Where somebody stands, as a clause after their name."""
    if abs(bal or 0) < 0.005:
        return ', settled'
    return (', owes you %s' % _inr(bal) if bal > 0
            else ', you owe them %s' % _inr(-bal))


def _signed(e):
    return float(e['amount']) if e['direction'] == 'GIVEN' else -float(e['amount'])


def _personal_find(conn, name, skip=None):
    """The person already on the list under this name -- read without its
    spaces, case or punctuation, so RAMESH BHAI and Ramesh Bhai are one."""
    key = squash(name)
    for r in conn.execute('SELECT * FROM personal_person'):
        if squash(r['name']) == key and r['id'] != skip:
            return dict(r)
    return None


def _personal_person(conn, pid):
    r = one(conn.execute('SELECT * FROM personal_person WHERE id=?', (pid,)))
    if not r:
        raise ApiError('That person is not on Personal Accounts any more.', 404)
    return r


def _personal_text(v, what, most):
    if v is None:
        return ''
    if not isinstance(v, str):
        if isinstance(v, (dict, list, bool)):
            raise ApiError('%s should be text.' % what)
        v = str(v)
    v = re.sub(r'[ \t]+', ' ', v).strip()
    if len(v) > most:
        raise ApiError('%s is %d letters long; it can be at most %d.'
                       % (what, len(v), most))
    return v


def _personal_range(q):
    frm = _steel_day(q.get('from'), 'From')
    to = _steel_day(q.get('to'), 'To')
    if frm and to and frm > to:
        raise ApiError('From is after To.')
    return frm, to


def personal_people(conn):
    """Everybody, with where they stand today -- for the pick window."""
    out = rows(conn.execute(
        'SELECT p.id, p.name, p.mobile, COUNT(e.id) entries, '
        '  MAX(e.entry_date) last_date, COALESCE(SUM(CASE e.direction '
        "  WHEN 'GIVEN' THEN e.amount ELSE -e.amount END), 0) balance "
        'FROM personal_person p LEFT JOIN personal_entry e ON e.person_id=p.id '
        'GROUP BY p.id ORDER BY p.name COLLATE NOCASE'))
    for r in out:
        r['balance'] = r2(r['balance'])
    return out


def list_personal(conn, q):
    """Everybody on Personal Accounts: what was given and received between
    the dates, and where each one stands at the end of them. With no From,
    everything before counts; with no To, everything after."""
    frm, to = _personal_range(q)
    lo, hi = frm or '0000-00-00', to or '9999-12-31'
    people = rows(conn.execute(
        'SELECT p.id, p.name, p.mobile, p.notes, p.created_at, '
        "  COALESCE(SUM(CASE WHEN e.entry_date<? THEN (CASE e.direction "
        "    WHEN 'GIVEN' THEN e.amount ELSE -e.amount END) END), 0) opening, "
        "  COALESCE(SUM(CASE WHEN e.entry_date BETWEEN ? AND ? "
        "    AND e.direction='GIVEN' THEN e.amount END), 0) given, "
        "  COALESCE(SUM(CASE WHEN e.entry_date BETWEEN ? AND ? "
        "    AND e.direction='RECEIVED' THEN e.amount END), 0) received, "
        '  COUNT(CASE WHEN e.entry_date BETWEEN ? AND ? THEN 1 END) entries, '
        '  COUNT(e.id) all_entries, '
        "  COUNT(CASE WHEN e.source<>'' THEN 1 END) imported, "
        '  MAX(CASE WHEN e.entry_date<=? THEN e.entry_date END) last_date '
        'FROM personal_person p LEFT JOIN personal_entry e ON e.person_id=p.id '
        'GROUP BY p.id ORDER BY p.name COLLATE NOCASE',
        (lo, lo, hi, lo, hi, lo, hi, hi)))
    t = dict(to_get=0.0, to_get_count=0, to_give=0.0, to_give_count=0,
             given=0.0, received=0.0, settled_count=0)
    for r in people:
        for k in ('opening', 'given', 'received'):
            r[k] = r2(r[k])
        r['balance'] = r2(r['opening'] + r['given'] - r['received'])
        if r['balance'] > 0.005:
            t['to_get'] += r['balance']
            t['to_get_count'] += 1
        elif r['balance'] < -0.005:
            t['to_give'] -= r['balance']
            t['to_give_count'] += 1
        else:
            t['settled_count'] += 1
        t['given'] += r['given']
        t['received'] += r['received']
    for k in ('to_get', 'to_give', 'given', 'received'):
        t[k] = r2(t[k])
    return dict(rows=people, totals=t, count=len(people), start=frm, end=to)


def get_personal(conn, pid, q=None):
    """One person: their entries between the dates, oldest first with the
    balance after each, and where they stood before the first."""
    frm, to = _personal_range(q or {})
    p = _personal_person(conn, pid)
    every = rows(conn.execute(
        'SELECT e.*, c.code firm FROM personal_entry e '
        'LEFT JOIN company c ON c.id=e.company_id WHERE e.person_id=? '
        'ORDER BY e.entry_date, e.id', (pid,)))
    opening = sum(_signed(e) for e in every if frm and e['entry_date'] < frm)
    run, shown = opening, []
    for e in every:
        if (frm and e['entry_date'] < frm) or (to and e['entry_date'] > to):
            continue
        run += _signed(e)
        e['amount'] = r2(e['amount'])
        e['balance'] = r2(run)
        shown.append(e)
    given = sum(e['amount'] for e in shown if e['direction'] == 'GIVEN')
    received = sum(e['amount'] for e in shown if e['direction'] == 'RECEIVED')
    return dict(person=p, entries=shown, opening=r2(opening),
                given=r2(given), received=r2(received), closing=r2(run),
                balance=r2(sum(_signed(e) for e in every)),
                all_entries=len(every),
                later=sum(1 for e in every if to and e['entry_date'] > to),
                first_date=every[0]['entry_date'] if every else '',
                sources=_personal_sources(conn, pid),
                start=frm, end=to)


def save_personal_person(conn, pid, data):
    """Somebody put on Personal Accounts, or their name, number or notes
    changed."""
    name = re.sub(r'\s+', ' ', check_name(data, 'The'))
    mobile = _personal_text(data.get('mobile'), 'The mobile number', 40)
    notes = _personal_text(data.get('notes'), 'The notes', 500)
    if pid:
        _personal_person(conn, pid)
    twin = _personal_find(conn, name, skip=pid)
    if twin:
        raise ApiError('%s is already on Personal Accounts.' % twin['name'],
                       extra=dict(person_id=twin['id']))
    if pid:
        conn.execute('UPDATE personal_person SET name=?, mobile=?, notes=? '
                     'WHERE id=?', (name, mobile, notes, pid))
    else:
        pid = conn.execute('INSERT INTO personal_person(name, mobile, notes) '
                           'VALUES (?,?,?)', (name, mobile, notes)).lastrowid
    conn.commit()
    return _personal_person(conn, pid)


def delete_personal_person(conn, pid):
    """Somebody taken off, with every entry they have."""
    _personal_person(conn, pid)
    n = conn.execute('DELETE FROM personal_entry WHERE person_id=?',
                     (pid,)).rowcount
    conn.execute('DELETE FROM personal_person WHERE id=?', (pid,))
    conn.commit()
    return dict(deleted=pid, entries=n)


def get_personal_entry(conn, eid):
    e = one(conn.execute(
        'SELECT e.*, p.name FROM personal_entry e '
        'JOIN personal_person p ON p.id=e.person_id WHERE e.id=?', (eid,)))
    if not e:
        raise ApiError('That entry is not there any more.', 404)
    e['amount'] = r2(e['amount'])
    return e


def save_personal_entry(conn, eid, data):
    """Money given to somebody or received from them. The person is named
    by `person_id`, or by `name` -- somebody not yet on the list is put on
    it as the entry is saved."""
    way = str(data.get('direction') or '').strip().upper()
    if way not in PERSONAL_WAYS:
        raise ApiError('Say whether the money was given or received.')
    try:
        amount = float(data.get('amount') or 0)
    except (TypeError, ValueError):
        raise ApiError('The amount is not a figure.')
    if not amount > 0:
        raise ApiError('Give the amount (more than 0).')
    if amount >= PERSONAL_MAX:
        raise ApiError('%s is more than an amount can be.' % _inr(amount))
    day = _steel_day(data.get('entry_date'), 'Date')
    if not day:
        raise ApiError('Give the date.')
    mode = _personal_text(data.get('mode'), 'How it was paid', 30)
    note = _personal_text(data.get('note'), 'What it was for', 300)
    if eid:
        get_personal_entry(conn, eid)                 # it is still there
    pid = data.get('person_id')
    if pid not in (None, ''):
        pid = _personal_person(conn, qint(pid, 'person'))['id']
    else:
        name = re.sub(r'\s+', ' ', check_name(data, 'The person\'s'))
        found = _personal_find(conn, name)
        pid = found['id'] if found else conn.execute(
            'INSERT INTO personal_person(name) VALUES (?)', (name,)).lastrowid
    row = (pid, day, way, r2(amount), mode, note)
    if eid:
        conn.execute('UPDATE personal_entry SET person_id=?, entry_date=?, '
                     'direction=?, amount=?, mode=?, note=? WHERE id=?',
                     row + (eid,))
    else:
        eid = conn.execute(
            'INSERT INTO personal_entry(person_id, entry_date, direction, '
            'amount, mode, note) VALUES (?,?,?,?,?,?)', row).lastrowid
    conn.commit()
    e = get_personal_entry(conn, eid)
    e['balance'] = r2(conn.execute(
        "SELECT COALESCE(SUM(CASE direction WHEN 'GIVEN' THEN amount "
        'ELSE -amount END), 0) FROM personal_entry WHERE person_id=?',
        (pid,)).fetchone()[0])
    return e


def delete_personal_entry(conn, eid):
    get_personal_entry(conn, eid)
    conn.execute('DELETE FROM personal_entry WHERE id=?', (eid,))
    conn.commit()
    return dict(deleted=eid)


# ------------------------------------- personal accounts: from the books
#
# 30 Sep 2026, the same day: "Add an option to import a normal account to
# personal account." Some of the money kept in the business books is the
# family's own -- BSTP even has a PERSONAL ACCOUNT group -- so an account can
# be brought across: every entry on it, the entries from a date with what
# came before as one opening line, or only its balance on a day. It is read
# exactly as the Accounts screen reads it (`report_ledger`), so the figure
# brought across is the figure seen there. A debit on the account -- money
# that went to them -- comes in as given, a credit as received.
#
# It is a copy. The account stays in the business books as it was, and a
# SHREE update goes on writing to it there. Each line brought in remembers
# the account it came from, so bringing the same account in again replaces
# those lines -- after SHREE has added to it, say -- and never doubles them.

PERSONAL_IMPORT_MODES = ('all', 'from', 'balance')
# Where a family's money is usually kept, offered before anything is typed:
# any group named PERSONAL first, then capital and loans. Not Deposits: the
# firms' accounts with one another sit there.
PERSONAL_LIKELY_GROUPS = ('1001', '2501', '3401', '6001', '6301')


def _account_key(firm, name):
    return '%s|%s' % (firm, ' '.join((name or '').upper().split()))


def _dmy(day):
    return '-'.join(reversed((day or '').split('-')))


def personal_accounts(conn, q):
    """Accounts in the business books that could be somebody's: every asset
    and liability account in the firms, less the firms' own cash and bank
    and the built-in ones. With nothing typed, the likely ones."""
    term = q.get('search') or ''
    sql = ('SELECT l.id, l.name, l.company_id, l.city, l.phone, '
           '  c.code firm, g.code gcode, g.name group_name '
           'FROM ledger l JOIN company c ON c.id=l.company_id '
           'JOIN account_group g ON g.id=l.group_id '
           "WHERE l.system_key IS NULL AND g.nature IN ('asset', 'liability') "
           'AND NOT ' + MONEY_SQL)
    args = []
    where, wargs = name_search(term, ['l.name', 'l.city', 'g.name'], 'l.name')
    if where:
        sql += ' AND ' + where
        args += wargs
    else:
        sql += (" AND (upper(g.name) LIKE '%%PERSONAL%%' OR g.code IN (%s))"
                % ','.join('?' * len(PERSONAL_LIKELY_GROUPS)))
        args += list(PERSONAL_LIKELY_GROUPS)
    rank, rargs = name_rank(term, 'l.name')
    sql += (' ORDER BY %sCASE WHEN upper(g.name) LIKE \'%%PERSONAL%%\' '
            'THEN 0 ELSE 1 END, l.name LIMIT 150' % (rank + ', ' if rank else ''))
    found = rows(conn.execute(sql, args + rargs))
    bals = {co: ledger_balances(conn, co)
            for co in {r['company_id'] for r in found}}
    counts = {}
    for part in _chunks([r['id'] for r in found], 500):
        for r in conn.execute(
                'SELECT ledger_id, COUNT(*) n, MAX(date) last FROM posting '
                'WHERE ledger_id IN (%s) GROUP BY ledger_id'
                % ','.join('?' * len(part)), part):
            counts[r['ledger_id']] = (r['n'], r['last'])
    brought = {r['source']: r['name'] for r in conn.execute(
        'SELECT DISTINCT e.source, p.name FROM personal_entry e '
        "JOIN personal_person p ON p.id=e.person_id WHERE e.source<>''")}
    for r in found:
        r['balance'] = r2(bals[r['company_id']].get(r['id'], 0))
        r['entries'], r['last_date'] = counts.get(r['id'], (0, None))
        r['personal'] = 'PERSONAL' in (r['group_name'] or '').upper()
        r['brought_into'] = brought.get(_account_key(r['firm'], r['name']))
    return found


def _import_mode(e):
    """How the money moved, as far as the entry says."""
    k = e['doc_kind']
    if k in MONEY_KINDS:
        return 'Cash' if 'CASH' in (e.get('particulars') or '').upper() else 'Bank'
    if k == 'JOURNAL':
        return 'Journal'
    return '' if k == 'OPENING' else e.get('kind') or ''


def _import_plan(conn, q):
    """What bringing an account across would write: (account, key, lines,
    what to say about it). Reads only."""
    lid = qint(q.get('ledger_id'), 'account')
    led = one(conn.execute(
        'SELECT l.*, c.code firm, g.name group_name FROM ledger l '
        'JOIN company c ON c.id=l.company_id '
        'JOIN account_group g ON g.id=l.group_id WHERE l.id=?', (lid,)))
    if not led:
        raise ApiError('That account is not in the books any more.', 404)
    mode = str(q.get('mode') or 'all').strip().lower()
    if mode not in PERSONAL_IMPORT_MODES:
        raise ApiError('Bring in every entry, the entries from a date, or '
                       'only the balance.')
    day = _steel_day(q.get('date'), 'Date')
    if mode != 'all' and not day:
        raise ApiError('Give the date.')
    look = {'ledger_id': lid}
    if mode == 'from':
        look['from'] = day
    elif mode == 'balance':
        look['to'] = day
    rep = report_ledger(conn, led['company_id'], look)
    if not rep.get('accounted'):
        raise ApiError('%s is not kept in the books the money is written in, '
                       'so where it stands cannot be read here.' % led['name'])
    # Named by the account the money is written in -- the same whichever of
    # a customer's firms it was picked in.
    held = sorted((b['company_id'], b['ledger_id']) for b in rep['in_books']
                  if b['is_account'])
    home = one(conn.execute(
        'SELECT l.name, c.code firm FROM ledger l JOIN company c '
        'ON c.id=l.company_id WHERE l.id=?', (held[0][1],))) if held else led
    key = _account_key(home['firm'], home['name'])
    today = datetime.date.today().isoformat()
    lines = []
    if mode == 'balance':
        opening, open_day = float(rep['closing'] or 0), day
        open_note = 'Balance on %s in the %s books' % (_dmy(day), home['firm'])
    else:
        opening = float(rep['opening'] or 0)
        entries = rep['entries']
        open_day = day if mode == 'from' else (
            entries[0]['date'] if entries and entries[0]['date'] else today)
        open_note = ('Balance brought forward from the %s books' if mode == 'from'
                     else 'Opening balance in the %s books') % home['firm']
        for e in entries:
            net = r2(e['debit'] - e['credit'])
            if abs(net) < 0.005:
                continue
            lines.append(dict(
                entry_date=e['date'] or open_day,
                direction='GIVEN' if net > 0 else 'RECEIVED', amount=abs(net),
                mode=_import_mode(e)[:30],
                note=(e['narration'] or e['particulars'] or '')[:300],
                ref=('%s %s' % (e['kind'], e['no'])).strip()[:60],
                doc_kind=e['doc_kind'] if e['doc_id'] else '',
                doc_id=e['doc_id'] or None, company_id=e['company_id']))
    if abs(opening) >= 0.005:
        lines.insert(0, dict(
            entry_date=open_day, direction='GIVEN' if opening > 0 else 'RECEIVED',
            amount=r2(abs(opening)), mode='', note=open_note, ref='',
            doc_kind='', doc_id=None, company_id=None))
    given = r2(sum(x['amount'] for x in lines if x['direction'] == 'GIVEN'))
    received = r2(sum(x['amount'] for x in lines if x['direction'] == 'RECEIVED'))
    before = rows(conn.execute(
        'SELECT p.id, p.name, COUNT(*) n FROM personal_entry e '
        'JOIN personal_person p ON p.id=e.person_id WHERE e.source=? '
        'GROUP BY p.id ORDER BY p.name', (key,)))
    twin = _personal_find(conn, led['name'])
    info = dict(
        account=dict(id=led['id'], name=led['name'], firm=led['firm'],
                     group_name=led['group_name'], phone=led['phone'] or ''),
        key=key, mode=mode, date=day, count=len(lines), given=given,
        received=received, balance=r2(given - received),
        first_date=min((x['entry_date'] for x in lines), default=''),
        last_date=max((x['entry_date'] for x in lines), default=''),
        brought_before=before,
        person=dict(id=twin['id'], name=twin['name']) if twin
        else dict(id=None, name=led['name']),
        latest=sorted(lines, key=lambda x: x['entry_date'], reverse=True)[:6])
    return led, key, lines, info


def personal_import_preview(conn, q):
    """What Import an account would bring across, before it does."""
    return _import_plan(conn, q)[3]


def import_personal(conn, data):
    """Bring an account across into somebody's page. Lines brought in from
    the same account before -- into anybody -- are replaced."""
    led, key, lines, info = _import_plan(conn, data)
    if not lines:
        raise ApiError('Nothing to bring in: %s has no entries and no balance%s.'
                       % (led['name'], ' on %s' % _dmy(info['date'])
                          if info['mode'] == 'balance' else
                          ' from %s' % _dmy(info['date'])
                          if info['mode'] == 'from' else ''))
    pid = data.get('person_id')
    if pid not in (None, ''):
        pid = _personal_person(conn, qint(pid, 'person'))['id']
    else:
        name = re.sub(r'\s+', ' ', check_name(data, 'The person\'s'))
        found = _personal_find(conn, name)
        pid = found['id'] if found else conn.execute(
            'INSERT INTO personal_person(name, mobile) VALUES (?,?)',
            (name, (led['phone'] or '').strip()[:40])).lastrowid
    gone = conn.execute('DELETE FROM personal_entry WHERE source=?',
                        (key,)).rowcount
    conn.executemany(
        'INSERT INTO personal_entry(person_id, entry_date, direction, amount, '
        'mode, note, source, ref, doc_kind, doc_id, company_id) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        [(pid, x['entry_date'], x['direction'], x['amount'], x['mode'],
          x['note'], key, x['ref'], x['doc_kind'], x['doc_id'],
          x['company_id']) for x in lines])
    conn.commit()
    bal = conn.execute(
        "SELECT COALESCE(SUM(CASE direction WHEN 'GIVEN' THEN amount "
        'ELSE -amount END), 0) FROM personal_entry WHERE person_id=?',
        (pid,)).fetchone()[0]
    return dict(person=_personal_person(conn, pid), entries=len(lines),
                replaced=gone, balance=r2(bal), account=info['account'])


def _personal_sources(conn, pid):
    """The accounts somebody's lines were brought in from, and where each is
    in the books now -- for Bring in again."""
    out = []
    for r in conn.execute(
            'SELECT source, COUNT(*) n FROM personal_entry '
            "WHERE person_id=? AND source<>'' GROUP BY source ORDER BY source",
            (pid,)):
        firm, _, name = r['source'].partition('|')
        led = None
        for l in conn.execute(
                'SELECT l.id, l.name FROM ledger l JOIN company c '
                'ON c.id=l.company_id WHERE c.code=? AND upper(l.name) LIKE ?',
                (firm, name.split(' ')[0] + '%')):
            if ' '.join(l['name'].upper().split()) == name:
                led = l
                break
        out.append(dict(key=r['source'], firm=firm, name=led['name'] if led else name,
                        ledger_id=led['id'] if led else None, entries=r['n']))
    return out
