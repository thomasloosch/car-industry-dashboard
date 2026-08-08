# Automotive Peer Comparison Dashboard

A self-updating dashboard comparing Tesla against 7 other automakers (Ford,
GM, Volkswagen, BMW, Mercedes, Stellantis, BYD) across margin, revenue, net
income, free cash flow, CapEx, and deliveries — 2017 through H1 2026.

Financial data refreshes automatically every Sunday via GitHub Actions and
[yfinance](https://github.com/ranaroussi/yfinance). No API key, no server,
no cost.

## Setup (one-time, ~2 minutes)

1. **Fork this repo** on GitHub (top-right "Fork" button).
2. In your fork, go to **Settings → Pages**. Under "Build and deployment",
   set **Source: Deploy from a branch**, branch **`main`**, folder **`/root`**.
   Save.
3. Your dashboard is now live at:
   ```
   https://{your-username}.github.io/{repo-name}/
   ```
   (GitHub Pages can take a minute or two to publish after you save.)
4. To fetch fresh data immediately instead of waiting for Sunday, go to
   **Actions → "Update financial data" → Run workflow**.
5. After that, it updates itself automatically every Sunday at 06:00 UTC —
   no further action needed.

## How it works

- [`fetcher.py`](fetcher.py) runs on GitHub Actions every Sunday, pulls
  annual + Tesla-quarterly financials via `yfinance`, and writes
  [`data.json`](data.json).
- The workflow ([`.github/workflows/update-data.yml`](.github/workflows/update-data.yml))
  commits the updated `data.json` back to the repo.
- [`index.html`](index.html) fetches `data.json` on page load and renders
  the charts. It requires no build step and no backend.
- If `data.json` can't be loaded (e.g. you open `index.html` directly from
  disk instead of via a server), the dashboard falls back to a built-in
  snapshot and shows a notice — everything still works, it just won't be
  live.

## Editing data manually

Click **✏ Edit Data** in the dashboard to override any value by hand (e.g.
delivery figures, which aren't available via any free API and are always
entered manually). Edits are saved to your browser's `localStorage` and are
applied *on top of* `data.json` after every load — so your manual edits
survive the weekly auto-update. Use **↺ Reset all to fetched defaults** to
discard your local edits and go back to what the pipeline fetched.

## Data notes

- Delivery figures (`del_total`, `del_bev`, `del_dm`) and Tesla's segment
  revenue breakdown are never fetched automatically — there's no reliable
  free API for them. They carry forward from the dashboard's built-in
  dataset until edited by hand.
- Non-USD reporters (VW, BMW, Mercedes, Stellantis report in EUR; BYD in
  CNY) are converted to USD using a live spot FX rate at fetch time — this
  is an approximation, not the period-average rate companies actually use
  in their filings.
- If a ticker's fetch fails or returns nothing usable (this happens
  occasionally for OTC ADRs like BMWYY, MBGYY, VWAGY, BYDDY — Yahoo's free
  data for these is inconsistent), that company keeps its last known good
  values and is flagged `"fetch_error": true` in `data.json` rather than
  being blanked out.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python fetcher.py          # writes data.json
python3 -m http.server 8000
```

Then open `http://localhost:8000/`. (Opening `index.html` directly via
`file://` also works, via the fallback dataset, but `fetch()` for
`data.json` will be blocked by the browser — serve it locally to test the
live-data path.)
