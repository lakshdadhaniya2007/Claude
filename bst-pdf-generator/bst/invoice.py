"""
BALKRISHANA STEEL TRADERS -- TAX INVOICE / DEBIT MEMO.

Every coordinate in this file was measured off the original PDF, so the output
is a positional replica of the existing stationery. Page origin is bottom-left,
units are PDF points, page is exactly 595.2 x 841.65 (the size the original
used, very slightly different from ReportLab's A4 constant).

DO NOT "tidy" the numbers below -- the quirks are faithful to the original and
are what make the reprint indistinguishable.

Three things were changed on purpose and are not to be "restored". The first
two were asked for on 3 Sep 2026 after reading a printed bill; the third on
10 Sep 2026:

  * **Figures sit centred in their ruled column, on their row's baseline.**
    The original set each figure a few tenths of a point off its neighbours --
    the HSN 1.2pt above the line it belonged to, the three cells of a GST
    summary row on three different baselines -- and hung the quantity off to
    the left of its own column. One baseline a row now, and one centre a cell.
    The label-and-value lists (the totals stack at the right, Round Off, Grand
    Total) keep their right-aligned figures: there is no column there to be
    centred in, only a label with its amount against the frame.
  * **The item columns were re-proportioned.** Particulars gave up 28.5pt so
    the HSN and the rate could have it; the HSN is set in the same size as
    every other figure on the sheet rather than the 8pt it used to be.
  * **One size a row, one size a heading** (16 Sep 2026, off a printed GT 256:
    *make sure nothing seems different from the other things on the bill and
    the challan*). Every figure on an item row is set at the row's size -- the
    GST rate reads "18%" so it fits its narrow column at that size, and the HSN
    column took 4pt more from Particulars for the same reason. The headings
    share one size; the GST summary's are shortened so they can. The totals
    stack runs down to Round Off at one pitch, the bank details take a line
    each, and a date or number field with nothing in it prints nothing
    rather than " /  /".
  * **Everything is set a fifth larger** -- `TYPE` and `ROW_GROW` below. The
    frame, the rules and the columns did not move; the type inside them grew,
    and the item rows stand further apart to match. The label-and-value pairs
    that the original spaced by eye ("Invoice No:", "Bank Details:", "To,",
    "Rs.") now measure their own label and start the value clear of it, and
    the strip Quantity keeps for "KGS" is measured rather than a flat 18pt --
    at the larger size all of those collided. `_w` is how a fit test asks
    about the *scaled* size; never measure with `c.stringWidth` directly.
"""

import os
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from .fonts import (register_fonts, font,
                    VERDANA, VERDANA_BOLD, VERDANA_BOLD_ITALIC,
                    HELV, HELV_BOLD)
from .amount_words import rupees_in_words, fmt
from .text import wrap_lines, wrap_fixed
# The goods at the challan's own size, worded as it words them (6 Oct 2026).
from .challan import NAME_SIZE

PAGE_W, PAGE_H = 595.2, 841.65

# ------------------------------------------------------------------- size --
# Asked for on 10 Sep 2026: make everything on the printed bill bigger.
#
# **The sheet itself cannot grow.** The frame already stops 15.45pt -- 5.4mm --
# from the right edge of the A4 and 24.75pt from the top, which is about as
# close as a laser printer will go. So what grows is the type and the pitch of
# the item rows, which is the whole of what anybody reads; the frame, the
# column rules and the blocks below the table stay exactly where they were, so
# the reprint still lands on the same stationery.
#
# One factor, applied by scaling `setFont` on the canvas rather than by editing
# forty literal sizes. `_w` measures at the size the text will actually be set
# in, so every "does this still go in its column?" is answered against the
# bigger type: a product name too long for Particulars shrinks back exactly as
# it did before and is never clipped by the increase. To change the size again,
# change these two numbers -- do not nudge the literals.
TYPE = 1.20                    # every point size on the sheet
# Row pitch, where the table has room for it: 1.20 for the 10pt rows until
# 26 Sep 2026, raised with them to 11pt so the rows stand as far apart for
# their size.
ROW_GROW = 1.32

# ---------------------------------------------------------------- geometry --
# Outer frame
FRAME_L, FRAME_R = 40.5, 579.75
FRAME_TOP, FRAME_BOT = 692.4, 31.2

# Header rules under the company name
HDR_RULE_L, HDR_RULE_R = 139.5, 577.5

# Party / invoice-details block
PARTY_SPLIT_X = 337.5          # vertical divider between party box and details
PARTY_GST_RULE_Y = 621.9       # rule above "Party's GST No"
DETAILS_RULE_Y = 675.15        # rule under "Invoice No / Date"

# Line-item table
TBL_TOP = 592.65               # top of the header row
TBL_HDR_BOT = 567.9            # bottom of the header row / top of the body
TBL_TOTAL_Y = 253.95           # rule above the sub-total strip
TBL_BOT = 238.2                # bottom of the whole table
COL_NO = 66.75                 # right edge of "No."
# Particulars was the widest column on the sheet and the two beside it the
# tightest: an eight-digit HSN would not go in at the size of the rest of the
# page, and "Rate Per Kgs." had to break across two lines. Particulars gives
# up 28.5pt, which is where both of those went. Nothing right of the rate
# moves, so the GST%, the amount and the whole lower half of the sheet stay
# exactly where the original put them. Most product names still set at full
# size; the longest lose a little, and none is trimmed.
#
# 26 Sep 2026 (off a printed GT 218 with the item line, the Total, the totals
# stack and the Grand Total ringed): the item row is set at the challan's
# size, so the HSN took 6pt from Quantity to hold eight digits at it, and
# Quantity took 7.5pt from the rate, which had room to spare. COL_RATE and
# everything right of it still stay where the original put them.
COL_PARTICULARS = 233.0        # right edge of "Particulars"   (was 265.5)
COL_HSN = 306.0                # right edge of "HSN Code"      (was 315.0, 300.0)
COL_QTY = 390.0                # right edge of "Quantity"      (was 397.5, 382.5)
COL_RATE = 460.5               # right edge of "Rate Per Kgs."
COL_GST = 493.5                # right edge of "GST%"

