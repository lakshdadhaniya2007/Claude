#!/usr/bin/env python3
"""Join each imported challan to the GST bill that was raised from it.

    python3 app/link_challans.py                    # show what it would do
    python3 app/link_challans.py --apply
    python3 app/link_challans.py --apply --fy 2025-26
    python3 app/link_challans.py --csv /tmp/pairs.csv
    python3 app/link_challans.py --undo --apply     # let every pair go again

A challan is raised in a no-tax firm and its tax invoice in one of the GST
firms, and the app records which is which: the bill carries `from_challan`, the
challan carries `billed_company` and `billed_invoice`. Documents made in the app
are joined as they are raised. The ten years imported out of SHREE are not --
SHREE never recorded the pair. Its bill screen has a "Challan No" box and it is
empty on 14,652 of the 14,668 bills in the registers; the sixteen that are not
hold a money amount that has strayed in from another box. Nothing else on
either side names the other document -- `SITM.CHALLANID` and `OCHNO` are empty
throughout, and `REF` carries only the document's own number. So the pair has
to be recognised from the goods themselves.

**Not every challan has a bill, and not every bill has a challan.** Plenty of
challans are cash sales that were never invoiced, and the GST firms bill far
more than ever goes out on a challan. Anything this cannot recognise is left
alone; that is the normal case, not a failure.

What counts as the same goods
-----------------------------
All four have to hold, or the challan is left alone:

1. **The same customer.** The two firms keep separate ledgers, so the customer
   is matched through `linked_ledger` where allocate_customers.py has set it,
   and otherwise by name -- allowing for a trailing contact name and for the
   spelling drifting between the two machines (LALKRUPA / LAL KRUPA).
2. **The same weight**, to half a kilo. Steel is sold by weight and both
   documents carry the total; two different loads landing on the same total to
   within 500g is not something that happens by accident.
3. **The same money.** The bill is the challan plus GST, so the bill's taxable
   value is the challan's total -- or, on a few hundred where the challan's
   cutting or loading was not billed on, its goods value. They are allowed to
   differ by up to twenty rupees: the two systems round differently and 98% of
   the pairs found here land within five. A larger gap means it is not the same
   load, or it was re-priced -- either way it is not certain enough to write
   down.
4. **One answer.** Exactly one bill fits the challan, and no other challan fits
   that bill. `from_challan` holds a single document, so a bill that could have
   come from either of two challans is left for a person to decide -- with two
   exceptions that are not a choice at all. A bill SHREE cancelled with a return
   of the whole of it gives way to the bill that replaced it (GHANSHYAM STEEL:
   A 60 billed as GT 21, returned as R 3, billed again as GT 26). And two
   identical loads billed twice over are paired in date order -- which of two
   identical bills belongs to which identical challan changes nothing.

The date is a filter rather than evidence: the bill follows its challan within a
day or two (96% inside a week), so only bills dated from two days before the
challan to thirty days after are considered at all.

When the bill is not simply the challan plus tax
------------------------------------------------
Asked for on 19 Sep 2026 -- "a bill was there for a challan, but the software
showed it as not billed". About 630 more challans had their bill in the books
in a shape the four rules above cannot see. They are recognised after those,
from what is left, strongest first, and each still has to be the only answer:

- **Billed in parts, or billed together.** One challan on two or three bills
  (SHREE SANTOSH E 96, 4,143 kg = GT 193 3,138 kg + GT 202 1,005 kg, to the
  rupee), two or three challans on one bill (GOPI METAL D 75 + D 33 = GT 154),
  or loads re-cut between them (AIWA D 59 + D 61 = GT 113 + GT 114). The kilos
  and the money of each side have to add up to the other's, and a return on
  the challan side counts against its load (MICRO E 71 - R 11 = GT 149). A bill
  here is sometimes written before the goods go out, so it may be dated up to
  twenty days before the first challan.
- **Re-priced**: the same weight to half a kilo, the value within ten per cent.
- **Close on both**: within a kilo (or 0.2%) and half a per cent of the value.
- **Re-weighed**: the same money to twenty rupees on a load of a thousand
  rupees or more, the weight changed on the bill -- the VAT years did this.

These are written on the challan (`billed_invoice`, the first of its bills) and
in the `challan_bill` table, which also says how the pair was recognised. The
bill's `from_challan` is left empty: editing the challan must never rebuild a
bill it only partly matches, deleting the challan must not take another load's
bill with it, and SHREE already posted the GST journal for it.

Nothing else on either document is touched -- not the number, not the value,
not the customer. Running it again only ever adds pairs it could not see before,
and a pair already recorded is never disturbed.
"""
import argparse
import bisect
import csv
import datetime
import difflib
import itertools
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db    # noqa: E402

# How far apart the two documents may be dated. A bill is written the same day
# or a day or two later; a couple of days early happens when it is raised
# against a load still going out.
DAYS_BEFORE, DAYS_AFTER = 2, 30

# Half a kilo on a weighbridge total, and twenty rupees between two systems
# that round differently.
WEIGHT_TOL = 0.5
AMOUNT_TOL = 20.0

# How alike two spellings of a name have to be before they are taken for the
# same customer. LAL KRUPA / LALKRUPA scores 0.98, DHARMARAJ / DHARMRAJ 0.98;
# two genuinely different customers score well under a half.
NAME_RATIO = 0.85

# The looser matches, tried only on what the four rules leave.
REPRICED_SHARE = 0.10           # same weight, value within ten per cent
NEAR_KG, NEAR_WEIGHT = 1.0, 0.002   # within a kilo, or 0.2% of the load ...
NEAR_VALUE = 0.005              # ... and half a per cent of the value
REWEIGHED_FLOOR = 1000.0        # money alone decides only on a real load

