# btc-data — free, keyless Bitcoin research data, updated daily

Daily snapshot pipeline behind the [Crypto Exponentials Bitcoin Research
Dashboard](https://cryptoexponentials.com/tools/). Every metric is computed from free, keyless,
primary public sources, published here as plain JSON, and re-read by the dashboard pages. Anyone may
read these files directly — this repository is, in effect, a small public API.

**Base URLs** (identical content):

```
https://akpasz.github.io/btc-data/data/<file>.json
https://raw.githubusercontent.com/akpasz/btc-data/main/data/<file>.json
```

Updated once daily at **06:15 UTC** by GitHub Actions (`.github/workflows/daily.yml`). No keys, no
rate limits beyond GitHub's own, no tracking.

## Files

| File | Contents | Primary source |
| --- | --- | --- |
| `manifest.json` | Run health: per-source status (`ok` / `partial` / `error`), fetch times, last dates, error strings. **Read this first** if you build on the data. | pipeline |
| `kpis.json` | Every headline reading the dashboard shows, precomputed: Metcalfe value (both calibrations, out-of-sample RMSE), power-law trend, realised price / MVRV / Z-score, positioning composite, self-test results, and the `extended` block (see below). | derived |
| `kpis_history.json` | One row per day of what `kpis.json` published — the dashboard's own audit trail. | derived |
| `blockchain.json` | Daily price, unique addresses, supply, hash rate (TH/s), fees USD, estimated tx volume USD. | Blockchain.com Charts API |
| `coinmetrics.json` | PriceUSD, CapMrktCurUSD, CapRealUSD, CapMVRVCur, SplyCur, supply-activity bands, IssTotUSD, AdrActCnt, AdrBalCnt. | Coin Metrics community API |
| `coinbase.json` | Daily Coinbase USD spot (accumulating). | Coinbase API |
| `offshore_spot.json` | OKX BTC-USDT daily closes — the offshore leg of the Coinbase premium. | OKX API |
| `derivatives.json` | Deribit DVOL, perpetual funding (8h daily mean), dated-future ~90d annualised basis, options put/call OI ratio; OKX open interest; CME OI from the CFTC report. | Deribit / OKX / CFTC |
| `stablecoins.json` | Total stablecoin supply, all chains. | DefiLlama |
| `fred.json` | Broad dollar index, fed funds, 10y nominal and real yields, M2, and the net-liquidity trio WALCL / WTREGEN (TGA) / RRPONTSYD. Note FRED units: WALCL and TGA in $ millions, RRP in $ billions. | FRED |
| `fear_greed.json` | Alternative.me Fear & Greed index. | Alternative.me |
| `coingecko_global.json` | Bitcoin dominance %, one point per day (accumulating). | CoinGecko |
| `etf_flows.json`, `etf_holdings.json` | Spot-ETF holdings parsed from issuer disclosures; flows derived from daily differences. Currently `partial` — the manifest lists which issuers parse. | issuer sites |
| `mempool.json` | Fee estimates and mempool stats. | mempool.space |
| `references.json` | **Hand-maintained** external reference figures (e.g. Cane Island MET/MAC), dated. Never written by the pipeline; edit and commit to update. | manual |

### Series format

Raw source files share one shape:

```json
{ "fetched_at": "...", "source_url": "...", "series": { "<name>": [["YYYY-MM-DD", value], ...] } }
```

Series are merged on write: history accumulates across runs and is deduplicated by date, so
single-point daily sources (dominance, basis, put/call, spot) grow into full histories over time.

### `kpis.json → extended` (added 2 Sep 2026)

Mayer multiple, Puell multiple, NUPL (with zone), thermocap multiple, NVT classic + 90-day signal,
hash-ribbons state, hashprice, fee share of miner revenue, 30-day realized volatility, variance risk
premium (DVOL − realized), cycle position vs prior cycles at the same day count, and address
momentum — each with a percentile over the window from 2017. `extended.fwd` holds median 365-day
forward returns by canonical bucket for Mayer/Puell/NUPL (in-sample, overlapping windows, labelled
as such). `extended.flows_extras` carries Fed net liquidity, Bitcoin dominance, the Coinbase
premium (vs OKX USDT — the caveat is in the field's note), futures basis and put/call OI; their
30-day changes and percentile ranks populate automatically as history accrues. Formulas and caveats
are documented on the [methods page](https://cryptoexponentials.com/tools/methods).

## Design principles

- **Free and keyless only.** Every source can be re-fetched by anyone; nothing depends on an
  account or a paid tier.
- **Isolated sources.** Each fetcher runs in its own try-block; one failing API records a manifest
  error and touches nothing else.
- **Honest status.** `partial` means partial. Known gaps (entity-adjusted on-chain metrics such as
  SOPR, holder cost bases, exchange netflows) are documented rather than approximated; they require
  UTXO-level or clustered-entity data no free source provides. The roadmap for computing them
  first-party from a full node (`dumptxoutset` bootstrap + per-block prevout processing) is the
  planned Tier 3 of this pipeline.
- **Self-tested.** `kpis.json → selftest` re-derives the headline numbers by an independent
  implementation on every run and fails the run on disagreement; `kpis_history.json` preserves what
  was published each day.

## Using the data

Attribution appreciated: *Crypto Exponentials Research, cryptoexponentials.com/tools*. The
underlying series remain subject to their original providers' terms (Blockchain.com, Coin Metrics
community tier, Deribit, OKX, DefiLlama, FRED, Alternative.me, CoinGecko, CFTC, Coinbase,
mempool.space). Nothing here is investment advice.
