#!/usr/bin/env python3
"""Fetches annual + Tesla quarterly financials via yfinance and writes data.json.

Merge rule: a fetched value only replaces what's already in data.json when it is
present and not NaN. Missing/failed values fall back to whatever was already
recorded (data.json on disk, or the built-in SEED on the very first run) so a
bad fetch can never blank out good data. Delivery figures for the other 7
companies (and Tesla's annual del_total/del_bev/del_dm rollup) are still never
fetched — they carry forward untouched (manual data, edited via the dashboard's
"Edit Data" panel / by hand). Tesla's *quarterly* production/delivery numbers
are the one exception: they're pulled from Tesla's own SEC EDGAR 8-K filings
(see fetch_tesla_deliveries_from_sec) since there's no financial-statement API
for that, but SEC's filing archive is a free, stable, no-auth source for it.
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("fetcher")

# SEC's bot-detection WAF requires something that looks like a real
# "name contact@domain" User-Agent (their documented format) — generic
# strings, and even email-shaped strings on a handful of common domains
# (github.com among them), get a 403 "Undeclared Automated Tool" response
# regardless of request volume. example.com (reserved for exactly this kind
# of documentation/placeholder use) works as a default; set SEC_USER_AGENT
# as a repo secret with your own contact email for a more compliant,
# longer-term-reliable identifier per SEC's fair-access policy.
# .get(..., default) isn't enough here: GitHub Actions sets the env var to
# an empty string (not unset) when the referenced secret doesn't exist.
SEC_USER_AGENT = os.environ.get('SEC_USER_AGENT') or 'car-industry-dashboard-fetcher contact@example.com'
TESLA_CIK = '0001318605'

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

COMPANIES = {
    'tesla':      {'ticker': 'TSLA',  'name': 'Tesla',      'color': '#DC2626'},
    'ford':       {'ticker': 'F',     'name': 'Ford',       'color': '#1D4ED8'},
    'gm':         {'ticker': 'GM',    'name': 'GM',         'color': '#0D9488'},
    'vw':         {'ticker': 'VWAGY', 'name': 'Volkswagen', 'color': '#EA580C'},
    'bmw':        {'ticker': 'BMWYY', 'name': 'BMW',        'color': '#2563EB'},
    'mercedes':   {'ticker': 'MBGYY', 'name': 'Mercedes',   'color': '#525252'},
    'stellantis': {'ticker': 'STLA',  'name': 'Stellantis', 'color': '#9333EA'},
    'byd':        {'ticker': 'BYDDY', 'name': 'BYD',        'color': '#16A34A'},
}

# yfinance reports financial-statement line items in the filer's reporting
# currency, not the ADR's USD trading currency — e.g. BYDDY revenue comes
# back in CNY (~7x USD), VW/BMW/Mercedes/Stellantis in EUR. These hints are
# used as a fallback if Yahoo's `financialCurrency` metadata is unavailable.
CURRENCY_HINTS = {
    'tesla': 'USD', 'ford': 'USD', 'gm': 'USD',
    'vw': 'EUR', 'bmw': 'EUR', 'mercedes': 'EUR', 'stellantis': 'EUR',
    'byd': 'CNY',
}
# Only used if a live FX quote can't be fetched.
FX_FALLBACK_TO_USD = {'USD': 1.0, 'EUR': 1.08, 'CNY': 0.14}

DELIVERIES_NOTE = "Updated manually — see Edit Data panel in dashboard"

# ═══════════════════════════════════════════════════════════════════════
# SEED DATA — the dashboard's original built-in dataset. Used as the
# baseline on the very first run (before data.json exists) and as the
# source of truth for fields that are never fetched (deliveries, Tesla
# segment revenue).
# ═══════════════════════════════════════════════════════════════════════
SEED_YEARS = ['2017', '2018', '2019', '2020', '2021', '2022', '2023', '2024', '2025', 'H1 26*']

SEED_PEERS = {
    'tesla': {
        'dash': [],
        'margin':  [-13.9, -1.8, -0.3, 6.3, 12.1, 16.8, 9.2, 7.2, 4.5, 2.6],
        'revenue': [11.76, 21.46, 24.58, 31.54, 53.82, 81.5, 96.8, 97.7, 94.8, 50.6],
        'ni':      [-1.96, -0.98, -0.87, 0.72, 5.52, 12.6, 15.0, 7.3, 3.8, 1.6],
        'fcf':     [-3.2, -0.22, 1.07, 2.8, 5.0, 7.6, 4.4, 3.5, 6.2, 0.35],
        'capex':   [3.4, 2.1, 1.33, 1.49, 6.48, 7.0, 8.9, 9.3, 8.5, 8.3],
        'del_total': [103, 245, 367, 500, 936, 1314, 1809, 1789, 1636, 817],
        'del_bev':   [103, 245, 367, 500, 936, 1314, 1809, 1789, 1636, 817],
        'del_dm':    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    },
    'ford': {
        'dash': [5, 3],
        'margin':  [3.5, 2.0, -0.1, 0.5, 3.6, 4.0, 3.1, 2.8, -4.9, 6.6],
        'revenue': [156.8, 160.3, 155.9, 127.1, 136.3, 158.1, 176.2, 185.0, 187.3, 91.5],
        'ni':      [7.6, 3.7, 0.05, -1.3, 1.8, -2.0, 4.6, 5.9, -8.2, 1.2],
        'fcf':     [2.6, 0.6, 2.4, -3.9, 4.5, 9.1, 6.0, 5.4, 2.5, 0.2],
        'capex':   [7.0, 7.8, 7.0, 5.7, 6.8, 7.7, 8.3, 8.4, 9.0, None],
        'del_total': [5600, 5400, 5400, 4200, 3900, 4200, 4400, 4300, 4100, None],
        'del_bev':   [0, 0, 0, 0, 3, 80, 72, 87, 75, None],
        'del_dm':    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    },
    'gm': {
        'dash': [],
        'margin':  [8.0, 7.5, 7.5, 3.5, 11.3, 9.2, 7.2, 7.5, 5.5, 9.0],
        'revenue': [145.6, 147.0, 137.2, 122.5, 127.0, 156.7, 171.8, 187.4, 190.0, 91.6],
        'ni':      [-3.9, 8.0, 6.7, 6.4, 10.0, 9.9, 10.0, 6.0, 5.0, 3.9],
        'fcf':     [5.0, 4.0, 6.5, 3.0, 7.5, 10.5, 10.5, 11.5, 9.0, None],
        'capex':   [7.0, 8.0, 7.5, 5.5, 8.0, 7.0, 8.5, 7.5, 7.0, None],
        'del_total': [9600, 8900, 7700, 6800, 6700, 5900, 6200, 6400, 6200, None],
        'del_bev':   [0, 0, 0, 0, 26, 39, 75, 130, 110, 57],
        'del_dm':    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    },
    'vw': {
        'dash': [6, 3],
        'margin':  [6.8, 6.7, 4.2, 2.9, 7.9, 8.1, 7.0, 5.9, 2.8, 3.8],
        'revenue': [260.7, 278.5, 282.7, 254.3, 296.0, 308.6, 348.0, 351.0, 343.0, 170.6],
        'ni':      [12.9, 14.4, 15.7, 11.4, 18.2, 16.2, 16.3, 6.1, 2.7, 3.3],
        'fcf':     [5.1, 2.4, 2.2, 1.1, 5.3, 7.4, 8.6, 5.4, 6.9, 3.5],
        'capex':   [15.8, 17.7, 20.1, 16.0, 17.7, 17.9, 20.5, 19.5, 16.2, None],
        'del_total': [10742, 10834, 10975, 9305, 8882, 8263, 9237, 9038, 8800, 4130],
        'del_bev':   [29, 80, 134, 231, 453, 572, 771, 866, 900, 438],
        'del_dm':    [0, 0, 0, 35, 95, 145, 193, 295, 350, 246],
    },
    'bmw': {
        'dash': [],
        'margin':  [8.9, 7.2, 4.0, 3.3, 10.3, 8.6, 9.8, 8.8, 6.5, 3.6],
        'revenue': [111.5, 115.1, 116.6, 112.4, 131.6, 150.2, 167.8, 154.2, 140.4, 67.2],
        'ni':      [9.8, 8.5, 5.6, 4.5, 14.7, 19.6, 13.2, 9.4, 8.1, 3.1],
        'fcf':     [5.1, 2.4, 0.6, 2.9, 5.9, 5.3, 7.6, 5.6, 5.4, 1.4],
        'capex':   [5.7, 5.9, 6.2, 5.1, 6.5, 6.8, 8.1, 7.6, 7.0, None],
        'del_total': [2464, 2490, 2520, 2326, 2521, 2399, 2554, 2437, 2350, 1157],
        'del_bev':   [33, 37, 25, 44, 104, 216, 376, 427, 440, 204],
        'del_dm':    [35, 55, 60, 55, 65, 95, 130, 150, 175, 91],
    },
    'mercedes': {
        'dash': [4, 4],
        'margin':  [9.9, 7.8, 4.3, 3.5, 12.9, 13.7, 12.9, 9.3, 6.2, 4.0],
        'revenue': [185.7, 197.7, 193.3, 176.0, 198.8, 161.3, 165.7, 157.7, 142.7, 68.7],
        'ni':      [12.3, 9.0, 3.0, 4.6, 28.2, 15.6, 15.7, 11.3, 5.9, 2.7],
        'fcf':     [5.7, 4.7, 4.5, 6.3, 11.8, 8.4, 11.3, 10.0, 5.8, 3.2],
        'capex':   [7.3, 8.3, 7.8, 6.3, 7.1, 4.7, 5.2, 4.9, 4.3, None],
        'del_total': [2289, 2310, 2340, 2165, 2094, 2041, 2042, 1850, 1750, 1012],
        'del_bev':   [1, 2, 5, 20, 67, 118, 230, 205, 190, 121],
        'del_dm':    [10, 20, 25, 30, 40, 100, 160, 175, 180, None],
    },
    'stellantis': {
        'dash': [8, 3, 2, 3],
        'margin':  [None, None, None, None, 11.8, 13.6, 12.8, 5.5, -0.5, 2.2],
        'revenue': [None, None, None, None, 180.0, 189.1, 204.8, 169.9, 165.6, 88.0],
        'ni':      [None, None, None, None, 15.0, 15.7, 19.8, 6.0, -24.1, 0.76],
        'fcf':     [None, None, None, None, 8.3, 10.5, 8.3, -6.5, -4.9, -0.97],
        'capex':   [None, None, None, None, 8.9, 7.4, 7.9, 6.3, 5.2, None],
        'del_total': [None, None, None, None, 6100, 5600, 6200, 5800, 5400, 2961],
        'del_bev':   [None, None, None, None, 44, 86, 100, 110, 90, None],
        'del_dm':    [None, None, None, None, 200, 350, 500, 560, 520, None],
    },
    'byd': {
        'dash': [],
        'margin':  [2.5, 2.0, 1.8, 2.2, 2.5, 5.4, 6.4, 7.0, 4.8, None],
        'revenue': [15.6, 19.8, 18.1, 22.7, 33.5, 62.8, 84.3, 107.2, 110.1, None],
        'ni':      [0.6, 0.4, 0.2, 0.6, 0.5, 2.5, 4.2, 5.6, 4.8, 0.6],
        'fcf':     [-0.5, -1.0, 0.5, 1.5, 0.5, 1.5, 4.2, 8.3, 6.9, None],
        'capex':   [2.5, 3.6, 4.0, 4.1, 7.3, 7.4, 11.2, 13.8, 12.3, None],
        'del_total': [447, 521, 462, 427, 593, 1868, 3024, 4272, 4272, None],
        'del_bev':   [113, 228, 219, 182, 325, 911, 1575, 1765, 1900, 975],
        'del_dm':    [334, 293, 243, 245, 268, 957, 1449, 2507, 2600, None],
    },
}

SEED_TESLA_QUARTERLY = {
    'labels': ['Q1 22', 'Q2 22', 'Q3 22', 'Q4 22', 'Q1 23', 'Q2 23', 'Q3 23', 'Q4 23',
               'Q1 24', 'Q2 24', 'Q3 24', 'Q4 24', 'Q1 25', 'Q2 25', 'Q3 25', 'Q4 25', 'Q1 26', 'Q2 26'],
    'tRA':  [14736, 14602, 18692, 21307, 19963, 21268, 19625, 21563, 17378, 19878, 20016, 19798, 13967, 16661, 21205, 17693, 16234, 20520],
    'tRE':  [1072, 866, 1117, 1310, 1529, 1509, 1559, 1438, 1635, 3014, 2376, 3061, 2730, 2789, 3415, 3837, 2408, 3140],
    'tRS':  [1948, 1466, 1645, 1701, 1837, 2150, 2166, 2166, 2288, 2608, 2790, 2848, 2638, 3046, 3475, 3371, 3745, 4580],
    'tOM':  [19.2, 14.6, 17.2, 16.0, 11.4, 9.6, 7.6, 8.2, 5.5, 6.3, 10.8, 6.2, 2.1, 4.1, 5.8, 5.7, 4.2, 1.4],
    'tGM':  [29.1, 25.0, 25.1, 23.8, 19.3, 18.2, 17.9, 17.6, 17.4, 18.0, 19.8, 16.3, 16.3, 17.2, 18.0, 20.1, 21.1, 16.8],
    'tNI':  [3318, 2259, 3292, 3687, 2513, 2703, 1853, 7928, 1390, 1400, 2173, 2317, 409, 1172, 1373, 840, 477, 1110],
    'tFCF': [2228, 621, 3284, 1420, 441, 1005, 848, 2064, -2531, 1300, 2742, 2034, 664, 146, 3990, 1420, 1440, -1090],
    'tCX':  [1700, 1652, 1803, 1858, 2072, 2060, 2460, 2306, 2731, 2300, 1500, 2780, 1492, 2394, 2248, 2393, 2505, 5790],
    'tCS':  [19600, 18900, 21100, 22185, 22402, 23075, 26077, 29094, 26863, 30700, 33600, 36563, 36996, 36782, 41647, 44059, 44759, 43520],
}

SEED_TESLA_DELIVERIES = {
    'labels': ['Q1 17', 'Q2 17', 'Q3 17', 'Q4 17', 'Q1 18', 'Q2 18', 'Q3 18', 'Q4 18', 'Q1 19', 'Q2 19', 'Q3 19', 'Q4 19',
               'Q1 20', 'Q2 20', 'Q3 20', 'Q4 20', 'Q1 21', 'Q2 21', 'Q3 21', 'Q4 21',
               'Q1 22', 'Q2 22', 'Q3 22', 'Q4 22', 'Q1 23', 'Q2 23', 'Q3 23', 'Q4 23',
               'Q1 24', 'Q2 24', 'Q3 24', 'Q4 24', 'Q1 25', 'Q2 25', 'Q3 25', 'Q4 25', 'Q1 26', 'Q2 26'],
    'production': [25418, 25708, 25336, 24565, 34412, 53339, 80142, 86555, 77138, 87048, 96155, 104891,
                   102672, 82272, 145063, 179757, 180338, 206421, 237823, 305840,
                   305407, 258580, 365923, 439701, 440808, 479700, 430488, 494989,
                   433371, 410831, 469796, 459445, 362615, 410244, 447450, 434358, 408386, 451758],
    'deliveries': [25000, 22000, 26150, 29870, 30300, 40740, 83500, 90700, 63019, 95356, 97186, 112095,
                   88496, 90891, 139593, 180667, 184877, 201304, 241391, 308600,
                   310048, 254695, 343830, 405278, 422875, 466140, 435059, 484507,
                   386810, 443956, 462890, 495570, 336681, 384122, 497099, 418227, 358023, 480126],
}

HIGHLIGHTS_NOTE = "Updated manually — edit the 'highlights' array in data.json directly (not yet exposed in the Edit Data panel)"

# The header stat cards. Never auto-computed — some of this (YoY deltas,
# "ahead of Tesla on BEV", "recovering") is editorial judgment, not
# something derivable from the numbers in `peers`/`tesla_quarterly`. Kept
# here (rather than hardcoded in index.html) purely so there's one source
# of truth instead of two copies that can silently drift apart.
SEED_HIGHLIGHTS = [
    {'id': 'tesla', 'color': '#DC2626', 'label': 'Tesla H1 2026 deliveries',
     'value': '817k', 'sub': 'All BEV · +21% YoY · op margin 2.6%'},
    {'id': 'byd', 'color': '#16A34A', 'label': 'BYD Q2 2026 BEV',
     'value': '557k', 'sub': 'BEV only · ahead of Tesla Q2 on BEV'},
    {'id': 'vw', 'color': '#EA580C', 'label': 'VW Group H1 2026',
     'value': '4.13M', 'sub': 'Total · 438k BEV (10.6% share)'},
    {'id': 'bmw', 'color': '#2563EB', 'label': 'BMW Group H1 2026',
     'value': '1.16M', 'sub': 'Total · 204k BEV (17.7% share)'},
    {'id': 'stellantis', 'color': '#9333EA', 'label': 'Stellantis H1 2026',
     'value': '2.96M', 'sub': 'Total shipments · recovering'},
]

# ═══════════════════════════════════════════════════════════════════════
# yfinance row-name lookups (Yahoo renames these occasionally across
# versions, so each field tries a few known aliases)
# ═══════════════════════════════════════════════════════════════════════
REV_NAMES = ['Total Revenue', 'TotalRevenue']
OPINC_NAMES = ['Operating Income', 'OperatingIncome']
NI_NAMES = ['Net Income', 'Net Income Common Stockholders', 'NetIncome']
OCF_NAMES = ['Operating Cash Flow', 'Total Cash From Operating Activities', 'CashFlowFromContinuingOperatingActivities']
CAPEX_NAMES = ['Capital Expenditure', 'Capital Expenditures', 'CapitalExpenditure']
GP_NAMES = ['Gross Profit', 'GrossProfit']
CASH_NAMES = ['Cash Cash Equivalents And Short Term Investments', 'Cash And Cash Equivalents', 'CashAndCashEquivalents']


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _row(df, names):
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            return df.loc[n]
    return None


def _series_by_year(df, names):
    row = _row(df, names)
    if row is None:
        return {}
    return {col.year: _num(row.get(col)) for col in df.columns}


def _series_by_quarter(df, names):
    row = _row(df, names)
    if row is None:
        return {}
    return {quarter_label(col): _num(row.get(col)) for col in df.columns}


def quarter_label(ts):
    q = (ts.month - 1) // 3 + 1
    return f"Q{q} {ts.year % 100:02d}"


def quarter_sort_key(label):
    qpart, yy = label.split(' ')
    return (int(yy), int(qpart[1:]))


def arrays_to_dict(labels, arr):
    return {label: (arr[i] if i < len(arr) else None) for i, label in enumerate(labels)}


def get_financial_currency(ticker, hint):
    try:
        cur = yf.Ticker(ticker).get_info().get('financialCurrency')
        if cur:
            return cur
    except Exception as e:
        log.warning(f"{ticker}: could not read financialCurrency ({e}); using hint {hint}")
    return hint


def fx_rate_to_usd(currency):
    """1 unit of `currency` in USD, live spot rate. Used only as a fallback
    when a year is missing from fx_yearly_averages()."""
    if currency == 'USD':
        return 1.0
    pair = f'{currency}USD=X'
    try:
        rate = yf.Ticker(pair).fast_info.get('lastPrice')
        rate = float(rate)
        if rate and rate > 0:
            return rate
    except Exception as e:
        log.warning(f"FX spot lookup failed for {pair} ({e}); using fallback rate")
    return FX_FALLBACK_TO_USD.get(currency, 1.0)


def fx_yearly_averages(currency, start_year=2017):
    """{year: average `currency`-to-USD rate for that calendar year}, from
    daily FX history. A period average is a much closer match to how
    companies actually translate a full year of foreign-currency financials
    than a single current spot rate would be."""
    if currency == 'USD':
        return {}
    pair = f'{currency}USD=X'
    try:
        hist = yf.Ticker(pair).history(start=f'{start_year}-01-01')
        if hist is None or hist.empty:
            return {}
        closes = hist['Close'].dropna()
        yearly = closes.groupby(closes.index.year).mean()
        return {int(y): float(v) for y, v in yearly.items()}
    except Exception as e:
        log.warning(f"FX history lookup failed for {pair} ({e}); yearly figures will use spot fallback")
        return {}


# ═══════════════════════════════════════════════════════════════════════
# FETCHING
# ═══════════════════════════════════════════════════════════════════════
def fetch_annual_metrics(ticker, fx_by_year=None, fx_fallback=1.0):
    """Return {year: {'margin','revenue','ni','fcf','capex'}} for years with usable data.
    fx_by_year converts the filer's reporting currency to USD per calendar
    year (falling back to fx_fallback for years it doesn't cover); margin is
    a ratio so it's unaffected either way."""
    fx_by_year = fx_by_year or {}
    t = yf.Ticker(ticker)
    inc = t.income_stmt
    cf = t.cashflow
    rev = _series_by_year(inc, REV_NAMES)
    op = _series_by_year(inc, OPINC_NAMES)
    ni = _series_by_year(inc, NI_NAMES)
    ocf = _series_by_year(cf, OCF_NAMES)
    capex = _series_by_year(cf, CAPEX_NAMES)

    out = {}
    for year, r in rev.items():
        if not r:
            continue
        fx = fx_by_year.get(year, fx_fallback)
        entry = {'revenue': round(r * fx / 1e9, 2)}
        o = op.get(year)
        entry['margin'] = round(o / r * 100, 2) if o is not None else None
        n = ni.get(year)
        entry['ni'] = round(n * fx / 1e9, 2) if n is not None else None
        oc, cx = ocf.get(year), capex.get(year)
        entry['fcf'] = round((oc - abs(cx)) * fx / 1e9, 2) if oc is not None and cx is not None else None
        entry['capex'] = round(abs(cx) * fx / 1e9, 2) if cx is not None else None
        out[year] = entry
    return out


def fetch_tesla_quarterly(ticker='TSLA'):
    """Return {quarter_label: {'tOM','tGM','tNI','tFCF','tCX','tCS'}}. Tesla
    reports in USD so no FX conversion is needed here."""
    t = yf.Ticker(ticker)
    inc = t.quarterly_income_stmt
    cf = t.quarterly_cashflow
    bs = t.quarterly_balance_sheet

    rev = _series_by_quarter(inc, REV_NAMES)
    op = _series_by_quarter(inc, OPINC_NAMES)
    gp = _series_by_quarter(inc, GP_NAMES)
    ni = _series_by_quarter(inc, NI_NAMES)
    ocf = _series_by_quarter(cf, OCF_NAMES)
    capex = _series_by_quarter(cf, CAPEX_NAMES)
    cash = _series_by_quarter(bs, CASH_NAMES)

    out = {}
    for label, r in rev.items():
        entry = {}
        o = op.get(label)
        entry['tOM'] = round(o / r * 100, 2) if o is not None and r else None
        g = gp.get(label)
        entry['tGM'] = round(g / r * 100, 2) if g is not None and r else None
        n = ni.get(label)
        entry['tNI'] = int(round(n / 1e6)) if n is not None else None
        oc, cx = ocf.get(label), capex.get(label)
        entry['tFCF'] = int(round((oc - abs(cx)) / 1e6)) if oc is not None and cx is not None else None
        entry['tCX'] = int(round(abs(cx) / 1e6)) if cx is not None else None
        cs = cash.get(label)
        entry['tCS'] = int(round(cs / 1e6)) if cs is not None else None
        out[label] = entry
    return out


# ═══════════════════════════════════════════════════════════════════════
# MERGING (fetched data on top of existing/seed baseline; never overwrite
# a good value with a missing one)
# ═══════════════════════════════════════════════════════════════════════
def compute_years_labels(existing_years, fetched_years):
    base = {int(y) for y in existing_years if y != 'H1 26*'}
    all_years = base | fetched_years
    years = [str(y) for y in sorted(all_years)]
    years.append('H1 26*')
    return years


def merge_annual(existing_years, existing_peer, fetched_by_year, years_labels):
    keys = ('margin', 'revenue', 'ni', 'fcf', 'capex')
    prev = {k: arrays_to_dict(existing_years, existing_peer.get(k, [])) for k in keys}
    out = {k: [] for k in keys}
    for label in years_labels:
        if label == 'H1 26*':
            for k in keys:
                out[k].append(prev[k].get(label))
            continue
        fetched = fetched_by_year.get(int(label))
        for k in keys:
            v = fetched.get(k) if fetched else None
            out[k].append(v if v is not None else prev[k].get(label))
    return out


def merge_static_by_label(existing_labels, existing_peer, new_labels, keys):
    out = {}
    for k in keys:
        prev = arrays_to_dict(existing_labels, existing_peer.get(k, []))
        out[k] = [prev.get(label) for label in new_labels]
    return out


def merge_tesla_quarterly(existing, fetched_by_label):
    existing_labels = existing.get('labels', [])
    all_labels = sorted(set(existing_labels) | set(fetched_by_label.keys()), key=quarter_sort_key)

    manual_keys = ('tRA', 'tRE', 'tRS')
    fetched_keys = ('tOM', 'tGM', 'tNI', 'tFCF', 'tCX', 'tCS')

    out = {'labels': all_labels}
    for k in manual_keys:
        prev = arrays_to_dict(existing_labels, existing.get(k, []))
        out[k] = [prev.get(label) for label in all_labels]
    for k in fetched_keys:
        prev = arrays_to_dict(existing_labels, existing.get(k, []))
        out[k] = []
        for label in all_labels:
            fetched = fetched_by_label.get(label)
            v = fetched.get(k) if fetched else None
            out[k].append(v if v is not None else prev.get(label))
    return out


# ═══════════════════════════════════════════════════════════════════════
# TESLA QUARTERLY DELIVERIES — from SEC EDGAR 8-K filings
# ═══════════════════════════════════════════════════════════════════════
QUARTER_WORDS = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4}

