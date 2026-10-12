#!/usr/bin/env python3
"""Work out which firm bills each customer, and set it on the customer.

    python3 app/allocate_customers.py                 # show what it would do
    python3 app/allocate_customers.py --apply
    python3 app/allocate_customers.py --apply --firm BSTP

Orders and challans are taken in a no-tax firm; the tax bill is raised in one of
the GST firms. Which one is a fact about the customer, and until it is recorded
the "Bill it" button has to ask every time. This reads it out of the history.

How the firm is decided, strongest evidence first
-------------------------------------------------
1. The old system already says so. A challan carries no tax, so when the bill
   went out, SHREE posted a journal in the order firm putting the GST on the
   customer against an account standing for the billing firm -- "BALKRISHNA
   STEEL TRADERS (GST)" and its like. Every one of those journals names a
   customer and a firm. Where a customer has several, the most recent wins:
   customers do move between the firms, and where they have, the latest bill is
   the one that says where they are now.

2. Failing that, the customer's own name. A customer who exists in exactly one
   of the GST firms is billed there. One who exists in both goes to whichever
   billed them most recently.

3. Anything still undecided is left alone rather than guessed at. Those are
   almost all names that have never been billed at all.

Nothing else on the customer is touched, and running it again only ever
re-states the same answer.
"""
import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db    # noqa: E402

# The account each firm's GST is parked against inside an order firm. SHREE
# renamed some of them over the years and kept the old one alongside, so a
# firm can have more than one.
BRIDGE_HINTS = [
    ('BST', ('BALKRISHNA STEEL TRADERS (GST)', 'BALKRISHANA STEEL TRADERS (GST)',
             'BALKRISHNA STEEL TRADERS VAT', 'BALKRISHANA STEEL TRADERS VAT')),
    ('BSI', ('BALKRISHNA STEEL INDUSTRIES', 'BALKRISHNA STEEL INDUSTRIES (GST)',
             'BALKRISHNA STEEL INDUSTRIES VAT')),
    ('PSI', ('PATEL STEEL INDUSTRIES VAT', 'PATEL STEEL IND VAT',
             'PATEL STEEL INDUSTRIES (GST)')),
    ('VT',  ('VRAJ TRADERS VAT', 'VRAJ TRADERS (GST)')),
]

# The group SHREE files those accounts under, and the receivables group. A
# bridge account sits outside receivables, which is what keeps it from being
# mistaken for a customer.
BRIDGE_GROUPS = ('9008', '9002', '8121')
CUSTOMER_GROUP = '2101'


def norm(name):
    return re.sub(r'\s+', ' ', (name or '').strip()).upper()


def bridges(conn, cid):
    """{ledger id: firm code} for the billing-firm accounts inside this firm."""
    want = {}
    for code, names in BRIDGE_HINTS:
        for n in names:
            want[norm(n)] = code
    out = {}
    for r in conn.execute(
            'SELECT l.id, l.name, g.code gcode FROM ledger l '
            'JOIN account_group g ON g.id=l.group_id WHERE l.company_id=?',
            (cid,)):
        code = want.get(norm(r['name']))
        if code and r['gcode'] != CUSTOMER_GROUP:
            out[r['id']] = code
    return out


def evidence(conn, cid, bridge):
    """{customer id: {firm: [dates]}} from the journals that billed them."""
    if not bridge:
        return {}
    ids = ','.join(str(k) for k in bridge)
    seen = defaultdict(lambda: defaultdict(list))
    for r in conn.execute(
            'SELECT p2.ledger_id cust, p1.ledger_id bridge, p1.date d '
            'FROM posting p1 '
            'JOIN posting p2 ON p2.doc_kind=p1.doc_kind AND p2.doc_id=p1.doc_id '
            '                AND p2.ledger_id<>p1.ledger_id '
            'JOIN ledger l ON l.id=p2.ledger_id '
            'JOIN account_group g ON g.id=l.group_id '
            'WHERE p1.company_id=? AND p1.doc_kind=? AND p1.ledger_id IN (%s) '
            'AND g.code=?' % ids, (cid, 'JOURNAL', CUSTOMER_GROUP)):
        seen[r['cust']][bridge[r['bridge']]].append(r['d'])
    return seen


def gst_firms(conn):
    return {r['code']: r['id'] for r in conn.execute(
        "SELECT id, code FROM company WHERE tax_mode='gst' AND active=1")}


def billed_direct(conn, fid):
    """{customer name: (last bill date, how many)} inside one GST firm."""
    out = {}
    for r in conn.execute(
            "SELECT l.name, max(i.invoice_date) d, count(*) n FROM invoice i "
            'JOIN ledger l ON l.id=i.party_id WHERE i.company_id=? AND '
            "i.doc_type IN ('SALE','CREDIT') GROUP BY l.name", (fid,)):
        key = norm(r['name'])
        cur = out.get(key)
        if not cur or (r['d'] or '') > cur[0]:
            out[key] = (r['d'] or '', r['n'])
        elif cur:
            out[key] = (cur[0], cur[1] + r['n'])
    return out


