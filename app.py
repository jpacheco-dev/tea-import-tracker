"""Streamlit dashboard for green tea trade between Japan and the US.

Two separate datasets, each on its own tab:
- US imports of green tea from Japan: data/tea_imports.csv (src/fetch_data.py, US Census)
- Japan's exports of powdered green tea (matcha) to the US: data/matcha_exports.csv
  (src/fetch_japan_data.py, Japan's trade statistics via e-Stat), in yen, with optional
  USD estimates from data/fx_rates.csv (src/fetch_fx_data.py, Federal Reserve via FRED)

The app reads only these CSV files. It never calls an API and needs no keys.

Run: .venv/bin/streamlit run app.py
"""

from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_PATH = DATA_DIR / "tea_imports.csv"
PULLED_AT_PATH = DATA_DIR / "last_pulled.txt"
MATCHA_PATH = DATA_DIR / "matcha_exports.csv"
MATCHA_PULLED_AT_PATH = DATA_DIR / "matcha_last_pulled.txt"
FX_PATH = DATA_DIR / "fx_rates.csv"
FX_PULLED_AT_PATH = DATA_DIR / "fx_last_pulled.txt"

# Short labels built from the three things that distinguish the HS10 codes:
# packing size (<= 3 kg "Packaged" vs larger "Bulk"), flavoring, and organic status.
# Ordered by total volume, largest first, so the biggest products sit at the
# bottom of the stacked chart. Each product keeps a fixed color regardless of filters.
PRODUCTS = {
    "0902209050": ("Bulk, unflavored, conventional", "#2a78d6"),
    "0902109050": ("Packaged, unflavored, conventional", "#eb6834"),
    "0902109015": ("Packaged, unflavored, organic", "#1baf7a"),
    "0902209015": ("Bulk, unflavored, organic", "#eda100"),
    "0902101050": ("Packaged, flavored, conventional", "#e87ba4"),
    "0902201000": ("Bulk, flavored (organic and conventional)", "#008300"),
    "0902101015": ("Packaged, flavored, organic", "#6250d6"),
}

# Matcha pack types: CSV column prefix -> (label, color). Colors stay fixed per
# pack type whichever one is selected.
PACKS = {
    "total": ("Combined total", "#2a78d6"),
    "small_pack": ("Small packs (3 kg or less)", "#eb6834"),
    "bulk": ("Bulk packs (over 3 kg)", "#1baf7a"),
}
# Japan's revision stages, in English. The Japanese terms appear only in the About section.
STAGE_EN = {"確定": "Final", "確々報": "Second revision", "確報": "Revised"}
STAGE_JA_READING = {"確定": "kakutei", "確々報": "kakukakuhō", "確報": "kakuhō"}
# Background shading for months that are not final yet (lighter = closer to final).
STAGE_FILL = {"Second revision": "#f3f2ed", "Revised": "#e6e4dc"}

SINGLE_SERIES_COLOR = "#2a78d6"
SURFACE = "#ffffff"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
MUTED_INK = "#52514e"


# ================================================================ shared helpers

def last_pulled(pulled_path: Path, data_path: Path) -> str:
    # The fetch scripts write the pull time (UTC, ISO 8601) to a text file.
    # If that file is missing or unreadable, fall back to the CSV's modified
    # date and say so, since copying or cloning the repo can change it.
    try:
        pulled = datetime.fromisoformat(pulled_path.read_text().strip())
        return pulled.strftime("%B %-d, %Y")
    except (OSError, ValueError):
        try:
            mtime = datetime.fromtimestamp(data_path.stat().st_mtime)
        except OSError:
            return "not downloaded yet"
        return f"{mtime:%B %-d, %Y} (estimated from file date)"


def fmt_usd(x: float) -> str:
    if abs(x) >= 1e6:
        return f"${x / 1e6:,.1f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:,.0f}K"
    return f"${x:,.0f}"


def fmt_yen(x: float) -> str:
    if abs(x) >= 1e9:
        return f"¥{x / 1e9:,.2f}B"
    if abs(x) >= 1e6:
        return f"¥{x / 1e6:,.1f}M"
    return f"¥{x:,.0f}"


