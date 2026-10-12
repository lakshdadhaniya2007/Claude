"""SQLite schema + helpers.

Modelled on the SHREE (FoxPro) system in this folder: many companies, each with
its own chart of accounts (HMAS groups + AMAS ledgers), documents that post
double-entry rows to a single ledger table, and document numbers that restart
every financial year.
"""
import os
import re
import sqlite3
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
# BILLING_DB points every script at another copy of the books -- how the SHREE
# update runs its whole chain on a working copy before the live file is touched.
DB_PATH = os.environ.get('BILLING_DB') or os.path.join(BASE, 'data', 'billing.db')
SCHEMA_VERSION = 18

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS company (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    address1      TEXT DEFAULT '',
    address2      TEXT DEFAULT '',
    city          TEXT DEFAULT '',
    state         TEXT DEFAULT '',
    state_code    TEXT DEFAULT '',
    pin           TEXT DEFAULT '',
    gstin         TEXT DEFAULT '',
    pan           TEXT DEFAULT '',
    phone         TEXT DEFAULT '',
    mobile        TEXT DEFAULT '',
    email         TEXT DEFAULT '',
    bank_name     TEXT DEFAULT '',
    bank_ac       TEXT DEFAULT '',
    bank_ifsc     TEXT DEFAULT '',
    bank_branch   TEXT DEFAULT '',               -- printed under the A/C line
    terms         TEXT DEFAULT '',
    books_from    TEXT DEFAULT '',
    logo          TEXT DEFAULT '',               -- data: URI, printed top-left
    jurisdiction  TEXT DEFAULT '',               -- "Subject to ___ Jurisdiction."
    short_name    TEXT DEFAULT '',               -- heading on the rough challan
    tax_mode      TEXT NOT NULL DEFAULT 'gst',   -- gst | none
    default_gst   REAL NOT NULL DEFAULT 18,      -- used when an item carries none
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS account_group (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    code          TEXT NOT NULL,
    name          TEXT NOT NULL,
    nature        TEXT NOT NULL,          -- asset | liability | income | expense
    parent_code   TEXT DEFAULT '',
    is_party      INTEGER NOT NULL DEFAULT 0,
    sort          INTEGER NOT NULL DEFAULT 0,
    UNIQUE(company_id, code)
);

CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    group_id      INTEGER NOT NULL REFERENCES account_group(id),
    system_key    TEXT,                   -- SALES, OUT_CGST, CASH ... (NULL = user)
    code          TEXT,
    name          TEXT NOT NULL,
    party_kind    TEXT NOT NULL DEFAULT '',  -- '' | customer | supplier | both
    gst_type      TEXT NOT NULL DEFAULT 'Regular',
    address1      TEXT DEFAULT '',
    address2      TEXT DEFAULT '',
    city          TEXT DEFAULT '',
    state         TEXT DEFAULT '',
    state_code    TEXT DEFAULT '',
    pin           TEXT DEFAULT '',
    gstin         TEXT DEFAULT '',
    pan           TEXT DEFAULT '',
    phone         TEXT DEFAULT '',
    email         TEXT DEFAULT '',
    opening_bal   REAL NOT NULL DEFAULT 0,   -- +ve = debit
    credit_limit  REAL NOT NULL DEFAULT 0,
    credit_days   INTEGER NOT NULL DEFAULT 0,
    notes         TEXT DEFAULT '',
    -- 'SHREE' on anything lifted out of the old FoxPro books. Re-running the
    -- import rebuilds only its own accounts, never one entered here.
    source        TEXT DEFAULT '',
    -- Orders are taken in the order firm and billed in an official firm.
    -- home_company is the firm that bills this customer; linked_ledger points
    -- at the same customer's record over in that firm.
    home_company  INTEGER REFERENCES company(id),
    linked_ledger INTEGER REFERENCES ledger(id),
    active        INTEGER NOT NULL DEFAULT 1,
    UNIQUE(company_id, code)
);
CREATE INDEX IF NOT EXISTS ix_ledger_co ON ledger(company_id, name);
-- Partial index: many user ledgers carry NULL, only built-ins must be unique.
CREATE UNIQUE INDEX IF NOT EXISTS ix_ledger_syskey
    ON ledger(company_id, system_key) WHERE system_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS item (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    code          TEXT,
    name          TEXT NOT NULL,
    category      TEXT DEFAULT '',
    hsn           TEXT DEFAULT '',
    unit          TEXT NOT NULL DEFAULT 'NOS',
    gst_rate      REAL NOT NULL DEFAULT 18,
    cess_rate     REAL NOT NULL DEFAULT 0,
    sale_rate     REAL NOT NULL DEFAULT 0,
    purchase_rate REAL NOT NULL DEFAULT 0,
    opening_qty   REAL NOT NULL DEFAULT 0,
    reorder_level REAL NOT NULL DEFAULT 0,
    track_stock   INTEGER NOT NULL DEFAULT 1,
    -- Steel is always priced per kilo; the piece count is only information.
    by_weight     INTEGER NOT NULL DEFAULT 1,
    source        TEXT DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    UNIQUE(company_id, code)
);
CREATE INDEX IF NOT EXISTS ix_item_co ON item(company_id, name);

CREATE TABLE IF NOT EXISTS sales_order (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    order_no      TEXT NOT NULL,
    order_date    TEXT NOT NULL,
    party_id      INTEGER NOT NULL REFERENCES ledger(id),
    due_date      TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open',
    fy            TEXT NOT NULL DEFAULT '',
    billed_company INTEGER REFERENCES company(id),  -- firm that raised the bill
    billed_invoice INTEGER REFERENCES invoice(id),
    remarks       TEXT DEFAULT '',
    subtotal      REAL NOT NULL DEFAULT 0,
    tax_total     REAL NOT NULL DEFAULT 0,
    grand_total   REAL NOT NULL DEFAULT 0,
    source        TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(company_id, fy, order_no)
);