# Every figure in the item table is centred in its own column now, so the
# right-alignment edges it used to be set against are gone. What is left is
# the room the unit label keeps for itself at the right of the Quantity
# column: the figure is centred in what remains, so a long weight can never
# run into the KGS beside it, and the column total below lines up under the
# figures rather than under the pair.
QTY_UNIT_W = 18.0             # room kept at the right of Quantity for "KGS"
PAD = 3.0                     # breathing room between a figure and its rule

ROW_H = 12.75                  # line-item row pitch
ROW_SIZE = 11.0                # every figure and word on an item row (was 10)
STACK_SIZE = 10.5              # Sub Total ... Round Off, where the stack has room (was 9)
GRAND_SIZE = 15.0              # the Grand Total figure (was 11, then 12.5)
MOBILE_SIZE = 10.5             # the customer's mobile number, in bold -- above the address's 10
HEAD_SIZE = 9.5                # every column heading of the item table
ROW1_BASELINE = 555.15

# Bank / GST-summary / totals block
SUMMARY_TOP = 188.7
SUMMARY_HDR_BOT = 178.2
SUMMARY_TOTAL_Y = 133.2
SUMMARY_BOT = 119.7
SUMMARY_RIGHT = 381.75
GS_COLS = [93.75, 150.0, 172.5, 230.25, 252.75, 310.5]   # verticals
GS_ROW_H = 11.25

WORDS_RULE_Y = 106.2
R_TOTALS = 576.75              # right edge of the Sub Total / SGST / CGST column
GRAND_BOX = (384.0, 121.2, 195.0, 21.0)
GRAND_FILL = 0.75294

LOGO_RECT = (42.0, 713.40002, 84.0, 103.5)        # x, y, w, h
SIGN_RECT = (459.75, 48.45, 114.0, 41.25)
# The mills whose material is stocked, printed as one strip across the foot of
# the bill. It sits in the band between the GST number and the jurisdiction
# line, left of the signature -- the only clear run of space down there, and
# where the trade prints them. 13.24:1, so 400pt wide comes to 30.2pt tall.
LOGOS_RECT = (44.0, 59.0, 400.0, 30.2)


def _magnify(c):
    """Set every piece of type drawn on this canvas `TYPE` times bigger.

    Wrapping `setFont` catches the sizes inside the helpers below as well as
    the literals in `build_invoice`, so none can be missed. ReportLab's own
    `drawCentredString` / `drawRightString` measure with the current font,
    which is the scaled one, so they still centre and right-align correctly.
    """
    if abs(TYPE - 1.0) < 1e-9:
        return
    plain = c.setFont
    c.setFont = lambda name, size, leading=None: plain(name, size * TYPE)


def _pair(c, x, y, label, fontname, size, at_least, gap=3.75):
    """Draw a label, and say where the value beside it starts.

    The sheet sets every value at its own measured x, which worked while the
    label was always narrower than the run up to it. Bigger type can carry a
    label past that point -- "Payment Terms" ran 16pt into its own colon -- so
    the value now starts at whichever is further right: the measured position,
    or just clear of the label. On a sheet where the label still fits, nothing
    moves at all.
    """
    c.setFont(fontname, size)
    c.drawString(x, y, label)
    return max(at_least, x + _w(c, label, fontname, size) + gap)


def _fit_pitch(lay, body):
    """The row pitch at which laid-out rows `lay` -- (lines, size, lead,
    pitch) each -- just fill `body`, their second lines included."""
    if len(lay) < 2:
        return ROW_H
    extra = sum(lead * (max(1, len(n)) - 1) for n, _, lead, _p in lay)
    return int((body - extra) / (len(lay) - 1) * 4) / 4.0


def _row_pitch(n):
    """How far apart the item rows stand on a bill of `n` lines.

    `ROW_GROW` times the original, except on a bill long enough that the wider
    pitch would push a line off the sheet -- then it tightens back towards the
    original, and never below it. The books' longest bill is 13 lines and the
    body holds 24 at the old pitch, so in practice every bill gets the full
    increase; this is only the guard.
    """
    if n < 2:
        return ROW_H * ROW_GROW
    room = ROW1_BASELINE - (TBL_TOTAL_Y + 4)
    return max(ROW_H, min(ROW_H * ROW_GROW, room / (n - 1)))


def _w(c, text, fontname, size):
    """Width of `text` at the size it will actually be set in.

    Every fit test goes through here rather than `c.stringWidth`, which would
    answer for the unscaled size and let the bigger type run over its rule.
    """
    return c.stringWidth(text, fontname, size * TYPE)


def _shrink(c, text, fontname, size, width, floor):
    """Step a size down until the text fits, but no further than `floor`."""
    while size > floor and _w(c, text, fontname, size) > width:
        size -= 0.25
    return size


def _rnum(c, x, y, text, fontname, size, width, floor=5.5):
    """Right-align a figure inside its column, shrinking it rather than
    letting it run back over the rule to its left. A number is never clipped."""
    c.setFont(fontname, _shrink(c, text, fontname, size, width, floor))
    c.drawRightString(x, y, text)


def _rate_text(v):
    """A rate to three decimals, without the trailing zeros nobody quotes.

    Steel is quoted to the paisa -- 80.25, not 80.250 -- and the third place
    only ever carries anything on an odd job rate. Printing it always made
    every rate a digit wider than it needed to be in the narrowest money
    column on the bill.
    """
    s = fmt(v, 3)
    if '.' not in s:
        return s
    head, dot, tail = s.partition('.')
    tail = tail.rstrip('0')
    while len(tail) < 2:
        tail += '0'
    return head + '.' + tail


def _pct(v):
    """18 -> "18%", 2.5 -> "2.5%": a rate as a bill prints one."""
    t = fmt(v)
    if t.endswith('.00'):
        t = t[:-3]
    elif '.' in t:
        t = t.rstrip('0')
    return t + '%'


def _cnum(c, x, y, text, fontname, size, width, floor=5.5):
    c.setFont(fontname, _shrink(c, text, fontname, size, width, floor))
    c.drawCentredString(x, y, text)


