"""Fetch monthly Japanese exports of powdered green tea (matcha) to the United States.

Source: Japan's Ministry of Finance trade statistics (普通貿易統計), via the e-Stat API.
Output: data/matcha_exports.csv (one row per month)

How it works, and why:
- Codes: 0902.10-100 (powdered green tea, packs of 3 kg or less) and 0902.20-100
  (powdered green tea, packs over 3 kg). Japan created these codes in 2019, so we
  start in January 2019.
- Tables: e-Stat publishes "品別国別表 輸出" (exports by commodity and country) as
  multi-year tables whose IDs change when a year is revised. So we search for them
  by name on every run and never store table IDs. If two tables cover the same
  year, we take the one updated most recently.
- Country: the United States is found by its name (アメリカ合衆国) in each table's
  country list, not by a stored code.
- Units: each table says which quantity column is in KG (for these codes it is
  "数量2"). We read that label instead of assuming. Values are reported in
  thousands of yen (千円), so we multiply by 1,000 to get yen. No currency conversion.
- Revision stage: Japan revises trade data in steps. 確報 (monthly confirmed) comes
  out about a month after the month ends, 確々報 (revised annual) the next March,
  and 確定 (final) the next November. We work out each month's stage from Japan
  Customs' official release calendar (an XML file linked from the calendar page),
  comparing release times with the current time in Japan. Years or months that
  have dropped off the calendar are older than everything on it, so they are final.
- Safety check: each e-Stat table title also states its stage, e.g.
  "(1-12月：確々報) 2025年". If e-Stat has not yet loaded a release the calendar
  says is out (it can lag by hours), we use the less-final stage and print a
  warning, so a month is never labeled more final than the numbers in the file.
- Unpublished months appear as 0 in e-Stat. We drop every month after the latest
  released 確報, so a 0 in the CSV always means no recorded exports.

The e-Stat app ID is sent in request URLs, so this script never prints URLs or
raw exception messages.
"""

import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ESTAT_URL = "https://api.e-stat.go.jp/rest/3.0/app/json/"
CALENDAR_PAGE = "https://www.customs.go.jp/toukei/calendar/calend.htm"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / "data" / "matcha_exports.csv"
# One line: the UTC time of the last successful pull, ISO 8601.
PULLED_AT_PATH = PROJECT_ROOT / "data" / "matcha_last_pulled.txt"

TRADE_STATS_CODE = "00350300"  # 普通貿易統計 (Japan's ordinary trade statistics)
TABLE_NAME = "貿易統計_全国分 品別国別表 輸出"  # national totals, by commodity and country, exports
US_NAME = "アメリカ合衆国"
CODES = {"090210100": "small_pack", "090220100": "bulk"}  # ≤3 kg packs, >3 kg packs
START_YEAR = 2019

JST = timezone(timedelta(hours=9))  # Japan has no daylight saving time
STAGES = ["確報", "確々報", "確定"]  # least to most final


# ---------- e-Stat ----------

def load_app_id() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    app_id = os.getenv("ESTAT_APP_ID")
    if not app_id:
        sys.exit("ESTAT_APP_ID not found in .env")
    return app_id


def estat_get(endpoint: str, app_id: str, **params) -> dict:
    params["appId"] = app_id
    try:
        resp = requests.get(ESTAT_URL + endpoint, params=params, timeout=120)
    except requests.RequestException as exc:
        # Don't print exc itself: its message can include the URL, which contains the app ID.
        sys.exit(f"Network error calling e-Stat {endpoint}: {type(exc).__name__}")
    if resp.status_code != 200:
        sys.exit(f"e-Stat {endpoint} returned HTTP {resp.status_code}")
    body = next(iter(resp.json().values()))  # e.g. {"GET_STATS_LIST": {...}}
    result = body["RESULT"]
    # Status 0 = success, 1 = success with no data; 100+ are errors.
    if int(result["STATUS"]) >= 100:
        sys.exit(f"e-Stat {endpoint} error {result['STATUS']}: {result['ERROR_MSG']}")
    return body


def as_list(x):
    return x if isinstance(x, list) else [x]


def text(x) -> str:
    return x.get("$", "") if isinstance(x, dict) else (x or "")


