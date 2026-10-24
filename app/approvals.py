"""Changes to admins' accounts that a second admin must approve, so that no
admin alone can give themselves another admin's account or a new one:

- create_admin: a new admin's account (and its setup code);
- admin_setup_code: a new setup code for an admin (resetting their account);
- unblock_admin, reactivate_admin: an admin's access given back.

Blocking or closing an admin stays immediate: taking access away must be
quick when an account is misused.

The admin who asks makes a request (`request`); another admin approves it
(`approve`), and the change is made then, as theirs, in the same write. A
setup code it makes is given to the approver, never to the admin who asked,
so taking over an account needs two admins. The approver must be an active,
unblocked admin who is neither the one who asked nor the account's admin.
When no such admin exists (two admins, one asking about the other), the
owner approves on the server: `python3 -m app.manage approve <id>`.

When the admin asking is the only active admin, there is nobody to ask: the
change is made at once (`request` says so), as before approvals existed.

A request expires after EXPIRES_HOURS. It can be rejected by another admin
(with a reason) or cancelled by the one who asked. Every step is logged.
"""
import json
from datetime import datetime, timedelta, timezone

from . import auth as A
from . import ledger as L

EXPIRES_HOURS = 24
ACTIONS = ("create_admin", "admin_setup_code", "unblock_admin", "reactivate_admin")


