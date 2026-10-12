"""The GST calculator: how a bill's lines become its taxable value, its tax
and its total -- worked out the way SHREE worked them out.

Asked for on 19 Sep 2026: *make the GST calculator foolproof; go through
SHREE if you don't understand anything.* SHREE's program is packed and gives
nothing away, so its rules were read off what it wrote: every GST bill BST and
BSI raised from July 2017 to September 2026, 12,155 of them, line by line
(SITM: kilos, rate, amount, the rate and amount of each tax) against the bill
(SALE: TOTAL1 taxable, TOTAL2 grand total). check_gst.py replays them all.

1. **A line is its kilos times its rate**, to the paisa. (81 lines of 12,981
   carry an amount typed over the product.)
2. **The tax is worked out on the bill, not on each line**, per rate: CGST and
   SGST are *each* the half rate on the taxable value, so they are always
   equal; IGST is the whole rate. 12,082 of 12,083 intra-state bills and all
   58 inter-state ones. The lines carry it in proportion -- SHREE keeps them
   unrounded; here they are rounded and the last line takes what is left, so
   they add up to the bill exactly.
3. **Charges are taxed with the goods.** SHREE's P & F box sits in the taxable
   value its tax is worked out on (GT 154: 43,080 + 500 at 18% = 7,844.40).
4. **The grand total is the whole rupee.** Every bill, 12,136 of 12,155; the
   rest carry something added after the tax (TCS in 2020-21).
5. **Half goes up** -- half a paisa on the tax (430 bills), fifty paise on the
   total (254 bills) -- and away from nought on a return. FoxPro's ROUND.

Before this the app took 18% once and halved it, so on those 430 bills CGST
and SGST came out a paisa apart (6,059.57 / 6,059.56 where SHREE has
6,059.57 twice); it rounded the total with Python's round(), which sends an
exact half to the even rupee -- a rupee short on half of those 254; the
screen rounded 1.005 to 1.00 while the server made it 1.01; and a return's
half paisa went the wrong way. The screen (static/app.js, `Gst`) mirrors this
file line for line, so what is typed, what is saved and what is printed agree.
"""
from decimal import Decimal, ROUND_HALF_UP


def money(x, places=2):
    """Rounded as SHREE rounds: to `places`, half away from nought, on the
    figure as it is written rather than as the computer holds it (1.005 is
    one rupee and half a paisa, not 1.00499999...).

    The figure is first written to nine places, which is what the screen does
    too (toFixed(9)), so the two can never round the same number apart.
    """
    v = float(x or 0)
    q = Decimal('%.9f' % abs(v)).quantize(Decimal(1).scaleb(-places),
                                          rounding=ROUND_HALF_UP)
    return -float(q) if v < 0 else float(q)


def rupee(x):
    return money(x, 0)


def spread(total, weights):
    """`total` shared over `weights` in proportion, each share to the paisa
    and the last taking what the rounding leaves, so they add up to it."""
    out, done = [], 0.0
    base = sum(weights)
    for i, w in enumerate(weights):
        if i == len(weights) - 1:
            out.append(money(total - done))
        else:
            part = money(total * w / base) if base else 0.0
            out.append(part)
            done += part
    return out


def tax_on(base, rate, cess_rate=0.0, is_igst=False):
    """The tax on one rate's taxable value: (cgst, sgst, igst, cess).

    CGST and SGST are each levied at half the rate, so each is worked out on
    its own and they cannot differ. IGST is the whole rate.
    """
    rate, cess_rate = float(rate or 0), float(cess_rate or 0)
    cess = money(base * cess_rate / 100.0)
    if is_igst:
        return 0.0, 0.0, money(base * rate / 100.0), cess
    half = money(base * rate / 200.0)
    return half, half, 0.0, cess


