"""E-invoice (IRN) JSON, in the shape the NIC portal takes for bulk upload.

The portal will not take a bill; it takes a file. This turns a saved bill into
that file and says up front what the portal would have thrown back -- a buyer
with no GSTIN, a missing HSN, a firm with no PIN code. Nothing is transmitted:
the file is downloaded and uploaded by hand at einvoice1.gst.gov.in, and the
IRN and the signed QR come back from there.

The shape is schema 1.1, the one the bulk tool reads: an array of bills, each
with its own `TranDtls`, `DocDtls`, `SellerDtls`, `BuyerDtls`, `itemList` and
`ValDtls`. `EwbDtls` is the part that asks for the e-way bill to be raised
along with the IRN, and it is written **only when the bill has transport on
it** -- a vehicle, or a transporter. Left off, the portal returns an IRN alone
and the e-way bill can be raised later from app/eway.py. That is the whole
difference between the two files the same screen produces.

An e-invoice is a B2B document: the buyer must be registered, and the seller
must be a GST firm. A challan out of the order firm has no place here at all.
"""
import json
import re

import api
import eway

# The schema version the bulk tool reads. It goes in the file as a string.
VERSION = '1.1'

# Our document kinds, as the e-invoice schema names them. A purchase is
# somebody else's e-invoice to raise, and a challan is not one at all.
DOC_KIND = {'SALE': 'INV', 'CREDIT': 'CRN', 'DEBIT': 'DBN'}

# What kind of supply the bill is. Everything this business raises is B2B;
# the rest are here because the field will not take anything else, and a firm
# that starts exporting will need one of them.
SUPPLY_TYPES = [
    ('B2B', 'Business to business'),
    ('SEZWP', 'To an SEZ, with payment of tax'),
    ('SEZWOP', 'To an SEZ, without payment of tax'),
    ('EXPWP', 'Export, with payment of tax'),
    ('EXPWOP', 'Export, without payment of tax'),
    ('DEXP', 'Deemed export'),
]

# The portal will not read a number longer than this, and refuses one that
# opens with a slash, a hyphen or a nought.
_DOCNO_RE = re.compile(r'^[A-Za-z0-9/-]{1,16}$')
_DOCNO_BAD_START = ('0', '/', '-')

# An IRN has to be asked for within this many days of the bill's own date.
# The rule is on the larger turnovers only, so a bill past it is a warning
# rather than a refusal.
IRN_WINDOW_DAYS = 30


def _unit_price(value):
    """86.306 -> 86.306, to the three places the schema allows.

    SHREE cut this to two -- its own filed GT211 says 86.30 against a rate of
    86.306 -- and it looks harmless, the line's own amount being what carries
    the money. It is not. The portal checks that **Qty x UnitPrice comes to
    TotAmt**, within a rupee, and a rate cut at the second place fails that as
    soon as the weight is large: 1088 kg at 86.30 is 93,894.40 against the
    93,900.93 the line actually says. Over this firm's own last year, cutting
    put 419 of 1624 lines out by more than a rupee, the worst by 172.60;
    keeping three places leaves 12, every one of them a line carrying its
    share of the bill's cutting or freight, which is money and not rounding.
    """
    return api.r3(value)


def _days_between(iso_a, iso_b):
    """Whole days from one yyyy-mm-dd to another, or None if either is bad."""
    import datetime
    try:
        a = datetime.date(int(iso_a[0:4]), int(iso_a[5:7]), int(iso_a[8:10]))
        b = datetime.date(int(iso_b[0:4]), int(iso_b[5:7]), int(iso_b[8:10]))
    except (TypeError, ValueError, IndexError):
        return None
    return (b - a).days


def _phone(value):
    """Digits only, and only when there are between six and twelve of them.

    The portal takes a plain number; "0281-2360443" as it is written on the
    letterhead is refused for the hyphen, and a blank is better than a refusal
    since the field is optional.
    """
    digits = re.sub(r'\D', '', str(value or ''))
    return digits if 6 <= len(digits) <= 12 else None


