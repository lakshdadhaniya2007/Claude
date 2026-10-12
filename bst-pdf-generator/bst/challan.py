"""
BST -- DELIVERY CHALLAN (two identical copies side by side, A4 landscape).

Coordinates measured off the original PDF. One copy is drawn by `_draw_copy()`
at a horizontal offset, so the left copy uses dx=0 and the right copy dx=COPY_DX.
Page is exactly 841.65 x 595.2.

Tidied on 16 Sep 2026, off a printed G 35, to read like the bill beside it:
a title row (DELIVERY CHALLAN / firm / ORIGINAL or DUPLICATE) in place of the
corner tab, the party's address under the name, the whole year in the date,
one size for the headings and one for an item row, a bold "Total" row, the
charges named in the order they add up, and every label sharing its value's
baseline. Thickness and cutting size are not printed (21 Sep 2026); their
room went to Particulars.
"""

import os
from reportlab.pdfgen import canvas

from .fonts import register_fonts, font, VERDANA, VERDANA_BOLD
from .amount_words import rupees_in_words, fmt
from .text import wrap_lines, wrap_fixed

PAGE_W, PAGE_H = 841.65, 595.2

COPY_DX = 408.75          # horizontal distance between the two copies

# ------------------------------------------------------------- copy geometry --
L = 47.775                # left border of a copy
R = 398.775               # right border of a copy
TOP = 546.29999           # top rule
BOT = 43.65               # bottom rule

HDR_BOT = 483.29999       # bottom of the "To / No / Date" header block
COL_HDR_BOT = 468.29999   # bottom of the column-heading row
# The item body stops 3pt higher than it did (170.4 until 26 Sep 2026) so the
# total row below it is tall enough for its larger bold figures.
BODY_BOT = 173.39999      # bottom of the item body
TOTWG_BOT = 157.64999     # bottom of the "Total Weight" row
FOOT_TOP = 87.15          # rule above the receiver-signature strip

# The header block's own divider, between the "To" address and the No/Date
# panel. It used to share a coordinate with the Qty column; the item columns
# have moved since, so it is held separately and stays where it always was.
HDR_SPLIT = 270.52499

# Column widths are set so the figures that actually turn up -- a lakh-scale
# amount, a five-digit weight -- print at full size. Anything wider still
# shrinks to fit rather than being clipped.
#
# Thickness and cutting size are not printed at all (21 Sep 2026, asked for
# with the two boxes taken off the challan and bill screens). Their 90pt went
# to Particulars.
#
# 26 Sep 2026, off a printed G 73 with them ringed in red: the goods, the
# kilos, the rate, the amount, the Total row, the charges and the Grand Total
# are what a customer reads, and they have to be big enough for anyone to
# read. They are set larger (the sizes below), and the figure columns were
# widened to hold them at that size -- sized off the 2,187 challans since
# April 2025: a lakh-scale amount, a four-digit weight in bold on the total
# row. Particulars gave the room; a long name breaks over two lines at the
# larger size rather than shrinking. The Qty column's left rule now falls on
# the header's own divider (HDR_SPLIT), so one line runs top to bottom.
C_NO = 64.275             # right edge of "No"      (16.5 wide)
C_PART = 201.77499        # right edge of "Particulars"  (137.5)
C_QTY = 270.52499         # right edge of "Qty kg."      (68.75)
C_RATE = 315.52499        # right edge of "Rate kg."     (45.0)
                          # Amount takes the remaining   (83.25)

PAD = 3.0                 # breathing room between a figure and its rule

# The sizes of what was ringed, before TYPE (below) enlarges everything.
ITEM_SIZE = 11.0          # every figure beside the goods (was 9)
# The goods themselves, one size on every challan whatever their length
# (6 Oct 2026) -- the size nearly every challan already printed them at. A
# long name takes more lines; it is never set smaller.
NAME_SIZE = 10.0
TOTAL_LABEL_SIZE = 10.0   # "Total" on the total row (was 8.5)
CHARGE_SIZE = 10.0        # the cutting, transport, loading, round off figures (8.5)
GRAND_LABEL_SIZE = 7.0    # "Grand" over "Total", beside the figure (was one line at 8)
GRAND_SIZE = 12.0         # the grand total itself (was 9)
MOBILE_SIZE = 9.5         # the customer's mobile number, in bold (8.5)