def line_amount(ln, taxed=True):
    """(rate, gross, discount, taxable) for one line.

    The rate is per kilo when the line goes by weight, otherwise per piece. A
    price given with the tax in it is stripped of the tax and spread over the
    kilos, the rate rounded to the paisa first so that rate x kilos is always
    the amount on the page.
    """
    gst = float(ln.get('gst_rate') or 0) if taxed else 0.0
    cess_rate = float(ln.get('cess_rate') or 0) if taxed else 0.0
    by_weight = 1 if ln.get('by_weight', 1) in (1, '1', True, None) else 0
    base = float(ln.get('weight') or 0) if by_weight else float(ln.get('qty') or 0)
    incl = float(ln.get('incl_price') or 0)
    rate = float(ln.get('rate') or 0)
    if incl > 0:
        rate = money(incl / (1.0 + (gst + cess_rate) / 100.0) / base) if base else 0.0
    gross = base * rate
    disc = gross * float(ln.get('discount_pct') or 0) / 100.0
    return rate, money(gross), money(disc), money(gross - disc)


def bill(lines, charges=0.0, adj=0.0, is_igst=False, taxed=True,
         round_off=None, whole_rupee=False, tcs=0.0):
    """Work a bill out. `lines` are dicts carrying at least `taxable`,
    `gst_rate` and `cess_rate`; each gets `charge_share`, `cgst`, `sgst`,
    `igst`, `cess` and `line_total` written into it. Returns the totals.

    `charges` is cutting + transport + loading, `adj` the challan's round off
    carried into the taxable value; both are spread over the lines by value
    and taxed with them. `round_off` is what somebody typed in the Round Off
    box; left blank, there is none and the total stays to the paisa -- the
    app stopped rounding by itself on 2 Oct 2026 ("remove auto round off").
    `whole_rupee` rounds a blank one to the rupee, half up, as SHREE did:
    check_gst.py sets it to prove these rules against SHREE's own bills.
    `tcs` is tax collected at source on a purchase (8 Oct 2026): added after
    the tax, never taxed itself, before any Round Off.
    """
    goods = money(sum(l['taxable'] for l in lines))
    charges = money(charges)
    adj = money(adj) if taxed else 0.0
    for l in lines:
        l['charge_share'] = 0.0
        l['cgst'] = l['sgst'] = l['igst'] = l['cess'] = 0.0
    spend = money(charges + adj) if taxed else 0.0
    if spend and goods:
        for l, share in zip(lines, spread(spend, [l['taxable'] for l in lines])):
            l['charge_share'] = share
    out = dict(goods=goods, charges=charges, adj=adj, cgst=0.0, sgst=0.0,
               igst=0.0, cess=0.0)
    if taxed:
        buckets = {}
        for l in lines:
            key = (float(l.get('gst_rate') or 0), float(l.get('cess_rate') or 0))
            buckets.setdefault(key, []).append(l)
        for (rate, cess_rate), group in sorted(buckets.items()):
            worth = [l['taxable'] + l['charge_share'] for l in group]
            base = money(sum(worth))
            cg, sg, ig, ce = tax_on(base, rate, cess_rate, is_igst)
            for k, amount in (('cgst', cg), ('sgst', sg), ('igst', ig),
                              ('cess', ce)):
                for l, part in zip(group, spread(amount, worth)):
                    l[k] = part
                out[k] = money(out[k] + amount)
        out['taxable'] = money(goods + charges + adj)
    else:
        out['taxable'] = goods
    for l in lines:
        l['line_total'] = money(l['taxable'] + l['charge_share'] + l['cgst']
                                + l['sgst'] + l['igst'] + l['cess'])
    out['tax'] = money(out['cgst'] + out['sgst'] + out['igst'] + out['cess'])
    # A firm outside GST adds its charges after the goods, untaxed.
    out['tcs'] = money(tcs)
    raw = money(out['taxable'] + out['tax'] + (0.0 if taxed else charges)
                + out['tcs'])
    typed = str(round_off if round_off is not None else '').strip()
    if typed not in ('', 'None'):
        out['round_off'] = money(round_off)
        out['grand_total'] = money(raw + out['round_off'])
    elif whole_rupee:
        out['grand_total'] = rupee(raw)
        out['round_off'] = money(out['grand_total'] - raw)
    else:
        out['round_off'] = 0.0
        out['grand_total'] = raw
    return out
