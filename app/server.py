#!/usr/bin/env python3
"""Billing & Order Management -- stdlib HTTP server.

Run:  python3 app/server.py [--port 8800] [--host 127.0.0.1]
"""
import argparse
import collections
import datetime
import gzip
import hashlib
import ipaddress
import json
import mimetypes
import os
import posixpath
import re
import socketserver
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api        # noqa: E402
import auth       # noqa: E402
import backup     # noqa: E402
import db         # noqa: E402
import pdf_print  # noqa: E402
import einvoice  # noqa: E402
import eway  # noqa: E402
import gstr1  # noqa: E402
import shree_sync  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, 'static')
STATIC_REAL = os.path.realpath(STATIC)


GZIP_MIN = 1024          # below this the header costs more than it saves
WRITE_PIECE = 256 * 1024  # an answer goes out this much at a time
NO_BUFFER = {55, 105, 10055}   # ENOBUFS: macOS, Linux; WSAENOBUFS
GZIP_LEVEL = 6
_TEXTUAL = ('application/json', 'text/', 'application/javascript',
            'image/svg+xml', 'application/xml')

# A request body bigger than this is refused unread. Nothing the app sends
# comes close -- the largest is a company logo pasted in as a data: URI -- and
# without a ceiling a single bad Content-Length asks the server to allocate
# whatever number it names.
MAX_BODY = 12 * 1024 * 1024

# A browser that goes away before its answer arrives -- a screen left while it
# was loading, a reload, a tab closed -- breaks the connection under the
# write. macOS says so as a broken pipe or a reset, Windows as 10053 (aborted)
# or 10054 (reset), and far more often. None of it is a fault, and there is
# nobody left to tell: logged as one, it made a false "error" in the log and a
# second traceback from trying to send the error page down the same socket.
CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

# Everything is served from this origin and nothing is fetched from anywhere
# else, so the page is told to refuse any other source. 'unsafe-inline' is for
# styles only -- the markup carries style="" attributes and the print code
# writes an @page rule -- while every script is a file of its own.
# frame-src/frame-ancestors are 'self' rather than 'none' because printing a
# document loads its PDF into a hidden same-origin frame.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' "
       "'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
       "connect-src 'self'; frame-src 'self'; object-src 'none'; "
       "form-action 'none'; base-uri 'none'; frame-ancestors 'self'")

# The files whose changing means the page in front of somebody is out of date.
_BUILD_FILES = ('static/app.js', 'static/style.css', 'static/index.html',
                'api.py', 'server.py', 'db.py', 'shree_sync.py')
_build_seen = [0.0, '']


def app_build():
    """A stamp that changes whenever the app's own files do.

    A browser holds the JavaScript it was loaded with, and this app is left
    open all day -- so a fix can sit in the folder for hours and never reach
    the screen, which looks exactly like a fix that did not work. Every API
    answer carries this stamp; the page keeps the first one it saw and says
    so when it stops matching. Stat'd at most every couple of seconds.
    """
    now = time.time()
    if _build_seen[1] and now - _build_seen[0] < 2.0:
        return _build_seen[1]
    here = os.path.dirname(os.path.abspath(__file__))
    parts = []
    for name in _BUILD_FILES:
        try:
            st = os.stat(os.path.join(here, name))
            parts.append('%d-%d' % (st.st_mtime, st.st_size))
        except OSError:
            parts.append('-')
    stamp = hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:12]
    _build_seen[0], _build_seen[1] = now, stamp
    return stamp


SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    # SAMEORIGIN, not DENY: another site still cannot frame the app and click
    # through it, but the print frame above is the app framing itself.
    'X-Frame-Options': 'SAMEORIGIN',
    'Referrer-Policy': 'same-origin',
    'Content-Security-Policy': CSP,
    # No other site may load an answer of ours as a script, image or anything
    # else, nor keep a handle on a window of ours it opened.
    'Cross-Origin-Resource-Policy': 'same-origin',
    'Cross-Origin-Opener-Policy': 'same-origin',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), '
                          'payment=(), usb=(), interest-cohort=()',
    'X-Permitted-Cross-Domain-Policies': 'none',
}

# How long a connection may sit without sending anything. A browser keeps
# its connections open between clicks and simply opens another when one has
# been closed; something that connects and never finishes a request is not
# left holding a thread.
IDLE_SECONDS = 120
# Connections served at once. A browser uses six; a flood beyond this is
# turned away at the door rather than given a thread each.
MAX_CLIENTS = 96

# Answers to GET requests, kept until anything is committed to the books
# (db.generation) or the day changes -- reports default to today.
ANSWER_BYTES = 64 * 1024 * 1024
ANSWER_ITEMS = 256
NOT_CACHED = {'session', 'sync', 'backup'}
FLIGHT_WAIT = 60

# Saves are made one at a time. A document takes the next free number by
# reading the last one and writing the one after; two saves side by side read
# the same number, and all but one of them failed with "already exists". A
# save takes milliseconds, so waiting a turn is not noticed.
WRITE_LOCK = threading.Lock()
WRITE_WAIT = 60

_static_lock = threading.Lock()
_static_cache = {}       # path -> (key, raw_bytes, gzipped_bytes or None)

# Hostnames the browser may have used to reach us, beyond the plain addresses.
# A name resolves through DNS, and DNS can be made to point a name that was
# once a public address at 127.0.0.1 a second later -- which is how a web page
# gets to talk to a server bound to loopback. Addresses cannot be re-pointed,
# so they are always allowed and names have to be named here.
ALLOWED_HOSTS = {h.strip().lower()
                 for h in (os.environ.get('BILLING_ALLOWED_HOSTS') or
                           'localhost').split(',') if h.strip()}

# For now Billing is used on the computer it runs on and nowhere else (10 Oct
# 2026: "for now keep the software only locally"). A request from the wifi,
# or relayed by a proxy on this computer such as Tailscale's, is turned away
# whatever it carries; the passcode and the rest wait for when that changes.
LOCAL_ONLY = True

# Names written down at this computer, one a line -- `billing.py --anywhere`
# puts this computer's Tailscale name here (8 Oct 2026: "use it from 2 devices,
# other network, anywhere"). Read again when it changes, so a name added while
# the app runs works without a restart.
HOSTS_FILE = os.path.join(os.path.dirname(db.DB_PATH), 'allowed-hosts.txt')
_hosts_seen = [None, frozenset()]           # (mtime, names)


def _named_hosts():
    try:
        mtime = os.path.getmtime(HOSTS_FILE)
    except OSError:
        return frozenset()
    if mtime != _hosts_seen[0]:
        try:
            with open(HOSTS_FILE, encoding='utf-8') as f:
                names = frozenset(
                    line.split('#', 1)[0].strip().lower() for line in f)
        except OSError:
            return _hosts_seen[1]
        _hosts_seen[:] = [mtime, names - {''}]
    return _hosts_seen[1]

_HEADER_UNSAFE = re.compile(r'[\r\n\x00-\x1f\x7f"\\]')


def _compressible(ctype):
    return any(ctype.startswith(t) for t in _TEXTUAL)


def safe_filename(name, fallback='download'):
    """A name fit to go in a Content-Disposition header.

    The name is built from the document number, which is typed by whoever
    raised it. A newline in there would end the header and let the rest be
    read as headers of its own, so anything that is not plainly part of a
    filename is dropped.
    """
    cleaned = _HEADER_UNSAFE.sub('', str(name or '')).replace('/', '-') \
        .replace('\\', '-').strip(' .')
    return cleaned[:120] or fallback


def _under_static(path):
    """The file this URL asks for, or None if it is not one we serve.

    A plain "does the resolved path start with the static folder" test is two
    things short of safe. It passes for a sibling folder whose name merely
    begins the same way, and on Windows a backslash is a path separator that
    posixpath.normpath leaves alone -- so /..\\..\\file normalises to nothing
    and is then split apart by os.path.join. Both are ruled out here: the path
    is taken apart into its own segments, and .. is never one of them.
    """
    seps = {'/', os.path.sep, os.path.altsep} - {None}
    parts = []
    for seg in posixpath.normpath(path).replace('\\', '/').split('/'):
        if not seg or seg == '.':
            continue
        if seg == '..' or any(s in seg for s in seps):
            return None
        parts.append(seg)
    if not parts:
        return None
    full = os.path.join(STATIC, *parts)
    real = os.path.realpath(full)
    if real != STATIC_REAL and not real.startswith(STATIC_REAL + os.sep):
        return None                      # a symlink pointing out of the folder
    return full if os.path.isfile(full) else None


def _static_bytes(full, st, want_gzip):
    """File contents, re-read and re-compressed only when the file changed.

    The bundle is a few hundred kilobytes and is asked for on every reload, so
    the compressed copy is kept beside the raw one rather than being rebuilt
    each time.
    """
    key = (st.st_mtime, st.st_size)
    with _static_lock:
        hit = _static_cache.get(full)
    if not hit or hit[0] != key:
        with open(full, 'rb') as fh:
            data = fh.read()
        hit = (key, data, None)
        with _static_lock:
            _static_cache[full] = hit
    if not want_gzip:
        return hit[1], False
    if hit[2] is None:
        packed = gzip.compress(hit[1], GZIP_LEVEL)
        if len(packed) >= len(hit[1]):
            packed = b''                 # compressing it bought nothing
        hit = (hit[0], hit[1], packed)
        with _static_lock:
            _static_cache[full] = hit
    return (hit[2], True) if hit[2] else (hit[1], False)


