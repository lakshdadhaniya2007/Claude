"""E-way bill JSON, in the shape the NIC portal expects for bulk upload.

The portal will not take a bill; it takes a file. This turns a saved document
into that file and, just as importantly, says up front what the portal would
have rejected -- a missing HSN, a party with no PIN code, a vehicle number in
the wrong shape. Nothing is transmitted: the file is downloaded and uploaded
by hand at ewaybillgst.gov.in.

The shape is not guessed. SHREE -- the old software -- moved e-way bills two
ways, and this writes both:

  bulk  what SHREE's own copy of the NIC "EWB Bulk Generation Tool"
        (FORMATS/ewayblank.xlsm) writes when its Generate JSON button is
        pressed: one object holding `version` and a `billLists` array, with
        that tool's own spelling of the field names -- `actualFromStateCode`,
        `transType`, `OthValue`, `TotNonAdvolVal`, `mainHsnCode`, and an
        `hsnCode` that is quoted while every other number is not. This is the
        file the portal has been taking for years, so it is the default.

  api   the shape SHREE's ShreeEwayBill.dll posts straight to the NIC API,
        which names the same fields `actFromStateCode`, `transactionType`,
        `otherValue` and `cessNonAdvolValue`. Kept for when the portal is
        fed through something that speaks the API spelling instead.

The unit codes, state codes and document types below are the ones off that
workbook's "Master Codes" sheet, so a bill cannot be written with a code the
tool would not have produced.
"""
import json
import re

import api

# --------------------------------------------------------------- the codes

# Why the goods are moving. The portal calls this the sub supply type.
SUB_SUPPLY = [
    ('1', 'Supply'), ('2', 'Import'), ('3', 'Export'), ('4', 'Job Work'),
    ('5', 'For Own Use'), ('6', 'Job Work Returns'), ('7', 'Sales Return'),
    ('8', 'Others'), ('9', 'SKD/CKD/Lots'), ('10', 'Line Sales'),
    ('11', 'Recipient Not Known'), ('12', 'Exhibition or Fairs'),
]

TRANS_MODES = [('1', 'Road'), ('2', 'Rail'), ('3', 'Air'), ('4', 'Ship')]

VEHICLE_TYPES = [('R', 'Regular'), ('O', 'Over Dimensional Cargo')]

TRANSACTION_TYPES = [
    ('1', 'Regular'), ('2', 'Bill To - Ship To'),
    ('3', 'Bill From - Dispatch From'), ('4', 'Combination of 2 and 3'),
]

# Our document kinds, as the portal names them. The tool's own pick list is
# Tax Invoice / Bill of Supply / Bill of Entry / Delivery Challan / Others,
# and it has no note at all -- a credit note goes as Others.
DOC_TYPES = [('INV', 'Tax Invoice'), ('BIL', 'Bill of Supply'),
             ('BOE', 'Bill of Entry'), ('CHL', 'Delivery Challan'),
             ('OTH', 'Others')]
DOC_KIND = {'SALE': 'INV', 'PURCHASE': 'INV', 'CREDIT': 'OTH', 'DEBIT': 'OTH'}
DOC_KIND_NOTAX = 'CHL'          # a challan carries no tax

# The two files this writes, and the wrapper the bulk one goes in. The version
# is the workbook's own -- it is written into every file it makes, and the
# portal reads it to know which spelling it is being handed.
SCHEMAS = [('bulk', 'Bulk tool file (ewayblank.xlsm)'),
           ('api', 'NIC API (ShreeEwayBill.dll)')]
BULK_VERSION = '1.0.0621'

# Which way the goods go. A sales return comes back in, a purchase return
# goes back out.
SUPPLY_DIR = {'SALE': 'O', 'DEBIT': 'O', 'PURCHASE': 'I', 'CREDIT': 'I'}

# Sub supply type that fits the document when nothing has been chosen.
SUB_SUPPLY_DEFAULT = {'SALE': '1', 'PURCHASE': '1',
                      'CREDIT': '7', 'DEBIT': '7'}

# A document number goes onto the portal as letters, digits, and . / - only,
# so "GT 188" -- which is how the bill prints and how the series has been
# written for thirteen thousand bills -- has to become something. Which
# something is not ours to decide: whatever goes here must match what is
# filed in GSTR-1, so it is asked once and remembered.
#
# The one thing that is not a choice is the space. The workbook checks the
# number against " a-zA-Z-/0-9." and then runs RemoveWhiteSpace over it, so
# every number SHREE ever filed went up joined whatever the bill printed --
# which is why "As printed" says what it does below.
DOCNO_STYLES = [
    ('plain', 'As printed, spaces dropped (GT188)'),
    ('hyphen', 'Hyphen (GT-188)'),
    ('joined', 'Joined (GT188)'),
]
DOCNO_DEFAULT = 'plain'


def doc_number(inv, style=DOCNO_DEFAULT):
    series = (inv.get('series') or '').strip()
    number = str(inv.get('invoice_no') or '').strip()
    if not series:
        return re.sub(r'\s', '', number)
    if style == 'hyphen':
        joined = '%s-%s' % (series, number)
    elif style == 'joined':
        joined = '%s%s' % (series, number)
    else:
        joined = '%s %s' % (series, number)
    return re.sub(r'\s', '', joined)


