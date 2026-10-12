#!/usr/bin/env python3
"""Print the real BST stationery.

The tax invoice and delivery challan templates live in ../bst-pdf-generator,
where every rule and baseline was measured off the original PDFs. This module
is the only thing that knows how a saved document maps onto their JSON, so the
templates themselves stay untouched.
"""
import os
import re
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(os.path.dirname(BASE), 'bst-pdf-generator')
ASSETS = os.path.join(GEN, 'assets')
if GEN not in sys.path:
    sys.path.insert(0, GEN)

import api  # noqa: E402


class PrintError(api.ApiError):
    pass


_available = None


def available():
    """True when the stationery templates and reportlab are both importable.

    Remembered after the first look: importing reportlab is not cheap, and a
    failed import is retried by Python on every call.
    """
    global _available
    if _available is None:
        try:
            import bst  # noqa: F401
            _available = True
        except Exception:                               # noqa: BLE001
            _available = False
    return _available


def warm():
    """Load ReportLab and the fonts before anyone prints.

    The server calls this in the background as it starts, so the first bill
    of the day comes out as fast as the rest instead of waiting on imports
    and a search of the Fonts folder. Any failure is left for the first
    real print to report.
    """
    try:
        if available():
            from bst import fonts
            fonts.register_fonts()
    except Exception:                                   # noqa: BLE001
        pass


# ------------------------------------------------------------------ helpers

def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _s(v):
    return ('' if v is None else str(v)).strip()


def _date(iso, short=False):
    """2026-08-21 -> 21/08/2026. The bill and the challan both print the
    whole year now; `short` is kept for anything that still asks for 21/08/26."""
    parts = _s(iso).split('-')
    if len(parts) != 3:
        return _s(iso)
    y, m, d = parts
    return '%s/%s/%s' % (d, m, y[2:] if short else y)


# SHREE kept a date in the bill's "payment terms" box -- "09/01/18", or an
# empty "/  /" -- on 12,865 of its 15,244 bills. Printed as terms, that read
# "Payment Terms : / /". Only digits and date punctuation is not terms.
_NOT_TERMS = re.compile(r'^[\d\s/.\-]*$')


def _terms(v):
    v = ' '.join(_s(v).split())
    return '' if _NOT_TERMS.match(v) else v.upper()


def _particulars(line):
    """What the line is, without the HSN SHREE typed after it.

    "CRCA  72099000" and "M S SHEET (7209)" printed their HSN twice -- once in
    the description and again in the HSN column beside it. Only an exact
    repeat of the line's own HSN is dropped; a longer code in the description
    says more than the column does and stays.
    """
    text = ' '.join(x for x in (_s(line.get('description')),
                                _s(line.get('item_detail'))) if x)
    hsn = _s(line.get('hsn'))
    if hsn:
        text = re.sub(r'\s*\(?\s*(?<!\d)%s\s*\)?\s*$' % re.escape(hsn), '',
                      _s(line.get('description')))
        text = ' '.join(x for x in (text, _s(line.get('item_detail'))) if x) \
            or _s(line.get('description'))
    return ' '.join(text.split())


TITLES = {
    'SALE': 'T A X    I N V O I C E',
    'PURCHASE': 'P U R C H A S E   B I L L',
    'CREDIT': 'C R E D I T   N O T E',
    'DEBIT': 'D E B I T   N O T E',
}


def _seller(co):
    """The letterhead, straight off the company master."""
    addr = ' '.join(x for x in (_s(co.get('address1')), _s(co.get('address2')))
                    if x)
    if not addr:
        addr = ', '.join(x for x in (_s(co.get('city')), _s(co.get('pin'))) if x)
    phone, mob = _s(co.get('phone')), _s(co.get('mobile'))
    line = []
    if phone:
        line.append('Phone: %s' % phone)
    if mob:
        line.append('Mobile No: %s' % mob)
    email = _s(co.get('email'))
    juris = _s(co.get('jurisdiction')) or _s(co.get('city'))
    return dict(
        name=_s(co.get('name')),
        short_name=_s(co.get('short_name')) or _s(co.get('code')),
        address=addr,
        phone_line='     '.join(line),
        email_line=('E-Mail: %s' % email) if email else '',
        gstin=_s(co.get('gstin')),
        jurisdiction_line=('Subject to %s Jurisdiction.' % juris) if juris else '',
        logo='logo.jpg', signature='signature.jpg')


