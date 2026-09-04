# Scripts

Pure stdlib Python — **no `pip install`, no virtualenv, no dependency**. That is
a hard constraint, not an accident: these scripts run unattended on a small
server where `pip install` is blocked by PEP 668, and a dependency that breaks at
3 a.m. is a session lost.

Compatible with Python **3.9+** thanks to `from __future__ import annotations`.

| Script | What it does |
|---|---|
| [`strava_api.py`](strava_api.py) | Shared layer: `.env`, token refresh, GET/PUT, signed POST |
| [`strava_oauth.py`](strava_oauth.py) | Obtain the refresh token — **once** |
| [`strava_sync.py`](strava_sync.py) | API → pre-filled log sheets (manual) |
| [`strava_publish.py`](strava_publish.py) | The job: fetch, log, commit, write to Strava, fire the webhook |
| [`webhook_replay.py`](webhook_replay.py) | Replay a payload into the trigger, to test it |
| [`import_tcx.py`](import_tcx.py) | Fallback with no API, from a TCX file |
| [`systemd/install.sh`](systemd/install.sh) | Install the 15-minute timer on a Linux server |

---

## 1. Credentials

Create a Strava API application: https://www.strava.com/settings/api

⚠️ **"Authorization Callback Domain" must be exactly `localhost`** — the OAuth
step fails with an unhelpful error otherwise.

```bash
cp .env.example .env
chmod 600 .env
```

Fill `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET`. To avoid the secret landing
in your shell history:

```bash
read -rs -p "Secret: " S && \
  sed -i '' "s|^STRAVA_CLIENT_SECRET=.*|STRAVA_CLIENT_SECRET=$S|" .env && unset S
```

> ⛔ **Never paste a secret into a conversation with an AI agent.** The transcript
> is written to disk in clear text. If an agent needs to sign something, give it
> a script that reads `.env` itself — that is exactly what `webhook_replay.py`
> does.

## 2. The refresh token, once

```bash
python3 scripts/strava_oauth.py
```

It opens your browser, you click *Authorize*, and the page then shows **"This
site can't be reached"** — that is normal, nothing is listening on localhost.
Copy the whole URL from the address bar and paste it back into the script.

**Leave both boxes ticked.** `activity:read_all` gives access to private
activities; `activity:write` is what allows rewriting titles and descriptions.
Missing one means redoing the whole flow later.

The token is written straight into `.env` and never printed. Verify without
revealing it:

```bash
grep -c '^STRAVA_REFRESH_TOKEN=.\+' .env      # should print 1
```

> Strava rotates the refresh token from time to time. The client persists the new
> one automatically — which is why `.env` must stay writable by the job.

## 3. First import

```bash
python3 scripts/strava_sync.py --dry-run --since 2026-01-01
python3 scripts/strava_sync.py --since 2026-01-01
```

A sheet already written is **never** overwritten (`journal/README.md`, rule 3).
Use `--append` to add the Strava data at the end of an existing sheet.

## 4. The automated job

```bash
python3 scripts/strava_publish.py --dry-run     # what would be written
python3 scripts/strava_publish.py --once        # one pass
python3 scripts/strava_publish.py --force <id>  # republish one activity
```

On a Linux server:

```bash
bash scripts/systemd/install.sh            # install + start (15 min timer)
bash scripts/systemd/install.sh --status   # state + next runs
bash scripts/systemd/install.sh --logs     # service journal
```

### Rate limits

Real limits, read from the response headers: **300 requests per 15 minutes,
3 000 per day.**

| Situation | Requests |
|---|---|
| Pass with nothing new | 1 |
| A new session to handle | 4 (list + detail + laps + write) |

96 passes a day against a 3 000 ceiling. No risk.

---

## The webhook, and how to test it

`strava_publish.py` fires an outgoing webhook after each new session has been
logged. That is what wakes the coach agent.

```dotenv
WEBHOOK_URL=https://agentsroom.dev/api/triggers/t_xxxxxxxxxxxx
WEBHOOK_SECRET=whsec_xxxxxxxxxxxxxxxxxxxxxxxx
```

Both values come from the AgentsRoom trigger editor.

### Replaying a session into the webhook

Useful for testing the trigger without waiting for your next run.

```bash
python3 scripts/webhook_replay.py scripts/examples/webhook-session.json --dry-run
python3 scripts/webhook_replay.py scripts/examples/webhook-session.json
```

The body is a plain JSON file in the `{type, title, body, author, url, id}`
schema — [`examples/webhook-session.json`](examples/webhook-session.json) is a
working one. The URL and the secret are read from `.env`, so **the secret never
travels on the command line** (not in `ps aux`, not in your shell history).

⚠️ **This only runs on a machine that has a `.env`** — in practice, the server.
From a laptop, `--dry-run` still validates the body.

### Three traps worth knowing

**The endpoint rejects unsigned calls.**

```json
{"error":"REJECTED","message":"Signature missing."}
```

The signature is an HMAC-SHA256 in the `X-AgentsRoom-Signature` header, keyed on
`WEBHOOK_SECRET`.

