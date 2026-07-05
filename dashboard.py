"""
dashboard.py
~~~~~~~~~~~~
Password-protected web admin dashboard, an ADDITIONAL way to do what the
Telegram admin commands already do — the bot and its /approve, /block, etc.
commands are untouched and keep working.

Runs in a background thread inside the bot's own process (see
start_dashboard_in_background, called from bot.py), so it shares the exact
same SQLite database file — no duplicated data, no separate service, no
Railway volume-sharing problem (a second Railway service can't mount the
bot's volume). Every DB operation calls the SAME database.py / access.py
functions the bot uses, so the two can never drift apart.

PHASE 1 (this file, so far): login + read-only views (stats, users,
pending, plans). Write actions (approve/reject/block/extend/broadcast,
plan CRUD) are deliberately NOT here yet — added in a later phase once the
read-only dashboard is verified against the live DB.

Env vars:
    ADMIN_DASHBOARD_PASSWORD  required — dashboard won't start without it
    SECRET_KEY                recommended — stable Flask session signing key;
                              if unset, a random one is generated per start
                              (works, but every restart logs you out)
    PORT                      injected by Railway; the web server binds here
"""

import hmac
import logging
import os
import secrets
from collections import Counter
from datetime import datetime
from functools import wraps

from flask import Flask, redirect, render_template, request, session, url_for

from access import (
    compute_access,
    STATUS_TRIAL,
    STATUS_ACTIVE,
    STATUS_EXPIRED_GRACE,
    STATUS_LOCKED,
)
from database import (
    IST,
    list_all_users,
    list_plans,
    list_products,
    get_all_products,
    get_approvals_since,
)

logger = logging.getLogger(__name__)

STATUS_LABEL = {
    STATUS_TRIAL: "Trial",
    STATUS_ACTIVE: "Active",
    STATUS_EXPIRED_GRACE: "Expired (grace)",
    STATUS_LOCKED: "Locked",
}
STATUS_CLASS = {
    STATUS_TRIAL: "ok",
    STATUS_ACTIVE: "ok",
    STATUS_EXPIRED_GRACE: "warn",
    STATUS_LOCKED: "bad",
}


def _fmt_days(days):
    if days is None:
        return "—"
    if days < 0:
        return "expired"
    if days < 1:
        return f"{days * 24:.1f}h left"
    return f"{days:.1f}d left"


def _display_name(u: dict) -> str:
    return u.get("first_name") or (f"@{u['username']}" if u.get("username") else str(u["user_id"]))


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    if not os.environ.get("SECRET_KEY"):
        logger.warning(
            "[dashboard] SECRET_KEY not set — using a random per-start key; "
            "you'll be logged out on every restart. Set SECRET_KEY to persist sessions."
        )

    def _password() -> str:
        return os.environ.get("ADMIN_DASHBOARD_PASSWORD", "")

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("authed"):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    # ── Auth ─────────────────────────────────────────────────────────────────
    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            supplied = request.form.get("password", "")
            expected = _password()
            # constant-time compare so response timing can't be used to guess
            # the password character-by-character
            if expected and hmac.compare_digest(supplied, expected):
                session["authed"] = True
                dest = request.args.get("next") or url_for("home")
                # only allow same-site relative redirects
                if not dest.startswith("/"):
                    dest = url_for("home")
                return redirect(dest)
            error = "Incorrect password."
            logger.warning("[dashboard] failed login attempt")
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ── Read-only views ──────────────────────────────────────────────────────
    @app.route("/")
    @login_required
    def home():
        users = list_all_users()
        counts = Counter(compute_access(u).status for u in users)
        now = datetime.now(IST)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        revenue = sum(a["amount"] or 0 for a in get_approvals_since(month_start))
        all_products = get_all_products()
        site_counts = Counter(p["site"] for p in all_products)
        top_store = site_counts.most_common(1)
        stats = {
            "total_users": len(users),
            "active": counts.get(STATUS_ACTIVE, 0),
            "trial": counts.get(STATUS_TRIAL, 0),
            "grace": counts.get(STATUS_EXPIRED_GRACE, 0),
            "locked": counts.get(STATUS_LOCKED, 0),
            "revenue": revenue,
            "total_products": len(all_products),
            "top_store": f"{top_store[0][0]} ({top_store[0][1]})" if top_store else "—",
            "active_plans": len(list_plans(active_only=True)),
        }
        return render_template("dashboard.html", stats=stats)

    @app.route("/users")
    @login_required
    def users():
        q = (request.args.get("q") or "").strip().lower()
        rows = []
        for u in list_all_users():
            info = compute_access(u)
            name = _display_name(u)
            if q and q not in str(u["user_id"]) and q not in name.lower():
                continue
            rows.append({
                "user_id": u["user_id"],
                "name": name,
                "status": STATUS_LABEL.get(info.status, info.status),
                "status_class": STATUS_CLASS.get(info.status, ""),
                "plan": info.plan["name"] if info.plan else "—",
                "days": _fmt_days(info.days_remaining),
                "blocked": bool(u.get("blocked")),
                "items": len(list_products(u["user_id"])),
            })
        return render_template("users.html", rows=rows, q=request.args.get("q") or "")

    @app.route("/pending")
    @login_required
    def pending():
        rows = []
        for u in list_all_users():
            if u.get("blocked"):
                continue
            info = compute_access(u)
            if info.status in (STATUS_TRIAL, STATUS_EXPIRED_GRACE):
                rows.append({
                    "user_id": u["user_id"],
                    "name": _display_name(u),
                    "kind": "Trial" if info.status == STATUS_TRIAL else "Awaiting approval (grace)",
                    "days": _fmt_days(info.days_remaining),
                    "grace_days": _fmt_days(info.grace_days_remaining) if info.grace_days_remaining else "—",
                })
        return render_template("pending.html", rows=rows)

    @app.route("/plans")
    @login_required
    def plans():
        return render_template("plans.html", plans=list_plans())

    return app


def start_dashboard_in_background() -> None:
    """
    Launch the dashboard on a daemon thread using waitress, bound to Railway's
    $PORT. No-ops (with a warning) if ADMIN_DASHBOARD_PASSWORD isn't set, so an
    existing deploy that hasn't configured the dashboard keeps behaving exactly
    as before. Never raises into the caller — a dashboard failure must not take
    the bot down with it.
    """
    if not os.environ.get("ADMIN_DASHBOARD_PASSWORD"):
        logger.warning(
            "[dashboard] ADMIN_DASHBOARD_PASSWORD not set — admin dashboard disabled. "
            "Set it (and ideally SECRET_KEY) to enable the web dashboard."
        )
        return

    import threading

    def _run():
        try:
            from waitress import serve
            port = int(os.environ.get("PORT", "8080"))
            app = create_app()
            logger.info(f"[dashboard] starting on 0.0.0.0:{port}")
            serve(app, host="0.0.0.0", port=port, threads=4)
        except Exception as exc:
            logger.error(f"[dashboard] failed to start (bot continues without it): {exc}")

    threading.Thread(target=_run, name="dashboard", daemon=True).start()
