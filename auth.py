"""Authentication, session security, and local-only enforcement.

- First-run bootstrap: if no user exists, GET /login shows a "create account"
  form; POST creates it (this is the only time it's open, and it's local-only).
- Flask-Login manages the session; /login and static assets are the only
  unauthenticated endpoints.
- Idle timeout + absolute lifetime enforced in before_request.
- Session regenerated on login (fresh login cookie), session cleared on logout.
- Requests from non-localhost clients are rejected with 403 unless
  ALLOW_REMOTE=true in config.
- API token support for external local tools (``/api/...`` routes).
"""

from __future__ import annotations

import ipaddress
import secrets
import time
from functools import wraps

from flask import (Blueprint, current_app, flash, g, redirect,
                   render_template, request, session, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from werkzeug.security import generate_password_hash

import database

bp = Blueprint("auth", __name__)

login_manager = LoginManager()
login_manager.login_view = "auth.login"          # endpoint name for @login_required
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "error"


class User(UserMixin):
    def __int__(self):
        return self.id

    def __init__(self, row):
        self.id = row["id"]
        self.username = row["username"]
        self.api_token = row["api_token"]


@login_manager.user_loader
def load_user(user_id: str):
    try:
        row = database.get_user_by_id(int(user_id))
    except (ValueError, TypeError):
        return None
    if row is None:
        return None
    return User(row)


def is_local_request() -> bool:
    """True when the request comes from this machine (loopback)."""
    if current_app.config.get("ALLOW_REMOTE", False):
        return True
    remote = request.remote_addr
    if not remote:
        return False
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return remote in ("127.0.0.1", "::1", "localhost")


def register_auth(app):
    """Attach Flask-Login, before/after hooks, and the auth blueprint."""
    login_manager.init_app(app)
    app.register_blueprint(bp)
    app.extensions["pse_auth"] = True

    @app.before_request
    def _auth_guard():
        # 0) Remote clients are locked out entirely unless explicitly enabled.
        if not is_local_request():
            return ("Forbidden: this instance is local-only.", 403,
                    {"Content-Type": "text/plain; charset=utf-8"})
        endpoint = request.endpoint or ""
        public = {"auth.login", "static"}
        if current_user.is_authenticated:
            # enforce idle + absolute timeouts
            idle = app.config["SESSION_IDLE_TIMEOUT"]
            lifetime = app.config["SESSION_LIFETIME"]
            now = time.time()
            last = session.get("_last_activity", 0)
            started = session.get("_session_start", 0)
            if not started:
                session["_session_start"] = now
                started = now
                session["_last_activity"] = now
            elif idle and (now - last) > idle:
                logout_user()
                session.clear()
                flash("Session timed out due to inactivity.", "error")
                return redirect(url_for("auth.login"))
            elif lifetime and (now - started) > lifetime:
                logout_user()
                session.clear()
        else:
            if endpoint not in public:
                return redirect(url_for("auth.login"))

        # CSRF-lite for state-changing local requests
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            if not _csrf_ok():
                return "CSRF token missing or invalid.", 400
        return None

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    def _csrf_ok() -> bool:
        token = session.get("_csrf")
        supplied = request.form.get("_csrf") or \
            request.headers.get("X-CSRF-Token") or ""
        if not token or not supplied:
            return False
        return secrets.compare_digest(str(token), str(supplied))


# ------------------------------------------------------------------- routes

@bp.route("/login", methods=["GET", "POST"])
def login():
    needs_setup = database.count_users() == 0
    # A CSRF token must exist before the first POST can be validated.
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("password_confirm")
        if needs_setup:
            if not username or not password:
                flash("Username and password are required.", "error")
            elif confirm is not None and confirm != password:
                flash("Passwords do not match.", "error")
            else:
                try:
                    database.create_user(username, password)
                except Exception as e:  # noqa: BLE001
                    flash(f"Could not create account: {e}", "error")
                else:
                    flash("Account created. Please log in.", "success")
                    return redirect(url_for("auth.login"))
        else:
            row = database.verify_user(username, password)
            if row is None:
                flash("Invalid username or password.", "error")
            else:
                # regenerate the session on login: fresh session id and a
                # fresh CSRF token (standard privilege-change hygiene)
                old_csrf = session.get("_csrf")
                session.clear()
                login_user(User(row), remember=False)
                session["_session_start"] = time.time()
                session["_last_activity"] = time.time()
                session["_csrf"] = secrets.token_hex(16)
                flash(f"Welcome back, {username}.", "success")
                return redirect(url_for("dashboard"))
    return render_template("login.html", needs_setup=needs_setup,
                           csrf_token=session.get("_csrf", ""))


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


# ------------------------------------------------------------ API token auth

def api_auth_required(f):
    """Accept session OR X-Api-Token header (for external local tools)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if current_user.is_authenticated:
            return f(*args, **kwargs)
        token = request.headers.get("X-Api-Token", "")
        if token:
            row = database.get_user_by_token(token)
            if row:
                g.user = User(row)
                return f(*args, **kwargs)
        return {"error": "unauthorized"}, 401
    return wrapper
