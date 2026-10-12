"""Put right what the SHREE import could not read correctly.

Each repair below was found by `verify.py`, and each one is a case where the
ledgers are already right -- they came from the voucher file -- and it is the
document beside them that reads wrong. Nothing here touches a posting, so no
repair can move a balance.

It writes nothing until asked:

    python3 app/repair.py                  # say what would change
    python3 app/repair.py --apply          # change it
    python3 app/repair.py --apply --only returns

Running it again finds nothing left to do.
"""
import re
from collections import defaultdict

import db

CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

# Columns on a line that carry a value and so carry a sign.
SIGNED = ('qty', 'weight', 'incl_price', 'taxable', 'charge_share',
          'cgst', 'sgst', 'igst', 'cess', 'line_total')


def r2(x):
    x = float(x or 0)
    return round(x + (1e-9 if x >= 0 else -1e-9), 2)


# ------------------------------------------------------------------ returns

def _tax_after_discount(conn, r, flipped):
    """The tax put back on a return that carries a discount, worked out as
    SHREE does it: on the value after the discount, CGST and SGST each the
    half rate on each rate's value (gst.tax_on), then shared over the lines.
    Worked off each line's own value it came to 8,521.65 on BSI's debit note
    GT/162 of 1 Sep 2026, where SHREE charged 8,308.90."""
    import api
    calc = api.gstcalc
    worth = sum(new['taxable'] for _lid, new in flipped)
    after = worth - float(r['discount_amt'] or 0)
    factor = after / worth if worth else 1.0
    igst = _is_igst(conn, r['id'])
    rate_of = {row['id']: (float(row['gst_rate'] or 0),
                           float(row['cess_rate'] or 0))
               for row in conn.execute('SELECT id, gst_rate, cess_rate FROM '
                                       'invoice_item WHERE invoice_id=?',
                                       (r['id'],))}
    buckets = defaultdict(list)
    for lid, new in flipped:
        buckets[rate_of.get(lid, (0.0, 0.0))].append(new)
    tax = dict(cgst=0.0, sgst=0.0, igst=0.0, cess=0.0)
    for (rate, cess_rate), group in buckets.items():
        weights = [new['taxable'] for new in group]
        base = calc.money(sum(weights) * factor)
        parts = calc.tax_on(base, rate, cess_rate, igst)
        for k, amount in zip(('cgst', 'sgst', 'igst', 'cess'), parts):
            for new, share in zip(group, calc.spread(amount, weights)):
                new[k] = share
            tax[k] += amount
    for _lid, new in flipped:
        new['line_total'] = r2(new['taxable'] + new['charge_share'] + new['cgst']
                               + new['sgst'] + new['igst'] + new['cess'])
    return tax