# An e-way bill is only required once the consignment passes this much.
THRESHOLD = 50000.0

# The firm a challan's e-way bill goes up under.
#
# Orders and challans are written in a no-tax book, and that book is not a
# legal entity: it has no GSTIN, no PIN and no address, and never will. The
# goods on a challan still move, and over the threshold they still need an
# e-way bill -- which the portal will only take under a real GSTIN. Which
# real firm it is is not a guess: it is the firm that bills that customer,
# which the books already record on the party. This setting answers only for
# the customers whose billing firm history never settled.
CONSIGNOR_KEY = 'eway_consignor'

# The portal takes only these unit codes (UQC). The left column is what the
# books have been written with over the years, including SHREE's four-letter
# truncations.
# Every code the workbook's "Master Codes" sheet offers, keyed by its own
# spelt-out name. Anything not on this list the portal does not know.
UQC_NAMES = [
    ('BAG', 'BAGS'), ('BAL', 'BALE'), ('BDL', 'BUNDLES'), ('BKL', 'BUCKLES'),
    ('BOU', 'BILLION OF UNITS'), ('BOX', 'BOX'), ('BTL', 'BOTTLES'),
    ('BUN', 'BUNCHES'), ('CAN', 'CANS'), ('CBM', 'CUBIC METERS'),
    ('CCM', 'CUBIC CENTIMETERS'), ('CMS', 'CENTI METERS'), ('CTN', 'CARTONS'),
    ('DOZ', 'DOZENS'), ('DRM', 'DRUMS'), ('GGK', 'GREAT GROSS'),
    ('GMS', 'GRAMMES'), ('GRS', 'GROSS'), ('GYD', 'GROSS YARDS'),
    ('KGS', 'KILOGRAMS'), ('KLR', 'KILOLITRE'), ('KME', 'KILOMETRE'),
    ('LTR', 'LITRES'), ('MLT', 'MILILITRE'), ('MTR', 'METERS'),
    ('MTS', 'METRIC TON'), ('NOS', 'NUMBERS'), ('OTH', 'OTHERS'),
    ('PAC', 'PACKS'), ('PCS', 'PIECES'), ('PRS', 'PAIRS'), ('QTL', 'QUINTAL'),
    ('ROL', 'ROLLS'), ('SET', 'SETS'), ('SQF', 'SQUARE FEET'),
    ('SQM', 'SQUARE METERS'), ('SQY', 'SQUARE YARDS'), ('TBS', 'TABLETS'),
    ('TGM', 'TEN GROSS'), ('THD', 'THOUSANDS'), ('TON', 'TONNES'),
    ('TUB', 'TUBES'), ('UGS', 'US GALLONS'), ('UNT', 'UNITS'),
    ('YDS', 'YARDS'),
]

# What the books have actually been written with over the years -- SHREE's
# four-letter truncations, the shorthand typed at the counter -- mapped onto
# those codes. Every code is its own key too, and so is its full name.
UQC = {code: code for code, _ in UQC_NAMES}
UQC.update({name: code for code, name in UQC_NAMES})
UQC.update({
    'KG': 'KGS', 'KILO': 'KGS', 'KILOS': 'KGS', 'KILOGRAM': 'KGS',
    'TONNE': 'TON', 'TONNE.': 'TON', 'MT': 'MTS', 'MTS.': 'MTS',
    'M TON': 'MTS', 'METRIC TONNE': 'MTS', 'QUINTALS': 'QTL',
    'NO': 'NOS', 'NO.': 'NOS', 'NUMBER': 'NOS', 'PC': 'PCS',
    'PIECE': 'PCS', 'S PI': 'PCS', 'SHEE': 'NOS', 'SHEET': 'NOS',
    'SHEETS': 'NOS', 'UNIT': 'UNT', 'PAIR': 'PRS', 'BUNDLE': 'BDL',
    'ROLL': 'ROL', 'METER': 'MTR', 'METRE': 'MTR', 'METRES': 'MTR',
    'LITRE': 'LTR', 'LITER': 'LTR', 'SQ FEET': 'SQF', 'SQ FT': 'SQF',
    'SQFT': 'SQF', 'SQ MTR': 'SQM', 'CUM': 'CBM', 'DOZEN': 'DOZ',
    'DRUM': 'DRM', 'CARTON': 'CTN', 'PACK': 'PAC', 'BOTTLE': 'BTL',
    'OTHER': 'OTH',
})
UQC_FALLBACK = 'OTH'

# A GSTIN's first two digits are the state, so a party's state code is worth
# more from the GSTIN than from a column somebody may not have filled in.
_HSN_RE = re.compile(r'(?<!\d)(\d{8}|\d{6}|\d{4})(?!\d)')
_PIN_RE = re.compile(r'(?<!\d)([1-9]\d{5})(?!\d)')
# The shapes the portal takes a vehicle number in: an ordinary registration
# (GJ03BY9139), a defence one (12AB123456A) and a temporary one (GJ123456).
# Anything else it refuses, and it has been refused here since 31 Aug 2026 --
# the books hold "GJ" and "NAGDANGADI" in that box, the second of them a
# transporter's name typed where the lorry goes.
_VEHICLE_RE = re.compile(r'^(?:[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{4}'
                         r'|[0-9]{2}[A-Z]{2}[0-9]{6}[A-Z]'
                         r'|[A-Z]{2}[0-9]{6})$')
