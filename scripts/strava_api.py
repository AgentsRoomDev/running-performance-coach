#!/usr/bin/env python3
"""
Shared Strava API v3 access layer — stdlib only.

Used by `strava_oauth.py` (obtaining the refresh token), `strava_sync.py`
(importing sessions), `strava_publish.py` (writing titles and descriptions back)
and `webhook_replay.py` (replaying a webhook payload).

No secret is ever printed or logged: credentials live in `.env` (not versioned)
and never leave it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

TOKEN_URL = "https://www.strava.com/oauth/token"
DEFAULT_ENDPOINT = "https://www.strava.com/api/v3"
USER_AGENT = "running-performance-coach/1.0 (stdlib)"
TIMEOUT = 30


# --------------------------------------------------------------------------- #
# .env
# --------------------------------------------------------------------------- #
def die(message: str) -> None:
    """Stop cleanly, carrying the cause INSIDE the exception.

    The message must travel in SystemExit rather than through a print: the
    background job catches the exception to log it, and a `print` would have left
    it with a bare "1" and no explanation. Python writes it to stderr itself when
    nobody catches it.
    """
    raise SystemExit(f"[FAILED] {message}")


def read_env(path: pathlib.Path = ENV_PATH) -> dict[str, str]:
    if not path.exists():
        die(f"{path} not found. Copy the template: cp .env.example .env")
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def write_env_value(path: pathlib.Path, key: str, value: str) -> None:
    """Replace (or append) a key in .env without touching the rest of the file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def _post(url: str, fields: dict[str, str]) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"User-Agent": USER_AGENT}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        die(f"Strava answered HTTP {error.code} on {url}:\n        {body}")
    except urllib.error.URLError as error:
        die(f"Network unreachable: {error.reason}")
    raise AssertionError  # unreachable


def post_json(
    url: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
    secret: str = "",
    signature_header: str = "X-AgentsRoom-Signature",
) -> tuple[bool, str]:
    """Generic JSON POST, **never fatal**.

    Unlike `_post` (dedicated to the token refresh, which calls `die()`), this one
    returns `(ok, message)` and never interrupts its caller. That is deliberate:
    it serves outgoing webhooks, and a third-party endpoint being down must not
    prevent a session from being imported. The log keeps the trace, the pass
    carries on.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json; charset=utf-8",
    }
    if secret:
        # ⚠️ The signature covers the EXACT BYTES SENT, not the dict.
        # Re-serialising somewhere else (whitespace, key order, Unicode escaping)
        # would produce a signature valid for a body the server will never
        # receive, and an undebuggable 401. That is precisely why the signature
        # is computed HERE and nowhere else.
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
        request_headers[signature_header] = f"sha256={digest.hexdigest()}"
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200].replace("\n", " ")
        return False, f"HTTP {error.code} — {detail}"
    except urllib.error.URLError as error:
        return False, f"network unreachable — {error.reason}"
    except (TimeoutError, OSError) as error:
        return False, f"network failure — {error}"


class Strava:
    """Minimal client: refresh the token, then authenticated GET / PUT."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = env if env is not None else read_env()
        self.endpoint = (self.env.get("STRAVA_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
        self.client_id = self.env.get("STRAVA_CLIENT_ID", "")
        self.client_secret = self.env.get("STRAVA_CLIENT_SECRET", "")
        self.refresh_token = self.env.get("STRAVA_REFRESH_TOKEN", "")
        self.access_token: str | None = None
        self.usage: dict[str, str] = {}
        self.requests = 0

        if not self.client_id or not self.client_secret:
            die(
                "STRAVA_CLIENT_ID or STRAVA_CLIENT_SECRET missing from .env.\n"
                "        See scripts/README.md."
            )
        if not self.refresh_token:
            die(
                "STRAVA_REFRESH_TOKEN is empty in .env.\n"
                "        Produce it once: python3 scripts/strava_oauth.py"
            )

    # -- authentication ----------------------------------------------------- #
    def authenticate(self) -> None:
        payload = _post(
            TOKEN_URL,
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        token = payload.get("access_token")
        if not token:
            die(f"No access_token in the Strava response: {payload}")
        self.access_token = token

        # Strava rotates the refresh token from time to time: persist it, or the
        # next run would fail with a stale token.
        rotated = payload.get("refresh_token")
        if rotated and rotated != self.refresh_token:
            write_env_value(ENV_PATH, "STRAVA_REFRESH_TOKEN", rotated)
            self.refresh_token = rotated
            print("  [info] Strava rotated the refresh token: .env updated.")

    # -- reading ------------------------------------------------------------ #
    def get(self, path: str, **params) -> dict | list:
        if self.access_token is None:
            self.authenticate()

        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        url = f"{self.endpoint}/{path.lstrip('/')}" + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                self.requests += 1
                for header in ("X-RateLimit-Limit", "X-RateLimit-Usage",
                               "X-ReadRateLimit-Limit", "X-ReadRateLimit-Usage"):
                    value = response.headers.get(header)
                    if value:
                        self.usage[header] = value
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            if error.code == 401:
                die(
                    "HTTP 401: token refused. The client secret may have been\n"
                    "        regenerated, or the app revoked. Redo:\n"
                    "          python3 scripts/strava_oauth.py"
                )
            if error.code == 403 and '"Inactive"' in body:
                die(
                    "HTTP 403 — Strava application INACTIVE.\n"
                    "        The token is fine: it is the app that Strava disabled.\n"
                    "        Since 30 June 2026, the Developer Program Standard Tier\n"
                    "        requires a paid Strava subscription on the account that\n"
                    "        OWNS the application (~USD 11.99/month).\n"
                    "        Two ways out:\n"
                    "          1. subscription active -> reactivation button on\n"
                    "             https://www.strava.com/settings/api ;\n"
                    "          2. no subscription -> use the TCX import from your watch:\n"
                    "             python3 scripts/import_tcx.py <file.tcx>"
                )
            if error.code == 403:
                die(f"HTTP 403 on {path}:\n        {body}")
            if error.code == 429:
                die(
                    "HTTP 429: rate limit reached.\n"
                    f"        Usage reported by the API: {self.usage}\n"
                    "        Retry on the next quarter hour (h:00, h:15, h:30, h:45)."
                )
            die(f"HTTP {error.code} on {path}:\n        {body}")
        except urllib.error.URLError as error:
            die(f"Network unreachable: {error.reason}")
        raise AssertionError  # unreachable

    def put(self, path: str, **fields) -> dict:
        """Update an activity. Requires the activity:write scope."""
        if self.access_token is None:
            self.authenticate()

        data = urllib.parse.urlencode(
            {k: v for k, v in fields.items() if v is not None}
        ).encode("utf-8")
        url = f"{self.endpoint}/{path.lstrip('/')}"
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="PUT",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                self.requests += 1
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            if error.code in (401, 403) and "authoriz" in body.lower():
                die(
                    "Write refused: the token lacks the activity:write scope.\n"
                    "        The current refresh token only allows reading.\n"
                    "        Re-authorise once:\n"
                    "          python3 scripts/strava_oauth.py\n"
                    f"        Response: {body}"
                )
            die(f"HTTP {error.code} on PUT {path}:\n        {body}")
        except urllib.error.URLError as error:
            die(f"Network unreachable: {error.reason}")
        raise AssertionError  # unreachable

    def sleep_between(self, seconds: float = 0.3) -> None:
        """Small courtesy between two calls, far below the rate limit."""
        time.sleep(seconds)
