"""GSTR-1, as the GST portal's offline tool takes it.

Asked for on 19 Sep 2026, with the workbook the return is filed from
(`sample_gst.xlsx`, BST's August 2026): the portal's GSTR-1 Excel template,
twenty-two sheets, every one headed the same way -- a title on row 1, the
sheet's totals labelled on row 2 and given on row 3, the column headings on
row 4, and the documents from row 5. The offline tool reads it by sheet name
and heading, so the sheets, headings, merged cells and column widths below
are lifted from that workbook as they stand, and only rows 3 and 5 onward are
written here.

What goes where, read off how these books are kept:

  * An invoice is a bill of the month with a total above nought. SHREE files
    some of the GT run with its "C" bill type, which the importer took for a
    credit note; the IRP has every one of them as an invoice (GT134 of
    2026-27: "Typ": "INV"), so from SHREE they are invoices here too.
  * A credit note is a return: SHREE writes it as a bill of the R series with
    a negative total (e-invoiced as CRN), and a credit note made in this app
    is a CREDIT document.
  * A debit note here is a purchase return, which the supplier reports as a
    credit note; it is not an outward supply and is left out, and said so.
  * Registered buyers go to b2b and cdnr, one row per invoice per rate.
    Unregistered buyers go to b2cl (inter-state, above one lakh) or are added
    up by place of supply and rate in b2cs.
  * The taxable value is the bill's own -- the IRP's AssVal to the paisa --
    and HSN is added up from the lines, net of credit notes, split between
    registered (hsn(b2b)) and unregistered (hsn(b2c)) buyers as the portal
    has asked since 2025.

Nothing here needs a library: the workbook is written as the zip of XML it
is, so a Windows PC with only Python can make it.
"""
import datetime
import io
import re
import zipfile
from collections import OrderedDict
from xml.sax.saxutils import escape

import api
import db