# requests' default headers (notably "Accept-Encoding: ...zstd") trip SEC's
# bot-detection WAF ("Your Request Originates from an Undeclared Automated
# Tool") even with a proper User-Agent — curl's much smaller default header
# set doesn't. Stripping down to that same minimal set fixes it.
_sec_session = requests.Session()
_sec_session.headers.clear()


def _sec_get(url):
    r = _sec_session.get(url, headers={'User-Agent': SEC_USER_AGENT, 'Accept': '*/*'}, timeout=20)
    r.raise_for_status()
    time.sleep(0.15)  # be a polite, well-under-the-limit citizen of SEC's free API
    return r


def _html_to_text(html):
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    return re.sub(r'\s+', ' ', text)


def _parse_tesla_delivery_exhibit(text):
    """Tesla's quarterly 'Production, Deliveries & Deployments' press
    release has a consistent 'Total <production> <deliveries>' summary row
    and a '<Ordinal> Quarter <year>' title — e.g. 'Total 451,758 480,126 2%'
    under 'Second Quarter 2026'. Returns (label, production, deliveries) or
    None if this filing doesn't look like a delivery report (e.g. it's the
    earnings-release 8-K instead, which shares the same SEC item code)."""
    m_q = re.search(r'\b(First|Second|Third|Fourth) Quarter (\d{4})', text)
    m_total = re.search(r'\bTotal\s+([\d,]+)\s+([\d,]+)', text)
    if not m_q or not m_total:
        return None
    qnum = QUARTER_WORDS[m_q.group(1).lower()]
    year = int(m_q.group(2))
    production = int(m_total.group(1).replace(',', ''))
    deliveries = int(m_total.group(2).replace(',', ''))
    return f"Q{qnum} {year % 100:02d}", production, deliveries


