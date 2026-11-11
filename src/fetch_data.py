"""Fetch monthly US green tea imports from Japan from the Census International Trade API.

Output: data/tea_imports.csv

Notes on the query (verified against the endpoint's variables.json):
- Quantity is only reported at the HS10 level; HS6 rows return quantity 0 and
  unit "-". So we pull every HS10 code under the two green tea HS6 headings and
  keep the parent HS6 code as a column. HS10 values sum exactly to the HS6 totals.
- GEN_* = general imports (everything entering the US, incl. bonded warehouses).
- Japan's country code (CTY_CODE) is 5880, confirmed via the API's country list.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

API_URL = "https://api.census.gov/data/timeseries/intltrade/imports/hs"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / "data" / "tea_imports.csv"
# One line: the UTC time of the last successful pull, ISO 8601. Read by app.py.
PULLED_AT_PATH = PROJECT_ROOT / "data" / "last_pulled.txt"

JAPAN_CTY_CODE = "5880"
GREEN_TEA_HS6 = ["090210", "090220"]
START_MONTH = "2020-01"

FIELDS = [
    "I_COMMODITY",        # HS10 code
    "I_COMMODITY_LDESC",  # long description
    "CTY_CODE",
    "CTY_NAME",
    "GEN_VAL_MO",         # general imports, value in USD, monthly
    "GEN_QY1_MO",         # general imports, quantity 1, monthly
    "UNIT_QY1",           # unit of quantity 1 (KG for green tea)
]


def load_api_key() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    key = os.getenv("CENSUS_API_KEY")
    if not key:
        sys.exit("CENSUS_API_KEY is not set. Put it in .env or set it as an environment variable.")
    return key


def fetch_hs6(hs6: str, api_key: str) -> pd.DataFrame:
    params = {
        "get": ",".join(FIELDS),
        "COMM_LVL": "HS10",
        "I_COMMODITY": f"{hs6}*",
        "CTY_CODE": JAPAN_CTY_CODE,
        "time": f"from {START_MONTH}",
        "key": api_key,
    }
    try:
        resp = requests.get(API_URL, params=params, timeout=120)
    except requests.RequestException as exc:
        # Don't print exc itself: its message can include the URL, which contains the key.
        sys.exit(f"Network error fetching HS {hs6}: {type(exc).__name__}")

    if resp.status_code == 204:  # Census returns 204 when no rows match
        # Both headings have had imports from Japan every month, so no rows means a
        # problem with the API, not a real zero. Saving only the other heading would
        # silently cut the totals, so stop instead.
        sys.exit(f"Census API returned no rows for HS {hs6}; the existing file was not changed.")
    if resp.status_code != 200:
        sys.exit(f"API error for HS {hs6}: HTTP {resp.status_code}: {resp.text[:300]}")

    rows = resp.json()
    df = pd.DataFrame(rows[1:], columns=rows[0])
    # The API echoes filter fields as extra columns, which creates duplicate names; keep first.
    return df.loc[:, ~df.columns.duplicated()]


def tidy(raw: pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame({
        "month": pd.to_datetime(raw["time"], format="%Y-%m"),
        "hs6_code": raw["I_COMMODITY"].str[:6],
        "hs10_code": raw["I_COMMODITY"],
        "description": raw["I_COMMODITY_LDESC"].str.strip(),
        "country_code": raw["CTY_CODE"],
        "country": raw["CTY_NAME"].str.strip(),
        "value_usd": pd.to_numeric(raw["GEN_VAL_MO"]),
        "quantity": pd.to_numeric(raw["GEN_QY1_MO"]),
        "unit": raw["UNIT_QY1"].str.strip(),
    })
    # Price per kg only makes sense when the unit is KG and quantity is positive.
    valid = (df["unit"] == "KG") & (df["quantity"] > 0)
    df["price_per_kg_usd"] = (df["value_usd"] / df["quantity"]).where(valid).round(2)
    return df.sort_values(["month", "hs10_code"]).reset_index(drop=True)


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
    api_key = load_api_key()
    frames = [fetch_hs6(hs6, api_key) for hs6 in GREEN_TEA_HS6]
    df = tidy(pd.concat(frames, ignore_index=True))
    check_no_lost_months(df["month"])

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False, date_format="%Y-%m")
    # Written only after the CSV saves, so it never claims a pull that failed.
    pulled_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    PULLED_AT_PATH.write_text(pulled_at + "\n")
    print(f"Saved {len(df)} rows to {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"Pull time {pulled_at} saved to {PULLED_AT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"Months covered: {df['month'].min():%Y-%m} to {df['month'].max():%Y-%m}")


if __name__ == "__main__":
    main()
