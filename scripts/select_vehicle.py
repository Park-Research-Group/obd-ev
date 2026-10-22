#!/usr/bin/env python3
"""Choose this kit's vehicle from the profiles on the image, and record it.

Typing `OBD_EV_VEHICLE` by hand is the riskiest step in per-kit setup, because
a wrong name fails *silently*: the logger falls back to generic Mode 01 and the
kit collects a tenth of the data it should, with nothing obviously broken until
the study is over. Picking from a list removes the possibility.

    scripts/select_vehicle.py                       # profiles on this image
    scripts/select_vehicle.py --all                 # every known vehicle
    scripts/select_vehicle.py --search bolt         # search the catalogue
    scripts/select_vehicle.py --list                # show what's on this image
    scripts/select_vehicle.py --set Chevrolet-Bolt-EUV --year 2023
    scripts/select_vehicle.py --none                # generic Mode 01

`--all` and `--search` read vehicles/index.json, the catalogue of every OBDb
vehicle that actually has data. Picking one that is not on the image downloads
it first, so imaging needs a network connection for that step only.

Writes OBD_EV_VEHICLE and OBD_EV_VEHICLE_YEAR into /etc/default/obd-ev,
leaving every other line of that file untouched.
"""

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VEHICLE_DIR = REPO_ROOT / "vehicles"
INDEX_PATH = VEHICLE_DIR / "index.json"
ENV_FILE = Path(os.environ.get("OBD_EV_ENV_FILE", "/etc/default/obd-ev"))

sys.path.insert(0, str(REPO_ROOT / "src"))


class Profile:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem
        meta_path = path.with_suffix(".meta.json")
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except ValueError:
                meta = {}
        self.years = meta.get("years") or []
        self.generations = meta.get("generations") or []
        self.local = True
        self._signalset = None

    def signalset(self, year=None):
        from obd_ev.obdb import SignalSet
        if self._signalset is None:
            self._signalset = SignalSet.load(self.path)
        return self._signalset.for_year(year)

    def summary(self, year=None):
        ss = self.signalset(year)
        signals = [s for c in ss.commands for s in c.signals]
        groups = {}
        for s in signals:
            groups[s.path.split(".")[0] or "-"] = \
                groups.get(s.path.split(".")[0] or "-", 0) + 1
        return len(ss.commands), len(signals), groups

    @property
    def year_label(self):
        if not self.years:
            return "any year"
        return f"{self.years[0]}-{self.years[-1]}"


class Catalogued:
    """A vehicle in the OBDb catalogue that is not on this image yet."""

    def __init__(self, entry):
        self.name = entry["name"]
        self.years = entry.get("years") or []
        self.commands = entry.get("commands", 0)
        self.signals = entry.get("signals", 0)
        self.groups = entry.get("groups") or {}
        self.local = False

    @property
    def year_label(self):
        if not self.years:
            return "any year"
        return f"{self.years[0]}-{self.years[-1]}"

    def summary(self, year=None):
        return self.commands, self.signals, self.groups


def load_index():
    if not INDEX_PATH.exists():
        return []
    try:
        return json.loads(INDEX_PATH.read_text()).get("vehicles", [])
    except ValueError:
        return []


def catalogue(query, installed_names):
    entries = load_index()
    if not entries:
        return []
    if query:
        q = query.lower()
        entries = [e for e in entries if q in e["name"].lower()]
    out = []
    for entry in entries:
        if entry["name"] in installed_names:
            continue
        out.append(Catalogued(entry))
    return out


def download(name):
    """Fetch a profile onto this image via the existing fetch script."""
    print(f"\nDownloading {name}...")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "fetch_signalset.py"), name])
    if result.returncode != 0:
        print(f"Could not download {name}.", file=sys.stderr)
        return None
    path = VEHICLE_DIR / f"{name}.json"
    return Profile(path) if path.exists() else None


# vehicles/ holds signalsets alongside their sidecars and the catalogue; only
# the signalsets are selectable.
NON_PROFILE_FILES = {"index.json"}


SIDECAR_SUFFIXES = (".meta.json", ".collect.json")


def is_profile(path: Path) -> bool:
    return (path.name not in NON_PROFILE_FILES
            and not path.name.endswith(SIDECAR_SUFFIXES))


def discover():
    if not VEHICLE_DIR.exists():
        return []
    return sorted((Profile(p) for p in VEHICLE_DIR.glob("*.json")
                   if is_profile(p)),
                  key=lambda p: p.name)


