#!/usr/bin/env python3
"""An account's statement between two dates, as a PDF to send the customer.

Asked for on 26 Sep 2026: *"add option to send custom date account history to
customers ... everything under those dates should be shown and it should be
generated as a pdf."*

**It is SHREE's own printed ledger** -- `LEDGER.TXT` in the SHREE folder is
what the customers have been sent for years, and the account screen already
reads the same way ([[account-view-month-wise]] in the notes):

    BST
    Ledger of VIHANA ENTERPRISE MOHITBHAI
    <address>
    FROM 01/04/2026 TO 09/09/2026                          PAGE NO.  1
    Date      Particulars                         Credit          Debit
    01/04/26  Opening Balance                                 1,44,285.00
    13/04/26  KOTAK BANK : B. S. INDUSTRIES   1,44,285.00
              Rcpt
    ...
                                             12,99,248.00    16,85,735.00
              Closing Debit Balance           3,86,487.00
                                             16,85,735.00    16,85,735.00

Oldest first, the opening the first line, the account on the other side of
each entry with its number and reference beneath, and the closing struck on
the lighter side so the two columns come out equal. **Every entry between the
dates is on it**; nothing is summarised. A challan and the GST raised on it
are one line here as they are on the screen (15 Sep 2026), with the two
amounts and the GST bill's number written under it.

The figures are the report's own (`api.report_ledger`), so the PDF can never
say something the screen does not.
"""
import datetime
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, NextPageTemplate,
                                PageTemplate, Paragraph, Spacer, Table,
                                TableStyle)
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfgen import canvas as rl_canvas

from bst.amount_words import fmt
from bst.fonts import VERDANA, VERDANA_BOLD, VERDANA_BOLD_ITALIC, font

PAGE_W, PAGE_H = A4
SIDE = 14 * mm
TOP_LATER = 26 * mm        # the name, the dates and the page on the rest
BOTTOM = 16 * mm
INK = colors.HexColor('#16202c')
SOFT = colors.HexColor('#5d6b7a')
RULE = colors.HexColor('#9aa6b2')
SHADE = colors.HexColor('#eef2f6')


def _d(iso):
    """2026-04-01 -> 01/04/2026, the way the bill and the challan print it."""
    parts = str(iso or '').split('-')
    return '%s/%s/%s' % (parts[2], parts[1], parts[0]) if len(parts) == 3 else ''


def _x(s):
    """Text for a Paragraph: its markup characters made plain."""
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;'))


def _money(v):
    return fmt(abs(float(v or 0)))