_DOCNO_RE = re.compile(r'^[A-Za-z0-9./-]{1,16}$')


def gstin_of(value):
    """Normalised, never raising -- a GSTIN that is wrong is a note here, not
    a dead end. The one in `api` throws, which is right on save and wrong
    here: the whole point is to report what is wrong, one bill at a time.
    """
    return re.sub(r'\s', '', str(value or '')).upper()


_GSTIN_CODE = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'


def gstin_check_digit(gstin):
    """Whether the last character is the one the GST check digit works out to.

    A GSTIN carries its own check digit, so a number that was mistyped can be
    told from a real one without asking the portal -- which matters, because
    the portal refuses it with nothing more helpful than a code. Eighteen of
    the 1,926 party numbers in these books fail it, and every one of them is
    an I typed for a 1 or an O for a 0: "24AINPA6403AIZO" against the
    "24AINPA6403A1Z0" the customer actually holds.
    """
    g = (gstin or '').strip().upper()
    if not api.GSTIN_RE.match(g):
        return False
    total = 0
    for i, ch in enumerate(g[:14]):
        v = _GSTIN_CODE.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return _GSTIN_CODE[(36 - total % 36) % 36] == g[14]


def _gstin_note(notes, who, gstin):
    """The one complaint both files make about a GSTIN, said once."""
    if not api.GSTIN_RE.match(gstin):
        _note(notes, 'error', '%s has a GSTIN the portal will not read: %s'
              % (who, gstin))
    elif not gstin_check_digit(gstin):
        _note(notes, 'error', '%s has a GSTIN that fails its own check digit: '
                              '%s. Look for an I typed for a 1 or an O for a 0.'
              % (who, gstin))


def rupees(amount):
    """1,66,027 -- lakhs and crores, the way the rest of the app writes it."""
    whole = '%d' % int(round(abs(float(amount or 0))))
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ','.join(parts + [tail])
    return ('-' if float(amount or 0) < 0 else '') + u'\u20b9' + whole


def _note(bag, level, text):
    bag.append(dict(level=level, text=text))


def _ddmmyyyy(iso):
    """2026-08-27 -> 27/08/2026, which is the only date the portal reads."""
    s = (iso or '').strip()[:10]
    if len(s) == 10 and s[4] == '-' and s[7] == '-':
        return '%s/%s/%s' % (s[8:10], s[5:7], s[0:4])
    return ''


def _txt(value, cap):
    return (str(value or '').strip())[:cap]


def pin_of(value, *addresses):
    """The 6-digit PIN, and where it was found.

    The column is read first. Failing that the address lines are, because a
    PIN has been written at the end of a letterhead -- "Mavdi Main Road,
    Rajkot-360004" -- for as long as the column beside it has stood empty,
    and the portal refuses a consignor with no PIN. One unfilled column
    otherwise stops every bill a firm sends, which is far more than that
    field is worth. A PIN lifted out of an address is a guess in the same way
    an HSN lifted out of an item name is, and says so.

    The last one in the address wins: a PIN goes at the end of an address,
    and the lookarounds keep the six digits from being cut out of a phone
    number or a longer run of figures.
    """
    d = re.sub(r'\D', '', str(value or ''))
    if len(d) == 6:
        return int(d), 'column'
    found = _PIN_RE.findall(' '.join(str(t or '') for t in addresses))
    if found:
        return int(found[-1]), 'address'
    return 0, 'missing'


def state_no(state_code, gstin='', state_name=''):
    """State code as the portal wants it -- a number, not a padded string."""
    g = re.sub(r'\s', '', str(gstin or '')).upper()
    if len(g) >= 2 and g[:2].isdigit() and g[:2] != '00':
        return int(g[:2])
    d = re.sub(r'\D', '', str(state_code or ''))
    if d and d != '0':
        return int(d)
    want = str(state_name or '').strip().lower()
    for code, name in api.STATES:
        if name.lower() == want:
            return int(code)
    return 0


def uqc(unit):
    key = str(unit or '').strip().upper()
    return UQC.get(key, UQC.get(key.rstrip('.'), '' if not key else UQC_FALLBACK))


def clean_vehicle(value):
    return re.sub(r'[^A-Z0-9]', '', str(value or '').upper())


def hsn_of(conn, cid, line):
    """The line's HSN, and where it was found.

    Years of bills were written with the HSN typed into the item's *name*
    ("CRCA 72091510") and the HSN column left empty, so the name is read as a
    last resort. Where each code came from is reported alongside it -- a code
    lifted out of a name is a guess and deserves to be looked at.
    """
    direct = re.sub(r'\D', '', str(line.get('hsn') or ''))
    if len(direct) in (4, 6, 8):
        return int(direct), 'line'

    if line.get('item_id'):
        row = api.one(conn.execute(
            'SELECT hsn, name FROM item WHERE id=? AND company_id=?',
            (line['item_id'], cid))) or {}
        from_item = re.sub(r'\D', '', str(row.get('hsn') or ''))
        if len(from_item) in (4, 6, 8):
            return int(from_item), 'item master'
        found = _HSN_RE.findall(str(row.get('name') or ''))
        if found:
            return int(max(found, key=len)), 'item name'

    found = _HSN_RE.findall(str(line.get('description') or ''))
    if found:
        return int(max(found, key=len)), 'description'
    return 0, 'missing'