LAYOUT = [('Help Instruction', [[(1, 'Help Instructions', '')], [], [], []], [], {}, None),
 ('b2b,sez,de',
  [[(1, 'Summary For B2B,SEZ,DE(4A,4B,6B,6C)', 'title')],
   [(1, 'No. of Recipients', 'label'), (2, None, 'title'), (3, 'No. of Invoices', 'label'),
    (4, None, 'label'), (5, 'Total Invoice Value', 'label'), (6, None, 'title'),
    (7, None, 'label'), (8, None, 'title'), (9, None, 'title'), (10, None, 'title'),
    (11, None, 'title'), (12, 'Total Taxable Value', 'label'), (13, 'Total Cess', 'label')],
   [],
   [(1, 'GSTIN/UIN of Recipient', 'head'), (2, 'Receiver Name', 'head'),
    (3, 'Invoice Number', 'head'), (4, 'Invoice date', 'head'), (5, 'Invoice Value', 'head'),
    (6, 'Place Of Supply', 'head'), (7, 'Reverse Charge', 'head'),
    (8, 'Applicable % of Tax Rate', 'head'), (9, 'Invoice Type', 'head'),
    (10, 'E-Commerce GSTIN', 'head'), (11, 'Rate', 'head'), (12, 'Taxable Value', 'head'),
    (13, 'Cess Amount', 'head')]],
  [],
  {'A': 36.29,
   'B': 23.86,
   'C': 15.57,
   'D': 12.43,
   'E': 18.57,
   'F': 15.29,
   'G': 15.29,
   'H': 23.43,
   'I': 12.57,
   'J': 18.71,
   'K': 9.14,
   'L': 19.0,
   'M': 13.14},
  'A5'),
 ('b2ba',
  [[(1, 'Summary For B2BA', 'title'), (2, 'Original Details', 'head'),
    (5, 'Revised details', 'rev')],
   [(1, 'No. of Recipients', 'label'), (2, None, 'title'), (3, 'No. of Invoices', 'label'),
    (4, None, 'label'), (5, None, 'title'), (6, None, 'label'),
    (7, 'Total Invoice Value', 'label'), (8, None, 'title'), (9, None, 'label'),
    (10, None, 'title'), (11, None, 'title'), (12, None, 'title'), (13, None, 'title'),
    (14, 'Total Taxable Value', 'label'), (15, 'Total Cess', 'label')],
   [],
   [(1, 'GSTIN/UIN of Recipient', 'head'), (2, 'Receiver Name', 'head'),
    (3, 'Original Invoice Number', 'head'), (4, 'Original Invoice date', 'head'),
    (5, 'Revised Invoice Number', 'rev'), (6, 'Revised Invoice date', 'rev'),
    (7, 'Invoice Value', 'rev'), (8, 'Place Of Supply', 'rev'), (9, 'Reverse Charge', 'rev'),
    (10, 'Applicable % of Tax Rate', 'rev'), (11, 'Invoice Type', 'rev'),
    (12, 'E-Commerce GSTIN', 'rev'), (13, 'Rate', 'rev'), (14, 'Taxable Value', 'rev'),
    (15, 'Cess Amount', 'rev')]],
  ['E1:O1', 'B1:D1'],
  {'A': 22.43,
   'B': 14.86,
   'C': 23.29,
   'D': 19.86,
   'E': 23.14,
   'F': 19.86,
   'G': 18.57,
   'H': 15.29,
   'I': 15.29,
   'J': 23.43,
   'K': 12.57,
   'L': 18.71,
   'M': 9.14,
   'N': 19.0,
   'O': 13.14},
  'A5'),
 ('b2cl',
  [[(1, 'Summary For B2CL(5)', 'title')],
   [(1, 'No. of Invoices', 'label'), (2, None, 'label'), (3, 'Total Invoice Value', 'label'),
    (4, None, 'title'), (5, None, 'title'), (6, None, 'title'),
    (7, 'Total Taxable Value', 'label'), (8, 'Total Cess', 'label'), (9, None, 'title'),
    (10, None, 'title')],
   [],
   [(1, 'Invoice Number', 'head'), (2, 'Invoice date', 'head'), (3, 'Invoice Value', 'head'),
    (4, 'Place Of Supply', 'head'), (5, 'Applicable % of Tax Rate', 'head'),
    (6, 'Rate', 'head'), (7, 'Taxable Value', 'head'), (8, 'Cess Amount', 'head'),
    (9, 'E-Commerce GSTIN', 'head'), (10, 'Sale from Bonded WH', 'head')]],
  [],
  {'A': 20.57,
   'B': 12.43,
   'C': 18.57,
   'D': 15.29,
   'E': 23.43,
   'F': 9.14,
   'G': 19.0,
   'H': 13.14,
   'I': 18.71,
   'J': 21.14},
  'A5'),
 ('b2cla',
  [[(1, 'Summary For B2CLA', 'title'), (2, 'Original Details', 'head'),
    (4, 'Revised details', 'rev')],
   [(1, 'No. of Invoices', 'label'), (2, None, 'label'), (3, None, 'title'), (4, None, 'title'),
    (5, None, 'label'), (6, 'Total Invoice Value', 'label'), (7, None, 'title'),
    (8, None, 'title'), (9, 'Total Taxable Value', 'label'), (10, 'Total Cess', 'label'),
    (11, None, 'title'), (12, None, 'title')],
   [],
   [(1, 'Original Invoice Number', 'head'), (2, 'Original Invoice date', 'head'),
    (3, 'Original Place Of Supply', 'head'), (4, 'Revised Invoice Number', 'rev'),
    (5, 'Revised Invoice date', 'rev'), (6, 'Invoice Value', 'rev'),
    (7, 'Applicable % of Tax Rate', 'rev'), (8, 'Rate', 'rev'), (9, 'Taxable Value', 'rev'),
    (10, 'Cess Amount', 'rev'), (11, 'E-Commerce GSTIN', 'rev'),
    (12, 'Sale from Bonded WH', 'rev')]],
  ['B1:C1', 'D1:L1'],
  {'A': 23.29,
   'B': 19.86,
   'C': 22.86,
   'D': 23.14,
   'E': 19.86,
   'F': 18.57,
   'G': 23.43,
   'H': 9.14,
   'I': 19.0,
   'J': 13.14,
   'K': 18.71,
   'L': 21.14},
  'A5'),
 ('b2cs',
  [[(1, 'Summary For B2CS(7)', 'title')],
   [(1, None, 'title'), (2, None, 'title'), (3, None, 'title'), (4, None, 'title'),
    (5, 'Total Taxable Value', 'label'), (6, 'Total Cess', 'label'), (7, None, 'title')],
   [],
   [(1, 'Type', 'head'), (2, 'Place Of Supply', 'head'),
    (3, 'Applicable % of Tax Rate', 'head'), (4, 'Rate', 'head'), (5, 'Taxable Value', 'head'),
    (6, 'Cess Amount', 'head'), (7, 'E-Commerce GSTIN', 'head')]],
  [], {'A': 20.71, 'B': 15.29, 'C': 23.43, 'D': 9.14, 'E': 19.0, 'F': 13.14, 'G': 18.71},
  'A5'),
 ('b2csa',
  [[(1, 'Summary For B2CSA', 'title'), (2, 'Original Details', 'head'),
    (3, 'Revised details', 'rev')],
   [(1, None, 'title'), (2, None, 'title'), (3, None, 'title'), (4, None, 'title'),
    (5, None, 'title'), (6, None, 'title'), (7, 'Total Taxable Value', 'label'),
    (8, 'Total Cess', 'label'), (9, None, 'title')],
   [],
   [(1, 'Financial Year', 'head'), (2, 'Original Month', 'head'), (3, 'Place Of Supply', 'rev'),
    (4, 'Type', 'rev'), (5, 'Applicable % of Tax Rate', 'rev'), (6, 'Rate', 'rev'),
    (7, 'Taxable Value', 'rev'), (8, 'Cess Amount', 'rev'), (9, 'E-Commerce GSTIN', 'rev')]],
  ['C1:I1'],
  {'A': 19.57,
   'B': 15.14,
   'C': 15.29,
   'D': 9.14,
   'E': 23.43,
   'F': 9.14,
   'G': 19.0,
   'H': 13.14,
   'I': 18.71},
  'A5'),
 ('cdnr',
  [[(1, 'Summary For CDNR(9B)', 'title')],
   [(1, 'No. of Recipient', 'label'), (2, None, 'title'), (3, 'No. of Notes', 'label'),
    (4, None, 'label'), (5, None, 'label'), (6, None, 'title'), (7, None, 'label'),
    (8, None, 'title'), (9, 'Total Note Value', 'label'), (10, None, 'title'),
    (11, None, 'title'), (12, 'Total Taxable Value', 'label'), (13, 'Total Cess', 'label')],
   [],
   [(1, 'GSTIN/UIN of Recipient', 'head'), (2, 'Receiver Name', 'head'),
    (3, 'Note Number', 'head'), (4, 'Note Date', 'head'), (5, 'Note Type', 'head'),
    (6, 'Place Of Supply', 'head'), (7, 'Reverse Charge', 'head'),
    (8, 'Note Supply Type', 'head'), (9, 'Note Value', 'head'),
    (10, 'Applicable % of Tax Rate', 'head'), (11, 'Rate', 'head'),
    (12, 'Taxable Value', 'head'), (13, 'Cess Amount', 'head')]],
  [],
  {'A': 22.57,
   'B': 14.86,
   'C': 13.57,
   'D': 10.57,
   'E': 10.57,
   'F': 15.29,
   'G': 15.29,
   'H': 17.14,
   'I': 16.43,
   'J': 23.43,
   'K': 9.14,
   'L': 19.0,
   'M': 13.14},
  'A5'),
 ('cdnra',
  [[(1, 'Summary For CDNRA', 'title'), (2, 'Original Details', 'head'),
    (7, 'Revised details', 'rev')],
   [(1, 'No. of Recipient', 'label'), (2, None, 'title'), (3, 'No. of Notes/Vouchers', 'label'),
    (4, None, 'label'), (5, None, 'title'), (6, None, 'label'), (7, None, 'label'),
    (8, None, 'title'), (9, None, 'label'), (10, None, 'title'),
    (11, 'Total Note Value', 'label'), (12, None, 'title'), (13, None, 'title'),
    (14, 'Total Taxable Value', 'label'), (15, 'Total Cess', 'label')],
   [],
   [(1, 'GSTIN/UIN of Recipient', 'head'), (2, 'Receiver Name', 'head'),
    (3, 'Original Note Number', 'head'), (4, 'Original Note Date', 'head'),
    (5, 'Revised Note Number', 'head'), (6, 'Revised Note Date', 'head'),
    (7, 'Note Type', 'rev'), (8, 'Place Of Supply', 'rev'), (9, 'Reverse Charge', 'rev'),
    (10, 'Note Supply Type', 'rev'), (11, 'Note Value', 'rev'),
    (12, 'Applicable % of Tax Rate', 'rev'), (13, 'Rate', 'rev'), (14, 'Taxable Value', 'rev'),
    (15, 'Cess Amount', 'rev')]],
  ['G1:O1', 'B1:F1'],
  {'A': 22.43,
   'B': 14.86,
   'C': 22.0,
   'D': 18.14,
   'E': 21.14,
   'F': 18.0,
   'G': 10.57,
   'H': 15.29,
   'I': 15.29,
   'J': 17.14,
   'K': 16.43,
   'L': 23.43,
   'M': 9.14,
   'N': 19.0,
   'O': 13.14},
  'A5'),
 ('cdnur',
  [[(1, 'Summary For CDNUR(9B)', 'title')],
   [(1, None, 'title'), (2, 'No. of Notes/Vouchers', 'label'), (3, None, 'label'),
    (4, None, 'label'), (5, None, 'title'), (6, 'Total Note Value', 'label'),
    (7, None, 'title'), (8, None, 'title'), (9, 'Total Taxable Value', 'label'),
    (10, 'Total Cess', 'label')],
   [],
   [(1, 'UR Type', 'head'), (2, 'Note Number', 'head'), (3, 'Note Date', 'head'),
    (4, 'Note Type', 'head'), (5, 'Place Of Supply', 'head'), (6, 'Note Value', 'head'),
    (7, 'Applicable % of Tax Rate', 'head'), (8, 'Rate', 'head'), (9, 'Taxable Value', 'head'),
    (10, 'Cess Amount', 'head')]],
  [],
  {'A': 24.0,
   'B': 22.0,
   'C': 10.57,
   'D': 10.57,
   'E': 15.29,
   'F': 16.43,
   'G': 23.43,
   'H': 9.14,
   'I': 19.0,
   'J': 13.14},
  'A5'),
 ('cdnura',
  [[(1, 'Summary For CDNURA', 'title'), (2, 'Original Details', 'head'),
    (6, 'Revised details', 'rev')],
   [(1, None, 'title'), (2, 'No. of Notes/Vouchers', 'label'), (3, None, 'label'),
    (4, None, 'title'), (5, None, 'label'), (6, None, 'label'), (7, None, 'title'),
    (8, 'Total Note Value', 'label'), (9, None, 'title'), (10, None, 'title'),
    (11, 'Total Taxable Value', 'label'), (12, 'Total Cess', 'label')],
   [],
   [(1, 'UR Type', 'head'), (2, 'Original Note Number', 'head'),
    (3, 'Original Note Date', 'head'), (4, 'Revised Note Number', 'head'),
    (5, 'Revised Note Date', 'head'), (6, 'Note Type', 'rev'), (7, 'Place Of Supply', 'rev'),
    (8, 'Note Value', 'rev'), (9, 'Applicable % of Tax Rate', 'rev'), (10, 'Rate', 'rev'),
    (11, 'Taxable Value', 'rev'), (12, 'Cess Amount', 'rev')]],
  ['F1:L1', 'B1:E1'],
  {'A': 21.71,
   'B': 22.0,
   'C': 18.14,
   'D': 21.14,
   'E': 18.0,
   'F': 10.57,
   'G': 15.29,
   'H': 16.43,
   'I': 23.43,
   'J': 9.14,
   'K': 19.0,
   'L': 13.14},
  'A5'),
 ('exp',
  [[(1, 'Summary For EXP(6)', 'title')],
   [(1, None, 'title'), (2, 'No. of Invoices', 'label'), (3, None, 'label'),
    (4, 'Total Invoice Value', 'label'), (5, None, 'title'),
    (6, 'No. of Shipping Bill', 'label'), (7, None, 'title'), (8, None, 'title'),
    (9, 'Total Taxable Value', 'label'), (10, 'Total Cess', 'label')],
   [],
   [(1, 'Export Type', 'head'), (2, 'Invoice Number', 'head'), (3, 'Invoice date', 'head'),
    (4, 'Invoice Value', 'head'), (5, 'Port Code', 'head'), (6, 'Shipping Bill Number', 'head'),
    (7, 'Shipping Bill Date', 'head'), (8, 'Rate', 'head'), (9, 'Taxable Value', 'head'),
    (10, 'Cess Amount', 'head')]],
  [],
  {'A': 19.57,
   'B': 15.57,
   'C': 12.43,
   'D': 18.57,
   'E': 10.29,
   'F': 20.29,
   'G': 17.14,
   'H': 9.14,
   'I': 19.0,
   'J': 13.14},
  'A5'),
 ('expa',
  [[(1, 'Summary For EXPA', 'title'), (2, 'Original Details', 'head'),
    (4, 'Revised details', 'rev')],
   [(1, None, 'title'), (2, 'No. of Invoices', 'label'), (3, None, 'label'), (4, None, 'title'),
    (5, None, 'label'), (6, 'Total Invoice Value', 'label'), (7, None, 'title'),
    (8, 'No. of Shipping Bill', 'label'), (9, None, 'title'), (10, None, 'title'),
    (11, 'Total Taxable Value', 'label'), (12, 'Total Cess', 'label')],
   [],
   [(1, 'Export Type', 'head'), (2, 'Original Invoice Number', 'head'),
    (3, 'Original Invoice date', 'head'), (4, 'Revised Invoice Number', 'rev'),
    (5, 'Revised Invoice date', 'rev'), (6, 'Invoice Value', 'rev'), (7, 'Port Code', 'rev'),
    (8, 'Shipping Bill Number', 'rev'), (9, 'Shipping Bill Date', 'rev'), (10, 'Rate', 'rev'),
    (11, 'Taxable Value', 'rev'), (12, 'Cess Amount', 'rev')]],
  ['B1:C1', 'D1:L1'],
  {'A': 18.57,
   'B': 23.29,
   'C': 19.86,
   'D': 23.14,
   'E': 19.86,
   'F': 18.57,
   'G': 10.29,
   'H': 20.29,
   'I': 17.14,
   'J': 9.14,
   'K': 19.0,
   'L': 13.14},
  'A5'),
 ('at',
  [[(1, 'Summary For Advance Received (11B)', 'title')],
   [(1, None, 'title'), (2, None, 'title'), (3, None, 'title'),
    (4, 'Total Advanced Received', 'label'), (5, 'Total Cess', 'label')],
   [],
   [(1, 'Place Of Supply', 'head'), (2, 'Applicable % of Tax Rate', 'head'),
    (3, 'Rate', 'head'), (4, 'Gross Advance Received', 'head'), (5, 'Cess Amount', 'head')]],
  [], {'A': 35.43, 'B': 23.43, 'C': 9.14, 'D': 23.86, 'E': 13.14}, 'A5'),
 ('ata',
  [[(1, 'Summary For Amended Tax Liability(Advance Received)', 'title'),
    (2, 'Original Details', 'head'), (4, 'Revised details', 'rev')],
   [(1, None, 'title'), (2, None, 'title'), (3, None, 'title'), (4, None, 'title'),
    (5, None, 'title'), (6, 'Total Advanced Received', 'label'), (7, 'Total Cess', 'label')],
   [],
   [(1, 'Financial Year', 'head'), (2, 'Original Month', 'head'),
    (3, 'Original Place Of Supply', 'head'), (4, 'Applicable % of Tax Rate', 'rev'),
    (5, 'Rate', 'rev'), (6, 'Gross Advance Received', 'rev'), (7, 'Cess Amount', 'rev')]],
  ['B1:C1', 'D1:G1'],
  {'A': 51.29, 'B': 15.14, 'C': 22.86, 'D': 23.43, 'E': 9.14, 'F': 23.86, 'G': 13.14}, 'A5'),
 ('atadj',
  [[(1, 'Summary For Advance Adjusted (11B)', 'title')],
   [(1, None, 'title'), (2, None, 'title'), (3, None, 'title'),
    (4, 'Total Advanced Adjusted', 'label'), (5, 'Total Cess', 'label')],
   [],
   [(1, 'Place Of Supply', 'head'), (2, 'Applicable % of Tax Rate', 'head'),
    (3, 'Rate', 'head'), (4, 'Gross Advance Adjusted', 'head'), (5, 'Cess Amount', 'head')]],
  [], {'A': 35.29, 'B': 23.43, 'C': 9.14, 'D': 23.86, 'E': 13.14}, 'A5'),
 ('atadja',
  [[(1, 'Summary For Amendement Of Adjustment Advances', 'title'),
    (2, 'Original Details', 'head'), (4, 'Revised details', 'rev')],
   [(1, None, 'title'), (2, None, 'title'), (3, None, 'title'), (4, None, 'title'),
    (5, None, 'title'), (6, 'Total Advanced Adjusted', 'label'), (7, 'Total Cess', 'label')],
   [],
   [(1, 'Financial Year', 'head'), (2, 'Original Month', 'head'),
    (3, 'Original Place Of Supply', 'head'), (4, 'Applicable % of Tax Rate', 'rev'),
    (5, 'Rate', 'rev'), (6, 'Gross Advance Adjusted', 'rev'), (7, 'Cess Amount', 'rev')]],
  ['B1:C1', 'D1:G1'],
  {'A': 48.86, 'B': 15.14, 'C': 22.86, 'D': 23.43, 'E': 9.14, 'F': 23.86, 'G': 13.14}, 'A5'),
 ('exemp',
  [[(1, 'Summary For Nil rated, exempted and non GST outward supplies (8)', 'title')],
   [(1, None, 'title'), (2, 'Total Nil Rated Supplies', 'label'),
    (3, 'Total Exempted Supplies', 'label'), (4, 'Total Non-GST Supplies', 'label')],
   [],
   [(1, 'Description', 'head'), (2, 'Nil Rated Supplies', 'head'),
    (3, 'Exempted(other than nil rated/non GST supply)', 'head'),
    (4, 'Non-GST supplies', 'head')]],
  [], {'A': 62.43, 'B': 22.71, 'C': 44.14, 'D': 22.43}, 'A5'),
 ('hsn(b2b)',
  [[(1, 'Summary For HSN(12)', 'title')],
   [(1, 'No. of HSN', 'label'), (2, None, 'title'), (3, None, 'title'), (4, None, 'title'),
    (5, 'Total Value', 'label'), (6, None, 'title'), (7, 'Total Taxable Value', 'label'),
    (8, 'Total Integrated Tax', 'label'), (9, 'Total Central Tax', 'label'),
    (10, 'Total State/UT Tax', 'label'), (11, 'Total Cess', 'label')],
   [],
   [(1, 'HSN', 'head'), (2, 'Description', 'head'), (3, 'UQC', 'head'),
    (4, 'Total Quantity', 'head'), (5, 'Total Value', 'head'), (6, 'Rate', 'head'),
    (7, 'Taxable Value', 'head'), (8, 'Integrated Tax Amount', 'head'),
    (9, 'Central Tax Amount', 'head'), (10, 'State/UT Tax Amount', 'head'),
    (11, 'Cess Amount', 'head')]],
  [],
  {'A': 21.86,
   'B': 18.14,
   'C': 15.71,
   'D': 24.57,
   'E': 15.57,
   'F': 13.43,
   'G': 14.57,
   'H': 12.14,
   'I': 20.71,
   'J': 16.57,
   'K': 14.14},
  None),
 ('hsn(b2c)',
  [[(1, 'Summary For HSN(12)', 'title')],
   [(1, 'No. of HSN', 'label'), (2, None, 'title'), (3, None, 'title'), (4, None, 'title'),
    (5, 'Total Value', 'label'), (6, None, 'title'), (7, 'Total Taxable Value', 'label'),
    (8, 'Total Integrated Tax', 'label'), (9, 'Total Central Tax', 'label'),
    (10, 'Total State/UT Tax', 'label'), (11, 'Total Cess', 'label')],
   [],
   [(1, 'HSN', 'head'), (2, 'Description', 'head'), (3, 'UQC', 'head'),
    (4, 'Total Quantity', 'head'), (5, 'Total Value', 'head'), (6, 'Rate', 'head'),
    (7, 'Taxable Value', 'head'), (8, 'Integrated Tax Amount', 'head'),
    (9, 'Central Tax Amount', 'head'), (10, 'State/UT Tax Amount', 'head'),
    (11, 'Cess Amount', 'head')]],
  [],
  {'A': 21.14,
   'B': 18.86,
   'C': 21.29,
   'D': 14.43,
   'E': 11.86,
   'F': 9.14,
   'G': 19.0,
   'H': 22.14,
   'I': 19.29,
   'J': 20.86,
   'K': 13.14},
  'A5'),
 ('docs',
  [[(1, 'Summary of documents issued during the tax period(13)', 'title')],
   [(1, None, 'title'), (2, None, 'title'), (3, None, 'title'), (4, 'Total Number', 'label'),
    (5, 'Total Cancelled', 'label')],
   [],
   [(1, 'Nature of Document', 'head'), (2, 'Sr. No. From', 'head'), (3, 'Sr. No. To', 'head'),
    (4, 'Total Number', 'head'), (5, 'Cancelled', 'head')]],
  [], {'A': 51.86, 'B': 12.57, 'C': 10.14, 'D': 13.86, 'E': 15.14}, 'A5'),
 ('eco',
  [[(1, 'Summary For Supplies through ECO-14', 'title')],
   [(1, None, 'title'), (2, 'No. of E-Commerce Operator', 'label'), (3, None, 'title'),
    (4, 'Total Net Value of Supplies', 'label'), (5, 'Total Integrated Tax', 'label'),
    (6, 'Total Central Tax', 'label'), (7, 'Total State/UT Tax', 'label'),
    (8, 'Total Cess', 'label')],
   [],
   [(1, 'Nature of Supply', 'head'), (2, 'GSTIN of E-Commerce Operator', 'head'),
    (3, 'E-Commerce Operator Name', 'head'), (4, 'Net value of supplies', 'head'),
    (5, 'Integrated tax', 'head'), (6, 'Central tax', 'head'), (7, 'State/UT tax', 'head'),
    (8, 'Cess', 'head')]],
  [],
  {'A': 36.0,
   'B': 29.71,
   'C': 27.29,
   'D': 25.57,
   'E': 19.43,
   'F': 16.57,
   'G': 18.14,
   'H': 10.43},
  'A5')]