def fmt_kg(x: float) -> str:
    if abs(x) >= 1e6:
        return f"{x / 1e6:,.2f}M kg"
    if abs(x) >= 1e3:
        return f"{x / 1e3:,.0f}K kg"
    return f"{x:,.0f} kg"


def pct_change(new: float, old: float) -> str | None:
    if not old or pd.isna(old) or pd.isna(new):
        return None
    return f"{(new - old) / old:+.1%} vs prior 12 months"


def kpi_windows(end: pd.Timestamp, first_month: pd.Timestamp):
    """Latest 12 months = the 12 ending at the selected end month; prior = the 12 before."""
    latest_start = end - pd.DateOffset(months=11)
    prior_start = end - pd.DateOffset(months=23)
    prior_end = end - pd.DateOffset(months=12)
    return latest_start, prior_start, prior_end, prior_start >= first_month


def kpi_heading(latest_start: pd.Timestamp, end: pd.Timestamp, first_month: pd.Timestamp) -> str:
    """Heading for the headline numbers. Near the start of the data, fewer than 12 months exist."""
    if latest_start >= first_month:
        return f"Last 12 months ({latest_start:%b %Y} to {end:%b %Y})"
    n = (end.year - first_month.year) * 12 + end.month - first_month.month + 1
    return (f"Last {n} month{'s' if n > 1 else ''} ({month_span(first_month, end)}, "
            f"the data starts in {first_month:%B %Y})")


def month_span(a: pd.Timestamp, b: pd.Timestamp) -> str:
    return f"{a:%b %Y}" if a == b else f"{a:%b %Y} to {b:%b %Y}"


def base_layout(fig: go.Figure, y_title: str, y_format: str) -> go.Figure:
    fig.update_layout(
        height=320,
        margin=dict(l=8, r=8, t=8, b=8),
        plot_bgcolor=SURFACE,
        paper_bgcolor=SURFACE,
        font=dict(family="system-ui, -apple-system, Segoe UI, sans-serif", color=MUTED_INK, size=13),
        hovermode="x unified",
        showlegend=False,
    )
    fig.update_xaxes(showgrid=False, linecolor=AXIS, ticks="", hoverformat="%b %Y")
    fig.update_yaxes(
        title_text=y_title, gridcolor=GRID, zeroline=False, linecolor=AXIS,
        tickformat=y_format, rangemode="tozero",
    )
    return fig


def line_chart(series: pd.Series, name: str, y_title: str, y_format: str, hover: str,
               color: str = SINGLE_SERIES_COLOR, customdata=None) -> go.Figure:
    # A line needs two points to be visible, so very short ranges also get markers.
    mode = "lines+markers" if len(series) < 3 else "lines"
    fig = go.Figure(go.Scatter(
        x=series.index, y=series.values, name=name, mode=mode,
        line=dict(color=color, width=2), marker=dict(size=8),
        customdata=customdata, hovertemplate=hover + "<extra></extra>",
    ))
    return base_layout(fig, y_title, y_format)


def month_range_slider(months: pd.DatetimeIndex, key: str):
    return st.select_slider(
        "Date range",
        options=list(months),
        value=(months[0], months[-1]),
        format_func=lambda d: d.strftime("%b %Y"),
        key=key,
    )


# ================================================================ US imports (Census)

@st.cache_data
def load_data() -> pd.DataFrame:
    df = pd.read_csv(DATA_PATH, dtype={"hs6_code": str, "hs10_code": str, "country_code": str})
    df["month"] = pd.to_datetime(df["month"], format="%Y-%m")
    df["product"] = df["hs10_code"].map(lambda c: PRODUCTS.get(c, (c, None))[0])
    return df


def monthly_totals(df: pd.DataFrame, months: pd.DatetimeIndex) -> pd.DataFrame:
    """Sum value and kg per month. Months with no rows count as zero imports."""
    out = df.groupby("month")[["value_usd", "quantity"]].sum().reindex(months, fill_value=0)
    # Price is total value / total kg (weighted), not an average of per-row prices.
    out["price_per_kg"] = out["value_usd"] / out["quantity"].where(out["quantity"] > 0)
    out.index.name = "month"
    return out


