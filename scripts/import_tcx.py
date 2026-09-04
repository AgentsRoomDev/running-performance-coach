#!/usr/bin/env python3
"""
Import a session from a TCX file -> pre-filled log sheet.

    python3 scripts/import_tcx.py ~/Downloads/activity_1234.tcx
    python3 scripts/import_tcx.py data/inbox/*.tcx --name "Track — 6 x 1000 m"
    python3 scripts/import_tcx.py file.tcx --dry-run

The route with NO API and NO subscription: since 30 June 2026 the Strava API
requires a paid subscription on the account that owns the application. The TCX
exported from your watch platform (activity -> gear icon -> "Export to TCX")
contains the same laps as the watch, hence the full "Repetitions" table.

Writes: journal/YYYY/YYYY-MM-DD.md  (never overwritten; --append to complete)

RULE: only measured values are written. Anything computed from the trace
(elevation gain, kilometre splits) is flagged as such in the sheet. The "Feel"
and "Analysis" sections stay empty.

Stdlib only (no pip install, see CLAUDE.md §8).
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
import xml.etree.ElementTree as ET

from strava_api import ROOT, die
from strava_sync import JOURNAL, fmt_duration, fmt_km, render_sheet

# Threshold below which an altitude change counts as GPS noise rather than climb.
ELEVATION_NOISE_M = 0.5


# --------------------------------------------------------------------------- #
# XML reading
# --------------------------------------------------------------------------- #
def text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    return found.text if found is not None and found.text else None


def number(node: ET.Element | None, path: str) -> float | None:
    value = text(node, path)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_instant(value: str) -> dt.datetime:
    """TCX timestamps are UTC (Z suffix)."""
    cleaned = value.strip().replace("Z", "+00:00")
    return dt.datetime.fromisoformat(cleaned)


# --------------------------------------------------------------------------- #
# The trace
# --------------------------------------------------------------------------- #
def read_trackpoints(activity: ET.Element) -> list[dict]:
    points = []
    for point in activity.iterfind(".//{*}Trackpoint"):
        instant = text(point, "{*}Time")
        if not instant:
            continue
        points.append({
            "t": parse_instant(instant),
            "d": number(point, "{*}DistanceMeters"),
            "alt": number(point, "{*}AltitudeMeters"),
            "hr": number(point, "{*}HeartRateBpm/{*}Value"),
            "cad": number(point, "{*}Extensions/{*}TPX/{*}RunCadence"),
        })
    return points


def elevation_gain(points: list[dict]) -> float | None:
    """Cumulative climb, ignoring changes smaller than the GPS noise floor.

    Without that floor, a flat track session reports tens of metres of climb:
    barometric and GPS altitude both jitter by a few decimetres continuously.
    """
    altitudes = [p["alt"] for p in points if p["alt"] is not None]
    if len(altitudes) < 2:
        return None
    total = 0.0
    reference = altitudes[0]
    for altitude in altitudes[1:]:
        delta = altitude - reference
        if delta > ELEVATION_NOISE_M:
            total += delta
            reference = altitude
        elif delta < -ELEVATION_NOISE_M:
            reference = altitude
    return total


def kilometre_splits(points: list[dict]) -> list[dict]:
    """Per-km splits rebuilt from the trace (cumulative distance + time)."""
    useful = [p for p in points if p["d"] is not None]
    if len(useful) < 2:
        return []

    splits: list[dict] = []
    boundary = 1000.0
    start_t = useful[0]["t"]
    start_index = 0
    for index, point in enumerate(useful):
        if point["d"] < boundary:
            continue
        samples = [p["hr"] for p in useful[start_index:index + 1] if p["hr"] is not None]
        altitudes = [p["alt"] for p in useful[start_index:index + 1] if p["alt"] is not None]
        splits.append({
            "split": len(splits) + 1,
            "distance": 1000.0,
            "moving_time": (point["t"] - start_t).total_seconds(),
            "average_heartrate": sum(samples) / len(samples) if samples else None,
            "elevation_difference": (altitudes[-1] - altitudes[0]) if len(altitudes) > 1 else None,
        })
        boundary += 1000.0
        start_t = point["t"]
        start_index = index
    return splits


# --------------------------------------------------------------------------- #
# TCX -> the shared structure (the Strava API's own)
# --------------------------------------------------------------------------- #
def read_tcx(path: pathlib.Path, name: str | None) -> tuple[dict, list[dict]]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        die(f"{path.name} is not valid XML: {error}")

    activity = root.find(".//{*}Activity")
    if activity is None:
        die(f"{path.name} contains no <Activity> tag: is it really a TCX?")

    identifier = text(activity, "{*}Id") or ""
    if not identifier:
        die(f"{path.name}: <Id> missing, cannot date the session.")

    # The TCX Id is UTC and the format does not carry the session's timezone:
    # convert into this machine's timezone.
    local_start = parse_instant(identifier).astimezone()

    laps: list[dict] = []
    for index, lap in enumerate(activity.iterfind("{*}Lap"), start=1):
        duration = number(lap, "{*}TotalTimeSeconds") or 0.0
        laps.append({
            "lap_index": index,
            "distance": number(lap, "{*}DistanceMeters") or 0.0,
            "moving_time": duration,
            "elapsed_time": duration,
            "average_heartrate": number(lap, "{*}AverageHeartRateBpm/{*}Value"),
            "max_heartrate": number(lap, "{*}MaximumHeartRateBpm/{*}Value"),
            "average_cadence": number(lap, "{*}Extensions/{*}LX/{*}AvgRunCadence"),
            "intensity": text(lap, "{*}Intensity") or "",
        })
    if not laps:
        die(f"{path.name}: no <Lap> found.")

    points = read_trackpoints(activity)
    heart_rates = [p["hr"] for p in points if p["hr"]]
    cadences = [p["cad"] for p in points if p["cad"]]
    device = text(activity, "{*}Creator/{*}Name") or ""

    lap_max_hr = [lap["max_heartrate"] for lap in laps if lap.get("max_heartrate")]
    max_hr = max([*(heart_rates or []), *lap_max_hr], default=None)

    act = {
        "id": identifier,
        "name": (name or "").strip(),
        "type": activity.get("Sport", ""),
        "start_date_local": local_start.replace(tzinfo=None).isoformat(),
        "distance": sum(lap["distance"] for lap in laps),
        "moving_time": sum(lap["moving_time"] for lap in laps),
        "total_elevation_gain": elevation_gain(points),
        "average_heartrate": (sum(heart_rates) / len(heart_rates)) if heart_rates else None,
        "max_heartrate": max_hr,
        "average_cadence": (sum(cadences) / len(cadences)) if cadences else None,
        "average_watts": None,
        "suffer_score": None,
        "splits_metric": kilometre_splits(points),
        "_device": device,
        "_points": len(points),
    }
    return act, laps


# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a session from a TCX file into journal/.",
    )
    parser.add_argument("files", nargs="+", metavar="FILE.tcx")
    parser.add_argument("--name", help="session title (a TCX carries none)")
    parser.add_argument("--shoes", default="", help="shoe model")
    parser.add_argument("--dry-run", action="store_true", help="list without writing")
    parser.add_argument("--append", action="store_true",
                        help="complete an existing sheet instead of skipping it")
    args = parser.parse_args()

    for raw in args.files:
        path = pathlib.Path(raw).expanduser()
        if not path.exists():
            die(f"{path} not found.")

        act, laps = read_tcx(path, args.name)
        start = dt.datetime.fromisoformat(act["start_date_local"])
        date_iso = start.date().isoformat()
        target = JOURNAL / str(start.year) / f"{date_iso}.md"

        notes = [
            f"Source: {path.name}"
            + (f", recorded by {act['_device']}" if act["_device"] else "")
            + f" ({act['_points']} trace points).",
            "Elevation gain and kilometre splits COMPUTED from the trace, not measured.",
        ]
        content = render_sheet(
            act, laps, args.shoes,
            source="TCX", notes=tuple(notes),
        )

        summary = (f"{start.strftime('%Y-%m-%d %H:%M')} — {fmt_km(act['distance'])} "
                   f"in {fmt_duration(act['moving_time'])} — {len(laps)} lap(s)")

        if args.dry_run:
            status = "WOULD COMPLETE" if target.exists() and args.append else (
                "WOULD SKIP (sheet exists)" if target.exists() else "WOULD CREATE")
            print(f"  [{status}] {target.relative_to(ROOT)}: {summary}")
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not args.append:
                print(f"  [SKIPPED] {target.relative_to(ROOT)} already exists: {summary}")
                print("            A written sheet is never overwritten "
                      "(journal/README.md, rule 3). Use --append.")
                continue
            with target.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\n\n---\n\n## TCX data imported on "
                    f"{dt.date.today().isoformat()} ({path.name})\n\n"
                    + content.split("## Executed", 1)[1].split("## Feel", 1)[0]
                )
            print(f"  [COMPLETED] {target.relative_to(ROOT)}: {summary}")
        else:
            target.write_text(content, encoding="utf-8")
            print(f"  [CREATED] {target.relative_to(ROOT)}: {summary}")

    if args.dry_run:
        print("\n--dry-run: no file written.\n")
        return
    print("\nNext: read the sheet, fill in \"Feel\", then hand it to the coach.\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130) from None
