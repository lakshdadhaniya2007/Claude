"""Check the books against themselves.

Every figure the app shows is derived from three places that have to agree:
the document header, its lines, and the double-entry rows in `posting`. An
import from SHREE writes all three from files that are thirteen years old and
were never meant to be read by anything but SHREE, so they can disagree. This
says where.

Nothing here writes. Run it from the command line, or open Reports -> Data
health, which calls `run()` and shows the same list.

    python3 app/verify.py                # every firm
    python3 app/verify.py --company BST  # one firm
    python3 app/verify.py --detail       # list the offending documents
"""
import contextlib
import re
import sqlite3

import db

# A round off is a rupee either way. Beyond this the document is not being
# rounded, it is not adding up, and the difference has been parked in the
# Round Off column because there was nowhere else for it to go.
ROUND_OFF_LIMIT = 25.0
ROUND_OFF_SHARE = 0.005        # ... or half a percent of the total, if larger

# Non-printing bytes that came out of a FoxPro memo field rather than off
# anyone's keyboard.
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
CONTROL_GLOB = '*[%s]*' % ''.join(chr(c) for c in list(range(1, 9)) + [11, 12]
                                  + list(range(14, 32)) + [127])

BAD, WARN, OK = 'bad', 'warn', 'ok'


class Check(object):
    """One question asked of the books, and what came back."""

    __slots__ = ('key', 'title', 'level', 'count', 'amount', 'note', 'rows',
                 'grouped')

    def __init__(self, key, title, level=OK, count=0, amount=0.0, note='',
                 rows=None, grouped=False):
        self.key, self.title, self.level = key, title, level
        self.count, self.amount, self.note = count, amount, note
        self.rows = rows or []
        # True where the rows are already a summary -- so many per firm, so
        # many of a kind -- rather than one row per document. Without it a
        # breakdown of two lines covering eight hundred documents reads as
        # "the first 2 of 809", which is not what it is.
        self.grouped = grouped

    def as_dict(self):
        return dict(key=self.key, title=self.title, level=self.level,
                    count=self.count, amount=round(self.amount, 2),
                    note=self.note, rows=self.rows, grouped=self.grouped)


def _r2(x):
    return round((x or 0) + (1e-9 if (x or 0) >= 0 else -1e-9), 2)


def _companies(conn, cid=None):
    sql = 'SELECT id, code, name, tax_mode FROM company'
    args = ()
    if cid:
        sql += ' WHERE id=?'
        args = (cid,)
    return conn.execute(sql + ' ORDER BY id', args).fetchall()


def _scope(cid, col='company_id'):
    """SQL fragment limiting a query to one firm, or to all of them."""
    return (' AND %s=%d' % (col, cid)) if cid else ''


# --------------------------------------------------------------- the checks

def check_trial_balance(conn, cid=None):
    """Debits must equal credits in every firm, openings included."""
    rows, worst = [], 0.0
    for co in _companies(conn, cid):
        p = conn.execute(
            'SELECT COALESCE(SUM(debit),0) d, COALESCE(SUM(credit),0) c '
            'FROM posting WHERE company_id=?', (co['id'],)).fetchone()
        ob = conn.execute('SELECT COALESCE(SUM(opening_bal),0) s FROM ledger '
                          'WHERE company_id=?', (co['id'],)).fetchone()['s']
        diff = _r2(p['d'] - p['c'] + ob)
        worst = max(worst, abs(diff))
        if abs(diff) > 0.005:
            rows.append(dict(company=co['code'], difference=diff,
                             debit=_r2(p['d']), credit=_r2(p['c']),
                             openings=_r2(ob)))
    return Check('trial_balance', 'Trial balance is square in every firm',
                 BAD if rows else OK, len(rows), worst,
                 'Every firm balances to the paisa.' if not rows else
                 'A firm whose debits and credits differ cannot produce a '
                 'balance sheet that means anything.', rows)


