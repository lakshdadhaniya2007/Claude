"""Laying out free text in a fixed number of ruled lines."""


def wrap_lines(lines, measure, width, max_lines):
    """Break each line that is too wide, and keep at most `max_lines`.

    `measure(text)` answers how wide `text` prints. A line is broken at the
    last space or comma that leaves it fitting -- addresses are typed as
    "OFF-NH-8B,SHAPAR,TAL-KOTDA SANGANI,(M)99..." -- and the rest carried to
    the next line. Nothing is dropped while there are lines to spare; what
    still does not fit is left for the caller to trim.
    """
    out = []
    queue = [' '.join(str(l or '').split()) for l in lines]
    queue = [l for l in queue if l]
    while queue and len(out) < max_lines:
        text = queue.pop(0)
        if measure(text) <= width or len(out) == max_lines - 1:
            out.append(text)
            continue
        cut = None
        for i in range(len(text) - 1, 0, -1):
            if text[i] == ' ' and measure(text[:i]) <= width:
                cut = (text[:i], text[i + 1:])
                break
            if text[i - 1] == ',' and measure(text[:i]) <= width:
                cut = (text[:i], text[i:])
                break
        if not cut:
            out.append(text)
            continue
        out.append(cut[0].rstrip())
        queue.insert(0, cut[1].strip())
    # Lines still waiting join the last one, so the caller can trim it.
    if queue and out:
        out[-1] = ' '.join([out[-1]] + queue)
    return out


def wrap_two(measure, text, size, width, floor=6.5, nudge=1.0, head=''):
    """Lay an item name out in at most two lines, never truncated. Answers
    (lines, size). Shared by the challan and the bill (26 Sep 2026).

    `measure(text, size)` answers how wide `text` prints at `size`. A name
    that will not fit is shrunk a little first -- by at most `nudge`, and not
    at all with `nudge=0`, when the caller has chosen the size for the whole
    column -- and only then broken at the word boundary that makes the two
    halves most even. Nothing is ellipsised: the goods have to be readable in
    full.

    `head` is the part that must stay whole on the first line -- a steel from
    Challan Stock, "CRCA - REGULAR - 0.50". Broken in the middle, its
    thickness went down beside the cutting size and "0.50 102 MM PATO" read
    as one size. With a head, a name that does not fit breaks straight after
    it.
    """
    def shrink(t, sz, lo):
        while sz > lo and measure(t, sz) > width:
            sz -= 0.25
        return sz

    text = ' '.join(str(text or '').split())
    if not text:
        return [], size
    if measure(text, size) <= width:
        return [text], size
    head = ' '.join(str(head or '').split())
    if head and text.startswith(head + ' '):
        a, b = head, text[len(head):].strip()
        two = size
        while two > 4.5 and max(measure(a, two), measure(b, two)) > width:
            two -= 0.25
        return [a, b], two

    if nudge:
        one = shrink(text, size, max(floor, size - nudge))
        if measure(text, one) <= width:
            return [text], one

    words = text.split(' ')
    if len(words) < 2:
        return [text], shrink(text, size, 4.5)

    # The split that leaves the widest half as narrow as possible.
    best = min(((max(measure(' '.join(words[:k]), size),
                     measure(' '.join(words[k:]), size)), k)
                for k in range(1, len(words))))[1]
    a, b = ' '.join(words[:best]), ' '.join(words[best:])
    two = size
    while two > 4.5 and max(measure(a, two), measure(b, two)) > width:
        two -= 0.25
    return [a, b], two


def wrap_fixed(measure, text, width, head=''):
    """Lay an item name out at one size, in as many lines as it takes, none
    of them wider than `width` -- never shrunk, never trimmed (6 Oct 2026:
    "as there is more text in Particulars the font size decreases -- make sure
    the font size remains the same and it shouldn't go outside the given
    space"). Answers the lines. The challan's (the bill keeps `wrap_two`).

    `measure(text)` answers how wide `text` prints at that one size. A name
    that fits is one line. Otherwise the breaks the pad has always made, while
    they fit: straight after a steel `head` ("0.90 CRCA - REGULAR 425.00"),
    else the two most even halves. Past that, word by word -- a steel too long
    for one line wraps first and what follows it starts a line of its own
    unless it fits whole beside the steel's last words, so the thickness never
    lands next to a cutting size. A single word longer than the line breaks
    inside itself.
    """
    text = ' '.join(str(text or '').split())
    if not text:
        return []
    if measure(text) <= width:
        return [text]
    head = ' '.join(str(head or '').split())
    if not (head and text.startswith(head + ' ')):
        words = text.split(' ')
        if len(words) > 1:
            k = min(range(1, len(words)),
                    key=lambda k: (max(measure(' '.join(words[:k])),
                                       measure(' '.join(words[k:]))), k))
            a, b = ' '.join(words[:k]), ' '.join(words[k:])
            if measure(a) <= width and measure(b) <= width:
                return [a, b]
        return _greedy(measure, words, width)
    rest = text[len(head):].strip()
    if measure(head) <= width:
        return [head] + wrap_fixed(measure, rest, width)
    lines = _greedy(measure, head.split(' '), width)
    beside = lines[-1] + ' ' + rest
    if measure(beside) <= width:
        return lines[:-1] + [beside]
    return lines + wrap_fixed(measure, rest, width)


def _greedy(measure, words, width):
    """As many words to a line as fit; a word wider than a whole line is cut
    where it reaches the edge and carried on to the next."""
    lines, cur = [], ''
    for w in words:
        t = cur + ' ' + w if cur else w
        if measure(t) <= width:
            cur = t
            continue
        if cur:
            lines.append(cur)
        cur = w
        while measure(cur) > width and len(cur) > 1:
            n = len(cur) - 1
            while n > 1 and measure(cur[:n]) > width:
                n -= 1
            lines.append(cur[:n])
            cur = cur[n:]
    if cur:
        lines.append(cur)
    return lines