# A challan billed in parts, or challans billed together: up to three a side.
GROUP_MAX = 3
GROUP_EARLY = 20                # a bill written ahead of its first challan
GROUP_KG, GROUP_WEIGHT = 1.0, 0.002
GROUP_VALUE = 0.0002            # plus twenty rupees a document

# A bill counts as cancelled when a return of the whole of it follows within
# this many days.
CANCEL_DAYS = 60

LOOSE = ('renamed', 'other name', 'near', 'repriced', 'reweighed')

# Two spellings of one word: AMBICA / AMBIKA 0.83, METEL / METAL 0.8,
# SUPERIER / SUPERIOR 0.88, CIMENT / CEMENT 0.83.
WORD_RATIO = 0.8

# Words that say what kind of business a name is, not whose. Two customers
# sharing one of these are no more alike than any two.
GENERIC = set('''
    SHREE SHRI SRI JAY JAI OM NEW THE AND CO COMPANY ENTERPRISE ENTERPRISES
    INDUSTRIES INDUSTRY INDUSTRIAL IND INDS ENGINEERING ENGINEERS ENGINEER
    ENG ENGG WORKS WORK METAL METALS STEEL STEELS TRADERS TRADING TRADE
    MANUFACTURERS MANUFACTURER MANUFACTURE MANUFACTURING MFG PVT LTD PRIVATE
    LIMITED LLP CORPORATION CORP AUTO AGRO TECH TECHNO FABRICATION
    FABRICATORS PRODUCTS PRODUCT SALES MARKETING INDIA GUJARAT RAJKOT TOOLS
    MACHINE MACHINES MACHINERY EQUIPMENT EQUIPMENTS GROUP BROTHERS BROS SONS
    CONSTRUCTION ELECTRICALS ELECTRIC PLAST PLASTIC STORES AGENCY AGENCIES
    SERVICES SOLUTIONS TECHNOLOGY TECHNOLOGIES SYSTEMS INTERNATIONAL GLOBAL
    OVERSEAS EXPORTS IMPORTS SHIV'''.split())


def norm(name):
    """A name with the punctuation and the double spaces taken out."""
    return re.sub(r'[^A-Z0-9 ]', '', re.sub(r'\s+', ' ', (name or '')).upper()).strip()


def day(text):
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except (TypeError, ValueError, IndexError):
        return None


def _leading_words(a, b):
    """How many words the two names open with in common.

    A word counts as the same when one is the start of the other, which is how
    a trade name is shortened on one machine and not the other: SHREE VALLABH
    IND is SHREE VALLABH INDUSTRIES, ANJANI ENG CO is ANJANI ENGINEERING.
    """
    n = 0
    for x, y in zip(a.split(), b.split()):
        short, long_ = (x, y) if len(x) <= len(y) else (y, x)
        if len(short) >= 3 and long_.startswith(short):
            n += 1
        else:
            break
    return n


def same_customer(challan, bill):
    """Why these two documents are for the same customer, or None.

    The order firm and the billing firm each keep their own copy of a customer.
    Where the two have been tied together that is the answer; otherwise the
    name is all there is, and it is the same name typed twice on two machines
    -- shortened on one, with the proprietor's name or his village on the end
    of the other, and now and then simply misspelt.

    The name is the last word rather than the first: two documents already have
    to agree on the weight to half a kilo and on the money to twenty rupees
    before it is asked at all. What it is really there to catch is the rare
    coincidence -- two unrelated loads that happen to weigh and cost the same.
    """
    if challan['linked_ledger'] and challan['linked_ledger'] == bill['party_id']:
        return 'linked'
    a, b = norm(challan['party_name']), norm(bill['party_name'])
    if not a or not b:
        return None
    if a == b:
        return 'same name'
    lead = _leading_words(a, b)
    if lead and lead >= min(2, len(a.split()), len(b.split())):
        return 'same name written shorter'
    if difflib.SequenceMatcher(None, a, b).ratio() >= NAME_RATIO:
        return 'name spelt differently'
    return None


def _word_same(x, y):
    """One word written twice: the same, one the start of the other (ENG,
    ENGINEERING), or spelt a letter or two apart."""
    if x == y:
        return True
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    if len(short) >= 3 and long_.startswith(short):
        return True
    return len(short) >= 4 and \
        difflib.SequenceMatcher(None, x, y).ratio() >= WORD_RATIO


def _own_word(w):
    """A word that says whose business it is: not a kind of trade, and not
    the contact's first name the order firm tacks on (NILESHBHAI)."""
    return len(w) >= 4 and w not in GENERIC and \
        not w.endswith(('BHAI', 'BHA', 'BEN'))


def kin(challan, bill):
    """Whether two names are one customer written two ways, allowing what
    `same_customer` does not.

    `same_customer` skips words under three letters, so "D PATEL SALES
    DHARAMBHAI" and "D PATEL SALES" -- 54 loads of the same goods on the same
    days -- were two customers, and so was every name that opens with its
    initials. Here an initial counts when it is the same initial, a word may
    be spelt a letter or two apart (SHREE AMBICA TOOLS ENG. / SHREE AMBIKA
    TOOLS ENGINEERING), and the shorter name may sit inside the longer one
    (SHIV MANUFACTURE / SHIV AUTO & MANUFACTURE NILESH). Two words in common
    are not enough when both are only initials or trade words: M K ENTERPRISE
    and M K ENGINEERING stay apart.
    """
    if same_customer(challan, bill):
        return True
    wa, wb = norm(challan['party_name']).split(), norm(bill['party_name']).split()
    if not wa or not wb:
        return False
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    lead = 0
    for x, y in zip(wa, wb):
        if not _word_same(x, y):
            break
        lead += 1
    if lead >= 2 and (lead == len(short) or lead >= 3 or any(
            _own_word(w) for w in wa[:lead] + wb[:lead])):
        return True
    inside = len(short) >= 2 and all(any(_word_same(x, y) for y in long_)
                                     for x in short)
    return inside and any(_own_word(x) for x in short)


