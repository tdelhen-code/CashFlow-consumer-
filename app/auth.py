"""Login: passwords, one-time setup codes and sessions.

- Users log in with their phone number (any usual form) and a password.
- Nobody types a password for someone else. An admin issues a one-time setup
  code (given in person or by WhatsApp); the worker uses it to choose their own
  password. The same is used when a password is forgotten. The admin sees the
  code, so issuing it and using it are both written to the admin log, and a
  code for an admin's account needs a reason.
- Passwords and setup codes are stored with salted scrypt, slow to guess even
  from a copy of the database; session tokens are stored only as hashes, so a
  copy of the database can't be used to log in.
- 5 wrong tries in a row (password, setup code, or the current password when
  changing it) from one client (the web: one address) lock that phone number
  for that client for 15 minutes, registered or not, so a stranger elsewhere
  can't lock its owner out. 50 wrong tries within 15 minutes from all clients
  together lock it for every client that never gave its right password, so
  guessing spread over many addresses stays slow while the owner can still log
  in from where they did before. An unknown phone, a wrong password and a
  locked account get the same answer. A right setup code still works and lifts
  every lock; so can an admin.
- A blocked or deactivated user can't log in, and their open sessions stop
  working at once. Changing or resetting a password ends the other sessions.
"""
import base64
import hashlib
import hmac
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

from . import ledger as L
from . import orders as O

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128
MAX_FAILED_LOGINS = 5            # wrong tries in a row from one client
MAX_FAILED_LOGINS_EVERYWHERE = 50   # wrong tries from all clients together
LOCK_MINUTES = 15
SETUP_CODE_HOURS = 72
SETUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
SETUP_CODE_LENGTH = 12                                    # shown as XXXX-XXXX-XXXX
SESSION_HOURS = {"customer": 24 * 7, "admin": 8}
ADMIN_IDLE_MINUTES = 30
LAST_SEEN_SECONDS = 60          # a session's last activity is written at most this often
SCRYPT = {"n": 2**15, "r": 8, "p": 3}       # one of OWASP's minimums: 32 MiB, about 0.3 s
SCRYPT_MAXMEM = 64 * 1024 * 1024
MAX_PARALLEL_HASHES = 4         # so many logins at once can't use up the memory
LOGIN_FAILED = "wrong phone number or password, or too many wrong tries: wait 15 minutes"

# Refused whatever the case: much-used passwords, runs of keys or digits
# ("qwertyuiop", "1234567890", "azertyuiop", "1q2w3e4r5t6y"...), counting
# ("12345678910"), a short piece repeated ("123123123123"), and a much-used
# word with only digits or symbols around it ("Password1!", "Qwerty123!").
COMMON_PASSWORDS = frozenset("""
1234567890 0123456789 1234554321 1234512345 1122334455 1111122222 1q2w3e4r5t
q1w2e3r4t5 1qaz2wsx3edc qwerty1234 qwerty12345 qwerty123456 password12
password123 password1234 passw0rd12 passw0rd123 iloveyou12 iloveyou123
welcome123 letmein123 admin12345 admin123456 administrator abc1234567
abcd123456 a123456789 123456789a qwer123456 princess12 football12 sunshine12
monkey1234 dragon1234 changeme12 libya12345 libya123456 tripoli123 benghazi123
""".split())
KEY_RUNS = ("01234567890123456789", "98765432109876543210",
            "abcdefghijklmnopqrstuvwxyz", "zyxwvutsrqponmlkjihgfedcba",
            "qwertyuiopasdfghjklzxcvbnm", "azertyuiopqsdfghjklmwxcvbn",
            "1q2w3e4r5t6y7u8i9o0p", "q1w2e3r4t5y6u7i8o9p0", "!@#$%^&*()_+",
            "1qaz2wsx3edc4rfv5tgb6yhn7ujm8ik9ol0p", "qazwsxedcrfvtgbyhnujmikolp")
COUNTING = ("".join(map(str, range(100))), "".join(map(str, range(99, -1, -1))))
COMMON_WORDS = frozenset("""
password passwort passw0rd p@ssword p@ssw0rd pa55word qwerty azerty iloveyou
welcome letmein admin administrator princess football sunshine monkey dragon
changeme master secret superman batman libya tripoli benghazi
""".split())


class LoginFailed(L.LedgerError):
    """Wrong phone, password or code; deliberately says no more than that."""


class WrongPassword(LoginFailed):
    """The current password given to change it is wrong. The session itself is
    fine, so this is not 'log in again'."""


