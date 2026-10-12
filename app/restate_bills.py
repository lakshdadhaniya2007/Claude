#!/usr/bin/env python3
"""Show each imported bill as its challan plus GST: the challan's rates, and
its cutting, transport and loading on the bill beside them.

    python3 app/restate_bills.py              # what it would do
    python3 app/restate_bills.py --apply
    python3 app/restate_bills.py --csv /tmp/left.csv   # the bills it left

Asked for on 19 Sep 2026: *all the bills fetched from SHREE have no cutting,
loading or transport, and the rate is different on the challan and the bill.
Pass the charges from the challan to the bill, then 18% tax should give the
amount on the bill -- and it must match the amount the bill has now.*

SHREE never carried a challan's charges onto its bill. It worked the bill's
rate out as the challan's total over the kilos -- 715 kg for Rs 52,500 billed
at 73.43 -- and often wrote the challan's lines as one. The bill came to the
right money; it just did not say how. A bill made in this app from a challan
repeats the challan's lines and rates, lists its charges, and carries the
challan's round off as `taxable_adj`, all spread over the lines as
`charge_share` and taxed with them. This writes each imported bill that way.

**Nothing the bill is worth moves.** Its taxable value, its CGST, SGST, IGST
and cess, its round off and its grand total stay exactly as SHREE filed them,
to the paisa; so do its postings (they are SHREE's own) and, per HSN, the
taxable value the GSTR-1 reads off it. What SHREE's rate rounding left over
-- a few paise, now and then a few rupees on a heavy load -- goes into
`taxable_adj` with the challan's own round off. Only the lines, the three
charges and that adjustment change.

A bill is rewritten only when its challans account for it exactly:

- it was paired with its challan(s) by link_challans.py -- one challan, or
  several billed together on this one bill (not a challan billed in parts);
- the challans' total comes to the bill's taxable value within twenty rupees
  -- a bill that left the charges off (D PATEL SALES G 70: Rs 50,765 of goods,
  Rs 1,735 of charges, billed Rs 50,765) or was re-priced cannot be both the
  challan and its own amount, and is left as SHREE wrote it;
- every challan line finds the bill's line for the same goods, and the kilos
  of each come to what the bill has -- so stock does not move;
- the bill carries one tax rate per HSN, so each line's rate is the bill's;
- the challan carries no tax of its own, and its lines, charges and round
  off come to its total -- so what the bill shows as the challan's round off
  is the challan's round off (K 55 carried Rs 7,806.60 of GST and a balancing
  -3,126.60; its bill GT 566 is left).

Every import rebuilds the bills from SHREE's files, so this runs again after
each update (shree_sync.py, after the pairing), and it rewrites the same bills
the same way each time. A bill edited in this app is the app's own and left.
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api   # noqa: E402
import db    # noqa: E402

AMOUNT_TOL = 20.0       # the challans' total against the bill's taxable value
# The kilos must be the bill's own, to the gram. Half a kilo was let
# through until 2 Oct 2026, and nine bills then showed their challan's
# 118.5 kg where SHREE's bill -- the one GST returns and e-way bills report
# -- says 118 kg; those stay as SHREE wrote them now.
WEIGHT_TOL = 0.0015


def r2(v):
    return round(float(v or 0) + 1e-9, 2)


def r3(v):
    return round(float(v or 0) + 1e-9, 3)


def _goods_name(text):
    """A line's goods without the HSN SHREE writes into the bill's name:
    'CRCA 72091520' and 'M S SCRAP (72044900)' are CRCA and M S SCRAP."""
    t = re.sub(r'[^A-Z0-9 ]', ' ', (text or '').upper())
    return ' '.join(w for w in t.split() if not (w.isdigit() and len(w) >= 4))


def _load(conn):
    """Each imported bill with the challans it came from, when those are all
    it came from: [(bill, [challans])]."""
    pairs = defaultdict(set)
    for r in conn.execute(
            'SELECT b.id bill, c.id challan FROM invoice b '
            'JOIN invoice c ON c.id=b.from_challan '
            "WHERE b.source='SHREE' AND c.source='SHREE'"):
        pairs[r['bill']].add(r['challan'])
    for r in conn.execute(
            'SELECT c.billed_invoice bill, c.id challan FROM invoice c '
            'JOIN invoice b ON b.id=c.billed_invoice '
            "WHERE b.source='SHREE' AND c.source='SHREE'"):
        pairs[r['bill']].add(r['challan'])
    # A challan billed in parts is on more than one bill; its charges cannot
    # be put on any one of them.
    parts = {r['challan_id'] for r in conn.execute(
        'SELECT challan_id FROM challan_bill GROUP BY challan_id '
        'HAVING COUNT(*) > 1')} if _has(conn, 'challan_bill') else set()
    out = []
    for bill, chs in sorted(pairs.items()):
        out.append((bill, sorted(chs), bool(parts & chs)))
    return out


def _has(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                        'name=?', (table,)).fetchone() is not None


def _doc(conn, iid):
    d = dict(conn.execute('SELECT * FROM invoice WHERE id=?', (iid,)).fetchone())
    d['lines'] = [dict(r) for r in conn.execute(
        'SELECT * FROM invoice_item WHERE invoice_id=? ORDER BY id', (iid,))]
    return d


def _spread(total, weights):
    """`total` split over `weights` in proportion, the last taking what the
    rounding leaves, so the parts add up to it exactly."""
    out, done = [], 0.0
    base = sum(weights)
    for i, w in enumerate(weights):
        if i == len(weights) - 1:
            out.append(r2(total - done))
        else:
            part = r2(total * w / base) if base else 0.0
            out.append(part)
            done += part
    return out


def _empty(ln):
    """A line with no kilos, no pieces and no money."""
    return not any(float(ln[k] or 0) for k in ('weight', 'qty', 'taxable',
                                                'charge_share'))


def _assign(ch_lines, groups):
    """Put each challan line under the bill's line for the same goods.

    One kind of goods on the bill takes them all. With more than one, a
    challan line goes where its name points when it points at one of them;
    SHREE's bill often splits the same sheet by HSN (M S SHEET 7208 / 7209)
    where the challan says CRCA throughout, so otherwise it is the kilos that
    decide: the lines are shared out so each kind of goods gets exactly the
    kilos the bill gives it. Of several ways to do that, the one whose goods
    come nearest what the bill says each kind is worth. False if none fits.
    """
    if len(groups) == 1:
        groups[0]['lines'] = list(ch_lines)
        return True
    allowed = []
    for ln in ch_lines:
        name = _goods_name(ln['description'])
        hits = [i for i, g in enumerate(groups) if name and (
            g['name'] == name or g['name'].startswith(name + ' ')
            or name.startswith(g['name'] + ' '))]
        allowed.append(hits if len(hits) == 1 else list(range(len(groups))))
    if len(ch_lines) > 12:
        return False
    order = sorted(range(len(ch_lines)),
                   key=lambda i: -float(ch_lines[i]['weight'] or 0))
    need = [g['weight'] for g in groups]
    best = [None, None]

    def walk(k, left, pick):
        if k == len(order):
            if all(abs(x) <= WEIGHT_TOL for x in left):
                cost = 0.0
                for gi, g in enumerate(groups):
                    worth = sum(float(ch_lines[i]['taxable'] or 0)
                                for i in range(len(ch_lines)) if pick[i] == gi)
                    cost += abs(g['value'] - worth)
                if best[0] is None or cost < best[0] - 1e-6:
                    best[0], best[1] = cost, dict(pick)
            return
        i = order[k]
        w = float(ch_lines[i]['weight'] or 0)
        for gi in allowed[i]:
            if left[gi] - w < -WEIGHT_TOL:
                continue
            left[gi] -= w
            pick[i] = gi
            walk(k + 1, left, pick)
            left[gi] += w
            del pick[i]
    walk(0, list(need), {})
    if best[1] is None:
        return False
    for gi, g in enumerate(groups):
        g['lines'] = [ch_lines[i] for i in range(len(ch_lines))
                      if best[1][i] == gi]
    return all(g['lines'] for g in groups)


def restate(bill, challans):
    """The bill as its challans plus GST, or (None, why) when they do not
    account for it exactly."""
    if bill['grand_total'] <= 0 or bill['doc_type'] != 'SALE':
        return None, 'a return'
    if not bill['lines']:
        return None, 'the bill has no lines'
    if any(ln['cess'] or ln['cess_rate'] for ln in bill['lines']):
        return None, 'the bill carries cess'
    # A line of nothing on the challan brings nothing to the bill.
    ch_lines = [ln for c in challans for ln in c['lines'] if not _empty(ln)]
    if not ch_lines:
        return None, 'the challan has no lines'
    for c in challans:
        # A challan that carries tax of its own (some old ones do) would
        # put that tax on the bill as a round off.
        if abs(c['cgst']) + abs(c['sgst']) + abs(c['igst']) + abs(c['cess']):
            return None, 'the challan carries tax'
        # And one whose lines and charges do not come to its total cannot
        # say what the rest of the bill was.
        if abs(c['grand_total'] - sum(ln['taxable'] for ln in c['lines'])
               - c['cutting'] - c['transport'] - c['loading']
               - c['round_off']) > 1:
            return None, 'the challan does not add up'
    charges = dict(cutting=r2(sum(c['cutting'] for c in challans)),
                   transport=r2(sum(c['transport'] for c in challans)),
                   loading=r2(sum(c['loading'] for c in challans)))
    goods = r2(sum(ln['taxable'] for ln in ch_lines))
    total = r2(sum(c['grand_total'] for c in challans))
    if abs(bill['taxable'] - total) > AMOUNT_TOL:
        if abs(bill['taxable'] - goods) <= AMOUNT_TOL and sum(charges.values()):
            return None, 'the bill left the challan\'s charges off'
        return None, 'the bill was re-priced'

    # The bill's own lines, one group per kind of goods it names. A line of
    # nothing -- SHREE's "Stores Department" with no kilos and no value --
    # stays as it is, after the goods.
    empty = [ln for ln in bill['lines'] if _empty(ln)]
    groups = []
    for ln in bill['lines']:
        if ln in empty:
            continue
        key = (ln['item_id'], _goods_name(ln['description']), ln['hsn'] or '')
        g = next((g for g in groups if g['key'] == key), None)
        if g is None:
            g = dict(key=key, line=ln, name=key[1], weight=0.0, value=0.0,
                     rates=set(), cgst=0.0, sgst=0.0, igst=0.0, lines=[])
            groups.append(g)
        g['weight'] += float(ln['weight'] or 0)
        g['value'] += float(ln['taxable'] or 0) + float(ln['charge_share'] or 0)
        g['rates'].add(float(ln['gst_rate'] or 0))
        for k in ('cgst', 'sgst', 'igst'):
            g[k] += float(ln[k] or 0)
    if any(len(g['rates']) > 1 for g in groups):
        return None, 'one kind of goods at two tax rates'

    # Each challan line to the bill's line for the same goods.
    if not groups:
        return None, 'the bill has no goods'
    if not _assign(ch_lines, groups):
        return None, 'a challan line names goods the bill does not'
    for g in groups:
        if abs(sum(float(ln['weight'] or 0) for ln in g['lines'])
               - g['weight']) > WEIGHT_TOL:
            return None, 'the kilos of the goods differ'

    adj = r2(bill['taxable'] - goods - sum(charges.values()))
    head_tax = {k: r2(bill[k]) for k in ('cgst', 'sgst', 'igst')}
    lines = []
    for g in groups:
        g_goods = [float(ln['taxable'] or 0) for ln in g['lines']]
        # What the bill says this kind of goods is worth stays what it is
        # worth -- the HSN summary and the GSTR-1 read it -- and the
        # challan's charges and round off make up the rest of it.
        shares = _spread(r2(g['value'] - sum(g_goods)), g_goods)
        rate = next(iter(g['rates']))
        src = g['line']
        for ln, share in zip(g['lines'], shares):
            lines.append(dict(
                item_id=src['item_id'], description=src['description'],
                hsn=src['hsn'] or '', unit=ln['unit'] or src['unit'] or '',
                qty=r3(ln['qty']), weight=r3(ln['weight']),
                by_weight=ln['by_weight'], incl_price=0.0,
                rate=ln['rate'], discount_pct=ln['discount_pct'],
                taxable=r2(ln['taxable']), gst_rate=rate, cess_rate=0.0,
                cess=0.0, charge_share=share,
                item_detail=ln['item_detail'] or src['item_detail'] or '',
                thickness=ln['thickness'] or src['thickness'] or '',
                cutting_size=ln['cutting_size'] or src['cutting_size'] or '',
                _group=id(g)))
        # The tax SHREE filed on this kind of goods, split over its lines.
        worth = [l['taxable'] + l['charge_share'] for l in lines
                 if l['_group'] == id(g)]
        mine = [l for l in lines if l['_group'] == id(g)]
        for k in ('cgst', 'sgst', 'igst'):
            for l, part in zip(mine, _spread(r2(g[k]), worth)):
                l[k] = part
    # The groups' tax is SHREE's line tax; the header's is what was filed.
    # They agree on every bill this rewrites, or it is not rewritten.
    for k in ('cgst', 'sgst', 'igst'):
        if abs(r2(sum(l[k] for l in lines)) - head_tax[k]) > 0.005:
            return None, 'the bill\'s lines and header tax disagree'
    for ln in empty:
        lines.append(dict({c: ln[c] for c in LINE_COLS}, _group=None))
    for l in lines:
        l.pop('_group')
        l['line_total'] = r2(l['taxable'] + l['charge_share'] + l['cgst']
                             + l['sgst'] + l['igst'])
    if abs(r2(sum(l['taxable'] + l['charge_share'] for l in lines))
           - r2(bill['taxable'])) > 0.005:
        return None, 'the taxable value would move'
    head = dict(subtotal=goods, discount_amt=0.0,
                weight_total=r3(sum(l['weight'] for l in lines)),
                taxable_adj=adj, **charges)
    return (head, lines), None


def _same(bill, head, lines):
    """Whether the bill already reads this way -- nothing to write."""
    if any(abs(float(bill[k] or 0) - v) > 0.004 for k, v in head.items()):
        return False
    if len(bill['lines']) != len(lines):
        return False
    cols = ('item_id', 'weight', 'rate', 'taxable', 'charge_share', 'cgst',
            'sgst', 'igst', 'gst_rate', 'description', 'hsn')
    return all(all(a[c] == b[c] if isinstance(b[c], str) or b[c] is None
                   else abs(float(a[c] or 0) - float(b[c] or 0)) < 0.004
                   for c in cols) for a, b in zip(bill['lines'], lines))


LINE_COLS = ('item_id', 'description', 'hsn', 'unit', 'qty', 'weight',
             'by_weight', 'incl_price', 'rate', 'discount_pct', 'taxable',
             'gst_rate', 'cgst', 'sgst', 'igst', 'cess_rate', 'cess',
             'charge_share', 'item_detail', 'thickness', 'cutting_size',
             'line_total')


def _stock_qty(ln):
    """What a line moves stock by -- api.STOCK_QTY, for one line."""
    return float(ln['weight'] or 0) or float(ln['qty'] or 0)


def write(conn, bill_id, head, lines):
    # The kilos written are the challan's, which may be the gram
    # WEIGHT_TOL allows away from the bill's. The import set each item's
    # opening so the stock ends on SHREE's own figure; the opening takes up
    # the difference, so it still does.
    kind = conn.execute('SELECT doc_type FROM invoice WHERE id=?',
                        (bill_id,)).fetchone()['doc_type']
    sign = api.DOC_STOCK.get(kind, 0)
    moved = defaultdict(float)
    for ln in conn.execute('SELECT item_id, weight, qty FROM invoice_item '
                           'WHERE invoice_id=? AND item_id IS NOT NULL',
                           (bill_id,)):
        moved[ln['item_id']] += _stock_qty(ln)
    for ln in lines:
        if ln['item_id']:
            moved[ln['item_id']] -= _stock_qty(ln)
    conn.execute('UPDATE invoice SET %s WHERE id=?'
                 % ', '.join('%s=?' % k for k in head),
                 list(head.values()) + [bill_id])
    conn.execute('DELETE FROM invoice_item WHERE invoice_id=?', (bill_id,))
    conn.executemany(
        'INSERT INTO invoice_item(invoice_id, %s) VALUES (?%s)'
        % (', '.join(LINE_COLS), ',?' * len(LINE_COLS)),
        [[bill_id] + [l[c] for c in LINE_COLS] for l in lines])
    for iid, q in moved.items():
        if sign and abs(q) > 1e-9:
            conn.execute('UPDATE item SET opening_qty=ROUND(opening_qty+?, 3) '
                         'WHERE id=?', (sign * q, iid))


def run(conn, apply_it=False):
    why = Counter()
    done, left = [], []
    for bill_id, chs, in_parts in _load(conn):
        bill = _doc(conn, bill_id)
        if in_parts:
            why['a challan billed in parts'] += 1
            left.append((bill, 'a challan billed in parts'))
            continue
        challans = [_doc(conn, c) for c in chs]
        got, reason = restate(bill, challans)
        if not got:
            why[reason] += 1
            left.append((bill, reason))
            continue
        head, lines = got
        if _same(bill, head, lines):
            why['already reads as its challan'] += 1
            continue
        done.append(bill_id)
        if apply_it:
            write(conn, bill_id, head, lines)
    if apply_it:
        conn.commit()
    return done, why, left


def main():
    db.quiet_windows_errors()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--apply', action='store_true',
                    help='rewrite the bills; without it nothing is changed')
    ap.add_argument('--csv', help='write the bills left as they are to this file')
    # The update's check is told which bills were rebuilt from their
    # challans: their lines are the challan's, not SHREE's, and on books
    # never restated before, thousands of them change at once (27 Sep 2026).
    ap.add_argument('--ids', help='write the ids of the bills rewritten to '
                                  'this file (JSON)')
    args = ap.parse_args()
    conn = db.connect()
    try:
        done, why, left = run(conn, args.apply)
        if args.ids:
            with open(args.ids, 'w', encoding='utf-8') as fh:
                json.dump(done, fh)
        print('%d bills %s as their challan plus GST.'
              % (len(done), 'rewritten' if args.apply else 'to rewrite'))
        print('Left as SHREE wrote them:')
        for reason, n in why.most_common():
            print('   %-46s %6d' % (reason, n))
        if args.csv:
            code = {r['id']: r['code'] for r in conn.execute(
                'SELECT id, code FROM company')}
            with open(args.csv, 'w', newline='', encoding='utf-8-sig') as fh:
                w = csv.writer(fh)
                w.writerow(['firm', 'bill', 'date', 'taxable', 'why'])
                for b, reason in left:
                    w.writerow([code.get(b['company_id']),
                                ('%s %s' % (b['series'], b['invoice_no'])).strip(),
                                b['invoice_date'], b['taxable'], reason])
        if not args.apply:
            print('\nNothing was changed. Run again with --apply.')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