def akin(challan, bill):
    """Looser still, for loads that agree to the kilo and the rupee: a name
    word of its own in common (JAY YAMUNA METEL / YAMUNA AGRO AUTOTECH), or
    the whole shorter name inside the longer."""
    if kin(challan, bill):
        return True
    wa, wb = norm(challan['party_name']).split(), norm(bill['party_name']).split()
    if not wa or not wb:
        return False
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if len(short) >= 2 and all(any(_word_same(x, y) for y in long_)
                               for x in short):
        return True
    return any(_own_word(x) and any(_word_same(x, y) for y in wb) for x in wa)


def has_link_table(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                        "name='challan_bill'").fetchone() is not None


def load(conn, fy=None, firm=None):
    """Every challan and every bill, each marked `free` when nothing names its
    other half yet.

    The ones already paired are read too: a loose match is only believed when
    nothing else looks like it, and a bill paired last month still counts as
    something else."""
    where_fy = ' AND i.fy=?' if fy else ''
    args_fy = [fy] if fy else []

    firms = {r['id']: r for r in conn.execute(
        'SELECT id, code, name, tax_mode FROM company')}
    if firm:
        no_tax = [f['id'] for f in firms.values() if f['code'] == firm]
    else:
        # Challans are raised in the firm that takes the orders, and only
        # there. The other no-tax firms are accounts books: their sale entries
        # are the GST firms' own bills written into the cash book a second
        # time, kept in a company of their own so the turnover is not counted
        # twice. Reading those as challans would tie a bill to itself.
        order_firm = db.get_meta(conn, 'order_company')
        no_tax = [int(order_firm)] if order_firm else []
        if not any(i in firms for i in no_tax):
            # Never chosen in Settings -- a book started empty, as the PC's
            # was on 6 Oct 2026 -- the firm is the one the screens take
            # (api.order_company_id): the first no-tax firm. Without it
            # nothing was paired, and every challan read as not billed.
            first = conn.execute("SELECT id FROM company WHERE tax_mode='none' "
                                 'AND active=1 ORDER BY id').fetchone()
            no_tax = [first['id']] if first else []
    no_tax = [i for i in no_tax if i in firms]
    gst = [f['id'] for f in firms.values() if f['tax_mode'] != 'none']
    if not no_tax or not gst:
        return firms, [], []

    def rows(company_ids, free):
        ph = ','.join('?' * len(company_ids))
        return [dict(r) for r in conn.execute(
            'SELECT i.id, i.company_id, i.series, i.invoice_no, i.invoice_date, '
            '  i.fy, i.weight_total, i.taxable, i.grand_total, i.party_id, '
            '  i.from_challan, i.billed_invoice, l.name AS party_name, '
            '  l.linked_ledger, (%s) AS free '
            'FROM invoice i JOIN ledger l ON l.id=i.party_id '
            "WHERE i.company_id IN (%s) AND i.doc_type='SALE'%s "
            'ORDER BY i.invoice_date, i.id'
            % (free, ph, where_fy), company_ids + args_fy)]

    challans = rows(no_tax, 'i.billed_invoice IS NULL')
    # A bill already named by a challan is taken, whichever way it was named.
    taken = 'i.from_challan IS NULL AND NOT EXISTS (SELECT 1 FROM invoice c ' \
            'WHERE c.billed_invoice=i.id)'
    if has_link_table(conn):
        taken += ' AND NOT EXISTS (SELECT 1 FROM challan_bill cb ' \
                 'WHERE cb.bill_id=i.id)'
    bills = rows(gst, taken)
    return firms, challans, bills


def cancelled(docs):
    """{document id: the return that cancelled it} among one side's documents.

    SHREE cancels a bill by writing a return of the whole of it -- the same
    customer, the same weight and the same money the other way -- and then,
    usually, bills the load again. Of two bills that both fit a challan, the one
    cancelled is not the one it went out on.
    """
    out = {}
    by = defaultdict(list)
    for d in docs:
        by[(d['company_id'], d['party_id'])].append(d)
    for ds in by.values():
        pos = [d for d in ds if d['grand_total'] > 0]
        for n in sorted((d for d in ds if d['grand_total'] < 0),
                        key=lambda d: (d['invoice_date'], d['id'])):
            nd = day(n['invoice_date'])
            fit = [p for p in pos if p['id'] not in out
                   and abs(p['grand_total'] + n['grand_total']) <= 1
                   and abs(p['weight_total'] + n['weight_total']) <= WEIGHT_TOL
                   and nd and day(p['invoice_date'])
                   and 0 <= (nd - day(p['invoice_date'])).days <= CANCEL_DAYS]
            # The latest bill before the return is the one it cancels; one
            # dated the same day is usually the bill that replaced it
            # (GHANSHYAM STEEL: GT 21 on 12 Apr, R 3 and GT 26 on 17 Apr), so
            # a same-day bill is taken only when there is no earlier one --
            # the first written.
            before = [p for p in fit if p['invoice_date'] < n['invoice_date']]
            if before:
                out[max(before, key=lambda d: (d['invoice_date'], d['id']))['id']] \
                    = n['id']
            elif fit:
                out[min(fit, key=lambda d: d['id'])['id']] = n['id']
    return out


def handle(name):
    """The first three letters of a name, spaces aside: LAL KRUPA and LALKRUPA
    both file under LAL. Only a first look for whole groups -- the customer is
    then asked properly."""
    return norm(name).replace(' ', '')[:3]