# Place of supply, spelt as the portal's own list spells it.
POS_NAME = {
    '01': 'Jammu & Kashmir', '02': 'Himachal Pradesh', '03': 'Punjab',
    '04': 'Chandigarh', '05': 'Uttarakhand', '06': 'Haryana', '07': 'Delhi',
    '08': 'Rajasthan', '09': 'Uttar Pradesh', '10': 'Bihar', '11': 'Sikkim',
    '12': 'Arunachal Pradesh', '13': 'Nagaland', '14': 'Manipur',
    '15': 'Mizoram', '16': 'Tripura', '17': 'Meghalaya', '18': 'Assam',
    '19': 'West Bengal', '20': 'Jharkhand', '21': 'Odisha',
    '22': 'Chhattisgarh', '23': 'Madhya Pradesh', '24': 'Gujarat',
    '26': 'Dadra & Nagar Haveli & Daman & Diu', '27': 'Maharashtra',
    '29': 'Karnataka', '30': 'Goa', '31': 'Lakshdweep', '32': 'Kerala',
    '33': 'Tamil Nadu', '34': 'Puducherry',
    '35': 'Andaman & Nicobar Islands', '36': 'Telangana',
    '37': 'Andhra Pradesh', '38': 'Ladakh', '97': 'Other Territory'}