def show(profiles, heading="Vehicle profiles on this image", searchable=False):
    if not profiles:
        print("No vehicle profiles on this image.\n"
              "Browse the catalogue:  scripts/select_vehicle.py --all")
        return
    print(f"\n{heading}:\n")
    for i, p in enumerate(profiles, 1):
        commands, signals, _ = p.summary()
        tag = "" if getattr(p, "local", True) else "  (will be downloaded)"
        print(f"  {i}) {p.name:<30} {signals:>5} signals  "
              f"{commands:>4} commands  {p.year_label}{tag}")
    print("  0) (no profile)                  generic Mode 01, works on any "
          "OBD-II vehicle")
    if searchable:
        catalogue_size = len(load_index())
        if catalogue_size:
            print(f"  s) search all {catalogue_size} known vehicles")


def ask_year(profile):
    if not profile.years:
        raw = input("Model year (blank to skip): ").strip()
        return int(raw) if raw.isdigit() else None
    if len(profile.years) == 1:
        print(f"Model year: {profile.years[0]} (only year for this profile)")
        return profile.years[0]
    options = ", ".join(str(y) for y in profile.years)
    while True:
        raw = input(f"Model year [{options}]: ").strip()
        if raw.isdigit() and int(raw) in profile.years:
            return int(raw)
        if raw.isdigit():
            confirm = input(f"  {raw} is outside the known range for this "
                            f"model. Use it anyway? [y/N] ").strip().lower()
            if confirm == "y":
                return int(raw)
            continue
        print("  Enter one of the listed years.")


def selected_counts(profile, year):
    """What the kit will actually poll: the profile narrowed by the signal
    selection that ships beside it. Without this the picker quotes the whole
    signalset -- 292 commands on the Bolt, where the kit sends 15 -- and
    warns about a BLE link that is never asked to carry them.

    Read with json rather than obd_ev.config: this runs before the venv
    exists, on whatever python3 the image came with.
    """
    collect = profile.path.with_suffix(".collect.json")
    if not collect.exists():
        return None
    try:
        doc = json.loads(collect.read_text())
    except ValueError:
        return None
    include = [p for p in doc.get("include_signals") or [] if isinstance(p, str)]
    exclude = [p for p in doc.get("exclude_signals") or [] if isinstance(p, str)]
    if not include and not exclude:
        return None

    def wanted(signal_id):
        if any(fnmatch.fnmatchcase(signal_id, p) for p in exclude):
            return False
        return not include or any(fnmatch.fnmatchcase(signal_id, p)
                                  for p in include)

    commands = signals = 0
    for command in profile.signalset(year).commands:
        kept = [s for s in command.signals if wanted(s.id)]
        if kept:
            commands += 1
            signals += len(kept)
    return commands, signals


def describe(profile, year):
    commands, signals, groups = profile.summary(year)
    selected = selected_counts(profile, year)
    if selected:
        polled_commands, polled_signals = selected
        print(f"\nThis kit will collect {polled_signals} signals from "
              f"{polled_commands} commands"
              f"{f' for model year {year}' if year else ''} -- the selection "
              f"in {profile.path.stem}.collect.json,")
        print(f"out of {signals} signals and {commands} commands this profile "
              f"describes:")
    else:
        polled_commands = commands
        print(f"\nThis kit will collect {signals} signals from {commands} commands"
              f"{f' for model year {year}' if year else ''}:")
    for group, n in sorted(groups.items(), key=lambda kv: -kv[1])[:8]:
        print(f"    {n:>4}  {group}")
    if polled_commands > 40:
        print(f"\n  Note: {polled_commands} commands is a lot for a BLE adapter"
              f" -- each\n  one is a round trip. If the achieved row rate is too"
              f" low,\n  list the signals you need in vehicle.include_signals in"
              f"\n  config.yaml (see docs/deployment.md).")


