#!/usr/bin/env python3
"""Vendor a vehicle's OBDb signalset into the kit, at imaging time.

OBDb (https://github.com/OBDb) publishes one repo per vehicle describing that
model's diagnostic commands. Devices go out to participants without a
guaranteed network, so the signalset is downloaded once here and committed
alongside the image rather than fetched at runtime.

    scripts/fetch_signalset.py --list ioniq
    scripts/fetch_signalset.py Hyundai-IONIQ-5 --year 2024
    scripts/fetch_signalset.py Hyundai-IONIQ-5 --year 2024 --summary

Then point config.yaml at the result:

    vehicle:
      signalset: vehicles/Hyundai-IONIQ-5.json
      make_model: Hyundai-IONIQ-5
      year: 2024
"""

import argparse
import collections
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW = "https://raw.githubusercontent.com/OBDb/{name}/main/signalsets/v3/default.json"
GENERATIONS = "https://raw.githubusercontent.com/OBDb/{name}/main/generations.yaml"
SEARCH = "https://api.github.com/search/repositories?q=org:OBDb+{q}+in:name&per_page=40"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "obd-ev/fetch-signalset"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def fetch_years(name: str) -> dict:
    """Vendor the model years from the vehicle's generations.yaml.

    Kept as a compact JSON sidecar so `select_vehicle.py` can offer a real
    year list instead of asking someone to type one. Missing upstream data is
    not fatal -- the picker just accepts any year.
    """
    meta = {"make_model": name, "years": [], "generations": []}
    try:
        raw = _get(GENERATIONS.format(name=name)).decode()
    except Exception as exc:
        print(f"  (no generations.yaml: {exc})")
        return meta
    try:
        import yaml
        doc = yaml.safe_load(raw) or {}
    except ImportError:
        # setup_kit.sh runs the vehicle picker before the venv (and PyYAML)
        # exist. generations.yaml is flat enough to read without a parser.
        doc = _generations_without_yaml(raw)
    except Exception as exc:
        print(f"  (could not parse generations.yaml: {exc})")
        return meta

    import datetime
    horizon = datetime.date.today().year + 1
    years = set()
    for gen in doc.get("generations") or []:
        start = gen.get("start_year")
        if not isinstance(start, int):
            continue
        end = gen.get("end_year")
        if not isinstance(end, int):
            end = horizon            # still in production
        for y in range(start, min(end, horizon) + 1):
            years.add(y)
        meta["generations"].append({
            "name": gen.get("name", ""),
            "start_year": start,
            "end_year": gen.get("end_year"),
        })
    meta["years"] = sorted(years)
    return meta


def _generations_without_yaml(raw: str) -> dict:
    """Minimal reader for OBDb's generations.yaml when PyYAML is absent:
    a list of `- name:` / `start_year:` / `end_year:` blocks."""
    import re
    gens, current = [], None
    for line in raw.splitlines():
        if re.match(r"^\s*-\s", line):
            current = {}
            gens.append(current)
            line = re.sub(r"^\s*-\s", "", line)
        m = re.match(r"^\s*(name|start_year|end_year)\s*:\s*(.*?)\s*$", line)
        if m and current is not None:
            key, value = m.group(1), m.group(2).strip("'\"")
            if key.endswith("_year"):
                current[key] = int(value) if value.isdigit() else None
            else:
                current[key] = value
    return {"generations": gens}


def list_vehicles(query: str) -> int:
    try:
        data = json.loads(_get(SEARCH.format(q=urllib.request.quote(query))))
    except urllib.error.URLError as exc:
        print(f"search failed: {exc}", file=sys.stderr)
        return 1
    items = data.get("items", [])
    if not items:
        print(f"no OBDb vehicle matching {query!r}")
        return 1
    for item in items:
        print(f"  {item['name']}")
    return 0


def summarize(signalset: dict, year: int | None) -> None:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from obd_ev.obdb import SignalSet

    ss = SignalSet.from_json(signalset).for_year(year)
    by_path: dict = collections.defaultdict(list)
    for command in ss.commands:
        for signal in command.signals:
            by_path[signal.path.split(".")[0] or "(none)"].append(signal)

    total = sum(len(v) for v in by_path.values())
    print(f"\n{len(ss.commands)} commands, {total} signals"
          + (f" for model year {year}" if year else " (all model years)"))
    for path in sorted(by_path, key=lambda p: -len(by_path[p])):
        names = by_path[path]
        sample = ", ".join(s.name or s.id for s in names[:3])
        print(f"  {len(names):>4}  {path:<14} {sample}"
              + ("..." if len(names) > 3 else ""))

    fastest = sorted((c for c in ss.commands if c.freq), key=lambda c: c.freq)
    if fastest:
        print(f"\n  fastest command: {fastest[0].request()} every {fastest[0].freq}s")
    print("\n  Not every command is answered by every trim -- unanswered ones are")
    print("  retired automatically after a few attempts on the first drive.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("vehicle", nargs="?",
                        help="OBDb repo name, e.g. Hyundai-IONIQ-5")
    parser.add_argument("--list", metavar="QUERY",
                        help="search OBDb for matching vehicles and exit")
    parser.add_argument("--year", type=int, help="model year, for the summary")
    parser.add_argument("--summary", action="store_true",
                        help="print what would be collected")
    parser.add_argument("--out", default=None,
                        help="output path (default: vehicles/<vehicle>.json)")
    args = parser.parse_args()

    if args.list:
        return list_vehicles(args.list)
    if not args.vehicle:
        parser.error("give a vehicle name, or --list QUERY to search")

    url = RAW.format(name=args.vehicle)
    try:
        body = _get(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"no signalset for {args.vehicle!r}. Try:\n"
                  f"  {sys.argv[0]} --list {args.vehicle.split('-')[0]}",
                  file=sys.stderr)
            return 1
        raise

    signalset = json.loads(body)
    if not signalset.get("commands"):
        print(f"{args.vehicle} exists upstream but has no commands yet -- the "
              f"vehicle is not reverse-engineered. Fall back to generic Mode 01 "
              f"by leaving vehicle.signalset unset.", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else REPO_ROOT / "vehicles" / f"{args.vehicle}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(signalset, indent=1) + "\n")
    print(f"wrote {out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}"
          f" ({len(body)} bytes) from {url}")

    meta = fetch_years(args.vehicle)
    meta_path = out.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, indent=1) + "\n")
    if meta["years"]:
        print(f"wrote {meta_path.name}: model years "
              f"{meta['years'][0]}-{meta['years'][-1]}")

    if args.summary or args.year:
        summarize(signalset, args.year)
    return 0


if __name__ == "__main__":
    sys.exit(main())