def fix_returns(conn, apply=False):
    """Make a sales return negative on its lines as well as its total.

    SHREE files a return as a bill with a negative total but positive line
    amounts, and records the tax on those lines as a positive figure too. The
    importer, finding a document that would not add up with the tax in it,
    dropped the tax and left the difference in Round Off -- so the tax vanished
    from the register while staying in the ledgers, and every report that adds
    up line values counted the return as a sale.

    The total, the party's balance and the postings are already right and are
    left exactly as they are. Only the lines are turned round -- weight with
    them, so the kilos on a return come off the register rather than adding
    to it -- and the tax they
    carry is put back on the document with Round Off reduced to match, so the
    document still comes to the same grand total.

    The import has already set each item's opening stock so the stock comes
    to SHREE's own closing figure, counting these lines as they came in.
    Turning a line round moves the stock by twice its kilos, so the opening
    of an imported document's item is moved back by as much -- the stock
    still ends on SHREE's figure, now with the return counted as goods back.
    """
    import api
    done, notes = 0, []
    reopen = {}                     # item -> added back to its opening
    rows = conn.execute(
        'SELECT i.id, i.company_id, i.doc_type, i.series, i.invoice_no, '
        '  i.invoice_date, i.taxable, i.cutting, i.transport, i.loading, '
        '  i.round_off, i.grand_total, i.source, i.discount_amt, c.code, '
        '  c.tax_mode '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        'JOIN (SELECT invoice_id, SUM(taxable) v FROM invoice_item '
        '      GROUP BY invoice_id) x ON x.invoice_id=i.id '
        'WHERE (i.grand_total < -0.005 AND x.v > 0.005) '
        '   OR (i.grand_total > 0.005 AND x.v < -0.005) '
        'ORDER BY i.company_id, i.invoice_date').fetchall()

    for r in rows:
        lines = conn.execute(
            'SELECT * FROM invoice_item WHERE invoice_id=? ORDER BY id',
            (r['id'],)).fetchall()
        taxed = r['tax_mode'] != 'none'
        tax = dict(cgst=0.0, sgst=0.0, igst=0.0, cess=0.0)
        flipped = []
        for ln in lines:
            new = {c: r2(-float(ln[c] or 0)) for c in SIGNED}
            if taxed and not (new['cgst'] or new['sgst'] or new['igst']):
                # The tax was dropped on the way in. Put back what this line's
                # own rate comes to, on the side the document is now on.
                amt = r2(new['taxable'] * float(ln['gst_rate'] or 0) / 100.0)
                if r['doc_type'] in ('SALE', 'CREDIT') and ln['igst']:
                    new['igst'] = amt
                elif _is_igst(conn, r['id']):
                    new['igst'] = amt
                else:
                    half = r2(amt / 2.0)
                    new['cgst'], new['sgst'] = half, r2(amt - half)
                new['cess'] = r2(new['taxable'] * float(ln['cess_rate'] or 0)
                                 / 100.0)
                new['line_total'] = r2(new['taxable'] + new['charge_share']
                                       + new['cgst'] + new['sgst']
                                       + new['igst'] + new['cess'])
            for k in tax:
                tax[k] += new[k]
            flipped.append((ln['id'], new))
        if taxed and abs(float(r['discount_amt'] or 0)) > 0.005 and \
                not any(float(ln[c] or 0) for ln in lines
                        for c in ('cgst', 'sgst', 'igst')):
            tax = _tax_after_discount(conn, r, flipped)

        for k in tax:
            tax[k] = r2(tax[k])
        tax_total = r2(sum(tax.values()))
        charges = r2(r['cutting'] + r['transport'] + r['loading'])
        base = r2(r['taxable'] + tax_total + (charges if not taxed else 0))
        round_off = r2(r['grand_total'] - base)

        notes.append(dict(
            company=r['code'], doc=('%s %s' % (r['series'], r['invoice_no'])).strip(),
            date=r['invoice_date'], grand_total=r2(r['grand_total']),
            tax_restored=tax_total,
            round_off='%s -> %s' % (r2(r['round_off']), round_off)))

        if apply:
            for lid, new in flipped:
                conn.execute(
                    'UPDATE invoice_item SET ' +
                    ', '.join('%s=?' % c for c in SIGNED) + ' WHERE id=?',
                    [new[c] for c in SIGNED] + [lid])
            conn.execute(
                'UPDATE invoice SET cgst=?, sgst=?, igst=?, cess=?, '
                'round_off=?, weight_total=-weight_total WHERE id=?',
                (tax['cgst'], tax['sgst'], tax['igst'], tax['cess'],
                 round_off, r['id']))
            if r['source'] == 'SHREE':
                sign = api.DOC_STOCK.get(r['doc_type'], 0)
                for ln in lines:
                    if ln['item_id']:
                        q = float(ln['weight'] or 0) or float(ln['qty'] or 0)
                        reopen[ln['item_id']] = \
                            reopen.get(ln['item_id'], 0.0) + 2 * sign * q
        done += 1
    for iid, q in reopen.items():
        if abs(q) > 1e-9:
            conn.execute('UPDATE item SET opening_qty=ROUND(opening_qty+?, 3) '
                         'WHERE id=?', (q, iid))
    return done, notes