def consignor_of(conn, cid, inv, book):
    """The firm the e-way bill goes up under, and how that was decided.

    A GST firm sends under its own name and there is nothing to decide. A
    challan cannot: the book it is written in has no GSTIN. The firm whose
    lorry it actually is, is the firm that bills the customer, so that is the
    one used -- the customer's billing firm where the books record one, and
    the firm picked on the e-way screen for the customers where they do not.

    Only the consignor changes. The document stays a delivery challan with no
    tax on it, because that is what it is; what firm it goes up under does not
    make it a sale.
    """
    if (book.get('tax_mode') or 'gst') != 'none':
        return book, 'own'
    firms = {f['id'] for f in api.billing_firms(conn)}
    home = inv.get('party_home_company') or inv.get('bill_company')
    if home in firms:
        return api.get_company(conn, home), 'billing firm'
    import db
    chosen = api._meta_int(db.get_meta(conn, CONSIGNOR_KEY))
    if chosen in firms:
        return api.get_company(conn, chosen), 'chosen'
    return book, 'none'


def consignor_id(conn, asked=None):
    """The firm challans go up under when the customer's own is not known."""
    import db
    want = api._meta_int(asked if asked not in (None, '') else
                         db.get_meta(conn, CONSIGNOR_KEY))
    firms = {f['id'] for f in api.billing_firms(conn)}
    return want if want in firms else None


# ------------------------------------------------------------- the payload