# The unit a quantity is counted in, as the portal's UQC list names it.
UQC = {'KGS': 'KGS-KILOGRAMS', 'KG': 'KGS-KILOGRAMS', 'NOS': 'NOS-NUMBERS',
       'NO': 'NOS-NUMBERS', 'PCS': 'PCS-PIECES', 'PC': 'PCS-PIECES',
       'UNT': 'UNT-UNITS', 'UNIT': 'UNT-UNITS', 'MTR': 'MTR-METERS',
       'MT': 'MTS-METRIC TON', 'MTS': 'MTS-METRIC TON', 'TON': 'TON-TONNES',
       'TONNE': 'TON-TONNES', 'QTL': 'QTL-QUINTAL', 'LTR': 'LTR-LITRES',
       'BOX': 'BOX-BOX', 'SET': 'SET-SETS', 'BAG': 'BAG-BAGS',
       'PAC': 'PAC-PACKS', 'ROL': 'ROL-ROLLS', 'SQM': 'SQM-SQUARE METERS',
       'SQF': 'SQF-SQUARE FEET', 'DOZ': 'DOZ-DOZENS', 'PRS': 'PRS-PAIRS',
       'CTN': 'CTN-CARTONS', 'BDL': 'BDL-BUNDLES', 'GMS': 'GMS-GRAMMES'}

MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep',
          'Oct', 'Nov', 'Dec')
B2CL_LIMIT = 100000.0       # an inter-state bill to the unregistered, above this
GSTIN_RE = re.compile(r'^\d{2}[A-Z0-9]{10}[0-9A-Z]Z[0-9A-Z]$')
DOC_NO_RE = re.compile(r'^[A-Za-z0-9/-]{1,16}$')
STATE_CODE = {api.staff_key(n): c for c, n in api.STATES}


def r2(x):
    return api.r2(x)


def day(iso):
    """'2026-08-05' -> '05-Aug-2026', as the template writes a date. Month
    names spelt here, not by strftime, which follows the machine's locale."""
    y, m, d = iso.split('-')
    return '%s-%s-%s' % (d, MONTHS[int(m) - 1], y)


def receiver(name, width=99):
    """A name as SHREE writes it into the return: anything but letters and
    digits -- '&', '.', '/' -- turned into a space ("N.B.S." is "N B S")."""
    text = re.sub(r'[^A-Za-z0-9 ]', ' ', name or '')
    return re.sub(r'\s+', ' ', text).strip()[:width]


def doc_no(inv):
    return ((inv['series'] or '').strip() + (inv['invoice_no'] or '').strip())


def _state_of(inv, firm_code):
    """Where the supply is: the state the bill names, else the buyer's
    GSTIN, else the buyer's state, else the firm's own."""
    for name in (inv['supply_state'] or '',):
        code = STATE_CODE.get(api.staff_key(name)) or (
            name[:2] if re.match(r'^\d{2}\b', name) else '')
        if code:
            return code
    g = (inv['gstin'] or '').strip().upper()
    if GSTIN_RE.match(g):
        return g[:2]
    return (inv['party_state_code'] or '').strip() or \
        STATE_CODE.get(api.staff_key(inv['party_state'])) or firm_code


def _pos(code):
    return '%s-%s' % (code, POS_NAME.get(code, api.STATE_NAME.get(code, '')))


def _by_rate(inv, lines):
    """{rate: [taxable, cess]} for one document, made to add up to the
    document's own taxable value: a bill whose lines round to a paisa more
    or less than its header takes the difference on its largest rate."""
    out = OrderedDict()
    for ln in lines:
        rate = r2(ln['gst_rate'])
        t = out.setdefault(rate, [0.0, 0.0])
        t[0] += abs(ln['taxable'] + ln['charge_share'])
        t[1] += abs(ln['cess'])
    want = abs(inv['taxable'])
    if not out:
        out[_rate_guess(inv)] = [want, abs(inv['cess'])]
    have = sum(v[0] for v in out.values())
    if abs(have - want) >= 0.005:
        big = max(out, key=lambda k: out[k][0])
        out[big][0] += want - have
    for v in out.values():
        v[0], v[1] = r2(v[0]), r2(v[1])
    return out


