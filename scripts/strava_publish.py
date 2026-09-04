#!/usr/bin/env python3
"""
Automatic title and description updates for sessions on Strava, plus logging.

    python3 scripts/strava_publish.py --dry-run     # what would be written
    python3 scripts/strava_publish.py --once        # one pass, then exit
    python3 scripts/strava_publish.py --watch       # loop (for systemd/cron)
    python3 scripts/strava_publish.py --force 19910260700

What the script does:

0. rebases the repository on origin/main: the week's plan lives IN the
   repository, and a session replanned from another machine must be read before
   the actual is compared against it (`--no-pull` to skip);
1. fetches recent activities (running);
2. reconstructs the REAL STRUCTURE of the session from the watch laps
   (N × distance or N × duration, recovery, split times of each repetition);
3. reads the PLANNED session in plan/weeks/YYYY-Wnn.md for that day;
4. composes a title (1 emoji) and a description (2-3 emojis), flagging the gap
   when the actual differs from the plan;
5. writes to Strava via PUT /activities/{id} (activity:write scope);
6. logs everything to logs/strava_publish.log;
7. optionally fires a signed webhook so an agent can pick the session up.

⚠️ VISIBILITY CANNOT BE CHANGED THROUGH THE API (Strava removed it in 2018):
   neither `private`, nor `visibility`, nor `hide_from_home` are accepted on this
   endpoint — they are silently ignored. To make sessions public it is an account
   setting, once and for all:
   Strava -> Settings -> Privacy Controls -> "Activities" -> Everyone.

Guardrail: an activity that already carries a description NOT written by this
script is never touched (except with --force). What the athlete wrote by hand
takes priority.

Stdlib only (no pip install, see CLAUDE.md §8).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time

from strava_api import ROOT, Strava, die, post_json, read_env
from strava_sync import JOURNAL, regenerate_csv, render_sheet, write_activities

STATE = ROOT / "data" / "inbox" / "publish-state.json"
LOG = ROOT / "logs" / "strava_publish.log"
WEEKS = ROOT / "plan" / "weeks"

RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}

# A lap shorter than this is button noise, not a repetition.
# Deliberately low: the recoveries of a 30/30 session are 80 to 100 m, and
# discarding them would make the repetitions look consecutive.
MIN_LAP_DISTANCE_M = 40
MIN_LAP_DURATION_S = 10
# Minimum pace gap between recovery and effort before we call it intervals.
MIN_PACE_GAP_S = 20.0

# Emoji + label per session family, keyed on the code used in plan/weeks/.
# 🔧 These codes must match plan/session-types.md. Order matters: the first
#    match wins.
FAMILIES = [
    (re.compile(r"RACE|🏁", re.I), "🏁", "Race"),
    (re.compile(r"VO2", re.I), "⚡", "Track"),
    (re.compile(r"THR", re.I), "🔥", "Threshold"),
    (re.compile(r"RP10|RP\b", re.I), "🎯", "Race pace"),
    (re.compile(r"HILL", re.I), "⛰️", "Hills"),
    (re.compile(r"LONG", re.I), "⌛️", "Long run"),
    (re.compile(r"REC", re.I), "🧘", "Recovery run"),
    (re.compile(r"SPD", re.I), "💨", "Speed"),
    (re.compile(r"EASY", re.I), "🏃", "Easy run"),
]


# --------------------------------------------------------------------------- #
# Execution log
# --------------------------------------------------------------------------- #
def log(message: str, echo: bool = True) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if echo:
        print(line)


def load_state() -> dict:
    if not STATE.exists():
        return {"published": {}, "last_pass": None}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log(f"unreadable state ({STATE.name}), resetting")
        return {"published": {}, "last_pass": None}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def clock(seconds: float | None) -> str:
    if not seconds:
        return ""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, sec = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}'{sec:02d}"
    return f"{minutes}'{sec:02d}"


def pace(metres: float | None, seconds: float | None) -> str:
    if not metres or not seconds or metres < 200:
        return ""
    sec_km = seconds / (metres / 1000.0)
    minutes, sec = divmod(int(round(sec_km)), 60)
    return f"{minutes}:{sec:02d}"


def raw_pace(metres: float, seconds: float) -> float:
    return seconds / (metres / 1000.0)


def km(metres: float) -> str:
    return f"{metres / 1000.0:.2f} km"


def distance_label(metres: float) -> str:
    """400m, 1000m, 2000m… rounded to the nearest training distance."""
    if metres < 1200:
        rounded = int(round(metres / 100.0) * 100)
        return f"{rounded}m"
    if metres < 5000:
        rounded = int(round(metres / 500.0) * 500)
        return f"{rounded}m"
    rounded = int(round(metres / 1000.0) * 1000)
    return f"{rounded / 1000:g}km"


def duration_label(seconds: float) -> str:
    minutes = seconds / 60.0
    if abs(minutes - round(minutes)) < 0.15:
        return f"{int(round(minutes))}'"
    return clock(seconds)


# --------------------------------------------------------------------------- #
# Detecting the real structure
# --------------------------------------------------------------------------- #
# Standard middle-distance repetition distances, used to recognise intent.
STANDARD_DISTANCES = (200, 300, 400, 500, 600, 800, 1000, 1200, 1500, 2000, 3000, 5000)


def homogeneous(values: list[float], tolerance: float = 0.12) -> bool:
    if len(values) < 2:
        return True
    mean = sum(values) / len(values)
    if mean <= 0:
        return False
    return (max(values) - min(values)) / mean <= tolerance


def spread(values: list[float]) -> float:
    mean = sum(values) / len(values)
    if mean <= 0:
        return 1.0
    return (max(values) - min(values)) / mean


def roundness(value: float, standards) -> float:
    """Relative gap to the nearest round value."""
    nearest = min(standards, key=lambda s: abs(s - value))
    return abs(value - nearest) / value if value else 1.0


def duration_roundness(value: float) -> float:
    standards = [15, 20, 30, 45] + [60 * k for k in range(1, 31)]
    return roundness(value, standards)


def structure(laps: list[dict]) -> dict:
    """Reconstruct the session from the watch laps.

    Segmentation by largest pace gap: we try every cut of the form "the k
    fastest laps are the repetitions" and keep the best VALID cut. Three
    conditions, which rule out the false positives (a progressive easy run is
    not an interval session):

      1. the pace gap at the cut is at least 20 s/km;
      2. the repetitions are homogeneous in distance OR in duration (±12 %);
      3. the repetitions are not all consecutive — there must be at least one
         recovery lap between the first and the last.

    k-means fails here: on one real session a 9:08/km recovery lap pulled the
    "slow" centroid so far out that the warm-up was classified as a repetition.

    Whether the session is expressed in DISTANCE or in DURATION is not guessed
    from the plan but read from the numbers: the quantity that is both the most
    regular AND the roundest betrays the intent. 2 × 2 km gives exact distances
    (2000 m) and arbitrary durations; 3 × 8' gives the opposite.
    """
    useful = [
        lap for lap in laps
        if (lap.get("distance") or 0) >= MIN_LAP_DISTANCE_M
        and (lap.get("moving_time") or lap.get("elapsed_time") or 0) >= MIN_LAP_DURATION_S
    ]
    if len(useful) < 3:
        return {"shape": "continuous"}

    measures = []
    for index, lap in enumerate(useful):
        elapsed = lap.get("moving_time") or lap["elapsed_time"]
        measures.append({
            "index": index,
            "distance": lap["distance"],
            "time": elapsed,
            "pace": raw_pace(lap["distance"], elapsed),
            # Kept per-lap: it is the only heart rate that means anything on a
            # quality session (see hr_on_reps below).
            "hr": lap.get("average_heartrate"),
        })
    by_pace = sorted(measures, key=lambda m: m["pace"])

    best = None
    for k in range(2, len(by_pace)):
        gap = by_pace[k]["pace"] - by_pace[k - 1]["pace"]
        if gap < MIN_PACE_GAP_S:
            continue
        candidates = by_pace[:k]
        distances = [c["distance"] for c in candidates]
        durations = [c["time"] for c in candidates]
        if not homogeneous(distances) and not homogeneous(durations):
            continue
        positions = sorted(c["index"] for c in candidates)
        if positions[-1] - positions[0] == len(positions) - 1:
            continue
        if best is None or gap > best["gap"]:
            best = {"gap": gap, "reps": positions}

    if best is None:
        return {"shape": "continuous"}

    positions = best["reps"]
    reps = [measures[i] for i in positions]
    recoveries = [
        m for m in measures
        if positions[0] < m["index"] < positions[-1] and m["index"] not in positions
    ]
    recovery = (sum(r["time"] for r in recoveries) / len(recoveries)
                if recoveries else None)

    distances = [r["distance"] for r in reps]
    durations = [r["time"] for r in reps]
    mean_distance = sum(distances) / len(distances)
    mean_duration = sum(durations) / len(durations)

    # The most regular AND roundest quantity wins
    distance_score = spread(distances) + roundness(mean_distance, STANDARD_DISTANCES)
    duration_score = spread(durations) + duration_roundness(mean_duration)
    base = "distance" if distance_score <= duration_score else "duration"

    alternating = (
        base == "duration"
        and recovery is not None
        and mean_duration <= 60
        and abs(recovery - mean_duration) <= 10
        and duration_roundness(mean_duration) < 0.10
    )
    if alternating:
        # Short alternating format: "15 x 30/30", the way runners write it.
        label = f"{len(reps)} x {int(round(mean_duration))}/{int(round(recovery))}"
    elif base == "distance":
        label = f"{len(reps)} x {distance_label(mean_distance)}"
        if recovery:
            label += f" r' {duration_label(recovery)}"
    else:
        label = f"{len(reps)} x {duration_label(mean_duration)}"
        if recovery:
            label += f" r' {duration_label(recovery)}"

    return {
        "shape": "intervals",
        "base": base,
        "alternating": alternating,
        "label": label,
        "count": len(reps),
        "mean_distance": mean_distance,
        "mean_duration": mean_duration,
        "reps": [
            {"distance": r["distance"], "time": r["time"], "hr": r.get("hr")}
            for r in reps
        ],
        "recovery_s": recovery,
    }


def hr_on_reps(struct: dict) -> float | None:
    """Mean heart rate ON THE REPETITIONS, weighted by their duration.

    On a quality session, the mean heart rate of the whole activity means
    nothing. It mixes the warm-up, the recoveries and the cool-down, so it comes
    out 25 to 30 bpm below the real effort — 157 for one real session against 183
    during the repetitions. Publishing 157 for a session run at 183 is publishing
    a false number.

    Weighted by duration rather than a plain mean: on a pyramid session
    (1000-2000-1000), a repetition twice as long counts twice as much.
    """
    if struct.get("shape") != "intervals":
        return None
    measured = [r for r in struct.get("reps", []) if r.get("hr")]
    if not measured:
        return None
    total_time = sum(r["time"] for r in measured)
    if total_time <= 0:
        return None
    return sum(r["hr"] * r["time"] for r in measured) / total_time


def split_times(struct: dict) -> str:
    """Repetition split times, in the format runners actually use (11'16 - 11'07)."""
    if struct.get("shape") != "intervals":
        return ""
    if struct.get("alternating"):
        # Fifteen times "0'30" teaches nobody anything.
        return ""
    reps = struct["reps"]
    if struct["base"] == "duration":
        values = [
            f"{distance_label(rep['distance'])} @{pace(rep['distance'], rep['time'])}"
            for rep in reps
        ]
    else:
        values = [clock(rep["time"]) for rep in reps]
    if len(values) > 6:
        if values[0] == values[-1]:
            return f"{values[0]} ({len(values)} reps)"
        return f"{values[0]} → {values[-1]} ({len(values)} reps)"
    return " - ".join(values)


# --------------------------------------------------------------------------- #
# The planned session
# --------------------------------------------------------------------------- #
# Matches a programme row of plan/weeks/YYYY-Wnn.md: | **Tue 03** | code | detail |
RE_DAY_ROW = re.compile(
    r"^\|\s*\*\*[A-Za-z]{3}\s+(\d{1,2})\*\*\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|", re.M
)
RE_STRUCT = re.compile(r"(\d+)\s*[x×]\s*(\d[\d\s ]*)\s*(m\b|km\b|['’]|\bmin\b)", re.I)


def read_planned(date: dt.date) -> dict:
    """The programme row of the week sheet matching this day."""
    year, week, _ = date.isocalendar()
    sheet = WEEKS / f"{year}-W{week:02d}.md"
    if not sheet.exists():
        return {}
    text = sheet.read_text(encoding="utf-8")
    for day, code, detail in RE_DAY_ROW.findall(text):
        if int(day) != date.day:
            continue
        # A cancelled session is struck through in the sheet:
        # "~~⭐ `THR-3x8`~~ → `REC`". Without removing the struck-through part,
        # the replacement easy run inherited the "Threshold 🔥" title — which
        # really happened, on a session run at HR 132.
        active = re.sub(r"~~[^~]*~~", "", code)
        if "→" in active or "->" in active:
            active = re.split(r"→|->", active)[-1]
        return {
            "sheet": sheet.name,
            "code": active.strip() or code.strip(),
            "raw_code": code.strip(),
            "detail": re.sub(r"\s+", " ", detail).strip(),
        }
    return {}


def normalise_structure(text: str) -> dict | None:
    """"3 × 8'" or "2 x 3 000 m" -> {count, base, value}, comparable.

    `value` is in metres for a distance base, in seconds for a duration.
    """
    found = RE_STRUCT.search(text or "")
    if not found:
        return None
    count = int(found.group(1))
    raw = re.sub(r"[\s  ]", "", found.group(2))
    if not raw.isdigit():
        return None
    value = float(raw)
    unit = found.group(3).lower()
    if unit in ("'", "’", "min"):
        return {"count": count, "base": "duration", "value": value * 60}
    if unit == "km":
        return {"count": count, "base": "distance", "value": value * 1000}
    return {"count": count, "base": "distance", "value": value}


def structure_label(shape: dict) -> str:
    if shape["base"] == "duration":
        return f"{shape['count']} x {duration_label(shape['value'])}"
    return f"{shape['count']} x {distance_label(shape['value'])}"


# Patterns applied to the STRAVA NAME. Deliberately narrow: Strava names
# activities automatically ("Afternoon Run", "Course à pied de l'après-midi"),
# so a pattern containing a common word would label every session a race. The
# plan's code stays the source of truth whenever it exists.
FAMILIES_BY_NAME = [
    (re.compile(r"🏁|race|championship|marathon|\bhalf\b|\b10k\b|\b5k\b|cross",
                re.I), "🏁", "Race"),
    (re.compile(r"track|\bvo2\b|30/30|interval|reps?\b", re.I), "⚡", "Track"),
    (re.compile(r"threshold|tempo", re.I), "🔥", "Threshold"),
    (re.compile(r"hills?|hilly|elevation", re.I), "⛰️", "Hills"),
    (re.compile(r"long run|\blong\b", re.I), "⌛️", "Long run"),
]


def family(code: str, strava_name: str) -> tuple[str, str]:
    """Emoji + family label. The week sheet's code beats the Strava name."""
    if code:
        for pattern, emoji, label in FAMILIES:
            if pattern.search(code):
                return emoji, label
    if strava_name:
        for pattern, emoji, label in FAMILIES_BY_NAME:
            if pattern.search(strava_name):
                return emoji, label
    return "🏃", "Easy run"


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #
QUALITY = re.compile(r"THR|VO2|RP10|HILL|SPD|RACE", re.I)