def build(conn, cid, inv, schema='bulk', docno_style=DOCNO_DEFAULT):
    """One e-way bill payload for one saved document, plus what to look at.

    Returns `(payload, notes)`. A note is `error` when the portal would refuse
    the row, `warn` when it would take it but somebody should look, and `info`
    for what is merely worth knowing.
    """
    notes = []
    # Two different firms, and only for a challan are they the same question
    # asked twice. The *book* the document is written in decides whether it
    # carries tax and what kind of document it is; the *consignor* decides
    # whose GSTIN the goods move under. A GST bill answers both with itself.
    book = inv.get('company') or api.get_company(conn, cid)
    co, consignor = consignor_of(conn, cid, inv, book)
    doc_type = inv['doc_type']
    taxed = (book.get('tax_mode') or 'gst') != 'none'

    # ---- who is sending, and from where
    from_gstin = gstin_of(co.get('gstin'))
    from_state = state_no(co.get('state_code'), from_gstin, co.get('state'))
    from_pin, from_pin_src = pin_of(co.get('pin'), co.get('address1'),
                                    co.get('address2'), co.get('city'))
    to_gstin = gstin_of(inv.get('party_gstin'))
    registered = bool(to_gstin)
    to_state = state_no(inv.get('party_state_code'), to_gstin,
                        inv.get('party_state'))
    to_pin, to_pin_src = pin_of(inv.get('party_pin'),
                                inv.get('party_address1'),
                                inv.get('party_address2'),
                                inv.get('party_city'))

    # ---- the goods
    items, hsn_sources = [], []
    for ln in inv.get('lines') or []:
        code, source = hsn_of(conn, cid, ln)
        hsn_sources.append(dict(description=ln.get('description') or '',
                                hsn=code or '', source=source))
        by_weight = str(ln.get('by_weight', 1)) not in ('0', 'False')
        qty = float(ln.get('weight') or 0) if by_weight \
            else float(ln.get('qty') or 0)
        unit = uqc(ln.get('unit'))
        # A challan carries no tax, whatever rate the line was written with.
        # Rates on the items with nothing in the tax totals is a disagreement
        # the portal works out for itself and refuses.
        rate = float(ln.get('gst_rate') or 0) if taxed else 0.0
        cess_rate = float(ln.get('cess_rate') or 0) if taxed else 0.0
        # The bill's cutting / transport / loading are spread over the lines
        # and taxed with them, so each line carries its share here too --
        # otherwise the item total would not add up to the taxable value.
        taxable = api.r2(float(ln.get('taxable') or 0)
                         + float(ln.get('charge_share') or 0))
        detail = ' '.join(str(ln.get(k) or '').strip() for k in
                          ('description', 'item_detail', 'thickness',
                           'cutting_size')).strip()
        items.append({
            'itemNo': len(items) + 1,
            'productName': _txt(ln.get('description'), 100),
            'productDesc': _txt(detail or ln.get('description'), 100),
            'hsnCode': code,
            'quantity': api.r3(qty),
            'qtyUnit': unit,
            'cgstRate': 0.0 if inv.get('is_igst') else api.r2(rate / 2.0),
            'sgstRate': 0.0 if inv.get('is_igst') else api.r2(rate / 2.0),
            'igstRate': api.r2(rate) if inv.get('is_igst') else 0.0,
            'cessRate': api.r2(cess_rate),
            'cessNonAdvol': 0.0,
            'taxableAmount': taxable,
        })

    # ---- the money. otherValue carries the round off so the portal's own
    # check -- total + taxes + other = invoice value -- comes out even.
    taxable_total = api.r2(inv.get('taxable') or 0)
    other = api.r2(inv.get('round_off') or 0)
    if not taxed:
        # A challan is priced but not taxed, and its charges sit outside the
        # taxable value rather than inside it.
        taxable_total = api.r2(float(inv.get('taxable') or 0))
        other = api.r2(float(inv.get('cutting') or 0)
                       + float(inv.get('transport') or 0)
                       + float(inv.get('loading') or 0)
                       + float(inv.get('round_off') or 0))

    # ---- transport
    vehicle = clean_vehicle(inv.get('vehicle_no'))
    trans_gstin = gstin_of(inv.get('transporter_gstin'))
    distance = int(float(inv.get('transport_distance') or 0))
    mode = str(inv.get('transport_mode') or '').strip() or ('1' if vehicle else '')
    sub_supply = str(inv.get('sub_supply_type') or '').strip() \
        or SUB_SUPPLY_DEFAULT.get(doc_type, '1')

    doc_no = doc_number(inv, docno_style)

    # The one HSN that stands for the consignment. The workbook means the
    # code on the line worth the most -- its own loop reaches for that and
    # then trips over a variable it never sets, so what it actually writes is
    # whichever line came last. The intent is what is followed here.
    main_hsn = 0
    if items:
        main_hsn = max(items, key=lambda i: i['taxableAmount'])['hsnCode']

    # One bill, under the names the NIC API uses. The bulk workbook spells
    # four of them differently and `_render` puts that right on the way out.
    bill = {
        'userGstin': from_gstin,
        'supplyType': SUPPLY_DIR.get(doc_type, 'O'),
        'subSupplyType': sub_supply,
        'subSupplyDesc': _txt(inv.get('sub_supply_desc'), 20)
                         if sub_supply == '8' else '',
        'docType': DOC_KIND_NOTAX if not taxed else DOC_KIND.get(doc_type, 'INV'),
        'docNo': doc_no,
        'docDate': _ddmmyyyy(inv.get('invoice_date')),

        'fromGstin': from_gstin,
        'fromTrdName': _txt(co.get('name'), 100),
        'fromAddr1': _txt(co.get('address1'), 120),
        'fromAddr2': _txt(co.get('address2'), 120),
        'fromPlace': _txt(co.get('city'), 50),
        'fromPincode': from_pin,
        'fromStateCode': from_state,

        'toGstin': to_gstin or 'URP',
        'toTrdName': _txt(inv.get('party_name'), 100),
        'toAddr1': _txt(inv.get('party_address1'), 120),
        'toAddr2': _txt(inv.get('party_address2'), 120),
        'toPlace': _txt(inv.get('party_city'), 50),
        'toPincode': to_pin,
        'toStateCode': to_state,

        'transactionType': int(str(inv.get('transaction_type') or '1') or 1),
        'otherValue': other,
        'totalValue': taxable_total,
        'cgstValue': api.r2(inv.get('cgst') or 0),
        'sgstValue': api.r2(inv.get('sgst') or 0),
        'igstValue': api.r2(inv.get('igst') or 0),
        'cessValue': api.r2(inv.get('cess') or 0),
        'cessNonAdvolValue': 0.0,
        'totInvValue': api.r2(inv.get('grand_total') or 0),

        'transporterId': trans_gstin,
        'transporterName': _txt(inv.get('transporter'), 100),
        'transDocNo': _txt(inv.get('lr_no'), 15),
        'transDocDate': _ddmmyyyy(inv.get('lr_date')),
        'transMode': mode,
        'transDistance': distance,
        'vehicleNo': vehicle,
        'vehicleType': str(inv.get('vehicle_type') or 'R').strip().upper() or 'R',
        'mainHsnCode': main_hsn,
        'itemList': items,
    }
    # The actual place of dispatch and delivery. They only differ from the
    # GSTIN states on a Bill To - Ship To, which is what transactionType says.
    bill['actFromStateCode'] = from_state
    bill['actToStateCode'] = to_state

    _check(bill, inv, co, items, hsn_sources, registered, taxed, notes,
           consignor, (from_pin_src, to_pin_src))
    return _render(bill, schema), notes, hsn_sources


# The bulk workbook's own spelling of the four fields it does not share with
# the API, in the order it writes them.
_BULK_ORDER = [
    'userGstin', 'supplyType', 'subSupplyType', 'subSupplyDesc', 'docType',
    'docNo', 'docDate', 'transType',
    'fromGstin', 'fromTrdName', 'fromAddr1', 'fromAddr2', 'fromPlace',
    'fromPincode', 'fromStateCode', 'actualFromStateCode',
    'toGstin', 'toTrdName', 'toAddr1', 'toAddr2', 'toPlace', 'toPincode',
    'toStateCode', 'actualToStateCode',
    'totalValue', 'cgstValue', 'sgstValue', 'igstValue', 'cessValue',
    'TotNonAdvolVal', 'OthValue', 'totInvValue',
    'transMode', 'transDistance', 'transporterName', 'transporterId',
    'transDocNo', 'transDocDate', 'vehicleNo', 'vehicleType',
    'mainHsnCode', 'itemList',
]
_BULK_ITEM_ORDER = ['itemNo', 'productName', 'productDesc', 'hsnCode',
                    'quantity', 'qtyUnit', 'taxableAmount', 'sgstRate',
                    'cgstRate', 'igstRate', 'cessRate', 'cessNonAdvol']


