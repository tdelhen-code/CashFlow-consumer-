"""Login: passwords, one-time setup codes and sessions.

- Users log in with their phone number (any usual form) and a password.
- Nobody types a password for someone else. An admin issues a one-time setup
  code (given in person or by WhatsApp); the worker uses it to choose their own
  password. The same is used when a password is forgotten.
- Passwords are stored with scrypt; setup codes and session tokens are stored
  only as hashes, so a copy of the database can't be used to log in.
- 5 wrong tries in a row lock the account for 15 minutes. The same message is
  given for an unknown phone and a wrong password.
- A blocked or deactivated user can't log in, and their open sessions stop
  working at once. Changing or resetting a password ends the other sessions.
"""
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from . import ledger as L
from . import orders as O

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
SETUP_CODE_HOURS = 72
SETUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
SETUP_CODE_LENGTH = 8
SESSION_HOURS = {"customer": 24 * 7, "admin": 8}
ADMIN_IDLE_MINUTES = 30
SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class LoginFailed(L.LedgerError):
    """Wrong phone, password or code; deliberately says no more than that."""


class AccountLocked(L.LedgerError):
    pass


class NotLoggedIn(L.LedgerError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  ended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS setup_codes (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  code_hash TEXT NOT NULL,
  issued_by INTEGER NOT NULL REFERENCES users(id),
  expires_at TEXT NOT NULL,
  used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_setup_codes_user ON setup_codes(user_id);

CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  at TEXT NOT NULL,
  ok INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(user_id);
"""


def connect(path=":memory:"):
    conn = O.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _now(now):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


# ---------- passwords ----------

def _check_password_rules(password, phone=None):
    if not isinstance(password, str):
        raise L.LedgerError("password must be text")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise L.LedgerError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise L.LedgerError("password is too long")
    if len(set(password)) < 3:
        raise L.LedgerError("password is too simple")
    if phone and password.lstrip("+0") in phone:
        raise L.LedgerError("password can't be your phone number")


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        **SCRYPT, salt=base64.b64encode(salt).decode(),
        digest=base64.b64encode(digest).decode())


def verify_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(actual, expected)


# Checked against when the phone is unknown, so both cases take the same time.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def create_first_admin(conn, name, phone, password, language="ar"):
    """Set-up step: the very first admin, with their own password."""
    _check_password_rules(password, L.normalize_phone(phone))
    user_id = L.create_user(conn, "admin", name, phone, language)
    with L._write(conn):
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (hash_password(password), user_id))
    return user_id


# ---------- lockout ----------

def _locked_until(conn, user_id, now):
    """When the account unlocks, or None. Counts failures since the last
    successful login within the lock window."""
    since = (now - timedelta(minutes=LOCK_MINUTES)).isoformat()
    rows = conn.execute(
        "SELECT at, ok FROM login_attempts WHERE user_id = ? AND at > ? "
        "ORDER BY id DESC", (user_id, since)).fetchall()
    failures = []
    for row in rows:
        if row["ok"]:
            break
        failures.append(row["at"])
    if len(failures) < MAX_FAILED_LOGINS:
        return None
    return datetime.fromisoformat(failures[0]) + timedelta(minutes=LOCK_MINUTES)


def _record_attempt(conn, user_id, ok, now):
    conn.execute("INSERT INTO login_attempts(user_id, at, ok) VALUES (?,?,?)",
                 (user_id, now.isoformat(), 1 if ok else 0))


def _user_for_login(conn, phone, now):
    """The user behind a phone, or None. Raises AccountLocked if locked."""
    try:
        phone = L.normalize_phone(phone)
    except L.LedgerError:
        return None
    user = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
    if user is None:
        return None
    _check_not_locked(conn, user["id"], now)
    return user


def _check_not_locked(conn, user_id, now):
    until = _locked_until(conn, user_id, now)
    if until and until > now:
        raise AccountLocked(f"too many wrong tries; try again after {until:%H:%M} UTC")


# ---------- setup codes ----------

def issue_setup_code(conn, user_id, *, admin_id, now=None):
    """One-time code a worker uses to choose their password, first time or after
    forgetting it. Returned only here; give it to them in person or by WhatsApp.
    Earlier codes stop working, the old password stops working and open
    sessions end."""
    now = _now(now)
    code = "".join(secrets.choice(SETUP_CODE_ALPHABET) for _ in range(SETUP_CODE_LENGTH))
    with L._write(conn):
        L._require_admin(conn, admin_id)
        user = L._user(conn, user_id)
        if not user["active"]:
            raise L.LedgerError("user is not active")
        conn.execute("UPDATE setup_codes SET used_at = ? WHERE user_id = ? "
                     "AND used_at IS NULL", (now.isoformat(), user_id))
        conn.execute(
            "INSERT INTO setup_codes(user_id, code_hash, issued_by, expires_at) "
            "VALUES (?,?,?,?)", (user_id, _sha256(code), admin_id,
                                 (now + timedelta(hours=SETUP_CODE_HOURS)).isoformat()))
        conn.execute("UPDATE users SET password_hash = NULL WHERE id = ?", (user_id,))
        _end_sessions(conn, user_id, now)
        L._log(conn, admin_id, "issue_setup_code", user_id)
    return code


def set_password_with_code(conn, phone, code, new_password, *, now=None):
    """Worker chooses their password with the setup code. Logs them in:
    returns a session token."""
    now = _now(now)
    code_hash = _sha256((code or "").strip().upper())
    user = _user_for_login(conn, phone, now)
    if user is None:
        raise LoginFailed("wrong phone number or code")
    valid = conn.execute(
        "SELECT 1 FROM setup_codes WHERE user_id = ? AND code_hash = ? "
        "AND used_at IS NULL AND expires_at > ?",
        (user["id"], code_hash, now.isoformat())).fetchone()
    if valid:
        _check_can_use(user)
        _check_password_rules(new_password, user["phone"])
        new_hash = hash_password(new_password)      # slow: kept out of the lock
    with L._write(conn):
        _check_not_locked(conn, user["id"], now)
        used = valid and conn.execute(
            "UPDATE setup_codes SET used_at = ? WHERE user_id = ? AND code_hash = ? "
            "AND used_at IS NULL AND expires_at > ?",
            (now.isoformat(), user["id"], code_hash, now.isoformat())).rowcount == 1
        _record_attempt(conn, user["id"], used, now)
        if used:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (new_hash, user["id"]))
            return _new_session(conn, user, now)
    raise LoginFailed("wrong phone number or code")    # failed try already committed


# ---------- sessions ----------

def _check_can_use(user):
    if not user["active"]:
        raise LoginFailed("this account is closed")
    if user["blocked"]:
        raise L.AccountBlocked("this account is blocked; please contact the office")


def _new_session(conn, user, now):
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen) "
        "VALUES (?,?,?,?,?)",
        (_sha256(token), user["id"], now.isoformat(),
         (now + timedelta(hours=SESSION_HOURS[user["role"]])).isoformat(), now.isoformat()))
    return token


def _end_sessions(conn, user_id, now, keep=None):
    conn.execute("UPDATE sessions SET ended_at = ? WHERE user_id = ? AND ended_at IS NULL "
                 "AND id IS NOT ?", (now.isoformat(), user_id, keep))


def login(conn, phone, password, *, now=None):
    """Returns a session token. Same error for unknown phone and wrong password."""
    now = _now(now)
    user = _user_for_login(conn, phone, now)
    stored = user["password_hash"] if user else None
    ok = verify_password(password or "", stored or _DUMMY_HASH) and stored is not None
    if user is None:
        raise LoginFailed("wrong phone number or password")
    with L._write(conn):        # the password check above is slow: kept out of the lock
        _check_not_locked(conn, user["id"], now)
        current = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        ok = ok and current["password_hash"] == stored     # not reset meanwhile
        _record_attempt(conn, user["id"], ok, now)
        if ok:
            _check_can_use(current)
            return _new_session(conn, current, now)
    raise LoginFailed("wrong phone number or password")   # failed try already committed


def authenticate(conn, token, *, now=None):
    """The logged-in user for a session token, as a dict. Call on every request.
    Raises NotLoggedIn if the session is unknown, ended or expired, or the
    user has been blocked or deactivated since."""
    now = _now(now)
    row = conn.execute(
        "SELECT s.id AS session_id, s.expires_at, s.last_seen, s.ended_at, u.* "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (_sha256(token or ""),)).fetchone()
    if row is None or row["ended_at"] is not None:
        raise NotLoggedIn("please log in")
    if now >= datetime.fromisoformat(row["expires_at"]):
        raise NotLoggedIn("session expired, please log in again")
    if row["role"] == "admin" and \
            now - datetime.fromisoformat(row["last_seen"]) > timedelta(minutes=ADMIN_IDLE_MINUTES):
        raise NotLoggedIn("logged out after inactivity, please log in again")
    if not row["active"] or row["blocked"]:
        raise NotLoggedIn("this account can't be used; please contact the office")
    conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?",
                 (now.isoformat(), row["session_id"]))
    return {k: row[k] for k in ("session_id", "id", "role", "name", "phone", "whatsapp",
                                "language")}


def logout(conn, token, *, now=None):
    conn.execute("UPDATE sessions SET ended_at = ? WHERE token_hash = ? AND ended_at IS NULL",
                 (_now(now).isoformat(), _sha256(token or "")))


def logout_everywhere(conn, token, *, now=None):
    """End all of this user's sessions, e.g. after losing a phone."""
    now = _now(now)
    user = authenticate(conn, token, now=now)
    with L._write(conn):
        _end_sessions(conn, user["id"], now)


def change_password(conn, token, old_password, new_password, *, now=None):
    """Change own password. Other sessions end; this one stays."""
    now = _now(now)
    user = authenticate(conn, token, now=now)
    stored = conn.execute("SELECT password_hash FROM users WHERE id = ?",
                          (user["id"],)).fetchone()[0]
    if not verify_password(old_password or "", stored or _DUMMY_HASH):
        raise LoginFailed("current password is wrong")
    _check_password_rules(new_password, user["phone"])
    new_hash = hash_password(new_password)
    with L._write(conn):
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (new_hash, user["id"]))
        _end_sessions(conn, user["id"], now, keep=user["session_id"])