def _is_igst(conn, iid):
    row = conn.execute('SELECT is_igst FROM invoice WHERE id=?', (iid,)).fetchone()
    return bool(row and row['is_igst'])


# --------------------------------------------------------------- memo bytes

def fix_text(conn, apply=False):
    """Drop the raw bytes a FoxPro memo field brought across.

    A narration that is not text prints as nonsense in the day book. Where
    stripping the unprintable bytes leaves nothing that reads as words, the
    field is cleared rather than left as a shorter piece of nonsense.
    """
    done, notes = 0, []
    for table, col in (('posting', 'narration'), ('posting', 'doc_no'),
                       ('invoice', 'remarks'), ('invoice', 'driver_name'),
                       ('voucher', 'remarks'), ('voucher_line', 'remarks'),
                       ('ledger', 'name'), ('ledger', 'address1'),
                       ('item', 'name'), ('invoice_item', 'description')):
        for row in conn.execute(
                "SELECT id, %s v FROM %s WHERE %s IS NOT NULL AND %s<>''"
                % (col, table, col, col)):
            if not CONTROL.search(row['v'] or ''):
                continue
            clean = CONTROL.sub(' ', row['v'])
            clean = re.sub(r'\s{2,}', ' ', clean).strip()
            # What is left has to look like something a person typed.
            letters = sum(c.isalnum() for c in clean)
            if letters < 3 or letters < len(clean) / 3.0:
                clean = ''
            notes.append(dict(table='%s.%s' % (table, col), id=row['id'],
                              was=repr(row['v'])[:48], now=repr(clean)))
            if apply:
                conn.execute('UPDATE %s SET %s=? WHERE id=?' % (table, col),
                             (clean, row['id']))
            done += 1
    return done, notes


# ---------------------------------------------------------------- numbering

def fix_counters(conn, apply=False):
    """Give every book that has documents a counter that matches them.

    A book with no counter row falls back to the company-wide default, so the
    first bill raised in it would be spelt INV-0001 where every bill before it
    was a plain number. Reading the prefix and the width off the documents
    already filed keeps the run going.
    """
    done, notes = 0, []
    for key, doc_type in (('SALE', 'SALE'), ('PURCHASE', 'PURCHASE'),
                          ('CREDIT', 'CREDIT'), ('DEBIT', 'DEBIT')):
        rows = conn.execute(
            'SELECT i.company_id, c.code, i.fy, COUNT(*) n '
            'FROM invoice i JOIN company c ON c.id=i.company_id '
            "WHERE i.doc_type=? AND i.fy<>'' "
            'GROUP BY i.company_id, i.fy', (doc_type,)).fetchall()
        for r in rows:
            have = conn.execute(
                'SELECT 1 FROM counter WHERE company_id=? AND key=? AND fy=?',
                (r['company_id'], key, r['fy'])).fetchone()
            if have:
                continue
            # The prefix is whatever sits in front of the digits on the
            # commonest spelling in that book and year.
            spellings = {}
            for d in conn.execute(
                    'SELECT invoice_no FROM invoice WHERE company_id=? AND '
                    'doc_type=? AND fy=?',
                    (r['company_id'], doc_type, r['fy'])):
                m = re.match(r'^(.*?)(\d+)$', (d['invoice_no'] or '').strip())
                if m:
                    spellings.setdefault(m.group(1), []).append(m.group(2))
            if not spellings:
                continue
            prefix = max(spellings, key=lambda p: len(spellings[p]))
            digits = spellings[prefix]
            # A run of numbers written 0001, 0002 is four wide; 1, 2, 3 is not
            # padded at all.
            pad = len(digits[0]) if all(
                len(d) == len(digits[0]) and d[0] == '0' for d in digits) else 0
            nxt = max(int(d) for d in digits) + 1
            notes.append(dict(company=r['code'], book=key, fy=r['fy'],
                              prefix=prefix, pad=pad, next_no=nxt,
                              documents=r['n']))
            if apply:
                conn.execute(
                    'INSERT INTO counter(company_id, key, fy, prefix, next_no,'
                    ' pad) VALUES (?,?,?,?,?,?)',
                    (r['company_id'], key, r['fy'], prefix, nxt, pad))
            done += 1
    return done, notes