def _render(bill, schema):
    """The bill in the spelling the chosen file uses.

    `api` is what SHREE's own ShreeEwayBill.dll posts. `bulk` is what its
    copy of the NIC workbook writes, down to the field order, the numbers
    that go unquoted, and the one -- hsnCode -- that does not.
    """
    if schema == 'api':
        out = dict(bill)
        out['transMode'] = str(bill['transMode'] or '')
        out['transDistance'] = str(bill['transDistance'])
        out.pop('mainHsnCode', None)
        return out

    b = dict(bill)
    b['subSupplyType'] = int(bill['subSupplyType'] or 1)
    b['transType'] = bill['transactionType']
    b['actualFromStateCode'] = bill['actFromStateCode']
    b['actualToStateCode'] = bill['actToStateCode']
    b['TotNonAdvolVal'] = bill['cessNonAdvolValue']
    b['OthValue'] = bill['otherValue']
    # Left blank the workbook writes 1 (Road) rather than nothing at all.
    b['transMode'] = int(bill['transMode'] or 1)
    b['itemList'] = [
        {k: (str(it['hsnCode'] or '') if k == 'hsnCode' else it[k])
         for k in _BULK_ITEM_ORDER}
        for it in bill['itemList']]
    return {k: b[k] for k in _BULK_ORDER}


def _line_faults(it, src):
    """What is wrong with one line, phrased to follow "Line 3 (CRCA)"."""
    if not it['hsnCode']:
        yield 'error', 'no HSN code. Put it on the item in the Items list.'
    elif src['source'] in ('item name', 'description'):
        yield 'warn', ('HSN %s came out of the %s, not an HSN column. Worth a '
                       'look.' % (it['hsnCode'], src['source']))
    if not it['qtyUnit']:
        yield 'error', 'no unit.'
    elif it['qtyUnit'] == UQC_FALLBACK:
        yield 'warn', 'a unit the portal does not know, so it goes as OTH.'
    if it['quantity'] <= 0:
        yield 'error', 'no quantity.'


def _numbered(nums):
    """[1..8] -> "Lines 1-8"; [1, 3] -> "Lines 1, 3"; [3] -> "Line 3"."""
    if len(nums) == 1:
        return 'Line %d' % nums[0]
    if nums == list(range(nums[0], nums[-1] + 1)):
        return 'Lines %d-%d' % (nums[0], nums[-1])
    return 'Lines ' + ', '.join(str(n) for n in nums)


def _line_notes(items, hsn_sources, notes):
    """One note per fault, not one per line.

    A steel bill is the same item over and over -- eight lines of CRCA cut to
    eight sizes. Eight copies of "no HSN code" is one thing wrong written out
    eight times, and it buries whatever else is on the list.
    """
    groups, order = {}, []
    for i, (it, src) in enumerate(zip(items, hsn_sources), 1):
        label = it['productName'] or 'no name'
        for level, message in _line_faults(it, src):
            key = (level, label, message)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(i)
    for key in order:
        level, label, message = key
        _note(notes, level, '%s (%s) -- %s'
              % (_numbered(groups[key]), label, message))


def _transport_notes(p, notes):
    """Part B, by the workbook's own rules.

    The tool refuses a row four ways over transport, and each of them is a
    lorry already loaded when the portal says no:

      * road, or nothing chosen -- there has to be a vehicle number or a
        transporter to hand it to;
      * rail or air -- the transport document number and its date are both
        required, and the load cannot be over dimensional;
      * ship -- over dimensional is the only vehicle type it will take.
    """
    mode = str(p['transMode'] or '')
    by_rail_air = mode in ('2', '3')
    by_ship = mode == '4'
    odc = p['vehicleType'] == 'O'

    if not by_rail_air and not by_ship:
        if not p['vehicleNo'] and not p['transporterId']:
            _note(notes, 'error', 'Neither a vehicle number nor a transporter '
                                  'GSTIN. By road the portal wants one of the '
                                  'two before it will take the row.')
    if p['vehicleNo'] and not _VEHICLE_RE.match(p['vehicleNo']):
        _note(notes, 'error', 'Vehicle number "%s" is not a vehicle number '
                              '(GJ03BY9139). The portal refuses the row rather '
                              'than take it as written.' % p['vehicleNo'])
    if p['transporterId'] and not gstin_check_digit(p['transporterId']):
        _note(notes, 'error', 'The transporter GSTIN "%s" is not valid.'
              % p['transporterId'])

    if by_rail_air:
        if not p['transDocNo'] or not p['transDocDate']:
            _note(notes, 'error', 'By %s the transport document number and '
                                  'its date are both required.'
                  % ('rail' if mode == '2' else 'air'))
        if odc:
            _note(notes, 'error', 'By %s the vehicle type has to be Regular, '
                                  'not over dimensional.'
                  % ('rail' if mode == '2' else 'air'))
    if by_ship and not odc:
        _note(notes, 'error', 'By ship the vehicle type has to be over '
                              'dimensional cargo; the portal will not take '
                              'Regular.')

    if p['transDistance'] > 4000:
        _note(notes, 'error', 'Distance is %d km. The portal takes 4000 at '
                              'most.' % p['transDistance'])
    elif not p['transDistance']:
        _note(notes, 'info', 'Distance left at 0 -- the portal works it out '
                             'from the two PIN codes.')


