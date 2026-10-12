#!/usr/bin/env python3
"""Apply a customer -> billing firm list.

    python3 app/set_billing.py --list                    # what firms exist
    python3 app/set_billing.py --file split.csv --dry-run
    python3 app/set_billing.py --file split.csv

The file needs a customer column and a firm column; a header row is used when
present, otherwise the first two columns are taken. CSV, TSV and .xlsx are all
read. The firm may be a company code (BSI), its name, or any unambiguous part
of the name.

Matching is on the customer name, ignoring case, punctuation and extra spaces,
so "M/S. YUG ENTERPRISE" and "MS YUG ENTERPRISE" line up. Anything that cannot
be matched is listed at the end rather than guessed at.
"""
import argparse
import csv
import os
import re
import sys
import zipfile
from xml.etree import ElementTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api  # noqa: E402
import db   # noqa: E402

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


def norm(name):
    """Collapse a party name to something comparable."""
    t = (name or '').upper()
    t = re.sub(r'\bM/?S\.?\b', ' ', t)          # drop a leading M/S
    t = re.sub(r'[^A-Z0-9]+', ' ', t)
    return ' '.join(t.split())


def read_xlsx(path):
    """First sheet of an .xlsx, as rows of strings. No dependencies."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = ElementTree.fromstring(z.read('xl/sharedStrings.xml'))
            for si in root.findall(NS + 'si'):
                shared.append(''.join(t.text or '' for t in si.iter(NS + 't')))
        name = next(n for n in z.namelist()
                    if n.startswith('xl/worksheets/sheet') and n.endswith('.xml'))
        root = ElementTree.fromstring(z.read(name))
        out = []
        for row in root.iter(NS + 'row'):
            cells = []
            for c in row.findall(NS + 'c'):
                v = c.find(NS + 'v')
                text = ''
                if v is not None and v.text is not None:
                    text = (shared[int(v.text)] if c.get('t') == 's'
                            and v.text.isdigit() else v.text)
                elif c.find(NS + 'is') is not None:
                    text = ''.join(t.text or ''
                                   for t in c.find(NS + 'is').iter(NS + 't'))
                cells.append(text)
            out.append(cells)
        return out


def read_table(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.xlsx', '.xlsm'):
        return read_xlsx(path)
    with open(path, newline='', encoding='utf-8-sig', errors='replace') as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
        except csv.Error:
            dialect = csv.excel
        return [r for r in csv.reader(fh, dialect)]


def pick_columns(rows):
    """Find the customer and firm columns, by header if there is one."""
    if not rows:
        raise SystemExit('The file is empty.')
    head = [c.strip().lower() for c in rows[0]]
    cust = firm = None
    for i, h in enumerate(head):
        if cust is None and any(k in h for k in
                                ('customer', 'party', 'name', 'account')):
            cust = i
        if firm is None and any(k in h for k in
                                ('firm', 'company', 'billed', 'bill', 'unit')):
            firm = i
    if cust is not None and firm is not None and cust != firm:
        return cust, firm, rows[1:]
    return 0, 1, rows            # no usable header: first two columns


def resolve_firm(text, firms):
    t = (text or '').strip().upper()
    if not t:
        return None
    for f in firms:
        if t == f['code'].upper() or t == f['name'].upper():
            return f
    hits = [f for f in firms if t in f['name'].upper() or f['code'].upper() in t]
    return hits[0] if len(hits) == 1 else None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--file', help='the customer -> firm list')
    ap.add_argument('--dry-run', action='store_true',
                    help='report what would change, write nothing')
    ap.add_argument('--list', action='store_true',
                    help='show the billing firms and stop')
    args = ap.parse_args()

    db.init()
    conn = db.connect()
    try:
        firms = api.billing_firms(conn)
        if args.list or not args.file:
            print('\nBilling firms:')
            for f in firms:
                print('  %-6s %s' % (f['code'], f['name']))
            print('\nGive a file with a customer column and a firm column:')
            print('  python3 app/set_billing.py --file split.csv --dry-run\n')
            return

        # every customer master, keyed by normalised name
        index = {}
        for r in conn.execute(
                "SELECT l.id, l.name, l.company_id, c.code FROM ledger l "
                'JOIN company c ON c.id=l.company_id '
                "WHERE l.party_kind IN ('customer','both')"):
            index.setdefault(norm(r['name']), []).append(dict(r))

        rows = read_table(args.file)
        ci, fi, body = pick_columns(rows)
        print('Reading %s  (customer = column %d, firm = column %d)'
              % (os.path.basename(args.file), ci + 1, fi + 1))

        applied = skipped = 0
        unmatched, unknown_firm = [], []
        for raw in body:
            if len(raw) <= max(ci, fi):
                continue
            name, firm_text = raw[ci].strip(), raw[fi].strip()
            if not name:
                continue
            firm = resolve_firm(firm_text, firms)
            if not firm:
                unknown_firm.append((name, firm_text))
                continue
            hits = index.get(norm(name))
            if not hits:
                unmatched.append(name)
                continue
            for h in hits:
                if args.dry_run:
                    applied += 1
                else:
                    api.set_billing_firm(conn, h['id'], firm['id'])
                    applied += 1

        print('\n  %s %d customer record(s) to their billing firm.'
              % ('Would set' if args.dry_run else 'Set', applied))
        if skipped:
            print('  %d unchanged.' % skipped)
        if unknown_firm:
            print('\n  Firm not recognised on %d row(s):' % len(unknown_firm))
            for n, f in unknown_firm[:10]:
                print('    %-38s -> "%s"' % (n[:38], f))
        if unmatched:
            print('\n  No customer master matched %d name(s):' % len(unmatched))
            for n in unmatched[:20]:
                print('    %s' % n)
            if len(unmatched) > 20:
                print('    ... and %d more' % (len(unmatched) - 20))
            print('  Add them on the Parties screen, then run this again.')
        if args.dry_run:
            print('\n  Nothing was written. Drop --dry-run to apply.')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
