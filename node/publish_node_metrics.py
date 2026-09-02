#!/usr/bin/env python3
"""
publish_node_metrics.py — turn the scanner's database into a snapshot file the site can read.

Writes data/node_onchain.json in the same shape as every other source in this repository:
a series map of [date, value] pairs, a fetched_at stamp, and a coverage block that states
plainly what the numbers do and do not cover.

    python publish_node_metrics.py --datadir /var/lib/btcmetrics --out ../data

The output is deliberately conservative. A day is published only when the scanner recorded a
plausible number of blocks for it, so a partly scanned day at the head or tail of a run never
appears as a real reading. Where a metric needs a price and the price was missing, that day is
omitted from that series rather than being carried forward.
"""

import argparse
import datetime as dt
import json
import os
import sqlite3

MIN_BLOCKS_PER_DAY = 100          # a full day is ~144 blocks; below this the day is partial


def load(con, sql, params=()):
    return con.execute(sql, params).fetchall()


def build(con):
    rows = load(con, "SELECT date,cdd,spent_btc,sopr_num,sopr_den,realised_delta,created_btc,blocks "
                     "FROM daily ORDER BY date")
    cdd, dormancy, sopr, realised, spent = [], [], [], [], []
    cum_realised = 0.0
    partial_days = 0
    for date, c, sb, num, den, rd, cb, blocks in rows:
        cum_realised += rd or 0.0
        if (blocks or 0) < MIN_BLOCKS_PER_DAY:
            partial_days += 1
            continue
        cdd.append([date, round(c or 0.0, 2)])
        spent.append([date, round(sb or 0.0, 4)])
        if sb and sb > 0:
            dormancy.append([date, round((c or 0.0) / sb, 4)])
        if den and den > 0:
            sopr.append([date, round((num or 0.0) / den, 6)])
        if cum_realised > 0:
            realised.append([date, round(cum_realised, 2)])

    series = {
        "coin_days_destroyed": cdd,
        "dormancy_days": dormancy,
        "sopr": sopr,
        "realised_cap_usd": realised,
        "spent_volume_btc": spent,
    }

    # HODL waves: one series per age band, as a share of the coins in the live set that day
    bands = load(con, "SELECT date,band,btc FROM hodl ORDER BY date")
    by_date = {}
    for date, band, btc in bands:
        by_date.setdefault(date, {})[band] = btc
    for date, m in by_date.items():
        total = sum(m.values())
        if total <= 0:
            continue
        for band, btc in m.items():
            key = "hodl_" + band.replace("<", "lt").replace("+", "plus").replace("-", "_")
            series.setdefault(key, []).append([date, round(100.0 * btc / total, 4)])
    for k in series:
        series[k].sort()

    height = con.execute("SELECT v FROM state WHERE k='height'").fetchone()
    unknown = con.execute("SELECT v FROM state WHERE k='unknown_inputs'").fetchone()
    return series, {
        "scanned_to_height": int(height[0]) if height else None,
        "days_recorded": len(rows),
        "partial_days_withheld": partial_days,
        "inputs_without_a_known_creation": int(unknown[0]) if unknown else 0,
        "note": ("Computed from a full archival node, output by output. No entity clustering, no "
                 "exchange labels and no change heuristics are applied, so these are ledger facts "
                 "plus a daily price, not attributions of ownership. A day appears only when at "
                 "least %d blocks were recorded for it." % MIN_BLOCKS_PER_DAY),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--datadir", required=True)
    ap.add_argument("--out", required=True, help="the repository's data/ directory")
    args = ap.parse_args()

    con = sqlite3.connect(os.path.join(args.datadir, "chain.sqlite"))
    series, coverage = build(con)
    con.close()

    doc = {
        "source": "node_onchain",
        "source_url": "first-party Bitcoin Core archival node",
        "fetched_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "coverage": coverage,
        "series": series,
    }
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "node_onchain.json")
    with open(path, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    counts = {k: len(v) for k, v in series.items()}
    print(f"wrote {path}")
    print(f"  scanned to height {coverage['scanned_to_height']}, "
          f"{coverage['partial_days_withheld']} partial days withheld")
    for k, n in sorted(counts.items()):
        print(f"  {k:28s} {n:>6d} rows")


if __name__ == "__main__":
    main()