# ------------------------------------------------------- totals left blank

def fix_blank_totals(conn, apply=False):
    """Fill in a document total the old file left at zero.

    A handful of challans came across with the header total blank while the
    lines beneath them were complete. Only where the lines plus the charges
    come to the grand total exactly is the header rewritten -- that proves the
    lines are the whole document. Anything that does not prove itself is left
    for `verify.py` to report.
    """
    done, notes = 0, []
    rows = conn.execute(
        'SELECT i.id, i.company_id, i.doc_type, i.series, i.invoice_no, '
        '  i.invoice_date, i.discount_amt, i.cutting, i.transport, i.loading, '
        '  i.taxable_adj, i.cgst, i.sgst, i.igst, i.cess, i.round_off, '
        '  i.grand_total, c.code, c.tax_mode, ROUND(x.v, 2) lines_total, '
        '  ROUND(x.w, 2) lines_weight '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        'JOIN (SELECT invoice_id, SUM(taxable) v, SUM(weight) w '
        '      FROM invoice_item GROUP BY invoice_id) x ON x.invoice_id=i.id '
        'WHERE ABS(i.subtotal) < 0.005 AND ABS(x.v) > 0.005').fetchall()
    for r in rows:
        charges = r2(r['cutting'] + r['transport'] + r['loading'])
        tax = r2(r['cgst'] + r['sgst'] + r['igst'] + r['cess'])
        goods = r2(r['lines_total'] - r['discount_amt'])
        if r['tax_mode'] == 'none':
            taxable = goods
            total = r2(taxable + tax + charges)
        else:
            taxable = r2(goods + charges + r['taxable_adj'])
            total = r2(taxable + tax)
        # The lines have to account for the whole document, to the rupee, or
        # this is not simply a blank box.
        if abs(r2(r['grand_total'] - total)) > 1.0:
            continue
        notes.append(dict(
            company=r['code'], doc=('%s %s' % (r['series'], r['invoice_no'])).strip(),
            date=r['invoice_date'], subtotal='0 -> %s' % r['lines_total'],
            round_off='%s -> %s' % (r2(r['round_off']),
                                    r2(r['grand_total'] - total))))
        if apply:
            conn.execute(
                'UPDATE invoice SET subtotal=?, taxable=?, weight_total=?, '
                'round_off=? WHERE id=?',
                (r['lines_total'], taxable, r['lines_weight'],
                 r2(r['grand_total'] - total), r['id']))
        done += 1
    return done, notes


# ------------------------------------------------- tax filed as a round off