CREATE TABLE IF NOT EXISTS sales_order_item (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      INTEGER NOT NULL REFERENCES sales_order(id) ON DELETE CASCADE,
    item_id       INTEGER REFERENCES item(id),
    description   TEXT NOT NULL DEFAULT '',
    hsn           TEXT DEFAULT '',
    unit          TEXT DEFAULT '',
    -- How the sheet is to be cut. Free text: gauge, mm, feet -- whatever the
    -- order was taken in.
    thickness     TEXT DEFAULT '',
    cutting_size  TEXT DEFAULT '',
    item_detail   TEXT DEFAULT '',
    qty           REAL NOT NULL DEFAULT 0,   -- pieces / units ordered
    weight        REAL NOT NULL DEFAULT 0,   -- filled in at invoice time
    by_weight     INTEGER NOT NULL DEFAULT 1,
    incl_price    REAL NOT NULL DEFAULT 0,
    rate          REAL NOT NULL DEFAULT 0,
    discount_pct  REAL NOT NULL DEFAULT 0,
    gst_rate      REAL NOT NULL DEFAULT 0,
    taxable       REAL NOT NULL DEFAULT 0,
    tax_amt       REAL NOT NULL DEFAULT 0,
    line_total    REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_so_co ON sales_order(company_id, order_date);
CREATE INDEX IF NOT EXISTS ix_soitem_order ON sales_order_item(order_id);


CREATE TABLE IF NOT EXISTS invoice (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    doc_type      TEXT NOT NULL,          -- SALE | PURCHASE | CREDIT | DEBIT
    series        TEXT DEFAULT '',        -- the small box before the number
    invoice_no    TEXT NOT NULL,
    invoice_type  TEXT NOT NULL DEFAULT 'Debit',   -- Debit | Credit
    invoice_date  TEXT NOT NULL,
    fy            TEXT NOT NULL DEFAULT '',
    party_id      INTEGER NOT NULL REFERENCES ledger(id),
    sales_ac_id   INTEGER REFERENCES ledger(id),   -- the "Sales A/c" box
    driver_name   TEXT DEFAULT '',
    payment_terms TEXT DEFAULT '',
    dept          TEXT DEFAULT '',                 -- "By Dept"
    order_id      INTEGER REFERENCES sales_order(id),
    supply_state  TEXT DEFAULT '',
    is_igst       INTEGER NOT NULL DEFAULT 0,
    rcm           INTEGER NOT NULL DEFAULT 0,
    ref_no        TEXT DEFAULT '',
    subtotal      REAL NOT NULL DEFAULT 0,
    discount_amt  REAL NOT NULL DEFAULT 0,
    taxable       REAL NOT NULL DEFAULT 0,
    cgst          REAL NOT NULL DEFAULT 0,
    sgst          REAL NOT NULL DEFAULT 0,
    igst          REAL NOT NULL DEFAULT 0,
    cess          REAL NOT NULL DEFAULT 0,
    weight_total  REAL NOT NULL DEFAULT 0,
    cutting       REAL NOT NULL DEFAULT 0,
    transport     REAL NOT NULL DEFAULT 0,
    loading       REAL NOT NULL DEFAULT 0,
    -- The source challan's own round off, carried into the taxable value so
    -- the bill is exactly the challan plus GST.
    taxable_adj   REAL NOT NULL DEFAULT 0,
    round_off     REAL NOT NULL DEFAULT 0,
    grand_total   REAL NOT NULL DEFAULT 0,
    party_amount  REAL NOT NULL DEFAULT 0,   -- what the party is billed (RCM aware)
    transporter   TEXT DEFAULT '',
    vehicle_no    TEXT DEFAULT '',
    lr_no         TEXT DEFAULT '',
    lr_date       TEXT DEFAULT '',
    cases         TEXT DEFAULT '',
    eway_no       TEXT DEFAULT '',
    remarks       TEXT DEFAULT '',
    -- 'SHREE' on anything lifted out of the old FoxPro books, blank on
    -- anything billed here. Re-running the import replaces only its own rows.
    source        TEXT DEFAULT '',
    -- A challan raised in the order firm and the GST bill made from it.
    from_challan   INTEGER REFERENCES invoice(id),
    billed_company INTEGER REFERENCES company(id),
    billed_invoice INTEGER REFERENCES invoice(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(company_id, doc_type, fy, series, invoice_no)
);
CREATE INDEX IF NOT EXISTS ix_inv_co ON invoice(company_id, invoice_date);
CREATE INDEX IF NOT EXISTS ix_inv_party ON invoice(party_id);
CREATE INDEX IF NOT EXISTS ix_inv_order ON invoice(order_id)
    WHERE order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS invoice_item (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id    INTEGER NOT NULL REFERENCES invoice(id) ON DELETE CASCADE,
    item_id       INTEGER REFERENCES item(id),
    description   TEXT NOT NULL DEFAULT '',
    hsn           TEXT DEFAULT '',
    unit          TEXT DEFAULT '',
    thickness     TEXT DEFAULT '',        -- carried over from the order
    cutting_size  TEXT DEFAULT '',
    qty           REAL NOT NULL DEFAULT 0,   -- pieces / units ordered
    weight        REAL NOT NULL DEFAULT 0,   -- kg; this is what prices the line
    by_weight     INTEGER NOT NULL DEFAULT 1,
    incl_price    REAL NOT NULL DEFAULT 0,   -- agreed price WITH tax, if used
    rate          REAL NOT NULL DEFAULT 0,
    discount_pct  REAL NOT NULL DEFAULT 0,
    taxable       REAL NOT NULL DEFAULT 0,
    gst_rate      REAL NOT NULL DEFAULT 0,
    cgst          REAL NOT NULL DEFAULT 0,
    sgst          REAL NOT NULL DEFAULT 0,
    igst          REAL NOT NULL DEFAULT 0,
    cess_rate     REAL NOT NULL DEFAULT 0,
    cess          REAL NOT NULL DEFAULT 0,
    -- This line's share of the cutting / transport / loading charges, which
    -- are part of the value of supply and so are taxed with it.
    charge_share  REAL NOT NULL DEFAULT 0,
    item_detail   TEXT DEFAULT '',        -- the "Item Detail" column
    line_total    REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_invitem_inv ON invoice_item(invoice_id);
-- Deleting an item counts its uses; stock reads group by it.
CREATE INDEX IF NOT EXISTS ix_invitem_item ON invoice_item(item_id)
    WHERE item_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS voucher (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,          -- RECEIPT | PAYMENT | JOURNAL | CONTRA
    voucher_no    TEXT NOT NULL,
    voucher_date  TEXT NOT NULL,
    fy            TEXT NOT NULL DEFAULT '',
    party_id      INTEGER REFERENCES ledger(id),   -- receipt/payment counterparty
    bank_id       INTEGER REFERENCES ledger(id),   -- cash or bank ledger hit
    mode          TEXT NOT NULL DEFAULT 'Cash',
    ref_no        TEXT DEFAULT '',
    amount        REAL NOT NULL DEFAULT 0,
    remarks       TEXT DEFAULT '',
    source        TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(company_id, kind, fy, voucher_no)
);
CREATE INDEX IF NOT EXISTS ix_vou_co ON voucher(company_id, voucher_date);

CREATE TABLE IF NOT EXISTS voucher_line (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_id    INTEGER NOT NULL REFERENCES voucher(id) ON DELETE CASCADE,
    ledger_id     INTEGER NOT NULL REFERENCES ledger(id),
    debit         REAL NOT NULL DEFAULT 0,
    credit        REAL NOT NULL DEFAULT 0,
    remarks       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_vouline_vou ON voucher_line(voucher_id);

-- Every document writes its double-entry rows here. All ledger-based reports
-- (statement, trial balance, P&L, balance sheet) read only this table.
CREATE TABLE IF NOT EXISTS posting (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    date          TEXT NOT NULL,
    ledger_id     INTEGER NOT NULL REFERENCES ledger(id),
    doc_kind      TEXT NOT NULL,          -- SALE, PURCHASE, RECEIPT, JOURNAL ...
    doc_id        INTEGER NOT NULL,
    doc_no        TEXT NOT NULL DEFAULT '',
    debit         REAL NOT NULL DEFAULT 0,
    credit        REAL NOT NULL DEFAULT 0,
    narration     TEXT DEFAULT ''
);
-- Covering: a firm's balances are added up straight out of the index, without
-- visiting the 240,000 rows themselves. Every balance screen does this.
CREATE INDEX IF NOT EXISTS ix_post_bal
    ON posting(company_id, ledger_id, date, debit, credit);
CREATE INDEX IF NOT EXISTS ix_post_doc ON posting(doc_kind, doc_id);

CREATE INDEX IF NOT EXISTS ix_post_co_date ON posting(company_id, date);

CREATE TABLE IF NOT EXISTS counter (
    company_id    INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    fy            TEXT NOT NULL DEFAULT '',
    prefix        TEXT NOT NULL DEFAULT '',
    next_no       INTEGER NOT NULL DEFAULT 1,
    -- How many digits the number is padded to. 4 gives INV-0007; 0 gives a
    -- plain 7, which is what the challan books are numbered with.
    pad           INTEGER NOT NULL DEFAULT 4,
    PRIMARY KEY (company_id, key, fy)
);

-- The people paid a salary. Each one is an account in the books -- the
-- salary account SHREE keeps for them under SALARY PAYABLE -- and all the
-- money is there. This holds only what the books do not: how they are paid.
-- Named, not numbered, as sync_rule is: a SHREE update rebuilds its accounts.
CREATE TABLE IF NOT EXISTS staff (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,              -- the account's name, upper case
    pay_basis     TEXT NOT NULL DEFAULT '',   -- month | day | hour; '' = as the books say
    rate          REAL NOT NULL DEFAULT 0,    -- a month's salary, or a day's or an hour's
    mobile        TEXT NOT NULL DEFAULT '',
    joined        TEXT NOT NULL DEFAULT '',
    left_on       TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    listed        INTEGER NOT NULL DEFAULT 1, -- 0 = taken off the staff list
    -- Their loose-money account: cash handed over in an emergency, kept
    -- apart from salary. Named like the person, "... (LOOSE MONEY)".
    loose_account TEXT NOT NULL DEFAULT '',
    UNIQUE(company_id, name)
);

-- The SHREE update rebuilds every imported row from the old software's files,
-- which would undo anything decided here about those rows. Each decision is
-- written down as it is made -- a customer deleted, an account folded into
-- its renamed successor, a party billed by another firm -- and played again
-- after every update. Named, not numbered: the rows get new ids each time.
CREATE TABLE IF NOT EXISTS sync_rule (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    firm          TEXT NOT NULL,          -- company code: BSTP, BST, BSI
    name          TEXT NOT NULL,          -- account name, or a document's key
    value         TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- One row per SHREE update, kept for the screen that shows what changed.
CREATE TABLE IF NOT EXISTS sync_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started       TEXT NOT NULL,
    finished      TEXT NOT NULL DEFAULT '',
    started_by    TEXT NOT NULL DEFAULT '',   -- you | automatic
    mode          TEXT NOT NULL DEFAULT 'apply',  -- apply | preview
    status        TEXT NOT NULL DEFAULT '',   -- done | failed | nothing new
    message       TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '{}',
    log           TEXT NOT NULL DEFAULT '',
    backup        TEXT NOT NULL DEFAULT ''
);
"""

# Which bills an imported challan went out on, where that is not simply one
# bill made from it -- a challan billed in two or three parts, two or three
# challans billed together, a bill re-priced or re-weighed. SHREE never wrote
# the pair down; link_challans.py recognises it from the goods. The challan's
# own billed_invoice names the first of its bills, so it reads as billed
# everywhere; this says what else. The bill's from_challan is left alone, so
# editing the challan never rebuilds a bill it only partly matches.
CHALLAN_BILL = """
CREATE TABLE IF NOT EXISTS challan_bill (
    challan_id    INTEGER NOT NULL REFERENCES invoice(id) ON DELETE CASCADE,
    bill_id       INTEGER NOT NULL REFERENCES invoice(id) ON DELETE CASCADE,
    how           TEXT NOT NULL DEFAULT '',  -- split | combined | regrouped |
                                              -- repriced | reweighed | near |
                                              -- renamed | other name
    PRIMARY KEY (challan_id, bill_id)
);
CREATE INDEX IF NOT EXISTS ix_challan_bill_bill ON challan_bill(bill_id);
"""
SCHEMA += CHALLAN_BILL

# The steel kept in the challan firm's yard (26 Sep 2026: "create a stock
# management for challans ... CRCA - REGULAR - 0.50"). A piece of stock is a
# type, a quality and a thickness, each picked from the lists below -- the
# three columns of the user's own sheet. Steel comes in by a stock-in entry
# (or the opening stock counted on the first day) and goes out on the
# challans raised in this app, whose lines name the same three.
STEEL = """
CREATE TABLE IF NOT EXISTS steel_list (
    id    INTEGER PRIMARY KEY,
    kind  TEXT NOT NULL,                  -- type | quality | thickness
    name  TEXT NOT NULL,
    sort  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(kind, name)
);
CREATE TABLE IF NOT EXISTS steel_entry (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'IN',   -- IN | OPENING | OUT | COUNT
    entry_date  TEXT NOT NULL,
    party       TEXT NOT NULL DEFAULT '',
    ref_no      TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_steel_entry_co ON steel_entry(company_id, entry_date);
CREATE TABLE IF NOT EXISTS steel_entry_line (
    id             INTEGER PRIMARY KEY,
    entry_id       INTEGER NOT NULL REFERENCES steel_entry(id) ON DELETE CASCADE,
    steel_type     TEXT NOT NULL,
    steel_quality  TEXT NOT NULL,
    steel_thick    TEXT NOT NULL,
    weight         REAL NOT NULL DEFAULT 0,   -- + in; OUT and COUNT signed
    counted        REAL                       -- COUNT: the kilos found
);
CREATE INDEX IF NOT EXISTS ix_steel_line_entry ON steel_entry_line(entry_id);
"""
SCHEMA += STEEL

# Stock put into or taken out of the billing firms' books by hand (27 Sep
# 2026: "add a option to add and delete stock"). Bills and purchases move the
# stock on their own; this is for everything else -- a count that does not
# agree, damaged or scrapped steel, an opening typed in. Each line is the
# change it makes to one item, in the item's own figure (kilos for steel):
# plus for stock added, minus for stock removed, and for a count, the weight
# found less what the books held that day -- `counted` keeps what was found.
# Nothing is posted to any account. SHREE's update never writes here, so
# these stand through every update, moving the stock on from SHREE's figure
# the way a bill made in this app does.
STOCK_ENTRY = """
CREATE TABLE IF NOT EXISTS stock_entry (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'ADD',  -- ADD | REMOVE | COUNT
    entry_date  TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_stock_entry_co ON stock_entry(company_id, entry_date);
CREATE TABLE IF NOT EXISTS stock_entry_line (
    id        INTEGER PRIMARY KEY,
    entry_id  INTEGER NOT NULL REFERENCES stock_entry(id) ON DELETE CASCADE,
    item_id   INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    qty       REAL NOT NULL DEFAULT 0,      -- the change: + in, - out
    counted   REAL                          -- a count: the weight found
);
CREATE INDEX IF NOT EXISTS ix_stock_line_entry ON stock_entry_line(entry_id);
CREATE INDEX IF NOT EXISTS ix_stock_line_item ON stock_entry_line(item_id);
"""
SCHEMA += STOCK_ENTRY

# The user's own money, apart from the business (30 Sep 2026: "a new page
# named Personal Accounts ... not related to the business ... the money given
# or received from different people"). A person and every sum handed to
# them or taken from them. None of it belongs to a firm: nothing is posted to
# an account, nothing reaches a trial balance, and SHREE's update never
# writes here -- the update's copy carries these tables across untouched.
PERSONAL = """
CREATE TABLE IF NOT EXISTS personal_person (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    mobile      TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS personal_entry (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES personal_person(id) ON DELETE CASCADE,
    entry_date  TEXT NOT NULL,
    direction   TEXT NOT NULL,            -- GIVEN to them | RECEIVED from them
    amount      REAL NOT NULL,            -- always more than 0
    mode        TEXT NOT NULL DEFAULT '', -- Cash, Bank, UPI, Cheque ...
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    -- Brought in from an account in the business books: 'FIRM|ACCOUNT NAME'
    -- ('' = typed here), and the document it was copied off. Bringing the
    -- same account in again replaces these rows and no others.
    source      TEXT NOT NULL DEFAULT '',
    ref         TEXT NOT NULL DEFAULT '',
    doc_kind    TEXT NOT NULL DEFAULT '',
    doc_id      INTEGER,
    company_id  INTEGER
);
CREATE INDEX IF NOT EXISTS ix_personal_entry ON personal_entry(person_id, entry_date);
"""
SCHEMA += PERSONAL

# Billing Stock's categories (4 Oct 2026: "Make categories for Billing Stock
# - GP SHEET, HR SHEET, CRCA SHEET, COLOUR COATED SHEET, MS PLATE, MS ANGLE,
# MS PIPE"). The list, in the order the screen shows it; a product names its
# category in item.stock_cat. Not item.category: that is SHREE's department,
# and every update writes it again.
STOCK_CAT = """
CREATE TABLE IF NOT EXISTS stock_cat (
    name  TEXT PRIMARY KEY,
    sort  INTEGER NOT NULL DEFAULT 0
);
"""
SCHEMA += STOCK_CAT

# Every save of a bill or challan, kept (6 Oct 2026: "add a history feature in
# all the bills and challans so I can see all the changes done in any
# particular detail"). Each row is the whole document as its screen showed it
# once that save was done -- names, not ids -- and the screen works out what
# changed from one row to the next. No foreign key: the SHREE update takes
# its own documents out and puts them back under the same id, and the history
# of one edited here has to come through that. Deleting a document deletes
# its history (api._purge_invoice, drop_years).
DOC_HISTORY = """
CREATE TABLE IF NOT EXISTS doc_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  INTEGER NOT NULL,
    company_id  INTEGER NOT NULL,
    -- created | edited | rebuilt (a bill redone from its edited challan) |
    -- before (as it stood when its history began) | elsewhere (changed by a
    -- SHREE update, a merge of accounts or a repair, between two saves)
    action      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    who         TEXT NOT NULL DEFAULT '',     -- this computer, or the device
    snapshot    TEXT NOT NULL,                -- JSON, api._doc_snapshot
    saved_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_doc_history ON doc_history(invoice_id, id);
"""
SCHEMA += DOC_HISTORY

# The group an account was in, year by year, where SHREE had it somewhere
# other than where it is now (6 Oct 2026: "make sure all the figures match").
# SHREE moves an account between groups from one year to the next -- TAX -
# CGST CASH A/C sat under Duties & Taxes until 2024-25 and under income
# since -- and each year's P&L and balance sheet show it where it was that
# year. Written by the import; a group chosen here applies to every year
# and clears its rows. `y` is the April the year opens in: 2022 = 2022-23.
LEDGER_GROUP_YEAR = """
CREATE TABLE IF NOT EXISTS ledger_group_year (
    ledger_id  INTEGER NOT NULL REFERENCES ledger(id) ON DELETE CASCADE,
    y          INTEGER NOT NULL,
    group_id   INTEGER NOT NULL REFERENCES account_group(id) ON DELETE CASCADE,
    PRIMARY KEY (ledger_id, y)
);
CREATE INDEX IF NOT EXISTS ix_lgy_group ON ledger_group_year(group_id);
"""
SCHEMA += LEDGER_GROUP_YEAR

STOCK_CAT_DEFAULTS = ('GP SHEET', 'HR SHEET', 'CRCA SHEET',
                      'COLOUR COATED SHEET', 'MS PLATE', 'MS ANGLE', 'MS PIPE')


def _cat_words(name):
    """A product name as words: "M.S.SHEET (7208)" -> MS, SHEET, 7208.
    Letters spelt out one at a time -- M S, H R, G P, C R -- are run back
    together, the way the trade writes them."""
    s = re.sub(r'[^A-Z0-9]+', ' ', (name or '').upper())
    s = re.sub(r'\b([A-Z])\s+(?=[A-Z]\b)', r'\1', s)
    return s.split()


# Words that say a product is none of the seven: scrap, bars, wire, mesh,
# fixings, machines. Checked before the HSN, which would otherwise file a
# flat bar (7211 14) as hot-rolled sheet.
_CAT_NONE = ('SCRAP', 'ASSORT', 'AASORT', 'ASSCORT', 'FLAT', 'BAR', 'ROUND',
             'ROND', 'SQUARE', 'TMT', 'WIRE', 'MESH', 'MASH', 'NAIL', 'FENC',
             'CHAINLINK', 'CHANLINK', 'MACHINE', 'TEST', 'SDS', 'PATTA',
             'SELF', 'COMPUTER')

# Then the HSN, longest first: what the tariff says the steel is.
_CAT_HSN = (('7204', ''), ('7216', 'MS ANGLE'), ('7304', 'MS PIPE'),
            ('7305', 'MS PIPE'), ('7306', 'MS PIPE'),
            ('721070', 'COLOUR COATED SHEET'), ('721240', 'COLOUR COATED SHEET'),
            ('721041', 'GP SHEET'), ('721049', 'GP SHEET'),
            ('721061', 'GP SHEET'), ('721069', 'GP SHEET'),
            ('721230', 'GP SHEET'), ('72111', 'HR SHEET'),
            ('72112', 'CRCA SHEET'), ('722530', 'HR SHEET'),
            ('722540', 'HR SHEET'), ('722550', 'CRCA SHEET'),
            ('7208', 'HR SHEET'), ('7209', 'CRCA SHEET'))


def guess_stock_cat(name, hsn='', cats=STOCK_CAT_DEFAULTS):
    """Which of the categories a product most likely is, or '' -- read off
    its name first, its HSN after. Only ever a first guess: a product made
    without a category gets it, and the screen changes it."""
    words = _cat_words(name)
    has = lambda *ps: any(w.startswith(ps) for w in words)
    hsn = re.sub(r'\D', '', hsn or '')
    if not hsn:
        # Years of codes were typed into the name -- "M.S.SHEET (7208)" --
        # and one code there is as good as the box (two is no answer).
        named = set(re.findall(r'(?<!\d)(\d{8}|\d{6}|\d{4})(?!\d)', name or ''))
        hsn = named.pop() if len(named) == 1 else ''
    want = ''
    if has('SCRAP', 'ASSORT', 'AASORT', 'ASSCORT') or hsn.startswith('7204'):
        want = ''
    elif has('PIPE', 'TUBE'):
        want = 'MS PIPE'
    elif has('ANGLE', 'CHANNEL', 'CHENAL', 'CHANAL'):
        want = 'MS ANGLE'
    elif has('COLOUR', 'COLOR', 'PPGI', 'PPGL') or (
            'CP' in words and 'CC' in words) or (
            'PP' in words and 'GL' in words):
        want = 'COLOUR COATED SHEET'
    elif has('PLATE', 'PLAT') and not has('SHEET', 'COIL'):
        want = 'MS PLATE'
    elif {'GP', 'GC', 'GL', 'MG'} & set(words) or has('GALV'):
        want = 'GP SHEET'
    elif {'HR', 'HRCA', 'HRPO', 'PKL'} & set(words) or has('PICKLE', 'HOT'):
        want = 'HR SHEET'
    elif {'CR', 'CRCA', 'CRC'} & set(words) or has('COLD'):
        want = 'CRCA SHEET'
    elif not has(*_CAT_NONE):
        for code, cat in _CAT_HSN:
            if hsn.startswith(code):
                want = cat
                break
    return want if want in cats else ''


def stock_cats(conn):
    return [r['name'] for r in
            conn.execute('SELECT name FROM stock_cat ORDER BY sort, name')]


# Two types asked for on 4 Oct 2026, when SHREE's challans were given their
# type: HRPO (and HRSPO with it) and HRSP each "a new category", not PICKLE
# or HRCA.
STEEL_TYPES_ADDED = ('HRPO', 'HRSP')

# The steel type a SHREE challan's detail names, as its words were typed --
# "3.00 CRAC 61 MM PATO", "2.10/30 PICCLE", "1.00MMCRCA". Read word by word
# (letters only, a size run into the front taken off); the first that names
# a type is the line's. CROSS, CRANE and SCRAP are not CRCA.
_STEEL_TYPE_WORDS = (
    (re.compile(r'^HR(S?PO|PA)$'), 'HRPO'),
    (re.compile(r'^HRSP$'), 'HRSP'),
    (re.compile(r'^HR(CA?)?$'), 'HRCA'),
    (re.compile(r'^(PI[CK]|PKL)[A-Z]*$'), 'PICKLE'),
    (re.compile(r'^C?CR(C[A-Z]*|AC[A-Z]*|A|TCA|RCA)?$'), 'CRCA'),
    (re.compile(r'^ROLLS?$'), 'ROLL'),
)


def guess_steel_type(detail, product='', types=()):
    """The type off `types` a SHREE challan line is, or ''.

    What the detail names; failing that, the product SHREE filed the line
    under when that is a type itself -- every SHREE challan line is "CRCA",
    so "22 GAGE CUTTING" is CRCA (the user's answer, 4 Oct 2026). A detail
    naming a type that is not on the list gets none rather than another.
    """
    for word in re.findall(r'[A-Z]+', (detail or '').upper()):
        word = re.sub(r'^(MM|E)(?=CR|PI|HR)', '', word)
        for pattern, kind in _STEEL_TYPE_WORDS:
            if pattern.match(word):
                return kind if kind in types else ''
    product = ' '.join((product or '').upper().split())
    return product if product in types else ''


def fill_steel_types(conn, cid=None):
    """Give every SHREE challan line with no steel type the one its words
    name. Only the type: SHREE never said the quality, so these lines name
    no whole steel and take nothing out of Challan Stock. Run after each
    import of the challan firm, and once over the books as they stood."""
    types = [r['name'] for r in conn.execute(
        "SELECT name FROM steel_list WHERE kind='type'")]
    if not types:
        return 0
    firms = [cid] if cid else [r['id'] for r in conn.execute(
        "SELECT id FROM company WHERE tax_mode='none'")]
    changed = []
    for fid in firms:
        for r in conn.execute(
                'SELECT ii.id, ii.description, ii.item_detail '
                'FROM invoice_item ii JOIN invoice i ON i.id=ii.invoice_id '
                'JOIN company c ON c.id=i.company_id '
                "WHERE i.company_id=? AND c.tax_mode='none' "
                "AND i.doc_type='SALE' AND i.source='SHREE' "
                "AND ii.steel_type=''", (fid,)):
            kind = guess_steel_type(r['item_detail'], r['description'], types)
            if kind:
                changed.append((kind, r['id']))
    conn.executemany('UPDATE invoice_item SET steel_type=? WHERE id=?', changed)
    return len(changed)

# The user's sheet, as given on 26 Sep 2026 ("SHREEFWFSF - Sheet1.pdf"). Its
# last thickness read "4.0 = 4.5", plainly the range 4.0 - 4.5 like the three
# above it. The lists can be changed from the Challan Stock screen.
STEEL_DEFAULTS = (
    ('type', ('CRCA', 'PICKLE', 'ROLL', 'HRCA') + STEEL_TYPES_ADDED),
    ('quality', ('REGULAR', 'SMALL', 'BABY', 'DEFECTIVE', 'PRIME', 'MEDIUM')),
    ('thickness', ('0.20', '0.30', '0.40', '0.45', '0.50', '0.55', '0.60',
                   '0.65', '0.70', '0.75', '0.80', '0.85', '0.90', '0.95',
                   '1.00', '1.20', '1.30', '1.40', '1.50', '1.60', '1.70',
                   '1.80', '1.90', '2.00', '2.20', '2.30', '2.40', '2.50',
                   '2.60', '2.70', '2.80', '2.90', '3.00', '3.0 - 3.2',
                   '3.3 - 3.5', '3.5 - 4.0', '4.0 - 4.5')),
)

# code, name, nature, parent, is_party  -- mirrors the SHREE HMAS group list
GROUPS = [
    ('1001', 'Capital Accounts', 'liability', '', 0),
    ('2001', 'Current Assets', 'asset', '', 0),
    ('2101', 'Accounts Receivable', 'asset', '2001', 1),
    ('2201', 'Bank Accounts', 'asset', '2001', 0),
    ('2301', 'Cash-in-hand', 'asset', '2001', 0),
    ('2401', 'Deposits', 'asset', '2001', 0),
    ('2501', 'Loans & Advances (Asset)', 'asset', '2001', 0),
    ('2601', 'Stock-in-hand', 'asset', '2001', 0),
    ('3001', 'Current Liabilities', 'liability', '', 0),
    ('3101', 'Accounts Payable', 'liability', '3001', 1),
    ('3201', 'Duties & Taxes', 'liability', '3001', 0),
    ('3301', 'Provisions', 'liability', '3001', 0),
    ('4001', 'Fixed Assets', 'asset', '', 0),
    ('5001', 'Investments', 'asset', '', 0),
    ('6001', 'Loans (Liability)', 'liability', '', 0),
    ('6101', 'Bank OD / CC A/c', 'liability', '6001', 0),
    ('6201', 'Secured Loans', 'liability', '6001', 0),
    ('6301', 'Unsecured Loans', 'liability', '6001', 0),
    ('8111', 'Direct Expenses', 'expense', '', 0),
    ('8121', 'Indirect Expenses', 'expense', '', 0),
    ('8201', 'Indirect Income', 'income', '', 0),
    ('8301', 'Purchase Accounts', 'expense', '', 0),
    ('8401', 'Sales Accounts', 'income', '', 0),
    ('9001', 'Suspense Account', 'asset', '', 0),
]

# system_key, group code, ledger name
SYSTEM_LEDGERS = [
    ('SALES', '8401', 'Sales A/c'),
    ('SALES_RETURN', '8401', 'Sales Return A/c'),
    ('PURCHASE', '8301', 'Purchase A/c'),
    ('PURCHASE_RETURN', '8301', 'Purchase Return A/c'),
    ('OUT_CGST', '3201', 'Output CGST'),
    ('OUT_SGST', '3201', 'Output SGST'),
    ('OUT_IGST', '3201', 'Output IGST'),
    ('OUT_CESS', '3201', 'Output Cess'),
    ('IN_CGST', '3201', 'Input CGST'),
    ('IN_SGST', '3201', 'Input SGST'),
    ('IN_IGST', '3201', 'Input IGST'),
    ('IN_CESS', '3201', 'Input Cess'),
    ('CUTTING', '8121', 'Cutting Charges'),
    ('TRANSPORT', '8121', 'Transport Charges'),
    ('LOADING', '8121', 'Loading Charges'),
    ('ROUNDOFF', '8121', 'Round Off'),
    ('CASH', '2301', 'Cash A/c'),
]

DEFAULT_COUNTERS = [
    ('SALE', 'INV-'), ('PURCHASE', 'PUR-'), ('CREDIT', 'CRN-'),
    ('DEBIT', 'DBN-'), ('ORDER', 'SO-'), ('RECEIPT', 'RCP-'),
    ('PAYMENT', 'PAY-'), ('JOURNAL', 'JV-'), ('CONTRA', 'CTR-'),
]

DEFAULT_TERMS = ('1. Goods once sold will not be taken back.\n'
                 '2. Interest @18% p.a. on bills unpaid beyond 30 days.\n'
                 '3. Subject to local jurisdiction.')


# Applied to every connection. WAL is already set on the file itself; the rest
# are per-connection settings SQLite forgets between opens.
#   busy_timeout  -- two browser tabs saving at once wait for each other
#                    instead of one of them failing with "database is locked".
#   synchronous   -- NORMAL is the documented safe pairing with WAL: a crash
#                    cannot corrupt the file, only lose the last commit or two.
#   cache/temp    -- half the books' 70 MB per connection. Answers are kept by
#                    the server as well, so a report asked twice is not
#                    worked out twice; a browser holds six connections, and
#                    six full copies would be more memory than an office PC
#                    should give up.
_PRAGMAS = (
    'PRAGMA foreign_keys=ON',
    'PRAGMA busy_timeout=15000',
    'PRAGMA synchronous=NORMAL',
    'PRAGMA temp_store=MEMORY',
    'PRAGMA cache_size=-32000',          # 32 MB, negative means kibibytes
)


# ------------------------------------------------------------ generation
#
# A number that moves whenever anything is committed to the books -- by this
# server, by an update from SHREE, or by a script run beside it. Results
# worked out from the books are kept until it moves (api.memo, the server's
# answer cache). SQLite's data_version does exactly this, but only as seen
# from a connection that did not make the change itself, so this one never
# writes.
_gen = {'conn': None, 'path': None, 'lock': threading.Lock()}


def generation():
    with _gen['lock']:
        if _gen['conn'] is None or _gen['path'] != DB_PATH:
            if _gen['conn'] is not None:
                _gen['conn'].close()
            _gen['conn'] = sqlite3.connect(DB_PATH, timeout=15,
                                           check_same_thread=False)
            _gen['path'] = DB_PATH
        return _gen['conn'].execute('PRAGMA data_version').fetchone()[0]


def upkeep():
    """What a long-running server does to the file now and then: fresh
    statistics for the planner, and the write-ahead log folded back into the
    books and cut down to nothing, so it does not sit at its largest size."""
    conn = connect()
    try:
        optimize(conn)
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:
        conn.close()


def quiet_windows_errors():
    """No "There is no disk in the drive" box from a background process.

    Windows shows one when a program touches an empty removable drive, and a
    process with no window of its own -- the server, an update from SHREE --
    would sit behind it, waiting for a click nobody can give."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        # SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x8000)
    except Exception:                              # noqa: BLE001
        pass


def optimize(conn, full=False):
    """Keep the query planner's picture of the books current.

    Without statistics SQLite guesses at how selective each index is, and on
    these tables it guessed badly often enough to double the dashboard's time.
    """
    have = conn.execute("SELECT 1 FROM sqlite_master "
                        "WHERE name='sqlite_stat1'").fetchone()
    conn.execute('PRAGMA analysis_limit=1000')
    if full or not have:
        conn.execute('ANALYZE')
    else:
        conn.execute('PRAGMA optimize')
    conn.commit()


# The books are read by whoever owns them and nobody else. Every other
# account on the machine could open the file otherwise -- it holds every
# customer, every GST number and every balance in plain SQLite.
DIR_MODE = 0o700
FILE_MODE = 0o600


def _harden_windows(folder):
    """Restrict the folder to its owner on Windows.

    os.chmod does almost nothing on NTFS -- it toggles the read-only flag and
    leaves the access list alone, so every other account on the PC can still
    open the books. icacls is what actually removes them: inheritance off, so
    the permissions handed down from C:\\Users do not apply, then full control
    granted back to this account alone.
    """
    import subprocess
    user = os.environ.get('USERNAME') or ''
    if not user:
        return False
    domain = os.environ.get('USERDOMAIN') or ''
    # The server runs with no console of its own, so a console program started
    # from it is given a window -- a black rectangle blinking on the screen
    # every time the app starts. CREATE_NO_WINDOW is what stops that.
    quiet = dict(capture_output=True,
                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW',
                                       0x08000000))
    try:
        # A machine joined to a domain, and a PC signed in with a Microsoft
        # account, both want the account named with its domain in front.
        # Plain USERNAME is right on a local account and is the fallback.
        # The grant comes first and the inherited permissions go only once
        # it has worked: the other way round, a grant that failed would
        # leave the folder with no permissions at all, and the app unable
        # to open its own books.
        for who in filter(None, ('%s\\%s' % (domain, user) if domain else '',
                                 user)):
            done = subprocess.run(
                ['icacls', folder, '/grant:r', '%s:(OI)(CI)F' % who, '/T'],
                timeout=60, **quiet)
            if done.returncode == 0:
                subprocess.run(['icacls', folder, '/inheritance:r'],
                               timeout=20, **quiet)
                return True
        return False
    except (OSError, subprocess.SubprocessError):
        return False


def harden(path=None):
    """Leave the books readable by their owner and nobody else.

    Applied on every start, not just at creation: a file copied in from a
    backup, or restored onto a machine with a laxer umask, arrives readable
    by anyone. Windows needs a different tool to the POSIX mode bits -- see
    _harden_windows -- and doing only the chmod there would look like it had
    worked while changing nothing that matters.
    """
    folder = os.path.dirname(path or DB_PATH)
    if not os.path.isdir(folder):
        return
    if os.name == 'nt':
        _harden_windows(folder)
        return
    try:
        os.chmod(folder, DIR_MODE)
        for name in os.listdir(folder):
            full = os.path.join(folder, name)
            if os.path.isfile(full):
                os.chmod(full, FILE_MODE)
    except OSError:
        pass                        # a read-only or foreign filesystem


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


def fy_of(date_str):
    """Indian financial year label for a yyyy-mm-dd date: '2025-26'."""
    try:
        y, m = int(date_str[0:4]), int(date_str[5:7])
    except (TypeError, ValueError, IndexError):
        return ''
    start = y if m >= 4 else y - 1
    return '%d-%02d' % (start, (start + 1) % 100)


# Columns added after the first release, applied to existing databases.
MIGRATIONS = [
    ('company', 'tax_mode', "TEXT NOT NULL DEFAULT 'gst'"),
    ('company', 'logo', "TEXT DEFAULT ''"),
    ('company', 'mobile', "TEXT DEFAULT ''"),
    ('company', 'jurisdiction', "TEXT DEFAULT ''"),
    ('company', 'short_name', "TEXT DEFAULT ''"),
    ('company', 'bank_branch', "TEXT DEFAULT ''"),
    ('company', 'default_gst', 'REAL NOT NULL DEFAULT 18'),
    ('invoice', 'taxable_adj', 'REAL NOT NULL DEFAULT 0'),
    ('invoice', 'from_challan', 'INTEGER'),
    ('invoice', 'billed_company', 'INTEGER'),
    ('invoice', 'billed_invoice', 'INTEGER'),
    ('invoice', 'lr_date', "TEXT DEFAULT ''"),
    ('invoice', 'cases', "TEXT DEFAULT ''"),
    ('ledger', 'home_company', 'INTEGER'),
    ('ledger', 'linked_ledger', 'INTEGER'),
    ('sales_order', 'billed_company', 'INTEGER'),
    ('sales_order', 'billed_invoice', 'INTEGER'),
    ('invoice', 'weight_total', 'REAL NOT NULL DEFAULT 0'),
    ('item', 'by_weight', 'INTEGER NOT NULL DEFAULT 1'),
    ('invoice_item', 'by_weight', 'INTEGER NOT NULL DEFAULT 1'),
    ('sales_order_item', 'by_weight', 'INTEGER NOT NULL DEFAULT 1'),
    ('invoice_item', 'charge_share', 'REAL NOT NULL DEFAULT 0'),
    ('invoice_item', 'weight', 'REAL NOT NULL DEFAULT 0'),
    ('invoice_item', 'incl_price', 'REAL NOT NULL DEFAULT 0'),
    ('sales_order_item', 'weight', 'REAL NOT NULL DEFAULT 0'),
    ('sales_order_item', 'incl_price', 'REAL NOT NULL DEFAULT 0'),
    ('invoice', 'series', "TEXT DEFAULT ''"),
    ('invoice', 'invoice_type', "TEXT NOT NULL DEFAULT 'Debit'"),
    ('invoice', 'sales_ac_id', 'INTEGER'),
    ('invoice', 'driver_name', "TEXT DEFAULT ''"),
    ('invoice', 'payment_terms', "TEXT DEFAULT ''"),
    ('invoice', 'dept', "TEXT DEFAULT ''"),
    ('invoice', 'cutting', 'REAL NOT NULL DEFAULT 0'),
    ('invoice', 'transport', 'REAL NOT NULL DEFAULT 0'),
    ('invoice', 'loading', 'REAL NOT NULL DEFAULT 0'),
    ('invoice_item', 'item_detail', "TEXT DEFAULT ''"),
    # Thickness and cutting size are recorded on the order and travel with the
    # line all the way to the bill. item_detail was on the order screen but had
    # nowhere to be stored, so anything typed there used to be dropped on save.
    ('sales_order_item', 'item_detail', "TEXT DEFAULT ''"),
    ('sales_order_item', 'thickness', "TEXT DEFAULT ''"),
    ('sales_order_item', 'cutting_size', "TEXT DEFAULT ''"),
    ('invoice_item', 'thickness', "TEXT DEFAULT ''"),
    ('counter', 'pad', 'INTEGER NOT NULL DEFAULT 4'),
    ('invoice_item', 'cutting_size', "TEXT DEFAULT ''"),
    # Orders are numbered per financial year like every other document, so the
    # year has to be stored alongside the number to keep them apart.
    ('sales_order', 'fy', "TEXT NOT NULL DEFAULT ''"),
    ('invoice', 'source', "TEXT DEFAULT ''"),
    ('ledger', 'source', "TEXT DEFAULT ''"),
    ('item', 'source', "TEXT DEFAULT ''"),
    ('voucher', 'source', "TEXT DEFAULT ''"),
    ('sales_order', 'source', "TEXT DEFAULT ''"),
    # What the e-way bill portal asks for and the bill never had a box for.
    # Distance is in kilometres; 0 means "let the portal work it out from the
    # two PIN codes".
    ('invoice', 'transporter_gstin', "TEXT DEFAULT ''"),
    ('invoice', 'transport_mode', "TEXT DEFAULT ''"),
    ('invoice', 'transport_distance', 'REAL NOT NULL DEFAULT 0'),
    ('invoice', 'vehicle_type', "TEXT NOT NULL DEFAULT 'R'"),
    ('invoice', 'sub_supply_type', "TEXT DEFAULT ''"),
    ('invoice', 'sub_supply_desc', "TEXT DEFAULT ''"),
    ('invoice', 'transaction_type', "TEXT NOT NULL DEFAULT '1'"),
    # The GST journal a bill raised from a challan posts on that challan's
    # customer in the order firm -- so the bill can rewrite it when it changes
    # and take it along when it is deleted. See api._sync_gst_journal.
    ('invoice', 'gst_voucher', 'INTEGER'),
    # An update from SHREE that ran by itself and found nothing, or failed
    # as the one before it did, counted here instead of adding a line. None
    # runs by itself since 24 Sep 2026; the column keeps the old rows' counts.
    ('sync_run', 'repeats', 'INTEGER NOT NULL DEFAULT 0'),
    # Loose money given to a member of staff, on an account of its own.
    ('staff', 'loose_account', "TEXT NOT NULL DEFAULT ''"),
    # The steel a challan line takes out of the yard's stock (26 Sep 2026):
    # type, quality and thickness off the Challan Stock lists. Blank on every
    # line that moves no stock -- bills, purchases, SHREE's own challans.
    ('invoice_item', 'steel_type', "TEXT NOT NULL DEFAULT ''"),
    ('invoice_item', 'steel_quality', "TEXT NOT NULL DEFAULT ''"),
    ('invoice_item', 'steel_thick', "TEXT NOT NULL DEFAULT ''"),
    # Challan Stock counted by hand (27 Sep 2026): the kilos found, beside
    # the change the count made (the line's weight).
    ('steel_entry_line', 'counted', 'REAL'),
    # Personal Accounts entries brought in from an account in the business
    # books (30 Sep 2026): which account, and the document each came off.
    ('personal_entry', 'source', "TEXT NOT NULL DEFAULT ''"),
    ('personal_entry', 'ref', "TEXT NOT NULL DEFAULT ''"),
    ('personal_entry', 'doc_kind', "TEXT NOT NULL DEFAULT ''"),
    ('personal_entry', 'doc_id', 'INTEGER'),
    ('personal_entry', 'company_id', 'INTEGER'),
    # The Billing Stock category a product is shown under (4 Oct 2026), one
    # of stock_cat's names or '' for none. SHREE's update never writes it.
    ('item', 'stock_cat', "TEXT NOT NULL DEFAULT ''"),
    # Tax collected at source on a purchase bill (8 Oct 2026), typed by hand.
    # Added after the GST and not taxed; the supplier is owed it and it is
    # posted to the firm's TCS A/C PURCHASE.
    ('invoice', 'tcs', 'REAL NOT NULL DEFAULT 0'),
]


# Indexes over columns that only exist once MIGRATIONS has run, so they cannot
# live in SCHEMA -- a database predating the column would fail to open.
MIGRATED_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_inv_challan ON invoice(from_challan)
    WHERE from_challan IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_inv_billed ON invoice(billed_invoice)
    WHERE billed_invoice IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_so_billed ON sales_order(billed_invoice)
    WHERE billed_invoice IS NOT NULL;

-- ix_post_bal covers everything this one did.
DROP INDEX IF EXISTS ix_post_led;

-- Every column that points at an account or an item has an index of its
-- own. Deleting an account makes SQLite look for anything still pointing at
-- it, and without these that meant reading every posting and every voucher
-- line once per account: 34 of the 45 seconds an update from SHREE took.
CREATE INDEX IF NOT EXISTS ix_post_ledger ON posting(ledger_id);
CREATE INDEX IF NOT EXISTS ix_vouline_ledger ON voucher_line(ledger_id);
CREATE INDEX IF NOT EXISTS ix_vou_party ON voucher(party_id)
    WHERE party_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_vou_bank ON voucher(bank_id)
    WHERE bank_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_inv_salesac ON invoice(sales_ac_id)
    WHERE sales_ac_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_ledger_linked ON ledger(linked_ledger)
    WHERE linked_ledger IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_so_party ON sales_order(party_id);
CREATE INDEX IF NOT EXISTS ix_soitem_item ON sales_order_item(item_id)
    WHERE item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_invitem_steel
    ON invoice_item(steel_type, steel_quality, steel_thick)
    WHERE steel_type<>'';
"""


# Document numbers restart every financial year -- SHREE numbers them that way
# and so does next_number() here -- so the year has to be part of what makes a
# number unique. Databases built before that was noticed carry a UNIQUE over
# the number alone, which rejects the second year's bill 1. SQLite cannot drop
# a table constraint, so those three tables are rebuilt in place.
FY_UNIQUE_REBUILD = [
    # A bill is identified by its series as well as its number: SHREE numbers
    # each series from 1, so T/5 and R/5 are two different bills in one year.
    ('invoice', ('UNIQUE(company_id, doc_type, invoice_no)',
                 'UNIQUE(company_id, doc_type, fy, invoice_no)')),
    ('voucher', ('UNIQUE(company_id, kind, voucher_no)',)),
    ('sales_order', ('UNIQUE(company_id, order_no)',)),
]


def _rebuild_fy_unique(conn):
    """Replace a number-only UNIQUE with the year-aware one, keeping the rows."""
    for table, old_clauses in FY_UNIQUE_REBUILD:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                           "AND name=?", (table,)).fetchone()
        sql = (row['sql'] or '').replace('\n', ' ') if row else ''
        if not sql or not any(c in sql for c in old_clauses):
            continue
        cols = [r['name'] for r in conn.execute('PRAGMA table_info(%s)' % table)]
        # Take the fresh definition straight out of SCHEMA so the rebuilt table
        # is byte-for-byte what a new database would have.
        want = re.search(r'CREATE TABLE IF NOT EXISTS %s \(.*?\n\);' % table,
                         SCHEMA, re.S)
        if not want:
            continue
        names = ','.join(cols)
        # legacy_alter_table keeps the RENAME from rewriting the foreign keys
        # in other tables to point at the temporary name.
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('PRAGMA legacy_alter_table=ON')
        try:
            conn.execute('ALTER TABLE %s RENAME TO %s_old_fy' % (table, table))
            conn.execute(want.group(0).rstrip(';'))
            conn.execute('INSERT INTO %s(%s) SELECT %s FROM %s_old_fy'
                         % (table, names, names, table))
            conn.execute('DROP TABLE %s_old_fy' % table)
        finally:
            conn.execute('PRAGMA legacy_alter_table=OFF')
            conn.execute('PRAGMA foreign_keys=ON')


def migrate(conn):
    """Add any missing column, and give every company the charge ledgers."""
    # One PRAGMA per table rather than one per migration: the list is long and
    # every entry was re-reading the same handful of tables.
    have = {}
    for table, column, decl in MIGRATIONS:
        if table not in have:
            have[table] = {r['name'] for r in
                           conn.execute('PRAGMA table_info(%s)' % table)}
        if column not in have[table]:
            conn.execute('ALTER TABLE %s ADD COLUMN %s %s'
                         % (table, column, decl))
            have[table].add(column)
    # An older database stored transport under the name "freight".
    if 'freight' in have.get('invoice', ()):
        conn.execute('UPDATE invoice SET transport=freight '
                     'WHERE transport=0 AND freight<>0')
    _rebuild_fy_unique(conn)
    conn.executescript(MIGRATED_INDEXES)
    # The steel lists start as the user's sheet; after that they are the
    # user's to change, and an emptied list is never refilled behind them.
    if not conn.execute('SELECT 1 FROM steel_list LIMIT 1').fetchone() \
            and get_meta(conn, 'steel_seeded') != '1':
        for kind, names in STEEL_DEFAULTS:
            conn.executemany(
                'INSERT OR IGNORE INTO steel_list(kind, name, sort) VALUES (?,?,?)',
                [(kind, n, i) for i, n in enumerate(names)])
        set_meta(conn, 'steel_seeded', '1')
    # HRPO and HRSP joined the types on 4 Oct 2026, the day SHREE's challans
    # were given their type. Once, like the seeding: the list is the user's.
    if get_meta(conn, 'steel_types_shree') != '1':
        if conn.execute("SELECT 1 FROM steel_list WHERE kind='type'").fetchone():
            top = conn.execute("SELECT MAX(sort) FROM steel_list "
                               "WHERE kind='type'").fetchone()[0]
            conn.executemany(
                'INSERT OR IGNORE INTO steel_list(kind, name, sort) VALUES (?,?,?)',
                [('type', n, top + i) for i, n in enumerate(STEEL_TYPES_ADDED, 1)])
        fill_steel_types(conn)
        set_meta(conn, 'steel_types_shree', '1')
    # Billing Stock's categories the same way: the seven asked for, once,
    # and every product already on the books sorted into them by its name
    # and HSN. After that the list and each product's place are the user's.
    if get_meta(conn, 'stock_cat_seeded') != '1':
        if not conn.execute('SELECT 1 FROM stock_cat LIMIT 1').fetchone():
            conn.executemany('INSERT OR IGNORE INTO stock_cat(name, sort) '
                             'VALUES (?,?)',
                             [(n, i) for i, n in enumerate(STOCK_CAT_DEFAULTS)])
        cats = stock_cats(conn)
        for r in conn.execute("SELECT id, name, hsn FROM item "
                              "WHERE stock_cat=''").fetchall():
            cat = guess_stock_cat(r['name'], r['hsn'], cats)
            if cat:
                conn.execute('UPDATE item SET stock_cat=? WHERE id=?',
                             (cat, r['id']))
        set_meta(conn, 'stock_cat_seeded', '1')

    # One pass over the ledgers that already exist, rather than a lookup per
    # company per system key.
    present = set()
    for r in conn.execute('SELECT company_id, system_key FROM ledger '
                          'WHERE system_key IS NOT NULL'):
        present.add((r['company_id'], r['system_key']))
    groups = {}
    for r in conn.execute('SELECT company_id, code, id FROM account_group'):
        groups.setdefault(r['company_id'], {})[r['code']] = r['id']
    for co in conn.execute('SELECT id FROM company').fetchall():
        gid = groups.get(co['id'], {})
        for key, gcode, name in SYSTEM_LEDGERS:
            if (co['id'], key) not in present and gcode in gid:
                conn.execute('INSERT INTO ledger(company_id, group_id, '
                             'system_key, name) VALUES (?,?,?,?)',
                             (co['id'], gid[gcode], key, name))


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    harden()
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.execute('INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)',
                     ('schema_version', str(SCHEMA_VERSION)))
        if not conn.execute('SELECT COUNT(*) c FROM company').fetchone()['c']:
            create_company(conn, dict(code='MAIN', name='My Company',
                                      state='Gujarat', state_code='24',
                                      terms=DEFAULT_TERMS))
        row = conn.execute('SELECT value FROM meta WHERE key=?',
                           ('active_company',)).fetchone()
        if not row:
            first = conn.execute('SELECT id FROM company ORDER BY id').fetchone()
            set_meta(conn, 'active_company', str(first['id']))
        conn.commit()
        optimize(conn)
    finally:
        conn.close()
    harden()            # the -wal and -shm files appear only once it is open


def get_meta(conn, key, default=None):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def set_meta(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)',
                 (key, str(value)))


def create_company(conn, data):
    """Insert a company plus its standard chart of accounts and counters."""
    fields = ['code', 'name', 'address1', 'address2', 'city', 'state',
              'state_code', 'pin', 'gstin', 'pan', 'phone', 'mobile', 'email',
              'bank_name', 'bank_ac', 'bank_ifsc', 'terms', 'books_from',
              'logo', 'jurisdiction', 'short_name', 'tax_mode', 'default_gst']
    vals = [str(data.get(f, '') or '') for f in fields]
    vals[fields.index('tax_mode')] = (data.get('tax_mode') or 'gst')
    try:
        vals[fields.index('default_gst')] = float(data.get('default_gst') or 18)
    except (TypeError, ValueError):
        vals[fields.index('default_gst')] = 18.0
    cur = conn.execute('INSERT INTO company(%s) VALUES (%s)'
                       % (','.join(fields), ','.join('?' * len(fields))), vals)
    cid = cur.lastrowid

    for i, (code, name, nature, parent, is_party) in enumerate(GROUPS):
        conn.execute(
            'INSERT INTO account_group(company_id, code, name, nature, '
            'parent_code, is_party, sort) VALUES (?,?,?,?,?,?,?)',
            (cid, code, name, nature, parent, is_party, i))

    gid = {r['code']: r['id'] for r in conn.execute(
        'SELECT code, id FROM account_group WHERE company_id=?', (cid,))}
    for key, gcode, name in SYSTEM_LEDGERS:
        conn.execute(
            'INSERT INTO ledger(company_id, group_id, system_key, name) '
            'VALUES (?,?,?,?)', (cid, gid[gcode], key, name))

    for key, prefix in DEFAULT_COUNTERS:
        conn.execute('INSERT INTO counter(company_id, key, fy, prefix, next_no)'
                     ' VALUES (?,?,?,?,1)', (cid, key, '', prefix))
    return cid


def _counter_row(conn, company_id, key, fy):
    """Counter for this year, seeded from how this book was last numbered.

    Numbers restart each financial year, but the way they are spelt does not.
    A book that has been running plain numbers since SHREE keeps running plain
    numbers on the first of April; only a book with no history at all falls
    back to the company-wide default (INV-0001 and its like). Without this,
    every firm's bills, purchases and credit notes changed shape overnight at
    the year end.
    """
    row = conn.execute(
        'SELECT prefix, next_no, pad FROM counter WHERE company_id=? AND key=? '
        'AND fy=?', (company_id, key, fy)).fetchone()
    if row:
        return row['prefix'], row['next_no'], row['pad']
    base = None
    if fy:
        base = conn.execute(
            "SELECT prefix, pad FROM counter WHERE company_id=? AND key=? "
            "AND fy<>'' AND fy<? ORDER BY fy DESC LIMIT 1",
            (company_id, key, fy)).fetchone()
    if base is None:
        base = conn.execute(
            "SELECT prefix, pad FROM counter WHERE company_id=? AND key=? "
            "AND fy=''", (company_id, key)).fetchone()
    prefix = base['prefix'] if base else ''
    pad = base['pad'] if base else 4
    conn.execute('INSERT INTO counter(company_id, key, fy, prefix, next_no, pad)'
                 ' VALUES (?,?,?,?,1,?)', (company_id, key, fy, prefix, pad))
    return prefix, 1, pad


# Where each counter's numbers actually live, so a counter can be re-synced
# against reality: (table, column, filter column, filter value).
NUMBER_SOURCE = {
    'SALE': ('invoice', 'invoice_no', 'doc_type', 'SALE'),
    'PURCHASE': ('invoice', 'invoice_no', 'doc_type', 'PURCHASE'),
    'CREDIT': ('invoice', 'invoice_no', 'doc_type', 'CREDIT'),
    'DEBIT': ('invoice', 'invoice_no', 'doc_type', 'DEBIT'),
    'ORDER': ('sales_order', 'order_no', None, None),
    'RECEIPT': ('voucher', 'voucher_no', 'kind', 'RECEIPT'),
    'PAYMENT': ('voucher', 'voucher_no', 'kind', 'PAYMENT'),
    'JOURNAL': ('voucher', 'voucher_no', 'kind', 'JOURNAL'),
    'CONTRA': ('voucher', 'voucher_no', 'kind', 'CONTRA'),
}


# The books whose numbers are written on paper. A bill or a challan is copied
# out of the book it belongs to, so what is already filed in that book decides
# the next number -- not the counter, which runs ahead the moment a document is
# deleted. Receipts and orders are internal and keep to their counter.
DOCUMENT_BOOKS = ('SALE', 'PURCHASE', 'CREDIT', 'DEBIT')


def _split_number(text):
    """'G 12' -> ('G ', '12'); 'INV-0007' -> ('INV-', '0007').

    None when the number is not a prefix followed by digits -- a number no
    counter could have handed out, and none to be read back off.
    """
    m = re.match(r'^(.*?)(\d+)$', (text or '').strip())
    return (m.group(1), m.group(2)) if m else None


def last_document(conn, company_id, key, fy):
    """The newest bill or challan in this book and year, or None.

    Newest by the date on the paper and then by the order they were entered:
    the one the customer was handed last, which is what says how the next one
    is written -- which challan book it comes out of, which run of bills it
    belongs to.
    """
    if key not in DOCUMENT_BOOKS or not fy:
        return None
    row = conn.execute(
        'SELECT series, invoice_no FROM invoice WHERE company_id=? AND '
        'doc_type=? AND fy=? ORDER BY invoice_date DESC, id DESC LIMIT 1',
        (company_id, key, fy)).fetchone()
    if row:
        return row
    # Numbers restart on the first of April; the way they are written does not.
    # With the year still empty it is last year's last bill that says which run
    # to carry on in, so the first bill of April is not the one bill that comes
    # out with its Sr box blank. Only the spelling is taken from it -- the
    # number is always counted inside the year being numbered.
    return conn.execute(
        "SELECT series, invoice_no FROM invoice WHERE company_id=? AND "
        "doc_type=? AND fy<>'' AND fy<? ORDER BY fy DESC, invoice_date DESC, "
        'id DESC LIMIT 1', (company_id, key, fy)).fetchone()


def _next_letter(prefix):
    """'A ' -> 'B '. The letter on the front of a challan number is the book
    it comes out of; when its digits run out the next book starts.

    Returns None at Z, which is the end of the scheme rather than something to
    silently work around.
    """
    m = re.search(r'([A-Za-z])(\s*)$', prefix or '')
    if not m or m.group(1).upper() == 'Z':
        return None
    return prefix[:m.start(1)] + chr(ord(m.group(1)) + 1) + m.group(2)


def _scan_highest(conn, company_id, key, prefix, fy='', series=None):
    """The highest number already issued under this prefix, or 0.

    `series` narrows it to one run of bills. A firm can keep GT and R going at
    the same time and each is numbered from one, so the highest GT bill says
    nothing at all about the next R.
    """
    src = NUMBER_SOURCE.get(key)
    if not src:
        return 0
    table, col, filt_col, filt_val = src
    start = len(prefix) + 1
    sql = ('SELECT MAX(CAST(substr(%s, ?) AS INTEGER)) AS hi FROM %s '
           'WHERE company_id=?' % (col, table))
    args = [start, company_id]
    # Numbers restart every financial year, so only this year's documents can
    # say what the next one is. Without this, importing a decade of history
    # made the next bill carry on from the highest number ever issued.
    if fy:
        sql += ' AND fy=?'
        args.append(fy)
    if filt_col:
        sql += ' AND %s=?' % filt_col
        args.append(filt_val)
    if series is not None and key in DOCUMENT_BOOKS:
        sql += ' AND series=?'
        args.append(series)
    sql += " AND %s LIKE ? ESCAPE '\\'" % col
    args.append(prefix.replace('\\', '\\\\').replace('%', '\\%')
                .replace('_', '\\_') + '%')
    sql += (" AND substr(%s, ?) GLOB '[0-9]*' "
            "AND substr(%s, ?) NOT GLOB '*[^0-9]*'"
            # (a number past twelve digits is no count -- see sync_counter)
            " AND length(substr(%s, ?)) <= 12" % (col, col, col))
    args += [start, start, start]
    row = conn.execute(sql, args).fetchone()
    return int(row['hi'] or 0) if row else 0


def catch_up(conn, company_id, key, fy='', series=None):
    """Work out the next number for a book, and leave the counter on it.

    Returns (prefix, number, pad, series).

    For a bill or a challan the paper decides. The number is the one after the
    last written in that book, read off the documents themselves: delete the
    newest bill and its number comes straight back, so a challan book never
    skips a page and a bill run never leaves a hole where a mistake was thrown
    away. The counter is left to start a book with nothing in it -- a new
    financial year, or a book just opened under Settings.

    Everything else -- receipts, orders -- keeps to its counter, and is only
    pushed past a number already used.

    The counter row is brought up to date but not committed: whoever is saving
    a document commits it along with the document. Asking what the next number
    is has to be able to leave the books exactly as it found them -- the health
    screen asks inside a savepoint it then rolls back, and a commit here tore
    that savepoint out from under it.
    """
    src = NUMBER_SOURCE.get(key)
    prefix, nxt, pad = _counter_row(conn, company_id, key, fy)
    if not src:
        return prefix, nxt, pad, (series or '')
    was_prefix, was_next, was_pad = prefix, nxt, pad

    if key in DOCUMENT_BOOKS and fy:
        last = last_document(conn, company_id, key, fy)
        last_series = (last['series'] or '') if last else ''
        # Unasked for, the number comes out of the run the last bill was in.
        # A firm running GT and R side by side numbers each of them from one,
        # and a bill dropped into neither took the wrong number and printed
        # with its Sr box empty.
        if series is None:
            series = last_series
        book = _split_number(last['invoice_no']) if last else None
        # A book letter that has moved on the paper takes the counter with it
        # -- but only once something is filed under the counter's own prefix.
        # A prefix with nothing behind it is a book somebody has just opened
        # under Settings, and opening one is their call, not the app's.
        if (book and book[0] != prefix
                and _scan_highest(conn, company_id, key, prefix, fy, series)):
            prefix = book[0]
            pad = len(book[1]) if book[1].startswith('0') else 0
        hi = _scan_highest(conn, company_id, key, prefix, fy, series)
        if hi:
            nxt = hi + 1
        elif last and series != last_series:
            nxt = 1              # a run just being opened starts at one
        # else the counter starts the book: a new year, or a first number
        # typed in under Settings.

        # The counter follows the book itself and not a run kept alongside it,
        # so the number the Settings screen shows stays the one in use.
        own = series == last_series
        if prefix != was_prefix or pad != was_pad or (own and nxt != was_next):
            conn.execute('UPDATE counter SET prefix=?, next_no=?, pad=? WHERE '
                         'company_id=? AND key=? AND fy=?',
                         (prefix, nxt if own else was_next, pad,
                          company_id, key, fy))
    else:
        # The highest number already used is asked of SQLite rather than every
        # number being read back and compared here -- this runs on every screen
        # that shows a "next number" box, against a table of every voucher ever
        # entered.
        highest = max(nxt - 1, _scan_highest(conn, company_id, key, prefix, fy))
        if highest + 1 > nxt:
            nxt = highest + 1
            conn.execute('UPDATE counter SET next_no=? WHERE company_id=? AND '
                         'key=? AND fy=?', (nxt, company_id, key, fy))

    # A 99 is the last of book A. Rolling on to B 01 happens here rather than
    # when the number is handed out, so the box on the entry screen previews
    # the same number that will be saved.
    limit = 10 ** int(pad) - 1 if pad else 0
    while limit and nxt > limit:
        nxt_prefix = _next_letter(prefix)
        if nxt_prefix is None:
            raise ValueError(
                'The challan series has reached %s. Start a new series under '
                'Settings -> Document numbering.'
                % _format_number(prefix, limit, pad))
        prefix = nxt_prefix
        nxt = max(1, _scan_highest(conn, company_id, key, prefix, fy, series) + 1)
        conn.execute('UPDATE counter SET prefix=?, next_no=? WHERE '
                     'company_id=? AND key=? AND fy=?',
                     (prefix, nxt, company_id, key, fy))
    return prefix, nxt, pad, (series or '')


def _format_number(prefix, num, pad):
    """INV-0007 at four digits, a plain 7 at none."""
    return '%s%0*d' % (prefix, max(0, int(pad or 0)), num)


def next_number(conn, company_id, key, fy='', series=None):
    prefix, num, pad, _sr = catch_up(conn, company_id, key, fy, series)
    # Forward only: a number handed to a run kept alongside the book must not
    # drag the book's own counter back down.
    conn.execute('UPDATE counter SET next_no=? WHERE company_id=? AND key=? '
                 'AND fy=? AND next_no<?',
                 (num + 1, company_id, key, fy, num + 1))
    return _format_number(prefix, num, pad)


def peek_number(conn, company_id, key, fy='', series=None):
    """The number a new document would get, without consuming it."""
    prefix, num, pad, _sr = catch_up(conn, company_id, key, fy, series)
    return _format_number(prefix, num, pad)


def peek_document(conn, company_id, key, fy='', series=None):
    """The series and the number a new document would be given.

    The entry screen fills both boxes from this, so a bill carries on in the
    run the last one was in instead of coming out with its Sr box empty.
    """
    prefix, num, pad, sr = catch_up(conn, company_id, key, fy, series)
    return {'number': _format_number(prefix, num, pad), 'series': sr}


def sync_counter(conn, company_id, key, fy, used):
    """Advance the counter past a number the client supplied itself.

    The entry screen fills the number box from peek_number() and posts it back,
    so without this the same number would be offered again next time.
    """
    prefix, nxt, _pad = _counter_row(conn, company_id, key, fy)
    used = (used or '').strip()
    if not used.startswith(prefix):
        return
    tail = used[len(prefix):]
    # A number too long to be a count -- a slip of the finger on the keys, or
    # a pasted reference -- is kept as typed but moves no counter: it would
    # not fit SQLite's integer and failed the save (29 Sep 2026).
    if not tail.isdigit() or len(tail) > 12:
        return
    n = int(tail)
    if n >= nxt:
        conn.execute('UPDATE counter SET next_no=? WHERE company_id=? AND '
                     'key=? AND fy=?', (n + 1, company_id, key, fy))
