#!/usr/bin/env python3
"""
Incremental import of Strava sessions -> pre-filled log sheets.

    python3 scripts/strava_sync.py                 # since the latest log sheet
    python3 scripts/strava_sync.py --since 2026-08-26
    python3 scripts/strava_sync.py --dry-run       # write nothing, just list
    python3 scripts/strava_sync.py --append        # complete an existing sheet

Reads : .env (credentials) + Strava API v3
Writes: journal/YYYY/YYYY-MM-DD.md        (pre-filled sheet, never overwritten)
        data/inbox/YYYY-MM-DD-<id>.json   (raw response, not versioned)
        data/derived/activities-api.csv   (incremental aggregate, not versioned)

RULE: the script fills ONLY what Strava measured. The "Feel" and "Analysis"
sections stay empty — they belong to the athlete and the coach, not to an
automatic import. No value is estimated or rounded up.

Stdlib only (no pip install, see CLAUDE.md §8).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import re
import sys

from strava_api import ROOT, Strava, die, read_env

JOURNAL = ROOT / "journal"
INBOX = ROOT / "data" / "inbox"
DERIVED_CSV = ROOT / "data" / "derived" / "activities-api.csv"

# Strava types considered to be running
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
        "Saturday", "Sunday"]

# Keywords that betray a quality session in an activity name.
# 🔧 Add your own language's words here — the coach reads titles you wrote.
RE_QUALITY = re.compile(
    r"(\d+\s?x\s?\d|30/30|threshold|tempo|track|fartlek|interval|reps?|"
    r"vo2|hills?|specific|speed|progression|pace)",
    re.I,
)
RE_LONG = re.compile(r"(long run|\bLR\b|endurance)", re.I)


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def num(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return ""
    return f"{value:,.{decimals}f}"


def fmt_km(metres: float | None) -> str:
    if not metres:
        return ""
    return f"{metres / 1000.0:.2f} km"


def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, sec = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}'{sec:02d}"
    return f"{minutes}'{sec:02d}"


def fmt_pace(metres: float | None, seconds: float | None) -> str:
    """Pace as mm:ss/km. Empty when the distance is too short to mean anything."""
    if not metres or not seconds or metres < 200:
        return ""
    sec_per_km = seconds / (metres / 1000.0)
    minutes, sec = divmod(int(round(sec_per_km)), 60)
    return f"{minutes}:{sec:02d}"


def pace_seconds(metres: float | None, seconds: float | None) -> float | None:
    if not metres or not seconds or metres < 500:
        return None
    return seconds / (metres / 1000.0)


def parse_local(iso: str) -> dt.datetime:
    """Strava's `start_date_local` is a LOCAL time, despite its Z suffix."""
    return dt.datetime.fromisoformat(iso.replace("Z", ""))


# --------------------------------------------------------------------------- #
# Time window
# --------------------------------------------------------------------------- #
def latest_sheet() -> dt.date | None:
    dates = []
    for path in JOURNAL.glob("*/*.md"):
        match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", path.stem)
        if match:
            dates.append(dt.date(*(int(g) for g in match.groups())))
    return max(dates) if dates else None


def start_bound(since: str | None) -> dt.date:
    if since:
        try:
            return dt.date.fromisoformat(since)
        except ValueError:
            die(f"--since expects an ISO date (YYYY-MM-DD), got: {since!r}")
    latest = latest_sheet()
    if latest is None:
        die("No sheet in journal/: pass --since YYYY-MM-DD.")
    # Restart from the day of the latest sheet: already-written sessions are
    # skipped (existing sheet), but a hole in the log gets caught up.
    return latest


# --------------------------------------------------------------------------- #
# Rendering the sheet
# --------------------------------------------------------------------------- #
def proposed_type(name: str, distance_m: float) -> str:
    """Type suggestion, ALWAYS suffixed with "?": this is an inference."""
    if RE_QUALITY.search(name):
        return "`THR-…` / `VO2-…` / `RP10-…` ? *(quality keyword found in the name)*"
    if RE_LONG.search(name) or distance_m >= 20000:
        return "`LONG` ? *(to confirm)*"
    return "`EASY` ? *(to confirm)*"