def fetch_tesla_deliveries_from_sec(max_filings_to_check=16):
    """Scan Tesla's recent 8-Ks for the quarterly delivery-report exhibit.
    Re-checks the last ~2 years of filings every run (not just new ones) so
    a stale/incorrect seed value gets self-corrected, the same way the
    yfinance-backed metrics already do. Returns {quarter_label: {'production','deliveries'}}."""
    out = {}
    try:
        subs = _sec_get(f'https://data.sec.gov/submissions/CIK{TESLA_CIK}.json').json()
    except Exception as e:
        log.warning(f"SEC submissions lookup failed: {e}")
        return out

    recent = subs.get('filings', {}).get('recent', {})
    forms = recent.get('form', [])
    accessions = recent.get('accessionNumber', [])
    items_list = recent.get('items', [])
    cik_num = TESLA_CIK.lstrip('0')

    checked = 0
    for i, form in enumerate(forms):
        if checked >= max_filings_to_check:
            break
        item_str = items_list[i] if i < len(items_list) else ''
        if form != '8-K' or '2.02' not in item_str:
            continue
        checked += 1
        accn = accessions[i].replace('-', '')
        try:
            index = _sec_get(f'https://www.sec.gov/Archives/edgar/data/{cik_num}/{accn}/index.json').json()
        except Exception as e:
            log.warning(f"SEC filing index fetch failed for {accessions[i]}: {e}")
            continue
        exhibit_name = next(
            (it['name'] for it in index.get('directory', {}).get('item', [])
             if re.match(r'ex(hibit)?-?99', it['name'], re.I)), None)
        if not exhibit_name:
            continue
        try:
            text = _html_to_text(_sec_get(
                f'https://www.sec.gov/Archives/edgar/data/{cik_num}/{accn}/{exhibit_name}').text)
        except Exception as e:
            log.warning(f"SEC exhibit fetch failed for {accessions[i]}: {e}")
            continue
        parsed = _parse_tesla_delivery_exhibit(text)
        if parsed:
            label, production, deliveries = parsed
            out[label] = {'production': production, 'deliveries': deliveries}

    if not out:
        log.warning(f"SEC: checked {checked} candidate 8-Ks, found no parseable delivery report")
    else:
        log.info(f"SEC: parsed {len(out)} Tesla delivery quarters from {checked} candidate 8-Ks")
    return out


