"""
Font registration for BST documents.

The original PDFs were produced with Verdana / Verdana-Bold / Verdana-Bold-Italic
(plus built-in Helvetica for the GST summary block). To get a byte-for-byte
identical *look*, Verdana must be available on the machine running this script.

Verdana ships with Windows and with MS Office on macOS, so on those machines the
output is pixel-identical to the original. On Linux, install it with:

    sudo apt install ttf-mscorefonts-installer

If Verdana is not found we fall back to DejaVu Sans (metrically close) and, as a
last resort, to the PDF built-in Helvetica. Layout coordinates never change --
only glyph shapes -- so column alignment stays correct either way.
"""

import os
import sys
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Logical font names used throughout invoice.py / challan.py
VERDANA = "BST-Verdana"
VERDANA_BOLD = "BST-Verdana-Bold"
VERDANA_BOLD_ITALIC = "BST-Verdana-BoldItalic"
HELV = "Helvetica"           # built-in, used by the GST summary table
HELV_BOLD = "Helvetica-Bold"  # built-in, used by the GST summary total row

_SEARCH_DIRS = [
    r"C:\Windows\Fonts",
    r"C:\WINDOWS\Fonts",
    os.path.expanduser(r"~\AppData\Local\Microsoft\Windows\Fonts"),
    "/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    os.path.expanduser("~/Library/Fonts"),
    "/usr/share/fonts/truetype/msttcorefonts",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/TTF",
    "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts"),
]

# Candidate filenames per logical font, best match first.
_CANDIDATES = {
    VERDANA: ["verdana.ttf", "Verdana.ttf", "Verdana.ttc",
              "DejaVuSans.ttf", "LiberationSans-Regular.ttf"],
    VERDANA_BOLD: ["verdanab.ttf", "Verdana Bold.ttf", "Verdana-Bold.ttf",
                   "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"],
    VERDANA_BOLD_ITALIC: ["verdanaz.ttf", "Verdana Bold Italic.ttf",
                          "Verdana-BoldItalic.ttf",
                          "DejaVuSans-BoldOblique.ttf",
                          "LiberationSans-BoldItalic.ttf"],
}

_registered = False
_report = {}


_listing_cache = {}


def _listing(d):
    """What is in a font directory, lowercased name -> full path.

    Read once per directory. The case-insensitive fallback used to list the
    whole directory again for every candidate filename it had not found --
    up to sixty full listings of C:\\Windows\\Fonts, which on Windows holds
    several hundred files and is the one directory this is guaranteed to
    search. The first PDF of the session paid for all of them.
    """
    got = _listing_cache.get(d)
    if got is None:
        got = {}
        try:
            for name in os.listdir(d):
                got.setdefault(name.lower(), os.path.join(d, name))
        except OSError:
            pass
        _listing_cache[d] = got
    return got


def _find(filenames):
    dirs = [d for d in _SEARCH_DIRS if d and os.path.isdir(d)]
    for name in filenames:
        for d in dirs:
            # The exact name first, which is the hit on every machine that has
            # the font installed under the name we expect.
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
            found = _listing(d).get(name.lower())
            if found:
                return found
    return None


def register_fonts(verbose=False):
    """Register the TTFs. Safe to call more than once."""
    global _registered
    if _registered:
        return _report

    for logical, names in _CANDIDATES.items():
        path = _find(names)
        if path:
            try:
                pdfmetrics.registerFont(TTFont(logical, path))
                _report[logical] = path
                continue
            except Exception as exc:  # pragma: no cover
                print(f"[fonts] could not load {path}: {exc}", file=sys.stderr)
        # Last resort: alias to a built-in Type 1 face.
        fallback = {
            VERDANA: "Helvetica",
            VERDANA_BOLD: "Helvetica-Bold",
            VERDANA_BOLD_ITALIC: "Helvetica-BoldOblique",
        }[logical]
        pdfmetrics.registerFontFamily(fallback)
        _report[logical] = f"(built-in) {fallback}"

    _registered = True
    if verbose:
        for k, v in _report.items():
            print(f"[fonts] {k:26s} -> {v}")
    return _report


def font(logical):
    """Resolve a logical name to whatever is actually registered."""
    register_fonts()
    val = _report.get(logical, logical)
    if isinstance(val, str) and val.startswith("(built-in) "):
        return val.replace("(built-in) ", "")
    return logical