def _numcol(c, mid, texts, fontname, size, width, floor=5.5):
    """Set out a column of figures, and say where to draw each one.

    Returns the one size the whole column is set in and the x every figure is
    right-aligned to. **The figures stack, and the stack sits in the middle of
    the column.** Centring each figure on its own centre would leave a column
    of money ragged -- 1,62,506.25 over 321.00 over 36,915.00, no two decimal
    points above each other -- and a money column is read down a page and
    added up. Right-aligned inside a block that is itself centred keeps the
    paise under the paise and still puts the figures in the middle of their
    column, which is what was asked for and what a bill wants. With one line
    on the bill the two are the same thing.

    One size for the column, too: the widest figure decides it, so a long
    line does not come out set smaller than the one above it.
    """
    texts = [t for t in texts if t]
    if not texts:
        return size, mid
    for t in texts:
        size = _shrink(c, t, fontname, size, width, floor)
    widest = max(_w(c, t, fontname, size) for t in texts)
    return size, mid + widest / 2.0


def _unit_name(unit):
    """KGS -> "Kgs.", as the rate heading beside it spells it."""
    return {"KGS": "Kgs.", "KG": "Kgs.", "NOS": "Nos.", "PCS": "Pcs.",
            "MTS": "Mts.", "MT": "Mts."}.get(unit, unit.title())


def _unit_w(c, unit, fontname, size):
    """The strip the unit label keeps for itself at the right of Quantity.

    It was a flat 18pt, measured against "KGS" at the old size. Measured now,
    so the figures beside it give up exactly the room the label needs and no
    more -- and so bigger type moves the figures left instead of printing
    "2,467.00KGS".
    """
    if not unit:
        return 0.0
    return max(QTY_UNIT_W, _w(c, unit, fontname, size) + PAD)


def _qty(c, x, y, text, unit, fontname, size, unit_size):
    """One quantity, against the column's shared right edge, with its unit.

    The unit is a label on the figure rather than a column of its own, so it
    keeps a fixed strip at the right of the column and the figures are laid
    out in what is left. Both sit on the row's baseline; the unit used to be
    lifted 1.2pt above it.
    """
    c.setFont(fontname, size)
    c.drawRightString(x, y, text)
    if unit:
        c.setFont(fontname, unit_size)
        c.drawRightString(COL_QTY - PAD, y, unit)


def _gs_edges(j):
    """Left and right rule of one GST-summary cell, counted from the first
    figure column: 0 is Taxable Amnt, 5 is Total GST."""
    right = GS_COLS[j + 1] if j + 1 < len(GS_COLS) else SUMMARY_RIGHT
    return GS_COLS[j], right


def _gs_mid(j):
    a, b = _gs_edges(j)
    return (a + b) / 2.0


def _gs_room(j):
    a, b = _gs_edges(j)
    return b - a - 2 * PAD


def _fit(c, text, fontname, size, width, floor=None):
    """Free text: shrink it a little, then trim what still will not fit."""
    text = str(text or '')
    if floor:
        size = _shrink(c, text, fontname, size, width, floor)
    c.setFont(fontname, size)
    if _w(c, text, fontname, size) <= width:
        return text
    while text and _w(c, text + '\u2026', fontname, size) > width:
        text = text[:-1]
    return text + '\u2026'


def _line(c, x1, y1, x2, y2, w=1):
    c.setLineWidth(w)
    c.line(x1, y1, x2, y2)


def build_invoice(data, out_path, assets_dir=None):
    """Render one tax invoice. `data` is the dict documented in README.md."""
    register_fonts()
    if assets_dir is None:
        assets_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

    c = canvas.Canvas(out_path, pagesize=(PAGE_W, PAGE_H))
    _magnify(c)
    c.setTitle(f"Tax Invoice {data['invoice'].get('number', '')}")
    # Sheet after sheet until every line is on one (6 Oct 2026): the goods
    # keep their size, so a long bill goes on over a second sheet rather than
    # shrinking onto one.
    start, bf, sheet = 0, None, 1
    while True:
        c.setStrokeColorRGB(0, 0, 0)
        c.setFillColorRGB(0, 0, 0)
        drawn, qty, amt, last = _draw_sheet(c, data, assets_dir, start, bf,
                                            sheet)
        c.showPage()
        if last or not drawn:
            break
        start, bf, sheet = start + drawn, (qty, amt), sheet + 1
    c.save()
    return out_path