def _check(p, inv, co, items, hsn_sources, registered, taxed, notes,
           consignor='own', pins=('column', 'column')):
    """Everything the portal would have thrown back, said in advance."""
    who = co.get('short_name') or co.get('code') or co.get('name') or 'this firm'
    party = inv.get('party_name') or 'The party'

    # Whose GSTIN it moves under, before anything about that firm is checked.
    # A challan book with nothing to send under would otherwise collect four
    # separate refusals -- no GSTIN, no PIN, no address, no city -- for one
    # thing wrong, and none of them the thing that is actually wrong.
    if consignor == 'none':
        _note(notes, 'error', 'A challan cannot go up on its own: the book it '
                              'is written in has no GSTIN. Nothing says which '
                              'firm %s is billed from, so there is nothing to '
                              'send it under -- set the billing firm on the '
                              'party, or pick the firm challans move under on '
                              'the e-way screen.' % party)
    else:
        if consignor != 'own':
            _note(notes, 'info', 'Written in the challan book, so it goes up '
                                 'under %s (%s) -- %s.'
                  % (co.get('name'), p['fromGstin'],
                     'the firm that bills this customer'
                     if consignor == 'billing firm'
                     else 'the firm picked on the e-way screen'))
        if not p['fromGstin']:
            _note(notes, 'error', '%s has no GSTIN. Settings -> Companies.'
                  % who)
        else:
            _gstin_note(notes, who, p['fromGstin'])
        if not p['fromPincode']:
            _note(notes, 'error', '%s has no 6-digit PIN code. Settings -> '
                                  'Companies.' % who)
        elif pins[0] == 'address':
            _note(notes, 'warn', '%s has no PIN code of its own, so %d was '
                                 'read out of its address. Put it in the PIN '
                                 'box under Settings -> Companies.'
                  % (who, p['fromPincode']))
        if not p['fromAddr1']:
            _note(notes, 'error', '%s has no address. Settings -> Companies.'
                  % who)
        if not p['fromPlace']:
            _note(notes, 'error', '%s has no city. Settings -> Companies.'
                  % who)
        if not p['fromStateCode']:
            _note(notes, 'error', '%s has no state. Settings -> Companies.'
                  % who)

    if registered:
        _gstin_note(notes, party, p['toGstin'])
    if not registered:
        _note(notes, 'warn', '%s has no GSTIN, so the bill goes out as URP '
                             '(unregistered).' % party)
    if not p['toPincode']:
        _note(notes, 'error', '%s has no 6-digit PIN code. Open the party and '
                              'fill it in.' % party)
    elif pins[1] == 'address':
        _note(notes, 'warn', '%s has no PIN code of its own, so %d was read '
                             'out of its address. Worth a look.'
              % (party, p['toPincode']))
    if not p['toAddr1']:
        _note(notes, 'error', '%s has no address.' % party)
    if not p['toPlace']:
        _note(notes, 'error', '%s has no city.' % party)
    if not p['toStateCode']:
        _note(notes, 'error', '%s has no state.' % party)

    if not p['docNo']:
        _note(notes, 'error', 'This document has no number.')
    elif not _DOCNO_RE.match(p['docNo']):
        _note(notes, 'error', 'The portal will not take the document number '
                              '"%s" -- it allows letters, digits, / and - '
                              'only, up to 16. Set "Number on the portal" '
                              'under Sales -> E-way Bill JSON to whatever '
                              'this bill is filed as in GSTR-1.'
              % p['docNo'])
    if not p['docDate']:
        _note(notes, 'error', 'This document has no readable date.')

    if not items:
        _note(notes, 'error', 'This document has no lines.')
    _line_notes(items, hsn_sources, notes)

    if len(items) > 250:
        _note(notes, 'error', 'This document has %d lines. One e-way bill '
                              'holds 250.' % len(items))

    if p['subSupplyType'] == '8' and not p['subSupplyDesc']:
        _note(notes, 'error', 'The reason for movement is Others, so the '
                              'portal wants it spelt out in a few words.')

    # The portal adds these up itself. The workbook lets the sum run two
    # rupees over the invoice value before it refuses -- thirteen lines each
    # rounded on their own do not always land on the same figure -- and says
    # nothing at all when it comes out short by less than three.
    reckoned = api.r2(p['totalValue'] + p['cgstValue'] + p['sgstValue']
                      + p['igstValue'] + p['cessValue']
                      + p['cessNonAdvolValue'] + p['otherValue'])
    gap = api.r2(reckoned - p['totInvValue'])
    if gap > 2:
        _note(notes, 'error', 'The values add up to %.2f against a total of '
                              '%.2f, which is %.2f more than the portal '
                              'allows.' % (reckoned, p['totInvValue'], gap))
    elif gap <= -3:
        _note(notes, 'warn', 'The values add up to %.2f but the total says '
                             '%.2f.' % (reckoned, p['totInvValue']))
    if p['totInvValue'] >= 100000000:
        _note(notes, 'warn', 'The total is over 10 crore. Worth reading twice '
                             'before it goes up.')
    lines_total = api.r2(sum(i['taxableAmount'] for i in items))
    if abs(lines_total - p['totalValue']) >= 0.01:
        _note(notes, 'error', 'The lines come to %.2f but the taxable value is '
                              '%.2f.' % (lines_total, p['totalValue']))

    _transport_notes(p, notes)

    # The portal reads the two state codes and works the tax out for itself,
    # so a bill that says IGST between two Gujarat parties is refused.
    if taxed and p['fromStateCode'] and p['toStateCode']:
        same = p['fromStateCode'] == p['toStateCode']
        if inv.get('is_igst') and same:
            _note(notes, 'error', 'The bill charges IGST but both parties are '
                                  'in state %d, where the portal expects CGST '
                                  'and SGST.' % p['fromStateCode'])
        elif not inv.get('is_igst') and not same:
            _note(notes, 'error', 'The bill charges CGST and SGST but the '
                                  'parties are in states %d and %d, where the '
                                  'portal expects IGST.'
                  % (p['fromStateCode'], p['toStateCode']))

    if not taxed:
        _note(notes, 'info', 'This is a challan, so it goes as a delivery '
                             'challan with no tax. The GST bill made from it '
                             'is usually the better document to move goods on.')
    if p['totInvValue'] < THRESHOLD:
        _note(notes, 'info', 'Value is %s, under the %s an e-way bill is '
                             'normally needed for.'
              % (rupees(p['totInvValue']), rupees(THRESHOLD)))