def check_document_postings(conn, cid=None):
    """Each document's own postings must balance."""
    rows = conn.execute(
        'SELECT c.code company, i.doc_type, i.series, i.invoice_no, '
        '  i.invoice_date, ROUND(x.d - x.c, 2) difference, i.id '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        'JOIN (SELECT doc_kind, doc_id, SUM(debit) d, SUM(credit) c '
        '      FROM posting GROUP BY doc_kind, doc_id) x '
        '  ON x.doc_kind=i.doc_type AND x.doc_id=i.id '
        'WHERE ABS(x.d - x.c) > 0.005' + _scope(cid, 'i.company_id') +
        ' ORDER BY ABS(x.d - x.c) DESC LIMIT 200').fetchall()
    v = conn.execute(
        'SELECT c.code company, v.kind doc_type, ? series, v.voucher_no '
        '  invoice_no, v.voucher_date invoice_date, ROUND(x.d - x.c, 2) '
        '  difference, v.id '
        'FROM voucher v JOIN company c ON c.id=v.company_id '
        'JOIN (SELECT doc_kind, doc_id, SUM(debit) d, SUM(credit) c '
        '      FROM posting GROUP BY doc_kind, doc_id) x '
        '  ON x.doc_kind=v.kind AND x.doc_id=v.id '
        'WHERE ABS(x.d - x.c) > 0.005' + _scope(cid, 'v.company_id') +
        ' ORDER BY ABS(x.d - x.c) DESC LIMIT 200', ('',)).fetchall()
    out = [dict(r) for r in rows] + [dict(r) for r in v]
    amt = sum(abs(r['difference']) for r in out)
    return Check('doc_postings', 'Every document posts a balanced entry',
                 BAD if out else OK, len(out), amt,
                 'Debits equal credits on every document.' if not out else
                 'One side of these entries is missing, so the ledger they '
                 'hit is out by that much.', out)


def check_missing_postings(conn, cid=None):
    """A document with a value but no ledger entry is invisible in the books."""
    rows = conn.execute(
        'SELECT c.code company, i.doc_type, i.series, i.invoice_no, '
        '  i.invoice_date, i.grand_total amount, l.name party, i.id '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        'JOIN ledger l ON l.id=i.party_id '
        'WHERE ABS(i.grand_total) > 0.005 AND NOT EXISTS ('
        '  SELECT 1 FROM posting p WHERE p.doc_kind=i.doc_type '
        '  AND p.doc_id=i.id)' + _scope(cid, 'i.company_id') +
        ' ORDER BY ABS(i.grand_total) DESC LIMIT 200').fetchall()
    out = [dict(r) for r in rows]
    amt = sum(abs(r['amount']) for r in out)
    return Check('missing_postings', 'Every document reaches the ledgers',
                 WARN if out else OK, len(out), amt,
                 'Every document with a value posts to the ledgers.'
                 if not out else
                 'These sit in the register but in no ledger, so the party '
                 'statement does not show them. Where the year they belong to '
                 'was never backed up, the opening balance carried forward '
                 'already covers them and posting them now would count them '
                 'twice.', out)


def check_totals_add_up(conn, cid=None):
    """grand total = taxable + tax + (charges, in a no-tax firm) + round off."""
    rows = conn.execute(
        'SELECT c.code company, i.doc_type, i.series, i.invoice_no, '
        '  i.invoice_date, i.grand_total, i.id, ROUND(i.grand_total - ('
        '   i.taxable + i.cgst + i.sgst + i.igst + i.cess + i.round_off + '
        "   CASE WHEN c.tax_mode='none' "
        '    THEN i.cutting + i.transport + i.loading ELSE 0 END), 2) difference '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        'WHERE ABS(i.grand_total - ('
        '   i.taxable + i.cgst + i.sgst + i.igst + i.cess + i.round_off + '
        "   CASE WHEN c.tax_mode='none' "
        '    THEN i.cutting + i.transport + i.loading ELSE 0 END)) > 0.05'
        + _scope(cid, 'i.company_id') +
        ' ORDER BY 8 DESC LIMIT 200').fetchall()
    out = [dict(r) for r in rows]
    return Check('totals', 'Every document total adds up from its own parts',
                 BAD if out else OK, len(out),
                 sum(abs(r['difference']) for r in out),
                 'Taxable value plus tax plus round off comes to the grand '
                 'total on every document.' if not out else
                 'The stored total is not what the parts come to.', out)


