#!/usr/bin/env python3
"""
Obtain the Strava refresh token — the OAuth flow, to be done ONCE.

    python3 scripts/strava_oauth.py

Reads : .env  (STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET)
Writes: .env  (STRAVA_REFRESH_TOKEN)

The token is never printed to the screen: it is written straight into .env,
which is excluded from the repository (see .gitignore).

Stdlib only, like the rest of the project (no pip install).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from strava_api import ENV_PATH, ROOT, USER_AGENT, die, read_env, write_env_value

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
REDIRECT_URI = "http://localhost/exchange_token"

# activity:read_all -> private activities; activity:write -> rewrite title and
# description. Ask for both now: adding a scope later means redoing this flow.
SCOPE = "activity:read_all,activity:write"


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #
def authorize_url(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "approval_prompt": "force",
        "scope": SCOPE,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def extract_code(raw: str) -> str:
    """Accept either the full redirect URL or the bare code."""
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        die("Nothing was entered.")
    match = re.search(r"[?&]code=([^&\s]+)", raw)
    if match:
        return urllib.parse.unquote(match.group(1))
    if re.fullmatch(r"[0-9a-fA-F]{20,60}", raw):
        return raw
    die(
        "Could not find a `code=` parameter in that.\n"
        "        Paste the FULL URL of the error page, the one starting with\n"
        "        http://localhost/exchange_token?state=&code=..."
    )
    raise AssertionError  # unreachable, for the type checker


def exchange(client_id: str, client_secret: str, code: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        hint = ""
        if error.code == 400:
            hint = (
                "\n        Classic causes:\n"
                "          - the code was already used (it is single-use) or expired\n"
                "            -> run this script again from the start;\n"
                "          - STRAVA_CLIENT_SECRET is empty or wrong in .env."
            )
        die(f"Strava answered HTTP {error.code}:\n        {body}{hint}")
    except urllib.error.URLError as error:
        die(f"Network unreachable: {error.reason}")
    raise AssertionError  # unreachable


# --------------------------------------------------------------------------- #
def main() -> None:
    env = read_env(ENV_PATH)
    client_id = env.get("STRAVA_CLIENT_ID", "")
    client_secret = env.get("STRAVA_CLIENT_SECRET", "")

    if not client_id:
        die("STRAVA_CLIENT_ID is empty in .env.")
    if not client_secret:
        die(
            "STRAVA_CLIENT_SECRET is empty in .env.\n"
            "        Get it from https://www.strava.com/settings/api then:\n"
            '          read -rs -p "Secret: " S && '
            "sed -i '' \"s|^STRAVA_CLIENT_SECRET=.*|STRAVA_CLIENT_SECRET=$S|\" .env && unset S\n"
            "        (reading it into a shell variable keeps it out of your history)"
        )

    url = authorize_url(client_id)

    print()
    print("=" * 78)
    print(" STEP 1/2 — authorise the application in your browser")
    print("=" * 78)
    print()
    print("Prerequisite, check it on https://www.strava.com/settings/api:")
    print("  \"Authorization Callback Domain\" must be exactly: localhost")
    print()
    print("Authorization URL (opened automatically):")
    print()
    print(f"  {url}")
    print()
    print("On the Strava page: click \"Authorize\", leaving BOTH boxes ticked —")
    print("access to private activities AND permission to modify activities.")
    print()
    print("Your browser will THEN show an error like \"This site can't be reached\":")
    print("that is NORMAL and expected, nothing is listening on localhost.")
    print("What matters is the URL in the address bar, of the form:")
    print()
    print("  http://localhost/exchange_token?state=&code=0123abc...&scope=read,activity:read_all")
    print()

    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - no browser: the URL is printed above
        pass

    print("-" * 78)
    try:
        raw = input("Paste the full URL from the address bar here, then Enter:\n> ")
    except EOFError:
        die(
            "No input available: this script is interactive.\n"
            "        Run it in a real terminal: python3 scripts/strava_oauth.py"
        )
        raise AssertionError  # unreachable
    code = extract_code(raw)

    print()
    print("=" * 78)
    print(" STEP 2/2 — exchange the code for tokens")
    print("=" * 78)
    payload = exchange(client_id, client_secret, code)

    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        die(f"Unexpected Strava response (no refresh_token): {payload}")

    granted = payload.get("scope") or ""
    write_env_value(ENV_PATH, "STRAVA_REFRESH_TOKEN", refresh_token)

    athlete = payload.get("athlete") or {}
    print()
    print(f"  refresh token : written to {ENV_PATH.name} "
          f"({len(refresh_token)} characters, not displayed)")
    print(f"  athlete       : id {athlete.get('id', '?')}")
    if granted:
        print(f"  scopes granted: {granted}")
    print()

    if granted and "activity:write" not in granted:
        print("  [WARNING] The activity:write scope was NOT granted: updating titles")
        print("  and descriptions (scripts/strava_publish.py) will fail.")
        print()
    if granted and "activity:read_all" not in granted:
        print("  [WARNING] The activity:read_all scope was NOT granted: private")
        print("  activities will stay invisible. Run this script again and leave the")
        print("  private-activity box ticked.")
        print()

    # Activities abandoned after repeated write failures must be picked up again:
    # the cache is purged here so there is only ONE command to run.
    cache = ROOT / "data" / "inbox" / "publish-state.json"
    if cache.exists():
        cache.unlink()
        print("  state cache purged: pending activities will be retried by the")
        print("  next pass of the service (within 15 minutes).")
        print()

    print("Done. You can verify without revealing the value:")
    print("  grep -c '^STRAVA_REFRESH_TOKEN=.\\+' .env      # should print 1")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. No file modified.", file=sys.stderr)
        raise SystemExit(130) from None
