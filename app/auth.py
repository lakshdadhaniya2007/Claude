"""Passcode and sessions.

The books are protected by the app itself rather than by whoever happens to
own the machine: one passcode, kept only as a PBKDF2 hash, and a signed cookie
that says the browser has already given it.

Everything lives in the `meta` table, so there is no schema to migrate and a
database copied to another machine carries its own lock with it.

Named users would sit on top of this without rework: the session payload is a
dict, so a `user` key can join `exp` later, and `check_passcode` becomes a
lookup rather than a single comparison.
"""
import base64
import hashlib
import hmac
import json
import os
import time

import db

# ---------------------------------------------------------------- storage

K_HASH = 'auth_hash'          # PBKDF2 of the passcode, hex
K_SALT = 'auth_salt'          # per-install salt, hex
K_ITER = 'auth_iter'          # iteration count, so it can be raised later
K_SECRET = 'auth_secret'      # HMAC key for session cookies, hex
K_EPOCH = 'auth_epoch'        # bumped to end every session at once

# ---------------------------------------------------------------- switch
#
# The lock is off. Nothing below is deleted -- the passcode, the sessions and
# the lock screen are all still here and still tested; they simply are not
# asked for. Turn it back on by setting this to True and restarting: the first
# screen then asks for a passcode to be chosen, and every route is refused
# until one is given. The frontend follows this automatically, through
# `lock_enabled` in /api/meta.
ENABLED = False

ITERATIONS = 240_000          # ~0.15s on this class of machine
MIN_LENGTH = 6
SESSION_HOURS = 12
COOKIE = 'billing_session'


def _derive(passcode, salt_hex, iterations):
    return hashlib.pbkdf2_hmac(
        'sha256', passcode.encode('utf-8'),
        bytes.fromhex(salt_hex), iterations).hex()


def is_set(conn):
    """True once a passcode has been chosen."""
    return bool(db.get_meta(conn, K_HASH))


def set_passcode(conn, passcode, current=None, force=False):
    """Choose or change the passcode. Returns nothing; raises ValueError.

    Changing one requires the current passcode, so a session left open on an
    unattended machine cannot be used to lock the owner out.
    """
    passcode = (passcode or '').strip()
    if len(passcode) < MIN_LENGTH:
        raise ValueError('The passcode must be at least %d characters.'
                         % MIN_LENGTH)
    # `force` is for the owner at the computer itself (billing.py
    # --set-passcode), who can read the books file anyway.
    if is_set(conn) and not force and not check_passcode(conn, current or ''):
        raise ValueError('The current passcode is not right.')

    salt = os.urandom(16).hex()
    db.set_meta(conn, K_SALT, salt)
    db.set_meta(conn, K_ITER, str(ITERATIONS))
    db.set_meta(conn, K_HASH, _derive(passcode, salt, ITERATIONS))
    # Changing the passcode ends every session that was open under the old
    # one -- a new signing key makes the cookies that carry them unverifiable.
    db.set_meta(conn, K_SECRET, os.urandom(32).hex())
    conn.commit()


def check_passcode(conn, passcode):
    stored = db.get_meta(conn, K_HASH)
    salt = db.get_meta(conn, K_SALT)
    if not stored or not salt:
        return False
    try:
        iterations = int(db.get_meta(conn, K_ITER) or ITERATIONS)
    except ValueError:
        iterations = ITERATIONS
    got = _derive(passcode or '', salt, iterations)
    # Constant time: a plain == leaks how much of the hash matched.
    return hmac.compare_digest(got, stored)


# ---------------------------------------------------------------- sessions

def epoch(conn):
    """Which generation of sessions is currently accepted."""
    try:
        return int(db.get_meta(conn, K_EPOCH) or 1)
    except ValueError:
        return 1


def end_all_sessions(conn):
    """Sign out everywhere.

    Clearing the browser's cookie is not enough on its own -- a copy of it
    kept anywhere would still open the books until it expired. Moving the
    generation on makes every token issued before this moment fail its check.
    """
    db.set_meta(conn, K_EPOCH, str(epoch(conn) + 1))
    conn.commit()


def _secret(conn):
    key = db.get_meta(conn, K_SECRET)
    if not key:
        key = os.urandom(32).hex()
        db.set_meta(conn, K_SECRET, key)
        conn.commit()
    return bytes.fromhex(key)


def _sign(secret, payload):
    return hmac.new(secret, payload, hashlib.sha256).digest()


