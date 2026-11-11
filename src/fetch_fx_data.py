"""Fetch the monthly average yen-to-dollar exchange rate, used to show matcha values in USD.

Source: Federal Reserve Board, G.5 foreign exchange release, via FRED (St. Louis Fed),
series EXJPUS "Japanese Yen to U.S. Dollar Spot Exchange Rate".
Output: data/fx_rates.csv (one row per month: month, jpy_per_usd)

How it works, and why:
- Units: yen per one US dollar (e.g. 156.42 means $1 = ¥156.42). To convert yen to
  dollars, divide by this rate.
- Method: each monthly value is the Fed's average of that month's daily noon buying
  rates in New York. We store it as published and do not recompute it.
- Access: FRED's public CSV download, which needs no API key. It is the same data
  the series page shows.
- Start: January 2019, matching the first month of the matcha data.
- Unfinished months: FRED can post a preliminary value for the current month before
  all its daily rates exist. That is not a full-month average, so we drop any month
  that has not ended yet in New York. The dashboard then shows no USD for that month
  instead of using a partial rate.
- Blank values (FRED writes "" or ".") are dropped, never filled in.
- The matcha CSV is never touched. USD is calculated in the dashboard, not stored.
"""

import io
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

SERIES_ID = "EXJPUS"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
START_DATE = "2019-01-01"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / "data" / "fx_rates.csv"
# One line: the UTC time of the last successful pull, ISO 8601. Read by app.py.
PULLED_AT_PATH = PROJECT_ROOT / "data" / "fx_last_pulled.txt"

NEW_YORK = ZoneInfo("America/New_York")  # the Fed's rates are New York noon buying rates
# Sanity range for yen per dollar. Since 2019 it has stayed roughly 100-165, so a value
# outside 50-400 almost certainly means the download is wrong, not the market.
PLAUSIBLE = (50, 400)


def check_no_lost_months(new_months: pd.Series) -> None:
    """Stop before saving if the new data is empty or is missing months the saved file has.

    Past months never disappear from the source, so a missing month means the download
    was incomplete. Stopping keeps the good file in place (and in the scheduled GitHub
    job, makes the run fail so nothing is committed).
    """
    if new_months.empty:
        sys.exit("No rows downloaded; the existing file was not changed.")
    try:
        old = set(pd.read_csv(OUTPUT_PATH, usecols=["month"], dtype=str)["month"])
    except (FileNotFoundError, ValueError):  # first run, or a file without a month column
        return
    lost = sorted(old - set(new_months.dt.strftime("%Y-%m")))
    if lost:
        sys.exit(f"New data is missing {len(lost)} month(s) the saved file has "
                 f"({lost[0]} to {lost[-1]}); the existing file was not changed.")


def main() -> None:
    resp = requests.get(CSV_URL, params={"id": SERIES_ID, "cosd": START_DATE}, timeout=60)
    resp.raise_for_status()
    raw = pd.read_csv(io.StringIO(resp.text), dtype=str)
    if list(raw.columns) != ["observation_date", SERIES_ID]:
        sys.exit(f"Unexpected columns from FRED: {list(raw.columns)}")

    df = pd.DataFrame({
        "month": pd.to_datetime(raw["observation_date"], format="%Y-%m-%d"),
        "jpy_per_usd": pd.to_numeric(raw[SERIES_ID].str.strip().replace(".", ""), errors="coerce"),
    })
    blank = df[df["jpy_per_usd"].isna()]
    df = df.dropna(subset=["jpy_per_usd"])

    this_month = pd.Timestamp(datetime.now(NEW_YORK).date()).to_period("M").to_timestamp()
    unfinished = df[df["month"] >= this_month]
    df = df[df["month"] < this_month].sort_values("month").reset_index(drop=True)

    if df.empty:
        sys.exit("FRED returned no usable monthly rates.")
    bad = df[~df["jpy_per_usd"].between(*PLAUSIBLE)]
    if not bad.empty:
        sys.exit(f"Implausible rates, not saved:\n{bad.to_string(index=False)}")
    check_no_lost_months(df["month"])
    expected = pd.date_range(df["month"].min(), df["month"].max(), freq="MS")
    gaps = expected.difference(df["month"])

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False, date_format="%Y-%m")
    # Written only after the CSV saves, so it never claims a pull that failed.
    pulled_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    PULLED_AT_PATH.write_text(pulled_at + "\n")

    for m in blank["month"]:
        print(f"WARNING: {m:%Y-%m}: FRED value is blank; no rate saved for this month")
    for m in unfinished["month"]:
        print(f"NOTE: {m:%Y-%m}: month not finished yet; preliminary rate ignored")
    for m in gaps:
        print(f"WARNING: {m:%Y-%m}: no rate published; USD will be blank for this month")
    print(f"Saved {len(df)} months to {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"Months covered: {df['month'].min():%Y-%m} to {df['month'].max():%Y-%m}")


if __name__ == "__main__":
    main()
