"""Plant-specific settings. Change these, not the calculation code."""

PLANT_NAME = "SRTPV plant"
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
