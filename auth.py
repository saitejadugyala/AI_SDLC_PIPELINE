"""
GitHub OAuth login for Foundry.

Flow:
  GET /auth/github/login    -> redirects to GitHub's consent screen
  GET /auth/github/callback -> GitHub redirects back here with a code;
                                 we exchange it for an access token, fetch
                                 the user's profile, upsert a users row,
                                 and store user_id in a signed session cookie.

Each user's own GitHub access token is stored in the users table — this is
what replaces the single shared GITHUB_PAT everywhere Deploy touches GitHub.
When a logged-in user triggers Deploy, look up their token by session
user_id instead of reading a hardcoded env var.

Scope requested is 'public_repo' (matches the public-repos-only decision
made earlier) — not 'repo', which would also grant private repo access.
"""

import logging
import sqlite3
import secrets
from urllib.parse import urlencode, urlparse

import requests
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse

import config

logger = logging.getLogger("auth")

GITHUB_CLIENT_ID = config.GITHUB_OAUTH_CLIENT_ID
GITHUB_CLIENT_SECRET = config.GITHUB_OAUTH_CLIENT_SECRET
APP_BASE_URL = config.APP_BASE_URL
CALLBACK_PATH = "/auth/github/callback"

DB_PATH = config.APP_DB_PATH  # shares the same sqlite file the rest of the app already uses

router = APIRouter()

CANONICAL_HOST = urlparse(APP_BASE_URL).netloc.lower()


def wrong_host_redirect(request: Request) -> RedirectResponse | None:
    """GitHub always returns to APP_BASE_URL. If the user started on another
    host (127.0.0.1 vs localhost), the state cookie lives on the wrong host and
    the callback fails with a state mismatch — so send them to the canonical
    host first."""
    host = (request.headers.get("host") or "").lower()
    if host and host != CANONICAL_HOST:
        target = f"{APP_BASE_URL}{request.url.path}"
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(target, status_code=307)
    return None


def _login_error(reason: str) -> RedirectResponse:
    """Send the browser back to the app with a readable reason instead of a raw JSON error."""
    response = RedirectResponse(f"/?login_error={reason}", status_code=303)
    response.delete_cookie("oauth_state", path=CALLBACK_PATH)
    return response


def init_auth_db():
    if not config.oauth_configured():
        # Don't crash the whole app (and every test) — login returns 503 with
        # setup instructions instead.
        logger.warning("GITHUB_OAUTH_CLIENT_ID / GITHUB_OAUTH_CLIENT_SECRET not set — GitHub login is disabled.")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            github_id INTEGER UNIQUE NOT NULL,
            username TEXT NOT NULL,
            avatar_url TEXT,
            access_token TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def get_user_by_id(user_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_current_user(request: Request) -> dict | None:
    """Call this from any route that needs to know who's logged in.
    Returns None if there's no valid session — caller decides whether
    that's an error (401) or just means 'show the logged-out view'."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return get_user_by_id(user_id)


@router.get("/auth/github/login")
def github_login(request: Request):
    redirect = wrong_host_redirect(request)
    if redirect:
        return redirect
    if not config.oauth_configured():
        raise HTTPException(
            status_code=503,
            detail="GitHub login is not configured: set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET in .env (see .env.example).",
        )
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": f"{APP_BASE_URL}{CALLBACK_PATH}",
        "scope": config.GITHUB_OAUTH_SCOPE,
        "state": state,
    }
    github_url = "https://github.com/login/oauth/authorize?" + urlencode(params)
    response = RedirectResponse(github_url)
    # short-lived cookie just to verify the callback isn't forged (CSRF check)
    response.set_cookie("oauth_state", state, httponly=True, max_age=600, samesite="lax",
                        secure=config.COOKIE_SECURE, path=CALLBACK_PATH)
    return response


@router.get(CALLBACK_PATH)
def github_callback(request: Request, code: str | None = None, state: str | None = None,
                    error: str | None = None):
    if error or not code:
        # e.g. error=access_denied when the user clicks Cancel on GitHub
        logger.info(f"OAuth callback without code | error={error}")
        return _login_error("denied" if error == "access_denied" else "failed")

    cookie_state = request.cookies.get("oauth_state")
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        # Still a hard CSRF failure (no login happens), just reported readably.
        logger.warning(f"OAuth state mismatch | state_cookie_present={bool(cookie_state)} | host={request.headers.get('host')}")
        return _login_error("expired")

    token_resp = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": f"{APP_BASE_URL}{CALLBACK_PATH}",
        },
        timeout=10,
    )
    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail=f"GitHub token exchange failed: {token_data.get('error_description') or token_data.get('error') or 'no access token returned'}")

    profile_resp = requests.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    profile = profile_resp.json()
    github_id = profile.get("id")
    username = profile.get("login")
    avatar_url = profile.get("avatar_url")
    if not github_id or not username:
        raise HTTPException(status_code=400, detail="Could not read GitHub profile")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO users (github_id, username, avatar_url, access_token)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(github_id) DO UPDATE SET
            username = excluded.username,
            avatar_url = excluded.avatar_url,
            access_token = excluded.access_token,
            updated_at = datetime('now')
    """, (github_id, username, avatar_url, access_token))
    conn.commit()
    user_row = conn.execute("SELECT id FROM users WHERE github_id = ?", (github_id,)).fetchone()
    conn.close()

    request.session.clear()  # fresh session on login (no session fixation)
    request.session["user_id"] = user_row[0]
    request.session["username"] = username

    response = RedirectResponse("/")
    response.delete_cookie("oauth_state", path=CALLBACK_PATH)
    return response


@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"status": "logged out"}


@router.get("/auth/me")
def whoami(request: Request):
    user = get_current_user(request)
    if not user:
        return {"logged_in": False}
    return {
        "logged_in": True,
        "username": user["username"],
        "avatar_url": user["avatar_url"],
    }