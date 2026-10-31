"""Plant-specific settings. Change these, not the calculation code."""

PLANT_NAME = "SRTPV DASHBOARD"
PLANT_FULL_NAME = "SRTPV RRMCH Plant One"
PLANT_SHORT = "SRTPV plant"
PLANT_SITE = "RRMCH Plant One"   # shown under the title on the login page
BRAND = "asase.energy"
CONFIDENTIAL = "CONFIDENTIAL — FOR INTERNAL USE ONLY"
PLANT_CAPACITY_KWP = 984.5

# Inverter id -> roof/location shown on the dashboard
INVERTERS: dict[str, str] = {
    "PV1": "RR Auditorium",
    "PV2": "RR Auditorium",
    "PV3": "RR Auditorium",
    "PV4": "RR College",
    "PV5": "RR College",
    "PV6": "RR College",
    "PV7": "Hospital Roof 2",
    "PV8": "Hospital Roof 1",
    "PV9": "Hospital Annex Roof 3",
}
INVERTER_IDS = list(INVERTERS)

# PV system comparison (consolidated view). Names follow the inverter portal's "PV system" list.
PV_SYSTEM_NAMES: dict[str, str] = {
    p: (loc if loc.startswith("RR") else f"RR {loc}") + f" {p}" for p, loc in INVERTERS.items()
}
# kWp used for kWh/kWp in that comparison. 100 kWp each is what the portal screenshot implies
# (kWh / kWh-per-kWp = 100.0 for all nine). Replace with each inverter's real DC capacity if different;
# note nine x 100 = 900, while PLANT_CAPACITY_KWP above is 984.5.
INVERTER_KWP: dict[str, float] = {p: 100.0 for p in INVERTER_IDS}

# Meter multiplying factors (site kWh = meter display units x MF)
LT_METER_MF = 300      # Landis+Gyr E650, Sl. P0028544
HT_METER_MF = 6000     # L&K ER300P bi-directional, Sl. 26003301

# Flagging rules
OUTAGE_FRACTION = 0.20        # day total below 20% of month median -> outage day
CELL_FLAG_FRACTION = 0.50     # inverter below 50% of that day's median inverter -> flagged cell
LOW_INVERTER_RATIO = 0.60     # inverter month total below 60% of the others' average -> chronic low
TOTAL_MISMATCH_KWH = 0.5      # tolerance when checking the sheet's Total column

# Productivity bands: daily specific yield = inverter kWh / plant kWp
# (name, from, to) in kWh per kWp per day
PRODUCTIVITY_BANDS = [
    ("Very low", 0.0, 1.0),
    ("Low", 1.0, 3.0),
    ("Normal", 3.0, 4.5),
    ("High", 4.5, float("inf")),
]
WEATHER_GROUPS = ["Sunny", "Partly cloudy", "Cloudy", "Rain / storm", "Not recorded"]

# AC loss between inverters and the LT meter (inverter kWh - LT kWh)
AC_LOSS_EXPECTED = (0.01, 0.04)   # 1-4% is normal for cables, transformer-less AC run and panel losses
# Power factor at the HT / EB meter
PF_LIMIT = 0.90                   # days below this are flagged (check your tariff's PF limit)

# Plant location for automatic weather (Kambipura, Bengaluru - approximate; refine from Google Maps)
PLANT_LAT = 12.89
PLANT_LON = 77.47
PLANT_TIMEZONE = "Asia/Kolkata"

# Projected view (see core/views.py). Set to False, or set the secret
# PROJECTED_VIEW_ENABLED = "false", to switch it off everywhere without a code change.
PROJECTED_VIEW_ENABLED = True

# "At a glance" labels: LT and HT figures come from the monthly bill readings
METER_FIGURES_FROM_BILL = True
OFFLINE_FRACTION = 0.10      # inverter below 10% of the day's median inverter = offline that day

# HT bill
CONTRACT_DEMAND_KVA = 1200.0
DEFAULT_EXPORT_NOTE = "Netted against import, no cash credit"

# Short roof names for charts ("PV1 · Auditorium")
INVERTER_SHORT = {
    "PV1": "Auditorium", "PV2": "Auditorium", "PV3": "Auditorium",
    "PV4": "College", "PV5": "College", "PV6": "College",
    "PV7": "Hospital Roof 2", "PV8": "Hospital Roof 1", "PV9": "Hospital Annex",
}

# A day below this share of the month's median inverter total is a "very low day" (red)
VERY_LOW_FRACTION = 0.35

# Spreading the monthly bill over days: site load by day type, scaled to the bill total
SITE_LOAD_WEIGHTS = {"weekday": 1.0, "Sat": 0.8471, "Sun": 0.7097}

# Projected view (matches the monthly report's method, totals never change):
# an inverter that is faulty on at least PROJECTION_MIN_FAULTY_DAYS of a month's days gets
# PROJECTION_SHARE of each day's actual total; every inverter's actual reading is scaled so the
# day total stays exactly the same. A reading is "faulty" when it is blank, or below
# PROJECTION_THRESHOLD of the average of the other inverters in its group.
PROJECTION_THRESHOLD = 0.70
PROJECTION_SHARE = 0.10   # default; a different share for a month is set in the app (Import data → Tools)
PROJECTION_MIN_FAULTY_DAYS = 0.5
# Groups of inverters that normally produce alike (used for projection)
PROJECTION_GROUPS = {
    "RR Auditorium": ["PV1", "PV2", "PV3"],
    "RR College": ["PV4", "PV5", "PV6"],
    "RR Hospital": ["PV7", "PV8", "PV9"],
}