def find_tables(app_id: str) -> list[dict]:
    """Return the export tables to use, one per year from START_YEAR, found by name."""
    body = estat_get("getStatsList", app_id, statsCode=TRADE_STATS_CODE,
                     searchWord="品別国別表 輸出", limit=1000)
    candidates = [
        t for t in as_list(body.get("DATALIST_INF", {}).get("TABLE_INF", []))
        if t.get("STATISTICS_NAME") == TABLE_NAME and t.get("CYCLE") == "年次"
        and int(str(t.get("SURVEY_DATE", "0"))[:4]) >= START_YEAR  # last year covered
    ]
    best_for_year: dict[int, dict] = {}
    for t in candidates:
        meta = estat_get("getMetaInfo", app_id, statsDataId=t["@id"])["METADATA_INF"]
        classes = {o["@id"]: as_list(o["CLASS"]) for o in as_list(meta["CLASS_INF"]["CLASS_OBJ"])}
        info = {
            "id": t["@id"],
            "title": text(t.get("TITLE")),
            "updated": str(t.get("UPDATED_DATE", "")),
            "classes": classes,
        }
        for c in classes["time"]:
            year = int(c["@code"][:4])
            if year >= START_YEAR and (year not in best_for_year
                                       or info["updated"] > best_for_year[year]["updated"]):
                best_for_year[year] = info
    if not best_for_year:
        sys.exit(f"No e-Stat tables named '{TABLE_NAME}' found")
    missing = set(range(START_YEAR, max(best_for_year) + 1)) - set(best_for_year)
    if missing:
        sys.exit(f"No e-Stat table covers year(s) {sorted(missing)}")

    tables: dict[str, dict] = {}
    for year, info in sorted(best_for_year.items()):
        tables.setdefault(info["id"], {**info, "years": []})["years"].append(year)
    return list(tables.values())


def fetch_table(table: dict, app_id: str) -> pd.DataFrame:
    """Long-format rows: year, month, code, kg, value_yen, table_id."""
    cls = table["classes"]
    us = [c["@code"] for c in cls["area"] if US_NAME in c["@name"]]
    if len(us) != 1:
        sys.exit(f"Expected one '{US_NAME}' in table {table['id']}, found {len(us)}")
    present = {c["@code"] for c in cls["cat01"]}
    if not set(CODES) <= present:
        sys.exit(f"Table {table['id']} is missing codes {sorted(set(CODES) - present)}")
    time_codes = [c["@code"] for c in cls["time"] if int(c["@code"][:4]) in table["years"]]
    measure_name = {c["@code"]: c["@name"] for c in cls["cat02"]}

    params = {"statsDataId": table["id"], "cdCat01": ",".join(CODES), "cdArea": us[0],
              "cdTime": ",".join(time_codes), "metaGetFlg": "N", "limit": 100000}
    values = []
    while True:
        data = estat_get("getStatsData", app_id, **params)["STATISTICAL_DATA"]
        values += as_list(data.get("DATA_INF", {}).get("VALUE", []))
        next_key = data["RESULT_INF"].get("NEXT_KEY")
        if not next_key:
            break
        params["startPosition"] = next_key

    # Work out which quantity column ("数量1" or "数量2") is in kilograms, per code.
    kg_slot = {}
    for v in values:
        name = measure_name[v["@cat02"]]
        if name in ("単位1", "単位2") and v["$"].strip() in ("ＫＧ", "KG"):
            kg_slot[(v["@cat01"], v["@time"])] = "数量" + name[-1]

    rows = {}
    for v in values:
        m = re.fullmatch(r"(\d+)月_(数量1|数量2|金額)", measure_name[v["@cat02"]])
        if not m:
            continue  # unit labels and annual totals
        key = (v["@cat01"], v["@time"])
        if key not in kg_slot:
            sys.exit(f"No KG quantity column for code {key[0]} in table {table['id']}")
        month, measure = int(m.group(1)), m.group(2)
        if measure.startswith("数量") and measure != kg_slot[key]:
            continue
        row = rows.setdefault((key[0], int(key[1][:4]), month),
                              {"code": key[0], "year": int(key[1][:4]), "month": month,
                               "table_id": table["id"]})
        num = float(v["$"]) if v["$"] not in ("", "-", "***") else 0.0
        if measure == "金額":
            row["value_yen"] = num * 1000  # source unit is 千円 (thousands of yen)
        else:
            row["kg"] = num
    return pd.DataFrame(rows.values())


def title_stages(title: str) -> dict[int, tuple[int, str]]:
    """Parse a table title into {year: (last_month, stage)}.

    Examples: "(輸出 1-12月：確定)　2016年～2020年", "(1-12月：確々報) 2025年", "(1-9月：確報) 2026年"
    """
    out = {}
    pattern = r"\d+-(\d+)月：(確定|確々報|確報)\)\s*(\d{4})年(?:～(\d{4})年)?"
    for last_month, stage, y1, y2 in re.findall(pattern, title):
        for y in range(int(y1), int(y2 or y1) + 1):
            out[y] = (int(last_month), stage)
    return out


# ---------- Release calendar ----------

def era_to_year(s: str) -> int | None:
    m = re.search(r"令和(元|\d+)年", s)
    return None if not m else 2018 + (1 if m.group(1) == "元" else int(m.group(1)))


