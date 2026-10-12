/* Billing & Order Management -- single-page front end (no dependencies). */
'use strict';

/* Windows draws the same type smaller, and style.css makes it larger under
   html.win (11 Oct 2026). Marked before anything else is drawn. */
if (/Windows/.test(navigator.userAgent)) document.documentElement.classList.add('win');

/* ============================================================== helpers */
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function el(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}
function num(v) { const n = parseFloat(v); return isFinite(n) ? n : 0; }

/* ------------------------------------------------------------ GST
   The GST calculator, line for line the same as app/gst.py -- which is how
   SHREE worked out every one of its 12,155 GST bills (check_gst.py replays
   them). What the bill screen shows while it is typed is what the server
   saves and what the bill prints; nothing is worked out any other way.    */
const Gst = {
  /* Rounded as SHREE rounds: half away from nought, on the figure as
     written -- 1.005 is 1.01, not the 1.00 that 1.005 * 100 would give.
     Written to nine places first, as gst.py does with '%.9f'. */
  money(v, places) {
    places = places === undefined ? 2 : places;
    const n = Number(v) || 0;
    const [ip, fp] = Math.abs(n).toFixed(9).split('.');
    const unit = 10n ** BigInt(9 - places);
    const all = BigInt(ip + fp);
    let q = all / unit;
    if ((all % unit) * 2n >= unit) q += 1n;
    const out = Number(q) / 10 ** places;
    return n < 0 ? -out : out;
  },
  rupee(v) { return Gst.money(v, 0); },
  spread(total, weights) {
    const base = weights.reduce((a, w) => a + w, 0);
    let done = 0;
    return weights.map((w, i) => {
      if (i === weights.length - 1) return Gst.money(total - done);
      const part = base ? Gst.money(total * w / base) : 0;
      done += part;
      return part;
    });
  },
  /* CGST and SGST are each the half rate, so they cannot differ. */
  taxOn(base, rate, cessRate, igst) {
    const cess = Gst.money(base * num(cessRate) / 100);
    if (igst) return { cgst: 0, sgst: 0, igst: Gst.money(base * num(rate) / 100), cess };
    const half = Gst.money(base * num(rate) / 200);
    return { cgst: half, sgst: half, igst: 0, cess };
  },
  /* One line: kilos (or pieces) x rate to the paisa. A price with the tax in
     it has the tax backed out and the rate rounded to the paisa first. */
  line(l, taxed) {
    const gst = taxed ? num(l.gst_rate) : 0;
    const cessRate = taxed ? num(l.cess_rate) : 0;
    const base = l.by_weight ? num(l.weight) : num(l.qty);
    const incl = num(l.incl_price);
    let rate = num(l.rate);
    if (incl > 0) rate = base ? Gst.money(incl / (1 + (gst + cessRate) / 100) / base) : 0;
    const gross = base * rate;
    const disc = gross * num(l.discount_pct) / 100;
    return { rate, base, gross: Gst.money(gross), disc: Gst.money(disc),
             taxable: Gst.money(gross - disc) };
  },
  /* The bill as a whole: charges and the challan's round off shared over the
     lines by value and taxed with them, the tax per rate on the bill, and
     the total to the paisa -- plus a Round Off only where one is typed.
     Nothing is rounded by itself since 2 Oct 2026 ("remove auto round off").
     */
  bill(rows, o) {
    const taxed = !o.noTax;
    const goods = Gst.money(rows.reduce((a, r) => a + r.taxable, 0));
    const charges = Gst.money(o.charges || 0);
    const adj = taxed ? Gst.money(o.adj || 0) : 0;
    const shares = rows.map(() => 0);
    const spend = taxed ? Gst.money(charges + adj) : 0;
    if (spend && goods) Gst.spread(spend, rows.map(r => r.taxable)).forEach((s, i) => { shares[i] = s; });
    const t = { goods, charges, adj, cgst: 0, sgst: 0, igst: 0, cess: 0 };
    if (taxed) {
      const buckets = new Map();
      rows.forEach((r, i) => {
        const k = num(r.gst_rate) + '|' + num(r.cess_rate);
        if (!buckets.has(k)) buckets.set(k, []);
        buckets.get(k).push(i);
      });
      buckets.forEach((idx, k) => {
        const [rate, cessRate] = k.split('|').map(Number);
        const base = Gst.money(idx.reduce((a, i) => a + rows[i].taxable + shares[i], 0));
        const x = Gst.taxOn(base, rate, cessRate, o.igst);
        ['cgst', 'sgst', 'igst', 'cess'].forEach(f => { t[f] = Gst.money(t[f] + x[f]); });
      });
      t.taxable = Gst.money(goods + charges + adj);
    } else {
      t.taxable = goods;
    }
    t.tax = Gst.money(t.cgst + t.sgst + t.igst + t.cess);
    // TCS on a purchase: after the tax, never taxed itself (8 Oct 2026).
    t.tcs = Gst.money(o.tcs || 0);
    const raw = Gst.money(t.taxable + t.tax + (taxed ? 0 : charges) + t.tcs);
    const typed = o.roundOff === undefined || o.roundOff === null ? '' : String(o.roundOff).trim();
    t.round_off = typed !== '' ? Gst.money(num(typed)) : 0;
    t.grand_total = Gst.money(raw + t.round_off);
    t.raw = raw;
    return t;
  },
  /* A state's two-digit code: the GSTIN's first two digits, then the code
     typed -- as the server and the e-invoice read it. */
  stateOf(o) {
    const g = String((o && o.gstin) || '').replace(/\s/g, '').toUpperCase();
    if (g.length === 15 && /^\d\d/.test(g) && g.slice(0, 2) !== '00') return g.slice(0, 2);
    const d = String((o && o.state_code) || '').replace(/\D/g, '');
    return d && Number(d) ? String(Number(d)).padStart(2, '0') : '';
  },
};

/* ------------------------------------------------------------- searching
   Names are not punctuated consistently -- "J.K.ENG.ENTERPRISE" sits beside
   "PVT LTD" -- and a search box that only does substring misses the row
   somebody is looking straight at. These mirror the server's matching in
   api.py, so a list filtered here and a list filtered there agree.          */

/** "J.K.ENG. ENTERPRISE" -> "JKENGENTERPRISE". */
function squash(s) {
  return String(s == null ? '' : s).toUpperCase().replace(/[^A-Z0-9]+/g, '');
}

/** How well `fields` answer `term`: 0 best, 3 no match. The first field is
 *  the name, and is the one read through its punctuation. */
function searchRank(term, fields) {
  const words = String(term || '').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return 0;
  const parts = fields.filter(Boolean).map(String);
  const key = squash(term);
  const name = squash(parts[0] || '');
  if (key && name.indexOf(key) === 0) return 0;      // starts with it
  if (key && name.indexOf(key) > 0) return 1;        // holds it
  const hay = parts.join(' ').toUpperCase();         // every word, any order
  return words.every(w => hay.indexOf(w.toUpperCase()) >= 0) ? 2 : 3;
}

/** Filter a list to what matches, closest first. `fields` maps a row to the
 *  values to search, name first. */
function searchList(term, all, fields, limit) {
  const hits = [];
  all.forEach(x => {
    const r = searchRank(term, fields(x));
    if (r < 3) hits.push([r, x]);
  });
  hits.sort((a, b) => a[0] - b[0]);        // stable, so ties stay in name order
  return hits.slice(0, limit || 30).map(h => h[1]);
}

/** Letters apart, a pair swapped counting as one (so "RIKIA" is one away
 *  from "RIKAI"). Gives up past `max` -- only "close enough" is asked. */
function editDistance(a, b, max) {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let pp = null, p = [...Array(b.length + 1).keys()];
  for (let i = 1; i <= a.length; i++) {
    const c = [i];
    let low = i;
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      let v = Math.min(p[j] + 1, c[j - 1] + 1, p[j - 1] + cost);
      if (pp && i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        v = Math.min(v, pp[j - 2] + 1);
      }
      c.push(v);
      if (v < low) low = v;
    }
    if (low > max) return max + 1;
    pp = p;
    p = c;
  }
  return p[b.length];
}

/* **How the Accounts box reads a name** (28 Sep 2026: "fix the search for
   Accounts"). The same order as `searchRank`, with one step between and one
   after: the typed words starting the name's words ("shree ind" for SHREE
   SANTOSH INDUSTRIES), and last, a name near enough -- one letter wrong,
   missing, extra or two swapped, two in a long word -- because "rikia" for
   RIKAI INDUSTRIES found nothing at all and the box just went blank.

     0 the very name                1 the name starts with it
     2 its words start the name's   3 the name holds it
     4 every word somewhere, `also` included
     5 near enough (a slip)         9 not at all                            */
function nameRank(term, name, also) {
  const split = s => String(s || '').toUpperCase().split(/[^A-Z0-9]+/).filter(Boolean);
  const words = split(term);
  if (!words.length) return 0;
  const key = words.join(''), nm = squash(name), nw = split(name);
  if (nm === key) return 0;
  if (nm.indexOf(key) === 0) return 1;
  const starts = w => nw.some(n => n.indexOf(w) === 0);
  if (words.every(starts)) return 2;
  if (nm.indexOf(key) > 0) return 3;
  const hay = split(name).concat(split(also)).join(' ');
  if (words.every(w => hay.indexOf(w) >= 0)) return 4;
  // Near enough: each word, as typed so far, against the start of one of
  // the name's words a letter shorter, as long, or a letter longer.
  // Its first letter is taken as typed right: a slip lands later in a word,
  // and without that SANTSH read as GANESH.
  const near = w => w.length >= 3 && nw.some(n => {
    if (n[0] !== w[0]) return false;
    const max = w.length <= 5 ? 1 : 2;
    for (let l = w.length - 1; l <= w.length + 1; l++) {
      if (l < 2) continue;
      if (editDistance(w, n.slice(0, l), max) <= max) return true;
      if (l >= n.length) break;
    }
    return false;
  });
  return words.every(w => starts(w) || near(w)) ? 5 : 9;
}

/* Indian grouping: 12,34,567.89 */
function fmt(v, dec) {
  const d = dec === undefined ? 2 : dec;
  const n = num(v);
  const neg = n < 0;
  let s = Math.abs(n).toFixed(d);
  let [i, f] = s.split('.');
  if (i.length > 3) {
    const last3 = i.slice(-3);
    const rest = i.slice(0, -3);
    i = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',') + ',' + last3;
  }
  return (neg ? '-' : '') + i + (f ? '.' + f : '');
}
function qty(v) {
  const n = num(v);
  return fmt(n, 3).replace(/\.?0+$/, m => (m.indexOf('.') === 0 ? '' : m));
}
function money(v) { return '₹' + fmt(v); }

/* A balance the way a ledger writes one: the figure, and which side it sits
   on. SHREE prints DB or CR beside it and never a minus sign. */
function drcr(v) { return fmt(Math.abs(num(v))) + ' ' + (num(v) >= 0 ? 'Dr' : 'Cr'); }

function today() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
    '-' + String(d.getDate()).padStart(2, '0');
}
function monthStart() { return today().slice(0, 8) + '01'; }

/** Which key was actually struck, when a modifier is held.
 *
 *  **Alt is Option on a Mac, and Option composes.** Option + 1 arrives as
 *  `e.key === '¡'`, Option + S as `'ß'`, Option + K as `'˚'` -- so every Alt
 *  shortcut in the app was dead there, the numbers on the buttons at the top
 *  of a screen included. `e.code` is the key's place on the board and says
 *  `Digit1` or `KeyS` on both platforms.
 *
 *  `e.key` is still preferred where it is already a plain letter or digit,
 *  so a keyboard that is not QWERTY answers to the letter its user typed
 *  rather than the one in that position on an American board.
 */
function keyStruck(e) {
  const k = (e.key || '').toLowerCase();
  if (/^[a-z0-9]$/.test(k)) return k;
  const c = e.code || '';
  if (/^Key[A-Z]$/.test(c)) return c.slice(3).toLowerCase();
  if (/^Digit[0-9]$/.test(c)) return c.slice(5);
  if (/^Numpad[0-9]$/.test(c)) return c.slice(6);
  return k;
}
/* Naming and laying out the months of an account is the server's job now --
   `api._months_between` and `month_label` draw them, so both views and the
   CSV agree about which months exist without the client counting them out a
   second time. */
function fyStart() {
  const d = new Date();
  const y = d.getMonth() >= 3 ? d.getFullYear() : d.getFullYear() - 1;
  return y + '-04-01';
}
/** A firm in a dropdown: its code first. The two GST firms' names differ
 *  only after "BALKRISHANA STEEL T…" / "BALKRISHNA STEEL I…", so a box cut
 *  short showed the same word for both (29 Sep 2026). */
function firmLabel(f) {
  const code = String(f.code || '').trim();
  return code && code.toUpperCase() !== String(f.name || '').trim().toUpperCase()
    ? code + ' — ' + f.name : f.name;
}
/** Bill number as the user reads it: the series box, then the number. */
function docNo(inv) {
  const sr = (inv.series || '').trim();
  return sr ? sr + ' ' + inv.invoice_no : inv.invoice_no;
}

/** The list a document of this type goes back to: a GST firm's purchase to
 *  Purchases, which reads both firms (6 Oct 2026); the rest to their own. */
function listFor(type) {
  return type === 'PURCHASE' && !noTax() ? '#/purchases' : '#/invoices?type=' + type;
}

/** Indian financial year label for a yyyy-mm-dd date: '2025-26'. */
function fyLabel(d) {
  const y = parseInt(d.slice(0, 4), 10);
  const m = parseInt(d.slice(5, 7), 10);
  const start = m >= 4 ? y : y - 1;
  return start + '-' + String((start + 1) % 100).padStart(2, '0');
}
function dt(s) {
  if (!s) return '';
  const p = String(s).split('-');
  return p.length === 3 ? p[2] + '-' + p[1] + '-' + p[0] : s;
}

const ONES = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight',
  'Nine', 'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen',
  'Seventeen', 'Eighteen', 'Nineteen'];
const TENS = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy',
  'Eighty', 'Ninety'];
function under100(n) {
  if (n < 20) return ONES[n];
  return TENS[Math.floor(n / 10)] + (n % 10 ? ' ' + ONES[n % 10] : '');
}
function under1000(n) {
  if (n < 100) return under100(n);
  return ONES[Math.floor(n / 100)] + ' Hundred' +
    (n % 100 ? ' ' + under100(n % 100) : '');
}
/* Indian system: crore / lakh / thousand */
function words(v) {
  let n = Math.floor(Math.abs(num(v)));
  const paise = Math.round((Math.abs(num(v)) - n) * 100);
  if (n === 0 && !paise) return 'Zero Only';
  const parts = [];
  const crore = Math.floor(n / 10000000); n %= 10000000;
  const lakh = Math.floor(n / 100000); n %= 100000;
  const thou = Math.floor(n / 1000); n %= 1000;
  if (crore) parts.push(under1000(crore) + ' Crore');
  if (lakh) parts.push(under1000(lakh) + ' Lakh');
  if (thou) parts.push(under1000(thou) + ' Thousand');
  if (n) parts.push(under1000(n));
  let out = parts.join(' ').trim();
  if (paise) out += ' and ' + under100(paise) + ' Paise';
  return (num(v) < 0 ? 'Minus ' : '') + out + ' Only';
}

/* ================================================================== api */
/* The page holds the JavaScript it was loaded with, and this app is left open
   all day -- so a change to the files can sit there for hours without ever
   reaching the screen, which looks exactly like a change that did not work.
   Every answer from the server carries the stamp of its own files; the first
   one is kept, and when it stops matching the page says so and offers to
   fetch itself again. Nothing reloads on its own: a half-typed bill is worth
   more than being up to date.                                              */
let appBuild = null;

function noteBuild(res) {
  const stamp = res.headers.get('X-App-Build');
  if (!stamp) return;
  if (appBuild === null) { appBuild = stamp; return; }
  if (stamp === appBuild || $('#update-bar')) return;
  const bar = document.createElement('div');
  bar.id = 'update-bar';
  bar.innerHTML = 'The app has been updated since this page was opened. '
    + '<button class="link" id="update-now">Load the new version</button>'
    + ' <span class="kbd">F5</span>';
  document.body.appendChild(bar);
  $('#update-now').onclick = () => location.reload();
}

/* ------------------------------------------------- updated from SHREE ---
   The books are updated from SHREE when Update now is pressed on the SHREE
   update screen -- perhaps in another window. Every row keeps its id
   through an update, so what a page holds stays good -- but it is behind:
   a list lacks the day's new bills, a dropdown the new customer. Every answer says when the books were last
   updated. A page takes a newer update in by itself, the moment doing so
   would lose nothing; while an entry is half typed it waits, and the entry
   can still be saved (the server stops only a save that touches something
   the update changed).                                                    */
let booksSeen = null;          // the update this page's data comes from
let booksNew = null;           // a newer one, not taken in yet
let booksTaking = false;
let lastInput = Date.now();
['keydown', 'pointerdown', 'input'].forEach(ev =>
  addEventListener(ev, () => { lastInput = Date.now(); }, true));

function noteBooks(res) {
  const at = res.headers.get('X-Books-Updated') || '';
  if (booksSeen === null) { booksSeen = at; return; }
  if (at !== booksSeen && at !== booksNew) {
    booksNew = at;
    setTimeout(takeBooks, 0);
  }
}

/** "2026-09-16 14:05:09" the way the screen says it. */
function stampText(s) {
  if (!s) return '';
  const d = String(s).slice(0, 10), t = String(s).slice(11, 16);
  return (d === today() ? 'today' : dt(d)) + (t ? ' at ' + t : '');
}

/** Bring the page up to date with the books. `now` is a screen being drawn
 *  afresh anyway; otherwise only when nothing on it would be lost. */
async function takeBooks(now) {
  if (!booksNew || booksTaking || lockShown) return;
  // Half-typed work, an open dialog, or somebody busy this very moment.
  const held = KB.unsavedWork() || $('#modal-root').children.length;
  if (!now && (held || Date.now() - lastInput < 15000)) {
    if (held) booksBar('behind', booksNew);
    return;
  }
  booksTaking = true;
  const at = booksNew;
  try {
    invalidate();
    await loadMeta(true);
    booksSeen = at;
    if (booksNew === at) booksNew = null;
    booksBar(null);
    if (!now) {
      await render();
      toast('Brought up to date with SHREE.', 'ok');
    }
  } catch (e) { /* tried again shortly */ } finally {
    booksTaking = false;
  }
}

function booksBar(state, at) {
  let bar = $('#books-bar');
  if (!state) { if (bar) bar.remove(); return; }
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'books-bar';
    bar.className = 'noprint';
    document.body.appendChild(bar);
  }
  bar.dataset.state = state;
  if (state === 'behind') {
    bar.innerHTML = 'New entries came in from SHREE ' + esc(stampText(at))
      + '. This entry can still be saved; the screen catches up once it is '
      + 'saved or closed.';
  } else if (state === 'stale') {
    bar.innerHTML = 'Something on this page changed in SHREE after it was '
      + 'opened. <button class="link" id="books-reload">Reload now</button>'
      + ' <span class="kbd">F5</span>';
    $('#books-reload').onclick = () => location.reload();
  } else {
    bar.innerHTML = 'The books are being updated from SHREE and did not take '
      + 'this save. <a class="link" href="#/sync">See how it is going</a>';
  }
}

// An idle page still hears of updates, and a waiting one takes them in --
// also behind SHREE's own window, so it is current when it is looked at. A
// browser slows timers down in a window that is not in front, so coming
// back to the window asks at once.
function askBooks() {
  if (lockShown || booksSeen === null) return;
  api.get('/api/sync/status').catch(() => {});
}
setInterval(askBooks, 30000);
setInterval(() => { if (booksNew) takeBooks(); }, 10000);
document.addEventListener('visibilitychange', askBooks);
addEventListener('focus', askBooks);

/* A save sent while an update is running is held by the server until the
   update is in -- a few seconds, now and then longer. Say so, rather than
   leave a button that seems to do nothing. */
async function saveWaitNote(done) {
  await new Promise(r => setTimeout(r, 1200));
  if (done.settled) return;
  let st = null;
  try { st = await send('GET', '/api/sync/status'); } catch (e) { return; }
  if (done.settled || !st || !st.running || st.mode !== 'apply') return;
  const t = el('<div class="toast">Saving — waiting for the update from SHREE '
    + 'to finish…</div>');
  $('#toast').appendChild(t);
  done.then(() => t.remove(), () => t.remove());
}

/* Two clicks on Save used to be two requests; while a save is held they
   would both go in once the update was done. The same request sent again
   before the first has answered is the first one. */
const writesInFlight = {};

async function req(method, url, body) {
  if (method === 'GET') return send(method, url, body);
  const key = method + ' ' + url + ' ' + (body === undefined ? '' : JSON.stringify(body));
  if (writesInFlight[key]) return writesInFlight[key];
  const p = deleteGuarded(method, url, body) ? sendWithDeletePassword(method, url, body)
    : send(method, url, body);
  p.settled = false;
  p.then(() => { p.settled = true; }, () => { p.settled = true; });
  writesInFlight[key] = p;
  saveWaitNote(p);
  try {
    return await p;
  } finally {
    delete writesInFlight[key];
  }
}

/* ------------------------------------------------- the delete password --
   4 Oct 2026: "To delete a challan or bill or an account the user has to
   enter a password and then only they can delete it." Every delete of a
   document (challan, bill, purchase, note), of an account, of ticked rows
   holding either, or of an order taking its challan along, asks for it --
   after the delete has been confirmed, so the question is the last step.
   The server checks it too (server._delete_gate). It is kept in memory
   only for the follow-up of the same delete (a challan whose bill must go
   too is sent twice) and for a minute at most; then it is asked again. */
let delPass = null;                 // { pw, at }
const DELPASS_MS = 60000;

function deleteGuarded(method, url, body) {
  const path = url.split('?')[0];
  if (method === 'DELETE') {
    if (/^\/api\/(invoices|ledgers)\/\d+$/.test(path)) return true;
    return /^\/api\/orders\/\d+$/.test(path) && /[?&]cascade=1/.test(url);
  }
  // The old years, deleted (Settings).
  if (method === 'POST' && path === '/api/books/drop-years' && body && !body.check) return true;
  // Fetch everything, when entries made here go with it (6 Oct 2026).
  if (method === 'POST' && path === '/api/sync/run' && body && body.everything && body.wipe) return true;
  if (method === 'POST' && path === '/api/bulk-delete' && body && !body.check) {
    const kinds = (body.items || []).map(x => x.kind);
    return kinds.some(k => k === 'invoice' || k === 'ledger')
      || (kinds.indexOf('order') >= 0 && !!body.cascade);
  }
  return false;
}

async function sendWithDeletePassword(method, url, body) {
  for (;;) {
    let pw = delPass && Date.now() - delPass.at < DELPASS_MS ? delPass.pw : null;
    if (!pw) {
      pw = await askDeletePassword();
      if (pw === null) {
        const e = new Error('Nothing was deleted — the delete password was not given.');
        e.status = 0;
        e.data = {};
        e.cancelled = true;
        throw e;
      }
      delPass = { pw, at: Date.now() };
    }
    try {
      const r = await send(method, url, body, { 'X-Delete-Password': encodeURIComponent(pw) });
      delPass = null;              // one delete, one password
      return r;
    } catch (e) {
      const why = (e.data || {}).delete_password;
      // Wrong, or none set yet: ask again -- the question offers to choose
      // one when there is none.
      if (why === 'wrong' || why === 'missing' || why === 'unset') {
        delPass = null;
        if (why === 'wrong') bad(e.message);
        continue;
      }
      // Only a delete blocked to be asked again (take the bill along?)
      // keeps it for its follow-up.
      if (e.status !== 409) delPass = null;
      throw e;
    }
  }
}

/** The delete password, asked for; null when the question is closed. With
 *  none chosen yet, chooses one first. */
async function askDeletePassword() {
  let st;
  try { st = await send('GET', '/api/delete-password'); } catch (e) { bad(e.message); return null; }
  if (!st.set) return chooseDeletePassword(st);
  let pw = null;
  const go = await modal('Delete password', `
    <p style="margin:0 0 12px;font-size:14px">Deleting a challan, a bill or an account
      needs the delete password.</p>
    <label for="dp-pw">Delete password</label>
    <input type="password" id="dp-pw" autocomplete="off" spellcheck="false">`, [
    { label: 'Cancel', value: false },
    { label: 'Delete', value: true, cls: 'danger', before: bg => {
        pw = $('#dp-pw', bg).value;
        if (!pw) { bad('Type the delete password.'); $('#dp-pw', bg).focus(); return false; }
      } }
  ], { nothingToSave: true, onOpen: passwordKeys });
  return go ? pw : null;
}

/** Enter in a password box presses the dialog's last button rather than
 *  walking to the next box -- the last box, that is. */
function passwordKeys(bg) {
  const boxes = $$('input[type=password]', bg);
  boxes.forEach((b, i) => b.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    e.stopPropagation();
    if (i < boxes.length - 1) { boxes[i + 1].focus(); return; }
    const btns = $$('.ft button', bg);
    btns[btns.length - 1].click();
  }, true));
  setTimeout(() => boxes[0] && boxes[0].focus(), 30);
}

/** The first delete password, chosen here -- at the computer Billing runs
 *  on. Resolves to it, or null. */
async function chooseDeletePassword(st) {
  if (!st.here) {
    await modal('Delete password', `<p style="margin:0;font-size:14px">Deleting a challan,
      a bill or an account needs a delete password, and none has been chosen yet.
      Choose it on the computer Billing runs on: try the delete there, or open
      Settings.</p>`);
    return null;
  }
  let pw = null;
  const go = await modal('Choose a delete password', `
    <p style="margin:0 0 12px;font-size:14px">From now on, deleting a challan, a bill
      or an account asks for this password. Keep it to yourself.</p>
    <div class="grid g2">
      <div><label for="dp-new">Delete password</label>
        <input type="password" id="dp-new" autocomplete="new-password" spellcheck="false"></div>
      <div><label for="dp-new2">The same again</label>
        <input type="password" id="dp-new2" autocomplete="new-password" spellcheck="false"></div>
    </div>
    <p class="hint" style="margin:10px 0 0">${st.min} characters or more. Forgotten, it can
      be chosen again at this computer: double-click <b>delete-password.bat</b>
      (or run <code>billing.py --set-delete-password</code>).</p>`, [
    { label: 'Cancel', value: false },
    { label: 'Set password and delete', value: true, cls: 'danger', before: async bg => {
        const a = $('#dp-new', bg).value, b = $('#dp-new2', bg).value;
        if (a.trim().length < st.min) { bad('At least ' + st.min + ' characters.'); return false; }
        if (a !== b) { bad('The two do not match.'); return false; }
        try { await send('POST', '/api/delete-password', { new: a }); }
        catch (e) { bad(e.message); return false; }
        pw = a;
      } }
  ], { nothingToSave: true, onOpen: passwordKeys });
  return go ? pw : null;
}

/* **Each window keeps its own firm** (found 5 Oct 2026). The server
   remembers the firm switched to last, from whichever window switched it, so
   with Billing open twice -- the icon clicked again, another computer -- a
   switch in one moved the other onto the wrong books under its own firm's
   name: its lists showed the other firm, and a bill opened there said
   "Document not found". Every request now says which firm this page is on
   (X-Firm), and a download names it in its address (`firmUrl`), since a
   page or a file being opened cannot carry the header. */
let firmHere = null;

/** An /api/ address opened as a page or a download, in this page's firm. */
function firmUrl(u) {
  if (!firmHere || typeof u !== 'string' || !/^\/api\//.test(u)
      || /[?&]company=/.test(u)) return u;
  return u + (u.indexOf('?') < 0 ? '?' : '&') + 'company=' + firmHere;
}
// A plain link to a download is given the firm as it is followed.
document.addEventListener('click', e => {
  const a = e.target && e.target.closest && e.target.closest('a[href^="/api/"]');
  if (a) a.setAttribute('href', firmUrl(a.getAttribute('href')));
}, true);

async function send(method, url, body, extra) {
  const opt = { method, headers: Object.assign({}, extra || {}) };
  if (method !== 'GET' && booksSeen !== null) opt.headers['X-Books-Seen'] = booksSeen;
  if (firmHere) opt.headers['X-Firm'] = String(firmHere);
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(url, opt);
  noteBuild(res);
  noteBooks(res);
  // Switched here: this page is on that firm from now on.
  const switched = method === 'POST' && res.ok
    && /^\/api\/companies\/(\d+)\/activate$/.exec(url.split('?')[0]);
  if (switched) firmHere = Number(switched[1]);
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { error: text }; }
  if (!res.ok) {
    // A 401 means the session ran out or was never given. Whatever the screen
    // was doing stops there and the lock takes over -- carrying on would show
    // half a page of errors from every call that follows.
    if (res.status === 401 && data && data.locked) {
      lockScreen(!!data.passcode_set);
      const stop = new Error('Locked.');
      stop.locked = true;
      throw stop;
    }
    if (res.status === 503 && data && data.syncing) booksBar('running');
    if (res.status === 409 && data && data.books_updated) booksBar('stale');
    // The body travels with the error: a blocked delete uses it to offer the
    // follow-up that would unblock it.
    const err = new Error((data && data.error) || ('HTTP ' + res.status));
    err.status = res.status;
    err.data = data || {};
    throw err;
  }
  return data;
}

/* ------------------------------------------------------------------ lock
   The passcode screen. It replaces the whole app rather than sitting over
   it, so nothing behind it can be read off the page while it is up.        */

let lockShown = false;

function lockScreen(passcodeSet) {
  if (lockShown) return;
  lockShown = true;
  const setting = !passcodeSet;
  document.getElementById('app').innerHTML = `
    <div class="lock-wrap">
      <form class="lock" id="lockform">
        <h1>${setting ? 'Choose a passcode' : 'Billing'}</h1>
        <p>${setting
          ? 'This is what will open the books from now on. Six characters or more, and it cannot be recovered \u2014 write it somewhere safe.'
          : 'Enter the passcode to open the books.'}</p>
        <input type="password" id="pc" autocomplete="${setting ? 'new-password' : 'current-password'}"
          placeholder="Passcode" autofocus>
        ${setting ? '<input type="password" id="pc2" placeholder="Repeat it" autocomplete="new-password">' : ''}
        <button class="primary" type="submit">${setting ? 'Set passcode' : 'Unlock'}</button>
        <div class="lock-msg" id="lockmsg"></div>
      </form>
    </div>`;
  const msg = t => { document.getElementById('lockmsg').textContent = t || ''; };
  document.getElementById('lockform').onsubmit = async e => {
    e.preventDefault();
    const pc = document.getElementById('pc').value;
    const two = document.getElementById('pc2');
    if (setting && pc !== (two ? two.value : '')) return msg('The two do not match.');
    msg('');
    try {
      const res = await fetch('/api/session', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(setting ? { new: pc } : { passcode: pc })
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) return msg(d.error || 'That did not work.');
      location.reload();
    } catch (err) { msg(String(err.message || err)); }
  };
  document.getElementById('pc').focus();
}
const api = {
  get: (u, q) => req('GET', u + (q ? '?' + new URLSearchParams(
    Object.entries(q).filter(([, v]) => v !== '' && v != null)) : '')),
  post: (u, b) => req('POST', u, b),
  put: (u, b) => req('PUT', u, b),
  del: u => req('DELETE', u)
};

/* =============================================================== toasts */
let toastTimer = [];
function toast(msg, kind) {
  const t = el('<div class="toast ' + (kind || '') + '">' + esc(msg) + '</div>');
  $('#toast').appendChild(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 6000 : 3000);
}
const ok = m => toast(m, 'ok');
const bad = m => toast(m, 'bad');

/* **Nothing fails without a word** (29 Sep 2026). A button whose work threw
   -- a delete refused, a save the server turned down, the server not
   running -- did nothing at all on screen when its handler had no catch of
   its own: the Delete on a journal was one. Whatever reaches the top is said
   now, the server's own message when there is one. */
(function sayUncaught() {
  let last = '', at = 0;
  function say(err, fromScript) {
    if (!err || err.locked || err.name === 'AbortError') return;
    let m = String((err && err.message) || err || '').trim();
    if (!m) return;
    if (/Failed to fetch|NetworkError|Load failed/i.test(m)) {
      m = 'Billing is not answering. Check it is running, then try again.';
    } else if (fromScript || !err.status) {
      m = 'Something went wrong on this screen: ' + m;
    }
    if (m === last && Date.now() - at < 4000) return;
    last = m; at = Date.now();
    try { bad(m); } catch (e) { /* the page is going */ }
  }
  window.addEventListener('unhandledrejection', e => say(e.reason, false));
  window.addEventListener('error', e => {
    if (e.filename && !/\/app\.js/.test(e.filename)) return;
    say(e.error || e.message, true);
  });
})();

/* =============================================================== modals */
/** The boxes a dialog's Enter walks, in the order they are read. A group of
 *  radio buttons is one stop -- the one ticked, or the first -- since the
 *  arrow keys already move between them. */
function dialogBoxes(scope) {
  const seen = {};
  return boxesIn(scope).filter(x => {
    if (x.type !== 'radio') return true;
    const group = x.name || x.id;
    if (seen[group]) return false;
    const ticked = $$('input[type=radio]', scope).find(r =>
      (r.name || r.id) === group && r.checked);
    if (ticked && ticked !== x) return false;
    seen[group] = true;
    return true;
  });
}

/** A dialog that is a form -- two or more boxes to type into -- as opposed to
 *  a question, a list to pick from, or a notice. Enter walks a form box by
 *  box, the way it walks every entry screen; a question answers to Enter at
 *  once. */
function isFormDialog(bg) {
  return boxesIn($('.bd', bg)).filter(x =>
    x.type !== 'checkbox' && x.type !== 'radio').length >= 2;
}

function modal(title, bodyHtml, buttons, opts) {
  const came = document.activeElement;
  opts = opts || {};
  return new Promise(resolve => {
    const bg = el('<div class="modal-bg"><div class="modal' +
      (opts.wide ? ' wide' : '') + '"><h2>' + esc(title) +
      '<button class="sm" data-x="1" tabindex="-1" title="Close (Esc)">✕</button></h2>' +
      '<div class="bd"></div><div class="ft"></div></div></div>');
    $('.bd', bg).innerHTML = bodyHtml;
    const ft = $('.ft', bg);
    const list = buttons || [{ label: 'Close', value: null }];
    // No key of its own on any of them (23 Sep 2026): the arrows walk to a
    // button and Enter presses it, the same in every dialog.
    list.forEach(b => {
      const btn = el('<button class="' + (b.cls || '') + '">' + esc(b.label) + '</button>');
      btn.onclick = async () => {
        if (b.before) { const r = await b.before(bg); if (r === false) return; }
        close(b.value);
      };
      ft.appendChild(btn);
    });
    // Something typed into a form is work. Esc still closes it, but asks
    // first -- the same question leaving a half-typed entry screen asks.
    let typed = false;
    bg.addEventListener('input', () => { typed = true; }, true);
    let asking = false;
    async function escape() {
      if (asking) return;
      // `opts.nothingToSave`: a dialog whose boxes only choose what to make
      // (the dates of a statement) holds no work, and Esc just closes it.
      if (typed && !opts.nothingToSave && isFormDialog(bg)
          && $('.ft button.primary', bg)) {
        asking = true;
        const leave = await confirmBox('Close without saving',
          'What was typed here has not been saved. Close it anyway?');
        asking = false;
        if (!leave) return;
      }
      close(null);
    }
    function close(v) {
      bg.remove();
      document.removeEventListener('keydown', key);
      window.removeEventListener('hashchange', leave);
      // Put the focus back where the dialog took it from. Without this it
      // falls to the page body, and Tab starts again from the top of the
      // window instead of carrying on from the row that was being worked.
      // A list row is a special case: the list behind the dialog often
      // reloads itself once the dialog answers, so the row that was focused
      // is a different element by the time it comes back. KB finds it.
      // A customer or product box given its focus back this way does not
      // open its list: Cancel on "Leave without saving?" put the whole
      // customer list up over the bill it had just kept (24 Sep 2026, once
      // Esc became the way back).
      if (came && came.tagName === 'TR') KB.focusCursor();
      else if (came && came.isConnected) {
        try { KB.handBack(came); } catch (err) { /* gone */ }
      }
      resolve(v);
    }
    function key(e) {
      // Only the dialog on top answers. A product list opened over a voucher
      // must not take the voucher down with it when it is escaped, and the
      // Enter that answers the list is not the Enter that saves the form.
      if ($('#modal-root').lastChild !== bg) return;
      // A lookup list open inside the dialog takes Esc for itself and marks
      // it so; only an Esc nothing smaller answered closes the dialog.
      if (e.key === 'Escape') { if (!e.defaultPrevented) escape(); return; }
      if (!bg.contains(e.target) && e.target !== document.body) return;
      const usable = b => b && !b.disabled && b.offsetParent !== null;
      const main = $('.ft button.primary', bg);
      // Ctrl+S saves from anywhere in the dialog -- the same Ctrl+S that
      // saves an entry screen.
      if ((e.ctrlKey || e.metaKey) && !e.altKey && keyStruck(e) === 's') {
        e.preventDefault();
        if (usable(main)) main.click();
        return;
      }
      const t = e.target;
      /* Enter. A question answers to it at once -- the main button, and only
         that; a dialog asking whether to delete something has no main button
         (its first is Cancel), so Enter cannot destroy anything by being
         leant on.

         **A form walks.** The receipt, the party, the item: Enter goes on to
         the next box, exactly as it does on every entry screen, and past the
         last box it stands on the main button without pressing it. One more
         Enter saves. It used to save from whichever box it was pressed in,
         half-typed or not. */
      if (e.key === 'Enter' && !e.defaultPrevented && !e.ctrlKey && !e.metaKey
          && !e.altKey) {
        if (t && (t.tagName === 'BUTTON' || t.tagName === 'A'
                  || t.tagName === 'SUMMARY')) return;
        if (t && t.matches && t.matches('input,select,textarea')
            && $('.pick-list')) return;
        if (t && t.tagName === 'TEXTAREA' && e.shiftKey) return;   // a new line
        if (isFormDialog(bg) && t && bg.contains(t)
            && t.matches('input,select,textarea')) {
          const boxes = dialogBoxes($('.bd', bg));
          const i = boxes.indexOf(t);
          if (i >= 0) {
            e.preventDefault();
            const next = boxes[i + (e.shiftKey ? -1 : 1)];
            if (next) { next.focus(); if (next.select) next.select(); }
            else if (!e.shiftKey && usable(main)) main.focus();
            return;
          }
        }
        if (t && t.tagName === 'TEXTAREA') return;
        if (usable(main)) { e.preventDefault(); main.click(); }
        return;
      }
      // The arrows are the keyboard module's: they walk to the box, tick
      // box or button in that direction, here as on every screen.
      // Tab is kept inside the dialog. Without this it walks out onto the
      // page behind, which cannot be seen and cannot be got back from.
      if (e.key === 'Tab') {
        const f = $$('a[href],button,input,select,textarea,[tabindex]', bg)
          .filter(x => !x.disabled && x.tabIndex !== -1 && x.offsetParent !== null);
        if (!f.length) return;
        const at = f.indexOf(document.activeElement);
        if (e.shiftKey && at <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && at === f.length - 1) { e.preventDefault(); f[0].focus(); }
      }
    }
    function leave() { close(null); }
    $('[data-x]', bg).onclick = () => close(null);
    bg.onclick = e => { if (e.target === bg) close(null); };
    document.addEventListener('keydown', key);
    window.addEventListener('hashchange', leave);
    $('#modal-root').appendChild(bg);
    // Something is always focused, boxes or not, so the keyboard has
    // somewhere to be. Focusing without scrolling matters on a long dialog:
    // the buttons are at the foot, and letting the focus scroll to them
    // opens the dialog at its own bottom.
    //
    // Looked for once the dialog has been filled in, and only if the dialog
    // has not already put the focus somewhere itself -- the product picker
    // stands on its first Choose button, and taking that away to land on
    // Apply is how a dialog comes to be answered before it was read. The
    // first box that can be typed in: a disabled or read-only one (the type
    // and number of a receipt being edited) is walked past.
    if (opts.onOpen) opts.onOpen(bg, close);
    setTimeout(() => {
      if (!bg.isConnected) return;
      const now = document.activeElement;
      if (now && now !== document.body && bg.contains(now)) return;
      // A yes-or-no question starts on Cancel -- → then Enter says yes -- so
      // an Enter leant on cannot delete anything.
      const first = (opts.yesNo ? $('.ft button', bg) : boxesIn($('.bd', bg))[0])
        || $('.ft button.primary', bg) || $('.ft button', bg);
      if (first) first.focus({ preventScroll: true });
      bg.scrollTop = 0;
    }, 40);
  });
}
function confirmBox(title, msg) {
  return modal(title, '<p style="margin:0;font-size:14px">' + esc(msg) + '</p>', [
    { label: 'Cancel', value: false },
    { label: 'Yes, continue', value: true, cls: 'danger' }
  ], { yesNo: true });
}

/** Leaving an entry that has not been saved: stay, leave it as it is, or --
 *  asked for on 5 Oct 2026, "add an option to also save change over here as
 *  well" -- save it and then go. Resolves 'stay', 'leave' or 'save'. It opens
 *  on Cancel, so an Enter leant on changes nothing; → is Don't save, as Yes
 *  was before, and → again is Save. Save is offered where the screen has one
 *  Save for the whole entry (a challan, a bill, a purchase, an order, a
 *  journal) -- not on Settings, whose cards each save on their own. */
async function askLeave(question, saveLabel) {
  const buttons = [{ label: 'Cancel', value: 'stay' },
                   { label: 'Don\u2019t save', value: 'leave', cls: 'danger' }];
  if (KB.canSaveEntry()) {
    buttons.push({ label: saveLabel || 'Save and go back', value: 'save', cls: 'primary' });
  }
  const got = await modal('Leave without saving', '<p style="margin:0;font-size:14px">'
    + 'This entry has not been saved yet. ' + esc(question) + '</p>', buttons, { yesNo: true });
  return got || 'stay';
}

/** Ask, then `go`: at once, or once the entry has been saved -- a save that
 *  is refused (a box left empty) stays on the entry, saying why. Answers
 *  whether it went (or will). */
async function leaveAsking(question, saveLabel, go) {
  const how = await askLeave(question, saveLabel);
  if (how === 'leave') { go(); return true; }
  if (how === 'save') return KB.saveThen(go);
  return false;
}

/** A lookup box that opens the whole list by itself.
 *
 *  Typing into the box finds a party or a product you can already name. The
 *  list is for the other case, and it is the commoner one: two thousand
 *  customers and four hundred products, and the one being billed known by
 *  sight rather than by spelling. Reaching for it used to mean the mouse and
 *  the small arrow beside the box.
 *
 *  So the box opens it as soon as the box is reached -- but only while it is
 *  still **empty**, which is exactly when something has to be picked and
 *  never when you are only passing through. A line that already names a
 *  product is walked past in silence; to change one, click the box or press
 *  F4, which is what F4 has dropped a list open with on this desk for thirty
 *  years.
 *
 *  `browse` is the same thing the arrow button does, so the two ways in can
 *  never come apart.
 */
/** Wire a box to the browse dialog behind it.
 *
 *  `always` makes reaching the box open that dialog whether or not something
 *  is already in it. Only the product box on a document asks for it: see the
 *  note where it is wired up.
 */
function opensPicker(input, browse, picker, always) {
  // The box has two lists behind it and only one may answer the focus.
  if (picker) picker.focusOpens = false;
  let busy = false;
  let offered = false;              // the list has been put up since arriving
  async function open(how) {
    offered = true;
    // The dialog hands the focus back to this very box as it closes, which
    // would open it again and again. It cannot, while this is still set:
    // the focus goes back before the promise below resolves.
    if (busy) return null;
    busy = true;
    try { return await browse(how); } finally { busy = false; }
  }

  /* **Typing opens it too** (28 Sep 2026, over the Select customer window:
     "whenever I start typing in the search bar this should automatically
     open. Check this everywhere in the software"). The small list that used
     to drop down under the box is never drawn; the first letter opens the
     window with that letter in its own search box, and the typing carries
     on there -- letters typed while it opens are picked up as it opens
     (`term` is read then, not before). Cancelled, what was typed stays in
     the box, as it did when the small list was the way. */
  let putting = false;
  input.addEventListener('input', () => {
    if (putting) return;
    if (picker) picker.close();
    if (busy || !input.value.trim()) return;
    typedOpen();
  });
  async function typedOpen() {
    const got = await open({ typed: true });
    if (got || !input.isConnected || pickLast.term === input.value) return;
    putting = true;
    try {
      input.value = pickLast.term;
      // Whatever reads the box -- a line's description, "a name typed over
      // the one picked no longer names that account" -- hears it as typing.
      input.dispatchEvent(new Event('input', { bubbles: true }));
    } finally { putting = false; }
    if (picker) picker.close();
  }
  // Reaching an empty one of these opens the whole list, which is what makes
  // them quick -- and is also why a screen must never be *landed* on an
  // empty one. It is marked so `focusEntry` can step over it; one that opens
  // whether or not it is filled (a bill line's product) says so.
  input.dataset.opensPicker = always ? 'always' : '1';
  // Walked onto with Enter or Tab, the list opens; passed over with the
  // arrows it does not -- ↓ down a column of product names would otherwise
  // open a dialog on every line. Arrived at by an arrow, the first Enter on
  // an empty one opens it (it has to be picked); after that Enter walks on
  // as it always did. F4 and the ▾ beside it, which → reaches, open it too.
  input.addEventListener('focus', () => {
    if (KB.returning()) return;         // a dialog handing the focus back
    if (KB.arriving()) { offered = false; return; }
    if (always || !input.value.trim()) open();
  });
  input.addEventListener('click', () => open());
  input.addEventListener('keydown', e => {
    if (e.altKey || e.ctrlKey || e.metaKey || e.defaultPrevented) return;
    if (e.key === 'F4'
        || (e.key === 'Enter' && !e.shiftKey && !input.value.trim() && !offered)) {
      e.preventDefault();
      open();
    }
  });
  return open;
}

/** The search a pick window last held, so a window opened by typing and
 *  then cancelled can leave what was typed in the box it came from. */
const pickLast = { term: '' };
/** A pick window's starting search: `term` may be a function, read as the
 *  window opens, so letters typed while it was loading are not lost. */
const pickTerm = o => String((typeof o.term === 'function' ? o.term() : o.term) || '')
  .replace(/^\s+/, '');

/* ============================================================ typeahead */
/** Attach a search-as-you-type dropdown to an input.
 *  The list is rendered in a fixed layer on <body> so it is never clipped by a
 *  scrolling table or a modal. */
function attachPicker(input, opts) {
  let list = null, rows = [], hi = -1;

  function closeList(redrawing) {
    if (list) { list.remove(); list = null; }
    hi = -1;
    // A search still waiting to run is not wanted once the list is put away
    // -- but a list only being drawn afresh keeps it: that search is for the
    // letter typed after this answer was asked for.
    if (redrawing !== true) clearTimeout(timer);
    window.removeEventListener('scroll', onScroll, true);
  }
  // The list's own scrolling -- the arrows walking down past its fourth
  // name -- is not the page moving, and used to close it under the key
  // (26 Sep 2026). The page moving takes the list along with its box, and
  // only closes it once the box itself has gone out of sight.
  function onScroll(e) {
    if (!list || (e.target && e.target.nodeType === 1 && list.contains(e.target))) return;
    const r = input.getBoundingClientRect();
    const a = KB.clearArea(input);
    if (r.bottom < a.top || r.top > a.bottom || !input.isConnected) closeList();
    else place();
  }
  /** Under the box, or over it where there is more room -- and never down
   *  behind the keys along the foot of the window, where the name the
   *  arrows had reached was out of sight. */
  function place() {
    if (!list) return;
    const r = input.getBoundingClientRect();
    list.style.left = r.left + 'px';
    list.style.width = Math.max(r.width, 240) + 'px';
    const foot = KB.clearArea(input).bottom;
    const below = foot - r.bottom - 6;
    const above = r.top - 6;
    const cap = opts.maxHeight || 250;
    if (below < 190 && above > below) {
      list.style.top = 'auto';
      list.style.bottom = (window.innerHeight - r.top + 3) + 'px';
      list.style.maxHeight = Math.max(80, Math.min(cap, above)) + 'px';
    } else {
      list.style.bottom = 'auto';
      list.style.top = (r.bottom + 3) + 'px';
      list.style.maxHeight = Math.max(80, Math.min(cap, below)) + 'px';
    }
  }
  function draw() {
    closeList(true);
    if (!rows.length) return;
    list = el('<div class="pick-list' + (opts.listClass ? ' ' + opts.listClass : '')
      + '" style="position:fixed"></div>');
    rows.forEach((r, i) => {
      const d = el('<div>' + opts.render(r) + '</div>');
      d.onmousedown = e => { e.preventDefault(); choose(i); };
      list.appendChild(d);
    });
    document.body.appendChild(list);
    place();
    window.addEventListener('scroll', onScroll, true);
  }
  function choose(i) {
    const r = rows[i];
    closeList();
    if (r) opts.onPick(r);
  }
  async function search() {
    rows = await opts.source(input.value.trim());
    // Answered after the box was left -- a quick Enter took its choice and
    // went on to the next box -- the list would open under a box nobody is
    // in, and stay there over the line (26 Sep 2026).
    if (document.activeElement !== input) return;
    draw();
  }
  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(search, 130);
  });
  // Landing on the box used to drop this small list open by itself. On the
  // boxes that now open the full list instead -- customer, product -- that
  // would be two lists for one box, and the little one left showing behind
  // the big one when the big one is escaped. `focusOpens` turns it off there;
  // typing, and the down arrow, still bring it up.
  input.addEventListener('focus', () => {
    if (handle.focusOpens && !KB.arriving() && !KB.returning()) search();
  });
  input.addEventListener('blur', () => setTimeout(closeList, 120));
  input.addEventListener('keydown', e => {
    // With no list showing, the arrows are the keyboard module's: they move
    // to the next box. Typing brings the list up.
    if (!list) return;
    const items = $$('div', list);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      hi = Math.max(0, Math.min(items.length - 1, hi + (e.key === 'ArrowDown' ? 1 : -1)));
      items.forEach((n, i) => n.classList.toggle('hi', i === hi));
      items[hi].scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter' && e.shiftKey) {
      // Shift+Enter is the walk stepping back a box. Taken as a choice, it
      // picked the first name and went on to the box after -- forward --
      // from every steel box, whose list opens on landing (10 Oct 2026).
      closeList();
    } else if (e.key === 'Enter') {
      // Enter takes the highlighted row, or the first one -- the same
      // behaviour as the old software's lookup boxes. But the list also opens
      // just from landing on the box, and that is not a choice: with nothing
      // typed and nothing highlighted it closes and lets Enter move on.
      if (items.length && (hi >= 0 || input.value.trim())) {
        e.preventDefault();
        choose(hi >= 0 ? hi : 0);
      } else {
        closeList();
      }
    } else if (e.key === 'Escape') {
      // The list is what Esc closes; marked, so neither the dialog nor the
      // screen behind the box takes the same Esc as leaving.
      e.preventDefault();
      closeList();
    }
  });
  // Marked, so the keyboard module leaves this box's arrow keys to it.
  input.dataset.picker = '1';
  const handle = { refresh: search, close: closeList, isOpen: () => !!list,
                   focusOpens: true,
                   // The row ↓ ↑ have lit in this box's own list, if any.
                   lit: () => (list && hi >= 0 ? rows[hi] || null : null) };
  return handle;
}

/* ================================================================ dates
   **One date box, everywhere** (3 Oct 2026: "fix the date adjustment system
   in the entire software. Make it userfriendly.").

   The browser's own date box stood in 45 places, and it worked against the
   user four ways:
   - it wrote the date in the browser's language: 10/02/2026 for 2 October on
     an English (US) Edge, while every list, every print and SHREE say
     02-10-2026;
   - the arrows could not reach the day, month or year inside it -- they are
     the keyboard module's, and ← → left the box;
   - every part typed already made a date, so a list read itself again as
     the year went 0002, 0020, 0202, 2026;
   - a date half typed became no date at all, without a word.

   Each `<input type="date">` stays where it was, holding yyyy-mm-dd for the
   code that reads and saves it: its id, name, value and change event are
   the same as ever. It is only no longer shown. The box beside it shows and
   takes the date as dd-mm-yyyy, and reads what was typed when it is left --
   Enter, an arrow, a click elsewhere -- so a date is settled once:
     2              the 2nd of the month already in the box
     2-10  2/10     2 October, in the same financial year (SHREE's way)
     2-10-26        2 October 2026; 02.10.2026, 021026, 02102026, 2 Oct 26 too
   The day of the week shows under the box as it is typed; a day that is not
   one (31-02) is refused with the reason, and the box keeps its date.
   F4 -- the key that opens every list -- or the calendar at the box's end
   opens the month: ← → a day, ↑ ↓ a week, PgUp PgDn a month, Enter takes
   it, Esc puts it away. A From past its To takes the To along with it. */
const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];
const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
  'Saturday'];
const DATE_HOW = 'Type the date as dd-mm-yyyy, e.g. 02-10-2026 — or just 2 for the 2nd.';

const pad2 = n => String(n).padStart(2, '0');
const isoOf = (y, m, d) => y + '-' + pad2(m) + '-' + pad2(d);
function partsOf(iso) {
  const p = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
  return p ? [+p[1], +p[2], +p[3]] : null;
}
const daysIn = (y, m) => new Date(y, m, 0).getDate();
function addDays(iso, n) {
  const [y, m, d] = partsOf(iso);
  const t = new Date(y, m - 1, d + n);
  return isoOf(t.getFullYear(), t.getMonth() + 1, t.getDate());
}
function addMonths(iso, n) {
  const [y, m, d] = partsOf(iso);
  const t = new Date(y, m - 1 + n, 1);
  const yy = t.getFullYear(), mm = t.getMonth() + 1;
  return isoOf(yy, mm, Math.min(d, daysIn(yy, mm)));
}
/** "Fri, 2 Oct 2026" -- and the financial year, when it is not this one. */
function dayWord(iso) {
  const p = partsOf(iso);
  if (!p) return '';
  const w = WEEKDAYS[new Date(p[0], p[1] - 1, p[2]).getDay()].slice(0, 3);
  const fy = fyLabel(iso);
  return w + ', ' + p[2] + ' ' + MONTH_NAMES[p[1] - 1].slice(0, 3) + ' ' + p[0]
    + (fy !== fyLabel(today()) ? '  ·  year ' + fy : '');
}
/** Last month, first day to last. */
function lastMonth() {
  const [y, m] = partsOf(today());
  const t = new Date(y, m - 2, 1);
  const yy = t.getFullYear(), mm = t.getMonth() + 1;
  return { from: isoOf(yy, mm, 1), to: isoOf(yy, mm, daysIn(yy, mm)) };
}

/** What a typed date means: {iso} ('' for nothing typed) or {error}.
 *  Day first, always -- the Indian way, and SHREE's. What is left out is
 *  taken from `ref`, the date the box held: its month and year for a day
 *  alone, its financial year for a day and month. */
function readDate(text, ref) {
  const t = String(text == null ? '' : text).trim();
  if (!t) return { iso: '' };
  const base = partsOf(ref) || partsOf(today());
  let d, m, y, p;
  if ((p = /^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/.exec(t))) {      // 2026-10-02
    y = +p[1]; m = +p[2]; d = +p[3];
  } else if (/^\d+$/.test(t)) {
    if (t.length <= 2) d = +t;
    else if (t.length === 4 || t.length === 6 || t.length === 8) {
      d = +t.slice(0, 2); m = +t.slice(2, 4);
      if (t.length > 4) y = +t.slice(4);
    } else return { error: DATE_HOW };
  } else {
    const bits = t.split(/[\s\-/.,]+/).filter(Boolean);
    if (!bits.length || bits.length > 3 || !/^\d{1,2}$/.test(bits[0])) return { error: DATE_HOW };
    d = +bits[0];
    if (bits[1] !== undefined) {
      if (/^\d{1,2}$/.test(bits[1])) m = +bits[1];
      else {
        const w = bits[1].toLowerCase();
        const i = w.length >= 3 ? MONTH_NAMES.findIndex(n => n.toLowerCase().startsWith(w)) : -1;
        if (i < 0) return { error: DATE_HOW };
        m = i + 1;
      }
    }
    if (bits[2] !== undefined) {
      if (!/^(\d{2}|\d{4})$/.test(bits[2])) return { error: DATE_HOW };
      y = +bits[2];
    }
  }
  if (m === undefined) {
    m = base[1];
    y = base[0];
  } else if (y === undefined) {
    const fy = base[1] >= 4 ? base[0] : base[0] - 1;
    y = m >= 4 ? fy : fy + 1;
  } else if (y < 100) {
    y += 2000;
  }
  if (m < 1 || m > 12) return { error: 'There is no month ' + m + '. ' + DATE_HOW };
  if (y < 1990 || y > 2099) return { error: 'The year ' + y + ' is not one the books can hold.' };
  if (d < 1) return { error: DATE_HOW };
  if (d > daysIn(y, m)) {
    return { error: MONTH_NAMES[m - 1] + ' ' + y + ' has only ' + daysIn(y, m) + ' days.' };
  }
  return { iso: isoOf(y, m, d) };
}

const INPUT_VALUE = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
const CAL_ICON = '<svg viewBox="0 0 16 16" width="17" height="17" aria-hidden="true" '
  + 'fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round">'
  + '<rect x="1.7" y="2.7" width="12.6" height="11.6" rx="2"/>'
  + '<path d="M1.7 6.4h12.6M5 1.2v2.9M11 1.2v2.9"/></svg>';
const DATE_TIP = 'dd-mm-yyyy. 2 alone is the 2nd of this month, 2-10 is 2 October. '
  + 'F4 opens the calendar.';

/** The From box's To, or the To box's From: #f1/#f2, #st-f1/#st-f2. */
function datePartner(real) {
  const p = /^(.*)f([12])$/.exec(real.id || '');
  const other = p && document.getElementById(p[1] + 'f' + (p[2] === '1' ? '2' : '1'));
  return other && other._dateBox ? { other, isFrom: p[2] === '1' } : null;
}

/** Turn one `<input type="date">` into the date box described above. */
function dateBox(real) {
  if (real._dateBox || !real.isConnected) return;
  const wrap = el('<span class="datebox"><input class="dtbox" type="text" size="9" '
    + 'inputmode="numeric" autocomplete="off" spellcheck="false" maxlength="16" '
    + 'placeholder="dd-mm-yyyy"><button type="button" class="dtbtn" tabindex="-1" '
    + 'title="Calendar (F4)" aria-label="Calendar">' + CAL_ICON + '</button></span>');
  const box = wrap.firstElementChild, btn = wrap.lastElementChild;
  if (real.style.width) { wrap.style.width = real.style.width; real.style.width = ''; }
  real.classList.add('dt-real');
  real.after(wrap);

  const held = () => INPUT_VALUE.get.call(real);
  const show = () => {
    box.value = dt(held());
    box.classList.remove('wrong');
  };
  // Code that sets the date -- a Year chosen, an address restored, a
  // month stepped -- sets the hidden box, and this one shows it.
  Object.defineProperty(real, 'value', {
    configurable: true,
    get() { return INPUT_VALUE.get.call(this); },
    set(v) { INPUT_VALUE.set.call(this, v); show(); }
  });
  real.focus = o => box.focus(o);
  real.select = () => box.select();
  const mirror = () => {
    box.disabled = real.disabled;
    box.readOnly = real.readOnly;
    btn.disabled = real.disabled || real.readOnly;
    box.title = real.title || DATE_TIP;
  };
  mirror();
  new MutationObserver(mirror).observe(real,
    { attributes: true, attributeFilter: ['disabled', 'readonly', 'title'] });

  /** Take what is typed, or a day from the calendar. False, and nothing
   *  changed, when it is not a date. */
  function settle(iso) {
    if (iso === undefined) {
      const got = readDate(box.value, held() || today());
      if (got.error) return got.error;
      iso = got.iso;
    }
    if (iso !== held()) {
      const pair = datePartner(real);
      if (pair && iso && pair.other.value
          && (pair.isFrom ? iso > pair.other.value : iso < pair.other.value)) {
        pair.other.value = iso;
        toast((pair.isFrom ? 'To' : 'From') + ' is ' + dt(iso) + ' too, so the dates run forward.');
      }
      INPUT_VALUE.set.call(real, iso);
      show();
      real.dispatchEvent(new Event('input', { bubbles: true }));
      real.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      show();                     // "2" reads 02-10-2026 once it is settled
    }
    return '';
  }
  box._settle = settle;
  box._real = real;
  real._dateBox = { box, settle };

  // What is typed is this box's own business until it is settled: the
  // screen hears the hidden box change, once, as it always did.
  box.addEventListener('input', e => {
    e.stopPropagation();
    box.classList.remove('wrong');
    const got = readDate(box.value, held() || today());
    if (calFor(box) && got.iso) calMove(got.iso);
    dateSay(box, box.value.trim() ? (got.iso ? dayWord(got.iso) : 'dd-mm-yyyy') : '');
  });
  box.addEventListener('change', e => e.stopPropagation());
  box.addEventListener('keydown', e => {
    if (e.altKey || e.ctrlKey || e.metaKey || e.defaultPrevented) return;
    if (calFor(box)) { calKey(e); return; }
    if (e.key === 'F4') {
      e.preventDefault();
      calOpen(box);
    } else if (e.key === 'Enter') {
      // Settled before Enter goes on to the next box, or to Apply.
      const wrong = settle();
      if (wrong) {
        e.preventDefault();
        box.classList.add('wrong');
        dateSay(box, wrong, true);
      } else {
        dateSay(box, '');
      }
    } else if (e.key === 'Escape' && box.classList.contains('wrong')) {
      e.preventDefault();          // Esc puts back the date it had
      show();
      dateSay(box, '');
    }
  });
  box.addEventListener('blur', () => {
    calClose(box);
    dateSay(box, '');
    if (!box.isConnected) return;
    if (settle()) {
      const typed = box.value.trim();
      show();
      bad('“' + typed + '” is not a date. ' + (held()
        ? 'The box keeps ' + dt(held()) + '.' : 'The box is left empty.'));
    }
  });
  btn.addEventListener('mousedown', e => e.preventDefault());
  btn.addEventListener('click', () => {
    if (calFor(box)) { calClose(box); return; }
    box.focus();
    calOpen(box);
  });
  show();
  if (document.activeElement === real) box.focus();
}

/* The few words under a box being typed in: the day it will be, or why
   it is not one. One for the whole page; it follows the box. */
let dateSaid = null;
function dateSay(box, text, wrong) {
  if (!text || calFor(box)) {
    if (dateSaid) { dateSaid.remove(); dateSaid = null; }
    return;
  }
  if (!dateSaid) {
    dateSaid = el('<div class="dtsay" role="status"></div>');
    document.body.appendChild(dateSaid);
  }
  dateSaid.textContent = text;
  dateSaid.classList.toggle('wrong', !!wrong);
  const r = box.getBoundingClientRect();
  dateSaid.style.left = Math.max(4, Math.min(r.left, window.innerWidth
    - dateSaid.offsetWidth - 8)) + 'px';
  dateSaid.style.top = (r.bottom + 5) + 'px';
}

/* The calendar: one month, over the page, the box keeping the keyboard. */
const Cal = { el: null, box: null, hi: '' };
const calFor = box => (Cal.el && Cal.box === box ? Cal : null);
function calOpen(box) {
  if (box.readOnly || box.disabled) return;
  calClose();
  const real = box._real;
  const typed = readDate(box.value, real.value || today());
  Cal.box = box;
  Cal.hi = typed.iso || real.value || today();
  Cal.el = el('<div class="calpop" role="dialog" aria-label="Calendar"></div>');
  // Clicking in it must not take the focus off the box.
  Cal.el.addEventListener('mousedown', e => e.preventDefault());
  Cal.el.addEventListener('click', e => {
    const day = e.target.closest('[data-day]');
    const step = e.target.closest('[data-step]');
    if (day) calTake(day.dataset.day);
    else if (step) calMove(addMonths(Cal.hi, num(step.dataset.step)));
    else if (e.target.closest('[data-today]')) calTake(today());
  });
  document.body.appendChild(Cal.el);
  dateSay(box, '');
  calDraw();
  window.addEventListener('scroll', calScroll, true);
  window.addEventListener('resize', calPlace);
  KB.paintBar();
}
function calClose(box) {
  if (!Cal.el || (box && Cal.box !== box)) return;
  Cal.el.remove();
  Cal.el = null;
  Cal.box = null;
  window.removeEventListener('scroll', calScroll, true);
  window.removeEventListener('resize', calPlace);
  KB.paintBar();
}
function calMove(iso) {
  if (!partsOf(iso)) return;
  Cal.hi = iso;
  calDraw();
}
function calTake(iso) {
  const box = Cal.box;
  calClose();
  if (!box) return;
  box._settle(iso);
  box.focus();
  box.select();
}
function calKey(e) {
  const k = e.key;
  const by = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[k];
  const [y, m] = partsOf(Cal.hi);
  let to = null;
  if (by) to = addDays(Cal.hi, by);
  else if (k === 'PageUp' || k === 'PageDown') {
    to = addMonths(Cal.hi, (k === 'PageUp' ? -1 : 1) * (e.shiftKey ? 12 : 1));
  } else if (k === 'Home') to = isoOf(y, m, 1);
  else if (k === 'End') to = isoOf(y, m, daysIn(y, m));
  if (to) { e.preventDefault(); calMove(to); return; }
  if (k === 'Enter') { e.preventDefault(); calTake(Cal.hi); return; }
  if (k === 'Escape' || k === 'F4') { e.preventDefault(); calClose(); return; }
  if (k === 'Tab') calClose();
  // Anything else is typing, and the calendar follows it.
}
function calDraw() {
  const [y, m] = partsOf(Cal.hi);
  const first = isoOf(y, m, 1);
  const start = addDays(first, -new Date(y, m - 1, 1).getDay());   // a Sunday
  const held = Cal.box._real.value, now = today();
  let days = '';
  for (let i = 0; i < 42; i++) {
    const iso = addDays(start, i);
    const cls = [iso.slice(0, 7) !== first.slice(0, 7) ? 'out' : '',
      iso === held ? 'on' : '', iso === now ? 'now' : '',
      iso === Cal.hi ? 'hi' : ''].filter(Boolean).join(' ');
    days += '<button type="button" tabindex="-1" data-day="' + iso + '"'
      + (cls ? ' class="' + cls + '"' : '') + '>' + (+iso.slice(8)) + '</button>';
  }
  Cal.el.innerHTML = '<div class="cal-hd">'
    + '<button type="button" tabindex="-1" data-step="-1" title="Month before (PgUp)">‹</button>'
    + '<b>' + MONTH_NAMES[m - 1] + ' ' + y + '</b>'
    + '<button type="button" tabindex="-1" data-step="1" title="Month after (PgDn)">›</button></div>'
    + '<div class="cal-wk">' + WEEKDAYS.map(w => '<span>' + w.slice(0, 2) + '</span>').join('')
    + '</div><div class="cal-days">' + days + '</div>'
    + '<div class="cal-ft"><span>' + esc(dayWord(Cal.hi)) + '</span>'
    + '<button type="button" tabindex="-1" data-today>Today</button></div>';
  calPlace();
}
/** Under the box, or over it where there is more room; never off the
 *  window, never down behind the keys along its foot. */
function calPlace() {
  if (!Cal.el || !Cal.box) return;
  const r = Cal.box.getBoundingClientRect();
  const w = Cal.el.offsetWidth, h = Cal.el.offsetHeight;
  const foot = KB.clearArea(Cal.box).bottom;
  Cal.el.style.left = Math.max(4, Math.min(r.left, window.innerWidth - w - 8)) + 'px';
  Cal.el.style.top = (foot - r.bottom - 6 < h && r.top - 6 > h
    ? r.top - h - 4 : r.bottom + 4) + 'px';
}
function calScroll(e) {
  if (!Cal.el || (e.target && e.target.nodeType === 1 && Cal.el.contains(e.target))) return;
  const r = Cal.box.getBoundingClientRect();
  const a = KB.clearArea(Cal.box);
  if (!Cal.box.isConnected || r.bottom < a.top || r.top > a.bottom) calClose();
  else calPlace();
}

/* Every date box the app ever draws -- on a screen, in a dialog, in a
   panel opened later -- becomes one of these as it arrives. */
function dateBoxes(node) {
  if (node.nodeType !== 1) return;
  if (node.matches('input[type=date]')) dateBox(node);
  else if (node.firstElementChild) $$('input[type=date]', node).forEach(dateBox);
}
new MutationObserver(recs => {
  recs.forEach(r => r.addedNodes.forEach(dateBoxes));
}).observe(document.body, { childList: true, subtree: true });

/* ================================================================ state */
/** Enter walks to the next box, as the old SHREE screens did, so a whole
 *  document can be typed without reaching for the mouse.
 *
 *  One list for the whole screen, in the order the boxes are read on the page
 *  -- header, then every line of the grid, then the footer and the charges
 *  below it. The grid used to walk itself off a list of its own, which is how
 *  By Dept, Particulars, Cutting, Transport, Loading and Round Off ended up
 *  with no way to reach them at all: Enter past the last box opened another
 *  row instead of carrying on down the page.
 *
 *  Past the very last box, where there is nowhere further to go and the cursor
 *  is still in the grid, Enter opens a fresh line -- that is the order screen,
 *  which has nothing below its grid. ↓ to ＋ Add line and Enter does it too.
 *
 *  Left alone deliberately: anything a lookup list has already claimed -- there
 *  Enter picks the highlighted party or item, and the next Enter moves on.
 *
 *  Shift+Enter steps back, except in Particulars, the one box on these screens
 *  that holds more than a line: there it makes the new line that plain Enter
 *  used to, because leaving Enter to it stopped the walk dead and put the
 *  charge boxes past it out of reach again.
 *
 *  Saving is Ctrl+S or the button, never a stray Enter.
 */
/** The boxes on a screen, in the order Enter walks them.
 *
 *  Read-only ones are skipped: the address under the customer and the
 *  running balance beside it are filled in by the app, not typed into. */
function boxesIn(scope) {
  return $$('input, select, textarea', scope || document).filter(x =>
    !x.disabled && !x.readOnly && x.type !== 'hidden'
    && x.type !== 'button' && x.offsetParent !== null);
}

/** Move to the next box along, as pressing Enter there would.
 *  Used after a party or a product has been picked out of a list, so the
 *  picking leaves you where the typing would have. */
function moveOn(from, scope) {
  const boxes = boxesIn(scope);
  const next = boxes[boxes.indexOf(from) + 1];
  if (next) { next.focus(); if (next.select) next.select(); }
  return !!next;
}

function enterMovesOn(scope) {
  scope.onkeydown = e => {
    if (e.key !== 'Enter' || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.defaultPrevented) return;              // a lookup list took it
    const from = e.target;
    if (!from || !from.matches || !from.matches('input, select, textarea')) return;
    // A tick box is walked past like any other box -- Space ticks it. The
    // walk used to stop dead on one (Reverse Charge on a purchase, the tick
    // against each line of the salary sheet) until Tab was found.
    if (from.type === 'button') return;
    if (from.tagName === 'TEXTAREA' && e.shiftKey) return;   // a new line
    const boxes = boxesIn(scope);
    const i = boxes.indexOf(from);
    if (i < 0) return;
    e.preventDefault();
    const next = boxes[i + (e.shiftKey ? -1 : 1)];
    if (next) {
      next.focus();
      if (next.select) next.select();
      return;
    }
    // Past the last box, with the cursor still in a grid, Enter opens a fresh
    // line. Both grids answer: `#lg-body` is the item grid on a document,
    // `#j-body` the entries on a journal.
    const add = !e.shiftKey && (
      (from.closest('#lg-body') && $('#lg-add', scope)) ||
      (from.closest('#j-body') && $('#j-add', scope)));
    if (add) add.click();
  };
}

const S = { meta: null, parties: [], ledgers: [], items: [], cashbank: [] };

async function loadMeta(force) {
  if (!S.meta || force) {
    S.meta = await api.get('/api/meta');
    firmHere = S.meta.company_id;       // see `firmUrl`
  }
  return S.meta;
}
/* A form draws many rows at once and every row's picker asks for the same
   list. Caching the finished array is not enough -- the callers all run
   before the first answer lands, so they each fired their own request. Hold
   the in-flight promise and hand the same one to everybody. */
const flight = {};        // key -> { p, forced }
const fetched = {};       // key -> timestamp of the last completed load
const FRESH_MS = 60000;   // 'fresh' reuses a list loaded within a minute

/** True when this call has to go to the server. */
function stale(key, have, force) {
  if (!have) return true;
  if (force === 'fresh') return (Date.now() - (fetched[key] || 0)) > FRESH_MS;
  return !!force;
}

/** Share one request between every caller that asks while it is in flight. */
function shared(key, force, fetcher) {
  const cur = flight[key];
  // A post-edit refresh must not join a request that started before the edit.
  if (cur && !(force === true && !cur.forced)) return cur.p;
  const p = fetcher().then(v => { fetched[key] = Date.now(); return v; })
    .finally(() => { if (flight[key] && flight[key].p === p) delete flight[key]; });
  flight[key] = { p, forced: force === true };
  return p;
}

async function loadParties(force) {
  if (!stale('parties', S.parties.length, force)) return S.parties;
  return shared('parties', force, async () => {
    S.parties = await api.get('/api/ledgers',
      { active_only: 1, parties_only: 1 });
    return S.parties;
  });
}
/** Every ledger, including non-trading accounts (for journal entries). */
async function loadLedgers(force) {
  if (!stale('ledgers', S.ledgers.length, force)) return S.ledgers;
  return shared('ledgers', force, async () => {
    S.ledgers = await api.get('/api/ledgers', { active_only: 1 });
    return S.ledgers;
  });
}
async function loadCashBank(force) {
  if (!stale('cashbank', S.cashbank.length, force)) return S.cashbank;
  return shared('cashbank', force, async () => {
    S.cashbank = await api.get('/api/ledgers', { active_only: 1, cash_bank: 1 });
    return S.cashbank;
  });
}
async function loadItems(force) {
  if (!stale('items', S.items.length, force)) return S.items;
  return shared('items', force, async () => {
    S.items = await api.get('/api/items', { active_only: 1 });
    return S.items;
  });
}
function invalidate() {
  S.parties = []; S.ledgers = []; S.items = []; S.cashbank = [];
  for (const k in flight) delete flight[k];
  for (const k in fetched) delete fetched[k];
}

function findParty(id) { return S.parties.find(p => p.id === num(id)); }

/* =============================================================== router */
/* The menu. ← from the left edge of a screen goes into it (Esc did from 22
   to 24 Sep 2026, and now goes back a screen), ↑ ↓ walk it and Enter (or
   →) opens the entry the highlight is on. Each entry used to wear a key of its own as well
   (22 Sep 2026); on 23 Sep 2026 they went -- "don't assign different keys
   to different functions" -- and the arrows are the one way in. */
const NAV = [
  ['Overview', [
    ['#/dashboard', '▦', 'Dashboard']
  ]],
  // A challan is raised in the order firm and a bill in one of the GST firms
  // -- the same screens, a different set of books. ?firm= says which, so the
  // entry puts you in the right one instead of filing the document wherever
  // the screen happened to be left.
  ['Sales', [
    ['#/invoice/new?firm=order', '＋', 'New Challan'],
    ['#/invoices?firm=order', '🧾', 'Challans'],
    // The steel the challans take out of BST's yard (26 Sep 2026).
    ['#/steel', '🧱', 'Challan Stock'],
    ['#/invoice/new?firm=billing', '＋', 'New Bill'],
    ['#/bills', '🧾', 'Bills'],
    ['#/order/new', '＋', 'New Order'],
    ['#/orders', '📋', 'Orders'],
    ['#/eway', '🚚', 'E-way Bill JSON'],
    ['#/einvoice', '🔖', 'e-Invoice JSON'],
    ['#/gstr1', '📑', 'GST']
  ]],
  ['Reports', [
    ['#/accounts', '📖', 'Accounts'],
    ['#/reports/receivables', '⏳', 'Who owes us'],
    ['#/reports/outstanding', '⚖️', 'Outstanding'],
    ['#/reports/pnl', '📈', 'Profit & Loss'],
    ['#/reports/balance-sheet', '🏦', 'Balance Sheet'],
    ['#/reports/stock', '📊', 'Billing Stock'],
    ['#/reports/gst', '🧮', 'GST Summary'],
    ['#/reports/daybook', '🗓', 'Day Book'],
    ['#/reports/health', '🩺', 'Data health']
  ]],
  // A purchase is bought into one of the two GST firms -- that is how the
  // stock is divided between them -- so the entry says which set of books it
  // belongs in, exactly as a bill does. Without it a purchase was filed in
  // whichever firm the screen happened to be left in.
  ['Purchase & Cash', [
    ['#/invoice/new?type=PURCHASE&firm=billing', '📥', 'New Purchase'],
    // Both GST firms' purchases in one list, as Bills (6 Oct 2026).
    ['#/purchases', '📦', 'Purchases'],
    ['#/vouchers', '💰', 'Receipts / Payments'],
    ['#/bank', '🏦', 'Bank'],
    ['#/cash', '💵', 'Cash'],
    // Staff accounts are kept in the accounts book, with the money.
    ['#/staff?firm=accounts', '👷', 'Staff & Salary'],
    ['#/voucher/new?kind=JOURNAL', '📝', 'Journal / Contra']
  ]],
  // The user's own money, apart from the business and every firm in it
  // (30 Sep 2026).
  ['Personal', [
    ['#/personal', '👛', 'Personal Accounts']
  ]],
  // Masters -- Parties, Customer Allocation, Chart of Accounts, Items --
  // was taken out of the menu on 26 Sep 2026 at the user's asking. The
  // screens are still there, opened from the places that link to them: the
  // "Customer Allocation" button when a challan cannot be billed, the notes
  // on Who owes us, "Chart of Accounts" on an empty bank book, a low-stock
  // item on the Dashboard.
  ['Setup', [
    ['#/companies', '🏢', 'Companies'],
    ['#/sync', '🔄', 'SHREE update'],
    ['#/settings', '⚙', 'Settings']
  ]]
];

/* ------------------------------------------------ which entry you are on ---
   The menu used to look for its own address spelled exactly as it writes it,
   which only held while a screen was left as the entry opened it. Screens
   carry their state in the address now -- dates, a search, the customer being
   read -- so nothing is ever spelled that way for long.

   An entry is where you are when the **path** is the same and nothing it
   asks for is contradicted. Two entries can share a path -- Challans and
   Purchases are both `/invoices` -- so the one that has more of what it asks for actually
   answered wins, and where neither does, the plainer of the two: the address
   said nothing to single out the narrower entry. */
function navScore(href, path, q) {
  const [hp, hq] = href.replace(/^#/, '').split('?');
  // A page opened from an entry's screen -- a customer off Who owes us --
  // is still that entry, though never ahead of an entry for the page itself.
  if (hp !== path && !(hp !== '/' && path.startsWith(hp + '/'))) return -1;
  const under = hp !== path;
  const want = new URLSearchParams(hq || '');
  let confirmed = 0, asked = 0;
  for (const [k, v] of want) {
    asked += 1;
    if (!q.has(k)) continue;              // the screen dropped it; not a clash
    if (q.get(k) !== v) return -1;        // it says something else outright
    confirmed += 1;
  }
  const score = confirmed * 10 + (9 - Math.min(9, asked));
  return under ? score / 1000 : score;
}

// The menu entry of the last screen that had one. A bill or a voucher has no
// entry of its own, so Esc on one lands on the list it was opened from.
let navHere = '';

function drawNav(active) {
  const [path, qs] = active.replace(/^#/, '').split('?');
  const q = new URLSearchParams(qs || '');
  let best = -1, on = '';
  NAV.forEach(([, links]) => links.forEach(([href]) => {
    const s = navScore(href, path, q);
    if (s > best) { best = s; on = href; }
  }));
  // One Tab stop for the whole menu -- the entry of the screen that is open
  // -- and the arrows inside it. Every entry being a stop of its own meant
  // twenty-eight presses of Tab to get from the firm box to the screen.
  if (on) navHere = on;
  const known = NAV.some(([, links]) => links.some(([href]) => href === navHere));
  const stop = on || (known ? navHere : NAV[0][1][0][0]);
  // Arriving at another entry's screen opens its group, so the entry that
  // is lit can be seen; a group closed by hand stays closed until then.
  const grpOf = href => (NAV.find(([, links]) => links.some(([h]) => h === href)) || [])[0];
  if (on && on !== navLit) { navOpen = grpOf(on); navLit = on; }
  const n = $('#nav');
  n.setAttribute('aria-label', 'Menu — ← to come here, ↑ ↓ and Enter');
  n.innerHTML = NAV.map(([grp, links]) =>
    '<div class="navgrp' + (grp === navOpen ? ' open' : '')
    + (grp === grpOf(on) ? ' here' : '') + '" data-grp="' + esc(grp) + '">'
    + '<button type="button" class="grp" tabindex="-1" aria-expanded="'
    + (grp === navOpen) + '"><span class="caret" aria-hidden="true"></span>' + esc(grp) + '</button>'
    + '<div class="grpbody">' + links.map(([href, ic, label]) =>
      '<a href="' + href + '" class="' + (href === on ? 'on' : '') + '"'
      + (href === stop ? ' data-stop="1"' : '') + ' tabindex="-1">'
      + '<span class="ic">' + ic + '</span><span class="nl">' + esc(label) + '</span></a>').join('')
    + '</div></div>'
  ).join('');
  navTabStop();
}

/* **The menu in groups** (5 Oct 2026: "make different groups and by
   clicking on a particular group all the things inside it will be shown").
   Each group is a heading; clicking it, or Enter on it, shows its entries
   and folds the others away -- one group open at a time. → opens a group
   too, ← closes it, and ← on an entry goes up to its heading. */
let navOpen = null;                     // the group standing open
let navLit = '';                        // the entry whose screen opened it

/** Open the group `name` (closing the others), or close it. */
function navShow(name, open) {
  navOpen = open ? name : (navOpen === name ? null : navOpen);
  $$('#nav .navgrp').forEach(g => {
    const on = g.dataset.grp === navOpen;
    g.classList.toggle('open', on);
    $('.grp', g).setAttribute('aria-expanded', String(on));
  });
  navTabStop();
}

/** The menu is one Tab stop: the entry of the open screen, or its group's
 *  heading while that group is folded away. */
function navTabStop() {
  $$('#nav .grp, #nav a').forEach(x => { x.tabIndex = -1; });
  const a = $('#nav a[data-stop]');
  const g = a && a.closest('.navgrp');
  const t = g && g.classList.contains('open') ? a : (g ? $('.grp', g) : $('#nav .grp'));
  if (t) t.tabIndex = 0;
}

/** Where the keyboard lands on coming into the menu -- ← off a screen's
 *  left edge, or Esc on a menu screen: the open screen's entry, its group
 *  opened to show it (6 Oct 2026: "the first esc should take me to Bills in
 *  the side menubar and the second esc should take me back to Sales"). Esc
 *  on the entry then folds the group onto its heading. */
function navEntry() {
  const a = $('#nav a.on') || $('#nav a[data-stop]') || $('#nav a');
  const g = a && a.closest('.navgrp');
  if (g && !g.classList.contains('open')) navShow(g.dataset.grp, true);
  return a;
}

/** What ↑ ↓ walk: every group's heading, and the entries of the open one. */
function navStops() {
  return $$('#nav .grp, #nav .navgrp.open a');
}

/** Sidebar header: company name, GSTIN and the company switcher. */
function drawCompany() {
  const c = S.meta.company;
  $('#co-name').textContent = c.name || 'Billing';
  $('#co-sub').textContent = c.code ? c.code + ' · Books' : 'Order & GST Billing';
  document.title = (c.name || 'Billing') + ' — Billing & Orders';
  // The Lock button appears only while the passcode is switched on
  // (auth.ENABLED in app/auth.py, reported here as meta.lock_enabled).
  $('#fy').innerHTML = (c.gstin ? 'GSTIN ' + esc(c.gstin) : 'GSTIN not set')
    + (S.meta.lock_enabled
      ? ' <button class="link" id="lock-now" style="float:right;color:inherit;'
        + 'opacity:.8" title="Lock the books">Lock</button>'
      : '');
  const lockBtn = $('#lock-now');
  if (lockBtn) lockBtn.onclick = async () => {
    await fetch('/api/session', { method: 'DELETE' });
    location.reload();
  };

  const sel = $('#co-switch');
  sel.innerHTML = S.meta.companies.map(x =>
    `<option value="${x.id}"${x.id === c.id ? ' selected' : ''}>${esc(x.name)}</option>`
  ).join('') + '<option value="__new">＋ Add company…</option>';
  sel.title = 'Switch company — Enter';
  sel.onchange = async () => {
    const to = sel.value;
    sel.value = String(c.id);           // shown again once the switch is made
    if (to === '__new') { location.hash = '#/companies'; return; }
    switchCompany(num(to));
  };
  /* The box is for the mouse. On Windows an arrow key on a closed dropdown
     changes it -- and changing this one switches the books there and then,
     one firm per key. From the keyboard it opens the list of firms instead,
     where nothing happens until one is chosen. */
  sel.onkeydown = e => {
    if (e.key === 'Tab' || e.key === 'Escape' || e.altKey || e.ctrlKey || e.metaKey) return;
    e.preventDefault();
    // The box heads the menu: ↓ goes on down into it, as ↑ from the first
    // entry comes up here. Enter or Space opens the list of firms; no other
    // key does anything to it.
    if (e.key === 'ArrowDown') {
      const first = navStops()[0];
      if (first) first.focus();
      return;
    }
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'F4') chooseCompany();
  };
}

/** Open another firm's books, and redraw the screen in them. Half-typed work
 *  on the screen would be lost, so it asks first. */
async function switchCompany(id) {
  if (!id || id === S.meta.company_id) return;
  if (KB.unsavedWork()) {
    const how = await askLeave('Switch firm and lose it?', 'Save and switch');
    if (how === 'stay') return;
    // Saved, the entry holds nothing unsaved and the switch goes ahead.
    if (how === 'save') { KB.saveThen(() => switchCompany(id)); return; }
  }
  try {
    await api.post('/api/companies/' + id + '/activate', {});
    invalidate();
    await loadMeta(true);
    ok('Switched to ' + S.meta.company.name);
    render();
  } catch (e) { bad(e.message); }
}

/** The firms: walk to one with ↑ ↓ and Enter. */
async function chooseCompany() {
  if ($('#modal-root').children.length) return;
  const cos = S.meta.companies || [];
  const body = `<div class="fp-list" style="max-height:none;margin-top:0">${cos.map(x => `
      <button class="fp-item" data-co="${x.id}">
        <span class="grow"><b>${esc(x.name)}</b>
          <span class="hint" style="margin:0 0 0 6px">${esc(x.code || '')}${
            x.tax_mode === 'none' ? ' · no tax' : ' · GST'}</span></span>
        ${x.id === S.meta.company_id ? '<span class="tag ok">open now</span>' : ''}</button>`).join('')}
      <button class="fp-item" data-co="new">
        <span class="grow">＋ Add a company…</span></button></div>
    <p class="hint" style="margin:10px 0 0">↑ ↓ to the firm, then Enter.</p>`;
  let chosen = null;
  await modal('Switch company', body, [{ label: 'Cancel', value: null }], {
    onOpen: (bg, close) => {
      $$('[data-co]', bg).forEach(b => {
        b.onclick = () => { chosen = b.dataset.co; close(null); };
      });
      const open = $('[data-co="' + S.meta.company_id + '"]', bg) || $('[data-co]', bg);
      if (open) open.focus();
    }
  });
  if (chosen === 'new') location.hash = '#/companies';
  else if (chosen) switchCompany(num(chosen));
}

function setTitle(title, sub, actions) {
  // The subtitle is kept to one line; the whole of it is its tooltip.
  $('#page-title').innerHTML = esc(title) +
    '<div class="sub" title="' + esc(sub || '') + '">' + esc(sub || '') + '</div>';
  $('#page-actions').innerHTML = '';
  // Reached by the arrows like everything else: ↑ from the top of the
  // screen, ← → along them, Enter.
  (actions || []).forEach(a => {
    const b = el('<button class="' + (a.cls || '') + '">' + esc(a.label) + '</button>');
    b.onclick = a.onClick;
    $('#page-actions').appendChild(b);
  });
}

const ROUTES = [];
function route(re, fn) { ROUTES.push([re, fn]); }

/* ------------------------------------------------------- the way back ---
   Every screen carries one. This is a hash-routed page, so the browser's own
   Back already works -- but the app is opened in a window whose chrome the
   user may not have in front of them, and reaching for it is not the same as
   having it on the screen. The arrow at the top left is that button, and
   Alt + ← is the same thing from the keyboard.

   **How deep we are rides in `history.state`.** Each entry the app pushes is
   stamped with its depth; arriving at one by Back or Forward brings the stamp
   with it, so the count survives a reload and never drifts. The screen the app
   was opened on is depth 0 and shows no arrow, which is what stops Back from
   walking out of the app into whatever page the window held before it. */
let navDepth = -1;                      // the first render makes this 0

/* **On a menu screen, Esc goes to the menu -- never back to the Dashboard**
   (26 Sep 2026: "by clicking on escape I should be directed to the menubar,
   but not directly to Dashboard, I should stay on the current selected
   option, for example Challans"). Esc walks back through what was opened
   from a screen -- a challan, its bill, a customer's page -- and on the
   screen that has an entry of its own in the menu it puts the keyboard on
   that entry, the screen left standing behind it; Esc or Enter there goes
   back into the screen at the same line.

   A menu screen reached by opening something -- the whole account from a
   customer's page, a customer's account from a bill -- is a step like any
   other, and Esc goes back to where it was opened. So the mark is made as
   the screen is arrived at: chosen from the menu, or reached from another
   menu screen (a link on the Dashboard, ＋ New on Challans), it is a root --
   unless the link opens one particular account (`opensOne`): an account
   clicked on Outstanding is a step, and Esc goes back to Outstanding.
   It rides in `history.state` with the depth, so Back and a reload find it
   again; an entry stamped before the mark existed counts as a root if it
   is a menu screen. The ← arrow at the top left and Alt + ← still go all
   the way back. */
let navRoot = false;                    // Esc on this screen goes to the menu
let menuChosen = null;                  // the entry just clicked, till it lands
let shownHash = null;                   // the screen drawn before this one

/** Is this address the screen of a menu entry itself -- Challans, Bills,
 *  Who owes us -- rather than a page opened from one of them? */
function menuScreen(hash) {
  const [path, qs] = String(hash || '').replace(/^#/, '').split('?');
  const q = new URLSearchParams(qs || '');
  return NAV.some(([, links]) => links.some(([href]) =>
    navScore(href, path || '/dashboard', q) >= 1));
}

/** Does this address open one particular account -- a customer's account,
 *  a bank's statement -- rather than the screen itself? Reached by a link
 *  from another screen, that is opening something, not moving to another
 *  part of the menu: an account clicked on Outstanding, Esc went to the
 *  Accounts list instead of back to Outstanding (30 Sep 2026). */
function opensOne(hash) {
  const q = new URLSearchParams(String(hash || '').split('?')[1] || '');
  // `party`: a new challan or bill raised from a customer's account (4 Oct
  // 2026) is a step too -- Esc goes back to the account, not to the menu.
  return ['ledger_id', 'account', 'party'].some(k => q.get(k));
}

function trackDepth() {
  const st = history.state;
  const back = { y: 0, place: null };
  const here = location.hash;
  if (st && typeof st.appDepth === 'number') {
    navDepth = st.appDepth;             // came here by Back, Forward or reload
    navRoot = 'navRoot' in st ? !!st.navRoot : menuScreen(here);
    back.y = num(st.scrollY);           // and this is how far down it was left
    back.place = st.place || null;      // on which line, with what open
  } else {
    navDepth += 1;                      // a fresh push, one deeper than before
    navRoot = (menuChosen !== null && menuChosen === here)
      || (menuScreen(here) && (shownHash === null
        || (menuScreen(shownHash) && !opensOne(here))));
    stamp({ appDepth: navDepth, navRoot });
  }
  menuChosen = null;
  shownHash = here;
  const b = $('#back-btn');
  if (b) b.hidden = navDepth < 1;
  return back;
}

function goBack() {
  if (navDepth > 0) history.back();
}

/** Leave a screen the way you came in. `home` is only used when there is
 *  nothing behind this screen -- the app was opened straight onto it. */
function leave(home) {
  if (navDepth > 0) history.back(); else location.hash = home;
}

/** Show a document just saved. A new one takes the place of the blank form
 *  it was typed on, in the history as well as on the screen: pushed on top
 *  of it, Back and Esc from the saved bill went to an empty "New" form, and
 *  so did deleting it -- a purchase deleted from its own screen landed on
 *  "Buy into which firm?" again (29 Sep 2026). An edit is drawn again where
 *  it stands. */
function showSaved(hash, wasNew) {
  if (wasNew && location.hash !== hash) {
    stamp({ place: null, scrollY: 0 }, hash);
    return render();
  }
  location.hash = hash;
  return wasNew ? Promise.resolve() : render();
}

/* ------------------------------------------------ what a screen was on ---
   A screen is more than the route that drew it. Which customer is open, the
   dates, the search, the view, how far down the list you had got: all of it
   used to live in the boxes on the screen and nowhere else, so stepping off
   the screen threw it away. Open a payment against a customer's account,
   close it, and you came back to the list of every customer rather than to
   the account you were reading.

   **The address is the screen's memory.** Every control that changes what is
   shown writes itself into the hash, and the hash is what `render` reads on
   the way back in. The write is a `replaceState`, not a push: changing a date
   is not a step for Back to walk through -- Back leaves the screen, it does
   not undo the last filter.

   How far down the page you were rides in `history.state` beside the depth
   stamp, because it belongs to the visit and not to the address: the same
   list reached twice is two places you were. */

/** Rewrite this history entry, keeping whatever the rest of it holds. */
function stamp(bits, hash) {
  const st = Object.assign({}, history.state, bits);
  try { history.replaceState(st, '', hash === undefined ? null : hash); }
  catch (e) { /* file:// has no history to write to */ }
}

/** Read one control, the way a query string wants it. */
function ctlValue(node) {
  if (!node) return '';
  if (node.type === 'checkbox') return node.checked ? '1' : '';
  return node.value == null ? '' : String(node.value);
}

/** The state one screen keeps, named by the query keys it travels under.
 *  `fields` maps each key to a selector, or to a pair of functions for the
 *  things that are not a box on the screen. */
function screenState(path, fields) {
  const keys = Object.keys(fields);
  const get = k => {
    const f = fields[k];
    return typeof f === 'object' ? String(f.get() == null ? '' : f.get())
                                 : ctlValue($(f));
  };
  const api = {
    /** Put back what the address remembers. A key it does not carry keeps
     *  the value the screen was drawn with, so defaults still stand. */
    restore(q) {
      keys.forEach(k => {
        if (!(k in q)) return;
        const f = fields[k], v = q[k];
        if (typeof f === 'object') { f.set(v); return; }
        const n = $(f);
        if (!n) return;
        if (n.type === 'checkbox') n.checked = v === '1' || v === 'true';
        else n.value = v;
      });
      fySync();               // dates set by code raise no change event
      return api;
    },
    /** What the screen is showing, as the address would put it. */
    hash() {
      const p = new URLSearchParams();
      keys.forEach(k => { const v = get(k); if (v) p.set(k, v); });
      const s = p.toString();
      return '#' + path + (s ? '?' + s : '');
    },
    /** Write the screen into the address, in place. */
    save() {
      const h = api.hash();
      if (location.hash !== h) stamp({}, h);
      return api;
    }
  };
  return api;
}

/* How far down the page you had got, written against the entry being left so
   that Back finds it there; `render` reads it once, on the way in, before
   anything has moved.

   The mark is taken on the way *into* a click or a keypress, in the capture
   phase -- ahead of the handler that is about to change the address. That is
   the exact position the screen was at when it was left, and it costs nothing
   on the clicks that go nowhere. `scroll` is listened to as well, for the
   times a screen is left by some other route, but nothing depends on it: a
   programmatic scroll does not always raise one. */
let scrollMark = -1;
function markScroll() {
  const y = Math.round(window.scrollY);
  if (y === scrollMark) return;
  scrollMark = y;
  stamp({ scrollY: y });
}
/* **And which line the cursor was on.** Asked on 24 Sep 2026, once Esc had
   become the way back: *"take the user back exactly where he left, don't
   take back the user to the top of the list."* The page came back scrolled
   to where it was, but the keyboard landed in the search box at the top --
   or on the first line -- and the first arrow jumped there. So the line
   being left, the rows folded open and the boxes opened or closed are
   written against this history entry too (`KB.place`), at the click or key
   that leaves it, and `KB.restorePlace` stands on that line again. */
function markPlace(e) {
  // A held arrow walking down a list scrolled the page on every press, and
  // every press wrote that into the history entry -- work the browser does
  // across processes, thirty times a second. The scroll listener below
  // writes it once the page stops; a key or click that leaves still writes
  // it first.
  const walking = e.type === 'keydown' && !e.altKey && !e.ctrlKey && !e.metaKey
    && /^(Arrow(Up|Down)|Page(Up|Down)|Home|End|Tab|Shift)$/.test(e.key);
  if (!walking) markScroll();
  if (e.type === 'keydown' && !(e.key === 'Enter' || e.key === 'Escape'
      || e.key === ' ' || e.altKey)) return;
  try { stamp({ place: KB.place(e.target) }); } catch (err) { /* no screen */ }
}
['click', 'keydown'].forEach(e => addEventListener(e, markPlace, true));
let scrollTimer = 0;
addEventListener('scroll', () => {
  clearTimeout(scrollTimer);
  scrollTimer = setTimeout(markScroll, 120);
}, { passive: true });

/** Put the page back where it was, once there is enough page to do it on.
 *  A list is fetched after its screen is drawn, so this waits for the rows --
 *  and gives up the moment the user takes over, or the page never grows that
 *  far. A timer rather than a frame: frames stop being handed out to a window
 *  that is not on screen, and this has to finish either way. */
const SCROLL_WAIT = 60, SCROLL_TRIES = 50;        // three seconds, at most
function restoreScroll(y, seq) {
  if (!(y > 0)) return;
  let live = true, tries = 0;
  const stop = () => { live = false; off(); };
  const off = () => ['wheel', 'touchstart', 'keydown'].forEach(
    e => removeEventListener(e, stop));
  ['wheel', 'touchstart', 'keydown'].forEach(
    e => addEventListener(e, stop, { once: true, passive: true }));
  const room = () => Math.max(document.body.scrollHeight,
                              document.documentElement.scrollHeight)
                     - window.innerHeight;
  const tick = () => {
    if (!live || seq !== renderSeq) { off(); return; }
    const r = room();
    if (r >= y - 2 || ++tries > SCROLL_TRIES) {
      if (r > 0) window.scrollTo(0, Math.min(y, r));
      off();
      // The keyboard was put on the screen's first line or box a moment
      // ago; scrolled back down to where the page was, that was left out of
      // sight above the window -- Back to the Dashboard stood on CASH IN
      // HAND, 100px above the top. It is brought back into view.
      const ae = document.activeElement;
      const at = ae && ae !== document.body && !ae.closest('#sidebar, .topbar')
        ? ae : $('#view .kb');
      if (at) KB.reveal(at);
      return;
    }
    setTimeout(tick, SCROLL_WAIT);
  };
  tick();
}

// A view that waits on the network -- or on a dialog -- can still be running
// when the next menu click starts the one after it. `stale` lets the slower of
// the two find out it has been overtaken rather than paint over the newer page.
let renderSeq = 0;

/** The screen a piece of work was started on: call it before sending, ask it
 *  when the answer lands. A list's `load` draws its rows into `#out`, `#list`
 *  and the like when they come back -- and every screen names its boxes the
 *  same, so rows that came back after the next screen was drawn were drawn
 *  into that one: Customer Accounts' list under the Stock heading, Data
 *  health's findings on the stock report (seen 21 Sep 2026, moving on before
 *  a slow screen had loaded). `stale` in `render` only covers a screen's
 *  first step; this covers what it does afterwards. */
function onScreen() {
  const seq = renderSeq;
  return () => seq === renderSeq;
}

/** `onScreen`, for a list its search box asks for: also false once another
 *  key has gone into the box. The list for that key is already on its way,
 *  and drawing this one first held the typing up -- the second letter typed
 *  into Vouchers' search waited the best part of a second on a slow PC while
 *  2,600 vouchers matching the first were drawn (27 Sep 2026). */
function onSearch() {
  const box = $('#view #q'), asked = box && box.value, on = onScreen();
  return () => on() && (!box || box.value === asked);
}

async function render() {
  const mine = ++renderSeq;
  const stale = () => mine !== renderSeq;
  const hash = location.hash.replace(/^#/, '') || '/dashboard';
  const [path, qs] = hash.split('?');
  const q = Object.fromEntries(new URLSearchParams(qs || ''));
  const view = $('#view');
  // Moved to another address, or drawn again where it stands (after a save).
  const moved = !!(arguments[0] && arguments[0].type === 'hashchange');
  KB.beginScreen(hash, moved);
  // Read before anything moves: the page is about to be sent to the top,
  // and that would write over the mark this visit is being restored from.
  const was = trackDepth();
  KB.paintBar();                          // "Esc back" -- or not, if chosen from the menu
  scrollMark = -1;                        // a new screen, nothing marked yet
  // The Enter walk is hung on #view itself, and #view outlives the screen it
  // was put there for -- only its contents are replaced. Left on, an entry
  // screen's walk would go on answering Enter over the list that came after
  // it. Every screen that wants one asks for it again.
  view.onkeydown = null;
  // Restarting the arrival animation means taking the class off and letting
  // a frame pass; set twice in a row it would never play again.
  view.classList.remove('turning');
  void view.offsetWidth;
  view.classList.add('turning');
  view.innerHTML = '<div class="empty">Loading…</div>';
  drawNav('#' + hash);
  window.scrollTo(0, 0);
  try {
    // A screen drawn afresh loses nothing by catching up first.
    if (booksNew) await takeBooks(true);
    await loadMeta();
    if (stale()) return;
    drawCompany();
    // A Sales menu entry says which set of books it belongs in. Raising a new
    // bill asks which firm outright -- the two GST firms read almost the same
    // and a bill in the wrong one is a real correction -- while a list only
    // has to land in a GST firm at all.
    // A link to a document says which firm's books it is in -- and so does
    // one to a new document raised from a customer's account.
    if (/^\d+$/.test(q.firm || '') && /^\/(invoice|voucher)\/(\d+|new)$/.test(path)) {
      if (num(q.firm) !== S.meta.company_id) {
        await useCompany(num(q.firm));
        if (stale()) return;
        drawCompany();
      }
    } else if (q.firm) {
      const chosen = await ensureFirm(
        q.firm, path === '/invoice/new',
        (q.type || '').toUpperCase() === 'PURCHASE');
      if (stale()) return;
      if (!chosen) {
        view.innerHTML = '<div class="empty"><b>No firm chosen</b>' +
          'Pick one from the list at the top left, or choose again from the menu.</div>';
        setTitle('Sales');
        return;
      }
      drawCompany();
    }
    for (const [re, fn] of ROUTES) {
      const m = path.match(re);
      if (m) {
        await fn(view, m.slice(1), q);
        if (stale()) return;
        // The screen is up. Come back to it, and it is as it was left: the
        // same line, the same rows open, scrolled the same. Arrive afresh,
        // and the keyboard is left somewhere it can start from.
        if (was.place) KB.restorePlace(was.place, was.y, mine);
        else {
          KB.focusEntry();
          restoreScroll(was.y, mine);
        }
        // "Save and go back": the saved entry is up, so on it goes.
        KB.drawn(moved);
        return;
      }
    }
    view.innerHTML = '<div class="empty"><b>Page not found</b>' +
      '<a href="#/dashboard">Go to dashboard</a></div>';
    setTitle('Not found');
    if (!stale()) KB.focusEntry();
  } catch (e) {
    if (stale()) return;
    view.innerHTML = '<div class="card"><div class="bd"><b>Something went wrong</b>' +
      '<p style="color:var(--bad)">' + esc(e.message) + '</p></div></div>';
  }
}
window.addEventListener('hashchange', render);

/* Column headings that stay in view sit just under the bar at the top of the
   page, and that bar changes height with the window: its title wraps when the
   window narrows, and Windows restores a maximised window to another size.
   Measured once when a list was drawn, the headings were left halfway down
   the rows after a resize (21 Sep 2026). */
(function keepTopbarHeight() {
  const bar = document.querySelector('.topbar');
  if (!bar || !window.ResizeObserver) return;
  new ResizeObserver(() => document.documentElement.style.setProperty(
    '--topbar-h', bar.offsetHeight + 'px')).observe(bar);
})();

/* **A list that fits its card is not a scroll box** (27 Sep 2026: "the
   software is laggy when used in Windows"). Every list is drawn in a
   `.tbl-scroll`, which scrolls sideways when the window is too narrow for
   its columns. Chrome paints every line inside a scroll box whenever
   anything in it changes -- the cursor moving a line, the pointer passing
   over one -- however far off screen the line is. So a key press on
   Vouchers repainted all 2,600 vouchers, where a plain list repaints the
   screenful: measured with the processor slowed to a modest office PC's,
   1,611 ms of painting against 77 for the same eight presses.

   So a list that fits is marked `fits` and stops being a scroll box; one a
   little too wide is first given narrower gaps between its columns (`tight`)
   and fits that way; only a list that still does not fit keeps the sideways
   scroll (`wide`). Its headings never stayed in view inside the box -- the
   box does not scroll up and down -- and `fits` keeps them where they are
   drawn. Boxes that scroll within a height of their own are left alone. */
(function fitLists() {
  const view = document.getElementById('view');
  if (!view || !window.MutationObserver) return;
  const seen = new WeakMap();                 // box -> {t, w}
  // What each list came to, by screen, place and window width: a list drawn
  // again is laid out once, already as it will stay. Two thousand lines take
  // a while to lay out, and trying it both ways cost Vouchers a quarter more.
  // Kept in the browser, so a reload -- or tomorrow -- starts right too.
  let kept;
  // (".2": the first cache knew no `tighter`, and a list it had down as wide
  // would never have tried it.)
  try { kept = new Map(JSON.parse(localStorage.getItem('fit.lists.2') || '[]')); }
  catch (e) { kept = new Map(); }
  try { localStorage.removeItem('fit.lists'); } catch (e) { /* nothing kept */ }
  function keep(key, v) {
    if ((kept.get(key) || []).join() === v.join()) return;
    kept.set(key, v);
    try { localStorage.setItem('fit.lists.2', JSON.stringify([...kept].slice(-200))); }
    catch (e) { /* only slower */ }
  }
  const MINE = ['fits', 'tight', 'tighter', 'wide'];
  // Narrower gaps, tried in turn before a list is left to scroll sideways.
  // At 1280 wide with Windows' scrollbar the Challans list was 11px too wide
  // even `tight`, and its Total and Actions were off to the right; `tighter`
  // closes the gaps a little more (29 Sep 2026).
  const STEPS = [['tight', 8], ['tighter', 5]];
  let sizes = null;
  const leave = box => {
    if (MINE.some(c => box.classList.contains(c))) box.classList.remove(...MINE);
    seen.delete(box);
  };
  const over = box => box.scrollWidth > box.clientWidth + 1;
  // How much narrower the list draws with gaps of `px`: the gaps it closes,
  // measured on the headings rather than found by laying the whole list out
  // again.
  function saving(t, px) {
    const r = (t.tHead && t.tHead.rows[0]) || t.rows[0];
    let s = 0;
    for (const c of r ? r.cells : []) {
      const cs = getComputedStyle(c);
      if (cs.paddingLeft === '14px') s += 14 - px;
      if (cs.paddingRight === '14px') s += 14 - px;
    }
    return s;
  }
  function fit(box, i) {
    const t = box.firstElementChild;
    if (!t || t.tagName !== 'TABLE' || t.classList.contains('lineitems')
        || box.style.maxHeight || box.style.height || box.style.overflow
        || box.style.overflowY) return leave(box);
    const key = location.hash.split('?')[0] + ' ' + i + ' ' + innerWidth;
    const cls = box.classList, was = seen.get(box);
    const fresh = !was || was.t !== t;
    const had = [].concat(kept.get(key) || []).filter(c => MINE.includes(c));
    // Before anything asks for a layout -- and only if it is different: the
    // same classes taken off and put back restyle every line of the list.
    if (fresh && MINE.filter(c => cls.contains(c)).join() !== had.join()) {
      cls.remove(...MINE);
      cls.add(...had);
    }
    // Only its first lines are in (`fillRows`): drawn as last time, and
    // measured once the rest are under them. Judged on a hundred lines, a
    // list that is too wide whole was taken to fit, and then had to be laid
    // out twice more when the rest arrived -- and remembered wrongly.
    if (box.dataset.partial) return;
    // The same list at the same width -- a line folded open, say -- keeps
    // what it has, unless it no longer fits.
    const w = box.clientWidth;
    if (!fresh && was.w === w && !(cls.contains('fits') && over(box))) return;
    if (getComputedStyle(box).maxHeight !== 'none') return leave(box);
    // A list remembered as too wide is held only while even the tightest
    // gaps would not bring it in: a screen whose columns were narrowed (the
    // customer page off Who owes us, 6 Oct 2026) stayed scrolling sideways
    // on the strength of what it used to be.
    const tightest = STEPS[STEPS.length - 1][1];
    const held = fresh && had.length
      && (cls.contains('fits') ? !over(box)
        : cls.contains('wide')
          ? box.scrollWidth - box.clientWidth - saving(t, tightest) > 1
          : over(box));
    if (!held) {
      cls.remove(...MINE);
      const extra = box.scrollWidth - box.clientWidth;
      const step = extra > 1 && STEPS.find(([, px]) => extra - saving(t, px) <= 1);
      if (step) cls.add(step[0]);
      cls.add(over(box) ? 'wide' : 'fits');
      if (cls.contains('wide')) cls.remove('tight', 'tighter');
    }
    keep(key, MINE.filter(c => cls.contains(c)));
    seen.set(box, { t, w: box.clientWidth });
    if (sizes) sizes.observe(box);
  }
  function all() {
    view.querySelectorAll('.tbl-scroll').forEach(fit);
  }
  // Before the list is first painted: a MutationObserver answers at the end
  // of the script that drew it, ahead of the frame.
  new MutationObserver(all).observe(view, { childList: true, subtree: true });
  // And again when the window -- so the card -- changes width.
  let timer = 0;
  if (window.ResizeObserver) {
    sizes = new ResizeObserver(entries => {
      if (!entries.some(e => { const s = seen.get(e.target);
        return s && s.w !== e.target.clientWidth; })) return;
      clearTimeout(timer);
      timer = setTimeout(all, 0);
    });
  }
})();

/* ============================================================ dashboard */
route(/^\/dashboard$/, async view => {
  const here = onScreen();
  const d = await api.get('/api/reports/dashboard', { today: today() });
  if (!here()) return;
  // Named for what it opens: in the no-tax firm a new sale is a challan.
  const noTaxHere = (S.meta.company || {}).tax_mode === 'none';
  setTitle('Dashboard', new Date().toDateString(), [
    { label: noTaxHere ? '＋ New Challan' : '＋ New Bill', cls: 'primary',
      onClick: () => location.hash = '#/invoice/new' },
    { label: '＋ New Order', onClick: () => location.hash = '#/order/new' }
  ]);

  // Every account that holds money, and what the six-figure row totals.
  const held = (d.cash_bank || []).filter(b => num(b.balance));
  const cashBankTotal = held.reduce((a, b) => a + num(b.balance), 0);
  const biggest = held.slice()
    .sort((a, b) => Math.abs(num(b.balance)) - Math.abs(num(a.balance)));
  const shown = biggest.slice(0, 8);
  const cashBankRows = held.length
    ? `<table><tbody>` + shown
        .map(b => `<tr class="clickable" data-bank="${b.id}"><td>${esc(b.name)}</td>
          <td class="r num" style="color:${num(b.balance) < 0 ? 'var(--bad)' : 'inherit'}">
            ${fmt(Math.abs(num(b.balance)))}</td>
          <td class="nw" style="width:1%;color:var(--muted);font-size:11.5px">
            ${num(b.balance) < 0 ? 'overdrawn' : 'in hand'}</td></tr>`).join('') +
      (biggest.length > shown.length
        ? `<tr><td colspan="3" style="color:var(--muted)">
            <a href="#/bank">and ${biggest.length - shown.length} more account(s) →</a></td></tr>`
        : '') +
      `</tbody><tfoot><tr><td>Total, all accounts</td>
        <td class="r num">${fmt(cashBankTotal)}</td><td></td></tr></tfoot></table>`
    : '<div class="empty">No cash or bank account holds a balance.</div>';

  const max = Math.max(1, ...d.trend.map(t => num(t.t)));
  // What a month came to was written into the tooltip and nowhere else, so
  // the only way to read a bar was to hold the mouse over it. Each column is
  // a tab stop now and says its figure when the keyboard reaches it, exactly
  // as it does under the pointer.
  const bars = d.trend.length
    ? '<div class="bars">' + d.trend.map(t =>
      '<div class="b" tabindex="0" title="' + esc(t.ym) + ': ' + money(t.t) + '">' +
      '<b>' + money(t.t) + '</b>' +
      '<i style="height:' + Math.max(2, (num(t.t) / max) * 100) + '%"></i>' +
      '<span>' + esc(t.ym.slice(5) + '/' + t.ym.slice(2, 4)) + '</span></div>').join('') +
    '</div>' : `<div class="empty">No sales invoiced in this app yet.${num(d.imported)
      ? ' Imported history is in the ledger reports.' : ''}</div>`;

  view.innerHTML = `
    <div class="kpis">
      <div class="kpi brand"><div class="lb">Sales today</div>
        <div class="vl">${money(d.sales_today)}</div>
        <div class="ft">${num(d.sales_today) ? 'billed today' : 'nothing billed yet today'}</div></div>
      <div class="kpi"><div class="lb">Sales this month</div>
        <div class="vl">${money(d.sales_month)}</div>
        <div class="ft">${d.invoice_count} invoice(s) all time</div></div>
      <div class="kpi good"><div class="lb">Receivable</div>
        <div class="vl">${money(d.receivable)}</div>
        <div class="ft"><a href="#/reports/outstanding">view outstanding →</a></div></div>
      <div class="kpi bad"><div class="lb">Payable</div>
        <div class="vl">${money(d.payable)}</div>
        <div class="ft"><a href="#/reports/outstanding?kind=payable">view payable →</a></div></div>
      <div class="kpi"><div class="lb">Open orders</div>
        <div class="vl">${d.open_orders}</div>
        <div class="ft">${money(d.open_order_value)} pending</div></div>
      <div class="kpi ${d.low_stock_count ? 'bad' : ''}"><div class="lb">Low stock items</div>
        <div class="vl">${d.low_stock_count}</div>
        <div class="ft"><a href="#/reports/stock">stock summary →</a></div></div>
      <div class="kpi"><div class="lb">Cash &amp; bank</div>
        <div class="vl">${money(cashBankTotal)}</div>
        <div class="ft"><a href="#/bank">bank →</a> &nbsp;
          <a href="#/cash">cash →</a></div></div>
    </div>

    <div class="grid g2" style="align-items:start">
      <div class="card"><h2>Monthly sales</h2><div class="bd">${bars}</div></div>
      <div class="card"><h2>Cash &amp; bank<span class="rt">
        <a class="btn sm" href="#/bank">Bank</a>
        <a class="btn sm" href="#/cash">Cash</a></span></h2>
        <div class="bd flush">${cashBankRows}</div></div>
    </div>

    <div class="grid g2" style="align-items:start">
      <div class="card"><h2>Recent invoices<span class="rt">
        <a class="btn sm" href="#/invoices">See all</a></span></h2><div class="bd flush">
        ${d.recent.length ? `<table><thead><tr><th>Date</th><th>No</th><th>Party</th>
          <th class="r">Amount</th></tr></thead><tbody>` +
          d.recent.map(r => `<tr class="clickable" data-open-invoice="${r.id}">
            <td class="nw">${dt(r.invoice_date)}</td><td class="nw">${esc(r.invoice_no)}</td>
            <td>${esc(r.party_name)}</td><td class="r num">${fmt(r.grand_total)}</td></tr>`).join('') +
          '</tbody></table>' : (num(d.imported)
            ? `<div class="empty"><b>No invoices raised in this app yet</b>
                ${fmt(d.imported, 0)} imported ledger entries are already in the books —
                see the <a href="#/accounts">accounts</a> screen or
                <a href="#/reports/outstanding?layout=tb">trial balance</a>.<br>
                Bill your next document here and it will appear in this list.</div>`
            : '<div class="empty"><b>No invoices yet</b>Create your first invoice to get started.</div>')}
      </div></div>
      <div class="card"><h2>Top customers</h2><div class="bd flush">
        ${d.top_parties.length ? `<table><thead><tr><th>Party</th><th class="c">Bills</th>
          <th class="r">Value</th></tr></thead><tbody>` +
          d.top_parties.map(p => `<tr data-href="${esc(accountLink(p.id))}">
            <td>${goLink(accountLink(p.id), esc(p.name))}</td><td class="c">${p.n}</td>
            <td class="r num">${fmt(p.t)}</td></tr>`).join('') +
          '</tbody></table>' : '<div class="empty">No customers billed yet.</div>'}
      </div></div>
    </div>

    <div class="card"><h2>Reorder alerts</h2><div class="bd flush">
      ${d.low_stock.length ? `<table><thead><tr><th>Item</th><th class="r">In stock</th>
        <th class="r">Reorder at</th></tr></thead><tbody>` +
        d.low_stock.map(i => `<tr data-href="${esc('#/items?search=' + encodeURIComponent(i.name))}">
          <td>${goLink('#/items?search=' + encodeURIComponent(i.name), esc(i.name), 'Find it in the item list')}</td>
          <td class="r num" style="color:var(--bad)">${qty(i.stock)} ${esc(i.unit)}</td>
          <td class="r num">${qty(i.reorder_level)}</td></tr>`).join('') +
        '</tbody></table>' : '<div class="empty">Every tracked item is above its reorder level.</div>'}
    </div></div>`;
});

/* =============================================================== ledgers */
/* A ledger is any account in the books. Ledgers sitting in a receivable or
   payable group are the trading parties you bill. */

/* One four-column grid, as on the company form. Six stacked grids of two,
   three and four columns meant no two rows of boxes began in the same place. */
const LEDGER_FORM = p => `
  <div class="grid g4" style="align-items:start">
    <div class="sp2"><label>Name *</label><input name="name" value="${esc(p.name || '')}"></div>
    <div class="sp2"><label>Account group *</label><select name="group_id">
      ${S.meta.groups.map(g => `<option value="${g.id}"${num(p.group_id) === g.id ? ' selected' : ''}>${esc(g.name)} (${esc(g.code)})</option>`).join('')}
    </select></div>

    <div><label>Code</label><input name="code" value="${esc(p.code || '')}" placeholder="optional"></div>
    <div><label>Party type</label><select name="party_kind">
      <option value="">Not a trading party</option>
      ${['customer', 'supplier', 'both'].map(k => `<option value="${k}"${(p.party_kind || '') === k ? ' selected' : ''}>${k[0].toUpperCase() + k.slice(1)}</option>`).join('')}
    </select></div>
    <div class="sp2"><label>GST registration</label><select name="gst_type">
      ${S.meta.gst_types.map(t => `<option${(p.gst_type || 'Regular') === t ? ' selected' : ''}>${esc(t)}</option>`).join('')}
    </select></div>

    <div><label>GSTIN</label><input name="gstin" value="${esc(p.gstin || '')}" style="text-transform:uppercase"></div>
    <div><label>PAN</label><input name="pan" value="${esc(p.pan || '')}" style="text-transform:uppercase"></div>
    <div><label>Phone</label><input name="phone" value="${esc(p.phone || '')}"></div>
    <div><label>Email</label><input name="email" value="${esc(p.email || '')}"></div>

    <div class="sp2"><label>Address line 1</label><input name="address1" value="${esc(p.address1 || '')}"></div>
    <div class="sp2"><label>Address line 2</label><input name="address2" value="${esc(p.address2 || '')}"></div>

    <div><label>City</label><input name="city" value="${esc(p.city || '')}"></div>
    <div><label>PIN</label><input name="pin" value="${esc(p.pin || '')}"></div>
    <div class="sp2"><label>State</label><select name="state_code">
      <option value="">— select —</option>
      ${S.meta.states.map(st => `<option value="${st.code}"${p.state_code === st.code ? ' selected' : ''}>${st.code} · ${esc(st.name)}</option>`).join('')}
    </select></div>

    <div><label>Opening balance</label>
      <input name="opening_bal" class="num" value="${num(p.opening_bal).toFixed(2)}">
      <div class="hint">+ debit (they owe you) · − credit</div></div>
    <div><label>Credit limit</label><input name="credit_limit" class="num" value="${num(p.credit_limit).toFixed(2)}"></div>
    <div><label>Credit days</label><input name="credit_days" class="num" value="${num(p.credit_days)}"></div>
    <div style="display:flex;align-items:center;padding-top:22px">
      <label style="margin:0;text-transform:none;font-size:13.5px;font-weight:500;color:var(--ink)">
        <input type="checkbox" name="active" style="width:auto"
        ${p.id === undefined || p.active ? 'checked' : ''}> Active</label></div>

    <div class="sp-all"><label>Notes</label><textarea name="notes">${esc(p.notes || '')}</textarea></div>
  </div>`;

function readForm(scope) {
  const out = {};
  $$('[name]', scope).forEach(f => {
    out[f.name] = f.type === 'checkbox' ? (f.checked ? 1 : 0) : f.value;
  });
  return out;
}

function defaultGroupFor(kind) {
  const code = kind === 'supplier' ? '3101' : '2101';
  const g = S.meta.groups.find(x => x.code === code) || S.meta.groups[0];
  return g ? g.id : '';
}

async function editParty(existing) {
  const p = Object.assign({}, existing || {});
  if (!p.id && !p.group_id) p.group_id = defaultGroupFor(p.party_kind);
  const saved = await modal(p.id ? 'Edit ledger — ' + p.name : 'New ledger',
    LEDGER_FORM(p), [
      { label: 'Cancel', value: null },
      {
        label: 'Save', cls: 'primary', value: true, before: async bg => {
          const data = readForm(bg);
          const st = S.meta.states.find(x => x.code === data.state_code);
          data.state = st ? st.name : '';
          try {
            await (p.id ? api.put('/api/ledgers/' + p.id, data)
              : api.post('/api/ledgers', data));
          } catch (e) { bad(e.message); return false; }
        }
      }
    ], {
      // Four columns of address and GST detail, the same as the company
      // dialog; at the standard width the boxes came out too narrow to read.
      wide: true,
      onOpen: bg => {
        const g = $('[name=gstin]', bg);
        g.oninput = () => { g.value = g.value.toUpperCase(); };
        const kind = $('[name=party_kind]', bg);
        if (!p.id) kind.onchange = () => {
          $('[name=group_id]', bg).value = defaultGroupFor(kind.value);
        };
      }
    });
  if (saved) { invalidate(); ok('Ledger saved.'); }
  return saved;
}
const editLedger = editParty;

/** The party book as a dialog: search it, look down it, click the customer
 *  the document is for.
 *
 *  The same way in as the product list, for the same reason -- the box on the
 *  form searches as you type, which is quick once you can name the party, and
 *  no help at all when you cannot. Here they are laid out with the city, the
 *  GSTIN and the balance standing against them, so the right one can be
 *  picked by eye and the wrong one -- the near-namesake two towns over -- is
 *  told apart before the bill is raised.
 *
 *  `kind` narrows to customers or suppliers, as the form itself does;
 *  `ledgers` opens the whole chart of accounts instead, for the journal and
 *  the statement screen; `source` hands the searching to the server, which is
 *  how the order screen looks across every firm at once. Resolves to the
 *  party chosen, or null.
 */
async function chooseParty(opts) {
  opts = opts || {};
  // Which book is being opened, and so which columns are worth showing:
  //   party     -- this firm's customers and suppliers, with their balances
  //   ledger    -- the whole chart of accounts, by group
  //   crossfirm -- the order screen, which looks for a customer in every firm
  //                at once and so asks the server rather than a loaded list
  const book = opts.ledgers ? 'ledger' : opts.source ? 'crossfirm' : 'party';
  const remote = opts.source || null;
  const title = opts.title || (book === 'ledger' ? 'Select ledger'
    : opts.kind === 'supplier' ? 'Select supplier'
    : opts.kind === 'customer' || book === 'crossfirm' ? 'Select customer'
    : 'Select party');

  let all = [];
  if (!remote) {
    const every = book === 'ledger' ? await loadLedgers() : await loadParties();
    // The form asks for one side of the book; 'both' answers either.
    all = opts.kind
      ? every.filter(p => p.party_kind === opts.kind || p.party_kind === 'both')
      : every;
  }
  // Nearly two and a half thousand accounts sit in the biggest firm. Drawing
  // them all is slow and nobody reads past the first screenful anyway, so the
  // list is capped and says so -- the search box is what gets you there.
  const CAP = 300;

  // Grouped by whichever column actually sorts this book: parties by the town
  // they are in, accounts by the group they hang under. A cross-firm search
  // never holds the whole set, so it has nothing to group by.
  const byGroup = book === 'ledger';
  // A town is read the way it is meant, not the way it was typed: "Rajkot",
  // "RAJKOT" and "VAVDI  RAJKOT" were separate entries, and the phone
  // numbers SHREE holds in some city boxes were offered as towns (29 Sep 2026).
  const bucketOf = p => byGroup ? (p.group_name || '')
    : String(p.city || '').trim().replace(/\s+/g, ' ').toUpperCase();
  const buckets = remote ? []
    : [...new Set(all.map(bucketOf)
        // (Eight digits or more is a phone number; "JAMNAGAR 361004" stays.)
        .filter(b => b && (byGroup || !/\d{8}/.test(b.replace(/\D+/g, '')))))].sort();

  const body = `
    <div class="row" style="gap:9px">
      <div style="flex:1"><input id="pp-q" autocomplete="off"
        placeholder="Search name, code, ${book === 'ledger' ? 'group'
          : 'city, GSTIN or phone'}…" value="${esc(pickTerm(opts))}"></div>
      ${remote ? '' : `<select id="pp-b" style="width:calc(190px * var(--typescale));flex:none">
        <option value="">${byGroup ? 'All groups' : 'All cities'}</option>
        ${buckets.map(b => '<option>' + esc(b) + '</option>').join('')}
      </select>`}
    </div>
    <div class="tbl-scroll pick-table" id="pp-list"
      style="max-height:52vh;overflow-y:auto;margin-top:12px;
             border:1px solid var(--line);border-radius:8px"></div>
    <p id="pp-note" style="margin:9px 0 0;font-size:11.5px;color:var(--muted)"></p>`;

  return modal(title, body, [{ label: 'Cancel', value: null }], {
    wide: true,
    onOpen: (bg, close) => {
      const q = $('#pp-q', bg), sel = $('#pp-b', bg);
      const host = $('#pp-list', bg), note = $('#pp-note', bg);
      let rows = [], shown = 0, hi = 0, seq = 0;

      function paintHi() {
        $$('tbody tr', host).forEach((tr, n) => tr.classList.toggle('hi', n === hi));
      }
      /** The balance, in the colour and the Dr/Cr the rest of the app uses. */
      function balCell(p) {
        const b = num(p.balance);
        return `<td class="r num" style="color:${b > 0 ? 'var(--ok)'
          : b < 0 ? 'var(--bad)' : 'inherit'}">${fmt(Math.abs(b))}
          ${b ? (b > 0 ? 'Dr' : 'Cr') : ''}</td>`;
      }
      function head() {
        return `<tr><th>${book === 'ledger' ? 'Ledger' : 'Party'}</th>
          <th>${byGroup ? 'Group' : 'City / State'}</th>
          ${book === 'ledger' ? ''
            : book === 'crossfirm' ? '<th>Billed by</th><th>GSTIN</th>'
            : '<th>Phone</th><th>GSTIN</th>'}
          ${book === 'crossfirm' ? '' : '<th class="r">Balance</th>'}</tr>`;
      }
      function row(p, n) {
        const over = num(p.credit_limit) > 0 && num(p.balance) > num(p.credit_limit);
        return `<tr data-n="${n}">
          <td><b>${esc(p.name)}</b>${p.code ? ' <span class="tag">' + esc(p.code) + '</span>' : ''}
            ${over ? ' <span class="tag bad" title="past the credit limit on this party">over limit</span>' : ''}
            ${p.party_kind === 'supplier' && !opts.kind
              ? ' <span class="tag warn">supplier</span>' : ''}</td>
          <td>${esc(byGroup || p.other ? (p.group_name || '')
            : [p.city, p.state].filter(Boolean).join(', '))}</td>
          ${book === 'ledger' ? ''
            : book === 'crossfirm'
              // An account that is not a customer -- a bank, an expense head,
              // found by the Accounts box -- is billed by nobody, and saying
              // "no billing firm" in red made it look broken.
              ? p.other ? '<td style="color:var(--muted)">account</td><td></td>'
              : `<td>${p.bill_name ? esc(p.bill_name)
                  : p.company_name ? '<span style="color:var(--muted)">in ' + esc(p.company_name) + '</span>'
                  : '<span class="tag bad">no billing firm</span>'}</td>
                 <td style="font-family:var(--mono);font-size:11.5px">${esc(p.gstin || '—')}</td>`
              : `<td style="font-size:12.5px">${esc(p.phone || '')}</td>
                 <td style="font-family:var(--mono);font-size:11.5px">${esc(p.gstin || '—')}</td>`}
          ${book === 'crossfirm' ? '' : balCell(p)}</tr>`;
      }
      async function draw() {
        const t = q.value.trim();
        if (remote) {
          // The answer comes back over the wire, and a slow one must not
          // overwrite a newer answer that already landed.
          const mine = ++seq;
          const got = await remote(t);
          if (mine !== seq) return;
          rows = got;
        } else {
          const b = sel ? sel.value : '';
          const pool = b ? all.filter(p => bucketOf(p) === b) : all;
          // Same ranking as the box on the form: the closest reading of the
          // name first, punctuation and all -- "jk eng" still finds J.K.ENG.
          rows = searchList(t, pool, p => book === 'ledger'
            ? [p.name, p.code, p.group_name, p.city]
            : [p.name, p.code, p.city, p.gstin, p.phone], 100000);
        }
        const view = rows.slice(0, CAP);
        shown = view.length;
        if (hi > shown - 1) hi = shown - 1;
        if (hi < 0) hi = 0;
        host.innerHTML = shown
          ? '<table><thead>' + head() + '</thead><tbody>'
            + view.map(row).join('') + '</tbody></table>'
          : `<div class="empty"><b>Nobody matches</b>
             Try another word${sel ? ', or clear the ' + (byGroup ? 'group' : 'city') : ''}.</div>`;
        note.textContent = rows.length > CAP
          ? 'Showing the first ' + CAP + ' of ' + rows.length
            + ' — type a little more to narrow it.'
          : '↑ ↓ moves, PgUp PgDn a page at a time, Enter takes the highlighted row.';
        paintHi();
        $$('tbody tr', host).forEach(tr => {
          tr.onmouseenter = () => { hi = num(tr.dataset.n); paintHi(); };
          tr.onclick = () => close(rows[num(tr.dataset.n)]);
        });
      }
      function move(step) {
        if (!shown) return;
        hi = Math.max(0, Math.min(shown - 1, hi + step));
        paintHi();
        const tr = $$('tbody tr', host)[hi];
        if (tr) KB.reveal(tr);
      }
      // The dialog opens on its search box: the box behind it still had the
      // cursor, and narrowing the list is the first thing anyone does here.
      // Opened by typing, it goes on from the last letter rather than
      // selecting it, or the next letter would type over the first.
      q.value = pickTerm(opts);
      pickLast.term = q.value;
      q.focus();
      if (opts.typed) q.setSelectionRange(q.value.length, q.value.length);
      else q.select();
      let timer;
      q.oninput = () => {
        hi = 0;
        pickLast.term = q.value;
        clearTimeout(timer);
        timer = setTimeout(draw, remote ? 160 : 0);
      };
      if (sel) sel.onchange = () => { hi = 0; draw(); q.focus(); };
      // Enter picks the party rather than walking to the next box -- this
      // dialog is one question. The dropdown keeps its own arrow keys.
      bg.addEventListener('keydown', e => {
        if (sel && e.target === sel && e.key !== 'Enter') return;
        if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
        // A page at a time, for a book of two thousand.
        else if (e.key === 'PageDown') { e.preventDefault(); move(10); }
        else if (e.key === 'PageUp') { e.preventDefault(); move(-10); }
        else if (e.key === 'Enter' && rows[hi]) { e.preventDefault(); close(rows[hi]); }
      });
      draw();
    }
  });
}

route(/^\/(ledgers|parties)$/, async (view, m, q) => {
  const partiesOnly = q.parties === '1' || m[0] === 'parties';
  setTitle(partiesOnly ? 'Parties' : 'Chart of accounts',
    partiesOnly ? 'Customers and suppliers you bill'
      : 'Every account in the books, grouped as in the old system', [
    { label: '⤓ CSV', onClick: () => location.href = firmUrl('/api/export/ledgers') },
    {
      label: partiesOnly ? '＋ New party' : '＋ New ledger', cls: 'primary',
      onClick: async () => {
        if (await editParty(partiesOnly ? { party_kind: 'customer' } : {})) render();
      }
    }
  ]);
  view.innerHTML = `
    <div class="card"><div class="bd">
      <div class="row"><div style="flex:1;max-width:340px">
        <label>Search</label>
        <input id="q" placeholder="Name, code, city or GSTIN…"></div>
        <div><label>Group</label><select id="grp" style="width:calc(220px * var(--typescale))">
          <option value="">All groups</option>
          ${S.meta.groups.map(g => `<option value="${g.id}">${esc(g.name)}</option>`).join('')}
        </select></div>
        <div><label>Party type</label><select id="kind" style="width:calc(150px * var(--typescale))">
          <option value="">Any</option><option value="customer">Customers</option>
          <option value="supplier">Suppliers</option></select></div>
        <div class="spacer"></div>
        <div class="hint" style="margin:0" id="count"></div>
      </div></div></div>
    <div class="card"><div class="bd flush tbl-scroll" id="list"></div></div>`;

  const mem = screenState('/' + m[0], {
    parties: { get: () => q.parties || '', set: () => {} },
    search: '#q', group: '#grp', kind: '#kind'
  });

  async function load() {
    mem.save();
    const here = onSearch();
    const rows = await api.get('/api/ledgers', {
      search: $('#q').value.trim(), group_id: $('#grp').value,
      kind: $('#kind').value, parties_only: partiesOnly ? 1 : ''
    });
    if (!here()) return;
    const dr = rows.reduce((a, r) => a + Math.max(0, num(r.balance)), 0);
    const cr = rows.reduce((a, r) => a + Math.max(0, -num(r.balance)), 0);
    $('#count').textContent = rows.length + (partiesOnly ? ' part' + (rows.length === 1 ? 'y' : 'ies') : ' ledger(s)');
    const ticks = () => bulkRows($('#list'), 'ledger', {
      noun: partiesOnly ? ['party', 'parties'] : ['account', 'accounts'],
      reload: load
    });
    if (!rows.length) {
      $('#list').innerHTML = '<div class="empty"><b>Nothing here</b>No ledger matches this filter.</div>';
      ticks();
      return;
    }
    const bind = b => {
      b.onclick = async ev => {
        const id = num(ev.target.closest('tr').dataset.id);
        const act = b.dataset.act;
        if (act === 'ledger') { location.hash = '#/accounts?ledger_id=' + id; return; }
        if (act === 'edit') {
          const p = await api.get('/api/ledgers/' + id);
          if (await editParty(p)) load();
          return;
        }
        if (await deleteAccount(id)) load();
      };
    };
    await fillRows($('#list'), `<table><thead><tr>
        <th>Name</th><th>Group</th><th>Type</th><th>City / State</th><th>GSTIN</th>
        <th class="r">Balance</th><th class="r">Actions</th></tr></thead><tbody>`,
      rows.map(p => {
        const b = num(p.balance);
        const over = p.credit_limit > 0 && b > p.credit_limit;
        return `<tr data-id="${p.id}"${p.is_system ? ' data-nodel' : ''}>
          <td><b>${esc(p.name)}</b>${p.code ? ' <span class="tag">' + esc(p.code) + '</span>' : ''}
            ${p.active ? '' : ' <span class="tag warn">inactive</span>'}
            ${p.is_system ? ' <span class="tag info">built-in</span>' : ''}
            ${over ? ' <span class="tag bad">over limit</span>' : ''}</td>
          <td class="nw" style="font-size:12.5px;color:var(--muted)">${esc(p.group_name)}</td>
          <td>${p.party_kind ? '<span class="tag ' + (p.party_kind === 'supplier' ? 'warn' : 'info') + '">' + esc(p.party_kind) + '</span>' : ''}</td>
          <td>${esc([p.city, p.state].filter(Boolean).join(', '))}</td>
          <td style="font-family:var(--mono);font-size:11.5px">${esc(p.gstin || '—')}</td>
          <td class="r num" style="color:${b > 0 ? 'var(--ok)' : b < 0 ? 'var(--bad)' : 'inherit'}">
            ${fmt(Math.abs(b))} ${b ? (b > 0 ? 'Dr' : 'Cr') : ''}</td>
          <td class="r" style="white-space:nowrap">
            <button class="link" data-act="ledger">Account</button>
            <button class="link" data-act="edit" data-enter>Edit</button>
            ${p.is_system ? '' : '<button class="link" data-act="del" style="color:var(--bad)">Del</button>'}</td>
        </tr>`;
      }), `</tbody><tfoot><tr><td colspan="5">${rows.length} ledger(s)</td>
        <td class="r num">${fmt(dr)} Dr / ${fmt(cr)} Cr</td><td></td></tr></tfoot></table>`, {
        here,
        each: trs => trs.forEach(tr => $$('button[data-act]', tr).forEach(bind)),
        first: ticks
      });
  }
  if (partiesOnly) $('#kind').value = '';
  mem.restore(q);          // after the default, so a remembered one stands
  $('#q').oninput = () => { clearTimeout(window._pt); window._pt = setTimeout(load, 160); };
  $('#grp').onchange = load;
  $('#kind').onchange = load;
  load();
});

/* ================================================================= items */
const ITEM_FORM = (it, cats) => `
  <div class="grid g2">
    <div><label>Item name *</label><input name="name" value="${esc(it.name || '')}"></div>
    <div><label>Code / SKU</label><input name="code" value="${esc(it.code || '')}"></div>
  </div>
  <div class="grid ${cats ? 'g4' : 'g3'}" style="margin-top:12px">
    <div><label>Type</label><input name="category" value="${esc(it.category || '')}" list="cat-list"
      placeholder="pick one, or type a new type"></div>
    ${cats ? `<div><label title="The category Billing Stock shows it under">Stock category</label>
      <select name="stock_cat"><option value="">No category</option>
      ${cats.map(c => `<option${c === (it.stock_cat || '') ? ' selected' : ''}>${esc(c)}</option>`).join('')}
    </select></div>` : ''}
    <div><label>HSN / SAC</label><input name="hsn" value="${esc(it.hsn || '')}"></div>
    <div><label>Unit</label><select name="unit">
      ${S.meta.units.map(u => `<option${(it.unit || 'KGS') === u ? ' selected' : ''}>${u}</option>`).join('')}
    </select></div>
  </div>
  <div class="grid g4" style="margin-top:12px">
    <div><label>GST %</label><select name="gst_rate">
      ${S.meta.gst_rates.map(r => `<option value="${r}"${num(it.gst_rate) === r ? ' selected' : ''}>${r}%</option>`).join('')}
    </select></div>
    <div><label>Cess %</label><input name="cess_rate" class="num" value="${num(it.cess_rate).toFixed(2)}"></div>
    <div><label>Sale rate</label><input name="sale_rate" class="num" value="${num(it.sale_rate).toFixed(2)}"></div>
    <div><label>Purchase rate</label><input name="purchase_rate" class="num" value="${num(it.purchase_rate).toFixed(2)}"></div>
  </div>
  <div class="grid g3" style="margin-top:12px">
    <div><label>Opening stock</label><input name="opening_qty" class="num" value="${num(it.opening_qty).toFixed(3)}"></div>
    <div><label>Reorder level</label><input name="reorder_level" class="num" value="${num(it.reorder_level).toFixed(3)}"></div>
    <div style="display:flex;align-items:flex-end;gap:16px;padding-bottom:7px;flex-wrap:wrap">
      <label style="margin:0" title="Rate is per kilo; the Quantity on the line prices it">
        <input type="checkbox" name="by_weight" style="width:auto"
        ${it.id === undefined || it.by_weight ? 'checked' : ''}> Priced per kg</label>
      <label style="margin:0"><input type="checkbox" name="track_stock" style="width:auto"
        ${it.id === undefined || it.track_stock ? 'checked' : ''}> Track stock</label>
      <label style="margin:0"><input type="checkbox" name="active" style="width:auto"
        ${it.id === undefined || it.active ? 'checked' : ''}> Active</label>
    </div>
  </div>
  <datalist id="cat-list">${[...new Set(S.items.map(i => i.category).filter(Boolean))]
    .map(c => '<option>' + esc(c) + '</option>').join('')}</datalist>`;

/** The item master's form. `existing` with an id is edited; without one it
 *  is what a new item starts with (a purchase line passes the name typed and
 *  the HSN on the line). Resolves to the item as saved, or null. */
async function editItem(existing, title) {
  const it = existing || {};
  await loadItems();
  // Billing Stock's categories, and for a new product the one its name
  // points to. Without them the box is left off and nothing changes.
  let cats = null, start = it;
  try {
    const c = await api.get('/api/stock/cats', it.id ? {} : { name: it.name || '', hsn: it.hsn || '' });
    cats = c.cats.map(x => x.name);
    if (!it.id && it.stock_cat === undefined) start = Object.assign({}, it, { stock_cat: c.guess || '' });
  } catch (e) { /* left off */ }
  let made = null;
  const saved = await modal(title || (it.id ? 'Edit item — ' + it.name : 'New item'),
    ITEM_FORM(start, cats), [
      { label: 'Cancel', value: null },
      {
        label: 'Save', cls: 'primary', value: true, before: async bg => {
          const data = readForm(bg);
          try {
            made = await (it.id ? api.put('/api/items/' + it.id, data)
              : api.post('/api/items', data));
          } catch (e) { bad(e.message); return false; }
        }
      }
    ], { onOpen: bg => { const n = $('input[name=name]', bg); if (n && !it.id) n.select(); } });
  if (!saved) return null;
  invalidate();
  ok(it.id ? 'Item saved.' : '“' + ((made && made.name) || 'Item') + '” added to the products.');
  return made || saved;
}

/** A new product made from a purchase (3 Oct 2026: "add an option to add new
 *  type of product in purchase"). The whole product form -- its type, HSN,
 *  unit, GST and rate -- starting from what was typed, saved into the firm
 *  the purchase is bought into. Resolves to the product, or null. */
function newProduct(name, hsn) {
  return editItem({ name: String(name || '').replace(/\s+/g, ' ').trim().toUpperCase(),
                    hsn: HSN_DIGITS(hsn || ''), unit: 'KGS', gst_rate: gstDefault() },
                  'New product');
}

/** The item master as a dialog: search it, look down it, click the product
 *  being sold.
 *
 *  The box on a line searches as you type, which is the quick way in but only
 *  works when you already know what the product is called. This is the other
 *  way: everything on the books laid out with the HSN, unit, GST rate and rate
 *  that will land on the line, so the right one can be picked by eye.
 *
 *  Resolves to the item chosen, or null if the dialog was closed. */
/** Re-pick the product behind each line of a bill, against the HSN.
 *
 *  A challan is written in the order firm's book, and every line of it is the
 *  one generic CRCA that book holds -- no HSN on it, because a challan
 *  carries no tax and nothing ever had to ask which grade it was. The bill
 *  does have to say: the billing firms keep twenty-odd CRCA grades apart by
 *  their HSN, and the portal refuses a line without one. So when a challan
 *  becomes a bill the product is chosen here, rather than guessed at from a
 *  name that cannot tell them apart.
 *
 *  Lines are grouped by what they are called. A steel bill is one item cut
 *  eight ways, and picking it eight times is picking it seven times too many.
 *
 *  **The money is never touched** -- weight, rate, discount and the piece
 *  count are what was weighed and agreed, and they stay. Only what the goods
 *  are called, their HSN and their tax rate come across from the product.
 *
 *  Returns the number of lines changed.
 */
async function pickProducts(lines, opts) {
  opts = opts || {};
  const groups = [], at = new Map();
  lines.forEach((l, i) => {
    const key = (l.description || '').trim().toLowerCase();
    if (!at.has(key)) {
      at.set(key, groups.length);
      groups.push({ label: (l.description || '').trim() || '(no name)',
                    rows: [], hsn: HSN_DIGITS(l.hsn), pick: null });
    }
    groups[at.get(key)].rows.push(i);
  });

  const body = `
    <div class="tbl-scroll" id="pk-list" style="max-height:52vh;
      overflow-y:auto;border:1px solid var(--line);border-radius:8px"></div>
    <p class="hint" style="margin:10px 0 0">Weights, rates and the piece count
      stay exactly as the challan had them — only the product name, its HSN
      and its tax rate come across.</p>`;

  const go = await modal(opts.title || 'Pick the product for each line', body, [
    { label: 'Cancel', value: null },
    { label: 'Apply', cls: 'primary', value: true }
  ], {
    wide: true,
    onOpen: bg => {
      const host = $('#pk-list', bg);
      function paint(after) {
        host.innerHTML = `<table><thead><tr>
          <th>On the challan</th><th class="c">Lines</th>
          <th>Product on the bill</th><th>HSN</th><th class="r">GST</th>
          <th></th></tr></thead><tbody>` + groups.map((g, n) => `
          <tr><td><b>${esc(g.label)}</b></td>
            <td class="c">${g.rows.length}</td>
            <td>${g.pick ? esc(g.pick.name)
              : '<span style="color:var(--muted)">— not picked —</span>'}</td>
            <td>${g.pick
              ? (hsnOfItem(g.pick)
                 ? '<span class="tag ok" style="font-family:var(--mono)">'
                   + esc(hsnOfItem(g.pick)) + '</span>'
                 : '<span class="tag bad">still none</span>')
              : (hsnOk(g.hsn)
                 ? '<span class="tag" style="font-family:var(--mono)">'
                   + esc(g.hsn) + '</span>'
                 : '<span class="tag warn">none</span>')}</td>
            <td class="r num">${g.pick ? fmt(num(g.pick.gst_rate), 0) + '%' : ''}</td>
            <td class="r"><button type="button" class="sm" data-n="${n}">${
              g.pick ? 'Change…' : 'Choose…'}</button></td>
          </tr>`).join('') + '</tbody></table>';
        $$('[data-n]', host).forEach(b => {
          b.onclick = async () => {
            const g = groups[+b.dataset.n];
            const i = await chooseItem({ mode: opts.mode, taxFree: opts.taxFree,
                                         term: g.pick ? '' : g.label });
            if (i) { g.pick = i; paint(+b.dataset.n); }
          };
        });
        // On to the next line still to pick, not back to the first: seven
        // products are picked one after another without walking back down
        // past the ones done. With all of them picked, Apply.
        if (after === undefined) {
          const first = $('[data-n]', host);
          if (first) first.focus({ preventScroll: true });
          return;
        }
        let next = -1;
        for (let n = 1; n <= groups.length && next < 0; n++) {
          const j = (after + n) % groups.length;
          if (!groups[j].pick) next = j;
        }
        const to = next >= 0 ? $('[data-n="' + next + '"]', host)
          : $('.ft button.primary', bg);
        if (to) { to.focus({ preventScroll: true }); KB.reveal(to); }
      }
      paint();
    }
  });
  if (!go) return 0;

  let changed = 0;
  groups.forEach(g => {
    if (!g.pick) return;
    g.rows.forEach(i => {
      const l = lines[i];
      l.item_id = g.pick.id;
      l.description = g.pick.name;
      l.hsn = hsnOfItem(g.pick);
      // The rate the product is taxed at is the more specific answer than the
      // firm default the draft fell back on. A product with none named keeps
      // whatever the line already had.
      if (num(g.pick.gst_rate)) l.gst_rate = num(g.pick.gst_rate);
      if (num(g.pick.cess_rate)) l.cess_rate = num(g.pick.cess_rate);
      changed++;
    });
  });
  return changed;
}

async function chooseItem(opts) {
  opts = opts || {};
  // On a purchase, a product never bought before is made here, in full, and
  // comes back onto the line; Cancel on its form comes back to the list.
  if (opts.mode === 'PURCHASE') {
    for (;;) {
      const got = await pickItem(opts);
      if (!got || !got._new) return got;
      const made = await newProduct(got.name, opts.hsn);
      if (made && made.id) return made;
      opts = Object.assign({}, opts, { term: got.name, typed: true });
    }
  }
  return pickItem(opts);
}

async function pickItem(opts) {
  const purchase = opts.mode === 'PURCHASE';
  // Outside GST the HSN and the rate it is taxed at say nothing -- the challan
  // book has no use for either -- so those columns come off the list.
  const taxed = !opts.taxFree;
  const all = await loadItems();
  const cats = [...new Set(all.map(i => i.category).filter(Boolean))].sort();
  const rateOf = i => num(purchase ? i.purchase_rate : i.sale_rate);

  const body = `
    <div class="row" style="gap:9px">
      <div style="flex:1"><input id="ip-q" autocomplete="off"
        placeholder="Search product name, code, HSN or category…"
        value="${esc(pickTerm(opts))}"></div>
      <select id="ip-cat" style="width:calc(190px * var(--typescale));flex:none">
        <option value="">All categories</option>
        ${cats.map(c => '<option>' + esc(c) + '</option>').join('')}
      </select>
    </div>
    <div class="tbl-scroll pick-table" id="ip-list"
      style="max-height:52vh;overflow-y:auto;margin-top:12px;
             border:1px solid var(--line);border-radius:8px"></div>
    <p style="margin:9px 0 0;font-size:11.5px;color:var(--muted)">
      ↑ ↓ moves, PgUp PgDn a page at a time, Enter takes the highlighted row.
      ${taxed ? 'Its HSN and GST rate always come across. ' : ''}Its unit and
      ${purchase ? 'purchase' : 'sale'} rate come across onto a line that has
      no rate yet — a line already priced, as one on a bill made from a
      challan is, keeps what it was raised with until you type over it.</p>`;

  const buttons = [{ label: 'Cancel', value: null }];
  if (purchase) {
    buttons.unshift({ label: '＋ New product', value: 'new',
                      before: bg => { pickLast.term = $('#ip-q', bg).value; } });
  }
  const got = await modal('Select product', body, buttons, {
    wide: true,
    onOpen: (bg, close) => {
      const q = $('#ip-q', bg), cat = $('#ip-cat', bg), host = $('#ip-list', bg);
      let rows = [], hi = 0;

      function paintHi() {
        $$('tbody tr', host).forEach((tr, n) => tr.classList.toggle('hi', n === hi));
      }
      function draw() {
        const t = q.value.trim(), c = cat.value;
        const pool = all.filter(i => !c || i.category === c);
        // Closest first, as the customer window reads names: a product whose
        // name starts with what is typed, then one that holds it, then the
        // words anywhere -- code, HSN, category. Kept in name order only,
        // "CRCA" put CR--- first (its category is CRCA) and Enter took it
        // (29 Sep 2026).
        rows = t ? searchList(t, pool, i => [i.name, i.code, i.hsn, i.category],
                              pool.length)
          : pool.slice();
        // A purchase can bring in something never bought before, and this
        // list only knew the products there were -- "No product matches",
        // and no way on but to type the name onto the line by hand (27 Sep
        // 2026). So what is typed here is offered as a new product, at the
        // foot of the list; saving the purchase adds it to the products and
        // its weight to Billing Stock.
        const typed = q.value.trim().replace(/\s+/g, ' ');
        const squash = n => String(n || '').replace(/\s+/g, ' ').trim().toUpperCase();
        if (purchase && typed && !all.some(i => squash(i.name) === squash(typed))) {
          rows.push({ id: null, _new: true, name: typed, hsn: '', unit: 'KGS',
                      by_weight: 1, gst_rate: 0, cess_rate: 0, purchase_rate: 0 });
        }
        if (hi > rows.length - 1) hi = rows.length - 1;
        if (hi < 0) hi = 0;
        host.innerHTML = rows.length ? `<table><thead><tr>
          <th>Product</th>${taxed ? '<th>HSN / SAC</th>' : ''}<th class="c">Unit</th>
          ${taxed ? '<th class="c">GST</th>' : ''}
          <th class="r">${purchase ? 'Purch. rate' : 'Sale rate'}</th>
          <th class="r">In stock</th></tr></thead><tbody>`
          + rows.map((i, n) => i._new ? `<tr data-n="${n}">
            <td colspan="${taxed ? 6 : 4}"><b>＋ New product: ${esc(i.name)}</b>
              <div style="color:var(--muted);font-size:11.5px">Not in the list
              yet — Enter opens the product form (its type, HSN, GST and rate),
              and it comes onto this line.</div></td></tr>`
            : `<tr data-n="${n}">
            <td><b>${esc(i.name)}</b>${i.code ? ' <span class="tag">' + esc(i.code) + '</span>' : ''}
              ${i.category ? '<div style="color:var(--muted);font-size:11.5px">'
                + esc(i.category) + '</div>' : ''}</td>
            ${taxed ? `<td style="font-family:var(--mono);font-size:12.5px">${i.hsn
              ? esc(i.hsn)
              : '<span class="tag warn" title="No HSN on this product — set one under Items">not set</span>'}</td>` : ''}
            <td class="c">${esc(i.unit || '')}${i.by_weight
              ? ' <span class="tag" title="rate is per kilo">/kg</span>' : ''}</td>
            ${taxed ? `<td class="c">${fmt(i.gst_rate, 2).replace(/\.00$/, '')}%</td>` : ''}
            <td class="r num">${fmt(rateOf(i))}</td>
            <td class="r num">${i.track_stock ? qty(i.stock) : '—'}</td>
          </tr>`).join('') + '</tbody></table>'
          : `<div class="empty"><b>No product matches</b>
             Try another word, or clear the category.</div>`;
        paintHi();
        $$('tbody tr', host).forEach(tr => {
          tr.onmouseenter = () => { hi = num(tr.dataset.n); paintHi(); };
          tr.onclick = () => close(rows[num(tr.dataset.n)]);
        });
      }
      function move(step) {
        if (!rows.length) return;
        hi = Math.max(0, Math.min(rows.length - 1, hi + step));
        paintHi();
        const tr = $$('tbody tr', host)[hi];
        if (tr) KB.reveal(tr);
      }
      // The dialog opens on its search box: the first thing anyone does here
      // is narrow the list, and the box behind it still had the cursor.
      // Opened by typing, it goes on from the last letter (see chooseParty).
      q.value = pickTerm(opts);
      pickLast.term = q.value;
      q.focus();
      if (opts.typed) q.setSelectionRange(q.value.length, q.value.length);
      else q.select();
      q.oninput = () => { hi = 0; pickLast.term = q.value; draw(); };
      cat.onchange = () => { hi = 0; draw(); q.focus(); };
      // Enter here picks the product rather than walking to the next box --
      // this dialog is one question, and the answer is the highlighted row.
      // The category dropdown keeps its own arrow keys: inside a <select>
      // they choose the category, which is the whole point of being in it.
      bg.addEventListener('keydown', e => {
        // The buttons at the foot answer for themselves: Enter on
        // "＋ New product" or Cancel is that button, not the highlighted row.
        if (e.target.closest && e.target.closest('.ft')) return;
        if (e.target === cat && e.key !== 'Enter') return;
        if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
        else if (e.key === 'PageDown') { e.preventDefault(); move(10); }
        else if (e.key === 'PageUp') { e.preventDefault(); move(-10); }
        else if (e.key === 'Enter' && rows[hi]) { e.preventDefault(); close(rows[hi]); }
      });
      draw();
    }
  });
  // The button: a new product, named by what was in the search box.
  if (got === 'new') return { id: null, _new: true, name: (pickLast.term || '').trim() };
  return got;
}

route(/^\/items$/, async (view, _m, q) => {
  setTitle('Items', 'Products and services you bill for', [
    { label: '⤓ CSV', onClick: () => location.href = firmUrl('/api/export/items') },
    { label: '＋ New item', cls: 'primary', onClick: async () => { if (await editItem()) render(); } }
  ]);
  view.innerHTML = `
    <div class="card"><div class="bd"><div class="row">
      <div style="flex:1;max-width:360px"><label>Search</label>
        <input id="q" placeholder="Name, code, HSN or category…"></div>
      <div class="spacer"></div>
      <div class="hint" style="margin:0" id="count"></div>
    </div></div></div>
    <div class="card"><div class="bd flush tbl-scroll" id="list"></div></div>`;

  const mem = screenState('/items', { search: '#q' }).restore(q);

  async function load() {
    mem.save();
    const here = onSearch();
    const rows = await api.get('/api/items', { search: $('#q').value.trim() });
    if (!here()) return;
    $('#count').textContent = rows.length + ' item(s)';
    $('#list').innerHTML = rows.length ? `<table><thead><tr>
      <th>Item</th><th>Type</th><th>Stock category</th><th>HSN</th><th class="c">Unit</th><th class="c">GST</th>
      <th class="r">Sale rate</th><th class="r">Purch. rate</th><th class="r">In stock</th>
      <th class="r">Actions</th>
      </tr></thead><tbody>` + rows.map(i => `<tr data-id="${i.id}">
        <td><b>${esc(i.name)}</b>${i.code ? ' <span class="tag">' + esc(i.code) + '</span>' : ''}
          ${i.active ? '' : ' <span class="tag warn">inactive</span>'}</td>
        <td>${esc(i.category || '')}</td>
        <td>${i.stock_cat ? esc(i.stock_cat) : '<span style="color:var(--muted)">—</span>'}</td>
        <td style="font-family:var(--mono);font-size:12px">${esc(i.hsn || '—')}</td>
        <td class="c">${esc(i.unit)}${i.by_weight ? '' : ' <span class="tag" title="priced on quantity, not weight">per unit</span>'}</td>
        <td class="c">${fmt(i.gst_rate, 2).replace(/\.00$/, '')}%</td>
        <td class="r num">${fmt(i.sale_rate)}</td><td class="r num">${fmt(i.purchase_rate)}</td>
        <td class="r num">${i.track_stock ? qty(i.stock) + (i.low ? ' <span class="tag bad">low</span>' : '') : '—'}</td>
        <td class="r" style="white-space:nowrap">
          <button class="link" data-act="edit" data-enter>Edit</button>
          <button class="link" data-act="del" style="color:var(--bad)">Del</button></td>
      </tr>`).join('') + `</tbody><tfoot><tr><td colspan="9">${rows.length} item(s)</td>
        <td></td></tr></tfoot></table>`
      : '<div class="empty"><b>No items yet</b>Add the products or services you sell.</div>';

    $$('#list button[data-act]').forEach(b => {
      b.onclick = async ev => {
        const id = num(ev.target.closest('tr').dataset.id);
        if (b.dataset.act === 'edit') {
          const it = await api.get('/api/items/' + id);
          if (await editItem(it)) load();
          return;
        }
        if (await deleteItem(id)) load();
      };
    });
    bulkRows($('#list'), 'item', { reload: load });
  }
  $('#q').oninput = () => { clearTimeout(window._it); window._it = setTimeout(load, 160); };
  load();
});

/* ======================================================== document editor
   Shared by sales invoices, purchases, credit/debit notes and orders.      */

const DOC_LABEL = {
  SALE: 'Tax Invoice', PURCHASE: 'Purchase Bill',
  CREDIT: 'Credit Note', DEBIT: 'Debit Note'
};

// A code the portal will read: 4, 6 or 8 digits and nothing else.
const HSN_DIGITS = v => String(v == null ? '' : v).replace(/\D/g, '');
const hsnOk = v => [4, 6, 8].indexOf(HSN_DIGITS(v).length) >= 0;
const HSN_IN_NAME = /(?:^|[^\d])(\d{8}|\d{6}|\d{4})(?![\d])/g;

/** An item's HSN -- its own column first, then the code written into its
 *  name, which is where years of them were typed. `app/fill_hsn.py` moves
 *  those into the column for good; this is what catches the ones typed that
 *  way from now on, and it reads the name the same way `eway.hsn_of` does.
 *  A name holding two different codes is not read: two answers is no answer.
 */
function hsnOfItem(it) {
  if (!it) return '';
  if (hsnOk(it.hsn)) return HSN_DIGITS(it.hsn);
  const found = [];
  let m;
  HSN_IN_NAME.lastIndex = 0;
  while ((m = HSN_IN_NAME.exec(String(it.name || '')))) found.push(m[1]);
  const one = [...new Set(found)];
  return one.length === 1 ? one[0] : '';
}

/** The GST rate a fresh line starts at: the firm's default, or none outside
 *  GST. Items bring their own rate when one is picked. */
function gstDefault() {
  const co = (S.meta && S.meta.company) || {};
  return (co.tax_mode === 'none') ? '' : num(co.default_gst || 18);
}

function blankLine() {
  return {
    // Every box starts empty so a new line can be typed straight through
    // without clearing defaults first. Picking an item from the list fills
    // the unit, HSN, rate and GST rate from the item master.
    item_id: null, description: '', hsn: '', unit: '', qty: '', weight: '',
    by_weight: 1, incl_price: '', rate: '', discount_pct: '',
    // The billing firm's own rate, not nothing. The Gst/Vat box is no longer
    // typed into, so a line left at zero would go out as a tax invoice with
    // no tax on it -- which is how one already in the books came to be nil.
    gst_rate: gstDefault(),
    cess_rate: 0, item_detail: '', thickness: '', cutting_size: '',
    // The steel a challan line takes out of the yard (Challan Stock).
    steel_type: '', steel_quality: '', steel_thick: ''
  };
}
/** Every HSN already on the books, each with the product it belongs to, for
 *  the suggestion list behind the HSN box. A code typed on one line is nearly
 *  always a code the item master already holds. */
function hsnOptions() {
  const seen = new Map();
  (S.items || []).forEach(i => {
    const h = String(i.hsn || '').trim();
    if (h && !seen.has(h)) seen.set(h, i.name);
  });
  return [...seen].sort((a, b) => a[0].localeCompare(b[0]))
    .map(([h, name]) => '<option value="' + esc(h) + '">' + esc(name) + '</option>')
    .join('');
}

/** True when the active firm is outside GST (BST-style purchase book). */
function noTax() { return (S.meta.company.tax_mode || 'gst') === 'none'; }
/** True when the active firm is the one orders are taken in. */
/** The firm the order book lives in -- every order is taken there, whichever
    firm happens to be on screen. Set under Settings. */
function orderFirmName() {
  const f = (S.meta.companies || []).find(c => c.id === S.meta.order_company);
  return f ? f.name : 'the order firm';
}
/** Orders are filed in the order firm, so they follow that firm's tax mode
    rather than the mode of whichever firm is being viewed. */
function orderFirmNoTax() {
  const f = (S.meta.companies || []).find(c => c.id === S.meta.order_company);
  return ((f && f.tax_mode) || 'gst') === 'none';
}

/** Switch the active company, refresh everything that is company-scoped. */
async function useCompany(id) {
  await api.post('/api/companies/' + id + '/activate', {});
  invalidate();
  await loadMeta(true);
}

/** Put the screen in the kind of firm a Sales menu entry is for.
 *
 *  Challans belong to the order firm, so that one is unambiguous and the
 *  switch is silent. A bill can come from either GST firm; `ask` forces the
 *  choice for a document about to be raised, while a list settles for any GST
 *  firm and only asks when none is on screen. False means the user backed out.
 */
async function ensureFirm(kind, ask, buying) {
  // The order firm is also the accounts book: challans, receipts, the bank,
  // and the staff's salary accounts all sit in it.
  if (kind === 'order' || kind === 'accounts') {
    const target = S.meta.order_company;
    if (!target) {
      bad('No order firm is set. Choose one under Settings → Firm roles.');
      return false;
    }
    if (target !== S.meta.company_id) {
      await useCompany(target);
      toast((kind === 'order' ? 'Challans are taken in '
                              : 'Staff accounts are kept in ') + S.meta.company.name);
    }
    return true;
  }
  if (kind === 'billing') {
    const firms = S.meta.billing_firms || [];
    if (!firms.length) { bad('No GST firm is set up to bill from.'); return false; }
    const here = firms.some(f => f.id === S.meta.company_id);
    if (here && !ask) return true;
    const picked = firms.length === 1 ? firms[0].id
      : await chooseBillingFirm(null, here ? S.meta.company_id : null, firms,
                                buying);
    if (!picked) return false;
    if (picked !== S.meta.company_id) {
      await useCompany(picked);
      toast((buying ? 'Buying into ' : 'Billing from ') + S.meta.company.name);
    }
    return true;
  }
  return true;
}

/** Send a delete, and when the server blocks it only because other documents
 *  hang off this one, offer to take those along instead of making the user go
 *  firm by firm deleting them by hand. */
async function delCascading(url, title, whatElse) {
  // Resolves to the list of things deleted so the caller can say exactly what
  // went -- an array, always truthy -- or false if it did not happen.
  const went = r => ((r && r.deleted) || []);
  try {
    return went(await api.del(url));
  } catch (e) {
    const d = e.data || {};
    if (!d.cascade) { bad(e.message); return false; }
    const list = (d.blocked_by || []).map(x => '<li>' + esc(x) + '</li>').join('');
    const go = await modal(title,
      '<p style="margin:0 0 10px;font-size:14px">' + esc(e.message) + '</p>' +
      (list ? '<p style="margin:0 0 6px;font-size:13px;color:var(--muted)">'
        + 'These will be deleted too:</p><ul style="margin:0 0 10px 18px;'
        + 'font-size:13.5px">' + list + '</ul>' : '') +
      '<p style="margin:0;font-size:13px;color:var(--muted)">'
      + esc(whatElse || 'Stock and balances are adjusted for every one of them. '
        + 'This cannot be undone.') + '</p>',
      [{ label: 'Cancel', value: false },
       { label: 'Delete all of them', value: true, cls: 'danger' }]);
    if (!go) return false;
    try {
      return went(await api.del(
        url + (url.indexOf('?') < 0 ? '?' : '&') + 'cascade=1'));
    } catch (e2) { bad(e2.message); return false; }
  }
}

/* ------------------------------------------------- deleting an account ---
   A customer goes with everything written to it, in every firm it is kept
   in (asked for on 16 Sep 2026, and made easier the same day). The server
   says first what that takes; one confirmation, the same as any other
   delete -- Cancel stands first, so Enter leant on does nothing; → then
   Enter deletes. An entry shared with another account loses only this account's
   part, so almost nothing is refused any more.                          */
const ACCOUNT_DOCS = [
  ['challans', 'challan'], ['bills', 'GST bill'], ['purchases', 'purchase'],
  ['credit_notes', 'credit note'], ['debit_notes', 'debit note'],
  ['orders', 'order'], ['receipts', 'receipt'], ['payments', 'payment'],
  ['journals', 'journal'], ['contras', 'contra'],
  ['opening', 'opening balance'],
  ['shared', 'shared entry', 'shared entries']
];
const counted = (n, one, many) => n + ' ' + (n === 1 ? one : (many || one + 's'));

/** Resolves true once the account has gone. */
async function deleteAccount(id) {
  let d;
  try { d = await api.get('/api/ledgers/' + id + '/delete-check'); }
  catch (e) { bad(e.message); return false; }
  const title = 'Delete ' + d.name;
  const head = `<p style="margin:0 0 12px;font-size:14px"><b>${esc(d.name)}</b>
    · ${d.balance === null ? 'no account in the accounts book'
        : 'balance <b>' + drcr(d.balance) + '</b>'}
    <span style="color:var(--muted)">· kept in ${d.ledgers.map(l => esc(l.firm)
      + (l.name !== d.name ? ' as ' + esc(l.name) : '')).join(', ')}</span></p>`;
  const small = 'margin:0 0 10px;font-size:13.5px';

  if (d.blocked.length) {
    const body = head + (d.is_party
      ? `<p style="${small}">It cannot be deleted yet. ${d.blocked.length === 1
          ? 'This entry also touches' : 'These entries also touch'} another
        account, and deleting ${d.blocked.length === 1 ? 'it' : 'them'} would
        change that account too. Open ${d.blocked.length === 1 ? 'it' : 'each one'}
        to correct it or delete it on its own, then try again. Enter or a
        click on a row opens it.</p>
        <div class="tbl-scroll" style="max-height:320px;overflow:auto;border:1px solid var(--line);border-radius:7px">
        <table><thead><tr><th>Firm</th><th>Entry</th><th>Date</th><th>Why</th></tr></thead><tbody>
        ${d.blocked.map((b, n) => `<tr data-n="${n}"${b.doc_id
          ? ' class="clickable" tabindex="0" title="Open it"' : ''}>
          <td class="nw">${esc(b.firm)}</td>
          <td class="nw"><b>${esc(b.label)} ${esc(b.no)}</b></td>
          <td class="nw">${b.date ? dt(b.date) : ''}</td>
          <td>${esc(b.why)}</td></tr>`).join('')}
        </tbody></table></div>`
      : `<p style="${small}">It cannot be deleted: ${esc(d.blocked[0].why)}.
         An account like this is only deleted while nothing is written against
         it. To stop it being offered, edit it and untick <b>Active</b>.</p>`);
    const open = await modal(title, body, [{ label: 'Close', value: null }], {
      wide: d.is_party,
      onOpen: (bg, close) => $$('tr[tabindex]', bg).forEach(tr => {
        const pick = () => close(d.blocked[num(tr.dataset.n)]);
        tr.onclick = pick;
        tr.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); pick(); } };
      })
    });
    // A document lives in its own firm's books, so opening one means going
    // there first -- the same walk the account's own rows make.
    if (open && open.doc_id) {
      if (open.company_id !== S.meta.company_id) await useCompany(open.company_id);
      location.hash = docHref(open.kind, open.doc_id);
    }
    return false;
  }

  // A bank, cash, expense or tax account with entries: where they go is
  // asked each time (26 Sep 2026).
  if (!d.is_party && d.choose) return deleteAccountEntries(d, title, head, small);

  const url = '/api/ledgers/' + id;
  if (!d.documents && !d.entries && !d.firms.length) {
    if (!await modal(title, head + `<p style="${small}">Nothing is written
        against it in any firm.</p>`,
      [{ label: 'Cancel', value: false },
       { label: 'Delete account', value: true, cls: 'danger' }])) return false;
    try { await api.del(url); }
    catch (e) { bad(e.message); return false; }
    invalidate();
    ok(d.name + ' deleted.');
    return true;
  }
  const sign = v => (v < 0 ? '−' : '+') + fmt(Math.abs(v));

  const bills = d.firms.reduce((a, f) => a + f.bills, 0);
  const eway = d.firms.reduce((a, f) => a + f.eway, 0);
  const opening = d.firms.reduce((a, f) => a + f.opening, 0);
  const notes = [];
  if (bills) notes.push(counted(bills, 'GST bill') + ' will leave the GST registers'
    + (eway ? ', ' + eway + ' of them with an e-way bill' : '')
    + '. A bill already filed stays filed on the portal; this does not cancel it.');
  // Shared entries lose only this account's part.
  if (d.shared.length) {
    const eg = d.shared[0];
    notes.push(counted(d.shared.length, 'entry', 'entries') + ' also name'
      + (d.shared.length === 1 ? 's' : '') + ' other accounts (' + esc(eg.label)
      + ' ' + esc(eg.no) + ' with ' + esc(eg.others.join(', '))
      + (d.shared.length > 1 ? ', and more' : '') + '). Only this account\'s '
      + 'part comes out of ' + (d.shared.length === 1 ? 'it' : 'them')
      + '; the other accounts do not change.'
      + (d.carried ? ' The ' + fmt(d.carried) + ' they took over from it is '
        + 'carried on <b>Difference in Opening Balances</b>.' : ''));
  }
  // The same customer billed under another spelling in a GST firm.
  d.renamed.forEach(r => notes.push(counted(r.documents, 'bill') + ' in '
    + esc(r.firm) + ' ' + (r.documents === 1 ? 'is' : 'are') + ' under the name <b>'
    + esc(r.name) + '</b>, which keeps its other ' + counted(r.kept, 'entry', 'entries')
    + '; its balance moves by ' + sign(r.change) + '.'));
  d.touches.forEach(t => notes.push('<b>' + esc(t.name) + '</b> (' + esc(t.firm)
    + ') moves by ' + sign(t.change) + (t.round_off
      ? ': the round off on ' + (t.entries === 1 ? 'one of these documents'
        : t.entries + ' of these documents') + ' was posted there.'
      : ': ' + (t.entries === 1 ? 'a line of these documents was'
        : t.entries + ' lines of these documents were') + ' posted to it.')));
  if (opening) notes.push('Its opening balance is moved onto <b>Difference in Opening Balances</b>.');
  // Money that came in or went out stays in the cash and bank books
  // (3 Oct 2026): only the customer's side of those receipts goes.
  (d.kept || []).forEach(k => notes.push(counted(k.entries, 'receipt or payment',
    'receipts and payments') + ' in ' + esc(k.firm) + ' moved '
    + fmt(Math.abs(k.money)) + ' through cash or a bank. '
    + (k.entries === 1 ? 'It stays' : 'They stay') + ' in the cash and bank '
    + 'books; this account\'s part, ' + fmt(k.carried) + ', is carried on '
    + '<b>Difference in Opening Balances</b>.'));
  (d.cash_bills || []).forEach(c => notes.push('<b>' + counted(c.documents,
    'cash bill') + ' in ' + esc(c.firm) + '</b> took ' + fmt(Math.abs(c.money))
    + ' into cash or a bank. ' + (c.documents === 1 ? 'It goes' : 'They go')
    + ' whole, so that money leaves the cash and bank books.'));
  // Documents whose other half went when a bank account was deleted with its
  // entries: taking the rest away moves what that firm is out by.
  d.lopsided.forEach(l => notes.push('<b style="color:var(--bad)">'
    + counted(l.documents, 'document') + ' in ' + esc(l.firm) + ' already '
    + (l.documents === 1 ? 'has' : 'have') + ' no other side</b>, most '
    + 'often because a bank account was deleted with its entries. '
    + esc(l.firm) + '\'s trial balance, out by ' + fmt(Math.abs(l.out_by))
    + ' today, will be out by ' + fmt(Math.abs(l.then)) + '.'));
  notes.push('Sales, tax and stock figures drop by these documents'
    + ((d.cash_bills || []).length ? '. ' : '; cash and bank do not change. ')
    + (d.lopsided.length ? 'Otherwise each firm\'s books stay as they are.'
      : 'Each firm\'s books stay balanced.'));
  notes.push('This cannot be undone from the app, but a copy of the books is '
    + 'saved first.');

  const body = head + `
    <p style="${small}">${d.documents
      ? 'Deleting it also deletes ' + counted(d.documents, 'document')
        + (d.first ? ' dated ' + dt(d.first)
          + (d.last !== d.first ? ' to ' + dt(d.last) : '') : '')
      : 'Nothing but its own entries goes with it'}:</p>
    <ul style="margin:0 0 12px 18px;font-size:14px">${d.firms.map(f => `<li><b>${esc(f.firm)}</b>:
      ${ACCOUNT_DOCS.filter(([k]) => f[k]).map(([k, w]) => counted(f[k], w)).join(', ')}${
      f.value ? ' <span style="color:var(--muted)">· goods worth ' + money(f.value) + '</span>' : ''}</li>`).join('')}
    </ul>
    <ul style="margin:0 0 4px 18px;font-size:13px;color:var(--muted)">
      ${notes.map(n => '<li>' + n + '</li>').join('')}</ul>`;
  const go = await modal(title, body, [
    { label: 'Cancel', value: false },
    { label: 'Delete', value: true, cls: 'danger' }
  ], { wide: true });
  if (!go) return false;
  toast('Deleting ' + d.name + '…');
  let r;
  try { r = await api.del(url + '?cascade=1'); }
  catch (e) { bad(e.message); return false; }
  invalidate();
  ok('Deleted ' + r.name + ' and ' + counted(r.documents, 'document') + '.'
    + (r.backup ? ' The books were copied first, to ' + r.backup + '.' : ''));
  return true;
}

/* --------------------------------------------- merging two accounts ---
   3 Oct 2026, the user: "sometimes by mistake the user has created two
   different accounts for one customer and they both have transactions for
   different years. So add a option to merge accounts." The other account is
   picked, then which of the two names stays; every entry of the other moves
   onto it, in every firm, and it is deleted. Nothing is deleted with it.

   The commonest pair is not a mistake made here at all: SHREE renamed a
   customer in 2022-23 (BALVIKRUPA INDUSTRIES -> ... JAGDISHBHAI) and the
   import, which knows accounts by name, kept the old name beside the new.
   The new name's first carried-forward line is the old name's balance, and
   the server counts it once; the dialog says so. */

/** The other account, then the merge. Resolves to the account kept
 *  ({id, name}) once merged, else null. `find` searches accounts the way the
 *  screen's own box does. */
async function mergeAccount(id, name, find) {
  // The other spelling usually starts the same way: the list opens on the
  // name's first word (two, when the first is a letter or two -- "D PATEL").
  const words = String(name || '').trim().split(/\s+/);
  let start = words[0] || '';
  if (start.length < 3 && words[1]) start += ' ' + words[1];
  const self = squash(name);
  const other = await chooseParty({
    title: 'Merge ' + name + ' with…', term: start,
    source: async term => (await find(term)).filter(x =>
      x.id !== id && squash(x.name) !== self)
  });
  if (!other) return null;
  let d;
  try { d = await api.get('/api/ledgers/' + id + '/merge-check', { into: other.id }); }
  catch (e) { bad(e.message); return null; }

  const small = 'margin:0 0 10px;font-size:13.5px';
  const span = s => s.first ? dt(s.first) + (s.last && s.last !== s.first ? ' to ' + dt(s.last) : '') : 'no entries';
  const side = s => `<b>${esc(s.name)}</b>
    <span class="hint" style="display:block;margin:2px 0 0">
      ${s.balance === null ? 'no account in the accounts book' : 'balance <b>' + drcr(s.balance) + '</b>'}
      · ${counted(s.entries, 'entry', 'entries')}${s.orders ? ', ' + counted(s.orders, 'order') : ''}
      · ${span(s)}
      · ${s.ledgers.map(l => '<span title="' + esc(l.firm) + '">' + esc(l.firm_code) + '</span>').join(', ')}
      ${s.gstins.length ? ' · GSTIN ' + s.gstins.map(esc).join(', ') : ''}</span>`;
  const ways = { [d.b.ledger_id]: d.a_into_b, [d.a.ledger_id]: d.b_into_a };
  const body = `<p style="${small}">Which name stays? Every challan, bill, order,
      receipt and journal of the other moves onto it, in every firm, and the
      other account is deleted. Nothing else is deleted.</p>
    ${[d.b, d.a].sort((x, y) => (y.ledger_id === d.keep) - (x.ledger_id === d.keep)).map(s => `
      <label class="check del-way"><input type="radio" name="mg-keep" value="${s.ledger_id}"${s.ledger_id === d.keep ? ' checked' : ''}>
        <span>Keep ${side(s)}</span></label>`).join('')}
    <div id="mg-what" style="margin-top:12px"></div>`;
  const paint = bg => {
    const kept = num($('input[name=mg-keep]:checked', bg).value);
    const w = ways[kept];
    const gone = kept === d.a.ledger_id ? d.b : d.a;
    const keep = kept === d.a.ledger_id ? d.a : d.b;
    if (w.blocked) {
      $('#mg-what', bg).innerHTML = `<p style="${small};color:var(--bad)">${esc(w.blocked)}</p>`;
      return;
    }
    const notes = w.steps.filter(s => s.once).map(s => esc(s.firm_code) + ': the '
      + drcr(s.once) + ' that <b>' + esc(s.once_from) + '</b> stopped at was carried by SHREE into <b>'
      + esc(s.once_from === s.name ? s.into : s.name) + '</b> on ' + dt(s.once_date)
      + '. Both accounts hold it today; merged, it is counted once.');
    // An entry written to both accounts is one entry of one account once
    // merged, and counts once (3 Oct 2026).
    const entry = e => esc(e.firm_code) + ' ' + esc(e.label) + ' ' + esc(e.no) + ' of '
      + dt(e.date) + ' (' + fmt(e.amount) + ')';
    if (w.between) notes.push('<b>' + counted(w.between, 'entry', 'entries') + ' written to both '
      + 'accounts ' + (w.between === 1 ? 'is' : 'are') + ' counted once:</b><ul style="margin:2px 0 0 18px">'
      + w.shared.map(e => '<li>' + entry(e) + (e.out
        ? ' only moved money from one to the other, so it is taken out'
        : ' has a line on each, which become one line') + '</li>').join('')
      + (w.between > w.shared.length ? '<li>and ' + (w.between - w.shared.length) + ' more</li>' : '')
      + '</ul>');
    // Alike is not the same: two loans pay one EMI on one day.
    if (w.alike) notes.push('<b style="color:var(--bad)">' + counted(w.alike, 'entry', 'entries')
      + ' look' + (w.alike === 1 ? 's' : '') + ' the same in both</b> — same day, kind and amount — but '
      + (w.alike === 1 ? 'is a separate document' : 'each is a separate document')
      + ' with its own number, so both are kept. If one was entered twice, cancel and delete it '
      + 'first.<ul style="margin:2px 0 0 18px">' + w.alike_rows.map(a => '<li>' + esc(a.firm_code)
        + ' ' + esc(a.label) + ' ' + esc(a.gone_no) + ' and ' + esc(a.kept_no) + ' of ' + dt(a.date)
        + ', ' + fmt(a.amount) + ' ' + a.side + '</li>').join('')
      + (w.alike > w.alike_rows.length ? '<li>and ' + (w.alike - w.alike_rows.length) + ' more</li>' : '')
      + '</ul>');
    if (gone.gstins.length && !keep.gstins.length) notes.push(esc(keep.name)
      + ' has no GSTIN; it takes ' + esc(gone.gstins.join(', ')) + ' from ' + esc(gone.name) + '.');
    if (w.gstin_changes) notes.push('<b style="color:var(--bad)">The GSTINs differ.</b> '
      + 'Old bills of ' + esc(gone.name) + ' will show ' + esc(keep.gstins.join(', '))
      + ' when printed again or sent in a GST return. Make sure these are one customer.');
    notes.push('Every firm\'s books stay balanced. A copy of the books is saved first.'
      + (gone.from_shree ? ' Each SHREE update merges them again.' : ''));
    $('#mg-what', bg).innerHTML = `
      <ul style="margin:0 0 10px 18px;font-size:14px">${w.steps.map(s => `<li><b title="${esc(s.firm)}">${esc(s.firm_code)}</b>:
        ${s.how === 'rename'
          ? esc(s.name) + ' is renamed <b>' + esc(s.into) + '</b>'
          : (s.entries || s.orders ? counted(s.entries, 'entry', 'entries')
              + (s.orders ? ' and ' + counted(s.orders, 'order') : '') + ' of ' + esc(s.name)
              + ' move onto <b>' + esc(s.into) + '</b>'
            : esc(s.name) + ' goes into <b>' + esc(s.into) + '</b>')}</li>`).join('')}</ul>
      <p style="${small}">One account, <b>${esc(keep.name)}</b>${w.balance === null ? ''
        : ', balance <b>' + drcr(w.balance) + '</b>'}.</p>
      <ul style="margin:0 0 4px 18px;font-size:13px;color:var(--muted)">
        ${notes.map(n => '<li>' + n + '</li>').join('')}</ul>`;
  };
  let kept = d.keep;
  const go = await modal('Merge two accounts', body, [
    { label: 'Cancel', value: false },
    { label: 'Merge', value: true, cls: 'danger', before: bg => {
      kept = num($('input[name=mg-keep]:checked', bg).value);
      if (ways[kept].blocked) { bad(ways[kept].blocked); return false; }
    } }
  ], { wide: true, yesNo: true, nothingToSave: true,
       onOpen: bg => {
         paint(bg);
         $$('input[name=mg-keep]', bg).forEach(r => { r.onchange = () => paint(bg); });
       } });
  if (!go) return null;
  const gone = kept === d.a.ledger_id ? d.b.ledger_id : d.a.ledger_id;
  toast('Merging…');
  let r;
  try { r = await api.post('/api/ledgers/' + gone + '/merge', { ledger_id: kept }); }
  catch (e) { bad(e.message); return null; }
  invalidate();
  ok('Merged ' + r.name + ' into ' + r.into + ' — ' + counted(r.entries, 'entry', 'entries')
    + ' moved' + (r.shared ? ', ' + counted(r.shared, 'entry', 'entries') + ' written to both counted once' : '')
    + '.' + (r.backup ? ' The books were copied first, to ' + r.backup + '.' : ''));
  return { id: r.ledger_id, name: r.into };
}

/* ------------------------------------------- deleting what is in use ---
   26 Sep 2026, the user: "there should always be a way to delete anything
   and everything". An account that is not a party but has entries, and an
   item on bills, used to be refused. Asked what should become of what they
   hold, the user chose to be offered both ways each time: moved onto
   another account or item picked then, or -- an account's entries deleted
   with it, an item's lines kept without an item. */

/** The two ways, as the dialog body: `ways` is [[value, title, hint], ...]
 *  and the one that moves carries the lookup box. */
function deleteWays(ways) {
  return ways.map(([value, what, hint, box], n) => `
    <label class="check del-way"><input type="radio" name="del-way" value="${value}"${n ? '' : ' checked'}>
      <span><b>${what}</b><span class="hint" style="display:block;margin:2px 0 ${box ? 6 : 0}px">${hint}</span>${box || ''}</span></label>`).join('');
}

/** Pick where it goes, with the app's own lookup list: `choices` is what can
 *  be picked, `named(x)` the words it is found by. Typing picks the move. */
function deleteTarget(bg, choices, named, start) {
  const box = $('#del-to', bg), move = $('input[value=move]', bg);
  let target = start || null;
  attachPicker(box, {
    source: async term => searchList(term, choices, named),
    render: x => `<b>${esc(x.name)}</b><small>${esc(named(x).slice(1).filter(Boolean).join(' · '))}</small>`,
    onPick: x => { target = x; box.value = x.name; move.checked = true; }
  });
  box.addEventListener('input', () => { target = null; move.checked = true; });
  return () => target;
}

/** An account that is not a party, with entries: moved onto another
 *  account, or deleted with it. */
async function deleteAccountEntries(d, title, head, small) {
  let all;
  try { all = await api.get('/api/ledgers', { company: d.company_id }); }
  catch (e) { bad(e.message); return false; }
  const choices = all.filter(x => x.id !== d.ledger_id);
  const start = choices.find(x => x.id === d.move_default) || null;
  const f = d.firms[0] || {};
  const kinds = ACCOUNT_DOCS.filter(([k]) => f[k]).map(([k, w, p]) => counted(f[k], w, p)).join(', ');
  const sign = v => (v < 0 ? '−' : '+') + fmt(Math.abs(v));
  const moved = d.others.map(o => '<li>' + esc(o.name) + (o.party ? ' <span class="tag">party</span>' : '')
    + ' moves by ' + sign(o.change) + '</li>').join('')
    + (d.others_count > d.others.length ? '<li>and ' + (d.others_count - d.others.length) + ' more</li>' : '');
  const them = d.entries === 1 ? 'it' : 'them';
  const body = head + `<p style="${small}">${counted(Math.max(d.entries, 1), 'entry', 'entries')}
      ${d.entries === 1 ? 'is' : 'are'} written against it${d.first ? ', ' + dt(d.first)
      + (d.last !== d.first ? ' to ' + dt(d.last) : '') : ''}. Where should ${d.entries === 1
      ? 'it' : 'they'} go?</p>`
    + deleteWays([
      ['move', 'Move ' + them + ' onto another account, then delete this one',
       'Every document and every other balance stays as it is. The account picked takes these entries, and this one\'s balance of ' + drcr(d.balance) + '.',
       `<input id="del-to" placeholder="Type to find the account…" autocomplete="off" value="${esc(start ? start.name : '')}">`],
      ['entries', 'Delete ' + them + ' with it' + (kinds ? ': ' + kinds : ''),
       'Each goes whole, in ' + esc(d.firm) + (f.value ? ', goods worth ' + money(f.value) : '') + '.'
       + (d.parties_moved ? ' ' + counted(d.parties_moved, 'customer or supplier', 'customers and suppliers') + ' on them change' + (d.parties_moved === 1 ? 's' : '') + ' too.' : '')
       + (f.bills ? ' ' + counted(f.bills, 'GST bill') + ' will leave the GST registers; a bill already filed stays filed on the portal.' : '')
       + (f.opening ? ' Its opening balance is carried onto Difference in Opening Balances.' : '')
       + (moved ? '<ul style="margin:6px 0 0 18px">' + moved + '</ul>' : '')
       + d.lopsided.map(l => '<span style="display:block;margin-top:6px;color:var(--bad)">' + esc(l.firm)
         + '\'s trial balance, out by ' + fmt(Math.abs(l.out_by)) + ' today, will be out by '
         + fmt(Math.abs(l.then)) + ': some of these entries have already lost their other side.</span>').join('')]
    ])
    + `<p class="hint" style="margin:10px 0 0">A copy of the books is saved first.${d.from_shree
      ? ' This account came from SHREE; every SHREE update does the same to it again.' : ''}</p>`;
  let pick = () => null, way = 'move';
  const go = await modal(title, body, [
    { label: 'Cancel', value: false },
    { label: 'Delete', value: true, cls: 'danger', before: bg => {
      way = $('input[name=del-way]:checked', bg).value;
      if (way === 'move' && !pick()) {
        bad('Pick the account the entries go to — type part of its name and choose it from the list.');
        $('#del-to', bg).focus();
        return false;
      }
    } }
  ], { wide: true, nothingToSave: true,
       onOpen: bg => { pick = deleteTarget(bg, choices, x => [x.name, x.group_name], start); } });
  if (!go) return false;
  toast('Deleting ' + d.name + '…');
  let r;
  try {
    r = await api.del('/api/ledgers/' + d.ledger_id + (way === 'move'
      ? '?move_to=' + pick().id : '?entries=1'));
  } catch (e) { bad(e.message); return false; }
  invalidate();
  ok((r.moved_to ? 'Moved ' + counted(r.entries, 'entry', 'entries') + ' onto ' + r.moved_to
      + ' and deleted ' + r.name : 'Deleted ' + r.name
      + (r.documents ? ' and ' + counted(r.documents, 'entry', 'entries') : ''))
    + '.' + (r.backup ? ' The books were copied first, to ' + r.backup + '.' : ''));
  return true;
}

/** Delete an item. One on bills or challans: its lines moved onto another
 *  item, whose stock takes them, or kept without an item. */
async function deleteItem(id) {
  let d;
  try { d = await api.get('/api/items/' + id + '/delete-check'); }
  catch (e) { bad(e.message); return false; }
  if (!d.lines) {
    if (!await confirmBox('Delete item', 'Delete "' + d.name + '"?')) return false;
    try { await api.del('/api/items/' + id); }
    catch (e) { bad(e.message); return false; }
    invalidate();
    ok('Item deleted.');
    return true;
  }
  let all;
  try { all = await api.get('/api/items'); }
  catch (e) { bad(e.message); return false; }
  const choices = all.filter(x => x.id !== d.id);
  const them = d.lines === 1 ? 'it' : 'them';
  const body = `<p style="margin:0 0 12px;font-size:14px"><b>${esc(d.name)}</b>${d.hsn
      ? ' · HSN ' + esc(d.hsn) : ''} is on ${counted(d.lines, 'line')} of
      ${counted(d.documents, 'bill or challan', 'bills and challans')}${d.order_lines
      ? ' and orders' : ''}${d.stock === null ? '' : ', and holds ' + qty(d.stock) + ' '
      + esc(d.unit) + ' in stock'}. Where should ${d.lines === 1 ? 'it' : 'they'} go?</p>`
    + deleteWays([
      ['move', 'Move ' + them + ' onto another item, then delete this one',
       'The bills keep every figure. The item picked takes these lines and this one\'s stock — the way to put a duplicate into the right one.',
       '<input id="del-to" placeholder="Type to find the item…" autocomplete="off">'],
      ['unlink', 'Keep ' + them + ' without an item',
       'The bills keep their description, kilos and amounts, but the lines no longer belong to any item, so they drop out of stock.']
    ])
    + `<p class="hint" style="margin:10px 0 0">A copy of the books is saved first.${d.from_shree
      ? ' This item came from SHREE; every SHREE update does the same to it again.' : ''}</p>`;
  let pick = () => null, way = 'move';
  const go = await modal('Delete ' + d.name, body, [
    { label: 'Cancel', value: false },
    { label: 'Delete', value: true, cls: 'danger', before: bg => {
      way = $('input[name=del-way]:checked', bg).value;
      if (way === 'move' && !pick()) {
        bad('Pick the item the lines go to — type part of its name and choose it from the list.');
        $('#del-to', bg).focus();
        return false;
      }
    } }
  ], { wide: true, nothingToSave: true,
       onOpen: bg => { pick = deleteTarget(bg, choices, x => [x.name, x.hsn, x.category]); } });
  if (!go) return false;
  let r;
  try {
    r = await api.del('/api/items/' + id + (way === 'move' ? '?move_to=' + pick().id : '?unlink=1'));
  } catch (e) { bad(e.message); return false; }
  invalidate();
  ok((r.moved_to ? 'Moved ' + counted(r.lines, 'line') + ' onto ' + r.moved_to + ' and deleted '
      : 'Deleted ') + r.name + '.' + (r.backup ? ' The books were copied first, to ' + r.backup + '.' : ''));
  return true;
}

/** Delete a saved document, saying plainly why if it cannot go. `firm` is
 *  the firm it sits in, when that is not the one on screen. */
/* ---------------------------------------------- deleting SHREE's data ---
   **What came from SHREE comes back from SHREE.** The app holds whatever
   SHREE holds; a bill, challan or voucher that came from SHREE and is
   deleted here is brought in again by the next SHREE update, unless the
   delete asks for it to be kept deleted here.

   Until 17 Sep 2026 every such delete was kept, and nothing said so: that
   afternoon all 1,116 of the year's bills and challans were deleted here,
   every update then deleted them again, and SHREE's data "could not be
   fetched". The choice is now made where the delete is, and every update
   says how many are being kept deleted. */
function shreeKeepBox(n, total) {
  const one = n === 1, them = one ? 'it' : 'them';
  const which = total === 1 ? 'This came' : (n === total ? 'These came'
    : n + ' of these came');
  return `<label class="check bulk-also"><input type="checkbox" id="keep-deleted">
    <span>Keep ${them} deleted here
    <span class="hint" style="display:block;margin:2px 0 0">${which} from
    SHREE. Unticked, the next SHREE update brings ${them} back — the app
    always holds what SHREE holds. Ticked, every update deletes ${them} again
    (listed under SHREE update → Kept here). To be rid of ${them} everywhere,
    delete ${them} in SHREE.</span></span></label>`;
}

/** Ask before deleting one thing; answers null, or { keep }. */
async function askDelete(title, msg, fromShree) {
  let keep = false;
  const go = await modal(title, '<p style="margin:0;font-size:14px">' + esc(msg)
    + '</p>' + (fromShree ? shreeKeepBox(1, 1) : ''), [
    { label: 'Cancel', value: false },
    { label: 'Yes, delete', value: true, cls: 'danger',
      before: bg => { keep = !!($('#keep-deleted', bg) || {}).checked; } }
  ], { yesNo: true });
  return go ? { keep } : null;
}

async function deleteInvoice(id, label, after, firm) {
  let source = '', ordered = false;
  try {
    const d = await api.get('/api/invoices/' + id, { company: firm || '' });
    source = d.source;
    ordered = !!d.order_id;
  } catch (e) { /* the delete says so if it is gone */ }
  // The order is spoken of only when there is one behind the document.
  const ans = await askDelete('Delete ' + label,
    'Delete ' + label + '? Stock and balances will be adjusted'
    + (ordered ? ', and the order behind it is deleted with it rather than '
      + 'reopening.' : '.')
    + (source === 'SHREE' ? '' : ' This cannot be undone.'), source === 'SHREE');
  if (!ans) return false;
  const q = new URLSearchParams();
  if (firm) q.set('company', firm);
  if (ans.keep) q.set('keep', '1');
  const gone = await delCascading('/api/invoices/' + id
    + (q.toString() ? '?' + q : ''), 'Delete ' + label + ' and its bill');
  if (!gone) return false;
  invalidate();
  ok((Array.isArray(gone) && gone.length > 1
    ? 'Deleted ' + gone.join(', ') + '.' : label + ' deleted.')
    + (source === 'SHREE' && !ans.keep
      ? ' The next SHREE update brings it back.' : ''));
  if (after) after();
  return true;
}

/** Delete an order, taking its challan and bill along if the user agrees. */
async function deleteOrder(id, label, after) {
  if (!await confirmBox('Delete order',
    'Delete order ' + label + '? This cannot be undone.')) return false;
  const gone = await delCascading('/api/orders/' + id,
    'Delete order ' + label + ' and its documents');
  if (!gone) return false;
  invalidate();
  ok(gone.length ? 'Deleted order ' + label + ', ' + gone.join(', ') + '.'
                 : 'Order ' + label + ' deleted.');
  if (after) after();
  return true;
}

/* ------------------------------------------------ deleting many at once ---
   Asked for on 17 Sep 2026: deleting one thing or many has to be easy.

   A list whose rows can be deleted carries a tick box on every row and one
   in its heading that ticks them all. Once anything is ticked a bar above
   the list says how many, and offers Delete selected. From the keyboard:
   Space ticks the row the cursor is on (Enter still opens it), Ctrl + A
   ticks the whole list, and the Delete key deletes what is ticked -- or,
   with nothing ticked, the row under the cursor, the same as its own
   Delete button.

   The server is asked first what the delete would do, and the one
   confirmation says it: how many, what goes with them, what cannot go and
   why. Then it all goes in one transaction, with one copy of the books
   taken first.                                                           */
const BULK_NOUN = {
  invoice: ['document', 'documents'], order: ['order', 'orders'],
  voucher: ['entry', 'entries'], ledger: ['account', 'accounts'],
  item: ['item', 'items'], steel_entry: ['stock entry', 'stock entries'],
  stock_entry: ['stock entry', 'stock entries'],
  personal: ['person', 'people'], personal_entry: ['entry', 'entries']
};

/** Tick boxes on the list drawn inside `host`. `kind` is what its rows are;
 *  `opts.noun` names them ([one, many]); `opts.reload` redraws the list;
 *  `opts.amount(tr)` is a row's value, for the total shown while ticking. */
/** The tick column's cell at the front of each of these rows: a box on a row
 *  that can be deleted, an empty cell on one that cannot. Copied, not
 *  written out -- on Receipts / Payments that was 2,800 trips through the
 *  page's HTML reader each time the list was drawn. */
let tickCell = null, tickNone = null;
function tickCells(trs) {
  if (!tickCell) {
    tickCell = el('<td class="sel-cell"><input type="checkbox" class="sel" aria-label="Tick this row"></td>');
    tickNone = el('<td class="sel-cell"></td>');
  }
  trs.forEach(tr => {
    const can = tr.dataset.id && !tr.hasAttribute('data-nodel');
    tr.insertBefore((can ? tickCell : tickNone).cloneNode(true), tr.firstChild);
  });
}

/* **A long list is drawn top first** (5 Oct 2026: "data loading is taking
   time", on the Windows PC). Nothing of a list could be seen until every
   line of it had been laid out, and the browser lays a table out whole:
   with the processor slowed to an office PC's, Receipts / Payments -- 2,865
   vouchers for the year -- took 0.96 s to show anything, its first hundred
   lines alone 0.04 s. So the first hundred go in and are painted, then the
   rest are put under them in one go. The list is complete as soon as it was
   before; it is on the screen at once. (Added a few hundred at a time it
   took twice as long: the browser lays the whole table out again for each.)

   `head` is the list up to and including `<tbody>`, `foot` from `</tbody>`
   on, `rows` one `<tr>` each. `each(trs)` is given every batch of lines as
   it goes in, to hang their buttons on; `first()` runs once the first batch
   is in, before it is painted (the tick column goes on there, so the lines
   do not move sideways when it arrives). `here()` turning false -- another
   search, another screen -- leaves the rest out. Resolves false if so. */
const FIRST_ROWS = 100;

/** After the next frame has been painted -- or a quarter of a second, in a
 *  window that is not being painted at all. */
function painted() {
  return new Promise(go => {
    let done = false;
    const once = () => { if (!done) { done = true; go(); } };
    requestAnimationFrame(() => setTimeout(once, 0));
    setTimeout(once, 250);
  });
}

async function fillRows(box, head, rows, foot, opts) {
  const { each, first, here } = opts || {};
  const cut = rows.length > FIRST_ROWS * 1.5 ? FIRST_ROWS : rows.length;
  // Not yet the whole list: `fitLists` waits for the rest before measuring.
  if (cut < rows.length) box.dataset.partial = '1';
  else delete box.dataset.partial;
  box.innerHTML = head + rows.slice(0, cut).join('') + foot;
  const body = $('tbody', box);
  if (each) each(Array.from(body.rows));
  if (first) first();
  if (cut === rows.length) return true;
  await painted();
  if ((here && !here()) || !body.isConnected) return false;
  delete box.dataset.partial;
  const had = body.rows.length;
  body.insertAdjacentHTML('beforeend', rows.slice(cut).join(''));
  const added = Array.from(body.rows).slice(had);
  // The tick column, if the first lines were given one.
  const lead = body.rows[0] && body.rows[0].cells[0];
  if (lead && lead.classList.contains('sel-cell')) tickCells(added);
  if (each) each(added);
  return true;
}

function bulkRows(host, kind, opts) {
  opts = opts || {};
  const table = $('table', host);
  const card = host.closest('.card') || host;
  let bar = host._selBar;
  if (!bar) {
    bar = host._selBar = el(`<div class="sel-bar" hidden role="region" aria-label="Ticked rows">
      <span class="sel-n"></span><span class="sel-sum"></span>
      <div class="spacer"></div>
      <button class="link" data-sel-clear>Clear</button>
      <button class="danger" data-sel-del>Delete selected</button></div>`);
    card.parentNode.insertBefore(bar, card);
  }
  bar.hidden = true;
  if (!table) return;
  const noun = opts.noun || BULK_NOUN[kind];
  host.dataset.bulk = kind;

  const head = $('thead tr', table);
  if (head) head.insertAdjacentHTML('afterbegin', '<th class="sel-cell">'
    + '<input type="checkbox" class="sel-all" title="Tick every row (Ctrl + A)"'
    + ' aria-label="Tick every row"></th>');
  tickCells($$('tbody > tr', table));
  $$('tfoot > tr', table).forEach(tr => {
    const first = tr.firstElementChild;
    if (first) first.colSpan = (first.colSpan || 1) + 1;
  });

  const boxes = () => $$('tbody .sel', table);
  const ticked = () => boxes().filter(b => b.checked);
  function update() {
    const on = ticked();
    boxes().forEach(b => b.closest('tr').classList.toggle('on', b.checked));
    const all = $('.sel-all', table);
    if (all) {
      all.checked = on.length > 0 && on.length === boxes().length;
      all.indeterminate = on.length > 0 && !all.checked;
    }
    bar.hidden = !on.length;
    $('.sel-n', bar).textContent = counted(on.length, noun[0], noun[1]) + ' ticked';
    const sum = opts.amount
      ? on.reduce((a, b) => a + num(opts.amount(b.closest('tr'))), 0) : 0;
    $('.sel-sum', bar).textContent = sum ? ' · ' + money(sum) : '';
  }

  // A click in the tick column only ticks: the row would otherwise open.
  table.addEventListener('click', e => {
    const cell = e.target.closest && e.target.closest('.sel-cell');
    if (!cell) return;
    e.stopPropagation();
    const box = $('input', cell);
    if (box && e.target !== box) {
      box.checked = !box.checked;
      box.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }, true);
  table.addEventListener('change', e => {
    if (e.target.classList.contains('sel-all')) {
      boxes().forEach(b => { b.checked = e.target.checked; });
    }
    if (e.target.matches('.sel, .sel-all')) update();
  });
  $('[data-sel-clear]', bar).onclick = () => {
    boxes().forEach(b => { b.checked = false; });
    update();
  };
  $('[data-sel-del]', bar).onclick = async () => {
    const ids = ticked().map(b => num(b.closest('tr').dataset.id));
    if (await bulkDelete(kind, ids, noun) && opts.reload) opts.reload();
  };
  host._bulk = { tickAll() { boxes().forEach(b => { b.checked = true; }); update(); },
                 ticked, remove: () => $('[data-sel-del]', bar).click() };
  update();
}

/** Delete the rows, after one confirmation that says what will happen.
 *  Resolves true once anything has gone. */
async function bulkDelete(kind, ids, noun) {
  noun = noun || BULK_NOUN[kind];
  const items = ids.map(id => ({ kind, id }));
  let d, all;
  try {
    d = await api.post('/api/bulk-delete', { items, check: true });
    all = d.needs.length
      ? await api.post('/api/bulk-delete', { items, check: true, cascade: true })
      : d;
  } catch (e) { bad(e.message); return false; }
  const n = d.deleted, extra = all.deleted - d.deleted;
  const list = rows => '<ul class="bulk-list">' + rows.slice(0, 12).map(r =>
    '<li>' + esc(r.label) + (r.amount ? ' <span class="hint">' + money(r.amount)
      + '</span>' : '') + (r.why ? '<div class="hint why">' + esc(r.why) + '</div>' : '')
    + '</li>').join('') + (rows.length > 12 ? '<li class="hint">and '
      + (rows.length - 12) + ' more</li>' : '') + '</ul>';
  const going = d.rows.filter(r => r.status === 'ok');
  const linked = all.also;
  let body = '';
  if (going.length) {
    body += `<p class="bulk-head">${counted(going.length, noun[0], noun[1])}${
      d.amount ? ' · <b>' + money(d.amount) + '</b>' : ''}</p>` + list(going);
  }
  if (d.needs.length) {
    const these = counted(d.needs.length, noun[0], noun[1]);
    // An account or an item says how much goes with it as a count; those
    // are added up, where documents are named one by one.
    const total = linked.reduce((a, x) => a + (parseInt(x, 10) || 0), 0);
    const also = kind === 'ledger' || kind === 'item'
      ? (total ? ' (' + counted(total, kind === 'item' ? 'line' : 'document') + ')' : '')
      : linked.length ? ' (' + esc(linked.slice(0, 6).join(', '))
        + (linked.length > 6 ? ', …' : '') + ')' : '';
    // What goes with them, said for the kind of row ticked. One at a time,
    // an account's entries or an item's lines can go onto another instead.
    const what = kind === 'item'
      ? `Also delete ${these} that bill or challan lines use${also}. The lines
         keep their description, kilos and amounts but no longer belong to an
         item, so they drop out of stock`
      : kind === 'ledger'
        ? `Also delete ${these} that have entries, and everything written to
           them${also}`
        : `Also delete ${these} that other documents were made from, and those
           documents with them${also}`;
    const alone = kind === 'item' || kind === 'ledger'
      ? ' To move ' + (kind === 'item' ? 'an item\'s lines' : 'an account\'s entries')
        + ' onto another instead, delete it on its own.' : '';
    body += `<label class="check bulk-also"><input type="checkbox" id="bulk-cascade">
      <span>${what}.
      <span class="hint" style="display:block;margin:2px 0 0">Unticked, these
      stay as they are.${alone}</span></span></label>` + list(d.needs.map(r => Object.assign({}, r, { why: '' })));
  }
  if (d.blocked.length) {
    body += `<p class="bulk-head" style="color:var(--bad)">${counted(d.blocked.length,
      noun[0], noun[1])} cannot be deleted and ${d.blocked.length === 1 ? 'stays' : 'stay'}:</p>`
      + list(d.blocked);
  }
  if (!going.length && !d.needs.length) {
    await modal('Nothing can be deleted', body || '<p>These are already gone.</p>');
    return false;
  }
  // Entries that had already lost their other side move a firm's trial
  // balance as they go; said in red, the way deleting one account says it.
  (all.moved || []).forEach(m => {
    body += `<p class="bulk-head" style="color:var(--bad);font-weight:500">
      ${esc(m.firm)}'s trial balance, out by ${fmt(Math.abs(m.before))} today,
      will be out by ${fmt(Math.abs(m.after))}: some of these entries have
      already lost their other side, most often because a bank account was
      deleted with its entries.</p>`;
  });
  const fromShree = d.rows.filter(r => r.shree
    && (r.status === 'ok' || r.status === 'needs')).length;
  if (fromShree) {
    body += shreeKeepBox(fromShree, going.length + d.needs.length);
  }
  // Personal Accounts hold no stock and no firm's books: a person goes
  // with their entries, and only where you stand with them moves.
  const own = kind === 'personal' || kind === 'personal_entry';
  body += `<p class="hint" style="margin:12px 0 0">${kind === 'ledger'
    ? 'An account goes with every document written to it, in every firm. '
    : kind === 'personal' ? 'A person goes with every entry they have. '
    : ''}${own ? 'Balances are adjusted' : 'Stock and balances are adjusted'},
    and a copy of the books is saved first.${fromShree ? ''
      : ' This cannot be undone from the app.'}</p>`;
  const label = cascade => 'Delete ' + (going.length + (cascade ? d.needs.length : 0));
  let cascade = false, keep = false;
  const go = await modal('Delete ' + counted(ids.length, noun[0], noun[1]), body, [
    { label: 'Cancel', value: false },
    { label: label(false), value: true, cls: 'danger',
      before: bg => {
        cascade = !!($('#bulk-cascade', bg) || {}).checked;
        keep = !!($('#keep-deleted', bg) || {}).checked;
      } }
  ], {
    wide: true, yesNo: true,
    onOpen: bg => {
      const box = $('#bulk-cascade', bg);
      const btn = $$('.ft button', bg)[1];
      if (!going.length) btn.disabled = true;
      if (box) box.onchange = () => {
        btn.textContent = label(box.checked);
        btn.disabled = !going.length && !box.checked;
      };
    }
  });
  if (!go) return false;
  toast('Deleting…');
  let r;
  try { r = await api.post('/api/bulk-delete', { items, cascade, keep }); }
  catch (e) { bad(e.message); return false; }
  invalidate();
  const also = cascade && r.also.length
    ? ' and ' + counted(r.also.length, 'linked document', 'linked documents') : '';
  ok('Deleted ' + counted(r.deleted, noun[0], noun[1]) + also + '.'
    + (r.blocked.length ? ' ' + r.blocked.length + ' could not be.' : '')
    + (r.backup ? ' The books were copied first.' : '')
    + (r.from_shree && !keep ? ' ' + counted(r.from_shree, 'of them', 'of them')
      + (r.from_shree === 1 ? ' comes' : ' come')
      + ' back at the next SHREE update.' : ''));
  return r.deleted > 0;
}

// Ctrl + A ticks the list; Delete deletes what is ticked, or the row the
// cursor is on. Neither does anything while a box is being typed in.
document.addEventListener('keydown', e => {
  const t = e.target;
  if (t && t.matches && t.matches('input:not([type=checkbox]), textarea, select, [contenteditable]')) return;
  if ($('#modal-root') && $('#modal-root').children.length) return;
  const host = $('#view [data-bulk]');
  if (!host || !host._bulk) return;
  if ((e.ctrlKey || e.metaKey) && (e.key === 'a' || e.key === 'A') && !e.altKey) {
    e.preventDefault();
    host._bulk.tickAll();
    return;
  }
  if (e.key === 'Delete' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    if (host._bulk.ticked().length) {
      e.preventDefault();
      host._bulk.remove();
      return;
    }
    const tr = $('#view tbody tr.kb');
    const del = tr && $('[data-del], [data-act=del]', tr);
    if (del) { e.preventDefault(); del.click(); }
  }
});

/** Ask which official firm raises the bill. The customer's usual firm is
    pre-selected, but either can be chosen for this one bill. */
function chooseBillingFirm(partyName, preferred, firms, buying) {
  let picked = preferred || firms[0].id;
  // Reached from the Sales menu there is no customer yet, so the question is
  // about the bill itself and the firm already ticked is simply the one on
  // screen -- not anybody's usual firm.
  const note = partyName ? ' · this customer\u2019s usual firm'
                         : ' · the firm on screen now';
  // The same two firms answer two different questions. Buying divides the
  // stock between them, so the answer decides which books the goods land in;
  // selling decides whose GSTIN goes on the bill.
  const asks = buying
    ? 'Which firm is this purchase bought into? The stock goes into that '
      + 'firm\u2019s books and comes out again when it is billed.'
    : (partyName
      ? 'Which firm raises the GST bill for <b>' + esc(partyName) + '</b>?'
      : 'Which firm raises this GST bill?');
  // ↑ ↓ choose the firm and Enter goes on, as in every other list.
  const body =
    '<p style="margin:0 0 11px;font-size:13.5px">' + asks + '</p>' +
    firms.map(f => `
      <label class="firm-pick">
        <input type="radio" name="bill-firm" value="${f.id}"${f.id === preferred ? ' checked' : ''}>
        <span><b>${esc(f.name)}</b>
          <small>${esc(f.code)}${f.id === preferred ? note : ''}</small>
        </span></label>`).join('')
    + '<p class="hint" style="margin:10px 0 0">\u2191 \u2193 to the firm, then Enter.</p>';
  return modal(buying ? 'Buy into which firm?' : 'Bill from which firm?', body, [
    { label: 'Cancel', value: null },
    { label: 'Continue', cls: 'primary', value: true,
      before: bg => {
        // Nothing ticked yet (the books on screen are neither GST firm):
        // Enter on a firm is that firm, not "Choose the firm" (10 Oct 2026).
        const on = document.activeElement;
        const sel = $('input[name=bill-firm]:checked', bg)
          || (on && on.name === 'bill-firm' && bg.contains(on) ? on : null);
        if (!sel) {
          bad(buying ? 'Choose the firm the goods are bought into.'
                     : 'Choose the firm to bill from.');
          return false;
        }
        picked = num(sel.value);
      } }
  ], { onOpen: bg => {
    // double-clicking a firm picks it and goes straight on
    $$('.firm-pick', bg).forEach(l => l.ondblclick = () => {
      $('input', l).checked = true;
      $$('.ft button', bg)[1].click();
    });
  } }).then(ok => (ok ? picked : null));
}

/** Hand a challan over to one of the two official firms. */
async function billChallan(invoiceId, challanCompanyId) {
  const cid = challanCompanyId || S.meta.company_id;
  const firms = S.meta.billing_firms || [];
  if (!firms.length) { bad('No GST firm is set up to bill from.'); return; }

  let ch;
  try { ch = await api.get('/api/invoices/' + invoiceId, { company: cid }); }
  catch (e) { bad(e.message); return; }

  const firmId = firms.length === 1 ? firms[0].id
    : await chooseBillingFirm(ch.party_name, num(ch.party_home_company), firms);
  if (!firmId) return;

  let draft;
  try {
    draft = await api.get('/api/invoices/' + invoiceId + '/gst-bill-draft',
      { company: cid, firm: firmId });
  } catch (e) { bad(e.message); return; }
  if (draft.company_id !== S.meta.company_id) {
    await useCompany(draft.company_id);
    toast('Billing from ' + draft.company_name);
  }
  // Carry the firm that was just chosen. Without it the bill screen asks the
  // server again with nothing but the challan, the server falls back to the
  // customer's own billing firm -- which may be unset, or be the order firm
  // that raises no tax bills -- and the draft comes back an error, leaving a
  // blank bill to be typed out by hand.
  location.hash = '#/invoice/new?from_challan=' + invoiceId +
    '&challan_company=' + (challanCompanyId || draft.challan_company) +
    '&firm=' + firmId;
}

/** Raise the challan for an order. Orders and challans both live in BST. */
async function challanOrder(orderId, orderCompanyId) {
  let draft;
  try {
    draft = await api.get('/api/orders/' + orderId + '/challan-draft',
      { company: orderCompanyId || S.meta.company_id });
  } catch (e) { bad(e.message); return; }
  if (draft.company_id !== S.meta.company_id) {
    await useCompany(draft.company_id);
    toast('Challan in ' + draft.company_name);
  }
  location.hash = '#/invoice/new?from_order=' + orderId +
    '&order_company=' + (orderCompanyId || draft.order_company) + '&as=challan';
}

/** Hand an order over to the firm that bills that customer. */
async function billOrder(orderId, orderCompanyId) {
  let draft;
  try {
    draft = await api.get('/api/orders/' + orderId + '/invoice-draft',
      { company: orderCompanyId || S.meta.company_id });
  } catch (e) { bad(e.message); return; }
  if (draft.company_id !== S.meta.company_id) {
    await useCompany(draft.company_id);
    toast('Billing from ' + draft.company_name);
  }
  location.hash = '#/invoice/new?from_order=' + orderId +
    '&order_company=' + (orderCompanyId || draft.order_company);
}

/** Renders the editable line-item grid and keeps totals live. */
function LineGrid(host, lines, onChange, opts) {
  opts = opts || {};
  const isIgst = () => opts.isIgst();
  // Columns a caller does not want. Orders leave out Quantity, amount,
  // discount and total -- those are only settled when the challan is billed.
  // The keys are the old field names: `weight` is the column now headed
  // Quantity, and `qty` -- the piece count -- is the one headed NOS.
  const hidden = opts.hide || [];
  const show = k => hidden.indexOf(k) < 0;
  // Which firm's tax mode this grid follows. The firm on screen by default,
  // but the order screen points it at the order firm instead.
  const taxFree = opts.noTax || noTax;
  // Opt-in, not hide-list driven: only the order screen takes a line by count
  // or by weight. show() answers "was this hidden?", which is true of every
  // column a caller never named -- it would turn the basis on everywhere.
  const hasBasis = !!opts.basis;
  // The order screen has room to breathe: fewer columns than a tax invoice, so
  // the ones it does show are given proper widths.
  const roomy = !!opts.roomy;
  // Where the free-text note goes. Under the item name reads better than a
  // column of its own and every screen wants it, but it used to ride along
  // with `roomy` -- and roomy lays the table out fixed, which on the wider
  // bill grid left the item name column nothing and collapsed it to a sliver.
  // So the note is asked for separately; roomy still implies it.
  const stackNote = opts.stackNote === undefined ? roomy : !!opts.stackNote;
  // The GST rate on a bill is settled before the line reaches the screen --
  // by the item, or by the firm's default -- so it is shown rather than typed.
  // It stays on the page because it is a figure a tax invoice is read for.
  const fixedGst = !!opts.fixedGst;
  // Every heading over the middle of its column and every figure centred in
  // it -- the bill and challan screen asks for this; the order screen keeps
  // its right-aligned figures.
  const centred = !!opts.centred;
  // On the bill the HSN is typed on the line under the product name, beside
  // the item detail, rather than in a column of its own (21 Sep 2026): it
  // belongs to the goods, not to the figures, and its column's width goes to
  // the name and the figures.
  const hsnBelow = stackNote && show('hsn') && !show('unit');
  // Thickness, Cutting size, NOS, Quantity and Rate are all typed into and
  // all hold something short, so they share one width and sit as an even run
  // of boxes. The item name takes whatever is left. The width is style.css's
  // (--entry-w, 138px): on a smaller window these boxes give up room before
  // the name does (10 Oct 2026) -- at 138 each, a challan's three steel boxes
  // had no room left to type in under 1180px, and typing went in backwards.
  const ENTRY_W = 'var(--entry-w)';
  // Thickness and Cutting size ("0.90", "10x5") the same, less on a small one.
  const ENTRY_SM = 'var(--entry-sm)';
  // Below this the name column is not narrowed further: the grid scrolls
  // sideways instead, as the bill's does (`.lineitems.dense`). Room for a
  // product name, or for the steel Type and Quality side by side.
  const NAME_MIN = opts.steel ? 250 : 200;

  /* ------------------------------------------------------- stock on a line
     The goods stand in the two GST firms: a purchase buys them into one of
     them and the bill raised against them takes them out again. So while a
     bill is being made, what the firm is left with is a figure worth having
     on the screen -- not one to go and look up on the stock report after the
     bill has gone out. Same signs the server keeps stock by.

     The order firm is left out on purpose. It buys nothing and bills nothing;
     a quantity in its name is a quantity in nobody's stock, and showing one
     beside a challan line would be worse than showing none. */
  const LINE_STOCK = { PURCHASE: 1, CREDIT: 1, SALE: -1, DEBIT: -1 };
  const stockSign = taxFree() ? 0 : (LINE_STOCK[opts.mode] || 0);
  // A weighed line moves stock by its weight, matching the item's unit --
  // exactly as `stock_map` on the server reads it.
  const moved = l => (num(l.weight) > 0 ? num(l.weight) : num(l.qty));

  /** What each product on this screen stands at, before and after this doc.
   *
   *  `before` is what the firm holds with every *other* document counted;
   *  `after` is what it is left with once the lines on screen are counted
   *  too. A document already saved has moved its goods once -- reopening it
   *  and taking the same line out again would show the load leaving twice --
   *  so its saved effect is put back before the screen's own is applied.
   */
  function stockState() {
    if (!stockSign) return null;
    const before = {}, stocked = {};
    (S.items || []).forEach(i => {
      if (!i.track_stock) return;
      before[i.id] = num(i.stock);
      // A server that has not been restarted since this went in sends no
      // `stocked` at all. Absent is not false: reading it as false would put
      // "never bought into this firm" under every line on the screen. When
      // the answer has not been sent, assume stocked and say the figures.
      stocked[i.id] = ('stocked' in i) ? !!i.stocked : true;
    });
    (opts.stockSaved || []).forEach(l => {
      const id = num(l.item_id);
      if (id && id in before) before[id] -= stockSign * moved(l);
    });
    const after = Object.assign({}, before);
    lines.forEach(l => {
      const id = num(l.item_id);
      if (id && id in after) after[id] += stockSign * moved(l);
    });
    return { before, after, stocked };
  }

  /** A line's stock note: what the firm holds, and what this line leaves.
   *
   *  It gets a line of its own under the HSN note rather than joining it.
   *  The name column is narrow and that note already fills it, and the two
   *  say different things: one is what the goods *are*, this is how many of
   *  them there are.
   */
  function stockNote(l, st) {
    if (!st) return ['', ''];
    const id = num(l.item_id);
    if (!id || !(id in st.after)) return ['', ''];
    const unit = esc(l.unit || '');
    const have = st.before[id], left = st.after[id];
    // A product this firm has never bought is not a heap that has run out --
    // it is a name with no stock behind it. Every bill made from a challan
    // starts out naming the challan book's one generic product, which is
    // exactly that, so calling those lines "short" would put eight red lines
    // on every such bill and teach the warning to be ignored. Said plainly
    // instead, and the HSN band above is already asking for a real product.
    if (!st.stocked[id]) {
      return ['stk', 'never bought into this firm'];
    }
    // Nothing on the line yet to move it: only the standing figure is news.
    if (Math.abs(left - have) < 0.0005) {
      return ['stk', 'stock ' + qty(have) + ' ' + unit];
    }
    // Short of what is being asked for. Saving is not blocked -- the books go
    // back to 2013 and a few products stood negative before this screen
    // existed -- but it is said plainly, in red, on the line itself.
    return [left < 0 ? 'stk short' : 'stk',
      'stock ' + qty(have) + ' → ' + qty(left) + ' ' + unit
        + (left < 0 ? ' short' : '')];
  }

  /* ------------------------------------------------ steel on a challan line
     **Challan Stock** (26 Sep 2026): a challan in BST names the steel each
     line takes out of the yard -- type, quality, thickness, off the lists on
     the Challan Stock screen -- and its kilos come off that steel's stock.
     `opts.steel` = { lists, stock: {key: kg in hand}, saved: the lines this
     challan was saved with }. Like the bill's stock note: a challan already
     saved has taken its steel out once, so that is put back before the lines
     on screen are counted, and two lines of one steel both come off it. */
  const steel = opts.steel || null;
  const steelKey = l => (l.steel_type && l.steel_quality && l.steel_thick
    ? [l.steel_type, l.steel_quality, l.steel_thick].join('|') : '');
  function steelState() {
    if (!steel) return null;
    const before = Object.assign({}, steel.stock);
    (steel.saved || []).forEach(l => {
      const k = steelKey(l);
      if (k) before[k] = (before[k] || 0) + num(l.weight);
    });
    const after = Object.assign({}, before);
    lines.forEach(l => {
      const k = steelKey(l);
      if (k) after[k] = (after[k] || 0) - num(l.weight);
    });
    return { before, after };
  }
  /** Under the product: what is in stock of the steel it names, and what
   *  this line leaves -- or, for a name that is not a steel off the lists,
   *  that it takes nothing out of stock. */
  function steelNote(l, st) {
    const k = steelKey(l);
    if (!k) {
      const missing = [['steel_type', 'type'], ['steel_quality', 'quality'],
                       ['steel_thick', 'thickness']].filter(([f]) => !l[f]).map(([, w]) => w);
      if (missing.length < 3) {
        return ['stk steelnote idle', 'Pick the ' + missing.join(' and ') + ' too'];
      }
      return String(l.description || '').trim() || num(l.weight) > 0
        ? ['stk steelnote idle', 'No steel picked — this line takes nothing out of stock']
        : ['stk steelnote idle', ''];
    }
    const have = st.before[k] || 0, left = st.after[k] || 0;
    if (Math.abs(left - have) < 0.0005) return ['stk steelnote', 'In stock ' + qty(have) + ' kg'];
    return left < 0
      ? ['stk steelnote short', 'In stock ' + qty(have) + ' kg — short by ' + qty(-left) + ' kg']
      : ['stk steelnote', 'In stock ' + qty(have) + ' kg → ' + qty(left) + ' kg left after this'];
  }
  /* **The steel is the product, in three dropdowns** (26 Sep 2026: "these
     details should be under the product itself, so merge it", then "give
     dropbox option over here to quickly select the product", then
     "different dropbox for each item"). On a challan the product column is
     the steel: a dropdown each for its Type, Quality and Thickness, each
     list saying what is in stock of every choice given the others already
     picked -- Quality lists what there is of the chosen type, Thickness
     what there is of the chosen type and quality, in stock first. The
     line's product behind it stays the challan book's own (CRCA), which is
     what a bill made from the challan looks for, and its name becomes the
     steel's, CRCA - REGULAR - 0.50, which is what the challan prints. */
  /** The challan book's own product, which every steel line is written under. */
  const steelItem = () => (S.items || []).find(i => /^crca$/i.test(String(i.name || '').trim()))
    || (S.items || [])[0] || null;

  function calc(l) {
    // The rate is per kilo: the Quantity column -- kilos -- prices the line.
    // NOS is the piece count and never multiplies the rate. Worked out by
    // Gst.line, the same as the server's gst.line_amount.
    const taxed = !taxFree();
    const c = Gst.line(l, taxed);
    const x = Gst.taxOn(c.taxable, taxed ? l.gst_rate : 0,
                        taxed ? l.cess_rate : 0, false);
    const tax = Gst.money(x.cgst + x.sgst + x.igst);
    return Object.assign(c, { tax, cess: x.cess,
                              total: Gst.money(c.taxable + tax + x.cess) });
  }
  function totals() {
    const t = { subtotal: 0, discount: 0, taxable: 0, tax: 0, cess: 0,
                weight: 0, units: 0, rows: [] };
    lines.forEach(l => {
      const c = calc(l);
      t.subtotal += c.gross; t.discount += c.disc; t.taxable += c.taxable;
      t.tax += c.tax; t.cess += c.cess;
      t.weight += num(l.weight); t.units += num(l.qty);
      t.rows.push({ taxable: c.taxable, gst_rate: num(l.gst_rate),
                    cess_rate: num(l.cess_rate) });
    });
    ['subtotal', 'discount', 'taxable', 'tax', 'cess'].forEach(k => { t[k] = Gst.money(t[k]); });
    return t;
  }

  // The widths the boxes are drawn at. Every grid is laid out fixed, so the
  // boxes form one even run and the item name takes what is left. A roomy
  // grid gives each typed box ENTRY_W. The bill's are shares of the grid's
  // width, so the row keeps its proportions on any screen: sized off 18,071
  // bill lines (21 Sep 2026) -- NOS is 0 on every one, a weight runs to five
  // digits, a rate is "80.25", Amount and Total run to 33,83,644.00 -- and at
  // 1440px the name keeps about 420px. The HSN box, when it has a column, is
  // wider than eight digits need because the browser keeps room in it for the
  // code list's arrow.
  const W = roomy
    ? { no: '32px', nos: ENTRY_W, weight: ENTRY_W, hsn: '142px',
        rate: ENTRY_W, amount: 'var(--amount-w)', gst: '90px', total: '136px',
        del: '36px' }
    : { no: '28px', nos: '7%', weight: '9.5%', hsn: '124px', rate: '9%',
        amount: '12%', gst: '6.5%', total: '12.5%', del: '32px' };

  function draw() {
    host.innerHTML = `<div class="tbl-scroll"><table class="lineitems fixed${roomy ? ' roomy' : ' dense'}${centred ? ' centred' : ''}"><thead><tr>
      <th style="width:${W.no}"></th>
      <th class="name">${steel ? 'Steel — Type · Quality · Thickness' : 'Item/Product Name'}</th>
      ${show('thickness') ? `<th style="width:${roomy ? ENTRY_SM : '102px'}" class="${roomy ? 'c' : 'r'}" title="Sheet thickness — gauge, mm, whatever the order was taken in">Thickness</th>` : ''}
      ${show('cutting_size') ? `<th style="width:${roomy ? ENTRY_SM : '120px'}"${roomy ? ' class="c"' : ''} title="Size the sheet is to be cut to">Cutting size</th>` : ''}
      ${hasBasis ? `<th style="width:${roomy ? 124 : 94}px" title="Whether this line is taken as a number of sheets or as a total quantity">Ordered by</th>` : ''}
      <th style="width:${W.nos}" class="r" title="Piece count — it never multiplies the rate">NOS</th>
      ${show('weight') ? `<th style="width:${W.weight}" class="r" title="Kilos off the weighbridge — this is what the rate is charged on">Quantity</th>` : ''}
      ${(show('unit') || (show('hsn') && !hsnBelow)) ? `<th style="width:${show('unit') && show('hsn') ? '200px' : (show('hsn') ? W.hsn : '78px')}"${
        show('unit') ? '' : ' class="c"'}${
        show('hsn') ? ' title="Harmonised code the line is taxed under — it comes off the product, and prints on the bill"' : ''}>${
        show('unit') && show('hsn') ? 'Unit · HSN/SAC' : (show('unit') ? 'Unit' : 'HSN / SAC')}</th>` : ''}
      <th style="width:${W.rate}" class="r" title="${hasBasis ? 'Rate for whichever basis the line is taken on' : 'Rate per kilo'}">${hasBasis ? 'Rate' : 'Rate kg.'}</th>
      ${show('taxable') ? `<th style="width:${W.amount}" class="r">Amount</th>` : ''}
      ${taxFree() ? '' : `<th style="width:${W.gst}" class="c">Gst/Vat</th>`}
      ${stackNote ? '' : '<th style="min-width:164px">Item Detail</th>'}
      ${show('discount_pct') ? '<th style="width:82px" class="r">Disc %</th>' : ''}
      ${show('total') ? `<th style="width:${W.total}" class="r">Total</th>` : ''}
      <th style="width:${W.del}"></th></tr></thead>
      <tbody id="lg-body"></tbody></table></div>
      <div class="row" style="margin-top:10px;align-items:center">
        <button type="button" id="lg-add">＋ Add line</button>
        ${opts.newItem ? '<button type="button" id="lg-newitem" style="margin-left:8px">＋ New '
          + (opts.mode === 'PURCHASE' ? 'product' : 'item') + '</button>' : ''}
        <span style="font-size:11.5px;color:var(--muted);margin-left:9px">Enter moves to the next box</span>
        <div class="spacer"></div><div id="lg-tot"></div>
      </div>`;

    // The other columns' widths added up, and the name's least: narrower
    // than that, the grid scrolls sideways rather than squeeze the name.
    const tbl = $('table.lineitems', host);
    if (roomy) {
      const ws = $$('thead th', tbl).map(th => th.style.width).filter(Boolean);
      tbl.style.minWidth = 'calc(' + ws.concat(NAME_MIN + 'px').join(' + ') + ')';
    }
    // The columns change width with the window: Amount is centred again.
    if (centred && window.ResizeObserver) {
      let was = 0;
      new ResizeObserver(() => {
        const w = Math.round(tbl.getBoundingClientRect().width);
        if (w !== was) { was = w; centreFigures(); }
      }).observe(tbl);
    }

    const body = $('#lg-body', host);
    lines.forEach((l, idx) => body.appendChild(rowEl(l, idx)));
    $('#lg-add', host).onclick = addLine;
    // A product not in the list yet is made where the lines are, not from a
    // button up in the header.
    if (opts.newItem) $('#lg-newitem', host).onclick = newItemOnLine;
    paint();
  }

  /** A chosen product onto a line -- see the note on `takeItem`. */
  function applyItem(l, i) {
    const priced = num(l.rate) > 0 || num(l.incl_price) > 0;
    l.item_id = i.id;
    l.description = i.name;
    // A new product has no HSN of its own yet: the one on the line stays.
    l.hsn = hsnOfItem(i) || (i._new ? l.hsn : '');
    if (num(i.gst_rate)) l.gst_rate = num(i.gst_rate);
    if (num(i.cess_rate)) l.cess_rate = num(i.cess_rate);
    if (!priced) {
      l.unit = i.unit;
      l.by_weight = i.by_weight ? 1 : 0;
      l.rate = num(opts.mode === 'PURCHASE' ? i.purchase_rate : i.sale_rate);
    }
  }

  /** Redrawing loses the cursor, so it goes back where pressing Enter on the
   *  name would have left it -- the next box along. It used to jump to NOS,
   *  which stepped over the note, thickness and cutting size, the very boxes
   *  a sheet is cut by. */
  function focusPastName(i) {
    const row = $$('#lg-body tr', host)[i];
    if (!row) return;
    const ins = $$('input[data-f], select[data-basis]', row);
    const next = ins[ins.indexOf($('input[data-f=description]', row)) + 1];
    if (next) { next.focus(); next.select(); }
  }

  /** A product made from the button under the lines goes onto the first line
   *  with nothing on it yet, or a new line -- made and then still to be
   *  picked was one step too many (3 Oct 2026). */
  async function newItemOnLine() {
    const it = await opts.newItem();
    if (!it || !it.id || steel) return;
    let at = lines.findIndex(x => !x.item_id && !String(x.description || '').trim());
    if (at < 0) {
      const l = blankLine();
      suggest(l);
      lines.push(l);
      at = lines.length - 1;
    }
    applyItem(lines[at], it);
    draw();
    focusPastName(at);
  }

  function focusRow(i) {
    const row = $$('#lg-body tr', host)[i];
    if (!row) return;
    // On a challan, the first of the steel's boxes still to be chosen.
    const box = steel ? ($$('input[data-steel]', row).find(b => !b.value)
                         || $('input[data-steel]', row))
                      : $('input[data-f=description]', row);
    if (box) box.focus();
  }

  /** Add a line and land on it: the button, and Enter past the last box. */
  function addLine() {
    const l = blankLine();
    // Most of a challan is one kind of steel in several thicknesses: a new
    // line starts with the type and quality of the one above it.
    const above = lines[lines.length - 1];
    if (steel && above) {
      l.steel_type = above.steel_type || '';
      l.steel_quality = above.steel_quality || '';
    }
    suggest(l);
    lines.push(l);
    draw();
    focusRow(lines.length - 1);
  }

  /** A rate the document offers for a line nobody has priced yet -- a new
   *  challan's, off the customer's last challan (`opts.suggestRate`). A rate
   *  typed in is never written over; one that was offered is offered again
   *  when what it depends on changes, and taken away if there is nothing
   *  left to offer. Answers whether the line's rate changed. */
  function suggest(l) {
    if (!opts.suggestRate) return false;
    const open = l.rate === '' || l.rate === null || l.rate === undefined;
    if (!open && !l._autoRate) return false;
    const r = opts.suggestRate(l);
    if (r) {
      const was = l.rate;
      l.rate = r;
      l._autoRate = true;
      return was !== r;
    }
    if (l._autoRate) { l.rate = ''; l._autoRate = false; return true; }
    return false;
  }
  /** Put a line's rate back in its box, marked as offered or as typed. */
  function showRate(tr, l) {
    const box = tr && $('input[data-f=rate]', tr);
    if (!box) return;
    if (document.activeElement !== box) box.value = l.rate;
    box.classList.toggle('autorate', !!l._autoRate);
    if (l._autoRate && opts.rateNote) box.title = opts.rateNote();
    else box.removeAttribute('title');
  }
  /** Offer a rate on every line that has none of its own -- the customer
   *  has just been chosen, or changed. */
  function suggestRates() {
    const trs = $$('#lg-body tr', host);
    lines.forEach((l, i) => { if (suggest(l)) showRate(trs[i], l); });
    paint();
  }

  function rowEl(l, idx) {
    const tr = el(`<tr>
      <td class="c" style="color:var(--muted);font-size:11px">${idx + 1}</td>
      <td class="pick">${steel ? `<div class="steeltrio">
          ${[['steel_type', 'Type…'], ['steel_quality', 'Quality…'], ['steel_thick', 'Thickness…']]
            .map(([k, ph]) => `<div class="pickbox steelpickbox"><input data-steel="${k}"
              class="steelbox" autocomplete="off" spellcheck="false" value="${esc(l[k] || '')}"
              placeholder="${ph}"><button type="button" class="pickbtn" data-drop="${k}" tabindex="-1"
              title="Show the list, with what is in stock (F4)">▾</button></div>`).join('')}
        </div><div class="stk steelnote" data-o="steel"></div>` : `<div class="pickbox">
          <input data-f="description" value="${esc(l.description)}" autocomplete="off"
            placeholder="Pick a product, or type to search…">
          <button type="button" data-browse class="pickbtn" tabindex="-1"
            title="Browse the product list and pick what is being sold (F4)">▾</button></div>`}
        ${stackNote ? `<div class="subline">
          <input data-f="item_detail" class="sub" value="${esc(l.item_detail || '')}" placeholder="Item detail / note…">
          ${hsnBelow ? `<span class="hsnlab">HSN</span>
          <input data-f="hsn" class="hsn c" value="${esc(l.hsn || '')}" list="hsn-list" placeholder="code"
            title="Harmonised code the line is taxed under — it comes off the product, and prints on the bill">` : ''}
        </div>` : ''}
        ${steel ? '' : '<div class="picked" data-o="picked"></div>'}
        <div class="stk" data-o="stock"></div></td>
      ${show('thickness') ? `<td><input data-f="thickness" class="${roomy ? 'c' : 'r'}" value="${esc(l.thickness || '')}" placeholder="mm"></td>` : ''}
      ${show('cutting_size') ? `<td><input data-f="cutting_size"${roomy ? ' class="c"' : ''} value="${esc(l.cutting_size || '')}" placeholder="e.g. 10x5"></td>` : ''}
      ${hasBasis ? `<td><select data-basis style="width:100%">
        <option value="0"${l.by_weight ? '' : ' selected'}>NOS</option>
        <option value="1"${l.by_weight ? ' selected' : ''}>Quantity</option></select></td>` : ''}
      <td><input data-f="qty" class="num" value="${l.qty}"></td>
      ${show('weight') ? `<td><input data-f="weight" class="num" value="${l.weight || ''}" placeholder="kg"></td>` : ''}
      ${!(show('unit') || (show('hsn') && !hsnBelow)) ? ''
      : show('unit') && show('hsn') ? `<td><div style="display:flex;gap:4px">
          <input data-f="unit" value="${esc(l.unit || '')}" list="unit-list" style="width:52px">
          <input data-f="hsn" class="hsn" value="${esc(l.hsn || '')}" list="hsn-list" placeholder="HSN"></div></td>`
      : show('unit') ? `<td><input data-f="unit" value="${esc(l.unit || '')}" list="unit-list"></td>`
      : `<td><input data-f="hsn" class="hsn c" value="${esc(l.hsn || '')}" list="hsn-list" placeholder="HSN"></td>`}
      <td><input data-f="rate" class="num${l._autoRate ? ' autorate' : ''}" value="${l.rate}"${
        l._autoRate && opts.rateNote ? ' title="' + esc(opts.rateNote()) + '"' : ''}></td>
      ${show('taxable') ? '<td class="r num" data-o="taxable"></td>' : ''}
      ${taxFree() ? '' : (fixedGst
        ? '<td class="c num" data-o="gst"></td>'
        : `<td><input data-f="gst_rate" class="num c" value="${l.gst_rate}"></td>`)}
      ${stackNote ? '' : `<td><input data-f="item_detail" value="${esc(l.item_detail || '')}"></td>`}
      ${show('discount_pct') ? `<td><input data-f="discount_pct" class="num" value="${l.discount_pct}"></td>` : ''}
      ${show('total') ? '<td class="r num" data-o="total"></td>' : ''}
      <td class="c"><button type="button" class="link" data-del style="color:var(--bad)">✕</button></td>
    </tr>`);

    $$('input[data-f]', tr).forEach(inp => {
      inp.addEventListener('input', () => {
        l[inp.dataset.f] = inp.classList.contains('num') ? num(inp.value) : inp.value;
        // Without a basis column both boxes stay open and neither is asked
        // for: a line is priced on its Quantity when one is given, and on
        // NOS when it is not. Nothing has to be chosen up front.
        if (!hasBasis && ['weight', 'qty', 'unit'].indexOf(inp.dataset.f) >= 0) {
          l.by_weight = num(l.weight) > 0 ? 1 : 0;
        }
        // A rate typed in is the line's own from then on, and nothing
        // offered afterwards writes over it. Until then a rate that was
        // offered follows the thickness typed into the item detail.
        if (inp.dataset.f === 'rate') { l._autoRate = false; showRate(tr, l); }
        if (inp.dataset.f === 'item_detail' && suggest(l)) showRate(tr, l);
        paint();
      });
    });


    const basisSel = $('[data-basis]', tr);
    if (basisSel) {
      basisSel.addEventListener('change', () => {
        l.by_weight = num(basisSel.value);
        paint();
      });
    }
    $('[data-del]', tr).onclick = () => {
      lines.splice(idx, 1);
      if (!lines.length) lines.push(blankLine());
      draw();
      // Redrawing throws the cursor away, so put it on the line that has
      // taken this one's place. Not on its name box while that is empty: an
      // empty name box opens the product list, which is right on a line being
      // entered and a surprise on one just cleared away.
      const row = $$('#lg-body tr', host)[Math.min(idx, lines.length - 1)];
      if (row) {
        const name = $('input[data-f=description]', row) || $('input[data-steel]', row);
        const boxes = $$('input[data-f], select[data-basis]', row);
        (name.value.trim() ? name : (boxes[1] || name)).focus();
      }
    };

    /* One place where a chosen product lands on the line, whichever way it
       was chosen -- typed into the name box, or picked out of the browse
       dialog. What the product settles is what the goods *are* and how they
       are taxed: the name, the HSN, the GST and cess rates.

       **What the line is worth is not the product's to say, once the line
       already says it.** A bill raised from a challan arrives priced -- the
       same weights and the same per-kilo rates the customer signed the
       challan for -- and the product is picked over it afterwards only to
       put a proper HSN on the line. Taking the item master's rate there
       rewrote money that was already agreed, usually down to 0.00, and took
       the unit and the basis it is charged on with it: a rate per kilo left
       multiplying a piece count is the same bill wrong twice over.

       So a line that is already priced keeps its rate, its unit and its
       basis, and nothing but the user typing changes them. A line with no
       rate yet has nothing to keep and is filled from the product as
       before, which is the ordinary way a fresh bill is entered.

       The tax rate is read the way `pickProducts` reads it, so choosing the
       same product one line at a time and choosing it for every line at once
       come out the same: a product that names a rate is the more specific
       answer than the firm default the draft fell back on, and a product
       that names none -- 0 in the master is nobody having set it -- leaves
       the line with what it already had. Taking a bare zero across is how a
       tax invoice goes out with no tax on it. */
    function takeItem(i) {
      applyItem(l, i);
      draw();
      focusPastName(idx);
    }

    if (steel) {
      steelTrioOf(tr, l);
      return tr;
    }
    const descBox = $('input[data-f=description]', tr);
    descBox._picker = attachPicker(descBox, {
      source: async term => {
        const all = await loadItems();
        const t = term.toLowerCase();
        return all.filter(i => !t || i.name.toLowerCase().includes(t) ||
          (i.code || '').toLowerCase().includes(t) ||
          (i.hsn || '').includes(t)).slice(0, 30);
      },
      render: i => `<b>${esc(i.name)}</b><small>${esc(i.code || '')} ${i.hsn ? '· HSN ' + esc(i.hsn) : '· no HSN'}
        · ${fmt(i.gst_rate, 0)}% · ${money(opts.mode === 'PURCHASE' ? i.purchase_rate : i.sale_rate)}
        ${i.track_stock ? '· stock ' + qty(i.stock) + ' ' + esc(i.unit) : ''}</small>`,
      onPick: takeItem
    });

    /* The name box only finds a product once you know what it is called, so
       reaching it opens the whole list by itself -- the HSN, the rate and
       what is in stock beside every row, to be picked by eye.

       **Reaching it, not just reaching it empty.** A bill made from a challan
       arrives with every line already named, and the one thing left to do on
       it is to pick each line's proper product for its HSN. Opening only on
       an empty box meant that on the very screen where the list is wanted on
       every line it opened on none of them, and the arrow had to be reached
       for by hand. Enter walks onto the box and the list is already up. The
       arrow button and F4 are the same list for the times the walk went past
       it. */
    const browseItem = async how => {
      descBox._picker.close();
      const i = await chooseItem({ mode: opts.mode, taxFree: taxFree(),
                                  term: () => descBox.value, typed: !!(how && how.typed),
                                  hsn: l.hsn });
      if (i) takeItem(i); else descBox.focus();
      return i;
    };
    $('[data-browse]', tr).onclick = browseItem;
    opensPicker(descBox, browseItem, descBox._picker, true);

    // No keys of the grid's own (Ctrl+Enter and Ctrl+Del went on 23 Sep
    // 2026): → along the line reaches its ✕, ↓ past the last line reaches
    // ＋ Add line, and Enter presses either. A plain Enter bubbles up to
    // enterMovesOn, so the whole screen walks as one list.
    return tr;
  }

  /** The three steel dropdowns of a challan line. */
  function steelTrioOf(tr, l) {
    const L = steel.lists;
    const LIST = { steel_type: L.types, steel_quality: L.qualities, steel_thick: L.thicknesses };
    // What the line was called before it named a steel (an old challan's
    // "CRCA"), kept while the three are not all chosen.
    const base = !steelKey(l) ? (l.description || '') : '';
    // What is in stock for one choice in one box, with the other two boxes
    // as they stand -- nothing chosen in them counts every one.
    const haveFor = (k, v) => {
      const st = steelState().before;
      const want = { steel_type: l.steel_type, steel_quality: l.steel_quality,
                     steel_thick: l.steel_thick };
      want[k] = v;
      let sum = 0;
      Object.keys(st).forEach(key => {
        const [t, q, th] = key.split('|');
        if ((!want.steel_type || t === want.steel_type)
            && (!want.steel_quality || q === want.steel_quality)
            && (!want.steel_thick || th === want.steel_thick)) sum += st[key];
      });
      return sum;
    };
    // The line follows its three boxes: all chosen, it is that steel.
    const settle = () => {
      if (steelKey(l)) {
        l.description = steelName(l);
        const gen = steelItem();
        if (!l.item_id && gen) { l.item_id = gen.id; if (!l.unit) l.unit = gen.unit || ''; }
        l.by_weight = 1;
        if (suggest(l)) showRate(tr, l);
      } else {
        l.description = base;
      }
      paint();
    };
    $$('input[data-steel]', tr).forEach(input => {
      const k = input.dataset.steel, list = LIST[k];
      const mark = () => {
        const t = input.value.trim();
        const wrong = !!t && !steelHit(list, t);
        input.classList.toggle('wrong', wrong);
        input.title = wrong ? '“' + t + '” is not on the steel list — pick one from the list' : '';
      };
      const choose = v => {
        input.value = v;
        l[k] = v;
        mark();
        settle();
        moveOn(input, $('#view'));
      };
      const options = term => {
        const m = steelMatches(list, term).map(v => ({ v, have: haveFor(k, v) }));
        // Opened to browse, what is in stock comes first.
        if (!String(term || '').trim()) {
          m.sort((a, b) => ((b.have > 0.0005) - (a.have > 0.0005))
                           || list.indexOf(a.v) - list.indexOf(b.v));
        }
        return m;
      };
      const picker = attachPicker(input, {
        source: async term => options(term),
        render: r => '<span class="nm">' + esc(r.v) + '</span><span class="kg'
          + (r.have > 0.0005 ? ' have' : r.have < -0.0005 ? ' short' : '') + '">'
          + (r.have > 0.0005 ? qty(r.have) + ' kg' : r.have < -0.0005
            ? 'short ' + qty(-r.have) + ' kg' : 'none') + '</span>',
        onPick: r => choose(r.v),
        maxHeight: 380,
        listClass: 'steelpick-list'
      });
      // A dropdown: a click in the box, its ▾, F4, or Enter while it is
      // empty opens the list. Enter on what is typed takes it at once --
      // the list lags a key behind -- and something not on the list stays
      // in red.
      input.addEventListener('click', () => { if (!picker.isOpen()) picker.refresh(); });
      input.addEventListener('keydown', e => {
        if (e.key === 'F4') { e.preventDefault(); picker.refresh(); return; }
        if (e.key !== 'Enter' || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return;
        const t = input.value.trim();
        if (!t) {
          if (!picker.isOpen()) {
            e.preventDefault();
            e.stopImmediatePropagation();
            picker.refresh();
          }
          return;
        }
        const m = steelMatches(list, t);
        const lit = picker.lit();
        const pick = lit && m.indexOf(lit.v) >= 0 ? lit.v : m[0];
        e.preventDefault();
        e.stopImmediatePropagation();
        picker.close();
        if (pick) { choose(pick); return; }
        l[k] = '';
        mark();
        settle();
        input.select();
      }, true);
      // Typing again lets go of what the box held until a choice is made.
      input.addEventListener('input', () => {
        input.classList.remove('wrong');
        if (l[k]) { l[k] = ''; settle(); }
      });
      input.addEventListener('change', () => {
        const hit = steelHit(list, input.value);
        if (hit) input.value = hit;
        if (hit !== l[k]) { l[k] = hit; settle(); }
        mark();
      });
      $('[data-drop="' + k + '"]', tr).onclick = () => { input.focus(); picker.refresh(); };
    });
  }

  function paint() {
    // Worked out once for the whole grid: two lines of the same product both
    // come off the same heap, and each has to see the other's bite taken.
    const st = stockState();
    const sst = steelState();
    let short = 0;
    $$('#lg-body tr', host).forEach((tr, i) => {
      const l = lines[i];
      const c = calc(l);
      const stlNote = sst && $('[data-o=steel]', tr);
      if (stlNote) {
        const [cls, text] = steelNote(l, sst);
        stlNote.className = cls;
        stlNote.textContent = text;
      }
      const amtCell = $('[data-o=taxable]', tr);
      if (amtCell) amtCell.textContent = fmt(c.taxable);
      const gstCell = $('[data-o=gst]', tr);
      if (gstCell) gstCell.textContent = fmt(num(l.gst_rate), 0) + '%';
      const totCell = $('[data-o=total]', tr);
      if (totCell) totCell.textContent = fmt(c.total);
      // Under the name, what the line is actually carrying: the product it
      // was taken from and the HSN, unit and rate that came with it. A line
      // typed straight in says so, because nothing filled its HSN and the
      // bill will go out without one unless it is entered by hand.
      const pickNote = $('[data-o=picked]', tr);
      if (pickNote) {
        const taxed = !taxFree();
        if (l.item_id) {
          pickNote.className = 'picked on';
          // With the HSN box on the line just above, the note does not
          // repeat the code -- only says so when there is none.
          pickNote.innerHTML = '✓ ' + [
            !taxed ? '' : !l.hsn ? '<span class="warn">no HSN on this product</span>'
                  : hsnBelow ? '' : 'HSN ' + esc(l.hsn),
            esc(l.unit || ''),
            taxed ? fmt(num(l.gst_rate), 0) + '% GST' : ''
          ].filter(Boolean).join(' · ');
        } else if (String(l.description || '').trim()) {
          pickNote.className = 'picked off';
          // Without a product the line moves no stock either -- a purchase
          // typed straight in never reaches the firm's books as goods, and a
          // bill typed straight in never takes any out. That is worth saying
          // on the very screen where the product is still one key away.
          const noStock = stockSign
            ? (stockSign > 0 ? ' — and nothing is bought into stock'
                             : ' — and nothing comes out of stock')
            : '';
          pickNote.innerHTML = !taxed
            ? 'Typed in, not from the product list'
            : l.hsn ? 'Typed in · HSN ' + esc(l.hsn)
                      + (noStock ? '<span class="warn">' + noStock + '</span>' : '')
            : '<span class="warn">Typed in — no product, no HSN'
                      + noStock + '</span>';
        } else {
          pickNote.className = 'picked';
          pickNote.textContent = '';
        }
      }
      // And under that, how many of them the firm has. A bill is where the
      // stock goes out, so what is left is read here rather than looked up
      // on the stock report afterwards.
      const stkNote = $('[data-o=stock]', tr);
      if (stkNote) {
        const [cls, text] = stockNote(l, st);
        stkNote.className = cls;
        stkNote.textContent = text;
        if (cls === 'stk short') short++;
        stkNote.title = text
          ? (stockSign > 0 ? 'Stock in this firm, and what buying this line in '
                             + 'brings it to'
                           : 'Stock in this firm, and what billing this line '
                             + 'out leaves')
          : '';
      }
      // When an agreed price is typed, the rate is worked back from it.
      // With a basis column the row says which figure is the one that counts.
      if (hasBasis) {
        tr.classList.toggle('by-weight', !!l.by_weight);
        tr.classList.toggle('by-qty', !l.by_weight);
      }
      const rateBox = $('[data-f=rate]', tr);
      if (num(l.incl_price) > 0 && document.activeElement !== rateBox) {
        rateBox.value = c.rate ? c.rate.toFixed(4).replace(/0+$/, '').replace(/\.$/, '') : '';
        rateBox.readOnly = true;
        rateBox.title = 'Worked back from the agreed price';
      } else {
        rateBox.readOnly = false;
        // A rate offered off the customer's last challan says where it came
        // from, and is tinted until it is typed over.
        rateBox.title = l._autoRate && opts.rateNote ? opts.rateNote() : '';
      }
      rateBox.classList.toggle('autorate', !!l._autoRate);
    });
    centreFigures();
    const t = totals();
    const igst = isIgst();
    const bits = [];
    if (show('taxable')) {
      bits.push(`<span style="color:var(--muted)">Amount</span> <b class="num">${fmt(t.taxable)}</b>`);
    }
    if (show('weight') && t.weight) {
      bits.push(`<span style="color:var(--muted)">Quantity</span> <b class="num">${qty(t.weight)}</b>`);
    }
    // Tax on its own means nothing once the amount it is charged on is gone.
    if (!noTax() && show('taxable')) {
      bits.push(`<span style="color:var(--muted)">${igst ? 'IGST' : 'CGST+SGST'}</span>
        <b class="num">${fmt(t.tax)}</b>`);
    }
    // A line short of stock says so under its own name, but the grid scrolls
    // and the strip under it does not -- so the count is repeated where the
    // totals are read.
    if (short) {
      bits.push('<span class="tag warn">' + short + ' line'
        + (short > 1 ? 's' : '') + ' short of stock</span>');
    }
    $('#lg-tot', host).innerHTML = bits.join(' &nbsp;·&nbsp; ');
    onChange(t);
  }

  /** Amount and Total, centred the way the printed bill centres a column.
   *
   *  The figures are read down the column, so their digits stay right-aligned
   *  and the decimal points stack -- 33,83,644.00 over 7,383.00 -- and it is
   *  that whole block, as wide as its widest figure, that is set in the
   *  middle of the column. Centring each figure on its own would leave
   *  nothing under anything on a bill of several lines (`_numcol` in
   *  bst/invoice.py is the same rule). */
  function centreFigures() {
    if (!centred) return;
    const r = document.createRange();
    ['taxable', 'total'].forEach(k => {
      const cells = $$('#lg-body td[data-o="' + k + '"]', host);
      let widest = 0;
      cells.forEach(td => {
        r.selectNodeContents(td);
        widest = Math.max(widest, r.getBoundingClientRect().width);
      });
      cells.forEach(td => {
        const w = td.getBoundingClientRect().width;
        td.style.paddingRight = Math.max(5, (w - widest) / 2) + 'px';
      });
    });
  }

  draw();
  return { totals, redraw: draw, repaint: paint, calc, suggestRates };
}

/* ====================================================== invoice / bill UI
   Laid out to match the SHREE "Entry of Sale Voucher" window: same field
   names, same order, same totals block.                                   */

const VOUCHER_TITLE = {
  SALE: 'Entry of Sale Voucher', PURCHASE: 'Entry of Purchase Voucher',
  CREDIT: 'Entry of Credit Note', DEBIT: 'Entry of Debit Note'
};
const TRADE_AC_LABEL = {
  SALE: 'Sales A/c', CREDIT: 'Sales A/c',
  PURCHASE: 'Purchase A/c', DEBIT: 'Purchase A/c'
};

// The e-way bill portal's own dropdowns. Kept in the same order and wording
// as app/eway.py, which is what actually writes them into the file.
const EWAY_MODES = [['1', 'Road'], ['2', 'Rail'], ['3', 'Air'], ['4', 'Ship']];
const EWAY_VEHICLE_TYPES = [['R', 'Regular'], ['O', 'Over Dimensional Cargo']];
const EWAY_SUB_SUPPLY = [
  ['1', 'Supply'], ['2', 'Import'], ['3', 'Export'], ['4', 'Job Work'],
  ['5', 'For Own Use'], ['6', 'Job Work Returns'], ['7', 'Sales Return'],
  ['8', 'Others'], ['9', 'SKD/CKD/Lots'], ['10', 'Line Sales'],
  ['11', 'Recipient Not Known'], ['12', 'Exhibition or Fairs']
];

/** Every key there is -- which since 23 Sep 2026 is not many. Asked: "don't
 *  assign different keys to different functions; everything should be
 *  controllable by the arrow keys or something similar." So the arrows go
 *  to a thing and Enter does it, on every screen and in every dialog, and
 *  the only other keys are the ones every Windows program already has.
 *  Reachable from F1 anywhere, from the ⌨ Keys button at the top, and from
 *  the Shortcut Key button on the entry screens. It opens with what the
 *  screen that is open answers to, and can be printed. */
const KEY_GUIDE = [
  ['Five keys do everything', [
    ['↑ ↓ ← →', 'Move to the next thing that way — a box, a button, a tick '
      + 'box, a link, a line of a list, the buttons at the top of the screen. '
      + 'The highlight shows where you are.'],
    ['Enter', 'Do what the highlight is on — open the line, press the '
      + 'button, follow the link, tick the box. In a box on a bill or in a '
      + 'form, the next box.'],
    ['Space', 'Tick a tick box or a line of a list; open a dropdown'],
    ['Esc', 'Close what is open — a list, a dialog. With nothing open, '
      + 'back to the screen you were on before — it asks first if an entry is '
      + 'half typed. On a screen of its own in the menu — Challans, Bills, '
      + 'Who owes us — up to the menu on the left, on that screen\'s group '
      + '(Sales, Reports …); Esc again comes back in where you were. The ← at '
      + 'the top left goes further back.'],
    ['Tab', 'The next thing in order, as in every program']
  ]],
  ['On a list', [
    ['↑ ↓', 'The line above or below. Off the top of the list, back up to '
      + 'the box you came down from — the search box, as a rule.'],
    ['→', 'Along the line to its own buttons and links — Bill it, Print, '
      + 'Delete, the customer — then Enter. ← comes back. A line with nothing '
      + 'along it that opens — a bank account, a month, a line with › at its '
      + 'end — → opens it, as Enter does.'],
    ['→ ←', 'Open or close a line that folds (a month, a group, a customer’s '
      + 'bills). On a screen read a month at a time, the month after or '
      + 'before.'],
    ['←', 'Inside a customer’s opened bills, back to the customer. On a '
      + 'statement in two columns, across to the other side. On a line '
      + 'with nothing to its left, the menu.'],
    ['Enter', 'Open the line — or edit it, or drop into its dropdown'],
    ['Space', 'Tick the line'],
    ['Ctrl+↑  Ctrl+↓', 'The first line of the list, or the last — '
      + 'from anywhere on the screen, the search box too. A long bank or '
      + 'cash book is drawn to its last entry first. Ctrl+Home and Ctrl+End '
      + 'do the same; on a Mac, Cmd+↑ and Cmd+↓.'],
    ['Home End PgUp PgDn', 'On the list: the first line, the last, ten at a time'],
    ['(typing)', 'Searches the list — the letters go into its search box; ↓ '
      + 'comes back down into the list']
  ]],
  ['In a box', [
    ['← →', 'Along what is typed; at either end, on to the next box. On a '
      + 'bill, an order or in a dialog, → past the end of a line goes on to '
      + 'the first box of the next line, and ← before the start of one back '
      + 'to the line above.'],
    ['↑ ↓', 'The box above or below'],
    ['Enter', 'On a bill, an order, a journal or a form: the next box, past '
      + 'the last line another line, and past the last box onto Save. Over a '
      + 'list: Apply, and down into the list.'],
    ['Space or Enter', 'On a dropdown: open it — then ↑ ↓ and Enter choose'],
    ['(a customer or product box)', 'Enter on an empty one opens the whole '
      + 'list — town, GSTIN and balance beside every party; rate and stock '
      + 'beside every product. → reaches the ▾ beside one that already names '
      + 'one.']
  ]],
  ['The menu on the left', [
    ['← or Esc', 'Into the menu, onto the screen\'s own entry: ← from the '
      + 'left edge of a screen, Esc on a screen of the menu\'s own'],
    ['↑ ↓', 'The groups — Sales, Reports, Purchase & Cash … — and the '
      + 'screens of the one that is open; off the top, the firm box (Enter to '
      + 'switch firm)'],
    ['Enter or →', 'On a group: show the screens in it (one group is open at '
      + 'a time). On a screen: open it.'],
    ['←', 'From a screen in the menu up to its group; on an open group, fold '
      + 'it'],
    ['Esc', 'On a screen: up to its group, folded. On a folded group: back '
      + 'into the screen just as it was left']
  ]],
  ['In a dialog', [
    ['↑ ↓ ← →', 'Between its boxes, tick boxes and buttons — past the end of '
      + 'a line, on to the next'],
    ['Enter', 'In a form: the next box, then onto Save — Enter there saves. '
      + 'On a button: press it.'],
    ['(a question)', 'Opens on Cancel, so Enter leant on changes nothing: → '
      + 'to Yes, then Enter.'],
    ['Esc', 'Close it, changing nothing — asks first if anything was typed']
  ]],
  ['In a date box', [
    ['(type)', 'Day first, always: <b>02-10-2026</b>, 2/10/26, 021026 or 2 Oct. '
      + 'Just <b>2</b> is the 2nd of the month the box shows; <b>2-10</b> is 2 '
      + 'October in the same financial year. The day of the week shows as you type.'],
    ['F4', 'The calendar: ← → a day, ↑ ↓ a week, PgUp PgDn a month, Enter takes '
      + 'it, Esc closes it'],
    ['Esc', 'After a date that is not one (31-02): puts back the date the box had']
  ]],
  ['The keys every Windows program has', [
    ['Ctrl + S', 'Save'],
    ['Ctrl + P', 'Print the bill or challan on screen, or the one on the '
      + 'highlighted line'],
    ['Alt + P', 'Print the challan — of the bill or challan on screen, or of '
      + 'the highlighted line'],
    ['Ctrl + F', 'The search box'],
    ['Ctrl + A', 'Tick every line of the list'],
    ['Delete', 'Delete the ticked lines — or the highlighted one. It always '
      + 'asks first.'],
    ['F4', 'Open the list of a customer, product or account box — or, '
      + 'on a date, the calendar'],
    ['F1', 'This list'],
    ['Alt + ←', 'Back to the screen before — the ← at the top left. A '
      + 'half-typed entry asks first.']
  ]]
];

function keyTable(list) {
  const K = (...keys) => keys.map(k => '<span class="kbd">' + k + '</span>').join(' ');
  return '<table class="keys"><tbody>' + list.map(([k, what]) =>
    '<tr><td class="nw">' + (/^\(/.test(k) ? '<i>' + esc(k) + '</i>'
      : K(...k.split(' or ').map(esc))) + '</td><td>' + what + '</td></tr>').join('')
    + '</tbody></table>';
}

function shortcutHelp() {
  if ($('#modal-root .keyguide')) return;
  KB.helpSeen();
  const here = KB.screenKeys();
  const title = ($('#page-title').childNodes[0] || {}).textContent || 'this screen';
  const sections = KEY_GUIDE.map(([head, list]) =>
    '<h3>' + esc(head) + '</h3>' + keyTable(list)).join('');
  const screen = here.length
    ? '<h3>On this screen — ' + esc(title.trim()) + '</h3>'
      + keyTable(here.map(([k, what]) => [k, esc(what)])) : '';
  let barOn = true;
  try { barOn = localStorage.getItem('kb.bar') !== 'off'; } catch (e) { /* none */ }
  const body = `<div class="keyguide">
      <p class="lead">The arrows go to a thing and Enter does it — on every
        screen and in every dialog. The bar along the bottom of the window
        always says what Enter will do where you are.</p>
      ${screen}${sections}
      <label class="check" style="margin-top:16px"><input type="checkbox" id="kb-bar"${
        barOn ? ' checked' : ''}> Show the keys along the bottom of the window</label>
    </div>`;
  return modal('Keyboard', body, [
    { label: '🖨 Print this list', value: null, before: () => {
        // A sheet to keep by the keyboard: every key, without the screen's.
        const area = $('#print-area');
        area.dataset.format = 'keys';
        pageStyle(false);
        area.innerHTML = '<div class="keyguide print"><h2>Billing — the keyboard</h2>'
          + sections + '</div>';
        document.body.classList.add('print-doc');
        try { window.print(); } finally { document.body.classList.remove('print-doc'); }
        return false;
      } },
    { label: 'Close', value: null, cls: 'primary' }
  ], { wide: true, onOpen: bg => {
    const box = $('#kb-bar', bg);
    box.onchange = () => KB.showBar(box.checked);
    // Opens on Close, so Enter or Esc puts it away again -- and at the top:
    // Close is at the foot of a long list, which PgUp and PgDn scroll.
    $('.ft button.primary', bg).focus({ preventScroll: true });
    bg.scrollTop = 0;
  } });
}

route(/^\/invoice\/(new|\d+)$/, async (view, m, q) => {
  const isNew = m[0] === 'new';
  const here = onScreen();

  // A draft says which firm's books the new document is written in. The
  // buttons that open this screen switch to that firm first, but Back, a
  // reload or the update bar's reload arrive with whatever firm is open --
  // and a bill drawn in the challan book came out as challan G 36 with no
  // tax. So the draft is read first and the firm follows it, before the
  // parties and items of the wrong firm are loaded.
  let draft = null, draftError = null;
  if (isNew && q.from_order) {
    try {
      draft = await api.get('/api/orders/' + q.from_order +
        (q.as === 'challan' ? '/challan-draft' : '/invoice-draft'),
        { company: q.order_company || '' });
    } catch (e) { draftError = e.message; }
  } else if (isNew && q.from_challan) {
    try {
      draft = await api.get('/api/invoices/' + q.from_challan + '/gst-bill-draft',
        { company: q.challan_company || '', firm: q.firm || '' });
    } catch (e) { draftError = e.message; }
  }
  if (draft && draft.company_id && draft.company_id !== S.meta.company_id) {
    await useCompany(draft.company_id);
    drawCompany();
  }
  await Promise.all([loadParties('fresh'), loadItems('fresh')]);
  if (!here()) return;

  let doc, lines;
  if (isNew) {
    doc = {
      doc_type: (draft && draft.doc_type) || (q.type || 'SALE').toUpperCase(),
      series: '', invoice_no: '',
      invoice_type: (draft && draft.invoice_type) || 'Debit',
      invoice_date: today(),
      party_id: (draft && draft.party_id) || '',
      party_name: (draft && draft.party_name) || '',
      order_id: (draft && draft.order_id) || null,
      order_no: (draft && draft.order_no) || '',
      from_challan: (draft && draft.from_challan) || null,
      challan_no: (draft && draft.challan_no) || '',
      challan_total: (draft && draft.challan_total) || 0,
      taxable_adj: (draft && draft.taxable_adj) || 0,
      split_basis: (draft && draft.split_basis) || '',
      ref_no: (draft && draft.ref_no) || '',
      driver_name: (draft && draft.driver_name) || '',
      payment_terms: (draft && draft.payment_terms) || '',
      // Carried from the challan when the bill came from one. These were
      // reset here, so a bill lost the challan's department and e-way number
      // and both had to be typed again.
      dept: (draft && draft.dept) || 'Stores Department',
      // A bill repeats its challan, charges and all; GST goes on the total.
      cutting: (draft && draft.cutting) || 0,
      transport: (draft && draft.transport) || 0,
      loading: (draft && draft.loading) || 0,
      remarks: (draft && draft.remarks) || '',
      transporter: (draft && draft.transporter) || '',
      vehicle_no: (draft && draft.vehicle_no) || '',
      lr_no: (draft && draft.lr_no) || '',
      lr_date: (draft && draft.lr_date) || '',
      cases: (draft && draft.cases) || '',
      eway_no: (draft && draft.eway_no) || '',
      transporter_gstin: (draft && draft.transporter_gstin) || '',
      transport_mode: (draft && draft.transport_mode) || '',
      transport_distance: (draft && draft.transport_distance) || '',
      vehicle_type: (draft && draft.vehicle_type) || 'R',
      sub_supply_type: (draft && draft.sub_supply_type) || '',
      is_igst: null, rcm: 0, sales_ac_id: ''
    };
    // A draft that failed must not quietly become a blank document: the whole
    // point of coming here from a challan is that nothing needs typing, so an
    // empty screen reads as "type it all again" rather than as an error.
    if (draftError) {
      setTitle('Could not build the bill');
      view.innerHTML = '<div class="card"><div class="bd">'
        + '<b>The bill could not be prepared from that challan</b>'
        + '<p style="color:var(--bad);margin:8px 0 14px">' + esc(draftError) + '</p>'
        + '<p style="margin:0 0 14px;color:var(--muted);font-size:13.5px">'
        + 'Nothing was saved. Set the customer\u2019s billing firm on the '
        + 'Customer Allocation screen (the button below), then bill the challan '
        + 'again.</p>'
        + '<div class="row"><button id="back">Back to the challan</button>'
        + '<a class="btn" href="#/allocation">Customer Allocation</a></div>'
        + '</div></div>';
      const back = $('#back', view);
      if (back) back.onclick = async () => {
        const cc = num(q.challan_company);
        if (cc && cc !== S.meta.company_id) await useCompany(cc);
        location.hash = '#/invoice/' + (q.from_challan || '');
      };
      return;
    }
    lines = (draft && draft.lines.length) ? draft.lines : [blankLine()];
    // Both boxes come off the last bill or challan filed in this book: the
    // number carries on from it, and the Sr box says which run it was in.
    // Left blank, the run was lost on every new bill and the number came out
    // of whichever run happened to be highest.
    const opening = await api.get('/api/next-no',
      { key: doc.doc_type, fy: fyLabel(doc.invoice_date) });
    doc.invoice_no = opening.number;
    doc.series = opening.series || '';
  } else {
    doc = await api.get('/api/invoices/' + m[0]);
    // Carry every priced field back. Dropping weight/by_weight here left the
    // kg box blank and made the grid reprice the line on the piece count, and
    // saving again wrote that zero weight over the real one.
    lines = doc.lines.map(l => ({
      item_id: l.item_id, description: l.description, hsn: l.hsn, unit: l.unit,
      qty: l.qty, weight: l.weight, by_weight: l.by_weight,
      incl_price: l.incl_price, rate: l.rate, discount_pct: l.discount_pct,
      gst_rate: l.gst_rate, cess_rate: l.cess_rate,
      item_detail: l.item_detail || '',
      thickness: l.thickness || '', cutting_size: l.cutting_size || '',
      // The steel it took out of Challan Stock -- left off, saving the
      // challan again would have put it all back.
      steel_type: l.steel_type || '', steel_quality: l.steel_quality || '',
      steel_thick: l.steel_thick || ''
    }));
    if (!lines.length) lines = [blankLine()];
  }
  if (!here()) return;

  const type = doc.doc_type;
  // The challan book numbers itself; every other document still lets the
  // number be set by hand.
  const autoNo = noTax() && type === 'SALE';
  const isPurchase = type === 'PURCHASE';
  const partyKind = (isPurchase || type === 'DEBIT') ? 'supplier' : 'customer';
  const tradeList = (isPurchase || type === 'DEBIT')
    ? S.meta.lookups.purchase_accounts : S.meta.lookups.sales_accounts;
  // The box always names a real account, as it does in the old screen.
  const systemKey = { SALE: 'SALES', CREDIT: 'SALES_RETURN',
    PURCHASE: 'PURCHASE', DEBIT: 'PURCHASE_RETURN' }[type];
  const fallbackAc = (tradeList.find(t => t.system_key === systemKey)
    || tradeList[0] || {}).id;
  const tradeDefault = num(doc.sales_ac_id) || fallbackAc;

  // In the no-tax firm the document IS a challan, so "Print bill" and
  // "Challan" both came out as the same challan -- two buttons for one
  // sheet. Only a GST firm has two layouts worth choosing between.
  // A purchase prints as the PURCHASE BILL it is; it has no challan.
  //
  // **Only what is saved is printed** (6 Oct 2026: "to print a challan or
  // bill, it needs to always be saved or updated, otherwise it shouldn't be
  // printed"). A new document is saved first and one changed since it was
  // opened is updated first -- through Save itself, every check it makes
  // still made -- and the sheet is printed off the books once that save has
  // gone through. A save refused prints nothing. The buttons say which it
  // will be ("Save & print", "Update & print"); Ctrl+P and Alt+P press them.
  const PRINTS = noTax() ? [['challan', 'Print challan']]
    : isPurchase ? [[undefined, 'Print']]
      : [[undefined, 'Print bill'], ['challan', 'Challan']];
  const printLabel = (plain, pending) => '🖨 ' + (!pending ? plain
    : (isNew ? 'Save & ' : 'Update & ')
      + (/^print\b/i.test(plain) ? plain.toLowerCase() : 'print ' + plain.toLowerCase()));
  setTitle(VOUCHER_TITLE[type],
    isNew ? 'New entry'
      : docNo(doc) + ' · saved ' + dt(doc.invoice_date) +
        (doc.billed_invoice_no ? ' · billed as ' + billedAs(doc)
          + (doc.billed_firm_code ? ' by ' + doc.billed_firm_code : '') : ''),
    [
      ...(!isNew && noTax() && type === 'SALE' && !doc.billed_invoice
        ? [{ label: '🧾 Make GST Bill', cls: 'primary',
             onClick: () => billChallan(doc.id) }] : []),
      ...PRINTS.map(([format, plain]) => ({
        label: printLabel(plain, isNew), cls: 'doc-print',
        onClick: () => printDoc(format) })),
      ...(isNew ? [] : [
        { label: '🚚 E-way JSON',
          onClick: () => ewayFor(doc.company_id, doc.id) },
        // Only a GST firm raises an e-invoice; a challan has no IRN, and a
        // purchase's e-invoice is the supplier's to raise -- the button could
        // only ever end in that refusal.
        ...(noTax() || isPurchase ? [] : [{ label: '🔖 e-Invoice JSON',
          onClick: () => einvoiceFor(doc.company_id, doc.id) }]),
        // Every save kept, and what each one changed (6 Oct 2026).
        { label: '🕘 History' + (doc.history_count ? ' (' + doc.history_count + ')' : ''),
          onClick: () => docHistory(doc) },
        {
          label: 'Delete', cls: 'danger',
          onClick: () => deleteInvoice(doc.id, docNo(doc),
            () => leave(listFor(doc.doc_type)))
        }])]);

  // A GST purchase or debit note also asks whether the tax is reverse charge.
  const rcmBox = (isPurchase || type === 'DEBIT') && !noTax();

  view.innerHTML = `
    <datalist id="unit-list">${S.meta.units.map(u => '<option>' + u + '</option>').join('')}</datalist>
    <datalist id="hsn-list">${hsnOptions()}</datalist>
    <datalist id="dept-list">${S.meta.lookups.departments.map(d => '<option>' + esc(d) + '</option>').join('')}</datalist>

    <div class="voucher">
      <div class="vhead">${esc(VOUCHER_TITLE[type])}
        ${doc.order_no ? '<span class="tag ok">' + goLink(orderLink(doc.order_id),
          'from order ' + esc(doc.order_no), 'Open the order') + '</span>' : ''}
        ${doc.challan_no ? '<span class="tag ok">' + goLink(
          docLink('SALE', doc.from_challan, doc.challan_company || num(q.challan_company)),
          'from challan ' + esc(doc.challan_no), 'Open the challan') +
          ' · ₹' + fmt(doc.challan_total) + ' + GST</span>'
          : (doc.challans || []).length ? '<span class="tag ok"'
            + linkedTitle(doc.challans) + '>from challan '
            + linkedDocs(doc.challans) + '</span>' : ''}
        ${doc.billed_invoice && doc.billed_invoice_no ? '<span class="tag ok"'
          + linkedTitle(doc.bills) + '>billed as '
          + ((doc.bills || []).length > 1 ? linkedDocs(doc.bills)
            : goLink(docLink('SALE', doc.billed_invoice, doc.billed_company),
                     esc(doc.billed_invoice_no), 'Open the bill'))
          + (doc.billed_firm_code ? ' · ' + esc(doc.billed_firm_code) : '')
          + '</span>' : ''}
        ${doc.gst_voucher_no ? '<span class="tag info">' + goLink(
          docLink('JOURNAL', doc.gst_voucher, doc.gst_voucher_company),
          'GST journal ' + esc(doc.gst_voucher_no),
          'The GST this bill put on the challan\'s customer') + '</span>' : ''}
        ${!isNew && doc.party_id ? '<span class="tag">' + goLink(accountLink(doc.party_id),
          '📖 ' + esc(doc.party_name) + '\'s account', 'Open the account') + '</span>' : ''}
        <span class="tag info" id="last-rate" style="display:none"></span>
        <span class="rt">${esc(S.meta.company.name)} · ${esc(fyLabel(doc.invoice_date))}</span></div>

      <div class="vtop">
        <div>
          <div class="vgrid">
            <!-- Everything about the party sits together: who, where, what
                 they owe, and the button for one not in the book yet. The
                 balance and the button used to be in the band below, where
                 the button ran off the right edge. -->
            <label>To :</label>
            <div class="pick partyrow"><div class="pickbox">
              <input id="f-party" autocomplete="off"
                placeholder="Pick a party, or type to search…" value="${esc(doc.party_name || '')}">
              <button type="button" data-browse-party class="pickbtn" tabindex="-1"
                title="Browse the party list and pick who this is for (F4)">▾</button></div>
              <button type="button" id="new-party" class="sm">＋ New party</button></div>
            <label>Address</label>
            <input id="f-address" readonly value="">
            <label>Current Balance</label>
            <input id="f-balance" readonly value="">
            <label>${esc(TRADE_AC_LABEL[type])}</label>
            <select id="f-salesac">
              ${tradeList.map(t => `<option value="${t.id}"${t.id === tradeDefault ? ' selected' : ''}>${esc(t.name)}</option>`).join('')}
            </select>
          </div>
        </div>
        <div>
          <div class="vgrid wide">
            <label>${autoNo ? 'Challan No' : 'Invoice No'}</label>
            <div class="vno">
              <!-- A challan number is the book letter and the page: A 01 ..
                   Z 99, handed out in order. There is nothing to choose, so
                   it is shown rather than asked for and the Sr box goes. -->
              ${autoNo ? '' : `<input id="f-series" value="${esc(doc.series || '')}" placeholder="Sr">`}
              <input id="f-no" value="${esc(doc.invoice_no)}"
                ${(isNew && !autoNo) ? '' : 'readonly'}
                ${autoNo ? 'title="Taken automatically from the challan book" style="grid-column:1/-1"' : ''}>
            </div>
            <label>Invoice Type</label>
            <select id="f-itype">
              ${S.meta.lookups.invoice_types.map(t => `<option${(doc.invoice_type || 'Debit') === t ? ' selected' : ''}>${esc(t)}</option>`).join('')}
            </select>
            <label>Invoice Date</label>
            <input type="date" id="f-date" value="${esc(doc.invoice_date)}">
            <label>Doc type</label>
            <select id="f-type" ${isNew ? '' : 'disabled'}>
              ${Object.keys(VOUCHER_TITLE).map(k => `<option value="${k}"${type === k ? ' selected' : ''}>${esc(DOC_LABEL[k])}</option>`).join('')}
            </select>
          </div>
        </div>
      </div>

      <!-- Driver, terms, reference and tax go in the header's own two
           columns: the same label widths, the same box edges and the same
           divider, so every box on the top of the voucher lines up with the
           one above it. The order the Enter key walks them is unchanged. -->
      <div class="vtop vsub">
        <div>
          <div class="vgrid">
            <label>Driver Name</label><input id="f-driver" value="${esc(doc.driver_name || '')}">
            <label>Payment Terms</label><input id="f-terms" value="${esc(doc.payment_terms || '')}">
          </div>
        </div>
        <div>
          <div class="vgrid wide">
            ${noTax()
              // A challan has no reference of its own -- the order it came from
              // is already on it. The box is kept out of sight rather than
              // dropped, so an imported one keeps whatever SHREE wrote there.
              ? `<input id="f-ref" type="hidden" value="${esc(doc.ref_no || '')}">`
              : `<label>Reference</label><input id="f-ref" value="${esc(doc.ref_no || '')}">`}
            <label>Tax Type</label>
            ${noTax()
              ? '<input id="f-tax" type="hidden" value="0"><input readonly value="No tax (firm outside GST)">'
              : `<select id="f-tax">
                  <option value="auto">Auto (by state)</option>
                  <option value="0">CGST + SGST (local)</option>
                  <option value="1">IGST (inter-state)</option>
                </select>`}
            ${rcmBox ? `<label>Reverse Charge</label>
              <label class="tick">
                <input type="checkbox" id="f-rcm" ${doc.rcm ? 'checked' : ''}>
                Tax self-assessed, not paid to supplier</label>` : ''}
          </div>
        </div>
      </div>

      <!-- Inset by the same 14px as every other part of the voucher, so the
           grid's edges are the header's and the footer's edges. -->
      <div id="hsn-band" style="padding:0 14px"></div>
      <div id="grid" style="padding:0 14px 10px"></div>

      <div class="vfoot${noTax() ? ' lean' : ''}">
        <div>
          <div class="vgrid${noTax() ? '' : ' fill'}">
            <label>NOS Total</label>
            <input id="f-qtytotal" readonly value="0">
            <label>Quantity Total</label>
            <input id="f-wttotal" readonly value="0">
            ${noTax() ? `
            <input id="f-dept" type="hidden" value="${esc(doc.dept || '')}">
            <textarea id="f-remarks" hidden>${esc(doc.remarks || '')}</textarea>` : `
            <label>By Dept</label>
            <input id="f-dept" list="dept-list" value="${esc(doc.dept || '')}">
            <label style="align-self:start;padding-top:13px">Particulars</label>
            <textarea id="f-remarks">${esc(doc.remarks || '')}</textarea>`}
          </div>
        </div>
        <div id="totals"></div>
      </div>

      <div class="vactions">
        <button type="button" id="btn-shortcut">Shortcut Key</button>
        <div class="spacer"></div>
        <button class="primary" id="save">${isNew ? 'Save' : 'Update'}
          <span class="kbd">Ctrl+S</span></button>
        <button type="button" id="cancel">Cancel</button>
      </div>
    </div>

    ${/* Not on a purchase (3 Oct 2026: "Remove Transport & e-way bill option
         from new purchase") -- the supplier raises the e-way bill for goods
         coming in. Hidden, not taken out: saving reads these boxes, and a
         purchase from SHREE keeps whatever it carries in them. Not on a
         challan either (8 Oct 2026: "remove this from new challan page
         completely") -- the challan prints none of it, and the bill made
         from it is where the e-way bill is raised. */ ''}
    <div class="card"${isPurchase || noTax() ? ' hidden' : ''}><h2>Transport &amp; e-way bill</h2><div class="bd">
      <div class="grid g3">
        <div><label>Transporter</label><input id="f-trans" value="${esc(doc.transporter || '')}"></div>
        <div><label>Vehicle no</label><input id="f-veh" value="${esc(doc.vehicle_no || '')}"></div>
        <div><label>LR / docket no</label><input id="f-lr" value="${esc(doc.lr_no || '')}"></div>
        <div><label>LR date</label><input type="date" id="f-lrdate" value="${esc(doc.lr_date || '')}"></div>
        <div><label>Cases</label><input id="f-cases" value="${esc(doc.cases || '')}"></div>
        <div><label>E-way bill no</label><input id="f-eway" value="${esc(doc.eway_no || '')}"></div>
        <div><label>Transporter GSTIN</label>
          <input id="f-tgstin" style="font-family:var(--mono)" maxlength="15"
            placeholder="only if the transporter moves it"
            value="${esc(doc.transporter_gstin || '')}"></div>
        <div><label>Mode</label><select id="f-tmode">
          <option value="">—</option>
          ${EWAY_MODES.map(([c, n]) => `<option value="${c}"${String(doc.transport_mode || '') === c ? ' selected' : ''}>${esc(n)}</option>`).join('')}
        </select></div>
        <div><label>Distance (km)</label>
          <input id="f-tdist" type="number" min="0" step="1"
            placeholder="0 = portal works it out"
            value="${doc.transport_distance ? esc(String(num(doc.transport_distance))) : ''}"></div>
        <div><label>Vehicle type</label><select id="f-vtype">
          ${EWAY_VEHICLE_TYPES.map(([c, n]) => `<option value="${c}"${String(doc.vehicle_type || 'R') === c ? ' selected' : ''}>${esc(n)}</option>`).join('')}
        </select></div>
        <div><label>Reason for movement</label><select id="f-subsupply">
          ${EWAY_SUB_SUPPLY.map(([c, n]) => `<option value="${c}"${String(doc.sub_supply_type || '1') === c ? ' selected' : ''}>${esc(n)}</option>`).join('')}
        </select></div>
      </div>
      <div class="hint">These last five are only read when an e-way bill JSON
        is made. Leave them alone for a bill whose goods are not moving.</div>
    </div></div>`;

  /* --- party ----------------------------------------------------------- */
  let party = doc.party_id ? findParty(doc.party_id) : null;
  function showParty() {
    if (!party) {
      $('#f-address').value = '';
      $('#f-balance').value = '';
      return;
    }
    $('#f-address').value = [party.address1, party.address2, party.city,
      party.state, party.pin].filter(Boolean).join(', ');
    const b = num(party.balance);
    $('#f-balance').value = b
      ? fmt(Math.abs(b)) + (b > 0 ? ' Dr' : ' Cr') +
        (party.credit_limit > 0 && b > party.credit_limit ? '  (over limit)' : '')
      : '0.00';
    if (!$('#f-terms').value && party.credit_days) {
      $('#f-terms').value = party.credit_days + ' days';
    }
    grid.repaint();
  }
  const partyBox = $('#f-party');
  const takeParty = p => { party = p; partyBox.value = p.name; showParty(); lastRateOf(p); };
  const partyPicker = attachPicker(partyBox, {
    source: async term => {
      const all = await loadParties();
      return searchList(term,
        all.filter(p => (p.party_kind === partyKind || p.party_kind === 'both')),
        p => [p.name, p.city, p.gstin]);
    },
    render: p => `<b>${esc(p.name)}</b><small>${esc([p.city, p.state].filter(Boolean).join(', '))}
      ${p.gstin ? '· ' + esc(p.gstin) : ''} · bal ${fmt(Math.abs(num(p.balance)))}</small>`,
    onPick: takeParty
  });
  // Typing finds a party you can already name. An empty box opens the whole
  // book instead -- city, GSTIN and balance beside every row -- because that
  // is the state the box is in when a customer has still to be chosen. Once
  // one is named the box is walked past; the arrow button and F4 change it.
  const browseParty = async how => {
    partyPicker.close();
    const p = await chooseParty({ kind: partyKind, term: () => partyBox.value,
                                  typed: !!(how && how.typed) });
    if (p) { takeParty(p); moveOn(partyBox, view); } else partyBox.focus();
    return p;
  };
  $('[data-browse-party]').onclick = browseParty;
  opensPicker(partyBox, browseParty, partyPicker);
  if ($('#new-party')) $('#new-party').onclick = async () => {
    if (await editParty({ party_kind: partyKind })) {
      await loadParties(true);
      toast('Party added — pick it from the list.');
    }
  };

  /* --- tax mode --------------------------------------------------------- */
  if (!noTax()) {
    $('#f-tax').value = doc.is_igst === null || doc.is_igst === undefined
      ? 'auto' : String(doc.is_igst);
  }
  function igstFlag() {
    if (noTax()) return false;
    const v = $('#f-tax').value;
    if (v !== 'auto') return v === '1';
    // The GSTIN's own state first, as the server and the e-invoice read it.
    const home = Gst.stateOf(S.meta.company);
    const away = party ? Gst.stateOf(party) : '';
    return !!(home && away && home !== away);
  }
  if (!noTax()) $('#f-tax').onchange = () => grid.repaint();
  if ($('#f-rcm')) $('#f-rcm').onchange = () => grid.repaint();

  /* The totals block is built once. Repainting only writes numbers into the
     output slots -- rebuilding it would blow away the charge boxes mid-typing
     and you would lose focus after every character.
     Declared before the grid, which paints as soon as it is constructed. */
  let totalsReady = false;

  function charge(id) { return num($('#' + id) ? $('#' + id).value : 0); }

  /* --- item grid + totals ---------------------------------------------- */
  // The order screen's grid, on every document: roomy boxes and the note
  // under the item name rather than fighting for a column of its own. Unit
  // and Disc % come off both -- no line in either firm has ever carried one,
  // and the unit a picked item does bring still travels to the print and the
  // GST summary without a column to type it in.
  //
  // HSN is the exception: it is the code the line is taxed under and it is
  // printed on the face of the bill, so on a tax invoice it gets a column of
  // its own -- filled from the product, and open to correct by hand. The
  // challan is outside GST and has no use for it.
  //
  // Total is dropped only where it repeats the Amount beside it: with no tax
  // and no discount that is the challan. On a bill it carries the tax, so it
  // says something the Amount column does not and stays.
  // A document already saved shows the figures it holds until someone changes
  // something; from then on it is being re-made and everything is worked out
  // again. `edited` is what tells the two apart.
  let edited = false;
  // The form as it was opened -- see paintPrints. Set once it is all drawn.
  let opened = null;
  view.addEventListener('input', () => { edited = true; }, true);
  view.addEventListener('change', () => { edited = true; }, true);

  /** What the portal would throw the bill back for, said on the bill itself.
   *
   *  A bill raised from a challan carries the challan's lines, and those name
   *  the order firm's one generic product -- no HSN, because that book never
   *  had to have one. This is where that is put right, and it is offered
   *  rather than forced: the goods are the same goods, and only the person
   *  who cut them knows which grade they were.
   */
  function paintHsnBand() {
    const host = $('#hsn-band');
    if (!host) return;
    const named = lines.filter(l => (l.description || '').trim());
    const bad = named.filter(l => !hsnOk(l.hsn));
    if (noTax() || !bad.length) { host.innerHTML = ''; return; }
    host.innerHTML = `
      <div class="hsn-band">
        <span class="tag warn">no HSN</span>
        <div><b>${bad.length} line${bad.length > 1 ? 's' : ''}</b> ${
          doc.challan_no
            ? 'came across from challan ' + esc(doc.challan_no) + ' without an HSN'
            : 'have no HSN'}. The portal refuses a line without one${
          doc.challan_no
            ? ', and the challan book keeps only the one generic product' : ''}.</div>
        <button type="button" class="sm primary" id="pick-products">Pick products</button>
      </div>`;
    $('#pick-products').onclick = async () => {
      const n = await pickProducts(lines, { mode: type, taxFree: noTax(),
                                            title: 'Pick the product for each line' });
      if (!n) return;
      edited = true;
      // The names and the codes on the rows themselves changed, so the grid
      // is rebuilt rather than merely recomputed. That rebuild paints the
      // totals and this band again on its way out.
      grid.redraw();
      ok(n + ' line(s) set. Check the rates before saving.');
    };
  }

  /* --- the rate off the customer's last challan -------------------------
     Asked on 23 Sep 2026: "whenever I make a new challan for any existing
     customer, the price per kilo in the last challan should automatically
     be fetched and autofilled." Choosing the customer on a new challan asks
     for their last one that was priced by the kilo (`/api/last-rates`), and
     every line with no rate of its own is given that price -- lines added
     afterwards too. A rate typed in is never written over; the offered ones
     are tinted and say which challan they came from, and the tag at the top
     links to it.

     A challan can carry two prices, one a thickness -- N.B.S. at ₹75 for
     0.40 and ₹70 for 0.60 on the same sheet -- and the thickness is what
     the item detail starts with. So a line whose item detail starts with a
     thickness the last challan also had takes that line's price; any other
     line takes the price the most kilos went out at. */
  const autoRate = isNew && noTax() && type === 'SALE';
  let lastRate = null;                 // /api/last-rates, when it found one
  let rateAsked = 0;
  const THICK = /^\s*(\d+\.\d+)/;
  const thickOf = t => { const m = THICK.exec(t || ''); return m ? parseFloat(m[1]) : null; };
  const rateText = r => String(Math.round(num(r) * 1000) / 1000);
  function mainRate(ls) {
    const kg = new Map();
    ls.forEach(x => kg.set(num(x.rate), (kg.get(num(x.rate)) || 0) + num(x.weight)));
    let best = null, most = -1;
    ls.forEach(x => {                   // a tie goes to the later line
      const k = kg.get(num(x.rate));
      if (k >= most) { most = k; best = num(x.rate); }
    });
    return best;
  }
  // The thickness of a line: the steel chosen on it (Challan Stock), else
  // the figure at the front of its item detail, the way SHREE wrote it.
  const thickLine = x => (x.steel_thick ? thickOf(x.steel_thick) : thickOf(x.item_detail));
  function rateFor(l) {
    if (!lastRate || !lastRate.lines.length) return null;
    const t = thickLine(l);
    const same = t === null ? [] : lastRate.lines.filter(x => thickLine(x) === t);
    return same.length ? num(same[same.length - 1].rate) : mainRate(lastRate.lines);
  }
  function rateNote() {
    return lastRate ? 'Rate from challan ' + docNo(lastRate) + ' of '
      + dt(lastRate.invoice_date) + ' — type over it to change' : '';
  }
  function paintLastRate() {
    const tag = $('#last-rate');
    if (!tag) return;
    if (!lastRate) { tag.style.display = 'none'; tag.innerHTML = ''; return; }
    const rates = [...new Set(lastRate.lines.map(x => num(x.rate)))];
    tag.innerHTML = 'rate from last challan '
      + goLink(docLink('SALE', lastRate.id, lastRate.company_id), esc(docNo(lastRate)),
               'Open that challan')
      + ' · ' + esc(dt(lastRate.invoice_date)) + ' · ₹'
      + rates.map(rateText).join(' / ₹') + '/kg' + (rates.length > 1 ? ' by thickness' : '');
    tag.style.display = '';
  }
  async function lastRateOf(p) {
    if (!autoRate) return;
    const mine = ++rateAsked;
    let got = null;
    try {
      got = p && p.id ? await api.get('/api/last-rates', { party_id: p.id, doc_type: 'SALE' }) : null;
    } catch (e) { got = null; }
    if (mine !== rateAsked || !here()) return;     // another customer since, or another screen
    lastRate = got && got.found ? got : null;
    paintLastRate();
    grid.suggestRates();
  }

  // Challan Stock (26 Sep 2026): a challan in BST names the steel each line
  // takes out of the yard, and shows what is in stock as it is typed.
  let steel = null;
  if (noTax() && type === 'SALE') {
    try {
      const st = await api.get('/api/steel/stock', { as_on: '9999-12-31' });
      const stock = {};
      st.rows.forEach(r => {
        stock[[r.steel_type, r.steel_quality, r.steel_thick].join('|')] = num(r.balance);
      });
      steel = { lists: st.lists, stock, saved: isNew ? [] : (doc.lines || []) };
    } catch (e) { steel = null; }
    if (!here()) return;
  }

  const grid = LineGrid($('#grid'), lines, t => { paintTotals(t); paintHsnBand(); paintPrints(); },
    Object.assign(
      // Thickness and cutting size are left off the challan and the bill
      // (21 Sep 2026) -- the order screen still takes them. The line keeps
      // whatever it already carries, since saving reads the line, not the box.
      { isIgst: igstFlag, mode: type, stackNote: true, fixedGst: true,
        centred: true,
        suggestRate: autoRate ? rateFor : null, rateNote, steel,
        hide: ['unit', 'discount_pct', 'thickness', 'cutting_size'],
        newItem: async () => {
          const it = type === 'PURCHASE' ? await newProduct('') : await editItem();
          if (it) await loadItems(true);
          return it;
        },
        // What this document has already taken out of stock (or bought into
        // it). The grid puts that back before counting the lines on screen,
        // so reopening a saved bill does not show its goods leaving twice.
        stockSaved: isNew ? [] : (doc.lines || []) },
      // The challan has few enough columns for the order screen's wide boxes,
      // and with no tax and no discount its Total only repeats the Amount. The
      // bill keeps both: Total carries the tax there, and the wide boxes would
      // squeeze the item name out of a grid this many columns across.
      noTax() ? { roomy: true, hide: ['hsn', 'unit', 'discount_pct', 'total',
                                      'thickness', 'cutting_size'] }
              : {}));

  // A challan raised from an order arrives with its customer already named.
  if (autoRate && party) lastRateOf(party);

  function buildTotals() {
    const money2 = v => (num(v) ? num(v) : '');
    $('#totals').innerHTML = `
      <div class="totrow"><span>Sub Total</span><span></span>
        <div class="out" data-o="goods">0.00</div></div>
      <div class="totrow tax" data-row="discount"><span>Less Discount</span><span></span>
        <div class="out" data-o="discount">0.00</div></div>
      <div class="totrow"><span>Cutting</span>
        <input id="c-cutting" class="num" value="${money2(doc.cutting)}" placeholder="0.00">
        <div class="out" data-o="cutting">0.00</div></div>
      <div class="totrow"><span>Transport</span>
        <input id="c-transport" class="num" value="${money2(doc.transport)}" placeholder="0.00">
        <div class="out" data-o="transport">0.00</div></div>
      <div class="totrow"><span>Loading</span>
        <input id="c-loading" class="num" value="${money2(doc.loading)}" placeholder="0.00">
        <div class="out" data-o="loading">0.00</div></div>
      <div class="totrow tax" data-row="adj"><span>Round Off (challan)</span><span></span>
        <div class="out" data-o="adj">0.00</div></div>
      <div class="totrow" data-row="taxable"><span><b>Taxable Value</b></span><span></span>
        <div class="out" data-o="taxable"><b>0.00</b></div></div>
      <div class="totrow tax" data-row="igst"><span>IGST</span><span></span>
        <div class="out" data-o="igst">0.00</div></div>
      <div class="totrow tax" data-row="cgst"><span>CGST</span><span></span>
        <div class="out" data-o="cgst">0.00</div></div>
      <div class="totrow tax" data-row="sgst"><span>SGST</span><span></span>
        <div class="out" data-o="sgst">0.00</div></div>
      <div class="totrow tax" data-row="cess"><span>Cess</span><span></span>
        <div class="out" data-o="cess">0.00</div></div>
      ${/* Tax collected at source, typed off the supplier's bill (8 Oct 2026:
           "add a TCS option which will be entered by the user"). A purchase
           only; added after the GST and not taxed. */ ''}
      ${isPurchase ? `<div class="totrow"><span>TCS</span>
        <input id="c-tcs" class="num" value="${money2(doc.tcs)}" placeholder="0.00"
          title="Tax collected at source, as the supplier's bill shows it. Added after GST.">
        <div class="out" data-o="tcs">0.00</div></div>` : ''}
      <div class="totrow"><span>Round Off</span>
        <!-- Nothing is rounded by itself (2 Oct 2026): a Round Off is only
             what is typed here. A saved document shows the one it holds --
             SHREE rounded every bill, and a few hundred imported documents
             carry lakhs here that the old screen had no column for -- so the
             screen shows the total the ledgers hold, and saving it again
             keeps it. Clear the box to take it off. -->
        <input id="c-round" class="num" placeholder="0.00"
          value="${doc.id && Math.abs(num(doc.round_off)) >= 0.005
            ? money2(doc.round_off) : ''}">
        <div class="out" data-o="roundoff">0.00</div></div>
      <div class="totrow grand"><span>Grand Total</span><span></span>
        <div class="out" data-o="grand">0.00</div></div>
      <div class="totrow tax" data-row="rcm" style="color:var(--warn)">
        <span>Payable to supplier</span><span></span>
        <div class="out" data-o="payable" style="color:var(--warn)">0.00</div></div>
      <div data-row="challan" style="font-size:11.5px;margin-top:8px;text-align:right;
        color:var(--muted)" data-o="challan"></div>
      <div data-row="words" style="font-size:11.5px;color:var(--muted);margin-top:8px;
        text-align:right" data-o="words"></div>`;

    ['c-cutting', 'c-transport', 'c-loading', 'c-tcs', 'c-round'].forEach(id => {
      const inp = $('#' + id);
      if (!inp) return;
      inp.addEventListener('input', () => grid.repaint());
      // Recalculate on Enter, but do not swallow it -- typing and blur already
      // keep the totals honest, and claiming Enter here stopped the walk down
      // the screen dead on the first charge box.
      inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') grid.repaint();
      });
      // Typing a charge and tabbing straight to Save should still count.
      inp.addEventListener('blur', () => grid.repaint());
    });
    totalsReady = true;
  }

  function paintTotals(t) {
    if (!totalsReady) buildTotals();
    const out = k => $('#totals [data-o="' + k + '"]');
    const row = k => $('#totals [data-row="' + k + '"]');
    const show = (k, on) => { row(k).style.display = on ? '' : 'none'; };

    const igst = igstFlag();
    const rcm = $('#f-rcm') && $('#f-rcm').checked;
    const cutting = charge('c-cutting');
    const transport = charge('c-transport');
    const loading = charge('c-loading');
    const tcs = Gst.money(charge('c-tcs'));
    const roundBox = $('#c-round').value.trim();

    // The whole bill by Gst.bill -- the server's gst.bill, SHREE's way:
    // charges and the challan's round off shared over the lines by value and
    // taxed with them, CGST and SGST each the half rate on the bill, IGST the
    // whole rate, the total to the paisa plus whatever Round Off is typed --
    // nothing rounded by itself.
    const goods = t.taxable;
    const adj = noTax() ? 0 : Gst.money(doc.taxable_adj || 0);
    const chargeTotal = Gst.money(cutting + transport + loading);
    const worked = Gst.bill(t.rows || [], {
      noTax: noTax(), charges: chargeTotal, adj, igst, tcs,
      roundOff: roundBox === '' ? null : roundBox });
    let tax = worked.tax;
    let cess = worked.cess;
    // Thirteen years of imported documents do not all agree with a fresh
    // working out of their own tax: a firm outside GST still holds VAT-era
    // challans and purchases that arrived taxed, the lines of a pre-GST bill
    // carry no rate to work anything out from, and a few of SHREE's own
    // bills slipped (GT 125 of July 2017 taxed 68 paise the line does not
    // show). Where a saved document has not been touched, what it holds is
    // what it is worth, and that is what is shown -- so the screen and the
    // books never differ. Change anything and it is worked out afresh.
    const held = Gst.money(num(doc.cgst) + num(doc.sgst) + num(doc.igst)
                           + num(doc.cess));
    const asSaved = !!doc.id && !edited && Math.abs(held - tax) > 0.005;
    if (asSaved) {
      tax = held;
      cess = Gst.money(num(doc.cess));
    }
    const taxableValue = worked.taxable;
    const raw = asSaved
      ? Gst.money(taxableValue + tax + (noTax() ? chargeTotal : 0) + tcs)
      : worked.raw;
    const grand = asSaved ? Gst.money(raw + num(roundBox)) : worked.grand_total;
    const roundOff = Gst.money(grand - raw);
    const payable = rcm ? Gst.money(grand - tax) : grand;
    const outIgst = asSaved ? num(doc.igst) : worked.igst;
    const outCgst = asSaved ? num(doc.cgst) : worked.cgst;
    const outSgst = asSaved ? num(doc.sgst) : worked.sgst;

    $('#f-qtytotal').value = qty(t.units !== undefined ? t.units
      : lines.reduce((a, l) => a + num(l.qty), 0));
    $('#f-wttotal').value = qty(t.weight || 0);

    out('goods').textContent = fmt(goods);
    out('adj').textContent = fmt(adj);
    out('taxable').innerHTML = '<b>' + fmt(taxableValue) + '</b>';
    out('discount').textContent = fmt(t.discount);
    out('igst').textContent = fmt(outIgst);
    out('cgst').textContent = fmt(outCgst);
    out('sgst').textContent = fmt(outSgst);
    out('cess').textContent = fmt(cess);
    out('cutting').textContent = fmt(cutting);
    out('transport').textContent = fmt(transport);
    out('roundoff').textContent = fmt(roundOff);
    out('loading').textContent = fmt(loading);
    if (out('tcs')) out('tcs').textContent = fmt(tcs);
    out('grand').textContent = fmt(grand);
    out('payable').textContent = fmt(payable);
    out('words').textContent = words(grand);

    // When a bill comes from a challan, say plainly whether the taxable value
    // landed on the challan total, and by how much the paise-rounded rate
    // moved it if not.
    if (num(doc.challan_total)) {
      const drift = r2js(taxableValue - num(doc.challan_total));
      out('challan').innerHTML =
        'Challan total <b>' + fmt(doc.challan_total) + '</b> → taxable <b>' +
        fmt(taxableValue) + '</b> + GST' +
        (Math.abs(drift) < 0.005 ? ''
          : ' <span style="color:var(--warn)">(differs by ' + fmt(drift) +
            ' — a line was changed)</span>');
    }
    show('challan', !!num(doc.challan_total));

    // A firm set to No tax still holds documents from before that was true --
    // VAT-era challans, and purchases that came in with GST on them. The
    // setting decides whether a NEW document charges tax; it must never hide
    // tax a document already carries, or the totals stop adding up on screen.
    const carriesTax = !noTax() || Math.abs(tax) > 0.005;
    const asIgst = asSaved ? Math.abs(outIgst) > 0.005 : igst;
    show('discount', !!t.discount);
    show('adj', !noTax() && !!adj);
    show('taxable', !noTax() && !!(chargeTotal || adj));
    show('igst', carriesTax && asIgst);
    show('cgst', carriesTax && !asIgst);
    show('sgst', carriesTax && !asIgst);
    show('cess', !!cess);
    show('rcm', !!rcm);
    centreTotals();

    view._totals = { cutting, transport, loading, tcs, roundOff, roundBox, grand };
  }
  /** The figures down the totals column, centred as one block: right-aligned
   *  among themselves so the decimal points stack from Sub Total to Round
   *  Off, and the block in the middle of the boxes -- as the grid's Amount
   *  and Total are. Only the rows on show are measured. Grand Total, set
   *  larger, is centred in its own box by the stylesheet. */
  function centreTotals() {
    const outs = $$('#totals .totrow:not(.grand) .out').filter(o => o.offsetParent);
    const r = document.createRange();
    let widest = 0;
    outs.forEach(o => {
      r.selectNodeContents(o);
      widest = Math.max(widest, r.getBoundingClientRect().width);
    });
    outs.forEach(o => {
      const w = o.getBoundingClientRect().width;
      o.style.paddingRight = Math.max(9, (w - widest) / 2) + 'px';
    });
  }
  // To the paisa exactly as the server rounds (Gst.money): half away from
  // nought on the figure as written. Scaling by 100 first made 1.005 read as
  // 100.4999... and round down while the saved bill had 1.01.
  function r2js(v) { return Gst.money(v); }

  showParty();

  $('#f-type').onchange = () => {
    location.hash = '#/invoice/new?type=' + $('#f-type').value;
  };
  // The number belongs to a run of bills and to a financial year, so either
  // box changing means asking for it again. A number typed by hand is left
  // alone -- only the one the screen offered is replaced.
  let offered = doc.invoice_no;
  async function refreshNo() {
    if (!isNew) return;
    const box = $('#f-no'), sr = $('#f-series');
    if (box.value.trim() && box.value.trim() !== offered) return;
    const here = onScreen();
    const got = await api.get('/api/next-no', {
      key: type, fy: fyLabel($('#f-date').value),
      series: sr ? sr.value.trim() : ''
    });
    if (!here()) return;
    offered = got.number;
    box.value = got.number;
  }
  $('#f-date').onchange = refreshNo;
  if ($('#f-series')) $('#f-series').onchange = refreshNo;
  // One Enter walk for the whole screen: header, every line, the footer and
  // the charge boxes beside it, and on into the transport card below. That
  // card sits outside .voucher, so scoping the walk to .voucher stopped it
  // dead at Round Off and left the vehicle, the LR and the e-way boxes --
  // the ones a lorry waits on -- reachable by Tab alone.
  enterMovesOn(view);

  // A bill raised from a challan arrives complete -- the lines, the weights,
  // the rates, the charges and the GST all came across, and there is nothing
  // left to type. So the cursor starts on Save and one Enter posts it. Any
  // box is still there to be clicked into if something does need changing;
  // stepping into one puts Enter back to walking the screen.
  // Raised from a customer's account (4 Oct 2026): that customer, already
  // picked -- terms, balance and the last rate follow as if chosen here.
  if (isNew && !doc.party_id && q.party) {
    const p = findParty(q.party);
    if (p && (p.party_kind === partyKind || p.party_kind === 'both')) takeParty(p);
  }
  if (isNew && doc.from_challan) {
    const btn = $('#save', view);
    if (btn) {
      // preventScroll, or focusing the action bar drags the page down to it
      // and the bill being posted is off the top of the screen.
      btn.focus({ preventScroll: true });
      window.scrollTo(0, 0);
      // Claim Enter on the button rather than leaving it to the browser's own
      // activation, which does not fire reliably here.
      btn.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.ctrlKey && !e.metaKey) {
          e.preventDefault();
          save();
        }
      });
      const hint = el('<span class="tag ok" style="margin-right:8px">'
        + 'Everything came from the challan — press Enter to save</span>');
      btn.parentNode.insertBefore(hint, btn);
    }
  }
  $('#btn-shortcut').onclick = shortcutHelp;
  $('#cancel').onclick = async () => {
    // The same question Escape asks, for the same reason -- Cancel used to
    // throw a half-typed voucher away without a word.
    // Nothing was written, so this is a step back out of the screen -- to
    // the list that was being read, or to whatever else opened it.
    const out = () => {
      if (isNew) leave(listFor(type));
      else { location.hash = '#/invoice/' + doc.id; render(); }
    };
    if (KB.unsavedWork()) await leaveAsking('Leave it and go back?', 'Save and go back', out);
    else out();
  };

  /* --- print what is saved ------------------------------------------------
     The form as it would be sent is compared with the form as it was opened:
     the same, and the saved document is what the screen shows, so it prints
     as it is; anything different -- a box typed in, a line added or taken
     off -- and Print saves first. A new document always saves first. */
  function pending() {
    return isNew || opened === null || JSON.stringify(formPayload()) !== opened;
  }
  function paintPrints() {
    if (!isNew && opened === null) return;      // still being drawn
    const now = pending();
    $$('#page-actions button.doc-print').forEach((b, i) => {
      const want = printLabel(PRINTS[i][1], now);
      if (b.textContent !== want) b.textContent = want;
    });
  }
  async function printDoc(format) {
    if (!pending()) { printInvoice(doc.id, format); return; }
    await save(saved => printInvoice(saved.id, format));
  }

  /* --- save ------------------------------------------------------------- */
  function formPayload() {
    const taxSel = $('#f-tax').value;
    const t = view._totals || {};
    return {
      doc_type: type,
      series: $('#f-series') ? $('#f-series').value.trim() : '',
      from_challan: doc.from_challan || null,
      invoice_no: $('#f-no').value.trim(),
      invoice_type: $('#f-itype').value,
      invoice_date: $('#f-date').value, party_id: party ? party.id : null,
      sales_ac_id: $('#f-salesac').value || null,
      order_id: doc.order_id || null, ref_no: $('#f-ref').value.trim(),
      is_igst: taxSel === 'auto' ? null : (taxSel === '1' ? 1 : 0),
      rcm: $('#f-rcm') && $('#f-rcm').checked ? 1 : 0,
      cutting: t.cutting || 0, transport: t.transport || 0,
      loading: t.loading || 0, tcs: t.tcs || 0,
      taxable_adj: doc.taxable_adj || 0,
      round_off: (t.roundBox === '' || t.roundBox === undefined) ? null : t.roundOff,
      driver_name: $('#f-driver').value.trim(),
      payment_terms: $('#f-terms').value.trim(),
      dept: $('#f-dept').value.trim(),
      transporter: $('#f-trans').value, vehicle_no: $('#f-veh').value,
      lr_no: $('#f-lr').value, lr_date: $('#f-lrdate').value,
      cases: $('#f-cases').value, eway_no: $('#f-eway').value,
      transporter_gstin: $('#f-tgstin').value.trim().toUpperCase(),
      transport_mode: $('#f-tmode').value,
      transport_distance: $('#f-tdist').value,
      vehicle_type: $('#f-vtype').value,
      sub_supply_type: $('#f-subsupply').value,
      remarks: $('#f-remarks').value,
      lines: lines.filter(l => l.description.trim() || num(l.qty) || num(l.weight))
    };
  }
  /** Save the form. `after`, if given, is called with the saved document
   *  once it has been drawn again -- how Print prints only what is saved.
   *  Returns the saved document, or nothing when the save did not happen. */
  async function save(after) {
    if (!party) {
      bad('Select a party in the "To :" box first.' + (after ? ' Nothing was printed.' : ''));
      $('#f-party').focus();
      return;
    }
    // A new challan's steel is what keeps Challan Stock true. A line with its
    // kilos but no steel is allowed -- it just moves no stock -- so it is
    // asked about once rather than refused.
    if (steel && isNew) {
      const none = lines.filter(l => num(l.weight) > 0
        && !l.steel_type && !l.steel_quality && !l.steel_thick);
      if (none.length && !await confirmBox('No steel picked',
          none.length + ' line' + (none.length === 1 ? ' is' : 's are')
          + ' not a steel picked from the list in the product box, so '
          + qty(none.reduce((s, l) => s + num(l.weight), 0))
          + ' kg will not come out of Challan Stock. Save anyway?')) return;
    }
    const payload = formPayload();
    try {
      const saved = isNew ? await api.post('/api/invoices', payload)
        : await api.put('/api/invoices/' + doc.id, payload);
      ok((noTax() ? 'Challan ' : DOC_LABEL[type] + ' ') + docNo(saved)
        + ' saved.');
      // A purchase goes straight into Billing Stock (27 Sep 2026), so say
      // what it put there -- and name any product that was typed rather
      // than picked, which the save has just added to the firm's products.
      if (type === 'PURCHASE' && !noTax()) {
        const ls = (saved.lines || []).filter(l => l.item_id);
        const kg = ls.every(l => num(l.weight) > 0)
          ? ls.reduce((a, l) => a + num(l.weight), 0) : 0;
        if (ls.length) {
          toast('Billing Stock: ' + (kg ? qty(kg) + ' kg' : 'the goods')
            + ' added to ' + (S.meta.company.code || S.meta.company.name) + '.', 'ok');
        }
        // Only a line with no product name at all stays out of stock.
        if (ls.length < (saved.lines || []).length) {
          bad('A line with no product name did not go into Billing Stock — '
            + 'name its product and save again.');
        }
        if ((saved.new_items || []).length) {
          toast('New in the product list, and in stock: '
            + saved.new_items.join(', ') + '.', 'ok');
        }
      }
      // Editing a billed challan rewrites its bill in the other firm. Say so
      // and say what it now comes to -- that bill is somebody's tax invoice,
      // and it changing is not something to find out about later.
      const again = saved.resynced_bill;
      if (again) {
        toast('Bill ' + again.number + ' in ' + again.firm + ' updated to '
          + fmt(again.grand_total) + '.');
      } else if (saved.resync_error) {
        bad('Saved, but its bill could not be updated: ' + saved.resync_error);
      }
      invalidate();
      await showSaved('#/invoice/' + saved.id, isNew);
      if (after) after(saved);
      return saved;
    } catch (e) {
      bad(e.message + (after ? ' Nothing was printed — it prints once it is saved.' : ''));
    }
  }
  // Ctrl+S is the keyboard module's, for every screen: it presses #save.
  // It used to be a listener added to #view here -- and #view outlives the
  // screen, so every bill opened in a day kept its own save() listening, and
  // one Ctrl+S saved the bill on screen *and* re-saved each bill opened
  // before it with this one's header (found 22 Sep 2026).
  $('#save').onclick = () => save();
  // Only if the screen is still up: left inside those 60 ms, the box was
  // gone and the focus threw.
  if (isNew && !party) setTimeout(() => { const b = $('#f-party'); if (b) b.focus(); }, 60);
  // The form as opened, for Print to tell a saved document from a changed
  // one. A box typed in, a list picked from or a line taken off says so on
  // the print buttons as it happens.
  if (!isNew) opened = JSON.stringify(formPayload());
  // On the screen's own parts, not on #view: #view outlives the screen, and
  // every bill opened in a day would have kept one listening.
  const later = () => setTimeout(() => { if (here()) paintPrints(); }, 0);
  Array.from(view.children).forEach(part => {
    part.addEventListener('input', later, true);
    part.addEventListener('change', later, true);
  });
});

/* --------------------------------------------------------------- listing */
route(/^\/invoices$/, async (view, _m, q) => {
  const type = (q.type || 'SALE').toUpperCase();
  // A sale in the no-tax firm is a challan, and that is what the menu entry
  // that lands here calls it. Everything else keeps its own name. The heading
  // and the Type box read off the same rule so they cannot disagree.
  const label = k => (k === 'SALE' && noTax()) ? 'Challan' : DOC_LABEL[k];
  // The heading names the kind of document being listed, so it is redrawn
  // whenever the Type box changes rather than only when the screen is built.
  const head = () => {
    const k = $('#ftype') ? $('#ftype').value : type;
    setTitle(label(k) + 's', 'All saved documents in ' + S.meta.company.name, [
      {
        label: '⤓ CSV', onClick: () => location.href = firmUrl('/api/export/register?' +
          new URLSearchParams({ doc_type: k, from: $('#f1').value, to: $('#f2').value }))
      },
      {
        label: '＋ New', cls: 'primary',
        onClick: () => location.hash = '#/invoice/new?type=' + k
      }
    ]);
  };
  head();
  view.innerHTML = `
    <div class="card"><div class="bd"><div class="row">
      <div><label>Type</label><select id="ftype" style="width:calc(160px * var(--typescale))">
        ${Object.keys(DOC_LABEL).map(k => `<option value="${k}"${type === k ? ' selected' : ''}>${label(k)}</option>`).join('')}
      </select></div>
      ${fySelect('fyq')}
      <div><label>From</label><input type="date" id="f1" value="${fyStart()}"></div>
      <div><label>To</label><input type="date" id="f2" value="${today()}"></div>
      <div style="flex:1;min-width:200px"><label>Search</label>
        <input id="q" placeholder="Number or party…"></div>
    </div></div></div>
    <div class="card"><div class="bd flush tbl-scroll" id="list"></div></div>`;

  const mem = screenState('/invoices', {
    // Carried, not shown: the Challans menu entry lands here through the
    // order firm, and coming back to this screen has to land there too.
    firm: { get: () => q.firm || '', set: () => {} },
    type: '#ftype', from: '#f1', to: '#f2', search: '#q',
    fy: fyField('f1', 'f2')
  }).restore(q);

  async function load() {
    mem.save();
    const here = onSearch();
    const rows = await api.get('/api/invoices', {
      doc_type: $('#ftype').value, from: $('#f1').value, to: $('#f2').value,
      search: $('#q').value.trim()
    });
    if (!here()) return;
    const tot = rows.reduce((a, r) => a + num(r.grand_total), 0);
    const gst = !noTax();
    const k = $('#ftype').value;
    const one = label(k) === 'Tax Invoice' ? 'bill' : label(k).toLowerCase()
      .replace(' bill', '');
    const total = {};
    rows.forEach(r => { total[r.id] = r.grand_total; });
    const ticks = () => bulkRows($('#list'), 'invoice', {
      noun: [one, one + 's'], reload: load,
      amount: tr => total[tr.dataset.id]
    });
    if (!rows.length) {
      $('#list').innerHTML = '<div class="empty"><b>Nothing here yet</b>No documents match this filter.</div>';
      ticks();
      return;
    }
    const bind = tr => {
      tr._challan = () => printInvoice(num(tr.dataset.id), 'challan');   // Alt+P
      tr.onclick = e => {
        if (e.target.closest('[data-bill]')) { billChallan(num(tr.dataset.id)); return; }
        if (e.target.hasAttribute('data-print')) { printInvoice(num(tr.dataset.id)); return; }
        if (e.target.hasAttribute('data-challan')) {
          printInvoice(num(tr.dataset.id), 'challan');
          return;
        }
        if (e.target.hasAttribute('data-del')) {
          const no = $('b', tr) ? $('b', tr).textContent : 'this document';
          deleteInvoice(num(tr.dataset.id), no, load);
          return;
        }
        location.hash = '#/invoice/' + tr.dataset.id;
      };
    };
    await fillRows($('#list'), `<table><thead><tr>
      <th>Date</th><th>Number</th><th>Party</th><th>GSTIN</th>
      ${gst ? '<th class="c">GST</th>' : ''}
      <th class="r">Taxable</th>${gst ? '<th class="r">Tax</th>' : ''}
      <th class="r">Total</th><th class="r">Actions</th>
      </tr></thead><tbody>`, rows.map(r => `<tr class="clickable" data-id="${r.id}">
        <td class="nw">${dt(r.invoice_date)}</td><td><b>${esc(docNo(r))}</b></td>
        <td>${goLink(accountLink(r.party_id), esc(r.party_name), 'Open the account')}<div style="font-size:11.5px;color:var(--muted)">${esc(r.party_city || '')}</div></td>
        <td style="font-family:var(--mono);font-size:11.5px">${esc(r.party_gstin || '—')}</td>
        ${gst ? '<td class="c"><span class="tag ' + (r.is_igst ? 'warn' : 'info') + '">' +
            (r.is_igst ? 'IGST' : 'CGST/SGST') + '</span></td>' : ''}
        <td class="r num">${fmt(r.taxable)}</td>
        ${gst ? `<td class="r num">${fmt(num(r.cgst) + num(r.sgst) + num(r.igst) + num(r.cess))}</td>` : ''}
        <td class="r num"><b>${fmt(r.grand_total)}</b></td>
        <td class="r nw">
          ${noTax() && r.doc_type === 'SALE' && !r.billed_invoice_no
            ? '<button class="link" data-bill><b>Bill it</b></button>' : ''}
          ${r.billed_invoice_no
            ? '<span class="tag ok">' + goLink(docLink('SALE', r.billed_invoice, r.billed_company),
                esc(r.billed_invoice_no) + (r.billed_more ? ' +' + r.billed_more : ''),
                r.billed_more ? 'Billed in ' + (r.billed_more + 1) + ' parts — open the first bill'
                  : 'Already billed — open the bill') + '</span>' : ''}
          <button class="link" data-print>Print</button>
          ${noTax() ? '' : '<button class="link" data-challan>Challan</button>'}
          <button class="link" data-del style="color:var(--bad)">Delete</button></td>
      </tr>`), `</tbody><tfoot><tr><td colspan="${gst ? 7 : 5}">${rows.length} document(s)</td>
        <td class="r num">${fmt(tot)}</td><td></td></tr></tfoot></table>`, {
        here,
        each: trs => trs.filter(tr => tr.dataset.id).forEach(bind),
        first: ticks
      });
  }
  $('#ftype').onchange = () => { head(); load(); };
  bindFy(load, 'f1', 'f2');
  ['#f1', '#f2'].forEach(s => $(s).onchange = load);
  $('#q').oninput = () => { clearTimeout(window._iv); window._iv = setTimeout(load, 160); };
  load();
});

/* ============================================================== printing
   Two layouts, matching the samples: the A4 tax invoice for the GST firms and
   the landscape two-up rough challan for the order firm.                    */

function shortDate(v) {                       // 21/08/2026
  const p = String(v || '').split('-');
  return p.length === 3 ? p[2] + '/' + p[1] + '/' + p[0] : '';
}
function shortDate2(v) {                      // 21/08/26
  const p = String(v || '').split('-');
  return p.length === 3 ? p[2] + '/' + p[1] + '/' + p[0].slice(2) : '';
}
/** Rate is carried to paise but printed to three places, as on the samples. */
function rate3(v) { return fmt(v, 3); }

/** The challan prints running balances without thousands separators. */
function plain(v) { return num(v).toFixed(2); }

/** "Forty six thousand ... only." -- sentence case, as the samples print it. */
function amountWords(v) {
  const w = words(v).replace(/ Only$/, ' only.');
  return w.charAt(0) + w.slice(1).toLowerCase();
}

function pageStyle(landscape) {
  let el = document.getElementById('page-size');
  if (!el) {
    el = document.createElement('style');
    el.id = 'page-size';
    document.head.appendChild(el);
  }
  el.textContent = '@page{size:A4 ' + (landscape ? 'landscape' : 'portrait') +
    ';margin:' + (landscape ? '6mm' : '8mm') + '}';
}

/** Load a PDF off-screen and raise the print dialog on it straight away.

    The document is same-origin, so the frame can be driven directly; if the
    browser will not allow that, the PDF is opened in a tab to print from. */
function printPdf(url) {
  const old = document.getElementById('pdf-print');
  if (old) old.remove();
  const frame = document.createElement('iframe');
  frame.id = 'pdf-print';
  frame.style.cssText =
    'position:fixed;right:0;bottom:0;width:1px;height:1px;opacity:0;border:0';
  let done = false;
  const fallback = () => {
    if (done) return;
    done = true;
    window.open(url, '_blank');
  };
  // Printing puts the keyboard inside the frame, and the frame is a pixel in
  // the corner: once the print dialog closed, every key went to the PDF and
  // not one of the app's own worked until the mouse clicked the page (found
  // 22 Sep 2026). The focus comes back to where it was as the dialog closes.
  // Only ever taken back from the frame itself -- a click somewhere else in
  // the meantime is left alone.
  const came = document.activeElement;
  const back = () => {
    if (document.activeElement !== frame) return;
    try { window.focus(); } catch (e) { /* nothing to do */ }
    if (came && came.isConnected && came !== document.body) {
      try { came.focus({ preventScroll: true }); } catch (e) { /* gone */ }
    }
    if (document.activeElement === frame) frame.blur();
  };
  frame.onload = () => {
    try {
      try { frame.contentWindow.addEventListener('afterprint', back); }
      catch (e) { /* a viewer that will not say; the timer below does */ }
      frame.contentWindow.focus();
      frame.contentWindow.print();
      done = true;
      back();
      setTimeout(back, 1500);
    } catch (e) { fallback(); }
  };
  frame.onerror = fallback;
  frame.src = url;
  document.body.appendChild(frame);
  // Nothing loaded at all -- fall back rather than leave the user with nothing.
  setTimeout(fallback, 8000);
}

async function printInvoice(id, format, company) {
  // `company` is for lists that span firms: a document is only readable from
  // its own books, so the firm has to be named rather than assumed.
  const inv = await api.get('/api/invoices/' + id, company ? { company } : {});
  const kind = format ||
    ((inv.company.tax_mode === 'none') ? 'challan' : 'tax');
  // The real stationery is rendered as a PDF from the measured templates. The
  // browser layout below is the fallback -- it is all an IGST bill can use,
  // since the printed form has no IGST columns.
  if (!(kind !== 'challan' && inv.is_igst)) {
    const url = '/api/invoices/' + id + '/pdf?company=' + inv.company_id +
      '&format=' + (kind === 'challan' ? 'challan' : 'invoice');
    printPdf(url);
    return;
  }
  toast('IGST bill — the printed form has no IGST columns, using the browser layout.');
  const area = $('#print-area');
  area.dataset.format = kind;
  pageStyle(kind === 'challan');
  area.innerHTML = kind === 'challan' ? challanHTML(inv) : taxInvoiceHTML(inv);
  // Say that the sheet, not the screen, is what goes to the printer. The
  // report screens print themselves and must not be swapped out.
  document.body.classList.add('print-doc');
  try { window.print(); } finally { document.body.classList.remove('print-doc'); }
}

/* ---------------------------------------------- a document's history ---
   6 Oct 2026: "add a history feature in all the bills and challans so I can
   see all the changes done in any particular detail. Whenever the bill or
   challan is saved the history should be kept, if anything changes then it
   should show." Every save that changed something is kept on the server
   (api._keep_history) as the whole document; this window lists them newest
   first, each with what it changed from the one before -- box by box, line
   by line -- and the whole document as it stood, folded away under it. */
const HISTORY_LABEL = {
  invoice_date: 'Date', party: 'Party', invoice_type: 'Invoice type',
  driver_name: 'Driver name', payment_terms: 'Payment terms', ref_no: 'Reference',
  is_igst: 'Tax type', rcm: 'Reverse charge', dept: 'By Dept', remarks: 'Particulars',
  weight_total: 'Quantity total', cutting: 'Cutting', transport: 'Transport',
  loading: 'Loading', taxable: 'Taxable value', cgst: 'CGST', sgst: 'SGST',
  igst: 'IGST', cess: 'Cess', tcs: 'TCS', round_off: 'Round off',
  grand_total: 'Grand total',
  transporter: 'Transporter', vehicle_no: 'Vehicle no', lr_no: 'LR / docket no',
  lr_date: 'LR date', cases: 'Cases', eway_no: 'E-way bill no',
  transporter_gstin: 'Transporter GSTIN', transport_mode: 'Mode',
  transport_distance: 'Distance (km)', vehicle_type: 'Vehicle type',
  sub_supply_type: 'Reason for movement',
  // a line
  description: 'Product', item_detail: 'Item detail', steel: 'Steel', hsn: 'HSN',
  qty: 'NOS', weight: 'Quantity', rate: 'Rate', amount: 'Amount', gst_rate: 'GST'
};
// What only a bill has: a challan's never change, and its "taxable value" is
// just its total again.
const HISTORY_TAX_ONLY = ['taxable', 'cgst', 'sgst', 'igst', 'cess', 'is_igst', 'rcm'];

function historyValue(field, v) {
  if (v === null || v === undefined || v === '') return '—';
  const code = list => (list.find(([c]) => c === String(v)) || [0, v])[1];
  switch (field) {
    case 'invoice_date': case 'lr_date': return dt(v);
    case 'is_igst': return v ? 'IGST (inter-state)' : 'CGST + SGST (local)';
    case 'rcm': return v ? 'Yes' : 'No';
    case 'weight_total': case 'weight': return qty(v) + ' kg';
    case 'qty': return qty(v);
    case 'rate': return rate3(v);
    case 'gst_rate': return qty(v) + '%';
    case 'transport_distance': return num(v) ? qty(v) : '—';
    case 'transport_mode': return code(EWAY_MODES);
    case 'vehicle_type': return code(EWAY_VEHICLE_TYPES);
    case 'sub_supply_type': return code(EWAY_SUB_SUPPLY);
    case 'cutting': case 'transport': case 'loading': case 'taxable': case 'cgst':
    case 'sgst': case 'igst': case 'cess': case 'tcs': case 'round_off':
    case 'grand_total':
    case 'amount': return fmt(v);
    default: return String(v);
  }
}

/** One line of a document, the way the bill reads it. */
function historyLine(l) {
  const name = [l.steel || l.description, l.item_detail].filter(Boolean).join(' · ');
  const kg = num(l.weight) ? qty(l.weight) + ' kg' : (num(l.qty) ? qty(l.qty) + ' nos' : '');
  return esc(name || '(no name)') + (kg ? ' · ' + esc(kg) : '')
    + (num(l.rate) ? ' @ ' + esc(rate3(l.rate)) : '')
    + ' = ' + esc(fmt(l.amount));
}

function historyChanges(v, h, label) {
  if (!v.changes.length) return '';
  const was = (f, a, b) => '<span class="hv-was">' + esc(historyValue(f, a))
    + '</span> → <b>' + esc(historyValue(f, b)) + '</b>';
  return '<ul class="hv-list">' + v.changes.map(c => {
    if (c.at === 'head') {
      if (!h.taxed && HISTORY_TAX_ONLY.indexOf(c.field) >= 0) return '';
      return '<li><span class="hv-k">' + esc(label(c.field)) + '</span> '
        + was(c.field, c.old, c.new) + '</li>';
    }
    if (c.at === 'added') {
      return '<li class="hv-add"><span class="hv-k">Line ' + c.line + ' added</span> '
        + historyLine(c.now) + '</li>';
    }
    if (c.at === 'removed') {
      return '<li class="hv-del"><span class="hv-k">Line ' + c.was_line + ' taken off</span> '
        + historyLine(c.now) + '</li>';
    }
    const name = [c.now.steel || c.now.description, c.now.item_detail].filter(Boolean).join(' · ');
    return '<li><span class="hv-k">Line ' + c.line
      + (c.was_line !== c.line ? ' (was ' + c.was_line + ')' : '') + '</span> '
      + '<span class="hv-name">' + esc(name) + '</span><br>'
      + c.fields.map(f => esc(label(f.field)) + ' ' + was(f.field, f.old, f.new)).join('; ')
      + '</li>';
  }).join('') + '</ul>';
}

/** The whole document as one version left it. */
function historyDoc(d, h, label) {
  const hd = d.head;
  const pairs = ['invoice_date', 'party', 'sales_ac', 'driver_name', 'payment_terms',
    'ref_no', 'dept', 'remarks', 'transporter', 'vehicle_no', 'eway_no']
    .filter(k => hd[k] !== '' && hd[k] !== undefined && hd[k] !== null)
    .map(k => '<span><span class="hv-k">' + esc(label(k)) + '</span> '
      + esc(historyValue(k, hd[k])) + '</span>').join('');
  const tax = h.taxed;
  const rows = d.lines.map((l, i) => `<tr><td class="c">${i + 1}</td>
      <td>${esc(l.steel || l.description)}${l.item_detail
        ? '<div class="hv-sub">' + esc(l.item_detail) + '</div>' : ''}</td>
      <td class="r">${num(l.qty) ? esc(qty(l.qty)) : ''}</td>
      <td class="r">${num(l.weight) ? esc(qty(l.weight)) : ''}</td>
      <td class="r">${num(l.rate) ? esc(rate3(l.rate)) : ''}</td>
      <td class="r">${esc(fmt(l.amount))}</td>
      ${tax ? `<td class="c">${esc(qty(l.gst_rate))}%</td>` : ''}</tr>`).join('');
  const money = ['cutting', 'transport', 'loading', ...(tax ? ['taxable', 'cgst', 'sgst', 'igst', 'cess'] : []),
    'tcs', 'round_off'].filter(k => num(hd[k]))
    .map(k => '<span><span class="hv-k">' + esc(label(k)) + '</span> ' + esc(fmt(hd[k])) + '</span>');
  return `<div class="hv-head">${pairs}</div>
    <div class="tbl-scroll"><table class="hv-tbl"><thead><tr><th class="c">#</th><th>Product</th>
      <th class="r">NOS</th><th class="r">Quantity</th><th class="r">Rate</th><th class="r">Amount</th>
      ${tax ? '<th class="c">GST</th>' : ''}</tr></thead>
      <tbody>${rows}</tbody></table></div>
    <div class="hv-head">${money.join('')}<span><span class="hv-k">Quantity total</span> ${
      esc(qty(hd.weight_total))} kg</span><span><span class="hv-k">Grand total</span> <b>${
      esc(fmt(hd.grand_total))}</b></span></div>`;
}

async function docHistory(doc) {
  let h;
  try { h = await api.get('/api/invoices/' + doc.id + '/history'); }
  catch (e) { bad(e.message); return; }
  const what = h.taxed ? (DOC_LABEL[h.doc_type] || 'document').toLowerCase() : 'challan';
  const label = f => f === 'no' ? (h.taxed ? 'Invoice no' : 'Challan no')
    : f === 'sales_ac' ? TRADE_AC_LABEL[h.doc_type] || 'Sales A/c' : (HISTORY_LABEL[f] || f);
  const title = v => {
    switch (v.action) {
      case 'created': return 'Made';
      case 'edited': return v.note === 'e-way screen' ? 'Changed on the e-way screen' : 'Changed';
      case 'rebuilt': return 'Redone from challan ' + v.note + ', which was changed';
      case 'before': return v.note === 'SHREE' ? 'As it came from SHREE'
        : 'As it stood before its first change was kept';
      case 'elsewhere': return 'Changed outside this screen';
      default: return v.action;
    }
  };
  const when = v => v.saved_at ? dt(v.saved_at.slice(0, 10)) + ' ' + v.saved_at.slice(11, 16) : '';
  const body = !h.versions.length
    ? `<p class="hv-none">Nothing has been changed on this ${esc(what)} since its history
        began on 06-10-2026. Every save that changes it from now on is kept here —
        who saved it, when, and each box and line it changed.</p>`
    : h.versions.map((v, i) => `<div class="hv${i ? '' : ' hv-latest'}">
        <div class="hv-top"><b>${esc(title(v))}</b>
          <span class="hv-when">${esc([when(v), v.who].filter(Boolean).join(' · '))}</span>
          <span class="tag${i ? '' : ' ok'}">${i ? 'version ' + v.version : 'as it is now'}</span></div>
        ${v.action === 'elsewhere' ? '<div class="hv-why">Between two saves — a SHREE update, a merge of accounts or a repair.</div>' : ''}
        ${v.changes.length ? historyChanges(v, h, label)
          : (v.action === 'before' || v.action === 'created'
            ? '<div class="hv-why">' + esc(v.doc.lines.length + ' line' + (v.doc.lines.length === 1 ? '' : 's')
              + ' · ' + v.doc.head.party + ' · ₹' + fmt(v.doc.head.grand_total)) + '</div>' : '')}
        <details><summary>The whole ${esc(what)} at this point</summary>${historyDoc(v.doc, h, label)}</details>
      </div>`).join('');
  return modal('History — ' + (h.taxed ? '' : 'Challan ') + h.no, body, null, { wide: true });
}

/* ------------------------------------------------------- A4 tax invoice -- */
function taxInvoiceHTML(inv, copyLabel) {
  const c = inv.company;
  const igst = !!inv.is_igst;
  const memo = { SALE: 'DEBIT MEMO', DEBIT: 'DEBIT MEMO',
    CREDIT: 'CREDIT MEMO', PURCHASE: 'PURCHASE BILL' }[inv.doc_type];
  const title = inv.doc_type === 'CREDIT'
    ? 'C R E D I T &nbsp;&nbsp; N O T E' : 'T A X &nbsp;&nbsp; I N V O I C E';
  const unit = (inv.lines[0] && inv.lines[0].unit) || 'Kgs';
  const qtyTotal = inv.lines.reduce(
    (a, l) => a + (num(l.weight) || num(l.qty)), 0);
  // Goods before the charges; `taxable` already has the charges inside it.
  const goods = num(inv.goods_total) ||
    inv.lines.reduce((a, l) => a + num(l.taxable), 0);
  const charged = num(inv.cutting) + num(inv.transport) + num(inv.loading);
  const pad = Math.max(0, 16 - inv.lines.length);
  const tax = num(inv.cgst) + num(inv.sgst) + num(inv.igst) + num(inv.cess);

  const addr = [c.address1, c.address2, c.city, c.state, c.pin]
    .filter(Boolean).join(', ');

  return `
  <div class="sheet"><div class="inv">
    <div class="head">
      <div class="logo">${c.logo ? `<img src="${esc(c.logo)}" alt="">` : ''}</div>
      <div class="who">
        <div class="co">${esc(c.name)}</div>
        <div class="rule"></div>
        <div class="ad">${esc(addr)}</div>
        <div class="ad">${[c.phone && 'Phone: ' + c.phone,
          c.mobile && 'Mobile No: ' + c.mobile].filter(Boolean).map(esc).join('&nbsp;&nbsp;&nbsp;&nbsp;')}</div>
        ${c.email ? `<div class="ad">E-Mail ${esc(c.email)}</div>` : ''}
      </div>
    </div>

    <div class="band"><span>${esc(memo)}</span>
      <span class="mid">${title}</span>
      <span>${esc(copyLabel || 'ORIGINAL')}</span></div>

    <div class="parties">
      <div class="to">
        <div class="body">
          <b>To, ${esc(inv.party_name)}</b>
          ${[inv.party_address1, inv.party_address2].filter(Boolean)
            .map(x => '<div>' + esc(x) + '</div>').join('')}
          <div>${esc([inv.party_city, inv.party_pin].filter(Boolean).join(' '))}</div>
        </div>
        <div class="gst">Party's GST No: ${esc(inv.party_gstin || '')}</div>
      </div>
      <div class="meta">
        <div class="top"><b>Invoice No: ${esc(docNo(inv))}</b>
          <span>Date: ${shortDate(inv.invoice_date)}</span></div>
        <div class="ln"><i>Transport :</i>${esc(inv.transporter || '')}</div>
        <div class="ln"><i>L. R. No. &nbsp;:</i>${esc(inv.lr_no || '')}
          <span style="margin-left:auto">Date: ${shortDate(inv.lr_date) || '/ /'}</span></div>
        <div class="ln"><i>Vhicle No :</i>${esc(inv.vehicle_no || '')}</div>
        <div class="ln"><i>Cases &nbsp; &nbsp; &nbsp;:</i>${esc(inv.cases || '')}</div>
        <div class="ln"><i>Payment Terms :</i>${esc(inv.payment_terms || '')}</div>
      </div>
    </div>

    <table class="items">
      <thead><tr>
        <th style="width:9mm">No.</th><th>Particulars</th>
        <th style="width:22mm">HSN Code</th><th style="width:26mm">Quantity</th>
        <th style="width:22mm">Rate Per<br>${esc(unit)}.</th>
        <th style="width:15mm">GST%</th><th style="width:28mm">Amount</th>
      </tr></thead>
      <tbody>
        ${inv.lines.map((l, i) => `<tr>
          <td class="c">${i + 1}</td>
          <td>${esc(l.description)}
            ${l.item_detail ? '<div style="font-size:10px">' + esc(l.item_detail) + '</div>' : ''}</td>
          <td class="c" style="font-size:10.5px">${esc(l.hsn || '')}</td>
          <td class="r">${fmt(num(l.weight) || l.qty)}
            <span style="font-size:9px">&nbsp;${esc(l.unit || '')}</span></td>
          <td class="r mono">${rate3(l.rate)}</td>
          <td class="r">${fmt(l.gst_rate)}</td>
          <td class="r mono">${fmt(l.taxable)}</td></tr>`).join('')}
        ${Array(pad).fill('<tr class="fill"><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>').join('')}
      </tbody>
      <tfoot><tr>
        <td></td><td></td><td></td>
        <td class="r mono">${fmt(qtyTotal)}</td>
        <td colspan="2">Sub Total</td>
        <td class="r mono">${fmt(goods)}</td></tr></tfoot>
    </table>

    <div class="foot">
      <div class="left">
        <div class="bank">Bank Details: &nbsp;<b>${esc(c.bank_name || '')}</b>
          ${c.bank_ac ? '<div>A/C No: ' + esc(c.bank_ac) +
            (c.bank_ifsc ? '&nbsp;&nbsp; IFSC CODE: ' + esc(c.bank_ifsc) : '') + '</div>' : ''}
        </div>
        <div style="flex:1"></div>
        <table class="gstsum">
          <thead><tr>
            <th>GST Summary</th><th>Taxable Amnt</th>
            ${igst ? '<th>IGST</th><th>IGST AMT</th>'
              : '<th>SGST</th><th>SGST AMT</th><th>CGST</th><th>CGST AMT</th>'}
            <th>Total GST</th></tr></thead>
          <tbody>
            ${inv.gst_summary.map(g => `<tr>
              <td>GST @${fmt(g.rate)}</td><td class="r mono">${fmt(g.taxable)}</td>
              ${igst ? `<td class="c">${fmt(g.rate)}</td><td class="r mono">${fmt(g.igst)}</td>`
                : `<td class="c">${fmt(g.rate / 2)}</td><td class="r mono">${fmt(g.sgst)}</td>
                   <td class="c">${fmt(g.rate / 2)}</td><td class="r mono">${fmt(g.cgst)}</td>`}
              <td class="r mono">${fmt(g.total_gst)}</td></tr>`).join('')}
            <tr><td>Total :</td><td class="r mono">${fmt(inv.taxable)}</td>
              ${igst ? `<td></td><td class="r mono">${fmt(inv.igst)}</td>`
                : `<td></td><td class="r mono">${fmt(inv.sgst)}</td>
                   <td></td><td class="r mono">${fmt(inv.cgst)}</td>`}
              <td class="r mono">${fmt(tax)}</td></tr>
          </tbody>
        </table>
      </div>
      <div class="right">
        <div class="ln"><b>Sub Total</b><b class="mono">${fmt(goods)}</b></div>
        ${num(inv.cutting) ? `<div class="ln"><span>Cutting</span><span class="mono">${fmt(inv.cutting)}</span></div>` : ''}
        ${num(inv.transport) ? `<div class="ln"><span>Transport</span><span class="mono">${fmt(inv.transport)}</span></div>` : ''}
        ${num(inv.loading) ? `<div class="ln"><span>Loading</span><span class="mono">${fmt(inv.loading)}</span></div>` : ''}
        ${num(inv.taxable_adj) ? `<div class="ln"><span>Round Off</span>
          <span class="mono">${fmt(inv.taxable_adj)}</span></div>` : ''}
        ${charged || num(inv.taxable_adj) ? `<div class="ln"><b>Taxable Value</b>
          <b class="mono">${fmt(inv.taxable)}</b></div>` : ''}
        ${igst ? `<div class="ln"><span>IGST</span><span class="mono">${fmt(inv.igst)}</span></div>`
          : `<div class="ln"><span>SGST</span><span class="mono">${fmt(inv.sgst)}</span></div>
             <div class="ln"><span>CGST</span><span class="mono">${fmt(inv.cgst)}</span></div>`}
        ${num(inv.cess) ? `<div class="ln"><span>Cess</span><span class="mono">${fmt(inv.cess)}</span></div>` : ''}
        <div class="sp"></div>
        <div class="ln"><span>Round Off</span><span class="mono">${fmt(inv.round_off)}</span></div>
        <div class="grand"><span>Grand Total</span>
          <span class="mono">${fmt(inv.grand_total)}</span></div>
      </div>
    </div>

    <div class="words"><b>Rs.</b>&nbsp; <b>${esc(amountWords(inv.grand_total))}</b></div>

    <div class="sign">
      <div><i>GST No:${esc(c.gstin || '')}</i></div>
      <div class="rt"><b>For, ${esc(c.name)}</b></div>
    </div>
    <div class="sign" style="border-top:0">
      <div>
        <!-- The mills whose material is stocked, as on the printed
             stationery. This layout is the fallback an IGST bill falls back
             to, so it has to carry them too. -->
        <img src="/mill_logos.png" alt="" style="height:11mm;max-width:100%;
          display:block;margin:1.5mm 0 2mm">
        Subject to ${esc(c.jurisdiction || c.city || '')} Jurisdiction.
        <div>E. &amp; O. E.</div></div>
      <div class="rt" style="padding-top:12mm">Authorised Signatory</div>
    </div>
  </div></div>`;
}

/* -------------------------------------------- landscape rough challan ---- */
function challanHTML(inv) {
  const one = challanCopy(inv);
  return `<div class="sheet"><div class="challan-page">${one}${one}</div></div>`;
}

function challanCopy(inv) {
  const c = inv.company;
  const pad = Math.max(0, 18 - inv.lines.length);
  const wtTotal = inv.lines.reduce((a, l) => a + (num(l.weight) || num(l.qty)), 0);
  const amtTotal = inv.lines.reduce((a, l) => a + num(l.taxable), 0);
  // A challan is a goods note and never carries tax. Printing one off a GST
  // bill has to leave the tax out, or the TOTAL stops agreeing with the
  // Amount column above it. On a real challan there is no tax to drop.
  const taxTotal = num(inv.cgst) + num(inv.sgst) + num(inv.igst) + num(inv.cess);
  const challanTotal = Math.round((num(inv.grand_total) - taxTotal) * 100) / 100;
  return `
  <div class="chal">
    <div class="top">${esc(c.short_name || c.code || c.name)}</div>
    <div class="hd">
      <div class="to">
        <div><b>To, ${esc(inv.party_name)}</b></div>
        <div>&nbsp;-${esc(inv.party_city || '')}</div>
        <div class="sp"></div>
        <div><b>Mo. No. : ${esc(inv.party_phone || '')}</b></div>
      </div>
      <div class="meta">
        <div>No. : &nbsp;${esc(docNo(inv))}</div>
        <div>Date: ${shortDate2(inv.invoice_date)}</div>
        <div style="font-size:11px">Payment Terms ${esc(inv.payment_terms || '')}</div>
      </div>
    </div>

    <table class="items">
      <thead><tr>
        <th style="width:7mm">No</th><th>Particulars</th>
        <th style="width:20mm">Qty kg.</th><th style="width:18mm">Rate kg.</th>
        <th style="width:24mm">Amount</th></tr></thead>
      <tbody>
        ${inv.lines.map((l, i) => `<tr>
          <td class="c" style="font-size:10px">${i + 1}</td>
          <td>${num(l.qty) && num(l.weight) ? fmt(l.qty) + ' ' : ''}${esc(l.description)}
            ${l.item_detail ? ' ' + esc(l.item_detail) : ''}</td>
          <td class="r mono">${fmt(num(l.weight) || l.qty)}</td>
          <td class="r mono">${fmt(l.rate)}</td>
          <td class="r mono">${fmt(l.taxable)}</td></tr>`).join('')}
        ${Array(pad).fill('<tr class="fill"><td></td><td></td><td></td><td></td><td></td></tr>').join('')}
      </tbody>
      <tfoot><tr>
        <td></td><td class="r">Total Quantity:</td>
        <td class="r mono">${plain(wtTotal)}</td><td></td>
        <td class="r mono"><b>${plain(amtTotal)}</b></td></tr></tfoot>
    </table>

    <div class="sums">
      <div class="l">
        <div class="bal">
          <div><b>Opening Bal.</b> &nbsp;<b>+</b> &nbsp;<b>This Bill</b>
            &nbsp;<b>=</b> &nbsp;<b>Closing Bal.</b></div>
          <div style="display:flex;gap:12mm"><b>${plain(inv.party_opening)}</b>
            <b>${plain(inv.grand_total)}</b><b>${plain(inv.party_closing)}</b></div>
        </div>
        <div class="row">Drvier Name: &nbsp;${esc(inv.driver_name || '')}</div>
        <div class="row"><span style="font-size:10px">Rs.</span>
          ${esc(amountWords(challanTotal))}</div>
      </div>
      <div class="r">
        <div class="ln"><span>CUTTING</span>
          <span class="mono">${num(inv.cutting) ? fmt(inv.cutting) : ''}</span></div>
        <div class="ln"><span>TRANSPORT</span>
          <span class="mono">${num(inv.transport) ? fmt(inv.transport) : ''}</span></div>
        <div class="ln"><span>ROUND OFF</span>
          <span class="mono">${num(inv.round_off) ? fmt(inv.round_off) : ''}</span></div>
        <div class="ln"><span>LODING</span>
          <span class="mono">${num(inv.loading) ? fmt(inv.loading) : ''}</span></div>
        <div class="ln tot"><span>TOTAL</span>
          <span class="mono">${fmt(challanTotal)}</span></div>
      </div>
    </div>

    <div class="ack">
      <div><div class="note">Above mentioined goods received in goods condition.</div>
        <div style="margin-top:6mm">Receiver's Signature</div></div>
      <div><div style="height:8mm"></div>Authorised Signatory</div>
    </div>
  </div>`;
}

/* ================================================================ orders */
/* ============================================================== e-way bill
   The government portal will not take a bill; it takes a JSON file. These
   two screens build that file. Neither of them sends anything: the file is
   downloaded and uploaded by hand at ewaybillgst.gov.in.

   Everything the portal would have thrown back is said here first, because
   finding out on the portal means finding out with the lorry loaded.      */

// How the series and the number are joined for the portal. Chosen on the
// e-way screen and remembered in the books, so the button on a bill uses the
// same answer without the screen having been opened.
let EWAY_DOCNO = 'plain';
// Which of the two files to write. `bulk` is what SHREE's own copy of the NIC
// workbook wrote and what the portal's bulk upload page takes; `api` is the
// spelling SHREE's ShreeEwayBill.dll posted straight to NIC.
let EWAY_SCHEMA = 'bulk';
let EWAY_SET = null;
async function ewaySettings() {
  if (!EWAY_SET) EWAY_SET = await api.get('/api/eway/settings');
  EWAY_DOCNO = EWAY_SET.docno_style;
  return EWAY_SET;
}

const EWAY_TAG = { error: 'bad', warn: 'warn', info: 'info' };
const EWAY_WORD = { error: 'Refused', warn: 'Check', info: 'Note' };

function ewayNotes(notes) {
  if (!notes || !notes.length) return '<div class="hint">Nothing to fix.</div>';
  return '<ul class="notes">' + notes.map(n =>
    '<li><span class="tag ' + EWAY_TAG[n.level] + '">' + EWAY_WORD[n.level] +
    '</span><span>' + esc(n.text) + '</span></li>').join('') + '</ul>';
}

/** Where a posting's own document lives, or '' where it has none.

    A receipt, a payment and a journal are vouchers; everything else is a
    document in the register. An opening balance is the one posting that came
    from no document at all.                                                */
const VOUCHER_KINDS = ['RECEIPT', 'PAYMENT', 'JOURNAL', 'CONTRA'];

/* What an open item on a customer's account is called where a customer can
   read it. A challan is the goods going out; the tax on it is a journal
   written beside it, and "GST" is what that is on a statement. Matches
   `DUE_KIND` in api.py, which names them the same way in the CSV. */
const DUE_KIND = {
  SALE: 'Challan', DEBIT: 'Debit note', JOURNAL: 'GST',
  OPENING: 'Opening balance', PURCHASE: 'Purchase',
  RECEIPT: 'Receipt', PAYMENT: 'Payment', CONTRA: 'Contra'
};

function docHref(kind, id) {
  if (!id) return '';
  return (VOUCHER_KINDS.indexOf(kind) >= 0 ? '#/voucher/' : '#/invoice/') + id;
}

/* -------------------------------------------- one thing leads to another ---
   Asked for on 16 Sep 2026, over a challan on Who owes us that could not be
   opened: *connect the software everywhere possible*. A document number, an
   account, an order printed anywhere leads to it.

   Every link is an ordinary address, so Back, a new tab and the remembered
   screen all behave. A document names the book it sits in -- `?firm=` --
   because a bill is in a different firm from the challan it came from, and
   `render` switches to that firm before the document is read.            */
function docLink(kind, id, firm) {
  const h = docHref(kind, id);
  return h && firm ? h + '?firm=' + firm : h;
}
/** How SHREE's challans and bills were matched when not one to one
 *  (link_challans.py), for the tag's tooltip. */
const LINK_HOW = {
  split: 'one challan billed in parts',
  combined: 'challans billed together on one bill',
  regrouped: 'challans re-cut into other bills',
  repriced: 'same weight — the bill was re-priced',
  reweighed: 'same money — the weight was changed on the bill',
  near: 'within a kilo and half a per cent',
  renamed: 'the same load — the customer is named differently in the two firms',
  'other name': 'the same load — billed in another party\'s name',
};
/** The challans of a bill, or the bills of a challan, each one a link. */
function linkedDocs(list) {
  return (list || []).map(d => goLink(docLink('SALE', d.id, d.company_id),
    esc(d.no), 'Open ' + d.no + ' · ' + fmt(d.weight_total) + ' kg · ₹'
      + fmt(d.taxable))).join(' + ');
}
function linkedTitle(list) {
  const how = (list || []).map(d => LINK_HOW[d.how]).find(Boolean);
  return how ? ' title="Matched from SHREE: ' + esc(how) + '"' : '';
}
/** "GT 193 + GT 202" for a challan billed in parts, else its one bill. */
function billedAs(doc) {
  return (doc.bills || []).length > 1 ? doc.bills.map(d => d.no).join(' + ')
    : doc.billed_invoice_no;
}
/** An account. `book` keeps to that one ledger, for a figure read off one
 *  firm's books (a trial balance line) rather than the customer as a whole. */
function accountLink(id, book) {
  return id ? '#/accounts?ledger_id=' + id + (book ? '&scope=book' : '') : '';
}
function orderLink(id) { return id ? '#/order/' + id : ''; }
/** The chart of accounts, narrowed to one group of this firm. */
function groupLink(code) {
  const g = (S.meta.groups || []).find(x => x.code === code);
  return g ? '#/ledgers?group=' + g.id : '';
}
/** Text that leads somewhere -- or just the text, when there is nowhere. */
function goLink(href, html, title) {
  return href ? '<a class="golink" href="' + esc(href) + '"'
    + (title ? ' title="' + esc(title) + '"' : '') + '>' + html + '</a>' : html;
}


function ewayScore(errors, warnings) {
  if (errors) return '<span class="tag bad">' + errors + ' to fix</span>';
  if (warnings) return '<span class="tag warn">' + warnings + ' to check</span>';
  return '<span class="tag ok">Ready</span>';
}

/** Hand the file over. `force` sends it even though the portal would refuse. */
function ewayDownload(refs, force) {
  location.href = firmUrl('/api/eway/json?' + new URLSearchParams({
    ids: refs.join(','), docno: EWAY_DOCNO, schema: EWAY_SCHEMA,
    force: force ? '1' : '' }));
}

/** Download, but ask first when the portal would only refuse the file. */
async function ewayConfirmDownload(refs, errors) {
  if (errors && !await modal('The portal would refuse this',
    '<p>' + errors + ' problem(s) still stand. The file will download, but ' +
    'the portal will not take those rows until they are put right.</p>',
    [{ label: 'Cancel', value: false },
     { label: 'Download anyway', cls: 'danger', value: true }])) return;
  ewayDownload(refs, true);
}

/** One bill: the checks, the transport boxes, and the file. */
async function ewayFor(companyId, invoiceId) {
  const ref = companyId + ':' + invoiceId;
  let d;
  try {
    await ewaySettings();
    d = await api.get('/api/invoices/' + invoiceId + '/eway',
                      { company: companyId, docno: EWAY_DOCNO,
                        schema: EWAY_SCHEMA });
  } catch (e) { return bad(e.message); }

  const body = () => `
    <div class="row" style="margin-bottom:10px">
      <div><b>${esc(d.doc_no)}</b> · ${dt(d.doc_date)} · ${esc(d.party)}</div>
      <div class="spacer"></div>
      <div id="ew-score">${ewayScore(d.errors, d.warnings)}</div>
    </div>
    <div class="grid g3">
      <div><label>Vehicle no</label><input id="ew-veh" placeholder="GJ03BY9139"
        value="${esc(d.payload.vehicleNo || '')}"></div>
      <div><label>Transporter GSTIN</label><input id="ew-tid" maxlength="15"
        style="font-family:var(--mono)" value="${esc(d.payload.transporterId || '')}"></div>
      <div><label>Transporter name</label><input id="ew-tnm"
        value="${esc(d.payload.transporterName || '')}"></div>
      <div><label>Mode</label><select id="ew-mode"><option value="">—</option>
        ${EWAY_MODES.map(([c, n]) => `<option value="${c}"${String(d.payload.transMode) === c ? ' selected' : ''}>${esc(n)}</option>`).join('')}
      </select></div>
      <div><label>Distance (km)</label><input id="ew-dist" type="number" min="0"
        step="1" placeholder="0 = portal works it out"
        value="${Number(d.payload.transDistance) ? esc(d.payload.transDistance) : ''}"></div>
      <div><label>Reason for movement</label><select id="ew-sub">
        ${EWAY_SUB_SUPPLY.map(([c, n]) => `<option value="${c}"${String(d.payload.subSupplyType) === c ? ' selected' : ''}>${esc(n)}</option>`).join('')}
      </select></div>
      <div><label>LR / docket no</label><input id="ew-lr" maxlength="15"
        placeholder="by rail, air or ship" value="${esc(d.lr_no || '')}"></div>
      <div><label>LR date</label><input type="date" id="ew-lrdt"
        value="${esc(d.lr_date || '')}"></div>
    </div>
    <div class="hint">By rail, air or ship the lorry receipt and its date are
      both required; by road they can be left empty.</div>
    <div class="hint">Saving here writes only these boxes. The bill's own
      figures are not touched.</div>
    <div id="ew-notes" style="margin-top:14px">${ewayNotes(d.notes)}</div>`;

  await modal('E-way bill JSON', body(), [
    {
      label: 'Save transport', value: null,
      before: async bg => {
        try {
          d = await api.put('/api/invoices/' + invoiceId + '/transport?company=' +
                            companyId, {
            vehicle_no: $('#ew-veh', bg).value.trim(),
            transporter_gstin: $('#ew-tid', bg).value.trim().toUpperCase(),
            transporter: $('#ew-tnm', bg).value.trim(),
            transport_mode: $('#ew-mode', bg).value,
            transport_distance: $('#ew-dist', bg).value,
            sub_supply_type: $('#ew-sub', bg).value,
            lr_no: $('#ew-lr', bg).value.trim(),
            lr_date: $('#ew-lrdt', bg).value,
            docno: EWAY_DOCNO, schema: EWAY_SCHEMA
          });
        } catch (e) { bad(e.message); return false; }
        $('#ew-notes', bg).innerHTML = ewayNotes(d.notes);
        $('#ew-score', bg).innerHTML = ewayScore(d.errors, d.warnings);
        ok('Transport saved.');
        return false;                       // stay open to see the new checks
      }
    },
    {
      label: '⤓ Download JSON', cls: 'primary', value: null,
      before: async () => { await ewayConfirmDownload([ref], d.errors); return false; }
    },
    { label: 'Close', value: null }
  ], { wide: true });
}

/* The bulk screen. One file can carry many bills, which is the whole reason
   the portal accepts JSON at all. */
route(/^\/eway$/, async (view, _m, q) => {
  const here = onScreen();
  const set = await ewaySettings();
  if (!here()) return;

  setTitle('E-way bill JSON', 'Build the file the government portal uploads', [
    { label: '⤓ Download JSON', cls: 'primary', onClick: () => ewayGo() },
    { label: 'Check selected', onClick: () => ewayCheck() }
  ]);

  view.innerHTML = `
    <div class="card"><div class="bd"><div class="row">
      <div><label>Firm</label><select id="ef" style="width:calc(300px * var(--typescale))">
        <option value="">Both billing firms</option>
        ${(S.meta.billing_firms || []).map(f =>
          `<option value="${f.id}"${String(q.firm) === String(f.id) ? ' selected' : ''}>${esc(firmLabel(f))}</option>`).join('')}
        <option value="order"${q.firm === 'order' ? ' selected' : ''}>Challans (order firm)</option>
      </select></div>
      ${fySelect('fyq')}
      <div><label>From</label><input type="date" id="f1" value="${esc(q.from || today())}"></div>
      <div><label>To</label><input type="date" id="f2" value="${esc(q.to || today())}"></div>
      <div style="flex:1;min-width:180px"><label>Search</label>
        <input id="q" placeholder="Bill no or party…"></div>
      <div><label>&nbsp;</label><label style="margin:0;text-transform:none;
        font-size:13px"><input type="checkbox" id="big" style="width:auto">
        Only over ₹50,000</label></div>
    </div>
    <div class="row" style="margin-top:10px">
      <div><label>Number on the portal</label><select id="dno" style="width:calc(230px * var(--typescale))">
        ${set.docno_styles.map(x =>
          `<option value="${x.code}"${set.docno_style === x.code ? ' selected' : ''}>${esc(x.name)}</option>`).join('')}
      </select></div>
      <div><label>File</label><select id="esc" style="width:calc(270px * var(--typescale))">
        ${set.schemas.map(x =>
          `<option value="${x.code}"${EWAY_SCHEMA === x.code ? ' selected' : ''}>${esc(x.name)}</option>`).join('')}
      </select></div>
      <div><label>Challans move under</label><select id="cons" style="width:calc(300px * var(--typescale))">
        <option value="">— nothing chosen —</option>
        ${(set.billing_firms || []).map(f =>
          `<option value="${f.id}"${String(set.consignor) === String(f.id) ? ' selected' : ''}>${esc(firmLabel(f))}</option>`).join('')}
      </select></div>
      <div class="hint" style="margin:0 0 8px">A bill prints as
        <b>GT&nbsp;188</b>, but the portal takes letters, digits, . / and -
        only, and drops spaces. Pick whatever these bills are filed as in
        GSTR-1 — it is remembered.<br>
        The challan book has no GSTIN of its own, so a challan goes up under
        the firm that bills that customer. This is the firm for the customers
        where nothing says which.</div>
    </div>
    <div class="hint">Tick the bills going out, check them, then download.
      Upload the file at <b>ewaybillgst.gov.in → e-Waybill → Generate Bulk</b>.
      Nothing is sent from here.</div>
    </div></div>
    <div class="card"><div class="bd flush tbl-scroll" id="list"></div></div>
    <div id="checks"></div>`;

  const picked = new Set();

  async function ewayCheck() {
    if (!picked.size) return bad('Tick at least one bill first.');
    $('#checks').innerHTML = '<div class="card"><div class="bd">Checking…</div></div>';
    let d;
    try {
      d = await api.get('/api/eway',
                        { ids: [...picked].join(','), docno: EWAY_DOCNO,
                          schema: EWAY_SCHEMA });
    }
    catch (e) { $('#checks').innerHTML = ''; return bad(e.message); }
    $('#checks').innerHTML = `
      <div class="card"><h2>Checked ${d.count} bill(s) · ${money(d.total)}
        <span class="rt">${ewayScore(d.errors, d.warnings)}</span></h2>
        <div class="bd">` + d.documents.map(x => `
          <div class="eway-doc">
            <div class="row"><b>${esc(x.doc_no)}</b>
              <span style="color:var(--muted)">${dt(x.doc_date)} · ${esc(x.party)} · ${money(x.grand_total)}</span>
              <div class="spacer"></div>${ewayScore(x.errors, x.warnings)}
              <button class="link" data-open="${x.company_id}:${x.invoice_id}">Fix</button>
            </div>${ewayNotes(x.notes)}</div>`).join('') + '</div></div>';
    $$('#checks [data-open]').forEach(b => b.onclick = async () => {
      const [c, i] = b.dataset.open.split(':');
      await ewayFor(num(c), num(i));
      ewayCheck();
    });
    return d;
  }

  async function ewayGo() {
    if (!picked.size) return bad('Tick at least one bill first.');
    const d = await ewayCheck();
    if (d) await ewayConfirmDownload([...picked], d.errors);
  }

  const mem = screenState('/eway', {
    firm: '#ef', from: '#f1', to: '#f2', search: '#q', big: '#big',
    fy: fyField('f1', 'f2')
  }).restore(q);

  async function load() {
    mem.save();
    const here = onSearch();
    const d = await api.get('/api/eway', {
      firm: $('#ef').value, from: $('#f1').value, to: $('#f2').value,
      search: $('#q').value.trim(), docno: EWAY_DOCNO
    });
    if (!here()) return;
    const rows = $('#big').checked
      ? d.rows.filter(r => !r.below_threshold) : d.rows;
    picked.clear();
    $('#checks').innerHTML = '';

    $('#list').innerHTML = rows.length ? `<table><thead><tr>
      <th style="width:34px"><input type="checkbox" id="all" style="width:auto"></th>
      <th>Date</th><th>Firm</th><th>Bill no</th><th>Party</th>
      <th class="r">Total</th><th>Vehicle</th><th>E-way no</th><th>Missing</th>
      </tr></thead><tbody>` + rows.map(r => `
      <tr data-ref="${r.firm_id}:${r.id}">
        <td><input type="checkbox" class="pick" style="width:auto"></td>
        <td class="nw">${dt(r.invoice_date)}</td>
        <td><span class="tag info">${esc(r.firm_code)}</span></td>
        <td><b>${goLink(docLink('SALE', r.id, r.firm_id), esc(r.doc_no), 'Open the bill to fill in what is missing')}</b></td>
        <td>${goLink(accountLink(r.party_id), esc(r.party_name), 'Open the account')}<div style="font-size:11.5px;color:var(--muted)">${esc(r.party_city)}</div></td>
        <td class="r num">${fmt(r.grand_total)}${r.below_threshold ? '<div style="font-size:11px;color:var(--muted)">under 50,000</div>' : ''}</td>
        <td style="font-family:var(--mono);font-size:11.5px">${esc(r.vehicle_no || '—')}</td>
        <td style="font-family:var(--mono);font-size:11.5px">${r.eway_no ? '<span class="tag ok">' + esc(r.eway_no) + '</span>' : '—'}</td>
        <td>${r.missing.length
          ? r.missing.map(m => '<span class="tag warn">' + esc(m) + '</span>').join(' ')
          : '<span class="tag ok">looks complete</span>'}</td>
      </tr>`).join('') + '</tbody></table>'
      : '<div class="empty"><b>No bills in this range</b>Widen the dates, or pick the other firm.</div>';

    $$('#list tr[data-ref]').forEach(tr => {
      const box = $('.pick', tr);
      box.onchange = () => {
        box.checked ? picked.add(tr.dataset.ref) : picked.delete(tr.dataset.ref);
        tr.classList.toggle('on', box.checked);
      };
    });
    const all = $('#all');
    if (all) all.onchange = () => {
      $$('#list .pick').forEach(b => { b.checked = all.checked; b.onchange(); });
    };
  }
  $('#dno').onchange = async () => {
    EWAY_DOCNO = $('#dno').value;
    try { EWAY_SET = await api.put('/api/eway/settings',
                                   { docno_style: EWAY_DOCNO }); }
    catch (e) { bad(e.message); }
    load();
  };
  $('#cons').onchange = async () => {
    try { EWAY_SET = await api.put('/api/eway/settings',
                                   { consignor: $('#cons').value }); }
    catch (e) { bad(e.message); }
    load();
  };
  // The file shape is a per-session choice, not a book setting -- the bulk
  // one is what goes to the portal and what it stays on unless asked.
  $('#esc').onchange = () => { EWAY_SCHEMA = $('#esc').value; $('#checks').innerHTML = ''; };
  $('#ef').onchange = load;
  bindFy(load, 'f1', 'f2');
  ['#f1', '#f2', '#big'].forEach(x => $(x).onchange = load);
  $('#q').oninput = () => { clearTimeout(window._ev); window._ev = setTimeout(load, 160); };
  await load();
});

/* ============================================================== e-invoice
   The IRN comes off a second portal, and the file it takes is a different
   shape from the e-way one -- the whole bill, seller and buyer and every
   line, because the portal signs it rather than merely recording a movement.

   One thing decides which of the two shapes comes out: `EwbDtls`. With
   transport on the bill it is written and the portal raises the e-way bill
   along with the IRN; without, the IRN comes back on its own and the e-way
   bill can be raised later from the other screen.                        */

function einvoiceDownload(refs, force, ewb) {
  location.href = firmUrl('/api/einvoice/json?' + new URLSearchParams({
    ids: refs.join(','), docno: EWAY_DOCNO, force: force ? '1' : '',
    ewb: ewb === undefined ? '' : (ewb ? '1' : '0') }));
}

/** Download, but ask first when the portal would only refuse the file. */
async function einvoiceConfirmDownload(refs, errors, ewb) {
  if (errors && !await modal('The portal would refuse this',
    '<p>' + errors + ' problem(s) still stand. The file will download, but ' +
    'the portal will not take those bills until they are put right.</p>',
    [{ label: 'Cancel', value: false },
     { label: 'Download anyway', cls: 'danger', value: true }])) return;
  einvoiceDownload(refs, true, ewb);
}

/** One bill: the checks, the transport boxes, and the file. */
async function einvoiceFor(companyId, invoiceId) {
  const ref = companyId + ':' + invoiceId;
  let d;
  const fetchOne = async ewb => api.get('/api/invoices/' + invoiceId + '/einvoice',
    { company: companyId, docno: EWAY_DOCNO,
      ewb: ewb === undefined ? '' : (ewb ? '1' : '0') });
  try {
    await ewaySettings();
    d = await fetchOne();
  } catch (e) { return bad(e.message); }

  const ewb = () => d.payload.EwbDtls || {};
  const body = () => `
    <div class="row" style="margin-bottom:10px">
      <div><b>${esc(d.doc_no)}</b> · ${dt(d.doc_date)} · ${esc(d.party)}</div>
      <div class="spacer"></div>
      <div id="ei-score">${ewayScore(d.errors, d.warnings)}</div>
    </div>
    <label style="text-transform:none;font-size:13.5px">
      <input type="checkbox" id="ei-ewb" style="width:auto"
        ${d.with_ewb ? 'checked' : ''}>
      Ask for the e-way bill as well</label>
    <div class="hint" style="margin:4px 0 12px">Ticked, the file carries
      <b>EwbDtls</b> and the portal raises both at once. Left clear, the IRN
      comes back on its own.</div>
    <div class="grid g3" id="ei-transport" ${d.with_ewb ? '' : 'style="display:none"'}>
      <div><label>Vehicle no</label><input id="ei-veh" placeholder="GJ03BY9139"
        value="${esc(ewb().VehNo || '')}"></div>
      <div><label>Transporter name</label><input id="ei-tnm"
        value="${esc(ewb().TransName || '')}"></div>
      <div><label>Transporter GSTIN</label><input id="ei-tid" maxlength="15"
        style="font-family:var(--mono)" value="${esc(ewb().TransId || '')}"></div>
      <div><label>Mode</label><select id="ei-mode"><option value="">—</option>
        ${EWAY_MODES.map(([c, n]) => `<option value="${c}"${ewb().TransMode === c ? ' selected' : ''}>${esc(n)}</option>`).join('')}
      </select></div>
      <div><label>Distance (km)</label><input id="ei-dist" type="number" min="0"
        step="1" placeholder="0 = portal works it out"
        value="${ewb().Distance ? esc(String(ewb().Distance)) : ''}"></div>
      <div><label>LR / docket no</label><input id="ei-lr" maxlength="15"
        placeholder="by rail, air or ship" value="${esc(d.lr_no || '')}"></div>
      <div><label>LR date</label><input type="date" id="ei-lrdt"
        value="${esc(d.lr_date || '')}"></div>
    </div>
    <div class="hint">By road the lorry receipt can be left empty -- the
      portal reads the invoice date in its place. By rail, air or ship both
      boxes are required and the file is refused without them.</div>
    <div id="ei-notes" style="margin-top:14px">${ewayNotes(d.notes)}</div>`;

  const redraw = bg => {
    $('#ei-notes', bg).innerHTML = ewayNotes(d.notes);
    $('#ei-score', bg).innerHTML = ewayScore(d.errors, d.warnings);
    const box = $('#ei-ewb', bg);
    if (box) box.checked = d.with_ewb;
    const t = $('#ei-transport', bg);
    if (t) t.style.display = d.with_ewb ? '' : 'none';
  };

  const dlg = modal('e-Invoice JSON', body(), [
    {
      label: 'Save transport', value: null,
      before: async bg => {
        try {
          d = await api.put('/api/invoices/' + invoiceId + '/transport?company=' +
                            companyId, {
            vehicle_no: $('#ei-veh', bg).value.trim(),
            transporter_gstin: $('#ei-tid', bg).value.trim().toUpperCase(),
            transporter: $('#ei-tnm', bg).value.trim(),
            transport_mode: $('#ei-mode', bg).value,
            transport_distance: $('#ei-dist', bg).value,
            lr_no: $('#ei-lr', bg).value.trim(),
            lr_date: $('#ei-lrdt', bg).value,
            portal: 'einvoice', docno: EWAY_DOCNO
          });
        } catch (e) { bad(e.message); return false; }
        redraw(bg);
        ok('Transport saved.');
        return false;                       // stay open to see the new checks
      }
    },
    {
      label: '⤓ Download JSON', cls: 'primary', value: null,
      before: async bg => {
        const want = $('#ei-ewb', bg) ? $('#ei-ewb', bg).checked : undefined;
        await einvoiceConfirmDownload([ref], d.errors, want);
        return false;
      }
    },
    { label: 'Close', value: null }
  ], { wide: true });

  // Asking for the e-way bill or not changes what the portal would refuse --
  // a tick with no vehicle behind it is the commonest way to be turned away
  // -- so the checks are asked for again the moment the box moves.
  const box = $('#ei-ewb');
  if (box) box.onchange = async () => {
    const want = box.checked;
    try { d = await fetchOne(want); } catch (e) { return bad(e.message); }
    d.with_ewb = want;
    redraw(document);
  };

  await dlg;
}

/* The bulk screen. One file carries many bills, which is what the portal's
   bulk upload is for. */
route(/^\/einvoice$/, async (view, _m, q) => {
  const here = onScreen();
  const set = await ewaySettings();
  if (!here()) return;

  setTitle('e-Invoice JSON', 'Build the file the IRP signs into an IRN', [
    { label: '⤓ Download JSON', cls: 'primary', onClick: () => eiGo() },
    { label: 'Check selected', onClick: () => eiCheck() }
  ]);

  view.innerHTML = `
    <div class="card"><div class="bd"><div class="row">
      <div><label>Firm</label><select id="ef" style="width:calc(300px * var(--typescale))">
        <option value="">Both billing firms</option>
        ${(S.meta.billing_firms || []).map(f =>
          `<option value="${f.id}"${String(q.firm) === String(f.id) ? ' selected' : ''}>${esc(firmLabel(f))}</option>`).join('')}
      </select></div>
      <div><label>Document</label><select id="dt" style="width:calc(170px * var(--typescale))">
        <option value="SALE">Bills</option>
        <option value="CREDIT">Credit notes</option>
        <option value="DEBIT">Debit notes</option>
      </select></div>
      ${fySelect('fyq')}
      <div><label>From</label><input type="date" id="f1" value="${esc(q.from || monthStart())}"></div>
      <div><label>To</label><input type="date" id="f2" value="${esc(q.to || today())}"></div>
      <div style="flex:1;min-width:180px"><label>Search</label>
        <input id="q" placeholder="Bill no or party…"></div>
      <div><label>&nbsp;</label><label style="margin:0;text-transform:none;
        font-size:13px"><input type="checkbox" id="ready" style="width:auto">
        Only the ones that look complete</label></div>
    </div>
    <div class="row" style="margin-top:10px">
      <div><label>Number on the portal</label><select id="dno" style="width:calc(230px * var(--typescale))">
        ${set.docno_styles.map(x =>
          `<option value="${x.code}"${set.docno_style === x.code ? ' selected' : ''}>${esc(x.name)}</option>`).join('')}
      </select></div>
      <div class="hint" style="margin:0 0 8px">The IRN is signed against this
        number, so it has to be the one the bill is filed under in GSTR-1.
        The same answer is used for the e-way file.</div>
    </div>
    <div class="hint">Tick the bills, check them, then download. Upload the
      file at <b>einvoice1.gst.gov.in → e-Invoice → Bulk Upload</b>, and the
      IRN and signed QR come back from there. Nothing is sent from here.</div>
    </div></div>
    <div class="card"><div class="bd flush tbl-scroll" id="list"></div></div>
    <div id="checks"></div>`;

  const picked = new Set();

  async function eiCheck() {
    if (!picked.size) return bad('Tick at least one bill first.');
    $('#checks').innerHTML = '<div class="card"><div class="bd">Checking…</div></div>';
    let d;
    try {
      d = await api.get('/api/einvoice',
                        { ids: [...picked].join(','), docno: EWAY_DOCNO });
    }
    catch (e) { $('#checks').innerHTML = ''; return bad(e.message); }
    $('#checks').innerHTML = `
      <div class="card"><h2>Checked ${d.count} bill(s) · ${money(d.total)}
        · ${d.with_ewb} with the e-way bill asked for
        <span class="rt">${ewayScore(d.errors, d.warnings)}</span></h2>
        <div class="bd">` + d.documents.map(x => `
          <div class="eway-doc">
            <div class="row"><b>${esc(x.doc_no)}</b>
              <span style="color:var(--muted)">${dt(x.doc_date)} · ${esc(x.party)} · ${money(x.grand_total)}</span>
              ${x.with_ewb ? '<span class="tag info">+ e-way</span>' : ''}
              <div class="spacer"></div>${ewayScore(x.errors, x.warnings)}
              <button class="link" data-open="${x.company_id}:${x.invoice_id}">Fix</button>
            </div>${ewayNotes(x.notes)}</div>`).join('') + '</div></div>';
    $$('#checks [data-open]').forEach(b => b.onclick = async () => {
      const [c, i] = b.dataset.open.split(':');
      await einvoiceFor(num(c), num(i));
      eiCheck();
    });
    return d;
  }

  async function eiGo() {
    if (!picked.size) return bad('Tick at least one bill first.');
    const d = await eiCheck();
    if (d) await einvoiceConfirmDownload([...picked], d.errors);
  }

  const mem = screenState('/einvoice', {
    firm: '#ef', doc_type: '#dt', from: '#f1', to: '#f2',
    search: '#q', ready: '#ready', fy: fyField('f1', 'f2')
  }).restore(q);

  async function load() {
    mem.save();
    const here = onSearch();
    const d = await api.get('/api/einvoice', {
      firm: $('#ef').value, doc_type: $('#dt').value, from: $('#f1').value,
      to: $('#f2').value, search: $('#q').value.trim(), docno: EWAY_DOCNO
    });
    if (!here()) return;
    const rows = $('#ready').checked ? d.rows.filter(r => !r.missing.length) : d.rows;
    picked.clear();
    $('#checks').innerHTML = '';

    $('#list').innerHTML = rows.length ? `<table><thead><tr>
      <th style="width:34px"><input type="checkbox" id="all" style="width:auto"></th>
      <th>Date</th><th>Firm</th><th>Bill no</th><th>Party</th>
      <th class="r">Total</th><th>GSTIN</th><th>E-way</th><th>Missing</th>
      </tr></thead><tbody>` + rows.map(r => `
      <tr data-ref="${r.firm_id}:${r.id}">
        <td><input type="checkbox" class="pick" style="width:auto"></td>
        <td class="nw">${dt(r.invoice_date)}</td>
        <td><span class="tag info">${esc(r.firm_code)}</span></td>
        <td><b>${goLink(docLink('SALE', r.id, r.firm_id), esc(r.doc_no), 'Open the bill to fill in what is missing')}</b></td>
        <td>${goLink(accountLink(r.party_id), esc(r.party_name), 'Open the account')}<div style="font-size:11.5px;color:var(--muted)">${esc(r.party_city)}</div></td>
        <td class="r num">${fmt(r.grand_total)}</td>
        <td style="font-family:var(--mono);font-size:11.5px">${esc(r.party_gstin || '—')}</td>
        <td>${r.with_ewb ? '<span class="tag info">asked for</span>' : '—'}</td>
        <td>${r.missing.length
          ? r.missing.map(m => '<span class="tag warn">' + esc(m) + '</span>').join(' ')
          : '<span class="tag ok">looks complete</span>'}</td>
      </tr>`).join('') + '</tbody></table>'
      : '<div class="empty"><b>No bills in this range</b>Widen the dates, or pick the other firm.</div>';

    $$('#list tr[data-ref]').forEach(tr => {
      const box = $('.pick', tr);
      box.onchange = () => {
        box.checked ? picked.add(tr.dataset.ref) : picked.delete(tr.dataset.ref);
        tr.classList.toggle('on', box.checked);
      };
    });
    const all = $('#all');
    if (all) all.onchange = () => {
      $$('#list .pick').forEach(b => { b.checked = all.checked; b.onchange(); });
    };
  }
  $('#dno').onchange = async () => {
    EWAY_DOCNO = $('#dno').value;
    try { EWAY_SET = await api.put('/api/eway/settings',
                                   { docno_style: EWAY_DOCNO }); }
    catch (e) { bad(e.message); }
    load();
  };
  bindFy(load, 'f1', 'f2');
  ['#ef', '#dt', '#f1', '#f2', '#ready'].forEach(x => $(x).onchange = load);
  $('#q').oninput = () => { clearTimeout(window._ei); window._ei = setTimeout(load, 160); };
  await load();
});

route(/^\/order\/(new|\d+)$/, async (view, m) => {
  const isNew = m[0] === 'new';
  const here = onScreen();
  await Promise.all([loadParties('fresh'), loadItems('fresh')]);

  let o, lines;
  if (isNew) {
    o = { order_no: (await api.get('/api/next-no', { key: 'ORDER' })).number,
      order_date: today(), due_date: '', party_id: '', remarks: '', status: 'open' };
    lines = [blankLine()];
  } else {
    o = await api.get('/api/orders/' + m[0]);
    lines = o.lines.map(l => ({
      item_id: l.item_id, description: l.description, hsn: l.hsn, unit: l.unit,
      qty: l.qty, weight: l.weight, by_weight: l.by_weight,
      rate: l.rate, discount_pct: l.discount_pct,
      gst_rate: l.gst_rate, cess_rate: 0,
      thickness: l.thickness || '', cutting_size: l.cutting_size || '',
      item_detail: l.item_detail || ''
    }));
    if (!lines.length) lines = [blankLine()];
  }
  if (!here()) return;

  setTitle(isNew ? 'New sales order' : 'Order ' + o.order_no,
    isNew
      ? 'Taken in ' + orderFirmName() +
        ' — challan raised there, then billed by the customer\u2019s own firm'
      : 'Status: ' + o.status + (o.billed_invoice_no
        ? (o.status === 'challaned'
          ? ' · challan ' + o.billed_invoice_no + ' in ' + (o.billed_firm_name || '')
          : ' · billed by ' + (o.billed_firm_name || '') + ' as ' + o.billed_invoice_no)
        : o.bill_firm_name ? ' · will be billed by ' + o.bill_firm_name : ''),
    isNew ? [] : [
      ...(o.status === 'open'
        ? [{ label: '📋 Make challan', cls: 'primary',
             onClick: () => challanOrder(o.id) }] : []),
      { label: o.status === 'cancelled' ? 'Re-open' : 'Cancel order',
        onClick: async () => {
          await api.put('/api/orders/' + o.id + '/status',
            { status: o.status === 'cancelled' ? 'open' : 'cancelled' });
          ok('Order updated.'); render();
        } },
      { label: 'Delete', cls: 'danger',
        onClick: () => deleteOrder(o.id, o.order_no,
          () => leave('#/orders')) }]);

  view.innerHTML = `
    <datalist id="unit-list">${S.meta.units.map(u => '<option>' + u + '</option>').join('')}</datalist>
    <datalist id="hsn-list">${hsnOptions()}</datalist>
    <div class="card"><h2>Order details</h2><div class="bd">
      <div class="grid g4">
        <div><label>Order no</label><input id="f-no" value="${esc(o.order_no)}" ${isNew ? '' : 'readonly'}></div>
        <div><label>Order date</label><input type="date" id="f-date" value="${esc(o.order_date)}"></div>
        <div><label>Delivery by</label><input type="date" id="f-due" value="${esc(o.due_date || '')}"></div>
        <div><label>Status</label><input value="${esc(o.status)}" readonly></div>
      </div>
      <div class="grid g4" style="margin-top:12px;align-items:start">
        <div class="pick sp2"><label>Customer *</label>
          <div class="pickbox">
            <input id="f-party" autocomplete="off" placeholder="Type to search…" value="${esc(o.party_name || '')}">
            <button type="button" data-browse-party class="pickbtn" tabindex="-1"
              title="Browse the customer list and pick who the order is for (F4)">▾</button></div>
          <div class="hint" id="party-info">&nbsp;</div></div>
        <div class="sp2"><label>Remarks</label>
          <input id="f-remarks" value="${esc(o.remarks || '')}"></div>
      </div>
    </div></div>
    ${(o.documents || []).length ? `<div class="card"><h2>Raised from this order</h2>
      <div class="bd flush tbl-scroll"><table><thead><tr><th>Date</th><th>Document</th>
        <th>Firm</th><th class="r">Value</th></tr></thead><tbody>
      ${o.documents.map(d => `<tr data-href="${esc(docLink(d.doc_type, d.id, d.company_id))}">
        <td class="nw">${dt(d.invoice_date)}</td>
        <td><b>${goLink(docLink(d.doc_type, d.id, d.company_id),
          (d.tax_mode === 'none' ? 'Challan ' : 'Bill ') + esc(d.no))}</b></td>
        <td><span class="tag info">${esc(d.firm_code)}</span></td>
        <td class="r num">${fmt(d.grand_total)}</td></tr>`).join('')}
      </tbody></table></div></div>` : ''}
    <div class="card"><h2>Ordered items</h2>
      <div class="bd" style="padding-bottom:0"><div class="hint" style="margin:0">
        Fill in NOS or a total Quantity, whichever the order was taken on —
        the Quantity prices the line when both are given.</div></div>
      <div class="bd" id="grid"></div></div>
    <div class="card"><div class="bd">
      <div class="row"><div id="ordtot" style="font-size:15px"></div><div class="spacer"></div>
        <button class="primary" id="save">${isNew ? 'Save order' : 'Update order'}
          <span class="kbd">Ctrl+S</span></button></div>
    </div></div>`;

  let party = o.party_id ? findParty(o.party_id) : null;
  // Every order is filed in the order firm, so the customer always has to be
  // found across the billing firms and followed over -- not just when that
  // firm happens to be the one on screen.
  const crossFirm = true;

  function showParty() {
    if (!party) { $('#party-info').innerHTML = 'No customer selected.'; return; }
    const firm = (S.meta.billing_firms || []).find(f => f.id === num(party.home_company));
    $('#party-info').innerHTML =
      esc([party.city, party.state].filter(Boolean).join(', ')) +
      (party.gstin ? ' · ' + esc(party.gstin) : ' · Unregistered') +
      (firm ? ' · <b>billed by ' + esc(firm.name) + '</b>'
            : ' · <span class="tag bad">no billing firm set</span>') +
      ' · ' + goLink(accountLink(party.id), 'account', 'Open this customer\'s account');
  }

  /** Customer masters live in the billing firms; search all of them,
   *  preferring a copy already pulled into this firm. */
  const findCustomers = term => crossFirm
    ? api.get('/api/customers',
        { search: term, limit: 40, prefer_company: S.meta.company_id })
    : loadParties().then(all => searchList(term,
        all.filter(p => p.party_kind !== 'supplier'), p => [p.name, p.city]));

  const partyBox = $('#f-party');
  async function takeParty(p) {
    if (crossFirm && p.company_id !== S.meta.company_id) {
      // Bring the customer into the order firm the first time it is used.
      try { p = await api.post('/api/customers/adopt', { ledger_id: p.id }); }
      catch (e) { bad(e.message); return; }
      S.parties = [];
    }
    party = p;
    partyBox.value = p.name;
    showParty();
    grid.repaint();
  }

  const partyPicker = attachPicker(partyBox, {
    source: term => findCustomers(term).then(r => r.slice(0, 30)),
    render: p => `<b>${esc(p.name)}</b><small>${esc([p.city, p.state].filter(Boolean).join(', '))}
      ${p.bill_name ? '· billed by ' + esc(p.bill_name)
        : p.company_name ? '· in ' + esc(p.company_name) : ''}</small>`,
    onPick: takeParty
  });
  // Typing finds a customer you can already name. An empty box opens the
  // list itself, with the town and the billing firm beside every row -- and
  // this one asks the server, so it looks in every firm at once.
  const browseParty = async how => {
    partyPicker.close();
    const p = await chooseParty({ source: findCustomers,
                                  term: () => partyBox.value, typed: !!(how && how.typed) });
    if (p) { await takeParty(p); moveOn(partyBox, view); }
    else partyBox.focus();
    return p;
  };
  $('[data-browse-party]').onclick = browseParty;
  opensPicker(partyBox, browseParty, partyPicker);
  showParty();

  function igstFlag() {
    const home = Gst.stateOf(S.meta.company);
    const away = party ? Gst.stateOf(party) : '';
    return !!(home && away && home !== away);
  }
  const grid = LineGrid($('#grid'), lines, t => {
    // An order records the rate and the number of sheets. Nothing is priced
    // here -- the weighbridge figure on the challan decides the amount.
    // Priced as soon as either figure is in: the Quantity if one is given,
    // otherwise NOS. An order with neither carries no value yet.
    const priced = num(t.taxable) > 0;
    $('#ordtot').innerHTML =
      `<span style="color:var(--muted)">NOS</span> <b class="num">${qty(t.units || 0)}</b>
       &nbsp;·&nbsp; <span style="color:var(--muted)">Quantity</span> <b class="num">${qty(t.weight || 0)}</b>
       &nbsp;·&nbsp; ` + (priced
        ? `<b style="font-size:15px">${fmt(t.taxable)}</b>`
        : `<b style="font-size:15px">Value on weighing</b>
           <span class="tag warn" style="margin-left:8px">enter NOS or a total Quantity</span>`);
  }, { isIgst: igstFlag, mode: 'SALE', noTax: orderFirmNoTax,
       roomy: true,
       hide: ['hsn', 'unit', 'taxable', 'discount_pct', 'total'] });

  // One Enter walk for the whole order. Nothing sits below its grid, so at
  // the last box Enter opens the next line.
  enterMovesOn(view);

  async function save() {
    if (!party) { bad('Select a customer first.'); return; }
    const payload = {
      order_no: $('#f-no').value.trim(), order_date: $('#f-date').value,
      due_date: $('#f-due').value, party_id: party.id, status: o.status || 'open',
      remarks: $('#f-remarks').value,
      lines: lines.filter(l => l.description.trim() || num(l.qty) || num(l.weight))
    };
    try {
      const saved = isNew ? await api.post('/api/orders', payload)
        : await api.put('/api/orders/' + o.id, payload);
      ok('Order ' + saved.order_no + ' saved.');
      showSaved('#/order/' + saved.id, isNew);
    } catch (e) { bad(e.message); }
  }
  $('#save').onclick = save;          // Ctrl+S presses it; see the bill screen
  if (isNew) setTimeout(() => { const b = $('#f-party'); if (b) b.focus(); }, 60);
});

route(/^\/orders$/, async (view, _m, q) => {
  // One order book, shown the same from every firm.
  setTitle('Sales orders',
    'All orders are taken in ' + orderFirmName() + ', waiting to be billed', [
    { label: '＋ New order', cls: 'primary', onClick: () => location.hash = '#/order/new' }
  ]);
  view.innerHTML = `
    <div class="card"><div class="bd"><div class="row">
      <div><label>Status</label><select id="st" style="width:calc(150px * var(--typescale))">
        <option value="open">Open</option><option value="">All</option>
        <option value="invoiced">Invoiced</option><option value="cancelled">Cancelled</option>
      </select></div>
      ${fySelect('fyq', { all: true })}
      <div><label>From</label><input type="date" id="f1" value=""></div>
      <div><label>To</label><input type="date" id="f2" value=""></div>
      <div style="flex:1;min-width:200px;max-width:360px"><label>Search</label>
        <input id="q" placeholder="Order no or party…"></div>
      <div class="spacer"></div>
      <div class="hint" style="margin:0" id="count"></div>
    </div></div></div>
    <div class="card"><div class="bd flush tbl-scroll" id="list"></div></div>`;

  // Every year by default: an open order is waiting whatever year it was
  // taken in, and a date window would hide the old ones.
  const mem = screenState('/orders', {
    status: '#st', from: '#f1', to: '#f2', search: '#q',
    fy: fyField('f1', 'f2')
  }).restore(q);

  async function load() {
    mem.save();
    const here = onSearch();
    const rows = await api.get('/api/orders', {
      status: $('#st').value, from: $('#f1').value, to: $('#f2').value,
      search: $('#q').value.trim()
    });
    if (!here()) return;
    $('#count').textContent = rows.length + ' order(s)';
    $('#list').innerHTML = rows.length ? `<table><thead><tr>
      <th>Date</th><th>Order no</th><th>Customer</th><th>Bill from</th>
      <th>Deliver by</th><th class="c">Status</th><th class="r">Value</th><th class="r">Actions</th>
      </tr></thead><tbody>` +
      rows.map(r => {
        const late = r.status === 'open' && r.due_date && r.due_date < today();
        const firm = r.billed_firm_code || r.bill_firm_code;
        return `<tr class="clickable" data-id="${r.id}">
          <td class="nw">${dt(r.order_date)}</td><td><b>${esc(r.order_no)}</b></td>
          <td>${goLink(accountLink(r.party_id), esc(r.party_name), 'Open the account')}<div style="font-size:11.5px;color:var(--muted)">${esc(r.party_city || '')}</div></td>
          <td>${firm ? '<span class="tag info">' + esc(firm) + '</span>' : '<span class="tag bad">not set</span>'}
            ${r.billed_invoice_no ? '<div style="font-size:11.5px;color:var(--muted)">'
              + goLink(docLink('SALE', r.billed_invoice, r.billed_company),
                       esc(r.billed_invoice_no), 'Open what it was raised as') + '</div>' : ''}</td>
          <td class="nw">${dt(r.due_date)} ${late ? '<span class="tag bad">overdue</span>' : ''}</td>
          <td class="c"><span class="tag ${r.status === 'open' ? 'info'
            : r.status === 'invoiced' ? 'ok' : r.status === 'challaned' ? 'warn' : ''}">${esc(r.status)}</span></td>
          <td class="r num">${num(r.grand_total) ? fmt(r.grand_total)
            : '<span style="color:var(--muted);font-size:11.5px">on weighing</span>'}</td>
          <td class="r" style="white-space:nowrap">${r.status === 'open'
            ? '<button class="link" data-challan>Make challan</button>' : ''}
            <button class="link" data-del style="color:var(--bad)">Delete</button></td></tr>`;
      }).join('') + '</tbody></table>'
      : '<div class="empty"><b>No orders</b>' + ($('#f1').value || $('#f2').value
          ? 'None in these dates — pick another year, or All years.'
          : 'Take an order and it will show up here.') + '</div>';

    $$('#list tr[data-id]').forEach(tr => {
      tr.onclick = e => {
        if (e.target.hasAttribute('data-challan')) {
          challanOrder(num(tr.dataset.id));
          return;
        }
        if (e.target.hasAttribute('data-del')) {
          const no = $('b', tr) ? $('b', tr).textContent : 'this order';
          deleteOrder(num(tr.dataset.id), no, load);
          return;
        }
        location.hash = '#/order/' + tr.dataset.id;
      };
    });
    const total = {};
    rows.forEach(r => { total[r.id] = r.grand_total; });
    bulkRows($('#list'), 'order', { reload: load, amount: tr => total[tr.dataset.id] });
  }
  $('#st').onchange = load;
  bindFy(load, 'f1', 'f2');
  ['#f1', '#f2'].forEach(x => $(x).onchange = load);
  $('#q').oninput = () => { clearTimeout(window._or); window._or = setTimeout(load, 160); };
  load();
});

/* ================================================ receipts & payments

   One dialog for money in and money out, wherever it is opened from -- the
   bank screen, the vouchers list, a person's salary page.

   Reworked 21 Sep 2026 ("make banking more user friendly"):
     - **The other side can be any account.** SHREE books a receipt or a
       payment against whatever the money came from or went to. Of this
       year's Kotak entries 442 are against another bank, and a hundred more
       against salary, expenses, taxes and deposits -- and the box offered
       customers and suppliers only, so none of those could be entered from
       here. Customers and suppliers still come first; the rest of the chart
       follows, each with its group.
     - It says what it is doing -- "Deposit into KOTAK BANK : B. S. TRADERS"
       -- with that account's balance now, and after the entry.
     - With no account chosen it starts on the cash in hand. It used to take
       the first account in the alphabet, a family member's AUSF account, and
       an entry nobody looked at twice went there.
     - The Ch/DD box offers the words SHREE's bank book uses: NEFT, RTGS,
       UPI, CLG, AU ...
     - Save & add another keeps the kind, the account, the date and the mode
       and clears the rest: a bank statement is entered a line at a time. */

// What SHREE's bank book writes in its Ch/DD column. This year's Kotak
// entries: AU 163, CLG 71, NEFT 23, UPI 21, RTGS 6.
const INSTRUMENTS = ['NEFT', 'RTGS', 'IMPS', 'UPI', 'CLG', 'AU', 'CHQ',
                     'SELF', 'CASH'];

function instrumentList(id) {
  return `<datalist id="${id}">${INSTRUMENTS
    .map(x => `<option value="${x}">`).join('')}</datalist>`;
}

/** The next number in the book a voucher dated `date` goes into -- 938 for a
 *  receipt of 2026-27, carrying on from SHREE's own run. Asked without the
 *  year, the server has no book to read and answers the first number of an
 *  empty one: RCP-0001 every time, and the second receipt entered here was
 *  refused as a number already used (found 21 Sep 2026). */
/* **No Voucher no box on any voucher form** (4 Oct 2026: "Remove Voucher
   no from new entry of bank, cash everywhere"). A new receipt, payment,
   transfer or journal takes the next number of its kind and year when it
   is saved -- the server's `next_number`, which the documents decide --
   and the toast says which. One being edited keeps its number, shown in
   the dialog's title. */

function moneyAccount(id) { return S.cashbank.find(b => b.id === num(id)); }

/** The account of the most recent entry, to start a new one on (4 Oct 2026:
 *  "while doing bank entry always autofill it with recent entry so it is
 *  easier because ... it is done bank accountwise"). `cash` true takes only
 *  cash accounts, false only banks, null either. '' when there is none. */
async function recentMoney(cash) {
  await loadCashBank();
  let ids = [];
  try { ids = (await api.get('/api/money-recent')).accounts || []; } catch (e) { return ''; }
  const hit = ids.map(moneyAccount).find(b => b && (cash === null || isCashAccount(b) === cash));
  return hit ? hit.id : '';
}

function isCashAccount(b) {
  return !!b && (b.system_key === 'CASH' || /cash/i.test(b.group_name || ''));
}

/** The cash and bank accounts to choose from, the way they are used: the
 *  cash, then the banks holding money, then the ones with nothing in them --
 *  each with its balance, so the right one is plain before it is picked. */
function moneyOptions(selected, blank) {
  const opt = b => `<option value="${b.id}"${num(selected) === b.id
    ? ' selected' : ''}>${esc(b.name)} — ${drcr(b.balance)}</option>`;
  const live = S.cashbank.filter(b => Math.abs(num(b.balance)) > 0.005);
  const idle = S.cashbank.filter(b => Math.abs(num(b.balance)) <= 0.005);
  const group = (label, list) => list.length
    ? `<optgroup label="${label}">${list.map(opt).join('')}</optgroup>` : '';
  return (blank ? `<option value="">${esc(blank)}</option>` : '')
    + group('Cash', live.filter(isCashAccount))
    + group('Bank', live.filter(b => !isCashAccount(b)))
    + group('Nothing in them', idle);
}

/** An account an old entry names that is no longer on the list -- made
 *  inactive since -- stays selectable, rather than the box quietly falling to
 *  the first account and the entry moving there when it is saved. */
function keepOwn(id, name) {
  return id && !moneyAccount(id)
    ? `<option value="${num(id)}" selected>${esc(name || 'Account ' + id)}</option>` : '';
}

/** What deleting a voucher asks, naming it: "Delete receipt 938
 *  (5,000.00)?" -- the list used to ask "Delete 938?" (29 Sep 2026). */
function voucherDeleteText(v) {
  const word = { RECEIPT: 'receipt', PAYMENT: 'payment', JOURNAL: 'journal',
                 CONTRA: 'contra' }[v.kind] || 'voucher';
  const amt = num(v.amount)
    || (v.lines || []).reduce((s, l) => s + num(l.debit), 0);
  return 'Delete ' + word + ' ' + v.voucher_no + (amt ? ' (' + fmt(amt) + ')' : '')
    + '? The balances it moved go back.';
}

/** Delete a receipt, payment or transfer from its own dialog, as the
 *  vouchers list does: asked first, and one from SHREE offered to be kept
 *  deleted. A wrong entry found on a bank statement can go from there. */
async function deleteVoucherAsk(v) {
  const fromShree = v.source === 'SHREE';
  const ans = await askDelete('Delete voucher', voucherDeleteText(v), fromShree);
  if (!ans) return false;
  try { await api.del('/api/vouchers/' + v.id + (ans.keep ? '?keep=1' : '')); }
  catch (e) { bad(e.message); return false; }
  invalidate();
  ok(v.voucher_no + ' deleted.' + (fromShree && !ans.keep
    ? ' The next SHREE update brings it back.' : ''));
  return true;
}

/** Enter or edit a receipt or payment. Answers the last voucher saved -- or,
 *  when the one open was deleted, `{deleted: true}` -- and null when nothing
 *  changed. */
async function editVoucher(existing, defKind) {
  let v = existing || {};
  let kind = v.kind || defKind || 'RECEIPT';
  let said = '', last = null;
  for (;;) {
    const got = await voucherDialog(v, kind, said);
    if (!got) break;
    if (got.deleted) return { deleted: true, id: v.id };
    last = got.saved;
    invalidate();
    ok(got.text);
    if (!got.again) break;
    // The next one starts where this one left off.
    kind = got.saved.kind;
    v = { from_bank: v.from_bank, bank_id: got.saved.bank_id,
          mode: got.saved.mode, voucher_date: got.saved.voucher_date };
    said = got.text;
  }
  return last;
}

async function voucherDialog(v, kind, said) {
  await Promise.all([loadLedgers(), loadCashBank()]);
  const editing = !!v.id;
  const IN = k => k === 'RECEIPT';
  const cash = cashInHand();
  const bankSel = v.bank_id || (cash && cash.id) || '';
  const mode = v.mode || (isCashAccount(moneyAccount(bankSel)) ? 'Cash' : 'Bank');
  const titleFor = (k, id) => {
    if (editing) return (IN(k) ? 'Receipt ' : 'Payment ') + (v.voucher_no || '');
    const a = moneyAccount(id);
    if (!a) return IN(k) ? 'New receipt' : 'New payment';
    if (isCashAccount(a)) return IN(k) ? 'Cash received' : 'Cash paid out';
    return (IN(k) ? 'Deposit into ' : 'Withdrawal from ') + a.name;
  };
  const html = `
    ${said ? `<div class="v-saved">✓ ${esc(said)}. Now the next one.</div>` : ''}
    <div class="grid g2">
      <div><label>Type</label><select name="kind" ${editing ? 'disabled' : ''}>
        <option value="RECEIPT"${IN(kind) ? ' selected' : ''}>Receipt — money in</option>
        <option value="PAYMENT"${IN(kind) ? '' : ' selected'}>Payment — money out</option>
      </select></div>
      <div><label>Date</label><input type="date" name="voucher_date" value="${esc(v.voucher_date || today())}"></div>
    </div>
    <div class="pick" style="margin-top:12px"><label id="v-party-lb">${IN(kind) ? 'Received from' : 'Paid to'} *</label>
      <div class="pickbox">
        <input id="v-party" autocomplete="off" value="${esc(v.party_name || '')}"
          placeholder="Customer, supplier, another bank, salary, an expense…">
        <button type="button" data-browse-party class="pickbtn" tabindex="-1"
          title="Browse every account (F4)">▾</button></div>
      <input type="hidden" name="party_id" value="${esc(v.party_id || '')}">
      <div class="hint" id="v-info">&nbsp;</div></div>
    <div class="grid g4" style="margin-top:12px">
      <div><label>Amount *</label><input name="amount" class="num" placeholder="0.00"
        value="${num(v.amount) ? num(v.amount).toFixed(2) : ''}"></div>
      <div class="sp2"><label id="v-bank-lb">${IN(kind) ? 'Into' : 'Out of'}</label>
        <select name="bank_id">${keepOwn(bankSel, v.bank_name)}${moneyOptions(bankSel)}</select>
        <div class="hint" id="v-bank-info">&nbsp;</div></div>
      <div><label>Mode</label><select name="mode">
        ${['Cash', 'Bank', 'UPI', 'Cheque', 'Card', 'Adjustment']
          .map(m => `<option${mode === m ? ' selected' : ''}>${m}</option>`).join('')}
      </select></div>
    </div>
    <div class="grid g4" style="margin-top:12px">
      <div class="sp2"><label>Ch/DD No.</label>
        <input name="ref_no" list="v-instr" value="${esc(v.ref_no || '')}"
          placeholder="NEFT, UPI, CLG, a cheque number…"
          title="What the money moved on, as the bank book prints it beside the entry">
        ${instrumentList('v-instr')}</div>
    </div>
    <div style="margin-top:12px"><label>Narration</label>
      <textarea name="remarks" placeholder="Printed under the entry in the bank book">${esc(v.remarks || '')}</textarea></div>`;

  let saved = null, whoName = '';
  const save = async bg => {
    const data = readForm(bg);
    data.amount = String(data.amount || '').replace(/[,\s₹]/g, '');
    if (!num(data.party_id)) {
      bad(IN(data.kind) ? 'Pick who the money came from.' : 'Pick who the money went to.');
      $('#v-party', bg).focus();
      return false;
    }
    if (num(data.party_id) === num(data.bank_id)) {
      bad('The money cannot come from and go to the same account.');
      return false;
    }
    if (!(num(data.amount) > 0)) {
      bad('Enter the amount.');
      $('[name=amount]', bg).focus();
      return false;
    }
    whoName = $('#v-party', bg).value.trim();
    try {
      saved = await (editing ? api.put('/api/vouchers/' + v.id, data)
        : api.post('/api/vouchers', data));
    } catch (e) { bad(e.message); return false; }
  };
  const buttons = [];
  if (editing) {
    buttons.push({ label: 'Delete', cls: 'danger', value: 'deleted',
                   before: async () => { if (!await deleteVoucherAsk(v)) return false; } });
  }
  buttons.push({ label: 'Cancel', value: null });
  if (!editing) buttons.push({ label: 'Save & add another', value: 'again', before: save });
  buttons.push({ label: 'Save', cls: 'primary', value: 'done', before: save });

  const answer = await modal(titleFor(kind, bankSel), html, buttons, {
    onOpen: bg => {
      const kindSel = $('[name=kind]', bg);
      const bankBox = $('[name=bank_id]', bg);
      const modeSel = $('[name=mode]', bg);
      const amt = $('[name=amount]', bg);
      const head = $('h2', bg).firstChild;
      const bankInfo = () => {
        const a = moneyAccount(bankBox.value);
        if (!a) { $('#v-bank-info', bg).innerHTML = '&nbsp;'; return; }
        const now = num(a.balance);
        const n = num(String(amt.value).replace(/[,\s₹]/g, ''));
        $('#v-bank-info', bg).textContent = 'Balance now ' + drcr(now)
          + (!editing && n > 0
            ? ' · after this ' + drcr(IN(kindSel.value) ? now + n : now - n) : '');
      };
      const relabel = () => {
        const k = kindSel.value;
        head.nodeValue = titleFor(k, bankBox.value);
        $('#v-party-lb', bg).textContent = (IN(k) ? 'Received from' : 'Paid to') + ' *';
        $('#v-bank-lb', bg).textContent = IN(k) ? 'Into' : 'Out of';
        bankInfo();
      };
      if (!editing) kindSel.onchange = relabel;
      // The way the money moves follows the account it moves through.
      bankBox.onchange = () => {
        if (isCashAccount(moneyAccount(bankBox.value))) modeSel.value = 'Cash';
        else if (modeSel.value === 'Cash') modeSel.value = 'Bank';
        relabel();
      };
      amt.addEventListener('input', bankInfo);
      relabel();

      const vBox = $('#v-party', bg);
      const pid = $('[name=party_id]', bg);
      let picked = vBox.value;
      const describe = p => {
        const b = num(p.balance);
        $('#v-info', bg).textContent = p.party_kind
          ? 'Outstanding: ' + fmt(Math.abs(b)) + (b > 0 ? ' receivable' : b < 0 ? ' payable' : '')
          : (p.group_name || 'Account') + ' · balance ' + drcr(b);
      };
      const takeV = p => {
        vBox.value = picked = p.name;
        pid.value = p.id;
        describe(p);
        // A customer's or supplier's whole balance is the likeliest amount;
        // a bank's or an expense head's never is.
        if (p.party_kind && !num(amt.value) && num(p.balance)) {
          amt.value = Math.abs(num(p.balance)).toFixed(2);
          bankInfo();
        }
      };
      const shown = S.ledgers.find(x => x.id === num(pid.value));
      if (shown) describe(shown);
      // A name typed over the one picked no longer names that account.
      vBox.addEventListener('input', () => {
        if (vBox.value !== picked) { pid.value = ''; $('#v-info', bg).innerHTML = '&nbsp;'; }
      });
      const vPicker = attachPicker(vBox, {
        source: async term => {
          const all = await loadLedgers();
          const skip = num(bankBox.value);
          // Customers and suppliers first wherever the name reads as well;
          // a closer match on any other account still leads.
          const pool = all.filter(x => x.party_kind && x.id !== skip)
            .concat(all.filter(x => !x.party_kind && x.id !== skip));
          return searchList(term, pool, p => [p.name, p.city, p.group_name, p.code]);
        },
        render: p => `<b>${esc(p.name)}</b><small>${esc(p.party_kind
          ? [p.city, p.party_kind === 'supplier' ? 'supplier' : ''].filter(Boolean).join(' · ')
          : p.group_name || '')} · ${drcr(num(p.balance))}</small>`,
        onPick: takeV
      });
      const browseV = async how => {
        vPicker.close();
        const p = await chooseParty({
          ledgers: true, term: () => vBox.value, typed: !!(how && how.typed),
          title: IN(kindSel.value) ? 'Received from — any account' : 'Paid to — any account'
        });
        if (p) { takeV(p); moveOn(vBox, bg); } else vBox.focus();
        return p;
      };
      $('[data-browse-party]', bg).onclick = browseV;
      opensPicker(vBox, browseV, vPicker);
    }
  });
  if (answer === 'deleted') return { deleted: true };
  if (!answer || !saved) return null;
  const text = (IN(saved.kind) ? 'Receipt ' : 'Payment ') + saved.voucher_no + ' saved — '
    + fmt(saved.amount) + (IN(saved.kind) ? ' from ' : ' to ') + (saved.party_name || whoName);
  return { saved, again: answer === 'again', text };
}

/* ------------------------------------------------ moving your own money --
   Cash paid into the bank, cash drawn out, one bank to another: a contra
   voucher, entered as From and To rather than as a debit and a credit --
   the account the money arrives in is debited, the one it leaves credited.
   (21 Sep 2026. The Transfer button used to open the journal grid.) */
async function editTransfer(preset) {
  const p = preset || {};
  await loadCashBank();
  const editing = !!p.id;
  let from = p.from || '', to = p.to || '', amount = num(p.amount);
  if (editing) {
    const cr = (p.lines || []).find(l => num(l.credit) > 0);
    const dr = (p.lines || []).find(l => num(l.debit) > 0);
    from = cr ? cr.ledger_id : '';
    to = dr ? dr.ledger_id : '';
    amount = num(p.amount) || (dr ? num(dr.debit) : 0);
  } else if (!from) {
    const cash = cashInHand();
    from = cash ? cash.id : '';
  }
  const nameOf = id => ((p.lines || []).find(l => num(l.ledger_id) === num(id)) || {}).ledger_name;
  const html = `
    <div class="grid g2">
      <div><label>Date</label><input type="date" name="voucher_date" value="${esc(p.voucher_date || today())}"></div>
      <div><label>Amount *</label><input name="amount" class="num" placeholder="0.00"
        value="${amount ? amount.toFixed(2) : ''}"></div>
    </div>
    <div class="xfer">
      <div><label>From — the money leaves</label>
        <select name="from">${keepOwn(from, nameOf(from))}${moneyOptions(from, 'Choose an account…')}</select>
        <div class="hint" id="x-from">&nbsp;</div></div>
      <button type="button" class="sm" id="x-swap"
        title="Swap From and To">⇄</button>
      <div><label>To — the money arrives</label>
        <select name="to">${keepOwn(to, nameOf(to))}${moneyOptions(to, 'Choose an account…')}</select>
        <div class="hint" id="x-to">&nbsp;</div></div>
    </div>
    <div class="grid g4" style="margin-top:12px">
      <div class="sp2"><label>Ch/DD No.</label>
        <input name="ref_no" list="x-instr" value="${esc(p.ref_no || '')}"
          placeholder="NEFT, SELF, a cheque number…">${instrumentList('x-instr')}</div>
    </div>
    <div style="margin-top:12px"><label>Narration</label>
      <textarea name="remarks" placeholder="Cash deposited, withdrawn for wages…">${esc(p.remarks || '')}</textarea></div>`;

  let saved = null;
  const answer = await modal(editing ? 'Transfer ' + p.voucher_no
    : 'Transfer between your own accounts', html, [
    ...(editing ? [{ label: 'Delete', cls: 'danger', value: 'deleted',
                     before: async () => { if (!await deleteVoucherAsk(p)) return false; } }]
      : []),
    { label: 'Cancel', value: null },
    {
      label: 'Save', cls: 'primary', value: true, before: async bg => {
        const d = readForm(bg);
        const amt = num(String(d.amount || '').replace(/[,\s₹]/g, ''));
        if (!num(d.from) || !num(d.to)) { bad('Choose both accounts.'); return false; }
        if (num(d.from) === num(d.to)) { bad('From and To are the same account.'); return false; }
        if (!(amt > 0)) { bad('Enter the amount.'); $('[name=amount]', bg).focus(); return false; }
        const body = {
          kind: 'CONTRA', voucher_date: d.voucher_date,
          ref_no: d.ref_no, remarks: d.remarks, mode: 'Bank',
          lines: [{ ledger_id: num(d.to), debit: amt, credit: 0 },
                  { ledger_id: num(d.from), debit: 0, credit: amt }]
        };
        try {
          saved = await (editing ? api.put('/api/vouchers/' + p.id, body)
            : api.post('/api/vouchers', body));
        } catch (e) { bad(e.message); return false; }
      }
    }
  ], {
    onOpen: bg => {
      const f = $('[name=from]', bg), t = $('[name=to]', bg), a = $('[name=amount]', bg);
      const info = () => {
        const n = num(String(a.value).replace(/[,\s₹]/g, ''));
        [[f, '#x-from', -1], [t, '#x-to', 1]].forEach(([box, id, sign]) => {
          const acc = moneyAccount(box.value);
          $(id, bg).textContent = acc ? 'Balance now ' + drcr(acc.balance)
            + (!editing && n > 0 ? ' · after this ' + drcr(num(acc.balance) + sign * n) : '')
            : '';
        });
      };
      f.onchange = t.onchange = info;
      a.addEventListener('input', info);
      $('#x-swap', bg).onclick = () => { const x = f.value; f.value = t.value; t.value = x; info(); };
      info();
    }
  });
  if (answer === 'deleted') return { deleted: true, id: p.id };
  if (!answer || !saved) return null;
  invalidate();
  ok('Transfer ' + saved.voucher_no + ' saved — ' + fmt(saved.amount));
  return saved;
}

/** A contra that is one account to another, which the transfer dialog can
 *  show; anything with more lines stays on the journal screen. */
function isSimpleTransfer(v) {
  const lines = (v && v.lines) || [];
  return v && v.kind === 'CONTRA' && lines.length === 2
    && lines.filter(l => num(l.debit) > 0).length === 1
    && lines.filter(l => num(l.credit) > 0).length === 1;
}

/* ------------------------------------------------- journal & contra ---- */
route(/^\/voucher\/(new|\d+)$/, async (view, m, q) => {
  const isNew = m[0] === 'new';
  const asked = (q.kind || '').toUpperCase();
  // The screen this route draws is the journal. A receipt, a payment or a
  // transfer between two of the firm's own accounts is entered in a dialog,
  // so ask for one by URL and the dialog is what opens.
  if (isNew && (asked === 'RECEIPT' || asked === 'PAYMENT' || asked === 'CONTRA')) {
    const here = location.hash;
    setTitle('Vouchers', 'Receipts, payments, journals and contras');
    view.innerHTML = '<div class="empty">Opening the entry form…</div>';
    await (asked === 'CONTRA' ? editTransfer() : editVoucher(null, asked));
    // The dialog also closes when the menu is used, and that click has
    // already chosen where to go -- sending it anywhere from here would
    // overrule it.
    if (location.hash === here) leave('#/vouchers');
    return;
  }
  const current = onScreen();
  await loadLedgers(true);
  if (!current()) return;

  let v, lines;
  if (isNew) {
    const kind = (q.kind || 'JOURNAL').toUpperCase();
    v = { kind, voucher_date: today(), remarks: '', ref_no: '' };
    lines = [blankJLine(), blankJLine()];
    // Raised from an account: that account on the first line.
    const from = q.ledger_id && S.ledgers.find(l => l.id === num(q.ledger_id));
    if (from) lines[0] = Object.assign(blankJLine(), { ledger_id: from.id, ledger_name: from.name });
  } else {
    v = await api.get('/api/vouchers/' + m[0]);
    if (!current()) return;
    if (v.kind === 'RECEIPT' || v.kind === 'PAYMENT') {
      // Reached from a customer's account as often as from the voucher
      // list, and closing it belongs back on whichever it was.
      const here = location.hash;
      await editVoucher(v);
      if (location.hash === here) leave('#/vouchers');
      return;
    }
    // One account to another opens as a transfer; a contra of more lines
    // stays on the journal grid below.
    if (isSimpleTransfer(v)) {
      const here = location.hash;
      await editTransfer(v);
      if (location.hash === here) leave('#/vouchers');
      return;
    }
    lines = v.lines.map(l => ({
      ledger_id: l.ledger_id, ledger_name: l.ledger_name,
      debit: l.debit, credit: l.credit, remarks: l.remarks || ''
    }));
    if (!lines.length) lines = [blankJLine(), blankJLine()];
  }
  if (!current()) return;

  const title = v.kind === 'CONTRA' ? 'Contra voucher' : 'Journal voucher';
  setTitle(isNew ? 'New ' + title.toLowerCase() : title + ' ' + v.voucher_no,
    v.kind === 'CONTRA'
      ? 'Move money between your own cash and bank accounts'
      : 'Any adjustment entry — debits must equal credits',
    isNew ? [] : [{
      // Asked as every other voucher delete is -- one from SHREE can be kept
      // deleted -- and a refusal is said: it used to fail without a word.
      label: 'Delete', cls: 'danger', onClick: async () => {
        const fromShree = v.source === 'SHREE';
        const ans = await askDelete('Delete voucher', voucherDeleteText(v), fromShree);
        if (!ans) return;
        try { await api.del('/api/vouchers/' + v.id + (ans.keep ? '?keep=1' : '')); }
        catch (e) { bad(e.message); return; }
        invalidate();
        ok(v.voucher_no + ' deleted.' + (fromShree && !ans.keep
          ? ' The next SHREE update brings it back.' : ''));
        leave('#/vouchers');
      }
    }]);

  view.innerHTML = `
    <div class="card"><h2>${esc(title)}</h2><div class="bd">
      <div class="grid g3">
        <div><label>Type</label><select id="j-kind" ${isNew ? '' : 'disabled'}>
          <option value="JOURNAL"${v.kind === 'JOURNAL' ? ' selected' : ''}>Journal</option>
          <option value="CONTRA"${v.kind === 'CONTRA' ? ' selected' : ''}>Contra</option>
        </select></div>
        <div><label>Date</label><input type="date" id="j-date" value="${esc(v.voucher_date)}"></div>
        <div><label>Reference</label><input id="j-ref" value="${esc(v.ref_no || '')}"></div>
      </div>
    </div></div>
    <div class="card"><h2>Entries</h2><div class="bd">
      <div class="tbl-scroll"><table class="lineitems"><thead><tr>
        <th style="width:32px"></th><th style="min-width:260px">Ledger account</th>
        <th style="min-width:150px">Note</th>
        <th style="width:130px" class="r">Debit</th>
        <th style="width:130px" class="r">Credit</th><th style="width:36px"></th>
      </tr></thead><tbody id="j-body"></tbody>
      <tfoot><tr><td colspan="3">Totals</td>
        <td class="r num" id="j-dr">0.00</td><td class="r num" id="j-cr">0.00</td><td></td></tr>
      </tfoot></table></div>
      <div class="row" style="margin-top:10px">
        <button type="button" id="j-add">＋ Add line</button>
        <div class="spacer"></div><div id="j-diff"></div>
      </div>
    </div></div>
    <div class="card"><div class="bd">
      <label>Narration</label><textarea id="j-remarks">${esc(v.remarks || '')}</textarea>
      <div class="row" style="margin-top:14px"><div class="spacer"></div>
        <button class="primary" id="j-save">${isNew ? 'Save' : 'Update'} voucher
          <span class="kbd">Ctrl+S</span></button></div>
    </div></div>`;

  function drawLines() {
    const body = $('#j-body');
    body.innerHTML = '';
    lines.forEach((l, i) => body.appendChild(lineRow(l, i)));
    paint();
  }
  function lineRow(l, idx) {
    const tr = el(`<tr>
      <td class="c" style="color:var(--muted);font-size:11px">${idx + 1}</td>
      <td class="pick"><div class="pickbox">
        <input data-f="ledger" value="${esc(l.ledger_name || '')}" placeholder="Type to search ledgers…">
        <button type="button" data-browse-party class="pickbtn" tabindex="-1"
          title="Browse the chart of accounts and pick the ledger">▾</button></div></td>
      <td><input data-f="remarks" value="${esc(l.remarks || '')}"></td>
      <td><input data-f="debit" class="num" value="${l.debit || ''}"></td>
      <td><input data-f="credit" class="num" value="${l.credit || ''}"></td>
      <td class="c"><button type="button" class="link" data-del style="color:var(--bad)">✕</button></td>
    </tr>`);
    $('[data-f=remarks]', tr).oninput = e => { l.remarks = e.target.value; };
    ['debit', 'credit'].forEach(side => {
      const inp = $(`[data-f=${side}]`, tr);
      inp.oninput = () => {
        l[side] = num(inp.value);
        if (l[side]) {                        // a line is one side only
          const other = side === 'debit' ? 'credit' : 'debit';
          l[other] = 0;
          $(`[data-f=${other}]`, tr).value = '';
        }
        paint();
      };
    });
    $('[data-del]', tr).onclick = () => {
      lines.splice(idx, 1);
      while (lines.length < 2) lines.push(blankJLine());
      drawLines();
      // Redrawing throws the cursor away, so put it on the line that has
      // taken this one's place -- otherwise the focus falls to the page body
      // and Tab starts again from the top of the window.
      // An empty ledger box opens the chart of accounts as soon as it is
      // reached, which is right on a line being entered and a surprise on one
      // that has just been cleared away. So land on the ledger only when it
      // already names one, and on the Debit box otherwise.
      const row = $$('#j-body tr')[Math.min(idx, lines.length - 1)];
      if (row) {
        const led = $('[data-f=ledger]', row);
        (led.value.trim() ? led : $('[data-f=debit]', row)).focus();
      }
    };
    const ledBox = $('[data-f=ledger]', tr);
    const takeLedger = x => {
      l.ledger_id = x.id;
      l.ledger_name = x.name;
      ledBox.value = x.name;
      $('[data-f=debit]', tr).focus();
    };
    const ledPicker = attachPicker(ledBox, {
      source: async term => {
        const all = await loadLedgers();
        return searchList(term, all, x => [x.name, x.group_name]);
      },
      render: x => `<b>${esc(x.name)}</b><small>${esc(x.group_name)} · balance
        ${fmt(Math.abs(num(x.balance)))} ${num(x.balance) > 0 ? 'Dr' : num(x.balance) < 0 ? 'Cr' : ''}</small>`,
      onPick: takeLedger
    });
    // The same way in as the product box on a document: an empty ledger box
    // opens the whole chart of accounts as soon as it is reached, and F4
    // opens it again on a line that already names one. Without this the only
    // way to the full list was the ▾ beside the box, which carries
    // tabindex="-1" and so could only ever be clicked.
    const browseLedger = async how => {
      ledPicker.close();
      const x = await chooseParty({ ledgers: true, term: () => ledBox.value,
                                    typed: !!(how && how.typed) });
      if (x) takeLedger(x); else ledBox.focus();
      return x;
    };
    $('[data-browse-party]', tr).onclick = browseLedger;
    opensPicker(ledBox, browseLedger, ledPicker);

    return tr;
  }
  function paint() {
    const dr = lines.reduce((a, l) => a + num(l.debit), 0);
    const cr = lines.reduce((a, l) => a + num(l.credit), 0);
    $('#j-dr').textContent = fmt(dr);
    $('#j-cr').textContent = fmt(cr);
    const diff = r2diff(dr, cr);
    $('#j-diff').innerHTML = diff === 0
      ? '<span class="tag ok">balanced</span>'
      : `<span class="tag bad">out by ${fmt(Math.abs(diff))}
         ${diff > 0 ? '(more debit)' : '(more credit)'}</span>`;
  }
  function r2diff(a, b) { return Math.round((a - b) * 100) / 100; }

  /** Add an entry and land on it: the button, and Enter past the last box. */
  function addJLine() {
    lines.push(blankJLine());
    drawLines();
    const row = $$('#j-body tr')[lines.length - 1];
    if (row) $('[data-f=ledger]', row).focus();
  }
  $('#j-add').onclick = addJLine;
  $('#j-kind').onchange = () => {
    location.hash = '#/voucher/new?kind=' + $('#j-kind').value;
  };
  drawLines();
  // The journal was the one entry screen Enter did not walk: it had to be
  // tabbed, box by box, while every other document moved on by itself.
  enterMovesOn(view);

  async function save() {
    const payload = {
      kind: $('#j-kind').value,
      voucher_date: $('#j-date').value, ref_no: $('#j-ref').value.trim(),
      remarks: $('#j-remarks').value,
      lines: lines.filter(l => l.ledger_id && (num(l.debit) || num(l.credit)))
    };
    try {
      const saved = isNew ? await api.post('/api/vouchers', payload)
        : await api.put('/api/vouchers/' + v.id, payload);
      ok('Voucher ' + saved.voucher_no + ' saved.');
      invalidate();
      showSaved('#/voucher/' + saved.id, isNew);
    } catch (e) { bad(e.message); }
  }
  $('#j-save').onclick = save;        // Ctrl+S presses it; see the bill screen
});

function blankJLine() {
  return { ledger_id: null, ledger_name: '', debit: 0, credit: 0, remarks: '' };
}

route(/^\/vouchers$/, async (view, _m, q) => {
  setTitle('Vouchers', 'Receipts, payments, journals and contras', [
    { label: '＋ Receipt', cls: 'primary',
      onClick: async () => { if (await editVoucher({ bank_id: await recentMoney(null) }, 'RECEIPT')) render(); } },
    { label: '＋ Payment',
      onClick: async () => { if (await editVoucher({ bank_id: await recentMoney(null) }, 'PAYMENT')) render(); } },
    { label: '＋ Journal', onClick: () => location.hash = '#/voucher/new?kind=JOURNAL' },
    { label: '＋ Contra', onClick: () => location.hash = '#/voucher/new?kind=CONTRA' }
  ]);
  view.innerHTML = `
    <div class="card"><div class="bd"><div class="row">
      <div><label>Type</label><select id="k" style="width:calc(150px * var(--typescale))">
        <option value="">All</option><option value="RECEIPT">Receipts</option>
        <option value="PAYMENT">Payments</option><option value="JOURNAL">Journals</option>
        <option value="CONTRA">Contras</option></select></div>
      ${fySelect('fyq')}
      <div><label>From</label><input type="date" id="f1" value="${fyStart()}"></div>
      <div><label>To</label><input type="date" id="f2" value="${today()}"></div>
      <div style="flex:1;min-width:180px"><label>Search</label><input id="q" placeholder="No, party, ref or note…"></div>
    </div></div></div>
    <div class="card"><div class="bd flush tbl-scroll" id="list"></div></div>`;

  const mem = screenState('/vouchers', {
    kind: '#k', from: '#f1', to: '#f2', search: '#q',
    fy: fyField('f1', 'f2')
  }).restore(q);

  async function load() {
    mem.save();
    const here = onSearch();
    const rows = await api.get('/api/vouchers', {
      kind: $('#k').value, from: $('#f1').value, to: $('#f2').value,
      search: $('#q').value.trim()
    });
    if (!here()) return;
    const rec = rows.filter(r => r.kind === 'RECEIPT').reduce((a, r) => a + num(r.amount), 0);
    const pay = rows.filter(r => r.kind === 'PAYMENT').reduce((a, r) => a + num(r.amount), 0);
    const tagFor = k => k === 'RECEIPT' ? 'ok' : k === 'PAYMENT' ? 'bad' : 'info';
    if (!rows.length) {
      $('#list').innerHTML = '<div class="empty"><b>No vouchers</b>Record a receipt when a customer pays you.</div>';
      bulkRows($('#list'), 'voucher', { reload: load });
      return;
    }
    const bind = b => {
      b.onclick = async ev => {
        const tr = ev.target.closest('tr');
        const id = num(tr.dataset.id);
        const row = rows.find(r => r.id === id);
        if (b.dataset.act === 'edit') {
          if (row.kind === 'JOURNAL' || row.kind === 'CONTRA') {
            location.hash = '#/voucher/' + id;
          } else if (await editVoucher(row)) { load(); }
          return;
        }
        const shreeRow = row.source === 'SHREE';
        const ans = await askDelete('Delete voucher', voucherDeleteText(row), shreeRow);
        if (!ans) return;
        try { await api.del('/api/vouchers/' + id + (ans.keep ? '?keep=1' : '')); }
        catch (e) { bad(e.message); return; }
        invalidate();
        ok(row.voucher_no + ' deleted.' + (shreeRow && !ans.keep
          ? ' The next SHREE update brings it back.' : ''));
        load();
      };
    };
    const total = {};
    rows.forEach(r => { total[r.id] = r.amount; });
    await fillRows($('#list'), `<table><thead><tr>
      <th>Date</th><th>Voucher</th><th class="c">Type</th><th>Party / entries</th>
      <th>Mode / ref</th><th class="r">Received</th><th class="r">Paid</th><th class="r">Actions</th>
      </tr></thead><tbody>`,
      rows.map(r => `<tr data-id="${r.id}" data-kind="${r.kind}">
        <td class="nw">${dt(r.voucher_date)}</td><td><b>${esc(r.voucher_no)}</b></td>
        <td class="c"><span class="tag ${tagFor(r.kind)}">${esc(r.kind.toLowerCase())}</span></td>
        <td>${r.party_name ? goLink(accountLink(r.party_id), esc(r.party_name), 'Open the account')
          : (r.lines || []).map(l => goLink(accountLink(l.ledger_id, true),
              esc(l.ledger_name), 'Open the account')).join(' / ') || esc(r.remarks || '')}
          ${r.bank_name ? '<div style="font-size:11.5px;color:var(--muted)">' + esc(r.bank_name) + '</div>' : ''}</td>
        <td>${esc(r.mode || '')}${r.ref_no ? ' · ' + esc(r.ref_no) : ''}</td>
        <td class="r num" style="color:var(--ok)">${r.kind === 'RECEIPT' ? fmt(r.amount) : ''}</td>
        <td class="r num" style="color:var(--bad)">${r.kind === 'PAYMENT' ? fmt(r.amount) : ''}</td>
        <td class="r" style="white-space:nowrap">
          <button class="link" data-act="edit" data-enter>Edit</button>
          <button class="link" data-act="del" style="color:var(--bad)">Del</button></td></tr>`),
      `</tbody><tfoot><tr><td colspan="5">${rows.length} voucher(s)</td>
        <td class="r num">${fmt(rec)}</td><td class="r num">${fmt(pay)}</td><td></td></tr></tfoot></table>`, {
        here,
        each: trs => trs.forEach(tr => $$('button[data-act]', tr).forEach(bind)),
        first: () => bulkRows($('#list'), 'voucher', { reload: load, amount: tr => total[tr.dataset.id] })
      });
  }
  bindFy(load, 'f1', 'f2');
  ['#k', '#f1', '#f2'].forEach(sel => $(sel).onchange = load);
  $('#q').oninput = () => { clearTimeout(window._vc); window._vc = setTimeout(load, 160); };
  load();
});

/* =============================================================== reports */
/* ============================================================= the year --
   **Bills, challans and accounts can always be read a year at a time.**
   Asked for on 17 Sep 2026, and it holds for every screen with dates on it:
   a new one gets `fySelect` + `bindFy` + `fyField`, never bare date boxes.
   SHREE opens a financial year and shows that year; this app opens on dates,
   so the Year box sits beside them and the two are kept in step both ways.

     a year          1 April to 31 March, the whole of it -- SHREE posts the
                     loan interest for the months ahead, and a year that
                     stopped at today would hide those entries
     2026-27 to date 1 April to today, which is what most lists open on
     Today, This month (to today), Last month (all of it) -- since 3 Oct
                     2026, on every screen with From and To but Accounts
     All years       both boxes empty, on the screens that can draw that much
     Custom          whatever was typed; picking it changes nothing

   On an as-on screen (no From box) a year means as on its 31 March, and
   "Today" takes the place of "to date".

   **The box is read off the dates, never trusted on its own**, so a default,
   a remembered range and dates set by code all show the year they are. The
   address keeps only what dates cannot: "to date" (Today, This month, Last
   month too) has to mean the day the screen is opened again, and "All
   years" is two empty boxes, which `screenState` does not write down.     */

/** Every year from the oldest thing in any firm to now, newest first. One
 *  unbroken run, so a year one firm has nothing in reads as an empty year
 *  rather than a missing one -- and the Bills screen, which reads both GST
 *  firms, is offered BST's early years whichever firm is open. */
function fyChoices() {
  const ys = (S.meta.fys || []).concat(S.meta.first_fy || [], fyLabel(today()))
    .map(f => parseInt(f, 10)).filter(y => y > 1900);
  const out = [];
  for (let y = Math.max(...ys); y >= Math.min(...ys); y--)
    out.push(y + '-' + String((y + 1) % 100).padStart(2, '0'));
  return out;
}
/** Newest year that actually holds data, else the current one. */
function defaultFy() {
  return (S.meta.fys && S.meta.fys[0]) || fyLabel(today());
}
function fyRange(label) {
  const y = parseInt(label.slice(0, 4), 10);
  return { from: y + '-04-01', to: (y + 1) + '-03-31' };
}
/** The Year box. `o.all` offers "All years" (or the label given); `o.asOn`
 *  is for a screen with one date, read as the day balances stand on.
 *
 *  Since 3 Oct 2026 it is the Period box on a screen with From and To: today,
 *  this month and last month sit under "to date", ahead of the whole years
 *  -- a day's challans or a month's bills were two dates typed by hand.
 *  `o.quick === false` leaves them out (Accounts, which reads months). */
function fySelect(id, o) {
  o = o || {};
  const quick = !o.asOn && o.quick !== false;
  return `<div><label>${quick ? 'Period' : 'Year'}</label><select id="${id}" data-fy
      style="width:calc(${quick ? 175 : 165}px * var(--typescale))"
      title="${o.asOn ? 'As on the last day of a financial year'
               : quick ? 'Today, this month, last month, or a whole financial year (1 April to 31 March)'
               : 'A whole financial year, 1 April to 31 March'}">
    <option value="now">${o.asOn ? 'Today' : fyLabel(today()) + ' to date'}</option>
    ${quick ? `<option value="today">Today</option>
    <option value="month">This month</option>
    <option value="lastmonth">Last month</option>` : ''}
    ${quick ? '<optgroup label="Whole year">' : ''}
    ${fyChoices().map(f => `<option value="${f}">${f}</option>`).join('')}
    ${quick ? '</optgroup>' : ''}
    ${o.all ? `<option value="all">${o.all === true ? 'All years' : esc(o.all)}</option>` : ''}
    <option value="">Custom</option>
  </select></div>`;
}
/** The choices of the Period box that move with the calendar, kept in the
 *  address as words so that "Today" opened tomorrow is tomorrow. */
const FY_RELATIVE = ['now', 'all', 'today', 'month', 'lastmonth'];

/** What the date boxes say, as the Year box would put it. `from` is null on
 *  an as-on screen. A full year is asked first: on 31 March the year and
 *  "to date" are the same dates, and the year is the better name for them. */
function fyOf(sel, from, to) {
  const f = from ? from.value : null, t = to ? to.value : '';
  let v = '';
  if (f === null) {
    v = /-03-31$/.test(t) ? fyLabel(t) : t === today() ? 'now' : '';
  } else if (!f && !t) {
    v = 'all';
  } else if (/-04-01$/.test(f) && t === fyRange(fyLabel(f)).to) {
    v = fyLabel(f);
  } else if (f === fyStart() && t === today()) {
    v = 'now';
  } else if (f === today() && t === today()) {
    v = 'today';
  } else if (f === monthStart() && t === today()) {
    v = 'month';
  } else if (f === lastMonth().from && t === lastMonth().to) {
    v = 'lastmonth';
  }
  return [...sel.options].some(x => x.value === v) ? v : '';
}
/** Put the date boxes where a Year choice says. Custom leaves them be. */
function fyApply(v, from, to) {
  let r = null;
  if (v === 'now') r = { from: fyStart(), to: today() };
  else if (v === 'all') r = { from: '', to: '' };
  else if (v === 'today') r = { from: today(), to: today() };
  else if (v === 'month') r = { from: monthStart(), to: today() };
  else if (v === 'lastmonth') r = lastMonth();
  else if (/^\d{4}-\d{2}$/.test(v || '')) r = fyRange(v);
  if (!r) return false;
  if (from) from.value = r.from;
  if (to) to.value = r.to;
  return true;
}
/** Move every Year box on the screen to what its dates now say. Called when
 *  code has set the dates -- `screenState.restore` does, at its end. */
function fySync() {
  $$('#view select[data-fy]').forEach(s => { if (s._fySync) s._fySync(); });
}
/** The address field for a Year box: put it last among a screen's fields,
 *  after the dates, so that "to date" and "All years" win over the dates
 *  they were saved with. Read off the dates at the moment of saving, so the
 *  order the change handlers run in cannot leave a stale choice behind. */
function fyField(fromId, toId, id) {
  const box = i => (i ? $('#' + i) : null);
  return {
    get: () => {
      const s = $('#' + (id || 'fyq'));
      const v = s ? fyOf(s, box(fromId), box(toId)) : '';
      return FY_RELATIVE.includes(v) ? v : '';
    },
    set: v => {
      const s = $('#' + (id || 'fyq'));
      // Only a choice this box offers: "All years" on a list that cannot
      // draw every year would be twenty thousand rows from a typed address.
      if (s && FY_RELATIVE.includes(v)
          && [...s.options].some(x => x.value === v)) {
        fyApply(v, box(fromId), box(toId));
      }
    }
  };
}

function filterBar(extra) {
  return `<div class="card"><div class="bd"><div class="row">
    ${fySelect('fyq')}
    <div><label>From</label><input type="date" id="f1" value="${fyStart()}"></div>
    <div><label>To</label><input type="date" id="f2" value="${today()}"></div>
    ${extra || ''}<div class="spacer"></div>
    <div class="acts">
      <button id="go" class="primary">Apply</button>
      <button id="csv">⤓ CSV</button>
    </div>
  </div></div></div><div id="out"></div>`;
}

/** Wire a Year box to its date boxes: picking a year fills them and loads,
 *  and typing a date moves the box to whatever the dates now say. `fromId`
 *  is null on an as-on screen. */
function bindFy(load, fromId, toId, id) {
  const sel = $('#' + (id || 'fyq'));
  if (!sel) return;
  const from = fromId ? $('#' + fromId) : null, to = $('#' + toId);
  const sync = () => { sel.value = fyOf(sel, from, to); };
  sel._fySync = sync;
  sel.onchange = () => { if (fyApply(sel.value, from, to)) load(); };
  [from, to].forEach(inp => { if (inp) inp.addEventListener('change', sync); });
  sync();
}

/* ==================================================== one account ------
   **Drawn once, shown in two places.** Customer Accounts opens an account
   the moment a customer is named, and the Account Ledger screen opens any
   account at all; they are the same account and this is the only code that
   draws one. Two screens drawing it two ways is what made them disagree.

   SHREE reads an account in two views and so does this:

     Monthly summary   ## | Month | Credit | Debit | Balance | C/D, every
                       month of the window drawn, empty ones included, the
                       balance a magnitude with DB or CR beside it. The
                       totals are the totals of the entries, which is what
                       `AMAS` stores on the ledger's own record.

     Datewise          Date | Particulars | Credit | Debit, where the
                       particulars is the account on the OTHER side --
                       SALES A/C, KOTAK BANK : B. S. TRADERS -- and the
                       number and reference sit beneath it. No running
                       balance: the opening is the first row and the closing
                       is struck at the foot, the way a T account squares. */

/** The two ways SHREE reads an account, as a control both screens carry. */
function acctViewPicker(q) {
  return `<div id="viewbox"><label>View</label>
    <select id="acctview" title="The ways an account can be read">
      <option value="months"${q.view ? '' : ' selected'}>Monthly summary</option>
      <option value="entries"${q.view === 'entries' ? ' selected' : ''}>Datewise</option>
      <option value="due"${q.view === 'due' ? ' selected' : ''}>Due &amp; ageing</option>
    </select></div>`;
}

/** One entry, in SHREE's columns: what it was written against, then its
    number and reference beneath. */
function acctEntryRow(e, inside) {
  return `<tr class="${docHref(e.doc_kind, e.doc_id) ? 'clickable' : ''}"
      data-doc="${e.doc_id}" data-firm="${e.company_id}" data-kind="${e.doc_kind}">
    <td class="nw"${inside ? ' style="padding-left:26px"' : ''}>${dt(e.date)}</td>
    <td><b>${e.particulars ? goLink(accountLink(e.other_id), esc(e.particulars),
      'Open this account') : esc(e.kind)}</b>
      ${e.ref ? '<div style="font-size:11.5px;color:var(--muted)">' + esc(e.ref) + '</div>' : ''}${
      e.gst ? '<div style="font-size:11.5px;color:var(--muted)">Challan ' + fmt(e.challan_amount)
        + ' + GST ' + fmt(e.gst) + (e.gst_ref ? ' (' + esc(e.gst_ref) + ')' : '') + '</div>' : ''}${
      // The GST bill this challan went out on, in its own firm -- the line
      // itself opens the challan.
      e.bill_id ? '<div style="font-size:11.5px">billed as ' + goLink(
        docLink('SALE', e.bill_id, e.bill_company), esc(e.bill_no), 'Open the GST bill')
        + '</div>' : ''}</td>
    <td class="nw" style="color:var(--muted)">${esc(e.kind)}</td>
    <td class="r num">${e.credit ? fmt(e.credit) : ''}</td>
    <td class="r num">${e.debit ? fmt(e.debit) : ''}</td></tr>`;
}

const ACCT_HEAD = `<tr><th style="width:110px">Date</th><th>Particulars</th>
  <th style="width:110px">Type</th><th class="r" style="width:140px">Credit</th>
  <th class="r" style="width:140px">Debit</th></tr>`;

/* SHREE's datewise ledger, footer and all: the two column totals, the
   closing struck on the lighter side, and the grand totals equal.

   **The latest entry on top** (23 Sep 2026: "the latest and the most recent
   should show on top, when opened"), and the Opening Balance at the foot of
   the entries -- SHREE's page read upwards. The totals, the closing and the
   grand totals stay at the foot, where a ledger strikes them. */
function acctEntriesTable(d, from) {
  const op = d.opening || 0;
  const close = d.closing === null ? null : (d.closing || 0);
  const drTotal = d.total_debit + (op > 0 ? op : 0);
  const crTotal = d.total_credit + (op < 0 ? -op : 0);
  return `<table><thead>${ACCT_HEAD}</thead>
    <tbody>${d.entries.slice().reverse().map(e => acctEntryRow(e)).join('')}
    <tr><td class="nw">${from ? dt(from) : ''}</td>
      <td colspan="2"><i>Opening Balance</i></td>
      <td class="r num">${op < 0 ? fmt(-op) : ''}</td>
      <td class="r num">${op >= 0 ? fmt(op) : ''}</td></tr></tbody>
    <tfoot>${close === null ? `<tr><td colspan="3">Totals</td>
        <td class="r num">${fmt(d.total_credit)}</td>
        <td class="r num">${fmt(d.total_debit)}</td></tr>` : `
      <tr><td colspan="3">Totals</td>
        <td class="r num">${fmt(crTotal)}</td><td class="r num">${fmt(drTotal)}</td></tr>
      <tr><td colspan="3">Closing ${close >= 0 ? 'Debit' : 'Credit'} Balance</td>
        <td class="r num">${close >= 0 ? fmt(close) : ''}</td>
        <td class="r num">${close < 0 ? fmt(-close) : ''}</td></tr>
      <tr><td colspan="3"></td>
        <td class="r num"><b>${fmt(crTotal + (close >= 0 ? close : 0))}</b></td>
        <td class="r num"><b>${fmt(drTotal + (close < 0 ? -close : 0))}</b></td></tr>`}
    </tfoot></table>`;
}

/** Months, latest first, each with the place it held in the list -- the
 *  rows and the panels that open under them are matched on that. */
function latestFirst(list) {
  return list.map((m, i) => [m, i]).reverse();
}

// What a month held, named rather than counted -- the challans first, because
// that is the thing the customer actually took.
const ACCT_KINDS = ['Challan + GST', 'Challan', 'Bill', 'Receipt', 'Payment', 'Journal'];
function acctHeld(m) {
  const k = m.kinds || {};
  // A challan and its GST are one line on the account, and said as one.
  const say = (n, c) => n === 'Challan + GST'
    ? c + ' challan' + (c === 1 ? '' : 's') + ' + GST'
    : c + ' ' + n.toLowerCase() + (c === 1 ? '' : 's');
  return ACCT_KINDS.filter(n => k[n])
    .concat(Object.keys(k).filter(n => ACCT_KINDS.indexOf(n) < 0).sort())
    .map(n => say(n, k[n])).join(' · ');
}

/* SHREE's monthly summary. **A month opens on a page of its own**
   (28 Sep 2026: "when clicking on the month everything in that month shows
   as a dropbox. Instead take me to a new page as per the month the user
   selects"). It used to fold its entries out under the row; now the row
   leads to `#/accounts/month`, and Back or Esc comes back to this line.

   **The latest month on top** (23 Sep 2026: "when displaying anything month
   wise show the latest entry on the top"). SHREE prints the months oldest
   first; read upwards, this is the same page -- the Opening Balance at the
   foot, each month's balance the one it closed on, the top month's the
   balance today. `monthHref(ym)` is where every month leads -- **an empty
   one too** (4 Oct 2026: "If I click on enter it should take me to
   October's statement but it doesn't because the month is empty"). Its
   page still has the balance it opened and closed on, and ‹ Prev / Next ›
   from there. */
function acctMonthsTable(d, monthHref) {
  return `<table><thead><tr><th style="width:34px"></th><th>Month</th>
      <th class="r">Credit</th><th class="r">Debit</th>
      <th class="r">Balance</th><th style="width:34px">C/D</th>
      <th style="width:28px"></th></tr></thead>
    <tbody>
    ${d.months.slice().reverse().map(m => {
      const href = monthHref ? monthHref(m.month) : '';
      return `
      <tr${href ? ' class="clickable" data-href="' + esc(href) + '" title="Open '
          + esc(m.label) + ' on its own page"' : ''}>
        <td class="nw" style="color:var(--muted)">${esc(m.mon)}</td>
        <td class="nw">${esc(m.label)}${m.count
          ? '<div style="font-size:11.5px;color:var(--muted)">' + esc(acctHeld(m)) + '</div>' : ''}</td>
        <td class="r num">${m.credit ? fmt(m.credit) : ''}</td>
        <td class="r num">${m.debit ? fmt(m.debit) : ''}</td>
        <td class="r num">${m.balance === null ? '—'
          : '<b>' + fmt(Math.abs(m.balance)) + '</b>'}</td>
        <td style="color:var(--muted)">${esc(m.dc || '')}</td>
        <td class="r" style="color:var(--muted);font-size:17px">${href ? '›' : ''}</td></tr>`;
    }).join('')}
    <tr><td></td><td colspan="3"><i>Opening Balance</i></td>
      <td class="r num">${d.opening === null ? '—' : fmt(Math.abs(d.opening))}</td>
      <td style="color:var(--muted)">${d.opening === null ? ''
        : (d.opening >= 0 ? 'DB' : 'CR')}</td><td></td></tr>
    </tbody><tfoot><tr><td></td>
      <td>${d.months.length} month(s) · ${d.entry_count} entr${
        d.entry_count === 1 ? 'y' : 'ies'}</td>
      <td class="r num">${fmt(d.total_credit)}</td>
      <td class="r num">${fmt(d.total_debit)}</td>
      <td class="r num">${d.closing === null ? '—'
        : '<b>' + fmt(Math.abs(d.closing)) + '</b>'}</td>
      <td style="color:var(--muted)">${d.closing === null ? ''
        : (d.closing >= 0 ? 'DB' : 'CR')}</td><td></td></tr></tfoot></table>`;
}

/** Where a month of an account opens: its own page, in the same books the
 *  account was read across. */
function acctMonthHref(lid, ym, book) {
  return '#/accounts/month?' + new URLSearchParams(
    Object.assign({ ledger_id: lid, month: ym }, book ? { scope: 'book' } : {}));
}

/** The whole account: its four figures, then whichever view is asked for. */
function acctHtml(d, o) {
  const p = d.party;
  const where = d.in_books.map(b => esc(b.company)
    + (b.is_account && d.accounted && d.in_books.length > 1
       ? ' <i>(the account)</i>' : '')).join(' · ');
  return `
    <div class="kpis">
      <div class="kpi"><div class="lb">Opening</div>
        <div class="vl">${d.opening === null ? '—' : money(Math.abs(d.opening))}</div>
        <div class="ft">${d.opening === null ? 'no account'
          : (d.opening >= 0 ? 'Dr' : 'Cr') + (o.from ? ' as at ' + dt(o.from)
            : ' brought into the books')}</div></div>
      <div class="kpi"><div class="lb">Debit</div><div class="vl">${money(d.total_debit)}</div>
        <div class="ft">${d.bill_count
          ? d.bill_count + ' challan' + (d.bill_count === 1 ? '' : 's')
            + ' worth ' + fmt(d.billed) + ', and the tax on them'
          : d.entry_count + ' entr' + (d.entry_count === 1 ? 'y' : 'ies') + ' in all'}</div></div>
      <div class="kpi"><div class="lb">Credit</div><div class="vl">${money(d.total_credit)}</div>
        <div class="ft">${d.paid ? fmt(d.paid) + ' of it money received' : '&nbsp;'}</div></div>
      <div class="kpi ${d.closing > 0 ? 'good' : d.closing < 0 ? 'bad' : ''}">
        <div class="lb">Closing balance</div>
        <div class="vl">${d.closing === null ? '—' : money(Math.abs(d.closing))}</div>
        <div class="ft">${d.closing === null ? 'no account in ' + esc(d.money_book_name)
          : (d.closing > 0 ? 'receivable' : d.closing < 0 ? 'payable' : 'settled')
            + ' as at ' + dt(o.to)}</div></div>
    </div>
    ${d.accounted ? '' : `<div class="card"><div class="bd">
      <p><b>${esc(p.name)}</b> is billed by the GST firms but has no account in
      <b>${esc(d.money_book_name)}</b>, where the challans and every receipt are
      written. There is no balance to work out here.
      ${fmt(d.billed_to_date)} stands on the register. If they are written in the
      accounts book under another name, join the two under
      <a href="#/allocation">Customer allocation</a>.</p></div></div>`}
    <div class="card"><h2>${esc(o.heading || p.name)}<span class="rt"
      style="font-weight:400;font-size:12px;color:var(--muted)">
      ${esc(p.group_name || '')} · ${where}</span></h2>
      <div class="bd flush tbl-scroll">${d.entry_count || d.months.length
        ? (o.byMonth ? acctMonthsTable(d, o.monthHref) : acctEntriesTable(d, o.from))
        : '<div class="empty"><b>Nothing in these months</b>No entry against this account between these dates.</div>'}
      </div>
      <div class="bd" style="border-top:1px solid var(--line);font-size:12px;color:var(--muted)">
        ${o.byMonth
          ? 'The month rows are the totals of the account\'s own entries, which is how the old software\'s <b>Account Ledger (Monthly Summary)</b> reads — credit, debit, and the balance each month closed on — with the latest month on top. Click a month, or Enter on it, to open that month on a page of its own.'
          : o.heading && !d.entry_count
          // A month with nothing in it is still drawn as a ledger -- the
          // balance in, the same balance out -- so the keyboard has its
          // Opening Balance line to stand on and ← → go on turning months.
          ? '<b>Nothing was written in this account in ' + esc(o.heading) + '.</b>'
            + (d.closing === null ? '' : ' The balance stood at ' + esc(drcr(d.closing)) + ' all month.')
          : o.heading
          ? 'Every entry in ' + esc(o.heading) + ', the latest first. <b>Particulars</b> is the account on the other side of each entry, with its number and reference beneath; the balance brought in from the month before is the Opening Balance at the foot, and the closing is struck below it — the old software\'s <b>Ledger of …</b> for one month.'
          : '<b>Particulars</b> is the account on the other side of each entry, with its number and reference beneath — the old software\'s <b>Ledger of …</b>, the latest entry first and the closing struck at the foot.'}
        ${d.stray ? ' <b>' + d.stray + '</b> entr' + (d.stray === 1 ? 'y falls' : 'ies fall')
          + ' outside these months.' : ''}
      </div></div>`;
}

/* ------------------------------------------------ what is due, and when ---
   A single balance cannot say whether a customer is late. Some of them pay
   after a few months, so the question worth asking is not "how much" but
   "how much of it should already be in".

   Every credit is applied against the oldest debt first -- the way a clerk
   applies a payment when nobody says which bill it is for -- and whatever is
   still standing is then asked: was it due by this date, and how long ago?
   A challan's due date is its own payment terms; the tax journal beside it
   carries none of its own and falls due with the customer's usual period. */
function acctDueHtml(d) {
  const late = d.oldest_overdue_days;
  const bands = { '1-30': 'up to a month late', '31-60': 'one to two months',
                  '61-90': 'two to three months', '91+': 'over three months' };
  return `
    <div class="kpis">
      <div class="kpi"><div class="lb">Owed on this day</div>
        <div class="vl">${money(d.owed)}</div>
        <div class="ft">${d.claim_count} open item(s) as at ${dt(d.as_on)}</div></div>
      <div class="kpi ${d.due_now > 0 ? 'bad' : ''}"><div class="lb">Should already be in</div>
        <div class="vl">${money(d.due_now)}</div>
        <div class="ft">${late ? 'oldest ' + late + ' day' + (late === 1 ? '' : 's')
          + ' past due (' + dt(d.oldest_due) + ')' : 'nothing overdue'}</div></div>
      <div class="kpi good"><div class="lb">Not yet due</div>
        <div class="vl">${money(d.not_due)}</div>
        <div class="ft">still inside its ${esc(d.usual_terms || 'terms')}</div></div>
      <div class="kpi"><div class="lb">Usual terms</div>
        <div class="vl" style="font-size:22px">${esc(d.usual_terms || '—')}</div>
        <div class="ft">what this customer is normally given</div></div>
    </div>
    <div class="card"><h2>How late it is</h2><div class="bd">
      ${d.due_now > 0 ? `<div class="ageband">${d.buckets.map(b => b.amount
        ? `<div style="flex:${b.amount}" class="ab ab${b.band.replace('+','p')}"
             title="${esc(bands[b.band] || b.band)}">
             <b>${fmt(b.amount)}</b><span>${esc(b.band)} days</span></div>` : '').join('')}
      </div>` : '<div class="empty" style="padding:14px">Nothing is past due.</div>'}
    </div></div>
    <div class="card"><h2>What is still standing<span class="rt"
      style="font-weight:400;font-size:13px;color:var(--muted)">
      ${d.settled} item(s) already settled by payments received</span></h2>
      <div class="bd flush tbl-scroll">${d.claims.length ? `
      <table><thead><tr><th style="width:118px">Date</th><th>Particulars</th>
        <th style="width:96px">No</th><th style="width:110px">Terms</th>
        <th style="width:118px">Due on</th>
        <th class="r" style="width:110px">Days late</th>
        <th class="r" style="width:160px">Still owed</th></tr></thead>
      <tbody>${d.claims.map(c => `<tr class="${c.overdue_days ? 'lateln' : ''}"${claimHref(c)}>
        <td class="nw">${dt(c.date)}</td>
        <td>${claimParticulars(c)}</td>
        <td class="nw"><b>${goLink(docLink(c.kind, c.doc_id, c.company_id), esc(c.no))}</b></td>
        <td class="nw" style="color:var(--muted)">${esc(c.terms || '—')}</td>
        <td class="nw">${dt(c.due)}</td>
        <td class="r num${c.overdue_days ? '" style="color:var(--bad);font-weight:700' : ''}">${
          c.overdue_days ? c.overdue_days : '—'}</td>
        <td class="r num"><b>${fmt(c.open)}</b></td></tr>`).join('')}
      </tbody><tfoot><tr><td colspan="5">${d.claim_count} open item(s)</td>
        <td class="r num">${d.due_now ? fmt(d.due_now) + ' due' : ''}</td>
        <td class="r num"><b>${fmt(d.owed)}</b></td></tr></tfoot></table>`
      : '<div class="empty"><b>Nothing outstanding</b>Every item on this account is paid up as at ' + dt(d.as_on) + '.</div>'}
      </div>
      <div class="bd" style="border-top:1px solid var(--line);font-size:13px;color:var(--muted)">
        Payments are applied to the oldest debt first. A challan and the GST
        raised on it are one item, the two added together and due by the terms
        written on the challan. A challan that was never billed stands on its
        own; a GST entry that could not be matched to its challan follows this
        customer's usual ${esc(d.usual_terms || 'terms')}.
      </div></div>`;
}

/** What an open item is, with the challan, its GST journal and the bill it
 *  went out on each leading to itself. Who owes us and a customer's Due &
 *  ageing both print their items this way. */
function claimParticulars(c, small) {
  const style = 'font-size:' + (small || '12px') + ';color:var(--muted)';
  const name = esc(c.item || DUE_KIND[c.kind] || c.kind || '');
  if (!c.gst) {
    return name + (c.ref ? '<div style="' + style + '">' + esc(c.ref) + '</div>' : '')
      + (c.bill_id ? '<div style="' + style + '">' + goLink(
          docLink('SALE', c.bill_id, c.bill_company), 'bill ' + esc(c.bill_no),
          'Open the bill this challan went out on') + '</div>' : '');
  }
  const journal = (c.gst_ids || [])[0];
  return name + '<div style="' + style + '">Challan ' + fmt(c.challan_amount)
    + ' + ' + goLink(docLink('JOURNAL', journal, c.company_id),
                     'GST ' + fmt(c.gst), 'Open the GST journal')
    + (c.bill_id
      ? ' (' + goLink(docLink('SALE', c.bill_id, c.bill_company),
                      esc(c.gst_ref || c.bill_no), 'Open bill ' + (c.bill_no || '')) + ')'
      : c.gst_ref ? ' (' + esc(c.gst_ref) + ')' : '') + '</div>';
}
/** The row attribute that makes an open item open its own document. */
function claimHref(c) {
  const h = docLink(c.kind, c.doc_id, c.company_id);
  return h ? ' data-href="' + esc(h) + '" title="Open ' + esc(c.no || 'it') + '"' : '';
}

/** Make the documents behind an account's entries reachable. A month row
 *  needs nothing here: it carries `data-href`, and leads to its own page. */
function acctBind() {
  // A document lives in its own firm's books, so opening one means going
  // there first -- the same walk the Bills screen makes.
  $$('#out tr[data-doc]').forEach(tr => {
    tr.onclick = async e => {
      e.stopPropagation();
      const id = num(tr.dataset.doc), firm = num(tr.dataset.firm);
      const href = docHref(tr.dataset.kind, id);
      if (!href) return;
      if (firm !== S.meta.company_id) await useCompany(firm);
      location.hash = href;
    };
  });
}

/* ================================== an account, sent as a PDF ----------
   Asked for on 26 Sep 2026: "add option to send custom date account history
   to customers. Add this in Accounts. Everything under those dates should be
   shown and it should be generated as a pdf."

   **Statement PDF** at the top of an open account asks for the dates -- a
   Year, or any From and To (the screen itself only reads a year at a time)
   -- says what the statement will hold, and makes the PDF: every entry
   between the dates, oldest first, the opening balance first and the
   closing struck at the foot, the way SHREE prints its "Ledger of ..."
   (`statement_pdf.py`). The figures are the screen's own report.

   Sending it: **Share** hands the file to the computer's own share sheet --
   WhatsApp, Mail, Outlook, whatever is installed -- where the browser can
   do that; **WhatsApp** saves the PDF and opens a chat with the customer's
   mobile, the message typed, for the file to be attached; **Download** and
   **Open** are there for anything else. Nothing is sent without the user
   pressing send in whatever it went to. */

/** The customer's mobile, as WhatsApp wants it: 91 and ten digits. The phone
 *  box holds whatever was typed -- "91574 88884", two numbers with a slash
 *  -- so the first ten-digit mobile in it is taken. */
function waNumber(phone) {
  const runs = String(phone || '').split(/[,/;]|\s{2,}/)
    .map(s => s.replace(/\D/g, '').replace(/^(?:0|91)(?=\d{10}$)/, ''));
  const hit = runs.find(s => /^[6-9]\d{9}$/.test(s));
  return hit ? '91' + hit : '';
}

/** Hand a file to the browser to save, under its own name. */
function saveFile(file) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(file);
  a.download = file.name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 60000);
}

async function statementDialog(party, o) {
  const mobile = waNumber(party.phone);
  let canShare = false;
  try {
    canShare = !!(navigator.canShare && navigator.canShare({
      files: [new File(['x'], 'x.pdf', { type: 'application/pdf' })] }));
  } catch (e) { canShare = false; }
  let facts = null, seq = 0, made = null;        // made: { url, file }

  const from = () => $('#st-f1').value, to = () => $('#st-f2').value || today();
  const url = () => firmUrl('/api/reports/statement?' + new URLSearchParams(
    Object.entries({ ledger_id: party.id, from: from(), to: to(), scope: o.scope || '' })
      .filter(([, v]) => v !== '')));
  const wrong = () => (from() && from() > to() ? 'The From date is after the To date.' : '');

  async function file() {
    const u = url();
    if (made && made.url === u) return made.file;
    const res = await fetch(u);
    if (!res.ok) {
      let msg = '';
      try { msg = (await res.json()).error; } catch (e) { msg = res.statusText; }
      throw new Error(msg || 'The PDF could not be made.');
    }
    const blob = await res.blob();
    const said = (res.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/);
    made = { url: u, file: new File([blob], said ? said[1] : 'Statement.pdf',
                                     { type: 'application/pdf' }) };
    return made.file;
  }
  const message = () => {
    const lines = ['Dear ' + party.name + ',',
      'Please find attached your account statement '
        + (from() ? 'from ' + dt(from()) + ' ' : '') + 'to ' + dt(to()) + '.'];
    if (facts && facts.closing !== null && facts.closing !== undefined) {
      lines.push('Balance as on ' + dt(to()) + ': ₹' + drcr(facts.closing) + '.');
    }
    if (facts && facts.account_book) lines.push('— ' + facts.account_book);
    return lines.join('\n');
  };

  async function preview() {
    const say = $('#st-say');
    if (!say) return;
    const mine = ++seq;
    facts = null;
    const w = wrong();
    if (w) { say.innerHTML = '<b style="color:var(--bad)">' + esc(w) + '</b>'; return; }
    say.textContent = 'Reading the account…';
    try {
      const d = await api.get('/api/reports/customers', {
        ledger_id: party.id, from: from(), to: to(), scope: o.scope || '' });
      if (mine !== seq || !$('#st-say')) return;
      facts = d;
      say.innerHTML = '<b>' + d.entry_count + '</b> entr' + (d.entry_count === 1 ? 'y' : 'ies')
        + ' between ' + (from() ? dt(from()) : 'the first entry') + ' and ' + dt(to())
        + (d.opening === null ? '' : ' · opening ' + esc(drcr(d.opening)))
        + (d.closing === null ? '' : ' · closing <b>' + esc(drcr(d.closing)) + '</b>');
    } catch (e) {
      if (mine === seq && $('#st-say')) say.textContent = e.message;
    }
  }

  // Each button leaves the dialog open, so the same statement can be looked
  // at and then sent, or sent two ways.
  const act = fn => async () => {
    const w = wrong();
    if (w) { bad(w); return false; }
    try { await fn(); } catch (e) {
      if (e && e.name !== 'AbortError') bad(e.message || String(e));
    }
    return false;
  };
  const buttons = [{ label: 'Close', value: null },
    { label: 'Open PDF', before: act(async () => { window.open(url(), '_blank'); }) },
    { label: '⤓ Download', cls: canShare ? '' : 'primary',
      before: act(async () => { const f = await file(); saveFile(f); ok('Saved ' + f.name); }) }];
  if (mobile) {
    buttons.push({ label: 'WhatsApp', before: act(async () => {
      const f = await file();
      saveFile(f);
      window.open('https://wa.me/' + mobile + '?text=' + encodeURIComponent(message()), '_blank');
      ok('Saved ' + f.name + ' — attach it in the chat');
    }) });
  }
  if (canShare) {
    buttons.push({ label: 'Share…', cls: 'primary', before: act(async () => {
      const f = await file();
      await navigator.share({ files: [f], title: f.name, text: message() });
    }) });
  }

  await modal('Statement — ' + party.name, `
    <div class="row">
      ${fySelect('st-fy', { all: true })}
      <div><label>From</label><input type="date" id="st-f1" value="${esc(o.from || '')}"></div>
      <div><label>To</label><input type="date" id="st-f2" value="${esc(o.to || today())}"></div>
    </div>
    <p id="st-say" style="margin:14px 0 6px;font-size:14.5px"></p>
    <p class="hint" style="margin:0 0 6px">Every entry between these dates, oldest
      first, with the opening balance on top and the closing balance struck at the
      foot — the way the old software prints the ledger.</p>
    ${mobile ? '<p class="hint" style="margin:0">WhatsApp saves the PDF and opens a chat with '
      + esc(party.phone) + ' with the message typed; attach the saved PDF there and send.</p>'
      : '<p class="hint" style="margin:0">No mobile number on this account, so there is no WhatsApp button — Download or Share it instead.</p>'}`,
    buttons, {
      nothingToSave: true,
      onOpen: () => {
        bindFy(preview, 'st-f1', 'st-f2', 'st-fy');
        ['st-f1', 'st-f2'].forEach(id => $('#' + id).addEventListener('change', preview));
        preview();
      }
    });
}

/* ======================================= the way in from elsewhere ------
   **Account Ledger was this same screen.** It drew an account with the very
   functions above, and differed only in how you reached one: a picker over
   the chart of accounts rather than a list of customers. Two screens off one
   set of code is how they came to disagree before, so there is one now --
   `#/accounts` -- and its box searches the customers first and then every
   other account in the books.

   This route stays because the chart of accounts, the parties list and old
   links all point at it. It hands over, carrying whatever was asked for. */

route(/^\/reports\/ledger$/, async (view, _m, q) => {
  const carry = new URLSearchParams();
  ['ledger_id', 'party_id', 'view', 'scope'].forEach(k => {
    if (q[k]) carry.set(k === 'party_id' ? 'ledger_id' : k, q[k]);
  });
  location.replace('#/accounts' + (carry.toString() ? '?' + carry : ''));
});

/* ============================================== customers, a line each ---
   Every customer over the window in the shape of SHREE's yearly summary:
   what the account stood at, what was debited, what was credited, and what
   it stands at now. Naming one -- or clicking a line -- opens that account
   **here, on this screen**, drawn by the same code the Account Ledger screen
   uses; emptying the box brings the list back. Nothing sends you elsewhere.

   It is all one book: the no-tax accounts book, where every challan, the tax
   on it, and every receipt and payment are written. **Challans are counted,
   never bills** -- the same goods leave once and are written down twice, as
   a challan here and again as a tax bill in whichever GST firm bills that
   customer, and counting both would count every load twice.                */

/* **A new entry from the account** (4 Oct 2026: "when I am in a particular
   customer's account I should be able to add a new challan/bill, bank/cash
   entry, journal for that particular customer account"). One button, then
   the kind: each opens its usual screen with the account already chosen --
   the challan in the order firm, the bill in the customer's own billing firm
   (asked when none is set), money in or out and the journal in the book the
   account is kept in. A firm the customer is not yet in gets their record
   the way billing a challan makes it. Resolves true when something was
   saved here (money in or out); the others open their own screen, and Back
   from it comes back to the account. */
async function newForAccount(party, books) {
  const customer = party.party_kind === 'customer' || party.party_kind === 'both';
  const order = S.meta.order_company;
  const firms = S.meta.billing_firms || [];
  const firmName = id => ((S.meta.companies || []).find(c => c.id === num(id)) || {}).name || '';
  const home = firms.some(f => f.id === num(party.home_company)) ? num(party.home_company) : null;
  // The book the account itself is kept in -- where money and journals go.
  const kept = (books || []).find(b => b.is_account)
    || { company_id: party.company_id, ledger_id: party.id };
  const choices = [];
  if (customer && order) choices.push(['challan', '🧾 Challan', 'in ' + firmName(order)]);
  if (customer && firms.length) {
    choices.push(['bill', '🧾 GST bill', home ? 'from ' + firmName(home) : 'the firm is asked']);
  }
  if (party.party_kind) {
    choices.push(['RECEIPT', '＋ Receipt — money in', 'into a bank or cash']);
    choices.push(['PAYMENT', '− Payment — money out', 'out of a bank or cash']);
  }
  choices.push(['journal', '✎ Journal', 'this account on the first line']);
  const kind = await modal('New entry for ' + party.name, `<div class="newfor">${choices.map(([k, t, sub]) =>
      `<button type="button" data-k="${k}"><b>${esc(t)}</b><small>${esc(sub)}</small></button>`).join('')}</div>`,
    [{ label: 'Cancel', value: null }], {
      onOpen: (bg, close) => {
        $$('[data-k]', bg).forEach(b => { b.onclick = () => close(b.dataset.k); });
        setTimeout(() => { const f = $('[data-k]', bg); if (f) f.focus(); }, 30);
      }
    });
  if (!kind) return false;

  // This customer's own record in a firm, made there if missing.
  const ledgerIn = async target => {
    const b = (books || []).find(x => x.company_id === target);
    if (b) return b.ledger_id;
    return (await api.post('/api/ledgers/' + party.id + '/in-firm', { firm: target })).id;
  };
  try {
    if (kind === 'challan') {
      location.hash = '#/invoice/new?firm=order&party=' + await ledgerIn(order);
      return false;
    }
    if (kind === 'bill') {
      const target = home || await chooseBillingFirm(party.name, null, firms, false);
      if (!target) return false;
      location.hash = '#/invoice/new?firm=' + target + '&party=' + await ledgerIn(target);
      return false;
    }
    if (kind === 'journal') {
      location.hash = '#/voucher/new?kind=JOURNAL&firm=' + kept.company_id
        + '&ledger_id=' + kept.ledger_id;
      return false;
    }
  } catch (e) { bad(e.message); return false; }
  // Money in or out: the dialog, over the account, in the account's book.
  if (kept.company_id !== S.meta.company_id) {
    await useCompany(kept.company_id);
    drawCompany();
  }
  return !!await editVoucher({ party_id: kept.ledger_id, party_name: party.name,
                               bank_id: await recentMoney(null) }, kind);
}

route(/^\/(accounts|reports\/customers)$/, async (view, m, q) => {
  // The one thing at the top is for an open account, so the bar is drawn
  // again whenever the screen changes between an account and the list.
  let titled = null;
  let shown = null;                 // the account last drawn, for Edit
  let shownBooks = [];              // the firms it is written in
  const titleBar = one => {
    if (titled === one) return;
    titled = one;
    setTitle('Accounts',
             'Every customer, and every other account, a month at a time',
             one ? [{
               // A challan, a bill, money in or out, a journal -- for this
               // account, without finding it again (4 Oct 2026).
               label: '＋ New entry', cls: 'primary',
               onClick: async () => {
                 if (!shown) return;
                 if (await newForAccount(shown, shownBooks)) load();
               }
             }, {
               // Any dates at all, as a PDF to send the customer.
               label: '📄 Statement PDF',
               onClick: () => {
                 if (!shown) return;
                 statementDialog(shown, {
                   from: $('#f1').value, to: $('#f2').value,
                   scope: $('#book').checked ? 'book' : '' });
               }
             }, {
               label: '✕ Delete account', cls: 'danger',
               onClick: async () => {
                 if (await deleteAccount(num($('#pid').value))) takeCustomer(null);
               }
             }, {
               // Its address, GSTIN, terms -- the master behind the account.
               label: '✎ Edit account',
               onClick: async () => {
                 if (!shown) return;
                 if (shown.company_id !== S.meta.company_id) {
                   await useCompany(shown.company_id);
                   drawCompany();
                 }
                 const p = await api.get('/api/ledgers/' + shown.id);
                 if (await editParty(p)) { invalidate(); load(); }
               }
             }, {
               // Two accounts that are one customer (3 Oct 2026).
               label: '⇄ Merge account',
               onClick: async () => {
                 const id = num($('#pid').value);
                 if (!id) return;
                 const kept = await mergeAccount(id, shown ? shown.name : $('#p').value,
                                                 findAccounts);
                 if (kept) takeCustomer(kept);
               }
             }] : []);
  };
  titleBar(!!q.ledger_id);

  /* **A Year box, and still no dates.** The dates and the year went on 17
     Sep 2026 ("on the account page remove the option for date and year");
     the year came back on 24 Sep 2026 ("for customer accounts add year
     option") -- this year to date, which is what it opens on, any earlier
     year from 1 April to 31 March, or every year. No Custom: there are no
     dates to type. The two date boxes stay, hidden, because the window, the
     "as at" and the Due view's day are all read off them. */
  view.innerHTML = `<div class="card"><div class="bd"><div class="row">
    <input type="hidden" id="f1" value="${fyStart()}">
    <input type="hidden" id="f2" value="${today()}">
    <div class="pick" style="flex:1;min-width:250px">
      <label>Customer</label>
      <div class="pickbox">
        <input id="p" autocomplete="off" placeholder="Type to find a customer on the list…">
        <button type="button" data-browse-party class="pickbtn" tabindex="-1"
          title="Search every account in the books — customers first (F4)">▾</button></div>
      <input type="hidden" id="pid" value="${esc(q.ledger_id || '')}"></div>
    ${fySelect('fyq', { all: true, quick: false }).replace('<option value="">Custom</option>', '')}
    ${acctViewPicker(q)}
    <div id="bookbox"><label>&nbsp;</label><label style="margin:0;text-transform:none;font-size:13px"
      title="A customer written in more than one firm has one account between them, and it is read across all of them unless you ask for this book alone.">
      <input type="checkbox" id="book" style="width:auto"
        ${q.scope === 'book' ? 'checked' : ''}> This book only</label></div>
    <div id="owingbox"><label>&nbsp;</label><label style="margin:0;text-transform:none;font-size:13px">
      <input type="checkbox" id="owing" style="width:auto"
        ${q.owing ? 'checked' : ''}> Only those who owe</label></div>
    <div id="everyonebox"><label>&nbsp;</label><label style="margin:0;text-transform:none;font-size:13px"
      title="The accounts book also holds land, salary and loan accounts. They are not customers, so they are left out unless you ask for them.">
      <input type="checkbox" id="everyone" style="width:auto"
        ${q.scope === 'all' ? 'checked' : ''}> Other accounts too</label></div>
    <div class="spacer"></div>
    <div class="acts"><button id="csv">⤓ CSV</button></div>
  </div></div></div><div id="out"></div>`;

  /* Which account is open is the whole of this screen, and it lived in a
     hidden box until now -- step off to the payment written against a
     customer and you came back to the list of every customer. It travels in
     the address with the rest of the screen's state. */
  /* The year chosen sets the two hidden dates. **All years** is how an
     account is opened from Who owes us -- the whole of it, back to its
     first entry -- because what a customer owes is so often for bills older
     than this year; it reads to today. */
  const yearRange = v => (v === 'all' ? { from: '', to: today() }
    : /^\d{4}-\d{2}$/.test(v || '') ? fyRange(v) : { from: fyStart(), to: today() });
  const setYear = v => {
    const sel = $('#fyq');
    const ok = v && [...sel.options].some(o => o.value === v) ? v : 'now';
    sel.value = ok;
    const r = yearRange(ok);
    $('#f1').value = r.from;
    $('#f2').value = r.to;
  };
  const mem = screenState('/' + m[0], {
    ledger_id: '#pid', view: '#acctview', owing: '#owing',
    // How Who owes us asked for the whole account before the Year box.
    years: { get: () => '', set: v => { if (v === 'all') setYear('all'); } },
    // One word carries two meanings here, as it does for the report: which
    // accounts the list holds, and which books one account is read across.
    scope: {
      get: () => $('#pid').value ? ($('#book').checked ? 'book' : '')
                                 : ($('#everyone').checked ? 'all' : ''),
      set: v => { $('#book').checked = v === 'book';
                  $('#everyone').checked = v === 'all'; }
    },
    fy: { get: () => ($('#fyq').value === 'now' ? '' : $('#fyq').value),
          set: v => setYear(v) }
  }).restore(q);

  /* Customers first, then everything else in the books.
     This screen used to answer only for customers and a second screen --
     Account Ledger -- answered for banks and expense heads, off the very
     same code. One account, one screen: the box searches the customers, and
     when none of them is what was meant, the rest of the chart of accounts
     follows underneath. */
  /* **In the order they are meant** (28 Sep 2026, "fix the search for
     Accounts"). "yug" put YUG 2 LATE CHARGE RECIVABLE first -- the names
     came back in the alphabet's order -- and Enter opened that instead of
     YUG ENTERPRISE 2, the customer the firm deals with. Now: how well the
     name answers (`nameRank`), customers before other accounts, then the
     one with the most entries on the list, then the alphabet. A name within
     a slip of what was typed ("rikia" for RIKAI INDUSTRIES) is offered only
     when nothing is spelt that way. A GSTIN is read only for something that
     could be part of one -- a digit, or five letters of its PAN -- so "egf"
     no longer finds customers by letters inside their GST numbers. */
  let busyByName = {};           // squashed name -> entries, off the list
  const findAccounts = async term => {
    const people = await api.get('/api/customers',
      { search: term, limit: 40, prefer_company: S.meta.company_id, fuzzy: 1 });
    const seen = new Set(people.map(x => x.id));
    const ledgers = (await loadLedgers()).filter(x => !seen.has(x.id))
      .map(x => Object.assign({}, x, { other: true }));
    if (!String(term || '').trim()) return people.concat(ledgers.slice(0, 30));
    const gst = /\d/.test(term) || /[a-z]{5}/i.test(term);
    const hits = people.map((x, i) => ({ x, i, who: 0,
        r: nameRank(term, x.name, [x.city, gst ? x.gstin : ''].join(' ')) }))
      .concat(ledgers.map((x, i) => ({ x, i, who: 1, r: nameRank(term, x.name, x.group_name) })))
      .filter(h => h.r < 9);
    const spelt = hits.filter(h => h.r < 5);
    const use = spelt.length ? spelt : hits;
    const busy = x => busyByName[squash(x.name)] || 0;
    use.sort((a, b) => a.r - b.r || a.who - b.who || busy(b.x) - busy(a.x) || a.i - b.i);
    return use.map(h => h.x);
  };
  const findCustomers = findAccounts;

  /* **Esc closes an account and goes back to the list, to the line it was
     opened from; Esc again goes to the menu** (26 Sep 2026: "if I click on
     escape from this page first it should take me back to accounts main
     page where I left the page and when I click escape for the second time
     then it should come to the menubar"). An account opens in place -- the
     list and the account are one screen and one entry in the history -- so
     there is no screen behind it for Esc to go back to, and the screen keeps
     the spot itself: the line and how far down the list was. It rides in
     `history.state`, so a challan opened from the account and closed with
     Esc still leaves the way back to the list. An account reached from
     somewhere else -- the whole account off Who owes us -- has no list
     behind it, and Esc goes back where it came from. */
  const LIST_AT = 'acctListAt';
  let listAt = (history.state && history.state[LIST_AT]) || null;
  const markList = at => { listAt = at; stamp({ [LIST_AT]: at, place: null }); opening = !!at; };
  // An account just opened from the list starts at its top, on its latest
  // month -- the cursor used to carry over the list's line number and land
  // on the account's last line, the Opening Balance, at the foot.
  let opening = false;
  const listShown = () => !$('#pid').value;
  /** Where the list stands: the line (the one being opened, or the cursor's)
   *  and the scroll. */
  const listSpot = id => {
    const rs = $$('#out tr[data-id]');
    const tr = id ? rs.find(r => r.dataset.id === String(id)) : $('#out tr[data-id].kb');
    return { y: window.scrollY, id: tr ? tr.dataset.id : '', idx: tr ? rs.indexOf(tr) : -1 };
  };
  async function backToList() {
    const at = listAt;
    markList(null);
    $('#p').value = '';
    $('#pid').value = '';
    await load();
    if (!listShown()) return;
    const rs = $$('#out tr[data-id]');
    if (!rs.length) return;
    // The same line -- or, if it has gone (deleted), the one now in its place.
    const tr = (at && at.id && rs.find(r => r.dataset.id === String(at.id)))
      || (at && at.idx >= 0 ? rs[Math.min(at.idx, rs.length - 1)] : null) || rs[0];
    if (at) window.scrollTo(window.scrollX, at.y);
    KB.setCursor(tr);
    stamp({ place: KB.place(tr) });
  }
  KB.setEscape(() => !!$('#pid').value && (!!listAt || navRoot), backToList);

  const takeCustomer = p => {
    // Emptying the box is the way back to the list, and lands where it was.
    if (!p) { backToList(); return; }
    if (listShown()) markList(listSpot());
    $('#p').value = p.name;
    $('#pid').value = p.id;
    load();
  };
  /* **The Select window opens from its ▾ alone** (6 Oct 2026, the ▾
     ringed: "the search on Accounts page shouldn't automatically popup ...
     only when clicking on the particular key it should open"). Everywhere
     else a name box opens its window as it is typed in (28 Sep 2026); here
     the box narrows the list under it instead, as every list's search box
     does, and the window -- every account in the books, customers first --
     is the ▾, or F4, its key. Nothing opens on a click into the box, on
     reaching it, on typing or on Enter. */
  let browsing = false;
  const browse = async () => {
    if (browsing) return null;
    browsing = true;
    let p;
    try { p = await chooseParty({ source: findCustomers, term: () => $('#p').value }); }
    finally { browsing = false; }
    if (p) takeCustomer(p); else $('#p').focus();
    return p;
  };
  $('[data-browse-party]').onclick = browse;
  $('#p').addEventListener('keydown', e => {
    if (e.key !== 'F4' || e.altKey || e.ctrlKey || e.metaKey) return;
    e.preventDefault();
    browse();
  });
  /** The list, narrowed to the customers the box names: spelt that way, or
   *  -- only when none is -- within a slip of it, as the window ranks them
   *  (`nameRank`, name then town). */
  function narrow() {
    const term = $('#p').value.trim();
    const trs = $$('#out tr[data-id]');
    if (!trs.length || $('#pid').value) return;
    const rank = trs.map(tr => (term ? nameRank(term, $('b', tr).textContent,
      ($('td div', tr) || {}).textContent || '') : 0));
    const spelt = rank.some(r => r < 5);
    let n = 0;
    trs.forEach((tr, i) => {
      const hide = !!term && rank[i] >= (spelt ? 5 : 9);
      if (tr.hidden !== hide) tr.hidden = hide;
      if (hide) tr.classList.remove('kb');
      else n += 1;
    });
    const foot = $('#out tfoot td');
    if (foot) {
      if (!foot.dataset.all) foot.dataset.all = foot.textContent;
      foot.textContent = term ? n + ' of ' + trs.length + ' customer(s)' : foot.dataset.all;
    }
    const none = $('#nomatch');
    if (none) none.hidden = !term || n > 0;
  }
  $('#p').addEventListener('input', async () => {
    if ($('#pid').value) {
      // A name typed over the open account's: back to the list, narrowed to
      // it. Rubbed out, back to the list where it was left.
      if (!$('#p').value.trim()) { backToList(); return; }
      markList(null);
      $('#pid').value = '';
      await load();
    }
    narrow();
  });
  // Emptying the box is how you get back to the whole list.
  $('#p').addEventListener('change', () => {
    if (!$('#p').value.trim() && $('#pid').value) takeCustomer(null);
  });

  const params = () => ({
    ledger_id: $('#pid').value, from: $('#f1').value, to: $('#f2').value,
    owing: $('#owing').checked ? '1' : '',
    // `scope` means two different things to the two halves: which accounts
    // the list holds, and which books one account is read across.
    scope: $('#pid').value ? ($('#book').checked ? 'book' : '')
                           : ($('#everyone').checked ? 'all' : '')
  });
  // The filters belong to the list, the view to an account; only one of them
  // is being looked at, so only that one is offered.
  const showFilters = () => {
    const one = !!$('#pid').value;
    titleBar(one);
    $('#viewbox').style.display = one ? '' : 'none';
    $('#bookbox').style.display = one ? '' : 'none';
    $('#owingbox').style.display = one ? 'none' : '';
    $('#everyonebox').style.display = one ? 'none' : '';
  };

  /* ---- every customer, a line each -- SHREE's yearly summary shape:
     what the account stood at, what was debited, what was credited, and what
     it stands at now. The challan count rides alongside because the goods
     going out is the other thing worth knowing at a glance. */
  function renderAll(d) {
    const asAt = dt($('#f2').value);
    // Who the firm deals with most, for putting them first in the search.
    busyByName = {};
    d.rows.forEach(r => { busyByName[squash(r.name)] = num(r.entries); });
    $('#out').innerHTML = `
      <div class="kpis">
        <div class="kpi"><div class="lb">Customers</div><div class="vl">${d.count}</div>
          <div class="ft">${d.scope === 'all'
            ? 'every party in the books'
            : 'people the firms send goods to'}</div></div>
        <div class="kpi"><div class="lb">Debit</div><div class="vl">${money(d.debit)}</div>
          <div class="ft">${d.bill_count} challan${d.bill_count === 1 ? '' : 's'} worth ${fmt(d.billed)}, and the tax on them</div></div>
        <div class="kpi"><div class="lb">Credit</div><div class="vl">${money(d.credit)}</div>
          <div class="ft">${d.entry_count} entr${d.entry_count === 1 ? 'y' : 'ies'} in all</div></div>
        <div class="kpi ${d.owed > 0 ? 'good' : ''}"><div class="lb">Owed to us</div>
          <div class="vl">${money(d.owed)}</div>
          <div class="ft">as at ${asAt}${d.advance ? ' · ' + fmt(d.advance) + ' held in advance' : ''}</div></div>
      </div>
      <div class="card"><div class="bd flush tbl-scroll">${d.rows.length ? `
        <table><thead><tr><th>Customer</th>
          <th class="r">Challans</th><th class="r">Opening</th>
          <th class="r">Debit</th><th class="r">Credit</th>
          <th class="r">Balance</th><th></th></tr></thead><tbody>` +
        d.rows.map(r => `<tr class="clickable" data-id="${r.ledger_id}">
          <td><b>${esc(r.name)}</b>${r.city ? '<div style="font-size:11.5px;color:var(--muted)">'
            + esc(r.city) + '</div>' : ''}</td>
          <td class="r num">${r.bills.count || ''}</td>
          <td class="r num nw">${r.opening === null ? '' : drcr(r.opening)}</td>
          <td class="r num">${r.debit ? fmt(r.debit) : ''}</td>
          <td class="r num">${r.credit ? fmt(r.credit) : ''}</td>
          <td class="r num nw">${r.owed === null
            ? `<span class="tag warn" title="No account in ${esc(d.money_book_name)}, where the money is written — ${fmt(r.billed_to_date)} on the register to date">not known</span>`
            : (r.owed ? '<b>' + drcr(r.owed) + '</b>' : '—')}</td>
          <td class="r"><button class="link" data-del style="color:var(--bad)"
            title="Delete this account">Delete</button></td></tr>`).join('') +
        `</tbody><tfoot><tr><td>${d.count} customer(s)</td>
          <td class="r num">${d.bill_count}</td><td></td>
          <td class="r num">${fmt(d.debit)}</td><td class="r num">${fmt(d.credit)}</td>
          <td class="r num nw"><b>${drcr(d.owed)}</b></td><td></td></tr></tfoot></table>`
      : '<div class="empty"><b>Nothing in these months</b>No entry against any customer between these dates.</div>'}
      <div class="empty" id="nomatch" hidden><b>No customer on the list is called that</b>
        ▾ beside the box, or F4, searches every account in the books.</div>
      </div></div>`;
    $$('#out tr[data-id]').forEach(tr => {
      tr.onclick = () => {
        markList(listSpot(tr.dataset.id));
        $('#pid').value = tr.dataset.id;
        $('#p').value = $('b', tr).textContent;
        load();
      };
      // Straight from the list -- the row's own key, 1, does the same. The
      // list is read again afterwards, and stays where it was scrolled to.
      $('[data-del]', tr).onclick = async e => {
        e.stopPropagation();
        if (!await deleteAccount(num(tr.dataset.id))) return;
        const y = document.scrollingElement.scrollTop;
        await load();
        document.scrollingElement.scrollTop = y;
      };
    });
    narrow();
  }

  async function load() {
    showFilters();
    mem.save();
    $('#out').innerHTML = '<div class="card"><div class="empty">Reading the books…</div></div>';
    const here = onScreen();
    // Due & ageing is a different question, and a different report answers it.
    if ($('#pid').value && $('#acctview').value === 'due') {
      try {
        const due = await api.get('/api/reports/due', {
          ledger_id: $('#pid').value, as_on: $('#f2').value,
          scope: $('#book').checked ? 'book' : '' });
        if (!here()) return;
        if (!$('#p').value.trim()) $('#p').value = due.party.name;
        shown = due.party;
        shownBooks = due.in_books || [];
        $('#out').innerHTML = acctDueHtml(due);
      } catch (e) {
        if (!here()) return;
        bad(e.message);
        $('#out').innerHTML = '<div class="card"><div class="empty">'
          + esc(e.message) + '</div></div>';
      }
      return;
    }
    let d;
    try { d = await api.get('/api/reports/customers', params()); }
    catch (e) {
      if (!here()) return;
      // Saying it once, on the screen. A toast that fades leaves "Reading the
      // books…" sitting there for ever, which reads as a screen that hangs --
      // and the commonest reason for landing here is an app still running the
      // code it was started with.
      bad(e.message);
      $('#out').innerHTML = `<div class="card"><div class="empty"><b>Could not
        read the books</b>${esc(e.message)}<div style="margin-top:8px">If this
        says the address was not found, the app is still running the version it
        was started with — stop it and start it again.</div></div></div>`;
      return;
    }
    if (!here()) return;
    // One customer is an account, and an account is drawn in one place --
    // the same `acctHtml` the Account Ledger screen uses.
    if (d.mode === 'one') {
      if (!$('#p').value.trim()) $('#p').value = d.party.name;
      shown = d.party;
      shownBooks = d.in_books || [];
      $('#out').innerHTML = acctHtml(d, {
        byMonth: $('#acctview').value !== 'entries',
        from: $('#f1').value, to: $('#f2').value,
        monthHref: ym => acctMonthHref($('#pid').value, ym, $('#book').checked) });
      acctBind();
      if (opening) {
        opening = false;
        window.scrollTo(window.scrollX, 0);
        const first = KB.rows()[0];
        if (first) KB.setCursor(first);
      }
    } else renderAll(d);
  }

  $('#acctview').onchange = load;
  $('#fyq').onchange = () => { setYear($('#fyq').value); load(); };
  $('#book').onchange = load;
  $('#owing').onchange = load;
  $('#everyone').onchange = load;
  $('#csv').onclick = () => location.href =
    firmUrl(($('#pid').value ? '/api/export/ledger?' : '/api/export/customers?')
    + new URLSearchParams($('#pid').value
      ? { ledger_id: $('#pid').value, from: $('#f1').value, to: $('#f2').value,
          view: $('#acctview').value === 'entries' ? 'entries' : 'months' }
      : params()));
  await load();          // a named customer fills the box in from the report
});

/* ======================================== one month of one account ------
   Asked for on 28 Sep 2026: "after opening a particular customer's account
   it shows the months and when clicking on the month everything in that
   month shows as a dropbox. Instead take me to a new page as per the month
   the user selects."

   `#/accounts/month?ledger_id=X&month=YYYY-MM[&scope=book]`. The month is
   the account's own report asked for the first of the month to its last
   day -- to today, for this month, which is where the summary row stops too
   -- so the figures are the row's: the balance the month opened on, its
   debits and credits, the balance it closed on. It is drawn as the Datewise
   view draws, latest entry first. The months of its year stand along the
   top as they do on a bank statement, ‹ Prev and Next › either side, and
   ← → step them. Back or Esc returns to the account, on that month's line. */
route(/^\/accounts\/month$/, async (view, _m, q) => {
  const lid = num(q.ledger_id);
  const scope = q.scope === 'book' ? 'book' : '';
  const now = today().slice(0, 7);
  // As far back as the Year boxes go is as far back as there is anything.
  const first = parseInt(fyChoices().slice(-1)[0], 10) + '-04';
  const valid = ym => /^\d{4}-\d{2}$/.test(ym || '');
  let month = !valid(q.month) || q.month > now ? now : q.month < first ? first : q.month;
  setTitle('Account', monthWord(month));
  if (!lid) {
    view.innerHTML = `<div class="card"><div class="empty"><b>No account chosen</b>
      Open one from <a href="#/accounts">Accounts</a>, then a month in it.</div></div>`;
    return;
  }
  view.innerHTML = `<div class="card"><div class="monthnav acctmonth noprint" id="mnav"></div></div>
    <div id="out"></div>`;

  const mem = screenState('/accounts/month', {
    ledger_id: { get: () => lid, set: () => {} },
    month: { get: () => month, set: () => {} },
    scope: { get: () => scope, set: () => {} }
  });

  const shift = (ym, n) => {
    const d = new Date(+ym.slice(0, 4), +ym.slice(5, 7) - 1 + n, 1);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  };
  /** The first and last day a month is read between. */
  const range = ym => {
    const end = ym + '-' + String(new Date(+ym.slice(0, 4), +ym.slice(5, 7), 0).getDate())
      .padStart(2, '0');
    const t = today();
    return { from: ym + '-01', to: t < end ? t : end };
  };
  const short = ym => new Date(+ym.slice(0, 4), +ym.slice(5, 7) - 1, 1)
    .toLocaleString('en-IN', { month: 'short' }) + ' ’' + ym.slice(2, 4);

  /* How many entries each month of the year holds, for the buttons along
     the top -- read once a year, not once a month. */
  let counts = {}, countsFy = '';
  async function yearCounts(fy) {
    if (countsFy === fy) return;
    const r = fyRange(fy);
    const d = await api.get('/api/reports/customers',
      { ledger_id: lid, from: r.from, to: r.to < today() ? r.to : today(), scope });
    counts = {};
    (d.months || []).forEach(m => { counts[m.month] = m.count; });
    countsFy = fy;
  }

  /* **‹ Prev and Next › are put up once and never drawn again** (28 Sep
     2026: "if there is a blank month in between then quick switching for
     months has some issue with the keys"). Redrawing them on every step
     took the keyboard off the one it stood on, so the next Enter went
     nowhere. Only what they say changes; the months between them are
     redrawn, and nothing stands on those but the mouse. */
  $('#mnav').innerHTML = `
    <button type="button" id="m-prev">‹ Prev</button>
    <div class="mchips" role="tablist"></div>
    <button type="button" id="m-next">Next ›</button>`;
  const prevOf = () => (month > first ? shift(month, -1) : '');
  const nextOf = () => (month < now ? shift(month, 1) : '');
  $('#m-prev').onclick = () => { const to = prevOf(); if (to) go(to, -1); };
  $('#m-next').onclick = () => { const to = nextOf(); if (to) go(to, 1); };

  function drawNav() {
    const ms = monthsOfFy(fyLabel(month + '-01')).filter(ym => ym <= now);
    const prev = prevOf(), next = nextOf();
    const known = countsFy === fyLabel(month + '-01');
    const p = $('#m-prev'), n = $('#m-next');
    // A button the keyboard stands on is not switched off under it at the
    // end of the line; pressing it there simply does nothing.
    p.disabled = !prev && document.activeElement !== p;
    n.disabled = !next && document.activeElement !== n;
    p.title = prev ? monthWord(prev) : 'Nothing earlier';
    n.title = next ? monthWord(next) : 'This is the latest month';
    $('#mnav .mchips').innerHTML = ms.map(ym => {
      const c = known ? counts[ym] || 0 : null;
      return `<button type="button" tabindex="-1" role="tab" data-ym="${ym}"
        class="mchip${ym === month ? ' on' : ''}${c === 0 ? ' none' : ''}"
        aria-selected="${ym === month}"
        title="${esc(monthWord(ym))}${c === null ? '' : ' — ' + counted(c, 'entry', 'entries')}">
        <b>${esc(short(ym))}</b><small>${c === null ? '&nbsp;' : c || '—'}</small></button>`;
    }).join('');
    // Clicked, a month does not take the keyboard off the line it is on.
    $$('#mnav .mchip').forEach(b => {
      b.onmousedown = e => e.preventDefault();
      b.onclick = () => go(b.dataset.ym, 0);
    });
  }

  /* The name and the buttons are put up once; a new month only changes the
     line under the name, so a button the keyboard stands on stays put. */
  let party = null;
  function titleBar() {
    if ($('#page-actions').childElementCount) {
      const sub = $('#page-title .sub');
      if (sub) sub.textContent = sub.title = monthWord(month);
      return;
    }
    setTitle(party.name, monthWord(month), [{
      // This month -- or any dates -- as a PDF to send the customer.
      label: '📄 Statement PDF', cls: 'primary',
      onClick: () => statementDialog(party, Object.assign({ scope }, range(month)))
    }, {
      label: '⤓ CSV',
      onClick: () => {
        const r = range(month);
        location.href = firmUrl('/api/export/ledger?' + new URLSearchParams(
          { ledger_id: lid, from: r.from, to: r.to, view: 'entries' }));
      }
    }]);
  }

  /* **Turning the months quickly.** The month on the screen stays there,
     dimmed, until the next one has been read -- wiping it took away the line
     the keyboard stood on, and the next ← landed somewhere else. The month
     is only asked for once the keys stop for a moment, so running ← ← ←
     across a year reads the one it stops on, not every month on the way. */
  let seq = 0, drawn = false;
  async function load() {
    const mine = ++seq;
    mem.save();
    const here = onScreen();
    drawNav();
    if (party) titleBar();
    if (drawn) {
      $('#out').classList.add('turning');
      await new Promise(res => setTimeout(res, 160));
      if (mine !== seq || !here()) return;
    } else {
      $('#out').innerHTML = '<div class="card"><div class="empty">Reading the month…</div></div>';
    }
    const r = range(month);
    let d;
    try {
      [d] = await Promise.all([
        api.get('/api/reports/customers', { ledger_id: lid, from: r.from, to: r.to, scope }),
        // Only the buttons along the top need this; the month stands without it.
        yearCounts(fyLabel(month + '-01')).catch(() => {})]);
    } catch (e) {
      if (mine !== seq || !here()) return;
      bad(e.message);
      drawn = false;
      $('#out').classList.remove('turning');
      $('#out').innerHTML = '<div class="card"><div class="empty"><b>Could not read the month</b>'
        + esc(e.message) + '</div></div>';
      return;
    }
    if (mine !== seq || !here()) return;
    party = d.party;
    titleBar();
    drawNav();
    $('#out').classList.remove('turning');
    $('#out').innerHTML = acctHtml(d, { byMonth: false, from: r.from, to: r.to,
                                        heading: monthWord(month) });
    acctBind();
    drawn = true;
  }

  /** To another month. The keyboard goes with it: from a line, onto the new
   *  month's latest line -- a month with nothing in it still has its Opening
   *  Balance line, so ← → go on turning. On ‹ Prev or Next ›, it stays there. */
  async function go(ym, dir) {
    if (ym === month) return;
    const here = onScreen();
    // Where the keyboard is, not which line is lit: the lit line stays lit
    // while the keyboard is up on ‹ Prev, and taking the keyboard down to it
    // there made the next Enter open a challan instead of turning the month.
    const ae = document.activeElement;
    const onRow = !!(ae && ae.matches && ae.matches('#view tbody tr'));
    month = ym;
    await load();
    if (month !== ym || !here() || !onRow) return;
    const rs = KB.rows();
    if (rs.length) { KB.setCursor(rs[0]); return; }
    const b = $(dir < 0 ? '#m-prev:not([disabled])' : '#m-next:not([disabled])')
      || $('#m-prev:not([disabled]), #m-next:not([disabled])');
    if (b) b.focus();
  }

  await load();
});

/* ============================================== challan stock -----------
   Asked for on 26 Sep 2026: "create a stock management for challans as
   well. These are the different types of steels the user will have in
   stock. For example CRCA - REGULAR - 0.50 ... for just challans under BST."
   And, asked which way: the challans are made in this app, steel comes in by
   a stock-in entry made here, and the count starts with the stock in hand
   today.

   A piece of stock is a type, a quality and a thickness -- the three columns
   of the user's sheet, kept as three lists (Steel list). **In hand = opening
   stock + stock in − challans**, in kilograms, in the challan firm whatever
   firm the screen is in. The opening and the stock in are entries made here;
   the challans are their own lines, so editing or deleting a challan moves
   the stock with it. Opening a line shows everything behind it. */

/** The steel a stock line or an entry line names, as one name. */
const steelName = x => [x.steel_type, x.steel_quality, x.steel_thick].join(' - ');

/* ---- a steel box ---------------------------------------------------------
   Type, quality and thickness are typed and picked, not dropped down (26 Sep
   2026: "the Stock in entry has some issue while entering the stock
   details"). A dropdown reads keys only as the first letters of an option,
   and only if they come quickly: "1.2" typed at an ordinary pace landed on
   2.00 and "0.5" stayed on 0.20 -- the wrong steel into stock without a word
   -- and ↑ ↓ go to the next box in this app, not down the list. Now each box
   narrows its list as it is typed, the way the customer and product boxes
   do: ↓ ↑ along what is left, Enter takes it and goes on to the next box. A
   figure is read as a figure -- 0.5, .5 and 0.50 are all 0.50 -- and a box
   holding anything that is not on the list says so in red and counts as
   empty. */

/** The list's own name for what was typed, or '' if there is none. */
function steelHit(list, text) {
  const t = String(text || '').trim().toUpperCase();
  if (!t) return '';
  const same = list.find(v => v.toUpperCase() === t);
  if (same) return same;
  if (/^\d*\.?\d+$/.test(t)) {
    const n = parseFloat(t);
    return list.find(v => /^\d*\.?\d+$/.test(v) && Math.abs(parseFloat(v) - n) < 1e-9) || '';
  }
  return '';
}
/** What is left on the list for what has been typed: its own name first,
 *  then those beginning with it, then those holding it anywhere. */
function steelMatches(list, text) {
  const t = String(text || '').trim().toUpperCase();
  if (!t) return list.slice();
  const hit = steelHit(list, t);
  const starts = list.filter(v => v !== hit && v.toUpperCase().startsWith(t));
  const inside = list.filter(v => v !== hit && starts.indexOf(v) < 0
                                 && v.toUpperCase().includes(t));
  return (hit ? [hit] : []).concat(starts, inside);
}
/** Make `input` a steel box over `list`; `take(name)` hears each choice
 *  ('' when the box is emptied or holds something not on the list). */
function steelCombo(input, list, take) {
  const scope = () => input.closest('.modal .bd') || $('#view');
  const mark = () => {
    const t = input.value.trim();
    const wrong = !!t && !steelHit(list, t);
    input.classList.toggle('wrong', wrong);
    input.title = wrong ? '“' + t + '” is not on the steel list — pick one from the list' : '';
  };
  const choose = v => {
    input.value = v;
    take(v);
    mark();
    moveOn(input, scope());
  };
  // Enter is read here first, against what is typed now. The list below the
  // box is filled a moment after each key, and a quick "0.5" and Enter would
  // otherwise take the top of the list as it stood before the 5.
  input.addEventListener('keydown', e => {
    if (e.key !== 'Enter' || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return;
    const t = input.value.trim();
    if (!t) return;                    // empty: Enter goes on, leaving it empty
    const m = steelMatches(list, t);
    const lit = handle.lit();
    const pick = lit && m.indexOf(lit.v) >= 0 ? lit.v : m[0];
    e.preventDefault();
    e.stopImmediatePropagation();
    handle.close();
    if (pick) { choose(pick); return; }
    // Nothing on the list is like it: stay here, in red, rather than take
    // whatever the list under the box last showed.
    take('');
    mark();
    input.select();
  }, true);
  const handle = attachPicker(input, {
    source: async term => steelMatches(list, term).map(v => ({ v })),
    render: r => '<b>' + esc(r.v) + '</b>',
    onPick: r => choose(r.v)
  });
  const settle = () => {
    const hit = steelHit(list, input.value);
    if (hit) input.value = hit;
    take(hit);
    mark();
  };
  input.addEventListener('change', settle);
  // Typing again takes the red off; leaving the box or Enter reads it again.
  input.addEventListener('input', () => input.classList.remove('wrong'));
  mark();
}
const steelInput = (k, value, holder) => `<input data-steel="${k}" class="steelbox"
  autocomplete="off" spellcheck="false" value="${esc(value || '')}" placeholder="${holder}">`;

/** Opening stock, or steel come in: the date, where from, and a line for
 *  each steel with its kilos. `id` edits one already made. Answers true when
 *  something was saved or deleted. */
async function steelEntryDialog(id, kind, preset) {
  const lists = await api.get('/api/steel/lists');
  let e = { kind: kind || 'IN', entry_date: today(), party: '', ref_no: '', note: '', lines: [] };
  if (id) e = await api.get('/api/steel/entries/' + id);
  // A removal is kept as minus kilos and a count as the change it made; the
  // form shows what was typed -- the kilos taken out, the kilos found.
  const shown = l => (e.kind === 'COUNT' ? l.counted : e.kind === 'OUT' ? -num(l.weight) : l.weight);
  // Opened from a steel's own line, the form starts with that steel.
  const rows = e.lines.length ? e.lines.map(l => Object.assign({}, l, { weight: shown(l) }))
    : [Object.assign({ steel_type: '', steel_quality: '', steel_thick: '', weight: '' }, preset || {})];
  let changed = false, stockOf = {}, dlg = null, saveBtn = null;
  const KIND = {
    IN: { title: 'Add stock', seg: '＋ Steel arrived', who: 'Supplier', holder: 'Where it came from', box: 'Kg to add',
      say: 'Steel that has come into the yard. Challans take it out again by themselves.' },
    OPENING: { title: 'Opening stock', seg: 'Opening stock', who: '', holder: '', box: 'Kg you have',
      say: 'The steel in the yard on this day, before anything else — the stock is counted from here. Use it once, at the start.' },
    OUT: { title: 'Remove stock', seg: '− Remove', who: 'Why', holder: 'Damaged, scrap, short…', box: 'Kg to remove',
      say: 'Steel that went out without a challan — damaged, scrap, used, short.' },
    COUNT: { title: 'Correct stock', seg: '✎ Correct', who: 'Why', holder: 'Weighed, stock count…', box: 'Real kg you have',
      say: 'Weighed or counted it? Type the real kilos you have of each steel — the app changes the stock to match. Type 0 if none is left.' }
  };
  const K = () => KIND[$('#se-kind').value];
  const keyOf = l => [l.steel_type, l.steel_quality, l.steel_thick].join('|');
  const done = l => l.steel_type && l.steel_quality && l.steel_thick;
  const typed = v => String(v === null || v === undefined ? '' : v).trim() !== '';

  const lineHtml = (l, i) => `<tr data-i="${i}">
      <td class="c" style="color:var(--muted);font-size:12px">${i + 1}</td>
      <td>${steelInput('steel_type', l.steel_type, 'Type…')}</td>
      <td>${steelInput('steel_quality', l.steel_quality, 'Quality…')}</td>
      <td>${steelInput('steel_thick', l.steel_thick, 'Thickness…')}</td>
      <td class="r num" data-c="have"></td>
      <td><input data-k="weight" class="num" value="${l.weight === '' || l.weight == null ? '' : esc(String(l.weight))}" placeholder="kg"></td>
      <td class="r num" data-c="after"></td>
      <td class="c"><button type="button" class="link" data-drop style="color:var(--bad)" title="Take this line off">✕</button></td></tr>`;

  const body = `
    <div class="seg bigseg" id="se-kinds" role="group" aria-label="What to do">${Object.keys(KIND).map(k =>
      `<button type="button" data-kind="${k}" class="k-${k.toLowerCase()}">${KIND[k].seg}</button>`).join('')}</div>
    <p class="stocksay" id="se-say" style="margin:8px 0 0"></p>
    <div class="row sehead" style="margin-top:10px">
      <input type="hidden" id="se-kind" value="${esc(e.kind)}">
      <div><label>Date</label><input type="date" id="se-date" value="${esc(e.entry_date)}"></div>
      <div style="flex:1;min-width:200px" id="se-whobox"><label id="se-wholab">Supplier</label><input id="se-party" value="${esc(e.party)}"></div>
      <div id="se-refbox"><label>Reference</label><input id="se-ref" value="${esc(e.ref_no)}" placeholder="Bill / lot no." style="width:150px"></div>
    </div>
    <div class="tbl-scroll" style="margin-top:12px"><table class="lineitems steelgrid"><thead><tr>
      <th style="width:30px"></th><th>Type</th><th>Quality</th><th>Thickness</th>
      <th class="r" style="width:120px">In stock now</th>
      <th class="r" style="width:135px" id="se-kghead">Kg</th>
      <th class="r" style="width:130px">After saving</th><th style="width:34px"></th></tr></thead>
      <tbody id="se-lines"></tbody></table></div>
    <div class="row" style="margin-top:10px;align-items:center">
      <button type="button" id="se-add">＋ Another steel</button>
      <span class="hint" style="margin:0 0 0 8px">or press Enter in the last Kg box</span>
      <div class="spacer"></div><b id="se-total"></b></div>
    <div style="margin-top:12px"><label>Note <span class="opt">optional</span></label><input id="se-note" value="${esc(e.note)}"></div>
    <p class="hint" id="se-err"></p>`;

  // The steel on each line is what its boxes have taken (steelCombo); only
  // the kilos are read off the box as typed.
  function readLines() {
    $$('#se-lines tr').forEach(tr => {
      rows[num(tr.dataset.i)].weight = $('[data-k=weight]', tr).value;
    });
  }
  const STEEL_LIST = { steel_type: lists.types, steel_quality: lists.qualities,
                       steel_thick: lists.thicknesses };
  /** What one line does to the stock: what it finds, and what is left. */
  function effect(l) {
    if (!done(l)) return null;
    const have = num(stockOf[keyOf(l)]);
    if (!typed(l.weight)) return { have, after: null, change: 0 };
    const w = num(String(l.weight).replace(/,/g, ''));
    const k = $('#se-kind').value;
    const change = k === 'OUT' ? -w : k === 'COUNT' ? w - have : w;
    return { have, after: have + change, change };
  }
  function paintLine(tr) {
    const fx = effect(rows[num(tr.dataset.i)]);
    $('[data-c=have]', tr).innerHTML = fx ? `<span class="${fx.have < -0.0005 ? 'neg' : ''}">${qty(fx.have)}</span>` : '';
    let after = '';
    if (fx && fx.after !== null) {
      after = `<b class="${fx.after < -0.0005 ? 'neg' : ''}">${qty(fx.after)}</b>` +
        ($('#se-kind').value === 'COUNT' ? `<div style="font-size:12px;color:var(--muted)">${Math.abs(fx.change) < 0.0005
          ? 'no change' : (fx.change > 0 ? '+' : '−') + qty(Math.abs(fx.change)) + ' kg'}</div>`
          : fx.after < -0.0005 ? '<div style="font-size:12px;color:var(--bad)">more than there is</div>' : '');
    }
    $('[data-c=after]', tr).innerHTML = after;
  }
  /** Save says what it will do, in the words of the choice at the top. */
  function total() {
    const k = $('#se-kind').value;
    let plus = 0, minus = 0, n = 0;
    rows.forEach(l => {
      const fx = effect(l);
      if (fx && fx.after !== null) {
        n++;
        if (fx.change > 0) plus += fx.change; else minus -= fx.change;
      }
    });
    $('#se-total').textContent = '';
    if (!saveBtn) return;
    saveBtn.textContent = !n ? (id ? 'Save changes' : 'Save')
      : k === 'IN' ? 'Add ' + qty(plus) + ' kg'
      : k === 'OPENING' ? 'Save opening stock — ' + qty(plus) + ' kg'
      : k === 'OUT' ? 'Remove ' + qty(minus) + ' kg'
      : 'Correct ' + (n === 1 ? 'the stock' : n + ' steels');
  }
  function paintAll() { $$('#se-lines tr').forEach(paintLine); total(); }
  function drawLines(focusI) {
    $('#se-lines').innerHTML = rows.map(lineHtml).join('');
    $$('#se-lines tr').forEach(tr => {
      const l = rows[num(tr.dataset.i)];
      $$('input[data-steel]', tr).forEach(x =>
        steelCombo(x, STEEL_LIST[x.dataset.steel], v => {
          l[x.dataset.steel] = v;
          paintLine(tr);
          total();
        }));
      $('[data-k=weight]', tr).addEventListener('input', () => { readLines(); paintLine(tr); total(); });
      $('[data-drop]', tr).onclick = () => {
        readLines();
        rows.splice(num(tr.dataset.i), 1);
        if (!rows.length) rows.push({ steel_type: '', steel_quality: '', steel_thick: '', weight: '' });
        drawLines(Math.min(num(tr.dataset.i), rows.length - 1));
      };
      // Enter in the last line's kilos, with kilos in it, is the next line.
      const kg = $('[data-k=weight]', tr);
      kg.addEventListener('keydown', ev => {
        if (ev.key !== 'Enter' || ev.shiftKey || ev.ctrlKey || ev.altKey) return;
        if (num(tr.dataset.i) !== rows.length - 1 || !kg.value.trim()) return;
        ev.preventDefault();
        addLine();
      });
      paintLine(tr);
    });
    total();
    if (focusI !== undefined) {
      const tr = $$('#se-lines tr')[focusI];
      const box = tr && $('input[data-steel]', tr);
      if (box) box.focus();
    }
  }
  function addLine() {
    readLines();
    const above = rows[rows.length - 1] || {};
    // A lot is mostly one steel in several thicknesses.
    rows.push({ steel_type: above.steel_type || '', steel_quality: above.steel_quality || '',
                steel_thick: '', weight: '' });
    drawLines(rows.length - 1);
    const tr = $$('#se-lines tr')[rows.length - 1];
    const thick = tr && $('[data-steel=steel_thick]', tr);
    if (thick && rows[rows.length - 1].steel_type) thick.focus();
  }
  /** What each steel held on the entry's date, leaving this entry out --
   *  again whenever the date changes. */
  async function loadStock() {
    const d = await api.get('/api/steel/stock', { as_on: $('#se-date').value || today(), skip: id || '' });
    stockOf = {};
    d.rows.forEach(r => { stockOf[keyOf(r)] = num(r.balance); });
    paintAll();
  }
  function sayKind() {
    const kind = $('#se-kind').value, k = K();
    $$('#se-kinds button').forEach(b => b.classList.toggle('on', b.dataset.kind === kind));
    $('#se-say').textContent = k.say;
    // A supplier and a reference only for steel that arrived; a reason
    // for steel taken out or corrected; nothing more for an opening.
    $('#se-whobox').style.display = k.who ? '' : 'none';
    $('#se-wholab').textContent = k.who;
    $('#se-party').placeholder = k.holder;
    $('#se-refbox').style.display = kind === 'IN' ? '' : 'none';
    $('#se-kghead').textContent = k.box;
    const h = dlg && $('h2', dlg);
    if (h && !id && h.firstChild) h.firstChild.textContent = k.title;
    paintAll();
  }

  const buttons = [{ label: 'Cancel', value: null }];
  if (id) {
    buttons.push({ label: 'Delete', cls: 'danger', before: async () => {
      if (!await confirmBox('Delete this entry', 'Delete this ' + e.what.toLowerCase()
          + ' entry? Challan Stock goes back to what it was without it.')) return false;
      try {
        await api.del('/api/steel/entries/' + id);
        ok('Entry deleted — Challan Stock is back as it was.');
        changed = true;
        return true;
      } catch (err) { bad(err.message); return false; }
    } });
  }
  buttons.push({ label: 'Save', cls: 'primary', before: async () => {
    readLines();
    const payload = {
      kind: $('#se-kind').value, entry_date: $('#se-date').value,
      party: $('#se-party').value, ref_no: $('#se-ref').value, note: $('#se-note').value,
      lines: rows.map(l => Object.assign({}, l, { weight: String(l.weight === null || l.weight === undefined
        ? '' : l.weight).replace(/,/g, '') }))
    };
    try {
      const saved = id ? await api.put('/api/steel/entries/' + id, payload)
                       : await api.post('/api/steel/entries', payload);
      const net = saved.lines.reduce((s, l) => s + num(l.weight), 0);
      ok(saved.what + ' saved — ' + (Math.abs(net) < 0.0005 ? 'no change'
        : (net > 0 ? '+' : '−') + qty(Math.abs(net)) + ' kg') + '.');
      changed = true;
      return true;
    } catch (err) {
      $('#se-err').innerHTML = '<b style="color:var(--bad)">' + esc(err.message) + '</b>';
      bad(err.message);
      return false;
    }
  } });

  await modal(id ? e.what + ' — ' + dt(e.entry_date) : KIND[e.kind].title,
    body, buttons, {
      wide: true,
      onOpen: bg => {
        dlg = bg;
        saveBtn = $('.ft button.primary', bg);
        drawLines();
        $('#se-add').onclick = addLine;
        $$('#se-kinds button').forEach(b => {
          b.onclick = () => { $('#se-kind').value = b.dataset.kind; sayKind(); };
        });
        $('#se-date').addEventListener('change', () => { loadStock().catch(err => bad(err.message)); });
        sayKind();
        loadStock().catch(err => bad(err.message));
        // Opened from a steel's own line: straight to its kilos.
        if (preset && !id) {
          setTimeout(() => { const kg = $('#se-lines [data-k=weight]'); if (kg) kg.focus(); }, 60);
        }
      }
    });
  return changed;
}

/** The three lists the steel is picked from: one name a line, in the order
 *  they are to be offered. */
async function steelListDialog() {
  const lists = await api.get('/api/steel/lists');
  const box = (id, label, list) => `<div style="flex:1;min-width:170px">
      <label>${label}</label>
      <textarea id="${id}" rows="16" style="width:100%;font-family:var(--mono)">${esc(list.join('\n'))}</textarea></div>`;
  let saved = false;
  await modal('Steel list', `
    <p class="hint" style="margin-top:0">One name a line (Enter for the next line), in the order the
      boxes should offer them. Taking a name off a list leaves it on every entry and challan that
      already names it.</p>
    <div class="row" style="align-items:flex-start">
      ${box('sl-types', 'Steel types', lists.types)}
      ${box('sl-qualities', 'Qualities', lists.qualities)}
      ${box('sl-thick', 'Thicknesses', lists.thicknesses)}
    </div>`, [{ label: 'Cancel', value: null }, { label: 'Save', cls: 'primary', before: async () => {
      const split = id => $('#' + id).value.split('\n').map(s => s.trim()).filter(Boolean);
      try {
        await api.put('/api/steel/lists', { types: split('sl-types'),
          qualities: split('sl-qualities'), thicknesses: split('sl-thick') });
        ok('Steel list saved.');
        saved = true;
        return true;
      } catch (err) { bad(err.message); return false; }
    } }], {
      wide: true,
      // One name a line: Enter in a list starts the next line, as in any
      // notepad. In a dialog Enter otherwise walks on to the next box, which
      // left no way to add a name but Shift+Enter. The arrows and Tab still
      // go from list to list, and Ctrl+S saves.
      onOpen: bg => $$('textarea', bg).forEach(ta => ta.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.ctrlKey && !e.metaKey && !e.altKey) e.stopPropagation();
      }))
    });
  return saved;
}

route(/^\/steel$/, async (view, _m, q) => {
  const here = onScreen();
  const lists = await api.get('/api/steel/lists');
  if (!here()) return;
  const reload = () => { if (here()) load(); };
  // Made for anyone to use (27 Sep 2026): three plain buttons -- steel in,
  // steel out, steel put right -- with the opening stock one of the choices
  // inside Add, and every steel's line carrying the same three.
  const change = async (id, kind, preset) => { if (await steelEntryDialog(id, kind, preset)) reload(); };
  setTitle('Challan Stock', 'The steel your challans go out of — by type, quality and thickness, in kilos', [
    { label: '＋ Add stock', cls: 'primary', onClick: () => change(null, 'IN') },
    { label: '− Remove stock', onClick: () => change(null, 'OUT') },
    { label: '✎ Correct stock', onClick: () => change(null, 'COUNT') },
    { label: '⚙ Steel names', onClick: async () => { if (await steelListDialog()) render(); } }
  ]);
  const pick = (id, label, list, all) => `<div id="${id}-box"><label>${label}</label>
    <select id="${id}"><option value="">${all}</option>${list.map(v =>
      `<option>${esc(v)}</option>`).join('')}</select></div>`;
  view.innerHTML = `<div class="card"><div class="bd"><div class="row">
    ${fySelect('fyq', { asOn: true })}
    <div><label>As on</label><input type="date" id="f2" value="${today()}"></div>
    ${pick('st-type', 'Type', lists.types, 'All types')}
    ${pick('st-q', 'Quality', lists.qualities, 'All qualities')}
    <select id="st-view" hidden><option value="stock"></option><option value="entries"></option></select>
    <input type="hidden" id="st-sel">
    <div id="st-emptybox"><label>&nbsp;</label><label style="margin:0;text-transform:none;font-size:13px"
      title="Steel whose stock has all gone out still has a line, so its history can be opened.">
      <input type="checkbox" id="st-all" style="width:auto"> Steel with none left too</label></div>
  </div></div></div><div id="out"></div>`;

  const mem = screenState('/steel', {
    as_on: '#f2', type: '#st-type', quality: '#st-q', view: '#st-view', all: '#st-all',
    sel: '#st-sel', fy: fyField(null, 'f2')
  }).restore(q);

  const kg = v => qty(v) + ' kg';
  const params = () => ({ as_on: $('#f2').value, type: $('#st-type').value,
                          quality: $('#st-q').value });
  const small = t => '<div style="font-size:12px;color:var(--muted)">' + t + '</div>';
  // One set of column widths for the stock table and every history opened
  // under it, so a history's kilos stand under the headings they belong to:
  // an opening under Opening, a stock in under Stock in, a challan under
  // Challans out, and what was left under In hand.
  // Steel removed or counted by hand has a column of its own -- in the stock
  // table and in every history under it -- only once there is some.
  let byHand = false;
  // The last column is each steel's own Add / Remove / Correct.
  const COLS = () => (byHand
    ? `<colgroup><col style="width:38px"><col><col style="width:10%"><col style="width:10%">
      <col style="width:10%"><col style="width:10%"><col style="width:11%"><col style="width:250px"></colgroup>`
    : `<colgroup><col style="width:38px"><col><col style="width:11%">
      <col style="width:11%"><col style="width:11%"><col style="width:12%"><col style="width:250px"></colgroup>`);
  const acts = r => `<td class="r nw rowacts">
      <button type="button" class="sm" data-do="IN" title="Add stock of this steel">＋ Add</button>
      <button type="button" class="sm" data-do="OUT" title="Take this steel out">− Remove</button>
      <button type="button" class="sm" data-do="COUNT" title="Set this steel to the real kilos you have">✎ Correct</button></td>`;
  /** The two tabs: what is in hand, and every entry made here. */
  const tabs = active => `<div class="seg stocktabs" role="group" aria-label="Show">
      <button type="button" data-tab="stock"${active === 'stock' ? ' class="on"' : ''}>Stock in hand</button>
      <button type="button" data-tab="entries"${active === 'entries' ? ' class="on"' : ''}>Stock added &amp; changes</button></div>`;
  function bindTabs() {
    $$('#out [data-tab]').forEach(b => {
      b.onclick = () => {
        if ($('#st-view').value === b.dataset.tab) return;
        $('#st-view').value = b.dataset.tab;
        load();
      };
    });
  }
  const signed = v => (Math.abs(num(v)) < 0.0005 ? ''
    : (num(v) > 0 ? '+' : '−') + qty(Math.abs(num(v))));

  /* **A steel opened, the way billing programs open an item** (27 Sep
     2026): the list narrowed to the left, the steel's page on the right --
     what is in hand, its own Add / Remove / Correct, and every opening, stock
     in, challan and change with the kilos left after each. ✕ or Esc puts the
     list back. It replaced the history folded open under a line. */
  const keyOf = r => [r.steel_type, r.steel_quality, r.steel_thick].join('|');
  const trio = r => ({ steel_type: r.steel_type, steel_quality: r.steel_quality, steel_thick: r.steel_thick });
  let lastStock = null;
  function closeSel() {
    $('#st-sel').value = '';
    mem.save();
    if (lastStock && $('#st-view').value !== 'entries') drawStock(lastStock);
  }
  const escHook = () => KB.setEscape(() => !!$('#st-sel').value && $('#st-view').value !== 'entries', closeSel);
  escHook();

  function drawSteelSplit(d, r) {
    const shown = d.rows.filter(x => Math.abs(x.balance) > 0.0005 || keyOf(x) === keyOf(r));
    $('#out').innerHTML = `${tabs('stock')}
      <p class="stocksay">Everything that moved this steel, newest first. Press a challan to open it,
        or a stock entry to change or delete it.</p>
      <div class="stocksplit">
        <div class="card splitlist"><div class="splitscroll"><table class="picklist"><tbody>
          ${shown.map(x => `<tr class="clickable${keyOf(x) === keyOf(r) ? ' on' : ''}" data-key="${esc(keyOf(x))}">
            <td><b>${esc(x.name)}</b></td>
            <td class="r num nw${x.balance < -0.0005 ? ' neg' : ''}"><b>${qty(x.balance)}</b></td></tr>`).join('')}
        </tbody></table></div></div>
        <div class="card splitdetail">
          <div class="sdhead">
            <div style="min-width:0"><div class="sdname">${esc(r.name)}</div>
              <div class="sdsub">Challan Stock · as on ${dt(d.as_on)}</div></div>
            <button type="button" class="sm" data-close title="Back to the whole list (Esc)">✕ Close</button>
          </div>
          <div class="sdfigs">
            <div class="sdfig main"><div class="lb">In hand</div><div class="vl">${qty(r.balance)} kg</div></div>
            <div class="sdfig"><div class="lb">Opening</div><div class="vl">${qty(r.opening)}</div></div>
            <div class="sdfig"><div class="lb">Stock in</div><div class="vl">${qty(r.stock_in)}</div></div>
            <div class="sdfig"><div class="lb">Challans out</div><div class="vl">${qty(r.out)}</div></div>
            ${Math.abs(r.by_hand) > 0.0005 ? `<div class="sdfig"><div class="lb">By you</div>
              <div class="vl">${signed(r.by_hand)}</div></div>` : ''}
          </div>
          <div class="sdacts">
            <button type="button" class="primary" data-do="IN">＋ Add stock</button>
            <button type="button" data-do="OUT">− Remove stock</button>
            <button type="button" data-do="COUNT">✎ Correct stock</button>
          </div>
          <div id="sd-moves"><div class="empty">Reading its history…</div></div>
        </div>
      </div>`;
    bindTabs();
    $('#out [data-close]').onclick = closeSel;
    $$('#out .sdacts [data-do]').forEach(b => { b.onclick = () => change(null, b.dataset.do, trio(r)); });
    $$('#out .picklist tr[data-key]').forEach(tr => {
      tr.onclick = () => { $('#st-sel').value = tr.dataset.key; mem.save(); drawStock(d); };
    });
    const on = $('#out .picklist tr.on');
    if (on) on.scrollIntoView({ block: 'nearest' });
    escHook();
    steelMoves(r);
  }
  async function steelMoves(r) {
    const key = keyOf(r);
    let d;
    try {
      d = await api.get('/api/steel/item', { type: r.steel_type, quality: r.steel_quality,
                                            thick: r.steel_thick, as_on: $('#f2').value });
    } catch (err) {
      if (here() && $('#sd-moves')) $('#sd-moves').innerHTML = '<div class="empty">' + esc(err.message) + '</div>';
      return;
    }
    if (!here() || $('#st-sel').value !== key || !$('#sd-moves')) return;
    const tag = k => ({ OPENING: 'warn', IN: 'ok', CHALLAN: 'info', OUT: 'bad', COUNT: 'warn' })[k] || '';
    $('#sd-moves').innerHTML = `
      <div class="sdsum">${d.moves.length} movement${d.moves.length === 1 ? '' : 's'} ·
        <b>+${qty(d.total_in)}</b> in · <b>−${qty(d.total_out)}</b> out</div>
      <div class="tbl-scroll"><table class="sdmoves"><thead><tr><th>Date</th><th>What</th><th>No.</th>
        <th>Party / why</th><th class="r">In kg</th><th class="r">Out kg</th><th class="r">Stock after</th></tr></thead><tbody>
      ${d.moves.slice().reverse().map(m => `<tr class="clickable" ${m.kind === 'CHALLAN'
          ? 'data-href="' + esc(docLink('SALE', m.doc_id, m.company_id)) + '" title="Open this challan"'
          : 'data-entry="' + m.entry_id + '" title="Open this entry"'}>
        <td class="nw">${dt(m.date)}</td>
        <td class="nw"><span class="tag ${tag(m.kind)}">${esc(m.kind === 'CHALLAN' ? 'Challan' : m.what)}</span></td>
        <td class="nw">${m.kind === 'CHALLAN' ? goLink(docLink('SALE', m.doc_id, m.company_id), esc(m.no)) : esc(m.ref || '')}</td>
        <td>${esc(m.party || '—')}${m.counted !== null && m.counted !== undefined
          ? '<div class="subfig">weighed ' + qty(m.counted) + '</div>' : ''}${m.note ? '<div class="subfig">' + esc(m.note) + '</div>' : ''}</td>
        <td class="r num">${m.kg_in ? qty(m.kg_in) : ''}</td>
        <td class="r num">${m.kg_out ? qty(m.kg_out) : ''}</td>
        <td class="r num"><b class="${m.balance < -0.0005 ? 'neg' : ''}">${qty(m.balance)}</b></td></tr>`).join('')
        || '<tr><td colspan="7" style="color:var(--muted)">Nothing yet.</td></tr>'}
      </tbody></table></div>`;
    $$('#sd-moves tr[data-entry]').forEach(tr => { tr.onclick = () => change(num(tr.dataset.entry)); });
  }

  function drawStock(d) {
    lastStock = d;
    const sel = $('#st-sel').value;
    const chosen = sel ? d.rows.find(r => keyOf(r) === sel) : null;
    if (chosen) { drawSteelSplit(d, chosen); return; }
    if (sel) $('#st-sel').value = '';
    const all = $('#st-all').checked;
    const rows = d.rows.filter(r => all || Math.abs(r.balance) > 0.0005);
    // Steel with none left is not listed unless asked for -- but it is still
    // part of what came in and went out, so it has a line of its own at the
    // foot and the columns add up to the figures at the top.
    const gone = d.rows.filter(r => !all && Math.abs(r.balance) <= 0.0005);
    const sum = (list, k) => list.reduce((x, r) => x + r[k], 0);
    const short = d.rows.filter(r => r.balance < -0.0005);
    const t = d.totals;
    byHand = !!d.by_hand;
    const handCell = v => (byHand ? `<td class="r num">${signed(v)}</td>` : '');
    $('#out').innerHTML = `
      ${tabs('stock')}
      <p class="stocksay">${d.started ? `Stock added here goes in, and every challan made in this app
        takes its steel out by itself. When the real steel is different — damaged, scrap, weighed
        again — use <b>− Remove</b> or <b>✎ Correct</b> on the steel's line. Press a line to see
        everything that moved it.` : 'Nothing is kept here yet — start below.'}
        <a href="#/reports/stock">Stock of the GST bills is in Billing Stock →</a></p>
      <div class="kpis"${!d.started && !d.rows.length ? ' style="display:none"' : ''}>
        <div class="kpi ${t.balance < -0.0005 ? 'bad' : t.balance > 0.0005 ? 'good' : ''}">
          <div class="lb">In hand</div>
          <div class="vl">${esc(kg(t.balance))}</div>
          <div class="ft">${rows.length} item${rows.length === 1 ? '' : 's'} · as on
            <span class="nw">${dt(d.as_on)}</span></div></div>
        <div class="kpi"><div class="lb">Opening stock</div><div class="vl">${esc(kg(t.opening))}</div>
          <div class="ft">counted at the start</div></div>
        <div class="kpi"><div class="lb">Stock in</div><div class="vl">${esc(kg(t.stock_in))}</div>
          <div class="ft">entered since</div></div>
        <div class="kpi"><div class="lb">Challans out</div>
          <div class="vl">${esc(kg(t.out))}</div>
          <div class="ft">taken out on challans</div></div>
        ${byHand ? `<div class="kpi"><div class="lb">Removed or corrected</div>
          <div class="vl">${esc(signed(t.by_hand) || '0')} kg</div>
          <div class="ft">with the buttons here</div></div>` : ''}
      </div>
      ${short.length ? `<div class="card shortband"><div class="bd">
        <b>${short.length} steel item${short.length === 1 ? ' has' : 's have'} gone below nothing</b>
        — more went out than was entered:
        ${short.slice(0, 6).map(r => '<span class="nw"><b>' + esc(r.name) + '</b> short by '
          + esc(kg(-r.balance)) + '</span>').join(', ')}${short.length > 6 ? ', …' : ''}.
        Add the stock that came in, or press <b>✎ Correct</b> on its line if you know the real kilos.</div></div>` : ''}
      ${!d.started ? `<div class="card startcard"><div class="bd">
        <div class="stepno">1</div><div>
        <b>Start by entering the steel you have today</b> — one line per steel: type, quality,
        thickness and kilos.<br>
        <button type="button" class="primary" data-start style="margin-top:10px">Enter the steel I have now</button>
        <p class="hint" style="margin:10px 0 0">After that, <b>＋ Add stock</b> when steel arrives. Every
        challan made in this app takes its steel out by itself.</p></div></div></div>` : ''}
      ${!d.started && !d.rows.length ? '' : `<div class="card"><div class="bd flush tbl-scroll">${
        rows.length || gone.length ? `
        <table class="steeltbl">${COLS()}<thead><tr><th></th><th>Steel</th>
          <th class="r">Opening</th><th class="r">Stock in</th><th class="r">Challans out</th>
          ${byHand ? '<th class="r" title="Removed (−) or corrected (±) with the buttons here">By you</th>' : ''}
          <th class="r">In hand (kg)</th><th class="r">Change stock</th></tr></thead><tbody>
        ${rows.map((r, i) => `<tr class="clickable" data-i="${i}" data-t="${esc(r.steel_type)}"
            data-q="${esc(r.steel_quality)}" data-th="${esc(r.steel_thick)}" title="Open this steel — everything that moved it">
          <td class="nw" style="color:var(--muted)">›</td>
          <td><b>${esc(r.name)}</b>${r.challans ? small(r.challans + ' challan'
            + (r.challans === 1 ? '' : 's') + (r.last ? ' · last ' + dt(r.last) : '')) : ''}</td>
          <td class="r num">${r.opening ? qty(r.opening) : ''}</td>
          <td class="r num">${r.stock_in ? qty(r.stock_in) : ''}</td>
          <td class="r num">${r.out ? qty(r.out) : ''}</td>
          ${handCell(r.by_hand)}
          <td class="r num${r.balance < -0.0005 ? ' neg' : ''}"><b>${qty(r.balance)}</b></td>${acts(r)}</tr>`).join('')}
        ${gone.length ? `<tr class="clickable gone" data-gone="1" title="List them">
          <td></td><td><i>${gone.length} more with none left</i>${small('all gone out — Enter to list them')}</td>
          <td class="r num">${sum(gone, 'opening') ? qty(sum(gone, 'opening')) : ''}</td>
          <td class="r num">${sum(gone, 'stock_in') ? qty(sum(gone, 'stock_in')) : ''}</td>
          <td class="r num">${sum(gone, 'out') ? qty(sum(gone, 'out')) : ''}</td>
          ${handCell(sum(gone, 'by_hand'))}
          <td class="r num">0</td><td></td></tr>` : ''}
        </tbody><tfoot><tr><td></td><td>${rows.length + gone.length} item(s)</td>
          <td class="r num">${qty(t.opening)}</td>
          <td class="r num">${qty(t.stock_in)}</td>
          <td class="r num">${qty(t.out)}</td>
          ${byHand ? `<td class="r num">${signed(t.by_hand) || '0'}</td>` : ''}
          <td class="r num${t.balance < -0.0005 ? ' neg' : ''}"><b>${qty(t.balance)}</b></td><td></td></tr></tfoot></table>`
        : '<div class="empty"><b>No steel in hand</b>Nothing matches these choices.</div>'}
      </div></div>`}`;
    $$('#out tr[data-t]').forEach(tr => {
      tr.onclick = () => {
        $('#st-sel').value = [tr.dataset.t, tr.dataset.q, tr.dataset.th].join('|');
        mem.save();
        drawStock(d);
      };
    });
    escHook();
    const more = $('#out tr[data-gone]');
    if (more) more.onclick = () => { $('#st-all').checked = true; load(); };
    bindTabs();
    // A steel's own buttons open the form with that steel filled in.
    $$('#out tr[data-t] [data-do]').forEach(btn => {
      btn.onclick = ev => {
        ev.stopPropagation();
        const tr = btn.closest('tr');
        change(null, btn.dataset.do, { steel_type: tr.dataset.t, steel_quality: tr.dataset.q,
                                       steel_thick: tr.dataset.th });
      };
    });
    const start = $('#out [data-start]');
    if (start) start.onclick = () => change(null, 'OPENING');
  }

  function drawEntries(d) {
    $('#out').innerHTML = `${tabs('entries')}
      <p class="stocksay">Every entry made here — opening stock, stock added, removed and corrected.
        Press one to change it; <b>Delete</b> undoes it and the stock goes back as it was.</p>
      <div class="card"><div class="bd flush tbl-scroll" id="st-list">${d.rows.length ? `
      <table><thead><tr><th style="width:120px">Date</th><th style="width:150px">Entry</th>
        <th>Supplier / why</th><th>Steel</th><th class="r" style="width:140px">Kg</th><th style="width:120px"></th></tr></thead><tbody>
      ${d.rows.map(e => `<tr class="clickable" data-id="${e.id}" title="Open this entry">
        <td class="nw">${dt(e.entry_date)}</td>
        <td class="nw"><span class="tag ${e.kind === 'OUT' ? 'bad' : e.kind === 'COUNT' ? 'info'
          : 'ok'}">${esc(e.what)}</span></td>
        <td>${esc(e.party || '—')}${e.ref_no || e.note ? small(esc([e.ref_no ? 'ref ' + e.ref_no : '', e.note]
          .filter(Boolean).join(' · '))) : ''}</td>
        <td style="font-size:13.5px">${e.lines.slice(0, 4).map(l => '<span class="nw">' + esc(steelName(l))
          + ' <span style="color:var(--muted)">' + (e.kind === 'COUNT'
            ? 'counted ' + qty(l.counted) + ', ' + (signed(l.weight) || 'no change')
            : (e.kind === 'OUT' ? '−' + qty(-l.weight) : qty(l.weight))) + ' kg</span></span>').join('<br>')}${e.lines.length > 4
          ? '<br><span style="color:var(--muted)">+ ' + (e.lines.length - 4) + ' more</span>' : ''}</td>
        <td class="r num"><b>${e.kind === 'OUT' || e.kind === 'COUNT' ? signed(e.weight) || '0' : qty(e.weight)}</b></td>
        <td class="r nw"><button class="sm" data-edit>Edit</button>
          <button class="sm danger" data-act="del">Delete</button></td></tr>`).join('')}
      </tbody><tfoot><tr><td colspan="4">${d.count} entr${d.count === 1 ? 'y' : 'ies'} up to ${dt($('#f2').value)}
        <span style="font-weight:400;color:var(--muted)"> · in less out</span></td>
        <td class="r num"><b>${qty(d.weight)}</b></td><td></td></tr></tfoot></table>`
      : '<div class="empty"><b>No entries yet</b>＋ Add stock, − Remove stock and ✎ Correct stock at the top make them.</div>'}
      </div></div>`;
    $$('#out tr[data-id]').forEach(tr => {
      tr.onclick = async () => { if (await steelEntryDialog(num(tr.dataset.id))) reload(); };
      // Deleted the way every list deletes: one dialog, a copy of the books
      // first; ticked in bulk the same way (26 Sep 2026).
      $('[data-act=del]', tr).onclick = async e => {
        e.stopPropagation();
        if (await bulkDelete('steel_entry', [num(tr.dataset.id)])) reload();
      };
    });
    if (d.rows.length) bulkRows($('#st-list'), 'steel_entry', { reload });
    bindTabs();
  }

  async function load() {
    mem.save();
    // Type, Quality and "none left" are about the stock; the list of entries
    // is every entry, so they are put away while it is shown.
    const entries = $('#st-view').value === 'entries';
    ['#st-emptybox', '#st-type-box', '#st-q-box'].forEach(x => {
      $(x).style.display = entries ? 'none' : '';
    });
    $('#out').innerHTML = '<div class="card"><div class="empty">Reading the stock…</div></div>';
    try {
      if ($('#st-view').value === 'entries') {
        const d = await api.get('/api/steel/entries', { to: $('#f2').value });
        if (!here()) return;
        drawEntries(d);
      } else {
        const d = await api.get('/api/steel/stock', params());
        if (!here()) return;
        drawStock(d);
      }
    } catch (err) {
      if (!here()) return;
      bad(err.message);
      $('#out').innerHTML = '<div class="card"><div class="empty"><b>Could not read the stock</b>'
        + esc(err.message) + '</div></div>';
    }
  }
  bindFy(load, null, 'f2');
  ['#f2', '#st-type', '#st-q', '#st-view', '#st-all'].forEach(s => { $(s).onchange = load; });
  await load();
});

/* ============================================== bank & cash -------------
   Where the firm's own money sits, and what has moved through it. The list
   is every cash and bank account over the chosen dates; pick one and the
   same screen becomes that account's statement, worded the way a bank
   statement is -- a deposit is money in, a withdrawal money out -- with a
   running balance down the right.

   Nothing is posted twice for this: an entry made here is the ordinary
   receipt, payment or contra voucher, and these figures are read back off
   the same postings the ledger reports use.                                */

/* **The bank book and the cash book are two screens** (22 Sep 2026: "make
   bank and cash two different pages"). They are the same screen underneath --
   an account list, and a statement once one is picked -- but each answers for
   its own book: its own accounts, its own totals, its own wording (a bank
   takes deposits and withdrawals; cash is received and paid), and its own
   money moved between the firm's own accounts. Cash paid into the bank leaves
   the cash book and arrives in the bank book, so each page counts it.

   A link to an account of the other kind -- the dashboard's cash row, an old
   `#/bank?account=` -- is handed over to the other page rather than refused. */
route(/^\/(bank|cash)$/, async (view, m, q) => {
  const book = m[0];                        // 'bank' or 'cash'
  const isCash = book === 'cash';
  // What the two columns of the book are called, and what its accounts are.
  const WORD = isCash
    ? { in: 'Received', out: 'Paid', head: 'Cash', kind: 'cash account',
        kinds: 'cash accounts' }
    : { in: 'Deposits', out: 'Withdrawals', head: 'Bank', kind: 'bank account',
        kinds: 'bank accounts' };
  const here = onScreen();
  await loadCashBank(true);
  if (!here()) return;
  let account = num(q.account) || 0;
  const mine = S.cashbank.filter(b => isCashAccount(b) === isCash);
  // A firm that keeps one cash account *is* that account: there is no list
  // worth reading first, so the cash book opens on the book itself. Only
  // ever done once -- "All cash accounts" then shows the list and stays.
  let opened = false;

  /* Reworked on 21 Sep 2026 ("the UI isn't user friendly"). What it showed
     was right; how it was laid out was not: the accounts table ran off the
     right edge with its Statement button cut off, names broke over three
     lines beside a Group column that said "Bank Accounts" nineteen times, the
     statement's Balance column was cut off too, and the summary cards went on
     showing the whole firm while one account was being read. Now:
       - the list is split into Bank and Cash, one line an account, and a row
         opens its statement;
       - with an account open the cards are that account's own -- opening,
         deposits, withdrawals, closing -- and the list steps aside;
       - a statement fits the page, keeps its headings in view as it scrolls,
         can be searched, and switches Datewise / Monthly where it is read;
       - Deposit and Withdrawal fill in the account on screen. */

  // What a new entry made from here starts with: the account being read, and
  // how money moves through it.
  // With no account open, the one the latest entry went through.
  async function preset() {
    const how = isCash ? 'Cash' : 'Bank';
    const bank = account || await recentMoney(isCash);
    if (!bank) return { from_bank: true, mode: how };
    return { from_bank: true, bank_id: bank, mode: how };
  }

  // After an entry the statement is read again, at the month the entry went
  // into -- a deposit dated last month should not seem to have vanished.
  function entered(got) {
    if (!got) return;
    const d = got.voucher_date || '';
    if (st && d && viewOf() === 'month') month = d.slice(0, 7);
    load();
  }

  // One line under the title, as on the Bank page: the Cash page's longer one
  // ran onto a second line at 1366 and 1440 and made its bar the taller.
  setTitle(isCash ? 'Cash' : 'Bank',
    isCash ? 'Cash in hand and the cash book'
           : 'Bank accounts, entries and statements', [
    { label: '⤓ CSV', onClick: () => location.href = firmUrl('/api/export/bank?' +
        new URLSearchParams({ kind: book, from: $('#f1').value,
                              to: $('#f2').value, account: account || '' })) },
    { label: '🖨 Print', onClick: () => window.print() },
    { label: isCash ? '＋ Cash received' : '＋ Deposit', cls: 'primary',
      onClick: async () => entered(await editVoucher(await preset(), 'RECEIPT')) },
    { label: isCash ? '＋ Cash paid' : '＋ Withdrawal',
      onClick: async () => entered(await editVoucher(await preset(), 'PAYMENT')) },
    // Cash into the bank, cash out of it, one bank to another.
    { label: '⇄ Transfer',
      onClick: async () => entered(await editTransfer({ from: account || '' })) }
  ]);

  // One row of filters. "View" and "Dormant too" are asked where they act --
  // on the statement and on the account list -- and kept here out of sight
  // only so the address remembers them.
  view.innerHTML = `<div class="card bankbar noprint"><div class="bd"><div class="row">
      ${fySelect('fyq', { all: 'All dates' })}
      <div><label>From</label><input type="date" id="f1" value="${fyStart()}"></div>
      <div><label>To</label><input type="date" id="f2" value="${today()}"></div>
      <div class="grow"><label>Account</label>
        <select id="acc"><option value="">All ${esc(WORD.kinds)}</option></select></div>
      <select id="bankview" hidden>
        <option value="">Month-wise</option>
        <option value="months">Summary</option>
        <option value="entries">All entries</option></select>
      <input type="checkbox" id="dormant" hidden ${q.dormant ? 'checked' : ''}>
      <button id="go" class="primary">Apply</button>
    </div></div></div><div id="out" class="bankbook"></div>`;

  $('#acc').innerHTML = '<option value="">All ' + esc(WORD.kinds) + '</option>' +
    mine.map(b => `<option value="${b.id}"${b.id === account ? ' selected' : ''}>${esc(b.name)}</option>`).join('');
  let month = '';           // the month a statement is read at, 'YYYY-MM'
  const mem = screenState('/' + book, {
    account: { get: () => account || '', set: v => { account = num(v) || 0; } },
    month: { get: () => month, set: v => { month = /^\d{4}-\d{2}$/.test(v) ? v : ''; } },
    from: '#f1', to: '#f2', dormant: '#dormant', view: '#bankview',
    fy: fyField('f1', 'f2')
  }).restore(q);
  $('#acc').value = account || '';

  /* **A statement is read a month at a time** (21 Sep 2026: "there are a lot
     of entries if it is shown year-wise"). Kotak B.S. Traders runs to 637
     entries in half a year and the cash book to eighteen thousand over every
     year; a month of either reads at once. So an account opens on its latest
     month, with the months of the dates asked for along the top, each one's
     count beside it, and Prev / Next between them. Summary is the months as
     one table; All entries is the old datewise list, drawn a thousand lines
     at a time. It is the same entries every way; nothing is fetched again. */
  const CHUNK = 1000;       // datewise lines drawn at a time
  const FOUND = 500;        // search matches drawn at most
  let last = null;          // the answer on screen
  let st = null;            // its statement, when an account is picked
  let shown = 0;            // datewise lines drawn so far
  let find = '';            // what the statement is being searched for

  const count = n => fmt(n, 0);
  const plural = (n, one, many) => count(n) + ' ' + (n === 1 ? one : many);
  // A balance with its side -- except nothing, which has no side: an unused
  // account read "0.00 Dr" on both its opening and its closing.
  const bal = v => (Math.abs(num(v)) < 0.005 ? fmt(0) : drcr(v));

  /* **The keyboard keeps its place as the book is drawn again** (29 Sep
     2026, as on an account's month page the day before). Turning a month
     with ← →, switching Month-wise / Summary / All entries, opening an
     account from the list: each drew the statement afresh and took away the
     line or button the keyboard stood on. It found a line again only a
     moment later -- so a second → pressed quickly, or held down, landed on
     the Year box and walked off along the filters -- and after an account
     was opened it found none at all, so the first ← went to the Year box
     too. Now it is put back at once: from a line, onto the first line of
     what is drawn -- a month's latest entry, and a month with nothing in it
     still has a line to stand on -- and from a button drawn again, back
     onto that button. */
  function keyAt() {
    const out = $('#out'), ae = document.activeElement;
    if (!out) return null;
    if (ae && ae !== document.body && out.contains(ae)) {
      if (ae.closest('tbody tr')) return { row: true };
      if (ae.id) return { id: ae.id };
      if (ae.dataset.view) return { view: ae.dataset.view };
      return null;
    }
    // The line itself went with the drawing it was in; the lit one, put
    // back quietly a moment after, is where the keyboard still is.
    return (!ae || ae === document.body) && $('#out tr.kb') ? { row: true } : null;
  }
  function putBack(at, prefer) {
    if (!at) return;
    const out = $('#out');
    const b = at.id ? $('#' + at.id, out)
      : at.view ? $('.seg [data-view="' + at.view + '"]', out) : null;
    if (b && !b.disabled) { b.focus(); return; }
    const rs = KB.rows().filter(r => out.contains(r));
    const tr = (prefer && $(prefer, out)) || rs.find(r => r.onclick) || rs[0];
    if (tr) KB.setCursor(tr);
  }
  // Draws the book again, the keyboard kept where it was.
  function redraw() {
    const at = keyAt();
    const y = window.scrollY;
    paint();
    window.scrollTo(0, y);
    putBack(at);
  }

  /* **Esc closes a statement opened from the list, back onto that account's
     line** -- as Accounts does since 26 Sep 2026 -- and Esc again goes to
     the menu. It used to leave the page altogether. A statement reached any
     other way (the dashboard's cash row, the cash book opening on its only
     account, a link) has no list behind it, and Esc does what it always
     did. The way back rides in the history entry, so a voucher opened from
     the statement and closed again keeps it. */
  const LIST_AT = 'bankListAt';
  let listAt = (history.state && history.state[LIST_AT]) || null;
  const markList = at => { listAt = at; stamp({ [LIST_AT]: at }); };
  KB.setEscape(() => !!account && !!listAt, () => pick(0));

  function rangeText() {
    const f = $('#f1').value, t = $('#f2').value;
    return !f && !t ? 'all dates' : !f ? 'up to ' + dt(t)
      : !t ? 'from ' + dt(f) : dt(f) + ' to ' + dt(t);
  }

  function viewOf() { return $('#bankview').value || 'month'; }

  /* ------------------------------------------------------------ the cards --
     With no account picked they are the firm's: money in the banks, cash in
     hand, and what came in and went out. With one picked they are that
     account's own, which is what is being read. */
  function cards(d) {
    if (st) {
      // Read month by month, the cards are the month's; otherwise the whole
      // of the dates asked for.
      const cur = viewOf() === 'month' && st.entries.length ? currentMonth() : null;
      const rows = cur ? monthRows(cur.m) : st.entries;
      const from = cur ? monthStart(cur.m.ym) : $('#f1').value;
      const to = cur ? monthStop(cur.m.ym) : $('#f2').value;
      const opening = cur ? cur.m.open : st.opening;
      const closing = cur ? cur.m.balance : st.closing;
      const deposits = cur ? cur.m.deposit : st.deposits;
      const withdrawals = cur ? cur.m.withdrawal : st.withdrawals;
      const ins = rows.filter(e => e.deposit).length;
      const outs = rows.filter(e => e.withdrawal).length;
      const when = cur ? ' · ' + esc(monthLabel(cur.m.ym)) : '';
      return `<div class="kpis four">
        <div class="kpi"><div class="lb">Opening balance</div>
          <div class="vl">₹${bal(opening)}</div>
          <div class="ft">${from ? 'on ' + dt(from) : 'before the first entry'}</div></div>
        <div class="kpi good"><div class="lb">${WORD.in}</div>
          <div class="vl">${money(deposits)}</div>
          <div class="ft">${plural(ins, 'entry', 'entries')}${when}</div></div>
        <div class="kpi bad"><div class="lb">${WORD.out}</div>
          <div class="vl">${money(withdrawals)}</div>
          <div class="ft">${plural(outs, 'entry', 'entries')}${when}</div></div>
        <div class="kpi brand"><div class="lb">Closing balance</div>
          <div class="vl"${closing < 0 ? ' style="color:var(--bad)"' : ''}>₹${bal(closing)}</div>
          <div class="ft">${closing < 0 ? '<b style="color:var(--bad)">overdrawn</b> · '
            : ''}${to ? 'on ' + dt(to) : 'after the last entry'}</div></div>
      </div>`;
    }
    const n = d.rows.length;
    /* Money moved between two accounts *of this book* -- Kotak to AUSF on the
       bank page -- came in to one and went out of the other, and is left out
       of both. Cash paid into the bank is not such a move here: it leaves the
       cash book and arrives in the bank book, so each page counts it once.
       (On the one screen these two were together, ₹22.6 crore of the ₹38.7
       crore "received" this year was the firm's own money moving.) */
    const moved = num(d.transfers);
    const own = 'Money moved between two of your own ' + WORD.kinds
      + ' is left out: ' + money(moved) + ' in these dates. Each account below '
      + 'still shows it.';
    const ownNote = '<span class="own">' + money(moved) + ' between own '
      + esc(WORD.kinds) + ' left out</span>';
    return `<div class="kpis four">
      <div class="kpi brand"><div class="lb">${isCash ? 'Cash in hand' : 'In the bank'}</div>
        <div class="vl">${money(isCash ? d.cash_total : d.bank_held)}</div>
        <div class="ft">${plural(n, 'account', 'accounts')}${!isCash && d.bank_overdrawn
          ? ' · <b style="color:var(--bad)">' + fmt(d.bank_overdrawn)
            + ' overdrawn</b>' : ''}</div></div>
      <div class="kpi"><div class="lb">Opening</div>
        <div class="vl">₹${bal(d.opening_total)}</div>
        <div class="ft">${$('#f1').value ? 'on ' + dt($('#f1').value)
          : 'before the first entry'}</div></div>
      <div class="kpi good" title="${own}"><div class="lb">${WORD.in}</div>
        <div class="vl">${money(d.received - moved)}</div>
        <div class="ft">${esc(rangeText())}${moved ? '<br>' + ownNote : ''}</div></div>
      <div class="kpi bad" title="${own}"><div class="lb">${WORD.out}</div>
        <div class="vl">${money(d.paid - moved)}</div>
        <div class="ft">${esc(rangeText())}${moved ? '<br>' + ownNote : ''}</div></div>
    </div>`;
  }

  /* ----------------------------------------------------- the account list --
     Bank accounts, then cash, one line each, and the whole row opens the
     statement. The Group column is gone: the section says it. */
  // The widths are in style.css (`.bankbook`), and narrow with the window.
  const ACC_COLS = `<colgroup><col><col class="w-n"><col class="w-bal">
    <col class="w-amt"><col class="w-amt"><col class="w-bal"><col class="w-go"></colgroup>`;

  function accountRow(r, asleep) {
    return `<tr class="clickable${asleep ? ' asleep' : ''}" data-account="${r.id}"
        title="Open the statement of ${esc(r.name)}">
      <td class="acct"><b>${esc(r.name)}</b>${
        r.overdrawn ? ' <span class="tag bad">overdrawn</span>' : ''}${
        r.active ? '' : ' <span class="tag warn">inactive</span>'}${r.code
          ? ' <span class="code">' + esc(r.code) + '</span>' : ''}</td>
      <td class="c">${r.entries ? count(r.entries) : '—'}</td>
      <td class="r num nw">${bal(r.opening)}</td>
      <td class="r num nw"${r.received ? ' style="color:var(--ok)"' : ''}>
        ${r.received ? fmt(r.received) : '—'}</td>
      <td class="r num nw"${r.paid ? ' style="color:var(--bad)"' : ''}>
        ${r.paid ? fmt(r.paid) : '—'}</td>
      <td class="r num nw"${r.overdrawn ? ' style="color:var(--bad)"' : ''}>
        <b>${bal(r.closing)}</b></td>
      <td class="c go" aria-hidden="true">›</td>
    </tr>`;
  }

  function section(title, rows, asleep) {
    if (!rows.length) return '';
    const sum = k => rows.reduce((a, r) => a + num(r[k]), 0);
    return `<tr class="grp${asleep ? ' asleep' : ''}"><td colspan="5"><b>${title}</b>
        <span>${plural(rows.length, 'account', 'accounts')}${asleep
          ? ' · no balance and no entries in these dates' : ''}</span></td>
      <td class="r num nw"><b>${asleep ? '' : bal(sum('closing'))}</b></td><td></td></tr>`
      + rows.map(r => accountRow(r, asleep)).join('');
  }

  function accountsCard(d) {
    if (!d.rows.length) {
      return `<div class="card"><div class="empty"><b>No ${esc(WORD.kind)}</b>
        Add one under <a href="#/ledgers">Chart of Accounts</a> in the
        ${isCash ? 'Cash-in-hand' : 'Bank Accounts'} group.${isCash
          ? ' The banks are on the <a href="#/bank">Bank</a> page.'
          : ' Cash in hand is on the <a href="#/cash">Cash</a> page.'}</div></div>`;
    }
    /* The accounts worked the most come first (21 Sep 2026). Alphabetical,
       the firm's two Kotak accounts -- 1,300 entries between them this year
       -- sat in the middle of thirteen family accounts with a few dozen.
       The ones with nothing in them and nothing through them, shown only
       when asked for, stand apart at the foot rather than mixed in, so the
       tick visibly does something. */
    const busiest = (a, b) => (b.entries - a.entries) || a.name.localeCompare(b.name);
    const live = d.rows.filter(r => !r.dormant).sort(busiest);
    const asleep = d.rows.filter(r => r.dormant);
    return `<div class="card"><h2>${esc(WORD.head)} accounts
        <span class="rt"><span class="meta">${esc(rangeText())} · click an
          account for its statement</span>
          <label class="tick noprint" title="Accounts with no balance and no entry in these dates are left out. Most firms carry a few that were opened and never used.">
            <input type="checkbox" id="dormant-show"${$('#dormant').checked
              ? ' checked' : ''}> Dormant too</label></span></h2>
      <div class="bd flush"><table class="banktbl">${ACC_COLS}<thead><tr>
        <th>Account</th><th class="c">Entries</th><th class="r">Opening</th>
        <th class="r">${WORD.in}</th><th class="r">${WORD.out}</th>
        <th class="r">Closing</th><th></th></tr></thead><tbody>
        ${live.map(r => accountRow(r)).join('')}${section('Not in use', asleep, true)}</tbody>
        <tfoot><tr><td colspan="2">${plural(d.rows.length, 'account', 'accounts')}</td>
          <td class="r num nw">${bal(d.opening_total)}</td>
          <td class="r num nw">${fmt(d.received)}</td>
          <td class="r num nw">${fmt(d.paid)}</td>
          <td class="r num nw">${bal(d.closing_total)}</td><td></td></tr></tfoot>
      </table></div>${d.dormant_hidden ? `<div class="bd note">
        <b>${d.dormant_hidden}</b> account${d.dormant_hidden === 1 ? '' : 's'} not shown —
        nothing in ${d.dormant_hidden === 1 ? 'it' : 'them'} and nothing through
        ${d.dormant_hidden === 1 ? 'it' : 'them'}. Tick <i>Dormant too</i> to see
        ${d.dormant_hidden === 1 ? 'it' : 'them'}.</div>` : ''}</div>`;
  }

  /* The old software's **Bank Book**, column for column:

       Date | Particulars | City | Ch/DD No. | Withdraw | Deposit | Balance

     Particulars is the account on the other side of the entry -- who was
     paid, or who paid in -- with the voucher's own narration beneath it, and
     the balance runs down the page carrying Dr or Cr. A cash account reads
     the same way; SHREE calls the two columns Credit and Debit there, but
     money out and money in is the same thing said plainer. The widths are
     set, so the table fits the page and Particulars takes what is left --
     and they narrow with the window (style.css, `.bankbook`): at 1100 wide
     the set widths had left Particulars nothing at all. In a narrow window
     the City goes beside the party's name instead of a column of its own. */
  const BANK_COLS = `<colgroup><col class="w-date"><col>
    <col class="w-city"><col class="w-chq"><col class="w-amt">
    <col class="w-amt"><col class="w-bal"></colgroup>`;
  const OUT = isCash ? 'Paid' : 'Withdraw';
  const IN = isCash ? 'Received' : 'Deposit';
  const BANK_HEAD = `<tr><th>Date</th><th>Particulars</th><th class="city">City</th>
    <th title="What the money moved on — the voucher and its cheque, UPI, clearing or transfer reference, as the old bank book prints it">Ch/DD No.</th>
    <th class="r">${OUT}</th><th class="r">${IN}</th><th class="r">Balance</th></tr>`;

  // Two kinds of line have no account on the other side: the balance a year
  // that was never backed up was carried forward on, and a voucher whose
  // other account has since been deleted. Say which, rather than print a
  // bare "Payment".
  function particulars(e) {
    const note = e.note ? '<div class="note">' + esc(e.note) + '</div>' : '';
    if (e.particulars) {
      return '<b>' + goLink(accountLink(e.other_id), esc(e.particulars),
                            'Open this account') + '</b>'
        + (e.city ? '<span class="pcity">' + esc(e.city) + '</span>' : '') + note;
    }
    if (e.doc_kind === 'OPENING') {
      return '<i>Balance carried forward' + (e.no ? ' into ' + esc(e.no) : '')
        + '</i>';
    }
    return '<b>' + esc(e.kind) + '</b> <span class="tag" title="The account on '
      + 'the other side of this entry has been deleted">other account deleted'
      + '</span>' + note;
  }

  function bankRow(e, inside) {
    return `<tr class="${docHref(e.doc_kind, e.doc_id) ? 'clickable' : ''}"
      data-doc="${e.doc_id}" data-kind="${e.doc_kind}">
      <td class="nw"${inside ? ' style="padding-left:26px"' : ''}>${dt(e.date)}</td>
      <td class="pt">${particulars(e)}</td>
      <td class="city">${esc(e.city || '')}</td>
      <td class="chq">${esc(e.chq || e.no || '')}</td>
      <td class="r num nw"${e.withdrawal ? ' style="color:var(--bad)"' : ''}>
        ${e.withdrawal ? fmt(e.withdrawal) : ''}</td>
      <td class="r num nw"${e.deposit ? ' style="color:var(--ok)"' : ''}>
        ${e.deposit ? fmt(e.deposit) : ''}</td>
      <td class="r num nw">${bal(e.balance)}</td>
    </tr>`;
  }

  function datewiseTable() {
    // Read over all dates there is no From: the balance is brought forward
    // to the first entry, and dated with it rather than left blank.
    const bf = $('#f1').value || (st.entries[0] && st.entries[0].date) || '';
    return `<table class="banktbl">${BANK_COLS}<thead>${BANK_HEAD}</thead>
      <tbody id="st-rows"><tr class="bf"><td class="nw">${dt(bf)}</td>
        <td colspan="5"><i>B/F Balance</i></td>
        <td class="r num nw">${bal(st.opening)}</td></tr>
        ${st.entries.slice(0, shown).map(e => bankRow(e)).join('')}</tbody>
      <tfoot><tr><td colspan="4">Closing balance</td>
        <td class="r num nw">${fmt(st.withdrawals)}</td>
        <td class="r num nw">${fmt(st.deposits)}</td>
        <td class="r num nw"${st.closing < 0 ? ' style="color:var(--bad)"' : ''}>
          <b>${bal(st.closing)}</b></td>
      </tr></tfoot></table>`;
  }

  function moreBar() {
    if (!st || find || viewOf() !== 'entries' || shown >= st.entries.length) return '';
    const left = st.entries.length - shown;
    return `<div class="bd note bar" id="st-more">
      <span>Showing the first <b>${count(shown)}</b> of
        <b>${count(st.entries.length)}</b> lines.</span>
      <button id="st-next"${left > CHUNK ? '' : ' data-draw-rest'}>Next ${count(Math.min(CHUNK, left))}</button>
      ${left > CHUNK ? `<button id="st-rest" data-draw-rest>All ${count(left)} remaining</button>` : ''}
      <span>or pick <i>Monthly</i> above to go straight to a month.</span></div>`;
  }

  function monthLabel(ym) {
    const p = ym.split('-').map(Number);
    return new Date(p[0], p[1] - 1, 1)
      .toLocaleString('en-IN', { month: 'long', year: 'numeric' });
  }

  // The statement is in date order, so each month is one run of it.
  function monthsOf() {
    const out = [];
    let cur = null;
    st.entries.forEach((e, i) => {
      const ym = (e.date || '').slice(0, 7);
      if (!cur || cur.ym !== ym) {
        cur = { ym, first: i, count: 0, deposit: 0, withdrawal: 0 };
        out.push(cur);
      }
      cur.count++;
      cur.deposit += e.deposit;
      cur.withdrawal += e.withdrawal;
      cur.balance = e.balance;
    });
    return out;
  }

  /* ------------------------------------------------------ month by month --
     Every month of the dates asked for, with or without entries, each with
     the balance it opened and closed on. A range with no dates set runs from
     the first entry to the last. */
  function monthList() {
    const runs = monthsOf();
    const byYm = {};
    runs.forEach(m => { byYm[m.ym] = m; });
    const e = st.entries;
    const f = ($('#f1').value || (e[0] && e[0].date) || '').slice(0, 7);
    const t = ($('#f2').value || (e.length && e[e.length - 1].date) || '').slice(0, 7);
    if (!f || !t) return runs;
    const out = [];
    let y = +f.slice(0, 4), mo = +f.slice(5, 7), bal = st.opening;
    const ey = +t.slice(0, 4), em = +t.slice(5, 7);
    while ((y < ey || (y === ey && mo <= em)) && out.length < 1200) {
      const ym = y + '-' + String(mo).padStart(2, '0');
      const m = byYm[ym] || { ym, first: -1, count: 0, deposit: 0,
                              withdrawal: 0, balance: bal };
      m.open = bal;
      bal = m.balance;
      out.push(m);
      if (++mo > 12) { mo = 1; y++; }
    }
    return out;
  }

  // The month on screen: the one in the address, else the latest month that
  // has entries -- the current month, on an account in use.
  function currentMonth() {
    const ms = monthList();
    if (!ms.length) return null;
    let i = ms.findIndex(m => m.ym === month);
    if (i < 0) {
      i = ms.length - 1;
      while (i > 0 && !ms[i].count) i--;
    }
    return { ms, i, m: ms[i] };
  }

  const monthRows = m => (m.count ? st.entries.slice(m.first, m.first + m.count) : []);
  function monthStart(ym) {
    const f = $('#f1').value;
    return f && f > ym + '-01' ? f : ym + '-01';
  }
  function monthStop(ym) {
    const p = ym.split('-').map(Number);
    const end = ym + '-' + String(new Date(p[0], p[1], 0).getDate()).padStart(2, '0');
    const t = $('#f2').value;
    return t && t < end ? t : end;
  }
  function shortMonth(ym) {
    const p = ym.split('-').map(Number);
    return new Date(p[0], p[1] - 1, 1).toLocaleString('en-IN', { month: 'short' })
      + " \u2019" + String(p[0]).slice(2);
  }

  function monthView() {
    const cur = currentMonth();
    if (!cur) return datewiseTable();
    const { ms, i, m } = cur;
    const rows = monthRows(m);
    // Up to thirteen months stand along the top as buttons; a longer range
    // -- All dates on the cash book is a dozen years -- is a list to pick from.
    // At either end of the dates ‹ Prev or Next › is greyed but not switched
    // off: pressed -- or ← → on a line -- it simply does nothing. Switched
    // off, it threw the keyboard somewhere else: ← on April's lines went onto
    // a link, and Next › held down passed the keyboard to ‹ Prev, which
    // turned straight back.
    const end = ' class="end" aria-disabled="true"';
    const nav = `<div class="monthnav noprint">
      <button type="button" id="m-prev"${i ? '' : end}
        title="${i ? esc(monthLabel(ms[i - 1].ym)) : 'This is the first month'}">‹ Prev</button>
      ${ms.length <= 13 ? `<div class="mchips" role="tablist">${ms.map((x, j) => `
        <button type="button" tabindex="-1" role="tab" data-ym="${x.ym}"
          class="mchip${j === i ? ' on' : ''}${x.count ? '' : ' none'}"
          aria-selected="${j === i}"
          title="${esc(monthLabel(x.ym))} — ${plural(x.count, 'entry', 'entries')}">
          <b>${esc(shortMonth(x.ym))}</b><small>${x.count ? count(x.count) : '—'}</small></button>`).join('')}</div>`
        : `<select id="m-pick" class="mpick">${ms.map(x => `<option value="${x.ym}"${
            x.ym === m.ym ? ' selected' : ''}>${esc(monthLabel(x.ym))} — ${
            plural(x.count, 'entry', 'entries')}</option>`).join('')}</select>`}
      <button type="button" id="m-next"${i < ms.length - 1 ? '' : end}
        title="${i < ms.length - 1 ? esc(monthLabel(ms[i + 1].ym)) : 'This is the last month'}">Next ›</button>
    </div>`;
    const outs = rows.reduce((a, e) => a + num(e.withdrawal), 0);
    const ins = rows.reduce((a, e) => a + num(e.deposit), 0);
    // The latest entry on top (23 Sep 2026), the balance beside each the one
    // it left, and the balance the month opened on at the foot -- the book
    // read from today backwards.
    return nav + `<div class="bd flush"><table class="banktbl">${BANK_COLS}
      <thead>${BANK_HEAD}</thead><tbody>
      ${rows.length ? rows.slice().reverse().map(e => bankRow(e)).join('')
        : `<tr><td></td><td colspan="6" class="muted-cell">Nothing moved through
            this account in ${esc(monthLabel(m.ym))}.</td></tr>`}
      <tr class="bf"><td class="nw">${dt(monthStart(m.ym))}</td>
        <td colspan="5"><i>B/F Balance</i></td>
        <td class="r num nw">${bal(m.open)}</td></tr>
      </tbody><tfoot><tr><td colspan="4">Closing balance · ${esc(monthLabel(m.ym))}
        <span class="note-inline">${plural(rows.length, 'entry', 'entries')}</span></td>
        <td class="r num nw">${fmt(outs)}</td>
        <td class="r num nw">${fmt(ins)}</td>
        <td class="r num nw"${m.balance < 0 ? ' style="color:var(--bad)"' : ''}>
          <b>${bal(m.balance)}</b></td></tr></tfoot></table></div>`;
  }

  // Go to a month, read month by month.
  function showMonth(ym) {
    month = ym;
    $('#bankview').value = '';
    mem.save();
    redraw();
  }

  const MONTH_COLS = `<colgroup><col><col class="w-mn"><col class="w-amt">
    <col class="w-amt"><col class="w-bal"><col class="w-go"></colgroup>`;

  function monthsTable() {
    const ms = monthsOf();
    return `<table class="banktbl">${MONTH_COLS}<thead><tr><th>Month</th>
        <th class="c">Entries</th><th class="r">${OUT}</th>
        <th class="r">${IN}</th><th class="r">Balance</th><th></th></tr></thead>
      <tbody>
      ${ms.slice().reverse().map(m => `<tr class="clickable" data-ym="${m.ym}"
          title="Read ${esc(monthLabel(m.ym))} entry by entry">
          <td class="nw"><b>${esc(monthLabel(m.ym))}</b></td>
          <td class="c">${count(m.count)}</td>
          <td class="r num nw"${m.withdrawal ? ' style="color:var(--bad)"' : ''}>
            ${m.withdrawal ? fmt(m.withdrawal) : ''}</td>
          <td class="r num nw"${m.deposit ? ' style="color:var(--ok)"' : ''}>
            ${m.deposit ? fmt(m.deposit) : ''}</td>
          <td class="r num nw"><b>${bal(m.balance)}</b></td>
          <td class="c go" aria-hidden="true">›</td></tr>`).join('')}
      <tr class="bf"><td colspan="4"><i>B/F Balance</i>${$('#f1').value
          ? ' <span class="note-inline">on ' + dt($('#f1').value) + '</span>' : ''}</td>
        <td class="r num nw">${bal(st.opening)}</td><td></td></tr>
      </tbody><tfoot><tr>
        <td>${plural(ms.length, 'month', 'months')}</td>
        <td class="c">${count(st.entries.length)}</td>
        <td class="r num nw">${fmt(st.withdrawals)}</td>
        <td class="r num nw">${fmt(st.deposits)}</td>
        <td class="r num nw"${st.closing < 0 ? ' style="color:var(--bad)"' : ''}>
          <b>${bal(st.closing)}</b></td><td></td></tr></tfoot></table>`;
  }

  /* ------------------------------------------------ searching a statement --
     A party, a cheque or UPI reference, a city, a narration or an amount --
     "57900" finds 57,900.00. The matching lines are listed by date with the
     balance each one left, and what they add up to. */
  function matches() {
    const t = find.toLowerCase().trim();
    const bare = t.replace(/[,\s₹]/g, '');
    return st.entries.filter(e => {
      const hay = [e.particulars, e.note, e.city, e.chq, e.no, e.kind,
                   dt(e.date)].join(' ').toLowerCase();
      if (hay.includes(t)) return true;
      if (!bare || !/^[\d.]+$/.test(bare)) return false;
      return [e.deposit, e.withdrawal].some(v => num(v) &&
        (String(num(v)).includes(bare) || num(v).toFixed(2).includes(bare)));
    });
  }

  function foundTable() {
    const hits = matches();
    const ins = hits.reduce((a, e) => a + num(e.deposit), 0);
    const outs = hits.reduce((a, e) => a + num(e.withdrawal), 0);
    if (!hits.length) {
      return `<div class="bd"><div class="empty"><b>Nothing matches
        “${esc(find)}”</b>No entry of this account ${esc(rangeText())} has
        it in its party, reference, city, narration or amount.</div></div>`;
    }
    return `<div class="bd flush"><table class="banktbl">${BANK_COLS}
      <thead>${BANK_HEAD}</thead><tbody>
      ${hits.slice(0, FOUND).map(e => bankRow(e)).join('')}</tbody>
      <tfoot><tr><td colspan="4">${plural(hits.length, 'entry matches', 'entries match')}${
        hits.length > FOUND ? ' · the first ' + count(FOUND) + ' shown' : ''}</td>
        <td class="r num nw">${fmt(outs)}</td><td class="r num nw">${fmt(ins)}</td>
        <td></td></tr></tfoot></table></div>`;
  }

  function statementBody() {
    const n = st.entries.length;
    if (!n) {
      const narrowed = !!($('#f1').value || $('#f2').value);
      return `<div class="bd"><div class="empty"><b>Nothing moved through this account
          ${esc(rangeText())}</b>Balance ${bal(st.closing)}.${narrowed
          ? ` Its entries may lie outside these dates.
            <div style="margin-top:12px"><button id="st-all">Show all dates</button></div>`
          : ' It has no entries at all.'}</div></div>`;
    }
    if (find.trim()) return foundTable();
    const v = viewOf();
    if (v === 'month') return monthView();
    shown = v === 'months' ? 0 : Math.min(CHUNK, n);
    return `<div class="bd flush">${v === 'months' ? monthsTable() : datewiseTable()}</div>${moreBar()}`;
  }

  function statementCard() {
    const a = st.account, n = st.entries.length;
    const v = viewOf();
    return `<div class="card stmt"><h2><span>${esc(a.name)}
        <span class="tag">${esc(a.group_name)}</span></span>
        <span class="rt"><span class="meta">${esc(rangeText())} ·
          ${plural(n, 'entry', 'entries')}</span>
          ${mine.length > 1 ? '<button class="sm" id="all-acc">← All '
            + esc(WORD.kinds) + '</button>' : ''}</span></h2>
      ${n ? `<div class="stmt-tools noprint">
        <input type="search" id="st-find" value="${esc(find)}"
          placeholder="Find party, cheque / UPI ref, amount…" title="Search this account's entries over all the dates asked for: party, cheque or UPI reference, city, narration or amount">
        <div class="spacer"></div>
        <div class="seg" role="group" aria-label="How the statement is read">
          <button type="button" data-view="month"${v === 'month' ? ' class="on"' : ''}
            title="One month at a time, with its opening and closing balance">Month-wise</button>
          <button type="button" data-view="months"${v === 'months' ? ' class="on"' : ''}
            title="One line a month; click a month to read it">Summary</button>
          <button type="button" data-view="entries"${v === 'entries' ? ' class="on"' : ''}
            title="Every entry of these dates, by date, with the running balance">All entries</button>
        </div></div>` : ''}
      <div id="st-body">${statementBody()}</div>
      ${st.closing < 0 ? `<div class="bd note" style="color:var(--bad)">
        This account is <b>overdrawn</b> by ${fmt(-st.closing)} — a credit
        balance is money owed to the bank, not money held.</div>` : ''}</div>`;
  }

  // Enter on a row runs that row's own onclick (the keyboard module looks
  // for it), so every row that opens something carries one -- including the
  // rows drawn later, when a month is opened or more lines are shown.
  function wire(root) {
    $$('tr[data-doc]', root).forEach(tr => {
      const href = docHref(tr.dataset.kind, num(tr.dataset.doc));
      if (href && !tr.onclick) {
        tr.onclick = e => { e.stopPropagation(); location.hash = href; };
      }
    });
    $$('tr[data-ym]', root).forEach(tr => {
      if (!tr.onclick) tr.onclick = () => showMonth(tr.dataset.ym);
    });
    // The months along the top of a statement, and the way either side.
    // Clicked, a month does not take the keyboard off the line it is on.
    $$('.mchip[data-ym]', root).forEach(b => {
      b.onmousedown = e => e.preventDefault();
      b.onclick = () => showMonth(b.dataset.ym);
    });
    const cur = st && viewOf() === 'month' ? currentMonth() : null;
    const prev = $('#m-prev', root), next = $('#m-next', root), pickM = $('#m-pick', root);
    if (cur && prev) prev.onclick = () => cur.i > 0 && showMonth(cur.ms[cur.i - 1].ym);
    if (cur && next) next.onclick = () => cur.i < cur.ms.length - 1 && showMonth(cur.ms[cur.i + 1].ym);
    if (pickM) pickM.onchange = () => showMonth(pickM.value);
  }

  function more(step) {
    const body = $('#st-rows');
    if (!body || !st) return;
    const upto = Math.min(st.entries.length, shown + step);
    const tmp = document.createElement('tbody');
    tmp.innerHTML = st.entries.slice(shown, upto).map(e => bankRow(e)).join('');
    wire(tmp);
    const frag = document.createDocumentFragment();
    Array.from(tmp.children).forEach(tr => frag.appendChild(tr));
    body.appendChild(frag);
    shown = upto;
    const bar = $('#st-more');
    if (bar) bar.outerHTML = moreBar();
    wireMore();
  }

  function wireMore() {
    const next = $('#st-next'), rest = $('#st-rest');
    if (next) next.onclick = () => more(CHUNK);
    if (rest) rest.onclick = () => more(Infinity);
  }

  // Only the statement's body is redrawn as it is searched or switched, so
  // the search box keeps the cursor.
  function repaintBody() {
    const host = $('#st-body');
    if (!host || !st) return;
    host.innerHTML = statementBody();
    const everything = $('#st-all');
    if (everything) everything.onclick = allDates;
    wireMore();
    wire(host);
  }

  function paint() {
    const d = last;
    st = d.statement;
    // The column headings stay in view under the bar at the top of the page.
    const bar = $('.topbar');
    document.documentElement.style.setProperty('--topbar-h',
      (bar ? bar.offsetHeight : 0) + 'px');
    $('#out').innerHTML = cards(d) + (st ? statementCard() : accountsCard(d));
    fitFigures();
    const kpis = $('#out .kpis');
    if (kpis && window.ResizeObserver) new ResizeObserver(fitFigures).observe(kpis);

    const back = $('#all-acc');
    if (back) back.onclick = () => pick(0);
    const everything = $('#st-all');
    if (everything) everything.onclick = allDates;
    wireMore();
    wire($('#out'));
    // Not data-acc: the keyboard module owns that name for its Alt-letter
    // badges and strips it from anything it did not put there, which left
    // every row opening account 0 -- no statement at all.
    $$('#out tr[data-account]').forEach(tr => {
      tr.onclick = () => pick(num(tr.dataset.account));
    });
    const dorm = $('#dormant-show');
    if (dorm) dorm.onchange = () => { $('#dormant').checked = dorm.checked; load(); };
    const box = $('#st-find');
    if (box) {
      let timer = null;
      box.oninput = () => {
        clearTimeout(timer);
        timer = setTimeout(() => { find = box.value; repaintBody(); }, 160);
      };
      // Escape clears the search before it does anything else.
      box.onkeydown = e => {
        if (e.key === 'Escape' && box.value) {
          e.preventDefault(); e.stopPropagation();
          box.value = ''; find = ''; repaintBody();
        }
      };
    }
    // Switching how the statement is read redraws the cards too: month by
    // month they are the month's.
    $$('#out .seg [data-view]').forEach(b => {
      b.onclick = () => {
        $('#bankview').value = b.dataset.view === 'month' ? '' : b.dataset.view;
        mem.save();
        redraw();
      };
    });
  }

  /* **A figure too long for its card is drawn smaller, not over the next
     card** (29 Sep 2026). Crores read over every year -- ₹1,74,79,61,146.00
     received into cash -- ran out of the card at 1280 wide, and at 1366
     once Windows' scroll bar takes its share; a closing balance with its Dr
     broke onto a second line. Each figure keeps the full size while it
     fits, and is fitted again whenever the cards change width. */
  function fitFigures() {
    $$('#out .kpi .vl').forEach(v => {
      v.style.fontSize = '';
      const room = v.clientWidth, need = v.scrollWidth;
      if (!room || need <= room + 1) return;
      const px = parseFloat(getComputedStyle(v).fontSize);
      v.style.fontSize = Math.max(15, Math.floor(px * room / need * 10) / 10) + 'px';
    });
  }

  // Where pick() wants the page and the keyboard once the answer is drawn.
  let landing = null;

  async function load() {
    const at = keyAt();
    const land = landing;
    landing = null;
    mem.save();
    $('#out').innerHTML = '<div class="empty">Loading…</div>';
    const here = onScreen();
    let got;
    try {
      got = await api.get('/api/reports/bank', {
        kind: book, from: $('#f1').value, to: $('#f2').value,
        account: account || '', dormant: $('#dormant').checked ? '1' : ''
      });
    } catch (e) {
      if (!here()) return;
      // A statement asked for in the wrong firm -- the address kept from
      // before the firm was switched at the top left, or a link from the
      // other books. It stopped at "Loading…" for good; now the firm's own
      // accounts are shown instead, and why.
      if (account && e.status === 404) {
        bad(e.message + ' Showing this firm’s accounts.');
        account = 0;
        month = '';
        $('#acc').value = '';
        return load();
      }
      $('#out').innerHTML = `<div class="card"><div class="empty"><b>Could not
        read the bank book</b>${esc(e.message)}</div></div>`;
      return;
    }
    if (!here()) return;
    // An account of the other book -- a cash account reached from the
    // dashboard, an old #/bank?account= link: the other page answers for it.
    const asked = got.statement && got.statement.account;
    if (asked && (asked.kind === 'cash') !== isCash) {
      location.replace('#/' + (asked.kind === 'cash' ? 'cash' : 'bank')
        + '?account=' + asked.id);
      return;
    }
    if (isCash && !account && !opened && got.rows.length === 1) {
      opened = true;
      return pick(got.rows[0].id);
    }
    last = got;
    paint();
    if (land) window.scrollTo(0, land.y);
    putBack(at, land && land.prefer);
  }

  function allDates() {
    $('#f1').value = '';
    $('#f2').value = '';
    $('#fyq').value = 'all';
    load();
  }

  // Choosing an account only changes what this screen shows, so the address
  // bar is kept in step without going back through the router and redrawing
  // the filters underneath. Opening one from far down the list lands at the
  // top of its statement, not halfway down the page; going back to the list
  // lands where the list was left, on the account that was open.
  async function pick(id) {
    const from = account;
    if (id && !from) {
      // Opened from the list -- the way back to it (see Esc, above).
      markList(last && !last.statement ? { id, y: Math.round(window.scrollY) } : null);
    }
    const back = !id && from ? listAt || { y: 0 } : null;
    if (!id) markList(null);
    account = id;
    find = '';
    month = '';
    $('#acc').value = account || '';
    landing = back ? { y: back.y, prefer: 'tr[data-account="' + from + '"]' } : { y: 0 };
    await load();
  }

  $('#acc').onchange = () => pick(num($('#acc').value) || 0);
  $('#go').onclick = load;
  bindFy(load, 'f1', 'f2');
  load();
});

/* ====================================================== GSTR-1 Excel ---
   Asked for on 19 Sep 2026, with the workbook the return is filed from: the
   GST portal's GSTR-1 template, which its offline tool imports. One GST firm
   and one month at a time -- the screen shows what goes where and anything
   the portal would turn back, and Download writes the workbook (app/gstr1.py
   says what goes in each sheet, and why).                                 */
route(/^\/gstr1$/, async (view, _m, q) => {
  const firms = S.meta.billing_firms || [];
  if (!firms.length) {
    setTitle('GST');
    view.innerHTML = '<div class="card"><div class="empty"><b>No GST firm</b>'
      + 'GSTR-1 is filed by the firms that raise GST bills.</div></div>';
    return;
  }
  let firm = firms.some(f => f.id === num(q.firm)) ? num(q.firm)
    : firms.some(f => f.id === S.meta.company_id) ? S.meta.company_id : firms[0].id;
  // Last month: the one being filed.
  const prev = new Date();
  prev.setDate(1);
  prev.setMonth(prev.getMonth() - 1);
  let month = /^\d{4}-\d{2}$/.test(q.month || '') ? q.month
    : prev.getFullYear() + '-' + String(prev.getMonth() + 1).padStart(2, '0');
  let d = null;

  const download = () => {
    location.href = firmUrl('/api/gstr1/xlsx?' + new URLSearchParams({ company: firm, month }));
  };
  setTitle('GST', 'The month’s return, the 3B figures and the credit on '
    + 'purchases — and the year, month by month', [
    { label: '⤓ GSTR-1 Excel', cls: 'primary', onClick: download }
  ]);

  view.innerHTML = `<div class="card"><div class="bd"><div class="row">
      <div><label>Firm</label><select id="gfirm" style="width:calc(320px * var(--typescale))">${firms.map(f =>
        `<option value="${f.id}">${esc(firmLabel(f))}</option>`).join('')}</select></div>
      <div><label>Year</label><select id="gfy" style="width:calc(150px * var(--typescale))">${fyChoices().map(f =>
        `<option value="${f}">${f}</option>`).join('')}</select></div>
      <div><label>Month</label><select id="gmonth" style="width:calc(190px * var(--typescale))"></select></div>
      <div class="spacer"></div>
    </div><div class="hint" style="margin-top:10px">In the GST offline tool:
      <i>GSTR-1 → Import Files → Import Excel</i>, pick the file, then
      generate the JSON and upload it on the portal. Every sheet the portal
      asks for is in the workbook; the ones these books have nothing for are
      left empty.</div></div></div><div id="out"></div>`;

  function months() {
    $('#gmonth').innerHTML = monthsOfFy($('#gfy').value).map(m =>
      `<option value="${m}">${esc(monthWord(m))}</option>`).join('');
  }
  const mem = screenState('/gstr1', {
    firm: { get: () => firm, set: v => { if (firms.some(f => f.id === num(v))) firm = num(v); } },
    month: { get: () => month, set: v => { if (/^\d{4}-\d{2}$/.test(v)) month = v; } }
  }).restore(q);
  $('#gfirm').value = firm;
  const fy = fyLabel(month + '-01');
  if (![...$('#gfy').options].some(o => o.value === fy)) {
    $('#gfy').insertAdjacentHTML('afterbegin', `<option value="${fy}">${fy}</option>`);
  }
  $('#gfy').value = fy;
  months();
  $('#gmonth').value = month;

  const money0 = v => '₹' + fmt(v);
  function docCell(r) {
    return goLink(docLink('SALE', r.id, firm), '<b>' + esc(r.no) + '</b>', 'Open the bill');
  }
  /* Rate by rate, and who it went to -- SHREE's "GST% Wise" and "Party Type
     + Gst %" in one table. The figures are the return's own. */
  function rateTable(rates) {
    if (!rates.length) return '';
    const sum = k => rates.reduce((a, r) => a + r[k], 0);
    return `<div class="card"><h2>Rate by rate <span class="rt"
        style="font-weight:400;font-size:12px;color:var(--muted)">taxable value net of credit notes</span></h2>
      <div class="bd flush tbl-scroll"><table><thead><tr><th class="c">Rate</th>
        <th class="r">B2B</th><th class="r">B2C</th><th class="r">Credit notes</th>
        <th class="r">Taxable</th><th class="r">IGST</th><th class="r">CGST</th>
        <th class="r">SGST</th>${sum('cess') ? '<th class="r">Cess</th>' : ''}</tr></thead>
      <tbody>${rates.map(r => `<tr>
        <td class="c"><b>${fmt(r.rate, 2).replace(/\.00$/, '')}%</b></td>
        <td class="r num">${r.b2b ? fmt(r.b2b) : '—'}</td>
        <td class="r num">${r.b2c ? fmt(r.b2c) : '—'}</td>
        <td class="r num">${r.note ? fmt(-r.note) : '—'}</td>
        <td class="r num"><b>${fmt(r.taxable)}</b></td>
        <td class="r num">${r.igst ? fmt(r.igst) : '—'}</td>
        <td class="r num">${r.cgst ? fmt(r.cgst) : '—'}</td>
        <td class="r num">${r.sgst ? fmt(r.sgst) : '—'}</td>
        ${sum('cess') ? '<td class="r num">' + (r.cess ? fmt(r.cess) : '—') + '</td>' : ''}</tr>`).join('')}
      </tbody><tfoot><tr><td>Total</td>
        <td class="r num">${fmt(sum('b2b'))}</td><td class="r num">${fmt(sum('b2c'))}</td>
        <td class="r num">${fmt(-sum('note'))}</td><td class="r num">${fmt(sum('taxable'))}</td>
        <td class="r num">${fmt(sum('igst'))}</td><td class="r num">${fmt(sum('cgst'))}</td>
        <td class="r num">${fmt(sum('sgst'))}</td>
        ${sum('cess') ? '<td class="r num">' + fmt(sum('cess')) + '</td>' : ''}</tr></tfoot>
      </table></div></div>`;
  }

  /* The 3B, under the portal's own table numbers -- SHREE keeps a
     form3bdetail view beside its GSTR-1 one. */
  function b3Table(b) {
    const line = (no, what, cells, strong) => `<tr${strong ? ' style="font-weight:600"' : ''}>
      <td class="nw mono" style="font-size:12.5px">${no}</td><td>${what}</td>
      ${cells.map(v => `<td class="r num">${v === null ? '' : (v ? fmt(v) : '—')}</td>`).join('')}</tr>`;
    const o = b.outward, r = b.rcm, u = b.unregistered_inter;
    return `<div class="card"><h2>GSTR-3B <span class="rt"
        style="font-weight:400;font-size:12px;color:var(--muted)">from these books, for the month</span></h2>
      <div class="bd flush tbl-scroll"><table><thead><tr><th>Table</th><th style="min-width:280px">What</th>
        <th class="r">Taxable</th><th class="r">IGST</th><th class="r">CGST</th>
        <th class="r">SGST</th><th class="r">Cess</th></tr></thead><tbody>
        ${line('3.1(a)', 'Outward taxable supplies', [o.taxable, o.igst, o.cgst, o.sgst, o.cess])}
        ${r.taxable || r.igst || r.cgst || r.sgst
          ? line('3.1(d)', 'Inward supplies liable to reverse charge', [r.taxable, r.igst, r.cgst, r.sgst, r.cess]) : ''}
        ${u.count ? line('3.2', 'Of 3.1(a): inter-State to unregistered buyers ('
          + u.count + ')', [u.taxable, u.igst, null, null, null]) : ''}
        ${line('4(A)(5)', 'ITC — all other ITC (purchases)', [null, b.itc_other.igst, b.itc_other.cgst, b.itc_other.sgst, b.itc_other.cess])}
        ${r.count ? line('4(A)(3)', 'ITC — inward supplies under reverse charge',
          [null, b.itc_rcm.igst, b.itc_rcm.cgst, b.itc_rcm.sgst, b.itc_rcm.cess]) : ''}
        ${line('4(C)', 'Net ITC available', [null, b.itc_total.igst, b.itc_total.cgst, b.itc_total.sgst, b.itc_total.cess], true)}
        ${line('', 'Tax on this month’s figures, head by head', [null, b.payable.igst, b.payable.cgst, b.payable.sgst, b.payable.cess], true)}
      </tbody></table></div>
      <div class="hint" style="margin:10px 16px 14px">A figure below nought is
        credit left over on that head. Nothing is set off across heads and no
        earlier balance is counted — the portal does that in 6.1. Nil-rated,
        exempt and non-GST supplies are not separated out here.</div></div>`;
  }

  /* What was bought and the credit on it -- SHREE's GSTR-2 views. */
  function buysTable(p) {
    const t = p.totals;
    if (!t.count) return '';
    const rows = p.rows.slice(0, 500);
    return `<div class="card"><h2>Purchases and the credit on them <span class="rt"
        style="font-weight:400;font-size:12px;color:var(--muted)">${t.count} bill${t.count === 1 ? '' : 's'} · taxable ${fmt(t.taxable)}</span></h2>
      ${t.no_gstin ? `<div class="sync-warn"><span class="tag warn">!</span><div>
        ${t.no_gstin} purchase bill${t.no_gstin === 1 ? ' has' : 's have'} no GSTIN on the
        supplier’s account. The credit is claimed against the supplier’s GSTIN, and
        2A/2B cannot be matched without it — add it under Parties.</div></div>` : ''}
      <div class="bd flush tbl-scroll"><table><thead><tr><th>Bill</th>
        <th style="min-width:220px">Supplier</th><th>Their bill no.</th>
        <th class="r">Value</th><th class="r">Taxable</th><th class="r">IGST</th>
        <th class="r">CGST</th><th class="r">SGST</th></tr></thead>
      <tbody>${rows.map(r => `<tr>
        <td class="nw">${goLink(docLink(r.kind, r.id, firm), '<b>' + esc(r.no) + '</b>', 'Open the bill')}
          ${r.rcm ? '<span class="tag warn">RCM</span>' : ''}
          ${r.kind === 'DEBIT' ? '<span class="tag">return</span>' : ''}
          <div style="font-size:12px;color:var(--muted)">${dt(r.date)}</div></td>
        <td>${esc(r.party)}${r.gstin ? '<div class="mono" style="font-size:12px;color:var(--muted)">'
          + esc(r.gstin) + '</div>' : ' <span class="tag warn">no GSTIN</span>'}</td>
        <td class="nw">${esc(r.ref || '—')}</td>
        <td class="r num">${fmt(r.value)}</td><td class="r num">${fmt(r.taxable)}</td>
        <td class="r num">${r.igst ? fmt(r.igst) : '—'}</td>
        <td class="r num">${r.cgst ? fmt(r.cgst) : '—'}</td>
        <td class="r num">${r.sgst ? fmt(r.sgst) : '—'}</td></tr>`).join('')}</tbody>
      <tfoot><tr><td colspan="3">${t.count} bill${t.count === 1 ? '' : 's'}${
        p.rows.length > rows.length ? ' (first ' + rows.length + ' shown)' : ''}${
        p.rcm.count ? ' · ' + p.rcm.count + ' under reverse charge' : ''}</td>
        <td class="r num">${fmt(t.value)}</td><td class="r num">${fmt(t.taxable)}</td>
        <td class="r num">${fmt(t.igst)}</td><td class="r num">${fmt(t.cgst)}</td>
        <td class="r num">${fmt(t.sgst)}</td></tr></tfoot></table></div></div>`;
  }

  /* The year, month by month -- SHREE's "Monthly Summary", both sides. */
  function monthTable(list) {
    if (!list.length) return '';
    const sum = k => list.reduce((a, r) => a + r[k], 0);
    return `<div class="card"><h2>${esc(d.fy)}, month by month <span class="rt"
        style="font-weight:400;font-size:12px;color:var(--muted)">click a month to open it</span></h2>
      <div class="bd flush tbl-scroll"><table><thead><tr><th>Month</th>
        <th class="r">Invoices</th><th class="r">Notes</th><th class="r">Taxable</th>
        <th class="r">IGST</th><th class="r">CGST</th><th class="r">SGST</th>
        <th class="r">Output tax</th><th class="r">Purchases</th>
        <th class="r">ITC</th><th class="r">Net</th></tr></thead>
      <tbody>${list.slice().reverse().map(m => `<tr class="clickable${m.month === month ? ' on' : ''}" data-month="${m.month}">
        <td class="nw"><b>${esc(monthWord(m.month))}</b></td>
        <td class="r">${m.invoices || '—'}</td><td class="r">${m.notes || '—'}</td>
        <td class="r num">${fmt(m.taxable)}</td>
        <td class="r num">${m.igst ? fmt(m.igst) : '—'}</td>
        <td class="r num">${m.cgst ? fmt(m.cgst) : '—'}</td>
        <td class="r num">${m.sgst ? fmt(m.sgst) : '—'}</td>
        <td class="r num"><b>${fmt(m.tax)}</b></td>
        <td class="r num">${m.in_taxable ? fmt(m.in_taxable) : '—'}</td>
        <td class="r num">${m.itc ? fmt(m.itc) : '—'}</td>
        <td class="r num"${m.net < 0 ? ' style="color:var(--good)"' : ''}>${fmt(m.net)}</td></tr>`).join('')}
      </tbody><tfoot><tr><td>Year</td><td class="r">${sum('invoices')}</td>
        <td class="r">${sum('notes')}</td><td class="r num">${fmt(sum('taxable'))}</td>
        <td class="r num">${fmt(sum('igst'))}</td><td class="r num">${fmt(sum('cgst'))}</td>
        <td class="r num">${fmt(sum('sgst'))}</td><td class="r num">${fmt(sum('tax'))}</td>
        <td class="r num">${fmt(sum('in_taxable'))}</td><td class="r num">${fmt(sum('itc'))}</td>
        <td class="r num">${fmt(sum('net'))}</td></tr></tfoot></table></div></div>`;
  }

  function billTable(list, note) {
    if (!list.length) return '';
    const shown = list.slice(0, 1000);
    const small = t => '<div style="font-size:12px;color:var(--muted)">' + t + '</div>';
    return `<div class="bd flush tbl-scroll"><table><thead><tr>
        <th>${note ? 'Note' : 'Invoice'}</th><th style="min-width:220px">Buyer</th><th>Place of supply</th>
        <th class="r">${note ? 'Note value' : 'Invoice value'}</th><th class="r">Taxable</th>
        <th class="r">IGST</th><th class="r">CGST</th><th class="r">SGST</th>
      </tr></thead><tbody>${shown.map(r => `<tr>
        <td class="nw">${docCell(r)}${r.source === 'SHREE' ? '' : ' <span class="tag">here</span>'}${small(dt(r.date))}</td>
        <td>${esc(r.party)}${r.gstin ? '<div class="mono" style="font-size:12px;color:var(--muted)">'
          + esc(r.gstin) + '</div>' : ' <span class="tag warn">no GSTIN</span>'}</td>
        <td class="nw">${esc(r.pos)}</td>
        <td class="r num">${fmt(r.value)}</td>
        <td class="r num">${fmt(r.taxable)}${small(r.rates.map(x => '@ ' + x + '%').join(', '))}</td>
        <td class="r num">${r.igst ? fmt(r.igst) : '—'}</td>
        <td class="r num">${r.cgst ? fmt(r.cgst) : '—'}</td>
        <td class="r num">${r.sgst ? fmt(r.sgst) : '—'}</td></tr>`).join('')}</tbody>
      <tfoot><tr><td colspan="3">${list.length} ${note ? 'note' : 'invoice'}${list.length === 1 ? '' : 's'}${
        list.length > shown.length ? ' (first ' + shown.length + ' shown)' : ''}</td>
        <td class="r num">${fmt(list.reduce((a, r) => a + r.value, 0))}</td>
        <td class="r num">${fmt(list.reduce((a, r) => a + r.taxable, 0))}</td>
        <td class="r num">${fmt(list.reduce((a, r) => a + r.igst, 0))}</td>
        <td class="r num">${fmt(list.reduce((a, r) => a + r.cgst, 0))}</td>
        <td class="r num">${fmt(list.reduce((a, r) => a + r.sgst, 0))}</td></tr></tfoot>
      </table></div>`;
  }

  function paint() {
    const t = d.totals, p = d.preview;
    const bt = d.purchases.totals;
    const net = Gst.money(t.tax - bt.tax);
    const empty = !p.b2b.length && !p.b2c.length && !p.notes.length;
    $('#out').innerHTML = `
      <div class="kpis">
        <div class="kpi brand"><div class="lb">B2B invoices</div>
          <div class="vl">${money0(t.b2b_value)}</div>
          <div class="ft">${t.b2b_count} invoice${t.b2b_count === 1 ? '' : 's'} · taxable ${fmt(t.b2b_taxable)}</div></div>
        <div class="kpi"><div class="lb">Credit notes</div>
          <div class="vl">${money0(t.cn_value)}</div>
          <div class="ft">${t.cn_count} note${t.cn_count === 1 ? '' : 's'} · taxable ${fmt(t.cn_taxable)}</div></div>
        ${t.b2c_count ? `<div class="kpi"><div class="lb">B2C</div>
          <div class="vl">${money0(t.b2c_value)}</div>
          <div class="ft">${t.b2c_count} to unregistered buyers</div></div>` : ''}
        <div class="kpi"><div class="lb">Taxable value</div>
          <div class="vl">${money0(t.taxable)}</div>
          <div class="ft">net of credit notes</div></div>
        <div class="kpi good"><div class="lb">Output tax</div>
          <div class="vl">${money0(t.tax)}</div>
          <div class="ft">IGST ${fmt(t.igst)} · CGST ${fmt(t.cgst)} · SGST ${fmt(t.sgst)}</div></div>
        <div class="kpi"><div class="lb">Credit on purchases</div>
          <div class="vl">${money0(bt.tax)}</div>
          <div class="ft">${bt.count ? bt.count + ' purchase bill' + (bt.count === 1 ? '' : 's')
            : 'no purchase this month'}</div></div>
        <div class="kpi${net > 0 ? ' bad' : ' good'}"><div class="lb">${net > 0 ? 'Tax to pay' : 'Credit left over'}</div>
          <div class="vl">${money0(Math.abs(net))}</div>
          <div class="ft">output tax less the credit</div></div>
      </div>
      ${d.warnings.length ? `<div class="card"><h2>To look at before filing
          <span class="rt" style="font-weight:400;font-size:12px;color:var(--muted)">${d.warnings.length}</span></h2>
        <div class="bd">${d.warnings.map(w =>
          `<div class="sync-warn"><span class="tag warn">!</span><div>${esc(w)}</div></div>`).join('')}</div></div>` : ''}
      ${empty ? `<div class="card"><div class="empty"><b>No bills in ${esc(monthWord(month))}</b>
          ${esc(d.firm.name)} raised no bill or credit note this month. The workbook
          can still be downloaded, with every sheet empty, for a nil return.</div></div>` : ''}
      ${rateTable(d.rates)}
      ${p.b2b.length ? `<div class="card"><h2>B2B invoices <span class="rt"
          style="font-weight:400;font-size:12px;color:var(--muted)">sheet b2b,sez,de · one row per invoice per rate</span></h2>
        ${billTable(p.b2b)}</div>` : ''}
      ${p.notes.length ? `<div class="card"><h2>Credit notes <span class="rt"
          style="font-weight:400;font-size:12px;color:var(--muted)">sheet cdnr for registered buyers</span></h2>
        ${billTable(p.notes, true)}</div>` : ''}
      ${p.b2c.length ? `<div class="card"><h2>B2C <span class="rt"
          style="font-weight:400;font-size:12px;color:var(--muted)">sheets b2cl (inter-state above ₹1 lakh) and b2cs</span></h2>
        ${billTable(p.b2c)}</div>` : ''}
      ${p.hsn.length ? `<div class="card"><h2>HSN summary <span class="rt"
          style="font-weight:400;font-size:12px;color:var(--muted)">sheets hsn(b2b) and hsn(b2c) · net of credit notes</span></h2>
        <div class="bd flush tbl-scroll"><table><thead><tr><th>For</th><th>HSN</th><th>Description</th>
          <th>UQC</th><th class="r">Quantity</th><th class="c">Rate</th><th class="r">Taxable</th>
          <th class="r">IGST</th><th class="r">CGST</th><th class="r">SGST</th></tr></thead>
        <tbody>${p.hsn.map(h => `<tr><td>${esc(h.book)}</td>
          <td class="mono">${h.hsn ? esc(h.hsn) : '<span class="tag bad">missing</span>'}</td>
          <td>${esc(h.desc)}</td><td class="nw">${esc(h.uqc)}</td>
          <td class="r num">${qty(h.qty)}</td><td class="c">${h.rate}%</td>
          <td class="r num">${fmt(h.taxable)}</td><td class="r num">${fmt(h.igst)}</td>
          <td class="r num">${fmt(h.cgst)}</td><td class="r num">${fmt(h.sgst)}</td></tr>`).join('')}</tbody>
        </table></div></div>` : ''}
      ${p.docs.length ? `<div class="card"><h2>Documents issued <span class="rt"
          style="font-weight:400;font-size:12px;color:var(--muted)">sheet docs · a number missing from a run counts as cancelled</span></h2>
        <div class="bd flush tbl-scroll"><table><thead><tr><th>Nature of document</th>
          <th>From</th><th>To</th><th class="r">Total</th><th class="r">Cancelled</th></tr></thead>
        <tbody>${p.docs.map(x => `<tr><td>${esc(x.nature)}</td><td class="mono">${esc(x.first)}</td>
          <td class="mono">${esc(x.last)}</td><td class="r">${x.total}</td>
          <td class="r">${x.cancelled ? '<b style="color:var(--bad)">' + x.cancelled + '</b>' : '0'}</td></tr>`).join('')}</tbody>
        </table></div></div>` : ''}
      ${b3Table(d.form3b)}
      ${buysTable(d.purchases)}
      ${monthTable(d.months)}
      ${d.left_out.length ? `<div class="card"><h2>Not in the return</h2>
        <div class="bd flush tbl-scroll"><table><thead><tr><th>Document</th><th>Date</th>
          <th>Party</th><th class="r">Value</th><th>Why</th></tr></thead>
        <tbody>${d.left_out.map(x => `<tr><td class="mono">${esc(x[0])}</td><td class="nw">${dt(x[1])}</td>
          <td>${esc(x[2])}</td><td class="r num">${fmt(x[3])}</td>
          <td style="font-size:12.5px;color:var(--muted)">${esc(x[4])}</td></tr>`).join('')}</tbody>
        </table></div></div>` : ''}`;
    $$('[data-month]', $('#out')).forEach(tr => {
      tr.onclick = () => {
        month = tr.dataset.month;
        const y = fyLabel(month + '-01');
        if ($('#gfy').value !== y) { $('#gfy').value = y; months(); }
        $('#gmonth').value = month;
        load();
      };
    });
  }

  async function load() {
    mem.save();
    $('#out').innerHTML = '<div class="empty">Loading…</div>';
    const here = onScreen();
    try {
      const got = await api.get('/api/gstr1', { company: firm, month });
      if (!here()) return;
      d = got;
      paint();
    } catch (e) {
      if (!here()) return;
      $('#out').innerHTML = '<div class="card"><div class="empty"><b>Could not read the month</b>'
        + esc(e.message) + '</div></div>';
    }
  }

  $('#gfirm').onchange = () => { firm = num($('#gfirm').value); load(); };
  $('#gfy').onchange = () => {
    const at = monthsOfFy(fyLabel(month + '-01')).indexOf(month);
    months();
    month = monthsOfFy($('#gfy').value)[Math.max(0, at)];
    $('#gmonth').value = month;
    load();
  };
  $('#gmonth').onchange = () => { month = $('#gmonth').value; load(); };
  // ← and → step the month, the way the arrows beside a statement do.
  KB.setStepper(dir => {
    const to = stepMonth($('#gfy'), $('#gmonth'), month, dir, months);
    if (!to) return false;
    month = to;
    load();
    return true;
  }, 'month');
  load();
});

/* ================================================== staff and salary ---
   Asked for on 19 Sep 2026: *a page to manage the salary and accounts for
   the staff*.

   SHREE keeps everyone paid a salary as an account under SALARY PAYABLE,
   and the shop keeps it one way. Through the month advances ("UPAD") go out
   as payments against the person's account; on the last day a journal books
   what the month earned -- SALARY EXP. debited, their account credited, the
   working as the narration: "284 X 71.25 = 20235 AUG 2026" for hours at a
   rate, "FIX 21000" for a fixed month. These screens read those accounts
   month by month, book a month's salary the same way, and pay from them.

   A month booked in SHREE counts exactly as one booked here -- it shows as
   booked, and is not booked a second time without being asked. The balance
   is never netted across people: owed to some and advanced to others are
   two different facts, as an overdraft is never netted against cash.   */

const PAY_WORD = { month: 'a month', day: 'a day', hour: 'an hour' };
const UNIT_WORD = { month: 'Days worked', day: 'Days worked', hour: 'Hours worked' };
// What each entry on a person's account is, in the words of the vouchers.
const STAFF_KIND = { JOURNAL: 'Journal', PAYMENT: 'Payment', RECEIPT: 'Receipt',
  CONTRA: 'Contra', SALE: 'Challan', PURCHASE: 'Purchase', CREDIT: 'Credit note',
  DEBIT: 'Debit note' };

function payText(r) {
  const rate = num(r.rate);
  return rate ? '₹' + fmt(rate, rate % 1 ? 2 : 0) + ' ' + PAY_WORD[r.pay_basis]
    : '<span style="color:var(--muted)">not set</span>';
}
/** A person's balance: in credit, salary is owed to them; in debit, they
 *  have been paid ahead of what they have earned. */
function staffBal(v) {
  v = num(v);
  if (Math.abs(v) < 0.005) return '<span style="color:var(--muted)">—</span>';
  return `<span class="nw" title="${v < 0 ? 'Credit: salary still owed to them'
      : 'Debit: paid ahead of what they have earned'}">${drcr(v)}</span>
    <span class="tag ${v < 0 ? 'warn' : ''}">${v < 0 ? 'due' : 'advance'}</span>`;
}
function monthsOfFy(fy) {
  const y = parseInt(fy, 10), out = [];
  for (let i = 0; i < 12; i++) {
    const d = new Date(y, 3 + i, 1);
    out.push(d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0'));
  }
  return out;
}
/** The month beside `month`, one either way and over the turn of the year,
 *  for ← and → on a screen read a month at a time. The Year and Month boxes
 *  move with it (`fill` refills the months when the year changes). Answers
 *  the new month, or null past the last year the Year box offers. */
function stepMonth(fySel, monthSel, month, dir, fill) {
  const d = new Date(+month.slice(0, 4), +month.slice(5, 7) - 1 + dir, 1);
  const to = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  const fy = fyLabel(to + '-01');
  if (![...fySel.options].some(o => o.value === fy)) return null;
  if (fySel.value !== fy) { fySel.value = fy; fill(); }
  monthSel.value = to;
  return to;
}
function monthWord(ym) {
  const [y, m] = ym.split('-').map(Number);
  return new Date(y, m - 1, 1).toLocaleString('en-IN', { month: 'long', year: 'numeric' });
}
/** Staff live in the accounts book. Opened in a GST firm, say where. */
function staffElsewhere() {
  const f = (S.meta.companies || []).find(c => c.id === S.meta.order_company);
  return f && f.id !== S.meta.company_id
    ? `<div style="margin-top:12px"><a class="btn" href="#/staff?firm=accounts">
        Open ${esc(f.name)}’s staff</a></div>` : '';
}

/** The cash the shop hands money out of: SHREE's CASH IN HAND, which
 *  carries the entries, before the app's own empty Cash A/c. */
function cashInHand() {
  return S.cashbank.find(b => /CASH IN HAND/i.test(b.name))
    || S.cashbank.find(b => b.system_key === 'CASH')
    || S.cashbank.find(b => /CASH/i.test(b.name));
}

/** Pay somebody, or take money back from them: the ordinary receipt and
 *  payment dialog, with the person already filled in. */
async function payStaff(r, kind) {
  kind = kind || 'PAYMENT';
  // From where their money usually goes out; else the cash in hand.
  await loadCashBank();
  const cash = cashInHand();
  return editVoucher({
    kind, party_id: r.ledger_id, party_name: r.name,
    bank_id: r.bank_id || (cash && cash.id) || '',
    remarks: kind === 'PAYMENT' ? (num(r.closing) < 0 ? 'SALARY' : 'UPAD') : ''
  }, kind);
}

async function staffSettings(r) {
  const html = `
    <div class="grid g3">
      <div><label>Paid by</label><select name="pay_basis">
        <option value="">Not set</option>
        <option value="month">The month (fixed)</option>
        <option value="day">The day</option>
        <option value="hour">The hour</option></select></div>
      <div><label>Rate (₹)</label><input name="rate" class="num"
        value="${num(r.rate) ? num(r.rate).toFixed(2) : ''}"></div>
      <div><label>Mobile</label><input name="mobile" value="${esc(r.mobile || '')}"></div>
    </div>
    <div class="grid g3" style="margin-top:12px">
      <div><label>Joined</label><input type="date" name="joined" value="${esc(r.joined || '')}"></div>
      <div><label>Left</label><input type="date" name="left_on" value="${esc(r.left_on || '')}"></div>
    </div>
    <div style="margin-top:12px"><label>Notes</label>
      <textarea name="notes">${esc(r.notes || '')}</textarea></div>
    <div class="hint">The rate is a whole month’s salary, a day’s pay or an
      hour’s pay.${r.pay_from && r.pay_from !== 'set here'
        ? ' Until it is set here it is read off the books — the '
          + esc(r.pay_from) + '.' : ''} Somebody given a date they left is
      kept off the list from then on.</div>`;
  const saved = await modal('Pay — ' + r.name, html, [
    { label: 'Cancel', value: null },
    { label: 'Save', cls: 'primary', value: true, before: async bg => {
        const data = readForm(bg);
        if (num(data.rate) && !data.pay_basis) {
          bad('Choose whether that is for a month, a day or an hour.');
          return false;
        }
        try { await api.put('/api/staff/' + r.ledger_id, data); }
        catch (e) { bad(e.message); return false; }
      } }
  ], { onOpen: bg => {
    $('[name=pay_basis]', bg).value = r.pay_from === 'set here' || num(r.rate)
      ? (r.pay_basis || '') : '';
  } });
  if (saved) ok('Saved.');
  return saved;
}

/** Loose money on hand with somebody: out with them, or owed to them. */
function looseBal(v) {
  v = num(v);
  if (Math.abs(v) < 0.005) return '<span style="color:var(--muted)">—</span>';
  return `<span class="nw">${fmt(Math.abs(v))}</span> <span class="tag ${v < 0 ? 'warn' : ''}"
    title="${v < 0 ? 'They gave more than they were given: owed to them'
      : 'Given to them and not yet taken back'}">${v < 0 ? 'owed' : 'with them'}</span>`;
}

/* Loose money: cash handed over in an emergency, and taken back later --
   in cash, or cut from their salary. It goes on an account of its own,
   "NAME (LOOSE MONEY)", so it never hides inside the salary balance. */
async function looseMoney(r, way, balance) {
  await loadCashBank();
  const cash = cashInHand();
  // Loose money is handed over in cash; the bank is a choice away.
  const from = (cash && cash.id) || r.bank_id || '';
  const bal = num(balance);
  const html = `
    <div class="grid g2">
      <label class="check" style="margin:0"><input type="radio" name="direction" value="given"
        style="width:auto"${way !== 'taken' ? ' checked' : ''}> <b>Given</b> to them</label>
      <label class="check" style="margin:0"><input type="radio" name="direction" value="taken"
        style="width:auto"${way === 'taken' ? ' checked' : ''}> <b>Taken</b> from them</label>
    </div>
    <div class="grid g3" style="margin-top:12px">
      <div><label>Amount (₹) *</label><input name="amount" class="num"></div>
      <div><label>Date</label><input type="date" name="date" value="${today()}"></div>
      <div id="lm-how-box"><label>How</label><select name="how">
        <option value="cash">In cash / the bank</option>
        <option value="salary">Cut from their salary</option></select></div>
    </div>
    <div style="margin-top:12px" id="lm-bank-box"><label id="lm-bank-lb">From</label>
      <select name="bank_id">${S.cashbank.map(b =>
        `<option value="${b.id}"${b.id === num(from) ? ' selected' : ''}>${esc(b.name)}</option>`).join('')}</select></div>
    <div style="margin-top:12px"><label>What for</label>
      <input name="remarks" placeholder="e.g. hospital, going home, a loan"></div>
    <div class="hint">${Math.abs(bal) < 0.005 ? 'Nothing is out with them now.'
      : bal > 0 ? 'With them now: <b>' + esc(money(bal)) + '</b>.'
      : 'Owed to them now: <b>' + esc(money(-bal)) + '</b>.'}
      Loose money is kept on its own account, ${esc(r.name)} (LOOSE MONEY),
      apart from their salary. <i>Cut from their salary</i> takes it out of
      what they are owed instead of in cash.</div>`;
  let said = '';
  const saved = await modal('Loose money — ' + r.name, html, [
    { label: 'Cancel', value: null },
    { label: 'Save', cls: 'primary', value: true, before: async bg => {
        const data = readForm(bg);
        data.direction = ($('[name=direction]:checked', bg) || {}).value || 'given';
        if (data.direction === 'given') data.how = 'cash';
        if (!(num(data.amount) > 0)) { bad('Give the amount.'); return false; }
        try {
          const res = await api.post('/api/staff/' + r.ledger_id + '/loose', data);
          said = (data.direction === 'given' ? 'Loose money given: '
            : data.how === 'salary' ? 'Cut from salary: ' : 'Loose money taken: ')
            + money(data.amount) + ' · now ' + (Math.abs(res.balance) < 0.005 ? 'nothing out'
            : res.balance > 0 ? money(res.balance) + ' with them'
            : money(-res.balance) + ' owed to them');
        } catch (e) { bad(e.message); return false; }
      } }
  ], { onOpen: bg => {
    const sync = () => {
      const way = ($('[name=direction]:checked', bg) || {}).value;
      const how = $('[name=how]', bg).value;
      $('#lm-how-box', bg).style.visibility = way === 'taken' ? '' : 'hidden';
      $('#lm-bank-box', bg).style.display = way === 'taken' && how === 'salary' ? 'none' : '';
      $('#lm-bank-lb', bg).textContent = way === 'taken' ? 'Into' : 'From';
    };
    $$('[name=direction]', bg).forEach(x => { x.onchange = sync; });
    $('[name=how]', bg).onchange = sync;
    sync();
    setTimeout(() => { const b = $('[name=amount]', bg); if (b) b.focus(); }, 60);
  } });
  if (saved) { invalidate(); ok(said); }
  return saved;
}

async function addStaff() {
  const cands = await api.get('/api/staff/accounts');
  const html = `
    <div><label>Name *</label><input name="name" list="staff-cands"
      autocomplete="off" placeholder="A new person, or an account the books have">
      <datalist id="staff-cands">${cands.map(c =>
        `<option value="${esc(c.name)}">${esc(c.group_name)}</option>`).join('')}</datalist>
      <div class="hint" id="add-said">&nbsp;</div></div>
    <div class="grid g3" style="margin-top:12px">
      <div><label>Paid by</label><select name="pay_basis">
        <option value="">Not set</option>
        <option value="month">The month (fixed)</option>
        <option value="day">The day</option>
        <option value="hour">The hour</option></select></div>
      <div><label>Rate (₹)</label><input name="rate" class="num"></div>
      <div><label>Mobile</label><input name="mobile"></div>
    </div>
    <div class="grid g3" style="margin-top:12px">
      <div><label>Joined</label><input type="date" name="joined" value="${today()}"></div>
    </div>
    <div class="hint">A new name opens an account for them under SALARY
      PAYABLE, as SHREE files its staff. Accounts the books already have are
      offered as you type — the family’s “(SALARY)” accounts first.</div>`;
  const find = name => cands.find(c => c.name.toUpperCase().replace(/\s+/g, ' ')
    === name.trim().toUpperCase().replace(/\s+/g, ' '));
  const saved = await modal('Add staff', html, [
    { label: 'Cancel', value: null },
    { label: 'Add', cls: 'primary', value: true, before: async bg => {
        const data = readForm(bg);
        if (!data.name.trim()) { bad('Give the name.'); return false; }
        if (num(data.rate) && !data.pay_basis) {
          bad('Choose whether that is for a month, a day or an hour.');
          return false;
        }
        const hit = find(data.name);
        if (hit) { data.ledger_id = hit.id; delete data.name; }
        try { await api.post('/api/staff', data); }
        catch (e) { bad(e.message); return false; }
      } }
  ], { onOpen: bg => {
    const box = $('[name=name]', bg);
    box.oninput = () => {
      const hit = find(box.value);
      $('#add-said', bg).textContent = hit
        ? 'Puts ' + hit.name + ' (' + hit.group_name + ') on the list, with its entries.'
        : box.value.trim() ? 'Opens a new account: ' + box.value.trim().toUpperCase() : ' ';
    };
  } });
  if (saved) { invalidate(); ok('Added to the staff list.'); }
  return saved;
}

route(/^\/staff$/, async (view, _m, q) => {
  let month = /^\d{4}-\d{2}$/.test(q.month || '') ? q.month : today().slice(0, 7);
  let last = null;

  setTitle('Staff & salary', 'Salary accounts, advances and each month’s salary', [
    { label: 'Book salary', cls: 'primary',
      onClick: () => location.hash = '#/staff/salary?month=' + month },
    { label: '＋ Add staff', onClick: async () => { if (await addStaff()) load(); } },
    { label: '🖨 Print', onClick: () => window.print() }
  ]);

  view.innerHTML = `<div class="card"><div class="bd"><div class="row">
      <div><label>Year</label><select id="sfy" style="width:calc(150px * var(--typescale))"
        title="A financial year, 1 April to 31 March">${fyChoices().map(f =>
          `<option value="${f}">${f}</option>`).join('')}</select></div>
      <div><label>Month</label><select id="smonth" style="width:calc(190px * var(--typescale))"></select></div>
      <div><label>&nbsp;</label><label style="margin:0;text-transform:none;font-size:13px"
        title="People who have left, or have had no entry for a year and are owed nothing either way">
        <input type="checkbox" id="former" style="width:auto"
          ${q.former ? 'checked' : ''}> Former staff too</label></div>
      <div class="spacer"></div>
    </div></div></div><div id="out"></div>`;

  function months() {
    const fy = $('#sfy').value;
    $('#smonth').innerHTML = monthsOfFy(fy).map(m =>
      `<option value="${m}">${esc(monthWord(m))}</option>`).join('');
  }
  const mem = screenState('/staff', {
    month: { get: () => month, set: v => { if (/^\d{4}-\d{2}$/.test(v)) month = v; } },
    former: '#former'
  }).restore(q);
  if (![...$('#sfy').options].some(o => o.value === fyLabel(month + '-01'))) {
    $('#sfy').insertAdjacentHTML('afterbegin',
      `<option value="${fyLabel(month + '-01')}">${fyLabel(month + '-01')}</option>`);
  }
  $('#sfy').value = fyLabel(month + '-01');
  months();
  $('#smonth').value = month;

  function paint() {
    const d = last, t = d.totals;
    if (!d.rows.length && !d.former_hidden) {
      $('#out').innerHTML = `<div class="card"><div class="empty"><b>No staff in
        ${esc(S.meta.company.name)}</b>Staff are the accounts under SALARY
        PAYABLE, where SHREE files everyone it books a salary for. Add somebody
        with <i>＋ Add staff</i>.${staffElsewhere()}</div></div>`;
      return;
    }
    // Still being paid: a rate set here, or a salary in the last two months.
    const since = new Date(d.start);
    since.setDate(since.getDate() - 62);
    const lately = since.toISOString().slice(0, 10);
    const unbooked = d.rows.filter(r => !r.booked.length && num(r.rate)
      && (r.pay_from === 'set here' || (r.last_salary && r.last_salary.date >= lately))).length;
    // Money handed back is rare; its column shows only in a month that has some.
    const backs = d.rows.some(r => r.back);
    $('#out').innerHTML = `
      <div class="kpis">
        <div class="kpi brand"><div class="lb">Salary booked · ${esc(monthWord(d.month))}</div>
          <div class="vl">${money(t.salary)}</div>
          <div class="ft">${t.posted} of ${d.rows.length} booked${unbooked
            ? ' · <a href="#/staff/salary?month=' + d.month + '">' + unbooked + ' more to book</a>' : ''}</div></div>
        <div class="kpi bad"><div class="lb">Paid out</div>
          <div class="vl">${money(t.paid)}</div>
          <div class="ft">advances and salary, in ${esc(monthWord(d.month))}${num(t.back)
            ? ' · ' + fmt(t.back) + ' handed back' : ''}</div></div>
        <div class="kpi"><div class="lb">Owed to staff</div>
          <div class="vl">${money(t.due)}</div>
          <div class="ft">salary still to pay · ${t.due_count} ${t.due_count === 1 ? 'person' : 'people'}</div></div>
        <div class="kpi"><div class="lb">Advances out</div>
          <div class="vl">${money(t.advance)}</div>
          <div class="ft">paid ahead of salary · ${t.advance_count} ${t.advance_count === 1 ? 'person' : 'people'}</div></div>
        <div class="kpi"><div class="lb">Loose money out</div>
          <div class="vl">${money(t.loose_out)}</div>
          <div class="ft">${t.loose_out_count
            ? 'with ' + t.loose_out_count + ' ' + (t.loose_out_count === 1 ? 'person' : 'people') : 'none out'}${
            t.loose_given || t.loose_taken ? ' · ' + [t.loose_given ? fmt(t.loose_given, 0) + ' given'
              : '', t.loose_taken ? fmt(t.loose_taken, 0) + ' taken' : ''].filter(Boolean).join(', ')
              + ' this month' : ''}${t.loose_owed ? ' · <b>' + fmt(t.loose_owed) + ' owed to staff</b>' : ''}</div></div>
      </div>
      <div class="card"><h2>${esc(monthWord(d.month))}<span class="rt"
          style="font-weight:400;font-size:12px;color:var(--muted)">
          ${dt(d.start)} to ${dt(d.end)} · click a name for the year</span></h2>
        <div class="bd flush tbl-scroll"><table><thead><tr>
          <th style="min-width:180px">Name</th><th>Pay</th>
          <th class="r" title="Journals crediting their account: the month's salary booked">Salary booked</th>
          <th class="r" title="Payments to them: advances and salary">Paid</th>
          ${backs ? '<th class="r" title="Receipts from them: money handed back">Back</th>' : ''}
          <th class="r" title="Their salary account: in credit, salary still owed to them; in debit, paid ahead">Balance</th>
          <th class="r" title="Cash handed over in an emergency, kept on its own account apart from salary">Loose money</th>
          <th class="r">Actions</th>
        </tr></thead><tbody>${d.rows.map((r, i) => `<tr class="clickable" data-i="${i}">
          <td><b>${goLink('#/staff/' + r.ledger_id, esc(r.name), 'Their year, month by month')}</b>
            ${r.former ? ' <span class="tag">former</span>' : ''}
            ${r.added ? ' <span class="tag" title="Put on the staff list here">added</span>' : ''}
            ${r.mobile ? '<div style="font-size:12px;color:var(--muted)">' + esc(r.mobile) + '</div>' : ''}</td>
          <td class="nw"><button class="link" data-set="${i}" title="Change how they are paid"
              style="padding:0;text-align:left">${payText(r)}</button>${r.pay_from && r.pay_from !== 'set here'
            ? '<div style="font-size:12px;color:var(--muted)" title="Read off the books: the '
              + esc(r.pay_from) + '">from the books</div>' : ''}</td>
          <td class="r num nw">${r.salary ? fmt(r.salary) : '—'}<div style="font-size:12px">${
            r.booked.length ? r.booked.map(b => goLink(docLink('JOURNAL', b.voucher_id),
              'J ' + esc(b.no), 'Open the salary journal') + (b.shree ? ' · SHREE' : '')).join(', ')
            : '<span style="color:var(--muted)">not booked</span>'}</div></td>
          <td class="r num" style="color:${r.paid ? 'var(--bad)' : 'inherit'}">${r.paid ? fmt(r.paid) : '—'}</td>
          ${backs ? '<td class="r num">' + (r.back ? fmt(r.back) : '—') + '</td>' : ''}
          <td class="r num nw"><b>${staffBal(r.closing)}</b></td>
          <td class="r num nw">${looseBal(r.loose_closing)}${r.loose_given || r.loose_taken
            ? '<div style="font-size:12px;color:var(--muted)">' + [r.loose_given
              ? '+' + fmt(r.loose_given, 0) + ' given' : '', r.loose_taken
              ? '−' + fmt(r.loose_taken, 0) + ' taken' : ''].filter(Boolean).join('<br>') + '</div>' : ''}</td>
          <td class="r nw"><button class="link" data-pay="${i}">Pay</button>
            <button class="link" data-loose="${i}" title="Loose money given or taken">Loose</button></td>
        </tr>`).join('')}</tbody>
        <tfoot><tr><td colspan="2">${d.rows.length} ${d.rows.length === 1 ? 'person' : 'people'}</td>
          <td class="r num">${fmt(t.salary)}</td><td class="r num">${fmt(t.paid)}</td>
          ${backs ? '<td class="r num">' + fmt(t.back) + '</td>' : ''}<td></td>
          <td class="r num nw">${t.loose_out ? fmt(t.loose_out) + ' with them' : ''}</td><td></td></tr></tfoot>
        </table></div>${d.former_hidden ? `<div class="bd"
          style="border-top:1px solid var(--line);font-size:12.5px;color:var(--muted)">
          <b>${d.former_hidden}</b> former staff not shown — left, or no entry for a
          year and nothing owed either way. Tick <i>Former staff too</i> to see them.</div>` : ''}</div>`;
    $$('#out tr[data-i]').forEach(tr => {
      tr.onclick = () => { location.hash = '#/staff/' + d.rows[num(tr.dataset.i)].ledger_id; };
    });
    $$('#out [data-pay]').forEach(b => {
      b.onclick = async e => {
        e.stopPropagation();
        if (await payStaff(d.rows[num(b.dataset.pay)])) load();
      };
    });
    $$('#out [data-loose]').forEach(b => {
      b.onclick = async e => {
        e.stopPropagation();
        const r = d.rows[num(b.dataset.loose)];
        if (await looseMoney(r, 'given', r.loose_closing)) load();
      };
    });
    $$('#out [data-set]').forEach(b => {
      b.onclick = async e => {
        e.stopPropagation();
        if (await staffSettings(d.rows[num(b.dataset.set)])) load();
      };
    });
  }

  async function load() {
    mem.save();
    $('#out').innerHTML = '<div class="empty">Loading…</div>';
    const here = onScreen();
    const got = await api.get('/api/staff', { month, former: $('#former').checked ? '1' : '' });
    if (!here()) return;
    last = got;
    paint();
  }

  $('#sfy').onchange = () => {
    // The same month of the year chosen: September stays September.
    const at = monthsOfFy(fyLabel(month + '-01')).indexOf(month);
    months();
    month = monthsOfFy($('#sfy').value)[Math.max(0, at)];
    $('#smonth').value = month;
    load();
  };
  $('#smonth').onchange = () => { month = $('#smonth').value; load(); };
  $('#former').onchange = load;
  KB.setStepper(dir => {
    const to = stepMonth($('#sfy'), $('#smonth'), month, dir, months);
    if (!to) return false;
    month = to;
    load();
    return true;
  }, 'month');
  load();
});

/* The month's salary, booked the way the shop books it: one journal a
   person, dated the last day, SALARY EXP. debited and their account
   credited, the working as the narration. A fixed month is the rate for a
   full month and a share of it for fewer days; a day's or an hour's rate is
   multiplied out. Everyone already booked for the month -- in SHREE or here
   -- is left unticked, and booking them again has to be asked for. */
route(/^\/staff\/salary$/, async (view, _m, q) => {
  const month = /^\d{4}-\d{2}$/.test(q.month || '') ? q.month : today().slice(0, 7);
  const here = onScreen();
  const d = await api.get('/api/staff', { month });
  if (!here()) return;

  setTitle('Book salary — ' + monthWord(month),
    'One journal a person, as SHREE books it', [
      { label: 'Book salary', cls: 'primary', onClick: () => book() },
      { label: 'Cancel', onClick: () => leave('#/staff?month=' + month) }
    ]);

  if (!d.rows.length) {
    view.innerHTML = `<div class="card"><div class="empty"><b>No staff in
      ${esc(S.meta.company.name)}</b>There is nobody to book a salary for.${staffElsewhere()}</div></div>`;
    return;
  }

  // Only somebody still being paid is filled in and ticked: a rate set
  // here, or a salary booked in the last two months. A rate read off a
  // salary of 2023 is offered, never booked by itself.
  const since = new Date(d.start);
  since.setDate(since.getDate() - 62);
  const lately = since.toISOString().slice(0, 10);
  const lines = d.rows.map(r => {
    const basis = r.pay_basis || 'month';
    const current = r.pay_from === 'set here'
      || !!(r.last_salary && r.last_salary.date >= lately);
    const ln = { r, basis, rate: num(r.rate), current, manual: false,
                 units: basis === 'month' && current ? d.days : '', amount: '' };
    ln.amount = figure(ln);
    ln.tick = !r.booked.length && current && num(ln.amount) > 0;
    return ln;
  });
  function figure(ln) {
    const rate = num(ln.rate), units = num(ln.units);
    if (!rate || !units) return '';
    return String(Math.round(ln.basis === 'month' ? rate * units / d.days : rate * units));
  }

  view.innerHTML = `<div class="card"><div class="bd"><div class="row">
      <div><label>Month</label><input type="month" id="bm" value="${month}"></div>
      <div><label>Date of the journals</label><input type="date" id="bdate" value="${d.end}"></div>
      <div style="flex:1;min-width:230px;max-width:340px"><label>Booked to</label>
        <select id="bexp">${d.expense.map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join('')
          || '<option value="">No salary expense account</option>'}</select></div>
      <div class="spacer"></div>
    </div><div class="hint" style="margin-top:10px">A fixed month is the rate
      for all ${d.days} days and that share of it for fewer. By the day or the
      hour, give what they worked. Everyone already booked for
      ${esc(monthWord(month))} — in SHREE or here — is left unticked.</div></div></div>
    <div class="card"><div class="bd flush tbl-scroll"><table><thead><tr>
      <th style="width:34px"><input type="checkbox" id="ball" style="width:auto"
        title="Tick or untick everyone"></th>
      <th>Name</th><th>Paid by</th><th class="r">Rate (₹)</th>
      <th class="r">Days / hours</th><th class="r">Salary (₹)</th>
      <th style="min-width:150px">Already</th>
    </tr></thead><tbody id="blines">${lines.map((ln, i) => `<tr data-i="${i}">
      <td><input type="checkbox" data-f="tick" style="width:auto"${ln.tick ? ' checked' : ''}></td>
      <td><b>${esc(ln.r.name)}</b>${ln.r.pay_from && ln.r.pay_from !== 'set here'
        ? '<div style="font-size:12px;color:var(--muted)">rate from the '
          + esc(ln.r.pay_from) + '</div>' : ''}${!ln.current && !ln.r.booked.length
        ? '<div style="font-size:12px;color:var(--muted)">no salary lately — give what they worked to book one</div>' : ''}</td>
      <td><select data-f="basis" style="min-width:calc(140px * var(--typescale))">${['month', 'day', 'hour'].map(b =>
        `<option value="${b}"${b === ln.basis ? ' selected' : ''}>${{ month: 'Fixed month', day: 'By the day', hour: 'By the hour' }[b]}</option>`).join('')}</select></td>
      <td class="r"><input data-f="rate" class="num" style="width:110px" value="${ln.rate ? ln.rate.toFixed(2) : ''}"></td>
      <td class="r"><input data-f="units" class="num" style="width:110px" value="${esc(ln.units)}"
        placeholder="${num(ln.r.last_units) ? 'last ' + qty(ln.r.last_units) : ''}"
        title="${UNIT_WORD[ln.basis]}"></td>
      <td class="r"><input data-f="amount" class="num" style="width:110px" value="${esc(ln.amount)}"></td>
      <td style="font-size:12.5px">${ln.r.booked.length ? ln.r.booked.map(b =>
          '<span class="tag ok">booked</span> ' + goLink(docLink('JOURNAL', b.voucher_id),
          'J ' + esc(b.no), 'Open the salary journal') + ' ' + fmt(b.amount)
          + (b.shree ? ' <span class="tag">SHREE</span>' : '')).join('<br>')
          + '<div style="color:var(--bad)">ticked, it is booked again</div>' : ''}</td>
    </tr>`).join('')}</tbody>
    <tfoot><tr><td></td><td colspan="4" id="bcount"></td>
      <td class="r num"><b id="btotal"></b></td><td></td></tr></tfoot></table></div></div>`;

  function sum() {
    const on = lines.filter(ln => ln.tick && num(ln.amount) > 0);
    $('#bcount').textContent = on.length + ' of ' + lines.length + ' to be booked';
    $('#btotal').textContent = fmt(on.reduce((a, ln) => a + num(ln.amount), 0));
    $$('#blines tr').forEach(tr => {
      const ln = lines[num(tr.dataset.i)];
      $('[data-f=units]', tr).title = UNIT_WORD[ln.basis];
      $('[data-f=units]', tr).placeholder = ln.basis === 'month' ? d.days + ' days'
        : num(ln.r.last_units) ? 'last ' + qty(ln.r.last_units) : '';
    });
  }
  $$('#blines tr').forEach(tr => {
    const ln = lines[num(tr.dataset.i)];
    $$('[data-f]', tr).forEach(inp => {
      const f = inp.dataset.f;
      inp.addEventListener(f === 'tick' || f === 'basis' ? 'change' : 'input', () => {
        if (f === 'tick') { ln.tick = inp.checked; sum(); return; }
        ln[f] = inp.value;
        if (f === 'amount') ln.manual = true;
        else {
          if (f === 'basis' && ln.basis === 'month' && !num(ln.units)) {
            ln.units = String(d.days);
            $('[data-f=units]', tr).value = ln.units;
          }
          ln.manual = false;
          ln.amount = figure(ln);
          $('[data-f=amount]', tr).value = ln.amount;
        }
        // Somebody not booked yet is ticked the moment they have a figure.
        if (!ln.r.booked.length && !ln.tick && num(ln.amount) > 0) {
          ln.tick = true;
          $('[data-f=tick]', tr).checked = true;
        }
        sum();
      });
    });
  });
  $('#ball').onchange = () => {
    lines.forEach((ln, i) => {
      ln.tick = $('#ball').checked && num(ln.amount) > 0 && !ln.r.booked.length;
      $('#blines tr[data-i="' + i + '"] [data-f=tick]').checked = ln.tick;
    });
    sum();
  };
  $('#bm').onchange = () => {
    if (/^\d{4}-\d{2}$/.test($('#bm').value)) location.hash = '#/staff/salary?month=' + $('#bm').value;
  };
  KB.setStepper(dir => {
    const d = new Date(+month.slice(0, 4), +month.slice(5, 7) - 1 + dir, 1);
    const go = () => {
      location.hash = '#/staff/salary?month=' + d.getFullYear() + '-'
        + String(d.getMonth() + 1).padStart(2, '0');
    };
    if (!KB.unsavedWork()) { go(); return true; }
    confirmBox('Leave without booking',
      'The figures typed here have not been booked. Go to the other month anyway?')
      .then(yes => { if (yes) go(); });
    return true;
  }, 'month');
  // The rows are boxes to be typed into: Enter walks them, as on a bill,
  // and the screen starts on the first person's days rather than the month.
  enterMovesOn(view);
  const firstDays = $('#blines [data-f=units]');
  if (firstDays) firstDays.setAttribute('autofocus', '');
  sum();

  async function book() {
    const on = lines.filter(ln => ln.tick && num(ln.amount) > 0);
    if (!on.length) { bad('Nobody is ticked with a salary to book.'); return; }
    if (!$('#bexp').value) { bad('Choose the account the salary is booked to.'); return; }
    const total = on.reduce((a, ln) => a + num(ln.amount), 0);
    const again = on.filter(ln => ln.r.booked.length).length;
    const go = await modal('Book salary', `<p style="margin:0;font-size:14px">
        ${on.length} journal${on.length === 1 ? '' : 's'} dated <b>${dt($('#bdate').value)}</b>,
        <b>${money(total)}</b> in all, booked to
        <b>${esc($('#bexp').selectedOptions[0].textContent.replace(/\.$/, ''))}</b>.${again
          ? ` <br><br><b style="color:var(--bad)">${again} of them ${again === 1 ? 'is' : 'are'}
            already booked for ${esc(monthWord(month))}</b> and will be booked again.` : ''}</p>`, [
      { label: 'Cancel', value: false },
      { label: 'Book', cls: 'primary', value: true }
    ]);
    if (!go) return;
    try {
      const res = await api.post('/api/staff/salary', {
        month, date: $('#bdate').value, expense_id: $('#bexp').value,
        lines: on.map(ln => ({ ledger_id: ln.r.ledger_id, basis: ln.basis,
          rate: ln.rate, units: ln.units, amount: ln.amount, again: ln.r.booked.length ? 1 : 0 }))
      });
      invalidate();
      ok('Salary booked: ' + res.made.length + ' journal' + (res.made.length === 1 ? '' : 's')
         + ', ' + money(res.total));
      location.hash = '#/staff?month=' + month;
    } catch (e) { bad(e.message); }
  }
});

/* One person over a year: what each month booked, what was paid, and where
   it left them. A month opens to its entries; the account's own statement,
   drawn the one way every account is drawn, is a click away.            */
route(/^\/staff\/(\d+)$/, async (view, m, q) => {
  const lid = num(m[0]);
  let fy = /^\d{4}-\d{2}$/.test(q.fy || '') ? q.fy : fyLabel(today());
  let d = null;

  async function load() {
    stamp({}, '#/staff/' + lid + (fy !== fyLabel(today()) ? '?fy=' + fy : ''));
    const here = onScreen();
    const got = await api.get('/api/staff/' + lid, { fy });
    if (!here()) return;
    d = got;
    paint();
  }

  function paint() {
    const r = d.staff;
    r.closing = d.closing;
    setTitle(r.name, 'Staff', [
      { label: '＋ Pay', cls: 'primary',
        onClick: async () => { if (await payStaff(r)) load(); } },
      { label: '↩ Received back',
        onClick: async () => { if (await payStaff(r, 'RECEIPT')) load(); } },
      { label: '✎ Rate',
        onClick: async () => { if (await staffSettings(r)) load(); } },
      { label: 'Statement', onClick: () => location.hash = accountLink(lid) },
      ...(r.listed ? [{ label: 'Off the list', onClick: async () => {
        if (!await confirmBox('Take off the staff list',
          r.name + ' leaves the staff list. The account and every entry in it stay in the books.')) return;
        try { await api.del('/api/staff/' + lid); ok('Taken off the staff list.'); leave('#/staff'); }
        catch (e) { bad(e.message); }
      } }] : [{ label: '＋ Put on the list', onClick: async () => {
        try { await api.post('/api/staff', { ledger_id: lid }); ok('On the staff list.'); load(); }
        catch (e) { bad(e.message); }
      } }])
    ]);
    const lm = d.loose;
    const others = d.months.some(x => Math.abs(x.other) >= 0.005);
    const yr = d.months.reduce((a, x) => ({ salary: a.salary + x.salary, paid: a.paid + x.paid,
      back: a.back + x.back }), { salary: 0, paid: 0, back: 0 });
    view.innerHTML = `
      <div class="card"><div class="bd"><div class="row">
        <div><label>Year</label><select id="dfy" style="width:calc(150px * var(--typescale))">${fyChoices().map(f =>
          `<option value="${f}"${f === fy ? ' selected' : ''}>${f}</option>`).join('')}</select></div>
        <div class="spacer"></div>
        <div style="font-size:13px;color:var(--muted)">${r.mobile ? '📞 ' + esc(r.mobile) + ' · ' : ''}${r.joined
          ? 'joined ' + dt(r.joined) + ' · ' : ''}${r.left_on ? 'left ' + dt(r.left_on) + ' · ' : ''}${esc(r.notes || '')}</div>
      </div></div></div>
      <div class="kpis">
        <div class="kpi ${d.closing < -0.005 ? 'bad' : 'brand'}"><div class="lb">${d.closing < -0.005
            ? 'Owed to them' : d.closing > 0.005 ? 'Advance with them' : 'Balance now'}</div>
          <div class="vl">${money(Math.abs(d.closing))}</div>
          <div class="ft">${d.closing < -0.005 ? 'salary still to pay · ' + drcr(d.closing)
            : d.closing > 0.005 ? 'paid ahead of their salary · ' + drcr(d.closing)
            : 'square'}</div></div>
        <div class="kpi"><div class="lb">Pay</div><div class="vl">${payText(r)}</div>
          <div class="ft">${r.pay_from === 'set here' ? 'set here' : r.pay_from
            ? 'read off the ' + esc(r.pay_from) : 'not set — ✎ Rate sets it'}</div></div>
        <div class="kpi"><div class="lb">Salary booked · ${esc(fy)}</div>
          <div class="vl">${money(yr.salary)}</div><div class="ft">${d.months.filter(x => x.salary).length} month(s)</div></div>
        <div class="kpi bad"><div class="lb">Paid · ${esc(fy)}</div>
          <div class="vl">${money(yr.paid)}</div><div class="ft">${yr.back ? fmt(yr.back) + ' handed back' : 'advances and salary'}</div></div>
        <div class="kpi"><div class="lb">Loose money</div>
          <div class="vl">${money(Math.abs(lm ? lm.closing : 0))}</div>
          <div class="ft">${!lm || Math.abs(lm.closing) < 0.005 ? 'nothing out'
            : lm.closing > 0 ? 'with them, to be taken back' : 'owed to them'}</div></div>
      </div>
      <div class="card"><h2>Month by month<span class="rt"
          style="font-weight:400;font-size:12px;color:var(--muted)">click a month for its entries</span></h2>
        <div class="bd flush tbl-scroll"><table><thead><tr><th style="width:40px"></th>
          <th>Month</th><th class="r">Opening</th><th class="r">Salary booked</th>
          <th class="r">Paid</th><th class="r">Back</th>${others ? `<th class="r"
            title="Anything else on the account: loose money cut from salary, corrections">Other</th>` : ''}
          <th class="r">Closing</th></tr></thead>
        <tbody>${d.months.length ? latestFirst(d.months).map(([x, i]) => `<tr class="clickable" data-m="${i}">
            <td style="color:var(--muted)"><span class="caret">▸</span></td>
            <td class="nw"><b>${esc(monthWord(x.month))}</b>${x.entries ? '' : ' <span style="color:var(--muted);font-size:12px">no entries</span>'}</td>
            <td class="r num nw">${staffBal(x.opening)}</td>
            <td class="r num">${x.salary ? fmt(x.salary) : '—'}</td>
            <td class="r num" style="color:${x.paid ? 'var(--bad)' : 'inherit'}">${x.paid ? fmt(x.paid) : '—'}</td>
            <td class="r num">${x.back ? fmt(x.back) : '—'}</td>
            ${others ? '<td class="r num nw">' + (Math.abs(x.other) < 0.005 ? '—' : drcr(x.other)) + '</td>' : ''}
            <td class="r num nw"><b>${staffBal(x.closing)}</b></td></tr>
          <tr class="det" data-for="${i}" style="display:none"><td colspan="${others ? 8 : 7}" style="padding:0"></td></tr>`).join('')
          : '<tr><td colspan="7" class="empty">Nothing in this year yet.</td></tr>'}</tbody>
        <tfoot><tr><td></td><td>${esc(fy)}</td><td></td><td class="r num">${fmt(yr.salary)}</td>
          <td class="r num">${fmt(yr.paid)}</td><td class="r num">${fmt(yr.back)}</td>${others ? '<td></td>' : ''}<td></td></tr></tfoot>
        </table></div></div>
      <div class="card"><h2>Loose money<span class="rt">
          <button class="sm" id="lm-give">＋ Given</button>
          <button class="sm" id="lm-take">↩ Taken</button>
          ${lm ? '<button class="sm" id="lm-stmt" title="The loose-money account\'s own statement">Statement</button>' : ''}</span></h2>
        ${!lm || (!lm.entries.length && Math.abs(lm.opening) < 0.005)
          ? `<div class="bd"><div class="empty" style="padding:14px"><b>${lm ? 'No loose money in ' + esc(fy)
              : 'No loose money given yet'}</b>Cash handed over in an emergency — and taken
              back in cash or cut from salary — is kept here, on its own account, apart
              from their salary.</div></div>`
          : `<div class="bd flush tbl-scroll"><table><thead><tr>
              <th style="width:104px">Date</th><th>What for</th><th style="width:130px">Entry</th>
              <th class="r" style="width:130px">Given</th><th class="r" style="width:130px">Taken</th>
              <th class="r" style="width:170px">With them</th></tr></thead>
            <tbody><tr><td class="nw">${dt(fyRange(fy).from)}</td><td colspan="4"><i>B/F</i></td>
              <td class="r num nw">${looseBal(lm.opening)}</td></tr>
            ${lm.entries.map(e => `<tr class="${docHref(e.doc_kind, e.doc_id) ? 'clickable' : ''}"
                data-doc="${e.doc_id}" data-kind="${e.doc_kind}">
              <td class="nw">${dt(e.date)}</td>
              <td>${esc((e.narration || '').replace(/^LOOSE MONEY (GIVEN|TAKEN|CUT FROM SALARY)( - )?/, '') || '—')}
                ${/CUT FROM SALARY/.test(e.narration || '') ? ' <span class="tag">cut from salary</span>' : ''}</td>
              <td class="nw" style="color:var(--muted)">${esc(STAFF_KIND[e.doc_kind] || e.doc_kind)} ${esc(e.doc_no)}</td>
              <td class="r num" style="color:${e.debit ? 'var(--bad)' : 'inherit'}">${e.debit ? fmt(e.debit) : ''}</td>
              <td class="r num" style="color:${e.credit ? 'var(--ok)' : 'inherit'}">${e.credit ? fmt(e.credit) : ''}</td>
              <td class="r num nw">${looseBal(e.balance)}</td></tr>`).join('')}</tbody>
            <tfoot><tr><td colspan="3">${esc(fy)}</td><td class="r num">${fmt(lm.given)}</td>
              <td class="r num">${fmt(lm.taken)}</td><td class="r num nw"><b>${looseBal(lm.closing)}</b></td></tr></tfoot>
            </table></div>`}</div>`;
    $('#dfy').onchange = () => { fy = $('#dfy').value; load(); };
    $('#lm-give').onclick = async () => { if (await looseMoney(r, 'given', lm && lm.closing)) load(); };
    $('#lm-take').onclick = async () => { if (await looseMoney(r, 'taken', lm && lm.closing)) load(); };
    if ($('#lm-stmt')) $('#lm-stmt').onclick = () => { location.hash = accountLink(lm.ledger_id, true); };
    $$('#view tr[data-doc]').forEach(row => {
      const href = docHref(row.dataset.kind, num(row.dataset.doc));
      if (href && !row.closest('tr.det')) row.onclick = () => { location.hash = href; };
    });
    $$('#view tr[data-m]').forEach(tr => { tr.onclick = () => openMonth(tr); });
    // The month being worked is open to begin with.
    const now = d.months.findIndex(x => x.month === today().slice(0, 7));
    const first = $('#view tr[data-m="' + (now >= 0 ? now : d.months.length - 1) + '"]');
    if (first) openMonth(first);
  }

  function openMonth(tr) {
    const i = num(tr.dataset.m), x = d.months[i];
    const det = $('#view tr.det[data-for="' + i + '"]');
    const opening = det.style.display === 'none';
    const cell = det.firstElementChild;
    if (opening && !cell.firstElementChild) {
      // Latest first, as every month-wise list is (23 Sep 2026).
      const es = d.entries.filter(e => e.date.slice(0, 7) === x.month).reverse();
      cell.innerHTML = es.length ? `<table style="margin:0"><tbody>
        <tr><th style="width:104px;padding-left:26px">Date</th><th>Particulars</th>
          <th style="width:110px">Entry</th><th class="r" style="width:130px">Paid to them</th>
          <th class="r" style="width:130px">Booked / back</th></tr>
        ${es.map(e => `<tr class="${docHref(e.doc_kind, e.doc_id) ? 'clickable' : ''}"
            data-doc="${e.doc_id}" data-kind="${e.doc_kind}">
          <td class="nw" style="padding-left:26px">${dt(e.date)}</td>
          <td>${e.other ? '<b>' + goLink(accountLink(e.other_id), esc(e.other), 'Open this account') + '</b>' : ''}
            ${e.narration ? '<div style="font-size:12px;color:var(--muted)">' + esc(e.narration) + '</div>' : ''}</td>
          <td class="nw" style="color:var(--muted)">${esc(STAFF_KIND[e.doc_kind] || e.doc_kind)} ${esc(e.doc_no)}${e.source === 'SHREE'
            ? ' <span class="tag">SHREE</span>' : ''}</td>
          <td class="r num" style="color:${e.debit ? 'var(--bad)' : 'inherit'}">${e.debit ? fmt(e.debit) : ''}</td>
          <td class="r num">${e.credit ? fmt(e.credit) : ''}</td></tr>`).join('')}</tbody></table>`
        : '<div class="empty" style="padding:14px">No entries in ' + esc(monthWord(x.month)) + '.</div>';
      $$('tr[data-doc]', cell).forEach(row => {
        const href = docHref(row.dataset.kind, num(row.dataset.doc));
        if (href) row.onclick = ev => { ev.stopPropagation(); location.hash = href; };
      });
    }
    det.style.display = opening ? '' : 'none';
    const caret = $('.caret', tr);
    if (caret) caret.classList.toggle('open', opening);
  }

  await load();
});

/* ================================================= personal accounts ---
   Asked for on 30 Sep 2026: *a new page named "Personal Accounts" where I
   will be managing my personal accounts which aren't related to the
   business -- the money given or received from different people.*

   Nothing here belongs to a firm. The firm box at the top makes no
   difference, nothing is posted to an account or counted in a trial
   balance, and SHREE's update never touches it. A person, and each sum
   given to them or received from them: given adds to what they owe you,
   received to what you owe them, and where they stand is the one less the
   other. What some owe you and what you owe others are two facts, and the
   page never nets them into one figure -- as an overdraft is never netted
   against cash. */

const PERSONAL_WAY = {
  GIVEN: { seg: '↑ Money given', title: 'Money given', to: 'Given to',
    say: 'Money you gave them — a loan, or paying back what you owed them. It adds to what they owe you.' },
  RECEIVED: { seg: '↓ Money received', title: 'Money received', to: 'Received from',
    say: 'Money they gave you — paying you back, or lending to you. It adds to what you owe them.' }
};
const PERSONAL_MODES = ['Cash', 'UPI', 'Bank', 'Cheque', 'Other'];

/** Where somebody stands, for a table cell: owes you, you owe, or settled.
 *  `entries` of 0 -- nothing has passed between you -- is a plain dash.
 *  `stack` puts the words under the figure, for a list with two such
 *  columns side by side: beside it, the list of people outgrew a 1366
 *  screen once a year was chosen. */
function personalBal(v, entries, stack) {
  v = num(v);
  if (Math.abs(v) < 0.005) {
    return '<span style="color:var(--muted)">' + (entries === 0 ? '—' : 'settled') + '</span>';
  }
  const tag = `<span class="tag ${v > 0 ? 'ok' : 'warn'}"
    title="${v > 0 ? 'Given them more than received: theirs to pay you'
      : 'Received more than given: yours to pay them'}">${v > 0 ? 'owes you' : 'you owe'}</span>`;
  return stack ? `<span class="nw">${fmt(Math.abs(v))}</span><div style="margin-top:3px">${tag}</div>`
    : `<span class="nw">${fmt(Math.abs(v))} ${tag}</span>`;
}
/** The same, in words: "they owe you ₹5,000.00". */
function personalSays(v) {
  v = num(v);
  if (Math.abs(v) < 0.005) return 'you are square';
  return v > 0 ? 'they owe you ' + money(v) : 'you owe them ' + money(-v);
}
const personalSigned = e => (e.direction === 'GIVEN' ? 1 : -1) * num(e.amount);

/** Everybody on Personal Accounts, to pick one from. A name nobody has is
 *  offered at the foot of the list, to be put on it as the entry is saved. */
async function choosePerson(opts) {
  opts = opts || {};
  let all = [];
  try { all = await api.get('/api/personal/people'); }
  catch (e) { bad(e.message); return null; }
  const body = `
    <input id="pp-q" autocomplete="off" placeholder="Type a name — somebody new is added as you save">
    <div class="tbl-scroll pick-table" id="pp-list"
      style="max-height:52vh;overflow-y:auto;margin-top:12px;
             border:1px solid var(--line);border-radius:8px"></div>
    <p id="pp-note" style="margin:9px 0 0;font-size:11.5px;color:var(--muted)"></p>`;
  return modal(opts.title || 'Choose a person', body, [{ label: 'Cancel', value: null }], {
    wide: true,
    onOpen: (bg, close) => {
      const q = $('#pp-q', bg), host = $('#pp-list', bg), note = $('#pp-note', bg);
      let rows = [], hi = 0;
      const paintHi = () => $$('tbody tr', host).forEach((tr, n) => tr.classList.toggle('hi', n === hi));
      function draw() {
        const t = q.value.replace(/\s+/g, ' ').trim();
        rows = t ? searchList(t, all, p => [p.name, p.mobile], 100000) : all.slice();
        if (t && !all.some(p => squash(p.name) === squash(t))) {
          rows.push({ id: null, name: t, fresh: true, balance: 0, entries: 0 });
        }
        if (hi > rows.length - 1) hi = Math.max(0, rows.length - 1);
        host.innerHTML = rows.length ? `<table><thead><tr><th>Name</th><th>Mobile</th>
            <th class="r">Where they stand</th></tr></thead><tbody>${rows.map((p, n) => p.fresh
          ? `<tr data-n="${n}"><td colspan="3"><b>＋ ${esc(p.name)}</b>
              <span class="tag info">new person</span></td></tr>`
          : `<tr data-n="${n}"><td><b>${esc(p.name)}</b></td>
              <td style="font-size:13px">${esc(p.mobile || '')}</td>
              <td class="r num">${personalBal(p.balance, p.entries)}</td></tr>`).join('')}</tbody></table>`
          : `<div class="empty"><b>Nobody here yet</b>Type their name — they are put on
              Personal Accounts as the entry is saved.</div>`;
        note.textContent = rows.length
          ? '↑ ↓ moves, Enter takes the highlighted row.' : '';
        paintHi();
        $$('tbody tr', host).forEach(tr => {
          tr.onmouseenter = () => { hi = num(tr.dataset.n); paintHi(); };
          tr.onclick = () => close(rows[num(tr.dataset.n)]);
        });
      }
      function move(step) {
        if (!rows.length) return;
        hi = Math.max(0, Math.min(rows.length - 1, hi + step));
        paintHi();
        const tr = $$('tbody tr', host)[hi];
        if (tr) KB.reveal(tr);
      }
      // Opened by typing, it carries on from the last letter; opened on a
      // name already there, the whole of it is selected to type over.
      q.value = pickTerm(opts);
      pickLast.term = q.value;
      q.focus();
      if (opts.typed) q.setSelectionRange(q.value.length, q.value.length);
      else q.select();
      q.oninput = () => { hi = 0; pickLast.term = q.value; draw(); };
      bg.addEventListener('keydown', e => {
        if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
        else if (e.key === 'PageDown') { e.preventDefault(); move(10); }
        else if (e.key === 'PageUp') { e.preventDefault(); move(-10); }
        else if (e.key === 'Enter' && rows[hi]) { e.preventDefault(); close(rows[hi]); }
      });
      draw();
    }
  });
}

/** Somebody put on the list by hand, or their name, number or notes
 *  changed. Answers the person saved, or null. */
async function personalPerson(p) {
  p = p || {};
  const html = `
    <div class="grid g2">
      <div><label>Name *</label><input name="name" value="${esc(p.name || '')}" autocomplete="off"></div>
      <div><label>Mobile</label><input name="mobile" value="${esc(p.mobile || '')}"></div>
    </div>
    <div style="margin-top:12px"><label>Notes <span class="opt">optional</span></label>
      <textarea name="notes" placeholder="Who they are, what was agreed…">${esc(p.notes || '')}</textarea></div>
    ${p.id ? '' : `<div class="hint">Nobody has to be added first: typing a new name
      on ↑ Money given or ↓ Money received puts them on the list as well.</div>`}`;
  let saved = null;
  await modal(p.id ? 'Edit — ' + p.name : 'Add a person', html, [
    { label: 'Cancel', value: null },
    { label: p.id ? 'Save' : 'Add', cls: 'primary', value: true, before: async bg => {
        const data = readForm(bg);
        if (!data.name.trim()) { bad('Give the name.'); $('[name=name]', bg).focus(); return false; }
        try {
          saved = await (p.id ? api.put('/api/personal/' + p.id, data)
            : api.post('/api/personal', data));
        } catch (e) { bad(e.message); return false; }
      } }
  ]);
  if (saved) ok(p.id ? 'Saved.' : saved.name + ' is on Personal Accounts.');
  return saved;
}

/** Money given or received, typed in -- or one entry opened to be changed
 *  or deleted. `o.person` {id, name} starts it on somebody, `o.direction` on
 *  one way or the other, `o.entry` opens that entry. "Save & add another"
 *  keeps the way, the date and how it was paid for the next one (and the
 *  person, when it was opened on somebody). Answers true once anything was
 *  saved or deleted. */
async function personalEntry(o) {
  o = o || {};
  let did = false, said = '';
  let next = { person: o.person || null, direction: o.direction || 'GIVEN',
               entry: o.entry || null, date: today(), mode: 'Cash' };
  for (;;) {
    const got = await personalEntryDialog(next, said);
    if (!got) break;
    did = true;
    if (!got.again) break;
    said = got.text;
    next = { person: o.person ? { id: got.saved.person_id, name: got.saved.name } : null,
             direction: got.saved.direction, entry: null,
             date: got.saved.entry_date, mode: got.saved.mode };
  }
  return did;
}

async function personalEntryDialog(o, said) {
  const e = o.entry;
  let way = e ? e.direction : o.direction;
  const who = o.person || (e ? { id: e.person_id, name: e.name } : null);
  let people = [];
  try { people = await api.get('/api/personal/people'); }
  catch (err) { /* the save says so if the books cannot be reached */ }
  const known = id => people.find(p => p.id === num(id));
  const byName = t => people.find(p => squash(p.name) === squash(t));
  const mode = e ? e.mode : o.mode || 'Cash';
  const modes = PERSONAL_MODES.indexOf(mode) >= 0 ? PERSONAL_MODES : [mode].concat(PERSONAL_MODES);
  const html = `
    ${said ? `<div class="v-saved">✓ ${esc(said)}. Now the next one.</div>` : ''}
    ${e && e.source ? `<p class="stocksay" style="margin-top:0">Brought in from ${esc(
      e.source.split('|').slice(1).join('|'))} in the ${esc(e.source.split('|')[0])} books${
      e.ref ? ' (' + esc(e.ref) + ')' : ''}. A change here stays until that account is brought
      in again, which puts back what the books say.</p>` : ''}
    <div class="seg bigseg" id="pe-ways" role="group" aria-label="Given or received">${Object.keys(PERSONAL_WAY).map(k =>
      `<button type="button" data-way="${k}" class="k-${k.toLowerCase()}">${PERSONAL_WAY[k].seg}</button>`).join('')}</div>
    <p class="stocksay" id="pe-say" style="margin:10px 0 12px"></p>
    <div class="pick"><label id="pe-who-lb">Given to *</label>
      <div class="pickbox"><input id="pe-who" autocomplete="off" value="${esc(who ? who.name : '')}"
          placeholder="Type a name, or choose one">
        <button type="button" data-browse class="pickbtn" tabindex="-1"
          title="Everybody on Personal Accounts (F4)">▾</button></div>
      <input type="hidden" name="person_id" value="${who && who.id ? who.id : ''}">
      <div class="hint" id="pe-info">&nbsp;</div></div>
    <div class="grid g3" style="margin-top:12px">
      <div><label>Amount (₹) *</label><input name="amount" class="num" placeholder="0.00"
        value="${e ? num(e.amount).toFixed(2) : ''}"></div>
      <div><label>Date</label><input type="date" name="entry_date"
        value="${esc(e ? e.entry_date : o.date || today())}"></div>
      <div><label>How</label><select name="mode">${modes.map(m =>
        `<option value="${esc(m)}"${m === mode ? ' selected' : ''}>${esc(m)}</option>`).join('')}</select></div>
    </div>
    <div style="margin-top:12px"><label>What for <span class="opt">optional</span></label>
      <input name="note" value="${esc(e ? e.note : '')}" maxlength="300"
        placeholder="e.g. hand loan, for the wedding, paid back part"></div>`;

  let saved = null;
  const save = async bg => {
    const data = readForm(bg);
    data.direction = way;
    const box = $('#pe-who', bg);
    const name = box.value.replace(/\s+/g, ' ').trim();
    if (!num(data.person_id)) {
      delete data.person_id;
      if (!name) {
        bad(way === 'GIVEN' ? 'Say who the money was given to.' : 'Say who the money came from.');
        box.focus();
        return false;
      }
      data.name = name;
    }
    data.amount = String(data.amount || '').replace(/[,\s₹]/g, '');
    if (!(num(data.amount) > 0)) {
      bad('Enter the amount.');
      $('[name=amount]', bg).focus();
      return false;
    }
    try {
      saved = await (e ? api.put('/api/personal/entries/' + e.id, data)
        : api.post('/api/personal/entries', data));
    } catch (err) { bad(err.message); return false; }
  };
  const buttons = [];
  if (e) {
    buttons.push({ label: 'Delete', cls: 'danger', value: 'deleted',
      before: async () => { if (!await bulkDelete('personal_entry', [e.id])) return false; } });
  }
  buttons.push({ label: 'Cancel', value: null });
  if (!e) buttons.push({ label: 'Save & add another', value: 'again', before: save });
  buttons.push({ label: 'Save', cls: 'primary', value: 'done', before: save });

  const answer = await modal(e ? 'Entry of ' + dt(e.entry_date) : PERSONAL_WAY[way].title,
    html, buttons, {
    onOpen: bg => {
      const box = $('#pe-who', bg), pid = $('[name=person_id]', bg);
      const amt = $('[name=amount]', bg);
      const head = $('h2', bg).firstChild;
      let picked = box.value;
      /** Where the person stands now -- this entry left out, when it is
       *  one being changed -- and where this entry leaves them. */
      function describe() {
        const n = num(String(amt.value).replace(/[,\s₹]/g, ''));
        const t = box.value.replace(/\s+/g, ' ').trim();
        const p = known(pid.value) || (t ? byName(t) : null);
        const out = $('#pe-info', bg);
        if (!p) {
          out.textContent = t ? t + ' is not on Personal Accounts yet — they are put on it as this is saved.'
            : ' ';
          return;
        }
        const before = num(p.balance) - (e && e.person_id === p.id ? personalSigned(e) : 0);
        const after = before + (way === 'GIVEN' ? n : -n);
        out.textContent = 'Now ' + personalSays(before)
          + (n > 0 ? ' · after this ' + personalSays(after) : '');
      }
      function paintWay() {
        $$('#pe-ways button', bg).forEach(b => b.classList.toggle('on', b.dataset.way === way));
        $('#pe-say', bg).textContent = PERSONAL_WAY[way].say;
        $('#pe-who-lb', bg).textContent = PERSONAL_WAY[way].to + ' *';
        if (!e && head) head.nodeValue = PERSONAL_WAY[way].title;
        describe();
      }
      $$('#pe-ways button', bg).forEach(b => {
        b.onclick = () => { way = b.dataset.way; paintWay(); };
      });
      const take = p => {
        box.value = picked = p.name;
        pid.value = p.id || '';
        describe();
      };
      // A name typed over the one picked no longer names that person.
      box.addEventListener('input', () => {
        if (box.value !== picked) pid.value = '';
        describe();
      });
      const browse = async how => {
        const p = await choosePerson({ term: () => box.value, typed: !!(how && how.typed),
          title: way === 'GIVEN' ? 'Given to whom?' : 'Received from whom?' });
        if (p) { take(p); moveOn(box, bg); } else box.focus();
        return p;
      };
      $('[data-browse]', bg).onclick = browse;
      opensPicker(box, browse, null);
      amt.addEventListener('input', describe);
      paintWay();
      // Somebody already named: straight to the amount. Nobody yet: the
      // person box, which puts everybody up to choose from.
      if (box.value.trim()) { amt.focus(); amt.select(); } else box.focus();
    }
  });
  if (answer === 'deleted') return { deleted: true };
  if (!answer || !saved) return null;
  const text = money(saved.amount) + (saved.direction === 'GIVEN' ? ' given to ' : ' received from ')
    + saved.name;
  ok(text + ' — ' + personalSays(saved.balance) + '.');
  return { saved, again: answer === 'again', text };
}

/* ------------------------------------ an account brought from the books ---
   30 Sep 2026: "Add an option to import a normal account to personal
   account." An account in the business books -- BSTP's PERSONAL ACCOUNT
   group, a family loan, anybody's -- copied onto somebody's page: every
   entry, the entries from a date with the balance before it as one line,
   or only the balance on a day. Read the way the Accounts screen reads it,
   so the figure is the one seen there; money that went to them comes in as
   given, money from them as received. The account stays in the books as it
   was. Bringing it in again replaces what was brought before, never adds to
   it -- that is how a page catches up after a SHREE update. */

/** An account in the business books, picked from the three firms: with
 *  nothing typed, the personal and loan accounts; typed, any of them. */
async function chooseBookAccount(opts) {
  opts = opts || {};
  const body = `
    <input id="ba-q" autocomplete="off" placeholder="Search every account in the books — name, city or group">
    <div class="tbl-scroll pick-table" id="ba-list"
      style="max-height:52vh;overflow-y:auto;margin-top:12px;
             border:1px solid var(--line);border-radius:8px"><div class="empty">Loading…</div></div>
    <p id="ba-note" style="margin:9px 0 0;font-size:11.5px;color:var(--muted)"></p>`;
  return modal(opts.title || 'Which account in the books?', body, [{ label: 'Cancel', value: null }], {
    wide: true,
    onOpen: (bg, close) => {
      const q = $('#ba-q', bg), host = $('#ba-list', bg), note = $('#ba-note', bg);
      let rows = [], hi = 0, seq = 0, timer = 0;
      const paintHi = () => $$('tbody tr', host).forEach((tr, n) => tr.classList.toggle('hi', n === hi));
      async function draw() {
        const mine = ++seq, t = q.value.trim();
        let got;
        try { got = await api.get('/api/personal/accounts', { search: t }); }
        catch (e) {
          if (mine === seq) host.innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
          return;
        }
        // A slow answer never draws over the one for a later letter.
        if (mine !== seq || !bg.isConnected) return;
        rows = got;
        if (hi > rows.length - 1) hi = Math.max(0, rows.length - 1);
        host.innerHTML = rows.length ? `<table><thead><tr><th>Account</th><th>Firm</th><th>Group</th>
            <th class="r">Where it stands</th><th class="r">Entries</th></tr></thead><tbody>${rows.map((r, n) =>
          `<tr data-n="${n}"><td><b>${esc(r.name)}</b>${r.personal
              ? ' <span class="tag ok">personal</span>' : ''}${r.brought_into
              ? ' <span class="tag info" title="Brought in before">on ' + esc(r.brought_into) + '’s page</span>' : ''}
              ${r.city ? '<div style="font-size:12px;color:var(--muted)">' + esc(r.city) + '</div>' : ''}</td>
            <td>${esc(r.firm)}</td><td style="font-size:13px">${esc(r.group_name)}</td>
            <td class="r num">${personalBal(r.balance, r.entries)}</td>
            <td class="r num">${r.entries || '—'}</td></tr>`).join('')}</tbody></table>`
          : `<div class="empty"><b>No account matches</b>Try another word.</div>`;
        note.textContent = !t ? 'The personal, capital and loan accounts. Type to search every account in the firms.'
          : rows.length >= 150 ? 'The first 150 — type a little more to narrow it.'
          : '↑ ↓ moves, Enter takes the highlighted account.';
        paintHi();
        $$('tbody tr', host).forEach(tr => {
          tr.onmouseenter = () => { hi = num(tr.dataset.n); paintHi(); };
          tr.onclick = () => close(rows[num(tr.dataset.n)]);
        });
      }
      function move(step) {
        if (!rows.length) return;
        hi = Math.max(0, Math.min(rows.length - 1, hi + step));
        paintHi();
        const tr = $$('tbody tr', host)[hi];
        if (tr) KB.reveal(tr);
      }
      q.value = pickTerm(opts);
      pickLast.term = q.value;
      q.focus();
      if (opts.typed) q.setSelectionRange(q.value.length, q.value.length);
      else q.select();
      q.oninput = () => {
        hi = 0;
        pickLast.term = q.value;
        clearTimeout(timer);
        timer = setTimeout(draw, 160);
      };
      bg.addEventListener('keydown', e => {
        if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
        else if (e.key === 'PageDown') { e.preventDefault(); move(10); }
        else if (e.key === 'PageUp') { e.preventDefault(); move(-10); }
        else if (e.key === 'Enter' && rows[hi]) { e.preventDefault(); close(rows[hi]); }
      });
      draw();
    }
  });
}

/** The Import account dialog. `o.account` {id, name, firm} starts it on an
 *  account (Bring in again), `o.person` {id, name} on somebody's page.
 *  Answers what was done -- {person, entries, replaced, balance} -- or null. */
async function personalImport(o) {
  o = o || {};
  let acct = o.account || null, mode = 'all', plan = null, done = null;
  const dates = { from: fyStart(), balance: today() };
  const MODE_SAY = {
    all: 'Every entry on the account, with its opening balance.',
    from: 'The entries from this date on; everything before it comes in as one opening line.',
    balance: 'One line: what the account stood at on this date.'
  };
  const body = `
    <p class="stocksay" style="margin-top:0">Copies an account from the business books onto
      Personal Accounts. The account itself stays in the business books as it is.</p>
    <div class="pick"><label>Account in the books *</label>
      <div class="pickbox"><input id="pi-acct" autocomplete="off" value="${esc(acct ? acct.name : '')}"
          placeholder="Type a name, or choose one">
        <button type="button" data-browse-acct class="pickbtn" tabindex="-1"
          title="Accounts in the books (F4)">▾</button></div>
      <div class="hint" id="pi-acct-info">&nbsp;</div></div>
    <div style="margin-top:12px"><label>Bring in</label>
      <div class="seg" id="pi-modes" role="group" aria-label="What to bring in">
        <button type="button" data-mode="all">Every entry</button><button type="button"
          data-mode="from">Entries from a date</button><button type="button"
          data-mode="balance">Only the balance</button></div>
      <div class="hint" id="pi-mode-say"></div></div>
    <div id="pi-date-box" style="margin-top:12px" hidden><label id="pi-date-lb">From</label>
      <input type="date" id="pi-date" style="width:200px"></div>
    <div class="pick" style="margin-top:12px"><label>Onto whose page *</label>
      <div class="pickbox"><input id="pi-who" autocomplete="off" value="${esc(o.person ? o.person.name : '')}"
          placeholder="Somebody on Personal Accounts, or a new name">
        <button type="button" data-browse-who class="pickbtn" tabindex="-1"
          title="Everybody on Personal Accounts (F4)">▾</button></div>
      <input type="hidden" id="pi-pid" value="${o.person && o.person.id ? o.person.id : ''}">
      <div class="hint" id="pi-who-info">&nbsp;</div></div>
    <div id="pi-plan" style="margin-top:14px"></div>`;

  await modal(o.account ? 'Bring in again — ' + o.account.name : 'Import an account from the books', body, [
    { label: 'Cancel', value: null },
    { label: 'Bring it in', cls: 'primary', value: true, before: async bg => {
        const acctBox = $('#pi-acct', bg), who = $('#pi-who', bg);
        if (!acct) { bad('Choose the account to bring in.'); acctBox.focus(); return false; }
        const data = { ledger_id: acct.id, mode, date: mode === 'all' ? '' : $('#pi-date', bg).value };
        const name = who.value.replace(/\s+/g, ' ').trim();
        if (num($('#pi-pid', bg).value)) data.person_id = num($('#pi-pid', bg).value);
        else if (name) data.name = name;
        else { bad('Say whose page it goes on.'); who.focus(); return false; }
        try { done = await api.post('/api/personal/import', data); }
        catch (e) { bad(e.message); return false; }
      } }
  ], {
    nothingToSave: true,
    onOpen: bg => {
      const acctBox = $('#pi-acct', bg), who = $('#pi-who', bg), pid = $('#pi-pid', bg);
      const dateBox = $('#pi-date', bg), go = $('.ft button.primary', bg);
      let pickedAcct = acctBox.value, pickedWho = who.value, whoTouched = !!o.person, seq = 0, timer = 0;

      function paintModes() {
        $$('#pi-modes button', bg).forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
        $('#pi-mode-say', bg).textContent = MODE_SAY[mode];
        $('#pi-date-box', bg).hidden = mode === 'all';
        $('#pi-date-lb', bg).textContent = mode === 'balance' ? 'Balance on' : 'From';
        if (mode !== 'all') dateBox.value = dates[mode];
      }
      function whoSays() {
        const out = $('#pi-who-info', bg), t = who.value.trim();
        out.textContent = num(pid.value) ? 'Goes onto ' + t + '’s page, beside anything already there.'
          : t ? t + ' is not on Personal Accounts yet — they are put on it.' : ' ';
      }
      /** What it would bring, asked again whenever a choice changes. */
      async function preview() {
        const mine = ++seq, box = $('#pi-plan', bg);
        if (!acct || (mode !== 'all' && !dateBox.value)) {
          box.innerHTML = '';
          go.textContent = 'Bring it in';
          go.disabled = false;              // Save says what is missing
          return;
        }
        box.innerHTML = '<div class="hint">Reading the account…</div>';
        let p;
        try {
          p = await api.get('/api/personal/import', { ledger_id: acct.id, mode,
            date: mode === 'all' ? '' : dateBox.value });
        } catch (e) {
          if (mine === seq) box.innerHTML = '<div class="hint" style="color:var(--bad)">' + esc(e.message) + '</div>';
          return;
        }
        if (mine !== seq || !bg.isConnected) return;
        plan = p;
        $('#pi-acct-info', bg).textContent = [p.account.firm, p.account.group_name].join(' · ');
        // Whose page, unless already said: where it went last time, else
        // somebody of the same name, else a new person named as the account.
        if (!whoTouched) {
          const to = p.brought_before.length === 1 ? p.brought_before[0] : p.person;
          who.value = pickedWho = to.name;
          pid.value = to.id || '';
          whoSays();
        }
        const lines = p.latest.map(x => `<tr><td class="nw">${dt(x.entry_date)}</td>
            <td>${esc(x.note || '')}${x.ref ? '<div style="font-size:12px;color:var(--muted)">' + esc(x.ref) + '</div>' : ''}</td>
            <td class="r num">${x.direction === 'GIVEN' ? fmt(x.amount) : ''}</td>
            <td class="r num">${x.direction === 'RECEIVED' ? fmt(x.amount) : ''}</td></tr>`).join('');
        box.innerHTML = p.count ? `
          <div class="hint" style="font-size:14px;color:var(--ink)"><b>${counted(p.count, 'entry', 'entries')}</b>${
            p.first_date ? ' (' + dt(p.first_date) + (p.last_date !== p.first_date ? ' to ' + dt(p.last_date) : '') + ')' : ''}:
            given ${money(p.given)}, received ${money(p.received)} — <b>${esc(personalSays(p.balance))}</b>.</div>
          ${p.brought_before.length ? `<div class="hint" style="color:var(--warn)">Brought in before, onto ${
            p.brought_before.map(b => esc(b.name) + ' (' + counted(b.n, 'entry', 'entries') + ')').join(', ')}.
            Those are replaced, so nothing is counted twice.</div>` : ''}
          <div class="tbl-scroll" style="margin-top:8px;border:1px solid var(--line);border-radius:8px">
            <table><thead><tr><th style="width:110px">Date</th><th>What for</th>
              <th class="r" style="width:120px">Given</th><th class="r" style="width:120px">Received</th></tr></thead>
            <tbody>${lines}</tbody></table></div>
          ${p.count > p.latest.length ? '<div class="hint">The latest ' + p.latest.length + ' of them.</div>' : ''}`
          : `<div class="hint" style="color:var(--bad)">Nothing to bring in: the account has no entries and no balance${
              mode === 'balance' ? ' on that date' : mode === 'from' ? ' from that date' : ''}.</div>`;
        go.textContent = p.count ? 'Bring in ' + counted(p.count, 'entry', 'entries') : 'Bring it in';
        go.disabled = !p.count;
      }
      const later = () => { clearTimeout(timer); timer = setTimeout(preview, 250); };

      $$('#pi-modes button', bg).forEach(b => {
        b.onclick = () => { mode = b.dataset.mode; paintModes(); preview(); };
      });
      dateBox.addEventListener('change', () => { dates[mode] = dateBox.value; later(); });

      const takeAcct = a => {
        acct = a;
        acctBox.value = pickedAcct = a.name;
        preview();
      };
      acctBox.addEventListener('input', () => {
        if (acctBox.value !== pickedAcct) {
          acct = null;
          $('#pi-acct-info', bg).innerHTML = '&nbsp;';
          preview();
        }
      });
      const browseAcct = async how => {
        const a = await chooseBookAccount({ term: () => acctBox.value, typed: !!(how && how.typed) });
        if (a) { takeAcct(a); moveOn(acctBox, bg); } else acctBox.focus();
        return a;
      };
      $('[data-browse-acct]', bg).onclick = browseAcct;
      opensPicker(acctBox, browseAcct, null);

      const takeWho = p => {
        whoTouched = true;
        who.value = pickedWho = p.name;
        pid.value = p.id || '';
        whoSays();
      };
      who.addEventListener('input', () => {
        whoTouched = true;
        if (who.value !== pickedWho) pid.value = '';
        whoSays();
      });
      const browseWho = async how => {
        const p = await choosePerson({ term: () => who.value, typed: !!(how && how.typed),
          title: 'Onto whose page?' });
        if (p) { takeWho(p); moveOn(who, bg); } else who.focus();
        return p;
      };
      $('[data-browse-who]', bg).onclick = browseWho;
      opensPicker(who, browseWho, null);

      paintModes();
      whoSays();
      preview();
      if (!acct) acctBox.focus();
    }
  });
  if (done) {
    ok('Brought in ' + counted(done.entries, 'entry', 'entries') + ' from ' + done.account.name
      + ' onto ' + done.person.name + '’s page — ' + personalSays(done.balance) + '.'
      + (done.replaced ? ' The ' + done.replaced + ' brought in before were replaced.' : ''));
  }
  return done;
}

/** What the Year box and dates say, as words for a heading. */
function personalPeriod() {
  const s = $('#fyq'), f = $('#f1').value, t = $('#f2').value;
  if (s && s.value) return s.options[s.selectedIndex].text;
  return (f ? dt(f) : 'the start') + ' to ' + (t ? dt(t) : 'now');
}

route(/^\/personal$/, async (view, _m, q) => {
  const SHOW = ['all', 'get', 'give', 'settled'];
  let last = null, show = SHOW.indexOf(q.show) >= 0 ? q.show : 'all';

  setTitle('Personal Accounts',
    'Your own money given to and received from people — kept apart from the business books', [
      { label: '↑ Money given', cls: 'primary',
        onClick: async () => { if (await personalEntry({ direction: 'GIVEN' })) load(); } },
      { label: '↓ Money received',
        onClick: async () => { if (await personalEntry({ direction: 'RECEIVED' })) load(); } },
      { label: '＋ Add person', onClick: async () => { if (await personalPerson()) load(); } },
      // An account in the business books copied across (30 Sep 2026); the
      // page it went onto is opened, to show what came in.
      { label: '⇩ Import account', onClick: async () => {
          const done = await personalImport();
          if (done) location.hash = '#/personal/' + done.person.id + carried();
        } },
      { label: '🖨 Print', onClick: () => window.print() }
    ]);

  view.innerHTML = `<div class="card noprint"><div class="bd"><div class="row">
      ${fySelect('fyq', { all: true })}
      <div><label>From</label><input type="date" id="f1"></div>
      <div><label>To</label><input type="date" id="f2"></div>
      <div style="flex:1;min-width:200px"><label>Search</label>
        <input id="q" autocomplete="off" placeholder="Name, mobile or notes…"></div>
    </div></div></div><div id="out"></div>`;

  // Everything, every year, until a year is chosen: money lent to family
  // runs over years, and what is owed today is the first thing looked for.
  const mem = screenState('/personal', {
    search: '#q',
    show: { get: () => (show === 'all' ? '' : show),
            set: v => { if (SHOW.indexOf(v) >= 0) show = v; } },
    from: '#f1', to: '#f2',
    fy: fyField('f1', 'f2')
  }).restore(q);

  const sideOf = r => (r.balance > 0.005 ? 'get' : r.balance < -0.005 ? 'give' : 'settled');
  /** The dates the list is on, carried to a person's page. */
  function carried() {
    const p = new URLSearchParams();
    const fy = fyField('f1', 'f2').get();
    if (fy) p.set('fy', fy);
    else {
      if ($('#f1').value) p.set('from', $('#f1').value);
      if ($('#f2').value) p.set('to', $('#f2').value);
    }
    const s = p.toString();
    return s ? '?' + s : '';
  }

  function paint() {
    const d = last, t = d.totals;
    if (!d.count) {
      $('#out').innerHTML = `<div class="card"><div class="empty"><b>Nobody here yet</b>
        Press <i>↑ Money given</i> or <i>↓ Money received</i> and type the person’s
        name — they are put on this page as you save — or <i>⇩ Import account</i> to
        copy an account across from the business books. Everything here is your own:
        it never changes the business books, a firm or SHREE.</div></div>`;
      return;
    }
    const term = $('#q').value.trim();
    const n = k => d.rows.filter(r => k === 'all' || sideOf(r) === k).length;
    const rows = d.rows.filter(r => show === 'all' || sideOf(r) === show)
      .filter(r => !term || searchRank(term, [r.name, r.mobile, r.notes]) < 3);
    const opening = !!d.start;
    const period = personalPeriod();
    const asOn = d.end && d.end !== today() ? ' · on ' + dt(d.end) : '';
    const go = carried();
    const sum = k => rows.reduce((a, r) => a + num(r[k]), 0);
    const chip = (k, word) => `<button type="button" data-show="${k}"${show === k ? ' class="on"' : ''}>${word} · ${n(k)}</button>`;
    $('#out').innerHTML = `
      <h1 class="pa-print">Personal Accounts <small>${esc(period)}</small></h1>
      <div class="kpis">
        <div class="kpi good"><div class="lb">Owed to you</div>
          <div class="vl">${money(t.to_get)}</div>
          <div class="ft">${t.to_get_count ? 'by ' + counted(t.to_get_count, 'person', 'people')
            : 'nobody owes you'}${asOn}</div></div>
        <div class="kpi bad"><div class="lb">You owe</div>
          <div class="vl">${money(t.to_give)}</div>
          <div class="ft">${t.to_give_count ? 'to ' + counted(t.to_give_count, 'person', 'people')
            : 'you owe nobody'}${asOn}</div></div>
        <div class="kpi"><div class="lb">Given · ${esc(period)}</div>
          <div class="vl">${money(t.given)}</div><div class="ft">money that went out to people</div></div>
        <div class="kpi"><div class="lb">Received · ${esc(period)}</div>
          <div class="vl">${money(t.received)}</div><div class="ft">money that came in from people</div></div>
      </div>
      <div class="seg stockchips noprint" role="group" aria-label="Who to show" style="margin:0 0 10px">
        ${chip('all', 'Everybody')}${chip('get', 'Owe you')}${chip('give', 'You owe')}${chip('settled', 'Settled')}</div>
      <div class="card"><h2>People<span class="rt"
          style="font-weight:400;font-size:12px;color:var(--muted)">click a name for everything
          given and received</span></h2>
        <div class="bd flush tbl-scroll" id="pa-list">${rows.length ? `<table><thead><tr>
          <th style="min-width:200px">Name</th>
          ${opening ? '<th class="r" title="Where they stood before ' + esc(dt(d.start)) + '">Opening</th>' : ''}
          <th class="r">Given</th><th class="r">Received</th>
          <th class="r" title="Given less received: what they owe you, or you owe them">Balance</th>
          <th class="noprint" style="width:1%"></th></tr></thead>
          <tbody>${rows.map(r => `<tr class="clickable" data-id="${r.id}" data-href="#/personal/${r.id}${esc(go)}">
            <td><b>${goLink('#/personal/' + r.id + go, esc(r.name), 'Everything given and received')}</b>
              <div style="font-size:12px;color:var(--muted)">${esc([r.mobile,
                r.last_date ? 'last entry ' + dt(r.last_date) : 'no entries yet',
                r.imported ? 'from the books' : '', r.notes]
                .filter(Boolean).join(' · '))}</div></td>
            ${opening ? '<td class="r num">' + personalBal(r.opening, 0, true) + '</td>' : ''}
            <td class="r num">${r.given ? fmt(r.given) : '—'}</td>
            <td class="r num">${r.received ? fmt(r.received) : '—'}</td>
            <td class="r num"><b>${personalBal(r.balance, r.all_entries, true)}</b></td>
            <td class="r rowacts noprint"><button class="sm" data-way="GIVEN"
                title="Money given to ${esc(r.name)}">↑ Given</button><button class="sm" data-way="RECEIVED"
                title="Money received from ${esc(r.name)}">↓ Received</button><button class="sm danger" data-del
                title="Take ${esc(r.name)} off, with every entry">Delete</button></td></tr>`).join('')}</tbody>
          <tfoot><tr><td>${counted(rows.length, 'person', 'people')}</td>${opening ? '<td></td>' : ''}
            <td class="r num">${fmt(sum('given'))}</td><td class="r num">${fmt(sum('received'))}</td>
            <td></td><td class="noprint"></td></tr></tfoot></table>`
          : `<div class="empty"><b>Nobody matches</b>${term ? 'Try another word' : 'Nobody'}${
              show !== 'all' ? ', or show everybody' : ''}.</div>`}</div></div>`;
    $$('#out [data-show]').forEach(b => {
      b.onclick = () => { show = b.dataset.show; mem.save(); paint(); };
    });
    $$('#pa-list tr[data-id]').forEach(tr => {
      const r = d.rows.find(x => x.id === num(tr.dataset.id));
      $$('[data-way]', tr).forEach(b => {
        b.onclick = async ev => {
          ev.stopPropagation();
          if (await personalEntry({ person: { id: r.id, name: r.name }, direction: b.dataset.way })) load();
        };
      });
      $('[data-del]', tr).onclick = async ev => {
        ev.stopPropagation();
        if (await bulkDelete('personal', [r.id])) load();
      };
    });
    if (rows.length) bulkRows($('#pa-list'), 'personal', { reload: load });
  }

  async function load() {
    mem.save();
    if (!last) $('#out').innerHTML = '<div class="empty">Loading…</div>';
    const here = onScreen();
    let got;
    try { got = await api.get('/api/personal', { from: $('#f1').value, to: $('#f2').value }); }
    catch (e) {
      if (!here()) return;
      $('#out').innerHTML = '<div class="card"><div class="empty">' + esc(e.message) + '</div></div>';
      return;
    }
    if (!here()) return;
    last = got;
    paint();
  }

  bindFy(load, 'f1', 'f2');
  $('#f1').onchange = load;
  $('#f2').onchange = load;
  // The list is short: it narrows as each letter is typed, with no trip
  // to the books.
  $('#q').oninput = () => { mem.save(); if (last) paint(); };
  await load();
});

route(/^\/personal\/(\d+)$/, async (view, m, q) => {
  const pid = num(m[0]);
  let d = null;
  setTitle('Personal account', 'Personal Accounts');

  view.innerHTML = `<div class="card noprint"><div class="bd"><div class="row">
      ${fySelect('fyq', { all: true })}
      <div><label>From</label><input type="date" id="f1"></div>
      <div><label>To</label><input type="date" id="f2"></div>
      <div class="spacer"></div>
      <div id="pa-who" style="font-size:13px;color:var(--muted);align-self:center;max-width:50%"></div>
    </div></div></div><div id="out"></div>`;
  const mem = screenState('/personal/' + pid, {
    from: '#f1', to: '#f2', fy: fyField('f1', 'f2')
  }).restore(q);

  const edit = async entry => {
    if (await personalEntry({ entry, person: { id: pid, name: d.person.name } })) load();
  };
  const add = async way => {
    if (await personalEntry({ person: { id: pid, name: d.person.name }, direction: way })) load();
  };

  function paint() {
    const p = d.person, b = d.balance;
    const period = personalPeriod();
    setTitle(p.name, 'Personal account' + (p.mobile ? ' · ' + p.mobile : ''), [
      { label: '↑ Money given', cls: 'primary', onClick: () => add('GIVEN') },
      { label: '↓ Money received', onClick: () => add('RECEIVED') },
      { label: '✎ Edit', onClick: async () => { if (await personalPerson(p)) load(); } },
      { label: '🗑 Delete', onClick: async () => {
          if (await bulkDelete('personal', [pid])) leave('#/personal');
        } },
      { label: '🖨 Print', onClick: () => window.print() }
    ]);
    $('#pa-who').textContent = [p.mobile ? '📞 ' + p.mobile : '', p.notes].filter(Boolean).join(' · ');
    const list = d.entries.slice().reverse();          // latest first
    // Accounts in the business books this page was brought in from, each
    // with the way to bring it in again once the books have moved on.
    const src = d.sources || [];
    const froms = src.length ? `<div class="card noprint"><div class="bd" style="padding:12px 16px">
        ${src.map(s => `<div class="row" style="align-items:center;gap:10px">
          <div style="flex:1;min-width:240px">Brought in from ${s.ledger_id
            ? goLink(accountLink(s.ledger_id), '<b>' + esc(s.name) + '</b>', 'Open the account in the business books')
            : '<b>' + esc(s.name) + '</b> <span class="tag warn">no longer in the books</span>'}
            <span style="color:var(--muted)">· ${esc(s.firm)} · ${counted(s.entries, 'entry', 'entries')}</span></div>
          ${s.ledger_id ? `<button class="sm" data-again="${s.ledger_id}" data-firm="${esc(s.firm)}"
            data-name="${esc(s.name)}" title="Replace these entries with the account as it stands now">↻ Bring in again</button>` : ''}
        </div>`).join('')}
        <div class="hint" style="margin-top:6px">Bringing it in again replaces what came from
          the account with the account as it stands now — after a SHREE update, say.
          Entries typed here stay as they are.</div></div></div>` : '';
    $('#out').innerHTML = `
      <h1 class="pa-print">${esc(p.name)} <small>${esc(period)}${p.mobile ? ' · ' + esc(p.mobile) : ''}</small></h1>
      ${froms}
      <div class="kpis">
        <div class="kpi ${b > 0.005 ? 'good' : b < -0.005 ? 'bad' : 'brand'}">
          <div class="lb">${b > 0.005 ? 'They owe you' : b < -0.005 ? 'You owe them' : 'Where you stand'}</div>
          <div class="vl">${Math.abs(b) < 0.005 ? (d.all_entries ? 'Settled' : 'Nothing yet') : money(Math.abs(b))}</div>
          <div class="ft">${d.all_entries ? 'after all ' + counted(d.all_entries, 'entry', 'entries')
            + (d.first_date ? ' since ' + dt(d.first_date) : '') : 'nothing given or received yet'}</div></div>
        <div class="kpi"><div class="lb">Given · ${esc(period)}</div>
          <div class="vl">${money(d.given)}</div><div class="ft">to them</div></div>
        <div class="kpi"><div class="lb">Received · ${esc(period)}</div>
          <div class="vl">${money(d.received)}</div><div class="ft">from them</div></div>
      </div>
      <div class="card"><h2>Entries<span class="rt" style="font-weight:400;font-size:12px;color:var(--muted)">
          latest first · click an entry to change it</span></h2>
        <div class="bd flush tbl-scroll" id="pe-list">${list.length || d.start ? `<table><thead><tr>
          <th style="width:110px">Date</th><th>What for</th><th style="width:90px">How</th>
          <th class="r" style="width:140px">Given</th><th class="r" style="width:140px">Received</th>
          <th class="r" style="width:210px">Balance</th><th class="noprint" style="width:1%"></th></tr></thead>
          <tbody>${list.map(e => `<tr class="clickable" data-id="${e.id}" title="Change this entry">
            <td class="nw">${dt(e.entry_date)}</td>
            <td>${e.note ? esc(e.note) : '<span style="color:var(--muted)">—</span>'}${e.source
              ? '<div style="font-size:12px;color:var(--muted)">' + goLink(docLink(e.doc_kind, e.doc_id, e.company_id),
                esc([e.firm, e.ref].filter(Boolean).join(' · ') || 'from the books'),
                'Open it in the business books') + '</div>' : ''}</td>
            <td>${esc(e.mode || '')}</td>
            <td class="r num">${e.direction === 'GIVEN' ? fmt(e.amount) : ''}</td>
            <td class="r num">${e.direction === 'RECEIVED' ? fmt(e.amount) : ''}</td>
            <td class="r num nw">${personalBal(e.balance)}</td>
            <td class="r rowacts noprint"><button class="sm" data-edit>Edit</button><button
              class="sm danger" data-del>Delete</button></td></tr>`).join('')}
            ${d.start ? `<tr><td class="nw">${dt(d.start)}</td><td colspan="4"><i>Opening balance</i></td>
              <td class="r num nw">${personalBal(d.opening, 0)}</td><td class="noprint"></td></tr>` : ''}</tbody>
          <tfoot><tr><td colspan="3">${esc(period)}</td>
            <td class="r num">${fmt(d.given)}</td><td class="r num">${fmt(d.received)}</td>
            <td class="r num nw">${personalBal(d.closing, d.all_entries)}</td><td class="noprint"></td></tr></tfoot>
          </table>` : `<div class="empty"><b>Nothing given or received${d.all_entries ? ' in these dates' : ' yet'}</b>
            Use <i>↑ Money given</i> or <i>↓ Money received</i> at the top.</div>`}</div>
        ${d.later ? `<div class="bd stockfoot">${counted(d.later, 'later entry', 'later entries')}
          after ${esc(dt(d.end))} not shown — choose <i>All years</i> to see them.</div>` : ''}</div>`;
    $$('#pe-list tr[data-id]').forEach(tr => {
      const e = d.entries.find(x => x.id === num(tr.dataset.id));
      tr.onclick = () => edit(e);
      $('[data-edit]', tr).onclick = ev => { ev.stopPropagation(); edit(e); };
      $('[data-del]', tr).onclick = async ev => {
        ev.stopPropagation();
        if (await bulkDelete('personal_entry', [e.id])) load();
      };
    });
    if (list.length) bulkRows($('#pe-list'), 'personal_entry', { reload: load });
    $$('#out [data-again]').forEach(b => {
      b.onclick = async () => {
        const done = await personalImport({
          account: { id: num(b.dataset.again), name: b.dataset.name, firm: b.dataset.firm },
          person: { id: pid, name: p.name } });
        if (!done) return;
        // Brought onto somebody else's page instead: that page is where it is.
        if (done.person.id !== pid) location.hash = '#/personal/' + done.person.id;
        else load();
      };
    });
  }

  async function load() {
    mem.save();
    const here = onScreen();
    let got;
    try { got = await api.get('/api/personal/' + pid, { from: $('#f1').value, to: $('#f2').value }); }
    catch (e) {
      if (!here()) return;
      setTitle('Personal account', 'Personal Accounts');
      $('#out').innerHTML = `<div class="card"><div class="empty"><b>${esc(e.message)}</b>
        <a class="btn" href="#/personal">Personal Accounts</a></div></div>`;
      return;
    }
    if (!here()) return;
    d = got;
    paint();
  }

  bindFy(load, 'f1', 'f2');
  $('#f1').onchange = load;
  $('#f2').onchange = load;
  await load();
});

/* ====================================== outstanding, groupwise vertical ---
   **The screen the shop actually reads**, and the one the user pointed a
   camera at: SHREE's *Trial Balance (Groupwise (Vertical))*. Two columns
   side by side -- **Liabilities** on the left, **Assets** on the right --
   each grouped under its account group, each group footed with
   `Sum of: <group>`, each side footed with how many lines it holds and what
   they come to.

   The same group shows on both sides, and that is the whole point of the
   layout: a customer in credit is an advance being held and belongs among
   the liabilities, while the rest of Accounts Receivable is money owed to
   us. What decides the side is the sign of the balance, not the kind of
   account.

   `Difference in Opening Balances` is kept out of both columns and printed
   at the foot the way SHREE prints it. It carries the years that were never
   backed up; left in the list it reads as the largest creditor in the book,
   which it is not.                                                        */

route(/^\/reports\/outstanding$/, async (view, _m, q) => {
  setTitle('Outstanding', 'Who owes us and what we owe — groupwise, or as a trial balance');

  view.innerHTML = `<div class="card"><div class="bd"><div class="row">
      ${fySelect('fyq', { asOn: true })}
      <div><label>As on</label><input type="date" id="f2"
        value="${esc(q.to || today())}"></div>
      <div><label>Layout</label>
        <select id="layout" title="The two ways the old software lays this out">
          <option value="grp"${q.layout === 'tb' ? '' : ' selected'}>Groupwise (two columns)</option>
          <option value="tb"${q.layout === 'tb' ? ' selected' : ''}>Trial balance (Dr / Cr)</option>
        </select></div>
      <div style="flex:1;min-width:200px"><label>Search</label>
        <input id="q" placeholder="Ledger name…" value="${esc(q.search || '')}"></div>
      <div class="acts">
        <button id="go" class="primary">Apply</button>
        <button id="csv">⤓ CSV</button>
      </div>
    </div></div></div><div id="out"></div>`;

  /* One side of the sheet. The group heading opens and closes, because a
     hundred and fifty receivables under one heading is a page of scrolling
     when all that was wanted is the sum. */
  function column(side, title, cls) {
    return `<div class="card osheet"><h2>${esc(title)}<span class="rt"
        style="font-weight:400;font-size:13px;color:var(--muted)">
        ${side.count} line${side.count === 1 ? '' : 's'}</span></h2>
      <div class="bd flush tbl-scroll">${side.groups.length ? `
      <table><thead><tr><th>Group Name / Ledger / Account Name</th>
        <th class="r" style="width:180px">${esc(title)}</th></tr></thead>
      <tbody>${side.groups.map((g, i) => {
        const key = cls + ':' + g.name, isShut = shut.has(key);
        return `
        <tr class="ogrp clickable" data-g="${cls}${i}" data-key="${esc(key)}">
          <td><span class="caret${isShut ? '' : ' open'}">▸</span> <b>${esc(g.name)}</b></td>
          <td class="r num">&nbsp;</td></tr>
        ${g.rows.map(r => `<tr class="orow" data-for="${cls}${i}"${isShut ? ' style="display:none"' : ''}
            data-href="${esc(accountLink(r.id, true))}">
          <td style="padding-left:34px">» ${goLink(accountLink(r.id, true), esc(r.name))}</td>
          <td class="r num">${fmt(r.amount)}</td></tr>`).join('')}
        <tr class="osum" data-for="${cls}${i}">
          <td><i>Sum of: ${esc(g.name)}</i></td>
          <td class="r num"><b>${fmt(g.sum)}</b></td></tr>`;
      }).join('')}
      </tbody><tfoot><tr><td>${side.count} =&gt;</td>
        <td class="r num"><b>${fmt(side.total)}</b></td></tr></tfoot></table>`
      : '<div class="empty">Nothing on this side.</div>'}</div></div>`;
  }

  /* The same ledgers, read down a page instead of across two columns: the
     conventional trial balance, group by group. */
  function trialSheet(d) {
    let last = '';
    return `<div class="kpis">
        <div class="kpi"><div class="lb">Total debit</div>
          <div class="vl">${money(d.total_debit)}</div></div>
        <div class="kpi"><div class="lb">Total credit</div>
          <div class="vl">${money(d.total_credit)}</div></div>
        <div class="kpi ${Math.abs(d.difference) < 0.01 ? 'good' : 'bad'}">
          <div class="lb">Difference</div><div class="vl">${fmt(d.difference)}</div>
          <div class="ft">${Math.abs(d.difference) < 0.01 ? 'books balance'
            : 'the two sides do not agree'}</div></div>
      </div>
      <div class="card"><div class="bd flush tbl-scroll">${d.rows.length ? `
        <table><thead><tr><th>Ledger</th><th class="r">Debit</th>
          <th class="r">Credit</th></tr></thead><tbody>` +
        d.rows.map(r => {
          const head = r.gname !== last
            ? `<tr class="ogrp"><td colspan="3"><b>${esc(r.gname)}</b></td></tr>` : '';
          last = r.gname;
          return head + `<tr data-href="${esc(accountLink(r.id, true))}">
            <td style="padding-left:26px">» ${goLink(accountLink(r.id, true), esc(r.name))}</td>
            <td class="r num">${r.debit ? fmt(r.debit) : ''}</td>
            <td class="r num">${r.credit ? fmt(r.credit) : ''}</td></tr>`;
        }).join('') +
        `</tbody><tfoot><tr><td>Total</td>
          <td class="r num"><b>${fmt(d.total_debit)}</b></td>
          <td class="r num"><b>${fmt(d.total_credit)}</b></td></tr></tfoot></table>`
        : '<div class="empty">No balances to show yet.</div>'}</div></div>`;
  }

  // The groups folded shut stay shut: they ride in the address with the rest
  // of the screen, so an account opened off this sheet and closed with Esc
  // comes back to the sheet as it was left (30 Sep 2026).
  let shut = new Set();
  const mem = screenState('/reports/outstanding', {
    to: '#f2', layout: '#layout', search: '#q', fy: fyField(null, 'f2'),
    shut: { get: () => [...shut].join('|'),
            set: v => { shut = new Set(String(v || '').split('|').filter(Boolean)); } }
  }).restore(q);

  async function load() {
    mem.save();
    // Trial balance is the same sheet read the other way, and its own report
    // already answers for it.
    const here = onSearch();
    if ($('#layout').value === 'tb') {
      const t = await api.get('/api/reports/trial-balance', { to: $('#f2').value });
      if (!here()) return;
      const hunt = $('#q').value.trim().toUpperCase();
      if (hunt) t.rows = t.rows.filter(r => r.name.toUpperCase().includes(hunt));
      $('#out').innerHTML = trialSheet(t);
      return;
    }
    const d = await api.get('/api/reports/outstanding',
      { to: $('#f2').value, search: $('#q').value.trim() });
    if (!here()) return;
    $('#out').innerHTML = `
      <div class="kpis">
        <div class="kpi good"><div class="lb">Owed to us</div>
          <div class="vl">${money(d.assets.total)}</div>
          <div class="ft">${d.assets.count} account(s) in debit</div></div>
        <div class="kpi bad"><div class="lb">We owe</div>
          <div class="vl">${money(d.liabilities.total)}</div>
          <div class="ft">${d.liabilities.count} account(s) in credit</div></div>
        <div class="kpi"><div class="lb">Net</div>
          <div class="vl">${money(Math.abs(d.assets.total - d.liabilities.total))}</div>
          <div class="ft">as on ${dt($('#f2').value)}</div></div>
      </div>
      <div class="osheets">
        ${column(d.liabilities, 'Liabilities', 'l')}
        ${column(d.assets, 'Assets', 'a')}
      </div>
      ${d.opening_difference ? `<div class="card"><div class="bd"
        style="font-size:14px">
        <b>Difference in opening Balance: ${fmt(Math.abs(d.opening_difference))}
        ${d.opening_difference >= 0 ? 'DB' : 'CR'}</b>
        <div style="color:var(--muted);margin-top:4px">Carried on
        <i>Difference in Opening Balances</i> — the years that were never
        backed up. It is a bookkeeping entry, not anybody's debt, so it is
        kept out of the two columns and printed here, the way the old
        software prints it.</div></div></div>` : ''}`;

    // A group heading folds its own ledgers away and leaves the sum.
    $$('#out tr.ogrp').forEach(tr => {
      tr.onclick = () => {
        const open = !tr.querySelector('.caret').classList.contains('open');
        tr.querySelector('.caret').classList.toggle('open', open);
        $$('#out tr.orow[data-for="' + tr.dataset.g + '"]').forEach(r => {
          r.style.display = open ? '' : 'none';
        });
        if (open) shut.delete(tr.dataset.key); else shut.add(tr.dataset.key);
        mem.save();
      };
    });
  }

  $('#go').onclick = load;
  $('#layout').onchange = load;
  $('#q').oninput = () => { clearTimeout(window._ot); window._ot = setTimeout(load, 200); };
  bindFy(load, null, 'f2');
  $('#csv').onclick = () => location.href = firmUrl(($('#layout').value === 'tb'
    ? '/api/export/trial-balance?to=' + $('#f2').value
    : '/api/export/outstanding?' + new URLSearchParams(
        { to: $('#f2').value, search: $('#q').value.trim() })));
  load();
});

/* ========================================================= who owes us ---
   **Due & ageing answers for one customer; this asks the whole book.** The
   account screen could already settle a customer and age what was left, but
   only one at a time -- which is no use for the question that actually gets
   asked, *who do I chase first*, because answering it needs the name you were
   trying to find.

   Every row is settled the way that customer's own page settles it, through
   the one `_settle` in `api.py` that both call, so a figure here and a figure
   there cannot drift apart and both tie to the account balance.

   **Two ages, and they are different questions.** How old the money is, from
   the day the challan was written; and how long it has been *late*, which is
   nothing at all until the terms on the document have run out. A customer on
   90 days holding a two-month-old bill owes old money and is not late by a
   day. The list leads with the first, because the oldest money is what gets
   chased, and carries the second beside it.                                */
/** How long, in days and nothing else -- asked for on 15 Sep 2026. Months
    and years were rounded ("5 months", "1 year 2m"), and a bill's terms
    are counted in days, so the age is too: "146 days", "1,204 days". */
function spellDays(d) {
  d = Math.round(num(d));
  if (d <= 0) return '—';
  return fmt(d, 0) + ' day' + (d === 1 ? '' : 's');
}

/* A customer's whole account: every challan, bill (the GST on it), receipt,
   payment and journal, in every year, the latest first (23 Sep 2026: "it
   should redirect me to the user's complete account, all bills, challans,
   payments, everything"). Every year, because half of these customers owe
   for bills written before this one began -- read over this year alone,
   ACCURATE FABRICATION's ₹14,000 was an opening balance with nothing under
   it. Since 24 Sep 2026 Who owes us opens the customer's own page first
   (below), and the whole account is a button on it. */
const wholeAccountHref = id => accountLink(id) + '&view=entries&fy=all';

/** One customer's page off Who owes us, settled to the same day. */
function customerDuesHref(id, asOn) {
  return '#/reports/receivables/customer?ledger_id=' + id
    + (asOn && asOn !== today() ? '&as_on=' + asOn : '');
}

route(/^\/reports\/receivables$/, async (view, _m, q) => {
  setTitle('Who owes us', 'Every customer with money outstanding, by name');

  view.innerHTML = `
    <div class="card"><div class="bd"><div class="row">
      ${fySelect('fyq', { asOn: true })}
      <div><label>As on</label><input type="date" id="f2"
        value="${esc(q.as_on || today())}"
        title="Settle every account to this day. Money received later — a post-dated cheque, say — is not counted as received."></div>
      <div><label>Show</label><select id="only" style="width:calc(190px * var(--typescale))">
        <option value="">Everyone who owes</option>
        <option value="late">Only the overdue</option>
      </select></div>
      <div><label>Order</label><select id="sort" style="width:calc(180px * var(--typescale))">
        <option value="name" selected>By name</option>
        <option value="age">Oldest money first</option>
        <option value="late">Longest overdue first</option>
        <option value="owed">Largest amount first</option>
      </select></div>
      <div style="flex:1;min-width:180px"><label>Search</label>
        <input id="q" placeholder="Customer, city or GSTIN…"></div>
      <div class="spacer"></div>
      <div class="acts">
        <button id="go" class="primary">Apply</button>
        <button id="csv">⤓ CSV</button>
        <button id="print">🖨 Print</button>
      </div>
    </div></div></div><div id="out"></div>`;

  const mem = screenState('/reports/receivables', {
    as_on: '#f2', only: '#only', sort: '#sort', search: '#q',
    fy: fyField(null, 'f2')
  }).restore(q);

  const BANDS = { '1-30': '1 to 30 days late', '31-60': '31 to 60 days late',
                  '61-90': '61 to 90 days late', '91+': 'over 90 days late' };

  const spell = spellDays;

  /* A customer's name opens their own page -- the bills still owed and every
     payment they made, newest first -- not the account straight away (24 Sep
     2026: "Don't redirect to the account directly, but open this in a new
     page"). The whole account is a button there. */
  const custPage = id => customerDuesHref(id, $('#f2').value);

  /* The open items behind one customer, each with its own two ages. This is
     the row that answers "how old is each bill" -- the one above it can only
     answer for the oldest. */
  const itemsTable = r => `<table class="sub"><thead><tr>
      <th style="width:108px">Date</th><th>Item</th>
      <th style="width:92px">No</th><th style="width:100px">Terms</th>
      <th style="width:108px">Due on</th>
      <th class="r" style="width:96px" title="Days since the challan was written">Age</th>
      <th class="r" style="width:104px" title="Days since it fell due — nothing until the terms have run">Late by</th>
      <th class="r" style="width:140px">Still owed</th></tr></thead>
    <tbody>${r.claims.map(c => `<tr class="${c.overdue_days ? 'lateln' : ''}"${claimHref(c)}>
      <td class="nw">${dt(c.date)}</td>
      <td>${claimParticulars(c, '11.5px')}</td>
      <td class="nw"><b>${goLink(docLink(c.kind, c.doc_id, c.company_id), esc(c.no || ''))}</b></td>
      <td class="nw" style="color:var(--muted)">${esc(c.terms || '—')}</td>
      <td class="nw">${dt(c.due)}</td>
      <td class="r num nw" style="color:var(--muted)">${spell(c.age_days)}</td>
      <td class="r num nw"${c.overdue_days ? ' style="color:var(--bad);font-weight:700"' : ''}>${
        c.overdue_days ? spell(c.overdue_days) : 'not yet'}</td>
      <td class="r num"><b>${fmt(c.open)}</b></td></tr>`).join('')}
    </tbody><tfoot><tr><td colspan="6">${r.claim_count} open item(s)</td>
      <td class="r num">${r.due_now ? fmt(r.due_now) + ' due' : ''}</td>
      <td class="r num"><b>${fmt(r.owed)}</b></td></tr></tfoot></table>`;

  function paint(d, rows) {
    const t = d.totals;
    $('#out').innerHTML = `
      <div class="kpis">
        <div class="kpi ${t.owed > 0 ? 'good' : ''}"><div class="lb">Owed to us</div>
          <div class="vl">${money(t.owed)}</div>
          <div class="ft">${t.count} customer(s) · ${t.claims} open item(s)
            as at ${dt(d.as_on)}</div></div>
        <div class="kpi ${t.due_now > 0 ? 'bad' : ''}"><div class="lb">Should already be in</div>
          <div class="vl">${money(t.due_now)}</div>
          <div class="ft">${t.overdue_count} customer(s) past their terms</div></div>
        <div class="kpi"><div class="lb">Not yet due</div>
          <div class="vl">${money(t.not_due)}</div>
          <div class="ft">still inside the terms they were given</div></div>
        <div class="kpi"><div class="lb">Oldest money</div>
          <div class="vl" style="font-size:22px">${rows.length
            ? esc(spell(Math.max(...rows.map(r => num(r.owed_for_days)))))
            : '—'}</div>
          <div class="ft">since the oldest unpaid challan was written</div></div>
      </div>

      <div class="card"><h2>How late it is<span class="rt"
          style="font-weight:400;font-size:12.5px;color:var(--muted)">
          every customer's overdue money, banded by how long ago it fell due</span></h2>
        <div class="bd">${t.due_now > 0 ? `<div class="ageband">${d.buckets.map(b =>
          b.amount ? `<div style="flex:${b.amount}" class="ab ab${b.band.replace('+','p')}"
            title="${esc(BANDS[b.band] || b.band)} — ${b.count} customer(s)">
            <b>${fmt(b.amount)}</b><span>${esc(b.band)} days · ${b.count}</span></div>` : '').join('')}
        </div>` : '<div class="empty" style="padding:14px">Nothing is past due.</div>'}
        </div></div>

      <div class="card"><h2>${rows.length} customer(s)<span class="rt"
          style="font-weight:400;font-size:12.5px;color:var(--muted)">
          Enter or a click opens the customer — every bill and payment · → or ▸ the unpaid bills here</span></h2>
        <div class="bd flush tbl-scroll">${rows.length ? `
        <table><thead><tr><th>Customer</th>
          <th class="r" style="width:150px">Owed</th>
          <th class="r" style="width:150px">Already due</th>
          <th style="width:118px">Oldest bill</th>
          <th class="r" style="width:120px" title="How long the oldest unpaid money has been outstanding">Owing for</th>
          <th class="r" style="width:120px" title="How long past its due date the oldest overdue item is">Late by</th>
          <th style="width:130px">Last paid</th></tr></thead>
        <tbody>${rows.map((r, i) => `
          <tr class="clickable ogrp" data-r="${i}" data-href="${esc(custPage(r.ledger_id))}"
              data-enter-label="open customer" data-fold="unpaid bills"
              title="Open ${esc(r.name)} — every bill and payment, newest first">
            <td><span class="caret" title="Show the unpaid bills (→)">▸</span> <b>${goLink(custPage(r.ledger_id),
              esc(r.name), 'Open ' + r.name + ' — every bill and payment, newest first')}</b>
              ${r.over_limit ? ' <span class="tag bad">over limit</span>' : ''}
              <div style="font-size:11.5px;color:var(--muted)">${
                esc([r.city, r.phone].filter(Boolean).join(' · '))}${
                r.usual_terms ? ' · usually ' + esc(r.usual_terms) : ''}</div></td>
            <td class="r num"><b>${fmt(r.owed)}</b></td>
            <td class="r num"${r.due_now ? ' style="color:var(--bad)"' : ''}>${
              r.due_now ? fmt(r.due_now) : '—'}</td>
            <td class="nw">${dt(r.oldest_bill)}</td>
            <td class="r nw">${esc(spell(r.owed_for_days))}</td>
            <td class="r nw"${r.oldest_overdue_days ? ' style="color:var(--bad);font-weight:600"' : ''}>${
              r.oldest_overdue_days ? esc(spell(r.oldest_overdue_days)) : 'not yet'}</td>
            <td class="nw">${r.last_paid
              ? dt(r.last_paid) + '<div style="font-size:11.5px;color:var(--muted)">'
                + esc(spell(r.quiet_days)) + ' ago</div>'
              : '<span class="tag warn">never</span>'}</td></tr>
          <tr class="det" data-for="${i}" style="display:none"><td colspan="7"
            style="padding:0 0 10px 26px">${itemsTable(r)}
            <div style="margin-top:7px"><a class="btn sm"
              href="${esc(custPage(r.ledger_id))}">Bills and payments →</a></div>
            </td></tr>`).join('')}
        </tbody><tfoot><tr><td>${rows.length} customer(s)</td>
          <td class="r num"><b>${fmt(rows.reduce((a, r) => a + num(r.owed), 0))}</b></td>
          <td class="r num">${fmt(rows.reduce((a, r) => a + num(r.due_now), 0))}</td>
          <td colspan="4"></td></tr></tfoot></table>`
        : '<div class="empty"><b>Nobody owes anything</b>No customer has money outstanding as at ' + dt(d.as_on) + '.</div>'}
        </div>
        <div class="bd" style="border-top:1px solid var(--line);font-size:12.5px;color:var(--muted)">
          Every account is settled the way its own page settles it — each
          payment against the oldest debt first — so a row here and the
          customer's <b>Due &amp; ageing</b> view always agree. A challan and
          the GST raised on it are <b>one item</b>, the two added together and
          due by the terms written on the challan. A challan that was never
          billed stands on its own; a GST entry that could not be matched to
          its challan follows that customer's usual period.
          ${d.unknown_count ? `<div style="margin-top:6px"><b>${d.unknown_count}
            customer(s)</b> the GST firms bill have never been written in
            <b>${esc(d.money_book)}</b>, where the money is. Nothing can say
            what they owe, so they are left out rather than guessed at —
            <a href="#/allocation">join them up under Customer allocation</a>.</div>` : ''}
        </div></div>`;

    /* **The line opens the customer's own page** -- the bills still owed
       and every payment, newest first (24 Sep 2026; from 23 Sep it opened
       the whole account: "I wasn't directed to the accounts of the
       customer"). Enter or a click on it once unfolded the unpaid bills
       instead, and the next Enter opened the oldest of them -- a challan.
       The line leads on (its data-href), and the unpaid bills fold out
       under it on → (← puts them away) or a click on the ▸. */
    $$('#out tr[data-r]').forEach(tr => {
      const det = $('#out tr.det[data-for="' + tr.dataset.r + '"]');
      const caret = $('.caret', tr);
      if (!det || !caret) return;
      tr._fold = () => {
        const open = det.style.display === 'none';
        det.style.display = open ? '' : 'none';
        caret.classList.toggle('open', open);
      };
      caret.onclick = e => { e.stopPropagation(); tr._fold(); KB.paintBar(); };
    });
  }

  // Settling the whole book takes long enough that two of these can be in the
  // air at once -- change the date while a load is running and the older
  // answer can come back last and paint over the newer one. The count says
  // which one is still wanted.
  let seq = 0;
  async function load() {
    const mine = ++seq;
    mem.save();
    $('#out').innerHTML = '<div class="card"><div class="empty">Settling every account…</div></div>';
    const here = onSearch();
    let d;
    try { d = await api.get('/api/reports/receivables', { as_on: $('#f2').value }); }
    catch (e) {
      if (mine !== seq || !here()) return;
      bad(e.message);
      $('#out').innerHTML = '<div class="card"><div class="empty">'
        + esc(e.message) + '</div></div>';
      return;
    }
    if (mine !== seq || !here()) return;
    // Narrowing and ordering are done here rather than asked for again: the
    // whole book is already in hand, and every account on it is settled.
    let rows = d.rows;
    if ($('#only').value === 'late') rows = rows.filter(r => num(r.due_now) > 0);
    const term = $('#q').value.trim();
    if (term) rows = searchList(term, rows, r => [r.name, r.city, r.gstin]);
    const by = $('#sort').value;
    rows = rows.slice().sort(
      by === 'owed' ? (a, b) => num(b.owed) - num(a.owed)
      : by === 'late' ? (a, b) => num(b.oldest_overdue_days) - num(a.oldest_overdue_days)
      : by === 'name' ? (a, b) => a.name.localeCompare(b.name)
      : (a, b) => num(b.owed_for_days) - num(a.owed_for_days));
    paint(d, rows);
  }

  $('#go').onclick = load;
  ['#only', '#sort'].forEach(x => $(x).onchange = load);
  bindFy(load, null, 'f2');
  $('#f2').onchange = load;
  $('#q').oninput = () => { clearTimeout(window._rc); window._rc = setTimeout(load, 160); };
  $('#print').onclick = () => window.print();
  $('#csv').onclick = () => location.href = firmUrl('/api/export/receivables?'
    + new URLSearchParams({ as_on: $('#f2').value }));
  load();
});

/* ======================================== one customer, off Who owes us ---
   Asked on 24 Sep 2026, over the unpaid bills a row of Who owes us folds out
   onto: *"Don't redirect to the account directly, but open this in a new
   page. And also show any kind of payment done by the customer. Also make
   sure to show the newest payment or bill on the top."*

   So a name on Who owes us opens this page: the bills still owed, each with
   its two ages, and everything the customer has paid -- into a bank, in
   cash, or brought off by a return or a journal -- in **one list, the newest
   at the top**. It is `api.report_due`, the settlement the row itself was
   worked out by (`_settle`), so the figures here and on the row agree. The
   whole account, every bill paid or not, is one button away.            */
const PAID_WORD = {
  RECEIPT: 'Received', JOURNAL: 'Journal', CREDIT: 'Credit note',
  DEBIT: 'Debit note', CONTRA: 'Contra', PAYMENT: 'Payment', SALE: 'Return',
  OPENING: 'Opening balance'
};

route(/^\/reports\/receivables\/customer$/, async (view, _m, q) => {
  const lid = num(q.ledger_id);
  setTitle('Customer', 'Every bill and payment, newest first');
  if (!lid) {
    view.innerHTML = `<div class="card"><div class="empty"><b>No customer chosen</b>
      Open one from <a href="#/reports/receivables">Who owes us</a>.</div></div>`;
    return;
  }
  view.innerHTML = `
    <div class="card"><div class="bd"><div class="row">
      ${fySelect('fyq', { asOn: true })}
      <div><label>As on</label><input type="date" id="f2"
        value="${esc(q.as_on || today())}"
        title="Settle the account to this day. Money received later is not counted as received."></div>
      <div><label>Show</label><select id="show" style="width:calc(230px * var(--typescale))">
        <option value="">Every transaction</option>
        <option value="allbills">Every bill, paid or not</option>
        <option value="bills">Only the bills still owed</option>
        <option value="paid">Only the payments</option>
      </select></div>
      <div class="spacer"></div>
      <div class="acts">
        <button id="go" class="primary">Apply</button>
        <button id="print">🖨 Print</button>
      </div>
    </div></div></div><div id="out"></div>`;

  const mem = screenState('/reports/receivables/customer', {
    ledger_id: { get: () => lid, set: () => {} },
    as_on: '#f2', show: '#show', fy: fyField(null, 'f2')
  }).restore(q);

  /** Whole days from one "2026-09-24" to another. */
  const daysFrom = (a, b) => Math.round(
    (new Date(b + 'T00:00:00') - new Date(a + 'T00:00:00')) / 864e5);

  /** A payment's line: what it was, the account it went into, and the
   *  narration SHREE wrote under it. */
  const paidParticulars = p => {
    const into = p.particulars
      ? (p.doc_kind === 'RECEIPT' ? 'into ' : p.doc_kind === 'JOURNAL' ? 'against ' : '')
        + goLink(accountLink(p.other_id, 'book'), esc(p.particulars),
                 'Open ' + p.particulars)
      : '';
    const under = [into, esc(p.narration || '')].filter(Boolean).join(' · ');
    return `<span class="tag ok">${esc(PAID_WORD[p.doc_kind] || p.kind || 'Paid')}</span>`
      + (under ? '<div style="font-size:11.5px;color:var(--muted)">' + under + '</div>' : '');
  };

  async function paint(d, here) {
    const party = d.party || {};
    const pays = d.payments || [];
    setTitle(party.name || 'Customer',
      [party.city, party.phone].filter(Boolean).join(' · ')
        || 'Every bill and payment, newest first',
      [{ label: 'Open the whole account →',
         onClick: () => { location.hash = wholeAccountHref(lid); } }]);

    const received = pays.filter(p => p.doc_kind === 'RECEIPT');
    const otherPaid = pays.filter(p => p.doc_kind !== 'RECEIPT');
    const sum = list => list.reduce((a, p) => a + num(p.amount), 0);
    const dated = pays.filter(p => p.date);
    const last = dated.length ? dated[dated.length - 1] : null;
    const lateDays = Math.max(0, ...d.claims.map(c => num(c.overdue_days)));

    // Every transaction in one list, the newest at the top (6 Oct 2026:
    // "make sure each and every transaction with that customer is shown") --
    // every bill, the ones paid off as well as the ones still owed, and every
    // payment. On one day the money is put above the goods it paid for; an
    // advance brought in as the opening balance has no date and sits at the
    // foot.
    const show = $('#show').value;
    const bills = d.bills || d.claims;
    const items = [];
    if (show === 'bills') d.claims.forEach(c => items.push({ t: 0, date: c.date || '', no: c.no || '', c }));
    else if (show !== 'paid') bills.forEach(c => items.push({ t: 0, date: c.date || '', no: c.no || '', c }));
    if (show !== 'bills' && show !== 'allbills') {
      pays.forEach(p => items.push({ t: 1, date: p.date || '', no: p.no || '', p }));
    }
    items.sort((a, b) => (a.date !== b.date ? (a.date < b.date ? 1 : -1)
      : b.t - a.t || String(b.no).localeCompare(String(a.no), undefined, { numeric: true })));
    const shownPaid = sum(items.filter(x => x.p).map(x => x.p));
    const shownBilled = items.filter(x => x.c).reduce((a, x) => a + num(x.c.amount), 0);
    const shownOwed = items.filter(x => x.c).reduce((a, x) => a + num(x.c.open), 0);
    const nBills = items.filter(x => x.c).length, nPaid = items.length - nBills;
    const nOpen = items.filter(x => x.c && num(x.c.open) > 0.004).length;

    // A bill paid off says so where the amount still owed would be; its ages
    // are a question only money still standing has.
    const row = x => {
      if (x.c) {
        const c = x.c;
        const owes = num(c.open) > 0.004;
        const part = owes && num(c.amount) - num(c.open) > 0.004;
        return `<tr class="${c.overdue_days && owes ? 'lateln' : ''}"${claimHref(c)}>
          <td class="nw">${dt(c.date)}</td>
          <td>${claimParticulars(c, '11.5px')}</td>
          <td class="nw"><b>${goLink(docLink(c.kind, c.doc_id, c.company_id), esc(c.no || ''))}</b></td>
          <td class="nw">${dt(c.due)}${c.terms
            ? '<div style="font-size:11.5px;color:var(--muted)">' + esc(c.terms) + '</div>' : ''}</td>
          <td class="r num nw" style="color:var(--muted)">${owes ? spellDays(c.age_days) : ''}</td>
          <td class="r num nw"${owes && c.overdue_days ? ' style="color:var(--bad);font-weight:700"' : ''}>${
            !owes ? '' : c.overdue_days ? spellDays(c.overdue_days) : 'not yet'}</td>
          <td class="r num">${fmt(c.amount)}</td>
          <td class="r num"></td>
          <td class="r num">${owes ? '<b>' + fmt(c.open) + '</b>' + (part
            ? '<div style="font-size:11.5px;color:var(--muted)">part paid</div>' : '')
            : '<span class="tag ok">Paid</span>'}</td></tr>`;
      }
      const p = x.p;
      const href = docLink(p.doc_kind, p.doc_id, p.company_id);
      return `<tr${href ? ' data-href="' + esc(href) + '" title="Open ' + esc(p.kind + ' ' + (p.no || '')) + '"' : ''}>
        <td class="nw">${p.date ? dt(p.date) : '—'}</td>
        <td>${paidParticulars(p)}</td>
        <td class="nw"><b>${goLink(href, esc(p.no || ''))}</b></td>
        <td colspan="4"></td>
        <td class="r num" style="color:var(--ok)"><b>${fmt(p.amount)}</b></td>
        <td class="r num"></td></tr>`;
    };

    $('#out').innerHTML = `
      ${d.accounted === false ? `<div class="card"><div class="bd" style="color:var(--bad)">
        This customer is not written in the accounts book, where the money is,
        so what they owe cannot be worked out. The bills below are everything
        billed to them. <a href="#/allocation">Join them up under Customer allocation</a>.</div></div>` : ''}
      <div class="kpis">
        <div class="kpi ${d.owed > 0 ? 'bad' : 'good'}"><div class="lb">Still owed</div>
          <div class="vl">${money(d.owed)}</div>
          <div class="ft">${d.claim_count} open of ${bills.length} bill(s) as at ${dt(d.as_on)}</div></div>
        <div class="kpi ${d.due_now > 0 ? 'bad' : ''}"><div class="lb">Should already be in</div>
          <div class="vl">${money(d.due_now)}</div>
          <div class="ft">${lateDays ? 'the oldest ' + spellDays(lateDays) + ' late'
            : 'nothing past its terms'}</div></div>
        <div class="kpi good"><div class="lb">Paid</div>
          <div class="vl">${money(sum(received))}</div>
          <div class="ft">${received.length} receipt(s)${otherPaid.length
            ? ' · ' + money(sum(otherPaid)) + ' by returns and journals' : ''}</div></div>
        <div class="kpi"><div class="lb">Last paid</div>
          <div class="vl" style="font-size:22px">${last ? dt(last.date) : 'Never'}</div>
          <div class="ft">${last ? money(last.amount) + ' · ' + esc(spellDays(daysFrom(last.date, d.as_on)))
            + ' ago' : 'no payment in the books'}</div></div>
      </div>

      <div class="card"><h2>${{ bills: 'Bills still owed', allbills: 'Every bill',
          paid: 'Payments' }[show] || 'Every transaction'}<span class="rt"
          style="font-weight:400;font-size:12.5px;color:var(--muted)">
          newest first · Enter or a click opens it</span></h2>
        <div class="bd flush tbl-scroll" id="txlist">${items.length ? ''
        : `<div class="empty">${show === 'paid' ? 'No payment is in the books up to ' + dt(d.as_on) + '.'
            : show === 'bills' ? 'Nothing is owed as at ' + dt(d.as_on) + '.'
            : 'Nothing is in the books for this customer up to ' + dt(d.as_on) + '.'}</div>`}
        </div>
        <div class="bd" style="border-top:1px solid var(--line);font-size:12.5px;color:var(--muted)">
          Every bill and every payment on the account up to the day. Each
          payment is set against the oldest bill first, so a bill marked
          <b>Paid</b> is one the payments have covered and the bills still owed
          are the newest ones not yet covered. A challan and the GST raised on
          it are one item; the GST bill it went out on opens from the line.
          Returns and journals that took money off the account are listed with
          the payments.</div></div>`;
    if (!items.length) return;
    // A long account -- nearly a thousand lines -- is drawn top first.
    await fillRows($('#txlist'), `
        <table><thead><tr>
          <th style="width:100px">Date</th><th>Particulars</th>
          <th style="width:76px">No</th>
          <th style="width:104px" title="When it falls due, and the terms it was given">Due on</th>
          <th class="r" style="width:84px" title="Days since the challan was written, while any of it is owed">Age</th>
          <th class="r" style="width:88px" title="Days since it fell due — nothing until the terms have run">Late by</th>
          <th class="r" style="width:112px">Billed</th>
          <th class="r" style="width:112px">Paid</th>
          <th class="r" style="width:120px">Still owed</th></tr></thead>
        <tbody>`, items.map(row), `</tbody>
        <tfoot><tr><td colspan="6">${[nBills ? nBills + ' bill(s)' + (show === 'bills' ? ''
              : ', ' + nOpen + ' still owed') : '',
            nPaid ? nPaid + ' payment(s)' : ''].filter(Boolean).join(' · ')}</td>
          <td class="r num">${nBills ? fmt(shownBilled) : ''}</td>
          <td class="r num">${nPaid ? fmt(shownPaid) : ''}</td>
          <td class="r num"><b>${nBills ? fmt(shownOwed) : ''}</b></td></tr></tfoot></table>`,
      { here });
  }

  let seq = 0;
  async function load() {
    const mine = ++seq;
    mem.save();
    const here = onScreen();
    $('#out').innerHTML = '<div class="card"><div class="empty">Settling the account…</div></div>';
    let d;
    try {
      d = await api.get('/api/reports/due',
        { ledger_id: lid, as_on: $('#f2').value, payments: 1 });
    } catch (e) {
      if (mine !== seq || !here()) return;
      bad(e.message);
      $('#out').innerHTML = '<div class="card"><div class="empty">'
        + esc(e.message) + '</div></div>';
      return;
    }
    if (mine !== seq || !here()) return;
    await paint(d, () => mine === seq && here());
  }

  $('#go').onclick = load;
  $('#show').onchange = load;
  bindFy(load, null, 'f2');
  $('#f2').onchange = load;
  $('#print').onclick = () => window.print();
  load();
});

/* Stock added, removed or corrected by hand in BST's or BSI's books (27 Sep
   2026). One form for the three. It was redrawn the same day for anyone to
   use ("the stock management is still confusing"): what is being done is
   three big buttons rather than a dropdown, the firm two more, a line's own
   buttons open it with the material already filled in, and Save says in
   words what it will do -- "Add 2,500 kg". */
const STOCK_KIND = {
  ADD: { title: 'Add stock', seg: '＋ Add stock', box: 'Kg to add', done: 'Add',
    say: 'For stock that came in without a purchase bill — an opening, goods returned, a mistake put right.',
    reasons: ['Opening stock', 'Received', 'Returned', 'Mistake put right'] },
  REMOVE: { title: 'Remove stock', seg: '− Remove stock', box: 'Kg to remove', done: 'Remove',
    say: 'For stock that went out without a bill — damaged, scrap, used, short.',
    reasons: ['Damaged', 'Scrap', 'Used', 'Short', 'Mistake put right'] },
  COUNT: { title: 'Correct stock', seg: '✎ Correct stock', box: 'Real kg you have', done: 'Correct',
    say: 'Weighed or counted it? Type the real weight you have — the app changes the stock to match. Type 0 if none is left.',
    reasons: ['Weighed', 'Stock count', 'Year-end count', 'Mistake put right'] }
};

/** The form. `id` opens one already made; otherwise `kind` says what it
 *  starts as, `firm` in which books, and `preset` = {item_id} the material
 *  its first line starts with. Answers true when something was saved or
 *  deleted. */
/** Billing Stock's categories (4 Oct 2026): the seven asked for to start
 *  with, then the user's to change. A name changed here takes its materials
 *  along; a category removed leaves its materials with none; a new one is
 *  added at the foot. Resolves true when saved. */
async function stockCatsDialog() {
  let d;
  try { d = await api.get('/api/stock/cats'); } catch (e) { bad(e.message); return false; }
  const holds = n => (n ? n + ' product' + (n === 1 ? '' : 's') : 'empty');
  const row = c => `<tr><td><input class="catname" value="${esc(c.name)}" data-old="${esc(c.name)}"
      maxlength="60" placeholder="New category name" autocomplete="off"></td>
    <td class="r nw catcount">${c.name ? holds(c.products) : 'new'}</td>
    <td class="r"><button type="button" class="sm" data-rm title="Remove this category">✕ Remove</button></td></tr>`;
  const removed = [];
  const said = name => d.cats.find(c => c.name === name);
  const saved = await modal('Billing Stock categories', `
    <p class="stocksay">Every material in Billing Stock sits under one of these. Change a name here
      and its materials go with it; remove one and its materials are left with no category.
      To move one material, open it in Billing Stock and change its <b>Category</b>.</p>
    <table class="catedit"><tbody>${d.cats.map(row).join('')}</tbody></table>
    <button type="button" class="sm" data-add style="margin-top:10px">＋ Add a category</button>
    ${d.none ? `<p class="subfig" style="margin:10px 0 0">${d.none} product${d.none === 1 ? ' is' : 's are'} in no category.</p>` : ''}`, [
    { label: 'Cancel', value: null },
    {
      label: 'Save', cls: 'primary', value: true, before: async bg => {
        const cats = $$('input.catname', bg).map(i => ({ old: i.dataset.old || '', name: i.value.trim() }));
        const names = cats.map(c => c.name.replace(/\s+/g, ' ').toUpperCase()).filter(Boolean);
        const twice = names.find((n, i) => names.indexOf(n) !== i);
        if (twice) { bad('"' + twice + '" is in the list twice.'); return false; }
        // A name rubbed out is a category removed, the same as ✕.
        const going = removed.concat(cats.filter(c => c.old && !c.name).map(c => said(c.old)))
          .filter(c => c && c.products);
        if (going.length && !await confirmBox('Remove ' + (going.length === 1 ? 'a category' : 'categories'),
          going.map(c => c.name + ' (' + holds(c.products) + ')').join(', ') + ' will be removed, and '
          + (going.length === 1 ? 'its' : 'their') + ' materials left with no category. Go ahead?')) return false;
        try { await api.put('/api/stock/cats', { cats }); } catch (e) { bad(e.message); return false; }
      }
    }
  ], {
    onOpen: bg => {
      const tb = $('table.catedit tbody', bg);
      const bindRow = tr => {
        $('[data-rm]', tr).onclick = () => {
          const old = $('input.catname', tr).dataset.old;
          if (old) removed.push(said(old));
          const next = tr.nextElementSibling || tr.previousElementSibling;
          tr.remove();
          const to = next ? $('input.catname', next) : $('[data-add]', bg);
          if (to) to.focus();
        };
      };
      $$('tr', tb).forEach(bindRow);
      $('[data-add]', bg).onclick = () => {
        tb.insertAdjacentHTML('beforeend', row({ name: '', products: 0 }));
        bindRow(tb.lastElementChild);
        $('input.catname', tb.lastElementChild).focus();
      };
    }
  });
  if (saved) ok('Categories saved.');
  return !!saved;
}

async function stockEntryDialog(id, kind, firm, preset) {
  const firms = S.meta.billing_firms || [];
  let e = { kind: kind || 'ADD', entry_date: today(), reason: '', note: '',
            company_id: num(firm) || (firms[0] && firms[0].id) || 0, lines: [] };
  if (id) e = await api.get('/api/stock/entries/' + id);
  let kindNow = e.kind, firmNow = e.company_id;
  const blank = () => ({ item_id: null, name: '', hsn: '', weight: '' });
  const rows = e.lines.length ? e.lines.map(l => ({
    item_id: l.item_id, name: l.name, hsn: l.hsn || '',
    weight: String(e.kind === 'COUNT' ? l.counted : Math.abs(num(l.qty)))
  })) : [Object.assign(blank(), preset && preset.item_id ? { item_id: preset.item_id } : {})];
  let items = [], byId = {}, changed = false, saveBtn = null, dlg = null;

  const body = `
    <div class="seg bigseg" id="sk-kinds" role="group" aria-label="What to do">${Object.keys(STOCK_KIND).map(k =>
      `<button type="button" data-kind="${k}" class="k-${k.toLowerCase()}">${STOCK_KIND[k].seg}</button>`).join('')}</div>
    <p class="stocksay" id="sk-say"></p>
    <div class="row sehead" style="align-items:flex-end">
      <div><label>Firm</label><div class="seg" id="sk-firms" role="group" aria-label="Firm">${firms.map(f =>
        `<button type="button" data-firm="${f.id}" title="${esc(f.name)}">${esc(f.code)}</button>`).join('')}</div></div>
      <div><label>Date</label><input type="date" id="sk-date" value="${esc(e.entry_date)}"></div>
      <div style="flex:1;min-width:180px"><label>Why <span class="opt">optional</span></label>
        <input id="sk-reason" list="sk-reasons" value="${esc(e.reason)}" placeholder="Choose or type a reason">
        <datalist id="sk-reasons"></datalist></div>
    </div>
    <div class="tbl-scroll" style="margin-top:14px"><table class="lineitems stockgrid"><thead><tr>
      <th style="width:30px"></th><th>Material</th><th class="r" style="width:160px">In stock now</th>
      <th class="r" style="width:150px" id="sk-boxhead"></th><th class="r" style="width:150px">After saving</th>
      <th style="width:34px"></th></tr></thead>
      <tbody id="sk-lines"></tbody></table></div>
    <div class="row" style="margin-top:10px;align-items:center">
      <button type="button" id="sk-add">＋ Another material</button>
      <span class="hint" style="margin:0 0 0 8px">or press Enter in the last Kg box</span></div>
    <div style="margin-top:12px"><label>Note <span class="opt">optional</span></label><input id="sk-note" value="${esc(e.note)}"
      placeholder="Anything to remember about it"></div>
    <p class="hint" id="sk-err"></p>`;

  const kilos = v => num(String(v).replace(/,/g, ''));
  const typed = v => String(v === null || v === undefined ? '' : v).trim() !== '';
  const firmCode = () => (firms.find(f => f.id === num(firmNow)) || {}).code || '';
  /** What one line does: the stock it finds, and what is left after. */
  function effect(l) {
    const it = byId[l.item_id];
    if (!it) return null;
    const have = num(it.stock);
    if (!typed(l.weight)) return { have, after: null, change: 0 };
    const w = kilos(l.weight);
    const change = kindNow === 'ADD' ? w : kindNow === 'REMOVE' ? -w : w - have;
    return { have, after: have + change, change };
  }
  function paintLine(tr) {
    const l = rows[num(tr.dataset.i)];
    const it = byId[l.item_id];
    const fx = effect(l);
    const unit = it && !it.kg ? ' ' + esc(it.unit) : ' kg';
    $('[data-c=have]', tr).innerHTML = fx ? `<span class="${fx.have < -0.0005 ? 'neg' : ''}">${qty(fx.have)}${unit}</span>` : '';
    let after = '';
    if (fx && fx.after !== null) {
      after = `<b class="${fx.after < -0.0005 ? 'neg' : ''}">${qty(fx.after)}${unit}</b>` +
        (kindNow === 'COUNT' ? `<div class="subfig">${Math.abs(fx.change) < 0.0005
          ? 'no change' : (fx.change > 0 ? '+' : '−') + qty(Math.abs(fx.change)) + unit}</div>`
          : fx.after < -0.0005 ? '<div class="subfig neg">more than there is</div>' : '');
    }
    $('[data-c=after]', tr).innerHTML = after;
    $('[data-f=item]', tr).classList.toggle('wrong', !l.item_id && typed($('[data-f=item]', tr).value));
  }
  /** Save says what it will do, in the words of the choice at the top. */
  function total() {
    let plus = 0, minus = 0, n = 0;
    rows.forEach(l => {
      const fx = effect(l);
      if (fx && fx.after !== null) {
        n++;
        if (fx.change > 0) plus += fx.change; else minus -= fx.change;
      }
    });
    if (!saveBtn) return;
    const k = STOCK_KIND[kindNow];
    saveBtn.textContent = !n ? (id ? 'Save changes' : 'Save')
      : kindNow === 'ADD' ? 'Add ' + qty(plus) + ' kg to ' + firmCode()
      : kindNow === 'REMOVE' ? 'Remove ' + qty(minus) + ' kg from ' + firmCode()
      : k.done + ' ' + (n === 1 ? 'the stock' : n + ' materials') + ' in ' + firmCode();
  }
  const lineHtml = (l, i) => `<tr data-i="${i}">
      <td class="c" style="color:var(--muted);font-size:12px">${i + 1}</td>
      <td><input data-f="item" autocomplete="off" spellcheck="false" value="${esc(l.name || '')}"
        placeholder="Type or click to choose…" style="width:100%"></td>
      <td class="r num" data-c="have"></td>
      <td><input data-f="weight" class="num" value="${esc(String(l.weight === null ? '' : l.weight))}" placeholder="kg"></td>
      <td class="r num" data-c="after"></td>
      <td class="c"><button type="button" class="link" data-drop style="color:var(--bad)" title="Take this line off">✕</button></td></tr>`;

  /** Words typed, each found somewhere in the name, HSN or code. */
  function matches(term) {
    const words = term.toLowerCase().split(/\s+/).filter(Boolean);
    return items.filter(it => {
      const hay = (it.name + ' ' + it.hsn + ' ' + it.code).toLowerCase();
      return words.every(w => hay.indexOf(w) >= 0);
    }).slice(0, 60);
  }
  const exact = t => {
    const want = String(t || '').replace(/\s+/g, ' ').trim().toLowerCase();
    return want ? items.find(it => it.name.replace(/\s+/g, ' ').trim().toLowerCase() === want) : null;
  };
  function drawLines(focusI, onKg) {
    $('#sk-lines').innerHTML = rows.map(lineHtml).join('');
    $$('#sk-lines tr').forEach(tr => {
      const l = rows[num(tr.dataset.i)];
      const box = $('[data-f=item]', tr), kg = $('[data-f=weight]', tr);
      const take = it => {
        l.item_id = it ? it.id : null;
        l.name = it ? it.name : box.value;
        l.hsn = it ? it.hsn : '';
        if (it) box.value = it.name;
        paintLine(tr);
        total();
      };
      const picker = attachPicker(box, {
        maxHeight: 340,
        source: async term => matches(term),
        render: it => `<span style="float:right;margin-left:12px" class="${num(it.stock) < -0.0005 ? 'neg'
            : Math.abs(num(it.stock)) < 0.0005 ? '' : 'pos'}">${qty(it.stock)} ${esc(it.unit)}</span>
          <b>${esc(it.name)}</b>${it.hsn ? ' <span style="color:var(--muted);font-size:12px">' + esc(it.hsn) + '</span>' : ''}`,
        onPick: it => { take(it); kg.focus(); kg.select(); }
      });
      box.addEventListener('click', () => { if (!picker.isOpen()) picker.refresh(); });
      box.addEventListener('input', () => { l.item_id = null; paintLine(tr); total(); });
      box.addEventListener('change', () => { if (!l.item_id) take(exact(box.value)); });
      kg.addEventListener('input', () => { l.weight = kg.value; paintLine(tr); total(); });
      // Enter in the last line's kilos, with kilos in it, is the next line.
      kg.addEventListener('keydown', ev => {
        if (ev.key !== 'Enter' || ev.shiftKey || ev.ctrlKey || ev.altKey) return;
        if (num(tr.dataset.i) !== rows.length - 1 || !kg.value.trim()) return;
        ev.preventDefault();
        addLine();
      });
      $('[data-drop]', tr).onclick = () => {
        rows.splice(num(tr.dataset.i), 1);
        if (!rows.length) rows.push(blank());
        drawLines(Math.min(num(tr.dataset.i), rows.length - 1));
      };
      paintLine(tr);
    });
    total();
    if (focusI !== undefined) {
      const tr = $$('#sk-lines tr')[focusI];
      if (tr) $(onKg ? '[data-f=weight]' : '[data-f=item]', tr).focus();
    }
  }
  function addLine() {
    rows.push(blank());
    drawLines(rows.length - 1);
  }
  /** The firm's materials with what each held on the date -- again whenever
   *  the firm or the date changes. A line keeps its material in the other
   *  firm when that firm has one of the same name, or the only one of the
   *  same HSN. */
  async function loadItems() {
    const d = await api.get('/api/stock/items', { firm: firmNow,
      as_on: $('#sk-date').value || today(), skip: id || '' });
    items = d.items;
    byId = {};
    items.forEach(it => { byId[it.id] = it; });
    rows.forEach(l => {
      if (l.item_id && byId[l.item_id]) { l.name = byId[l.item_id].name; l.hsn = byId[l.item_id].hsn; return; }
      const same = exact(l.name) || (l.hsn && items.filter(it => it.hsn === l.hsn).length === 1
        ? items.find(it => it.hsn === l.hsn) : null);
      l.item_id = same ? same.id : null;
      if (same) { l.name = same.name; l.hsn = same.hsn; }
    });
    drawLines();
  }
  function paintChoices() {
    const k = STOCK_KIND[kindNow];
    $$('#sk-kinds button').forEach(b => b.classList.toggle('on', b.dataset.kind === kindNow));
    $$('#sk-firms button').forEach(b => b.classList.toggle('on', num(b.dataset.firm) === num(firmNow)));
    $('#sk-say').textContent = k.say;
    $('#sk-boxhead').textContent = k.box;
    $('#sk-reasons').innerHTML = k.reasons.map(r => `<option value="${esc(r)}">`).join('');
    const h = dlg && $('h2', dlg);
    if (h && !id && h.firstChild) h.firstChild.textContent = k.title;
    $$('#sk-lines tr').forEach(paintLine);
    total();
  }

  const buttons = [{ label: 'Cancel', value: null }];
  if (id) {
    buttons.push({ label: 'Delete', cls: 'danger', before: async () => {
      if (!await confirmBox('Delete this change', 'Delete this change? The stock goes back to '
          + 'what it was without it.')) return false;
      try {
        await api.del('/api/stock/entries/' + id);
        ok('Change deleted — the stock is back as it was.');
        changed = true;
        return true;
      } catch (err) { bad(err.message); return false; }
    } });
  }
  buttons.push({ label: 'Save', cls: 'primary', before: async () => {
    const payload = {
      kind: kindNow, firm: firmNow,
      entry_date: $('#sk-date').value, reason: $('#sk-reason').value,
      note: $('#sk-note').value,
      lines: rows.map(l => ({ item_id: l.item_id,
        weight: String(l.weight === null ? '' : l.weight).replace(/,/g, '') }))
    };
    // A name typed that is not one of the firm's materials is said here,
    // by name, rather than as a line number from the server.
    const lost = rows.find(l => !l.item_id && typed(l.name));
    if (lost) {
      $('#sk-err').innerHTML = '<b style="color:var(--bad)">"' + esc(lost.name)
        + '" is not one of ' + esc(firmCode()) + '\'s materials — choose it from the list.</b>';
      return false;
    }
    try {
      const saved = id ? await api.put('/api/stock/entries/' + id, payload)
                       : await api.post('/api/stock/entries', payload);
      const bits = [];
      if (saved.added) bits.push('+' + qty(saved.added) + ' kg');
      if (saved.removed) bits.push('−' + qty(saved.removed) + ' kg');
      ok(saved.what + ' in ' + saved.firm_code + (bits.length ? ': ' + bits.join(', ') : ': no change') + '.');
      changed = true;
      return true;
    } catch (err) {
      $('#sk-err').innerHTML = '<b style="color:var(--bad)">' + esc(err.message) + '</b>';
      bad(err.message);
      return false;
    }
  } });

  await modal(id ? e.what + ' — ' + dt(e.entry_date) + ' (' + e.firm_code + ')'
      : STOCK_KIND[kindNow].title, body, buttons, {
    wide: true,
    onOpen: bg => {
      dlg = bg;
      saveBtn = $('.ft button.primary', bg);
      $('#sk-add').onclick = addLine;
      $$('#sk-kinds button').forEach(b => {
        b.onclick = () => { kindNow = b.dataset.kind; paintChoices(); };
      });
      $$('#sk-firms button').forEach(b => {
        b.onclick = () => {
          if (num(b.dataset.firm) === num(firmNow)) return;
          firmNow = num(b.dataset.firm);
          paintChoices();
          loadItems().then(paintChoices).catch(err => bad(err.message));
        };
      });
      $('#sk-date').addEventListener('change', () => { loadItems().then(paintChoices).catch(err => bad(err.message)); });
      drawLines();
      paintChoices();
      loadItems().then(() => {
        paintChoices();
        // A new change starts on its first material -- or, opened from a
        // material's own line, on the kilos.
        if (!id) drawLines(0, !!(preset && preset.item_id));
      }).catch(err => bad(err.message));
    }
  });
  return changed;
}

/* ================================================================= stock ---
   **The material stands in the two GST firms and nowhere else.** Every
   purchase is bought into one of them and every sale is billed out of one of
   them, so those are the books that hold steel. Read together the rows
   gather by HSN; one firm at a time is that firm's own item list.

   **Anyone should be able to use it** (27 Sep 2026): the screen answers
   "how much do I have" first -- the weight, each firm's share, a line per
   material -- and every line carries its own Add / Remove / Correct. The
   opening, purchased, sold and value columns are one tick away, under Show
   details; what was changed by hand is its own tab.                        */
route(/^\/reports\/stock$/, async (view, _m, q) => {
  const change = async (id, kind, firm, preset) => {
    if (await stockEntryDialog(id, kind, firm || $('#sf').value, preset)) load();
  };
  setTitle('Billing Stock', 'Steel held by the two GST firms — BST (Balkrishana Steel Traders) and BSI (Balkrishna Steel Industries)', [
    { label: '＋ Add stock', cls: 'primary', onClick: () => change(null, 'ADD') },
    { label: '− Remove stock', onClick: () => change(null, 'REMOVE') },
    { label: '✎ Correct stock', onClick: () => change(null, 'COUNT') }
  ]);

  view.innerHTML = filterBar(`
    <div><label>Firm</label><select id="sf" style="width:calc(300px * var(--typescale))">
      <option value="">Both firms</option>
    </select></div>
    <div style="flex:1;min-width:200px;max-width:320px"><label>Find</label>
      <input id="q" type="search" placeholder="Material name or HSN…" autocomplete="off"></div>
    <div><label>&nbsp;</label><label class="tickline"><input type="checkbox" id="sdet"> Show details</label></div>
    <select id="sv" hidden><option value="stock"></option><option value="entries"></option></select>
    <input type="hidden" id="ssel"><input type="hidden" id="schip"><input type="hidden" id="scat">`);
  const mem = screenState('/reports/stock',
    { firm: '#sf', view: '#sv', find: '#q', details: '#sdet', show: '#schip', sel: '#ssel',
      cat: '#scat', from: '#f1', to: '#f2', fy: fyField('f1', 'f2') }).restore(q);

  const params = () => ({ firm: $('#sf').value, from: $('#f1').value,
                          to: $('#f2').value });

  /* An HSN bought into both books under two different product names shows
     both, but a code that half a dozen names sit under would run the column
     off the page. */
  const naming = g => {
    const all = g.names || [g.name];
    return esc(all.slice(0, 2).join(' / ')) + (all.length > 2
      ? ' <span style="color:var(--muted)">+' + (all.length - 2) + ' more</span>'
      : '');
  };
  const kg = v => qty(v) + ' kg';
  const sum = (list, k) => list.reduce((a, x) => a + num(x[k]), 0);
  const KG = ' <span style="text-transform:none;color:var(--muted)">kg</span>';
  const per = x => (x.kg ? '' : ' <span style="color:var(--muted);font-size:12px">'
    + esc(x.stock_unit || x.unit) + '</span>');
  const hsnLine = h => (h ? `<div style="font-family:var(--mono);font-size:12px;color:var(--muted)">HSN ${esc(h)}</div>` : '');
  const signed = v => (Math.abs(num(v)) < 0.0005 ? '<span style="color:var(--muted)">—</span>'
    : (num(v) > 0 ? '+' : '−') + qty(Math.abs(num(v))));
  // A figure below nothing is stock billed that was never bought in.
  const held = (v, low) => `<b${num(v) < -0.0005 || low
    ? ' style="color:var(--bad)"' : ''}>${qty(v)}</b>`;
  const details = () => $('#sdet').checked;
  /* Most of the item list is names never stocked, or nothing is left of and
     nothing moved in these dates: all noughts. One line at the foot until
     asked for. */
  let showIdle = false, last = null;
  const idle = x => !['opening', 'purchased', 'sold', 'stock', 'added', 'removed']
    .some(k => Math.abs(num(x[k])) > 0.0005);
  const found = list => {
    const words = $('#q').value.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return list;
    return list.filter(x => {
      const hay = ((x.names || [x.name]).join(' ') + ' ' + (x.hsn || '') + ' ' + (x.code || '')
        + ' ' + (x.stock_cat || '')).toLowerCase();
      return words.every(w => hay.indexOf(w) >= 0);
    });
  };
  /* A line's own buttons. In both firms together a line is an HSN, so they
     open in the firm holding most of it (the form can switch). */
  // With the details showing there is no room for the words, and the
  // buttons keep their signs alone (the words stay as their tooltips).
  const acts = (firm, item) => {
    const w = !details();
    return `<td class="r nw rowacts">
      <button type="button" class="sm" data-do="ADD" data-firm="${firm}" data-item="${item}" title="Add stock of this material">＋${w ? ' Add' : ''}</button>
      <button type="button" class="sm" data-do="REMOVE" data-firm="${firm}" data-item="${item}" title="Take stock of this material out">−${w ? ' Remove' : ''}</button>
      <button type="button" class="sm" data-do="COUNT" data-firm="${firm}" data-item="${item}" title="Correct: set this material to the real weight you have">✎${w ? ' Correct' : ''}</button></td>`;
  };
  function groupPick(g, d) {
    const want = $('#sf').value;
    const ids = Object.keys(g.pick || {});
    let fid = want && g.pick[want] ? want : ids.sort((a, b) =>
      Math.abs(num(g.firms[b])) - Math.abs(num(g.firms[a])))[0];
    return fid ? [fid, g.pick[fid]] : [d.firms[0].id, ''];
  }

  /* **Categories** (4 Oct 2026: "Make categories for Billing Stock - GP
     SHEET, HR SHEET, CRCA SHEET, COLOUR COATED SHEET, MS PLATE, MS ANGLE,
     MS PIPE"). A bar of them with the weight each holds, which is also the
     filter; in All categories each line stands under its category's own
     heading and subtotal. A material is moved from its details. */
  const NONE = '(none)';
  const catSel = () => $('#scat').value;
  const inCat = x => !catSel() || (catSel() === NONE ? !x.stock_cat : x.stock_cat === catSel());
  function catBar(d) {
    const btn = (val, label, w, tip) => `<button type="button" data-cat="${esc(val)}"${
      catSel() === val ? ' class="on"' : ''} title="${esc(tip)}"><span class="cn">${esc(label)}</span>
        <span class="ck">${esc(kg(w))}</span></button>`;
    return `<div class="catbar" role="group" aria-label="Category">
      ${btn('', 'All categories', d.kg, 'Every material')}
      ${(d.cats || []).map(c => btn(c.name || NONE, c.name || 'No category', c.kg,
        (c.items ? c.items + ' material' + (c.items === 1 ? '' : 's') + ' in stock' : 'Nothing in stock')
        + (c.name ? '' : ' — not put in a category yet'))).join('')}
      <button type="button" class="catedit" data-cats title="Add, rename or remove categories">✎ Categories</button></div>`;
  }
  /* In All categories, every category's lines under its heading, in the
     list's order, with what has none last. */
  function withHeads(d, list, catRow, line) {
    const shown = list.filter(x => showIdle || !idle(x));
    if (catSel()) return shown.map(line).join('');
    return (d.cat_list || []).concat(['']).map(name => {
      const part = shown.filter(x => (x.stock_cat || '') === name);
      return part.length ? catRow(name, part) + part.map(line).join('') : '';
    }).join('');
  }
  function bindCats(d) {
    $$('#out [data-cat]').forEach(b => {
      b.onclick = () => {
        const v = b.dataset.cat;
        $('#scat').value = v;
        mem.save();
        draw(d);
        const again = $$('#out .catbar [data-cat]').find(x => x.dataset.cat === v);
        if (again) again.focus();
      };
    });
    const ed = $('#out [data-cats]');
    if (ed) ed.onclick = async () => { if (await stockCatsDialog()) load(); };
  }

  function renderGrouped(d, list) {
    const firms = d.firms, det = details();
    const catRow = (name, part) => `<tr class="cathead clickable" data-cat="${esc(name || NONE)}" title="Show only ${esc(name || 'what has no category')}">
        <td colspan="${det ? 5 : 1}"><b>${esc(name || 'No category')}</b>
          <span class="catn">${part.length} line${part.length === 1 ? '' : 's'}</span></td>
        <td class="r num"><b>${qty(sum(part.filter(x => x.kg), 'stock'))}</b></td>
        ${firms.map(f => {
          const w = part.filter(x => x.kg).reduce((a, x) => a + num(x.firms[f.id]), 0);
          return `<td class="r num share">${Math.abs(w) < 0.0005 ? '—' : qty(w)}</td>`;
        }).join('')}
        ${det ? `<td class="r num">${fmt(sum(part, 'value'), 0)}</td>` : ''}<td></td></tr>`;
    const head = det
      ? `<th class="r">Opening${KG}</th><th class="r">Purchased${KG}</th><th class="r">Sold${KG}</th>
         <th class="r" title="Added (+) or taken out (−) with the buttons on this screen">By you${KG}</th>`
      : '';
    return `<table class="stocktbl${det ? ' byhand' : ''}"><thead><tr><th>Material</th>${head}
      <th class="r">In stock${KG}</th>
      ${firms.map(f => `<th class="r" title="${esc(f.name)}">${esc(f.code)}${KG}</th>`).join('')}
      ${det ? '<th class="r">Value ₹</th>' : ''}<th class="r">${det ? '' : 'Change stock'}</th></tr></thead><tbody>` +
      withHeads(d, list, catRow, g => {
        const [fid, iid] = groupPick(g, d);
        return `<tr data-key="${esc(keyOf(d, g))}"><td><b>${naming(g)}</b>
        ${g.low ? ' <span class="tag bad">low</span>' : ''}${hsnLine(g.hsn)}</td>
        ${det ? `<td class="r num">${qty(g.opening)}${per(g)}</td>
        <td class="r num">${qty(g.purchased)}${per(g)}</td>
        <td class="r num">${qty(g.sold)}${per(g)}</td>
        <td class="r num">${signed(num(g.added) - num(g.removed))}</td>` : ''}
        <td class="r num">${held(g.stock, g.low)}${per(g)}</td>
        ${firms.map(f => `<td class="r num share">${
          num(g.firms[f.id]) ? qty(g.firms[f.id]) + per(g) : '—'}</td>`).join('')}
        ${det ? `<td class="r num">${fmt(g.value, 0)}</td>` : ''}
        ${acts(fid, iid)}</tr>`;
      }) +
      `</tbody><tfoot><tr><td>Total, in kg</td>
        ${det ? kgFoot(list) : ''}
        <td class="r num"><b>${qty(sum(list.filter(x => x.kg), 'stock'))}</b></td>
        ${firms.map(f => `<td class="r num share">${qty(list.filter(x => x.kg)
          .reduce((a, x) => a + num(x.firms[f.id]), 0))}</td>`).join('')}
        ${det ? `<td class="r num">${fmt(sum(list, 'value'), 0)}</td>` : ''}<td></td></tr></tfoot></table>`;
  }
  const kgFoot = list => {
    const w = list.filter(x => x.kg);
    return `<td class="r num">${qty(sum(w, 'opening'))}</td>
      <td class="r num">${qty(sum(w, 'purchased'))}</td>
      <td class="r num">${qty(sum(w, 'sold'))}</td>
      <td class="r num">${signed(sum(w, 'added') - sum(w, 'removed'))}</td>`;
  };

  function renderOne(d, list) {
    const det = details(), fid = d.per_firm[0].id;
    const catRow = (name, part) => `<tr class="cathead clickable" data-cat="${esc(name || NONE)}" title="Show only ${esc(name || 'what has no category')}">
        <td colspan="${det ? 5 : 1}"><b>${esc(name || 'No category')}</b>
          <span class="catn">${part.length} line${part.length === 1 ? '' : 's'}</span></td>
        <td class="r num"><b>${qty(sum(part.filter(x => x.kg), 'stock'))}</b></td>
        ${det ? `<td></td><td class="r num">${fmt(sum(part, 'value'), 0)}</td>` : ''}<td></td></tr>`;
    return `<table class="stocktbl${det ? ' byhand' : ''}"><thead><tr><th>Material</th>
      ${det ? `<th class="r">Opening${KG}</th><th class="r">Purchased${KG}</th><th class="r">Sold${KG}</th>
        <th class="r" title="Added (+) or taken out (−) with the buttons on this screen">By you${KG}</th>` : ''}
      <th class="r">In stock${KG}</th>
      ${det ? '<th class="r">Rate</th><th class="r">Value ₹</th>' : ''}<th class="r">${det ? '' : 'Change stock'}</th></tr></thead><tbody>` +
      withHeads(d, list, catRow, r => `<tr data-key="${esc(keyOf(d, r))}"><td><b>${esc(r.name)}</b>
        ${r.low ? ' <span class="tag bad">low</span>' : ''}${hsnLine(r.hsn)}</td>
        ${det ? `<td class="r num">${r.tracked ? qty(r.opening) + per(r) : '—'}</td>
        <td class="r num">${qty(r.purchased)}${per(r)}</td>
        <td class="r num">${qty(r.sold)}${per(r)}</td>
        <td class="r num">${signed(num(r.added) - num(r.removed))}</td>` : ''}
        <td class="r num">${r.tracked ? held(r.stock, r.low) + per(r)
          : '<span style="color:var(--muted)">not tracked</span>'}</td>
        ${det ? `<td class="r num">${fmt(r.rate)}</td><td class="r num">${fmt(r.value, 0)}</td>` : ''}
        ${r.tracked ? acts(fid, r.id) : '<td></td>'}</tr>`) +
      `</tbody><tfoot><tr><td>Total, in kg</td>${det ? kgFoot(list) : ''}
        <td class="r num"><b>${qty(sum(list.filter(x => x.kg), 'stock'))}</b></td>
        ${det ? `<td></td><td class="r num">${fmt(sum(list, 'value'), 0)}</td>` : ''}<td></td></tr></tfoot></table>`;
  }

  /** The two tabs: what is in stock, and the changes made on this screen. */
  function tabs(active, n) {
    return `<div class="seg stocktabs" role="group" aria-label="Show">
      <button type="button" data-tab="stock"${active === 'stock' ? ' class="on"' : ''}>Stock in hand</button>
      <button type="button" data-tab="entries"${active === 'entries' ? ' class="on"' : ''}>Changes you made${
        n ? ' · ' + n : ''}</button></div>`;
  }
  function bindTabs() {
    $$('#out [data-tab]').forEach(b => {
      b.onclick = () => {
        if ($('#sv').value === b.dataset.tab) return;
        $('#sv').value = b.dataset.tab;
        load();
      };
    });
  }
  function bindActs() {
    $$('#out [data-do]').forEach(b => {
      b.onclick = ev => {
        ev.stopPropagation();
        change(null, b.dataset.do, b.dataset.firm, { item_id: num(b.dataset.item) || null });
      };
    });
  }

  async function load() {
    mem.save();
    $('#out').innerHTML = '<div class="card"><div class="empty">Counting…</div></div>';
    const here = onScreen();
    const entries = $('#sv').value === 'entries';
    // Downloading is the stock's; the changes are a short list on screen.
    $('#csv').style.display = entries ? 'none' : '';
    let d;
    try {
      d = await api.get(entries ? '/api/stock/entries' : '/api/reports/stock', params());
    } catch (e) {
      if (!here()) return;
      bad(e.message);
      $('#out').innerHTML = '<div class="card"><div class="empty">'
        + esc(e.message) + '</div></div>';
      return;
    }
    if (!here()) return;
    // Built once the firms are known, and left alone after that so the box
    // does not forget what it was set to on every refresh.
    if ($('#sf').options.length <= 1) {
      $('#sf').innerHTML = '<option value="">Both firms</option>' +
        d.firms.map(f => `<option value="${f.id}">${esc(f.code)} — ${esc(f.name)}</option>`).join('');
      $('#sf').value = q.firm || '';
    }
    last = d;
    if (entries) drawEntries(d); else draw(d);
  }

  /* Every change made by hand in the dates, newest first: opened to be
     changed, deleted one at a time or ticked and deleted together. */
  function drawEntries(d) {
    const small = t => '<div style="font-size:12px;color:var(--muted)">' + t + '</div>';
    const line = l => '<span class="nw">' + esc(l.name) + ' <span style="color:var(--muted)">'
      + (l.counted !== null && l.counted !== undefined
        ? 'set to ' + qty(l.counted) + ', ' : '')
      + signed(l.qty) + ' ' + esc(l.unit) + '</span></span>';
    const rows = found(d.rows.map(e => Object.assign({}, e, { names: e.lines.map(l => l.name),
      hsn: e.lines.map(l => l.hsn).join(' ') })));
    $('#out').innerHTML = `
      ${tabs('entries', d.count)}
      <p class="stocksay">Everything added, taken out or corrected with the buttons on this screen.
        <b>Delete</b> a change to undo it — the stock goes back as it was.</p>
      <div class="card"><div class="bd flush tbl-scroll" id="se-list">${rows.length ? `
      <table><thead><tr><th style="width:110px">Date</th><th style="width:70px">Firm</th>
        <th style="width:150px">What</th><th>Material</th>
        <th class="r" style="width:130px">Change${KG}</th><th>Why</th><th style="width:130px"></th></tr></thead><tbody>
      ${rows.map(e => `<tr class="clickable" data-id="${e.id}" title="Open this change">
        <td class="nw">${dt(e.entry_date)}</td>
        <td>${esc(e.firm_code)}</td>
        <td class="nw"><span class="tag ${e.kind === 'ADD' ? 'ok' : e.kind === 'REMOVE' ? 'bad' : 'info'}">${esc(e.what)}</span></td>
        <td style="font-size:13.5px">${e.lines.slice(0, 4).map(line).join('<br>')}${e.lines.length > 4
          ? '<br><span style="color:var(--muted)">+ ' + (e.lines.length - 4) + ' more</span>' : ''}</td>
        <td class="r num"><b>${signed(e.change)}</b></td>
        <td>${esc(e.reason || '—')}${e.note ? small(esc(e.note)) : ''}</td>
        <td class="r nw"><button class="sm" data-edit>Edit</button>
          <button class="sm danger" data-act="del">Delete</button></td></tr>`).join('')}
      </tbody></table>`
      : `<div class="empty"><b>No changes in these dates</b>
          Use ＋ Add stock, − Remove stock or ✎ Correct stock — at the top, or on a material's line.</div>`}
      </div></div>`;
    bindTabs();
    $$('#out tr[data-id]').forEach(tr => {
      tr.onclick = () => change(num(tr.dataset.id));
      $('[data-edit]', tr).onclick = ev => { ev.stopPropagation(); change(num(tr.dataset.id)); };
      // Deleted the way every list deletes: one question, a copy of the
      // books first.
      $('[data-act=del]', tr).onclick = async ev => {
        ev.stopPropagation();
        if (await bulkDelete('stock_entry', [num(tr.dataset.id)])) load();
      };
    });
    if (rows.length) bulkRows($('#se-list'), 'stock_entry', { reload: load });
  }

  /* **Quick filters and item details, the way other billing programs do
     it** (27 Sep 2026: "check online for billing softwares and have a look
     at their stock management page"). Vyapar, myBillBook and Zoho all show a
     searchable list with the stock beside each name, a filter for what is
     low, and -- on a click -- that item's own page: its stock, an Adjust
     button, and every purchase, sale and adjustment with the stock after
     each. Here the list narrows to the left and the material opens on the
     right; ✕ or Esc puts the list back. */
  const keyOf = (d, x) => (d.one_firm ? 'item:' + x.id : x.key);
  const chip = () => $('#schip').value || 'all';
  const chipped = list => list.filter(x => chip() === 'in' ? num(x.stock) > 0.0005
    : chip() === 'below' ? num(x.stock) < -0.0005 : true);
  const chips = (list) => {
    const n = k => list.filter(x => k === 'in' ? num(x.stock) > 0.0005
      : k === 'below' ? num(x.stock) < -0.0005 : !idle(x)).length;
    return `<div class="seg stockchips" role="group" aria-label="Which materials">
      <button type="button" data-chip="all"${chip() === 'all' ? ' class="on"' : ''}>All · ${n('all')}</button>
      <button type="button" data-chip="in"${chip() === 'in' ? ' class="on"' : ''}>In stock · ${n('in')}</button>
      <button type="button" data-chip="below"${chip() === 'below' ? ' class="on"' : ''}>Below zero · ${n('below')}</button></div>`;
  };
  function bindChips(d) {
    $$('#out [data-chip]').forEach(b => {
      b.onclick = () => { $('#schip').value = b.dataset.chip; mem.save(); draw(d); };
    });
  }
  const pickFor = (d, x) => (d.one_firm ? [d.per_firm[0].id, x.id] : groupPick(x, d));
  function closeSel() {
    $('#ssel').value = '';
    mem.save();
    if (last && $('#sv').value !== 'entries') draw(last);
  }
  // Set again after each drawing, so the keys along the foot say what Esc
  // does now: close the material, or leave the screen.
  const escHook = () => KB.setEscape(() => !!$('#ssel').value && $('#sv').value !== 'entries', closeSel);
  escHook();

  function draw(d) {
    const low = (d.one_firm ? d.rows : d.groups).filter(r => r.low).length;
    const when = d.as_on ? 'on <span class="nw">' + dt(d.as_on) + '</span>' : 'now';
    const short = d.short || [];
    const others = d.others || [];
    // Most held first, in one firm as in both -- what is on the floor is
    // what is looked for.
    const all = found(d.one_firm ? d.rows.slice().sort((x, y) =>
      Math.abs(num(y.stock)) - Math.abs(num(x.stock)) || x.name.localeCompare(y.name)) : d.groups);
    // A category renamed or removed since it was chosen shows everything.
    if (catSel() && !(d.cats || []).some(c => (c.name || NONE) === catSel())) $('#scat').value = '';
    const inc = all.filter(inCat);
    const list = chipped(inc);
    const idleN = chip() === 'all' ? list.filter(idle).length : 0;
    const searching = !!$('#q').value.trim();
    const sel = $('#ssel').value;
    const chosen = sel ? all.find(x => keyOf(d, x) === sel) : null;
    if (sel && !chosen) $('#ssel').value = '';
    const help = `<p class="stocksay">Purchases add to this stock and bills take it out by themselves.
        When the real weight is different — damaged, scrap, weighed again — use
        <b>＋ Add</b>, <b>− Remove</b> or <b>✎ Correct</b>. Click a material to see everything that moved it.
        <a href="#/steel">Steel for challans is in Challan Stock →</a></p>`;
    if (chosen) {
      drawSplit(d, inc, list, chosen, help, when);
      return;
    }
    $('#out').innerHTML = `
      ${tabs('stock', d.entries)}
      ${help}
      <div class="kpis">
        <div class="kpi brand"><div class="lb">Steel in stock ${when}</div>
          <div class="vl">${esc(kg(d.kg))}</div>
          <div class="ft">${d.one_firm ? esc(d.per_firm[0].code) + ' · ' : 'both firms · '}worth ${money(d.total_value)}</div></div>
        ${d.one_firm ? '' : d.per_firm.map(f => `<div class="kpi"><div class="lb" title="${esc(f.name)}">${esc(f.code)} — ${esc(f.name)}</div>
          <div class="vl">${esc(kg(f.kg))}</div>
          <div class="ft">${f.items} material(s) in stock · ${money(f.value)}</div></div>`).join('')}
        ${low ? `<div class="kpi bad"><div class="lb">Below reorder level</div>
          <div class="vl">${low}</div><div class="ft">worth buying</div></div>` : ''}
      </div>
      ${short.length ? `<div class="card shortband"><div class="bd">
        <b>${short.length} material${short.length === 1 ? ' shows' : 's show'} less than nothing</b>
        — more was billed than bought (${esc(kg(d.short_kg))} in all). Press <b>Below zero</b> to see
        them; if you know the real weight, press <b>✎ Correct</b> on its line.</div></div>` : ''}
      ${catBar(d)}
      <div class="card"><div class="stmt-tools">${chips(inc)}<div class="spacer"></div>
        <span class="meta" style="color:var(--muted);font-size:13px">${list.length - (showIdle ? 0 : idleN)} shown</span></div>
      <div class="bd flush tbl-scroll">` +
      (list.length
        ? (d.one_firm ? renderOne(d, list) : renderGrouped(d, list))
        : `<div class="empty">${searching ? 'No material matches "' + esc($('#q').value) + '"'
            + (catSel() ? ' in ' + esc(catSel() === NONE ? 'No category' : catSel()) : '') + '.'
            : chip() === 'below' ? 'Nothing is below zero' + (catSel() ? ' here' : '') + '.'
            : catSel() === NONE ? 'Every material is in a category.'
            : catSel() ? 'No material is in ' + esc(catSel()) + ' yet — open a material and choose its Category.'
            : 'No materials to show.'}</div>`) + `</div>
      <div class="bd stockfoot">
        ${idleN ? `<div style="margin-bottom:6px">${showIdle
          ? idleN + ' line(s) above have nothing in stock and nothing moving in these dates.'
          : idleN + ' more with nothing in stock and nothing moving in these dates.'}
          <button type="button" class="sm" data-idle style="margin-left:8px">${
            showIdle ? 'Hide them' : 'Show them'}</button></div>` : ''}
        All weights are in kilos, as ${when.replace(/<[^>]+>/g, '')}. ${d.one_firm ? ''
          : 'A line is one HSN code within its category; BST and BSI are each firm\'s share of it. '}
        Tick <b>Show details</b> for the opening, purchased, sold and value.
        ${others.length ? '<br>Counted, not weighed, so not in the kilos: ' + others.map(x =>
          '<b>' + esc(x.name) + '</b> ' + esc(qty(x.stock) + ' ' + x.unit)).join(', ') + '.' : ''}
      </div></div>`;
    bindTabs();
    bindActs();
    bindChips(d);
    bindCats(d);
    // A line opens its material's details.
    $$('#out .stocktbl tbody tr[data-key]').forEach(tr => {
      tr.classList.add('clickable');
      tr.title = 'Open this material — its stock and everything that moved it';
      tr.onclick = () => { $('#ssel').value = tr.dataset.key; mem.save(); draw(d); };
    });
    const btn = $('#out [data-idle]');
    if (btn) btn.onclick = () => { showIdle = !showIdle; draw(d); $('#out [data-idle]').focus(); };
    escHook();
  }

  /** The list narrowed to the left, the chosen material's page on the right. */
  function drawSplit(d, all, list, x, help, when) {
    const [fid, iid] = pickFor(d, x);
    const shown = list.filter(y => !idle(y) || keyOf(d, y) === keyOf(d, x));
    const pickRow = y => `<tr class="clickable${keyOf(d, y) === keyOf(d, x) ? ' on' : ''}" data-key="${esc(keyOf(d, y))}">
            <td><b>${esc(y.names ? y.names[0] : y.name)}</b>${(y.names || []).length > 1
              ? ' <span style="color:var(--muted);font-size:12px">+' + (y.names.length - 1) + '</span>' : ''}${hsnLine(y.hsn)}</td>
            <td class="r num nw">${held(y.stock, y.low)}${per(y)}</td></tr>`;
    const pickHead = (name, part) => `<tr class="cathead clickable" data-cat="${esc(name || NONE)}" title="Show only ${esc(name || 'what has no category')}">
            <td><b>${esc(name || 'No category')}</b></td><td class="r num nw"><b>${qty(sum(part.filter(y => y.kg), 'stock'))}</b></td></tr>`;
    const base = x.stock_cat ? String(x.key || '').slice(('cat:' + x.stock_cat + '|').length) : x.key;
    $('#out').innerHTML = `
      ${tabs('stock', d.entries)}
      ${help}
      ${catBar(d)}
      <div class="stocksplit">
        <div class="card splitlist"><div class="stmt-tools">${chips(all)}</div>
          <div class="splitscroll"><table class="picklist"><tbody>
          ${catSel() ? shown.map(pickRow).join('') : (d.cat_list || []).concat(['']).map(name => {
            const part = shown.filter(y => (y.stock_cat || '') === name);
            return part.length ? pickHead(name, part) + part.map(pickRow).join('') : '';
          }).join('')}
          </tbody></table></div></div>
        <div class="card splitdetail">
          <div class="sdhead">
            <div style="min-width:0"><div class="sdname">${d.one_firm ? esc(x.name) : naming(x)}</div>
              <div class="sdsub">${x.hsn ? 'HSN ' + esc(x.hsn) + ' · ' : ''}${d.one_firm
                ? esc(d.per_firm[0].code) : 'BST and BSI together'}</div>
              <div class="sdcat"><label for="sd-cat">Category</label><select id="sd-cat"
                  title="The category this material is shown under${!d.one_firm && (x.ids || []).length > 1 ? ' — all ' + x.ids.length + ' products on this line move together' : ''}">
                <option value="">No category</option>
                ${(d.cat_list || []).map(c => `<option${c === (x.stock_cat || '') ? ' selected' : ''}>${esc(c)}</option>`).join('')}
              </select></div></div>
            <button type="button" class="sm" data-close title="Back to the whole list (Esc)">✕ Close</button>
          </div>
          <div class="sdfigs">
            <div class="sdfig main"><div class="lb">In stock ${when}</div><div class="vl">${qty(x.stock)} ${esc(x.kg ? 'kg' : x.stock_unit || x.unit)}</div></div>
            ${d.one_firm ? '' : d.firms.map(f => `<div class="sdfig"><div class="lb" title="${esc(f.name)}">${esc(f.code)}</div>
              <div class="vl">${num(x.firms[f.id]) ? qty(x.firms[f.id]) : '—'}</div></div>`).join('')}
            <div class="sdfig"><div class="lb">Value</div><div class="vl">${money(x.value)}</div></div>
          </div>
          <div class="sdacts">
            <button type="button" class="primary" data-do="ADD" data-firm="${fid}" data-item="${iid}">＋ Add stock</button>
            <button type="button" data-do="REMOVE" data-firm="${fid}" data-item="${iid}">− Remove stock</button>
            <button type="button" data-do="COUNT" data-firm="${fid}" data-item="${iid}">✎ Correct stock</button>
          </div>
          <div id="sd-moves"><div class="empty">Reading its history…</div></div>
        </div>
      </div>`;
    bindTabs();
    bindActs();
    bindChips(d);
    bindCats(d);
    $('#out [data-close]').onclick = closeSel;
    // Moving the material to another category. Its line's key carries the
    // category, so the details stay open on it in its new place.
    $('#sd-cat').onchange = async ev => {
      const cat = ev.target.value;
      try {
        await api.post('/api/stock/cat', { item_ids: d.one_firm ? [x.id] : x.ids, stock_cat: cat });
      } catch (e) {
        bad(e.message);
        ev.target.value = x.stock_cat || '';
        return;
      }
      ok((x.names ? x.names[0] : x.name) + (cat ? ' is now in ' + cat + '.' : ' is now in no category.'));
      if (!d.one_firm) $('#ssel').value = cat ? 'cat:' + cat + '|' + base : base;
      mem.save();
      await load();
      // Back on the box, so the next material is a click or an arrow away.
      const again = $('#sd-cat');
      if (again) again.focus();
    };
    $$('#out .picklist tr[data-key]').forEach(tr => {
      tr.onclick = () => { $('#ssel').value = tr.dataset.key; mem.save(); draw(d); };
    });
    const on = $('#out .picklist tr.on');
    if (on) on.scrollIntoView({ block: 'nearest' });
    escHook();
    loadMoves(d, x);
  }

  /** Every movement of the material in the dates, newest first, with the
   *  stock it left; the stock the dates began with at the foot. */
  async function loadMoves(d, x) {
    const here = onScreen();
    const key = keyOf(d, x);
    let m;
    try {
      m = await api.get('/api/stock/moves', Object.assign(
        d.one_firm ? { item: x.id } : { key: x.key, firm: $('#sf').value },
        { from: $('#f1').value, to: $('#f2').value }));
    } catch (e) {
      if (here() && $('#sd-moves')) $('#sd-moves').innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
      return;
    }
    if (!here() || $('#ssel').value !== key || !$('#sd-moves')) return;
    const both = m.firms.length > 1;
    const unit = m.kg ? 'kg' : m.unit;
    const tag = w => ({ Purchase: 'ok', 'Sales return': 'ok', 'Stock added': 'ok', Bill: 'info',
      'Stock removed': 'bad', 'Purchase return': 'bad', 'Stock corrected': 'warn' })[w] || '';
    $('#sd-moves').innerHTML = `
      <div class="sdsum">In these dates: <b>+${qty(m.total_in)}</b> in · <b>−${qty(m.total_out)}</b> out
        · ${m.moves.length} movement${m.moves.length === 1 ? '' : 's'}</div>
      <div class="tbl-scroll"><table class="sdmoves"><thead><tr><th>Date</th><th>What</th><th>No.</th>
        <th>Party / why</th>${both ? '<th>Firm</th>' : ''}<th class="r">In ${esc(unit)}</th>
        <th class="r">Out ${esc(unit)}</th><th class="r">Stock after</th></tr></thead><tbody>
      ${m.moves.slice().reverse().map(v => `<tr class="clickable" ${v.doc_id
          ? 'data-href="' + esc(docLink(v.kind, v.doc_id, v.firm_id)) + '" title="Open this ' + esc(v.what.toLowerCase()) + '"'
          : 'data-entry="' + v.entry_id + '" title="Open this change"'}>
        <td class="nw">${dt(v.date)}</td>
        <td class="nw"><span class="tag ${tag(v.what)}">${esc(v.what)}</span></td>
        <td class="nw">${v.doc_id ? goLink(docLink(v.kind, v.doc_id, v.firm_id), esc(v.no)) : ''}</td>
        <td>${esc(v.party || '—')}${v.counted !== null && v.counted !== undefined
          ? '<div class="subfig">weighed ' + qty(v.counted) + '</div>' : ''}</td>
        ${both ? '<td>' + esc(v.firm) + '</td>' : ''}
        <td class="r num">${v.kg_in ? qty(v.kg_in) : ''}</td>
        <td class="r num">${v.kg_out ? qty(v.kg_out) : ''}</td>
        <td class="r num"><b class="${v.balance < -0.0005 ? 'neg' : ''}">${qty(v.balance)}</b></td></tr>`).join('')}
      <tr class="bf"><td class="nw">${m.date_from ? dt(m.date_from) : ''}</td><td colspan="${both ? 6 : 5}"><i>Stock when these dates began</i></td>
        <td class="r num"><b>${qty(m.opening)}</b></td></tr>
      </tbody></table></div>`;
    $$('#sd-moves tr[data-entry]').forEach(tr => {
      tr.onclick = () => change(num(tr.dataset.entry));
    });
  }

  $('#go').onclick = load;
  $('#sf').onchange = load;
  $('#sdet').onchange = () => { mem.save(); if (last && $('#sv').value !== 'entries') draw(last); };
  // Finding narrows what is already drawn -- no asking the server again.
  $('#q').addEventListener('input', () => {
    mem.save();
    if (!last) return;
    if ($('#sv').value === 'entries') drawEntries(last); else draw(last);
  });
  bindFy(load, 'f1', 'f2');
  $('#csv').onclick = () => location.href =
    firmUrl('/api/export/stock?' + new URLSearchParams(params()));
  load();
});

/* Sales Register was this app's third telling of one list. `report_register`
   is `list_invoices` plus a total, which is the very query the Challans,
   Bills and Purchases screens run -- and those screens already download the
   register's own CSV, tax split and all. The screen is gone; the address
   still works and lands on the list it was a copy of. */
route(/^\/reports\/register$/, async (view, _m, q) => {
  const type = (q.doc_type || 'SALE').toUpperCase();
  location.replace(listFor(type));
});

route(/^\/reports\/gst$/, async (view, _m, q) => {
  setTitle('GST summary', 'Rate-wise, HSN-wise and party-wise break-up');
  view.innerHTML = filterBar(`<div><label>On</label><select id="dt" style="width:calc(160px * var(--typescale))">
    <option value="SALE">Outward (sales)</option>
    <option value="PURCHASE">Inward (purchases)</option></select></div>`);
  const mem = screenState('/reports/gst',
    { doc_type: '#dt', from: '#f1', to: '#f2', fy: fyField('f1', 'f2') }).restore(q);
  async function load() {
    mem.save();
    const here = onScreen();
    const d = await api.get('/api/reports/gst',
      { doc_type: $('#dt').value, from: $('#f1').value, to: $('#f2').value });
    if (!here()) return;
    const t = d.totals;
    const tbl = (title, head, rows, cells, foot) => `
      <div class="card"><h2>${title}</h2><div class="bd flush tbl-scroll">` +
      (rows.length ? `<table><thead><tr>${head}</tr></thead><tbody>` +
        rows.map(cells).join('') + '</tbody>' + (foot || '') + '</table>'
        : '<div class="empty">Nothing to show.</div>') + '</div></div>';

    $('#out').innerHTML = `
      <div class="kpis">
        <div class="kpi"><div class="lb">Taxable value</div><div class="vl">${money(t.taxable)}</div></div>
        <div class="kpi"><div class="lb">CGST</div><div class="vl">${money(t.cgst)}</div></div>
        <div class="kpi"><div class="lb">SGST</div><div class="vl">${money(t.sgst)}</div></div>
        <div class="kpi"><div class="lb">IGST</div><div class="vl">${money(t.igst)}</div></div>
        <div class="kpi brand"><div class="lb">Total tax</div><div class="vl">${money(t.tax_total)}</div></div>
      </div>` +
      tbl('Rate-wise summary',
        '<th class="c">GST rate</th><th class="r">Taxable</th><th class="r">CGST</th><th class="r">SGST</th><th class="r">IGST</th><th class="r">Cess</th>',
        d.rate_wise, r => `<tr><td class="c"><b>${fmt(r.rate, 2).replace(/\.00$/, '')}%</b></td>
          <td class="r num">${fmt(r.taxable)}</td><td class="r num">${fmt(r.cgst)}</td>
          <td class="r num">${fmt(r.sgst)}</td><td class="r num">${fmt(r.igst)}</td>
          <td class="r num">${fmt(r.cess)}</td></tr>`,
        `<tfoot><tr><td>Total</td><td class="r num">${fmt(t.taxable)}</td>
          <td class="r num">${fmt(t.cgst)}</td><td class="r num">${fmt(t.sgst)}</td>
          <td class="r num">${fmt(t.igst)}</td><td class="r num">${fmt(t.cess)}</td></tr></tfoot>`) +
      tbl('HSN-wise summary <span class="rt"><a class="btn sm" href="/api/export/gst-hsn?' +
        new URLSearchParams({ doc_type: $('#dt').value, from: $('#f1').value, to: $('#f2').value }) + '">⤓ CSV</a></span>',
        '<th>HSN</th><th class="c">Unit</th><th class="r">Qty</th><th class="r">Taxable</th><th class="r">CGST</th><th class="r">SGST</th><th class="r">IGST</th><th class="r">Total</th>',
        d.hsn_wise, r => `<tr><td style="font-family:var(--mono)">${esc(r.hsn)}</td>
          <td class="c">${esc(r.unit || '')}</td><td class="r num">${qty(r.qty)}</td>
          <td class="r num">${fmt(r.taxable)}</td><td class="r num">${fmt(r.cgst)}</td>
          <td class="r num">${fmt(r.sgst)}</td><td class="r num">${fmt(r.igst)}</td>
          <td class="r num">${fmt(r.total)}</td></tr>`) +
      tbl('B2B — registered parties <span class="rt"><a class="btn sm" href="/api/export/gst-b2b?' +
        new URLSearchParams({ doc_type: $('#dt').value, from: $('#f1').value, to: $('#f2').value }) + '">⤓ CSV</a></span>',
        '<th>Party</th><th>GSTIN</th><th>State</th><th class="c">Docs</th><th class="r">Taxable</th><th class="r">Tax</th><th class="r">Total</th>',
        d.b2b, r => `<tr><td>${goLink(accountLink(r.party_id), esc(r.party), 'Open the account')}</td>
          <td style="font-family:var(--mono);font-size:12px">${esc(r.gstin)}</td>
          <td>${esc(r.state || '')}</td><td class="c">${r.invoices}</td>
          <td class="r num">${fmt(r.taxable)}</td>
          <td class="r num">${fmt(r.cgst + r.sgst + r.igst + r.cess)}</td>
          <td class="r num">${fmt(r.total)}</td></tr>`) +
      (d.b2c && d.b2c.invoices ? `<div class="card"><h2>B2C — unregistered parties</h2><div class="bd">
        <div class="totbox" style="max-width:340px">
          <div class="lb">Documents</div><div class="vl">${d.b2c.invoices}</div>
          <div class="lb">Taxable</div><div class="vl">${fmt(d.b2c.taxable)}</div>
          <div class="lb">Tax</div><div class="vl">${fmt(d.b2c.cgst + d.b2c.sgst + d.b2c.igst + d.b2c.cess)}</div>
          <div class="lb grand">Total</div><div class="vl grand">${fmt(d.b2c.total)}</div>
        </div></div></div>` : '');
  }
  $('#dt').onchange = load;
  $('#go').onclick = load;
  bindFy(load, 'f1', 'f2');
  $('#csv').onclick = () => location.href = firmUrl('/api/export/gst-hsn?' + new URLSearchParams(
    { doc_type: $('#dt').value, from: $('#f1').value, to: $('#f2').value }));
  load();
});

route(/^\/reports\/daybook$/, async (view, _m, q) => {
  setTitle('Day book', 'Every document, in date order');
  view.innerHTML = filterBar('');
  $('#f1').value = monthStart();
  const mem = screenState('/reports/daybook',
    { from: '#f1', to: '#f2', fy: fyField('f1', 'f2') }).restore(q);
  async function load() {
    mem.save();
    const here = onScreen();
    const d = await api.get('/api/reports/daybook', { from: $('#f1').value, to: $('#f2').value });
    if (!here()) return;
    $('#out').innerHTML = `<div class="card"><h2>${d.count} entries · ${money(d.total)}</h2>
      <div class="bd flush tbl-scroll">` +
      (d.rows.length ? `<table><thead><tr><th>Date</th><th>Type</th><th>No</th>
        <th>Party</th><th>Mode</th><th class="r">Amount</th></tr></thead><tbody>` +
        d.rows.map(r => `<tr data-href="${esc(docLink(r.type, r.id, r.company_id))}">
          <td class="nw">${dt(r.date)}</td>
          <td><span class="tag ${r.type === 'SALE' ? 'info' : r.type === 'RECEIPT' ? 'ok' : ''}">${esc(r.type)}</span></td>
          <td><b>${goLink(docLink(r.type, r.id, r.company_id), esc(r.no))}</b></td>
          <td>${goLink(accountLink(r.party_id), esc(r.party), 'Open the account')}</td>
          <td>${esc(r.mode || '')}</td>
          <td class="r num">${fmt(r.amount)}</td></tr>`).join('') +
        `</tbody><tfoot><tr><td colspan="5">Total</td><td class="r num">${fmt(d.total)}</td></tr></tfoot></table>`
        : '<div class="empty">No activity in this period.</div>') + '</div></div>';
  }
  $('#go').onclick = load;
  bindFy(load, 'f1', 'f2');
  $('#csv').onclick = () => location.href = firmUrl('/api/export/daybook?' +
    new URLSearchParams({ from: $('#f1').value, to: $('#f2').value }));
  load();
});

/* ------------------------------------------------- accounting reports --- */
/* --------------------------------------------------------- data health -- */
/* Every figure on screen comes from three places that have to agree: the
   document header, the lines under it, and the double-entry rows. This asks
   whether they do, and says where they do not. Nothing here writes. */
route(/^\/reports\/health$/, async (view, _m, q) => {
  setTitle('Data health', 'Whether the books agree with themselves');
  view.innerHTML = `<div class="card"><div class="bd"><div class="row">
      <div><label>Look at</label><select id="scope">
        <option value="company">This firm</option>
        <option value="all">Every firm</option>
      </select></div>
      <div class="spacer"></div>
      <button id="go" class="primary">Check again</button>
    </div></div></div><div id="out"><div class="empty">Checking…</div></div>`;

  const LEVEL = { bad: ['bad', '!', 'to put right'],
                  warn: ['warn', '?', 'worth knowing'],
                  ok: ['good', '✓', 'clean'] };

  const mem = screenState('/reports/health', { scope: '#scope' }).restore(q);

  async function load() {
    mem.save();
    $('#out').innerHTML = '<div class="empty">Checking…</div>';
    const here = onScreen();
    const d = await api.get('/api/reports/health', { scope: $('#scope').value });
    if (!here()) return;
    $('#out').innerHTML = `
      <div class="kpis">
        <div class="kpi ${d.bad ? 'bad' : 'good'}"><div class="lb">To put right</div>
          <div class="vl">${d.bad}</div>
          <div class="ft">${d.bad ? 'the books disagree with themselves here'
                                  : 'nothing disagrees'}</div></div>
        <div class="kpi"><div class="lb">Worth knowing</div><div class="vl">${d.warn}</div>
          <div class="ft">true, and explained</div></div>
        <div class="kpi good"><div class="lb">Clean</div><div class="vl">${d.ok}</div>
          <div class="ft">checked and right</div></div>
      </div>` +
      d.checks.map(c => {
        const [cls, mark] = LEVEL[c.level];
        const open = c.level !== 'ok' && c.rows.length;
        return `<div class="card"><div class="bd">
          <div class="row" style="align-items:flex-start;gap:12px">
            <span class="tag ${cls}" style="margin-top:2px">${mark}</span>
            <div style="flex:1;min-width:0">
              <div style="font-weight:600">${esc(c.title)}</div>
              <div style="color:var(--muted);font-size:13px;margin-top:3px">${esc(c.note)}</div>
            </div>
            ${c.count ? `<div class="r" style="white-space:nowrap">
              <div style="font-weight:700">${c.count}</div>
              ${c.amount ? `<div class="num" style="color:var(--muted);font-size:12px">${fmt(c.amount)}</div>` : ''}
            </div>` : ''}
          </div>
          ${open ? `<details${c.level === 'bad' ? ' open' : ''} style="margin-top:10px">
            <summary style="cursor:pointer;color:var(--muted);font-size:13px">
              ${c.grouped ? 'Show the breakdown'
                : c.rows.length >= c.count ? 'Show all ' + c.rows.length
                : 'Show the first ' + c.rows.length + ' of ' + c.count}</summary>
            <div class="tbl-scroll" style="margin-top:8px">${healthTable(c.rows)}</div>
          </details>` : ''}
        </div></div>`;
      }).join('');
  }
  $('#go').onclick = load;
  $('#scope').onchange = load;
  load();
});

/** Each check answers with its own shape, so the columns come from the rows. */
function healthTable(rows) {
  const cols = [];
  rows.forEach(r => Object.keys(r).forEach(k => {
    if (k !== 'id' && cols.indexOf(k) < 0) cols.push(k);
  }));
  const label = k => k.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
  const numeric = v => typeof v === 'number';
  return '<table><thead><tr>' +
    cols.map(k => `<th class="${numeric(rows[0][k]) ? 'r' : ''}">${esc(label(k))}</th>`).join('') +
    '</tr></thead><tbody>' + rows.map(r => {
      const href = healthHref(r);
      return '<tr' + (href ? ' data-href="' + esc(href) + '"' : '') + '>' + cols.map(k => {
      const v = r[k];
      if (v === undefined || v === null || v === '') return '<td></td>';
      if (numeric(v)) return `<td class="r num">${fmt(v)}</td>`;
      return k === 'invoice_no' && href
        ? `<td><b>${goLink(href, esc(String(v)))}</b></td>` : `<td>${esc(String(v))}</td>`;
    }).join('') + '</tr>';
    }).join('') + '</tbody></table>';
}

/** Where a checked row's document is: the firm is named by its code, and a
 *  voucher row gives its kind as the document type. */
function healthHref(r) {
  if (!r.id || !(r.invoice_no || r.doc_type)) return '';
  const firm = (S.meta.companies || []).find(c => c.code === r.company);
  if (!firm) return '';
  return docLink(r.doc_type || 'SALE', r.id, firm.id);
}

/* Trial Balance and Outstanding read the same ledgers off the same balances
   -- one down a page as Debit and Credit, one across two columns as
   Liabilities and Assets. SHREE keeps both behind a single *Layout* box on
   one screen, and so does this. The address still works and opens that
   screen in the layout it names. */
route(/^\/reports\/trial-balance$/, async (view, _m, q) => {
  location.replace('#/reports/outstanding?layout=tb'
    + (q.to ? '&to=' + encodeURIComponent(q.to) : ''));
});

route(/^\/reports\/pnl$/, async (view, _m, q) => {
  setTitle('Profit & loss', 'Income and expenditure for the period');
  view.innerHTML = filterBar('').replace('<button id="csv">⤓ CSV</button>',
    '<button id="print">🖨 Print</button>');
  const mem = screenState('/reports/pnl',
    { from: '#f1', to: '#f2', fy: fyField('f1', 'f2') });
  async function load() {
    mem.save();
    const here = onScreen();
    const d = await api.get('/api/reports/pnl',
      { from: $('#f1').value, to: $('#f2').value });
    if (!here()) return;
    const side = (title, groups, total, label) => `
      <div><h3>${title}</h3>
        ${groups.map(g => `<div class="bsline"><span>${goLink(groupLink(g.code),
          esc(g.name), 'The accounts in this group')}</span>
          <span class="num">${fmt(g.amount)}</span></div>`).join('') ||
          '<div class="bsline"><span style="color:var(--muted)">Nothing posted</span><span></span></div>'}
        <div class="bsline tot"><span>${label}</span><span class="num">${fmt(total)}</span></div>
      </div>`;
    const profit = num(d.profit);
    $('#out').innerHTML = `
      <div class="kpis">
        <div class="kpi"><div class="lb">Income</div><div class="vl">${money(d.income_total)}</div></div>
        <div class="kpi"><div class="lb">Expenditure</div><div class="vl">${money(d.expense_total)}</div></div>
        <div class="kpi ${profit >= 0 ? 'good' : 'bad'}">
          <div class="lb">${profit >= 0 ? 'Net profit' : 'Net loss'}</div>
          <div class="vl">${money(Math.abs(profit))}</div>
          <div class="ft">${dt($('#f1').value)} to ${dt($('#f2').value)}</div></div>
      </div>
      <div class="card"><div class="bd flush"><div class="twocol">
        ${side('Expenditure', d.expense, d.expense_total + Math.max(0, profit),
               profit >= 0 ? 'Total (incl. net profit)' : 'Total')}
        ${side('Income', d.income, d.income_total + Math.max(0, -profit),
               profit < 0 ? 'Total (incl. net loss)' : 'Total')}
      </div></div></div>
      <div class="hint" style="padding:0 4px">Closing stock is not valued into
        these figures — the app tracks stock in quantity only.</div>`;
  }
  $('#go').onclick = load;
  bindFy(load, 'f1', 'f2');
  $('#print').onclick = () => window.print();
  const fy = defaultFy();
  if (fy) {
    const r = fyRange(fy);
    $('#f1').value = r.from;
    $('#f2').value = r.to;
  }
  mem.restore(q);          // after the year default, so a remembered one wins
  load();
});

route(/^\/reports\/balance-sheet$/, async (view, _m, q) => {
  setTitle('Balance sheet', 'Assets against liabilities as on a date');
  view.innerHTML = `<div class="card"><div class="bd"><div class="row">
      ${fySelect('fyq', { asOn: true })}
      <div><label>As on</label><input type="date" id="f2" value="${today()}"></div>
      <div class="spacer"></div><button id="go" class="primary">Apply</button>
      <button id="print">🖨 Print</button>
    </div></div></div><div id="out"></div>`;

  const mem = screenState('/reports/balance-sheet',
    { to: '#f2', fy: fyField(null, 'f2') }).restore(q);
  async function load() {
    mem.save();
    const here = onScreen();
    const d = await api.get('/api/reports/balance-sheet', { to: $('#f2').value });
    if (!here()) return;
    const profit = num(d.profit);
    const block = g => `<div class="bsline"><span>${goLink(groupLink(g.code),
        esc(g.name), 'The accounts in this group')}
        ${g.contra ? '<span class="tag warn">contra</span>' : ''}</span>
        <span class="num">${fmt(g.amount)}</span></div>`;
    const liabTotal = d.liability_total + profit;
    $('#out').innerHTML = `
      <div class="kpis">
        <div class="kpi"><div class="lb">Total assets</div><div class="vl">${money(d.asset_total)}</div></div>
        <div class="kpi"><div class="lb">Liabilities + capital</div><div class="vl">${money(liabTotal)}</div></div>
        <div class="kpi ${profit >= 0 ? 'good' : 'bad'}">
          <div class="lb">${profit >= 0 ? 'Profit' : 'Loss'} carried</div>
          <div class="vl">${money(Math.abs(profit))}</div></div>
      </div>
      <div class="card"><div class="bd flush"><div class="twocol">
        <div><h3>Liabilities &amp; capital</h3>
          ${d.liabilities.map(block).join('')}
          <div class="bsline"><span>${profit >= 0 ? 'Net profit' : 'Net loss'}</span>
            <span class="num">${fmt(profit)}</span></div>
          <div class="bsline tot"><span>Total</span><span class="num">${fmt(liabTotal)}</span></div></div>
        <div><h3>Assets</h3>
          ${d.assets.map(block).join('')}
          <div class="bsline tot"><span>Total</span><span class="num">${fmt(d.asset_total)}</span></div></div>
      </div></div></div>
      ${Math.abs(num(d.difference)) > 0.01
        ? `<div class="card"><div class="bd" style="color:var(--warn)">
            <b>Difference of ${fmt(d.difference)}</b> — the two sides do not agree.
            This usually means opening balances were entered on one side only.</div></div>`
        : ''}
      <div class="hint" style="padding:0 4px">Closing stock is not valued here —
        see the Stock Summary for quantities.</div>`;
  }
  $('#go').onclick = load;
  bindFy(load, null, 'f2');
  $('#print').onclick = () => window.print();
  load();
});

/* ============================================ customer allocation ------
   Customers are split between the official firms. This screen shows the
   whole list in one place and moves a party from one firm to the other.   */
/* ============================================== bills, across both firms
   Each firm keeps its own books, so the ordinary Bills list can only show the
   firm you are standing in. This one reads both and totals them separately,
   which is the reason to look at them together at all.                     */

route(/^\/bills$/, (view, _m, q) => bothFirms(view, q, 'SALE'));
// Purchases too (6 Oct 2026: "show purchase bills for both the firms
// together"): bought into either GST firm, read the same way as the bills.
route(/^\/purchases$/, (view, _m, q) => bothFirms(view, q, 'PURCHASE'));

async function bothFirms(view, q, type) {
  const buying = type === 'PURCHASE';
  const one = buying ? 'purchase' : 'bill';
  setTitle(buying ? 'Purchases' : 'Bills', buying
    ? 'Every purchase bill, bought into both GST firms'
    : 'Every GST bill, from both billing firms', [
    { label: '⤓ CSV', onClick: () => location.href = firmUrl('/api/export/bills?' +
        new URLSearchParams({ doc_type: type, firm: $('#bf').value, from: $('#f1').value,
                              to: $('#f2').value, search: $('#q').value.trim() })) },
    { label: '＋ New ' + one, cls: 'primary',
      onClick: () => location.hash = '#/invoice/new?' + (buying ? 'type=PURCHASE&' : '')
        + 'firm=billing' }
  ]);

  view.innerHTML = `
    <div class="card"><div class="bd"><div class="row">
      <div><label>Firm</label><select id="bf" style="width:calc(300px * var(--typescale))">
        <option value="">Both firms</option>
        ${(S.meta.billing_firms || []).map(f =>
          `<option value="${f.id}"${String(q.firm) === String(f.id) ? ' selected' : ''}>${esc(firmLabel(f))}</option>`).join('')}
      </select></div>
      ${fySelect('fyq')}
      <div><label>From</label><input type="date" id="f1" value="${fyStart()}"></div>
      <div><label>To</label><input type="date" id="f2" value="${today()}"></div>
      <div style="flex:1;min-width:200px"><label>Search</label>
        <input id="q" placeholder="Bill no or ${buying ? 'supplier' : 'party'}…"></div>
    </div></div></div>
    <div id="sum" class="kpis"></div>
    <div class="card"><div class="bd flush tbl-scroll" id="list"></div></div>`;

  // The Year box is read back off the dates; only "to date" rides along.
  const mem = screenState(buying ? '/purchases' : '/bills', {
    firm: '#bf', from: '#f1', to: '#f2', search: '#q',
    fy: fyField('f1', 'f2')
  }).restore(q);

  async function load() {
    mem.save();
    const here = onSearch();
    const d = await api.get('/api/bills', {
      doc_type: type, firm: $('#bf').value, from: $('#f1').value, to: $('#f2').value,
      search: $('#q').value.trim()
    });
    if (!here()) return;

    // Each firm's own figure first, then the two added up -- the number you
    // cannot get from either firm's own screen.
    $('#sum').innerHTML = d.per_firm.map(f => `
      <div class="kpi"><div class="lb">${esc(f.name)}</div>
        <div class="vl">${money(f.total)}</div>
        <div class="ft">${f.count} ${one}(s) · taxable ${fmt(f.taxable)} · GST ${fmt(f.tax)}</div>
      </div>`).join('') + (d.per_firm.length > 1 ? `
      <div class="kpi good"><div class="lb">Both firms</div>
        <div class="vl">${money(d.totals.total)}</div>
        <div class="ft">${d.totals.count} ${one}(s) · taxable ${fmt(d.totals.taxable)} · GST ${fmt(d.totals.tax)}</div>
      </div>` : '');

    const total = {};
    d.rows.forEach(r => { total[r.id] = r.grand_total; });
    const ticks = () => bulkRows($('#list'), 'invoice', {
      noun: [one, one + 's'], reload: load, amount: tr => total[tr.dataset.id]
    });
    if (!d.rows.length) {
      $('#list').innerHTML = '<div class="empty"><b>No ' + one + 's yet</b>Nothing matches this filter in either firm.</div>';
      ticks();
      return;
    }
    // A bill lives in its own firm's books, so opening or printing one means
    // going there first.
    const bind = tr => {
      const id = num(tr.dataset.id), firm = num(tr.dataset.firm);
      // Alt+P: a bill's challan. A purchase is the supplier's; it has none.
      if (!buying) tr._challan = () => printInvoice(id, 'challan', firm);
      tr.onclick = async e => {
        if (e.target.hasAttribute('data-print')) { printInvoice(id, null, firm); return; }
        if (e.target.hasAttribute('data-del')) {
          deleteInvoice(id, $('b', tr).textContent, load, firm);
          return;
        }
        if (firm !== S.meta.company_id) await useCompany(firm);
        location.hash = '#/invoice/' + id;
      };
    };
    await fillRows($('#list'), `<table><thead><tr>
      <th>Date</th><th>Firm</th><th>Bill no</th><th>${buying ? 'Supplier' : 'Party'}</th><th>GSTIN</th>
      <th class="r">Taxable</th><th class="r">GST</th><th class="r">Total</th><th class="r">Actions</th>
      </tr></thead><tbody>`, d.rows.map(r => `
      <tr class="clickable" data-id="${r.id}" data-firm="${r.firm_id}">
        <td class="nw">${dt(r.invoice_date)}</td>
        <td><span class="tag info">${esc(r.firm_code)}</span></td>
        <td><b>${esc(docNo(r))}</b></td>
        <td>${goLink(accountLink(r.party_id), esc(r.party_name), 'Open the account')}<div style="font-size:11.5px;color:var(--muted)">${esc(r.party_city || '')}</div></td>
        <td style="font-family:var(--mono);font-size:11.5px">${esc(r.party_gstin || '—')}</td>
        <td class="r num">${fmt(r.taxable)}</td>
        <td class="r num">${fmt(num(r.cgst) + num(r.sgst) + num(r.igst) + num(r.cess))}</td>
        <td class="r num"><b>${fmt(r.grand_total)}</b></td>
        <td class="r" style="white-space:nowrap">
          <button class="link" data-print>Print</button>
          <button class="link" data-del style="color:var(--bad)">Delete</button></td>
      </tr>`), `</tbody><tfoot><tr>
        <td colspan="5">${d.totals.count} ${one}(s)</td>
        <td class="r num">${fmt(d.totals.taxable)}</td>
        <td class="r num">${fmt(d.totals.tax)}</td>
        <td class="r num"><b>${fmt(d.totals.total)}</b></td><td></td></tr></tfoot></table>`, {
        here,
        each: trs => trs.filter(tr => tr.dataset.id).forEach(bind),
        first: ticks
      });
  }
  $('#bf').onchange = load;
  bindFy(load, 'f1', 'f2');
  ['#f1', '#f2'].forEach(x => $(x).onchange = load);
  $('#q').oninput = () => { clearTimeout(window._bv); window._bv = setTimeout(load, 160); };
  load();
}

route(/^\/allocation$/, async (view, _m, q) => {
  setTitle('Customer allocation', 'Which official firm raises the bill for each customer', [
    { label: '⤓ CSV', onClick: () => location.href = firmUrl('/api/export/allocation') },
    { label: '↻ Refresh', onClick: () => render() }
  ]);
  view.innerHTML = `
    <div class="card"><div class="bd"><div class="row">
      <div style="flex:1;max-width:360px"><label>Search</label>
        <input id="q" placeholder="Customer name, city or GSTIN…"></div>
      <div><label>Firm</label><select id="firm" style="width:calc(300px * var(--typescale))">
        <option value="">All firms</option></select></div>
      <div style="display:flex;align-items:flex-end;padding-bottom:7px">
        <label style="margin:0;text-transform:none;font-weight:500">
          <input type="checkbox" id="act" style="width:auto" checked> Active only</label></div>
    </div></div></div>
    <div class="card"><div class="bd flush tbl-scroll" id="list"></div></div>
    <div class="card"><h2>Applying a list in bulk</h2><div class="bd">
      <p style="margin:0 0 10px">Export the list above, set the <b>Billed by</b>
        column, and apply it in one go. Names are matched ignoring case,
        punctuation and extra spaces; anything that does not match is reported
        rather than guessed at.</p>
      <pre style="background:var(--line-soft);padding:12px;border-radius:6px;overflow-x:auto;font-size:12.5px;margin:0"
      >python3 app/set_billing.py --file split.csv --dry-run
python3 app/set_billing.py --file split.csv</pre>
      <div class="hint">CSV, TSV and .xlsx are all read. Run the dry run first —
        it writes nothing and tells you what it would do.</div>
    </div></div>`;

  const mem = screenState('/allocation', {
    search: '#q', firm: '#firm',
    // Ticked is the state this box is drawn in, so it is the empty one that
    // has to be written down -- an absent key means "as it comes".
    active: { get: () => $('#act').checked ? '' : '0',
              set: v => { $('#act').checked = v !== '0'; } }
  }).restore(q);

  let firms = [], billFirms = [];
  async function load() {
    const here = onSearch();
    const d = await api.get('/api/allocation', {
      search: $('#q').value.trim(), active_only: $('#act').checked ? 1 : ''
    });
    if (!here()) return;
    firms = d.firms;
    billFirms = d.billing_firms;
    if ($('#firm').options.length <= 1) {
      $('#firm').innerHTML = '<option value="">All firms</option>' +
        firms.map(f => `<option value="${f.id}">${esc(firmLabel(f))}</option>`).join('');
      mem.restore(q);      // the firm box only has options to be set to now
    }
    mem.save();
    const pick = $('#firm').value;
    const rows = pick ? d.rows.filter(r => r.company_id === num(pick)) : d.rows;

    // Names that appear under more than one firm are worth flagging.
    const seen = {};
    d.rows.forEach(r => { seen[r.name.toLowerCase()] = (seen[r.name.toLowerCase()] || 0) + 1; });

    $('#list').innerHTML = rows.length ? `<table><thead><tr>
      <th>Customer</th><th>City / State</th><th>GSTIN</th>
      <th>Master in</th><th class="c">Docs</th><th class="r">Balance</th>
      <th style="width:290px">Billed by</th></tr></thead><tbody>` +
      rows.map(r => `<tr data-id="${r.id}">
        <td><b>${goLink(accountLink(r.id), esc(r.name), 'Open the account')}</b>
          ${r.active ? '' : ' <span class="tag warn">inactive</span>'}
          ${seen[r.name.toLowerCase()] > 1 ? ' <span class="tag bad">in both firms</span>' : ''}</td>
        <td>${esc([r.city, r.state].filter(Boolean).join(', '))}</td>
        <td style="font-family:var(--mono);font-size:11.5px">${esc(r.gstin || '—')}</td>
        <td style="font-size:12.5px;color:var(--muted)">${esc(r.company_code)}</td>
        <td class="c">${r.docs}</td>
        <td class="r num">${fmt(Math.abs(num(r.balance)))} ${num(r.balance) ? (num(r.balance) > 0 ? 'Dr' : 'Cr') : ''}</td>
        <td><select data-bill style="min-width:calc(200px * var(--typescale))">
          <option value="">— not set —</option>
          ${billFirms.map(f => `<option value="${f.id}"${f.id === num(r.bill_company_id) ? ' selected' : ''}>${esc(firmLabel(f))}</option>`).join('')}
        </select></td>
      </tr>`).join('') + `</tbody><tfoot><tr><td colspan="7">${rows.length} customer(s)</td></tr></tfoot></table>`
      : '<div class="empty"><b>No customers</b>Nothing matches this filter.</div>';

    // The dropdown on the row is the whole of this screen -- it is what moves
    // a customer from one firm to the other. It was wired by a name the
    // markup does not use, so nothing was listening and the screen did
    // nothing at all, by keyboard or by mouse.
    $$('#list select[data-bill]').forEach(sel => {
      const original = sel.value;
      sel.onchange = async () => {
        const tr = sel.closest('tr');
        const id = num(tr.dataset.id);
        const target = firms.find(f => f.id === num(sel.value));
        const name = $('b', tr).textContent;
        // "— not set —" is what a customer nobody has allocated reads as. It
        // is a state to be got out of, not one to be chosen: there is no firm
        // to move to, so put the box back rather than throw.
        if (!target) {
          sel.value = original;
          bad('Pick the firm that bills this customer.');
          return;
        }
        if (!await confirmBox('Move customer',
          'Bill "' + name + '" from ' + target.name + ' from now on?')) {
          sel.value = original;
          return;
        }
        try {
          const res = await api.post('/api/ledgers/' + id + '/allocate',
            { company_id: target.id });
          invalidate();
          ok(res.message || 'Moved.');
          load();
        } catch (e) { bad(e.message); sel.value = original; }
      };
    });
  }
  $('#q').oninput = () => { clearTimeout(window._al); window._al = setTimeout(load, 180); };
  $('#firm').onchange = load;
  $('#act').onchange = load;
  load();
});

/* ============================================================= companies */
/* One four-column grid for the whole form. It used to be a stack of separate
   grids -- four columns, then two, then three -- so no row's boxes began where
   the row above them did. Fields that want more room span tracks instead. */
const COMPANY_FORM = c => `
  <div class="grid g4" style="align-items:start">
    <div><label>Company name *</label><input name="name" value="${esc(c.name || '')}"></div>
    <div><label>Short code</label><input name="code" value="${esc(c.code || '')}" placeholder="e.g. BSI"></div>
    <div><label>Tax</label><select name="tax_mode">
      <option value="gst"${(c.tax_mode || 'gst') === 'gst' ? ' selected' : ''}>GST firm</option>
      <option value="none"${c.tax_mode === 'none' ? ' selected' : ''}>No tax (quantity × rate only)</option>
    </select></div>
    <div><label>Default GST %</label>
      <input name="default_gst" class="num"
        value="${num(c.default_gst === undefined ? 18 : c.default_gst)}">
      <div class="hint">Used when a challan is billed and the item carries no rate.</div></div>

    <div class="sp2"><label>GSTIN</label>
      <input name="gstin" value="${esc(c.gstin || '')}" style="text-transform:uppercase"></div>
    <div class="sp2"><label>PAN</label>
      <input name="pan" value="${esc(c.pan || '')}" style="text-transform:uppercase"></div>

    <div class="sp2"><label>Address line 1</label>
      <input name="address1" value="${esc(c.address1 || '')}"></div>
    <div class="sp2"><label>Address line 2</label>
      <input name="address2" value="${esc(c.address2 || '')}"></div>

    <div><label>City</label><input name="city" value="${esc(c.city || '')}"></div>
    <div><label>PIN</label><input name="pin" value="${esc(c.pin || '')}"></div>
    <div class="sp2"><label>State</label><select name="state_code">
      <option value="">— select —</option>
      ${S.meta.states.map(s => `<option value="${s.code}"${c.state_code === s.code ? ' selected' : ''}>${s.code} · ${esc(s.name)}</option>`).join('')}
    </select></div>

    <div><label>Phone</label><input name="phone" value="${esc(c.phone || '')}"></div>
    <div><label>Mobile No</label><input name="mobile" value="${esc(c.mobile || '')}"></div>
    <div class="sp2"><label>Email</label>
      <input name="email" value="${esc(c.email || '')}"></div>

    <div><label>Short name (challan heading)</label>
      <input name="short_name" value="${esc(c.short_name || '')}" placeholder="e.g. BST"></div>
    <div><label>Jurisdiction</label>
      <input name="jurisdiction" value="${esc(c.jurisdiction || '')}" placeholder="e.g. RAJKOT"></div>
    <div class="sp2"><label>Logo (printed top-left)</label>
      <div class="row" style="gap:6px;flex-wrap:nowrap;align-items:center">
        <input type="file" id="logo-file" accept="image/*" style="flex:1;padding:4px">
        <input type="hidden" name="logo" value="${esc(c.logo || '')}">
        ${c.logo ? '<img id="logo-prev" src="' + esc(c.logo) + '" style="height:34px;border:1px solid var(--line);border-radius:4px">' : '<span id="logo-prev"></span>'}
      </div></div>

    <div class="sp2"><label>Bank name &amp; branch</label>
      <input name="bank_name" value="${esc(c.bank_name || '')}"></div>
    <div><label>Account number</label><input name="bank_ac" value="${esc(c.bank_ac || '')}"></div>
    <div><label>IFSC</label>
      <input name="bank_ifsc" value="${esc(c.bank_ifsc || '')}" style="text-transform:uppercase"></div>

    <div class="sp-all"><label>Terms &amp; conditions (printed on invoices)</label>
      <textarea name="terms" style="min-height:80px">${esc(c.terms || '')}</textarea></div>
  </div>
  <div class="hint">The state chosen here decides CGST + SGST versus IGST on every
    document for this company.</div>`;

async function editCompany(existing) {
  const c = existing || {};
  const saved = await modal(c.id ? 'Edit company — ' + c.name : 'New company',
    COMPANY_FORM(c), [
      { label: 'Cancel', value: null },
      {
        label: 'Save', cls: 'primary', value: true, before: async bg => {
          const data = readForm(bg);
          const st = S.meta.states.find(x => x.code === data.state_code);
          data.state = st ? st.name : '';
          try {
            await (c.id ? api.put('/api/companies/' + c.id, data)
              : api.post('/api/companies', data));
          } catch (e) { bad(e.message); return false; }
        }
      }
    ], { wide: true, onOpen: bindLogoPicker });
  if (saved) { invalidate(); await loadMeta(true); ok('Company saved.'); }
  return saved;
}

/** Read a chosen logo file into the hidden field as a data: URI. */
function bindLogoPicker(scope) {
  const file = $('#logo-file', scope);
  if (!file) return;
  file.onchange = () => {
    const f = file.files[0];
    if (!f) return;
    if (f.size > 400 * 1024) {
      bad('Logo must be under 400 KB — it is stored inside the database.');
      file.value = '';
      return;
    }
    const rd = new FileReader();
    rd.onload = () => {
      $('[name=logo]', scope).value = rd.result;
      const prev = $('#logo-prev', scope);
      if (prev) prev.outerHTML = '<img id="logo-prev" src="' + rd.result +
        '" style="height:34px;border:1px solid var(--line);border-radius:4px">';
    };
    rd.readAsDataURL(f);
  };
}

route(/^\/companies$/, async view => {
  setTitle('Companies', 'Each company keeps its own books, masters and numbering', [
    {
      label: '＋ New company', cls: 'primary',
      onClick: async () => { if (await editCompany()) render(); }
    }
  ]);
  const here = onScreen();
  const list = await api.get('/api/companies');
  if (!here()) return;
  view.innerHTML = `
    <div class="card"><div class="bd flush tbl-scroll">
      <table><thead><tr><th>Company</th><th>Code</th><th class="c">Tax</th><th>GSTIN</th>
        <th>City / State</th><th class="r">Documents</th>
        <th class="r">Actions</th></tr></thead><tbody>` +
      list.map(c => `<tr data-id="${c.id}">
        <td><b>${esc(c.name)}</b>${c.id === S.meta.company_id ? ' <span class="tag ok">active</span>' : ''}
          ${c.active ? '' : ' <span class="tag warn">inactive</span>'}</td>
        <td>${esc(c.code)}</td>
        <td class="c"><span class="tag ${c.tax_mode === 'none' ? 'warn' : 'info'}">${c.tax_mode === 'none' ? 'no tax' : 'GST'}</span></td>
        <td style="font-family:var(--mono);font-size:12px">${esc(c.gstin || '—')}</td>
        <td>${esc([c.city, c.state].filter(Boolean).join(', '))}</td>
        <td class="r num">${fmt(c.doc_count, 0)}</td>
        <td class="r" style="white-space:nowrap">
          ${c.id === S.meta.company_id ? '' : '<button class="link" data-act="use">Open</button>'}
          <button class="link" data-act="edit" data-enter>Edit</button>
          <button class="link" data-act="del" style="color:var(--bad)">Del</button></td>
      </tr>`).join('') + '</tbody></table></div></div>';
  // The card that sent people to a terminal and the old one-year importer
  // went on 6 Oct 2026 with the importer itself: SHREE is read from the
  // SHREE update screen, into the three firms only.

  $$('#view button[data-act]').forEach(b => {
    b.onclick = async ev => {
      const id = num(ev.target.closest('tr').dataset.id);
      const row = list.find(c => c.id === id);
      if (b.dataset.act === 'use') {
        await api.post('/api/companies/' + id + '/activate', {});
        invalidate(); await loadMeta(true); ok('Switched to ' + row.name);
        location.hash = '#/dashboard';
        return;
      }
      if (b.dataset.act === 'edit') {
        if (await editCompany(await api.get('/api/companies/' + id))) render();
        return;
      }
      if (!await confirmBox('Delete company',
        'Delete "' + row.name + '" and all its masters? This cannot be undone.')) return;
      try { await api.del('/api/companies/' + id); await loadMeta(true); ok('Deleted.'); render(); }
      catch (e) { bad(e.message); }
    };
  });
});

/* ========================================================= SHREE update
   Asked for on 16 Sep 2026: *a section which will automatically fetch the
   data from the old software files and accordingly update this software's
   data.* The work is app/shree_sync.py. This is where an update is started
   and watched, where SHREE's folders are named, and where the decisions
   taken here about imported rows -- the ones every update puts back -- can
   be seen and let go of. Since 24 Sep 2026 nothing is fetched by itself:
   *"only when the user commands should it fetch everything from SHREE."*  */
const SYNC_RESULT = {
  done: ['ok', 'Updated'], preview: ['info', 'Preview'],
  failed: ['bad', 'Not applied'], '': ['warn', 'Stopped']
};

/** The tag for one update. One that left a firm out says so: a green
 *  "Updated" over bills that never came in read as all was well. */
function syncResult(r) {
  if (r.status === 'done'
      && / (was|were) (not updated|left as it was)| not in -- | not checked against /.test(r.message || ''))
    return ['warn', 'Partly updated'];
  return SYNC_RESULT[r.status] || SYNC_RESULT[''];
}

/** "SALE|25-26|GT|256" in BST reads "Challan GT 256 (25-26)". */
function syncDocName(firm, key) {
  const [kind, fy, series, no] = String(key).split('|');
  const co = (S.meta.companies || []).find(c => c.code === firm);
  const label = kind === 'SALE' && co && co.tax_mode === 'none' ? 'Challan'
    : DOC_LABEL[kind] || kind.charAt(0) + kind.slice(1).toLowerCase();
  return label + ' ' + [series, no].filter(Boolean).join(' ') + (fy ? ' (' + fy + ')' : '');
}

/** The update's own words are plain ASCII, so a Windows console can print
 *  them; the screen gives them their proper dashes and arrows. */
function syncText(s) {
  return esc(String(s == null ? '' : s).replace(/ -> /g, ' → ').replace(/ -- /g, ' — '));
}

function syncPlain(s) {
  return String(s == null ? '' : s).replace(/ -> /g, ' → ').replace(/ -- /g, ' — ');
}

/** How long between two of the server's "2026-09-16 14:05:09" stamps. */
function syncTook(a, b) {
  const s = (new Date(String(b).replace(' ', 'T'))
    - new Date(String(a).replace(' ', 'T'))) / 1000;
  if (!isFinite(s) || s < 0) return '';
  return s < 60 ? Math.round(s) + ' s'
    : Math.floor(s / 60) + ' min ' + Math.round(s % 60) + ' s';
}

/** A firm an update left as it was, and why. Where the only question is
 *  whether SHREE's files are to be believed -- documents it no longer has, a
 *  newest entry that went back -- it can be taken as SHREE has it, once. */
function syncHeldHtml(held) {
  return Object.keys(held || {}).map(f => {
    const h = held[f];
    return `<div class="sync-warn"><span class="tag bad">!</span><div>
      <b>${esc(f)} was left as it was.</b>
      ${(h.why || []).map(w => '<div>' + syncText(w) + '</div>').join('')}
      ${h.can_accept ? `<div style="margin-top:8px">If that is what SHREE really
        has now, update ${esc(f)} as SHREE has it.
        <button class="sm" data-accept="${esc(f)}">Update ${esc(f)} anyway</button></div>`
        : '<div class="hint" style="margin:4px 0 0">The full log under Details says more.</div>'}
    </div></div>`;
  }).join('');
}

/** Asked once, with what would go; then an update that takes the firm. */
async function syncAccept(firm, h) {
  const gone = (h && h.removed) || [];
  const total = Math.max(num(h && h.gone_total), gone.length);
  const list = gone.length ? `<p style="margin:0 0 6px">SHREE no longer has
      ${total === 1 ? 'this one, so it would be taken out' : 'these ' + fmt(total, 0) + ', so they would be taken out'}
      of ${esc(firm)} here too:</p><ul class="sync-notes">` +
    gone.slice(0, 12).map(x => `<li>${esc(syncDocName(firm, x[0]))} · ${esc(x[1] || '')}
      · ${fmt(x[2])} · ${dt(x[3])}</li>`).join('') +
    (total > 12 ? `<li>and ${fmt(total - 12, 0)} more</li>` : '') + '</ul>' : '';
  if (!await modal('Update ' + firm + ' as SHREE has it',
    `${list}<p style="margin:8px 0 0">Do this only if SHREE’s folder is the one
       in use today — not a copy or a backup. A copy of the books is kept
       first, as with every update.</p>`,
    [{ label: 'Cancel', value: false },
     { label: 'Update ' + firm + ' anyway', cls: 'primary', value: true }])) return false;
  try {
    await api.post('/api/sync/run', { accept: [firm] });
    return true;
  } catch (e) { bad(e.message); return false; }
}

/** Now, spelt the way the server spells a time. */
function syncNow() {
  const d = new Date();
  return new Date(d - d.getTimezoneOffset() * 60000).toISOString()
    .slice(0, 19).replace('T', ' ');
}

route(/^\/sync$/, async view => {
  const here = onScreen();
  const d = await api.get('/api/sync');
  if (!here()) return;
  const st = d.settings;
  const running = d.state.running;

  async function start(preview) {
    if (!preview && !d.has_run && !await modal('Update the books from SHREE',
      `<p style="margin:0 0 8px">This is the first update from this screen. It
         reads SHREE’s folders, rebuilds every entry that came from SHREE,
         and puts back the ${d.rules.length} decisions listed under
         <b>Kept here</b>.</p>
       <p style="margin:0">A copy of the books is kept first, and nothing is
         replaced unless every check passes. A save made while it runs goes
         in the moment it finishes. Nothing is fetched from SHREE again until
         <b>Update now</b> is pressed. <b>Preview changes</b> does the same
         without replacing anything.</p>`,
      [{ label: 'Cancel', value: false },
       { label: 'Update now', cls: 'primary', value: true }])) return;
    try {
      await api.post('/api/sync/run', { preview: preview ? 1 : '' });
      render();
    } catch (e) { bad(e.message); }
  }
  /* Fetch everything (2 Oct 2026, "fetch everything no questions asked"):
     every firm taken as SHREE has it, every year read again by its
     contents. Since 6 Oct 2026 it first clears the books of everything made
     or decided here -- "delete everything from the software and fetch
     everything from SHREE ... the software and SHREE figures should
     match" -- so it says once what goes, and an entry made here going asks
     for the delete password as any delete does. A copy of the books is
     kept first, as with every update. */
  async function everything() {
    let p;
    try { p = await api.get('/api/sync/everything'); } catch (e) { bad(e.message); return; }
    const MAX = 12;
    const docList = (rows, total) => '<ul class="sync-notes">' + rows.slice(0, MAX).map(x =>
      `<li><span class="tag">${esc(x.firm)}</span><span>${[esc(syncDocName(x.firm, x.key)),
        esc(x.party || ''), fmt(x.amount), dt(x.date)].filter(Boolean).join(' · ')}</span></li>`).join('') +
      (total > MAX ? `<li>and ${fmt(total - MAX, 0)} more</li>` : '') + '</ul>';
    const many = (n, one, more) => fmt(n, 0) + ' ' + (n === 1 ? one : more);
    const extra = [
      p.orders && many(p.orders, 'order', 'orders'),
      (p.stock || p.steel) && many(p.stock + p.steel, 'stock entry', 'stock entries')
    ].filter(Boolean);
    // One line a firm and kind: seven CO-OP banks are one decision to read.
    const drops = [];
    p.drops.forEach(r => {
      let g = drops.find(x => x.firm === r.firm && x.label === r.label);
      if (!g) drops.push(g = { firm: r.firm, label: r.label, what: [] });
      g.what.push(r.what);
    });
    const body = `
      <p style="margin:0 0 10px">Every firm SHREE can be read for is cleared
        and read again from SHREE, every year of it, so the figures here are
        SHREE’s.</p>
      ${p.made.length ? `<p style="margin:0 0 6px"><b>Made here — SHREE does not
        have ${p.made.length === 1 ? 'it, so it goes' : 'these, so they go'}:</b></p>
        ${docList(p.made, p.made.length)}` : ''}
      ${p.edited.length ? `<p style="margin:0 0 6px"><b>Edited here — back to
        what SHREE has:</b></p>${docList(p.edited, p.edited.length)}` : ''}
      ${extra.length ? `<p style="margin:0 0 10px"><b>Also made here, and
        going:</b> ${esc(extra.join(', '))}.</p>` : ''}
      ${drops.length ? `<p style="margin:0 0 6px"><b>Decisions that kept the
        figures apart from SHREE’s — let go, so ${p.drops.length === 1 ? 'it comes' : 'these come'}
        back as SHREE has ${p.drops.length === 1 ? 'it' : 'them'}:</b></p>
        <ul class="sync-notes">${drops.map(g =>
          `<li><span class="tag">${esc(g.firm)}</span><span>${esc(g.label)}:
            ${g.what.map(syncText).join(', ')}</span></li>`).join('')}</ul>` : ''}
      ${p.kept.length ? `<p class="hint" style="margin:0 0 10px">Kept, as they
        change no figure: ${p.kept.map(k => esc(k.label.toLowerCase()) + ' (' + k.count + ')').join(', ')}.</p>` : ''}
      <p style="margin:0">A copy of the books is kept first. Settings,
        Personal Accounts and the staff list are left as they are.</p>`;
    const gone = p.password;
    if (!await modal('Fetch everything from SHREE', body,
      [{ label: 'Cancel', value: false },
       { label: gone ? 'Delete and fetch everything' : 'Fetch everything',
         cls: gone ? 'danger' : 'primary', value: true }],
      { wide: true })) return;
    try {
      await api.post('/api/sync/run', { everything: 1, wipe: gone ? 1 : '' });
      render();
    } catch (e) { bad(e.cancelled ? e.message.replace('was deleted', 'was fetched') : e.message); }
  }
  setTitle('SHREE update', 'Brings in what is entered in the old software, when you ask', [
    { label: 'Update now', cls: 'primary', onClick: () => start(false) },
    { label: 'Fetch everything', onClick: everything },
    { label: 'Preview changes', onClick: () => start(true) }
  ]);
  $$('#page-actions button').forEach(b => { b.disabled = running; });

  const unread = d.folders.filter(f => !f.found);
  const lacking = d.missing.map(m => m.firm);
  const keptDocs = d.rules.filter(r => r.kind === 'delete_doc').length;
  // "bill registers (BST, BSI)", "accounts book (BSTP)"
  const books = {};
  d.missing.forEach(m => { (books[m.book] = books[m.book] || []).push(m.firm); });
  const lackText = Object.keys(books).map(b =>
    (books[b].length > 1 ? b.replace(/ter$/, 'ters').replace(/book$/, 'books') : b)
    + ' (' + books[b].join(', ') + ')').join(' or ');
  const folderKpi = !d.folders.length ? ['bad', 'Not found', 'name its folder below']
    : unread.length ? ['bad', 'Cannot read', esc(unread[0].path)]
    : lacking.length ? ['bad', 'Not all found', 'nothing for ' + esc(lacking.join(', '))]
    : ['good', 'Found', 'accounts book and bill registers'];
  view.innerHTML = `
    <div id="sync-now"></div>
    <div id="sync-held">${running ? '' : syncHeldHtml(d.held)}</div>
    <div class="kpis">
      <div class="kpi"><div class="lb">Last updated</div>
        <div class="vl sm">${d.last_update ? esc(stampText(d.last_update)) : 'Not yet'}</div>
        <div class="ft">${d.last_update ? 'books replaced with SHREE’s' : 'from this screen'}</div></div>
      <div class="kpi"><div class="lb">Fetching</div>
        <div class="vl sm">Only when asked</div>
        <div class="ft">SHREE is read when Update now is pressed</div></div>
      <div class="kpi ${folderKpi[0]}"><div class="lb">SHREE</div>
        <div class="vl sm">${folderKpi[1]}</div>
        <div class="ft">${folderKpi[2]}</div></div>
      <div class="kpi"><div class="lb">Kept here</div>
        <div class="vl sm">${d.rules.length}</div>
        <div class="ft">decisions put back after every update</div></div>
    </div>

    <div class="card"><h2>Updates</h2>
      <div class="bd flush tbl-scroll" id="sync-runs"></div></div>

    <div class="card"><h2>Where SHREE is<span class="rt">
        <button id="s-find">Find SHREE</button>
        <button id="s-add">Add a folder…</button></span></h2>
      <div class="bd" style="padding-bottom:0"><div class="hint" style="margin:0">
        SHREE runs from its own folder on each of its two computers — the
        accounts book on one, the bill registers on the other. The folder on
        the other computer is reached over the network, as a mapped drive or
        as <span class="mono">\\\\COMPUTER\\F</span>. The backups SHREE keeps
        beside its folder are read too; the older years are only in them.
        ${d.folders.length ? (st.found ? 'These were found on this computer.'
                                       : 'These were chosen here.') : ''}</div></div>
      ${lacking.length && d.folders.length ? `<div class="sync-warn"><span class="tag bad">!</span><div>
        None of these holds SHREE’s ${esc(lackText)}.
        Add the SHREE folder of the computer it is kept on. Until then an
        update still brings in the other firms and leaves
        ${esc(lacking.join(', '))} as ${lacking.length > 1 ? 'they are' : 'it is'}.</div></div>` : ''}
      <div class="bd flush tbl-scroll" id="sync-folders"></div></div>

    <div class="card"><h2>Fetching from SHREE</h2><div class="bd">
      <div class="hint" style="margin:0">Nothing is read from SHREE by
        itself. <b>Update now</b> fetches everything SHREE holds — every
        firm, every year, with the backups beside it — and brings in every
        entry, change and deletion made there since the last update.
        <b>Fetch everything</b> first clears the books of everything made or
        decided here that SHREE does not have — entries made here, documents
        edited or deleted here, accounts deleted here — and then reads every
        firm and every year again as SHREE has it, so the figures here are
        SHREE’s. It says what will go before it starts. Work
        carries on meanwhile: a save made while an update runs goes in the
        moment it finishes, and open screens catch up by themselves.</div>
    </div></div>

    <div class="card"><h2>Kept here${keptDocs ? `<span class="rt">
        <button id="s-release">Bring back ${keptDocs} deleted document${keptDocs === 1 ? '' : 's'}</button></span>` : ''}</h2>
      <div class="bd" style="padding-bottom:0"><div class="hint" style="margin:0">
        What was decided in this app about rows that came from SHREE. SHREE
        knows nothing of it, so every update puts it back. Stop keeping one
        and the next update brings the row back the way SHREE has it.</div></div>
      <div class="bd flush tbl-scroll" id="sync-rules"></div></div>`;

  // ---- the updates run so far
  $('#sync-runs').innerHTML = d.runs.length ? `<table><thead><tr>
      <th>Started</th><th>By</th><th>Kind</th><th>Result</th><th>What happened</th>
      <th class="r">Took</th><th class="r">Actions</th></tr></thead><tbody>` +
    d.runs.map(r => {
      const [cls, word] = syncResult(r);
      return `<tr data-run="${r.id}">
        <td class="nw">${esc(stampText(r.started))}</td>
        <td>${esc(r.started_by)}</td>
        <td>${r.mode === 'preview' ? 'Preview' : 'Update'}</td>
        <td class="nw"><span class="tag ${cls}">${word}</span></td>
        <td>${syncText(r.message)}${r.repeats ? `<div class="hint" style="margin:0">and
          ${r.repeats} more time${r.repeats === 1 ? '' : 's'}, last ${esc(stampText(r.finished))}</div>` : ''}</td>
        <td class="r nw">${r.finished && !r.repeats ? syncTook(r.started, r.finished) : ''}</td>
        <td class="r"><button class="link" data-enter data-act="detail">Details</button></td>
      </tr>`;
    }).join('') + '</tbody></table>'
    : '<div class="empty"><b>No updates yet</b>Preview changes shows what an update would do, without doing it.</div>';
  $$('#sync-runs button[data-act=detail]').forEach(b => {
    b.onclick = () => syncRunDetail(num(b.closest('tr').dataset.run));
  });
  $$('#sync-held [data-accept]').forEach(b => {
    b.onclick = async () => {
      if (await syncAccept(b.dataset.accept, d.held[b.dataset.accept])) render();
    };
  });

  // ---- where SHREE is
  const paths = d.folders.map(f => f.path);
  const saveFolders = async (list, found, said) => {
    try {
      await api.put('/api/sync/settings', { sources: list, found: !!found });
      ok(said);
      render();
    } catch (e) { bad(e.message); }
  };
  $('#sync-folders').innerHTML = d.folders.length ? `<table><thead><tr>
      <th>Folder</th><th>What it holds</th><th>Backups beside it</th>
      <th class="r">Actions</th></tr></thead><tbody>` +
    d.folders.map((f, i) => `<tr data-i="${i}">
      <td class="mono">${esc(f.path)}</td>
      <td>${f.found ? (f.books.length ? f.books.map(b => `<div>${syncBook(b)}</div>`).join('')
                                      : '<span class="tag warn">none of this app’s firms</span>')
                    : '<span class="tag bad">cannot be read</span>'}</td>
      <td title="${esc(f.beside.join(', '))}">${f.beside.length
        ? f.beside.length + (f.beside.length === 1 ? ' folder or archive' : ' folders and archives')
        : '<span class="hint">none</span>'}</td>
      <td class="r nw"><button class="link" data-act="change" data-enter>Change…</button>
        <button class="link danger-link" data-act="remove">Remove</button></td>
    </tr>`).join('') + '</tbody></table>'
    : `<div class="empty"><b>SHREE was not found on this computer</b>
        Find SHREE looks on every drive again. Add a folder names it — on the
        other computer, type its network name, like \\\\ACCOUNTS\\F.</div>`;
  $$('#sync-folders button[data-act]').forEach(b => {
    const i = num(b.closest('tr').dataset.i);
    b.onclick = async () => {
      if (b.dataset.act === 'remove') {
        const rest = paths.filter((_p, j) => j !== i);
        saveFolders(rest, false, rest.length ? 'Removed.'
          : 'Removed. SHREE will be looked for again.');
        return;
      }
      const home = await pickShreeFolder(paths[i]);
      if (home) saveFolders(paths.map((p, j) => (j === i ? home : p)), false, 'Folder changed.');
    };
  });
  $('#s-add').onclick = async () => {
    const home = await pickShreeFolder('');
    if (home) saveFolders(paths.concat([home]), false, 'Folder added.');
  };
  $('#s-find').onclick = async () => {
    const list = await findShree(paths);
    if (list) saveFolders(list, true, 'Using the folders found.');
  };

  // ---- the decisions kept
  $('#sync-rules').innerHTML = d.rules.length ? `<table><thead><tr>
      <th>Decision</th><th>Firm</th><th>What</th><th>Noted</th>
      <th class="r">Actions</th></tr></thead><tbody>` +
    d.rules.map(r => {
      const doc = r.kind === 'supersede_doc' || r.kind === 'delete_doc';
      // An edited document is still in the books, as this app's own copy.
      const what = r.kind === 'supersede_doc'
        ? goLink('#/sync/open?' + new URLSearchParams({ firm: r.firm, key: r.name }),
                 syncText(r.what), 'Open the copy kept here')
        : syncText(r.what);
      return `<tr data-rule="${r.id}">
        <td class="nw">${esc(r.label)}</td><td>${esc(r.firm)}</td>
        <td${doc ? '' : ' class="mono"'}>${what}</td>
        <td><div>${esc(r.note || '')}</div>
          <div class="hint" style="margin:0">${esc(stampText(r.created_at))}</div></td>
        <td class="r nw"><button class="link danger-link" data-act="let-go">Stop keeping</button></td>
      </tr>`;
    }).join('') + '</tbody></table>'
    : '<div class="empty">Nothing decided here yet.</div>';
  const release = $('#s-release');
  if (release) release.onclick = async () => {
    if (!await confirmBox('Bring back deleted documents',
      'Stop keeping the ' + keptDocs + ' document' + (keptDocs === 1 ? '' : 's')
      + ' deleted in this app. The next update brings '
      + (keptDocs === 1 ? 'it' : 'them') + ' back from SHREE as SHREE has '
      + (keptDocs === 1 ? 'it' : 'them') + '.')) return;
    try {
      const r = await api.post('/api/sync/rules/release', { kind: 'delete_doc' });
      ok(counted(r.released, 'document', 'documents') + ' will come back at the next update — press Update now.');
      render();
    } catch (e) { bad(e.message); }
  };
  $$('#sync-rules button[data-act=let-go]').forEach(b => {
    b.onclick = async () => {
      const r = d.rules.find(x => x.id === num(b.closest('tr').dataset.rule));
      if (!await confirmBox('Stop keeping this',
        r.label + ': ' + r.what.replace(/ -> /g, ' → ') + '. The next update brings it back the way SHREE has it.')) return;
      try {
        await api.del('/api/sync/rules/' + r.id);
        ok('No longer kept.');
        render();
      } catch (e) { bad(e.message); }
    };
  });

  // ---- an update under way, or one started elsewhere while this is open
  //      (another window, or app/shree_sync.py --run)
  const box = $('#sync-now');
  if (running) { syncProgress(box, d.state); return; }
  const idle = async () => {
    if (!box.isConnected) return;
    const s = await api.get('/api/sync/status').catch(() => null);
    if (!box.isConnected) return;
    if (s && s.running) render();
    else setTimeout(idle, 10000);
  };
  setTimeout(idle, 10000);
});

/** "+2 −1 ~5", or a dash for none. */
function syncCounts(c) {
  const bits = [];
  if (c.added) bits.push('+' + fmt(c.added, 0));
  if (c.removed) bits.push('−' + fmt(c.removed, 0));
  if (c.changed) bits.push('~' + fmt(c.changed, 0));
  return bits.join(' ') || '—';
}

/** "BSTP accounts book, to 10-09-2026" */
function syncBook(b) {
  return '<b>' + esc(b.firm) + '</b> ' + (b.kind === 'accounts' ? 'accounts book' : 'bill register')
    + (b.upto ? ' <span class="hint">to ' + dt(b.upto) + '</span>' : '');
}

/** Choose SHREE's folder by walking the folders this computer can reach.
 *  Answers the SHREE folder the chosen one stands for, or null. */
async function pickShreeFolder(start) {
  let at = null;
  const open = async (bg, path) => {
    let d;
    try { d = await api.get('/api/sync/browse', { path }); } catch (e) {
      // What is listed stays, so Up still works; nothing typed is chosen.
      $('#fp-msg', bg).textContent = e.message;
      $('#fp-home', bg).textContent = '';
      $$('.ft button', bg)[1].disabled = true;
      if (at) at = Object.assign({}, at, { home: '' });
      return;
    }
    at = d;
    $('#fp-msg', bg).textContent = '';
    $('#fp-path', bg).value = d.path;
    $('#fp-up', bg).disabled = d.parent == null;
    $('#fp-home', bg).innerHTML = d.home
      ? '<span class="tag ok">SHREE</span> <span class="mono">' + esc(d.home) + '</span> will be used.'
      : (d.path ? 'No SHREE books in this folder. Open the one that holds shree.exe and the year folders (25-26, 26-27 …), or the drive it is on.'
                : 'Choose a drive.');
    $$('.ft button', bg)[1].disabled = !d.home;
    const list = $('#fp-list', bg);
    list.innerHTML = d.entries.length ? d.entries.map(e =>
      `<button class="fp-item" data-path="${esc(e.path)}"><span>${e.computer ? '🖥' : '📁'}</span>
        <span class="grow">${esc(e.name)}</span>${e.shree ? '<span class="tag ok">SHREE</span>' : ''}</button>`).join('')
      : '<div class="empty">No folders in here.</div>';
    list.scrollTop = 0;
    $$('.fp-item', list).forEach(x => { x.onclick = () => open(bg, x.dataset.path); });
  };
  const v = await modal('Choose SHREE’s folder', `
      <div class="row" style="flex-wrap:nowrap;gap:8px">
        <button id="fp-up" title="The folder this one is in">↑ Up</button>
        <input id="fp-path" class="mono" spellcheck="false"
          placeholder="F:\\SHREE, or \\\\COMPUTER\\F for another computer">
        <button id="fp-go">Open</button></div>
      <div id="fp-msg" class="hint" style="color:var(--bad)"></div>
      <div id="fp-list" class="fp-list"></div>
      <div id="fp-home" class="hint" style="margin-top:10px"></div>`,
    [{ label: 'Cancel', value: null },
     { label: 'Use this folder', cls: 'primary', value: 'use',
       before: () => !!(at && at.home) }],
    { wide: true, onOpen: bg => {
      const box = $('#fp-path', bg);
      $('#fp-go', bg).onclick = () => open(bg, box.value.trim());
      $('#fp-up', bg).onclick = () => { if (at && at.parent != null) open(bg, at.parent); };
      // Enter in the box opens what was typed; it does not choose it.
      box.onkeydown = e => {
        if (e.key === 'Enter') { e.preventDefault(); open(bg, box.value.trim()); }
      };
      open(bg, start || '');
    } });
  return v === 'use' && at ? at.home : null;
}

/** Look for SHREE on every drive and in its shortcuts; answer the folders
 *  ticked, or null. */
async function findShree(current) {
  toast('Looking for SHREE…');
  let got;
  try { got = await api.get('/api/sync/discover'); } catch (e) { bad(e.message); return null; }
  // Where the look went: every drive, and every other computer tried.
  const looked = (got.looked || []).length ? `<ul class="sync-notes">${got.looked.map(l =>
      `<li><span class="mono">${esc(l.where)}</span> ${l.kind === 'network' ? '(another computer)' : ''}
        — ${l.found ? l.found + ' found' : 'nothing'}${l.note ? ' · ' + esc(l.note) : ''}</li>`).join('')}</ul>` : '';
  if (!got.found.length) {
    await modal('SHREE was not found', `<p style="margin:0">No SHREE folder with
      this app’s books was found on this computer’s drives, its Desktop and
      Documents, in SHREE’s shortcuts, or on the other computers this one
      can see.</p>${looked ? '<p style="margin:8px 0 4px">Looked at:</p>' + looked : ''}
      <p style="margin:8px 0 0">If SHREE is on the other computer, switch it
      on and share its drive, then look again — or use <b>Add a folder…</b>,
      where the other computers are listed under the drives, or type its
      network name, like <span class="mono">\\\\ACCOUNTS\\F</span>.</p>`);
    return null;
  }
  const how = h => h === 'copy' ? 'a copy on this computer'
    : h === 'running' ? 'SHREE is running from it'
    : h === 'found here before' ? 'where it was found before'
    : h.indexOf('drive') === 0 ? 'on ' + h
    : h.indexOf('on ') === 0 ? 'on the computer ' + h.slice(3) : 'from ' + h;
  const tick = got.found.map((c, i) => {
    const on = current.indexOf(c.path) >= 0 || got.chosen.indexOf(c.path) >= 0;
    return `<label class="check fp-cand"><input type="checkbox" value="${i}"${on ? ' checked' : ''}>
      <span><span class="mono">${esc(c.path)}</span>
        <span class="hint" style="display:block;margin:2px 0 0">${esc(how(c.how))} ·
          ${c.books.map(syncBook).join(', ')}</span></span></label>`;
  }).join('');
  let picked = [];
  const v = await modal('SHREE found on this computer', `<p style="margin:0 0 6px">Tick the
      folders an update should read. SHREE itself is better than a copy of
      it: a copy stops on the day it was made.</p>${tick}`,
    [{ label: 'Cancel', value: null },
     { label: 'Use the ticked folders', cls: 'primary', value: 'use', before: bg => {
       picked = $$('.fp-cand input', bg).filter(x => x.checked).map(x => got.found[num(x.value)].path);
       if (!picked.length) { bad('Tick at least one folder.'); return false; }
       return true;
     } }], { wide: true });
  return v === 'use' ? picked : null;
}

/** The live panel. Asks every second and a half until the update finishes,
 *  or the screen is left. */
function syncProgress(box, first) {
  box.innerHTML = `<div class="card sync-live"><div class="bd">
      <div class="row" style="align-items:baseline">
        <b id="sy-title"></b><div class="spacer"></div>
        <span id="sy-count" class="hint" style="margin:0"></span></div>
      <div class="sync-track"><i id="sy-bar"></i></div>
      <div id="sy-step" style="margin-top:6px"></div>
      <details style="margin-top:8px"><summary>What it is doing</summary>
        <pre id="sy-log" class="sync-log"></pre></details>
    </div></div>`;
  const draw = s => {
    $('#sy-title', box).textContent = s.mode === 'preview'
      ? 'Working out what an update would change — nothing will be replaced'
      : s.everything
        ? 'Fetching everything from SHREE — the books cleared and every firm, every year read again; anything saved meanwhile goes in when it finishes'
        : 'Updating the books from SHREE — anything saved meanwhile goes in when it finishes';
    const n = Math.min(s.step_no, s.steps);
    $('#sy-count', box).textContent = 'step ' + n + ' of ' + s.steps + ' · '
      + syncTook(s.started, syncNow());
    $('#sy-bar', box).style.width = Math.round(100 * Math.max(0, n - 1) / s.steps) + '%';
    $('#sy-step', box).textContent = s.step;
    const log = $('#sy-log', box);
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    log.textContent = (s.log || []).join('\n');
    if (atEnd) log.scrollTop = log.scrollHeight;
  };
  draw(first);
  const tick = async () => {
    if (!box.isConnected) return;
    let s = null;
    try { s = await api.get('/api/sync/status'); } catch (e) { /* asked again */ }
    if (!box.isConnected) return;
    if (!s || s.running) {
      if (s) draw(s);
      setTimeout(tick, 1500);
      return;
    }
    const r = s.result || {};
    if (r.message) toast(syncPlain(r.message), r.status === 'failed' ? 'bad' : 'ok');
    render();
  };
  setTimeout(tick, 1500);
}

async function syncRunDetail(id) {
  let r;
  try { r = await api.get('/api/sync/runs/' + id); } catch (e) { bad(e.message); return; }
  const s = r.summary || {};
  const firms = s.firms || {};
  const codes = Object.keys(firms);
  const [cls, word] = syncResult(r);
  // Only an update that was kept put these documents in the books.
  const live = r.status === 'done';
  const docCell = (firm, key, linked) => {
    const name = esc(syncDocName(firm, key));
    return linked ? goLink('#/sync/open?' + new URLSearchParams({ firm, key }), name)
                  : name;
  };
  const part = (f, what) => ['docs', 'vouchers'].reduce(
    (t, k) => t + num(((f.counts || {})[k] || {})[what]), 0);
  const bullets = (items, tag) => items.length ? '<ul class="sync-notes">' +
    items.map(x => `<li><span class="tag ${tag}">${tag === 'bad' ? '!' : '?'}</span>${syncText(x)}</li>`).join('')
    + '</ul>' : '';
  const section = (title, rows, head, cells) => rows.length ? `
    <details class="sync-sec"><summary>${esc(title)} <b>${fmt(rows.length, 0)}</b></summary>
      <div class="tbl-scroll"><table><thead><tr>${head}</tr></thead>
        <tbody>${rows.map(cells).join('')}</tbody></table></div>
    </details>` : '';
  const every = key => [].concat(...codes.map(
    c => (firms[c][key] || []).map(x => [c].concat(x))));

  const html = `
    <div class="row" style="gap:10px;align-items:baseline;flex-wrap:nowrap">
      <span class="tag ${cls}">${word}</span><b>${syncText(r.message)}</b></div>
    <div class="hint" style="margin:6px 0 12px">${r.mode === 'preview' ? 'Preview' : 'Update'}
      started by ${esc(r.started_by)} ${esc(stampText(r.started))}${r.finished
        ? ', took ' + syncTook(r.started, r.finished) : ''}.
      ${r.backup ? 'The books as they were before it are kept as <code>'
        + esc(r.backup.split(/[\\/]/).pop()) + '</code> in the data folder.' : ''}</div>
    ${syncHeldHtml(s.held)}
    ${bullets((s.failures || []).filter(x => !Object.values(s.held || {})
        .some(h => (h.why || []).includes(x))), 'bad')}
    ${bullets((s.warnings || []).filter(x => !Object.keys(s.held || {})
        .some(f => x.startsWith(f + ' was left as it was')
                   || x.startsWith(f + ' was not updated'))), 'warn')}
    ${codes.length ? `<div class="tbl-scroll"><table><thead><tr>
        <th>Firm</th><th class="r">New</th><th class="r">Changed</th><th class="r">Gone</th>
        <th class="r" title="Accounts new, gone and changed">Accounts</th>
        <th class="r" title="Items new, gone and changed">Items</th>
        <th title="The newest entry that came from SHREE">Newest</th>
        <th class="r" title="The trial balance difference">TB out by</th></tr></thead><tbody>` +
      codes.map(c => {
        const f = firms[c], a = (f.counts || {}).accounts || {};
        const moved = Math.abs(num(f.out_by_after) - num(f.out_by_before)) > 0.5;
        return `<tr><td><b>${esc(c)}</b></td>
          <td class="r num">${fmt(part(f, 'added'), 0)}</td>
          <td class="r num">${fmt(part(f, 'changed'), 0)}</td>
          <td class="r num">${fmt(part(f, 'removed'), 0)}</td>
          <td class="r num nw">${syncCounts(a)}</td>
          <td class="r num nw">${syncCounts((f.counts || {}).items || {})}</td>
          <td class="nw">${dt(f.newest_after)}${f.newest_after !== f.newest_before
            ? ' <span class="hint">was ' + dt(f.newest_before) + '</span>' : ''}</td>
          <td class="r num nw">${fmt(f.out_by_after)}${moved
            ? ' <span class="hint">was ' + fmt(f.out_by_before) + '</span>' : ''}</td></tr>`;
      }).join('') + '</tbody></table></div>' : ''}
    ${section('New in SHREE', every('added'),
      '<th>Firm</th><th>Document</th><th>Party</th><th class="r">Amount</th><th>Date</th>',
      x => `<tr><td>${esc(x[0])}</td><td class="nw">${docCell(x[0], x[1], live)}</td>
        <td>${esc(x[2])}</td><td class="r num">${fmt(x[3])}</td><td class="nw">${dt(x[4])}</td></tr>`)}
    ${section('Changed in SHREE', every('changed'),
      '<th>Firm</th><th>Document</th><th>Party</th><th class="r">Amount</th><th>Date</th><th>Was</th>',
      x => `<tr><td>${esc(x[0])}</td><td class="nw">${docCell(x[0], x[1], live)}</td>
        <td>${esc(x[2])}</td><td class="r num">${fmt(x[3])}</td><td class="nw">${dt(x[4])}</td>
        <td>${[x[5] !== x[2] ? esc(x[5]) : '',
               num(x[6]) !== num(x[3]) ? fmt(x[6]) : '',
               x[7] && x[7] !== x[4] ? dt(x[7]) : ''].filter(Boolean).join(' · ')}</td></tr>`)}
    ${section('No longer in SHREE', every('removed'),
      '<th>Firm</th><th>Document</th><th>Party</th><th class="r">Amount</th><th>Date</th>',
      x => `<tr><td>${esc(x[0])}</td><td class="nw">${docCell(x[0], x[1], false)}</td>
        <td>${esc(x[2])}</td><td class="r num">${fmt(x[3])}</td><td class="nw">${dt(x[4])}</td></tr>`)}
    ${section('Accounts new in SHREE', every('accounts_added'),
      '<th>Firm</th><th>Account</th>', x => `<tr><td>${esc(x[0])}</td><td>${esc(x[1])}</td></tr>`)}
    ${section('Accounts no longer in SHREE', every('accounts_removed'),
      '<th>Firm</th><th>Account</th>', x => `<tr><td>${esc(x[0])}</td><td>${esc(x[1])}</td></tr>`)}
    ${section('Accounts changed in SHREE', every('accounts_changed'),
      '<th>Firm</th><th>Account</th>', x => `<tr><td>${esc(x[0])}</td><td>${esc(x[1])}</td></tr>`)}
    ${section('Items new, changed or gone', every('items_added').map(x => x.concat('new'))
        .concat(every('items_changed').map(x => x.concat('changed')))
        .concat(every('items_removed').map(x => x.concat('gone'))),
      '<th>Firm</th><th>Item</th><th></th>',
      x => `<tr><td>${esc(x[0])}</td><td>${esc(x[1])}</td><td>${x[2]}</td></tr>`)}
    ${(s.timings || []).length ? `<details class="sync-sec"><summary>Time taken
        <b>${s.seconds ? s.seconds + ' s' : ''}</b></summary><table><tbody>${
        s.timings.map(t => `<tr><td>${esc(t[0])}</td><td class="r num">${t[1]} s</td></tr>`).join('')
      }</tbody></table></details>` : ''}
    ${section('Decisions put back', s.replayed || [],
      '<th>Firm</th><th>What</th><th>Result</th>',
      x => `<tr><td>${esc(x.firm)}</td><td>${syncText(x.what)}</td>
        <td><span class="tag ${x.status === 'warn' ? 'warn' : 'ok'}">${
          x.status === 'warn' ? 'look' : 'done'}</span> ${syncText(x.note || '')}</td></tr>`)}
    ${r.log ? `<details class="sync-sec"><summary>The full log</summary>
      <pre class="sync-log">${esc(r.log)}</pre></details>` : ''}`;
  const shown = modal((r.mode === 'preview' ? 'Preview' : 'Update') + ', '
                      + stampText(r.started), html, null, { wide: true });
  $$('.modal-bg [data-accept]').forEach(b => {
    b.onclick = async () => {
      const bg = b.closest('.modal-bg'), x = bg && $('[data-x]', bg);
      if (x) x.click();
      if (await syncAccept(b.dataset.accept, (s.held || {})[b.dataset.accept])
          && /^#\/sync/.test(location.hash)) render();
    };
  });
  await shown;
}

/* A document named in an update report. A report cannot hold ids -- every
   update gives SHREE's documents new ones -- so the numbers on its paper are
   looked up here, and this entry becomes the document's own. */
route(/^\/sync\/open$/, async (view, _m, q) => {
  // A receipt opens as a dialog over whatever is behind it; this is.
  setTitle('SHREE update', 'Opening ' + syncDocName(q.firm, q.key || ''));
  let f;
  try {
    f = await api.get('/api/sync/find', { firm: q.firm, key: q.key });
  } catch (e) {
    view.innerHTML = '<div class="empty"><b>' + esc(syncDocName(q.firm, q.key || ''))
      + '</b>' + esc(e.message) + '</div>';
    return;
  }
  stamp({}, docLink(f.kind, f.id, f.company_id));
  render();
});

/* ============================================================== settings */
route(/^\/settings$/, async view => {
  const c = S.meta.company;
  setTitle('Settings', 'Details and numbering for ' + c.name, [
    { label: 'All companies', onClick: () => location.hash = '#/companies' }
  ]);
  const fys = S.meta.fys.slice();
  const thisFy = fyLabel(today());
  if (!fys.includes(thisFy)) fys.unshift(thisFy);

  view.innerHTML = `
    <div class="card"><h2>Firm details</h2><div class="bd" id="co">
      ${COMPANY_FORM(c)}
      <div class="row" style="margin-top:14px"><div class="spacer"></div>
        <button class="primary" id="save-co">Save firm details</button></div>
    </div></div>

    <div class="card"><h2>Document numbering<span class="rt">
      <select id="fy" style="width:calc(180px * var(--typescale))">
        <option value="">All years (default)</option>
        ${fys.map(f => `<option value="${f}">${f}</option>`).join('')}
      </select></span></h2><div class="bd" id="ctr">
      <div class="empty">Loading…</div>
    </div></div>

    <div class="card"><h2>Firm roles</h2><div class="bd">
      <div class="grid g2">
        <div><label>Orders are taken in</label>
          <select id="order-firm">
            ${S.meta.companies.map(x => `<option value="${x.id}"${x.id === S.meta.order_company ? ' selected' : ''}>${esc(x.name)}${x.tax_mode === 'none' ? ' (no tax)' : ''}</option>`).join('')}
          </select>
          <div class="hint">Every sales order is entered here. The tax bill is
            raised by the firm each customer is allocated to.</div></div>
        <div><label>Billing firms</label>
          <div style="padding:7px 9px;border:1px solid var(--line);border-radius:6px;
            background:var(--line-soft);color:var(--muted);font-size:13.5px;min-height:34px">
            ${esc((S.meta.billing_firms || []).map(f => f.name).join(', ')) || '—'}</div>
          <div class="hint">Any firm whose Tax setting is “GST firm”.</div></div>
      </div>
    </div></div>

    <div class="card"><h2>Delete password</h2><div class="bd" id="delpw">
      <div class="empty">Loading…</div></div></div>

    <div class="card"><h2>Delete old years</h2><div class="bd" id="oldyrs">
      <div class="empty">Loading…</div></div></div>

    <div class="card"><h2>Data &amp; backup</h2><div class="bd">
      <div class="row" style="align-items:center;margin-bottom:14px">
        <div style="flex:1;min-width:260px;font-size:14px">
          <b>Back up everything at once.</b> One file with every firm's books, the
          personal accounts, the settings and passwords, and the Billing program
          itself. Keep it on a pendrive or in Google Drive.
          <div class="hint" id="bk-last" style="margin-top:6px">&nbsp;</div></div>
        <button class="primary" id="bk-btn">⤓ Back up everything</button>
      </div>
      <p class="hint" style="margin:0 0 8px">Single lists, as spreadsheets:</p>
      <div class="row">
        <a class="btn" href="/api/export/ledgers">⤓ Ledgers CSV</a>
        <a class="btn" href="/api/export/items">⤓ Items CSV</a>
        <a class="btn" href="/api/export/trial-balance">⤓ Trial balance CSV</a>
        <a class="btn" href="/api/export/register?doc_type=SALE">⤓ Sales register CSV</a>
        <a class="btn" href="/api/export/outstanding">⤓ Outstanding CSV</a>
      </div>
    </div></div>`;

  /* The password deleting a challan, a bill or an account asks for (4 Oct
     2026): chosen once at this computer, changed here with the old one. */
  async function drawDeletePassword() {
    const here = onScreen();
    let st;
    try { st = await api.get('/api/delete-password'); } catch (e) { return; }
    if (!here()) return;
    $('#delpw').innerHTML = `<div class="row" style="align-items:center">
        <div style="flex:1;font-size:14px">${st.set
          ? '<span class="tag ok">Set</span> Deleting a challan, a bill or an account asks for it.'
          : '<span class="tag warn">Not chosen yet</span> The first delete asks for one to be chosen.'}
          <div class="hint" style="margin-top:6px">Forgotten? Choose a new one at the computer
            Billing runs on: double-click <b>delete-password.bat</b>
            (or run <code>billing.py --set-delete-password</code>).</div></div>
        <button id="delpw-btn"${!st.set && !st.here ? ' disabled title="Choose it on the computer Billing runs on"' : ''}>${
          st.set ? 'Change delete password' : 'Choose delete password'}</button></div>`;
    $('#delpw-btn').onclick = async () => {
      const done = await modal(st.set ? 'Change the delete password' : 'Choose a delete password', `
        <div class="grid ${st.set ? 'g3' : 'g2'}">
          ${st.set ? `<div><label for="dp-cur">Current password</label>
            <input type="password" id="dp-cur" autocomplete="off" spellcheck="false"></div>` : ''}
          <div><label for="dp-new">New password</label>
            <input type="password" id="dp-new" autocomplete="new-password" spellcheck="false"></div>
          <div><label for="dp-new2">The same again</label>
            <input type="password" id="dp-new2" autocomplete="new-password" spellcheck="false"></div>
        </div>
        <p class="hint" style="margin:10px 0 0">${st.min} characters or more.</p>`, [
        { label: 'Cancel', value: false },
        { label: 'Save', value: true, cls: 'primary', before: async bg => {
            const a = $('#dp-new', bg).value, b = $('#dp-new2', bg).value;
            if (a.trim().length < st.min) { bad('At least ' + st.min + ' characters.'); return false; }
            if (a !== b) { bad('The two do not match.'); return false; }
            try {
              await api.post('/api/delete-password',
                { new: a, current: st.set ? $('#dp-cur', bg).value : '' });
            } catch (e) { bad(e.message); return false; }
          } }
      ], { nothingToSave: true, onOpen: passwordKeys });
      if (done) { ok('Delete password saved.'); drawDeletePassword(); }
    };
  }
  drawDeletePassword();

  /* **The old years, deleted** (5 Oct 2026: "Add option to delete account
     transactions year-wise, for all accounts, because the old data is of no
     use"). Every year the books hold, oldest first, with what each firm has
     in it; tick the years to go -- always from the oldest up, ticking a year
     ticks the ones before it -- and every challan, bill, purchase, receipt,
     payment and journal of them goes, in every account. Each account keeps
     its balance as one "Balance brought forward" line on the last day
     deleted (api.drop_years). The year we are in cannot go. */
  async function drawOldYears() {
    const here = onScreen();
    const box = $('#oldyrs');
    let d;
    try { d = await api.get('/api/books/years'); } catch (e) {
      if (here()) box.innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
      return;
    }
    if (!here()) return;
    const firms = d.firms;
    const n = x => Number(x || 0).toLocaleString('en-IN');
    const pick = () => { const v = ($('#oy-firm') || {}).value; return v ? [v] : firms.map(f => f.code); };
    const holds = (y, codes) => codes.some(c => y.firms[c]
      && (y.firms[c].documents || y.firms[c].entries));
    const cell = (y, f) => {
      const c = y.firms[f.code];
      if (!c || !(c.documents || c.entries || c.lines)) return '<span style="color:var(--muted)">—</span>';
      if (!c.documents && !c.entries) {
        return '<span class="tag ok" title="Deleted; each account\'s balance carried forward">deleted</span>';
      }
      // Two short lines, so three firms fit a narrow window (Windows at 125%).
      return n(c.documents) + ' <span style="color:var(--muted)">docs</span><br>'
        + n(c.entries) + ' <span style="color:var(--muted)">entries</span>';
    };
    const years = d.years.filter(y => y.fy <= d.current);
    box.innerHTML = `
      <p style="margin:0 0 10px;font-size:14px">Tick the years that are of no use any more.
        Every challan, bill, purchase, note, receipt, payment and journal dated in them is
        deleted, in every account. <b>Each account keeps its balance</b>: what the deleted
        years added up to stays as one <i>Balance brought forward</i> line on 31 March of the
        last year deleted, so outstanding, the trial balance and stock stay as they are.</p>
      <div class="row" style="align-items:flex-end;margin-bottom:10px">
        <div><label for="oy-firm">Which books</label>
          <select id="oy-firm" style="min-width:calc(240px * var(--typescale))">
            <option value="">All firms — ${esc(firms.map(f => f.code).join(', '))}</option>
            ${firms.map(f => `<option value="${esc(f.code)}">${esc(f.code)} — ${esc(f.name)}</option>`).join('')}
          </select></div>
      </div>
      <div class="hint" style="margin:0 0 6px">In each year: <b>docs</b> are challans, bills,
        purchases and notes; <b>entries</b> are receipts, payments, journals and contras.</div>
      <div class="tbl-scroll"><table><thead><tr>
        <th style="width:34px"></th><th style="width:110px">Year</th>
        ${firms.map(f => `<th>${esc(f.code)}</th>`).join('')}</tr></thead>
        <tbody>${years.map(y => {
          // A year already deleted holds only its brought-forward lines.
          const can = y.deletable && holds(y, firms.map(f => f.code));
          return `<tr data-fy="${esc(y.fy)}"${can ? '' : ' class="muted"'}>
            <td>${can ? `<input type="checkbox" class="oy-tick" data-fy="${esc(y.fy)}" aria-label="Delete ${esc(y.fy)}">` : ''}</td>
            <td class="nw"><b>${esc(y.fy)}</b>${y.deletable ? '' : '<br><span class="tag">this year — kept</span>'}</td>
            ${firms.map(f => `<td class="nw">${cell(y, f)}</td>`).join('')}</tr>`;
        }).join('') || '<tr><td colspan="9" class="empty">No years in the books yet.</td></tr>'}</tbody>
      </table></div>
      <div class="row" style="margin-top:12px;align-items:center">
        <div class="hint" style="margin:0;flex:1">A copy of the books is saved first, and the
          delete password is asked. The SHREE update will not bring the deleted years back
          (Setup → SHREE update lists it among the decisions taken here).</div>
        <button class="danger" id="oy-go" disabled>Delete ticked years…</button></div>`;
    const ticks = () => $$('#oldyrs .oy-tick');
    const newest = () => ticks().filter(t => t.checked).map(t => t.dataset.fy).sort().pop() || '';
    function sayButton() {
      const upto = newest();
      const codes = pick();
      const first = years.find(y => y.deletable && holds(y, codes) && y.fy <= upto);
      const btn = $('#oy-go');
      btn.disabled = !upto || !first;
      btn.textContent = !upto ? 'Delete ticked years…'
        : !first ? 'Nothing left to delete up to ' + upto
        : first.fy === upto ? 'Delete ' + upto + '…'
        : 'Delete ' + first.fy + ' to ' + upto + '…';
      KB.paintBar();
    }
    // Always from the oldest up: a year ticked ticks every year before it,
    // and one unticked unticks every year after it.
    ticks().forEach(t => {
      t.onchange = () => {
        ticks().forEach(o => {
          if (t.checked && o.dataset.fy < t.dataset.fy) o.checked = true;
          if (!t.checked && o.dataset.fy > t.dataset.fy) o.checked = false;
        });
        sayButton();
      };
    });
    $('#oy-firm').onchange = sayButton;
    $('#oy-go').onclick = async () => {
      const upto = newest();
      if (!upto) return;
      const codes = $('#oy-firm').value ? [$('#oy-firm').value] : [];
      let chk;
      try {
        chk = await api.post('/api/books/drop-years', { upto, firms: codes, check: true });
      } catch (e) { bad(e.message); return; }
      if (!chk.documents && !chk.entries) {
        toast('Nothing dated up to ' + dt(chk.last_day) + ' is left to delete.');
        return;
      }
      const last = dt(chk.last_day);
      const lines = chk.firms.filter(f => f.documents || f.entries).map(f =>
        `<li><b>${esc(f.firm)}</b> — ${n(f.documents)} challans, bills and purchases,
          ${n(f.entries)} receipts, payments and journals${f.own
            ? ` (${n(f.own)} of them made in this app)` : ''}</li>`).join('');
      const go = await modal('Delete everything up to ' + last, `
        <p style="margin:0 0 8px;font-size:14px">Every document dated up to
          <b>${esc(last)}</b> is deleted:</p>
        <ul style="margin:0 0 10px;padding-left:20px;font-size:14px">${lines}</ul>
        <p style="margin:0 0 8px;font-size:14px">Each account keeps its balance: what
          these years added up to stays as one <b>Balance brought forward</b> line on
          ${esc(last)}. Outstanding, the trial balance and stock stay as they are.</p>
        <p style="margin:0;font-size:14px">A copy of the books is saved first. This cannot
          be undone from the screen.</p>`, [
        { label: 'Cancel', value: false },
        { label: 'Yes, delete them', value: true, cls: 'danger' }
      ], { yesNo: true });
      if (!go) return;
      const btn = $('#oy-go');
      if (btn) { btn.disabled = true; btn.textContent = 'Deleting…'; }
      let got;
      try {
        got = await api.post('/api/books/drop-years', { upto, firms: codes });
      } catch (e) {
        bad(e.message);
        if (here()) sayButton();
        return;
      }
      invalidate();
      ok('Deleted ' + n(got.documents) + ' documents and ' + n(got.entries)
        + ' entries up to ' + dt(got.last_day) + '. Every balance carried forward.');
      if (here()) drawOldYears();
    };
    sayButton();
  }
  drawOldYears();

  /* **Everything backed up at once** (5 Oct 2026: "Add a option to backup
     everything at once"). One .zip -- the books of every firm with the
     personal accounts, settings and passwords, checked, and the program --
     saved like any download (app/backup.py). Taking a few seconds, the button
     says it is working; the card says when the last one was made, in amber
     once that is more than a week ago. */
  async function drawBackup() {
    const here = onScreen();
    let d = {};
    try { d = await api.get('/api/backup/last'); } catch (e) { d = {}; }
    if (!here()) return;
    if (!d || !d.made) {
      $('#bk-last').innerHTML = '<span class="tag warn">Never</span> No backup has been made here yet.';
      return;
    }
    const days = Math.floor((new Date(today()) - new Date(d.made.slice(0, 10))) / 864e5);
    const ago = days <= 0 ? 'today' : days === 1 ? 'yesterday' : days + ' days ago';
    $('#bk-last').innerHTML = 'Last backup: <span class="tag ' + (days > 7 ? 'warn' : 'ok') + '">'
      + esc(ago) + '</span> ' + esc(dt(d.made.slice(0, 10)) + ' ' + d.made.slice(11, 16))
      + ' · ' + esc(d.name || '') + (d.bytes ? ' (' + (d.bytes / 1048576).toFixed(1) + ' MB)' : '');
  }
  drawBackup();
  $('#bk-btn').onclick = async () => {
    const b = $('#bk-btn');
    if (b.disabled) return;
    const label = b.textContent;
    b.disabled = true;
    b.textContent = 'Making the backup…';
    try {
      const res = await fetch('/api/backup');
      noteBuild(res);
      if (!res.ok) {
        let why = '';
        try { why = (await res.json()).error; } catch (e) { /* not JSON */ }
        throw new Error(why || 'HTTP ' + res.status);
      }
      const named = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '');
      const name = named ? named[1] : 'Billing-backup.zip';
      saveFile(new File([await res.blob()], name, { type: 'application/zip' }));
      ok('Backup saved: ' + name + ' (in Downloads, unless you chose another place). '
        + 'Copy it to a pendrive or Google Drive to keep it safe.');
      drawBackup();
    } catch (e) {
      bad('The backup could not be made: ' + e.message);
    } finally {
      b.disabled = false;
      b.textContent = label;
    }
  };

  bindLogoPicker($('#co'));
  /* Leaving asks only when a box here or in the numbering differs from what
     was drawn -- see unsavedWork(). */
  const formNow = n => JSON.stringify(readForm(n));
  const coWas = formNow($('#co'));
  $('#co').classList.add('own-unsaved');
  $('#co')._unsaved = () => formNow($('#co')) !== coWas;
  const ctrNow = () => $$('#ctr input[data-k]').map(i => i.value).join('\u0001');
  let ctrWas = null;
  $('#ctr').classList.add('own-unsaved');
  $('#ctr')._unsaved = () => ctrWas !== null && ctrNow() !== ctrWas;
  $('#save-co').onclick = async () => {
    const data = readForm($('#co'));
    const st = S.meta.states.find(x => x.code === data.state_code);
    data.state = st ? st.name : '';
    try {
      await api.put('/api/companies/' + c.id, data);
      await loadMeta(true);
      ok('Firm details saved.');
      render();
    } catch (e) { bad(e.message); }
  };

  async function drawCounters() {
    const fy = $('#fy').value;
    const here = onScreen();
    const counters = await api.get('/api/counters', { fy });
    if (!here()) return;
    $('#ctr').innerHTML = `
      <div class="hint" style="margin:0 0 10px">Prefix · next number · digits.
        Four digits gives <b>INV-0007</b>; nought gives a plain <b>7</b>, which
        with a letter in the voucher's Sr box prints as <b>A 7</b>.
        A bill or challan book takes its next number from the last document
        filed in it, so the number here is what starts a book that is still
        empty — a new year, or a new challan book opened with its letter as
        the prefix.</div>
      <div class="grid g3">
        ${counters.map(k => `<div><label>${esc(k.key)}</label>
          <div class="row" style="gap:6px;flex-wrap:nowrap">
            <input data-k="${esc(k.key)}" data-p="1" value="${esc(k.prefix)}" style="flex:1.2" placeholder="prefix">
            <input data-k="${esc(k.key)}" data-n="1" class="num" value="${k.next_no}" style="flex:1">
            <input data-k="${esc(k.key)}" data-d="1" class="num" value="${k.pad}" style="width:52px" title="Digits: 4 gives 0007, 0 gives a plain 7">
          </div><div class="hint">next: ${esc(k.prefix)}${String(k.next_no).padStart(num(k.pad), '0')}</div></div>`).join('')}
      </div>
      <div class="row" style="margin-top:14px">
        <div class="hint" style="margin:0">${fy
          ? 'These apply to documents dated inside ' + esc(fy) + '.'
          : 'Defaults used to seed each new financial year.'}</div>
        <div class="spacer"></div>
        <button class="primary" id="save-ctr">Save numbering</button></div>`;
    ctrWas = ctrNow();
    $('#save-ctr').onclick = async () => {
      const map = {};
      $$('#ctr input[data-k]').forEach(i => {
        const k = i.dataset.k;
        map[k] = map[k] || { key: k };
        if (i.dataset.p) map[k].prefix = i.value;
        else if (i.dataset.d) map[k].pad = num(i.value);
        else map[k].next_no = num(i.value);
      });
      try {
        await api.put('/api/counters', { fy, counters: Object.values(map) });
        ok('Numbering saved.');
        drawCounters();
      } catch (e) { bad(e.message); }
    };
  }
  // Asked first: on Windows an arrow key on this box changes it, and the
  // change is the whole setting -- every order from then on goes there.
  let orderWas = $('#order-firm').value;
  $('#order-firm').onchange = async () => {
    const box = $('#order-firm');
    if (!await confirmBox('Where orders are taken',
        'Take every order in ' + box.selectedOptions[0].text + ' from now on?')) {
      box.value = orderWas;
      return;
    }
    try {
      await api.put('/api/order-firm', { company_id: num(box.value) });
      orderWas = box.value;
      await loadMeta(true);
      ok('Orders will be taken in ' + box.selectedOptions[0].text + '.');
    } catch (e) { bad(e.message); box.value = orderWas; }
  };
  $('#fy').onchange = drawCounters;
  drawCounters();
});

/* ================================================================== boot */

/* A row that only navigates names the document it opens instead of carrying
   an inline onclick. One listener covers every such row however often the
   view is redrawn, and it lets the page be served under a Content-Security-
   Policy that allows no inline script at all. */
document.addEventListener('click', e => {
  if (!e.target.closest) return;
  const row = e.target.closest('[data-open-invoice]');
  if (row) { location.hash = '#/invoice/' + row.getAttribute('data-open-invoice'); return; }
  const acc = e.target.closest('[data-bank]');
  if (acc) { location.hash = '#/bank?account=' + acc.getAttribute('data-bank'); return; }
  // A row that leads somewhere, unless the click was on something of its own.
  const go = e.target.closest('[data-href]');
  if (go) {
    const own = e.target.closest('a[href],button,input,select,textarea,label,summary');
    if (!own || !go.contains(own)) location.hash = go.getAttribute('data-href');
  }
});

/* A link inside a row belongs to the link, not the row: caught on the way
   down, so the row's own click never hears of it. And a half-typed entry is
   not thrown away by one -- the same question Escape asks. */
document.addEventListener('click', async e => {
  const a = e.target.closest && e.target.closest('a.golink');
  if (!a || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
  e.stopPropagation();
  if (!KB.unsavedWork()) return;
  e.preventDefault();
  const href = a.getAttribute('href');
  await leaveAsking('Leave it and open this?', 'Save and open it',
                    () => { location.hash = href; });
}, true);

// The lock screen replaces the whole of #app, taking the menu button with
// it, and this still runs on every resize. Guard both rather than throw.
const menuBtn = $('#menu-btn');
if (menuBtn) menuBtn.onclick = () => $('#sidebar').classList.toggle('open');
const backBtn = $('#back-btn');
if (backBtn) { backBtn.hidden = true; backBtn.onclick = () => KB.leaveScreen(); }
const keysBtn = $('#keys-btn');
if (keysBtn) keysBtn.onclick = () => shortcutHelp();
/* A menu entry leaves the screen, by its key or by the mouse -- so a
   half-typed entry asks first, the same question Esc and every link ask. */
const navEl = $('#nav');
// A group's heading shows its entries, or folds them away again.
if (navEl) navEl.addEventListener('click', e => {
  const h = e.target.closest && e.target.closest('.grp');
  if (!h) return;
  const g = h.closest('.navgrp');
  navShow(g.dataset.grp, !g.classList.contains('open'));
});
if (navEl) navEl.addEventListener('click', async e => {
  const a = e.target.closest && e.target.closest('a[href]');
  if (!a || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
  // The screen this opens sends Esc to the menu (`navRoot`).
  menuChosen = a.getAttribute('href');
  if (!KB.unsavedWork()) return;
  e.preventDefault();
  const name = ($('.nl', a) || a).textContent.trim();
  const href = a.getAttribute('href');
  // Chosen from the menu, however long the save takes in between.
  await leaveAsking('Leave it and open ' + name + '?', 'Save and open ' + name,
                    () => { menuChosen = href; location.hash = href; });
}, true);
function fitMenu() {
  const b = $('#menu-btn');
  if (b) b.style.display = window.innerWidth <= 900 ? '' : 'none';
}
window.addEventListener('resize', fitMenu);
fitMenu();

/* ============================================================== keyboard
   The whole of this app is worked without a mouse, and since 23 Sep 2026
   with one set of keys everywhere:

       ↑ ↓ ← →   go to the next thing that way -- a box, a button, a tick
                 box, a link, a line of a list, a line's own buttons, the
                 buttons at the top of the screen, the menu
       Enter     do what the highlight is on; in a box on a bill, next box
       Space     tick; open a dropdown
       Esc       close what is open; with nothing open, the screen before
       Tab       the next thing in order

   and the keys every Windows program already has: Ctrl+S, Ctrl+P, Ctrl+F,
   Ctrl+A, Delete, F1, F4 and Alt+←. One of the app's own since 27 Sep
   2026, asked for by name: Alt+P prints the challan.

   Asked on 23 Sep 2026: "don't assign different keys to different
   functions. Everything should be controllable by the arrow keys or
   something similar" -- and, asked which of the old keys to keep, none of
   them. Gone: a letter on every button and tick box (Alt and it), the
   numbers on the buttons at the top of a screen and along the foot of a
   dialog, the numbers on a list line's buttons, the key beside every menu
   entry, the Alt keys that went straight to a screen (B O P I D R C N F L
   H K), Ctrl+K, F6, F3, / and ?, Insert, Ctrl+Enter and Ctrl+Del on a
   bill's lines, Y and N, and the numbers in "Bill from which firm?".
   Everything they did is still there to be done: the arrows reach it and
   Enter does it.

   **How the arrows choose** (`nearest`). Among everything the keyboard can
   stand on -- in the dialog on top, or on the screen and the bar above it
   -- the nearest thing that way, keeping to the same column going up and
   down and to the same line going sideways. Going back the way you came
   returns to where you came from (`back`), not to whatever happens to be
   nearest. A list is one thing to the arrows from outside it: they land on
   a line, ↑ ↓ then walk the lines, and → goes along a line to its own
   buttons and links. In a box the arrows move along what is typed first,
   and go on to the next box only from its end -- the way the fields of the
   old FoxPro screens answered. ← with nothing further left goes to the
   menu, and → in the menu comes back.

   Nothing here takes a key away from typing: in a box only the arrows are
   read, and only once they have nowhere left to go inside it. */

const KB = (function () {
  let lastRow = -1;                 // where the cursor stood before a repaint
  let wanted = false;               // asked for the list before it had loaded
  let parked = null;                // the box the screen was landed on meanwhile
  let stepper = null;               // { fn(dir), noun } -- ← and → on this screen
  let escHook = null;               // { can(), run() } -- a screen's own first Esc
  let arriving = false;             // the focus is being moved by an arrow
  let returning = false;            // ... or handed back by a closing dialog

  /** Give the focus back to what a dialog took it from, opening nothing. */
  function handBack(el) {
    returning = true;
    try { el.focus({ preventScroll: true }); } finally { returning = false; }
  }

  /* ------------------------------------------------------------- helpers */

  // On the page, and not folded away: the lines inside a closed "Show the
  // breakdown" still have a place on the page in Chrome (the fold only
  // stops them being drawn), and the cursor used to walk onto them unseen.
  const shown = x => !!x && x.offsetParent !== null
    && (typeof x.checkVisibility !== 'function' || x.checkVisibility());
  const usable = x => shown(x) && !x.disabled;
  const typing = () => {
    const t = document.activeElement;
    return !!(t && t.matches && t.matches('input,select,textarea')
              && t.type !== 'checkbox' && t.type !== 'radio');
  };
  const dialogOpen = () => !!$('#modal-root').firstChild;
  const topDialog = () => $('#modal-root').lastElementChild;
  const label = b => ((b.childNodes[0] && b.childNodes[0].nodeType === 3
    ? b.childNodes[0].textContent : b.textContent) || '').replace(/\s+/g, ' ').trim();
  const isRow = x => !!(x && (x.tagName === 'TR'
    || (x.classList && x.classList.contains('bsline'))));
  const free = x => !x || x === document.body || x === document.documentElement;

  /** Every row a cursor can stand on, in the order they are read.
   *  The line-item grid is left out: it is a grid of boxes, walked box by
   *  box. A folded row's container (`tr.det`) is left out too -- the cursor
   *  goes into the rows inside it -- and the lines of the Profit & Loss and
   *  the Balance Sheet, which are not a table, are in. */
  const ROWS = '#view table:not(.lineitems) > tbody > tr:not(.det), #view .bsline';
  /* One key press asks for the rows several times over -- the move, the
     cursor, the key bar twice -- and on Vouchers each asking walked 2,600
     lines for whether they are on screen: a third of a second a key on a
     slower Windows PC (27 Sep 2026). The answer is now kept until something
     under #view changes. The observer's pending records say so at once, so
     a fold opened in the same key press is seen by the very next asking.
     The cursor moving (`kb` on a row, its tabindex) hides nothing, so that
     alone keeps it. */
  let rowsKept = null, rowsWatch = null;
  const noKb = s => (s || '').replace(/\bkb\b/g, '').trim().replace(/\s+/g, ' ');
  const rowsMoved = recs => recs.some(m => {
    if (m.type !== 'attributes') return true;
    const now = m.target.getAttribute(m.attributeName);
    if (m.oldValue === now) return false;             // written, not changed
    return !(m.target.tagName === 'TR' && (m.attributeName === 'tabindex'
      || (m.attributeName === 'class' && noKb(m.oldValue) === noKb(now))));
  });
  function rows() {
    const view = $('#view');
    if (!rowsWatch && view && window.MutationObserver) {
      rowsWatch = new MutationObserver(recs => { if (rowsMoved(recs)) rowsKept = null; });
      rowsWatch.observe(view, { childList: true, subtree: true, attributes: true,
                                attributeOldValue: true,
                                attributeFilter: ['class', 'style', 'hidden', 'open', 'tabindex'] });
      addEventListener('resize', () => { rowsKept = null; });
    }
    if (rowsWatch && rowsMoved(rowsWatch.takeRecords())) rowsKept = null;
    const whole = document.body.className;
    if (rowsKept && rowsKept.body === whole) return rowsKept.list.slice();
    const list = $$(ROWS).filter(shown);
    if (rowsWatch) rowsKept = { list, body: whole };
    return list.slice();
  }
  function cursor() { return $('#view .kb'); }
  /** The list row a control stands in, if it stands in one. */
  function ownRow(x) {
    const r = x && x.closest && x.closest('tr, .bsline');
    return r && r.matches(ROWS) ? r : null;
  }
  /** Where a list is entered: its first row that opens something, rather
   *  than a heading such as "Bank accounts" or a B/F line -- but only in the
   *  first table. A report whose first table is figures alone (GST, rate by
   *  rate) starts on it, not three tables down the page. */
  function firstRow() {
    const rs = rows();
    const table = r => r.closest('table');
    const doing = rs.find(r => rowEnter(r) || rowActions(r).length);
    return doing && rs.length && table(doing) === table(rs[0]) ? doing : rs[0];
  }

  /** The buttons and links of one row -- for Ctrl+P, and for where a list
   *  is entered. */
  function rowActions(tr) {
    return $$('button', tr).filter(usable).concat($$('a[href]', tr).filter(usable));
  }
  function rowBox(tr) {
    return $$('input[type=checkbox]', tr).filter(shown)[0] || null;
  }
  /** The tick box at the head of a list, that ticks every row. */
  function headBox() {
    return $$('#view thead input[type=checkbox]').filter(usable)[0] || null;
  }
  /** A row that folds open onto the rows behind it: its caret says which.
   *  Most fold on their own click; a row that leads somewhere else when it
   *  is chosen (a customer on Who owes us) folds through `tr._fold`. */
  function folds(tr) {
    const caret = $('.caret', tr);
    const toggle = tr._fold || (tr.onclick ? () => tr.click() : null);
    return caret && toggle
      ? { open: caret.classList.contains('open'), toggle, noun: tr.dataset.fold || '' }
      : null;
  }

  /** What Enter does on this row, and what to call it along the foot.
   *
   *  Its own handler first, then the delegated attributes a navigating row
   *  carries, then the button it names as the one Enter should run, then a
   *  row whose only control is a dropdown (Customer Allocation), and last a
   *  row whose only way anywhere is a link -- a line of the Profit & Loss,
   *  a bill on the GST screen. A row with none of those answers null and the
   *  bar does not offer Enter at all. */
  function rowEnter(tr) {
    // A row that folds but leads somewhere: Enter goes there, → ← fold it.
    if (tr._fold && tr.matches('[data-href]')) {
      return { label: tr.dataset.enterLabel || 'open',
               run: () => { location.hash = tr.getAttribute('data-href'); } };
    }
    if (tr.onclick || tr.matches('[data-open-invoice],[data-bank],[data-href]')) {
      const f = folds(tr);
      return { label: f ? (f.open ? 'close' : 'open') : 'open', run: () => tr.click() };
    }
    const named = $('[data-enter]', tr);
    if (usable(named)) {
      return { label: (label(named) || 'open').toLowerCase(), run: () => named.click() };
    }
    const sel = $$('select', tr).filter(usable)[0];
    if (sel) return { label: 'change', run: () => { sel.focus(); openList(sel); } };
    const link = $$('a[href]', tr).filter(usable)[0];
    if (link) return { label: 'open', run: () => link.click() };
    return null;
  }

  function setCursor(tr, quiet) {
    $$('#view .kb').forEach(x => {
      x.classList.remove('kb');
      x.removeAttribute('tabindex');
    });
    if (!tr) {
      lastRow = -1;
      document.body.classList.remove('kb-on');
      paintBar();
      return;
    }
    document.body.classList.add('kb-on');
    tr.classList.add('kb');
    tr.tabIndex = -1;
    if (!quiet) tr.focus({ preventScroll: true });
    // Into view up and down -- and sideways only inside a table that scrolls
    // on its own. A card a little wider than a narrow window used to slide
    // the whole page left, menu and all, the moment a screen opened. A line
    // stood on quietly -- a list redrawn under a box being typed in -- does
    // not take the page away from the box that has the keyboard.
    const ae = document.activeElement;
    if (!quiet || free(ae) || tr.contains(ae)) reveal(tr);
    lastRow = rows().indexOf(tr);
    paintBar();
  }

  /** The first line of the list, or the last (3 Oct 2026: "add shortcut
   *  command to scroll up or down till the last entry, for a long list of
   *  entries"). A list drawn a part at a time -- a bank book's All entries,
   *  1,000 lines a go -- is drawn whole first, so the last line is the last
   *  entry and not the last one drawn so far. */
  function toEnd(last) {
    if (last) {
      const rest = $$('#view [data-draw-rest]').filter(usable)[0];
      if (rest) rest.click();
    }
    const rs = rows();
    if (!rs.length) return false;
    setCursor(last ? rs[rs.length - 1] : rs[0]);
    return true;
  }

  function move(delta) {
    const rs = rows();
    if (!rs.length) return false;
    const at = rs.indexOf(cursor());
    const i = at < 0 ? (delta > 0 ? 0 : rs.length - 1) : at + delta;
    setCursor(rs[Math.max(0, Math.min(rs.length - 1, i))]);
    return true;
  }

  /* ------------------------------------------ what a screen can be asked */

  /** The screen's search box, or the dialog's. */
  function searchBox() {
    const dlg = topDialog();
    if (dlg) {
      return $$('input', dlg).filter(x => usable(x) && !x.readOnly
        && /^(text|search|)$/.test(x.type || ''))[0] || null;
    }
    return [$('#view #q'), $('#view #search'), $('#view #st-find'), $('#view #p')]
      .filter(x => usable(x))[0] || null;
  }
  /** The button that saves what is on screen. */
  function saveButton() {
    const own = ['#save', '#j-save', '#save-co'].map(s => $('#view ' + s))
      .filter(usable)[0];
    if (own) return own;
    // SHREE's Update now is not a save: a fetch from SHREE starts only when
    // that button itself is pressed (24 Sep 2026).
    return $$('#page-actions button.primary').filter(usable)
      .find(b => /^(save|update|book)\b/i.test(label(b))
        && !/^update now\b/i.test(label(b))) || null;
  }
  /** The button that prints what is on screen. */
  function printButton() {
    // The rows' own buttons were read and measured one by one before being
    // passed over -- 5,300 of them on Vouchers, twice a key press. The
    // walk below never goes into a list's rows at all.
    const out = $$('#page-actions button'), view = $('#view');
    if (view) {
      const walk = document.createTreeWalker(view, NodeFilter.SHOW_ELEMENT, {
        acceptNode: n => n.tagName === 'TBODY' ? NodeFilter.FILTER_REJECT
          : n.tagName === 'BUTTON' ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP });
      for (let b = walk.nextNode(); b; b = walk.nextNode()) out.push(b);
    }
    return out.find(b => /\bprint\b/i.test(label(b)) && usable(b)) || null;
  }
  /** What Alt+P does: print the challan (27 Sep 2026: "add a shortcut key
   *  to print challan as well"). On a list, the highlighted line's -- a
   *  line says how (`tr._challan`), has a Challan button of its own, or is
   *  itself a link to a bill or challan; on a bill or a challan, the screen's
   *  own 🖨 Challan / 🖨 Print challan. Null where there is none. */
  function challanPrint() {
    if (dialogOpen()) return null;
    const tr = cursor();
    if (tr) {
      if (typeof tr._challan === 'function') return tr._challan;
      const own = $$('button', tr).find(b => /^challan$/i.test(label(b)) && usable(b));
      if (own) return () => own.click();
      const m = (tr.getAttribute('data-href') || '').match(/^#\/invoice\/(\d+)(?:\?firm=(\d+))?/);
      if (m) return () => printInvoice(num(m[1]), 'challan', m[2] ? num(m[2]) : undefined);
      const id = tr.getAttribute('data-open-invoice');
      if (id) return () => printInvoice(num(id), 'challan');
      // A line of an account, the day book or a bank book names its entry.
      if (/^(SALE|PURCHASE|CREDIT|DEBIT)$/.test(tr.dataset.kind || '') && tr.dataset.doc) {
        return () => printInvoice(num(tr.dataset.doc), 'challan',
                                  tr.dataset.firm ? num(tr.dataset.firm) : undefined);
      }
    }
    const b = $$('#page-actions button')
      .find(x => /🖨/.test(x.textContent) && /challan/i.test(x.textContent) && usable(x));
    return b ? () => b.click() : null;
  }
  /** An entry screen: one being typed into, with an Enter walk of its own. */
  const entryScreen = () => !!($('#view').onkeydown || $('#view #save')
                               || $('#view #j-save') || $('#view #save-co'));
  /** Is Enter in this box the Enter walk's -- a bill, an order, a journal,
   *  a form in a dialog -- rather than something to be done to the box? */
  function inWalk(x) {
    const dlg = topDialog();
    if (dlg && dlg.contains(x)) return isFormDialog(dlg);
    return !!($('#view').onkeydown && $('#view').contains(x));
  }

  /** A letter typed on a list, put into its search box as if typed there.
   *  The value goes in before the focus does: an empty customer box opens
   *  its whole list on being reached, and this is a search, not a browse. */
  function typeInto(box, ch) {
    box.value = ch;
    box.focus();
    try { box.setSelectionRange(ch.length, ch.length); } catch (e) { /* a date */ }
    box.dispatchEvent(new Event('input', { bubbles: true }));
  }

  /** Open a dropdown's list, where the browser can be asked to. */
  function openList(sel) {
    if (!sel || sel.disabled || typeof sel.showPicker !== 'function') return false;
    try { sel.showPicker(); return true; } catch (e) { return false; }
  }

  /** ← and →: a screen read a month at a time says how to move. */
  function setStepper(fn, noun) { stepper = { fn, noun: noun || 'month' }; paintBar(); }
  /** A screen that opens something inside itself -- one customer's account
   *  out of the list on Accounts -- takes the first Esc to close it again,
   *  before Esc goes back a screen or to the menu. `can()` says whether it
   *  has anything open; `run()` closes it. Cleared when the screen changes. */
  function setEscape(can, run) { escHook = { can, run }; paintBar(); }
  const escOwn = () => !!(escHook && escHook.can());
  function stepButtons() {
    return { prev: $('#view #m-prev'), next: $('#view #m-next') };
  }
  function canStep() {
    const b = stepButtons();
    return !!(stepper || usable(b.prev) || usable(b.next));
  }
  function step(dir) {
    if (stepper) return !!stepper.fn(dir);
    const b = stepButtons()[dir > 0 ? 'next' : 'prev'];
    if (usable(b)) { b.click(); return true; }
    return false;
  }

  /* ----------------------------------------------- what the arrows reach */

  const STOP = 'a[href], button, input:not([type=hidden]), select, textarea, summary, '
    + '[tabindex]:not([tabindex="-1"]), .pickbtn';
  /** Can the arrows stand on it? Shown, working, and not read-only -- the
   *  address and balance under a customer are the app's to fill, not
   *  yours. The ▾ beside a customer or product box is a stop although Tab
   *  steps over it: → from the box reaches it, and Enter opens the list. */
  const stopOk = x => usable(x) && !x.readOnly && !x.closest('[hidden]')
    && !x.matches('[data-x], .dtbtn');

  /** A row's own things, left to right: its buttons, its links, a dropdown
   *  or the boxes of a salary line. Not the tick box a list is ticked with
   *  -- Space on the row does that -- and, on a screen read a month at a
   *  time, not its links, so that ← → there go on turning the month. */
  function rowStops(tr) {
    const all = $$(STOP, tr).filter(x => ownRow(x) === tr && stopOk(x));
    const boxes = all.some(x => x.matches('input:not([type=checkbox]):not([type=radio]), textarea'));
    const box = boxes ? null : rowBox(tr);
    const links = !canStep();
    return all.filter(x => x !== box && (links || x.tagName !== 'A'));
  }

  /** Everything the arrows can stand on outside the rows of a list: in the
   *  dialog on top, or on the screen and the bar above it. */
  function looseStops() {
    const dlg = topDialog();
    if (dlg) return $$(STOP, $('.modal', dlg) || dlg).filter(stopOk);
    const out = $$(STOP, $('.topbar')).filter(stopOk);
    $$(STOP, $('#view')).forEach(x => {
      // The tick box at the head of a list is Ctrl+A's; a list's own lines
      // are reached through the list.
      if (!ownRow(x) && !x.closest('thead') && stopOk(x)) out.push(x);
    });
    return out;
  }
  /** And the rows, as a list is entered: all of them on an ordinary list,
   *  the first and last of each on a long one (and the one the cursor was
   *  on), so that a key press never measures three thousand rows. */
  function candidates() {
    const out = looseStops();
    if (dialogOpen()) return out;
    const rs = rows();
    if (rs.length <= 600) return out.concat(rs);
    const ends = new Map();
    rs.forEach(r => {
      const p = r.parentElement;
      if (!ends.has(p)) ends.set(p, [r, r]); else ends.get(p)[1] = r;
    });
    ends.forEach(([a, b]) => { out.push(a); if (b !== a) out.push(b); });
    if (cursor()) out.push(cursor());
    return out;
  }

  /** Is `b` that way from `a`? A little overlap is forgiven: a tick box
   *  standing beside a taller box is beside it, not below it. */
  function inDir(a, b, dir) {
    const slack = (p, q) => Math.min(8, p / 3, q / 3);
    if (dir === 'down') return b.top >= a.bottom - slack(a.height, b.height);
    if (dir === 'up') return b.bottom <= a.top + slack(a.height, b.height);
    if (dir === 'right') return b.left >= a.right - slack(a.width, b.width);
    return b.right <= a.left + slack(a.width, b.width);
  }
  /** How far, the way the eye reads it. Up and down lean to the column --
   *  sideways distance counts twice over, enough to go down the left-hand
   *  column of a bill's header rather than across to the right one, not so
   *  much that ↑ from a line of the bill skips the header for the bar at the
   *  top -- and sideways keeps to the line: something on another line is not
   *  "to the right". */
  function distance(a, b, dir) {
    const vert = dir === 'up' || dir === 'down';
    const gap = dir === 'down' ? b.top - a.bottom : dir === 'up' ? a.top - b.bottom
      : dir === 'right' ? b.left - a.right : a.left - b.right;
    const off = vert
      ? Math.max(0, Math.max(a.left, b.left) - Math.min(a.right, b.right))
      : Math.max(0, Math.max(a.top, b.top) - Math.min(a.bottom, b.bottom));
    if (!vert && off > 2) return Infinity;
    const align = vert ? Math.abs(b.left - a.left)
      : Math.abs((b.top + b.bottom) - (a.top + a.bottom)) / 2;
    return Math.max(0, gap) + off * 2 + align * 0.02;
  }
  /** Where a thing is on the page -- not in the window. The bar at the top
   *  stays put while the page scrolls under it, so measured in the window it
   *  would always be the nearest thing above, and ↑ from a box on a scrolled
   *  page would jump to it past the box just above. It is counted as the top
   *  of the page, which is where it belongs. */
  function place(x) {
    const r = x.getBoundingClientRect();
    const dy = dialogOpen() || x.closest('.topbar') ? 0 : window.scrollY;
    return { left: r.left, right: r.right, top: r.top + dy, bottom: r.bottom + dy,
             width: r.width, height: r.height };
  }
  /** The card a thing sits in -- what makes the columns of a screen laid
   *  out in cards, as the Dashboard is. The bar at the top is not one: its
   *  buttons stand over the column they are drawn over. */
  const CARD = '.card, .kpi, .modal, #sidebar';
  const cardOf = x => (x.closest && x.closest(CARD)) || x;
  function nearest(from, dir, cands) {
    const a = place(from);
    const vert = dir === 'up' || dir === 'down';
    const home = vert ? place(cardOf(from)) : null;
    const cards = new Map();            // a list's lines share one card
    const cardAt = c => {
      const k = cardOf(c);
      if (!cards.has(k)) cards.set(k, place(k));
      return cards.get(k);
    };
    let best = null, least = Infinity, col = null, colLeast = Infinity;
    cands.forEach(c => {
      if (c === from || c.contains(from) || from.contains(c)) return;
      const b = place(c);
      if (!b.width && !b.height) return;
      if (!inDir(a, b, dir)) return;
      const d = distance(a, b, dir);
      if (d < least) { least = d; best = c; }
      if (vert && d < colLeast && stacked(home, cardAt(c))) { colLeast = d; col = c; }
    });
    // Up and down keep to the column of cards they are in, when there is
    // anything in it within a screen or so: ↓ from the Dashboard's sales
    // chart went to the bank list in the card beside it, a little nearer,
    // rather than down to Recent invoices under the chart (5 Oct 2026).
    if (col && col !== best && colLeast < Math.max(600, least * 3)) return col;
    return best;
  }

  /** The part of the window where a thing can actually be read, top and
   *  bottom. On the page: below the bar at the top, and below whatever is
   *  held in view under it -- a list's own headings, the bar of ticked rows
   *  -- and above the bars along the foot: the keys, "the app has been
   *  updated", "the books have changed". In a dialog: the dialog's own
   *  scrolling layer, less the keys, which are drawn over it. */
  function clearArea(x) {
    let top = 0, bottom = window.innerHeight;
    const bg = x.closest('.modal-bg');
    if (bg) {
      const r = bg.getBoundingClientRect();
      top = Math.max(0, r.top);
      bottom = Math.min(bottom, r.bottom);
    } else {
      const bar = $('.topbar');
      if (bar) top = bar.getBoundingClientRect().bottom;
      // A list's headings count only while they are held and the lines are
      // passing under them; left where they were drawn they cover nothing.
      const tbl = x.closest('table');
      const head = tbl && tbl.tHead, body = tbl && tbl.tBodies[0];
      const held = $$('#view .sel-bar');
      if (head && body && !head.contains(x)
          && body.getBoundingClientRect().top < head.getBoundingClientRect().bottom - 1) {
        held.push(head);
      }
      held.forEach(h => {
        if (!shown(h) || h.contains(x)) return;
        const r = h.getBoundingClientRect();
        if (r.top <= top + 2 && r.bottom > top) top = r.bottom;
      });
    }
    // Fixed to the window, these have no offsetParent even when showing.
    ['#kbbar', '#update-bar', '#books-bar'].forEach(s => {
      const b = $(s);
      if (!b || !b.getClientRects().length || b.contains(x)) return;
      const r = b.getBoundingClientRect();
      if (r.height && r.top < bottom) bottom = r.top;
    });
    return { top, bottom };
  }

  /** Into view -- all of it, where it can be read, not under any of the bars
   *  `clearArea` names -- moving the page as little as will do it, and never
   *  sliding it sideways. A table or a list that scrolls on its own is
   *  scrolled first. Taller than the window, its top is shown. */
  function reveal(x) {
    if (!x || !x.isConnected || !x.getBoundingClientRect || !shown(x)) return;
    const sx = window.scrollX;
    x.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    if (window.scrollX !== sx) window.scrollTo(sx, window.scrollY);
    // The menu stops above the keys; the bar at the top and a lookup list
    // are always in the window.
    if (x.closest('#sidebar, .topbar, .pick-list')) return;
    const a = clearArea(x);
    const r = x.getBoundingClientRect();
    const pad = Math.min(8, Math.max(0, (a.bottom - a.top - r.height) / 2));
    let dy = 0;
    if (r.top < a.top + pad || r.height > a.bottom - a.top) dy = r.top - a.top - pad;
    else if (r.bottom > a.bottom - pad) dy = r.bottom - a.bottom + pad;
    if (Math.abs(dy) < 1) return;
    const bg = x.closest('.modal-bg');
    if (bg) bg.scrollTop += dy;
    else window.scrollBy(0, dy);
  }

  /** On a form worked box by box -- a challan, a bill, an order, a voucher,
   *  a form in a dialog -- the page goes along with the box the keyboard is
   *  on (10 Oct 2026: "the page should also automatically scroll along with
   *  the current selected entry box"). Only just in view, the box went down
   *  the window until it stood on the keys along the foot, the line under
   *  it, ＋ Add line and the totals out of sight below, and a steel box's
   *  list had to open upwards over the line above. So the box is kept off
   *  the foot by some 40% of the window -- room for what comes next and for
   *  a list dropped from it -- and off the top by a little, for Shift+Enter
   *  and ↑ going back up. Where the page ends it stops; the box is still in
   *  view. */
  function follow(x) {
    reveal(x);
    if (!x || !x.isConnected || !shown(x) || !inWalk(x)) return;
    const a = clearArea(x);
    const room = a.bottom - a.top;
    // A box with a list (`attachPicker`) wants room for it to drop open
    // under the box rather than over the line above.
    const ahead = Math.min(300, Math.max(x.dataset.picker ? 220 : 120, room * 0.4));
    const behind = Math.min(120, room * 0.2);
    const r = x.getBoundingClientRect();
    if (r.height > room - ahead - behind) return;
    let dy = 0;
    if (r.bottom > a.bottom - ahead) dy = r.bottom - (a.bottom - ahead);
    else if (r.top < a.top + behind) dy = r.top - (a.top + behind);
    if (Math.abs(dy) < 1) return;
    const bg = x.closest('.modal-bg');
    if (bg) bg.scrollTop += dy;
    else window.scrollBy(0, dy);
  }

  /** A line folded open shows what it opened -- as much of it as the window
   *  holds, never pushing the line itself up out of sight. What is inside
   *  can come from the server a moment later, so it is looked at again then;
   *  a key, a click or the wheel in the meantime and it is left alone. */
  function showFold(tr) {
    let live = true;
    const EVENTS = ['wheel', 'touchstart', 'keydown', 'mousedown'];
    const stop = () => { live = false; EVENTS.forEach(e => removeEventListener(e, stop, true)); };
    EVENTS.forEach(e => addEventListener(e, stop, { capture: true, passive: true }));
    const look = () => {
      if (!live || cursor() !== tr || !tr.isConnected) return;
      const det = tr.nextElementSibling;
      if (!det || !det.matches('tr.det') || !shown(det)) return;
      const a = clearArea(tr);
      const over = det.getBoundingClientRect().bottom - (a.bottom - 8);
      const room = tr.getBoundingClientRect().top - (a.top + 8);
      if (over > 0 && room > 0) window.scrollBy(0, Math.min(over, room));
    };
    look();
    [120, 400, 900].forEach(ms => setTimeout(look, ms));
    setTimeout(stop, 1000);
  }

  /* The way back. Going ↓ from the Year box into a list and ↑ again comes
     back to the Year box, not to whichever box happens to sit nearest the
     top of the list. Only the last move is remembered: an arrow that goes
     anywhere else forgets it. */
  let lastMove = null;              // { from, to, dir }
  const OPP = { up: 'down', down: 'up', left: 'right', right: 'left' };

  function moveTo(t, from, dir) {
    // Walked down into a list from a box or a button above it: ↑ off the
    // list's first line comes back here (`doorOf`).
    if (isRow(t) && dir === 'down' && from && !isRow(from) && !ownRow(from)) door = from;
    if (isRow(t)) setCursor(t);
    else {
      arriving = true;
      try { t.focus({ preventScroll: true }); } finally { arriving = false; }
      if (t.tagName === 'INPUT' && t.select && /^(text|search|)$/.test(t.type || '')) t.select();
      follow(t);
    }
    lastMove = from && dir ? { from, to: t, dir } : null;
    paintBar();
    return true;
  }
  /** The nearest thing that way -- or the way back, if that is where the
   *  last arrow came from. A customer or product box and the ▾ drawn over
   *  its right-hand end are one pair: → from the box is the ▾, ← from the ▾
   *  the box. */
  function spatial(from, dir) {
    const m = lastMove;
    if (m && m.to === from && OPP[m.dir] === dir && m.from.isConnected
        && (isRow(m.from) ? shown(m.from) : stopOk(m.from))
        && (!dialogOpen() || topDialog().contains(m.from))) {
      return moveTo(m.from, from, dir);
    }
    const pair = from.closest && from.closest('.pickbox');
    if (pair) {
      const box = $('input', pair), btn = $('.pickbtn', pair);
      if (dir === 'right' && from === box && btn && stopOk(btn)) return moveTo(btn, from, dir);
      if (dir === 'left' && from === btn && box && stopOk(box)) return moveTo(box, from, dir);
    }
    let t = nearest(from, dir, candidates().filter(c => !(pair && pair.contains(c))));
    if (t && dir === 'down') t = lineStart(t, from) || t;
    return t ? moveTo(t, from, dir) : false;
  }
  /** Coming down onto a line of a bill, a challan or an order from the
   *  boxes above it, the keyboard starts the line at its first box -- the
   *  steel type, the product -- as the Enter walk does. Measured by place
   *  alone, ↓ from Payment Terms landed on the challan line's Quality box,
   *  the box that happened to stand under it (5 Oct 2026). */
  function lineStart(t, from) {
    const line = t.closest && t.closest('.lineitems tbody tr');
    if (!line || line.contains(from)) return null;
    // The boxes themselves, not the ▾ drawn over a box's right-hand end:
    // on a narrow window (Windows at 125%) the steel boxes are so tight that
    // the ▾ starts left of its own box, and was taken for the first one.
    const all = $$(STOP, line).filter(stopOk);
    const boxes = all.filter(x => !x.matches('.pickbtn, button'));
    const stops = boxes.length ? boxes : all;
    if (!stops.length) return null;
    const at = x => place(x);
    const top = Math.min(...stops.map(x => at(x).top));
    return stops.filter(x => at(x).top <= top + 6)
      .sort((p, q) => at(p).left - at(q).left)[0] || null;
  }

  /** In a box, the arrows move along what is typed; only from its end do
   *  they go on to the next box. A box just arrived in has all of it
   *  chosen, so the first arrow goes straight on. Up and down go on at
   *  once, but for the lines of a note. A dropdown, a date and a tick box
   *  are left at once whichever way: an arrow never changes one. */
  function leavesBox(x, dir) {
    if (x.tagName === 'SELECT' || x.type === 'checkbox' || x.type === 'date'
        || x.type === 'month') return true;
    const v = x.value || '';
    let s, e;
    try { s = x.selectionStart; e = x.selectionEnd; } catch (err) { return true; }
    if (s === null || s === undefined) return true;
    const all = s === 0 && e === v.length;
    if (x.tagName === 'TEXTAREA' && (dir === 'up' || dir === 'down')) {
      if (all) return true;
      return dir === 'up' ? v.lastIndexOf('\n', s - 1) < 0 : v.indexOf('\n', e) < 0;
    }
    if (dir === 'up' || dir === 'down') return true;
    if (all) return true;
    return dir === 'left' ? s === 0 && e === 0 : s === v.length && e === v.length;
  }

  /* ------------------------------------------- when nothing is that way --
     24 Sep 2026: *"There are still some errors in the arrow key controls.
     Fix it and make it user friendly."* Three places an arrow used to do
     nothing, or go somewhere nobody expected: */

  /** On a statement read in two columns -- expenses beside income -- the
   *  lines of the two sides are seldom at exactly the same height, so → from
   *  a line on the left found nothing "on its line" and stood still. It
   *  goes to the line across from it: the nearest one that way. */
  function across(tr, dir) {
    const a = place(tr);
    const mid = (a.top + a.bottom) / 2;
    let best = null, least = Infinity;
    rows().forEach(r => {
      if (r === tr) return;
      const b = place(r);
      if (!inDir(a, b, dir)) return;
      const d = Math.abs((b.top + b.bottom) / 2 - mid);
      if (d < least) { least = d; best = r; }
    });
    if (!best) return false;
    lastMove = null;
    setCursor(best);
    return true;
  }
  /** Inside a customer's opened panel -- the "Bills and payments →" under
   *  their unpaid bills -- ← with nothing to its left went all the way out
   *  to the menu. It goes back to the customer's own line, the way ← from
   *  one of the bills in the panel always did. */
  function toFoldRow(x) {
    const det = x && x.closest && x.closest('tr.det');
    const up = det && det.previousElementSibling;
    if (!up || !isRow(up) || !shown(up)) return false;
    lastMove = null;
    setCursor(up);
    return true;
  }

  /* ---------------------------------- made plainer for a new user ------
     5 Oct 2026: *"the navigation or control of the software with the arrow
     keys isn't that user friendly for new users ... make it smooth and
     easier."* What a first-time user tripped on, key by key: */

  /** Two things one above the other on the screen -- sharing a column --
   *  rather than side by side. */
  const stacked = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 8;
  /** Is the next line in reading order a list standing beside this one --
   *  another card on the Dashboard -- rather than the line below? The two
   *  sides of one statement (Outstanding, the Profit & Loss, the Balance
   *  Sheet) are read on from the foot of the left side to the head of the
   *  right, as they always were; → and ← go straight across. */
  function otherColumn(tr, next) {
    if (next.parentElement === tr.parentElement) return false;
    const sheet = tr.closest('.twocol, .osheets');
    if (sheet && sheet.contains(next)) return false;
    return !stacked(place(tr), place(next));
  }
  /** The nearest thing that way in the same column as `x`: the list under
   *  a card on the Dashboard, not the one beside it. */
  function inColumn(x, dir) {
    const a = place(x);
    return nearest(x, dir, candidates().filter(c => stacked(a, place(c))));
  }

  /** The box a list was walked into from, so that ↑ off its first line goes
   *  back there: ↓ from the search box onto Challans' first line and ↑
   *  again is the search box, not the Type dropdown at the list's left-hand
   *  end. With none remembered, a search box above the list. */
  let door = null;
  function doorOf(tr) {
    if (dialogOpen()) return null;
    const top = place(tr).top;
    const above = x => x && x.isConnected && stopOk(x) && place(x).bottom <= top + 2;
    if (door && above(door)) return door;
    const sb = searchBox();
    return above(sb) ? sb : null;
  }

  /** → on a line that leads somewhere -- a bank account, a month of an
   *  account, a challan -- and has nothing of its own to go along to: in,
   *  as Enter goes. Its "›" says it opens, and → is the key that reads
   *  that way; it used to do nothing at all. A line that is ticked rather
   *  than opened is left to Space and Enter. */
  function openOn(tr) {
    const box = rowBox(tr);
    if (box && !box.classList.contains('sel')) return false;
    if (!tr.matches('.clickable,[data-href],[data-open-invoice],[data-bank]')) return false;
    const go = rowEnter(tr);
    if (!go) return false;
    go.run();
    paintBar();
    return true;
  }

  /** A form reads like a page: → past the last box of a line goes on to
   *  the first box of the next line, and ← before the first box of a line
   *  back to the last box of the line above -- on a bill, an order, a
   *  voucher, and in every dialog. It used to stop dead at the end of a
   *  line (the challan's "＋ New party", the Driver box). Lists keep ← for
   *  the menu, and the menu is still ← from the very first box. */
  function wrap(x, dir) {
    if (dir !== 'left' && dir !== 'right') return false;
    const dlg = topDialog();
    if (!dlg && !$('#view').onkeydown) return false;
    const scope = dlg || $('#view');
    const a = place(x);
    const cands = (dlg ? looseStops() : looseStops().filter(c => scope.contains(c)))
      .filter(c => c !== x && !c.contains(x) && !x.contains(c))
      .map(c => [c, place(c)]).filter(([, b]) => b.width || b.height);
    const down = dir === 'right';
    const lineOf = cands.filter(([, b]) => inDir(a, b, down ? 'down' : 'up'));
    if (!lineOf.length) return false;
    // The next line: the nearest edge that way, and everything sharing it.
    const edge = down ? Math.min(...lineOf.map(([, b]) => b.top))
                      : Math.max(...lineOf.map(([, b]) => b.bottom));
    const line = lineOf.filter(([, b]) => (down ? b.top <= edge + 6 : b.bottom >= edge - 6));
    line.sort((p, q) => (down ? p[1].left - q[1].left : q[1].right - p[1].right));
    return moveTo(line[0][0], x, dir);
  }

  /** A row the cursor stands on. ↑ ↓ the next row -- or something standing
   *  between the two, such as the button in a customer's opened panel. → into
   *  the row: open it if it folds, else along to its own buttons and links,
   *  else the next month on a screen read a month at a time. ← the other
   *  way, and with nothing to go back to, the menu. */
  function rowArrow(tr, dir) {
    if (dir === 'up' || dir === 'down') {
      const rs = rows();
      const next = rs[rs.indexOf(tr) + (dir === 'down' ? 1 : -1)];
      if (!next) {
        // ↑ off the top of the list goes back to the box it was entered
        // from -- the search box, as a rule -- not to whatever stands
        // nearest the list's left-hand end (the Type dropdown on Challans).
        const back = dir === 'up' && doorOf(tr);
        return back ? moveTo(back, tr, dir) : spatial(tr, dir);
      }
      const a = place(tr), b = place(next);
      const gap = dir === 'down' ? b.top - a.bottom : a.top - b.bottom;
      // The next list on the page standing in another column -- the
      // Dashboard's cards side by side, the other half of a statement -- is
      // not the next line. ↓ off the foot of "Cash & bank" went across and
      // up to "Recent invoices"; it goes to what is under it on the screen.
      if (otherColumn(tr, next)) {
        const t = inColumn(tr, dir);
        if (t) return moveTo(t, tr, dir);
        return false;
      }
      if (gap > 4 || next.parentElement !== tr.parentElement) {
        const t = nearest(tr, dir, looseStops());
        if (t) {
          const c = place(t);
          if (dir === 'down' ? c.top < b.top : c.bottom > b.bottom) return moveTo(t, tr, dir);
        }
      }
      lastMove = null;
      setCursor(next);
      return true;
    }
    const f = folds(tr);
    const inside = rowStops(tr);
    if (dir === 'right') {
      if (f && !f.open) { f.toggle(); paintBar(); showFold(tr); return true; }
      if (inside.length) return moveTo(inside[0], tr, 'right');
      if (f && f.open) return move(1);
      if (canStep() && step(1)) return true;
      return spatial(tr, 'right') || across(tr, 'right') || openOn(tr);
    }
    if (f && f.open) { f.toggle(); paintBar(); return true; }
    const det = tr.closest('tr.det');
    const parent = det ? det.previousElementSibling
      : tr.dataset.for && $('#view tr.ogrp[data-g="' + tr.dataset.for + '"]');
    if (parent && shown(parent)) { setCursor(parent); return true; }
    if (canStep() && step(-1)) return true;
    return spatial(tr, 'left') || across(tr, 'left') || go('menu');
  }

  /** A button, link or box inside a row. ← → along the row, and ← off its
   *  first thing back onto the row; ↑ ↓ the thing standing in the same
   *  place in the row above or below -- a salary sheet is walked like a
   *  sheet of paper. */
  function inRowArrow(x, tr, dir) {
    const list = rowStops(tr);
    const i = list.indexOf(x);
    if (dir === 'left' || dir === 'right') {
      const j = i + (dir === 'right' ? 1 : -1);
      if (i < 0 || j < 0) { setCursor(tr); return true; }
      if (list[j]) return moveTo(list[j], x, dir);
      // Past the last link of a line on a statement in two columns: across
      // to the other side, as → from the line itself goes.
      if (dir === 'right') across(tr, 'right');
      return true;
    }
    const rs = rows();
    const n = rs[rs.indexOf(tr) + (dir === 'down' ? 1 : -1)];
    if (!n || otherColumn(tr, n)) {
      setCursor(tr);
      return rowArrow(tr, dir);
    }
    const a = x.getBoundingClientRect();
    const mid = (a.left + a.right) / 2;
    let t = null, least = Infinity;
    rowStops(n).forEach(c => {
      const b = c.getBoundingClientRect();
      const d = Math.abs((b.left + b.right) / 2 - mid);
      if (d < least) { least = d; t = c; }
    });
    if (!t) { setCursor(n); return true; }
    setCursor(n, true);
    return moveTo(t, x, dir);
  }

  /** The dispatcher for the four arrows, on a screen or in a dialog. */
  function arrow(dir) {
    const ae = document.activeElement;
    const dlg = topDialog();
    if (dlg) {
      if (free(ae) || !dlg.contains(ae)) {
        const first = looseStops()[0];
        return first ? moveTo(first) : false;
      }
      if (ae.matches('input,select,textarea') && !leavesBox(ae, dir)) return false;
      if (ae.type === 'radio' && radio(ae, dir)) return true;
      return spatial(ae, dir) || wrap(ae, dir) || fixed(ae);
    }
    const tr = cursor();
    if (free(ae) || ae === $('#view')) {
      if (tr) return rowArrow(tr, dir);
      if (rows().length && (dir === 'down' || dir === 'up')) return move(dir === 'down' ? 1 : -1);
      const first = looseStops().filter(x => $('#view').contains(x))[0];
      return first ? moveTo(first) : false;
    }
    if (isRow(ae)) return rowArrow(ae, dir);
    if (ae.matches('input,select,textarea') && !leavesBox(ae, dir)) return false;
    if (ae.type === 'radio' && radio(ae, dir)) return true;
    const own = ownRow(ae);
    if (own) return inRowArrow(ae, own, dir);
    return spatial(ae, dir) || (dir === 'left' && toFoldRow(ae)) || wrap(ae, dir)
      || (dir === 'left' && go('menu')) || fixed(ae);
  }
  /** A dropdown, a date or a choice with nowhere further to go keeps the
   *  arrow all the same: left to the browser, it would change the value. */
  const fixed = x => !!(x && (x.tagName === 'SELECT' || x.type === 'date'
    || x.type === 'month' || x.type === 'radio'));
  /** A choice between a few (a firm to bill from): the arrows move along
   *  the choices and choose as they go, as they always do on one -- and
   *  past the last one, on to what is next. */
  function radio(x, dir) {
    const scope = x.form || topDialog() || $('#view');
    const group = $$('input[type=radio]', scope).filter(r => r.name === x.name && usable(r));
    const to = group[group.indexOf(x) + (dir === 'down' || dir === 'right' ? 1 : -1)];
    if (!to) return false;
    moveTo(to, x, dir);
    to.checked = true;
    to.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }

  /* ------------------------------- the bar along the foot of the window --
     **Always there, and always about where the focus is.** Since 23 Sep 2026
     it teaches one thing: the arrows move, and Enter does -- and it says
     what Enter will do where you are standing. */
  const K = (k, what) => '<span><span class="k">' + k + '</span>'
    + esc(what) + '</span>';
  const short = s => (s.length > 22 ? s.slice(0, 21) + '…' : s);
  const nameOf = x => short(x.classList && x.classList.contains('pickbtn') ? 'the list'
    : x.tagName === 'SELECT' ? 'the dropdown'
    : x.tagName === 'INPUT' || x.tagName === 'TEXTAREA' ? (x.placeholder || 'the box')
      : (label(x) || x.title || 'it'));

  function barBits() {
    const ae = document.activeElement;
    const dlg = topDialog();
    const MOVE = K('↑↓←→', 'move');
    // A date box with its calendar open has the arrows to itself.
    if (ae && ae.classList && ae.classList.contains('dtbox') && calFor(ae)) {
      return [K('←→', 'a day'), K('↑↓', 'a week'), K('PgUp PgDn', 'a month'),
              K('Enter', 'take it'), K('Esc', 'close')];
    }
    if (dlg) {
      if ($('.pick-table', dlg)) {
        return [K('type', 'to search'), K('↑↓', 'move'), K('PgUp PgDn', 'a page'),
                K('Enter', 'take it'), K('Esc', 'close')];
      }
      const out = [MOVE];
      const main = $('.ft button.primary', dlg);
      if (ae && dlg.contains(ae)) {
        if (ae.matches('input[type=checkbox]')) {
          out.push(K('Space', ae.checked ? 'untick' : 'tick'));
          if (!isFormDialog(dlg)) out.push(K('Enter', ae.checked ? 'untick' : 'tick'));
        } else if (ae.matches('input[type=radio]')) {
          if (usable(main)) out.push(K('Enter', label(main)));
        } else if (ae.tagName === 'SELECT') {
          out.push(K('Space', 'open it'));
        }
        if (ae.matches('input:not([type=checkbox]):not([type=radio]),select,textarea')
            && isFormDialog(dlg)) {
          out.push(K('Enter', 'next box'));
          if (ae.dataset.opensPicker) out.push(K('F4', 'the list'));
          if (ae.classList.contains('dtbox')) out.push(K('F4', 'calendar'));
          if (usable(main)) out.push(K('Ctrl+S', label(main)));
        } else if (ae.matches('button, a[href], summary')) {
          out.push(K('Enter', ae.tagName === 'A' ? 'open' : (label(ae) || 'press')));
        } else if (!ae.matches('input,select,textarea') && usable(main)) {
          out.push(K('Enter', label(main)));
        }
      }
      out.push(K('Esc', 'close'));
      return out;
    }
    if (ae && ae.closest && ae.closest('#nav .grp')) {
      const open = ae.closest('.navgrp').classList.contains('open');
      return [K('↑↓', 'choose'), K('Enter', open ? 'fold it away' : 'show what is in it'),
              K('Esc', open ? 'fold it away' : 'back to the screen')];
    }
    if (ae && ae.closest && ae.closest('#nav')) {
      return [K('↑↓', 'choose'),
              K('Enter', ae.classList.contains('on') ? 'back into it' : 'open it'),
              K('Esc', 'up to ' + ae.closest('.navgrp').dataset.grp)];
    }
    if (ae && ae.id === 'co-switch') {
      return [K('Enter', 'choose a firm'), K('↓', 'into the menu'),
              K('Esc', 'back to the screen')];
    }
    if (ae && ae.id === 'lock-now') {
      return [K('Enter', 'lock the books'), K('↑', 'into the menu'),
              K('Esc', 'back to the screen')];
    }
    let out = [MOVE];
    // Said after the rest, and the first to give way on a narrow window:
    // hints that do not fit drop out whole, never cut off mid-word (the
    // bar's CSS, `.kb-l`). At 1366, on a line of Challans, "type to search"
    // and Ctrl+P used to run off the end of the window.
    const later = [];
    const tr = cursor();
    const onRow = tr && (ae === tr || free(ae));
    const own = !onRow && ae && ownRow(ae);
    if (onRow) {
      out = [K('↑↓', 'line')];
      const box = rowBox(tr);
      const forDelete = box && box.classList.contains('sel');
      if (!box || forDelete) {
        const go = rowEnter(tr);
        if (go) out.push(K('Enter', go.label));
      } else out.push(K('Enter', box.checked ? 'untick' : 'tick'));
      const f = folds(tr);
      const inside = rowStops(tr);
      // What → reaches along the line, each named short: a customer's name
      // in full ran the rest of the bar off the window.
      const along = inside.map(x => { const n = nameOf(x); return n.length > 15 ? n.slice(0, 14) + '…' : n; });
      if (f && !f.open) out.push(K('→', f.noun || 'open'));
      else if (inside.length) out.push(K('→', along.slice(0, 3).join(' · ') + (along.length > 3 ? ' …' : '')));
      else if (f) out.push(K('←', f.noun ? 'hide ' + f.noun : 'close'));
      else if (canStep()) out.push(K('←→', stepper ? stepper.noun : 'month'));
      if (box) out.push(K('Space', box.checked ? 'untick' : 'tick'));
      if (searchBox()) out.push(K('type', 'to search'));
      if (forDelete) {
        later.push(K('Del', 'delete' + ($$('#view tbody .sel:checked').length ? ' ticked' : '')));
        later.push(K('Ctrl+A', 'tick all'));
      } else if (box && headBox()) {
        later.push(K('Ctrl+A', headBox().checked ? 'untick all' : 'tick all'));
      }
      if (rows().length > 20) later.push(K('Ctrl+↑↓', 'first / last'));
    } else if (own) {
      out = [K('←→', 'along the line'), K('↑↓', 'the line above / below')];
      if (ae.matches('button, a[href]')) out.push(K('Enter', ae.tagName === 'A' ? 'open' : nameOf(ae)));
      else if (ae.tagName === 'SELECT') out.push(K('Space', 'open it'));
    } else if (typing() && $('#view').contains(ae)) {
      if (inWalk(ae)) {
        out.push(K('Enter', ae.dataset.opensPicker && !ae.value.trim() ? 'the list' : 'next box'));
        if (ae.tagName === 'SELECT') out.push(K('Space', 'open it'));
      } else {
        const apply = applyFor(ae);
        if (ae.tagName === 'SELECT') out.push(K('Enter', 'open it'));
        else if (apply) out.push(K('Enter', label(apply)));
        else if (rows().length && !formCard(ae)) out.push(K('Enter', 'to the list'));
        else out.push(K('Enter', 'next box'));
        if (rows().length > 20 && !formCard(ae)) later.push(K('Ctrl+↑↓', 'first / last line'));
      }
      // Typing in one of these opens its whole list, searching (28 Sep 2026).
      if (ae.dataset.opensPicker) out.push(K('type', 'to search the list'));
      if (ae.dataset.opensPicker && ae.value.trim()) out.push(K('F4', 'the list'));
      if (ae.classList.contains('dtbox')) out.push(K('F4', 'calendar'));
    } else if (ae && ae.matches && ae.matches('input[type=checkbox], input[type=radio]')) {
      out.push(K('Space', ae.type === 'radio' ? 'choose' : ae.checked ? 'untick' : 'tick'));
      if (!inWalk(ae)) out.push(K('Enter', ae.checked ? 'untick' : 'tick'));
    } else if (ae && ae.matches && ae.matches('summary')) {
      out.push(K('Enter', ae.parentElement && ae.parentElement.open ? 'close' : 'open'));
    } else if (ae && ae.matches && ae.matches('button, a[href]') && !free(ae)) {
      out.push(K('Enter', ae.tagName === 'A' ? 'open' : (nameOf(ae) || 'press')));
    }
    if (saveButton()) out.push(K('Ctrl+S', 'save'));
    if (printButton() || (onRow && $$('button', tr).some(b => /print/i.test(label(b))))) {
      out.push(K('Ctrl+P', 'print'));
    }
    if (challanPrint()) later.push(K('Alt+P', 'challan'));
    return out.concat(later);
  }

  let barWas = '';
  /** One thing looks chosen at a time. The line of a list keeps its mark
   *  while the keyboard is on it (or nowhere), and lets it go while the
   *  keyboard is on a box, a button, the menu or a dialog: a new user saw
   *  "CASH IN HAND" lit and the Bank button ringed at once, and could not
   *  tell which Enter would press. The line is still where the keyboard
   *  comes back to. */
  function markAway() {
    const tr = cursor();
    const ae = document.activeElement;
    document.body.classList.toggle('kb-away',
      !!tr && !free(ae) && ae !== tr && !tr.contains(ae));
  }
  function paintBar() {
    markAway();
    const b = $('#kbbar');
    if (!b) return;
    // The passcode screen has one box and one button; nothing to say.
    if (lockShown) { b.className = 'noprint'; barWas = ''; return; }
    let seen = false;
    try { seen = localStorage.getItem('kb.help.seen') === '1'; } catch (e) { /* none */ }
    // In the menu its own keys are on the left; the way into it and the way
    // back are not repeated here.
    const ae = document.activeElement;
    const side = !!(ae && ae.closest && ae.closest('#sidebar'));
    const right = '<span class="sp">'
      + (seen ? '' : '<span class="new"><span class="k">F1</span>New here? Every key</span>')
      + (seen ? K('F1', 'all keys') : '')
      + (!dialogOpen() && !side ? K('Esc', escOwn() || (navDepth >= 1 && !navRoot) ? 'back' : 'menu') : '')
      + '</span>';
    const html = '<div class="kb-l">' + barBits().join('') + '</div>' + right;
    if (html === barWas) return;
    barWas = html;
    b.innerHTML = html;
    b.className = 'noprint on';
  }
  let barTimer = 0;
  function schedule() {
    clearTimeout(barTimer);
    barTimer = setTimeout(paintBar, 0);
  }
  // The foot of the page gives the bar room, and what sits along the bottom
  // -- the notes, the "new version" bar -- stands on top of it, not under.
  function watchBar() {
    const b = $('#kbbar');
    if (!b) return;
    let off = false;
    try { off = localStorage.getItem('kb.bar') === 'off'; } catch (e) { /* none */ }
    document.body.classList.toggle('kbbar-off', off);
    if (window.ResizeObserver) {
      new ResizeObserver(() => document.documentElement.style.setProperty(
        '--kbbar-h', (off || !b.offsetHeight ? 0 : b.offsetHeight) + 'px')).observe(b);
    }
    ['focusin', 'focusout', 'keyup'].forEach(ev =>
      document.addEventListener(ev, schedule, true));

    new MutationObserver(schedule).observe($('#modal-root'), { childList: true });
    new MutationObserver(schedule).observe(document.body, { childList: true });
  }
  function showBar(on) {
    try { localStorage.setItem('kb.bar', on ? 'on' : 'off'); } catch (e) { /* none */ }
    document.body.classList.toggle('kbbar-off', !on);
    const b = $('#kbbar');
    document.documentElement.style.setProperty('--kbbar-h',
      (on && b ? b.offsetHeight : 0) + 'px');
  }
  function helpSeen() {
    try { localStorage.setItem('kb.help.seen', '1'); } catch (e) { /* none */ }
    barWas = '';
    schedule();
  }

  /* ------------------------------------------ into the menu and back out --
     **← takes the keyboard to the menu, and Enter takes it back in** (22
     Sep 2026: "when I click escape it should take me to the menu and when I
     click enter it should take me inside the current selected menu
     option" -- Esc went there until 24 Sep 2026, when it became the way
     back a screen, and ← off the left edge is the way in). Going to the menu moves nothing but the keyboard: the screen,
     its filters, its cursor line and anything half typed on it stay just as
     they were. Enter (or →) on the entry of that same screen -- or Esc
     again -- puts the keyboard back where it was. Enter on any other entry
     opens it. The screen before this one is Esc, Alt + ← or the arrow at
     the top left. */
  let returnTo = null;
  function noteReturn(el) {
    const side = $('#sidebar');
    if (el && el !== document.body && el.isConnected && !(side && side.contains(el))
        && !$('#modal-root').contains(el)) {
      returnTo = el;
    } else if (cursor()) {
      returnTo = cursor();
    }
  }
  function backIn() {
    const t = returnTo;
    returnTo = null;
    drawer(false);
    if (t && t.isConnected && shown(t)) {
      if (isRow(t)) setCursor(t);
      else { t.focus({ preventScroll: true }); }
      if (document.activeElement === t) return;
    }
    const tr = cursor() || (lastRow >= 0 ? rows()[lastRow] : null);
    if (tr) { setCursor(tr); return; }
    const box = entryTarget();
    if (box) { box.focus({ preventScroll: true }); return; }
    if (go('list') || go('boxes')) return;
    const b = $('#page-actions button');
    if (b) b.focus(); else if (document.activeElement) document.activeElement.blur();
  }
  // However the keyboard came into the menu -- Esc, ←, Shift+Tab, a click --
  // it remembers where it came from.
  document.addEventListener('focusin', e => {
    const side = $('#sidebar');
    if (side && side.contains(e.target) && e.relatedTarget
        && !side.contains(e.relatedTarget)) noteReturn(e.relatedTarget);
  });

  /** The places a screen is entered at. */
  function regions() {
    return [
      ['menu', navEntry],
      ['boxes', () => $$('#view input,#view select,#view textarea')
        .filter(x => shown(x) && !x.disabled && !x.readOnly)[0]],
      ['list', () => cursor() || firstRow()],
      ['actions', () => $('#page-actions button')]
    ];
  }
  /** On a narrow window the menu is a drawer parked off the left of the
   *  screen, and the ☰ button is the only thing that brings it in. Focusing
   *  it without opening it put the focus on links nobody could see, so the
   *  drawer follows the focus. */
  function drawer(on) {
    const s = $('#sidebar');
    if (s && window.innerWidth <= 900) s.classList.toggle('open', !!on);
  }

  function go(name) {
    const side = name === 'menu';
    if (side) noteReturn(document.activeElement);
    const r = regions().find(x => x[0] === name);
    const target = r && r[1]();
    drawer(side && !!target);
    if (!target) {
      // A list is still on its way back from the server when a screen has
      // only just opened. Remember that it was asked for and put the cursor
      // on it the moment the rows arrive, rather than doing nothing.
      if (name === 'list') wanted = true;
      return false;
    }
    wanted = false;
    if (isRow(target)) setCursor(target);
    else { target.focus(); if (target.select) target.select(); }
    return true;
  }
  /* --------------------------------------- where a screen starts -------
     **Opening a screen should leave the keyboard somewhere useful.** In this
     order:

       1. anything the screen marked `autofocus`
       2. its search box -- on a list, that is what you came to do
       3. on an entry screen (a bill, an order, Settings), its first box
       4. anywhere else, the list itself, cursor on the first row -- or on
          the first row to arrive, if it is still being fetched
       5. failing that, the first filter box, then the first button at the top

     A report used to open on its Year box, and on Windows the arrow key
     that was meant to go down the list changed the year instead, and read
     the whole report again. A tick box is never where a screen starts.

     Two things it steps over. A box that opens a picker merely by being
     focused, or every list screen would open a dialog over itself on
     arrival. And nothing is taken from the user: if the focus has already
     gone somewhere by the time the screen settles -- they typed, or
     clicked, or a route put it there itself -- it is left alone. */
  // A customer box already filled in opens nothing on being reached, and is
  // where a saved challan or bill is opened at -- "To:", its first box. It
  // was stepped over, and a challan opened on its Sales A/c dropdown.
  const boxOk = x => shown(x) && !x.disabled && !x.readOnly && x.type !== 'hidden'
    && (!x.dataset.opensPicker || (x.dataset.opensPicker === '1' && !!x.value.trim()))
    && x.type !== 'checkbox' && x.type !== 'radio';
  function entryTarget() {
    const marked = $('#view [autofocus]');
    if (marked && shown(marked)) return marked;
    const search = [$('#view #q'), $('#view #search')].filter(shown)[0];
    if (search) return search;
    if (entryScreen()) {
      return $$('#view input,#view select,#view textarea').filter(boxOk)[0] || null;
    }
    return null;
  }

  let entryTries = 0;
  /* ------------------------------------------ where a screen was left ---
     Written against the history entry as it is left (see `markPlace`), and
     read back when Back, Esc, Alt + ← or a reload comes to it again. */

  /** A line, told apart from the others: where it leads, and what it says. */
  function rowKey(tr) {
    return { href: tr.getAttribute('data-href') || '',
             text: (tr.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160) };
  }
  /** Where the screen stands now: the line the keyboard (or the click) is
   *  on, the rows folded open, the boxes opened or shut. (Not `place`: that
   *  is where a thing sits on the page, and the arrows are built on it --
   *  named the same, this one hid it and every sideways move broke.) */
  function leftAt(target) {
    const view = $('#view');
    const out = {};
    let tr = target && target.closest && view.contains(target) ? ownRow(target) : null;
    if (!tr) tr = cursor();
    if (tr && view.contains(tr)) {
      out.row = rowKey(tr);
      out.idx = tr === cursor() && lastRow >= 0 ? lastRow : rows().indexOf(tr);
    } else {
      const ae = document.activeElement;
      if (ae && ae.id && view.contains(ae)) out.box = ae.id;
    }
    const open = $$('#view tr.det').filter(d => d.style.display !== 'none');
    if (open.length) {
      out.folds = open.map(d => {
        const up = d.previousElementSibling;
        return { of: d.dataset.for || '', href: up ? up.getAttribute('data-href') || '' : '' };
      });
    }
    const boxes = $$('#view details');
    if (boxes.length) out.details = boxes.map(d => (d.open ? 1 : 0));
    return out;
  }
  /** The line a remembered key stands for: by where it leads, then by what
   *  it says, the nearer to where it was when there are two. */
  function findRow(key, idx) {
    const rs = rows();
    const near = list => list.sort((a, b) =>
      Math.abs(rs.indexOf(a) - idx) - Math.abs(rs.indexOf(b) - idx))[0];
    let hit = key.href ? rs.filter(r => r.getAttribute('data-href') === key.href) : [];
    if (hit.length > 1) {
      const same = hit.filter(r => rowKey(r).text === key.text);
      if (same.length) hit = same;
    }
    if (!hit.length) hit = rs.filter(r => rowKey(r).text === key.text);
    return hit.length ? near(hit) : null;
  }
  /** Open again the rows that were folded open. */
  function openFolds(list) {
    (list || []).forEach(f => {
      let det = null;
      if (f.href) {
        const up = rows().find(r => r.getAttribute('data-href') === f.href);
        const next = up && up.nextElementSibling;
        if (next && next.matches('tr.det')) det = next;
      }
      if (!det && f.of) det = $('#view tr.det[data-for="' + CSS.escape(f.of) + '"]');
      if (!det || det.style.display !== 'none') return;
      const up = det.previousElementSibling;
      const fold = up && folds(up);
      if (fold && !fold.open) fold.toggle();
    });
  }
  /** Stand on the remembered line again, once the list it is in has come
   *  back from the server, and put the page where it was. Gives up on the
   *  first key or click -- whoever takes over is right -- and after five
   *  seconds lands the way a screen always lands. */
  function restorePlace(p, y, seq) {
    let live = true, tries = 0, seenRows = 0, opened = false;
    const EVENTS = ['wheel', 'touchstart', 'keydown', 'mousedown'];
    const stop = () => { live = false; off(); };
    const off = () => EVENTS.forEach(e => removeEventListener(e, stop, true));
    EVENTS.forEach(e => addEventListener(e, stop, { capture: true, passive: true }));
    const room = () => Math.max(document.body.scrollHeight,
                                document.documentElement.scrollHeight) - window.innerHeight;
    const settle = tr => {
      off();
      if (tr) {
        setCursor(tr);
      } else if (p.box && $('#' + CSS.escape(p.box))) {
        const box = $('#' + CSS.escape(p.box));
        box.focus({ preventScroll: true });
      } else {
        focusEntry();
      }
      const r = room();
      if (r > 0) window.scrollTo(window.scrollX, Math.min(y, r));
      else window.scrollTo(window.scrollX, 0);
      // Never left standing on a line out of sight, or half under a bar --
      // the page is moved only as far as that takes.
      const at = tr || document.activeElement;
      if (at && !free(at)) reveal(at);
      paintBar();
    };
    const tick = () => {
      if (!live || seq !== renderSeq) { off(); return; }
      if (dialogOpen()) { setTimeout(tick, 120); return; }
      const rs = rows();
      if (rs.length && !opened) {
        opened = true;
        openFolds(p.folds);
        if (p.details) {
          const boxes = $$('#view details');
          if (boxes.length === p.details.length) {
            boxes.forEach((d, i) => { d.open = !!p.details[i]; });
          }
        }
      }
      if (rs.length) seenRows += 1;
      const tr = p.row ? findRow(p.row, num(p.idx)) : null;
      const tall = room() >= y - 2;
      if ((tr || !p.row) && (tall || tries > 20)) { settle(tr); return; }
      // The rows are here and the line is not among them -- deleted, or the
      // list is shorter now: the line that stood in its place.
      if (p.row && seenRows > 12) {
        settle(rs.length ? rs[Math.min(Math.max(0, num(p.idx)), rs.length - 1)] : null);
        return;
      }
      if (++tries > 85) { settle(null); return; }
      setTimeout(tick, 60);
    };
    tick();
  }

  function focusEntry(again) {
    if (!again) entryTries = 0;
    if (dialogOpen()) return;
    const at = document.activeElement;
    if (at && at !== document.body && at !== document.documentElement) return;
    const box = entryTarget();
    if (box) {
      box.focus({ preventScroll: true });
      if (box.select && box.tagName === 'INPUT' && box.type === 'text') box.select();
      return;
    }
    if (go('list')) return;
    // The list has not come back yet: `go` has asked for it, and the cursor
    // lands on it when it does. Meanwhile the keyboard waits in the first
    // filter, and is only moved on from there if it is still there.
    const filter = $$('#view input,#view select,#view textarea').filter(boxOk)[0];
    if (filter) {
      filter.focus({ preventScroll: true });
      parked = filter;
      return;
    }
    const btn = $('#page-actions button');
    if (btn && !wanted) { btn.focus(); return; }
    // Some screens fetch before they draw anything at all -- Settings has no
    // box on it until its firm comes back. Look again while the screen is
    // still settling, then leave it alone.
    if (entryTries++ < 6) setTimeout(() => focusEntry(true), 200);
    else if (btn) btn.focus();
  }

  /* ------------------------------------------ leaving a half-typed screen
     A screen holding a Save button is holding work that is not in the books
     yet. `touched` says whether anything has actually been typed on it --
     set by the first input, cleared when the screen changes -- so leaving an
     untouched form is silent and leaving a half-written one asks.

     A filter or a search box is not work: those screens have no Save, so
     they never ask. */
  let touched = false;
  function watchTyping() {
    const host = $('#view');
    if (!host) return;
    ['input', 'change'].forEach(ev =>
      host.addEventListener(ev, () => { touched = true; }, true));
    window.addEventListener('hashchange', () => { touched = false; });
  }

  /* A screen of several cards each with its own Save (Settings) cannot go
     by `touched`: changing the numbering year or ticking an old year is a
     touch, and nothing was waiting to be saved (8 Oct 2026, "why is this
     shown?"). Each such card carries `_unsaved()`, which compares its boxes
     with what was drawn, and the screen asks only when one says so. */
  const unsavedWork = () => {
    const own = $$('#view .own-unsaved').filter(n => n._unsaved);
    if (own.length) return own.some(n => n._unsaved());
    return touched && !!($('#view #save') || $('#view #j-save')
      || $('#view #save-co') || $('#view').onkeydown);
  };

  async function leaveScreen() {
    if (navDepth < 1) return;
    if (unsavedWork()) {
      await leaveAsking('Leave it and go back?', 'Save and go back',
                        () => { touched = false; goBack(); });
      return;
    }
    touched = false;
    goBack();
  }

  /* ----------------------------------------------- save, then go on -----
     "Save and go back" presses the screen's own Save -- every check it
     makes still made, every question it asks still asked -- and goes on
     only once the saved entry has been drawn again in its place, which is
     how every save here ends (`showSaved`). A save refused stays where it
     is with its reason; a key pressed or a click on the screen meanwhile
     means someone has taken over, and nothing moves. */
  let afterSave = null;                 // { go, at }
  const canSaveEntry = () => usable($('#view #save')) || usable($('#view #j-save'));
  function saveThen(go) {
    const b = usable($('#view #save')) ? $('#view #save') : $('#view #j-save');
    if (!usable(b)) { toast('Nothing on this screen to save.'); return false; }
    afterSave = { go, at: Date.now() };
    b.click();
    return true;
  }
  ['keydown', 'mousedown'].forEach(ev => addEventListener(ev, e => {
    if (afterSave && !(e.target && e.target.closest && e.target.closest('#modal-root'))) {
      afterSave = null;
    }
  }, true));
  /** Called by `render` once a screen is up. */
  function drawn(moved) {
    const a = afterSave;
    afterSave = null;
    if (!a || moved || Date.now() - a.at > 120000) return;
    setTimeout(a.go, 0);
  }

  /* ------------------------------------ Enter in a box over a list ------
     On a list or a report the boxes at the top are filters. Enter in one
     means "done with this box":

       - a box whose card has an Apply button presses it, and the cursor
         goes down into the list when the answer comes back;
       - a box in a form with its own Save (Settings, the SHREE update's
         timings) goes on to the next box of that form, and past the last
         one stands on its Save;
       - a search box, or a filter that reloads by itself, goes straight
         down into the list -- type a name, Enter, and Enter again opens it;
       - a box inside a list row (the salary sheet) goes on to the next box.

     An entry screen -- a bill, an order, a journal -- has its own Enter walk
     and is left to it. */
  function applyFor(box) {
    const card = box.closest('.card');
    const apply = card && $('#go', card);
    return usable(apply) ? apply : null;
  }
  function formCard(box) {
    const card = box.closest('.card');
    if (!card || applyFor(box)) return null;
    const save = $$('button.primary', card).filter(usable)[0];
    return save ? { card, save } : null;
  }
  function boxDone(box) {
    if ($('#view').onkeydown || box.tagName === 'TEXTAREA') return false;
    if (box.closest('tbody tr')) return moveOn(box, $('#view'));
    const apply = applyFor(box);
    if (apply) {
      wanted = true;
      parked = box;
      door = box;
      apply.click();
      return true;
    }
    const form = formCard(box);
    if (form) {
      if (moveOn(box, form.card)) return true;
      form.save.focus();
      return true;
    }
    door = box;                 // ↑ off the list's first line comes back here
    if (rows().length) {
      setCursor(cursor() || firstRow());
      return true;
    }
    // The list is still coming back: land on it when it does.
    wanted = true;
    parked = box;
    return true;
  }

  /* ----------------------------------------- the keys of this screen ----
     Worked out from the screen as it stands, for the top of F1: it can
     never go out of date the way a written list does. */
  function screenKeys() {
    const out = [];
    const top = $$('#page-actions button').filter(usable).map(label).filter(Boolean);
    if (top.length) out.push(['↑ then ← →', 'The buttons at the top: ' + top.join(' · ')]);
    const sb = saveButton(), pb = printButton(), fb = searchBox();
    if (sb) out.push(['Ctrl + S', 'Save — ' + label(sb)]);
    if (pb) out.push(['Ctrl + P', label(pb)]);
    if (challanPrint()) out.push(['Alt + P', 'Print the challan']);
    if (fb) out.push(['Ctrl + F', 'The search box']);
    if ($('#view').onkeydown) {
      out.push(['Enter', 'The next box; past the last line, another line']);
      if ($('#view [data-opens-picker]')) {
        out.push(['F4', 'The whole list, on a party, product or ledger box']);
      }
    }
    if ($('#view [data-opens-picker]')) {
      out.push(['Type', 'In a customer, product or account box: the whole list opens, searching what you type']);
    }
    if ($('#view .dtbox')) {
      out.push(['F4', 'On a date: the calendar. Or type it day first — 2, 2-10 or 02-10-2026']);
    }
    const rs = rows();
    if (rs.length) {
      out.push(['↑ ↓', 'The lines of the list']);
      const tr = cursor() || firstRow();
      const go = tr && rowEnter(tr);
      if (go) out.push(['Enter', go.label.charAt(0).toUpperCase() + go.label.slice(1) + ' the line']);
      const inside = tr ? rowStops(tr).map(nameOf) : [];
      if (inside.length) out.push(['→', 'Along the line: ' + inside.join(' · ')]);
      else if (tr && !folds(tr) && !canStep() && go
               && tr.matches('.clickable,[data-href],[data-open-invoice],[data-bank]')) {
        out.push(['→', 'Open the line, as Enter does']);
      }
      if (fb) out.push(['(typing)', 'Searches the list']);
    }
    if ($('#view .caret')) out.push(['→ ←', 'Open / close the line the cursor is on']);
    if (canStep()) out.push(['← →', 'The ' + (stepper ? stepper.noun : 'month') + ' before / after']);
    if ($('#view [data-bulk]')) {
      out.push(['Space', 'Tick the line']);
      out.push(['Ctrl + A', 'Tick every line']);
      out.push(['Delete', 'Delete the ticked lines — or, with none ticked, the line the cursor is on']);
    } else if (headBox()) {
      out.push(['Space', 'Tick the line']);
      out.push(['Ctrl + A', 'Tick every line — again to untick them']);
    }
    return out;
  }

  /* ------------------------------------------------------ the dispatcher */

  const ARROWS = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right' };

  function onKey(e) {
    if (e.defaultPrevented) return;
    // The passcode screen has replaced the app: its form is all there is.
    if (lockShown) return;
    const k = e.key || '';          // dead keys and IME send nothing at all
    if (!k) return;
    const plain = !e.altKey && !e.ctrlKey && !e.metaKey;
    const ctrl = (e.ctrlKey || e.metaKey) && !e.altKey;
    const struck = keyStruck(e);
    const ae = document.activeElement;

    // Every key there is, and this screen's first. F1 is help in every program.
    if (k === 'F1' && !e.altKey && !e.ctrlKey) {
      e.preventDefault();
      shortcutHelp();
      return;
    }
    // Ctrl+F goes to the search box -- the screen's, or the dialog's. With
    // none, the browser's own Find is left to do it.
    if (ctrl && struck === 'f' && !e.shiftKey) {
      const box = searchBox();
      if (box) {
        e.preventDefault();
        box.focus();
        if (box.select) box.select();
      }
      return;
    }
    // Ctrl+S saves the screen. A dialog answers it itself; the browser's own
    // "Save page as" is never what was meant.
    if (ctrl && struck === 's') {
      if (dialogOpen()) return;
      e.preventDefault();
      const b = saveButton();
      if (b) b.click(); else toast('Nothing on this screen to save.');
      return;
    }

    // The arrows, everywhere but the menu (which has its own, below) -- see
    // `arrow`. With Shift they select text, with Ctrl they jump a word, with
    // Alt ← is Back: all the browser's.
    if (plain && !e.shiftKey && ARROWS[k] && !(ae && ae.closest && ae.closest('#sidebar'))) {
      if (arrow(ARROWS[k])) e.preventDefault();
      return;
    }
    // A dropdown opens on Space (and F4), and on Enter where Enter is not
    // walking a bill or a form on to the next box. An arrow never changes
    // one: on Windows it used to switch firms, change a setting or leave a
    // half-typed bill, one key at a time.
    if (ae && ae.tagName === 'SELECT' && !e.altKey && !e.ctrlKey && !e.metaKey
        && (k === ' ' || k === 'F4' || (k === 'Enter' && !inWalk(ae)))) {
      if (openList(ae)) e.preventDefault();
      return;
    }
    // Enter on a tick box ticks it -- except where Enter is walking a form,
    // which walks on past it (Space ticks there).
    if (plain && k === 'Enter' && ae && ae.matches && ae.matches('input[type=checkbox]')
        && !inWalk(ae)) {
      e.preventDefault();
      ae.click();
      paintBar();
      return;
    }
    if (dialogOpen()) {
      // With a dialog up, Alt + ← was the browser's Back behind it -- past
      // the "not saved yet" question. Esc is how out.
      if (e.altKey && !e.ctrlKey && !e.metaKey
          && (k === 'ArrowLeft' || k === 'ArrowRight' || k === 'Home')) e.preventDefault();
      return;
    }

    // Ctrl+A ticks every row of a list that is ticked row by row -- the
    // bills on the e-way and e-invoice screens, the lines of the salary
    // sheet -- by its tick box at the head of the list. (A list that deletes
    // in bulk answers it itself.)
    if (ctrl && struck === 'a' && !e.shiftKey && !typing()) {
      const all = headBox();
      if (all) { e.preventDefault(); all.click(); paintBar(); }
      return;
    }
    // Ctrl + ↑ and Ctrl + ↓: the first line of the list and the last, from
    // wherever the keyboard is on the screen -- the search box a list opens
    // on, a filter, a button above it (3 Oct 2026: "keep the short key
    // (control + up/down)"). Ctrl+Home and Ctrl+End do the same, as in every
    // Windows list; Home and End alone, on the list itself. A note or a bill
    // being walked keeps them for its own box. On a Mac, Ctrl + ↑ ↓ belong
    // to the system; Cmd + ↑ ↓ are its own way to the top and the bottom.
    const END_KEYS = { ArrowUp: false, Home: false, ArrowDown: true, End: true };
    if (ctrl && !e.shiftKey && k in END_KEYS
        && !(ae && ae.closest && ae.closest('#sidebar'))
        && !(ae && ae.tagName === 'TEXTAREA') && !(typing() && inWalk(ae))) {
      if (toEnd(END_KEYS[k])) e.preventDefault();
      return;
    }
    // Alt + ← is Back in every browser, and here it asks first when an entry
    // is half typed. The arrow at the top left does the same.
    if (e.altKey && !e.ctrlKey && !e.metaKey && k === 'ArrowLeft') {
      e.preventDefault();
      leaveScreen();
      return;
    }
    // Ctrl+P prints the document on screen -- the bill or challan, or the
    // one under the cursor on a list -- rather than a picture of the screen.
    // A report with no print of its own is printed by the browser, as before.
    if (ctrl && struck === 'p' && !e.shiftKey) {
      const tr = cursor();
      const own = tr && $$('button', tr).filter(usable)
        .find(b => /\bprint\b/i.test(label(b)));
      const b = own || printButton();
      if (b) { e.preventDefault(); b.click(); }
      return;
    }
    // Alt+P prints the challan: a bill's, a challan's, or the highlighted
    // line's -- see `challanPrint`. Not Ctrl+Shift+P: Edge, the browser
    // Windows comes with, keeps that for a private window and never passes
    // it on. No program on Windows uses Alt+P.
    if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey && struck === 'p') {
      e.preventDefault();
      if (dialogOpen()) return;
      const run = challanPrint();
      if (run) run(); else toast('No challan to print on this screen.');
      return;
    }

    /* The menu -- the whole sidebar: the firm box at the top, the entries,
       and Lock at the foot when the passcode is on. ↑ ↓ step, Home and End
       go to the ends, PgUp PgDn jump a group, ↑ off the first entry reaches
       the firm box and ↓ off the last one Lock. Enter or → opens the entry
       the arrows stand on. Esc in an open group -- on an entry or on its
       heading -- folds it and leaves the keyboard on the heading (5 Oct
       2026); on a folded heading, or anywhere else in the sidebar, it goes
       back to the screen. The menu is one Tab stop; Tab goes on to the
       screen. */
    const inSide = ae && ae.closest && ae.closest('#sidebar');
    const inNav = inSide && ae.closest('#nav');
    if (inSide && plain && k === 'Escape') {
      e.preventDefault();
      const grp = inNav && ae.closest('.navgrp');
      if (grp && grp.classList.contains('open')) {
        navShow(grp.dataset.grp, false);
        $('.grp', grp).focus();
      } else backIn();
      return;
    }
    const lock = $('#lock-now');
    if (inSide && !inNav && ae === lock && plain
        && (k === 'ArrowUp' || k === 'ArrowDown')) {
      e.preventDefault();
      if (k === 'ArrowUp') {
        const links = navStops();
        if (links.length) links[links.length - 1].focus();
      }
      return;
    }
    if (inNav && plain) {
      // The group headings and the open group's entries (navStops).
      const links = navStops();
      const at = links.indexOf(ae);
      const head = ae.classList.contains('grp');
      const box = ae.closest('.navgrp');
      const open = box.classList.contains('open');
      const land = i => {
        const t = links[Math.max(0, Math.min(links.length - 1, i))];
        if (t) { e.preventDefault(); t.focus(); }
      };
      if (k === 'ArrowDown') {
        if (at === links.length - 1 && usable(lock)) {
          e.preventDefault(); lock.focus(); return;
        }
        land(at + 1);
        return;
      }
      if (k === 'ArrowUp') {
        const sw = $('#co-switch');
        if (at <= 0 && usable(sw)) { e.preventDefault(); sw.focus(); return; }
        land(at - 1);
        return;
      }
      if (k === 'Home') { land(0); return; }
      if (k === 'End') { land(links.length - 1); return; }
      // A group at a time: the heading of the group above or below.
      if (k === 'PageDown' || k === 'PageUp') {
        const down = k === 'PageDown';
        const heads = links.filter(x => x.classList.contains('grp'));
        const here = heads.filter(f => down ? links.indexOf(f) > at
                                            : links.indexOf(f) < at);
        const target = down ? here[0] : here[here.length - 1];
        if (target) { e.preventDefault(); target.focus(); }
        return;
      }
      // On a heading: Enter shows the group or folds it away, → shows it
      // (or, shown already, goes to its first entry), ← folds it.
      if (head) {
        e.preventDefault();
        if (k === 'Enter' || k === ' ') navShow(box.dataset.grp, !open);
        else if (k === 'ArrowRight' && !open) navShow(box.dataset.grp, true);
        else if (k === 'ArrowRight') { const a = $('a', box); if (a) a.focus(); }
        else if (k === 'ArrowLeft' && open) navShow(box.dataset.grp, false);
        return;
      }
      // Enter or → goes into the entry the highlight is on. On the screen
      // that is already open that means back into it as it was left -- not
      // the same screen drawn again from the start, and not nothing at all,
      // which is what following a link to the address already open does.
      if (k === 'Enter' || k === ' ' || k === 'ArrowRight') {
        e.preventDefault();
        if (ae.classList.contains('on')) backIn(); else ae.click();
        return;
      }
      // ← from an entry goes up to its group's heading.
      if (k === 'ArrowLeft') { e.preventDefault(); $('.grp', box).focus(); return; }
      return;
    }

    /* Escape, in the order it is asked. A dialog, a lookup list under a box:
       each answers it first with a handler of its own, and this one has
       stood down for them. **With none of those open, Esc goes back to the
       screen before this one** (24 Sep 2026: "after doing my work when I
       click on escape then it should take me back to the page I was before
       ... this happens everywhere in the software") -- exactly as the arrow
       at the top left and Alt + ← do, asking first when an entry is half
       typed. From 22 Sep it went to the menu instead. **On a menu screen
       it goes to the menu again** (26 Sep 2026): on Challans, Esc puts the
       keyboard in the menu, the screen left as it is behind it, rather than
       going back to the Dashboard (`navRoot`, `trackDepth`) -- on its own
       entry, Bills, its group open (`navEntry`; 6 Oct 2026). Esc again goes
       up to the group's heading, Sales, the group folded; Esc once more
       comes back in at the same line. On the screen the app was opened on,
       with nothing before it, Esc goes to the menu too; so does ← from the
       left edge of a screen. */
    if (k === 'Escape' && plain && !$('.pick-list')) {
      e.preventDefault();
      if (escOwn()) { escHook.run(); return; }
      if (navRoot || navDepth < 1) go('menu');
      else leaveScreen();
      return;
    }

    // Enter in a filter or search box: see `boxDone`.
    if (plain && k === 'Enter' && typing() && $('#view').contains(ae) && !$('.pick-list')) {
      if (boxDone(ae)) e.preventDefault();
      return;
    }

    if (typing()) return;
    if (ae && ae.matches && ae.matches('button, a[href], summary')) return;
    const tr = cursor();
    const onList = free(ae) || (tr && (ae === tr || tr.contains(ae)));

    // Typing on a list searches it: the letter goes into the search box and
    // the list narrows as the name is typed, as any list of names answers.
    if (plain && onList && /^[a-z0-9]$/i.test(k)
        && !(ae && ae.closest && ae.closest('#sidebar'))) {
      const box = searchBox();
      if (box) { e.preventDefault(); typeInto(box, k); }
      return;
    }
    if (!tr) return;
    // A tick box, a fold-out -- anything else the keyboard is standing on --
    // answers for itself. The cursor line only answers while it has the
    // keyboard.
    if (!onList) return;

    if (plain && (k === 'Home' || k === 'End')) {
      e.preventDefault();
      toEnd(k === 'End');
      return;
    }
    if (k === 'PageDown') { e.preventDefault(); move(10); return; }
    if (k === 'PageUp') { e.preventDefault(); move(-10); return; }

    // Space ticks the row; so does Enter, on the lists that are ticked
    // rather than opened. Everywhere else Enter opens what the cursor is on.
    const box = rowBox(tr);
    // A deletion tick box (.sel) takes Space only; Enter still opens the row.
    if (box && (k === ' ' || (k === 'Enter' && !box.classList.contains('sel')))) {
      e.preventDefault();
      box.checked = !box.checked;
      box.dispatchEvent(new Event('change', { bubbles: true }));
      paintBar();
      return;
    }
    if (k === 'Enter') {
      const run = rowEnter(tr);
      const f = folds(tr);
      if (run) {
        e.preventDefault();
        run.run();
        paintBar();
        if (f && !f.open && tr.isConnected && (folds(tr) || {}).open) showFold(tr);
      }
    }
  }

  /* ------------------------------------------------------------ repaints */

  // A list redraws itself whenever a filter moves, and the cursor has to
  // survive that -- otherwise every change of date throws away the place.
  // A list asked for before it arrived is stood on when it does -- unless
  // the keyboard has been taken somewhere else in the meantime.
  let pending = null;
  let barLater = null;
  function watch() {
    new MutationObserver(() => {
      if (lastRow < 0 && !wanted) {
        // The keys along the foot were said before the list arrived -- "next
        // box" in a search box that now has a list under it to go to.
        clearTimeout(barLater);
        barLater = setTimeout(paintBar, 120);
        return;
      }
      clearTimeout(pending);
      pending = setTimeout(() => {
        if (cursor()) return;
        const rs = rows();
        if (!rs.length) { if (lastRow >= 0) setCursor(null); return; }
        if (wanted) {
          wanted = false;
          const ae = document.activeElement;
          const free = !ae || ae === document.body || ae === parked;
          parked = null;
          if (free && !dialogOpen()) setCursor(firstRow());
          return;
        }
        if (lastRow < 0) return;
        setCursor(rs[Math.min(lastRow, rs.length - 1)], true);
      }, 60);
    }).observe($('#view'), { childList: true, subtree: true });
  }

  /* A screen is about to be drawn. Called by `render` before anything else
     -- not on hashchange, which is heard only after `render` has already
     drawn a screen that fetches nothing first, and would wipe the cursor,
     the list it asked for and the month stepper it set up (22 Sep 2026).
     A screen drawn again in place keeps its cursor line; another one starts
     without. */
  /* What has the keyboard is always in sight (26 Sep 2026: "the currently
     selected item on screen should always be properly visible"). The
     arrows always brought it into view; Tab, the Enter walk down a bill, a
     dialog opening on its first box, a box handed back when a dialog closes
     did not -- the browser only scrolls a box into the window, and the bar
     at the top and the keys along the foot stand over the window's edges.
     Not after a click: what was clicked is under the mouse already, and
     moving the page between press and release lands the click on something
     else. Not when the window itself comes back to the front with the same
     box in it: the page stays where it was left. */
  function watchFocus() {
    let pointer = 0, had = null;
    addEventListener('pointerdown', () => { pointer = Date.now(); }, true);
    document.addEventListener('focusin', e => {
      const t = e.target;
      const same = t === had;
      had = t;
      if (same || Date.now() - pointer < 500) return;
      if (!t || free(t) || t === window) return;
      // The Enter walk and Tab go along the form (`follow`); a box a closing
      // dialog hands back is only brought into view -- the page stays put.
      if (returning) reveal(t);
      else follow(t);
    }, true);
    // Maximised, restored, the window made narrower: the line or box being
    // worked on stays where it can be read.
    let resized = null;
    addEventListener('resize', () => {
      clearTimeout(resized);
      resized = setTimeout(() => {
        const ae = document.activeElement;
        const at = !free(ae) ? ae : cursor();
        if (at) reveal(at);
      }, 150);
    });
  }

  let screenWas = null;
  function beginScreen(hash, moved) {
    wanted = false;
    parked = null;
    door = null;
    lastMove = null;
    // A screen drawn afresh holds nothing typed yet -- and this is how a
    // saved challan, bill, order or voucher comes back: drawn again at the
    // same address, no hashchange to clear `touched`. Every Esc after a save
    // asked "Leave without saving" (5 Oct 2026). The box typed in last is let
    // go first: taken off the page still holding the keyboard, it reported
    // its change a moment later and marked the fresh screen as typed in.
    const typedIn = document.activeElement;
    const host = $('#view');
    if (typedIn && host && host.contains(typedIn) && typedIn.blur) typedIn.blur();
    touched = false;
    // The ← at the top left, ⌨ Keys and the firm box outlive every screen.
    // Pressed from the keyboard, the screen they led to kept the keyboard on
    // them -- on the ← itself, hidden on the Dashboard -- and so never put
    // it anywhere of its own: Back landed nowhere near the line it left.
    const ae = document.activeElement;
    if (ae && ae.closest && !ae.closest('#page-actions')
        && ae.closest('.topbar, #sidebar') && ae.blur) ae.blur();
    // A lookup list left open under a box of the screen before -- its box
    // gone without ever losing the focus -- would otherwise stand on the
    // page, and while one is there Esc is taken as closing it.
    $$('.pick-list').forEach(x => x.remove());
    stepper = null;
    escHook = null;
    const path = hash.split('?')[0];
    if (moved || path !== screenWas) setCursor(null);
    screenWas = path;
    drawer(false);            // the screen asked for is now the one in front
  }
  document.addEventListener('keydown', onKey);
  watch();
  watchTyping();
  watchBar();
  watchFocus();
  paintBar();

  /** Put the focus back on the cursor once whatever moved it has settled.
   *  A list that reloads behind a dialog replaces its rows, so the row to
   *  focus is not the one that was focused -- it is whichever one the
   *  cursor has landed back on. */
  function focusCursor() {
    setTimeout(() => {
      const tr = cursor() || (lastRow >= 0 ? rows()[lastRow] : null);
      if (tr) setCursor(tr);
    }, 90);
  }

  return { setCursor, go, rows, focusCursor, focusEntry, unsavedWork, setStepper,
           saveThen, canSaveEntry, drawn,
           screenKeys, paintBar, showBar, helpSeen, openList, beginScreen,
           leaveScreen, arriving: () => arriving, handBack,
           returning: () => returning, place: leftAt, restorePlace,
           reveal, clearArea, setEscape };
})();

render();