class AccountLocked(L.LedgerError):
    """Too many wrong tries; only told to someone already logged in."""


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
  code_hash TEXT NOT NULL,            -- scrypt, like passwords
  issued_by INTEGER NOT NULL REFERENCES users(id),
  expires_at TEXT NOT NULL,
  used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_setup_codes_user ON setup_codes(user_id);

CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY,
  phone TEXT NOT NULL,                -- as stored for users, registered or not
  at TEXT NOT NULL,
  ok INTEGER NOT NULL,
  client TEXT                         -- who tried (the web: their address); an ok
                                      -- row without one lifts every lock
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_phone ON login_attempts(phone);
CREATE INDEX IF NOT EXISTS idx_login_attempts_known ON login_attempts(phone, client)
  WHERE ok = 1;
"""


def connect(path=":memory:"):
    conn = O.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _now(now):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _login_phone(phone):
    """The phone as stored for users, or None if it isn't a Libyan mobile."""
    try:
        return L.normalize_phone(phone)
    except L.LedgerError:
        return None


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
    if _too_common(password.lower()):
        raise L.LedgerError("password is too common; choose another")
    if phone and (phone[-9:] in re.sub(r"\D", "", password)
                  or password.lstrip("+0") in phone):
        raise L.LedgerError("password can't contain your phone number")


def _too_common(lower):
    """True for a much-used password (see COMMON_PASSWORDS), also with symbols
    around it."""
    word = re.sub(r"^[\d\W_]+|[\d\W_]+$", "", lower)     # without digits or symbols around
    if word in COMMON_WORDS or (len(word) >= 4 and any(word in run for run in KEY_RUNS)):
        return True
    for text in {lower, re.sub(r"^[\W_]+|[\W_]+$", "", lower)}:
        if text and (text in COMMON_PASSWORDS or any(text in run for run in KEY_RUNS)
                     or (text.isdigit() and any(text in run for run in COUNTING))
                     or re.fullmatch(r"(.{1,4})\1{2,}.{0,2}", text)):
            return True
    return False


_hashing = threading.BoundedSemaphore(MAX_PARALLEL_HASHES)


def _scrypt(secret, salt, n, r, p):
    with _hashing:      # each one takes 32 MiB: only a few at once
        return hashlib.scrypt(secret.encode(), salt=salt, n=n, r=r, p=p,
                              maxmem=SCRYPT_MAXMEM)


def hash_password(password):
    """scrypt$n$r$p$salt$digest: the cost is kept with the hash, so it can be
    raised later and older hashes still check (and are redone at login)."""
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, **SCRYPT)
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        **SCRYPT, salt=base64.b64encode(salt).decode(),
        digest=base64.b64encode(digest).decode())