def read_env_lines():
    """The env file is root-owned; fall back to sudo rather than crashing."""
    if not ENV_FILE.exists():
        return None
    try:
        return ENV_FILE.read_text().splitlines()
    except PermissionError:
        result = subprocess.run(["sudo", "cat", str(ENV_FILE)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.splitlines()
        print(f"warning: cannot read {ENV_FILE}; other settings in it would be "
              f"lost, so refusing to rewrite it", file=sys.stderr)
        raise SystemExit(1)


def write_env(vehicle, year):
    """Update the two keys in place, preserving the rest of the file."""
    lines = read_env_lines()
    if lines is None:
        print(f"note: {ENV_FILE} does not exist yet; creating it")
        lines = []

    wanted = {}
    if vehicle:
        wanted["OBD_EV_VEHICLE"] = vehicle
        if year:
            wanted["OBD_EV_VEHICLE_YEAR"] = str(year)

    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in ("OBD_EV_VEHICLE", "OBD_EV_VEHICLE_YEAR"):
            if key in wanted:
                out.append(f"{key}={wanted[key]}")
                seen.add(key)
            # dropping the line is how "no profile" is expressed
            continue
        out.append(line)
    for key, value in wanted.items():
        if key not in seen:
            out.append(f"{key}={value}")

    body = "\n".join(out).rstrip("\n") + "\n"
    try:
        ENV_FILE.write_text(body)
    except PermissionError:
        subprocess.run(["sudo", "tee", str(ENV_FILE)], input=body, text=True,
                       stdout=subprocess.DEVNULL, check=True)
        # Leave it readable by the tooling that has to consult it later.
        group = subprocess.run(["id", "-gn"], capture_output=True,
                               text=True).stdout.strip()
        if group:
            subprocess.run(["sudo", "chown", f"root:{group}", str(ENV_FILE)])
        subprocess.run(["sudo", "chmod", "640", str(ENV_FILE)])
    print(f"\nWrote {ENV_FILE}:")
    for key in ("OBD_EV_VEHICLE", "OBD_EV_VEHICLE_YEAR"):
        value = wanted.get(key)
        print(f"  {key}={value}" if value else f"  {key} (unset)")
    print("\nRestart to apply:  sudo systemctl restart obd-ev")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true",
                        help="show available profiles and exit")
    parser.add_argument("--all", action="store_true",
                        help="include every vehicle in the OBDb catalogue")
    parser.add_argument("--search", metavar="TERM",
                        help="search the catalogue (implies --all)")
    parser.add_argument("--set", metavar="NAME",
                        help="select this profile without prompting")
    parser.add_argument("--year", type=int)
    parser.add_argument("--none", action="store_true",
                        help="clear the profile; use generic Mode 01")
    args = parser.parse_args()

    profiles = discover()


    if args.none:
        write_env(None, None)
        return 0

    if args.set:
        match = next((p for p in profiles if p.name.lower() == args.set.lower()),
                     None)
        if match is None:
            known = [e for e in load_index()
                     if e["name"].lower() == args.set.lower()]
            if known:
                match = download(known[0]["name"])
            if match is None:
                print(f"No profile named {args.set!r} on this image or in the "
                      f"catalogue.", file=sys.stderr)
                show(profiles)
                return 1
        describe(match, args.year)
        write_env(match.name, args.year)
        return 0

    choices, heading = profiles, "Vehicle profiles on this image"
    if args.all or args.search:
        entries = load_index()
        if not entries:
            print("No catalogue at vehicles/index.json.\n"
                  "Build it with:  scripts/build_index.py", file=sys.stderr)
            return 1
        query = args.search
        if query is None and not args.list:
            query = input(f"\nSearch {len(entries)} known vehicles "
                          f"(blank for all): ").strip()
        query = query or ""
        installed = {p.name for p in profiles}
        matched_local = [p for p in profiles
                         if not query or query.lower() in p.name.lower()]
        choices = matched_local + catalogue(query, installed)
        heading = (f"Matching {query!r}" if query
                   else f"All {len(choices)} known vehicles")
        if not choices:
            print(f"\nNothing matches {query!r}.")
            return 1

    if args.list:
        show(choices, heading)
        return 0

    if not choices:
        show(choices)
        return 1

    searchable = not (args.all or args.search)
    show(choices, heading, searchable)
    while True:
        prompt = f"\nSelect [0-{len(choices)}{'/s' if searchable else ''}]: "
        raw = input(prompt).strip()
        if raw.lower() == "s" and searchable and load_index():
            query = input("  Search: ").strip()
            installed = {p.name for p in profiles}
            choices = ([p for p in profiles
                        if not query or query.lower() in p.name.lower()]
                       + catalogue(query, installed))
            if not choices:
                print(f"  Nothing matches {query!r}.")
                choices, searchable = profiles, True
                show(choices, "Vehicle profiles on this image", True)
                continue
            searchable = False
            show(choices, f"Matching {query!r}" if query else "All known vehicles")
            continue
        if raw == "0":
            describe_none()
            write_env(None, None)
            return 0
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            profile = choices[int(raw) - 1]
            if not profile.local:
                fetched = download(profile.name)
                if fetched is None:
                    return 1
                profile = fetched
            break
        print("  Enter one of the listed numbers.")

    year = ask_year(profile)
    describe(profile, year)
    if input("\nWrite this to the kit? [y/N] ").strip().lower() != "y":
        print("Nothing written.")
        return 1
    write_env(profile.name, year)
    return 0


def describe_none():
    print("\nThis kit will use generic Mode 01: about 16 widely supported PIDs,\n"
          "probed against whatever the vehicle actually answers. Works on any\n"
          "OBD-II vehicle, but yields far less than a vehicle profile.")


if __name__ == "__main__":
    sys.exit(main())
