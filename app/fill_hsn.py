#!/usr/bin/env python3
"""Put the HSN codes where the portal looks for them.

    python3 app/fill_hsn.py                   # what it would do, writes nothing
    python3 app/fill_hsn.py --apply           # write them
    python3 app/fill_hsn.py --apply --lines   # and onto old bill lines too

Years of items were entered with the code typed into the *name* -- `CRCA
72091510` -- and the HSN column beside it left empty. Everything downstream
reads the column: the bill line takes its HSN off the item when a product is
picked, and the e-way and e-invoice files refuse a line that has none. So the
code has to be moved into the column it belongs in.

**The name is not being trusted on its own.** For every item this would fill,
the bills that firm has already raised for it are read, and the HSN they were
filed with is compared against the one in the name. Where they agree -- 134
items of the 136 that carry a code in their name -- the name is not a guess at
all but the same answer the books have been filing for years. Where they
disagree, nothing is written and the item is listed for someone to settle,
because which code is right decides what goes into GSTR-1.

Items with no code in the name and none in their history are listed too. There
is nothing to derive them from: what a plate or an angle is classified under is
a decision, not a lookup, and this script will not invent one.

Only the GST firms are touched. The order firm's book carries no tax and its
single generic item is the thing a challan is re-picked against on the bill --
see the *Pick products* button on a bill raised from a challan.
"""
import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api  # noqa: E402
import db   # noqa: E402

# The same shape eway.hsn_of reads: the portal takes 4, 6 or 8 digits.
HSN_RE = re.compile(r'(?<!\d)(\d{8}|\d{6}|\d{4})(?!\d)')


def digits(value):
    return re.sub(r'\D', '', str(value or ''))


def is_hsn(value):
    return len(digits(value)) in (4, 6, 8)


def from_name(name):
    """The code written into an item's name, longest first.

    `CRCA COIL SHEET 72091590` gives 72091590. `PIPE-7306 (73061011)` holds two
    and the longer one is the more specific, so that is the one taken -- but a
    name holding two different codes is reported rather than filled, because
    the two are a disagreement in themselves.
    """
    found = HSN_RE.findall(str(name or ''))
    if not found:
        return None, []
    return max(found, key=len), sorted(set(found), key=len, reverse=True)


def filed_for(conn, cid, item_id):
    """What this firm's own bills carried as the HSN for this item."""
    seen = collections.Counter()
    for row in conn.execute(
            'SELECT ii.hsn FROM invoice_item ii JOIN invoice i '
            'ON i.id=ii.invoice_id WHERE i.company_id=? AND ii.item_id=? '
            "AND TRIM(COALESCE(ii.hsn,''))<>''", (cid, item_id)):
        if is_hsn(row[0]):
            seen[digits(row[0])] += 1
    return seen


def look(conn):
    """Every GST firm's items, sorted into what can be filled and what cannot."""
    out = []
    for firm in api.rows(conn.execute(
            "SELECT id, code, name FROM company WHERE tax_mode='gst' "
            'ORDER BY name')):
        fill, conflict, nothing, already = [], [], [], 0
        for it in api.rows(conn.execute(
                'SELECT id, name, hsn FROM item WHERE company_id=? '
                'ORDER BY name', (firm['id'],))):
            if is_hsn(it['hsn']):
                already += 1
                # A column holding 7204 under a name saying 72044900 is not
                # wrong, only less exact. Worth knowing, never overwritten.
                named, _ = from_name(it['name'])
                if named and named != digits(it['hsn']):
                    conflict.append(dict(item=it, why='column', named=named,
                                         other=digits(it['hsn']), n=0))
                continue
            named, every = from_name(it['name'])
            filed = filed_for(conn, firm['id'], it['id'])
            top = filed.most_common(1)[0][0] if filed else None
            if named and len(every) > 1:
                conflict.append(dict(item=it, why='two in the name',
                                     named=named, other='/'.join(every), n=0))
            elif named and top and named != top:
                conflict.append(dict(item=it, why='the bills say otherwise',
                                     named=named, other=top, n=filed[top]))
            elif named:
                fill.append(dict(item=it, hsn=named, n=filed.get(named, 0)))
            elif top:
                # Nothing in the name, but the bills agree on one all the same.
                fill.append(dict(item=it, hsn=top, n=filed[top]))
            else:
                nothing.append(it)
        out.append(dict(firm=firm, fill=fill, conflict=conflict,
                        nothing=nothing, already=already))
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split('\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true',
                    help='write the codes; without it nothing is changed')
    ap.add_argument('--lines', action='store_true',
                    help='also fill the HSN on saved bill lines that have '
                         'none and whose item now has one')
    args = ap.parse_args()

    conn = db.connect()
    report = look(conn)
    filled = lines_filled = 0

    for r in report:
        firm, fill = r['firm'], r['fill']
        print('\n%s (%s)' % (firm['name'], firm['code']))
        print('  %d item(s) already carry an HSN.' % r['already'])
        if fill:
            checked = sum(1 for f in fill if f['n'])
            print('  %s %d item(s) -- %d of them confirmed by %s own bills:'
                  % ('Filling' if args.apply else 'Would fill', len(fill),
                     checked, "the firm's"))
            for f in fill[:200]:
                print('    %-44s -> %-9s %s'
                      % (f['item']['name'][:44], f['hsn'],
                         ('(%d bill line(s) filed with it)' % f['n'])
                         if f['n'] else '(not billed yet)'))
        if args.apply and fill:
            for f in fill:
                conn.execute('UPDATE item SET hsn=? WHERE id=?',
                             (f['hsn'], f['item']['id']))
                filled += 1
            if args.lines:
                for f in fill:
                    cur = conn.execute(
                        'UPDATE invoice_item SET hsn=? WHERE item_id=? AND '
                        "TRIM(COALESCE(hsn,''))='' AND invoice_id IN "
                        '(SELECT id FROM invoice WHERE company_id=?)',
                        (f['hsn'], f['item']['id'], firm['id']))
                    lines_filled += cur.rowcount

        if r['conflict']:
            print('  %d left alone -- two answers, and which one is right '
                  'decides GSTR-1:' % len(r['conflict']))
            for c in r['conflict']:
                print('    %-44s name says %-9s %s says %s'
                      % (c['item']['name'][:44], c['named'],
                         c['why'], c['other']
                         + (' (%d bill line(s))' % c['n'] if c['n'] else '')))
        if r['nothing']:
            print('  %d with no code anywhere -- not in the name, not in any '
                  'bill. Nothing to read one off, so nothing is written:'
                  % len(r['nothing']))
            for it in r['nothing']:
                print('    %s' % it['name'])

    if args.apply:
        conn.commit()
        print('\n  Filled %d item(s).' % filled)
        if args.lines:
            print('  Filled the HSN on %d saved bill line(s).' % lines_filled)
        print('  The Items list shows them; a bill picks them up from now on.')
    else:
        print('\n  Nothing was written. Add --apply to fill them in,')
        print('  and --lines as well to put them onto old bill lines too.')


if __name__ == '__main__':
    main()
