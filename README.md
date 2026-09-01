# Crypto Exponentials Bitcoin research tools: data layer

A free, keyless daily data layer and KPI computation behind the Bitcoin research tools at https://cryptoexponentials.com/tools/.

GitHub Actions runs `fetch/fetch_all.py` once a day (06:15 UTC), writes one JSON file per source into `data/`, computes each tool's headline readings with an independent self-test, commits the results, and GitHub Pages serves them with open CORS so the tool pages read them directly in the browser.

## What the tools are

| Page | Question | Data used |
|---|---|---|
| Dashboard | Four lenses on one price, live, with thesis status and model agreement | kpis.json, manifest.json |
| Metcalfe Value Monitor | What is the network worth given its users? | Blockchain.com, Coin Metrics |
| Power Law Monitor | Where is price against its long-run trend in time? | Blockchain.com, Coin Metrics |
| Realised Value Monitor | What did holders pay, and are they in profit? | Coin Metrics |
| Flows and Positioning Monitor | Who is buying and how levered is the market? | DefiLlama, Deribit, OKX, CFTC, FRED, Alternative.me, ETF issuers |
| Methods, Audit and Changelog | The full record: methods, reconciliation, what was rejected, every change | this repository |

Every tool is validated out of sample, states what it claims and does not, and carries a data audit with independent recomputation checks. Methods, audit record and changelog: https://cryptoexponentials.com/tools/methods

## Repository layout

```
fetch/fetch_all.py        daily snapshot of every source, isolated per source, merged with history
fetch/kpis.py             headline readings of each tool at its default specification, self-test, daily history
fetch/etf.py              spot ETF flows from issuer disclosures (partial coverage; see status in manifest)
fetch/requirements.txt    requests, numpy
.github/workflows/daily.yml   schedule, commit, Pages build request
data/                     outputs (committed daily)
```

## Files served

| File | Source | Content |
|---|---|---|
| manifest.json | this job | status of every source, last date per metric, KPI and self-test status, errors |
| kpis.json | this job | headline readings per tool, their distributions, sparklines, self-test result |
| kpis_history.json | this job | one row per day: price, Metcalfe (reference and validated), power law, realised, composite |
| blockchain.json | Blockchain.com Charts API | price, unique addresses, supply, transactions, UTXO count, hash rate, fees, volume |
| coinmetrics.json | Coin Metrics community API | price, market cap, MVRV, supply, active addresses, addresses with balance, and others the free tier allows |
| coinbase.json | Coinbase | spot at fetch time |
| mempool.json | mempool.space | hash rate, difficulty, recommended fee |
| stablecoins.json | DefiLlama | total stablecoin supply |
| fred.json | FRED | broad dollar index, fed funds, 10y nominal and real yields, M2 |
| fear_greed.json | Alternative.me | Fear and Greed index |
| derivatives.json | Deribit, OKX, CFTC | DVOL, perpetual funding, open interest, CME open interest |
| etf_flows.json, etf_holdings.json | ETF issuers | holdings by issuer and derived net flows, with per-issuer parse status |

Base URL: `https://akpasz.github.io/btc-data/data/` (fallback: `https://raw.githubusercontent.com/akpasz/btc-data/main/data/`).

Each source is independent. If one fails, its previous file is kept and the manifest says so. `kpis.json` is only written if the self-test passes; a day-over-day move above 25% in any headline is recorded as a warning.

## Running it yourself

1. Fork or clone. Public repositories get free Actions minutes and free Pages.
2. Settings → Actions → General → Workflow permissions → Read and write.
3. Settings → Pages → Deploy from a branch → main, / (root).
4. Actions → Daily data snapshot → Run workflow. Read `data/manifest.json`.

Local run: `pip install -r fetch/requirements.txt && python fetch/fetch_all.py` from the repository root.

Health check:

```powershell
Invoke-RestMethod https://akpasz.github.io/btc-data/data/manifest.json | Select-Object generated_at, kpis, errors
```

## Reproducibility

The git history of `data/` is a dated archive of every snapshot and every KPI file the tools have shown. `kpis_history.json` is the readable version. The methods page records how every displayed number was reconciled against an independent recomputation.

## Maintainer

Kishor Akshinthala, Founder and Chief Blockchain Officer, Crypto Exponentials, Princeton NJ.
https://www.linkedin.com/in/kishorakshinthala/ · https://cryptoexponentials.com/tools/

## Citing

Akshinthala, K. (2026). Crypto Exponentials Bitcoin research tools: data snapshot and KPI computation. Crypto Exponentials. https://github.com/akpasz/btc-data

## Data sources and terms

Blockchain.com Charts API, Coin Metrics community API (CC BY-NC 4.0), Coinbase, mempool.space, DefiLlama, FRED, Alternative.me, Deribit, OKX, CFTC, and ETF issuer disclosures. Each source's terms apply to its data; this repository redistributes daily snapshots for the purpose of running the tools and does not claim rights over the underlying series.

## Licence

Code: MIT (see LICENSE). Data files under `data/` remain subject to their sources' terms.

Nothing in this repository or the tools it serves is investment advice.