def _line_discount(ln):
    """What the line's discount comes to in rupees.

    The books keep the discount as a percentage and the taxable value already
    net of it; the schema wants the gross and the discount side by side.
    """
    pct = float(ln.get('discount_pct') or 0)
    net = api.r2(float(ln.get('taxable') or 0))
    if pct <= 0 or pct >= 100:
        return 0.0, net
    gross = api.r2(net / (1.0 - pct / 100.0))
    return api.r2(gross - net), gross


def _other_charge(conn, cid, inv):
    """The part of the round-off that is not a rounding -- TCS, in practice.

    Tax collected at source under 206C(1H) is charged on the face of the bill,
    and the import had nowhere to put it: `round_off` is where a total that
    will not otherwise add up ends, so the two are there together. On the
    portal they are different things. `OthChrg` is added to the invoice value
    and not taxed, which is exactly what TCS is and what NIC asks for it;
    `RndOffAmt` holds paise, and a bill offering it 1,609.60 is refused.

    The split is read off the bill's own postings -- the TCS ledger as the
    firm posted it, the remainder left as the kasar it is -- so it is their
    figure rather than a guess. Where there is no posting to read and the
    round-off is plainly too big to be one, all of it goes as a charge, since
    the portal would refuse it where it stands.
    """
    rnd = api.r2(inv.get('round_off') or 0)
    rows = conn.execute(
        'SELECT l.name, p.debit, p.credit FROM posting p '
        'JOIN ledger l ON l.id = p.ledger_id '
        'WHERE p.company_id=? AND p.doc_kind=? AND p.doc_id=?',
        (cid, inv['doc_type'], inv['id'])).fetchall()
    tcs = api.r2(sum((r['credit'] or 0) - (r['debit'] or 0) for r in rows
                     if 'TCS' in (r['name'] or '').upper()))
    if not tcs and abs(rnd) > 99.99:
        tcs = rnd
    return tcs, api.r2(rnd - tcs)


# ------------------------------------------------------------- the payload