def _buyer_address(inv):
    """Up to the four address lines the party box holds."""
    tail = ' '.join(x for x in (_s(inv.get('party_city')),
                                _s(inv.get('party_pin'))) if x)
    lines = [_s(inv.get('party_address1')), _s(inv.get('party_address2')), tail]
    return [l for l in lines if l][:4]


def _qty_of(l):
    """A weighed line is printed on its weight; anything else on pieces."""
    w = _f(l.get('weight'))
    return w if (l.get('by_weight') and w) else _f(l.get('qty'))


# ------------------------------------------------------------- tax invoice

def _mobile(conn, inv):
    """The customer's mobile for the bill: its own account's, else the
    challan's customer's it was billed from, else the challan book's account
    tied to it. SHREE kept the numbers on the challan book's customers -- 70
    of 1,573 bills since April 2025 had one of their own."""
    mob = _s(inv.get('party_phone'))
    for sql, arg in (
            ('SELECT l.phone FROM invoice c JOIN ledger l ON l.id=c.party_id '
             'WHERE c.id=?', inv.get('from_challan')),
            ("SELECT phone FROM ledger WHERE linked_ledger=? "
             "AND TRIM(COALESCE(phone,''))<>'' ORDER BY id LIMIT 1",
             inv.get('party_id')),
            ('SELECT l.phone FROM ledger p JOIN ledger l ON l.id=p.linked_ledger '
             'WHERE p.id=?', inv.get('party_id'))):
        if mob:
            break
        if arg:
            row = conn.execute(sql, (arg,)).fetchone()
            mob = _s(row[0]) if row else ''
    return mob