def _rate_guess(inv):
    tax = abs(inv['cgst']) + abs(inv['sgst']) + abs(inv['igst'])
    base = abs(inv['taxable'])
    return r2(round(tax / base * 100, 1)) if base else 0.0


def build(conn, cid, month):
    """Everything the return holds for one GST firm and one month."""
    frm, to, _days = api.month_bounds(month)
    firm = api.one(conn.execute('SELECT * FROM company WHERE id=?', (cid,)))
    if not firm:
        raise api.ApiError('That firm is not in the books.', 404)
    firm_state = (firm['state_code'] or (firm['gstin'] or '')[:2] or '').strip()
    docs = api.rows(conn.execute(
        'SELECT i.*, p.name AS party_name, p.gstin, p.gst_type, '
        '  p.state AS party_state, p.state_code AS party_state_code '
        'FROM invoice i JOIN ledger p ON p.id=i.party_id '
        'WHERE i.company_id=? AND i.invoice_date>=? AND i.invoice_date<=? '
        "AND i.doc_type IN ('SALE','CREDIT','DEBIT') "
        'ORDER BY i.invoice_date, i.series, '
        '  CAST(i.invoice_no AS INTEGER), i.invoice_no', (cid, frm, to)))
    lines = {}
    ids = [d['id'] for d in docs]
    names = {}
    for chunk in api._chunks(ids, 400):
        for ln in api.rows(conn.execute(
                'SELECT ii.*, it.name AS item_name FROM invoice_item ii '
                'LEFT JOIN item it ON it.id=ii.item_id WHERE ii.invoice_id '
                'IN (%s) ORDER BY ii.id' % ','.join('?' * len(chunk)), chunk)):
            lines.setdefault(ln['invoice_id'], []).append(ln)
    for d in docs:
        for k in ('taxable', 'cgst', 'sgst', 'igst', 'cess', 'grand_total'):
            d[k] = float(d[k] or 0)
        for ln in lines.get(d['id'], []):
            for k in ('taxable', 'charge_share', 'cgst', 'sgst', 'igst',
                      'cess', 'qty', 'weight', 'gst_rate'):
                ln[k] = float(ln[k] or 0)

    sheets = {name: [] for name, *_ in LAYOUT}
    warn, left = [], []
    b2cs = OrderedDict()
    hsn = {'hsn(b2b)': OrderedDict(), 'hsn(b2c)': OrderedDict()}
    series = OrderedDict()       # (nature, prefix) -> [numbers, texts]
    exemp = {k: [0.0, 0.0, 0.0] for k in range(4)}
    tot = dict(b2b_count=0, b2b_value=0.0, b2b_taxable=0.0, b2c_count=0,
               b2c_value=0.0, b2c_taxable=0.0, cn_count=0, cn_value=0.0,
               cn_taxable=0.0, igst=0.0, cgst=0.0, sgst=0.0, cess=0.0)
    preview = dict(b2b=[], notes=[], b2c=[], hsn=[], docs=[])
    # Rate by rate, the way SHREE's GST page shows it ("GST% Wise" and
    # "Party Type + Gst %"), off the same pass that fills the sheets.
    rate_tot = {}
    # 3.2 of the 3B: what went inter-state to buyers with no GSTIN.
    b2c_inter = dict(taxable=0.0, igst=0.0, count=0)

    for d in docs:
        kind = d['doc_type']
        total = d['grand_total']
        no = doc_no(d)
        if kind == 'DEBIT':
            left.append([no, d['invoice_date'], d['party_name'], abs(total),
                         'a debit note here is a purchase return -- the '
                         'supplier reports it, as a credit note'])
            continue
        if abs(total) < 0.005:
            left.append([no, d['invoice_date'], d['party_name'], 0.0,
                         'nothing on it'])
            continue
        is_note = total < 0 or (kind == 'CREDIT' and d['source'] != 'SHREE')
        gstin = (d['gstin'] or '').strip().upper()
        registered = bool(GSTIN_RE.match(gstin))
        if gstin and not registered:
            warn.append('%s %s: the GSTIN "%s" is not a valid one, so it is '
                        'filed as a sale to an unregistered buyer.'
                        % (no, d['party_name'], gstin))
        elif not gstin:
            # These firms sell to the registered; a buyer without a GSTIN is
            # far more often a GSTIN never typed in than a consumer.
            warn.append('%s %s: no GSTIN on the account, so it is filed as a '
                        'sale to an unregistered buyer (B2C). If they are '
                        'registered, add the GSTIN under Parties and download '
                        'again.' % (no, d['party_name']))
        if not DOC_NO_RE.match(no):
            warn.append('%s: the portal takes a number of up to 16 letters, '
                        'digits, "/" and "-" only.' % no)
        pos = _state_of(d, firm_state)
        if pos not in POS_NAME:
            warn.append('%s: the place of supply (state %s) is not one the '
                        'portal knows.' % (no, pos or 'blank'))
        inter = pos != firm_state
        rates = _by_rate(d, lines.get(d['id'], []))
        if not lines.get(d['id']):
            warn.append('%s: the bill has no lines, so its HSN cannot be '
                        'reported.' % no)
        rcm = 'Y' if d['rcm'] else 'N'
        value = abs(total)
        sign = -1 if is_note else 1
        bucket = 'note' if is_note else ('b2b' if registered else 'b2c')
        # The document's own tax, shared over its rates by taxable value --
        # almost every bill carries one rate, where this is exact.
        share_base = sum(t for t, _c in rates.values())
        spent = dict(igst=0.0, cgst=0.0, sgst=0.0)
        for n, (rate, (taxable, _cess)) in enumerate(rates.items()):
            last = n == len(rates) - 1
            slot = rate_tot.setdefault(rate, dict(
                rate=rate, b2b=0.0, b2c=0.0, note=0.0, taxable=0.0,
                igst=0.0, cgst=0.0, sgst=0.0, cess=0.0, count=0))
            slot[bucket] += sign * taxable
            slot['taxable'] += sign * taxable
            slot['count'] += 1
            for k in ('igst', 'cgst', 'sgst'):
                whole = abs(d[k])
                part = r2(whole - spent[k]) if last else (
                    r2(whole * taxable / share_base) if share_base else 0.0)
                spent[k] += part
                slot[k] += sign * part
            slot['cess'] += sign * abs(_cess)
        if bucket == 'b2c' and inter:
            b2c_inter['taxable'] += sign * abs(d['taxable'])
            b2c_inter['igst'] += sign * abs(d['igst'])
            b2c_inter['count'] += 1
        nature = ('Credit Note' if is_note else 'Invoices for outward supply')
        m = re.match(r'^(.*?)(\d+)$', no)
        prefix, numeral = (m.group(1), int(m.group(2))) if m else (no, None)
        series.setdefault((nature, prefix), []).append((numeral, no))

        for rate, (taxable, cess) in rates.items():
            if not rate:
                slot = (0 if registered else 2) + (0 if inter else 1)
                exemp[slot][0] += sign * taxable
                continue
            if is_note and registered:
                sheets['cdnr'].append([
                    gstin, receiver(d['party_name']), no, day(d['invoice_date']),
                    'C', _pos(pos), rcm, 'Regular B2B', value, None, rate,
                    taxable, cess])
            elif is_note and inter and value > B2CL_LIMIT:
                sheets['cdnur'].append([
                    'B2CL', no, day(d['invoice_date']), 'C', _pos(pos), value,
                    None, rate, taxable, cess])
            elif is_note:
                t = b2cs.setdefault((pos, rate), [0.0, 0.0])
                t[0] -= taxable
                t[1] -= cess
            elif registered:
                typ = 'Regular B2B'
                if (d['gst_type'] or '') == 'SEZ':
                    typ = ('SEZ supplies with payment' if d['igst']
                           else 'SEZ supplies without payment')
                sheets['b2b,sez,de'].append([
                    gstin, receiver(d['party_name']), no, day(d['invoice_date']),
                    value, _pos(pos), rcm, None, typ, None, rate, taxable, cess])
            elif inter and value > B2CL_LIMIT:
                sheets['b2cl'].append([
                    no, day(d['invoice_date']), value, _pos(pos), None, rate,
                    taxable, cess, None, None])
            else:
                t = b2cs.setdefault((pos, rate), [0.0, 0.0])
                t[0] += taxable
                t[1] += cess

        # The bill itself, for the screen and the totals.
        tax = d['igst'] + d['cgst'] + d['sgst']
        row = dict(id=d['id'], no=no, date=d['invoice_date'],
                   party=d['party_name'], gstin=gstin if registered else '',
                   pos=_pos(pos), value=value, taxable=abs(d['taxable']),
                   igst=abs(d['igst']), cgst=abs(d['cgst']),
                   sgst=abs(d['sgst']), cess=abs(d['cess']),
                   rates=[r for r in rates], note=is_note, source=d['source'])
        if is_note:
            preview['notes'].append(row)
            tot['cn_count'] += 1
            tot['cn_value'] += value
            tot['cn_taxable'] += abs(d['taxable'])
        elif registered:
            preview['b2b'].append(row)
            tot['b2b_count'] += 1
            tot['b2b_value'] += value
            tot['b2b_taxable'] += abs(d['taxable'])
        else:
            preview['b2c'].append(row)
            tot['b2c_count'] += 1
            tot['b2c_value'] += value
            tot['b2c_taxable'] += abs(d['taxable'])
        for k in ('igst', 'cgst', 'sgst', 'cess'):
            tot[k] += sign * abs(d[k])
        if abs(tax) > 0.005 and not any(rates):
            warn.append('%s: tax is charged but no line carries a rate.' % no)

        # HSN, from the lines, net of credit notes.
        book = hsn['hsn(b2b)' if registered else 'hsn(b2c)']
        doc_lines = lines.get(d['id'], [])
        want = abs(d['taxable'])
        have = sum(abs(ln['taxable'] + ln['charge_share']) for ln in doc_lines)
        big = max(range(len(doc_lines)), key=lambda i: abs(
            doc_lines[i]['taxable'] + doc_lines[i]['charge_share'])) \
            if doc_lines else None
        for i, ln in enumerate(doc_lines):
            code = re.sub(r'\D', '', ln['hsn'] or '')
            if not code:
                warn.append('%s: "%s" has no HSN code.'
                            % (no, ln['item_name'] or ln['description']))
            unit = (ln['unit'] or '').strip().upper()
            uqc = UQC.get(unit, 'OTH-OTHERS')
            qty = abs(ln['weight']) if ln['by_weight'] and ln['weight'] \
                else abs(ln['qty'])
            taxable = abs(ln['taxable'] + ln['charge_share'])
            if i == big:
                taxable += want - have
            key = (code, uqc, r2(ln['gst_rate']))
            h = book.setdefault(key, dict(
                desc=receiver(ln['item_name'] or ln['description'], 30),
                qty=0.0, taxable=0.0, igst=0.0, cgst=0.0, sgst=0.0, cess=0.0))
            h['qty'] += sign * qty
            h['taxable'] += sign * taxable
            for k in ('igst', 'cgst', 'sgst', 'cess'):
                h[k] += sign * abs(ln[k])

    for (pos, rate), (taxable, cess) in b2cs.items():
        sheets['b2cs'].append(['OE', _pos(pos), None, rate, r2(taxable),
                               r2(cess), None])
    for name, book in hsn.items():
        for (code, uqc, rate), h in book.items():
            taxes = h['igst'] + h['cgst'] + h['sgst'] + h['cess']
            sheets[name].append([
                code, h['desc'], uqc, round(h['qty'], 3),
                r2(h['taxable'] + taxes), rate, r2(h['taxable']),
                r2(h['igst']), r2(h['cgst']), r2(h['sgst']), r2(h['cess'])])
            preview['hsn'].append(dict(
                book='B2B' if name == 'hsn(b2b)' else 'B2C', hsn=code,
                desc=h['desc'], uqc=uqc, qty=round(h['qty'], 3), rate=rate,
                taxable=r2(h['taxable']), igst=r2(h['igst']),
                cgst=r2(h['cgst']), sgst=r2(h['sgst']), cess=r2(h['cess'])))
    labels = ('Inter-State supplies to registered persons',
              'Intra-State supplies to registered persons',
              'Inter-State supplies to unregistered persons',
              'Intra-State supplies to unregistered persons')
    for i, label in enumerate(labels):
        sheets['exemp'].append([label] + [r2(v) if abs(v) >= 0.005 else None
                                          for v in exemp[i]])

    # Documents issued: each run of numbers from its first to its last. A
    # number missing inside the run is a document cancelled -- deleted --
    # and counts in the total, as the portal counts it.
    for (nature, prefix), nos in series.items():
        numbered = sorted(n for n, _t in nos if n is not None)
        if numbered and len(numbered) == len(nos):
            lo, hi = numbered[0], numbered[-1]
            issued = len(set(numbered))
            total = hi - lo + 1
            sheets['docs'].append([nature, '%s%d' % (prefix, lo),
                                   '%s%d' % (prefix, hi), total,
                                   total - issued])
        else:
            texts = sorted(t for _n, t in nos)
            sheets['docs'].append([nature, texts[0], texts[-1], len(nos), 0])
    for r in sheets['docs']:
        preview['docs'].append(dict(nature=r[0], first=r[1], last=r[2],
                                    total=r[3], cancelled=r[4]))

    for k in tot:
        if isinstance(tot[k], float):
            tot[k] = r2(tot[k])
    tot['taxable'] = r2(tot['b2b_taxable'] + tot['b2c_taxable']
                        - tot['cn_taxable'])
    tot['tax'] = r2(tot['igst'] + tot['cgst'] + tot['sgst'] + tot['cess'])
    code = (firm['code'] or '').lower()
    y, mm = month.split('-')
    name = '%s-%s-%s-gstr1(%s).xlsx' % (mm, y, (firm['gstin'] or code).lower(),
                                        code)
    return dict(firm=dict(id=firm['id'], code=firm['code'], name=firm['name'],
                          gstin=firm['gstin'] or '', state=firm_state,
                          tax_mode=firm['tax_mode']),
                month=month, start=frm, end=to, sheets=sheets, totals=tot,
                warnings=list(OrderedDict.fromkeys(warn)), left_out=left,
                preview=preview, file_name=name,
                rates=[{k: (r2(v) if isinstance(v, float) else v)
                        for k, v in slot.items()}
                       for _r, slot in sorted(rate_tot.items())],
                b2c_inter={k: (r2(v) if isinstance(v, float) else v)
                           for k, v in b2c_inter.items()})