class Book:
    """The challans and bills still waiting, sorted the ways they are looked up
    -- bills by weight, by value, and by customer and date; challans by
    customer and date -- so a challan is only ever held up against the few
    bills that could be its own, and never against the whole decade."""

    def __init__(self, challans, bills):
        self.challans, self.bills = list(challans), list(bills)
        self.day = {d['id']: day(d['invoice_date'])
                    for d in self.challans + self.bills}
        dated = [b for b in self.bills if self.day[b['id']]]
        self.by_weight = sorted(dated, key=lambda b: b['weight_total'])
        self.weights = [b['weight_total'] for b in self.by_weight]
        self.by_value = sorted(dated, key=lambda b: b['taxable'])
        self.values = [b['taxable'] for b in self.by_value]
        self.bills_of = defaultdict(list)       # handle or ledger -> by date
        for b in sorted(dated, key=lambda b: (self.day[b['id']], b['id'])):
            self.bills_of[handle(b['party_name'])].append(b)
            self.bills_of[b['party_id']].append(b)
        dated_c = [c for c in self.challans if self.day[c['id']]]
        self.c_by_weight = sorted(dated_c, key=lambda c: c['weight_total'])
        self.c_weights = [c['weight_total'] for c in self.c_by_weight]
        self.c_by_value = {k: sorted(dated_c, key=lambda c: c[k])
                           for k in ('grand_total', 'taxable')}
        self.c_values = {k: [c[k] for c in v] for k, v in self.c_by_value.items()}
        self.challans_of = defaultdict(list)
        for c in sorted((c for c in self.challans if self.day[c['id']]),
                        key=lambda c: (self.day[c['id']], c['id'])):
            self.challans_of[c['party_id']].append(c)
            if c['linked_ledger']:
                self.challans_of[('linked', c['linked_ledger'])].append(c)
        self.dates = {k: [self.day[d['id']] for d in v]
                      for k, v in list(self.bills_of.items())
                      + [(('c', k), v) for k, v in self.challans_of.items()]}
        self.cancelled_bills = cancelled(self.bills)
        self.cancelled_challans = cancelled(self.challans)
        self._who = {}

    def customer(self, ch, b):
        key = (ch['party_id'], b['party_id'])
        if key not in self._who:
            self._who[key] = same_customer(ch, b)
        return self._who[key]

    def _named(self, test, ch, b):
        key = (test.__name__, ch['party_id'], b['party_id'])
        if key not in self._who:
            self._who[key] = test(ch, b)
        return self._who[key]

    def kind_of(self, ch, b):
        """How this bill fits this challan, customer included, or None.

        An exact load under the same customer is 'same'; under a name only
        like it, 'renamed'. The looser shapes need the names to be `kin`.
        """
        kind = fit(ch, b)
        if kind is None:
            return None
        if kind == 'same':
            if self.customer(ch, b):
                return 'same'
            return 'renamed' if self._named(akin, ch, b) else None
        return kind if self._named(kin, ch, b) else None

    def exact(self, ch, b):
        """The same load to the kilo and the rupee, whoever it names."""
        g = self.gap(ch, b)
        return g is not None and -DAYS_BEFORE <= g <= DAYS_AFTER and \
            fit(ch, b) == 'same'

    def gap(self, ch, b):
        """Days from the challan to the bill, or None when either is undated."""
        cd, bd = self.day[ch['id']], self.day[b['id']]
        return (bd - cd).days if cd and bd else None

    def fits(self, ch, kinds, free=None):
        """[(bill, kind)] for every bill this challan fits in one of the kinds,
        dated from two days before it to thirty after -- only the free ones
        when `free` is given."""
        w = ch['weight_total']
        found = {}

        def take(docs, keys, lo, hi):
            for b in docs[bisect.bisect_left(keys, lo):bisect.bisect_right(keys, hi)]:
                found[b['id']] = b
        span = 0.0
        if w and ({'same', 'repriced'} & set(kinds)):
            span = WEIGHT_TOL
        if w and 'near' in kinds:
            span = max(span, NEAR_KG, NEAR_WEIGHT * abs(w))
        if span:
            take(self.by_weight, self.weights, w - span, w + span)
        if 'reweighed' in kinds:
            for v in {ch['grand_total'], ch['taxable']}:
                take(self.by_value, self.values, v - AMOUNT_TOL, v + AMOUNT_TOL)
        out = []
        for bid in sorted(found):
            b = found[bid]
            g = self.gap(ch, b)
            if (free is not None and bid not in free) or g is None \
                    or not -DAYS_BEFORE <= g <= DAYS_AFTER:
                continue
            kind = self.kind_of(ch, b)
            if kind in kinds:
                out.append((b, kind))
        return out

    def challans_fitting(self, b, kinds, any_name=False):
        """Every challan, paired or not, that this bill fits in one of the
        kinds -- the same question as `fits`, asked from the bill's side.
        `any_name`: the same load to the kilo and the rupee, whoever it
        names."""
        w = b['weight_total']
        found = {}

        def take(docs, keys, lo, hi):
            for c in docs[bisect.bisect_left(keys, lo):bisect.bisect_right(keys, hi)]:
                found[c['id']] = c
        # A challan's 'near' allowance is 0.2% of its own weight; a bill's
        # weight is within a kilo or so of it, so 0.3% of the bill's is room
        # enough, and `fit` then decides exactly.
        span = max(WEIGHT_TOL, NEAR_KG, 0.003 * abs(w))
        if w:
            take(self.c_by_weight, self.c_weights, w - span, w + span)
        if 'reweighed' in kinds:
            for k in ('grand_total', 'taxable'):
                take(self.c_by_value[k], self.c_values[k],
                     b['taxable'] - AMOUNT_TOL, b['taxable'] + AMOUNT_TOL)
        out = []
        for cid in sorted(found):
            c = found[cid]
            g = self.gap(c, b)
            if g is None or not -DAYS_BEFORE <= g <= DAYS_AFTER:
                continue
            if (self.exact(c, b) if any_name else self.kind_of(c, b) in kinds):
                out.append(c)
        return out

    def bills_near(self, ch, before, after, free):
        """The free bills of this challan's customer dated from `before` days
        ahead of it to `after` days behind."""
        cd = self.day[ch['id']]
        if not cd:
            return []
        lo, hi = cd - datetime.timedelta(days=before), cd + datetime.timedelta(days=after)
        seen, out = set(), []
        for key in (handle(ch['party_name']), ch['linked_ledger']):
            docs = self.bills_of.get(key)
            if not docs:
                continue
            days = self.dates[key]
            for b in docs[bisect.bisect_left(days, lo):bisect.bisect_right(days, hi)]:
                if b['id'] in free and b['id'] not in seen and \
                        self._named(kin, ch, b):
                    seen.add(b['id'])
                    out.append(b)
        return out

    def mates(self, a, free):
        """The free challans of this one's customer raised in the month after
        it -- later in date, or the same day and later in number."""
        ad = self.day[a['id']]
        seen, out = {a['id']}, []
        keys = [a['party_id']]
        if a['linked_ledger']:
            keys.append(('linked', a['linked_ledger']))
        for key in keys:
            docs, days = self.challans_of.get(key), self.dates.get(('c', key))
            if not docs:
                continue
            for c in docs[bisect.bisect_left(days, ad):bisect.bisect_right(
                    days, ad + datetime.timedelta(days=DAYS_AFTER))]:
                if c['id'] in free and c['id'] not in seen and (
                        self.day[c['id']] > ad or c['id'] > a['id']):
                    seen.add(c['id'])
                    out.append(c)
        return out


