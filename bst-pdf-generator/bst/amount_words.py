"""Rupees-in-words, Indian numbering system, matching the original stationery.

The original bills print e.g.:
    46729  -> "Forty six thousand seven hundred twenty nine only."
    39600  -> "Thirty nine thousand six hundred only."

Note the house style: no "and", no hyphens, sentence case, trailing " only."
"""

_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]


def _two(n):
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + (" " + _ONES[o] if o else "")


def _three(n):
    h, r = divmod(n, 100)
    out = []
    if h:
        out.append(_ONES[h] + " hundred")
    if r:
        out.append(_two(r))
    return " ".join(out)


def indian_words(n):
    """Integer -> words using crore / lakh / thousand / hundred."""
    n = int(n)
    if n == 0:
        return "zero"
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1_000)
    if crore:
        parts.append(indian_words(crore) + " crore")
    if lakh:
        parts.append(_two(lakh) + " lakh")
    if thousand:
        parts.append(_two(thousand) + " thousand")
    if n:
        parts.append(_three(n))
    return " ".join(p for p in parts if p)


def rupees_in_words(amount, include_paise=True):
    """Format a rupee amount the way the printed bill does."""
    rupees = int(abs(amount))
    paise = int(round((abs(amount) - rupees) * 100))
    if paise == 100:
        rupees += 1
        paise = 0

    text = indian_words(rupees)
    if include_paise and paise:
        text += " and paise " + _two(paise)
    text += " only."
    return text[0].upper() + text[1:]


def fmt(amount, decimals=2, commas=True):
    """Indian-grouped number, e.g. 1234567.5 -> '12,34,567.50'."""
    neg = amount < 0
    s = f"{abs(amount):.{decimals}f}"
    if commas:
        if "." in s:
            whole, frac = s.split(".")
        else:
            whole, frac = s, ""
        if len(whole) > 3:
            head, tail = whole[:-3], whole[-3:]
            groups = []
            while len(head) > 2:
                groups.insert(0, head[-2:])
                head = head[:-2]
            if head:
                groups.insert(0, head)
            whole = ",".join(groups + [tail])
        s = whole + ("." + frac if frac else "")
    return ("-" if neg else "") + s