BAL_L = 47.775            # opening/this-bill/closing balance block
BAL_R = 260.27499
BAL_TOP = 156.89999
BAL_MID1 = 122.4
BAL_MID2 = 108.9
BAL_BOT = 87.15

TOT_L = 260.27499         # round-off / loading / total block
TOT_R = 398.775

ROW_H = 12.75
ROW1_BASELINE = 455.85

# ------------------------------------------------------------------- size --
# Asked for on 10 Sep 2026, alongside the bill: make everything bigger. Two
# copies have to sit side by side on one landscape A4, so the copy itself
# cannot grow much -- what grows is the type and the pitch of the item rows.
# One factor, applied by scaling `setFont` on the canvas; `_w` measures at the
# size text will actually be set in, so a long product name still shrinks or
# breaks across two lines rather than running over its rule. To change the
# size again, change these two numbers -- do not nudge the literals.
TYPE = 1.20                    # every point size on the copy
# Row pitch, where the body has room for it: 1.20 for the 9pt rows until
# 26 Sep 2026, raised with them to 11pt so the rows still stand as far apart
# for their size.
ROW_GROW = 1.40

SIGN_RECT = (275.77499, 57.9, 99.0, 27.75)   # relative x is absolute for dx=0


def _magnify(c):
    """Set every piece of type drawn on this canvas `TYPE` times bigger.

    Wrapping `setFont` catches the sizes inside the helpers as well as the
    literals in `_draw_copy`, so none can be missed. ReportLab's own
    `drawRightString` measures with the current font, which is the scaled one,
    so it still right-aligns correctly.
    """
    if abs(TYPE - 1.0) < 1e-9:
        return
    plain = c.setFont
    c.setFont = lambda name, size, leading=None: plain(name, size * TYPE)


def _w(c, text, fontname, size):
    """Width of `text` at the size it will actually be set in."""
    return c.stringWidth(text, fontname, size * TYPE)


def _fit_pitch(lay, body):
    """The row pitch at which laid-out rows `lay` -- (lines, size, lead,
    pitch) each -- just fill `body`, their second lines included."""
    if len(lay) < 2:
        return ROW_H
    extra = sum(lead * (max(1, len(n)) - 1) for n, _, lead, _p in lay)
    return int((body - extra) / (len(lay) - 1) * 4) / 4.0


def _row_pitch(n):
    """How far apart the item rows stand on a challan of `n` lines.

    `ROW_GROW` times the original, tightening back towards it -- never below
    it -- on a challan long enough that the wider pitch would push a line off
    the copy. The busiest challan on the books is 18 lines.
    """
    if n < 2:
        return ROW_H * ROW_GROW
    room = ROW1_BASELINE - (BODY_BOT + 4)
    return max(ROW_H, min(ROW_H * ROW_GROW, room / (n - 1)))


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


def _shrink(c, text, fontname, size, width, floor=6.0):
    """Step a font down until the text fits. Never clip a legal figure."""
    while size > floor and _w(c, text, fontname, size) > width:
        size -= 0.25
    return size


def _wrap(c, text, fontname, width, head=''):
    """An item name's lines at `NAME_SIZE` -- see `bst.text.wrap_fixed`."""
    return wrap_fixed(lambda t: _w(c, t, fontname, NAME_SIZE), text, width,
                      head)


def _line(c, x1, y1, x2, y2, w=1, dx=0.0):
    c.setLineWidth(w)
    c.line(x1 + dx, y1, x2 + dx, y2)