def fit(ch, b):
    """How a single bill fits a single challan, or None -- the customer and the
    date are asked separately.

    'same' is the four rules; the rest are the looser shapes, taken only once
    the four have found everything they can.
    """
    if (ch['grand_total'] < 0) != (b['grand_total'] < 0):
        return None
    w_same = abs(b['weight_total'] - ch['weight_total']) <= WEIGHT_TOL
    v_same = (abs(b['taxable'] - ch['grand_total']) <= AMOUNT_TOL
              or abs(b['taxable'] - ch['taxable']) <= AMOUNT_TOL)
    if w_same and v_same and ch['weight_total']:
        return 'same'
    value = abs(ch['grand_total'])
    if not value:
        return None
    # Against the challan's total, or its goods where the cutting or loading
    # was not billed on -- as the four rules measure it.
    gap = min(abs(b['taxable'] - ch['grand_total']),
              abs(b['taxable'] - ch['taxable']))
    w = abs(ch['weight_total'])
    if w and abs(b['weight_total'] - ch['weight_total']) <= max(NEAR_KG, NEAR_WEIGHT * w) \
            and gap <= NEAR_VALUE * value:
        return 'near'
    if w and w_same and gap <= REPRICED_SHARE * value:
        return 'repriced'
    if v_same and value >= REWEIGHED_FLOOR:
        return 'reweighed'
    return None


# What else counts as looking like the pair, for each loose kind: anything
# that fits as well as it, or better.
STRONGER = {
    'repriced': ('same', 'renamed', 'near', 'repriced'),
    'near': ('same', 'renamed', 'near', 'repriced'),
    'reweighed': ('same', 'renamed', 'near', 'repriced', 'reweighed'),
}


def _alone(book, ch, b, rivals):
    """Whether this challan and this bill fit nothing but each other."""
    other_b = [x for x, _k in book.fits(ch, rivals)
               if x['id'] not in book.cancelled_bills]
    if [x['id'] for x in other_b] != [b['id']]:
        return False
    other_c = [x for x in book.challans_fitting(b, rivals)
               if x['id'] not in book.cancelled_challans]
    return [x['id'] for x in other_c] == [ch['id']]


def pair_by_numbers(book, free_c, free_b):
    """Exact loads the customer's name turned away.

    The same weight to half a kilo and the same money to twenty rupees, the
    bill in the month after the challan, and nothing else in the books,
    paired or not, matching either of them to the kilo and the rupee -- under
    a like name for a like-named pair, under any name for the rest. With the
    real bill of each of 7,964 known pairs taken out, a load that exact found
    another customer's bill for only six. What is left to the name:

    - 'renamed': the two names are one customer written two ways (`akin`) --
      D PATEL SALES DHARAMBHAI / D PATEL SALES, 54 loads;
    - 'other name': the names have nothing in common, but these two parties
      meet like this on at least two loads -- goods sent to one and billed to
      another (SADGURU INDUSTRIES' challans billed to VRAJBHUMI TECHNOCAST).
      Once is a coincidence; it is left.
    """
    found = []
    for cid in sorted(free_c):
        ch = free_c[cid]
        w = ch['weight_total']
        if not w:
            continue
        near = book.by_weight[bisect.bisect_left(book.weights, w - WEIGHT_TOL):
                              bisect.bisect_right(book.weights, w + WEIGHT_TOL)]
        bs = [b for b in near if book.exact(ch, b)
              and b['id'] not in book.cancelled_bills]
        # Under a name like the challan's, only a look-alike under a name
        # like it too stands in the way: another customer's 715 kg for
        # Rs 50,765 (SHREE SANTOSH F 71, paired with its own bill) does not
        # make D PATEL SALES' G 70 and GT 215 any less one load.
        own = [b for b in bs if book._named(akin, ch, b)]
        if own:
            if len(own) != 1 or own[0]['id'] not in free_b:
                continue
            b = own[0]
            cs = [c for c in book.challans_fitting(b, (), any_name=True)
                  if c['id'] not in book.cancelled_challans
                  and book._named(akin, c, b)]
        else:
            if len(bs) != 1 or bs[0]['id'] not in free_b:
                continue
            b = bs[0]
            cs = [c for c in book.challans_fitting(b, (), any_name=True)
                  if c['id'] not in book.cancelled_challans]
        if [c['id'] for c in cs] == [cid]:
            found.append((ch, b))
    met = Counter((ch['party_id'], b['party_id']) for ch, b in found)
    pairs = []
    for ch, b in found:
        if book.customer(ch, b):
            continue            # the four rules' own, left to them
        if book._named(akin, ch, b):
            kind = 'renamed'
        elif met[(ch['party_id'], b['party_id'])] >= 2:
            kind = 'other name'
        else:
            continue
        pairs.append((ch, b, kind))
        del free_c[ch['id']]
        del free_b[b['id']]
    return pairs