def reps_block(laps: list[dict]) -> str:
    lines = [
        "| # | Distance | Time | Pace | Avg HR | Max HR | Recovery |",
        "|---|---|---|---|---|---|---|",
    ]
    for lap in laps:
        distance = lap.get("distance") or 0.0
        elapsed = lap.get("moving_time") or lap.get("elapsed_time") or 0.0
        lines.append(
            "| {idx} | {dist} | {time} | {pace} | {hr} | {hrmax} | |".format(
                idx=lap.get("lap_index", ""),
                dist=fmt_km(distance),
                time=fmt_duration(elapsed),
                pace=fmt_pace(distance, elapsed),
                hr=num(lap.get("average_heartrate"), 0),
                hrmax=num(lap.get("max_heartrate"), 0),
            )
        )
    return "\n".join(lines)


def splits_block(splits: list[dict], source: str = "Strava") -> str:
    if not splits:
        return ""
    lines = [
        "",
        f"**Kilometre splits ({source}):**",
        "",
        "| km | Time | Pace | Avg HR | Elev+ |",
        "|---|---|---|---|---|",
    ]
    for split in splits:
        distance = split.get("distance") or 0.0
        elapsed = split.get("moving_time") or split.get("elapsed_time") or 0.0
        lines.append(
            "| {idx} | {time} | {pace} | {hr} | {elev} |".format(
                idx=split.get("split", ""),
                time=fmt_duration(elapsed),
                pace=fmt_pace(distance, elapsed),
                hr=num(split.get("average_heartrate"), 0),
                elev=num(split.get("elevation_difference"), 0),
            )
        )
    return "\n".join(lines)


def render_sheet(
    act: dict,
    laps: list[dict],
    shoes: str,
    source: str = "Strava",
    notes: tuple[str, ...] = (),
) -> str:
    """Mirror of templates/session.md, filled with measured data only.

    `source` and `notes` document where the numbers came from: the TCX import
    (scripts/import_tcx.py) reuses this renderer.
    """
    start = parse_local(act["start_date_local"])
    date_iso = start.date().isoformat()
    day = DAYS[start.date().weekday()]
    iso_week = start.date().isocalendar()
    week_sheet = f"{iso_week[0]}-W{iso_week[1]:02d}"

    distance = act.get("distance") or 0.0
    elapsed = act.get("moving_time") or 0.0
    cadence = act.get("average_cadence")
    cadence_txt = ""
    if cadence:
        # Strava and Garmin both report cadence per leg: double it for real
        # steps per minute.
        cadence_txt = f"{num(cadence, 1)} (per leg) → {num(cadence * 2, 0)} spm"

    lap_detail = "\n".join(
        f"Lap {lap.get('lap_index', '?')}: {fmt_km(lap.get('distance'))} "
        f"in {fmt_duration(lap.get('moving_time') or lap.get('elapsed_time'))} "
        f"({fmt_pace(lap.get('distance'), lap.get('moving_time') or lap.get('elapsed_time'))}/km)"
        for lap in laps
    )
    note_block = ""
    if notes:
        note_block = "\n\n" + "\n".join("     " + n for n in notes)

    return f"""# {date_iso} — {day} — {act.get('name', '').strip()}

| | |
|---|---|
| **Type** | {proposed_type(act.get('name', ''), distance)} |
| **Planned** | *(copy from the week sheet)* |
| **Where** | |
| **Weather** | |
| **Shoes** | {shoes} |

## Executed

<!-- Imported from {source} — activity {act.get('id')} of {date_iso}.
     Raw lap breakdown as recorded by the watch.{note_block} -->

```
Warm-up:
Main set:
Cool-down:

Recorded laps:
{lap_detail}
```

| | Total | Distance | Time | Avg pace | Elev+ |
|---|---|---|---|---|---|
| Whole session | | {fmt_km(distance)} | {fmt_duration(elapsed)} | {fmt_pace(distance, elapsed)} /km | {num(act.get('total_elevation_gain'), 0)} m |
| Of which effort | | | | | |

**Repetitions:**

{reps_block(laps)}
{splits_block(act.get('splits_metric') or [], source)}

## Physiology

| | |
|---|---|
| Average HR | {num(act.get('average_heartrate'), 0)} |
| Max HR | {num(act.get('max_heartrate'), 0)} |
| Average cadence | {cadence_txt} |
| Average power | {num(act.get('average_watts'), 0)} |
| Relative effort ({source}) | {num(act.get('suffer_score'), 0)} |

## Feel

**Effort rating (RPE 1-10):** /10
**Freshness at the start (1-5):** /5
**Sleep the night before:** h — quality:

<!-- For the athlete to fill: sensations, pain, context. Not importable. -->

## Analysis

**Verdict:** ✅ on target / ⚠️ above / 🔻 below — gap of … s/km

<!-- For the coach to fill: comparison against the target paces in
     athlete/zones-and-paces.md and against the planned session. -->

**Consequence for what follows:**

---

*Week sheet: [`../../plan/weeks/{week_sheet}.md`](../../plan/weeks/{week_sheet}.md)*
"""