**The signature covers the exact bytes sent, not the object.** Sign a file on
disk and then let another layer re-serialise the dict — different whitespace, key
order, Unicode escaping — and you get a signature that is valid for a body the
server will never receive. The rejection is then impossible to diagnose. This is
why `post_json()` serialises *and* signs in the same place, and why
`webhook_replay.py` delegates both to it.

**⛔ `--force <id>` does NOT replay the webhook.** `notify_webhook()` returns as
soon as `state["webhooks"][id].ok` is true, without ever consulting `args.force`
— which only governs rewriting the description. For a genuine second call from
the job itself, remove the entry from the `webhooks` section of
`data/inbox/publish-state.json`. `webhook_replay.py` avoids having to touch
production state at all.

---

## How the session shape is reconstructed

Your watch records eleven laps. The script works out that this was a
`5 × 1000 m with 2 min recovery`, and which laps were the repetitions.

It tries every cut of the form *"the k fastest laps are the repetitions"* and
keeps the best **valid** one. Three conditions rule out false positives:

1. the pace gap at the cut is at least **20 s/km**;
2. the repetitions are homogeneous in distance **or** in duration (±12 %);
3. the repetitions are **not all consecutive** — there must be a recovery lap
   between the first and the last.

Whether the session is expressed in **distance** or in **duration** is not read
from the plan but from the numbers: the quantity that is both the most regular
*and* the roundest betrays the intent. `2 × 2 km` gives exact distances and
arbitrary durations; `3 × 8'` gives the opposite.

### Why not k-means

It was tried and it fails. On one real session a recovery lap jogged at 9:08/km
pulled the "slow" centroid so far out that the **warm-up was classified as a
repetition**. Condition 3 exists for a related reason: without it, a progressive
easy run reads as an interval session.

The noise floor is deliberately low (40 m, 10 s). The recoveries of a 30/30
session are only 80-100 m long, and discarding them would make the repetitions
look consecutive — which condition 3 would then reject.

---

## Idempotence without shared state

The problem: the job may run on several machines. How does the home machine know
the work laptop already published a description?

The answer: **it does not need to.** The source of truth is Strava itself. The
lines the script always writes act as a signature:

| Description | Verdict | Action |
|---|---|---|
| Empty | `publish` | write it |
| Carries a signature | `already-done` | leave it alone |
| Non-empty, unsigned | `left-to-athlete` | ⛔ **never touch** |

That last row is the important one. What you wrote by hand always wins.

> 🔧 **If you translate or restyle the description, update the signature regexes
> in `strava_publish.py` in the same commit.** Otherwise the script stops
> recognising its own descriptions, reclassifies them as hand-written — and they
> become untouchable, including for correcting them.

`data/inbox/publish-state.json` is only a **cost-saving cache**: it avoids
re-fetching the detail of an activity already settled. Deleting it breaks
nothing, it just makes one pass more expensive.

---

## No API? Use TCX

Since 30 June 2026 the Strava API requires a paid Developer Program subscription
on the account that **owns the application**. Without it, every call answers:

```
HTTP 403 — Application Status Inactive
```

The fallback exports the same laps from your watch platform (activity → gear
icon → *Export to TCX*):

```bash
python3 scripts/import_tcx.py ~/Downloads/activity_1234.tcx \
    --name "Track — 6 x 1000 m" --dry-run
```

⚠️ **Export TCX, not GPX.** A GPX carries the trace but loses the `<Lap>`
elements — and the laps *are* the session. Everything downstream (shape
reconstruction, splits, the coach's analysis) works identically from a TCX.

Elevation gain and kilometre splits are recomputed from the trace and **flagged
as computed** in the sheet, because they were not measured.

---

## Traps that cost real time

- **`start_date_local` carries a `Z` but is a local time.** Treating it as UTC
  shifts every session by your timezone offset.
- **`average_cadence` is per leg.** Double it for steps per minute.
- **Strava auto-names activities** ("Afternoon Run"). A pattern matching common
  words in an activity name will label everything a race — which is why the
  plan's code always wins over the name.
- **`die()` must carry its cause inside `SystemExit`.** With a `print`, the
  background job logs a bare `1` and you have nothing to debug.
- **A refused write must not kill the pass.** Logging the session matters more
  than dressing it up on Strava. After 5 failures on the same activity the script
  gives up on the write and says so — otherwise a missing scope burns the quota
  to no effect until someone reads the log.
- **Under systemd, never point at a pyenv shim.** It depends on a `PATH` systemd
  does not provide. `install.sh` resolves `sys.executable` and falls back to
  `/usr/bin/python3`.
- **`.gitattributes` with `eol=lf` is not optional.** The `csv` module writes
  CRLF, and with `core.autocrlf=input` the file looks clean on one machine and
  modified on another — dozens of lines of phantom diff per CSV, and a
  `git pull --rebase` that breaks.
- **Activity visibility cannot be set through the API.** Strava removed it around
  2018; `private`, `visibility` and `hide_from_home` are silently ignored. It is
  an account setting.