def us_section() -> None:
    df = load_data()
    all_months = pd.date_range(df["month"].min(), df["month"].max(), freq="MS")

    st.header("US green tea imports from Japan")

    # Filters sit inside the tab so they clearly apply to this dataset only.
    f1, f2 = st.columns([2, 3], gap="large")
    with f1:
        start, end = month_range_slider(all_months, key="us_range")
        st.caption("Packaged = retail packs of 3 kg or less. Bulk = larger packs.")
    with f2:
        st.markdown("**Product category**")
        cols = st.columns(2)
        selected = [
            label for i, (code, (label, _)) in enumerate(PRODUCTS.items())
            if cols[i % 2].checkbox(label, value=True, key=f"product_{code}")
        ]

    if not selected:
        st.info("Select at least one product category above.")
        return

    by_product = df[df["product"].isin(selected)]
    range_months = pd.date_range(start, end, freq="MS")
    in_range = by_product[by_product["month"].between(start, end)]
    totals = monthly_totals(in_range, range_months)

    st.caption(f"Monthly general imports, {start:%b %Y} to {end:%b %Y}. Source: US Census Bureau. "
               "Values in US dollars.")

    # Uses the product filter but looks back past the start of the date range if needed.
    latest_start, prior_start, prior_end, has_prior = kpi_windows(end, all_months[0])

    def window(a, b):
        w = by_product[by_product["month"].between(a, b)]
        value, kg = w["value_usd"].sum(), w["quantity"].sum()
        return value, kg, (value / kg if kg else float("nan"))

    lv, lk, lp = window(latest_start, end)
    pv, pk, pp = window(prior_start, prior_end) if has_prior else (None, None, None)

    st.subheader(kpi_heading(latest_start, end, all_months[0]))
    c1, c2, c3 = st.columns(3)
    # delta_color="off": changes are shown in neutral gray, since a rise is not
    # inherently good or bad here (e.g. a higher price per kg).
    c1.metric("Total import value", fmt_usd(lv), pct_change(lv, pv) if has_prior else None,
              delta_color="off", border=True)
    c2.metric("Total volume", fmt_kg(lk), pct_change(lk, pk) if has_prior else None,
              delta_color="off", border=True)
    c3.metric("Average price per kg", f"${lp:,.2f}" if not pd.isna(lp) else "n/a",
              pct_change(lp, pp) if has_prior else None, delta_color="off", border=True)
    if not has_prior:
        st.caption("Not enough earlier data for a prior 12-month comparison.")

    left, right = st.columns(2)
    with left:
        st.markdown("**Monthly import volume**")
        st.plotly_chart(
            line_chart(totals["quantity"], "Volume", "kg", "~s", "%{y:,.0f} kg"),
            width="stretch", key="us_volume",
        )
    with right:
        st.markdown("**Monthly import value**")
        st.plotly_chart(
            line_chart(totals["value_usd"], "Value", "USD", "$~s", "$%{y:,.0f}"),
            width="stretch", key="us_value",
        )

    st.markdown("**Average price per kg** (total value divided by total kg)")
    st.plotly_chart(
        line_chart(totals["price_per_kg"], "Price per kg", "USD per kg", "$.0f", "$%{y:,.2f} per kg"),
        width="stretch", key="us_price",
    )

    st.markdown("**Monthly volume by product category**")
    fig = go.Figure()
    for code, (label, color) in PRODUCTS.items():
        if label not in selected:
            continue
        s = (in_range[in_range["hs10_code"] == code].groupby("month")["quantity"].sum()
             .reindex(range_months, fill_value=0))
        fig.add_trace(go.Bar(
            x=s.index, y=s.values, name=label,
            marker=dict(color=color, line=dict(color=SURFACE, width=1)),
            hovertemplate=f"{label}: %{{y:,.0f}} kg<extra></extra>",
        ))
    base_layout(fig, "kg", "~s")
    fig.update_layout(
        barmode="stack", bargap=0.15, height=420, showlegend=True,
        legend=dict(orientation="h", yanchor="top", y=-0.08, x=0, traceorder="normal"),
    )
    st.plotly_chart(fig, width="stretch", key="us_by_product")

    with st.expander("View data table"):
        table = (in_range.groupby(["month", "product"])[["value_usd", "quantity"]].sum().reset_index())
        table["price_per_kg_usd"] = (table["value_usd"] / table["quantity"].where(table["quantity"] > 0)).round(2)
        table["month"] = table["month"].dt.strftime("%Y-%m")
        st.dataframe(
            table.sort_values(["month", "product"], ascending=[False, True]),
            hide_index=True, width="stretch",
            column_config={
                "month": "Month", "product": "Product category",
                "value_usd": st.column_config.NumberColumn("Value (USD)", format="$%d"),
                "quantity": st.column_config.NumberColumn("Volume (kg)", format="%d"),
                "price_per_kg_usd": st.column_config.NumberColumn("Price per kg", format="$%.2f"),
            },
        )

    st.divider()
    st.subheader("About this data")
    codes = "\n".join(f"- `{c}`: {label}" for c, (label, _) in PRODUCTS.items())
    st.markdown(
        f"""
Source: **US Census Bureau International Trade API** (monthly imports by HS code),
general imports from Japan (country code 5880). Value is customs value in US dollars;
volume is in kilograms.

HS codes used: all HS10 codes under **0902.10** (green tea in immediate packings of
3 kg or less) and **0902.20** (other green tea, i.e. bulk):

{codes}

Data last pulled: **{last_pulled(PULLED_AT_PATH, DATA_PATH)}**. Covers {all_months[0]:%B %Y} to {all_months[-1]:%B %Y}.
Months with no recorded imports for a product are shown as zero.
"""
    )