def build(conn, cid, inv, docno_style=eway.DOCNO_DEFAULT, with_ewb=None):
    """One e-invoice payload for one saved bill, plus what to look at.

    Returns `(payload, notes, hsn_sources)`. A note is `error` when the portal
    would refuse the bill, `warn` when it would take it but somebody should
    look, and `info` for what is merely worth knowing.

    `with_ewb` forces the e-way half on or off; left alone, it is written when
    the bill has a vehicle or a transporter on it.
    """
    notes = []
    co = inv.get('company') or api.get_company(conn, cid)
    doc_type = inv['doc_type']
    taxed = (co.get('tax_mode') or 'gst') != 'none'

    from_gstin = eway.gstin_of(co.get('gstin'))
    from_state = eway.state_no(co.get('state_code'), from_gstin, co.get('state'))
    to_gstin = eway.gstin_of(inv.get('party_gstin'))
    to_state = eway.state_no(inv.get('party_state_code'), to_gstin,
                             inv.get('party_state'))
    # Where the supply is taxed. It follows the buyer unless the bill says
    # otherwise, which is the same thing that decided IGST when it was saved.
    pos = eway.state_no('', '', inv.get('supply_state')) or to_state

    # ---- the goods
    items, hsn_sources = [], []
    for n, ln in enumerate(inv.get('lines') or [], 1):
        code, source = eway.hsn_of(conn, cid, ln)
        hsn_sources.append(dict(description=ln.get('description') or '',
                                hsn=code or '', source=source))
        by_weight = str(ln.get('by_weight', 1)) not in ('0', 'False')
        qty = float(ln.get('weight') or 0) if by_weight \
            else float(ln.get('qty') or 0)
        discount, gross = _line_discount(ln)
        # The bill's cutting, transport and loading are spread over the lines
        # and taxed with them, so each line carries its share -- otherwise the
        # items would not add up to the assessable value the portal checks.
        share = api.r2(float(ln.get('charge_share') or 0))
        tot = api.r2(gross + share)
        # The portal checks the rate against the amount, and a line carrying a
        # share of the bill's charges is worth more than its own rate says.
        # The share is taxed with the goods, so it cannot be moved to the
        # line's other charges, which the portal leaves untaxed -- the rate
        # carries it, and works out to what the customer paid a kilo.
        price = _unit_price(tot / qty) if (share and qty) \
            else _unit_price(ln.get('rate'))
        ass = api.r2(api.r2(float(ln.get('taxable') or 0)) + share)
        cgst = api.r2(ln.get('cgst') or 0)
        sgst = api.r2(ln.get('sgst') or 0)
        igst = api.r2(ln.get('igst') or 0)
        cess = api.r2(ln.get('cess') or 0)
        items.append({
            'SlNo': str(n),
            'prdDesc': eway._txt(ln.get('description'), 300),
            'IsServc': 'N',
            'hsnCd': str(code) if code else '',
            'barCd': None,
            'Qty': api.r3(qty),
            'FreeQty': 0,
            'Unit': eway.uqc(ln.get('unit')),
            'UnitPrice': price,
            'TotAmt': tot,
            'Discount': discount,
            'PreTaxVal': ass,
            'AssAmt': ass,
            'GstRt': api.r2(ln.get('gst_rate') or 0),
            'IgstAmt': igst,
            'CgstAmt': cgst,
            'SgstAmt': sgst,
            'CesRt': api.r2(ln.get('cess_rate') or 0),
            'CesAmt': cess,
            'CesNonAdvlAmt': 0.00,
            'StateCesRt': 0,
            'StateCesAmt': 0,
            'OthChrg': 0,
            'TotItemVal': api.r2(ass + cgst + sgst + igst + cess),
            'BchDtls': None,
        })

    # ---- the e-way half, only when there is something to move the goods on.
    # It is settled before the payload rather than added afterwards so the
    # fields come out in the order the filed bills have them.
    # The PIN both sides go up with. The IRP refuses a seller without one,
    # and on these books the PIN has been written at the end of the address
    # while the column beside it stayed empty -- so it is read the same way
    # the e-way file reads it, and for the same reason.
    seller_pin, seller_pin_src = eway.pin_of(
        co.get('pin'), co.get('address1'), co.get('address2'), co.get('city'))
    buyer_pin, buyer_pin_src = eway.pin_of(
        inv.get('party_pin'), inv.get('party_address1'),
        inv.get('party_address2'), inv.get('party_city'))

    vehicle = eway.clean_vehicle(inv.get('vehicle_no'))
    trans_gstin = eway.gstin_of(inv.get('transporter_gstin'))
    trans_name = eway._txt(inv.get('transporter'), 100)
    wants = with_ewb if with_ewb is not None else bool(
        vehicle or trans_gstin or trans_name)
    ewb = None
    if wants:
        # The portal reads the invoice date as the transport document date
        # where no lorry receipt was written, which is what the bills already
        # filed for this firm carry.
        ewb = {
            'TransId': trans_gstin or None,
            'TransName': trans_name or None,
            'TransMode': str(inv.get('transport_mode') or '').strip()
                         or ('1' if vehicle else None),
            'Distance': int(float(inv.get('transport_distance') or 0)),
            'TransDocNo': eway._txt(inv.get('lr_no'), 15) or None,
            'TransDocDt': eway._ddmmyyyy(inv.get('lr_date'))
                          or eway._ddmmyyyy(inv.get('invoice_date')),
            'VehNo': vehicle or None,
            'VehType': str(inv.get('vehicle_type') or 'R').strip().upper() or 'R',
        }

    # ---- the money
    other_chrg, round_off = _other_charge(conn, cid, inv)
    doc_no = eway.doc_number(inv, docno_style)
    payload = {
        'Version': VERSION,
        'TranDtls': {
            'TaxSch': 'GST',
            'SupTyp': 'B2B',
            # CGST and SGST inside one state is the ordinary case; IGST inside
            # one state happens only where the law asks for it, and the portal
            # wants to be told rather than to work it out.
            'IgstOnIntra': 'Y' if (inv.get('is_igst') and from_state
                                   and from_state == pos) else 'N',
            'RegRev': 'Y' if inv.get('rcm') else 'N',
            'EcmGstin': None,
        },
        'DocDtls': {
            'Typ': DOC_KIND.get(doc_type, 'INV'),
            'No': doc_no,
            'Dt': eway._ddmmyyyy(inv.get('invoice_date')),
        },
        'SellerDtls': {
            'Gstin': from_gstin,
            'LglNm': eway._txt(co.get('name'), 100),
            'TrdNm': eway._txt(co.get('short_name'), 100) or None,
            'Addr1': eway._txt(co.get('address1'), 100),
            'Addr2': eway._txt(co.get('address2'), 100) or None,
            'Loc': eway._txt(co.get('city'), 50),
            'Pin': seller_pin,
            'Stcd': str(from_state or ''),
            'Ph': _phone(co.get('phone') or co.get('mobile')),
            'Em': eway._txt(co.get('email'), 50) or None,
        },
        'BuyerDtls': {
            'Gstin': to_gstin,
            'LglNm': eway._txt(inv.get('party_name'), 100),
            'TrdNm': None,
            'Pos': str(pos or ''),
            'Addr1': eway._txt(inv.get('party_address1'), 100),
            'Addr2': eway._txt(inv.get('party_address2'), 100) or None,
            'Loc': eway._txt(inv.get('party_city'), 50),
            'Pin': buyer_pin,
            'Stcd': str(to_state or ''),
            'Ph': _phone(inv.get('party_phone')),
            'Em': eway._txt(inv.get('party_email'), 50) or None,
        },
        'DispDtls': None,
        'ShipDtls': None,
        'itemList': items,
        'ValDtls': {
            'AssVal': api.r2(sum(i['AssAmt'] for i in items)),
            'IgstVal': api.r2(inv.get('igst') or 0),
            'CgstVal': api.r2(inv.get('cgst') or 0),
            'SgstVal': api.r2(inv.get('sgst') or 0),
            'CesVal': api.r2(inv.get('cess') or 0),
            'StCesVal': 0,
            'Discount': api.r2(inv.get('discount_amt') or 0),
            'OthChrg': other_chrg,
            'RndOffAmt': round_off,
            'TotInvVal': api.r2(inv.get('grand_total') or 0),
        },
    }

    if ewb is not None:
        # Between ShipDtls and itemList, where the filed files have it.
        payload = dict(list(payload.items())[:7]
                       + [('EwbDtls', ewb)] + list(payload.items())[7:])

    _check(payload, inv, co, items, hsn_sources, taxed, doc_type, notes,
           (seller_pin_src, buyer_pin_src))
    return payload, notes, hsn_sources


