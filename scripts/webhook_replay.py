#!/usr/bin/env python3
"""
Replay a payload into the coach webhook, signed exactly as the job would sign it.

    python3 scripts/webhook_replay.py body.json --dry-run   # what would be sent
    python3 scripts/webhook_replay.py body.json             # send it
    python3 scripts/webhook_replay.py body.json --url https://…/t_xxx

Why this exists: `strava_publish.py` notifies the webhook only ONCE per activity
(the local state in `data/inbox/publish-state.json` remembers the call), and only
for a freshly fetched session. So there is no way to test the coach trigger
without waiting for your next run — or tampering with production state. This
script does the one thing that was missing: POST an arbitrary body to the right
URL, with the right signature.

⚠️ THE SIGNATURE COVERS THE EXACT BYTES SENT. That is why the body is read here,
   deserialised, then handed to `post_json()`, which re-serialises AND signs in
   the same place. Signing the file as it sits on disk and then letting another
   layer re-serialise the dict would produce a signature valid for a body the
   server will never receive — and an undebuggable `Signature missing` /
   `Signature mismatch` rejection.

The secret never travels on the command line: it is read from `.env`
(`WEBHOOK_SECRET`), exactly as the job does. So it never appears in `ps aux`, nor
in a shell history, nor in an agent transcript.

Stdlib only (no pip install, see CLAUDE.md §8).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from strava_api import die, post_json, read_env

EXPECTED_KEYS = {"type", "title", "body", "author", "url", "id"}


def load_body(path: pathlib.Path) -> dict:
    """Read the file and check it looks like what the trigger expects."""
    if not path.exists():
        die(f"{path} not found.")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        die(f"{path} is not valid JSON: {error}")
    if not isinstance(body, dict):
        die(f"{path} must contain a JSON object, not a {type(body).__name__}.")

    # The endpoint maps {type,title,body,author,url,id} onto the {{event.*}}
    # variables of the prompt. A missing key does not fail the call: it leaves a
    # hole in the agent's prompt, which is far worse to diagnose. Warn here rather
    # than discovering it inside the run.
    missing = EXPECTED_KEYS - body.keys()
    if missing:
        print(f"⚠️  keys absent from the body: {', '.join(sorted(missing))} "
              f"— the matching {{{{event.*}}}} will be empty", file=sys.stderr)
    return body


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay a signed payload into the coach webhook.",
    )
    parser.add_argument("file", type=pathlib.Path,
                        help="JSON file holding the body to send")
    parser.add_argument("--url", default=None,
                        help="webhook URL (default: WEBHOOK_URL from .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent, send nothing")
    args = parser.parse_args()

    body = load_body(args.file)

    if args.dry_run:
        # Deliberately BEFORE reading `.env`: a dry run must validate the body
        # from a machine that has no credentials.
        print(f"→ {(args.url or 'WEBHOOK_URL from .env').split('?')[0]}")
        print(f"  activity {body.get('id', 'no-id')} — “{body.get('title', '')}”")
        print("\n--- body that would be sent ---")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        print("\n(--dry-run: nothing was sent)")
        return

    env = read_env()
    url = args.url or (env.get("WEBHOOK_URL") or "").strip()
    if not url:
        die("no URL: neither --url nor WEBHOOK_URL in .env.")
    secret = (env.get("WEBHOOK_SECRET") or "").strip()
    if not secret:
        die("WEBHOOK_SECRET missing from .env — the endpoint rejects unsigned "
            "calls (“Signature missing.”).")

    activity_id = str(body.get("id") or "no-id")
    # Same delivery header as the job: an id that is STABLE per activity, so the
    # receiver can deduplicate. Prefixed "replay-" so a test replay does not
    # masquerade as the original delivery.
    headers = {"X-AgentsRoom-Delivery": f"replay-{activity_id}"}

    print(f"→ {url.split('?')[0]}")
    print(f"  activity {activity_id} — “{body.get('title', '')}”")

    ok, message = post_json(url, body, headers, secret=secret)
    if ok:
        print(f"✅ {message}")
    else:
        print(f"⛔ {message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