def decide(conn, cid, verbose=True):
    """Return [(customer id, name, firm code, why, twin id or None)]."""
    firms = gst_firms(conn)
    # A firm that is no longer in the app cannot bill anybody, so its bridge
    # account is no longer evidence -- Patel Steel and Vraj Traders were taken
    # out and their VAT-era accounts are still standing in the order firm.
    bridge = {k: v for k, v in bridges(conn, cid).items() if v in firms}
    if verbose:
        names = defaultdict(list)
        for lid, code in bridge.items():
            row = conn.execute('SELECT name FROM ledger WHERE id=?',
                               (lid,)).fetchone()
            names[code].append(row['name'])
        for code in sorted(names):
            print('   %-4s billed through: %s' % (code, ', '.join(names[code])))
        if not bridge:
            print('   no billing-firm account found in this firm; '
                  'falling back to names alone')

    seen = evidence(conn, cid, bridge)
    # The bills themselves are evidence too, and sometimes fresher than the
    # journal: a customer who has quietly moved firms shows up in the new
    # firm's sales register before anything else says so.
    direct = {code: billed_direct(conn, fid) for code, fid in firms.items()}

    # Every customer in this firm, and the same names over in the GST firms.
    customers = [(r['id'], r['name']) for r in conn.execute(
        'SELECT l.id, l.name FROM ledger l JOIN account_group g '
        'ON g.id=l.group_id WHERE l.company_id=? AND l.active=1 AND '
        '(g.code=? OR l.party_kind=?) ORDER BY l.name',
        (cid, CUSTOMER_GROUP, 'customer'))]
    over = {}
    for code, fid in firms.items():
        over[code] = {}
        for r in conn.execute('SELECT id, name FROM ledger l WHERE '
                              'company_id=? AND active=1', (fid,)):
            over[code].setdefault(norm(r['name']), r['id'])

    out = []
    for lid, name in customers:
        key = norm(name)
        pick = why = None

        # Everything known about who has billed this customer, and when.
        when = {}
        count = {}
        for f, dates in (seen.get(lid) or {}).items():
            when[f] = max(dates)
            count[f] = len(dates)
        for f in firms:
            hit = direct.get(f, {}).get(key)
            if not hit or not hit[0]:
                continue
            if hit[0] > when.get(f, ''):
                when[f] = hit[0]
            count[f] = max(count.get(f, 0), hit[1])

        if when:
            # Where a customer has ever been billed by one of the firms still
            # trading, an older bill from one that has stopped must not outvote
            # it.
            main = {f: d for f, d in when.items() if f in ('BST', 'BSI')}
            use = main or when
            pick = max(use, key=lambda f: (use[f], count.get(f, 0)))
            rest = ', '.join('%s to %s' % (f, when[f])
                             for f in sorted(when) if f != pick)
            why = 'billed by %s x%d, last %s' % (pick, count.get(pick, 0),
                                                 when[pick])
            if rest:
                why += ' (also %s)' % rest
        else:
            here = [c for c in firms if key in over.get(c, {})]
            if len(here) == 1:
                pick, why = here[0], 'only %s has this customer' % here[0]
            elif len(here) > 1:
                pick = sorted(here)[0]
                why = 'in %s, none has billed; taking %s' % (
                    '+'.join(sorted(here)), pick)
        twin = over.get(pick, {}).get(key) if pick else None
        out.append((lid, name, pick, why, twin))
    return out, firms


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--firm', action='append',
                    help='order-firm code to work through; default every no-tax firm')
    ap.add_argument('--apply', action='store_true',
                    help='write the answers; without it nothing is changed')
    ap.add_argument('--show', type=int, default=0,
                    help='print this many decided customers per firm')
    args = ap.parse_args()

    conn = db.connect()
    try:
        codes = args.firm or [r['code'] for r in conn.execute(
            "SELECT code FROM company WHERE tax_mode='none' AND active=1 "
            'ORDER BY code')]
        grand = defaultdict(int)
        for code in codes:
            row = conn.execute('SELECT id, name FROM company WHERE code=?',
                               (code,)).fetchone()
            if not row:
                print('\n%s: no such firm' % code)
                continue
            print('\n%s -- %s' % (code, row['name']))
            picks, firms = decide(conn, row['id'])
            tally = defaultdict(int)
            for lid, name, pick, why, twin in picks:
                tally[pick or '(undecided)'] += 1
                grand[pick or '(undecided)'] += 1
            total = len(picks)
            for k in sorted(tally, key=lambda x: (-tally[x], x)):
                print('   %-12s %5d  (%.0f%%)'
                      % (k, tally[k], 100.0 * tally[k] / total if total else 0))
            print('   %-12s %5d' % ('total', total))
            if args.show:
                print('   %-38s %-5s %s' % ('customer', 'firm', 'why'))
                for lid, name, pick, why, twin in picks[:args.show]:
                    print('   %-38s %-5s %s' % (name[:38], pick or '-', why or ''))
            if args.apply:
                n = 0
                for lid, name, pick, why, twin in picks:
                    if not pick:
                        continue
                    conn.execute(
                        'UPDATE ledger SET home_company=?, '
                        'linked_ledger=COALESCE(?, linked_ledger) WHERE id=?',
                        (firms[pick], twin, lid))
                    n += 1
                conn.commit()
                print('   set the billing firm on %d customer(s)' % n)
        print('\n' + '=' * 58)
        print('Across every order firm')
        for k in sorted(grand, key=lambda x: (-grand[x], x)):
            print('  %-12s %5d' % (k, grand[k]))
        if not args.apply:
            print('\nNothing was changed. Re-run with --apply to write it.')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