def _line_notes(items, hsn_sources, notes):
    """What is wrong with the lines, said one line at a time."""
    for it, src in zip(items, hsn_sources):
        where = 'Line %s (%s)' % (it['SlNo'], (it['prdDesc'] or '?')[:30])
        if not it['hsnCd']:
            eway._note(notes, 'error', '%s has no HSN code. Put it on the item '
                                       'in the Items list.' % where)
        elif src['source'] in ('item name', 'description'):
            eway._note(notes, 'warn', '%s takes its HSN %s out of the %s -- '
                                      'worth confirming.'
                       % (where, it['hsnCd'], src['source']))
        elif len(it['hsnCd']) < 6:
            eway._note(notes, 'warn', '%s has a %d-digit HSN. Six digits are '
                                      'required above five crore turnover.'
                       % (where, len(it['hsnCd'])))
        if not it['Unit']:
            eway._note(notes, 'error', '%s has no unit the portal knows.' % where)
        if not it['Qty']:
            eway._note(notes, 'warn', '%s has no quantity.' % where)
        reckoned = api.r2(it['AssAmt'] + it['CgstAmt'] + it['SgstAmt']
                          + it['IgstAmt'] + it['CesAmt'])
        if abs(reckoned - it['TotItemVal']) >= 0.01:
            eway._note(notes, 'error', '%s comes to %.2f, not the %.2f it says.'
                       % (where, reckoned, it['TotItemVal']))
        # The portal works the tax out from the rate and checks it against the
        # amount. Bills from 2017 and 2018 carry the tax with the rate column
        # left at zero, which reads to the portal as tax charged at no rate.
        taxes = api.r2(it['CgstAmt'] + it['SgstAmt'] + it['IgstAmt'])
        due = api.r2(it['AssAmt'] * float(it['GstRt']) / 100.0)
        if abs(taxes - due) > 1:
            eway._note(notes, 'error', '%s is taxed %.2f, which is not %s%% of '
                                       '%.2f (%.2f). Put the rate on the line.'
                       % (where, taxes, ('%g' % it['GstRt']), it['AssAmt'], due))