def fetch_calendar() -> tuple[dict, dict]:
    """Return ({(year, month): export 確報 release time}, {(year, stage): release time}).

    Releases with no exact date yet (e.g. "3月頃", around March) are left out,
    which means they count as not yet released.
    """
    try:
        page = requests.get(CALENDAR_PAGE, timeout=60)
        page.raise_for_status()
        # The XML file's folder name changes when Customs revises the calendar, so find the link.
        m = re.search(rb'href="([^"]*e-stat_futsuu\.xml)"', page.content)
        if not m:
            sys.exit("Could not find the release calendar XML link on the Customs calendar page")
        xml_url = requests.compat.urljoin(CALENDAR_PAGE, m.group(1).decode())
        xml = requests.get(xml_url, timeout=60)
        xml.raise_for_status()
    except requests.RequestException as exc:
        sys.exit(f"Could not download the release calendar: {exc}")

    monthly, yearly = {}, {}
    for c1 in ET.fromstring(xml.content).iter("class_1"):
        period = c1.get("name", "")
        year = era_to_year(period)
        month_match = re.search(r"年(\d+)月", period)
        for c2 in c1.findall("class_2"):
            kind = c2.get("name", "")
            for c5 in c2.iter("class_5"):
                try:
                    when = datetime(int(c5.findtext("release_year")), int(c5.findtext("release_month")),
                                    int(c5.findtext("release_day")), int(c5.findtext("release_hour")),
                                    int(c5.findtext("release_minute")), tzinfo=JST)
                except (TypeError, ValueError):
                    continue  # date not fixed yet
                if month_match and "輸出確報" in kind:
                    monthly[(year, int(month_match.group(1)))] = when
                elif not month_match and kind in ("確々報", "確定"):
                    yearly[(year, kind)] = when
    if not monthly or not yearly:
        sys.exit("Release calendar XML had an unexpected layout")
    return monthly, yearly


def calendar_stage(year: int, month: int, now: datetime, monthly: dict, yearly: dict) -> str | None:
    """Stage of a month's data at time `now`, or None if its 確報 is not out yet."""
    earliest_month = min(monthly)
    if (year, month) in monthly:
        if now < monthly[(year, month)]:
            return None
    elif (year, month) > earliest_month:
        return None  # newer than anything on the calendar: not scheduled yet
    # 確報 is out. Now check the annual revisions.
    earliest_year = min(y for y, _ in yearly)
    if year < earliest_year:
        return "確定"  # dropped off the calendar, so long since final
    for stage in ("確定", "確々報"):
        when = yearly.get((year, stage))
        if when and now >= when:
            return stage
    return "確報"


# ---------- Main ----------

def main() -> None:
    app_id = load_app_id()
    now = datetime.now(JST)
    monthly_cal, yearly_cal = fetch_calendar()
    tables = find_tables(app_id)

    long = pd.concat([fetch_table(t, app_id) for t in tables], ignore_index=True)
    published = {}  # year -> (last_month, stage), as stated by e-Stat table titles
    for t in tables:
        published.update(title_stages(t["title"]))

    stages, warnings = {}, []
    for year, month in sorted(set(zip(long["year"], long["month"]))):
        cal = calendar_stage(year, month, now, monthly_cal, yearly_cal)
        if cal is None:
            continue
        if year not in published:
            warnings.append(f"{year}: table title does not state a stage; using calendar ({cal})")
            stages[(year, month)] = cal
            continue
        last_month, title_stage = published[year]
        if month > last_month:
            warnings.append(f"{year}-{month:02d}: released per calendar but not yet in e-Stat; skipped")
            continue
        stage = min(cal, title_stage, key=STAGES.index)
        if stage != cal:
            warnings.append(f"{year}-{month:02d}: calendar says {cal}, e-Stat still has {title_stage}; "
                            f"labeled {stage}")
        stages[(year, month)] = stage

    long = long[[(y, m) in stages for y, m in zip(long["year"], long["month"])]]
    wide = long.pivot_table(index=["year", "month"], columns="code", values=["kg", "value_yen"],
                            aggfunc="sum")
    df = pd.DataFrame({"month": [pd.Timestamp(y, m, 1) for y, m in wide.index]})
    df["stage"] = [stages[k] for k in wide.index]
    for code, label in CODES.items():
        df[f"{label}_kg"] = wide[("kg", code)].to_numpy()
        df[f"{label}_value_yen"] = wide[("value_yen", code)].to_numpy()
    df["total_kg"] = df[[f"{l}_kg" for l in CODES.values()]].sum(axis=1)
    df["total_value_yen"] = df[[f"{l}_value_yen" for l in CODES.values()]].sum(axis=1)
    df["estat_table_id"] = (long.groupby(["year", "month"])["table_id"].first()
                            .reindex(wide.index).to_numpy())
    num_cols = [c for c in df.columns if c.endswith("_kg") or c.endswith("_yen")]
    df[num_cols] = df[num_cols].round().astype("int64")
    df = df.sort_values("month").reset_index(drop=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False, date_format="%Y-%m")
    # Written only after the CSV saves, so it never claims a pull that failed.
    pulled_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    PULLED_AT_PATH.write_text(pulled_at + "\n")

    for w in warnings:
        print("WARNING:", w)
    print(f"Tables used: " + "; ".join(f"{t['id']} ({min(t['years'])}-{max(t['years'])})" for t in tables))
    print(f"Saved {len(df)} months to {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"Months covered: {df['month'].min():%Y-%m} to {df['month'].max():%Y-%m}")
    print("Months per stage: " + ", ".join(f"{s} {n}" for s, n in df["stage"].value_counts()
                                            .reindex(STAGES[::-1]).dropna().astype(int).items()))


if __name__ == "__main__":
    main()