# --------------------------------------------------------------------------- #
# Incremental CSV aggregate
# --------------------------------------------------------------------------- #
COLUMNS = [
    "date", "id", "type", "name", "dist_km", "time_s", "pace_s_km",
    "elev_m", "hr_avg", "hr_max", "relative_effort",
]

ACTIVITIES_DIR = ROOT / "data" / "derived" / "activities"


def activity_row(act: dict) -> dict[str, str]:
    distance = act.get("distance") or 0.0
    elapsed = act.get("moving_time") or 0.0
    pace = pace_seconds(distance, elapsed)
    return {
        "date": parse_local(act["start_date_local"]).isoformat(timespec="seconds"),
        "id": str(act.get("id")),
        "type": act.get("type", ""),
        "name": (act.get("name") or "").replace("\n", " ").strip(),
        "dist_km": f"{distance / 1000.0:.3f}",
        "time_s": f"{int(elapsed)}",
        "pace_s_km": f"{pace:.1f}" if pace else "",
        "elev_m": f"{act.get('total_elevation_gain') or 0:.0f}",
        "hr_avg": f"{act['average_heartrate']:.0f}" if act.get("average_heartrate") else "",
        "hr_max": f"{act['max_heartrate']:.0f}" if act.get("max_heartrate") else "",
        "relative_effort": f"{act['suffer_score']:.0f}" if act.get("suffer_score") else "",
    }


def write_activities(activities: list[dict]) -> list[pathlib.Path]:
    """One JSON file per activity, under data/derived/activities/.

    ONE FILE PER ACTIVITY rather than a shared CSV: two machines importing
    different sessions add different files, which Git merges without conflict.
    A shared CSV would make the same lines diverge.
    """
    ACTIVITIES_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for act in activities:
        row = activity_row(act)
        day = row["date"][:10]
        target = ACTIVITIES_DIR / f"{day}-{row['id']}.json"
        content = json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if target.exists() and target.read_text(encoding="utf-8") == content:
            continue
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written


def regenerate_csv() -> int:
    """Rebuild the local CSV aggregate from the per-activity files.

    This CSV is NOT versioned (see .gitignore): it is derived, therefore
    regenerable, and versioning it would bring back the very conflicts that
    splitting per file avoids.
    """
    rows = []
    if ACTIVITIES_DIR.exists():
        for path in sorted(ACTIVITIES_DIR.glob("*.json")):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
    DERIVED_CSV.parent.mkdir(parents=True, exist_ok=True)
    # lineterminator="\n" is not cosmetic: the csv module writes CRLF by default,
    # and with core.autocrlf=input the file looks clean on one machine and
    # modified on another — dozens of lines of phantom diff per CSV.
    with DERIVED_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in sorted(rows, key=lambda r: r.get("date", "")):
            writer.writerow({k: row.get(k, "") for k in COLUMNS})
    return len(rows)


# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Incremental import of Strava sessions into journal/.",
    )
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="start date (default: day of the latest log sheet)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list without writing anything")
    parser.add_argument("--append", action="store_true",
                        help="complete an existing sheet instead of skipping it")
    parser.add_argument("--all-types", action="store_true",
                        help="do not filter on running")
    args = parser.parse_args()

    start = start_bound(args.since)
    after = int(dt.datetime.combine(start, dt.time.min).timestamp())

    strava = Strava(read_env())
    print(f"\nWindow: from {start.isoformat()} 00:00 (local)")
    strava.authenticate()

    # -- list the activities ------------------------------------------------ #
    activities: list[dict] = []
    page = 1
    while True:
        batch = strava.get("athlete/activities", after=after, per_page=100, page=page)
        if not isinstance(batch, list):
            die(f"Unexpected response on athlete/activities: {batch!r}")
        activities.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        strava.sleep_between()

    runs = [
        a for a in activities
        if args.all_types or a.get("type") in RUN_TYPES or a.get("sport_type") in RUN_TYPES
    ]
    skipped = len(activities) - len(runs)
    print(f"{len(activities)} activity(ies) found, {len(runs)} kept"
          + (f", {skipped} non-running ignored" if skipped else ""))

    if not runs:
        print("\nNothing to import.\n")
        return

    # -- detail, laps, sheet ------------------------------------------------ #
    gear_cache: dict[str, str] = {}
    created, completed, skipped_sheets = [], [], []
    details: list[dict] = []

    for summary in sorted(runs, key=lambda a: a["start_date_local"]):
        activity_id = summary["id"]
        detail = strava.get(f"activities/{activity_id}", include_all_efforts="false")
        strava.sleep_between()
        laps = strava.get(f"activities/{activity_id}/laps")
        if not isinstance(laps, list):
            laps = []
        strava.sleep_between()
        details.append(detail)

        shoes = ""
        gear_id = detail.get("gear_id")
        if gear_id:
            if gear_id not in gear_cache:
                gear = strava.get(f"gear/{gear_id}")
                gear_cache[gear_id] = (gear or {}).get("name", "") if isinstance(gear, dict) else ""
                strava.sleep_between()
            shoes = gear_cache[gear_id]

        started = parse_local(detail["start_date_local"])
        date_iso = started.date().isoformat()
        target = JOURNAL / str(started.year) / f"{date_iso}.md"
        content = render_sheet(detail, laps, shoes)

        label = (f"{date_iso} — {detail.get('name', '').strip()} — "
                 f"{fmt_km(detail.get('distance'))} in {fmt_duration(detail.get('moving_time'))}")

        if args.dry_run:
            status = "WOULD COMPLETE" if target.exists() and args.append else (
                "WOULD SKIP (sheet exists)" if target.exists() else "WOULD CREATE")
            print(f"  [{status}] {target.relative_to(ROOT)}: {label}")
            continue

        INBOX.mkdir(parents=True, exist_ok=True)
        (INBOX / f"{date_iso}-{activity_id}.json").write_text(
            json.dumps({"activity": detail, "laps": laps}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not args.append:
                skipped_sheets.append((target, label))
                continue
            with target.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\n\n---\n\n## Strava data imported on "
                    f"{dt.date.today().isoformat()} (activity {activity_id})\n\n"
                    + content.split("## Executed", 1)[1].split("## Feel", 1)[0]
                )
            completed.append((target, label))
        else:
            target.write_text(content, encoding="utf-8")
            created.append((target, label))

    if args.dry_run:
        print("\n--dry-run: no file written.\n")
        return

    activity_files = write_activities(details)
    csv_total = regenerate_csv()

    # -- report -------------------------------------------------------------- #
    print()
    for title, batch in (("Sheets created", created),
                         ("Sheets completed", completed),
                         ("Ignored (sheet already written)", skipped_sheets)):
        if batch:
            print(f"{title}:")
            for path, label in batch:
                print(f"  {path.relative_to(ROOT)}  {label}")
            print()

    if skipped_sheets and not args.append:
        print("A sheet already written is never overwritten (journal/README.md, rule 3).")
        print("To append the Strava data at the end of the sheet: --append\n")

    print(f"data/derived/activities/: {len(activity_files)} file(s) written")
    print(f"data/derived/activities-api.csv regenerated: {csv_total} activity(ies)")
    print(f"API requests: {strava.requests}"
          + (f" — usage {strava.usage.get('X-ReadRateLimit-Usage', '?')}"
             f" / {strava.usage.get('X-ReadRateLimit-Limit', '?')}" if strava.usage else ""))
    print("\nNext: read each sheet, fill in \"Feel\", then hand it to the coach")
    print("for the analysis and the week adjustment.\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130) from None