def _check(p, inv, co, items, hsn_sources, taxed, doc_type, notes,
           pins=('column', 'column')):
    """Everything the portal would have thrown back, said in advance."""
    who = co.get('short_name') or co.get('code') or co.get('name') or 'this firm'
    seller, buyer = p['SellerDtls'], p['BuyerDtls']

    if not taxed:
        eway._note(notes, 'error', 'A challan is not an e-invoice. Make the '
                                   'GST bill from it and take the IRN on that.')
    if doc_type not in DOC_KIND:
        eway._note(notes, 'error', 'An e-invoice is raised by whoever sells. A '
                                   'purchase bill is the supplier\'s to file.')

    if not seller['Gstin']:
        eway._note(notes, 'error', '%s has no GSTIN. Settings -> Companies.' % who)
    else:
        eway._gstin_note(notes, who, seller['Gstin'])
    if not seller['Pin']:
        eway._note(notes, 'error', '%s has no 6-digit PIN code. Settings -> '
                                   'Companies.' % who)
    elif pins[0] == 'address':
        eway._note(notes, 'warn', '%s has no PIN code of its own, so %d was '
                                  'read out of its address. Put it in the PIN '
                                  'box under Settings -> Companies.'
                   % (who, seller['Pin']))
    if not seller['Addr1']:
        eway._note(notes, 'error', '%s has no address. Settings -> Companies.' % who)
    if not seller['Loc']:
        eway._note(notes, 'error', '%s has no city. Settings -> Companies.' % who)
    if not seller['Stcd']:
        eway._note(notes, 'error', '%s has no state. Settings -> Companies.' % who)

    party = inv.get('party_name') or 'The party'
    if not buyer['Gstin']:
        eway._note(notes, 'error', '%s has no GSTIN. An e-invoice is a B2B '
                                   'document -- an unregistered buyer cannot '
                                   'have one.' % party)
    else:
        eway._gstin_note(notes, party, buyer['Gstin'])
    if not buyer['Pin']:
        eway._note(notes, 'error', '%s has no 6-digit PIN code. Open the party '
                                   'and fill it in.' % party)
    elif pins[1] == 'address':
        eway._note(notes, 'warn', '%s has no PIN code of its own, so %d was '
                                  'read out of its address. Worth a look.'
                   % (party, buyer['Pin']))
    if not buyer['Addr1']:
        eway._note(notes, 'error', '%s has no address.' % party)
    if not buyer['Loc']:
        eway._note(notes, 'error', '%s has no city.' % party)
    if not buyer['Stcd']:
        eway._note(notes, 'error', '%s has no state.' % party)
    if not p['BuyerDtls']['Pos']:
        eway._note(notes, 'error', 'No place of supply on this bill.')

    no = p['DocDtls']['No']
    if not no:
        eway._note(notes, 'error', 'This bill has no number.')
    elif not _DOCNO_RE.match(no):
        eway._note(notes, 'error', 'The portal will not take the number "%s" '
                                   '-- letters, digits, / and - only, up to '
                                   '16. Set "Number on the portal" to whatever '
                                   'this bill is filed as in GSTR-1.' % no)
    elif no[0] in _DOCNO_BAD_START:
        eway._note(notes, 'error', 'The portal refuses a number starting with '
                                   '"%s" (%s).' % (no[0], no))
    if not p['DocDtls']['Dt']:
        eway._note(notes, 'error', 'This bill has no readable date.')
    else:
        import datetime
        today = datetime.date.today().isoformat()
        old = _days_between(inv.get('invoice_date') or '', today)
        if old is not None and old < 0:
            eway._note(notes, 'error', 'The bill is dated ahead of today; the '
                                       'portal takes no future date.')
        elif old is not None and old > IRN_WINDOW_DAYS:
            eway._note(notes, 'warn', 'The bill is %d days old. Above ten '
                                      'crore turnover the portal refuses an '
                                      'IRN more than %d days after the bill.'
                       % (old, IRN_WINDOW_DAYS))

    if not items:
        eway._note(notes, 'error', 'This bill has no lines.')
    _line_notes(items, hsn_sources, notes)

    v = p['ValDtls']
    lines_total = api.r2(sum(i['AssAmt'] for i in items))
    if abs(lines_total - v['AssVal']) >= 0.01:
        eway._note(notes, 'error', 'The lines come to %.2f but the assessable '
                                   'value is %.2f.' % (lines_total, v['AssVal']))
    for label, header, from_lines in (
            ('CGST', v['CgstVal'], api.r2(sum(i['CgstAmt'] for i in items))),
            ('SGST', v['SgstVal'], api.r2(sum(i['SgstAmt'] for i in items))),
            ('IGST', v['IgstVal'], api.r2(sum(i['IgstAmt'] for i in items)))):
        if abs(header - from_lines) >= 0.01:
            eway._note(notes, 'error', '%s on the lines comes to %.2f but the '
                                       'bill says %.2f.'
                       % (label, from_lines, header))
    reckoned = api.r2(v['AssVal'] + v['CgstVal'] + v['SgstVal'] + v['IgstVal']
                      + v['CesVal'] + v['OthChrg'] + v['RndOffAmt'])
    if abs(reckoned - v['TotInvVal']) >= 0.01:
        eway._note(notes, 'error', 'The values do not add up: %.2f against a '
                                   'total of %.2f.' % (reckoned, v['TotInvVal']))
    if abs(v['RndOffAmt']) > 99.99:
        eway._note(notes, 'error', 'Round off is %.2f. The portal takes paise '
                                   'there, not rupees -- anything charged on '
                                   'the bill belongs in other charges.'
                   % v['RndOffAmt'])
    elif abs(v['RndOffAmt']) > 0.99:
        eway._note(notes, 'warn', 'Round off is %.2f, which is more than a '
                                  'rounding. The portal allows up to 99 paise.'
                   % v['RndOffAmt'])
    if v['OthChrg']:
        eway._note(notes, 'info', 'Other charges of %.2f go up untaxed -- the '
                                  'TCS on this bill.' % v['OthChrg'])

    # The portal reads the two state codes and works the tax out for itself.
    if taxed and seller['Stcd'] and p['BuyerDtls']['Pos']:
        same = seller['Stcd'] == p['BuyerDtls']['Pos']
        if inv.get('is_igst') and same and p['TranDtls']['IgstOnIntra'] == 'N':
            eway._note(notes, 'error', 'The bill charges IGST but both sides '
                                       'are in state %s, where the portal '
                                       'expects CGST and SGST.' % seller['Stcd'])
        elif not inv.get('is_igst') and not same:
            eway._note(notes, 'error', 'The bill charges CGST and SGST but the '
                                       'sides are in states %s and %s, where '
                                       'the portal expects IGST.'
                       % (seller['Stcd'], p['BuyerDtls']['Pos']))

    ewb = p.get('EwbDtls')
    if ewb is None:
        eway._note(notes, 'info', 'No transport on this bill, so it asks for '
                                  'the IRN alone. Fill in the vehicle to have '
                                  'the e-way bill raised with it.')
    else:
        if not ewb['VehNo'] and not ewb['TransId']:
            eway._note(notes, 'error', 'The e-way half needs a vehicle number '
                                       'or a transporter GSTIN. Clear the '
                                       'transport boxes to ask for the IRN '
                                       'alone.')
        if ewb['VehNo'] and not eway._VEHICLE_RE.match(ewb['VehNo']):
            # The e-way half is checked by the same people either way round,
            # so this is refused here exactly as `eway.py` refuses it.
            eway._note(notes, 'error', 'Vehicle number "%s" is not a vehicle '
                                       'number (GJ03BY9139).' % ewb['VehNo'])
        if ewb['TransId'] and not eway.gstin_check_digit(ewb['TransId']):
            eway._note(notes, 'error', 'The transporter GSTIN "%s" is not '
                                       'valid.' % ewb['TransId'])
        if not ewb['Distance']:
            eway._note(notes, 'info', 'Distance left at 0 -- the portal works '
                                      'it out from the two PIN codes.')