def strip_markdown(text: str) -> str:
    """Strip Markdown from an excerpt of a week sheet."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("`", "").replace("~~", "")
    return re.sub(r"\s+", " ", text).strip()


def compose(act: dict, laps: list[dict], planned: dict) -> tuple[str, str, dict]:
    """Title (1 emoji) + description (2-3 emojis).

    Deliberate choice: the description NEVER copies the plan's text. A week sheet
    contains internal instructions (target heart rates, trade-offs, injuries)
    that have no business on a public activity. Only the facts of what was done
    go out, plus a neutral mention of the gap to plan when there is one.
    """
    struct = structure(laps)
    emoji, family_label = family(planned.get("code", ""), act.get("name", ""))

    distance = act.get("distance") or 0.0
    elapsed = act.get("moving_time") or 0.0

    # -- title: short, 1 emoji ---------------------------------------------- #
    if struct["shape"] == "intervals":
        if family_label == "Easy run" and struct["mean_distance"] > 250:
            # Clear repetitions with no known family: that is track work, not an
            # easy run. Below 250 m we keep "Easy run": those are strides at the
            # end of a session.
            emoji, family_label = "⚡", "Track"
        core = struct["label"].split(" r'")[0]
        title = f"{family_label} — {core} {emoji}"
    else:
        title = f"{family_label} {emoji}"
    if len(title) > 60:
        title = f"{family_label} {emoji}"

    # -- description --------------------------------------------------------- #
    # ⚠️ The summary line "📊 <total> @<pace> · HR <mean>" goes out ONLY on easy
    # runs. On a quality session its three numbers are misleading:
    #   - the total distance mixes warm-up, recoveries and cool-down, so it says
    #     12.51 km where the session is a 5 × 1000 m;
    #   - the average pace (5:25) matches no moment of the session;
    #   - the average HR (157) is 26 bpm below the real HR of the reps (183).
    # What replaces it: the heart rate of the repetitions, and that alone.
    lines: list[str] = []
    if struct["shape"] == "intervals":
        lines.append(struct["label"])
        detail = split_times(struct)
        if detail:
            lines.append(f"⏱️ {detail}")
        effort_hr = hr_on_reps(struct)
        if effort_hr:
            lines.append(f"❤️ HR {effort_hr:.0f} on the reps")
    else:
        lines.append("By feel")
        summary = f"📊 {km(distance)} @{pace(distance, elapsed)}/km"
        if act.get("average_heartrate"):
            summary += f" · HR {act['average_heartrate']:.0f}"
        lines.append(summary)

    # -- gap to plan, only when it is real and significant -------------------- #
    expected = normalise_structure(strip_markdown(planned.get("detail", "")))
    quality_planned = bool(QUALITY.search(planned.get("code", "")))

    gap = None
    if expected and struct["shape"] == "intervals":
        actual_value = (struct["mean_distance"] if expected["base"] == "distance"
                        else struct["mean_duration"])
        same_count = expected["count"] == struct["count"]
        same_quantity = (
            expected["base"] == struct["base"]
            and abs(actual_value - expected["value"]) / expected["value"] <= 0.10
        )
        if not (same_count and same_quantity):
            done = struct["label"].split(" r'")[0]
            gap = f"🎯 Planned {structure_label(expected)} → did {done}"
    elif expected and quality_planned:
        # "Run continuously" is only flagged when the PLANNED session was a
        # quality session: the 6 × 100 m strides of an easy run do not deserve
        # a line.
        gap = f"🎯 Planned {structure_label(expected)} → run continuously"
    if gap:
        lines.append(gap)

    return title, "\n".join(lines), struct


# --------------------------------------------------------------------------- #
# Idempotence: the source of truth is Strava, not a local file
# --------------------------------------------------------------------------- #
# The lines this script always writes act as a signature. They are what makes the
# whole thing idempotent WITHOUT shared state between machines: the work laptop
# and the home machine read the same description and reach the same conclusion.
# A local state file could never have told either what the other had already done.
#
# 🔧 IF YOU TRANSLATE OR RESTYLE THE DESCRIPTION, UPDATE THESE REGEXES IN THE
#    SAME COMMIT. Otherwise the script stops recognising its own descriptions,
#    classifies them as hand-written by the athlete — and therefore untouchable,
#    including for correcting them.
#
# Easy runs: the summary line.
RE_SIGNATURE = re.compile(r"^📊\s+\d[\d ]*\.\d{2}\s+km\s+@\d+:\d{2}/km", re.M)
# Quality sessions: the summary line is gone (it published false numbers, see
# compose), so it needed two replacements.
RE_SIGNATURE_HR = re.compile(r"^❤️\s+HR\s+\d{2,3}\s+on the reps\s*$", re.M)
# Safety net for a quality session with NO heart rate (watch without a strap):
# the split-times line is then the only one the script writes systematically.
RE_SIGNATURE_SPLITS = re.compile(r"^⏱️\s+\S", re.M)

SIGNATURES = (RE_SIGNATURE, RE_SIGNATURE_HR, RE_SIGNATURE_SPLITS)

LOCK = ROOT / "data" / "inbox" / "publish.lock"
LOCK_TTL_S = 600
# Past this, we stop re-fetching the detail of an activity whose write keeps
# failing: otherwise a missing scope burns the quota to no effect, on every
# machine, until someone reads the log.
FAILURES_BEFORE_GIVING_UP = 5


def is_signed(description: str | None) -> bool:
    """Was this description written by this script?

    Keeping every historical signature is not optional: descriptions published
    before a format change all carry the old line, and forgetting them would
    reclassify them as "written by the athlete" — hence never correctable again.
    """
    text = description or ""
    return any(pattern.search(text) for pattern in SIGNATURES)


def decision(description: str | None) -> str:
    """publish | already-done | left-to-athlete"""
    text = (description or "").strip()
    if not text:
        return "publish"
    if is_signed(text):
        return "already-done"
    return "left-to-athlete"


def take_lock() -> bool:
    """Prevent two simultaneous passes ON THIS MACHINE (an overlapping timer if a
    pass drags). The lock is local by nature: between machines, the Strava
    signature is what protects."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        age = time.time() - LOCK.stat().st_mtime
        if age < LOCK_TTL_S:
            return False
        log(f"stale lock ({age:.0f}s), resuming")
    LOCK.write_text(f"{os.getpid()} {socket.gethostname()}\n", encoding="utf-8")
    return True