# ------------------------------------------------ the rest of the page
#
# SHREE's GST page is not the return alone. Its saved views (FORMATS/
# REPORTS.DBF: gstr1.gstr1 and gstr2.gstr2) are B2B / B2C large / B2C small /
# export / credit note vouchers, "GST% Wise", "Party Type + Gst %", HSN and
# category summaries, a sales register, a **Monthly Summary**, and the same
# again on the buying side, with a **form3bdetail** view for the 3B. The
# return's own figures come from build(); these three add the rest.


def purchases(conn, cid, frm, to):
    """What was bought in the period and the input tax it carries.

    A debit note here is a purchase return, so it comes off. Reverse charge
    is kept apart: it is both a liability (3.1(d)) and a credit (4(A)(3)).
    """
    rows = api.rows(conn.execute(
        'SELECT i.id, i.doc_type, i.series, i.invoice_no, i.invoice_date, '
        '  i.ref_no, i.taxable, i.cgst, i.sgst, i.igst, i.cess, '
        '  i.grand_total, i.rcm, p.name AS party_name, p.gstin '
        'FROM invoice i JOIN ledger p ON p.id=i.party_id '
        'WHERE i.company_id=? AND i.invoice_date>=? AND i.invoice_date<=? '
        "AND i.doc_type IN ('PURCHASE','DEBIT') "
        'ORDER BY i.invoice_date, i.id', (cid, frm, to)))
    tot = dict(count=0, taxable=0.0, igst=0.0, cgst=0.0, sgst=0.0, cess=0.0,
               value=0.0, no_gstin=0)
    rcm = dict(count=0, taxable=0.0, igst=0.0, cgst=0.0, sgst=0.0, cess=0.0)
    out = []
    for r in rows:
        back = r['doc_type'] == 'DEBIT' or (r['grand_total'] or 0) < 0
        sign = -1 if back else 1
        gstin = (r['gstin'] or '').strip().upper()
        tot['count'] += 1
        tot['value'] += sign * abs(r['grand_total'] or 0)
        if not GSTIN_RE.match(gstin):
            tot['no_gstin'] += 1
        for k in ('taxable', 'igst', 'cgst', 'sgst', 'cess'):
            tot[k] += sign * abs(r[k] or 0)
            if r['rcm']:
                rcm[k] += sign * abs(r[k] or 0)
        if r['rcm']:
            rcm['count'] += 1
        out.append(dict(
            id=r['id'], no=doc_no(r), date=r['invoice_date'],
            party=r['party_name'], gstin=gstin, ref=(r['ref_no'] or '').strip(),
            value=sign * abs(r['grand_total'] or 0),
            taxable=sign * abs(r['taxable'] or 0),
            igst=sign * abs(r['igst'] or 0), cgst=sign * abs(r['cgst'] or 0),
            sgst=sign * abs(r['sgst'] or 0), cess=sign * abs(r['cess'] or 0),
            rcm=bool(r['rcm']), kind=r['doc_type']))
    for d in (tot, rcm):
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = r2(v)
    tot['tax'] = r2(tot['igst'] + tot['cgst'] + tot['sgst'] + tot['cess'])
    return dict(totals=tot, rcm=rcm, rows=out)