def pair_single(book, kinds, free_c, free_b, note=None):
    """One challan to one bill, by the kinds given, until nothing more pairs.

    Each challan takes only its strongest kind of fit, a cancelled bill gives
    way to a live one, and a bill two challans fit is left -- unless the
    challans and the bills are the same load over and over, which are then
    taken in date order. A bill once taken stops being a candidate for
    anything else, and a challan that had two to choose between is often left
    with one, so it goes round again on what is left until nothing changes.
    """
    rank = {k: i for i, k in enumerate(kinds)}
    every = {cid: book.fits(free_c[cid], kinds, free_b) for cid in sorted(free_c)}
    # A loose fit is believed only when nothing else looks like it: no other
    # bill of that customer in the month, paired or not, fits the challan as
    # well or better, and no other challan fits the bill. A customer who takes
    # the same 2,025 kg every fortnight is exactly where a re-priced bill
    # would otherwise be pinned on the wrong load.
    loose = 'same' not in kinds
    rivals = STRONGER[kinds[-1]] if loose else ()
    if note is not None:
        for cid in sorted(free_c):
            ch = free_c[cid]
            if every[cid] or len(note) >= 40 or not ch['weight_total']:
                continue
            for b in book.bills_near(ch, DAYS_BEFORE, DAYS_AFTER, free_b):
                if abs(b['weight_total'] - ch['weight_total']) <= WEIGHT_TOL:
                    note.append((ch, b, round(b['taxable'] - ch['grand_total'], 2)))
                    break
    pairs = []
    while True:
        wants = {}                  # challan id -> [(bill, kind)]
        for cid, got in every.items():
            if cid not in free_c:
                continue
            got = [(b, f) for b, f in got if b['id'] in free_b]
            if not got:
                continue
            best = min(rank[f] for _b, f in got)
            got = [(b, f) for b, f in got if rank[f] == best]
            live = [(b, f) for b, f in got if b['id'] not in book.cancelled_bills]
            if len(got) > 1 and len(live) == 1:
                got = live
            wants[cid] = got

        found, claims = [], defaultdict(list)
        for cid, got in wants.items():
            if len(got) != 1:
                continue
            b = got[0][0]
            if loose and not _alone(book, free_c[cid], b, rivals):
                continue
            claims[b['id']].append((free_c[cid], b, got[0][1]))
        for ws in claims.values():
            # One bill, two challans that both fit it. `from_challan` holds one
            # document, and guessing which would be a tax invoice pointed at
            # the wrong load -- unless every challan but one was cancelled.
            live = [w for w in ws if w[0]['id'] not in book.cancelled_challans]
            if len(ws) == 1 or len(live) == 1:
                found.append(ws[0] if len(ws) == 1 else live[0])

        # The same load, billed as many times as it went out: every challan
        # fits exactly the same bills, and there are as many of each.
        alike = defaultdict(list)
        for cid, got in wants.items():
            if len(got) > 1 and not loose:
                alike[frozenset(b['id'] for b, _f in got)].append(cid)
        for bids, twins in alike.items():
            if len(twins) != len(bids):
                continue
            kind = {b['id']: f for b, f in wants[twins[0]]}
            for c, b in zip(sorted(twins, key=lambda i: (free_c[i]['invoice_date'], i)),
                            sorted(bids, key=lambda i: (free_b[i]['invoice_date'], i))):
                found.append((free_c[c], free_b[b], kind[b]))
        taken_c, taken_b = set(), set()
        for ch, b, kind in found:
            if ch['id'] in taken_c or b['id'] in taken_b:
                continue
            taken_c.add(ch['id'])
            taken_b.add(b['id'])
            pairs.append((ch, b, kind))
            del free_c[ch['id']]
            del free_b[b['id']]
        if not found:
            return pairs


