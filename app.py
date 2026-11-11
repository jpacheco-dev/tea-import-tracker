"""Streamlit dashboard for US green tea imports from Japan.

Reads only data/tea_imports.csv (produced by src/fetch_data.py). It never calls
the Census API and does not need the API key.

Run: .venv/bin/streamlit run app.py
"""

from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DATA_PATH = Path(__file__).resolve().parent / "data" / "tea_imports.csv"
PULLED_AT_PATH = DATA_PATH.parent / "last_pulled.txt"

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
SINGLE_SERIES_COLOR = "#2a78d6"
SURFACE = "#ffffff"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
MUTED_INK = "#52514e"


@st.cache_data
def load_data() -> pd.DataFrame:
    df = pd.read_csv(DATA_PATH, dtype={"hs6_code": str, "hs10_code": str, "country_code": str})
    df["month"] = pd.to_datetime(df["month"], format="%Y-%m")
    df["product"] = df["hs10_code"].map(lambda c: PRODUCTS.get(c, (c, None))[0])
    return df


def last_pulled() -> str:
    # fetch_data.py writes the pull time (UTC, ISO 8601) to last_pulled.txt.
    # If that file is missing or unreadable, fall back to the CSV's modified
    # date and say so, since copying or cloning the repo can change it.
    try:
        pulled = datetime.fromisoformat(PULLED_AT_PATH.read_text().strip())
        return pulled.strftime("%B %-d, %Y")
    except (OSError, ValueError):
        mtime = datetime.fromtimestamp(DATA_PATH.stat().st_mtime)
        return f"{mtime:%B %-d, %Y} (estimated from file date)"


def monthly_totals(df: pd.DataFrame, months: pd.DatetimeIndex) -> pd.DataFrame:
    """Sum value and kg per month. Months with no rows count as zero imports."""
    out = df.groupby("month")[["value_usd", "quantity"]].sum().reindex(months, fill_value=0)
    # Price is total value / total kg (weighted), not an average of per-row prices.
    out["price_per_kg"] = out["value_usd"] / out["quantity"].where(out["quantity"] > 0)
    out.index.name = "month"
    return out


def fmt_usd(x: float) -> str:
    if abs(x) >= 1e6:
        return f"${x / 1e6:,.1f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:,.0f}K"
    return f"${x:,.0f}"


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


def line_chart(series: pd.Series, name: str, y_title: str, y_format: str, hover: str) -> go.Figure:
    fig = go.Figure(go.Scatter(
        x=series.index, y=series.values, name=name, mode="lines",
        line=dict(color=SINGLE_SERIES_COLOR, width=2),
        hovertemplate=hover + "<extra></extra>",
    ))
    return base_layout(fig, y_title, y_format)


# ---------------------------------------------------------------- page
st.set_page_config(page_title="Green Tea Imports from Japan", layout="wide")

df = load_data()
all_months = pd.date_range(df["month"].min(), df["month"].max(), freq="MS")

# ---------------------------------------------------------------- sidebar filters
st.sidebar.header("Filters")
start, end = st.sidebar.select_slider(
    "Date range",
    options=list(all_months),
    value=(all_months[0], all_months[-1]),
    format_func=lambda d: d.strftime("%b %Y"),
)
st.sidebar.markdown("**Product category**")
selected = [
    label for code, (label, _) in PRODUCTS.items()
    if st.sidebar.checkbox(label, value=True, key=f"product_{code}")
]
st.sidebar.caption("Packaged = retail packs of 3 kg or less. Bulk = larger packs.")

if not selected:
    st.title("US Green Tea Imports from Japan")
    st.info("Select at least one product category in the sidebar.")
    st.stop()

by_product = df[df["product"].isin(selected)]
range_months = pd.date_range(start, end, freq="MS")
in_range = by_product[by_product["month"].between(start, end)]
totals = monthly_totals(in_range, range_months)

# ---------------------------------------------------------------- header + KPIs
st.title("US Green Tea Imports from Japan")
st.caption(f"Monthly general imports, {start:%b %Y} to {end:%b %Y}. Source: US Census Bureau.")

# Latest 12 months = the 12 ending at the selected end month; prior = the 12 before.
# Uses the product filter but looks back past the start of the date range if needed.
latest_start = end - pd.DateOffset(months=11)
prior_start = end - pd.DateOffset(months=23)
prior_end = end - pd.DateOffset(months=12)
has_prior = prior_start >= all_months[0]


def window(a, b):
    w = by_product[by_product["month"].between(a, b)]
    value, kg = w["value_usd"].sum(), w["quantity"].sum()
    return value, kg, (value / kg if kg else float("nan"))


lv, lk, lp = window(latest_start, end)
pv, pk, pp = window(prior_start, prior_end) if has_prior else (None, None, None)

st.subheader(f"Last 12 months ({latest_start:%b %Y} to {end:%b %Y})")
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

# ---------------------------------------------------------------- charts
left, right = st.columns(2)
with left:
    st.markdown("**Monthly import volume**")
    st.plotly_chart(
        line_chart(totals["quantity"], "Volume", "kg", "~s", "%{y:,.0f} kg"),
        width="stretch",
    )
with right:
    st.markdown("**Monthly import value**")
    st.plotly_chart(
        line_chart(totals["value_usd"], "Value", "USD", "$~s", "$%{y:,.0f}"),
        width="stretch",
    )

st.markdown("**Average price per kg** (total value divided by total kg)")
st.plotly_chart(
    line_chart(totals["price_per_kg"], "Price per kg", "USD per kg", "$.0f", "$%{y:,.2f} per kg"),
    width="stretch",
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
st.plotly_chart(fig, width="stretch")

# ---------------------------------------------------------------- table view
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

# ---------------------------------------------------------------- about
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

Data last pulled: **{last_pulled()}**. Covers {all_months[0]:%B %Y} to {all_months[-1]:%B %Y}.
Months with no recorded imports for a product are shown as zero.
"""
)