def json_default(o):
    try:
        return float(o)
    except (TypeError, ValueError):
        return str(o)


class AnswerCache(object):
    """Serialised answers, oldest dropped first, bounded in bytes."""

    def __init__(self, max_bytes, max_items):
        self.max_bytes, self.max_items = max_bytes, max_items
        self.lock = threading.Lock()
        self.items = collections.OrderedDict()
        self.size = 0
        self.flights = {}

    def claim(self, key, gen):
        """True if this request is to work the answer out. Otherwise the same
        question is already being answered: wait for that answer rather than
        work it out a second time. After a save every open screen asks again
        at once, and a dozen copies of one report fighting over the
        interpreter each took a dozen times as long."""
        with self.lock:
            ev = self.flights.get((key, gen))
            if ev is None:
                self.flights[(key, gen)] = threading.Event()
                return True
        ev.wait(FLIGHT_WAIT)
        return False

    def landed(self, key, gen):
        with self.lock:
            ev = self.flights.pop((key, gen), None)
        if ev is not None:
            ev.set()

    def get(self, key, gen):
        with self.lock:
            hit = self.items.get(key)
            if hit is None:
                return None
            if hit[0] != gen:
                self._drop(key)
                return None
            self.items.move_to_end(key)
            return hit

    def put(self, key, gen, body):
        if len(body) > self.max_bytes // 8:
            return
        with self.lock:
            if key in self.items:
                self._drop(key)
            self.items[key] = [gen, body, None]
            self.size += len(body)
            while self.items and (self.size > self.max_bytes
                                  or len(self.items) > self.max_items):
                self._drop(next(iter(self.items)))

    def packed(self, hit):
        """The gzipped body, made once."""
        if hit[2] is None:
            packed = gzip.compress(hit[1], _gzip_level(len(hit[1])))
            hit[2] = packed if len(packed) < len(hit[1]) else b''
            with self.lock:
                self.size += len(hit[2])
        return hit[2] or None

    def _drop(self, key):
        gen, body, packed = self.items.pop(key)
        self.size -= len(body) + len(packed or b'')


ANSWERS = AnswerCache(ANSWER_BYTES, ANSWER_ITEMS)


def _gzip_level(n):
    # A big list is compressed quickly rather than tightly: the time saved on
    # the wire is gone if the server spends it squeezing.
    return 1 if n > 512 * 1024 else GZIP_LEVEL