def fix_uncaptured_tax(conn, apply=False):
    """Move tax out of Round Off and into the tax columns where it belongs.

    A handful of bills came across with nothing in their tax boxes even though
    every line carries a rate. The total was right, because the tax had gone
    into Round Off, and the document added up -- but the tax was invisible to
    the GST summary, and opening the bill to change a line would have charged
    it twice.

    Only where the figure sitting in Round Off is what the lines' own rates
    come to, within a rupee, is it moved. That is the proof it is tax; the
    rupee is the paisa rounding SHREE did its own way. Anything else stays put
    for `verify.py` to report.
    """
    done, notes = 0, []
    rows = conn.execute(
        'SELECT i.id, i.doc_type, i.series, i.invoice_no, i.invoice_date, '
        '  i.is_igst, i.taxable, i.round_off, i.grand_total, c.code '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        "WHERE c.tax_mode='gst' AND ABS(i.round_off) > 1 "
        'AND ABS(i.cgst) + ABS(i.sgst) + ABS(i.igst) + ABS(i.cess) < 0.005'
    ).fetchall()
    for r in rows:
        lines = conn.execute(
            'SELECT * FROM invoice_item WHERE invoice_id=? ORDER BY id',
            (r['id'],)).fetchall()
        if not lines:
            continue
        # Tax is worked out on each rate's own total and then split back over
        # that rate's lines -- the same way the app does it, so re-opening the
        # bill shows what was stored.
        buckets = {}
        for ln in lines:
            key = (ln['gst_rate'], ln['cess_rate'])
            buckets.setdefault(key, []).append(ln)
        share, total = {}, 0.0
        for (rate, cess_rate), group in buckets.items():
            base = r2(sum(ln['taxable'] + ln['charge_share'] for ln in group))
            tax = r2(base * rate / 100.0)
            cess = r2(base * cess_rate / 100.0)
            total += tax + cess
            done_tax = done_cess = done_cg = done_sg = 0.0
            half = r2(tax / 2.0)
            for i, ln in enumerate(group):
                last = i == len(group) - 1
                part = ln['taxable'] + ln['charge_share']
                t = r2(tax - done_tax) if last else (
                    r2(tax * part / base) if base else 0.0)
                ce = r2(cess - done_cess) if last else (
                    r2(cess * part / base) if base else 0.0)
                done_tax += t
                done_cess += ce
                if r['is_igst']:
                    share[ln['id']] = (0.0, 0.0, t, ce)
                else:
                    if last:
                        cg, sg = r2(half - done_cg), r2(tax - half - done_sg)
                    else:
                        cg = r2(t / 2.0)
                        sg = r2(t - cg)
                    done_cg += cg
                    done_sg += sg
                    share[ln['id']] = (cg, sg, 0.0, ce)
        total = r2(total)
        if not total or abs(r2(r['round_off'] - total)) > 1.0:
            continue                    # not tax, or not all of it -- leave it
        head = [0.0, 0.0, 0.0, 0.0]
        for cg, sg, ig, ce in share.values():
            head[0] += cg
            head[1] += sg
            head[2] += ig
            head[3] += ce
        head = [r2(v) for v in head]
        left = r2(r['round_off'] - total)
        notes.append(dict(
            company=r['code'], doc=('%s %s' % (r['series'], r['invoice_no'])).strip(),
            date=r['invoice_date'], tax_recovered=total,
            round_off='%s -> %s' % (r2(r['round_off']), left)))
        if apply:
            for lid, (cg, sg, ig, ce) in share.items():
                conn.execute(
                    'UPDATE invoice_item SET cgst=?, sgst=?, igst=?, cess=?, '
                    'line_total=taxable+charge_share+?+?+?+? WHERE id=?',
                    (cg, sg, ig, ce, cg, sg, ig, ce, lid))
            conn.execute(
                'UPDATE invoice SET cgst=?, sgst=?, igst=?, cess=?, round_off=? '
                'WHERE id=?', tuple(head) + (left, r['id']))
        done += 1
    return done, notes


# --------------------------------------------------------------- phantom cess