def pair_groups(book, free_c, free_b):
    """Challans billed in parts or together: whole groups whose kilos and money
    add up on both sides.

    Each challan is taken as the first of up to three of the same customer
    raised within a month of it, against up to three of that customer's bills
    dated from twenty days before it to thirty after the last of them. Every
    bill combination's sums are worked out once per first challan and looked up
    by weight, so a customer with thirty bills in the month is still quick. A
    group is written only if none of its documents fits any other group.
    """
    # A bill and the return that cancelled it add up to nothing, and would
    # fit beside any group -- which would then never be the only answer.
    void_b = set(book.cancelled_bills) | set(book.cancelled_bills.values())
    void_c = set(book.cancelled_challans) | set(book.cancelled_challans.values())
    live_c = {k: v for k, v in free_c.items() if k not in void_c}
    found = []
    for cid in sorted(live_c, key=lambda i: (live_c[i]['invoice_date'], i)):
        a = live_c[cid]
        if not book.day[cid]:
            continue
        near = [b for b in book.bills_near(a, GROUP_EARLY, 2 * DAYS_AFTER, free_b)
                if b['id'] not in void_b]
        if not near:
            continue
        mates = book.mates(a, live_c)
        sums = []
        # Three at a time from a crowded month is thousands of sums for
        # nothing: a customer that busy is billed load by load.
        for m in range(1, (GROUP_MAX if len(near) <= 40 else 2) + 1):
            for bs in itertools.combinations(near, m):
                sums.append((sum(b['weight_total'] for b in bs),
                             sum(b['taxable'] for b in bs), bs))
        sums.sort(key=lambda s: s[0])
        weights = [s[0] for s in sums]
        for k in range(GROUP_MAX):
            for more in itertools.combinations(mates, k):
                chs = (a,) + more
                last = max(book.day[c['id']] for c in chs)
                w = sum(c['weight_total'] for c in chs)
                g = sum(c['grand_total'] for c in chs)
                v = sum(c['taxable'] for c in chs)
                span = max(GROUP_KG, GROUP_WEIGHT * abs(w))
                for i in range(bisect.bisect_left(weights, w - span),
                               bisect.bisect_right(weights, w + span)):
                    _bw, bv, bs = sums[i]
                    if len(chs) == 1 and len(bs) == 1:
                        continue
                    if any((book.day[b['id']] - last).days > DAYS_AFTER
                           for b in bs):
                        continue
                    tol = 20 * (len(chs) + len(bs)) + GROUP_VALUE * abs(g)
                    if abs(bv - g) <= tol or abs(bv - v) <= tol:
                        found.append((chs, bs))
    uses = Counter()
    for chs, bs in found:
        uses.update(('c', c['id']) for c in chs)
        uses.update(('b', b['id']) for b in bs)
    groups = []
    for chs, bs in found:
        if all(uses[('c', c['id'])] == 1 for c in chs) and \
                all(uses[('b', b['id'])] == 1 for b in bs):
            how = ('regrouped' if len(chs) > 1 and len(bs) > 1 else
                   'combined' if len(chs) > 1 else 'split')
            groups.append((chs, bs, how))
            for c in chs:
                free_c.pop(c['id'], None)
            for b in bs:
                free_b.pop(b['id'], None)
    return groups


def pair_everything(challans, bills, note=None, loose=True):
    """Every pair that can be recognised, and why the rest were left.

    Returns (pairs, groups, left): pairs are (challan, bill, kind) with kind
    'same' for the four rules or one of LOOSE; groups are (challans, bills,
    how). The four rules go first and to the end; then whole groups, whose sums
    on both sides are stronger evidence than any single loose fit; then the
    loose fits, the weight-and-value ones before money alone.
    """
    book = Book(challans, bills)
    free_c = {c['id']: c for c in book.challans if c['free']}
    free_b = {b['id']: b for b in book.bills if b['free']}
    pairs = pair_single(book, ('same',), free_c, free_b, note)
    groups = []
    if loose:
        pairs += pair_by_numbers(book, free_c, free_b)
        groups = pair_groups(book, free_c, free_b)
        pairs += pair_single(book, ('near', 'repriced'), free_c, free_b)
        pairs += pair_single(book, ('reweighed',), free_c, free_b)
        groups += pair_groups(book, free_c, free_b)
    left = Counter()
    for c in free_c.values():
        near = book.bills_near(c, DAYS_BEFORE, DAYS_AFTER, free_b)
        left['no bill of that customer in the days around it' if not near
             else 'a bill of that customer, but not of that load'] += 1
    return pairs, groups, left


def write(conn, pairs, groups):
    """Record what was recognised. Returns how many challans were joined."""
    conn.executescript(db.CHALLAN_BILL)
    n = 0
    for ch, b, kind in pairs:
        if kind == 'same':
            conn.execute('UPDATE invoice SET from_challan=? WHERE id=? AND '
                         'from_challan IS NULL', (ch['id'], b['id']))
        else:
            conn.execute('INSERT OR IGNORE INTO challan_bill(challan_id, '
                         'bill_id, how) VALUES (?,?,?)', (ch['id'], b['id'], kind))
        n += conn.execute('UPDATE invoice SET billed_company=?, billed_invoice=? '
                          'WHERE id=? AND billed_invoice IS NULL',
                          (b['company_id'], b['id'], ch['id'])).rowcount
    for chs, bs, how in groups:
        first = min(bs, key=lambda b: (b['invoice_date'], b['id']))
        for ch in chs:
            for b in bs:
                conn.execute('INSERT OR IGNORE INTO challan_bill(challan_id, '
                             'bill_id, how) VALUES (?,?,?)', (ch['id'], b['id'], how))
            n += conn.execute('UPDATE invoice SET billed_company=?, '
                              'billed_invoice=? WHERE id=? AND billed_invoice '
                              'IS NULL', (first['company_id'], first['id'],
                                          ch['id'])).rowcount
    conn.commit()
    return n


def undo(conn, apply_it):
    """Let go of every pair between two imported documents.

    A bill raised in the app keeps its challan: it was joined when it was
    raised, by somebody who knew, and `source` is what tells the two apart.
    """
    imported = "(SELECT id FROM invoice WHERE source='SHREE')"
    n = conn.execute(
        "SELECT COUNT(*) n FROM invoice WHERE source='SHREE' AND "
        'billed_invoice IN %s' % imported).fetchone()['n']
    if not apply_it:
        print('%d challans joined to imported bills. Run with --apply to let '
              'them go.' % n)
        return 0
    conn.execute("UPDATE invoice SET billed_company=NULL, billed_invoice=NULL "
                 "WHERE source='SHREE' AND billed_invoice IN %s" % imported)
    conn.execute("UPDATE invoice SET from_challan=NULL WHERE source='SHREE' "
                 'AND from_challan IN %s' % imported)
    if has_link_table(conn):
        conn.execute('DELETE FROM challan_bill WHERE challan_id IN %s AND '
                     'bill_id IN %s' % (imported, imported))
    conn.commit()
    print('%d pairs let go.' % n)
    return 0