def _draw_copy(c, data, dx, assets_dir, copy_label="", start=0, bf=None,
               sheet=1):
    """One copy of one sheet. A challan whose goods will not go on one pad
    at their own size goes on over more sheets (6 Oct 2026): `start` is the
    first line this sheet carries and `bf` the (qty, amount) brought forward
    from the sheets before. Answers (lines drawn, qty, amount, last sheet)."""
    F_R, F_B = font(VERDANA), font(VERDANA_BOLD)
    seller = data["seller"]
    buyer = data["buyer"]
    meta = data["challan"]
    items = data["items"]

    def s(x, y, t, f=None, size=None):
        if f:
            c.setFont(f, size)
        c.drawString(x + dx, y, t)

    def rs(x, y, t):
        c.drawRightString(x + dx, y, t)

    def cs(x, y, t):
        c.drawCentredString(x + dx, y, t)

    # ------------------------------------------------------------- outer box --
    _line(c, L, TOP, R, TOP, 1, dx)
    _line(c, L, BOT, L, 545.54999, 1, dx)
    _line(c, R, 42.9, R, 545.54999, 1, dx)
    # Square with the two sides -- it used to run 1.5pt past the right one.
    _line(c, L - 0.5, BOT, R + 0.5, BOT, 2, dx)

    # ------------------------------------------------------------- title row --
    # The bill's own arrangement: what the document is on the left, whose it
    # is in the middle, which copy on the right. The firm's short name was the
    # only word up here, which told a customer nothing about the paper.
    TITLE_Y = 550.5
    c.setFont(F_B, 11)
    cs((L + R) / 2.0, TITLE_Y, seller.get("short_name", "BST"))
    c.setFont(F_B, 9)
    s(L + 0.75, TITLE_Y, meta.get("title", "DELIVERY CHALLAN"))
    if copy_label:
        rs(R - 0.75, TITLE_Y, copy_label)

    # ------------------------------------------------------------- header row --
    _line(c, HDR_SPLIT, HDR_BOT, HDR_SPLIT, TOP, 1, dx)       # header/right split
    _line(c, L + 0.75, HDR_BOT, R + 0.75, HDR_BOT, 1, dx)
    _line(c, L, COL_HDR_BOT, R, COL_HDR_BOT, 1, dx)

    # To, the name, and the address beneath it -- the same address the bill
    # prints, lined up under the name. It used to be the city alone, printed
    # as "-RAJKOT".
    NAME_Y = 536.232
    c.setFont(F_B, 9)
    s(50.775, NAME_Y, "To,")
    nm_x = max(69.75, 50.775 + _w(c, "To,", F_B, 9) + 2.25)
    room = HDR_SPLIT - nm_x - PAD
    s(nm_x, NAME_Y, _fit(c, buyer["name"], F_B, 9, room, floor=7.0))
    # Four lines under the name: the address, broken where a line is too
    # long rather than cut off, then the mobile number.
    mobile = seller.get("mobile_line", "")
    under = wrap_lines(buyer.get("address") or [buyer.get("city", "")],
                       lambda t: _w(c, t, F_R, 8.5), room, 3 if mobile else 4)
    under = under[:3 if mobile else 4]
    for k, line in enumerate(under):
        s(nm_x, 525.15 - k * 10.75, _fit(c, line, F_R, 8.5, room, floor=6.5))
    if mobile:
        # Ringed on the printed G 73 (26 Sep 2026): the number is set larger,
        # and in bold, a little further below the address so the bigger type
        # clears it. Label and number at one size -- the largest that fits.
        label, _, number = mobile.partition(" : ")
        label, number = (label + " :", number) if number else ("", mobile)
        size = MOBILE_SIZE

        def lead(sz):                  # the label and the space after it
            return _w(c, label + " ", F_R, sz) if label else 0.0
        while size > 6.5 and lead(size) + _w(c, number, F_B, size) > room:
            size -= 0.25
        mob_y = 525.15 - len(under) * 10.75 - 1.4
        if label:
            c.setFont(F_R, size)
            s(nm_x, mob_y, label)
        num_x = nm_x + lead(size)
        s(num_x, mob_y, _fit(c, number, F_B, size, room - (num_x - nm_x)))

    # No., Date and Terms: label, colon and value each in their own column,
    # on the 12.75pt rhythm, one size for the three values.
    LBL_X, COLON_X, VAL_X = 276.52499, 298.5, 305.25
    panel = (("No.", meta.get("number", "")),
             ("Date", meta.get("date", "")),
             ("Terms", meta.get("payment_terms", "")))
    colon_x = max(COLON_X, LBL_X + max(
        _w(c, t, F_R, 8.5) for t, _ in panel) + 3.0)
    val_x = max(VAL_X, colon_x + (VAL_X - COLON_X))
    room = R - val_x - PAD
    vsize = 8.5
    for _, value in panel:
        if value:
            vsize = _shrink(c, value, F_R, vsize, room, 6.5)
    for yy, (label, value) in zip((533.4, 520.65, 507.9), panel):
        c.setFont(F_R, 8.5)
        s(LBL_X, yy, label)
        s(colon_x, yy, ":")
        if value:
            s(val_x, yy, _fit(c, value, F_R, vsize, room))

    # ------------------------------------------------------ column headings --
    heads = [(L, C_NO, "No"), (C_NO, C_PART, "Particulars"),
             (C_PART, C_QTY, "Qty kg."), (C_QTY, C_RATE, "Rate kg."),
             (C_RATE, R, "Amount")]
    # One size for the row of headings: they were set at four.
    hsize = 8.0
    for x1, x2, label in heads:
        hsize = _shrink(c, label, F_R, hsize, x2 - x1 - 2.0, 6.0)
    c.setFont(F_R, hsize)
    for x1, x2, label in heads:
        cs((x1 + x2) / 2.0, 472.2, label)

    # --------------------------------------------------------- item columns --
    for x in (C_NO, C_PART, C_QTY, C_RATE):
        _line(c, x, TOTWG_BOT, x, HDR_BOT, 1, dx)
    _line(c, L + 0.75, TOTWG_BOT, R + 0.75, TOTWG_BOT, 1, dx)
    _line(c, L + 0.75, BODY_BOT, R + 0.75, BODY_BOT, 1, dx)

    # Right-aligned figures under a centred heading read as shoved to the
    # right -- worst in Rate kg., where five characters sit in a forty-point
    # column. Keep the right alignment, because a money column has to scan
    # down its decimal points, but slide the whole block of figures until it
    # is centred under its own heading. Every figure that shares a column --
    # the item rows, the totals row, the charges and the grand total -- is
    # measured together so they all still line up with each other.
    _amts = [float(it["amount"]) if it.get("amount") is not None
             else round(float(it["qty"]) * float(it["rate"]), 2) for it in items]
    _tq = sum(round(float(it["qty"]), 2) for it in items)
    _charges = [float(data.get(k) or 0)
                for k in ("cutting", "transport", "round_off", "loading")]
    _grand = data.get("grand_total")
    _grand = float(_grand) if _grand is not None else sum(_amts) + sum(_charges)

    # Each figure is measured the way it prints -- a row's in regular type,
    # the total row's in bold, the charges at their own size -- so the block
    # is centred on what is really there. The grand total stands in a row of
    # its own with no rule above the Amount column, so it is left out: it
    # right-aligns to the same edge and may reach further left.
    def _block_edge(x1, x2, entries):
        widest = 0.0
        for t, f, size in entries:
            sz = _shrink(c, t, f, size, x2 - x1 - PAD, 5.5)
            widest = max(widest, _w(c, t, f, sz))
        return min(x2 - PAD, x1 + (x2 - x1 + widest) / 2.0)

    e_qty = _block_edge(C_PART, C_QTY,
                        [(fmt(float(it["qty"])), F_R, ITEM_SIZE) for it in items]
                        + [(fmt(_tq), F_B, ITEM_SIZE)])
    e_rate = _block_edge(C_QTY, C_RATE,
                         [(fmt(float(it["rate"])), F_R, ITEM_SIZE) for it in items]
                         or [("0.00", F_R, ITEM_SIZE)])
    e_amt = _block_edge(C_RATE, R,
                        [(fmt(a), F_R, ITEM_SIZE) for a in _amts]
                        + [(fmt(v), F_R, CHARGE_SIZE) for v in _charges if v]
                        + [(fmt(sum(_amts)), F_B, ITEM_SIZE)])

    total_qty, total_amt = bf if bf else (0.0, 0.0)
    items = items[start:]
    # A row is one line tall unless the item name needs two, so the cursor is
    # carried down rather than worked out from the row number.
    # The rows stand further apart to match the bigger type, as far as the
    # body has room for; a challan long enough to need the old pitch keeps it.
    name_x = 66.525
    # The goods at one size, `NAME_SIZE`, on every challan (6 Oct 2026: "as
    # there is more text in Particulars the font size decreases -- make sure
    # the font size remains the same and it shouldn't go outside the given
    # space"). They used to come down to fit: the column a point for one long
    # name, then a two-line name further still until both halves fitted --
    # a steel with its piece count, "0.70 CRCA - REGULAR 400.00", printed at
    # 6.5. Now a long name takes a third line, or more, inside the column.
    name_room = C_PART - name_x - PAD
    # Lay the rows out before drawing any: a name over several lines takes
    # their room, and a challan whose names all break may not fit the body at
    # the wider pitch. Tighten the pitch first, then the names' own lines
    # (their type is 12pt; 13 is the comfortable spacing), and only then
    # leave rows off -- with room kept for the notice that says so. The type
    # never gets smaller.
    body = ROW1_BASELINE - (BODY_BOT + 4)
    # A sheet after the first opens with what the ones before it came to.
    bf_pitch = ROW_H * ROW_GROW if bf else 0.0
    body -= bf_pitch
    names = [_wrap(c, it["particulars"], F_R, name_room, it.get("head", ""))
             for it in items]

    def layout(lead, pitch):
        return [(n, NAME_SIZE, lead, pitch) for n in names]

    def height(lay):
        return sum(p + lead * (max(1, len(n)) - 1) for n, _, lead, p in lay) \
            - (lay[-1][3] if lay else 0.0)

    lead = NAME_SIZE * TYPE + 1.0
    lay = layout(lead, _row_pitch(len(items)))
    if lay and height(lay) > body:
        # The widest pitch at which the names' second lines still fit,
        # rather than straight down to the tightest (26 Sep 2026): a busy
        # challan's rows had bunched up with room to spare below them.
        lay = layout(lead, max(ROW_H, _fit_pitch(lay, body)))
        if height(lay) > body:
            lead = NAME_SIZE * TYPE
            lay = layout(lead, max(lead, _fit_pitch(layout(lead, ROW_H), body)))
        if height(lay) > body:
            # Not on one sheet even at the tightest: the rest goes on the
            # next, so this one keeps the ordinary spacing.
            lead = NAME_SIZE * TYPE + 1.0
            lay = layout(lead, _row_pitch(len(items)))
    fits_all = not lay or height(lay) <= body
    floor_y = BODY_BOT + 4 + (0.0 if fits_all else 10.0)

    y = ROW1_BASELINE
    if bf:
        c.setFont(F_B, NAME_SIZE)
        s(name_x, y, _fit(c, "Brought forward", F_B, NAME_SIZE, name_room))
        for x, left, t in ((e_qty, C_PART, fmt(total_qty)),
                           (e_amt, C_RATE, fmt(total_amt))):
            c.setFont(F_B, _shrink(c, t, F_B, ITEM_SIZE, x - left - PAD, 5.5))
            rs(x, y, t)
        y -= bf_pitch
    drawn_rows = 0
    for i, it in enumerate(items):
        name, row_size, lead, pitch = lay[i]
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
        amt = it.get("amount")
        amt = float(amt) if amt is not None else round(qty * rate, 2)
        # Total what the page shows, so the column adds up as it is read.
        total_qty += round(qty, 2)
        total_amt += amt

        # A second line stands a full line below the first (`lead`). It was
        # spaced for the type before it was enlarged, and the two touched.
        sr = str(it.get("sr", i + 1))
        c.setFont(F_R, _shrink(c, sr, F_R, ITEM_SIZE, C_NO - L - 2.0, 5.5))
        cs((L + C_NO) / 2.0, y, sr)
        for k, part in enumerate(name):
            c.setFont(F_R, row_size)
            s(name_x, y - k * lead, part)
        for x, txt, left in ((e_qty, fmt(qty), C_PART),
                             (e_rate, fmt(rate), C_QTY),
                             (e_amt, fmt(amt), C_RATE)):
            c.setFont(F_R, _shrink(c, txt, F_R, ITEM_SIZE, x - left - PAD, 5.5))
            rs(x, y, txt)

        # A wrapped name needs every extra line's worth of room beneath it.
        y -= pitch + lead * (max(1, len(name)) - 1)

    # More lines than the pad holds: they go on over the next sheet, and
    # this one says so rather than travel with the goods looking complete.
    last = drawn_rows >= len(items)
    if not last:
        c.setFont(F_B, 8)
        s(name_x, max(y, BODY_BOT + 4),
          _fit(c, "Continued on sheet %d" % (sheet + 1), F_B, 8, name_room))

    # -------------------------------------------------------------- total row --
    # "Total", and both of its figures in bold -- the bill's total row reads
    # the same way. A sheet that goes on carries its figures forward; the
    # charges, the grand total and the balances are on the last sheet only.
    TOT_Y = 162.0
    c.setFont(F_B, TOTAL_LABEL_SIZE)
    rs(C_PART - PAD, TOT_Y, "Total" if last else "Carried forward")
    for x, left, t in ((e_qty, C_PART, fmt(total_qty)),
                       (e_amt, C_RATE, fmt(total_amt))):
        c.setFont(F_B, _shrink(c, t, F_B, ITEM_SIZE, x - left - PAD, 5.5))
        rs(x, TOT_Y, t)

    # -------------------------------------------------------- balance block --
    _line(c, BAL_R, 93.9, BAL_R, BAL_TOP, 1, dx)
    _line(c, BAL_R, 42.9, BAL_R, 95.4, 1, dx)
    _line(c, BAL_L, BAL_MID1, BAL_R, BAL_MID1, 1, dx)
    _line(c, BAL_L, BAL_MID2, BAL_R, BAL_MID2, 1, dx)
    _line(c, BAL_L, FOOT_TOP, R, FOOT_TOP, 1, dx)

    # Opening + This Bill = Closing, in three equal cells with the operators
    # in the gaps between them. The three figures share one font size -- the
    # largest that fits all of them -- so a crore opening balance cannot end up
    # half the size of the one beside it, and none of them runs into a rule.
    bal = data.get("balances", {})
    OP_W = 11.0                                  # gap holding "+" and "="
    cell = (BAL_R - BAL_L - 2 * OP_W) / 3.0
    cells = [(BAL_L + i * (cell + OP_W), BAL_L + i * (cell + OP_W) + cell)
             for i in range(3)]

    labels = ("Opening Bal.", "This Bill", "Closing Bal.")
    lsize = 7.0
    for label in labels:                         # clear of the frame, too
        lsize = _shrink(c, label, F_B, lsize, cell - 2 * PAD, 5.0)
    c.setFont(F_B, lsize)
    for (x1, x2), label in zip(cells, labels):
        cs((x1 + x2) / 2.0, 143.0, label)
    c.setFont(F_B, 10)
    for (_, x2), op in zip(cells[:2], ("+", "=")):
        cs(x2 + OP_W / 2.0, 140.94, op)

    if bal and last:
        figures = [fmt(bal.get(k, 0))
                   for k in ("opening", "this_bill", "closing")]
        room = cell - 5.0
        size = 8.0
        for t in figures:                        # one size that suits them all
            size = min(size, _shrink(c, t, F_B, 8, room, 5.0))
        for (x1, x2), t in zip(cells, figures):
            c.setFont(F_B, size)
            cs((x1 + x2) / 2.0, 132.903, t)

    # Label and name on one baseline, the name starting clear of the label --
    # at the bigger type the two ran into each other.
    DRV_Y = 112.05
    c.setFont(F_R, 8)
    s(50.775, DRV_Y, "Driver Name:")
    drv_x = 50.775 + _w(c, "Driver Name:", F_R, 8) + 4.0
    if meta.get("driver_name"):
        s(drv_x, DRV_Y, _fit(c, meta["driver_name"], F_R, 8,
                             BAL_R - drv_x - PAD, floor=6.5))

    # ------------------------------------------------ charges / grand total --
    _line(c, C_RATE, 104.4, C_RATE, BAL_TOP, 1, dx)
    _line(c, TOT_L + 0.75, 144.89999, TOT_R, 144.89999, 0, dx)
    _line(c, TOT_L + 0.75, 132.14999, TOT_R, 132.14999, 0, dx)
    _line(c, TOT_L + 0.75, 118.65, TOT_R, 118.65, 0, dx)
    _line(c, TOT_L + 0.75, 104.4, TOT_R, 104.4, 1, dx)

    round_off = float(data.get("round_off") or 0)
    loading = float(data.get("loading") or 0)
    cutting = float(data.get("cutting") or 0)
    transport = float(data.get("transport") or 0)
    grand = data.get("grand_total")
    grand = float(grand) if grand is not None else (
        total_amt + cutting + transport + round_off + loading)

    # Four charge rows between the item body and the grand total, in the
    # order they are added up: cutting, transport, loading, then the round
    # off. The labels are short so all four can be set at one readable size
    # in the room the pad gives them, and each shares its figure's baseline.
    lbl_x = TOT_L + 3.0
    lbl_room = C_RATE - lbl_x - PAD
    # Named rather than fixed, because a challan out of the VAT years carries
    # tax on this row and calling that a round off would leave the page not
    # adding up. Since the Amount column widened (26 Sep 2026) "GST + Round
    # Off" no longer goes in the label cell at a readable size -- 17
    # challans, 2020-25 -- so on those it is written "GST + R/Off".
    ro_label = meta.get("round_off_label", "Round Off")
    if ro_label != "Round Off" and _w(c, ro_label, F_R, 6.5) > lbl_room:
        ro_label = ro_label.replace("Round Off", "R/Off")
    charge_rows = (
        (147.8, meta.get("cutting_label", "Cutting"), cutting),
        (135.05, meta.get("transport_label", "Transport"), transport),
        (121.55, meta.get("loading_label", "Loading"), loading),
        (108.0, ro_label, round_off))
    csize = 7.5
    for _, txt, _ in charge_rows:
        csize = _shrink(c, txt, F_R, csize, lbl_room, 5.5)
    room = e_amt - C_RATE - PAD
    for yy, txt, v in charge_rows:
        c.setFont(F_R, csize)
        s(lbl_x, yy, txt)
        if v and last:
            t = fmt(v)
            c.setFont(F_R, _shrink(c, t, F_R, CHARGE_SIZE, room, 5.5))
            rs(e_amt, yy, t)
    # The grand total is the largest figure on the copy (26 Sep 2026). Its
    # box is 138pt across with no rule above the Amount column, too narrow
    # for "Grand Total" on one line and a figure this size beside it, so the
    # two words stand one over the other at its left and the figure reaches
    # left of the Amount column -- as far as the words, and no further.
    GT_Y = 90.8
    c.setFont(F_B, GRAND_LABEL_SIZE)
    s(lbl_x, 97.3, "Grand")
    s(lbl_x, 89.6, "Total")
    gt_room = e_amt - (lbl_x + max(_w(c, "Grand", F_B, GRAND_LABEL_SIZE),
                                   _w(c, "Total", F_B, GRAND_LABEL_SIZE)) + 6.0)
    t = fmt(grand)
    c.setFont(F_B, _shrink(c, t, F_B, GRAND_SIZE, gt_room, 6.0))
    if last:
        rs(e_amt, GT_Y, t)

    # ---------------------------------------------------- words and footer --
    # The amount in words, on the last sheet with the grand total.
    if last:
        words = rupees_in_words(grand)
        c.setFont(F_R, 8)
        words_x = max(64.275, 50.775 + _w(c, "Rs.", F_R, 8) + 3.0)
        room = BAL_R - words_x - PAD
        size = _shrink(c, words, F_R, 8, room, 7.0)
        if _w(c, words, F_R, size) <= room:
            rows_out = [(99.3, words)]
        else:
            # Two lines beat shrinking a legal figure into illegibility.
            size = 7.0
            rest = words.split(' ')
            first = ''
            while rest and _w(c, (first + ' ' + rest[0]).strip(),
                              F_R, size) <= room:
                first = (first + ' ' + rest.pop(0)).strip()
            # The band between the driver-name rule (108.9) and the footer rule
            # (87.15) is 21.75pt. At 103.05 the capitals of the first line reached
            # 108.1 and sat on the rule above; these two baselines clear both.
            rows_out = [(101.0, first), (92.5, ' '.join(rest))]
        # "Rs." at the size of the words it stands in front of.
        c.setFont(F_R, size)
        s(50.775, rows_out[0][0], "Rs.")
        for yy, txt in rows_out:
            s(words_x, yy, _fit(c, txt, F_R, size, room))
            c.setFont(F_R, size)

    # The two signature boxes read alike: a line of their own at the top of
    # the receiver's, and each name centred in its box on one baseline.
    note = _fit(c, "Above mentioned goods received in good condition.",
                F_R, 6.5, BAL_R - BAL_L - 2 * PAD, floor=5.0)
    cs((BAL_L + BAL_R) / 2.0, 77.1, note)
    c.setFont(F_R, 8.5)
    cs((BAL_L + BAL_R) / 2.0, 48.0, "Receiver's Signature")
    cs((BAL_R + R) / 2.0, 48.0, "Authorised Signatory")

    sign = os.path.join(assets_dir, seller.get("signature", "signature.jpg"))
    if os.path.isfile(sign) and data.get("show_signature", True):
        from reportlab.lib.utils import ImageReader
        img = ImageReader(sign)
        x, y, w, h = SIGN_RECT
        # Fit the signature inside its cell on its own proportions and centre
        # it there. Stretching it to the cell leaves the hand squashed.
        iw, ih = img.getSize()
        if iw and ih:
            scale = min(w / float(iw), h / float(ih))
            dw, dh = iw * scale, ih * scale
            x, y, w, h = x + (w - dw) / 2.0, y + (h - dh) / 2.0, dw, dh
        # Knock the paper out so the signature sits on the pad rather than in
        # a white patch of its own.
        c.drawImage(img, x + dx, y, w, h, preserveAspectRatio=True,
                    mask=[200, 255, 200, 255, 200, 255])
    return drawn_rows, total_qty, total_amt, last


