"""Turn vendor signal identifiers into readable, unit-suffixed column names.

The research team reads the CSV directly, so `IONIQ5_LATERAL_ACCELERATION`
becomes `lateral_acceleration_mps2`. Traceability is not lost: every column is
written to `signals.csv` next to its original identifier, its human-readable
name, its unit and the command it came from.

Names are derived from the signalset's own `name` and `unit` fields, so they
follow whatever the vehicle's maintainers called things rather than a mapping
we would have to hand-maintain per model.
"""

import re
from typing import Dict, Iterable, Optional, Tuple

# Short suffixes for the OBDb unit enum. Deliberately collision-free:
# metersPerSecond is `mps` (not `ms`) so it cannot be read as milliseconds,
# and coulombs stays spelled out so it cannot be read as celsius.
UNIT_SUFFIX = {
    "volts": "v", "millivolts": "mv", "kilovolts": "kv",
    "amps": "a", "milliamps": "ma", "kiloamps": "ka",
    "ampereHours": "ah", "milliampereHours": "mah", "kiloampereHours": "kah",
    "coulombs": "coulomb",
    "percent": "pct", "scalar": None, "unknown": None, "normal": None,
    "celsius": "c", "fahrenheit": "f", "kelvin": "k",
    "kilometersPerHour": "kph", "milesPerHour": "mph",
    "metersPerSecond": "mps", "metersPerSecondSquared": "mps2",
    "gravity": "g", "rpm": "rpm",
    "hertz": "hz", "millihertz": "mhz_milli", "kilohertz": "khz",
    "megahertz": "mhz", "gigahertz": "ghz", "terahertz": "thz",
    "microhertz": "uhz", "nanohertz": "nhz", "framesPerSecond": "fps",
    "newtonMeters": "nm", "poundFoot": "lbft", "inchPound": "inlb",
    "kilometers": "km", "miles": "mi", "meters": "m", "centimeters": "cm",
    "millimeters": "mm", "inches": "in", "feet": "ft", "yards": "yd",
    "kilowatts": "kw", "watts": "w", "milliwatts": "mw",
    "kilowattHours": "kwh", "joules": "j", "kilojoules": "kj",
    "kilowattHoursPer100Kilometers": "kwh_per_100km",
    "kilowattHoursPer100Miles": "kwh_per_100mi",
    "milesPerKilowattHour": "mi_per_kwh",
    "seconds": "s", "milliseconds": "ms", "minutes": "min", "hours": "h",
    "degrees": "deg", "radians": "rad",
    "degreesPerSecond": "dps", "radiansPerSecond": "rad_per_s",
    "psi": "psi", "bars": "bar", "kilopascal": "kpa",
    "ohms": "ohm", "kiloohms": "kohm", "milliohms": "mohm",
    "megaohms": "megohm", "microohms": "uohm",
    "liters": "l", "gallons": "gal",
    "litersPerHour": "l_per_h", "gallonsPerHour": "gal_per_h",
    "gramsPerSecond": "g_per_s", "kilogramsPerHour": "kg_per_h",
    "gramsPerLiter": "g_per_l", "milligramsPerDeciliter": "mg_per_dl",
    "milligramsPerStroke": "mg_per_stroke",
    # Enumerations and booleans read fine without a unit suffix.
    "noyes": None, "yesno": None, "offon": None, "onoff": None,
    "hex": None, "ascii": None,
}


def slug(text: str) -> str:
    """`Front left wheel speed, high resolution` -> `front_left_wheel_speed_high_resolution`."""
    out = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    if out and out[0].isdigit():
        out = "x" + out           # keep it usable as an identifier in pandas/R
    return out


def unit_suffix(unit: Optional[str]) -> Optional[str]:
    if not unit:
        return None
    if unit in UNIT_SUFFIX:
        return UNIT_SUFFIX[unit]
    # An unrecognised unit is still worth recording, just spelled out.
    return slug(unit) or None


def column_name(name: str, unit: Optional[str] = None,
                fallback: str = "") -> str:
    base = slug(name) or slug(fallback)
    suffix = unit_suffix(unit)
    if suffix and not base.endswith("_" + suffix):
        return f"{base}_{suffix}"
    return base


def assign_columns(entries: Iterable[Tuple[str, str, Optional[str]]]
                   ) -> Dict[str, str]:
    """Map each `(key, name, unit)` to a unique column name, in order.

    Two signals can legitimately share a display name -- the same quantity
    reported by two ECUs, say -- so a clash falls back to the vendor
    identifier, which is unique by construction, rather than to a positional
    suffix that would shift if the signalset changed.
    """
    used: Dict[str, str] = {}
    out: Dict[str, str] = {}
    for key, name, unit in entries:
        candidate = column_name(name, unit, fallback=key)
        if not candidate:
            candidate = slug(key) or "signal"
        if candidate in used and used[candidate] != key:
            candidate = slug(key) or candidate
            n = 2
            while candidate in used and used[candidate] != key:
                candidate = f"{slug(key)}_{n}"
                n += 1
        used[candidate] = key
        out[key] = candidate
    return out


# Column order of signals.csv. `group` is the top-level category and is what
# you filter or group on; `category` keeps OBDb's full path, which for per-cell
# signals goes as deep as "Battery.Modules.147".
DICTIONARY_FIELDS = ["column", "name", "unit", "group", "category", "source",
                     "source_id", "command", "period_s", "min", "max"]


def group_of(category: str) -> str:
    return (category or "").split(".")[0]