def _draw_sheet(c, data, assets_dir, start=0, bf=None, sheet=1):
    """One sheet of a bill: from line `start`, with `bf` the (qty, amount)
    brought forward from the sheets before. The GST summary, the totals, the
    grand total and the words are on the last sheet. Answers (lines drawn,
    qty, amount, last sheet)."""
    seller = data["seller"]
    buyer = data["buyer"]
    meta = data["invoice"]
    items = data["items"]

    F_R, F_B, F_BI = font(VERDANA), font(VERDANA_BOLD), font(VERDANA_BOLD_ITALIC)

    # ------------------------------------------------------------ letterhead --
    logo = os.path.join(assets_dir, seller.get("logo", "logo.jpg"))
    if os.path.isfile(logo):
        c.drawImage(ImageReader(logo), *LOGO_RECT,
                    preserveAspectRatio=False, mask=None)

    # The letterhead is centred between the rules, so it has those rules'
    # width to live in and no more. Every line is fitted to it: a long trading
    # name or a two-line address used to be drawn at full size straight off the
    # edge of the paper -- one address ran seventy points past the right margin
    # and simply vanished at the print.
    mid = (HDR_RULE_L + HDR_RULE_R) / 2.0
    hdr_w = HDR_RULE_R - HDR_RULE_L
    c.drawCentredString(mid, 774.372,
                        _fit(c, seller["name"], F_B, 18, hdr_w, floor=11.0))
    c.drawCentredString(mid, 744.9,
                        _fit(c, seller["address"], F_R, 10, hdr_w, floor=7.0))
    c.drawCentredString(mid, 731.4,
                        _fit(c, seller["phone_line"], F_R, 10, hdr_w, floor=7.5))
    c.drawCentredString(mid, 717.9,
                        _fit(c, seller["email_line"], F_R, 10, hdr_w, floor=7.5))

    _line(c, HDR_RULE_L, 766.65002, HDR_RULE_R, 766.65002, 1)
    _line(c, HDR_RULE_L, 764.40002, HDR_RULE_R, 764.40002, 2)
    _line(c, HDR_RULE_L, 758.40002, HDR_RULE_R, 758.40002, 1)

    # ------------------------------------------------- DEBIT MEMO / title row --
    c.setFont(F_B, 9)
    c.drawString(42, 697.761, meta.get("memo_label", "DEBIT MEMO"))
    c.drawRightString(579.0, 697.761, meta.get("copy_label", "ORIGINAL"))
    c.setFont(F_B, 11)
    c.drawCentredString((FRAME_L + FRAME_R) / 2.0, 696.369,
                        meta.get("title", "T A X    I N V O I C E"))

    # ------------------------------------------------------------- box frame --
    _line(c, FRAME_L, 30.45, FRAME_L, 691.65002, 1)              # left
    _line(c, FRAME_R, 30.45, FRAME_R, FRAME_TOP, 1)              # right
    _line(c, 41.25, FRAME_TOP, FRAME_R, FRAME_TOP, 2)            # top
    _line(c, 41.25, FRAME_BOT, FRAME_R, FRAME_BOT, 2)            # bottom

    # ---------------------------------------------------------- party block --
    _line(c, PARTY_SPLIT_X, TBL_TOP, PARTY_SPLIT_X, FRAME_TOP, 1)
    _line(c, PARTY_SPLIT_X, DETAILS_RULE_Y, FRAME_R, DETAILS_RULE_Y, 1)
    _line(c, FRAME_L, PARTY_GST_RULE_Y, PARTY_SPLIT_X, PARTY_GST_RULE_Y, 0)

    # The name is never smaller than the address beneath it.
    name_x = _pair(c, 45, 676.761, "To,", F_B, 10, 63.75, gap=2.25)
    w = PARTY_SPLIT_X - name_x - PAD
    c.drawString(name_x, 676.761, _fit(c, buyer["name"], F_B, 10, w, floor=7.0))
    w = PARTY_SPLIT_X - 63.75 - PAD
    # Three lines fit above the GST rule. A long line breaks into a spare one
    # rather than being shrunk and cut off.
    # With a mobile number to print the lines close up a little -- 11.75pt,
    # not 12.75 -- so the larger number fits under them above the rule.
    mobile = buyer.get("mobile", "")
    y = 662.4
    for line in wrap_lines(buyer.get("address", []),
                           lambda t: _w(c, t, F_R, 10), w, 3):
        c.setFont(F_R, 10)
        c.drawString(63.75, y, _fit(c, line, F_R, 10, w, floor=8.0))
        y -= 11.75 if mobile else 12.75
    if mobile:
        # The customer's mobile, as the challan prints it (26 Sep 2026: it
        # was ringed on the bill, where it only showed inside the address
        # SHREE typed): larger, the number in bold, clear of the line above.
        label = "Mo. No. :"
        size = MOBILE_SIZE
        while size > 7.0 and (_w(c, label + " ", F_R, size)
                              + _w(c, mobile, F_B, size)) > w:
            size -= 0.25
        my = y - 0.9
        c.setFont(F_R, size)
        c.drawString(63.75, my, label)
        num_x = 63.75 + _w(c, label + " ", F_R, size)
        c.drawString(num_x, my, _fit(c, mobile, F_B, size, w - (num_x - 63.75)))
    # Read as often as the address it sits under, and it used to be set two
    # points smaller than everything around it.
    c.setFont(F_R, 10)
    c.drawString(42.75, 609.0, f"Party's GST No: {buyer.get('gstin', '')}")

    # -------------------------------------------------- invoice details block --
    # One colon column for the whole block, and every value bounded so it
    # cannot run into the field beside it. Label and value share a baseline.
    LBL_X = 345.0
    COLON_X = 423.0
    VAL_X = 432.75
    LR_DATE_X = 477.75

    # "Invoice No: GT 256" and "Date: 11/09/2026" share one size: the
    # largest at which all four pieces go in the row. The number used to be
    # set at full size and the date squeezed into whatever was left.
    number, date = meta.get("number", ""), meta.get("date", "")
    GAP, SEP = 3.75, 9.0

    def row_w(sz):
        return (_w(c, "Invoice No:", F_B, sz) + GAP + _w(c, number, F_B, sz)
                + SEP + _w(c, "Date:", F_R, sz) + GAP + _w(c, date, F_R, sz))
    hsz = 10.0
    while hsz > 7.5 and row_w(hsz) > FRAME_R - PAD - LBL_X:
        hsz -= 0.25
    no_x = _pair(c, LBL_X, 679.44, "Invoice No:", F_B, hsz, 0, gap=GAP)
    date_w = _w(c, date, F_R, hsz)
    date_x = FRAME_R - PAD - date_w
    dt_lbl_x = date_x - GAP - _w(c, "Date:", F_R, hsz)
    c.setFont(F_B, hsz)
    c.drawString(no_x, 679.44, _fit(c, number, F_B, hsz,
                                    dt_lbl_x - SEP - no_x, floor=7.0))
    c.setFont(F_R, hsz)
    c.drawString(dt_lbl_x, 679.44, "Date:")
    c.drawString(date_x, 679.44, date)

    # The colon is a column, so it is set once for the whole block off the
    # longest label rather than per row -- five colons on five different
    # verticals is what this block must never look like.
    labels = ("Transport", "L. R. No.", "Vehicle No", "Cases", "Payment Terms")
    colon_x = max(COLON_X, LBL_X + max(
        _w(c, t, F_R, 10) for t in labels) + 3.75)
    val_x = max(VAL_X, colon_x + (VAL_X - COLON_X))
    wide = FRAME_R - val_x - PAD
    lr_date_x = max(LR_DATE_X, val_x + 45.0)
    for yy, label, value, room in (
        (663.15, labels[0], meta.get("transport", ""), wide),
        (649.65, labels[1], meta.get("lr_no", ""), lr_date_x - val_x - PAD),
        (636.15, labels[2], meta.get("vehicle_no", ""), wide),
        (622.65, labels[3], meta.get("cases", ""), wide),
        (607.65, labels[4], meta.get("payment_terms", ""), wide),
    ):
        c.setFont(F_R, 10)
        c.drawString(LBL_X, yy, label)
        c.drawString(colon_x, yy, ":")
        if value:
            c.drawString(val_x, yy,
                         _fit(c, str(value), F_R, 10, room, floor=7.0))

    # L. R. date sub-field, right of the L.R. No.
    lrd_x = _pair(c, lr_date_x, 649.65, "Date:", F_R, 10,
                  max(507.75, lr_date_x + 30.0))
    if meta.get("lr_date"):
        c.drawString(lrd_x, 649.65, _fit(c, meta["lr_date"], F_R, 10,
                                         FRAME_R - lrd_x - PAD, floor=7.5))

    # ------------------------------------------------------- item table grid --
    _line(c, 41.25, TBL_TOP, FRAME_R, TBL_TOP, 1)
    _line(c, 41.25, TBL_HDR_BOT, FRAME_R, TBL_HDR_BOT, 1)
    _line(c, 41.25, TBL_BOT, FRAME_R, TBL_BOT, 1)
    _line(c, 41.25, TBL_TOTAL_Y, FRAME_R, TBL_TOTAL_Y, 0)
    for x in (COL_NO, COL_HSN, COL_QTY, COL_RATE, COL_GST):
        _line(c, x, TBL_BOT, x, 591.90002, 1)
    _line(c, COL_PARTICULARS, 238.95, COL_PARTICULARS, TBL_TOP, 1)

    # One size for every heading -- the largest the narrowest of them wears.
    # "GST" rather than "GST%": the figures under it say "18%" themselves.
    units = {str(it.get("unit") or "KGS").strip().upper() for it in items}
    one_unit = _unit_name(units.pop()) if len(units) == 1 else ""
    heads = ((41.25, COL_NO, ("No.",)),
             (COL_NO, COL_PARTICULARS, ("Particulars",)),
             (COL_PARTICULARS, COL_HSN, ("HSN Code",)),
             (COL_HSN, COL_QTY, ("Quantity", one_unit) if one_unit
              else ("Quantity",)),
             (COL_QTY, COL_RATE, ("Rate Per", one_unit or "Kgs.")),
             (COL_RATE, COL_GST, ("GST",)),
             (COL_GST, FRAME_R, ("Amount",)))
    hsize = HEAD_SIZE
    for x1, x2, lines in heads:
        for t in lines:
            hsize = _shrink(c, t, F_B, hsize, x2 - x1 - 3.0, 7.0)
    c.setFont(F_B, hsize)
    for x1, x2, lines in heads:
        mid_x = (x1 + x2) / 2.0
        if len(lines) == 1:
            c.drawCentredString(mid_x, 576.261, lines[0])
        else:
            # Spread to the edges of the heading row: 10.25pt apart, the
            # descender of "Quantity" met the capitals of "Kgs." under it.
            c.drawCentredString(mid_x, 583.1, lines[0])
            c.drawCentredString(mid_x, 570.9, lines[1])

    # ------------------------------------------------------------ item rows --
    # Read the rows that fit before any of them is drawn: a column of figures
    # has to be set out as a column -- one size, one edge to hang the digits
    # off -- and that cannot be known one row at a time.
    # The rows stand further apart to match the bigger type -- but only as far
    # as the ruled body has room for. A bill long enough to need the old pitch
    # gets the old pitch, so nothing that used to fit on one sheet stops
    # fitting because the type grew.
    # Names first. Until 26 Sep 2026 a long name was shrunk to fit one line
    # and read smaller than the figures beside it ("CRCA 2.50 CRCA 91/92 MM
    # PATO" on GT 218); until 6 Oct it broke over two lines and shrank until
    # both halves fitted.
    part_room = COL_PARTICULARS - 68.25 - PAD
    # Since 6 Oct 2026 the challan's way exactly ("repeat the same format,
    # font size and everything as Challan's particulars for the Bill's
    # particulars"): worded as the challan words them, thickness first
    # (`pdf_print`), at the challan's `NAME_SIZE` on every bill, in as many
    # lines as a name takes (`wrap_fixed`) -- never shrunk, never past the
    # rule. Lines that will not go on this sheet go on over the next.
    body = ROW1_BASELINE - (TBL_TOTAL_Y + 4)
    # A sheet after the first opens with what the ones before it came to.
    bf_pitch = ROW_H * ROW_GROW if bf else 0.0
    body -= bf_pitch
    sheet_items = items[start:]
    names = [wrap_fixed(lambda t: _w(c, t, F_R, NAME_SIZE), it["particulars"],
                        part_room, it.get("head", "")) for it in sheet_items]

    def layout(lead, pitch):
        return [(n, NAME_SIZE, lead, pitch) for n in names]

    def height(lay):
        return sum(p + lead * (max(1, len(n)) - 1) for n, _, lead, p in lay) \
            - (lay[-1][3] if lay else 0.0)

    # A bill whose names all break may not fit the body at the wider pitch:
    # tighten the pitch first, then the names' own lines, and only then go
    # on over another sheet -- at the ordinary spacing, with room kept for
    # the notice that says so.
    lead = NAME_SIZE * TYPE + 1.0
    pitch = _row_pitch(len(sheet_items))
    lay = layout(lead, pitch)
    if lay and height(lay) > body:
        # The widest pitch at which the names' second lines still fit, rather
        # than straight down to the tightest: a 14-line bill's rows bunched
        # up with room to spare below them.
        lay = layout(lead, max(ROW_H, _fit_pitch(lay, body)))
        if height(lay) > body:
            lead = NAME_SIZE * TYPE
            lay = layout(lead, max(lead, _fit_pitch(layout(lead, ROW_H), body)))
        if height(lay) > body:
            lead = NAME_SIZE * TYPE + 1.0
            lay = layout(lead, pitch)
    floor_y = TBL_TOTAL_Y + 4 + (0.0 if not lay or height(lay) <= body
                                 else 12.0)
    rows = []
    total_qty, sub_total = bf if bf else (0.0, 0.0)
    y = ROW1_BASELINE
    if bf:
        rows.append(dict(y=y, sr="", name=["Brought forward"], nsize=NAME_SIZE,
                         lead=lead, hsn="", qty=fmt(total_qty), unit="",
                         rate="", gst="", amount=fmt(sub_total), bold=True))
        y -= bf_pitch
    drawn_rows = 0
    for i, it in enumerate(sheet_items):
        name, nsize, lead, row_pitch = lay[i]
        if y - lead * (max(1, len(name)) - 1) < floor_y:
            if drawn_rows:
                break
            # One name taller than a whole sheet: as much of it as the sheet
            # holds, rather than a sheet with nothing on it, forever.
            keep = max(1, int((y - floor_y) / lead) + 1)
            name = name[:keep - 1] + [name[keep - 1] + ' \u2026'] \
                if keep < len(name) else name
        drawn_rows = i + 1
        qty = float(it["qty"])
        rate = float(it["rate"])
        amount = it.get("amount")
        amount = float(amount) if amount is not None else round(qty * rate, 2)
        # Total what the page shows, so the column adds up as it is read.
        total_qty += round(qty, 2)
        sub_total += amount
        rows.append(dict(y=y, sr=str(it.get("sr", start + i + 1)),
                         name=name, nsize=nsize, lead=lead,
                         hsn=str(it.get("hsn") or ""),
                         qty=fmt(qty), unit=it.get("unit", "KGS"),
                         rate=_rate_text(rate), gst=_pct(it.get("gst_pct", 18)),
                         amount=fmt(amount)))
        # A wrapped name needs every extra line's room beneath it.
        y -= row_pitch + lead * (max(1, len(name)) - 1)
    last = drawn_rows >= len(sheet_items)
    next_y = y                  # where a line left off would have gone

    # The sub-total is part of the column it foots, so it has a say in how
    # wide that column is and hangs off the same edge as the figures above.
    # One unit for the whole bill is said once, in the heading, and the
    # figures get the column's full width. Mixed units keep a label a line.
    if one_unit:
        for r in rows:
            r["unit"] = ""
    unit_w = max([_unit_w(c, r["unit"], F_R, 8) for r in rows] or [0.0])
    qty_mid = (COL_HSN + COL_QTY - unit_w) / 2.0
    qty_room = COL_QTY - unit_w - COL_HSN - 2 * PAD
    # The bold total is part of the column it foots, so it is measured too.
    qty_size, qty_x = _numcol(
        c, qty_mid, [r["qty"] for r in rows] + [fmt(total_qty)], F_R, ROW_SIZE,
        qty_room)
    rate_size, rate_x = _numcol(
        c, (COL_QTY + COL_RATE) / 2.0, [r["rate"] for r in rows], F_R, ROW_SIZE,
        COL_RATE - COL_QTY - 2 * PAD)
    amt_size, amt_x = _numcol(
        c, (COL_GST + FRAME_R) / 2.0, [r["amount"] for r in rows] + [fmt(sub_total)],
        F_R, ROW_SIZE, FRAME_R - COL_GST - 2 * PAD)
    hsn_size = ROW_SIZE
    for r in rows:
        if r["hsn"]:
            hsn_size = _shrink(c, r["hsn"], F_R, hsn_size,
                               COL_HSN - COL_PARTICULARS - 2 * 2.5, 7.0)
    gst_size = ROW_SIZE
    for r in rows:
        gst_size = _shrink(c, r["gst"], F_R, gst_size,
                           COL_GST - COL_RATE - 2 * 2.0, 7.0)

    for r in rows:
        y = r["y"]
        c.setFont(F_R, _shrink(c, r["sr"], F_R, ROW_SIZE,
                               COL_NO - 41.25 - 2.0, 6.0))
        c.drawCentredString((41.25 + COL_NO) / 2.0, y, r["sr"])
        # The line brought forward from the sheet before is set in bold.
        fr = F_B if r.get("bold") else F_R
        for k, part in enumerate(r["name"]):
            c.setFont(fr, r["nsize"])
            c.drawString(68.25, y - k * r["lead"], part)
        if r["hsn"]:
            # The same size as the description it belongs beside. It used to
            # be set at 8pt and lifted off the line, which is what made it
            # read as a footnote to the row rather than part of it.
            c.setFont(F_R, hsn_size)
            c.drawCentredString((COL_PARTICULARS + COL_HSN) / 2.0, y, r["hsn"])
        if r.get("bold"):
            # Bold, so fitted to its room as the Total strip's figures are.
            _rnum(c, qty_x, y, r["qty"], F_B, qty_size, qty_x - COL_HSN - PAD)
            _rnum(c, amt_x, y, r["amount"], F_B, amt_size, amt_x - COL_GST - PAD)
            continue
        _qty(c, qty_x, y, r["qty"], r["unit"], F_R, qty_size, 8)
        c.setFont(F_R, rate_size)
        c.drawRightString(rate_x, y, r["rate"])
        c.setFont(F_R, gst_size)
        c.drawCentredString((COL_RATE + COL_GST) / 2.0, y, r["gst"])
        c.setFont(F_R, amt_size)
        c.drawRightString(amt_x, y, r["amount"])

    # More lines than this sheet holds: they go on over the next sheet, and
    # this one says so rather than go out looking complete.
    if not last:
        c.setFont(F_BI, 8)
        c.drawString(68.25, max(next_y, TBL_TOTAL_Y + 4),
                     "Continued on sheet %d" % (sheet + 1))

    # ---------------------------------------------------------- totals strip --
    # The quantity total, the words and the amount shared no baseline at all
    # -- 242.382, 241.2 and 241.2. One line, and each figure squared under the
    # column it adds up.
    # "Total" rather than "Sub Total": the stack below says Sub Total once.
    # Centred in the 15.75pt strip for the row's larger bold figures (26 Sep
    # 2026); at 241.2 their commas reached the rule under them.
    TOT_Y = 242.6
    _rnum(c, qty_x, TOT_Y, fmt(total_qty), F_B, qty_size,
          qty_x - COL_HSN - PAD)
    c.setFont(F_B, ROW_SIZE)
    # A sheet that goes on carries its figures forward ("C/F"); the bill's
    # Total is on the last sheet.
    c.drawCentredString((COL_QTY + COL_RATE) / 2.0, TOT_Y,
                        "Total" if last else "C/F")
    _rnum(c, amt_x, TOT_Y, fmt(sub_total), F_B, amt_size,
          amt_x - COL_GST - PAD)

    # --------------------------------------------------------- bank details --
    # The heading and the bank's name are what somebody paying looks for, so
    # they are set at the size of the sheet rather than below it. The account
    # and branch lines are long enough that they are only nudged up, and each
    # is bounded so it can never run past the rule into the totals column.
    bank = data.get("bank", {})
    bank_x = _pair(c, 44.25, 227.55, "Bank Details:", F_R, 10, 119.25)
    room = SUMMARY_RIGHT - bank_x - PAD
    c.setFont(F_B, 10)
    c.drawString(bank_x, 227.382, _fit(c, bank.get("name", ""), F_B, 10,
                                       room, floor=8.0))
    # Account, IFSC and branch a line each, all at one size. Two lines had
    # the account and IFSC run together, shrunk to fit, under a branch line
    # set larger than them.
    lines = [t for t in (bank.get("account_line", ""), bank.get("ifsc_line", ""),
                         bank.get("branch_line", "")) if t][:3]
    bsize = 9.0
    for t in lines:
        bsize = _shrink(c, t, F_R, bsize, room, 7.0)
    for k, t in enumerate(lines):
        c.setFont(F_R, bsize)
        c.drawString(bank_x, 216.15 - k * 11.25, _fit(c, t, F_R, bsize, room))

    # ---------------------------------------------------------- GST summary --
    _line(c, SUMMARY_RIGHT, SUMMARY_BOT, SUMMARY_RIGHT, TBL_BOT, 1)
    _line(c, 41.25, SUMMARY_TOP, SUMMARY_RIGHT, SUMMARY_TOP, 1)
    _line(c, 41.25, SUMMARY_HDR_BOT, SUMMARY_RIGHT, SUMMARY_HDR_BOT, 1)
    _line(c, 41.25, SUMMARY_TOTAL_Y, SUMMARY_RIGHT, SUMMARY_TOTAL_Y, 1)
    _line(c, 41.25, SUMMARY_BOT, FRAME_R, SUMMARY_BOT, 1)
    for x in GS_COLS:
        _line(c, x, SUMMARY_BOT, x, SUMMARY_TOP, 1)

    # Seven headings across 340pt of narrow cells. Each is shrunk to its own
    # cell rather than set at a size the widest of them cannot wear -- at the
    # bigger type "GST Summary" left its cell at both ends.
    # One size across the row: the rate cells are headed "%", and the tax
    # each one sits beside is named over its own amount.
    gs_heads = ((41.25, GS_COLS[0], "GST Rate"),
                (GS_COLS[0], GS_COLS[1], "Taxable"),
                (GS_COLS[1], GS_COLS[2], "%"),
                (GS_COLS[2], GS_COLS[3], "SGST"),
                (GS_COLS[3], GS_COLS[4], "%"),
                (GS_COLS[4], GS_COLS[5], "CGST"),
                (GS_COLS[5], SUMMARY_RIGHT, "Total GST"))
    gsize = 7.5
    for x1, x2, label in gs_heads:
        gsize = _shrink(c, label, F_BI, gsize, x2 - x1 - 3.0, 5.0)
    c.setFont(F_BI, gsize)
    for x1, x2, label in gs_heads:
        c.drawCentredString((x1 + x2) / 2.0, 180.774, label)

    totals = data.get("totals", {})
    charges = data.get("charges") or []
    taxable_value = data.get("taxable_value")
    slabs = data.get("gst_summary")
    if not slabs:
        slabs = _derive_slabs(items)
    tot = [0.0] * 5
    # Which slabs fit, and every figure formatted, before the block is set
    # out -- the same reason the item table is read before it is drawn.
    shown = []
    for i, s in enumerate(slabs):
        yl = 168.683 - i * GS_ROW_H
        if yl < SUMMARY_TOTAL_Y + 2:
            break
        shown.append((yl, s))
        tot[0] += s["taxable"]
        tot[1] += s["sgst_amt"]
        tot[2] += s["cgst_amt"]
        tot[3] += s["total_gst"]
        # The right-hand totals column is rounded independently of the summary
        # table (that is how the original prints 3,564.09 next to 3564.08).
        tot[4] += round(s["taxable"] * s["rate"] / 200.0, 2)

    # Cell index -> the value on each slab row, and the one on the Total row.
    AMTS = {0: "taxable", 2: "sgst_amt", 4: "cgst_amt", 5: "total_gst"}
    TOT_AT = {0: 0, 2: 1, 4: 2, 5: 3}
    edge, csize = {}, {}
    for j, key in AMTS.items():
        csize[j], edge[j] = _numcol(
            c, _gs_mid(j),
            [fmt(s[key]) for _, s in shown] + [fmt(tot[TOT_AT[j]])],
            HELV, 8.5, _gs_room(j))
    for j in (1, 3):
        csize[j], edge[j] = _numcol(
            c, _gs_mid(j), [_pct(s["rate"] / 2.0) for _, s in shown],
            HELV, 8.5, _gs_room(j))

    # One baseline a row: the label, the amounts and the two rate cells used
    # to sit on 168.683, 168.502 and 168.864, which is a row that does not
    # quite line up with itself.
    # "GST 18%", set like the figures beside it. "GST @18.00" at a smaller
    # size ran over the rule into the taxable amount.
    first_room = GS_COLS[0] - 43.5 - 2.0
    if not last:
        shown = []
    for yl, s in shown:
        c.drawString(43.5, yl, _fit(c, "GST " + _pct(s["rate"]), HELV, 8.5,
                                    first_room, floor=6.0))
        for j, key in AMTS.items():
            c.setFont(HELV, csize[j])
            c.drawRightString(edge[j], yl, fmt(s[key]))
        for j in (1, 3):
            c.setFont(HELV, csize[j])
            c.drawRightString(edge[j], yl, _pct(s["rate"] / 2.0))

    c.setFont(HELV_BOLD, 8.5)
    c.drawString(43.5, 122.703, "Total")
    for j, at in TOT_AT.items():
        c.setFont(HELV_BOLD, csize[j])
        if last:
            c.drawRightString(edge[j], 122.703, fmt(tot[at]))

    # ------------------------------------------------------- totals (right) --
    _line(c, SUMMARY_RIGHT, 144.45, FRAME_R, 144.45, 1)
    sgst_v = totals.get("sgst", tot[4])
    cgst_v = totals.get("cgst", tot[4])

    # Goods, then every charge under its own name, then the figure the tax is
    # actually worked out on -- so the column adds up as it is read.
    stack = [("Sub Total", sub_total, F_B)]
    for ch in charges:
        amt = float(ch.get("amount") or 0)
        if amt:
            stack.append((ch.get("label", "Charges"), amt, F_R))
    taxed_on = float(taxable_value) if taxable_value is not None else sub_total
    if len(stack) > 1:
        stack.append(("Taxable Value", taxed_on, F_B))
    stack.append(("SGST", sgst_v, F_R))
    stack.append(("CGST", cgst_v, F_R))
    # What is added after the tax and not taxed -- TCS on a purchase bill.
    after = 0.0
    for ch in data.get("after_tax") or []:
        amt = float(ch.get("amount") or 0)
        if amt:
            stack.append((ch.get("label", "Charges"), amt, F_R))
            after += amt

    grand_raw = taxed_on + sgst_v + cgst_v + after
    grand = data.get("grand_total")
    grand = float(grand) if grand is not None else round(grand_raw)
    round_off = data.get("round_off")
    round_off = float(round_off) if round_off is not None else round(grand - grand_raw, 2)
    stack.append(("Round Off", round_off, F_R))

    # One column, one pitch, from Sub Total down to Round Off. It used to
    # stop at CGST with a gap of three rows before Round Off, which sat on
    # the rule under it. Each extra charge grows the stack upward, tightening
    # the pitch only once it would otherwise reach the item table above.
    #
    # 26 Sep 2026 (ringed on GT 218): as large as the stack's own room allows
    # -- STACK_SIZE with room to spare, and never the old 9 or 8 where it has
    # none. The rows spread up to the table above; the type is four-fifths
    # of the pitch, so no two rows touch however many charges there are.
    TOP_LIMIT = 228.5
    last_y = 148.5
    pitch = min(14.0, (TOP_LIMIT - last_y) / (len(stack) - 1))
    size = min(STACK_SIZE, max(8.0, int(pitch * 0.8 * 4) / 4.0))
    for k, (label, value, fnt) in enumerate(stack):
        yy = last_y + (len(stack) - 1 - k) * pitch
        c.setFont(fnt, size)
        c.drawString(385.5, yy, label)
        # The figure keeps clear of its own label, whatever its length.
        room = min(R_TOTALS - 452.0,
                   R_TOTALS - (385.5 + _w(c, label, fnt, size) + 6.0))
        if last:
            _rnum(c, R_TOTALS, yy, fmt(value), fnt, size, room)

    c.setFillGray(GRAND_FILL)
    c.setLineWidth(1)
    c.rect(*GRAND_BOX, stroke=1, fill=1)
    c.setFillColorRGB(0, 0, 0)
    # Larger (26 Sep 2026), centred in its shaded box; the figure shrinks
    # only if a lakh-scale total would otherwise reach its label.
    #
    # 27 Sep 2026 ("the final amount is still a bit small"): at one size
    # with its label the figure had 84pt of the box, so 34,692.00 was
    # already shrinking and a lakh total came out smaller than the words
    # beside it. The label now takes the stack's size, as Sub Total above
    # it, and the figure gets the room; the pair stays centred in the box
    # at whatever size the figure is set.
    figure = fmt(grand)
    room = R_TOTALS - (386.25 + _w(c, "Grand Total", F_B, STACK_SIZE) + 8.0)
    fsize = _shrink(c, figure, F_B, GRAND_SIZE, room, 8.0)
    GT_Y = GRAND_BOX[1] + GRAND_BOX[3] / 2 - 0.26 * fsize * TYPE
    c.setFont(F_B, STACK_SIZE)
    c.drawString(386.25, GT_Y, "Grand Total")
    c.setFont(F_B, fsize)
    if last:
        c.drawRightString(R_TOTALS, GT_Y, figure)

    # ------------------------------------------------------ words / footer --
    _line(c, 41.25, WORDS_RULE_Y, FRAME_R, WORDS_RULE_Y, 1)
    words = rupees_in_words(grand)
    wsize = _shrink(c, words, F_B, 9.5, FRAME_R - 70.0 - PAD, 7.0)
    if last:
        words_x = _pair(c, 43.5, 108.6, "Rs.", F_B, wsize, 0)
        c.drawString(words_x, 108.6, _fit(c, words, F_B, wsize,
                                          FRAME_R - words_x - PAD, floor=7.0))

    c.setFont(F_B, 9.5)
    c.drawString(45, 94.311, f"GST No: {seller.get('gstin', '')}")
    c.drawRightString(R_TOTALS, 94.311, f"For, {seller['name']}")

    sign = os.path.join(assets_dir, seller.get("signature", "signature.jpg"))
    if os.path.isfile(sign) and data.get("show_signature", True):
        c.drawImage(ImageReader(sign), *SIGN_RECT,
                    preserveAspectRatio=False, mask=None)

    logos = os.path.join(assets_dir, seller.get("logos", "mill_logos.png"))
    if os.path.isfile(logos) and data.get("show_logos", True):
        c.drawImage(ImageReader(logos), *LOGOS_RECT,
                    preserveAspectRatio=True, anchor='sw', mask=None)

    c.setFont(F_R, 10)
    c.drawString(47.25, 48.45, seller.get("jurisdiction_line",
                                          "Subject to RAJKOT Jurisdiction."))
    c.drawString(47.25, 35.7, "E. & O. E.")
    # Set from the right: at the bigger size its measured left edge ran it
    # 12pt off the sheet.
    c.drawRightString(FRAME_R - PAD, 35.7, "Authorised Signatory")
    return drawn_rows, total_qty, sub_total, last


def _derive_slabs(items):
    """Group line items by GST rate when no explicit summary is supplied."""
    buckets = {}
    for it in items:
        qty, rate = float(it["qty"]), float(it["rate"])
        amt = it.get("amount")
        amt = float(amt) if amt is not None else round(qty * rate, 2)
        pct = float(it.get("gst_pct", 18))
        buckets[pct] = buckets.get(pct, 0.0) + amt
    out = []
    for pct in sorted(buckets):
        taxable = round(buckets[pct], 2)
        half = int(taxable * (pct / 2.0)) / 100.0        # truncated, as on the original
        out.append({"rate": pct, "taxable": taxable, "sgst_amt": half,
                    "cgst_amt": half,
                    "total_gst": round(taxable * pct / 100.0, 2)})
    return out