def verify_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), n, r, p)
    except (ValueError, AttributeError):
        return False
    # A hash made before SCRYPT was raised is cheaper to check than the dummy
    # an unknown phone is checked against. The work it falls short is done as
    # well (lanes at the current cost, the rest at its own), so the time
    # doesn't tell who is registered, right password or not.
    short = SCRYPT["n"] * SCRYPT["r"] * SCRYPT["p"] - n * r * p
    if short > 0:
        lanes, rest = divmod(short, SCRYPT["n"] * SCRYPT["r"])
        if lanes:
            _scrypt("", b"same time", SCRYPT["n"], SCRYPT["r"], lanes)
        if rest:
            _scrypt("", b"same time", n, r, -(-rest // (n * r)))
    return hmac.compare_digest(actual, expected)


def _outdated(stored):
    """True if a hash was made with another scrypt cost than SCRYPT."""
    return stored.split("$")[1:4] != [str(SCRYPT[k]) for k in ("n", "r", "p")]


_dummy = {}


def _dummy_hash():
    """Checked against when there is no real hash (unknown phone, no password or
    code yet), so every case takes the same time."""
    cost = tuple(SCRYPT.items())
    if cost not in _dummy:
        _dummy[cost] = hash_password(secrets.token_hex(16))
    return _dummy[cost]


_dummy_hash()       # made now, so even the first unknown phone takes the usual time


def create_first_admin(conn, name, phone, password, language="ar"):
    """Set-up step: the very first admin, with their own password."""
    _check_password_rules(password, L.normalize_phone(phone))
    password_hash = hash_password(password)
    user_id = L.create_user(conn, "admin", name, phone, language)
    with L._write(conn):
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (password_hash, user_id))
    return user_id


# ---------- lockout ----------

def _locked_until(conn, phone, now, client=None):
    """When the phone unlocks for `client`, or None. Within the lock window it
    counts this client's failures since its last successful try, and everyone's
    failures; a lock lifted for everyone (an admin, a right setup code) ends
    both counts. So a stranger's wrong tries lock only the stranger out, and
    everyone's lock spares a client that has given the right password before
    (the owner's usual address): only its own tries lock it."""
    since = (now - timedelta(minutes=LOCK_MINUTES)).isoformat()
    rows = conn.execute(
        "SELECT at, ok, client FROM login_attempts WHERE phone = ? AND at > ? "
        "ORDER BY id DESC", (phone, since)).fetchall()
    mine, everyone, mine_ended = [], [], False
    for row in rows:
        if row["ok"] and row["client"] is None:
            break                                   # lifted for everyone
        if row["ok"]:
            mine_ended = mine_ended or row["client"] == client
            continue
        everyone.append(row["at"])
        if row["client"] == client and not mine_ended:
            mine.append(row["at"])
    known = client is not None and conn.execute(
        "SELECT 1 FROM login_attempts WHERE phone = ? AND client = ? AND ok = 1 LIMIT 1",
        (phone, client)).fetchone() is not None
    for failures, limit in ((mine, MAX_FAILED_LOGINS),
                            ([] if known else everyone, MAX_FAILED_LOGINS_EVERYWHERE)):
        if len(failures) >= limit:
            return datetime.fromisoformat(failures[0]) + timedelta(minutes=LOCK_MINUTES)
    return None


def _record_attempt(conn, phone, ok, now, client=None):
    """client None on a successful try lifts the lock for everyone."""
    conn.execute("INSERT INTO login_attempts(phone, at, ok, client) VALUES (?,?,?,?)",
                 (phone, now.isoformat(), 1 if ok else 0, client))


def _check_not_locked(conn, phone, now, client=None):
    """For someone already logged in; logins just say LOGIN_FAILED."""
    until = _locked_until(conn, phone, now, client)
    if until and until > now:
        raise AccountLocked(f"too many wrong tries; try again after {until:%H:%M} UTC")


def unlock_login(conn, user_id, *, admin_id, reason, now=None):
    """Lift a lock left by wrong tries, e.g. when someone else keeps guessing a
    worker's or an admin's phone. Logged with the reason."""
    now = _now(now)
    reason = L._reason(reason)
    with L._write(conn):
        L._require_admin(conn, admin_id)
        user = L._user(conn, user_id)
        _record_attempt(conn, user["phone"], True, now)     # lifts every lock
        L._log(conn, admin_id, "unlock_login", user_id, reason=reason)


# ---------- setup codes ----------

def _clean_code(code):
    """A code as typed: any case, with or without its dashes or spaces."""
    return re.sub(r"[\s\-]", "", str(code or "")).upper()


def issue_setup_code(conn, user_id, *, admin_id, reason=None, now=None):
    """One-time code a worker uses to choose their password, first time or after
    forgetting it. Returned only here; give it to them in person or by WhatsApp.
    Earlier codes stop working, the old password stops working and open
    sessions end. Whoever holds the code can use it, the issuing admin
    included, so this is logged, and an admin's account needs a reason."""
    now = _now(now)
    code = "".join(secrets.choice(SETUP_CODE_ALPHABET) for _ in range(SETUP_CODE_LENGTH))
    code_hash = hash_password(code)                 # slow: kept out of the lock
    with L._write(conn):
        L._require_admin(conn, admin_id)
        user = L._user(conn, user_id)
        if not user["active"]:
            raise L.LedgerError("user is not active")
        if user["role"] == "admin":
            reason = L._reason(reason)
        conn.execute("UPDATE setup_codes SET used_at = ? WHERE user_id = ? "
                     "AND used_at IS NULL", (now.isoformat(), user_id))
        conn.execute(
            "INSERT INTO setup_codes(user_id, code_hash, issued_by, expires_at) "
            "VALUES (?,?,?,?)", (user_id, code_hash, admin_id,
                                 (now + timedelta(hours=SETUP_CODE_HOURS)).isoformat()))
        conn.execute("UPDATE users SET password_hash = NULL WHERE id = ?", (user_id,))
        _end_sessions(conn, user_id, now)
        L._log(conn, admin_id, "issue_setup_code", user_id,
               reason=(reason or "").strip() or None)
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def set_password_with_code(conn, phone, code, new_password, *, now=None, client=None):
    """Worker chooses their password with the setup code. Logs them in:
    returns a session token. A right code works even while the phone is
    locked by wrong tries, and lifts every lock; a wrong one counts as a
    wrong try from `client` (the web: the address)."""
    now = _now(now)
    phone = _login_phone(phone)
    _check_password_rules(new_password, phone)      # first: a refusal says nothing about the code
    user = phone and conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
    issued = user and conn.execute(
        "SELECT id, code_hash, issued_by FROM setup_codes WHERE user_id = ? "
        "AND used_at IS NULL AND expires_at > ? ORDER BY id DESC LIMIT 1",
        (user["id"], now.isoformat())).fetchone()
    # One slow check whether or not the phone and a code exist, so the time
    # taken doesn't tell who is registered.
    stored = issued["code_hash"] if issued is not None else None
    valid = verify_password(_clean_code(code), stored or _dummy_hash()) and stored is not None
    if valid:
        _check_can_use(user)
        new_hash = hash_password(new_password)      # slow: kept out of the lock
    if phone is None:
        raise LoginFailed("wrong phone number or code")
    with L._write(conn):
        used = valid and conn.execute(
            "UPDATE setup_codes SET used_at = ? WHERE id = ? AND used_at IS NULL "
            "AND expires_at > ?", (now.isoformat(), issued["id"], now.isoformat())
        ).rowcount == 1
        _record_attempt(conn, phone, used, now, None if used else client)
        if used:
            current = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
            _check_can_use(current)                 # not blocked or closed meanwhile
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (new_hash, user["id"]))
            _end_sessions(conn, user["id"], now)    # any opened since the code was issued
            L._log(conn, issued["issued_by"], "setup_code_used", user["id"],
                   code_id=issued["id"])
            return _new_session(conn, current, now)
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