def fix_phantom_cess(conn, apply=False):
    """Take out a cess that was never charged.

    On a few dozen documents the importer read the CGST and SGST boxes a
    second time into the cess column, and the total still came out right
    because a negative round off of the same size cancelled it. The ledgers
    never saw it -- there is no cess posting anywhere against these documents
    -- but the GST summary counted it, and the entry screen showed a Cess line
    beside a round off of minus a lakh.

    A document qualifies only where nothing was posted to a cess account and
    the round off cancels the cess to within a rupee. That is the proof it was
    never charged: take it out and the round off becomes a rounding again.
    """
    done, notes = 0, []
    rows = conn.execute(
        'SELECT i.id, i.doc_type, i.series, i.invoice_no, i.invoice_date, '
        '  i.cess, i.round_off, i.grand_total, c.code '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        'WHERE ABS(i.cess) > 0.005').fetchall()
    for r in rows:
        if abs(r2(r['round_off'] + r['cess'])) > 1.0:
            continue                    # the round off does not cancel it
        posted = conn.execute(
            "SELECT COALESCE(SUM(ABS(p.debit - p.credit)), 0) c FROM posting p "
            'JOIN ledger l ON l.id=p.ledger_id WHERE p.doc_kind=? AND '
            "p.doc_id=? AND l.name LIKE '%CESS%'",
            (r['doc_type'], r['id'])).fetchone()['c']
        if posted > 0.005:
            continue                    # a cess really was charged
        left = r2(r['round_off'] + r['cess'])
        notes.append(dict(
            company=r['code'], doc=('%s %s' % (r['series'], r['invoice_no'])).strip(),
            date=r['invoice_date'], cess_removed=r2(r['cess']),
            round_off='%s -> %s' % (r2(r['round_off']), left)))
        if apply:
            conn.execute(
                'UPDATE invoice_item SET cess=0, cess_rate=0, '
                'line_total=taxable+charge_share+cgst+sgst+igst '
                'WHERE invoice_id=?', (r['id'],))
            conn.execute('UPDATE invoice SET cess=0, round_off=? WHERE id=?',
                         (left, r['id']))
        done += 1

    # A rate with nothing charged at it is the same mistake, without the
    # arithmetic to prove it -- the GST rate copied into the cess column.
    stale = conn.execute(
        'SELECT COUNT(*) n FROM invoice_item WHERE cess_rate > 0 AND '
        'ABS(cess) < 0.005 AND cess_rate = gst_rate').fetchone()['n']
    if stale:
        notes.append(dict(company='-', doc='%d lines' % stale, date='',
                          cess_removed=0.0,
                          round_off='cess rate copied from the GST rate'))
        if apply:
            conn.execute('UPDATE invoice_item SET cess_rate=0 WHERE '
                         'cess_rate > 0 AND ABS(cess) < 0.005 AND '
                         'cess_rate = gst_rate')
        done += stale
    return done, notes


# --------------------------------------------------------- charges on lines

def fix_charge_share(conn, apply=False):
    """Spread a bill's cutting, transport and loading over its own lines.

    On a GST bill the charges are part of the value of the supply, so they sit
    inside the taxable value and each line carries a share of them. The app
    does that when a bill is saved; the importer never did, so on these bills
    the header's taxable value counts the charges and the lines do not. The
    document adds up either way, but the rate-wise and HSN grids in the GST
    summary are built from the lines, so they came up short.

    The header is not touched -- it is already right. The shares are worked out
    the way the app works them out: in proportion to each line's own value,
    with the last line taking whatever is left, so they add back to the exact
    figure the header carries.
    """
    done, notes = 0, []
    rows = conn.execute(
        'SELECT i.id, i.doc_type, i.series, i.invoice_no, i.invoice_date, '
        '  i.cutting, i.transport, i.loading, i.taxable_adj, c.code '
        'FROM invoice i JOIN company c ON c.id=i.company_id '
        "WHERE c.tax_mode='gst' "
        'AND (i.cutting + i.transport + i.loading + i.taxable_adj) <> 0 '
        'AND COALESCE((SELECT SUM(charge_share) FROM invoice_item '
        '              WHERE invoice_id=i.id), 0) = 0').fetchall()
    for r in rows:
        spread = r2(r['cutting'] + r['transport'] + r['loading']
                    + r['taxable_adj'])
        lines = conn.execute(
            'SELECT id, taxable, cgst, sgst, igst, cess FROM invoice_item '
            'WHERE invoice_id=? ORDER BY id', (r['id'],)).fetchall()
        goods = r2(sum(ln['taxable'] for ln in lines))
        if not spread or not lines or not goods:
            continue
        spent, shares = 0.0, []
        for i, ln in enumerate(lines):
            last = i == len(lines) - 1
            share = r2(spread - spent) if last else r2(spread * ln['taxable']
                                                      / goods)
            spent += share
            shares.append((ln, share))
        notes.append(dict(
            company=r['code'], doc=('%s %s' % (r['series'], r['invoice_no'])).strip(),
            date=r['invoice_date'], charges=spread, over_lines=len(lines)))
        if apply:
            for ln, share in shares:
                conn.execute(
                    'UPDATE invoice_item SET charge_share=?, '
                    'line_total=taxable+?+cgst+sgst+igst+cess WHERE id=?',
                    (share, share, ln['id']))
        done += 1
    return done, notes