def merge_tesla_deliveries(existing, fetched_by_label):
    existing_labels = existing.get('labels', [])
    all_labels = sorted(set(existing_labels) | set(fetched_by_label.keys()), key=quarter_sort_key)
    prev_prod = arrays_to_dict(existing_labels, existing.get('production', []))
    prev_del = arrays_to_dict(existing_labels, existing.get('deliveries', []))
    production, deliveries = [], []
    for label in all_labels:
        f = fetched_by_label.get(label)
        production.append(f['production'] if f else prev_prod.get(label))
        deliveries.append(f['deliveries'] if f else prev_del.get(label))
    return {'labels': all_labels, 'production': production, 'deliveries': deliveries}


def build_seed_dataset():
    peers = []
    for pid, meta in COMPANIES.items():
        seed = SEED_PEERS[pid]
        peers.append({
            'id': pid,
            'name': meta['name'],
            'color': meta['color'],
            'dash': list(seed['dash']),
            'margin': list(seed['margin']),
            'revenue': list(seed['revenue']),
            'ni': list(seed['ni']),
            'fcf': list(seed['fcf']),
            'capex': list(seed['capex']),
            'del_total': list(seed['del_total']),
            'del_bev': list(seed['del_bev']),
            'del_dm': list(seed['del_dm']),
            'fetch_error': False,
        })
    return {
        'generated_at': None,
        'years': list(SEED_YEARS),
        'peers': peers,
        'deliveries_note': DELIVERIES_NOTE,
        'tesla_quarterly': {k: list(v) for k, v in SEED_TESLA_QUARTERLY.items()},
        'tesla_deliveries': {k: list(v) for k, v in SEED_TESLA_DELIVERIES.items()},
        'highlights_note': HIGHLIGHTS_NOTE,
        'highlights': [dict(h) for h in SEED_HIGHLIGHTS],
    }