class _Numbered(rl_canvas.Canvas):
    """A canvas that knows how many pages there are once it has drawn them
    all, so every page can say "Page 2 of 3"."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._pages = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._page_count(total)
            super().showPage()
        super().save()

    def _page_count(self, total):
        self.setFont(font(VERDANA), 8.5)
        self.setFillColor(SOFT)
        self.drawRightString(PAGE_W - SIDE, PAGE_H - 14 * mm,
                             'Page %d of %d' % (self._pageNumber, total))


def _particulars(e):
    """What an entry was written against, and beneath it what it was: the
    reference on one line and, for a challan with its GST, the two amounts on
    the next -- each a line of its own rather than wrapped wherever it ran
    out of room."""
    head = e.get('particulars') or e.get('kind') or ''
    first = []
    ref = (e.get('ref') or '').strip()
    no = (e.get('no') or '').strip()
    if ref:
        first.append(ref)
    elif no:
        first.append('%s No.: %s' % (e.get('kind') or 'Entry', no))
    elif e.get('kind'):
        first.append(e['kind'])
    if e.get('bill_no'):
        first.append('GST bill %s' % e['bill_no'])
    elif e.get('gst_ref'):
        first.append('GST %s' % e['gst_ref'])
    narration = (e.get('narration') or '').strip()
    if narration and narration not in ' '.join(first) \
            and narration != no.replace(' ', ''):
        first.append(narration)
    lines = [' · '.join(first)] if first else []
    if e.get('gst'):
        lines.append('Challan %s + GST %s' % (_money(e.get('challan_amount')),
                                               _money(e['gst'])))
    return head, lines


def build(rep, frm, to, firm):
    """The statement as PDF bytes.

    `rep` is `api.report_ledger` for the account over [frm, to]; `firm` the
    company whose book the account is kept in -- its name heads the page, as
    SHREE heads its ledger with the firm.
    """
    p = rep['party']
    name = p.get('name') or ''
    address = [x for x in (p.get('address1'), p.get('address2'),
                           ' '.join(y for y in (p.get('city'), p.get('pin')) if y))
               if (x or '').strip()]
    contact = ' · '.join(x for x in (
        ('GSTIN: %s' % p['gstin']) if p.get('gstin') else '',
        ('Phone: %s' % p['phone']) if p.get('phone') else '') if x)
    period = 'From %s To %s' % (_d(frm) or 'the first entry', _d(to))
    firm_name = firm.get('name') or firm.get('short_name') or ''
    firm_lines = [x for x in (
        ' '.join(y for y in (firm.get('address1'), firm.get('address2')) if y),
        ' · '.join(y for y in (
            ('Phone: %s' % firm['phone']) if firm.get('phone') else '',
            ('Mobile: %s' % firm['mobile']) if firm.get('mobile') else '',
            ('GSTIN: %s' % firm['gstin']) if firm.get('gstin') else '') if y))
        if x]

    R, B, BI = font(VERDANA), font(VERDANA_BOLD), font(VERDANA_BOLD_ITALIC)
    # <b> in a line of particulars only finds the bold face through the family.
    # Each face may stand for one style only -- naming the regular face as the
    # italic too made ReportLab read it as italic, and every <b> came out bold
    # italic -- so nothing here is set in italic.
    registerFontFamily(R, normal=R, bold=B, italic=BI, boldItalic=BI)
    cell = ParagraphStyle('cell', fontName=R, fontSize=9, leading=11.5,
                          textColor=INK)
    sub = ParagraphStyle('sub', parent=cell, fontSize=7.8, leading=10,
                         textColor=SOFT)

    # How far down the first page the heading reaches -- the same steps
    # `first_page` takes -- so the table starts just under it, however many
    # address lines the firm and the customer have.
    top_first = (16 * mm + 11 * len(firm_lines) + 9 + 17
                 + 12 * len(address[:3]) + (12 if contact else 0) + 16 + 14)

    def first_page(c, _doc):
        c.saveState()
        y = PAGE_H - 16 * mm
        c.setFillColor(INK)
        c.setFont(B, 17)
        c.drawCentredString(PAGE_W / 2, y, firm_name)
        c.setFont(R, 8.5)
        c.setFillColor(SOFT)
        for line in firm_lines:
            y -= 11
            c.drawCentredString(PAGE_W / 2, y, line)
        y -= 9
        c.setStrokeColor(RULE)
        c.setLineWidth(0.8)
        c.line(SIDE, y, PAGE_W - SIDE, y)
        y -= 17
        c.setFillColor(INK)
        c.setFont(B, 12.5)
        c.drawString(SIDE, y, 'Ledger of %s' % name)
        c.setFont(R, 9)
        c.setFillColor(SOFT)
        c.drawRightString(PAGE_W - SIDE, y, 'Account statement')
        for line in address[:3]:
            y -= 12
            c.setFillColor(INK)
            c.drawString(SIDE, y, line[:110])
        if contact:
            y -= 12
            c.drawString(SIDE, y, contact)
        y -= 16
        c.setFont(B, 10)
        c.setFillColor(INK)
        c.drawString(SIDE, y, period.upper())
        c.setFont(R, 8)
        c.setFillColor(SOFT)
        c.drawString(SIDE, BOTTOM - 8 * mm,
                     'Printed on %s' % datetime.date.today().strftime('%d/%m/%Y'))
        c.restoreState()

    def later_page(c, _doc):
        c.saveState()
        y = PAGE_H - 14 * mm
        c.setFont(B, 10)
        c.setFillColor(INK)
        c.drawString(SIDE, y, '%s — Ledger of %s' % (firm_name, name))
        c.setFont(R, 8.5)
        c.setFillColor(SOFT)
        c.drawString(SIDE, y - 12, period.upper() + ' (continued)')
        c.setFont(R, 8)
        c.drawString(SIDE, BOTTOM - 8 * mm,
                     'Printed on %s' % datetime.date.today().strftime('%d/%m/%Y'))
        c.restoreState()

    width = PAGE_W - 2 * SIDE
    cols = [25 * mm, width - 25 * mm - 2 * 34 * mm, 34 * mm, 34 * mm]
    rows = [['Date', 'Particulars', 'Credit', 'Debit']]
    style = [
        ('FONT', (0, 0), (-1, 0), B, 8.8),
        ('TEXTCOLOR', (0, 0), (-1, 0), INK),
        ('LINEABOVE', (0, 0), (-1, 0), 0.9, INK),
        ('LINEBELOW', (0, 0), (-1, 0), 0.9, INK),
        ('ALIGN', (2, 0), (3, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('FONT', (0, 1), (-1, -1), R, 9),
        ('TEXTCOLOR', (0, 1), (-1, -1), INK),
        ('TOPPADDING', (0, 0), (-1, -1), 3.2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3.2),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]

    opening = rep.get('opening')
    accounted = opening is not None
    op = float(opening or 0)
    rows.append([_d(frm) if frm else '',
                 Paragraph('Opening Balance', cell),
                 _money(op) if accounted and op < 0 else '',
                 _money(op) if accounted and op >= 0 else ''])

    # Oldest first, in the order the report already reads them.
    entries = rep.get('entries') or []
    for e in entries:
        head, said = _particulars(e)
        text = '<b>%s</b>' % _x(head)
        for line in said:
            text += '<br/><font size="7.8" color="#5d6b7a">%s</font>' % _x(line)
        rows.append([_d(e.get('date')), Paragraph(text, cell),
                     _money(e['credit']) if e.get('credit') else '',
                     _money(e['debit']) if e.get('debit') else ''])
    if not entries:
        rows.append(['', Paragraph('No entry between these dates.', sub),
                     '', ''])

    body_end = len(rows) - 1
    style.append(('LINEBELOW', (0, body_end), (-1, body_end), 0.9, INK))
    tot_cr = float(rep.get('total_credit') or 0) + (-op if accounted and op < 0 else 0)
    tot_dr = float(rep.get('total_debit') or 0) + (op if accounted and op >= 0 else 0)
    rows.append(['', '', fmt(tot_cr), fmt(tot_dr)])
    t = len(rows) - 1
    style.append(('FONT', (2, t), (3, t), B, 9))
    closing = rep.get('closing')
    if closing is not None:
        close = float(closing)
        rows.append(['', 'Closing %s Balance' % ('Debit' if close >= 0 else 'Credit'),
                     _money(close) if close >= 0 else '',
                     _money(close) if close < 0 else ''])
        c_row = len(rows) - 1
        style.append(('FONT', (1, c_row), (-1, c_row), B, 9))
        grand_cr = tot_cr + (close if close >= 0 else 0)
        grand_dr = tot_dr + (-close if close < 0 else 0)
        rows.append(['', '', fmt(grand_cr), fmt(grand_dr)])
        g = len(rows) - 1
        style += [('FONT', (2, g), (3, g), B, 9.5),
                  ('LINEABOVE', (2, g), (3, g), 0.9, INK),
                  ('LINEBELOW', (2, g), (3, g), 1.4, INK),
                  ('BACKGROUND', (0, c_row), (-1, c_row), SHADE)]

    # The totals, the closing and the grand totals are one block, kept with
    # the last entry: split across a page, the last page carried the grand
    # totals alone.
    style.append(('NOSPLIT', (0, body_end), (-1, len(rows) - 1)))
    table = Table(rows, colWidths=cols, repeatRows=1)
    table.setStyle(TableStyle(style))

    story = [NextPageTemplate('later'), table, Spacer(1, 9)]
    if closing is not None:
        close = float(closing)
        story.append(Paragraph(
            'Balance as on %s: <b>%s %s</b>' % (
                _d(to), _money(close), 'Dr' if close >= 0 else 'Cr')
            + ('' if close else ' (settled)'),
            ParagraphStyle('bal', parent=cell, fontName=R, fontSize=10,
                           leading=13)))
    else:
        story.append(Paragraph(
            'There is no account for this party in %s, where the money is '
            'written, so no balance is struck.' % _x(rep.get('money_book_name') or ''),
            sub))

    out = io.BytesIO()
    doc = BaseDocTemplate(
        out, pagesize=A4, leftMargin=SIDE, rightMargin=SIDE,
        topMargin=TOP_LATER, bottomMargin=BOTTOM,
        title='Ledger of %s, %s' % (name, period), author=firm_name,
        subject='Account statement')
    doc.addPageTemplates([
        PageTemplate('first', [Frame(SIDE, BOTTOM, width,
                                     PAGE_H - top_first - BOTTOM, id='f',
                                     leftPadding=0, rightPadding=0,
                                     topPadding=0, bottomPadding=0)],
                     onPage=first_page),
        PageTemplate('later', [Frame(SIDE, BOTTOM, width,
                                     PAGE_H - TOP_LATER - BOTTOM, id='l',
                                     leftPadding=0, rightPadding=0,
                                     topPadding=0, bottomPadding=0)],
                     onPage=later_page)])
    doc.build(story, canvasmaker=_Numbered)
    return out.getvalue()