def _now(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _others(conn, admin_id):
    """Active, unblocked admins other than admin_id."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM users WHERE role = 'admin' AND active = 1 AND blocked = 0 "
        "AND id != ?", (admin_id,))]


def _admin_target(conn, user_id):
    user = L._user(conn, user_id)
    if user["role"] != "admin":
        raise L.LedgerError(f"user {user_id} is not an admin")
    return user


def _check(conn, action, target, details):
    """Refuses now what the change would refuse when approved (so nobody
    approves a request that can't be done)."""
    if action == "create_admin":
        phone = L.normalize_phone(details["phone"])
        if conn.execute("SELECT 1 FROM users WHERE phone = ?", (phone,)).fetchone():
            raise L.Conflict(f"{phone} is already registered")
        if details["language"] not in L.LANGUAGES:
            raise L.LedgerError(f"language must be one of {', '.join(L.LANGUAGES)}")
        if not str(details.get("name") or "").strip():
            raise L.LedgerError("a name is required")
        return
    user = _admin_target(conn, target)
    if action == "admin_setup_code" and not user["active"]:
        raise L.LedgerError("user is not active")
    if action == "unblock_admin" and not user["blocked"]:
        raise L.LedgerError("user is not blocked")
    if action == "reactivate_admin" and user["active"]:
        raise L.LedgerError("user is already active")


def _run(conn, action, target, details, actor):
    """Makes the change as `actor`. Returns {"user_id", "setup_code"?}."""
    reason = details["reason"]
    if action == "create_admin":
        user_id = L.create_user(conn, "admin", details["name"], details["phone"],
                                details["language"], admin_id=actor,
                                whatsapp=details.get("whatsapp"))
        return {"user_id": user_id,
                "setup_code": A.issue_setup_code(conn, user_id, admin_id=actor, reason=reason)}
    if action == "admin_setup_code":
        return {"user_id": target,
                "setup_code": A.issue_setup_code(conn, target, admin_id=actor, reason=reason)}
    if action == "unblock_admin":
        L.unblock_user(conn, target, admin_id=actor, reason=reason)
    else:
        L.reactivate_user(conn, target, admin_id=actor, reason=reason)
    return {"user_id": target}


def request(conn, action, *, admin_id, target=None, details, now=None):
    """Asks for a change (see ACTIONS). details: {"reason"} and, for
    create_admin, {"name", "phone", "language", "whatsapp"?}. Returns
    {"approval_id": n} when it waits for another admin, or {"done": result}
    when admin_id is the only active admin and it was made at once."""
    if action not in ACTIONS:
        raise L.LedgerError(f"action must be one of {', '.join(ACTIONS)}")
    details = dict(details, reason=L._reason(details.get("reason")))
    if action == "create_admin":
        details["name"] = str(details.get("name") or "").strip()
        details["phone"] = L.normalize_phone(details.get("phone"))
        if details.get("whatsapp") is not None:
            details["whatsapp"] = L.normalize_whatsapp(details["whatsapp"])
    now = _now(now)
    with L._write(conn):
        L._require_admin(conn, admin_id)
        if target is not None and target == admin_id:
            raise L.LedgerError("ask another admin to do this for your own account")
        _check(conn, action, target, details)
        if not _others(conn, admin_id):
            return {"done": _run(conn, action, target, details, admin_id)}
        cur = conn.execute(
            "INSERT INTO approvals(action, target, details, requested_by, requested_at, "
            "expires_at) VALUES (?,?,?,?,?,?)",
            (action, target, json.dumps(details, sort_keys=True), admin_id, now.isoformat(),
             (now + timedelta(hours=EXPIRES_HOURS)).isoformat()))
        L._log(conn, admin_id, "request_approval", cur.lastrowid, change=action,
               user_id=target, reason=details["reason"])
        return {"approval_id": cur.lastrowid}


def _pending(conn, approval_id, now):
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if not row:
        raise L.NotFound("no such approval request")
    if row["status"] != "pending":
        raise L.Conflict(f"this request is already {row['status']}")
    if row["expires_at"] <= now.isoformat():
        raise L.Conflict("this request has expired; ask again")
    return row


def approve(conn, approval_id, *, admin_id=None, now=None):
    """Approves and makes the change, as the approving admin (admin_id), or
    with admin_id=None as the owner on the server (app.manage), the change
    then made in the name of the admin who asked. Returns the change's result:
    a setup code it made is for the approver to give on."""
    now = _now(now)
    with L._write(conn):
        if admin_id is not None:
            L._require_admin(conn, admin_id)
        row = _pending(conn, approval_id, now)
        if admin_id is not None:
            if admin_id == row["requested_by"]:
                raise L.PermissionDenied("another admin must approve your request")
            if admin_id == row["target"]:
                raise L.PermissionDenied("this request is about your own account; "
                                         "another admin must approve it")
        actor = admin_id if admin_id is not None else row["requested_by"]
        details = json.loads(row["details"])
        _check(conn, row["action"], row["target"], details)
        result = _run(conn, row["action"], row["target"], details, actor)
        conn.execute(
            "UPDATE approvals SET status = 'approved', target = ?, decided_by = ?, "
            "decided_at = ? WHERE id = ?",
            (result["user_id"], admin_id, now.isoformat(), approval_id))
        L._log(conn, actor, "approve", approval_id, change=row["action"],
               user_id=result["user_id"], requested_by=row["requested_by"],
               **({} if admin_id is not None else {"by": "the owner, on the server"}))
        return result


def reject(conn, approval_id, *, admin_id, reason, now=None):
    """Another admin says no, with a reason."""
    reason = L._reason(reason)
    now = _now(now)
    with L._write(conn):
        L._require_admin(conn, admin_id)
        row = _pending(conn, approval_id, now)
        if admin_id == row["requested_by"]:
            raise L.LedgerError("cancel your own request instead")
        conn.execute("UPDATE approvals SET status = 'rejected', decided_by = ?, "
                     "decided_at = ?, decision_reason = ? WHERE id = ?",
                     (admin_id, now.isoformat(), reason, approval_id))
        L._log(conn, admin_id, "reject_approval", approval_id, change=row["action"],
               reason=reason)


def cancel(conn, approval_id, *, admin_id, now=None):
    """The admin who asked takes the request back."""
    now = _now(now)
    with L._write(conn):
        L._require_admin(conn, admin_id)
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if not row:
            raise L.NotFound("no such approval request")
        if row["requested_by"] != admin_id:
            raise L.PermissionDenied("only the admin who asked can cancel a request")
        if row["status"] != "pending":
            raise L.Conflict(f"this request is already {row['status']}")
        conn.execute("UPDATE approvals SET status = 'cancelled', decided_by = ?, "
                     "decided_at = ? WHERE id = ?", (admin_id, now.isoformat(), approval_id))
        L._log(conn, admin_id, "cancel_approval", approval_id, change=row["action"])


def listing(conn, *, status=None, limit=50, now=None):
    """Requests, newest first, each with its state: pending, expired (pending
    past its time), approved, rejected or cancelled; and, for a pending one,
    who could approve it (`approvers`: admin ids; empty: only the owner, on
    the server). status= narrows to one state."""
    now = _now(now).isoformat()
    rows = conn.execute("SELECT * FROM approvals ORDER BY id DESC").fetchall()
    out = []
    for row in rows:
        state = row["status"]
        if state == "pending" and row["expires_at"] <= now:
            state = "expired"
        if status is not None and state != status:
            continue
        item = dict(row, status=state, details=json.loads(row["details"]))
        if state == "pending":
            item["approvers"] = [i for i in _others(conn, row["requested_by"])
                                 if i != row["target"]]
        out.append(item)
        if len(out) >= limit:
            break
    return out