# ================================================================ Matcha exports (Japan)

@st.cache_data
def load_matcha() -> pd.DataFrame:
    # estat_table_id is read as text so leading zeros (e.g. "0003313965") are kept.
    df = pd.read_csv(MATCHA_PATH, dtype={"estat_table_id": str, "stage": str})
    df["month"] = pd.to_datetime(df["month"], format="%Y-%m")
    unknown = sorted(set(df["stage"]) - set(STAGE_EN))
    if unknown:
        raise ValueError(f"Unknown revision stage(s) in {MATCHA_PATH.name}: {unknown}")
    df["stage_ja"] = df["stage"]
    df["stage"] = df["stage"].map(STAGE_EN)
    for pack in PACKS:
        # Price is total value / total kg for the month (weighted), in yen per kg.
        df[f"{pack}_price_per_kg"] = df[f"{pack}_value_yen"] / df[f"{pack}_kg"].where(df[f"{pack}_kg"] > 0)
    return df.sort_values("month").set_index("month")


@st.cache_data
def load_fx() -> pd.Series | None:
    """Monthly average yen per US dollar (Fed series EXJPUS), indexed by month. None if no file."""
    try:
        fx = pd.read_csv(FX_PATH)
    except FileNotFoundError:
        return None
    fx["month"] = pd.to_datetime(fx["month"], format="%Y-%m")
    return fx.set_index("month")["jpy_per_usd"]


def with_usd(jp: pd.DataFrame, fx: pd.Series | None) -> pd.DataFrame:
    """Copy of the matcha data with USD columns added, in memory only.

    Each month's yen is divided by that same month's average rate. A month with no
    published rate gets a blank (NaN) USD value, never a borrowed or guessed rate.
    The yen columns are left as they are, and nothing is written back to the CSV.
    """
    out = jp.copy()
    out["jpy_per_usd"] = fx.reindex(out.index) if fx is not None else float("nan")
    for pack in PACKS:
        out[f"{pack}_value_usd"] = out[f"{pack}_value_yen"] / out["jpy_per_usd"]
        # Same as USD value / kg, since both months' figures use the same rate.
        out[f"{pack}_price_per_kg_usd"] = out[f"{pack}_price_per_kg"] / out["jpy_per_usd"]
    return out


def month_list(months: pd.DatetimeIndex) -> str:
    """Short text for a set of months, e.g. "Oct 2026" or "Sep 2026 and Oct 2026"."""
    names = [f"{m:%b %Y}" for m in months]
    if len(names) <= 3:
        return " and ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else names[0]
    return f"{len(names)} months ({names[0]} to {names[-1]})"


def stage_runs(stages: pd.Series) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    """Consecutive months that share a non-final stage, as (stage, first month, last month)."""
    runs: list[list] = []
    for month, stage in stages.items():
        if runs and runs[-1][0] == stage and runs[-1][2] + pd.DateOffset(months=1) == month:
            runs[-1][2] = month
        else:
            runs.append([stage, month, month])
    return [tuple(r) for r in runs if r[0] != "Final"]