def invoice_data(inv, challan_terms='', mobile=''):
    co = inv['company']
    items = []
    for i, l in enumerate(inv['lines']):
        # Charges get named rows of their own in the totals column, so the
        # line amount is the goods alone. The printed rate is worked back from
        # the amount so rate x quantity always reads true on the page.
        amount = round(_f(l.get('taxable')), 2)
        qty = _qty_of(l)
        # Worded as its challan words it, thickness first (6 Oct 2026).
        particulars, head = _challan_particulars(l)
        items.append(dict(
            sr=i + 1,
            particulars=particulars, head=head,
            hsn=_s(l.get('hsn')), qty=qty, unit=_s(l.get('unit')) or 'KGS',
            rate=round(amount / qty, 3) if qty else _f(l.get('rate')),
            gst_pct=_f(l.get('gst_rate')), amount=amount))

    summary = []
    for g in inv.get('gst_summary') or []:
        tax = _f(g.get('cgst')) + _f(g.get('sgst')) + _f(g.get('igst')) \
            + _f(g.get('cess'))
        summary.append(dict(rate=_f(g.get('rate')), taxable=_f(g.get('taxable')),
                            sgst_amt=_f(g.get('sgst')), cgst_amt=_f(g.get('cgst')),
                            total_gst=round(tax, 2)))

    itype = _s(inv.get('invoice_type')) or 'Debit'
    ac = _s(co.get('bank_ac'))
    ifsc = _s(co.get('bank_ifsc'))
    branch = _s(co.get('bank_branch'))
    return dict(
        seller=_seller(co),
        buyer=dict(name=_s(inv.get('party_name')), address=_buyer_address(inv),
                   gstin=_s(inv.get('party_gstin')), mobile=_s(mobile)),
        invoice=dict(
            memo_label='%s MEMO' % itype.upper(),
            title=TITLES.get(inv['doc_type'], TITLES['SALE']),
            copy_label='ORIGINAL', number=api.docno(inv),
            date=_date(inv.get('invoice_date')),
            transport=_s(inv.get('transporter')), lr_no=_s(inv.get('lr_no')),
            lr_date=_date(inv.get('lr_date')),
            vehicle_no=_s(inv.get('vehicle_no')), cases=_s(inv.get('cases')),
            # The bill's own terms when it has any, else the challan's -- the
            # two documents go to one customer and should say the same.
            payment_terms=_terms(inv.get('payment_terms')) or _terms(challan_terms)),
        items=items,
        bank=dict(name=_s(co.get('bank_name')),
                  account_line=('A/C No: %s' % ac) if ac else '',
                  ifsc_line=('IFSC Code: %s' % ifsc) if ifsc else '',
                  branch_line=('Branch: %s' % branch) if branch else ''),
        gst_summary=summary,
        totals=dict(sgst=_f(inv.get('sgst')), cgst=_f(inv.get('cgst'))),
        charges=[dict(label='Cutting Charges', amount=_f(inv.get('cutting'))),
                 dict(label='Transport Charges', amount=_f(inv.get('transport'))),
                 dict(label='Loading Charges', amount=_f(inv.get('loading'))),
                 # The challan's own round off, carried in so the bill lands
                 # exactly on the challan total plus GST.
                 dict(label='Round Off (challan)',
                      amount=_f(inv.get('taxable_adj')))],
        taxable_value=_f(inv.get('taxable')),
        # Added after the GST and not taxed (a purchase's TCS, 8 Oct 2026).
        after_tax=[dict(label='TCS', amount=_f(inv.get('tcs')))],
        round_off=_f(inv.get('round_off')),
        grand_total=_f(inv.get('grand_total')))


# ----------------------------------------------------------------- challan

# The thickness a challan line's detail opens with, the way it is typed on
# the pad: "0.90", "1.35MM", "3 MM", "22 GAGE", "2.10/30", "4.00/1.00/0.50".
# A bare whole number is not one -- "05/05/18" and "290124" open details too.
_THICK = re.compile(r'(\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)*)(\s*(?:MM|GAGE|GAUGE)\b)?'
                    r'(?=\s|$)', re.I)


# A steel's own name, "CRCA - REGULAR - 0.70". A bill raised off a challan
# keeps the name but not the steel (a GST firm keeps no Challan Stock), so on
# a bill the name is what says it is one.
_STEEL_NAME = re.compile(r'^(\S.* - \S.*) - (\d+(?:\.\d+)?)$')


def _challan_particulars(line):
    """(particulars, head) for a challan line -- and, since 6 Oct 2026, a
    bill's ("repeat the same format, font size and everything as Challan's
    particulars for the Bill's particulars"): thickness, then the product,
    then the piece count, then the rest of the detail -- "0.90 CRCA 425.00
    CRAC CUTTING" (4 Oct 2026: "thickness first and NOS afterwards"; the
    piece count used to ride in front, "425.00 CRCA 0.90 CRAC CUTTING").

    The thickness is a steel's own (CRCA - REGULAR - 0.50 prints as
    0.50 CRCA - REGULAR), else the one the detail opens with. A line with
    neither keeps its words in order, the piece count after the product.
    """
    pieces = _f(line.get('qty'))
    nos = ('%.2f' % pieces) if (pieces and _f(line.get('weight'))) else ''
    name = _particulars(dict(line, item_detail=''))
    detail = ' '.join(_s(line.get('item_detail')).split())
    thick, st = '', _s(line.get('steel_thick'))
    steel = bool(_s(line.get('steel_type')) and st and name.endswith(' - ' + st))
    if not st and _STEEL_NAME.match(name):
        st, steel = _STEEL_NAME.match(name).group(2), True
    if steel:
        thick, name = st, name[:-len(' - ' + st)]
    else:
        m = _THICK.match(detail)
        if m and ('.' in m.group(1) or m.group(2)):
            thick, detail = ' '.join(m.group(0).split()), detail[m.end():].strip()
    head = ' '.join(x for x in (thick, name, nos) if x)
    # Only a steel's name is held whole on the first line; a SHREE line,
    # which names just its type, breaks where it reads evenest.
    return ' '.join(x for x in (head, detail) if x), head if steel else ''