def by_year(firms, challans, bills, joined):
    """A line per year: what was there to pair, and what was paired.

    Some years pair far better than others, and the reason is almost always
    how much of the bill register survives for that year rather than anything
    about the challans.
    """
    ch_n, bill_n, hit = Counter(), Counter(), Counter()
    for ch in challans:
        ch_n[ch['fy']] += 1
    for b in bills:
        bill_n[b['fy']] += 1
    for ch in joined:
        hit[ch['fy']] += 1
    out = ['   %-9s %9s %9s %9s' % ('year', 'challans', 'bills', 'paired')]
    for fy in sorted(set(ch_n) | set(bill_n)):
        out.append('   %-9s %9d %9d %9d' % (fy, ch_n[fy], bill_n[fy], hit[fy]))
    return '\n'.join(out)


HOW_SAID = {
    'same': 'the bill is the challan plus GST',
    'renamed': 'the same load, the customer named differently',
    'other name': 'the same load, billed in another name',
    'near': 'within a kilo and half a per cent',
    'repriced': 'same weight, bill re-priced',
    'reweighed': 'same money, weight changed on the bill',
    'split': 'one challan billed in parts',
    'combined': 'challans billed together',
    'regrouped': 'challans re-cut into other bills',
}


def main():
    db.quiet_windows_errors()
    # A customer's name printed to a Windows console must not stop a run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(
        description='Join imported challans to the bills raised from them.')
    ap.add_argument('--apply', action='store_true',
                    help='write the pairs; without it nothing is changed')
    ap.add_argument('--fy', help="only this financial year, e.g. 2025-26")
    ap.add_argument('--firm', help='read challans from this firm instead of the '
                                   'order firm, by code, e.g. PSIA')
    ap.add_argument('--csv', help='write every pair found to this file')
    ap.add_argument('--show', type=int, default=8,
                    help='how many pairs to print (default 8)')
    ap.add_argument('--undo', action='store_true',
                    help='let go of every pair between two imported documents')
    ap.add_argument('--strict', action='store_true',
                    help='only a bill that is the challan plus GST -- for books '
                         'whose returns are not yet repaired (repair.py)')
    args = ap.parse_args()

    conn = db.connect()
    try:
        if args.undo:
            return undo(conn, args.apply)
        firms, every_challan, every_bill = load(conn, args.fy, args.firm)
        challans = [c for c in every_challan if c['free']]
        bills = [b for b in every_bill if b['free']]
        if not challans:
            print('No challans are waiting for a bill.')
            return 0
        odd = []
        pairs, groups, left = pair_everything(every_challan, every_bill, odd,
                                              loose=not args.strict)
        joined = [ch for ch, _b, _k in pairs] + [c for chs, _bs, _h in groups
                                                 for c in chs]

        def no(d):
            s = (d['series'] or '').strip()
            return '%s %s' % (s, d['invoice_no']) if s else d['invoice_no']

        def code(d):
            return firms[d['company_id']]['code']

        print('%d challans with no bill recorded, %d bills with no challan '
              'recorded.' % (len(challans), len(bills)))
        print('%d pairs recognised.\n' % len(joined))
        kinds = Counter(k for _c, _b, k in pairs)
        for chs, _bs, how in groups:
            kinds[how] += len(chs)
        for kind, n in sorted(kinds.items(), key=lambda x: -x[1]):
            print('   %-44s %6d' % (HOW_SAID.get(kind, kind), n))
        if args.show and pairs:
            print('\nFor example:')
            for ch, b, kind in pairs[:args.show]:
                print('   %-6s %-9s %-28s %10.2f  ->  %-4s %-8s %10.2f  (%s)'
                      % (code(ch), no(ch), ch['party_name'][:28],
                         ch['grand_total'], code(b), no(b), b['taxable'], kind))
        if args.show and groups:
            print('\nBilled in parts or together, for example:')
            for chs, bs, how in groups[:args.show]:
                print('   %s  ->  %s  (%s, %s)' % (
                    ' + '.join('%s %s' % (code(c), no(c)) for c in chs),
                    ' + '.join('%s %s' % (code(b), no(b)) for b in bs),
                    chs[0]['party_name'][:28], how))
        print('\nThe rest were left alone:')
        for reason, n in left.most_common():
            print('   %-46s %6d' % (reason, n))
        print('\nYear by year:')
        print(by_year(firms, challans, bills, joined))
        if odd:
            print('\nSame customer and the same weight, but the value differs.'
                  '\nWorth an eye -- a re-priced bill looks like this:')
            for ch, b, gap in odd[:8]:
                print('   %-6s %-9s %10.2f  vs  %-4s %-8s %10.2f   off by %s'
                      % (code(ch), no(ch), ch['grand_total'], code(b), no(b),
                         b['taxable'], gap))

        if args.csv:
            with open(args.csv, 'w', newline='', encoding='utf-8-sig') as fh:
                w = csv.writer(fh)
                w.writerow(['challan_firm', 'challan', 'challan_date',
                            'challan_customer', 'challan_total', 'bill_firm',
                            'bill', 'bill_date', 'bill_customer',
                            'bill_taxable', 'weight', 'matched_on'])
                for ch, b, kind in pairs:
                    w.writerow([code(ch), no(ch), ch['invoice_date'],
                                ch['party_name'], ch['grand_total'], code(b),
                                no(b), b['invoice_date'], b['party_name'],
                                b['taxable'], ch['weight_total'], kind])
                for chs, bs, how in groups:
                    for c in chs:
                        for b in bs:
                            w.writerow([code(c), no(c), c['invoice_date'],
                                        c['party_name'], c['grand_total'],
                                        code(b), no(b), b['invoice_date'],
                                        b['party_name'], b['taxable'],
                                        c['weight_total'], how])
            print('\nEvery pair written to %s' % args.csv)

        if not args.apply:
            print('\nNothing was changed. Run again with --apply to record '
                  'these pairs.')
            return 0
        write(conn, pairs, groups)
        print('\n%d pairs recorded.' % len(joined))
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