def issue(conn, hours=SESSION_HOURS):
    """A signed cookie value saying this browser has given the passcode."""
    body = json.dumps({'exp': int(time.time() + hours * 3600),
                       'gen': epoch(conn)},
                      separators=(',', ':')).encode('utf-8')
    raw = base64.urlsafe_b64encode(body).rstrip(b'=')
    sig = base64.urlsafe_b64encode(_sign(_secret(conn), raw)).rstrip(b'=')
    return (raw + b'.' + sig).decode('ascii')


def valid(conn, token):
    """True when the cookie is one we signed and has not run out."""
    if not token or '.' not in token:
        return False
    raw, _, sig = token.encode('ascii', 'ignore').partition(b'.')
    want = base64.urlsafe_b64encode(_sign(_secret(conn), raw)).rstrip(b'=')
    if not hmac.compare_digest(sig, want):
        return False
    try:
        pad = b'=' * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(raw + pad))
        if int(data.get('gen', 0)) != epoch(conn):
            return False                 # signed out, or the passcode changed
        return int(data.get('exp', 0)) > time.time()
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------ brute force

# Failures per client address. In memory on purpose: a restart clearing them
# is not a weakness when the passcode itself is unchanged, and it keeps the
# books free of anything that is not the books.
_failures = {}
LOCK_AFTER = 5
LOCK_SECONDS = 60


FAILURES_KEPT = 1000


def note_failure(addr):
    hits, _ = _failures.get(addr, (0, 0.0))
    _failures[addr] = (hits + 1, time.time())
    # Many addresses guessing at once must not grow this without end; the
    # longest a wait lasts is fifteen minutes, so older entries mean nothing.
    if len(_failures) > FAILURES_KEPT:
        cut = time.time() - 900
        for k in [k for k, v in _failures.items() if v[1] < cut]:
            _failures.pop(k, None)


def clear_failures(addr):
    _failures.pop(addr, None)


def locked_for(addr):
    """Seconds this address must wait, or 0. Guessing is slowed, not the
    owner's own typo -- five tries are free before any wait at all."""
    hits, when = _failures.get(addr, (0, 0.0))
    if hits < LOCK_AFTER:
        return 0
    over = hits - LOCK_AFTER + 1
    wait = min(LOCK_SECONDS * over, 900)
    left = int(when + wait - time.time())
    return left if left > 0 else 0


# ---------------------------------------------------------- delete password
#
# Asked on 4 Oct 2026: "To delete a challan or bill or an account the user
# has to enter a password and then only they can delete it." A password of
# its own -- not the passcode that opens the books, which the people who
# make the entries are given -- kept the same way, as a PBKDF2 hash in
# `meta`. Every delete of a document or an account sends it and the server
# checks it (server._delete_gate), so no screen can delete around it.

K_DEL_HASH = 'delete_pw_hash'
K_DEL_SALT = 'delete_pw_salt'
K_DEL_ITER = 'delete_pw_iter'
DELETE_MIN = 4


def delete_password_set(conn):
    return bool(db.get_meta(conn, K_DEL_HASH))


def check_delete_password(conn, password):
    stored = db.get_meta(conn, K_DEL_HASH)
    salt = db.get_meta(conn, K_DEL_SALT)
    if not stored or not salt:
        return False
    try:
        iterations = int(db.get_meta(conn, K_DEL_ITER) or ITERATIONS)
    except ValueError:
        iterations = ITERATIONS
    return hmac.compare_digest(_derive(password or '', salt, iterations), stored)


def set_delete_password(conn, password, current=None, force=False):
    """Choose or change the delete password; raises ValueError. Changing it
    takes the one it replaces -- or `force`, for the owner at the computer
    itself (billing.py --set-delete-password), who can read the books file
    anyway."""
    password = (password or '').strip()
    if len(password) < DELETE_MIN:
        raise ValueError('The delete password must be at least %d characters.'
                         % DELETE_MIN)
    if delete_password_set(conn) and not force \
            and not check_delete_password(conn, current or ''):
        raise ValueError('The current delete password is not right.')
    salt = os.urandom(16).hex()
    db.set_meta(conn, K_DEL_SALT, salt)
    db.set_meta(conn, K_DEL_ITER, str(ITERATIONS))
    db.set_meta(conn, K_DEL_HASH, _derive(password, salt, ITERATIONS))
    conn.commit()