def challan_data(inv):
    co = inv['company']
    items = []
    for i, l in enumerate(inv['lines']):
        particulars, head = _challan_particulars(l)
        items.append(dict(
            sr=i + 1,
            particulars=particulars,
            # A steel from Challan Stock stays whole on the first line when
            # the name has to break (see bst/challan.py `_wrap2`).
            head=head,
            # Thickness and cutting size are not printed (21 Sep 2026): the
            # line keeps them, the pad gives their room to Particulars.
            unit=_s(l.get('unit')) or 'KGS',
            qty=_qty_of(l), rate=_f(l.get('rate')), amount=_f(l.get('taxable'))))

    # A challan raised today carries no tax, so printing one off a GST bill
    # drops the tax and the Amount column still adds up to the TOTAL.
    #
    # A firm set to No tax is a different case: its older documents -- the VAT
    # years, and purchases that came in with GST on them -- do carry tax, and
    # that tax is part of the total the party was charged. Dropping it there
    # would print a TOTAL that is not the document's own. So it is kept, and
    # shown on the round off row under its own name.
    tax = sum(_f(inv.get(k)) for k in ('cgst', 'sgst', 'igst', 'cess'))
    own_firm_untaxed = co.get('tax_mode') == 'none'
    round_off = _f(inv.get('round_off'))
    round_label = 'Round Off'
    if own_firm_untaxed and abs(tax) > 0.005:
        grand = round(_f(inv.get('grand_total')), 2)
        round_off = round(round_off + tax, 2)
        round_label = 'GST' if _f(inv.get('igst')) or _s(inv.get('eway_no')) \
            or (inv.get('invoice_date') or '') >= '2017-07-01' else 'VAT'
        if abs(_f(inv.get('round_off'))) > 0.005:
            round_label += ' + Round Off'
    else:
        grand = round(_f(inv.get('grand_total')) - tax, 2)
    # Each charge is named on its own row, the way the pad is printed.
    mob = _s(inv.get('party_phone'))
    terms = _terms(inv.get('payment_terms'))
    return dict(
        seller=dict(short_name=_s(co.get('short_name')) or _s(co.get('code')),
                    mobile_line=('Mo. No. : %s' % mob) if mob else '',
                    # The challan is signed with its own signature; the tax
                    # invoice keeps the one it already had.
                    signature='signature_challan.jpg'),
        # The same address the bill prints, not the city alone.
        buyer=dict(name=_s(inv.get('party_name')),
                   city=_s(inv.get('party_city')),
                   address=_buyer_address(inv)),
        challan=dict(number=api.docno(inv),
                     date=_date(inv.get('invoice_date')),
                     payment_terms=terms,
                     driver_name=_s(inv.get('driver_name')),
                     round_off_label=round_label),
        items=items,
        balances=dict(opening=_f(inv.get('party_opening')),
                      this_bill=_f(inv.get('party_amount')),
                      closing=_f(inv.get('party_closing'))),
        round_off=round_off, cutting=_f(inv.get('cutting')),
        transport=_f(inv.get('transport')), loading=_f(inv.get('loading')),
        grand_total=grand)


# -------------------------------------------------------------------- entry