# --------------------------------------------------------------- the files

def preview(conn, cid, iid, style=None, with_ewb=None):
    """One bill, ready to look at on screen before it is downloaded."""
    style = eway.docno_style(conn, style)
    inv = api.get_invoice(conn, cid, iid)
    payload, notes, sources = build(conn, cid, inv, style, with_ewb)
    # The lorry receipt goes back as the bill has it, not as `TransDocDt` has
    # it: that falls back to the invoice date, which on screen would read as
    # an LR date somebody wrote.
    return dict(invoice_id=iid, company_id=cid, doc_no=payload['DocDtls']['No'],
                doc_date=inv['invoice_date'], party=inv.get('party_name') or '',
                grand_total=api.r2(inv.get('grand_total') or 0),
                lr_no=inv.get('lr_no') or '', lr_date=inv.get('lr_date') or '',
                with_ewb='EwbDtls' in payload,
                payload=payload, notes=notes, hsn=sources,
                errors=sum(1 for n in notes if n['level'] == 'error'),
                warnings=sum(1 for n in notes if n['level'] == 'warn'))


def preview_many(conn, spec, style=None):
    docs = [preview(conn, cid, iid, style)
            for cid, iid in eway._refs(conn, spec)]
    return dict(count=len(docs), documents=docs,
                errors=sum(d['errors'] for d in docs),
                warnings=sum(d['warnings'] for d in docs),
                with_ewb=sum(1 for d in docs if d['with_ewb']),
                total=api.r2(sum(d['grand_total'] for d in docs)))


def bundle(conn, spec, force=False, style=None):
    """The file itself: a JSON array, which is what the portal uploads."""
    docs = [preview(conn, cid, iid, style)
            for cid, iid in eway._refs(conn, spec)]
    bad = [d for d in docs if d['errors']]
    if bad and not force:
        raise api.ApiError(
            '%d of %d bill(s) would be refused by the portal. Put those right '
            'first, or download anyway.' % (len(bad), len(docs)),
            400, dict(einvoice_errors=[dict(doc_no=d['doc_no'],
                                            notes=[n['text'] for n in d['notes']
                                                   if n['level'] == 'error'])
                                       for d in bad]))
    text = json.dumps([d['payload'] for d in docs], indent=1)
    if len(docs) == 1:
        name = 'einvoice-%s.json' % re.sub(r'[^A-Za-z0-9]+', '-',
                                           docs[0]['doc_no']).strip('-')
    else:
        name = 'einvoice-%d-bills.json' % len(docs)
    return text, name