def form3b(tot, b2c_inter, bought):
    """The month's 3B figures, under the portal's own table numbers.

    Only what these books can answer: what went out (3.1(a)), what came in
    under reverse charge (3.1(d)), what of the outward went inter-state to
    buyers with no GSTIN (3.2), and the credit on what was bought (4(A)).
    Nil-rated, exempt and non-GST supplies are not separated here, and
    nothing is set off across heads -- 6.1 is the portal's own working.
    """
    itc = bought['totals']
    rcm = bought['rcm']
    other = {k: r2(itc[k] - rcm[k]) for k in ('igst', 'cgst', 'sgst', 'cess')}
    out = dict(
        outward=dict(taxable=tot['taxable'], igst=tot['igst'],
                     cgst=tot['cgst'], sgst=tot['sgst'], cess=tot['cess'],
                     tax=tot['tax']),
        rcm=dict(taxable=rcm['taxable'], igst=rcm['igst'], cgst=rcm['cgst'],
                 sgst=rcm['sgst'], cess=rcm['cess']),
        unregistered_inter=b2c_inter,
        itc_other=other,
        itc_rcm=dict(igst=rcm['igst'], cgst=rcm['cgst'], sgst=rcm['sgst'],
                     cess=rcm['cess']),
        itc_total={k: r2(itc[k]) for k in ('igst', 'cgst', 'sgst', 'cess')})
    out['payable'] = {k: r2(tot[k] + out['rcm'][k] - out['itc_total'][k])
                      for k in ('igst', 'cgst', 'sgst', 'cess')}
    return out


MONTH_SALES = (
    "SELECT substr(i.invoice_date,1,7) m, "
    "  (CASE WHEN i.grand_total < 0 OR (i.doc_type='CREDIT' AND "
    "    COALESCE(i.source,'')<>'SHREE') THEN 1 ELSE 0 END) note, "
    "  (CASE WHEN length(trim(COALESCE(p.gstin,'')))=15 THEN 1 ELSE 0 END) reg,"
    "  COUNT(*) n, SUM(ABS(i.taxable)) taxable, SUM(ABS(i.igst)) igst, "
    "  SUM(ABS(i.cgst)) cgst, SUM(ABS(i.sgst)) sgst, SUM(ABS(i.cess)) cess, "
    "  SUM(ABS(i.grand_total)) value "
    "FROM invoice i JOIN ledger p ON p.id=i.party_id "
    "WHERE i.company_id=? AND i.fy=? AND i.doc_type IN ('SALE','CREDIT') "
    "GROUP BY m, note, reg")

MONTH_BUYS = (
    "SELECT substr(i.invoice_date,1,7) m, COUNT(*) n, "
    "  SUM((CASE WHEN i.doc_type='DEBIT' THEN -1 ELSE 1 END) "
    "      * ABS(i.taxable)) taxable, "
    "  SUM((CASE WHEN i.doc_type='DEBIT' THEN -1 ELSE 1 END) "
    "      * (ABS(i.igst)+ABS(i.cgst)+ABS(i.sgst)+ABS(i.cess))) itc "
    "FROM invoice i WHERE i.company_id=? AND i.fy=? "
    "AND i.doc_type IN ('PURCHASE','DEBIT') GROUP BY m")


def months(conn, cid, fy):
    """A line per month of the year: what went out, what came in, and the tax
    on each -- SHREE's "Monthly Summary", both sides on one screen."""
    out = OrderedDict()

    def slot(m):
        return out.setdefault(m, dict(
            month=m, invoices=0, notes=0, taxable=0.0, igst=0.0, cgst=0.0,
            sgst=0.0, cess=0.0, value=0.0, b2b=0.0, b2c=0.0, buys=0,
            in_taxable=0.0, itc=0.0))
    for r in conn.execute(MONTH_SALES, (cid, fy)):
        s = slot(r['m'])
        sign = -1 if r['note'] else 1
        s['notes' if r['note'] else 'invoices'] += r['n']
        s['value'] += sign * (r['value'] or 0)
        s['taxable'] += sign * (r['taxable'] or 0)
        s['b2b' if r['reg'] else 'b2c'] += sign * (r['taxable'] or 0)
        for k in ('igst', 'cgst', 'sgst', 'cess'):
            s[k] += sign * (r[k] or 0)
    for r in conn.execute(MONTH_BUYS, (cid, fy)):
        s = slot(r['m'])
        s['buys'] += r['n']
        s['in_taxable'] += r['taxable'] or 0
        s['itc'] += r['itc'] or 0
    for s in out.values():
        for k, v in list(s.items()):
            if isinstance(v, float):
                s[k] = r2(v)
        s['tax'] = r2(s['igst'] + s['cgst'] + s['sgst'] + s['cess'])
        s['net'] = r2(s['tax'] - s['itc'])
    return [out[m] for m in sorted(out)]


def preview(conn, cid, q):
    """What the return holds, for the screen -- the sheets are not sent."""
    month = (q.get('month') or '').strip() or \
        datetime.date.today().isoformat()[:7]
    got = build(conn, cid, month)
    got.pop('sheets')
    bought = purchases(conn, cid, got['start'], got['end'])
    got['purchases'] = bought
    got['form3b'] = form3b(got['totals'], got['b2c_inter'], bought)
    got['fy'] = db.fy_of(got['start'])
    got['months'] = months(conn, cid, got['fy'])
    return got


# --------------------------------------------------------------- summary