def render(conn, cid, iid, kind=None, copies=2):
    """Return (pdf_bytes, filename) for one saved document."""
    if not available():
        raise PrintError(
            'The PDF stationery needs reportlab. Install it with '
            '"python3 -m pip install --user reportlab" and try again.', 500)
    from bst import build_invoice, build_challan
    # Pictures go into the file as binary rather than as ASCII85 text, which
    # ReportLab writes in pure Python: most of a bill's rendering time went
    # there, and the file comes out a fifth smaller. It looks the same.
    from reportlab import rl_config
    rl_config.useA85 = 0

    inv = api.get_invoice(conn, cid, iid)
    if not kind:
        kind = 'challan' if inv['company'].get('tax_mode') == 'none' else 'invoice'
    if kind == 'invoice' and inv.get('is_igst'):
        raise PrintError(
            'This bill is IGST and the printed stationery has only SGST and '
            'CGST columns. Use the browser print for it.')

    # The document number is typed by whoever raised it and ends up in a
    # Content-Disposition header, so only the characters a filename may hold
    # survive. server.py checks again on the way out.
    name = re.sub(r'[^A-Za-z0-9._-]', '', api.docno(inv).replace('/', '-')) \
        or str(iid)
    fd, path = tempfile.mkstemp(suffix='.pdf', prefix='bst-')
    os.close(fd)
    try:
        if kind == 'challan':
            build_challan(challan_data(inv), path, assets_dir=ASSETS,
                          copies=max(1, min(2, int(copies or 2))))
            fname = 'CHALLAN_%s.pdf' % name
        else:
            terms = ''
            if inv.get('from_challan'):
                row = conn.execute('SELECT payment_terms FROM invoice WHERE id=?',
                                   (inv['from_challan'],)).fetchone()
                terms = row[0] if row else ''
            build_invoice(invoice_data(inv, terms, _mobile(conn, inv)), path,
                          assets_dir=ASSETS)
            fname = 'INVOICE_%s.pdf' % name
        with open(path, 'rb') as fh:
            return fh.read(), fname
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --------------------------------------------------------- account statement

_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def statement(conn, cid, q):
    """Return (pdf_bytes, filename): one account between two dates.

    The customer is sent what the Accounts screen shows, entry by entry --
    `api.report_ledger` over the dates asked for, drawn the way SHREE prints
    its *Ledger of ...* (`statement_pdf`). Read-only: nothing is written.
    """
    if not available():
        raise PrintError(
            'The PDF needs reportlab. Install it with '
            '"python3 -m pip install --user reportlab" and try again.', 500)
    frm = _s(q.get('from'))
    to = _s(q.get('to'))
    import datetime
    if not to:
        to = datetime.date.today().isoformat()
    for label, v in (('From', frm), ('To', to)):
        if v and not _ISO.match(v):
            raise api.ApiError('%s is not a date: %s' % (label, v))
        if v:
            try:
                datetime.date.fromisoformat(v)
            except ValueError:
                raise api.ApiError('%s is not a date: %s' % (label, v))
    if frm and frm > to:
        raise api.ApiError('The From date is after the To date.')
    rep = api.report_ledger(conn, cid, {
        'ledger_id': q.get('ledger_id'), 'from': frm, 'to': to,
        'scope': q.get('scope') or ''})
    # The page is headed with the firm the account is kept in, as SHREE heads
    # its ledger -- the accounts book for a customer.
    held = [b['company_id'] for b in rep.get('in_books') or [] if b.get('is_account')]
    firm = api.get_company(conn, held[0] if held else rep['party']['company_id'])
    from reportlab import rl_config
    rl_config.useA85 = 0
    import statement_pdf
    blob = statement_pdf.build(rep, frm, to, dict(firm))
    who = re.sub(r'[^A-Za-z0-9 .&()-]+', ' ', rep['party']['name'] or '').strip()
    who = re.sub(r'\s+', ' ', who)[:60] or 'account'
    when = '%s to %s' % (_date(frm).replace('/', '-') if frm else 'start',
                         _date(to).replace('/', '-'))
    return blob, 'Statement %s %s.pdf' % (who, when)