# The copies on one sheet, in order. The customer takes the first with the
# goods; the second is signed for and kept.
COPY_LABELS = ("ORIGINAL", "DUPLICATE")


def build_challan(data, out_path, assets_dir=None, copies=2):
    """Render a delivery challan sheet (2 identical copies by default)."""
    register_fonts()
    if assets_dir is None:
        assets_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

    c = canvas.Canvas(out_path, pagesize=(PAGE_W, PAGE_H))
    _magnify(c)
    c.setTitle(f"Delivery Challan {data['challan'].get('number', '')}")
    c.setStrokeColorRGB(0, 0, 0)
    c.setFillColorRGB(0, 0, 0)

    # The little tab the original sheet had at its top-left corner is gone:
    # printed on a customer's copy it read as a stray mark.
    labels = data.get("copy_labels") or COPY_LABELS
    # Sheet after sheet until every line is on one (6 Oct 2026): the goods
    # keep their size, so a long challan goes on over a second sheet rather
    # than shrinking onto one.
    start, bf, sheet = 0, None, 1
    while True:
        for i in range(max(1, copies)):
            drawn, qty, amt, last = _draw_copy(
                c, data, i * COPY_DX, assets_dir,
                labels[i] if i < len(labels) else "", start, bf, sheet)
        c.showPage()
        if last or not drawn:
            break
        start, bf, sheet = start + drawn, (qty, amt), sheet + 1
    c.save()
    return out_path