def release_lock() -> None:
    LOCK.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Outgoing webhook — notify an external service on every new session
# --------------------------------------------------------------------------- #
# This is what wakes the coach agent. Three principles, in this order:
#
#   1. NEVER BLOCKING. An endpoint that is down, slow, or answering 500 must not
#      prevent a session from being imported. We log and carry on: capturing the
#      session matters more than notifying a third party.
#   2. FIRED AFTER LOGGING, not after the Strava write. The receiver wants to
#      know a session exists, including when the script did not touch its
#      description (because the athlete had written it by hand).
#   3. ONCE PER ACTIVITY. The call is remembered in the local state.
#      ⚠️ That state is LOCAL and not versioned: if the job runs on two machines,
#      each will fire once. The receiver must deduplicate on `id`, which is in
#      the body precisely for that.
WEBHOOK_TIMEOUT_S = 10


def load_webhook(env: dict[str, str]) -> dict | None:
    """Webhook configuration, or None when it is not configured."""
    url = (env.get("WEBHOOK_URL") or "").strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        log(f"WEBHOOK_URL ignored (neither http nor https): {url[:60]}")
        return None
    headers: dict[str, str] = {}
    raw = (env.get("WEBHOOK_HEADERS") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                headers = {str(k): str(v) for k, v in parsed.items()}
            else:
                log("WEBHOOK_HEADERS ignored: not a JSON object")
        except json.JSONDecodeError as error:
            log(f"WEBHOOK_HEADERS ignored (invalid JSON): {error}")
    secret = (env.get("WEBHOOK_SECRET") or "").strip()
    if not secret:
        log("⚠️ WEBHOOK_SECRET missing: calls will go out UNSIGNED")
    return {"url": url, "headers": headers, "secret": secret}


def session_summary(
    detail: dict, struct: dict, planned: dict, sheet: pathlib.Path | None,
    start: dt.datetime,
) -> str:
    """The body the agent will read: facts, in plain text, ready to reason on.

    ⛔ The plan's text does NOT go in here — same rule as for the description
    published on Strava. We give the CODE of the planned session and the PATH of
    the sheets: an agent that has the repository will read them itself, and an
    agent that does not has no business receiving internal instructions.
    """
    distance = detail.get("distance") or 0.0
    elapsed = detail.get("moving_time") or 0.0
    lines = [f"Session of {start.date().isoformat()} imported from Strava."]

    if struct.get("shape") == "intervals":
        lines.append(f"\nQuality session: {struct.get('label')}")
        detail_splits = split_times(struct)
        if detail_splits:
            lines.append(f"Splits: {detail_splits}")
        effort_hr = hr_on_reps(struct)
        if effort_hr:
            lines.append(f"Mean HR over the repetitions: {effort_hr:.0f}")
    else:
        lines.append("\nContinuous run.")
        if detail.get("average_heartrate"):
            lines.append(f"Mean HR: {detail['average_heartrate']:.0f}")

    lines.append(
        f"\nTotal volume: {km(distance)} in {clock(elapsed)} "
        f"({pace(distance, elapsed)}/km), elev+ "
        f"{detail.get('total_elevation_gain') or 0:.0f} m"
    )
    if planned.get("code"):
        lines.append(f"Planned session: {planned['code']}")
    year, week, _ = start.isocalendar()
    lines.append(
        f"\nSession sheet: {sheet.relative_to(ROOT) if sheet else '(already existed)'}"
    )
    lines.append(f"Week sheet: plan/weeks/{year}-W{week:02d}.md")
    return "\n".join(lines)


def webhook_body(
    detail: dict, struct: dict, planned: dict, sheet: pathlib.Path | None,
    start: dt.datetime,
) -> dict:
    """Payload, in the schema the AgentsRoom trigger expects.

    The endpoint expects exactly {type, title, body, author, url, id} — we stick
    to it rather than adding our own keys: an unexpected field may be rejected,
    and everything that matters fits in `body`, which is what the agent reads
    anyway.
    """
    activity_id = detail.get("id")
    day = start.strftime("%d/%m")
    if struct.get("shape") == "intervals":
        header = f"{day} — {struct.get('label')}"
    else:
        header = f"{day} — easy run {km(detail.get('distance') or 0)}"
    return {
        "type": "created",
        "title": header,
        "body": session_summary(detail, struct, planned, sheet, start),
        "author": "strava_publish",
        "url": f"https://www.strava.com/activities/{activity_id}",
        "id": str(activity_id),
    }


def notify_webhook(config, state: dict, activity_id: str, body: dict) -> None:
    """Fire the webhook once per activity. Never interrupts the pass."""
    if not config:
        return
    already = state.get("webhooks", {}).get(activity_id)
    if already and already.get("ok"):
        return
    headers = dict(config["headers"])
    # STABLE delivery id per activity, not timestamped: if the job runs on two
    # machines, both calls carry the same id and the receiver can deduplicate.
    headers.setdefault("X-AgentsRoom-Delivery", f"strava-{activity_id}")
    ok, message = post_json(
        config["url"], body, headers,
        timeout=WEBHOOK_TIMEOUT_S, secret=config["secret"],
    )
    state.setdefault("webhooks", {})[activity_id] = {
        "ok": ok,
        "message": message,
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "machine": socket.gethostname(),
    }
    if ok:
        log(f"{activity_id}: webhook notified ({message})")
    else:
        # Deliberately no immediate retry: the next pass will try again in 15
        # minutes, which is enough and avoids hammering a service that is down.
        log(f"{activity_id}: webhook FAILED — {message} (retry on the next pass)")


# --------------------------------------------------------------------------- #
# Logging to the journal + commit
# --------------------------------------------------------------------------- #
def log_session(detail: dict, laps: list[dict], shoes: str) -> pathlib.Path | None:
    """Create the log sheet if it does not exist. NEVER rewrites one.

    The content is deterministic (no execution date inside): every machine
    produces the same file, so Git merges without conflict.
    """
    start = dt.datetime.fromisoformat(detail["start_date_local"].replace("Z", ""))
    target = JOURNAL / str(start.year) / f"{start.date().isoformat()}.md"
    if target.exists():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_sheet(detail, laps, shoes), encoding="utf-8")
    return target


