# First-party on-chain metrics from your own node

This directory computes the coin-age family of metrics — coin days destroyed, dormancy, SOPR,
realised capitalisation and the HODL waves — from a full Bitcoin Core node, and publishes them as
`data/node_onchain.json` in the same format as every other source in this repository.

It exists because no free public feed provides these to a standard this project is willing to
publish under. They need UTXO-level data, and UTXO-level data needs a node.

## What is and is not claimed

Everything here is a direct consequence of the ledger plus a daily price series. There is **no
entity clustering, no exchange labelling and no change-output heuristic**. That is a deliberate
limit, not an oversight: the widely quoted "entity-adjusted" versions of these metrics depend on
proprietary address clusters that cannot be independently checked, and this project would rather
publish the unadjusted figure and say so than publish an adjusted one it cannot defend.

Practical consequence: the SOPR here is the raw, output-level SOPR. It counts internal transfers
and change alongside genuine sales, so its level is not comparable to a vendor's entity-adjusted
SOPR. Its **shape and its crossings of 1.0** are the useful part, and they are reproducible by
anyone with a node and this script.

## Hardware

| | Minimum | Comfortable |
|---|---|---|
| Disk | 1.2 TB SSD | 2 TB NVMe |
| RAM | 8 GB | 16 GB |
| CPU | any 4-core from the last decade | anything newer |
| Network | unmetered, 500 GB for the initial sync | same |

An SSD is not optional. The scan performs one random-access database lookup per transaction input,
which on a spinning disk turns days into weeks. The chain itself is roughly 700 GB and this tool's
own database grows to roughly 15–25 GB.

A cheap always-on box works well: a mini PC, an old desktop with an SSD added, or a rented server
(Hetzner's storage-capable dedicated boxes are the usual budget choice). A laptop that sleeps is
the one setup to avoid, though the scanner does resume cleanly after interruption.

## 1. Install and sync Bitcoin Core

Download from https://bitcoincore.org/en/download/ and verify the signatures. Then use a config
like the following. `prune=0` and `txindex=1` matter: the scan needs the whole chain, and the
transaction index makes block queries fast.

```
# bitcoin.conf
server=1
txindex=1
prune=0
dbcache=4096          # raise if you have the RAM; it shortens the initial sync considerably
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
```

Start it and wait. The initial block download takes roughly a day on a fast machine and several on
a slow one. Check progress with:

```
bitcoin-cli getblockchaininfo | grep -E 'blocks|verificationprogress'
```

Do not start the scan until `verificationprogress` is essentially 1.0.

## 2. Get a price series

The scan needs a daily USD close to compute SOPR and realised cap. Use this repository's own,
so the node's numbers and the site's numbers share one price convention:

```
curl -o prices.json https://akpasz.github.io/btc-data/data/blockchain.json
```

`scan_chain.py` accepts that file directly.

## 3. Run the scan

```
python3 scan_chain.py --datadir /var/lib/btcmetrics --prices prices.json
```

The first run walks the chain from genesis. Expect **two to six days** depending on disk and CPU;
it prints a running rate and estimate. It commits continuously, so stopping it costs nothing:

```
# stop any time with Ctrl-C, then later
python3 scan_chain.py --datadir /var/lib/btcmetrics --prices prices.json   # resumes
```

To do it in stages, use `--stop-at 400000`, then a later run with a higher stop height, then one
with none at all.

After the first pass, each subsequent run processes only new blocks and takes seconds.

## 4. Publish

```
python3 publish_node_metrics.py --datadir /var/lib/btcmetrics --out /path/to/btc-data/data
```

Then commit `data/node_onchain.json` to the repository. A minimal daily job:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /path/to/btc-data
git pull --quiet origin main
curl -sS -o /tmp/prices.json https://akpasz.github.io/btc-data/data/blockchain.json
python3 node/scan_chain.py --datadir /var/lib/btcmetrics --prices /tmp/prices.json
python3 node/publish_node_metrics.py --datadir /var/lib/btcmetrics --out data
if ! git diff --quiet data/node_onchain.json; then
  git add data/node_onchain.json
  git commit -m "node: on-chain metrics through $(date -u +%F)"
  git push origin main
fi
```

Schedule it after the hosted job so the price file is current:

```
# crontab -e   (07:00 UTC, after the 06:15 UTC hosted snapshot)
0 7 * * * /path/to/node_daily.sh >> /var/log/btc-node-metrics.log 2>&1
```

On Windows, the same script under Task Scheduler with `pwsh`/`bash` works; keep the machine set to
never sleep.

## Verifying it works

The scan is checkable without waiting for the full history:

```
python3 scan_chain.py --datadir /tmp/test --prices prices.json --stop-at 200000
python3 publish_node_metrics.py --datadir /tmp/test --out /tmp/test
```

Then compare a well known figure: realised cap at a historical date, or the supply held by outputs
older than a year. Two independent checks worth doing once:

1. **Supply reconciliation.** The sum of the live UTXO table at any height must equal the coin
   supply at that height. `SELECT SUM(sats)/1e8 FROM utxo` against `getblockstats`'s cumulative
   subsidy is a hard check on the scan's correctness.
2. **Spot-check a large old spend.** Pick a dormant coin movement reported publicly, and confirm
   the coin days destroyed on that date jump accordingly.

The scanner also self-reports: `inputs_without_a_known_creation` in the published coverage block
must be zero after a full scan from genesis. A non-zero value means the database was started from
a partial chain and the earliest metrics are incomplete.

## Cost and honesty about effort

This is the most expensive item in the project: a machine, several days of initial compute, and a
daily job that must keep running. What it buys is a set of metrics that cannot be obtained any
other way without paying a vendor, computed transparently enough that a reader can reproduce them.
If the node stops, the published series simply stops advancing, the coverage block records the
height it reached, and nothing else on the site is affected.