def mark_stages(fig: go.Figure, stages: pd.Series, chart_px: int = 600) -> go.Figure:
    """Shade months that are not final yet, and name the stage above bands wide enough to fit it.

    chart_px is the rough plot width on screen (600 for half-width charts, 1250 for full-width).
    """
    total_months = len(stages)
    for stage, a, b in stage_runs(stages):
        # Points sit at the start of each month; each band edge goes halfway to the neighbouring
        # month, so adjacent bands meet exactly.
        prev_a, next_b = a - pd.DateOffset(months=1), b + pd.DateOffset(months=1)
        fig.add_vrect(x0=a - (a - prev_a) / 2, x1=b + (next_b - b) / 2,
                      fillcolor=STAGE_FILL[stage], line_width=0, layer="below")
        months = (b.year - a.year) * 12 + b.month - a.month + 1
        band_px = chart_px * months / total_months
        # About 7 px per character at 11 px text. Narrower bands get no label; the caption
        # under the charts and the hover text still name their stage.
        if band_px >= 7 * len(stage) + 12:
            fig.add_annotation(x=a + (b - a) / 2, y=1, xref="x", yref="paper", yanchor="bottom",
                               text=stage, showarrow=False, font=dict(size=11, color=MUTED_INK))
    fig.update_layout(margin=dict(t=24))
    return fig


def stage_caption(stages: pd.Series) -> str:
    runs = stage_runs(stages)
    if not runs:
        return "All months shown are final."
    parts = [f"{month_span(a, b)}: {stage}" for stage, a, b in runs]
    return ("Shaded months are not final yet, so Japan may still revise them ("
            + "; ".join(parts) + "). Darker shading = less final. Hover a month to see its stage.")