def git(*arguments: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *arguments], cwd=ROOT, capture_output=True, text=True
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def refresh() -> None:
    """Rebase the repository on origin/main BEFORE reading the plan.

    The week's plan is versioned: when the athlete replans a session from their
    machine, the next pass must see the new version, otherwise it compares the
    actual against a stale plan and publishes a false gap.

    A failure is never blocking: we abort the rebase and work with what we have,
    saying so in the log. A repository left mid-rebase by a background job is far
    worse than a repository that is behind.
    """
    _, before = git("rev-parse", "HEAD")
    code, output = git("pull", "--rebase", "--autostash", "origin", "main")
    if code != 0:
        last = output.splitlines()[-1] if output else "unknown cause"
        log(f"pull --rebase failed, working on the local repository: {last}")
        git("rebase", "--abort")
        return
    _, after = git("rev-parse", "HEAD")
    if after != before:
        _, head = git("log", "--oneline", "-1")
        log(f"repository refreshed from origin/main — {head}")


def commit_paths(paths: list[pathlib.Path], message: str) -> bool:
    """Commit limited to the given paths.

    NEVER `git add -A`: the repository is shared with the athlete and with
    agents, and sweeping their work in progress into an automatic commit would be
    the worst possible way to "help".
    """
    relative = [str(path.relative_to(ROOT)) for path in paths]
    code, output = git("add", "--", *relative)
    if code != 0:
        log(f"git add failed: {output}")
        return False
    code, output = git("diff", "--cached", "--quiet", "--", *relative)
    if code == 0:
        return False  # nothing to commit
    code, output = git(
        "commit", "-q", "-m",
        f"{message}\n\nAutomatic import (scripts/strava_publish.py)\n"
        f"Machine: {socket.gethostname()}",
        "--", *relative,
    )
    if code != 0:
        log(f"git commit failed: {output}")
        return False
    _, head = git("log", "--oneline", "-1")
    log(f"commit {head}")
    return True