def _summary(name, rows):
    """Row 3 of a sheet: its counts and totals, where row 2 labels them. A
    sheet with nothing in it is left blank, as the template leaves it."""
    if not rows or (name == 'exemp' and not any(v for r in rows for v in r[1:])):
        return {}

    def distinct(col):
        return len({r[col] for r in rows if r[col]})

    def total(col):
        return r2(sum(r[col] or 0 for r in rows))
    if name == 'b2b,sez,de':
        return {1: distinct(0), 3: len({r[2] for r in rows}),
                5: r2(sum({r[2]: r[4] for r in rows}.values())), 12: total(11),
                13: total(12)}
    if name == 'cdnr':
        return {1: distinct(0), 3: len({r[2] for r in rows}),
                9: r2(sum({r[2]: r[8] for r in rows}.values())), 12: total(11),
                13: total(12)}
    if name == 'b2cl':
        return {1: len({r[0] for r in rows}),
                3: r2(sum({r[0]: r[2] for r in rows}.values())), 7: total(6),
                8: total(7)}
    if name == 'cdnur':
        return {2: len({r[1] for r in rows}),
                6: r2(sum({r[1]: r[5] for r in rows}.values())), 9: total(8),
                10: total(9)}
    if name == 'b2cs':
        return {5: total(4), 6: total(5)}
    if name == 'exemp':
        return {2: total(1), 3: total(2), 4: total(3)}
    if name in ('hsn(b2b)', 'hsn(b2c)'):
        return {1: distinct(0), 5: total(4), 7: total(6), 8: total(7),
                9: total(8), 10: total(9), 11: total(10)}
    if name == 'docs':
        return {4: sum(r[3] for r in rows), 5: sum(r[4] for r in rows)}
    return {}


# --------------------------------------------------------------- the file
#
# An .xlsx is a zip of XML parts. Only what the template uses is written:
# shared strings, five cell styles, column widths, a frozen heading and
# merged cells.

STYLE = {'': 0, 'title': 1, 'label': 2, 'head': 3, 'rev': 4,
         'money': 5, 'date': 6, 'text': 7, 'center': 8, 'plain': 0}
# Columns written as text (a date, an HSN code), and the ones centred, as the
# template has them; the rest of a sheet's numbers are money, except where
# the template leaves them plain.
TEXT_COLS = {'b2b,sez,de': {4: 'date', 7: 'center'},
             'cdnr': {4: 'date', 5: 'center', 7: 'center'},
             'b2cl': {2: 'date'}, 'cdnur': {3: 'date', 4: 'center'},
             'hsn(b2b)': {1: 'text'}, 'hsn(b2c)': {1: 'text'}}
PLAIN_SHEETS = {'hsn(b2b)', 'hsn(b2c)', 'docs'}

STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="###0.00"/></numFmts>'
    '<fonts count="3">'
    '<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><color rgb="FF000000"/><name val="Calibri"/><family val="2"/></font>'
    '</fonts>'
    '<fills count="5">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF0070C0"/><bgColor indexed="64"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFF8CBAD"/><bgColor indexed="64"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFB4C6E7"/><bgColor indexed="64"/></patternFill></fill>'
    '</fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="9">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center"/></xf>'
    '<xf numFmtId="0" fontId="2" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center"/></xf>'
    '<xf numFmtId="0" fontId="2" fillId="4" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center"/></xf>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center"/></xf>'
    '<xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center"/></xf>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>')


def _col(n):
    s = ''
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class _Strings(object):
    def __init__(self):
        self.index, self.items = {}, []

    def get(self, text):
        if text not in self.index:
            self.index[text] = len(self.items)
            self.items.append(text)
        return self.index[text]

    def xml(self):
        body = ''.join('<si><t xml:space="preserve">%s</t></si>' % escape(t)
                       for t in self.items)
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
                ' count="%d" uniqueCount="%d">%s</sst>'
                % (len(self.items), len(self.items), body))


def _cell(ref, value, style, strings):
    s = ' s="%d"' % style if style else ''
    if value is None or value == '':
        return '<c r="%s"%s/>' % (ref, s) if style else ''
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        num = repr(round(value, 6)) if isinstance(value, float) else str(value)
        if num.endswith('.0'):
            num = num[:-2]
        return '<c r="%s"%s><v>%s</v></c>' % (ref, s, num)
    return '<c r="%s"%s t="s"><v>%d</v></c>' % (ref, s, strings.get(str(value)))


def _sheet_xml(name, rows_spec, merges, widths, freeze, data, strings):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
           ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">']
    if freeze:
        out.append('<sheetViews><sheetView workbookViewId="0"><pane ySplit="4" '
                   'topLeftCell="A5" activePane="bottomLeft" state="frozen"/>'
                   '</sheetView></sheetViews>')
    else:
        out.append('<sheetViews><sheetView workbookViewId="0"/></sheetViews>')
    out.append('<sheetFormatPr defaultRowHeight="15"/>')
    if widths:
        cols = sorted(widths.items(), key=lambda kv: (len(kv[0]), kv[0]))
        out.append('<cols>%s</cols>' % ''.join(
            '<col min="%d" max="%d" width="%s" customWidth="1"/>'
            % (_idx(k), _idx(k), w) for k, w in cols))
    out.append('<sheetData>')
    summary = _summary(name, data)
    money_cols = {c for c, v in summary.items() if isinstance(v, float)}
    for r, cells in enumerate(rows_spec, 1):
        xs = []
        if r == 3:
            for c in sorted(summary):
                style = STYLE['money'] if c in money_cols and \
                    name not in PLAIN_SHEETS else 0
                xs.append(_cell('%s3' % _col(c), summary[c], style, strings))
        else:
            for c, v, st in cells:
                xs.append(_cell('%s%d' % (_col(c), r), v, STYLE[st], strings))
        out.append('<row r="%d">%s</row>' % (r, ''.join(x for x in xs if x)))
    fmt = TEXT_COLS.get(name, {})
    for i, values in enumerate(data):
        r = 5 + i
        xs = []
        for c, v in enumerate(values, 1):
            if c in fmt:
                style = STYLE[fmt[c]]
            elif isinstance(v, (int, float)) and name not in PLAIN_SHEETS:
                style = STYLE['money']
            else:
                style = 0
            xs.append(_cell('%s%d' % (_col(c), r), v, style, strings))
        out.append('<row r="%d">%s</row>' % (r, ''.join(x for x in xs if x)))
    out.append('</sheetData>')
    if merges:
        out.append('<mergeCells count="%d">%s</mergeCells>' % (
            len(merges), ''.join('<mergeCell ref="%s"/>' % m for m in merges)))
    out.append('<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" '
               'header="0.3" footer="0.3"/></worksheet>')
    return ''.join(out)


def _idx(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def workbook(sheets):
    """The .xlsx bytes for {sheet name: data rows}, in the template's order."""
    strings = _Strings()
    parts = []
    for i, (name, rows_spec, merges, widths, freeze) in enumerate(LAYOUT, 1):
        parts.append(('xl/worksheets/sheet%d.xml' % i,
                      _sheet_xml(name, rows_spec, merges, widths, freeze,
                                 sheets.get(name) or [], strings)))
    n = len(LAYOUT)
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          + ''.join('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType='
                    '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    % i for i in range(1, n + 1)) +
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
          '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
          '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            '</Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
          ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<bookViews><workbookView activeTab="1"/></bookViews><sheets>'
          + ''.join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
                    % (escape(name, {'"': '&quot;'}), i, i)
                    for i, (name, *_r) in enumerate(LAYOUT, 1)) +
          '</sheets></workbook>')
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + ''.join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>'
                         % (i, i) for i in range(1, n + 1)) +
               '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
               '</Relationships>' % (n + 1, n + 2))
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"'
            ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dc:title>GSTR-1</dc:title><dc:creator>Billing</dc:creator>'
            '<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created>'
            '<dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified>'
            '</cp:coreProperties>' % (now, now))
    app = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
           '<Application>Billing</Application></Properties>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', ct)
        z.writestr('_rels/.rels', rels)
        z.writestr('xl/workbook.xml', wb)
        z.writestr('xl/_rels/workbook.xml.rels', wb_rels)
        z.writestr('xl/styles.xml', STYLES_XML)
        for path, xml in parts:
            z.writestr(path, xml)
        z.writestr('xl/sharedStrings.xml', strings.xml())
        z.writestr('docProps/core.xml', core)
        z.writestr('docProps/app.xml', app)
    return buf.getvalue()


def excel(conn, cid, q):
    """(bytes, file name, content type) for the download."""
    month = (q.get('month') or '').strip() or \
        datetime.date.today().isoformat()[:7]
    got = build(conn, cid, month)
    if got['firm']['tax_mode'] != 'gst':
        raise api.ApiError('%s keeps no GST; its challans are billed in BST '
                           'and BSI.' % got['firm']['name'])
    return (workbook(got['sheets']), got['file_name'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