def encode_json(data):
    """Compact, and UTF-8 rather than \\u escapes: a third smaller."""
    return json.dumps(data, default=json_default, separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')


def _fault_ref():
    return os.urandom(3).hex().upper()


class Handler(BaseHTTPRequestHandler):
    server_version = 'Billing'
    sys_version = ''
    protocol_version = 'HTTP/1.1'
    timeout = IDLE_SECONDS
    # Headers and body go out as two writes. With Nagle's algorithm on, the
    # body waits for the headers to be acknowledged, and Windows holds that
    # acknowledgement back for up to 200 ms -- on every answer.
    disable_nagle_algorithm = True

    def version_string(self):
        # No Python version for anyone to look up the weaknesses of.
        return self.server_version

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt, *args):
        if not fmt.startswith('"%s"'):
            # Not a request but a complaint about one. An idle connection
            # timing out is how a browser's spare connections end, not news.
            if 'timed out' not in fmt:
                sys.stderr.write('  %s\n' % (fmt % args))
            return
        if not hasattr(self, 'path'):
            return                      # a request line that never parsed
        if self.path.startswith('/api/') or not self._is_local():
            # How long the answer took to work out, so a slow screen on the
            # computer the books are kept on can be read off the log
            # (5 Oct 2026: "data loading is taking time" on Windows).
            began = getattr(self, '_began', None)
            sys.stderr.write('  %s%s %s%s\n' % (
                '' if self._is_local() else '[%s] ' % self._device(),
                self.command, self.path.split('?')[0]
                if not self.path.startswith('/api/') else self.path,
                '' if began is None
                else '  %d ms' % ((time.perf_counter() - began) * 1000)))

    def parse_request(self):
        self._began = time.perf_counter()   # for the log: see log_message
        return super().parse_request()

    def _firm_asked(self):
        """The firm the page asking is showing (X-Firm), or ''.

        The firm remembered in the books is the one switched to last, from
        whichever window did it. With two windows open -- the icon clicked
        again, another computer -- a switch in one put the other's screens on
        the wrong firm under its own firm's name: its lists showed the other
        books, and a document opened from it said "Document not found"
        (found 5 Oct 2026). Each page now says which firm it is on."""
        v = (self.headers.get('X-Firm') or '').strip()
        return v if v.isdigit() and len(v) <= 9 else ''

    def _is_local(self):
        """A request from this computer itself -- not from the network, and
        not relayed by a proxy in front of the app.

        A proxy on this computer (Tailscale's, for one) connects from
        127.0.0.1 too. It says so in its headers, and the name the browser
        asked for is its own, not this computer's -- either marks the request
        as another device's."""
        try:
            peer = ipaddress.ip_address(self.client_address[0])
        except (ValueError, IndexError, TypeError):
            return False
        if not peer.is_loopback:
            return False
        headers = getattr(self, 'headers', None) or {}
        if any(headers.get(h) for h in ('X-Forwarded-For', 'Forwarded',
                                        'X-Real-IP', 'X-Forwarded-Host')):
            return False
        name = self._host_name()
        if name == 'localhost':
            return True
        try:
            return ipaddress.ip_address(name).is_loopback
        except ValueError:
            return False

    def _device(self):
        """The address of the device asking: the computer that connected, or
        -- through a proxy on this computer -- the one the proxy names. The
        last address on the list is the one the proxy itself saw."""
        try:
            addr = self.client_address[0]
        except (IndexError, TypeError):
            return '?'
        try:
            loopback = ipaddress.ip_address(addr).is_loopback
        except ValueError:
            return addr
        headers = getattr(self, 'headers', None) or {}
        relayed = (headers.get('X-Forwarded-For') or '').split(',')[-1].strip()
        if loopback and relayed:
            try:
                return str(ipaddress.ip_address(relayed))
            except ValueError:
                return relayed[:64]
        return addr

    def _auth_required(self):
        """The passcode is asked of anybody not at this computer, whether or
        not it has been switched on for the computer itself."""
        return auth.ENABLED or not self._is_local()

    def _send(self, status, body, ctype='application/json; charset=utf-8',
              extra=None, cache='no-store'):
        # From here on a failure is the socket's, not the request's.
        self._answering = True
        if isinstance(body, str):
            body = body.encode('utf-8')
        enc = None
        if isinstance(body, tuple):                 # (raw, already gzipped)
            body, enc = body
        # Compressing for the same computer costs time and saves nothing:
        # nothing crosses a wire.
        elif (self._wants_gzip() and len(body) >= GZIP_MIN
              and _compressible(ctype) and not self._is_local()):
            packed = gzip.compress(body, _gzip_level(len(body)))
            # Only pay the header if it actually bought something.
            if len(packed) < len(body):
                body, enc = packed, 'gzip'
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        if cache:
            self.send_header('Cache-Control', cache)
        if enc:
            self.send_header('Content-Encoding', enc)
            self.send_header('Vary', 'Accept-Encoding')
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        # Behind a proxy that speaks https, the browser is told never to use
        # plain http for this address again.
        if self.headers.get('X-Forwarded-Proto') == 'https':
            self.send_header('Strict-Transport-Security',
                             'max-age=31536000')
        self.send_header('X-App-Build', app_build())
        if shree_sync.MANAGER.books_updated:
            self.send_header('X-Books-Updated', shree_sync.MANAGER.books_updated)
        # A save sent now waits for the update; the page says so.
        if shree_sync.MANAGER.blocking_writes:
            self.send_header('X-Books-Updating', '1')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self._write_body(body)

    def _write_body(self, body):
        """The answer, a piece at a time.

        Written in one go, a whole year's vouchers (26 MB) failed half way
        with "No buffer space available" while other answers were going out
        -- the operating system had no room to take it all at once (ENOBUFS;
        Windows has the same WSAENOBUFS). A piece that meets that is offered
        again after a moment; anything else is the browser gone, as before.
        """
        view = memoryview(body)
        for at in range(0, len(view), WRITE_PIECE):
            piece = view[at:at + WRITE_PIECE]
            for attempt in range(40):
                try:
                    self.wfile.write(piece)
                    break
                except OSError as e:
                    if e.errno not in NO_BUFFER or attempt == 39:
                        raise
                    time.sleep(0.025)

    def _wants_gzip(self):
        return 'gzip' in (self.headers.get('Accept-Encoding') or '')

    def _json(self, data, status=200):
        self._send(status, encode_json(data))

    def _error(self, message, status=400, extra=None):
        body = {'error': message}
        if extra:
            body.update(extra)
        self._json(body, status)

    def _drain(self):
        """Read the whole request body up front.

        Every byte has to leave the socket even when the route never looks at
        the body -- on a keep-alive connection anything left behind is read as
        the start of the next request. Returns an error message when the body
        cannot be accepted, in which case the connection is not reusable and
        the caller must close it.
        """
        self._raw_body = b''
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return 'Content-Length is not a number.'
        if n < 0:
            return 'Content-Length is not a number.'
        if n > MAX_BODY:
            return ('Request body is too large (%d bytes; the limit is %d).'
                    % (n, MAX_BODY))
        if n:
            self._raw_body = self.rfile.read(n)
            if len(self._raw_body) != n:
                return 'The request body ended early.'
        return None

    # ------------------------------------------------------------ guarding
    def _host_ok(self):
        """Reject a Host this server is not actually known by.

        Without this, a page on the internet can point a name it controls at
        127.0.0.1 and then talk to the app in the browser of whoever is
        reading it -- the browser sees same-origin, because the origin is that
        name. Plain addresses cannot be re-pointed that way, so they pass.
        """
        name = self._host_name()
        if not name:
            return False
        if name in ALLOWED_HOSTS:
            return True
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return name in _named_hosts()

    def _host_name(self):
        """The name or address in Host, without its port, lower case."""
        headers = getattr(self, 'headers', None) or {}
        host = (headers.get('Host') or '').strip()
        if host.startswith('['):                        # [::1]:8800
            name = host[1:host.find(']')] if ']' in host else host[1:]
        else:
            name = host.rsplit(':', 1)[0] if host.count(':') == 1 else host
        return name.lower()

    def _origin_ok(self):
        """Same-origin check for anything that writes.

        A form on any website can post to this server without asking the
        browser's permission first -- no preflight is required for a form --
        so a page open in another tab could rewrite the books. Browsers do
        send Origin on every write, so an Origin naming somebody else is
        refused. A request with neither Origin nor Referer is not a browser
        doing this behind the user's back (curl, a script), and is allowed.
        """
        origin = self.headers.get('Origin')
        if origin:
            if origin == 'null':
                return False
            return self._same_site(urlparse(origin))
        ref = self.headers.get('Referer')
        if ref:
            return self._same_site(urlparse(ref))
        return True

    def _same_site(self, got):
        # A browser leaves the port out of https://name -- through a proxy
        # on 443 the Host leaves it out too, and both mean 443.
        try:
            port = got.port
        except ValueError:
            return False
        if not port:
            port = 443 if got.scheme == 'https' else 80
        return '%s:%s' % ((got.hostname or '').lower(), port) == \
            self._own_hostport(got.scheme)

    def _own_hostport(self, scheme):
        host = (self.headers.get('Host') or '').strip()
        if host.startswith('['):
            name = host[1:host.find(']')] if ']' in host else host[1:]
            port = host.rsplit(':', 1)[1] if host.rfind(':') > host.find(']') \
                else ''
        elif host.count(':') == 1:
            name, port = host.rsplit(':', 1)
        else:
            name, port = host, ''
        if not port:
            port = '443' if scheme == 'https' else '80'
        return '%s:%s' % (name.lower(), port)

    def _cookie(self, name):
        raw = self.headers.get('Cookie') or ''
        for part in raw.split(';'):
            k, _, v = part.strip().partition('=')
            if k == name:
                return v
        return ''

    def _signed_in(self, conn):
        """No passcode chosen yet means the app is still being set up: at this
        computer it answers, so the first screen can be the one that sets it.
        From anywhere else nothing is answered until one has been chosen --
        here, at this computer."""
        if not self._auth_required():
            return True
        if not auth.is_set(conn):
            return self._is_local()
        return auth.valid(conn, self._cookie(auth.COOKIE))

    def _set_session_cookie(self, token, hours=auth.SESSION_HOURS):
        bits = ['%s=%s' % (auth.COOKIE, token), 'Path=/', 'HttpOnly',
                'SameSite=Strict']
        if token:
            bits.append('Max-Age=%d' % int(hours * 3600))
        else:
            bits.append('Max-Age=0')
        # Secure is set only behind TLS -- on plain http the browser would
        # drop the cookie and the app could never be unlocked.
        if self.headers.get('X-Forwarded-Proto') == 'https':
            bits.append('Secure')
        self._pending_cookie = '; '.join(bits)

    def _body(self):
        raw = getattr(self, '_raw_body', b'')
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode('utf-8'))
        except ValueError:
            raise api.ApiError('Request body is not valid JSON.')
        # Every save reads named fields off the body. A list, a string or a
        # bare number used to reach `.get` and come back as "Something went
        # wrong" instead of saying what was wrong with the request.
        if not isinstance(data, dict):
            raise api.ApiError('Request body must be a JSON object.')
        return _clean_body(data)

    # -------------------------------------------------------------- verbs
    def do_GET(self):
        self._dispatch('GET')

    def do_HEAD(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PUT(self):
        self._dispatch('PUT')

    def do_DELETE(self):
        self._dispatch('DELETE')

    def _dispatch(self, method):
        self._answering = False
        # The body is read before anything else so the socket is clean, but a
        # refusal must not leave the rest of it to be read as a new request.
        too_big = self._drain()
        if too_big:
            self.close_connection = True
            return self._error(too_big, 413)
        if not self._host_ok():
            self.close_connection = True
            return self._error(
                'This server does not answer to that address. Reach it on '
                '127.0.0.1, or name the host in BILLING_ALLOWED_HOSTS.', 403)
        if LOCAL_ONLY and not self._is_local():
            self.close_connection = True
            return self._error('Billing is used only on the computer it runs '
                               'on.', 403)
        if method not in ('GET', 'HEAD') and not self._origin_ok():
            return self._error(
                'That request came from another website, so it was refused.',
                403)

        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path.startswith('/api/'):
            # Browsers say where a request came from. Nothing on another site
            # has any business reading or triggering the API -- not even a
            # link, which would carry the owner's session with it.
            if (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
                return self._error('That request came from another website, '
                                   'so it was refused.', 403)
            # A write's body is JSON. A form on another site can only send
            # form encodings or plain text without asking the browser first.
            ctype = (self.headers.get('Content-Type') or '').split(';')[0]
            if (getattr(self, '_raw_body', b'') and method != 'GET'
                    and ctype.strip().lower() != 'application/json'):
                return self._error('Send the request as JSON.', 415)
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            parts = path[5:].strip('/').split('/')
            head = parts[0]
            # SHREE's folders and updates are looked after at the computer
            # Billing runs on; from the network they can only be watched.
            if (head == 'sync' and method != 'GET' or parts[:2] in
                    (['sync', 'browse'], ['sync', 'discover'])) \
                    and not self._is_local():
                return self._error('SHREE updates are managed on the computer '
                                   'Billing runs on.', 403)
            # The update screen asks how an update is going every second or
            # two; that is not somebody using the books.
            if head != 'sync':
                shree_sync.MANAGER.touch()
            # What may go ahead while an update from SHREE is running: reading,
            # signing in, switching firm, and asking for the update itself.
            writing = method not in ('GET', 'HEAD') and not (
                head == 'session'
                or (head == 'companies' and parts[-1] == 'activate')
                or (head == 'sync' and parts[1:2] == ['run']))
            # A save made while an update runs is held here until it is in.
            gate = head != 'sync' or writing
            if gate:
                blocked = shree_sync.MANAGER.enter(writing)
                if blocked:
                    return self._error(blocked, 503, {'syncing': True})
            # One save at a time -- see WRITE_LOCK.
            turn = False
            if method != 'GET':
                turn = WRITE_LOCK.acquire(timeout=WRITE_WAIT)
                if not turn:
                    if gate:
                        shree_sync.MANAGER.leave(writing)
                    return self._error('Another save is taking unusually long. '
                                       'Nothing was saved -- try again.', 503)
            conn = self._conn()
            failed = None
            leading = False
            try:
                # The page says which update it was opened after. A save from
                # a page that is behind only goes ahead if nothing it rewrites
                # or points at was changed by an update since.
                seen = self.headers.get('X-Books-Seen')
                if (writing and seen is not None
                        and seen != shree_sync.MANAGER.books_updated):
                    try:
                        body = self._body()
                    except api.ApiError:
                        body = {}
                    why = ''
                    if head == 'bulk-delete':
                        # Every ticked row is a row this save rewrites.
                        ticked = body.get('items') or []
                        for it in (ticked if isinstance(ticked, list)
                                   else [])[:api.BULK_MAX]:
                            if not isinstance(it, dict):
                                continue
                            table = _BULK_TABLE.get(str(it.get('kind')))
                            if table and str(it.get('id', '')).isdigit():
                                why = shree_sync.MANAGER.stale_for(
                                    seen, (table, int(it['id'])), {})
                                if why:
                                    break
                    else:
                        why = shree_sync.MANAGER.stale_for(
                            seen, *_save_targets(parts, body))
                    if why:
                        return self._error(why, 409, {'books_updated': True})
                # The books are behind the passcode. `session` is the way in
                # and is the only route answered without one; everything else
                # gets a 401 the browser turns into the lock screen.
                if head != 'session' and not self._signed_in(conn):
                    return self._error(
                        'Locked. Enter the passcode to continue.', 401,
                        {'locked': True,
                         'passcode_set': auth.is_set(conn)})
                # A read answered before, from the same state of the books.
                key = gen = None
                if method == 'GET' and head not in NOT_CACHED:
                    gen = db.generation()
                    key = (path, tuple(sorted(q.items())),
                           datetime.date.today().isoformat(),
                           self._is_local(), self._firm_asked())
                    hit = ANSWERS.get(key, gen)
                    if hit is None:
                        leading = ANSWERS.claim(key, gen)
                        if not leading:
                            hit = ANSWERS.get(key, gen)
                    if hit is not None:
                        packed = (ANSWERS.packed(hit) if self._wants_gzip()
                                  and not self._is_local() else None)
                        return self._send(200, (packed, 'gzip') if packed
                                          else hit[1])
                result = route(method, path[5:].strip('/'), q, self, conn)
                if isinstance(result, tuple) and len(result) == 3:
                    # (bytes, filename, content-type). A PDF opens in the
                    # viewer; anything else is a file to save.
                    blob, name, ctype = result
                    inline = ctype.startswith('application/pdf')
                    self._send(200, blob, ctype,
                               {'Content-Disposition': '%s; filename="%s"'
                                % ('inline' if inline else 'attachment',
                                   safe_filename(name, 'document'))})
                elif isinstance(result, tuple):      # (csv_text, filename)
                    text, name = result
                    # **Excel on Windows needs the byte-order mark.** Handed a
                    # .csv with none, it reads the file in the machine's ANSI
                    # code page rather than UTF-8, and every rupee sign and
                    # every name that is not plain English comes out as
                    # mojibake -- on the machine these exports are actually
                    # opened on. Three bytes fix it, and every other reader
                    # skips them.
                    self._send(200, '\ufeff' + text, 'text/csv; charset=utf-8',
                               {'Content-Disposition':
                                'attachment; filename="%s"'
                                % safe_filename(name, 'export.csv')})
                else:
                    extra = {}
                    if getattr(self, '_pending_cookie', None):
                        extra['Set-Cookie'] = self._pending_cookie
                        self._pending_cookie = None
                    body = encode_json(result)
                    if key is not None and not extra:
                        ANSWERS.put(key, gen, body)
                    self._send(200, body, extra=extra or None)
                failed = False
                if leading:
                    ANSWERS.landed(key, gen)
                    leading = False
            except api.ApiError as e:
                failed = True
                self._error(e.message, e.status, getattr(e, 'extra', None))
            except Exception as e:                    # noqa: BLE001
                failed = True
                if (isinstance(e, sqlite3.IntegrityError)
                        and 'FOREIGN KEY' in str(e)):
                    # A save naming an account, product or document that is
                    # no longer there -- a page left open while it was
                    # deleted, most often. What it had begun is rolled back
                    # below; say what to do about it.
                    self._error('That names an account, product or document '
                                'that is no longer in the books -- it was '
                                'deleted or moved. Reload the page and try '
                                'again.', 409)
                elif isinstance(e, CLIENT_GONE) and self._answering:
                    # The browser went while its answer was being written.
                    # The connection is finished, and whatever the request
                    # did is kept or undone just as it was before this was
                    # told apart from a fault.
                    self.close_connection = True
                else:
                    ref = _fault_ref()
                    sys.stderr.write('  error %s on %s %s\n' % (ref, method,
                                                                path))
                    traceback.print_exc()
                    # The details go to the log. This computer's own screen is
                    # told what broke; another device only that something did.
                    self._error('Something went wrong (ref %s)%s' % (
                        ref, ': %s: %s' % (type(e).__name__, e)
                        if self._is_local() else '. The details are in the '
                        'server log.'), 500)
            finally:
                if leading:                   # whoever waits need not
                    ANSWERS.landed(key, gen)
                # A read never leaves anything behind, and a save that failed
                # half way is undone -- left open, its half would be committed
                # by whatever request used this connection next, and the
                # open transaction would hold every other save waiting.
                try:
                    if conn.in_transaction:
                        if method == 'GET' or failed is not False:
                            conn.rollback()
                        else:
                            conn.commit()
                except sqlite3.Error:
                    self._close_conn()
                if turn:
                    WRITE_LOCK.release()
                # The connection outlives the request and is closed with the
                # socket, in `_close_conn` below.
                if gate:
                    shree_sync.MANAGER.leave(writing)
        else:
            self._serve_static(path)

    # -------------------------------------------------- the books, once
    def _conn(self):
        """One SQLite handle per keep-alive connection, not per request.

        **Opening the books is not free, and on Windows it is the least free.**
        Every request used to open the 58 MB file, set five pragmas, throw away
        the 16 MB page cache it had just been given, and close it again -- so
        no query ever found a warm page and the same index was read off disk
        over and over. A file open costs more on Windows than on the machine
        this was written on, and the virus scanner reads the file again each
        time it is opened.

        The browser holds its connections open (HTTP/1.1, and it keeps about
        six of them), so a handle kept for the life of a connection is reused
        by request after request and its cache stays warm. Each connection is
        served by its own thread and gets its own handle, which is what SQLite
        wants -- a handle is never touched by two threads.
        """
        conn = getattr(self, '_db', None)
        if conn is None:
            conn = self._db = db.connect()
        return conn

    def _close_conn(self):
        conn = getattr(self, '_db', None)
        self._db = None
        if conn is not None:
            try:
                conn.close()
            except Exception:                          # noqa: BLE001
                pass

    def finish(self):
        # Called once the socket is done with, however it ended.
        try:
            BaseHTTPRequestHandler.finish(self)
        finally:
            self._close_conn()

    def _serve_static(self, path):
        if path in ('/', ''):
            path = '/index.html'
        full = _under_static(path)
        if not full:
            full = os.path.join(STATIC, 'index.html')       # SPA fallback
            if not os.path.isfile(full):
                return self._send(404, 'Not found', 'text/plain')
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        st = os.stat(full)
        etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        # Files are edited in place, so the browser must ask every time --
        # but a 304 saves it re-downloading and re-parsing the whole bundle.
        if self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Length', '0')
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            return
        extra = {'ETag': etag}
        body, packed = _static_bytes(
            full, st, self._wants_gzip() and st.st_size >= GZIP_MIN
            and _compressible(ctype))
        if packed:
            extra['Content-Encoding'] = 'gzip'
            extra['Vary'] = 'Accept-Encoding'
            # _send would otherwise compress the already-compressed bytes.
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD':
                self._write_body(body)
            return
        self._send(200, body, ctype, extra, cache='no-cache')


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise api.ApiError('Invalid id.', 404)


# The row a save rewrites, by the address it is sent to.
_ROW_OF = {'invoices': 'invoice', 'vouchers': 'voucher', 'ledgers': 'ledger',
           'parties': 'ledger', 'items': 'item'}
# The rows a save points at, by the names its body gives them.
_REF_OF = {'party_id': 'ledger', 'sales_ac_id': 'ledger', 'bank_id': 'ledger',
           'ledger_id': 'ledger', 'item_id': 'item', 'from_challan': 'invoice'}


_BULK_TABLE = {'invoice': 'invoice', 'voucher': 'voucher', 'ledger': 'ledger',
               'item': 'item'}


# ------------------------------------------------ what a save may carry ---
# The screens send text as text and figures as figures, but a request is
# only as good as whatever sent it. A number where text was expected, a
# figure typed as "1,000", or a list where one value belonged used to reach
# `.strip()` or `float()` and come back as "Something went wrong" (found by
# feeding every save field the wrong kind of value, 29 Sep 2026). These are
# read here once, before any save sees them: a figure may carry commas, a
# rupee sign or spaces; text may arrive as a number; anything else is
# refused by the field's name.
_FIGURES = {
    'qty', 'weight', 'rate', 'incl_price', 'discount_pct', 'gst_rate',
    'cess_rate', 'cutting', 'transport', 'loading', 'taxable_adj',
    'round_off', 'amount', 'debit', 'credit', 'opening_bal', 'credit_limit',
    'credit_days', 'sale_rate', 'purchase_rate', 'opening_qty',
    'reorder_level', 'transport_distance', 'default_gst', 'next_no', 'pad'}
# (A salary line's units are read as leniently as the salary screen always
# read them -- api._amount -- so they are left alone here.)
_TEXTS = {
    'doc_type', 'series', 'invoice_no', 'invoice_type', 'invoice_date',
    'ref_no', 'driver_name', 'payment_terms', 'dept', 'transporter',
    'vehicle_no', 'lr_no', 'lr_date', 'cases', 'eway_no',
    'transporter_gstin', 'transport_mode', 'vehicle_type', 'sub_supply_type',
    'remarks', 'description', 'hsn', 'unit', 'item_detail', 'thickness',
    'cutting_size', 'kind', 'voucher_no', 'voucher_date', 'mode',
    'order_no', 'order_date', 'due_date', 'status', 'party_kind', 'gstin',
    'name', 'code', 'city', 'state', 'state_code', 'address1', 'address2',
    'phone', 'mobile', 'email', 'pin', 'pan', 'note', 'reason', 'entry_date',
    'category', 'steel_type', 'steel_quality', 'steel_thick', 'gst_type',
    'stock_cat',
    'narration', 'prefix', 'fy', 'pay_basis', 'direction', 'notes'}
_LISTS = {'lines', 'items', 'counters'}
_IDS = {'id', 'order_id', 'item_id', 'party_id', 'ledger_id', 'bank_id',
        'group_id', 'sales_ac_id', 'from_challan', 'company_id',
        'expense_id', 'linked_ledger', 'home_company', 'person_id'}
_FIGURE_RE = re.compile(r'^[+-]?(\d+(\.\d*)?|\.\d+)$')


def _clean_body(v, key=None):
    # Each field's own rule first: a list or an object sent where one value
    # belongs is refused by name, not walked into.
    if key in _LISTS:
        if v is None:
            return v
        if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
            raise api.ApiError('"%s" should be a list of entries.' % key)
        return [_clean_body(x) for x in v]
    if key in _FIGURES:
        if isinstance(v, (bool, dict, list)):
            raise api.ApiError('"%s" should be a figure.' % key)
        if isinstance(v, str):
            t = re.sub(r'[,\s\u20b9]', '', v)
            if not t:
                return v
            if not _FIGURE_RE.match(t):
                raise api.ApiError('"%s" is not a figure for %s.'
                                   % (v.strip()[:30], key.replace('_', ' ')))
            return float(t)
        return v
    if key in _TEXTS:
        if v is None or v is False:
            return v
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(int(v)) if float(v).is_integer() else str(v)
        if not isinstance(v, str):
            raise api.ApiError('"%s" should be text.' % key)
        return v
    if key in _IDS:
        if v is None or v == '':
            return v
        if isinstance(v, bool) or not (
                isinstance(v, int) or (isinstance(v, float) and v.is_integer())
                or (isinstance(v, str) and v.strip().isdigit())):
            raise api.ApiError('"%s" is not a valid %s.'
                               % (str(v)[:30], key.replace('_', ' ')))
        return v
    if isinstance(v, dict):
        return {k: _clean_body(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean_body(x) for x in v]
    return v


def _save_targets(parts, body):
    """(target, refs) for MANAGER.stale_for."""
    target = None
    table = _ROW_OF.get(parts[0] if parts else '')
    if table and len(parts) > 1 and parts[1].isdigit():
        target = (table, int(parts[1]))
    refs = {}

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                t = _REF_OF.get(k)
                if t and str(x).isdigit():
                    refs.setdefault(t, set()).add(int(x))
                elif isinstance(x, (dict, list)):
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(body)
    return target, refs


def _local(handler):
    """True for a request from this computer itself -- not one relayed by a
    proxy on it (Handler._is_local)."""
    return handler._is_local()


def _who(handler):
    """Which device a save came from, for a document's history: this
    computer, or the address of the other one."""
    if _local(handler):
        return 'this computer'
    return handler._device()


def _flag(v):
    return str(v or '').lower() in ('1', 'true', 'yes', 'on')


def _tristate(v):
    """Yes, no, or "you decide" -- for a box that can be left alone.

    An e-invoice asks for the e-way bill alongside it or does not, and the
    screen can say either; left out of the query it is the bill's own
    transport that settles it.
    """
    if v is None or str(v).strip() == '':
        return None
    return _flag(v)


READ_ONLY = {'meta', 'next-no', 'allocation', 'bills', 'reports', 'export',
             'backup',
             'gstr1', 'last-rates'}
READ_ONLY_SUB = {'gst-bill-draft', 'eway', 'eway.json', 'einvoice',
                 'einvoice.json', 'pdf', 'challan-draft', 'invoice-draft'}


def _delete_gate(handler, conn):
    """A delete of a challan, a bill (any document) or an account goes ahead
    only with the delete password (4 Oct 2026). The page sends it in the
    X-Delete-Password header, percent-encoded so any letters travel; the
    answer's `delete_password` says why one was refused, for the page to
    ask again. Wrong guesses are slowed the way the passcode's are."""
    who = 'delete:' + handler._device()
    if not auth.delete_password_set(conn):
        raise api.ApiError('Choose a delete password first: deleting a '
                           'challan, a bill or an account asks for it.', 403,
                           {'delete_password': 'unset'})
    wait = auth.locked_for(who)
    if wait:
        raise api.ApiError('Too many wrong delete passwords. Try again in '
                           '%d seconds.' % wait, 429,
                           {'delete_password': 'wait', 'wait': wait})
    given = unquote(handler.headers.get('X-Delete-Password') or '')
    if not given:
        raise api.ApiError('Deleting needs the delete password.', 403,
                           {'delete_password': 'missing'})
    if not auth.check_delete_password(conn, given):
        auth.note_failure(who)
        raise api.ApiError('That is not the delete password.', 403,
                           {'delete_password': 'wrong'})
    auth.clear_failures(who)


def _bulk_guarded(data):
    """Whether a bulk delete takes a document or an account -- an order
    only with what was made from it."""
    if data.get('check'):
        return False
    ticked = data.get('items') if isinstance(data.get('items'), list) else []
    kinds = {str(it.get('kind')) for it in ticked if isinstance(it, dict)}
    return bool(kinds & {'invoice', 'ledger'}
                or ('order' in kinds and data.get('cascade')))


def route(method, path, q, handler, conn):
    parts = path.split('/') if path else []
    head = parts[0] if parts else ''
    rest = parts[1:]
    body = handler._body if method in ('POST', 'PUT') else (lambda: {})

    # ------------------------------------------------- delete password --
    if head == 'delete-password':
        if method == 'GET':
            return dict(set=auth.delete_password_set(conn),
                        min=auth.DELETE_MIN, here=handler._is_local())
        if method == 'POST':
            data = body()
            first = not auth.delete_password_set(conn)
            # The first one is chosen at the computer Billing runs on, so
            # nobody else on the network can choose it before the owner;
            # changing it anywhere takes the one it replaces.
            if first and not handler._is_local():
                raise api.ApiError('The delete password is chosen on the '
                                   'computer Billing runs on.', 403)
            who = 'delete:' + handler._device()
            wait = auth.locked_for(who)
            if wait and not first:
                raise api.ApiError('Too many wrong delete passwords. Try '
                                   'again in %d seconds.' % wait, 429)
            try:
                auth.set_delete_password(conn, data.get('new'),
                                         data.get('current'))
            except ValueError as e:
                if not first and 'current' in str(e):
                    auth.note_failure(who)
                raise api.ApiError(str(e))
            auth.clear_failures(who)
            return dict(set=True)
        raise api.ApiError('Method not allowed.', 405)

    # --------------------------------------------------------- the lock --
    if head == 'session':
        who = handler._device()

        if not handler._auth_required():
            return dict(enabled=False, passcode_set=False, signed_in=True,
                        locked_for=0)

        if method == 'GET':
            return dict(enabled=True, passcode_set=auth.is_set(conn),
                        signed_in=handler._signed_in(conn),
                        locked_for=auth.locked_for(who))

        if method == 'DELETE':                      # sign out
            auth.end_all_sessions(conn)
            handler._set_session_cookie('')
            return dict(signed_in=False)

        if method == 'POST':
            data = body()
            # Choosing the first passcode, or changing a known one -- only at
            # the computer itself. Anyone on the network could otherwise
            # choose it before the owner did, or change it with a session
            # borrowed from an unattended screen.
            if data.get('new') and not handler._is_local():
                raise api.ApiError('The passcode can only be chosen or changed '
                                   'on the computer Billing runs on.', 403)
            if data.get('new'):
                try:
                    auth.set_passcode(conn, data.get('new'),
                                      data.get('current'))
                except ValueError as e:
                    raise api.ApiError(str(e))
                handler._set_session_cookie(auth.issue(conn))
                return dict(passcode_set=True, signed_in=True)

            wait = auth.locked_for(who)
            if wait:
                raise api.ApiError(
                    'Too many wrong tries. Wait %d second(s).' % wait, 429)
            if not auth.check_passcode(conn, data.get('passcode')):
                auth.note_failure(who)
                raise api.ApiError('That passcode is not right.', 401)
            auth.clear_failures(who)
            handler._set_session_cookie(auth.issue(conn))
            return dict(signed_in=True)

        raise api.ApiError('Method not allowed.', 405)

    # These only ever read. Asked with any other method they are refused, so
    # nothing reached by a form or a script can be mistaken for a save.
    if method != 'GET' and (head in READ_ONLY or (
            head in ('invoices', 'orders') and len(rest) > 1
            and rest[1] in READ_ONLY_SUB) or (
            head == 'customers' and not rest) or (
            head in ('eway', 'einvoice') and rest[:1] != ['settings'])):
        raise api.ApiError('Method not allowed.', 405)

    # Every request works inside one company: ?company=<id>, else the
    # remembered one.
    if head == 'companies':
        if not rest:
            if method == 'GET':
                return api.list_companies(conn)
            if method == 'POST':
                return api.save_company(conn, None, body())
        else:
            cid = _int(rest[0])
            if len(rest) > 1 and rest[1] == 'activate':
                # It changes which firm documents are filed in, so a link
                # followed from anywhere must not do it.
                if method not in ('POST', 'PUT'):
                    raise api.ApiError('Method not allowed.', 405)
                return api.switch_company(conn, cid)
            if method == 'GET':
                return api.get_company(conn, cid)
            if method == 'PUT':
                return api.save_company(conn, cid, body())
            if method == 'DELETE':
                return api.delete_company(conn, cid)

    # Updating the books from SHREE. Not scoped to a firm: the update takes in
    # all three.
    if head == 'sync':
        sub = rest[0] if rest else ''
        if not sub and method == 'GET':
            return shree_sync.overview(conn)
        if sub == 'status' and method == 'GET':
            return shree_sync.MANAGER.status()
        if sub == 'run' and method == 'POST':
            data = body()
            mode = 'preview' if _flag(data.get('preview')) else 'apply'
            # Firms the user chose to take as SHREE has them, having been
            # shown what the last update held them back for.
            accept = data.get('accept') or []
            if not isinstance(accept, list):
                accept = [accept]
            # Fetch everything: the books cleared of everything made or
            # decided here, every firm read again as SHREE has it (6 Oct
            # 2026). Entries made here go with it, so it asks for the delete
            # password as any delete of a document does.
            everything = _flag(data.get('everything')) and mode == 'apply'
            if everything and shree_sync.everything_plan(conn)['password']:
                _delete_gate(handler, conn)
            return shree_sync.MANAGER.start(
                mode, 'you', [str(f) for f in accept], everything=everything)
        if sub == 'everything' and method == 'GET':
            # What Fetch everything would take away, asked before it runs.
            return shree_sync.everything_plan(conn)
        if sub == 'settings' and method in ('PUT', 'POST'):
            return shree_sync.save_settings(conn, body())
        if sub == 'rules' and rest[1:] == ['release'] and method == 'POST':
            return shree_sync.release_rules(conn, body())
        if sub == 'rules' and len(rest) > 1 and method == 'DELETE':
            return shree_sync.delete_rule(conn, _int(rest[1]))
        if sub == 'runs' and len(rest) > 1 and method == 'GET':
            return shree_sync.run_detail(conn, _int(rest[1]))
        if sub == 'find' and method == 'GET':
            return shree_sync.find_doc(conn, q.get('firm'), q.get('key'))
        # Choosing where SHREE is: the folders in one folder, and every SHREE
        # this computer can see.
        if sub in ('browse', 'discover') and not _local(handler):
            # Opened to the network with --lan, the app would otherwise list
            # this computer's drives to anyone on the wifi.
            raise api.ApiError('Choose SHREE\'s folder on the computer Billing '
                               'runs on.', 403)
        if sub == 'browse' and method == 'GET':
            return shree_sync.browse(q.get('path') or '')
        if sub == 'discover' and method == 'GET':
            st = shree_sync.settings(conn)
            found = shree_sync.discover(fresh=True, known=st.get('known'),
                                        extra=st.get('sources'))
            return dict(found=found,
                        chosen=[c['path'] for c in shree_sync.chosen(found)],
                        looked=list(shree_sync._looked))

    # ?company= names the firm outright. Without it, the firm the page is on
    # (handler._firm_asked) -- unless that firm has since been deleted, when
    # the page is moved to the one remembered.
    firm = q.get('company')
    if not firm:
        firm = handler._firm_asked()
        if firm and not conn.execute('SELECT 1 FROM company WHERE id=?',
                                     (int(firm),)).fetchone():
            firm = None
    cid = api.active_company_id(conn, firm)

    if head == 'meta':
        return dict(states=[{'code': c, 'name': n} for c, n in api.STATES],
                    units=api.UNITS,
                    # 40% is the slab GST added on 22 Sep 2025.
                    gst_rates=[0, 0.1, 0.25, 1, 1.5, 3, 5, 6, 7.5, 12, 18, 28,
                               40],
                    gst_types=api.GST_TYPES,
                    doc_types=list(api.DOC_TYPES),
                    doc_labels=api.DOC_LABEL,
                    voucher_kinds=list(api.VOUCHER_KINDS),
                    companies=api.list_companies(conn),
                    company=api.get_company(conn, cid),
                    company_id=cid,
                    lock_enabled=handler._auth_required(),
                    groups=api.list_groups(conn, cid),
                    lookups=api.voucher_lookups(conn, cid),
                    order_company=api.order_company_id(conn),
                    billing_firms=api.billing_firms(conn),
                    fys=api.list_fys(conn, cid),
                    first_fy=api.first_fy(conn))

    if head == 'company':                       # the active company
        if method == 'GET':
            return api.get_company(conn, cid)
        if method in ('PUT', 'POST'):
            return api.save_company(conn, cid, body())

    if head == 'groups':
        if not rest:
            if method == 'GET':
                return api.list_groups(conn, cid)
            if method == 'POST':
                return api.save_group(conn, cid, None, body())
        elif method == 'PUT':
            return api.save_group(conn, cid, _int(rest[0]), body())

    if head == 'counters':
        if method == 'GET':
            return api.get_counters(conn, cid, q.get('fy') or '')
        if method in ('PUT', 'POST'):
            return api.save_counters(conn, cid, body())

    if head == 'next-no':
        key = q.get('key') or 'SALE'
        # An order is filed in the order firm, so its number has to come off
        # that firm's counter -- not the counter of the firm being viewed.
        src = api.order_company_id(conn) or cid if key == 'ORDER' else cid
        # The number belongs to a run of bills as much as to a year. Asked
        # without one, the answer carries on from the last bill and says which
        # run that was, so the entry screen can fill the Sr box in too.
        return db.peek_document(conn, src, key, q.get('fy') or '',
                                q.get('series'))

    # The rates on a customer's last challan, for a new one to start from.
    if head == 'last-rates':
        return api.last_rates(conn, cid, q.get('party_id'),
                              q.get('doc_type') or 'SALE')

    # "parties" is kept as an alias so links and bookmarks keep working.
    if head in ('ledgers', 'parties'):
        if not rest:
            if method == 'GET':
                return api.list_ledgers(conn, cid, q)
            if method == 'POST':
                return api.save_ledger(conn, cid, None, body())
        else:
            lid = _int(rest[0])
            # Deleting an account reaches every firm it is written in, so
            # neither half is scoped to the firm on screen.
            if len(rest) > 1 and rest[1] == 'delete-check' and method == 'GET':
                return api.account_delete_check(conn, lid)
            # Merging two accounts reaches every firm either is written in,
            # like a delete. The body names the one kept as `ledger_id`.
            if len(rest) > 1 and rest[1] == 'merge-check' and method == 'GET':
                return api.merge_check(conn, lid, q.get('into'))
            if len(rest) > 1 and rest[1] == 'merge' and method == 'POST':
                return api.merge_accounts(conn, lid, body().get('ledger_id'))
            if method == 'GET':
                l = api.one(conn.execute(
                    'SELECT * FROM ledger WHERE id=? AND company_id=?',
                    (lid, cid)))
                if not l:
                    raise api.ApiError('Ledger not found.', 404)
                return l
            if method == 'PUT':
                return api.save_ledger(conn, cid, lid, body())
            # The same customer in another firm, for a new challan or bill
            # raised from the account.
            if len(rest) > 1 and rest[1] == 'in-firm' and method == 'POST':
                return api.party_in_firm(conn, lid, body().get('firm'))
            if len(rest) > 1 and rest[1] == 'allocate' and method in ('POST', 'PUT'):
                target = body().get('company_id')
                if not target:
                    raise api.ApiError('Choose the firm to move this party to.')
                return api.allocate_ledger(conn, lid,
                                           api.qint(target, 'firm'))
            if method == 'DELETE':
                _delete_gate(handler, conn)
                # Any account that is not a party is deleted the way asked:
                # its entries moved onto another account, or with it.
                return api.delete_account(conn, lid, _flag(q.get('cascade')),
                                          move_to=q.get('move_to'),
                                          entries=_flag(q.get('entries')))

    if head == 'allocation':
        return api.list_allocation(conn, q)

    # The old years: what each firm holds year by year, and every document
    # of the years up to one deleted, balances carried forward (5 Oct 2026).
    # `check` only counts; the delete itself asks the delete password.
    if head == 'books' and rest[:1] == ['years'] and method == 'GET':
        return api.book_years(conn, q)
    if head == 'books' and rest[:1] == ['drop-years']:
        if method != 'POST':
            raise api.ApiError('Method not allowed.', 405)
        if not body().get('check'):
            _delete_gate(handler, conn)
        return api.drop_years(conn, body())

    # Many rows deleted together, from any list that ticks them; `check`
    # says what would happen without doing it.
    if head == 'bulk-delete':
        if method != 'POST':
            raise api.ApiError('Method not allowed.', 405)
        if _bulk_guarded(body()):
            _delete_gate(handler, conn)
        return api.bulk_delete(conn, body())

    # Both billing firms' bills in one list. Not scoped to the active company
    # -- seeing them side by side is the point.
    if head == 'bills':
        return api.list_bills(conn, q)

    if head == 'customers':                 # cross-firm customer lookup
        if not rest:
            return api.search_customers(conn, q)
        if rest[0] == 'adopt' and method == 'POST':
            src = body().get('ledger_id')
            if not src:
                raise api.ApiError('Pick a customer first.')
            return api.adopt_customer(conn, cid, api.qint(src, 'customer'))

    if head == 'order-firm':
        if method == 'GET':
            return {'company_id': api.order_company_id(conn)}
        if method in ('POST', 'PUT'):
            target = body().get('company_id')
            if not target:
                raise api.ApiError('Choose the firm orders are taken in.')
            target = api.qint(target, 'firm')
            api.get_company(conn, target)
            db.set_meta(conn, api.ORDER_FIRM_KEY, str(target))
            conn.commit()
            return {'company_id': target}

    if head == 'billing-firm' and method in ('POST', 'PUT'):
        b = body()
        if not b.get('ledger_id') or not b.get('company_id'):
            raise api.ApiError('Choose a customer and a billing firm.')
        return api.set_billing_firm(conn, api.qint(b['ledger_id'], 'customer'),
                                    api.qint(b['company_id'], 'firm'))

    if head == 'items':
        if not rest:
            if method == 'GET':
                return api.list_items(conn, cid, q)
            if method == 'POST':
                return api.save_item(conn, cid, None, body())
        else:
            iid = _int(rest[0])
            if len(rest) > 1 and rest[1] == 'delete-check' and method == 'GET':
                return api.item_delete_check(conn, cid, iid)
            if method == 'GET':
                it = api.one(conn.execute(
                    'SELECT * FROM item WHERE id=? AND company_id=?',
                    (iid, cid)))
                if not it:
                    raise api.ApiError('Item not found.', 404)
                return it
            if method == 'PUT':
                return api.save_item(conn, cid, iid, body())
            if method == 'DELETE':
                # An item on bills: its lines onto another item, or kept
                # without one -- asked on the screen.
                return api.delete_item(conn, cid, iid, q.get('move_to'),
                                       _flag(q.get('unlink')))

    if head == 'invoices':
        if not rest:
            if method == 'GET':
                return api.list_invoices(conn, cid, q)
            if method == 'POST':
                return api.save_invoice(conn, cid, None, body(),
                                        who=_who(handler))
        else:
            iid = _int(rest[0])
            if len(rest) > 1 and rest[1] == 'gst-bill-draft':
                return api.challan_to_bill(conn, cid, iid, q.get('firm'))
            if len(rest) > 1 and rest[1] == 'eway':
                # The bill as the portal wants it, with everything it would
                # have refused said first.
                return eway.preview(conn, cid, iid,
                                    q.get('schema') or 'bulk', q.get('docno'))
            if len(rest) > 1 and rest[1] == 'eway.json':
                text, name = eway.bundle(conn, '%d:%d' % (cid, iid),
                                         q.get('schema') or 'bulk',
                                         _flag(q.get('force')), q.get('docno'))
                return text.encode('utf-8'), name, 'application/json'
            if len(rest) > 1 and rest[1] == 'einvoice':
                # The same bill as the e-invoice portal wants it. `ewb` forces
                # the e-way half on or off; left out, the bill's own transport
                # decides.
                return einvoice.preview(conn, cid, iid, q.get('docno'),
                                        _tristate(q.get('ewb')))
            if len(rest) > 1 and rest[1] == 'einvoice.json':
                text, name = einvoice.bundle(conn, '%d:%d' % (cid, iid),
                                             _flag(q.get('force')),
                                             q.get('docno'))
                return text.encode('utf-8'), name, 'application/json'
            if len(rest) > 1 and rest[1] == 'transport' and method in ('PUT', 'POST'):
                return api.save_transport(conn, cid, iid, body(),
                                          who=_who(handler))
            if len(rest) > 1 and rest[1] == 'pdf':
                # The real stationery, rendered from bst-pdf-generator.
                blob, name = pdf_print.render(conn, cid, iid, q.get('format'),
                                              api.qint(q.get('copies'),
                                                       'copy count', 2))
                return blob, name, 'application/pdf'
            if len(rest) > 1 and rest[1] == 'history':
                return api.invoice_history(conn, cid, iid)
            if method == 'GET':
                return api.get_invoice(conn, cid, iid)
            if method == 'PUT':
                return api.save_invoice(conn, cid, iid, body(),
                                        who=_who(handler))
            if method == 'DELETE':
                _delete_gate(handler, conn)
                return api.delete_invoice(conn, cid, iid,
                                          _flag(q.get('cascade')),
                                          _flag(q.get('keep')))

    if head == 'orders':
        # Orders live in one firm only -- the one named under Settings ->
        # "Orders are taken in". Whichever firm is on screen, the order book
        # is the same one, so every order route works against that firm
        # rather than the active one.
        cid = api.order_company_id(conn) or cid
        if not rest:
            if method == 'GET':
                return api.list_orders(conn, cid, q)
            if method == 'POST':
                return api.save_order(conn, cid, None, body())
        else:
            oid = _int(rest[0])
            if len(rest) > 1 and rest[1] == 'challan-draft':
                return api.order_to_challan(conn, cid, oid)
            if len(rest) > 1 and rest[1] == 'invoice-draft':
                return api.order_to_invoice(conn, cid, oid, q.get('firm'))
            if len(rest) > 1 and rest[1] == 'status' and method in ('PUT', 'POST'):
                st = (body().get('status') or '').strip()
                if st not in ('open', 'invoiced', 'cancelled'):
                    raise api.ApiError('Invalid order status.')
                conn.execute('UPDATE sales_order SET status=? WHERE id=? AND '
                             'company_id=?', (st, oid, cid))
                conn.commit()
                return api.get_order(conn, cid, oid)
            if method == 'GET':
                return api.get_order(conn, cid, oid)
            if method == 'PUT':
                return api.save_order(conn, cid, oid, body())
            if method == 'DELETE':
                # An order alone is no document; taking its challan and
                # bill along is.
                if _flag(q.get('cascade')):
                    _delete_gate(handler, conn)
                return api.delete_order(conn, cid, oid,
                                        _flag(q.get('cascade')),
                                        _flag(q.get('keep')))

    # The money accounts most lately written to -- where a new receipt or
    # payment starts.
    if head == 'money-recent' and method == 'GET':
        return api.recent_money(conn, cid)

    if head == 'vouchers':
        if not rest:
            if method == 'GET':
                return api.list_vouchers(conn, cid, q)
            if method == 'POST':
                return api.save_voucher(conn, cid, None, body())
        else:
            vid = _int(rest[0])
            if method == 'GET':
                return api.get_voucher(conn, cid, vid)
            if method == 'PUT':
                return api.save_voucher(conn, cid, vid, body())
            if method == 'DELETE':
                return api.delete_voucher(conn, cid, vid,
                                          _flag(q.get('keep')))

    # Stock added, removed or counted by hand in the billing firms (27 Sep
    # 2026). The firm is named in the request, never the one on screen.
    if head == 'stock':
        part = rest[0] if rest else ''
        if part == 'items' and method == 'GET':
            return api.stock_items(conn, q)
        if part == 'moves' and method == 'GET':
            return api.stock_moves(conn, q)
        # Its categories (4 Oct 2026): the list, and a line moved into one.
        if part == 'cats' and len(rest) == 1:
            if method == 'GET':
                return api.stock_cat_list(conn, q)
            if method == 'PUT':
                return api.save_stock_cats(conn, body())
        if part == 'cat' and len(rest) == 1 and method == 'POST':
            return api.set_stock_cat(conn, body())
        if part == 'entries' and len(rest) == 1:
            if method == 'GET':
                return api.list_stock_entries(conn, q)
            if method == 'POST':
                return api.save_stock_entry(conn, None, body())
        elif part == 'entries' and len(rest) == 2:
            eid = _int(rest[1])
            if method == 'GET':
                return api.get_stock_entry(conn, eid)
            if method == 'PUT':
                return api.save_stock_entry(conn, eid, body())
            if method == 'DELETE':
                return api.delete_stock_entry(conn, eid)
        raise api.ApiError('Method not allowed.', 405)

    # The people paid a salary, and their accounts in this firm's books.
    # The steel in the challan firm's yard -- Challan Stock (26 Sep 2026).
    if head == 'steel':
        part = rest[0] if rest else ''
        if part == 'lists':
            if method == 'GET':
                return api.steel_lists(conn)
            if method == 'PUT':
                return api.save_steel_lists(conn, body())
        elif part == 'stock' and method == 'GET':
            return api.steel_stock(conn, cid, q)
        elif part == 'item' and method == 'GET':
            return api.steel_item(conn, cid, q)
        elif part == 'entries' and len(rest) == 1:
            if method == 'GET':
                return api.list_steel_entries(conn, cid, q)
            if method == 'POST':
                return api.save_steel_entry(conn, cid, None, body())
        elif part == 'entries':
            eid = _int(rest[1])
            if method == 'GET':
                return api.get_steel_entry(conn, cid, eid)
            if method == 'PUT':
                return api.save_steel_entry(conn, cid, eid, body())
            if method == 'DELETE':
                return api.delete_steel_entry(conn, cid, eid)
        raise api.ApiError('Method not allowed.', 405)

    if head == 'staff':
        if not rest:
            if method == 'GET':
                return api.report_staff(conn, cid, q)
            if method == 'POST':
                return api.add_staff(conn, cid, body())
        elif rest[0] == 'salary':
            if method == 'POST':
                return api.post_salary(conn, cid, body())
        elif rest[0] == 'accounts':
            if method == 'GET':
                return api.staff_candidates(conn, cid)
        else:
            lid = _int(rest[0])
            if rest[1:] == ['loose']:
                if method == 'POST':
                    return api.loose_money(conn, cid, lid, body())
                raise api.ApiError('Method not allowed.', 405)
            if method == 'GET':
                return api.staff_detail(conn, cid, lid, q)
            if method == 'PUT':
                return api.save_staff(conn, cid, lid, body())
            if method == 'DELETE':
                return api.unlist_staff(conn, cid, lid)

    # Personal Accounts: the user's own money given to and received from
    # people, apart from every firm's books (30 Sep 2026).
    if head == 'personal':
        part = rest[0] if rest else ''
        if not rest:
            if method == 'GET':
                return api.list_personal(conn, q)
            if method == 'POST':
                return api.save_personal_person(conn, None, body())
        elif part == 'people' and len(rest) == 1:
            if method == 'GET':
                return api.personal_people(conn)
        # An account in the business books brought across (30 Sep 2026):
        # the accounts to pick from, what it would bring, and doing it.
        elif part == 'accounts' and len(rest) == 1:
            if method == 'GET':
                return api.personal_accounts(conn, q)
        elif part == 'import' and len(rest) == 1:
            if method == 'GET':
                return api.personal_import_preview(conn, q)
            if method == 'POST':
                return api.import_personal(conn, body())
        elif part == 'entries' and len(rest) == 1:
            if method == 'POST':
                return api.save_personal_entry(conn, None, body())
        elif part == 'entries' and len(rest) == 2:
            eid = _int(rest[1])
            if method == 'GET':
                return api.get_personal_entry(conn, eid)
            if method == 'PUT':
                return api.save_personal_entry(conn, eid, body())
            if method == 'DELETE':
                return api.delete_personal_entry(conn, eid)
        elif len(rest) == 1:
            pid = _int(rest[0])
            if method == 'GET':
                return api.get_personal(conn, pid, q)
            if method == 'PUT':
                return api.save_personal_person(conn, pid, body())
            if method == 'DELETE':
                return api.delete_personal_person(conn, pid)
        raise api.ApiError('Method not allowed.', 405)

    if head == 'reports' and rest:
        # One account between two dates, as the PDF sent to the customer.
        if rest[0] == 'statement':
            blob, name = pdf_print.statement(conn, cid, q)
            return blob, name, 'application/pdf'
        table = {'dashboard': api.report_dashboard, 'ledger': api.report_ledger,
                 'outstanding': api.report_outstanding, 'stock': api.report_stock,
                 'customers': api.report_customers, 'due': api.report_due,
                 'receivables': api.report_ageing,
                 'register': api.report_register, 'gst': api.report_gst,
                 'daybook': api.report_daybook,
                 'health': api.report_health,
                 'bank': api.report_bank,
                 'trial-balance': api.report_trial_balance,
                 'pnl': api.report_pnl,
                 'balance-sheet': api.report_balance_sheet}
        fn = table.get(rest[0])
        if fn:
            return fn(conn, cid, q)

    if head == 'eway':
        schema, style = q.get('schema') or 'bulk', q.get('docno')
        if rest and rest[0] == 'json':
            text, name = eway.bundle(conn, q.get('ids'), schema,
                                     _flag(q.get('force')), style)
            return text.encode('utf-8'), name, 'application/json'
        if rest and rest[0] == 'settings':
            if method in ('PUT', 'POST'):
                return api.save_eway_settings(conn, body())
            return api.eway_settings(conn)
        if q.get('ids'):
            return eway.preview_many(conn, q.get('ids'), schema, style)
        return api.eway_candidates(conn, q)

    if head == 'einvoice':
        style = q.get('docno')
        if rest and rest[0] == 'json':
            text, name = einvoice.bundle(conn, q.get('ids'),
                                         _flag(q.get('force')), style)
            return text.encode('utf-8'), name, 'application/json'
        if q.get('ids'):
            return einvoice.preview_many(conn, q.get('ids'), style)
        return api.einvoice_candidates(conn, q)

    if head == 'export' and rest:
        return api.export_csv(conn, cid, rest[0], q)

    # Everything at once -- the books and the program, one .zip to keep
    # (backup.py) -- and when that was last done, for Settings.
    if head == 'backup':
        if rest == ['last']:
            return backup.last()
        if not rest:
            blob, name = backup.make(conn)
            return blob, name, 'application/zip'

    # GSTR-1 for one GST firm and month: what it holds, or the workbook the
    # portal's offline tool imports.
    if head == 'gstr1':
        if rest[:1] == ['xlsx']:
            return gstr1.excel(conn, cid, q)
        return gstr1.preview(conn, cid, q)

    raise api.ApiError('No route for %s /api/%s' % (method, path), 404)


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)
        socketserver.ThreadingTCPServer.__init__(self, *a, **kw)

    def process_request(self, request, client_address):
        # Past MAX_CLIENTS a connection is closed at once rather than given a
        # thread of its own.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            socketserver.ThreadingTCPServer.process_request(
                self, request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            socketserver.ThreadingTCPServer.process_request_thread(
                self, request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # A browser that left mid-answer -- the bundle cut off by a reload, an
        # error page with nobody left to read it -- is not written up as a
        # fault (see CLIENT_GONE). Anything else is, as before.
        if isinstance(sys.exc_info()[1], CLIENT_GONE):
            return
        socketserver.ThreadingTCPServer.handle_error(self, request,
                                                     client_address)
    # SO_REUSEADDR means two different things. On Unix it lets us rebind a port
    # still in TIME_WAIT, which is what we want. On Windows it lets a second
    # process bind a port that is genuinely in use -- so a stray second copy
    # would start silently and two servers would write the same SQLite file.
    # Left off there, a second copy's bind fails loudly. (SO_EXCLUSIVEADDRUSE
    # is not asked for: socketserver never read the attribute this used to
    # set, and with it a restart could not bind while the last run's
    # connections sat in TIME_WAIT.)
    allow_reuse_address = os.name != 'nt'


def main():
    ap = argparse.ArgumentParser(description='Billing & Order Management')
    ap.add_argument('--port', type=int, default=8800)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args()

    # On Windows the log this writes to is in the machine's code page, and a
    # character it cannot hold -- a folder under a Gujarati user name, in the
    # database path printed below -- stopped the server before it started.
    # Such a character goes into the log as '?' instead; errors already do.
    try:
        sys.stdout.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass
    db.quiet_windows_errors()
    db.init()
    shree_sync.MANAGER.load()
    try:
        srv = Server((args.host, args.port), Handler)
    except OSError as e:
        sys.stderr.write(
            '\n  Cannot listen on %s:%d -- %s\n'
            '  Billing may already be running. Open http://127.0.0.1:%d/ ,\n'
            '  or stop the other copy first.\n\n'
            % (args.host, args.port, e, args.port))
        return 1
    # 0.0.0.0 is not an address a browser can visit.
    shown = '127.0.0.1' if args.host in ('', '0.0.0.0', '::') else args.host
    url = 'http://%s:%d/' % (shown, args.port)
    print('\n  Billing & Order Management')
    print('  ---------------------------')
    print('  Running at   %s' % url)
    if shown != args.host:
        print('  On this network  http://%s:%d/  (no login -- trusted wifi only)'
              % (_lan_ip() or 'this-machine', args.port))
    print('  Database     %s' % db.DB_PATH)
    print('  Stop with    Ctrl+C\n')
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    # Nothing is fetched from SHREE by itself: an update runs only when it
    # is asked for, from the SHREE update screen (24 Sep 2026).
    threading.Thread(target=_upkeep_loop, name='upkeep', daemon=True).start()
    # The printing library and its fonts, loaded before the first print.
    threading.Thread(target=pdf_print.warm, name='pdf-warm',
                     daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n  Stopped.')
    finally:
        srv.server_close()
    return 0


UPKEEP_SECONDS = 6 * 3600


def _upkeep_loop():
    while True:
        time.sleep(UPKEEP_SECONDS)
        try:
            db.upkeep()
        except Exception:                          # noqa: BLE001
            traceback.print_exc()


def _lan_ip():
    """The address other machines on this network would use. Best effort."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))       # no packets are sent by a UDP connect
        return s.getsockname()[0]
    except OSError:
        return ''
    finally:
        s.close()


if __name__ == '__main__':
    sys.exit(main() or 0)