def sync() -> None:
    """Rebase on origin then push. Optional (--push), never by default.

    Any rebase that fails is aborted: a repository left mid-rebase by a
    background job is far worse than a repository that is behind.
    """
    code, output = git("pull", "--rebase", "--autostash", "origin", "main")
    if code != 0:
        log(f"pull --rebase failed, aborting: {output.splitlines()[-1:]}")
        git("rebase", "--abort")
        return
    code, output = git("push", "origin", "main")
    log("push done" if code == 0 else f"push failed: {output}")


# --------------------------------------------------------------------------- #
# One pass
# --------------------------------------------------------------------------- #
def one_pass(strava: Strava, state: dict, args, webhook=None) -> int:
    if not args.dry_run and not args.no_pull:
        refresh()

    since = dt.datetime.now() - dt.timedelta(days=args.days)
    activities = strava.get(
        "athlete/activities", after=int(since.timestamp()), per_page=50
    )
    if not isinstance(activities, list):
        die(f"Unexpected response on athlete/activities: {activities!r}")

    handled = 0
    to_commit: list[pathlib.Path] = []
    summaries: list[str] = []

    for summary in activities:
        activity_id = str(summary.get("id"))
        if (summary.get("type") not in RUN_TYPES
                and summary.get("sport_type") not in RUN_TYPES):
            continue
        if args.force and activity_id != str(args.force):
            continue

        summary_start = dt.datetime.fromisoformat(
            summary["start_date_local"].replace("Z", "")
        )
        sheet = JOURNAL / str(summary_start.year) / f"{summary_start.date().isoformat()}.md"

        # The local state is a COST-SAVING CACHE, not the source of truth: it
        # avoids re-fetching the detail of an activity already settled. On the
        # other machine it is empty, so that machine makes the call once, reads
        # the signature and caches it in turn. Correctness therefore never
        # depends on this file — only quota consumption does.
        known = state["published"].get(activity_id)
        if not args.force and known and sheet.exists():
            if known.get("status") != "write-failed":
                continue
            if known.get("failures", 0) >= FAILURES_BEFORE_GIVING_UP:
                continue

        detail = strava.get(f"activities/{activity_id}")
        strava.sleep_between()
        verdict = "publish" if args.force else decision(detail.get("description"))

        start = dt.datetime.fromisoformat(detail["start_date_local"].replace("Z", ""))
        sheet_missing = not sheet.exists()

        if verdict != "publish" and not sheet_missing:
            # Nothing to do: nothing to write on Strava, nothing to log. Record
            # it so we stop re-fetching the detail on the next pass.
            state["published"][activity_id] = {
                "status": verdict,
                "seen": dt.datetime.now().isoformat(timespec="seconds"),
                "machine": socket.gethostname(),
            }
            continue

        laps = strava.get(f"activities/{activity_id}/laps")
        if not isinstance(laps, list):
            laps = []
        strava.sleep_between()

        shoes = ""
        gear_id = detail.get("gear_id")
        if gear_id:
            gear = strava.get(f"gear/{gear_id}")
            shoes = gear.get("name", "") if isinstance(gear, dict) else ""
            strava.sleep_between()

        planned = read_planned(start.date())
        title, description, struct = compose(detail, laps, planned)

        if args.dry_run:
            print(f"\n--- {activity_id} — {start.strftime('%Y-%m-%d %H:%M')} "
                  f"[{verdict}]")
            print(f"  planned   : {planned.get('code', '(no week sheet)')}")
            print(f"  structure : {struct.get('label', 'continuous')}")
            print(f"  journal   : {'to create' if sheet_missing else 'already there'}")
            if verdict == "publish":
                print(f"  TITLE     : {title}")
                for line in description.splitlines():
                    print(f"              {line}")
            else:
                print(f"  Strava    : left untouched ({verdict})")
            handled += 1
            continue

        # -- Strava ---------------------------------------------------------- #
        if verdict == "publish":
            try:
                strava.put(f"activities/{activity_id}",
                           name=title, description=description)
            except SystemExit as failure:
                # A refused write (missing scope, 429, network) must not prevent
                # logging: capturing the session in the journal is what matters,
                # the Strava dressing can wait for the next pass.
                previous = state["published"].get(activity_id, {})
                failures = previous.get("failures", 0) + 1
                state["published"][activity_id] = {
                    "status": "write-failed",
                    "failures": failures,
                    "reason": str(failure).splitlines()[0],
                    "seen": dt.datetime.now().isoformat(timespec="seconds"),
                    "machine": socket.gethostname(),
                }
                log(f"{activity_id}: Strava write refused "
                    f"({failures}/{FAILURES_BEFORE_GIVING_UP}) — {failure}")
                if failures >= FAILURES_BEFORE_GIVING_UP:
                    log(
                        f"{activity_id}: GIVING UP on the write after {failures} "
                        "failures. Logging continues. To resume: fix the cause then "
                        f"`python3 scripts/strava_publish.py --force {activity_id}`."
                    )
            else:
                log(f"{activity_id} published — {title} | "
                    + description.replace("\n", " ⏎ "))
                state["published"][activity_id] = {
                    "status": "published", "title": title,
                    "at": dt.datetime.now().isoformat(timespec="seconds"),
                    "machine": socket.gethostname(),
                }
        else:
            log(f"{activity_id}: Strava left as is ({verdict})")
            state["published"][activity_id] = {
                "status": verdict,
                "seen": dt.datetime.now().isoformat(timespec="seconds"),
                "machine": socket.gethostname(),
            }

        # -- journal + derived ------------------------------------------------ #
        written_sheet = log_session(detail, laps, shoes)
        if written_sheet:
            to_commit.append(written_sheet)
            summaries.append(f"{start.strftime('%d/%m')} — "
                             f"{struct.get('label') or 'easy run'}")
            log(f"{activity_id} logged in {written_sheet.relative_to(ROOT)}")
        to_commit.extend(write_activities([detail]))

        # -- webhook: after logging, so it fires even for a session whose
        #    description was hand-written and left untouched ----------------- #
        notify_webhook(
            webhook, state, activity_id,
            webhook_body(detail, struct, planned, written_sheet, start),
        )
        handled += 1

    if args.dry_run:
        return handled

    regenerate_csv()
    state["last_pass"] = dt.datetime.now().isoformat(timespec="seconds")
    save_state(state)

    if to_commit and not args.no_commit:
        commit_title = ("journal: " + " · ".join(summaries) if summaries
                        else "data: refresh Strava activities")
        if commit_paths(to_commit, commit_title) and args.push:
            sync()

    return handled


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Automatic title, description and logging of sessions.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="a single pass (default)")
    group.add_argument("--watch", action="store_true", help="endless loop")
    parser.add_argument("--interval", type=int, default=900,
                        help="seconds between passes in --watch mode (default 900)")
    parser.add_argument("--days", type=int, default=3,
                        help="catch-up window in days (default 3)")
    parser.add_argument("--dry-run", action="store_true", help="writes nothing")
    parser.add_argument("--no-commit", action="store_true",
                        help="log without committing")
    parser.add_argument("--no-pull", action="store_true",
                        help="do not rebase on origin/main before the pass")
    parser.add_argument("--push", action="store_true",
                        help="rebase on origin/main and push after commit")
    parser.add_argument("--force", metavar="ID",
                        help="republish this activity even if already signed")
    args = parser.parse_args()

    if args.interval < 300:
        die("--interval below 300 s is pointless and burns quota for nothing.")

    if not args.dry_run and not take_lock():
        log("pass skipped: another one is already running on this machine")
        return

    try:
        env = read_env()
        strava = Strava(env)
        webhook = load_webhook(env)
        if webhook:
            log(f"webhook active → {webhook['url'].split('?')[0]}"
                f"{'' if webhook['secret'] else ' (UNSIGNED)'}")
        state = load_state()

        if not args.watch:
            count = one_pass(strava, state, args, webhook)
            log(f"pass finished — {count} activity(ies) handled, "
                f"{strava.requests} request(s)", echo=not args.dry_run)
            return

        log(f"starting the loop, one pass every {args.interval} s")
        while True:
            try:
                count = one_pass(strava, state, args, webhook)
                if count:
                    log(f"pass: {count} activity(ies) handled")
            except SystemExit as exit_error:
                log(f"pass failed ({exit_error}) — retrying in {args.interval} s")
            time.sleep(args.interval)
    finally:
        if not args.dry_run:
            release_lock()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130) from None