def matcha_section() -> None:
    fx = load_fx()
    jp = with_usd(load_matcha(), fx)
    all_months = pd.date_range(jp.index.min(), jp.index.max(), freq="MS")

    st.header("Japan's powdered green tea (matcha) exports to the US")

    currency = st.radio(
        "Currency", options=["JPY", "USD"], horizontal=True, key="matcha_currency",
        format_func=lambda c: {"JPY": "JPY (yen, as reported)", "USD": "USD (estimate)"}[c],
        help="Switches value and price per kg. Volume in kg is the same in both.",
    )
    usd = currency == "USD"
    if usd and fx is None:
        st.warning("No exchange rate file found (data/fx_rates.csv), so USD values cannot be shown. "
                   "Run `src/fetch_fx_data.py` to create it.")

    f1, f2 = st.columns([2, 3], gap="large")
    with f1:
        start, end = month_range_slider(all_months, key="matcha_range")
    with f2:
        pack = st.radio(
            "Pack type", options=list(PACKS), format_func=lambda p: PACKS[p][0],
            horizontal=True, key="matcha_pack",
        )
    label, color = PACKS[pack]
    kg_col = f"{pack}_kg"
    # Value and price columns for the chosen currency. Volume is the same in both.
    val_col = f"{pack}_value_usd" if usd else f"{pack}_value_yen"
    price_col = f"{pack}_price_per_kg_usd" if usd else f"{pack}_price_per_kg"
    sym = "$" if usd else "¥"

    rng = jp.loc[start:end]
    stages = rng["stage"]

    if usd:
        st.caption(f"Monthly exports, {start:%b %Y} to {end:%b %Y}. Source: Japan's Ministry of Finance "
                   "trade statistics, via e-Stat, reported in yen. **USD values are estimates**: each "
                   "month's yen is converted at that month's average exchange rate (Federal Reserve). "
                   "See About this data.")
        no_rate = rng.index[rng["jpy_per_usd"].isna()]
        if fx is not None and len(no_rate):
            st.warning(f"No exchange rate has been published yet for {month_list(no_rate)}, so USD is "
                       "left blank for that month rather than guessed. It appears as a gap in the value "
                       "and price charts, and the headline numbers below show n/a if their 12 months "
                       "include it. Its volume (kg) and yen values are shown as usual.")
    else:
        st.caption(f"Monthly exports, {start:%b %Y} to {end:%b %Y}. Source: Japan's Ministry of Finance "
                   "trade statistics, via e-Stat. **Values are in Japanese yen (JPY)** as reported.")

    # ---------------------------------------------------------------- headline numbers
    latest_start, prior_start, prior_end, has_prior = kpi_windows(end, all_months[0])

    def window(a, b):
        w = jp.loc[a:b]
        kg = w[kg_col].sum()
        # A sum would silently skip months with no rate, so a 12-month USD total would cover
        # fewer months than it says. Instead the USD total is n/a if any month lacks a rate.
        missing = w.index[w["jpy_per_usd"].isna()] if usd else w.index[:0]
        value = float("nan") if len(missing) else w[val_col].sum()
        price = value / kg if kg and not pd.isna(value) else float("nan")
        return value, kg, price, int((w["stage"] != "Final").sum()), missing

    lv, lk, lp, l_open, l_missing = window(latest_start, end)
    pv, pk, pp, p_open, p_missing = (window(prior_start, prior_end) if has_prior
                                     else (None, None, None, 0, jp.index[:0]))

    def money(x: float) -> str:
        return "n/a" if pd.isna(x) else (fmt_usd(x) if usd else fmt_yen(x))

    def per_kg(x: float) -> str:
        return "n/a" if pd.isna(x) else (f"${x:,.2f}" if usd else f"¥{x:,.0f}")

    est = ", estimate" if usd else ""
    st.subheader(f"{kpi_heading(latest_start, end, all_months[0])}: {label}")
    c1, c2, c3 = st.columns(3)
    c1.metric(f"Total export value ({currency}{est})", money(lv), pct_change(lv, pv) if has_prior else None,
              delta_color="off", border=True)
    c2.metric("Total volume", fmt_kg(lk), pct_change(lk, pk) if has_prior else None,
              delta_color="off", border=True)
    c3.metric(f"Average price per kg ({currency}{est})", per_kg(lp),
              pct_change(lp, pp) if has_prior else None, delta_color="off", border=True)
    notes = []
    if not has_prior:
        notes.append("Not enough earlier data for a prior 12-month comparison (the data starts in January 2019).")
    if l_open or p_open:
        notes.append(f"Includes months that are not final yet: {l_open} of the latest 12"
                     + (f" and {p_open} of the prior 12" if has_prior else "") + ".")
    if len(l_missing):
        notes.append(f"USD value and price are n/a because there is no published exchange rate yet for "
                     f"{month_list(l_missing)}. Move the end of the date range earlier to see USD totals.")
    elif len(p_missing):
        notes.append(f"No USD comparison: no published exchange rate for {month_list(p_missing)}.")
    if notes:
        st.caption(" ".join(notes))

    # ---------------------------------------------------------------- selected pack type
    stage_list = stages.to_numpy()
    left, right = st.columns(2)
    with left:
        st.markdown(f"**Monthly export volume**: {label}")
        st.plotly_chart(mark_stages(line_chart(
            rng[kg_col], "Volume", "kg", "~s", "%{y:,.0f} kg · %{customdata}",
            color=color, customdata=stage_list), stages), width="stretch", key="jp_volume")
    with right:
        st.markdown(f"**Monthly export value**: {label}")
        if usd:
            value_args = ("Million US dollars (USD, estimate)", ",.0f", "$%{y:,.2f}M · %{customdata}")
        else:
            value_args = ("Million yen (JPY)", ",.0f", "¥%{y:,.1f}M · %{customdata}")
        st.plotly_chart(mark_stages(line_chart(
            rng[val_col] / 1e6, "Value", *value_args,
            color=color, customdata=stage_list), stages), width="stretch", key="jp_value")

    price_axis = "US dollars per kg (USD, estimate)" if usd else "Yen per kg (JPY)"
    price_fmt = "$,.0f" if usd else ",.0f"
    price_hover = "$%{y:,.2f}" if usd else "¥%{y:,.0f}"
    st.markdown(f"**Average price per kg**: {label} (total {'USD' if usd else 'yen'} divided by total kg)")
    st.plotly_chart(mark_stages(line_chart(
        rng[price_col], "Price per kg", price_axis, price_fmt, price_hover + " per kg · %{customdata}",
        color=color, customdata=stage_list), stages, chart_px=1250), width="stretch", key="jp_price")
    st.caption(stage_caption(stages))

    # ---------------------------------------------------------------- small vs bulk
    st.subheader("Small packs vs bulk packs")
    st.caption("Both pack types side by side for the selected dates. Together they make up the combined total.")
    left, right = st.columns(2)
    with left:
        st.markdown("**Monthly export volume by pack type** (stacked = combined total)")
        fig = go.Figure()
        for p in ("small_pack", "bulk"):
            fig.add_trace(go.Bar(
                x=rng.index, y=rng[f"{p}_kg"], name=PACKS[p][0], customdata=stage_list,
                marker=dict(color=PACKS[p][1], line=dict(color=SURFACE, width=1)),
                hovertemplate=f"{PACKS[p][0]}: %{{y:,.0f}} kg · %{{customdata}}<extra></extra>",
            ))
        base_layout(fig, "kg", "~s")
        fig.update_layout(barmode="stack", bargap=0.15, height=360, showlegend=True,
                          legend=dict(orientation="h", yanchor="top", y=-0.1, x=0, traceorder="normal"))
        st.plotly_chart(mark_stages(fig, stages), width="stretch", key="jp_volume_by_pack")
    with right:
        st.markdown(f"**Average price per kg by pack type** ({currency}{est})")
        fig = go.Figure()
        mode = "lines+markers" if len(rng) < 3 else "lines"
        suffix = "_usd" if usd else ""
        for p, (p_label, p_color) in PACKS.items():
            fig.add_trace(go.Scatter(
                x=rng.index, y=rng[f"{p}_price_per_kg{suffix}"], name=p_label, mode=mode, customdata=stage_list,
                line=dict(color=p_color, width=2), marker=dict(size=8),
                hovertemplate=f"{p_label}: {price_hover} per kg · %{{customdata}}<extra></extra>",
            ))
        base_layout(fig, price_axis, price_fmt)
        fig.update_layout(height=360, showlegend=True,
                          legend=dict(orientation="h", yanchor="top", y=-0.1, x=0))
        st.plotly_chart(mark_stages(fig, stages), width="stretch", key="jp_price_by_pack")

    # ---------------------------------------------------------------- table view
    with st.expander("View data table"):
        table = rng.reset_index()
        table["month"] = table["month"].dt.strftime("%Y-%m")
        cols = ["month", "stage"]
        config = {"month": "Month", "stage": "Revision stage"}
        if usd:
            # Show the rate used for each month, so every USD figure can be checked by hand.
            cols.append("jpy_per_usd")
            config["jpy_per_usd"] = st.column_config.NumberColumn("Yen per USD (monthly average)", format="%.4f")
        for p, (p_label, _) in PACKS.items():
            short = p_label.split(" (")[0]
            cols += [f"{p}_kg", f"{p}_value_yen", f"{p}_price_per_kg"]
            config[f"{p}_kg"] = st.column_config.NumberColumn(f"{short}: volume (kg)", format="%d")
            config[f"{p}_value_yen"] = st.column_config.NumberColumn(f"{short}: value (JPY)", format="¥%d")
            config[f"{p}_price_per_kg"] = st.column_config.NumberColumn(f"{short}: price per kg (JPY)", format="¥%.0f")
            if usd:
                cols += [f"{p}_value_usd", f"{p}_price_per_kg_usd"]
                config[f"{p}_value_usd"] = st.column_config.NumberColumn(
                    f"{short}: value (USD, estimate)", format="$%d")
                config[f"{p}_price_per_kg_usd"] = st.column_config.NumberColumn(
                    f"{short}: price per kg (USD, estimate)", format="$%.2f")
        cols.append("estat_table_id")
        config["estat_table_id"] = "e-Stat table ID"
        st.dataframe(table[cols].sort_values("month", ascending=False), hide_index=True,
                     width="stretch", column_config=config)

    # ---------------------------------------------------------------- about
    st.divider()
    st.subheader("About this data")
    stage_rows = "\n".join(
        f"| {STAGE_EN[ja]} | {ja} ({STAGE_JA_READING[ja]}) | {meaning} |"
        for ja, meaning in [
            ("確報", "Monthly confirmed figures, published about a month after the month ends."),
            ("確々報", "Annual revision, published the following March."),
            ("確定", "Final figures, published the following November. These no longer change."),
        ]
    )
    tables_used = "; ".join(
        f"`{tid}` ({g.index.min():%b %Y} to {g.index.max():%b %Y})"
        for tid, g in jp.groupby("estat_table_id", sort=False)
    )
    if fx is None:
        fx_note = "The exchange rate file has not been downloaded yet, so no USD figures are available."
    else:
        no_rate = jp.index[jp["jpy_per_usd"].isna()]
        fx_note = (f"Exchange rates available: {fx.index.min():%B %Y} to {fx.index.max():%B %Y}, "
                   f"last pulled **{last_pulled(FX_PULLED_AT_PATH, FX_PATH)}**. A month's rate is used only "
                   "once the month is over, never a partial-month figure. ")
        fx_note += (f"No rate has been published yet for {month_list(no_rate)}, so its USD values are "
                    "left blank." if len(no_rate) else "Every month in the matcha data has a published rate.")
    st.markdown(
        f"""
**What this measures.** Exports of **powdered green tea** from Japan to the United States.
Japan's trade statistics have no separate category for matcha, so powdered green tea is the
closest official measure. It covers all powdered green tea, so it can include powdered teas
that are not strictly matcha. Two codes are used:

- `0902.10-100`: powdered green tea in packs of 3 kg or less (shown as **small packs**)
- `0902.20-100`: powdered green tea in packs over 3 kg (shown as **bulk packs**)

The data starts in **January 2019**, when Japan introduced these codes. Earlier
figures for powdered tea are not available on their own.

**Where it comes from.** Japan's customs agency compiles these figures, published by the
Ministry of Finance as its trade statistics (普通貿易統計) and distributed through e-Stat.
The **US imports** tab comes from the US side (US Census Bureau). The two will not
match exactly: they are recorded by different countries at different points in the
shipment (leaving Japan vs arriving in the US), valued differently, in different
currencies, and the US codes cover all green tea, not only powdered.

**Currency.** Values are reported in **Japanese yen**. e-Stat publishes them in thousands
of yen, which we multiply by 1,000. Price per kg is the month's total value divided by its
total kg. The yen figures in the data file are never changed; USD is calculated on the page.

**USD estimates.** The exchange rate is the Federal Reserve Board's **Japanese Yen to U.S.
Dollar Spot Exchange Rate** (G.5 release, series `EXJPUS`, via FRED), in yen per dollar.
Each month's yen value is divided by that same month's average rate, which the Fed
calculates from the daily noon buying rates in New York. These USD figures are
**estimates**: the actual shipments were priced and paid on different days, at different
rates. Even in USD, these figures will not match the US imports tab, for the reasons above.
{fx_note}

**Revision stages.** Japan publishes each month's figures and then revises them in steps.
Months that are not final are shaded in the charts.

| On this dashboard | Japanese term | Meaning |
|---|---|---|
{stage_rows}

**Checked against official totals.** The monthly figures were checked against e-Stat's
official annual totals: for every year from 2019 to 2025, and for January to September 2026,
the monthly kg and yen figures for both pack types add up exactly to e-Stat's published
totals (checked November 11, 2026, against the data then in this file).

Data last pulled: **{last_pulled(MATCHA_PULLED_AT_PATH, MATCHA_PATH)}**. Covers
{all_months[0]:%B %Y} to {all_months[-1]:%B %Y}. e-Stat tables used: {tables_used}.
"""
    )


# ================================================================ page

st.set_page_config(page_title="Green Tea Trade: Japan and the US", layout="wide")
st.title("Green Tea Trade: Japan and the US")
st.caption("Two separate datasets from two different countries. Each tab has its own filters and notes.")
# Each date is when that dataset last changed: the daily refresh job saves a new pull
# time only together with new numbers.
st.markdown(
    "**Data last updated:** "
    f"US imports {last_pulled(PULLED_AT_PATH, DATA_PATH)} · "
    f"Japan matcha exports {last_pulled(MATCHA_PULLED_AT_PATH, MATCHA_PATH)} · "
    f"Exchange rates {last_pulled(FX_PULLED_AT_PATH, FX_PATH)}"
)

us_tab, matcha_tab = st.tabs(["US imports of green tea (USD)", "Japan's matcha exports (JPY / USD)"])
with us_tab:
    us_section()
with matcha_tab:
    matcha_section()