# --------------------------------------------------------------- the files

def docno_style(conn, asked=None):
    import db
    style = (asked or db.get_meta(conn, 'eway_docno', '') or DOCNO_DEFAULT)
    return style if style in dict(DOCNO_STYLES) else DOCNO_DEFAULT


def schema_of(asked=None):
    """Which of the two files is being asked for. Unknown means the bulk
    one -- that is what the portal's own upload page takes."""
    want = str(asked or '').strip().lower()
    return want if want in dict(SCHEMAS) else 'bulk'


def wrap(payloads, schema='bulk'):
    """The bills as a whole file.

    The workbook writes one object -- its version and a `billLists` array --
    and that wrapper is not decoration: the portal reads the version to know
    which spelling of the field names it has been handed. The API shape has
    no such wrapper and goes as a plain array.
    """
    if schema == 'api':
        return list(payloads)
    return {'version': BULK_VERSION, 'billLists': list(payloads)}


def preview(conn, cid, iid, schema='bulk', style=None):
    """One document, ready to look at on screen before it is downloaded."""
    style, schema = docno_style(conn, style), schema_of(schema)
    inv = api.get_invoice(conn, cid, iid)
    payload, notes, sources = build(conn, cid, inv, schema, style)
    return dict(invoice_id=iid, company_id=cid, doc_no=payload['docNo'],
                doc_date=inv['invoice_date'], party=inv.get('party_name') or '',
                grand_total=api.r2(inv.get('grand_total') or 0),
                lr_no=inv.get('lr_no') or '', lr_date=inv.get('lr_date') or '',
                payload=payload, notes=notes, hsn=sources,
                errors=sum(1 for n in notes if n['level'] == 'error'),
                warnings=sum(1 for n in notes if n['level'] == 'warn'))


def _refs(conn, spec):
    """"4:248869,3:248870" or plain ids -- (company_id, invoice_id) pairs.

    A bill lives in its own firm's books, so an id alone is not enough to find
    one when the list spans both billing firms.
    """
    out = []
    for chunk in str(spec or '').replace(' ', '').split(','):
        if not chunk:
            continue
        if ':' in chunk:
            co, _, doc = chunk.partition(':')
            out.append((api.qint(co, 'firm'), api.qint(doc, 'document')))
        else:
            iid = api.qint(chunk, 'document')
            row = api.one(conn.execute(
                'SELECT company_id FROM invoice WHERE id=?', (iid,)))
            if not row:
                raise api.ApiError('Document %d not found.' % iid, 404)
            out.append((row['company_id'], iid))
    if not out:
        raise api.ApiError('Pick at least one bill.')
    return out


def preview_many(conn, spec, schema='bulk', style=None):
    schema = schema_of(schema)
    docs = [preview(conn, cid, iid, schema, style)
            for cid, iid in _refs(conn, spec)]
    return dict(count=len(docs), documents=docs,
                errors=sum(d['errors'] for d in docs),
                warnings=sum(d['warnings'] for d in docs),
                total=api.r2(sum(d['grand_total'] for d in docs)))


def bundle(conn, spec, schema='bulk', force=False, style=None):
    """The file itself, as the portal's upload page takes it."""
    schema = schema_of(schema)
    docs = [preview(conn, cid, iid, schema, style)
            for cid, iid in _refs(conn, spec)]
    bad = [d for d in docs if d['errors']]
    if bad and not force:
        raise api.ApiError(
            '%d of %d bill(s) would be refused by the portal. Put those right '
            'first, or download anyway.' % (len(bad), len(docs)),
            400, dict(eway_errors=[dict(doc_no=d['doc_no'],
                                        notes=[n['text'] for n in d['notes']
                                               if n['level'] == 'error'])
                                   for d in bad]))
    text = json.dumps(wrap([d['payload'] for d in docs], schema), indent=1)
    if len(docs) == 1:
        name = 'eway-%s.json' % re.sub(r'[^A-Za-z0-9]+', '-',
                                       docs[0]['doc_no']).strip('-')
    else:
        name = 'eway-%d-bills.json' % len(docs)
    return text, name
