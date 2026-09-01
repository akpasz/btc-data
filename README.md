# Crypto Exponentials data snapshot

A free, keyless daily data layer for the tools at https://cryptoexponentials.com/tools/.
GitHub Actions runs `fetch/fetch_all.py` once a day, writes one JSON file per source into `data/`,
commits them, and GitHub Pages serves them with open CORS so the tool pages can read them directly.

## One-time setup (about 15 minutes)

1. Create a new **public** repository on GitHub named `btc-data` (any name works; public is required for free Pages and free Actions minutes).
2. Upload the contents of this folder to the repository root, keeping the structure:
   `fetch/fetch_all.py`, `fetch/requirements.txt`, `.github/workflows/daily.yml`, `data/.gitkeep`, `README.md`.
3. Repository **Settings → Actions → General → Workflow permissions**: select **Read and write permissions**, save.
4. Repository **Settings → Pages**: Source **Deploy from a branch**, Branch **main**, folder **/ (root)**, save.
   Your data will be served at `https://<your-github-username>.github.io/btc-data/data/`.
5. **Actions** tab → **Daily data snapshot** → **Run workflow**. Wait two to three minutes.
6. Open `https://<your-github-username>.github.io/btc-data/data/manifest.json`. It lists every source with `ok` or `error`.
   Send me that file (or paste it) and I will fix any source that failed.

From then on it runs itself every day at 06:15 UTC. You never run anything by hand again.

## Pointing the tool pages at the snapshot

In each tool page there is one line near the top of the script:

    const DATA_BASE = '';

Set it to your Pages URL, for example:

    const DATA_BASE = 'https://<your-github-username>.github.io/btc-data/data/';

Re-upload the page. It will load the snapshot first (fast, cached, timestamped in the audit panel) and fall back
to the live Blockchain.com API only if the snapshot is unavailable.

## What is fetched

| File | Source | Content |
|---|---|---|
| blockchain.json | Blockchain.com Charts API | price, unique addresses, supply, transactions, UTXO count, hash rate, tx volume, fees |
| coinmetrics.json | Coin Metrics community API | realised cap, MVRV, active addresses, supply activity bands, NVT, and more (whatever the free tier allows) |
| coinbase.json | Coinbase | spot at fetch time |
| mempool.json | mempool.space | hash rate, difficulty, recommended fee |
| stablecoins.json | DefiLlama | total stablecoin supply |
| fred.json | FRED | broad dollar index, fed funds, 10y nominal and real yields, M2 |
| fear_greed.json | Alternative.me | Fear and Greed index history |
| derivatives.json | Deribit, OKX, CFTC | DVOL, perpetual funding, open interest, CME open interest |
| etf_flows.json | issuers | phase 2, not implemented yet |
| manifest.json | this script | status of every source, last date per metric, errors |

Each source is independent. If one fails, its previous file is kept and the manifest says so.