def check_lines_match_header(conn, cid=None):
    """The header's own subtotal, weight and tax come off its lines."""
    out, found, amount = [], 0, 0.0
    for col, expr, label in (
            ('subtotal', 'SUM(taxable)', 'subtotal'),
            ('weight_total', 'SUM(weight)', 'weight'),
            ('cgst', 'SUM(cgst)', 'CGST'),
            ('sgst', 'SUM(sgst)', 'SGST'),
            ('igst', 'SUM(igst)', 'IGST')):
        tol = 0.5 if col == 'weight_total' else 1.0
        body = ('FROM invoice i JOIN company c ON c.id=i.company_id '
                'JOIN (SELECT invoice_id, %s v FROM invoice_item '
                '      GROUP BY invoice_id) x ON x.invoice_id=i.id '
                'WHERE ABS(i.%s - x.v) > ?' % (expr, col)
                + _scope(cid, 'i.company_id'))
        # Counted in full, listed in part: the list is for the eye, the count
        # is the answer.
        tot = conn.execute(
            'SELECT COUNT(*) n, COALESCE(SUM(ABS(i.%s - x.v)),0) s ' % col
            + body, (tol,)).fetchone()
        found += tot['n']
        amount += tot['s']
        rows = conn.execute(
            'SELECT c.code company, i.doc_type, i.series, i.invoice_no, '
            '  i.invoice_date, i.id, ? part, ROUND(i.%s - x.v, 2) difference '
            % col + body + ' ORDER BY ABS(i.%s - x.v) DESC LIMIT 40' % col,
            (label, tol)).fetchall()
        out += [dict(r) for r in rows]
    return Check('lines', 'Header figures come off the lines beneath them',
                 BAD if found else OK, found, amount,
                 'Subtotal, weight and tax match the lines on every document.'
                 if not out else
                 'The header says one thing and the lines another, so the '
                 'register and the printed document disagree.', out)


def check_line_signs(conn, cid=None):
    """A return is a negative document; its lines have to be negative too.

    SHREE files a sales return as a bill with a negative total and positive
    line amounts. Imported as they stand, the ledgers are right -- they come
    from the voucher file -- but every report that adds up line values counts
    the return as a sale, so the turnover is out by twice its value.
    """
    rows = conn.execute(
        'SELECT c.code company, i.doc_type, i.series, i.invoice_no, '
        '  i.invoice_date, i.fy, i.grand_total, i.id, '
        '  ROUND(x.v, 2) lines_total '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        'JOIN (SELECT invoice_id, SUM(taxable) v FROM invoice_item '
        '      GROUP BY invoice_id) x ON x.invoice_id=i.id '
        'WHERE ((i.grand_total < -0.005 AND x.v > 0.005) '
        '    OR (i.grand_total > 0.005 AND x.v < -0.005))'
        + _scope(cid, 'i.company_id') +
        ' ORDER BY ABS(x.v) DESC LIMIT 200').fetchall()
    out = [dict(r) for r in rows]
    return Check('line_signs', 'Returns are negative on both sides',
                 BAD if out else OK, len(out),
                 sum(abs(r['lines_total']) * 2 for r in out),
                 'No document has a total pointing one way and lines the '
                 'other.' if not out else
                 'These are credited in the ledgers but their lines read as '
                 'sales, so every report built on lines -- the register, the '
                 'GST summary, the rate-wise and HSN grids -- overstates '
                 'turnover by twice the value shown.', out)