def login(conn, phone, password, *, now=None, client=None):
    """Returns a session token. The same error for an unknown phone, a wrong
    password and a locked account, so it doesn't tell who is registered.
    `client` (the web: the address) is who the try counts for; see the lock."""
    now = _now(now)
    phone = _login_phone(phone)
    if phone and _locked_until(conn, phone, now, client):
        raise LoginFailed(LOGIN_FAILED)
    user = phone and conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
    stored = user["password_hash"] if user else None
    ok = verify_password(password or "", stored or _dummy_hash()) and stored is not None
    # A hash made with an older scrypt cost is redone with the current one.
    new_hash = hash_password(password) if ok and _outdated(stored) else None
    if phone is None:
        raise LoginFailed(LOGIN_FAILED)
    with L._write(conn):        # the password check above is slow: kept out of the lock
        if _locked_until(conn, phone, now, client):
            raise LoginFailed(LOGIN_FAILED)
        current = user and conn.execute("SELECT * FROM users WHERE id = ?",
                                        (user["id"],)).fetchone()
        ok = ok and current["password_hash"] == stored      # not reset meanwhile
        _record_attempt(conn, phone, ok, now, client)
        if ok:
            _check_can_use(current)
            if new_hash:
                conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                             (new_hash, user["id"]))
            return _new_session(conn, current, now)
    raise LoginFailed(LOGIN_FAILED)     # failed try already committed


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
    idle = now - datetime.fromisoformat(row["last_seen"])
    if row["role"] == "admin" and idle > timedelta(minutes=ADMIN_IDLE_MINUTES):
        raise NotLoggedIn("logged out after inactivity, please log in again")
    if not row["active"] or row["blocked"]:
        raise NotLoggedIn("this account can't be used; please contact the office")
    # Written at most once a minute, so reading a page doesn't wait for the
    # write lock. The admin idle limit counts from it: it can end a session up
    # to a minute early, never late.
    if idle >= timedelta(seconds=LAST_SEEN_SECONDS):
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


def change_password(conn, token, old_password, new_password, *, now=None, client=None):
    """Change own password. Other sessions end; this one stays. A wrong current
    password counts towards the same lock as a wrong login from `client`."""
    now = _now(now)
    user = authenticate(conn, token, now=now)
    _check_password_rules(new_password, user["phone"])  # first: says nothing about the old one
    _check_not_locked(conn, user["phone"], now, client)
    stored = conn.execute("SELECT password_hash FROM users WHERE id = ?",
                          (user["id"],)).fetchone()[0]
    ok = verify_password(old_password or "", stored or _dummy_hash()) and stored is not None
    new_hash = hash_password(new_password) if ok else None     # slow: kept out of the lock
    with L._write(conn):
        authenticate(conn, token, now=now)      # not logged out, blocked or closed meanwhile
        _check_not_locked(conn, user["phone"], now, client)
        _record_attempt(conn, user["phone"], ok, now, client)
        if ok:
            changed = conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ? AND password_hash = ?",
                (new_hash, user["id"], stored)).rowcount
            if not changed:
                raise L.Conflict("the password was changed meanwhile; try again")
            _end_sessions(conn, user["id"], now, keep=user["session_id"])
            return
    raise WrongPassword("current password is wrong")    # failed try already committed