# ------------------------------------------------- lines pointing elsewhere

def fix_cross_firm_items(conn, apply=False):
    """Point a line at its own firm's copy of the product.

    Every firm keeps its own item list, and a line must name the item in the
    book its document is filed in -- otherwise the sale takes stock out of a
    firm that never held it, and `verify.py` reads the reference as dangling.
    Where the same product exists by name in the document's own firm the link
    is moved to it; where it does not, the line is left alone and said so,
    because inventing an item is not a repair.

    Only the link moves. No amount, no posting and no balance is touched.
    """
    done, notes = 0, []
    for r in conn.execute(
            'SELECT x.id, x.item_id, t.name, i.company_id AS to_co, '
            '       i.series, i.invoice_no, i.invoice_date, c.code AS to_code '
            'FROM invoice_item x JOIN invoice i ON i.id=x.invoice_id '
            'JOIN item t ON t.id=x.item_id JOIN company c ON c.id=i.company_id '
            'WHERE t.company_id<>i.company_id').fetchall():
        mine = conn.execute(
            'SELECT id FROM item WHERE company_id=? AND name=? ORDER BY id '
            'LIMIT 1', (r['to_co'], r['name'])).fetchone()
        doc = ('%s %s' % (r['series'] or '', r['invoice_no'] or '')).strip()
        if not mine:
            notes.append(dict(company=r['to_code'], doc=doc, item=r['name'],
                              note='no item of that name here -- left alone'))
            continue
        notes.append(dict(company=r['to_code'], doc=doc, date=r['invoice_date'],
                          item=r['name'],
                          item_id='%d -> %d' % (r['item_id'], mine['id'])))
        if apply:
            conn.execute('UPDATE invoice_item SET item_id=? WHERE id=?',
                         (mine['id'], r['id']))
        done += 1
    if apply and done:
        conn.commit()
    return done, notes


REPAIRS = [
    ('returns', 'Sales returns reading as sales', fix_returns),
    ('totals', 'Document totals the old file left blank', fix_blank_totals),
    # Cess first: a cess that was never charged carries a rate with it, and
    # that rate would otherwise be counted when the tax below is worked out.
    ('cess', 'Cess that was never charged', fix_phantom_cess),
    ('tax', 'Tax that was filed as a round off', fix_uncaptured_tax),
    ('charges', 'Charges that never reached the lines', fix_charge_share),
    ('counters', 'Books with no counter of their own', fix_counters),
    ('text', 'Memo fields that came across as raw bytes', fix_text),
    ('items', "Lines naming another firm's item", fix_cross_firm_items),
]


def run(conn, apply=False, only=None):
    out = []
    for key, title, fn in REPAIRS:
        if only and key not in only:
            continue
        n, notes = fn(conn, apply)
        out.append(dict(key=key, title=title, count=n, rows=notes))
    if apply:
        conn.commit()
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--apply', action='store_true', help='write the changes')
    ap.add_argument('--only', action='append',
                    choices=[k for k, _t, _f in REPAIRS],
                    help='one repair only; repeatable')
    ap.add_argument('--limit', type=int, default=8,
                    help='how many examples to list (default 8)')
    a = ap.parse_args(argv)

    conn = db.connect()
    out = run(conn, a.apply, a.only)
    print('')
    print('  Repairs%s' % ('' if a.apply else ' -- nothing written, add --apply'))
    print('  ' + '-' * 74)
    total = 0
    for res in out:
        total += res['count']
        print('  %-52s %6d' % (res['title'], res['count']))
        for r in res['rows'][:a.limit]:
            print('        - ' + '  '.join('%s=%s' % (k, v)
                                           for k, v in r.items()))
        if len(res['rows']) > a.limit:
            print('        - ... and %d more' % (len(res['rows']) - a.limit))
        print('')
    print('  ' + '-' * 74)
    print('  %d %s' % (total, 'changed' if a.apply else 'would change'))
    print('')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