def check_round_off(conn, cid=None):
    """Round Off should round; anything larger is unexplained value."""
    rows = conn.execute(
        'SELECT c.code company, i.doc_type, i.series, i.invoice_no, '
        '  i.invoice_date, i.taxable, i.round_off, i.grand_total, i.id, '
        '  ROUND(i.cgst + i.sgst + i.igst + i.cess, 2) tax '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        'WHERE ABS(i.round_off) > ? AND ABS(i.round_off) > ? * ABS(i.grand_total)'
        + _scope(cid, 'i.company_id') +
        ' ORDER BY ABS(i.round_off) DESC LIMIT 200',
        (ROUND_OFF_LIMIT, ROUND_OFF_SHARE)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # Where the figure parked in Round Off is a standard rate of the
        # taxable value, it is tax the old file did not record as tax.
        d['looks_like'] = ''
        if d['taxable']:
            pct = d['round_off'] / d['taxable'] * 100.0
            for rate in (28, 18, 12, 5, 3, 1.5, 1, 0.5, 0.25):
                if abs(abs(pct) - rate) < 0.12 and (pct > 0) == (d['taxable'] > 0):
                    d['looks_like'] = 'tax at %g%%' % rate
                    break
        out.append(d)
    total = conn.execute(
        'SELECT COUNT(*) n, COALESCE(SUM(ABS(round_off)),0) s FROM invoice i '
        'WHERE ABS(i.round_off) > ? AND ABS(i.round_off) > ? * ABS(i.grand_total)'
        + _scope(cid, 'i.company_id'),
        (ROUND_OFF_LIMIT, ROUND_OFF_SHARE)).fetchone()
    return Check('round_off', 'Round Off is only ever a rounding',
                 WARN if total['n'] else OK, total['n'], total['s'],
                 'No document parks real value in Round Off.'
                 if not total['n'] else
                 'These documents add up and their ledgers are right, but a '
                 'figure the old screen had no column for -- most often tax it '
                 'never recorded as tax -- was carried into Round Off. It is '
                 'in the total; it is just not labelled.', out)


def check_hidden_tax(conn, cid=None):
    """A no-tax firm holding documents that do carry tax."""
    rows = conn.execute(
        'SELECT c.code company, i.doc_type, COUNT(*) n, '
        '  MIN(i.invoice_date) first_date, MAX(i.invoice_date) last_date, '
        '  ROUND(SUM(i.cgst + i.sgst + i.igst + i.cess), 2) tax '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        "WHERE c.tax_mode='none' AND (i.cgst+i.sgst+i.igst+i.cess) <> 0"
        + _scope(cid, 'i.company_id') +
        ' GROUP BY 1, 2 ORDER BY 6 DESC').fetchall()
    out = [dict(r) for r in rows]
    return Check('hidden_tax', 'Tax on a document is shown to the user',
                 WARN if out else OK, sum(r['n'] for r in out),
                 sum(abs(r['tax']) for r in out),
                 'No firm is holding tax it cannot display.' if not out else
                 'These firms are set to No tax, but carry VAT or GST from '
                 'before that was true. The tax is inside the total and is '
                 'shown on the entry screen and the printed challan; the firm '
                 'setting only decides whether new documents charge tax.', out,
                 grouped=True)


def check_references(conn, cid=None):
    """Nothing points at a row that is not there, or in another firm's books."""
    tests = [
        ('a posting with no ledger',
         'SELECT COUNT(*) n FROM posting p LEFT JOIN ledger l ON l.id=p.ledger_id '
         'WHERE l.id IS NULL' + _scope(cid, 'p.company_id')),
        ("a posting against another firm's ledger",
         'SELECT COUNT(*) n FROM posting p JOIN ledger l ON l.id=p.ledger_id '
         'WHERE l.company_id<>p.company_id' + _scope(cid, 'p.company_id')),
        ('a document with no party',
         'SELECT COUNT(*) n FROM invoice i LEFT JOIN ledger l ON l.id=i.party_id '
         'WHERE l.id IS NULL' + _scope(cid, 'i.company_id')),
        ("a document billed to another firm's party",
         'SELECT COUNT(*) n FROM invoice i JOIN ledger l ON l.id=i.party_id '
         'WHERE l.company_id<>i.company_id' + _scope(cid, 'i.company_id')),
        ('a line with no document',
         'SELECT COUNT(*) n FROM invoice_item x '
         'LEFT JOIN invoice i ON i.id=x.invoice_id WHERE i.id IS NULL'),
        ("a line naming another firm's item",
         'SELECT COUNT(*) n FROM invoice_item x JOIN invoice i ON i.id=x.invoice_id '
         'JOIN item t ON t.id=x.item_id WHERE t.company_id<>i.company_id'
         + _scope(cid, 'i.company_id')),
        ('a voucher line with no voucher',
         'SELECT COUNT(*) n FROM voucher_line x '
         'LEFT JOIN voucher v ON v.id=x.voucher_id WHERE v.id IS NULL'),
        ("a voucher line against another firm's ledger",
         'SELECT COUNT(*) n FROM voucher_line x JOIN voucher v ON v.id=x.voucher_id '
         'JOIN ledger l ON l.id=x.ledger_id WHERE l.company_id<>v.company_id'
         + _scope(cid, 'v.company_id')),
        ('a ledger in no account group',
         'SELECT COUNT(*) n FROM ledger l '
         'LEFT JOIN account_group g ON g.id=l.group_id WHERE g.id IS NULL'
         + _scope(cid, 'l.company_id')),
        ('a challan pointing at a bill that is gone',
         'SELECT COUNT(*) n FROM invoice i LEFT JOIN invoice o ON o.id=i.billed_invoice '
         'WHERE i.billed_invoice IS NOT NULL AND o.id IS NULL'
         + _scope(cid, 'i.company_id')),
        ('a bill pointing at a challan that is gone',
         'SELECT COUNT(*) n FROM invoice i LEFT JOIN invoice o ON o.id=i.from_challan '
         'WHERE i.from_challan IS NOT NULL AND o.id IS NULL'
         + _scope(cid, 'i.company_id')),
        ('a customer billed by a firm that is gone',
         'SELECT COUNT(*) n FROM ledger l LEFT JOIN company c ON c.id=l.home_company '
         'WHERE l.home_company IS NOT NULL AND c.id IS NULL'
         + _scope(cid, 'l.company_id')),
        ('a posting whose document is gone',
         "SELECT COUNT(*) n FROM posting p WHERE p.doc_kind IN "
         "('SALE','PURCHASE','CREDIT','DEBIT') AND NOT EXISTS "
         '(SELECT 1 FROM invoice i WHERE i.id=p.doc_id)' + _scope(cid, 'p.company_id')),
        ('a voucher posting whose voucher is gone',
         "SELECT COUNT(*) n FROM posting p WHERE p.doc_kind IN "
         "('RECEIPT','PAYMENT','JOURNAL','CONTRA') AND NOT EXISTS "
         '(SELECT 1 FROM voucher v WHERE v.id=p.doc_id)' + _scope(cid, 'p.company_id')),
    ]
    out = []
    for label, sql in tests:
        n = conn.execute(sql).fetchone()['n']
        if n:
            out.append(dict(what=label, count=n))
    return Check('references', 'Nothing points at a row that is not there',
                 BAD if out else OK, sum(r['count'] for r in out), 0.0,
                 'Every reference resolves, and no firm reaches into another '
                 "firm's books." if not out else
                 'A dangling reference makes a screen fail rather than show a '
                 'wrong number.', out, grouped=True)


def check_dates(conn, cid=None):
    """Dates are real, and a posting carries its document's date."""
    out = []
    for label, sql in (
        ('a document with an unreadable date',
         "SELECT COUNT(*) n FROM invoice WHERE invoice_date NOT GLOB "
         "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'" + _scope(cid)),
        ('a voucher with an unreadable date',
         "SELECT COUNT(*) n FROM voucher WHERE voucher_date NOT GLOB "
         "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'" + _scope(cid)),
        ('a posting dated away from its document',
         'SELECT COUNT(*) n FROM posting p JOIN invoice i '
         'ON i.id=p.doc_id AND i.doc_type=p.doc_kind '
         'WHERE p.date<>i.invoice_date' + _scope(cid, 'p.company_id')),
        ('a posting dated away from its voucher',
         'SELECT COUNT(*) n FROM posting p JOIN voucher v '
         'ON v.id=p.doc_id AND v.kind=p.doc_kind '
         'WHERE p.date<>v.voucher_date' + _scope(cid, 'p.company_id')),
        ('a document filed under the wrong financial year',
         "SELECT COUNT(*) n FROM invoice WHERE fy <> (CASE WHEN "
         "CAST(substr(invoice_date,6,2) AS INT)>=4 THEN substr(invoice_date,1,4)"
         "||'-'||substr(CAST(substr(invoice_date,1,4) AS INT)+1,3,2) ELSE "
         "substr(CAST(substr(invoice_date,1,4) AS INT)-1,1,4)||'-'||"
         "substr(invoice_date,3,2) END)" + _scope(cid)),
        ('a voucher filed under the wrong financial year',
         "SELECT COUNT(*) n FROM voucher WHERE fy <> (CASE WHEN "
         "CAST(substr(voucher_date,6,2) AS INT)>=4 THEN substr(voucher_date,1,4)"
         "||'-'||substr(CAST(substr(voucher_date,1,4) AS INT)+1,3,2) ELSE "
         "substr(CAST(substr(voucher_date,1,4) AS INT)-1,1,4)||'-'||"
         "substr(voucher_date,3,2) END)" + _scope(cid)),
    ):
        n = conn.execute(sql).fetchone()['n']
        if n:
            out.append(dict(what=label, count=n))
    return Check('dates', 'Dates and financial years line up',
                 WARN if out else OK, sum(r['count'] for r in out), 0.0,
                 'Every date reads, and every document is filed under the year '
                 'its date falls in.' if not out else
                 'A document under the wrong year is missing from that '
                 "year's reports and its numbering.", out, grouped=True)


@contextlib.contextmanager
def scratch(conn, name):
    """Changes that are only for asking a question, undone afterwards.

    Inside somebody else's transaction a savepoint is rolled back and let go.
    Outside one the whole transaction is rolled back: releasing the savepoint
    there would commit an empty transaction, which still counts as a change
    to the books and throws away every answer the server had kept.
    """
    outer = conn.in_transaction
    conn.execute('SAVEPOINT %s' % name)
    try:
        yield
    finally:
        if outer:
            conn.execute('ROLLBACK TO %s' % name)
            conn.execute('RELEASE %s' % name)
        else:
            conn.rollback()


def check_numbering(conn, cid=None):
    """The next number a book would hand out must not already be in use."""
    out = []
    fys = [r['fy'] for r in conn.execute(
        "SELECT DISTINCT fy FROM invoice WHERE fy<>'' ORDER BY fy DESC LIMIT 2")]
    # Asking for the next number is what makes a book create its counter, so
    # the question is asked inside a savepoint and rolled back. A screen that
    # only reports must not change what it reports on.
    with scratch(conn, 'verify_numbering'):
        for co in _companies(conn, cid):
            for key, kind in (('SALE', 'SALE'), ('PURCHASE', 'PURCHASE'),
                              ('CREDIT', 'CREDIT'), ('DEBIT', 'DEBIT')):
                for fy in fys:
                    try:
                        nd = db.peek_document(conn, co['id'], key, fy)
                    except Exception as e:      # a series that has run out
                        out.append(dict(company=co['code'], book=key, fy=fy,
                                        number='', problem=str(e)))
                        continue
                    # Two runs of bills can share a number -- GT 5 and R 5 are
                    # different bills -- so the series is part of the question.
                    used = conn.execute(
                        'SELECT COUNT(*) n FROM invoice WHERE company_id=? AND '
                        'doc_type=? AND fy=? AND series=? AND invoice_no=?',
                        (co['id'], kind, fy, nd['series'],
                         nd['number'])).fetchone()['n']
                    if used:
                        out.append(dict(company=co['code'], book=key, fy=fy,
                                        number=nd['number'],
                                        problem='already used by a document'))
    return Check('numbering', 'The next document number is free',
                 BAD if out else OK, len(out), 0.0,
                 'No book would hand out a number it has already used.'
                 if not out else
                 'Two documents would end up sharing a number.', out)


def check_series_continuity(conn, cid=None):
    """A book must keep its spelling when the financial year turns over.

    Numbers restart every April; the way they are written should not. A book
    running plain numbers since SHREE has to go on running plain numbers, and
    a challan book has to keep its letter. Where a book has documents but no
    counter of its own, the company-wide default takes over and the first
    document of the new year comes out spelt INV-0001 instead.
    """
    out = []
    this_fy = conn.execute('SELECT MAX(fy) f FROM invoice').fetchone()['f']
    if not this_fy:
        return Check('series', 'Numbering keeps its shape across the year end',
                     OK, note='No documents yet.')
    start = int(this_fy[:4]) + 1
    next_fy = '%d-%02d' % (start, (start + 1) % 100)

    with scratch(conn, 'verify_series'):
        for co in _companies(conn, cid):
            for key, kind in (('SALE', 'SALE'), ('PURCHASE', 'PURCHASE'),
                              ('CREDIT', 'CREDIT'), ('DEBIT', 'DEBIT')):
                last = conn.execute(
                    'SELECT invoice_no, fy FROM invoice WHERE company_id=? AND '
                    'doc_type=? ORDER BY invoice_date DESC, id DESC LIMIT 1',
                    (co['id'], kind)).fetchone()
                if not last:
                    continue          # a book never used starts where it likes
                try:
                    nxt = db.peek_number(conn, co['id'], key, next_fy)
                except Exception:
                    continue
                was = _spelling(last['invoice_no'])
                now = _spelling(nxt)
                # A challan book moving from A to B is the scheme working, not
                # a break: both are a single letter and a space.
                if was == now or (_is_book_letter(was) and _is_book_letter(now)):
                    continue
                out.append(dict(company=co['code'], book=key,
                                last_used=(last['invoice_no'] or '').strip(),
                                in_year=last['fy'], next_year=next_fy,
                                would_become=nxt))
    return Check('series', 'Numbering keeps its shape across the year end',
                 WARN if out else OK, len(out), 0.0,
                 'Every book carries its own spelling into %s.' % next_fy
                 if not out else
                 'On the first day of %s these books would change shape. '
                 'Set the prefix under Settings -> Document numbering, or run '
                 'app/repair.py to seed each book from the documents already '
                 'filed in it.' % next_fy, out)


def _spelling(number):
    """What comes in front of the digits: '' for 7, 'A ' for A 07, 'INV-'."""
    m = re.match(r'^(.*?)\d+$', (number or '').strip())
    return m.group(1) if m else (number or '').strip()


def _is_book_letter(prefix):
    """'A ', 'G ' -- a challan book, where rolling to the next letter is normal."""
    return bool(re.match(r'^[A-Za-z]\s*$', prefix or ''))


def check_stock(conn, cid=None):
    """Stock that has gone negative means goods went out that never came in."""
    rows = conn.execute(
        'SELECT c.code company, t.name item, t.unit, '
        '  ROUND(t.opening_qty + COALESCE(m.moved, 0), 2) in_hand, '
        '  ROUND(t.opening_qty, 2) opening '
        'FROM item t JOIN company c ON c.id=t.company_id '
        'LEFT JOIN (SELECT ii.item_id, SUM(CASE '
        "     WHEN i.doc_type IN ('PURCHASE','CREDIT') THEN "
        '       (CASE WHEN ii.by_weight=1 THEN ii.weight ELSE ii.qty END) '
        "     WHEN i.doc_type IN ('SALE','DEBIT') THEN "
        '       -(CASE WHEN ii.by_weight=1 THEN ii.weight ELSE ii.qty END) '
        '     ELSE 0 END) moved '
        '   FROM invoice_item ii JOIN invoice i ON i.id=ii.invoice_id '
        '   WHERE ii.item_id IS NOT NULL GROUP BY ii.item_id) m '
        '  ON m.item_id=t.id '
        'WHERE t.track_stock=1 AND t.opening_qty + COALESCE(m.moved,0) < -0.5'
        + _scope(cid, 't.company_id') +
        ' ORDER BY 4 LIMIT 100').fetchall()
    out = [dict(r) for r in rows]
    return Check('stock', 'No item has sold more than it ever held',
                 WARN if out else OK, len(out),
                 sum(abs(r['in_hand']) for r in out),
                 'Every tracked item holds nought or more.' if not out else
                 'Goods left on a bill that were never bought in. Usually a '
                 'year of purchases that was never backed up, rather than an '
                 'error in what is here.', out)


def check_billing_firm(conn, cid=None):
    """A customer with no billing firm cannot be billed from a challan."""
    rows = conn.execute(
        'SELECT c.code company, COUNT(*) n FROM ledger l '
        'JOIN company c ON c.id=l.company_id '
        "WHERE l.party_kind IN ('customer','both') AND l.home_company IS NULL "
        "AND c.tax_mode='none'" + _scope(cid, 'l.company_id') +
        ' GROUP BY 1 ORDER BY 2 DESC').fetchall()
    out = [dict(r) for r in rows]
    return Check('billing_firm', 'Every customer knows who bills them',
                 WARN if out else OK, sum(r['n'] for r in out), 0.0,
                 'Every customer in an order firm has a billing firm.'
                 if not out else
                 'These buy on challan and have never had a tax invoice, so '
                 'the history could not say which firm bills them. Bill It '
                 'asks the first time and remembers the answer; '
                 'The Customer Allocation screen sets them in bulk.', out,
                 grouped=True)


def check_text(conn, cid=None):
    """Text that came out of a FoxPro memo rather than off a keyboard."""
    out = []
    for table, col, co_col in (
            ('posting', 'narration', 'company_id'),
            ('posting', 'doc_no', 'company_id'),
            ('invoice', 'remarks', 'company_id'),
            ('invoice', 'driver_name', 'company_id'),
            ('voucher', 'remarks', 'company_id'),
            ('voucher_line', 'remarks', None),
            ('ledger', 'name', 'company_id'),
            ('ledger', 'address1', 'company_id'),
            ('item', 'name', 'company_id'),
            ('invoice_item', 'description', None)):
        # SQLite picks out the few rows worth a look -- a control character
        # anywhere, or a NUL, which GLOB cannot see -- rather than every one
        # of half a million strings being handed to Python.
        where = ("%s <> '' AND (%s GLOB ? OR instr(%s, char(0)) > 0)"
                 % (col, col, col))
        args = [CONTROL_GLOB]
        if co_col and cid:
            # The unary plus keeps SQLite off the company index: nearly every
            # row qualifies, and reading them in order is quicker than
            # looking each one up.
            where += ' AND +%s=?' % co_col
            args.append(int(cid))
        n = 0
        for r in conn.execute('SELECT %s v FROM %s WHERE %s'
                              % (col, table, where), args):
            if CONTROL.search(r['v'] or ''):
                n += 1
        if n:
            out.append(dict(what='%s.%s' % (table, col), count=n))
    return Check('text', 'Text reads as text',
                 WARN if out else OK, sum(r['count'] for r in out), 0.0,
                 'No field carries bytes that will print as nonsense.'
                 if not out else
                 'A memo field came across as raw bytes. It shows as nonsense '
                 'in the day book and on screen.', out, grouped=True)


def check_charge_share(conn, cid=None):
    """On a GST bill the charges are taxed with the goods, so they sit on the
    lines as well as in the header. Where they never reached the lines, the
    rate-wise and HSN grids in the GST summary come up short by that much."""
    row = conn.execute(
        'SELECT COUNT(*) n, COALESCE(SUM(i.cutting + i.transport + i.loading '
        '  + i.taxable_adj), 0) s '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        "WHERE c.tax_mode='gst' "
        'AND (i.cutting + i.transport + i.loading + i.taxable_adj) <> 0 '
        'AND COALESCE((SELECT SUM(charge_share) FROM invoice_item '
        '              WHERE invoice_id=i.id), 0) = 0'
        + _scope(cid, 'i.company_id')).fetchone()
    rows = conn.execute(
        'SELECT c.code company, i.doc_type, i.series, i.invoice_no, '
        '  i.invoice_date, i.id, ROUND(i.cutting + i.transport + i.loading '
        '  + i.taxable_adj, 2) charges '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        "WHERE c.tax_mode='gst' "
        'AND (i.cutting + i.transport + i.loading + i.taxable_adj) <> 0 '
        'AND COALESCE((SELECT SUM(charge_share) FROM invoice_item '
        '              WHERE invoice_id=i.id), 0) = 0'
        + _scope(cid, 'i.company_id') +
        ' ORDER BY 7 DESC LIMIT 100').fetchall()
    return Check('charge_share', 'Charges sit on the lines they are taxed with',
                 WARN if row['n'] else OK, row['n'], row['s'],
                 'Every charge is spread over the lines it is taxed with.'
                 if not row['n'] else
                 'The header counts these charges in the taxable value and the '
                 'lines do not, so the rate-wise and HSN grids in the GST '
                 'summary are short by this much. Running app/repair.py '
                 'spreads them the way the app does.', [dict(r) for r in rows])


CHECKS = [check_trial_balance, check_document_postings, check_missing_postings,
          check_totals_add_up, check_lines_match_header, check_line_signs,
          check_charge_share, check_references, check_dates, check_numbering,
          check_series_continuity, check_round_off, check_hidden_tax,
          check_stock, check_billing_firm, check_text]

# The checks that can come back BAD; the rest only ever warn. The SHREE update
# keeps nothing but the BAD findings (shree_sync.census), so it asks only these
# and is spared the slow text and date scans -- about 1.4 s a census. A check
# that starts returning BAD must be added here, in CHECKS order.
SERIOUS = [fn for fn in CHECKS if fn in (
    check_trial_balance, check_document_postings, check_totals_add_up,
    check_lines_match_header, check_line_signs, check_references,
    check_numbering)]


def run(conn, cid=None, serious_only=False):
    """Every check, worst first -- or, serious_only, the ones that can be
    BAD."""
    out = []
    for fn in (SERIOUS if serious_only else CHECKS):
        try:
            out.append(fn(conn, cid))
        except Exception as e:                  # a check must never take the
            out.append(Check(fn.__name__, fn.__name__, WARN, 0, 0.0,   # page
                             'This check could not run: %s' % e))      # down
    rank = {BAD: 0, WARN: 1, OK: 2}
    out.sort(key=lambda c: (rank[c.level], -c.count))
    return out


# ------------------------------------------------------------- command line

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='Check the books against themselves.')
    ap.add_argument('--company', help='one firm, by code')
    ap.add_argument('--detail', action='store_true',
                    help='list the documents behind each finding')
    ap.add_argument('--limit', type=int, default=10,
                    help='how many to list with --detail (default 10)')
    a = ap.parse_args(argv)

    conn = db.connect()
    cid = None
    if a.company:
        row = conn.execute('SELECT id, code FROM company WHERE code=?',
                           (a.company.upper(),)).fetchone()
        if not row:
            raise SystemExit('No firm with code %s.' % a.company)
        cid = row['id']

    mark = {BAD: '!!', WARN: ' ?', OK: 'ok'}
    checks = run(conn, cid)
    bad = sum(1 for c in checks if c.level == BAD)
    warn = sum(1 for c in checks if c.level == WARN)

    print('')
    print('  Data health%s' % (' -- %s' % a.company.upper() if a.company else ''))
    print('  ' + '-' * 74)
    for c in checks:
        head = '  %s  %-52s' % (mark[c.level], c.title)
        if c.level == OK:
            print(head)
            continue
        tail = '%6d' % c.count
        if c.amount:
            tail += '   %16s' % ('{:,.2f}'.format(c.amount))
        print(head + tail)
        for line in _wrap(c.note, 68):
            print('        ' + line)
        if a.detail and c.rows:
            for r in c.rows[:a.limit]:
                print('        - ' + '  '.join(
                    '%s=%s' % (k, v) for k, v in r.items()
                    if k != 'id' and v not in ('', None)))
            if len(c.rows) > a.limit:
                print('        - ... and %d more' % (len(c.rows) - a.limit))
        print('')
    print('  ' + '-' * 74)
    print('  %d to put right, %d to be aware of, %d clean'
          % (bad, warn, len(checks) - bad - warn))
    print('')
    return 1 if bad else 0


def _wrap(text, width):
    out, line = [], ''
    for word in (text or '').split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = (line + ' ' + word).strip()
    if line:
        out.append(line)
    return out


if __name__ == '__main__':
    raise SystemExit(main())