def load_existing():
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.info(f"No usable existing {DATA_FILE} ({e}); using built-in seed as baseline")
        return None


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
def main():
    existing = load_existing() or build_seed_dataset()
    existing_years = existing.get('years', SEED_YEARS)
    peers_by_id = {p['id']: p for p in existing.get('peers', [])}

    fetched_per_company = {}
    fetch_errors = {}
    all_fetched_years = set()
    fx_by_year_cache = {}  # currency -> {year: rate}, shared across companies

    for pid, meta in COMPANIES.items():
        ticker = meta['ticker']
        try:
            currency = get_financial_currency(ticker, CURRENCY_HINTS.get(pid, 'USD'))
            if currency not in fx_by_year_cache:
                fx_by_year_cache[currency] = fx_yearly_averages(currency)
            fx_by_year = fx_by_year_cache[currency]
            fx_fallback = fx_rate_to_usd(currency)
            log.info(f"Fetching annual financials for {pid} ({ticker}), reporting currency={currency}, "
                     f"{len(fx_by_year)} yearly FX rates cached, spot fallback={fx_fallback:.4f}...")
            fby = fetch_annual_metrics(ticker, fx_by_year=fx_by_year, fx_fallback=fx_fallback)
            fetched_per_company[pid] = fby
            if fby:
                all_fetched_years |= set(fby.keys())
            else:
                log.warning(f"{pid}: yfinance returned no usable annual data")
                fetch_errors[pid] = True
        except Exception as e:
            log.error(f"{pid}: fetch failed — {e}")
            fetched_per_company[pid] = {}
            fetch_errors[pid] = True

    years_labels = compute_years_labels(existing_years, all_fetched_years)

    new_peers = []
    for pid, meta in COMPANIES.items():
        existing_peer = peers_by_id.get(pid, {})
        merged = merge_annual(existing_years, existing_peer, fetched_per_company.get(pid, {}), years_labels)
        del_merged = merge_static_by_label(existing_years, existing_peer, years_labels,
                                            ('del_total', 'del_bev', 'del_dm'))
        new_peers.append({
            'id': pid,
            'name': meta['name'],
            'color': meta['color'],
            'dash': existing_peer.get('dash', []),
            **merged,
            **del_merged,
            'fetch_error': bool(fetch_errors.get(pid, False)),
        })

    try:
        log.info("Fetching Tesla quarterly financials...")
        tq_fetched = fetch_tesla_quarterly('TSLA')
        if not tq_fetched:
            log.warning("tesla: yfinance returned no usable quarterly data")
    except Exception as e:
        log.error(f"tesla quarterly: fetch failed — {e}")
        tq_fetched = {}

    tesla_quarterly = merge_tesla_quarterly(
        existing.get('tesla_quarterly', SEED_TESLA_QUARTERLY), tq_fetched)

    try:
        log.info("Fetching Tesla quarterly deliveries from SEC EDGAR...")
        td_fetched = fetch_tesla_deliveries_from_sec()
        deliveries_fetch_error = not td_fetched
    except Exception as e:
        log.error(f"tesla deliveries (SEC): fetch failed — {e}")
        td_fetched = {}
        deliveries_fetch_error = True

    tesla_deliveries = merge_tesla_deliveries(
        existing.get('tesla_deliveries', SEED_TESLA_DELIVERIES), td_fetched)
    tesla_deliveries['fetch_error'] = deliveries_fetch_error

    highlights = existing.get('highlights', [dict(h) for h in SEED_HIGHLIGHTS])

    output = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'years': years_labels,
        'peers': new_peers,
        'deliveries_note': DELIVERIES_NOTE,
        'tesla_quarterly': tesla_quarterly,
        'tesla_deliveries': tesla_deliveries,
        'highlights_note': HIGHLIGHTS_NOTE,
        'highlights': highlights,
    }

    with open(DATA_FILE, 'w') as f:
        json.dump(output, f, indent=2)
    log.info(f"Wrote {DATA_FILE} ({len(new_peers)} companies, {len(years_labels)} years, "
              f"{len(tesla_quarterly.get('labels', []))} Tesla quarters, "
              f"{len(tesla_deliveries.get('labels', []))} Tesla delivery quarters)")

    failed_ids = sorted(p['id'] for p in new_peers if p['fetch_error'])
    if deliveries_fetch_error:
        failed_ids.append('tesla_deliveries(SEC)')
    if failed_ids:
        log.warning(f"Fetch errors this run: {', '.join(failed_ids)}")
    gh_output = os.environ.get('GITHUB_OUTPUT')
    if gh_output:
        with open(gh_output, 'a') as f:
            f.write(f"has_errors={'true' if failed_ids else 'false'}\n")
            f.write(f"fetch_errors={','.join(failed_ids)}\n")


if __name__ == '__main__':
    main()
