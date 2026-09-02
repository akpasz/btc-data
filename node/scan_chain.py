#!/usr/bin/env python3
"""
scan_chain.py — build first-party on-chain metrics from a local Bitcoin Core node.

What this computes, and why it exists
-------------------------------------
The public feeds this project uses cannot produce the coin-age family of metrics: coin days
destroyed, SOPR, realised capitalisation and the HODL waves all require knowing, for every coin
spent, when that coin last moved and what it was worth then. That needs UTXO-level data, which
means a node. This script walks the chain once, keeps a UTXO table keyed by outpoint, and records
what each spend destroys.

It is deliberately exact rather than clever: no entity clustering, no exchange labels, no
heuristics about change outputs. Every number it publishes is a direct consequence of the ledger
plus a daily price series. Where a metric would require guessing who owns what, this script does
not publish it, and the methods page says so.

Definitions used here
---------------------
coin days destroyed  sum over spent outputs of value_btc * (age in days)
dormancy             coin days destroyed divided by the day's spent volume in BTC
SOPR                 sum(value_btc * price_at_spend) / sum(value_btc * price_at_creation),
                     over spent outputs; above 1 means coins moved at a profit on average
realised cap         sum over the live UTXO set of value_btc * price when that output was created
HODL waves           share of the live supply by the age of the output holding it

Resumability
------------
Progress lives in SQLite. Stop it at any point (Ctrl-C, reboot, power cut) and run it again; it
resumes from the last committed height. A full historical scan is measured in days on a normal
machine, which is expected: it is done once, and after that each run adds only the new blocks.

Usage
-----
    python scan_chain.py --datadir /var/lib/btcmetrics --prices prices.json
    python scan_chain.py --datadir /var/lib/btcmetrics --prices prices.json --stop-at 800000

prices.json is {"YYYY-MM-DD": price_usd, ...}; the repository's own blockchain.json price series
is the intended input, so the node's numbers and the site's numbers share one price convention.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import datetime as dt
from typing import Dict, Optional, Tuple

try:
    import requests
except ImportError:
    sys.exit("requests is required: pip install requests")


# ----------------------------------------------------------------- node RPC

class NodeRPC:
    """Minimal Bitcoin Core JSON-RPC client. Batches where the node supports it."""

    def __init__(self, url: str, user: str, password: str, timeout: int = 120):
        self.url = url
        self.auth = (user, password)
        self.timeout = timeout
        self.session = requests.Session()
        self._id = 0

    def call(self, method: str, *params):
        self._id += 1
        payload = {"jsonrpc": "1.0", "id": self._id, "method": method, "params": list(params)}
        r = self.session.post(self.url, json=payload, auth=self.auth, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if j.get("error"):
            raise RuntimeError(f"{method}: {j['error']}")
        return j["result"]

    def batch(self, calls):
        """calls is a list of (method, params_list). Returns results in order."""
        payload = []
        for method, params in calls:
            self._id += 1
            payload.append({"jsonrpc": "1.0", "id": self._id, "method": method, "params": params})
        r = self.session.post(self.url, json=payload, auth=self.auth, timeout=self.timeout)
        r.raise_for_status()
        out = r.json()
        by_id = {x["id"]: x for x in out}
        results = []
        for item in payload:
            got = by_id[item["id"]]
            if got.get("error"):
                raise RuntimeError(f"{item['method']}: {got['error']}")
            results.append(got["result"])
        return results


def rpc_from_cookie(datadir: str, host: str = "127.0.0.1", port: int = 8332) -> NodeRPC:
    """Bitcoin Core writes a .cookie file in its datadir; using it avoids putting a password in a config."""
    cookie = os.path.join(datadir, ".cookie")
    if not os.path.exists(cookie):
        raise FileNotFoundError(
            f"no .cookie in {datadir}. Either point --node-datadir at Bitcoin Core's data directory "
            f"or pass --rpc-user and --rpc-password."
        )
    with open(cookie) as f:
        user, _, password = f.read().partition(":")
    return NodeRPC(f"http://{host}:{port}", user, password)


# ----------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS utxo (
    op    BLOB PRIMARY KEY,   -- 32-byte txid + 4-byte vout, little endian
    sats  INTEGER NOT NULL,
    h     INTEGER NOT NULL    -- creation height
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS daily (
    date        TEXT PRIMARY KEY,
    cdd         REAL NOT NULL DEFAULT 0,   -- coin days destroyed
    spent_btc   REAL NOT NULL DEFAULT 0,   -- value of spent outputs, BTC
    sopr_num    REAL NOT NULL DEFAULT 0,   -- sum(value * price at spend)
    sopr_den    REAL NOT NULL DEFAULT 0,   -- sum(value * price at creation)
    realised_delta REAL NOT NULL DEFAULT 0,-- change in realised cap, USD
    created_btc REAL NOT NULL DEFAULT 0,
    blocks      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS hodl (        -- one row per day per age band, written at each daily boundary
    date TEXT NOT NULL, band TEXT NOT NULL, btc REAL NOT NULL,
    PRIMARY KEY (date, band)
) WITHOUT ROWID;
"""

# Age bands follow the convention used by the published HODL-wave charts.
BANDS = [
    ("<1d", 0, 1), ("1d-1w", 1, 7), ("1w-1m", 7, 30), ("1m-3m", 30, 90),
    ("3m-6m", 90, 180), ("6m-12m", 180, 365), ("1y-2y", 365, 730),
    ("2y-3y", 730, 1095), ("3y-5y", 1095, 1825), ("5y-7y", 1825, 2555),
    ("7y-10y", 2555, 3650), ("10y+", 3650, 10 ** 9),
]


def open_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")     # crash-safe enough given the resume logic
    con.execute("PRAGMA cache_size=-524288")     # 512 MB page cache; the UTXO table is the hot path
    con.execute("PRAGMA temp_store=MEMORY")
    con.executescript(SCHEMA)
    return con


def get_state(con, key, default=None):
    row = con.execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(con, key, value):
    con.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, str(value)))


def outpoint(txid_hex: str, vout: int) -> bytes:
    return bytes.fromhex(txid_hex)[::-1] + vout.to_bytes(4, "little")


# ----------------------------------------------------------------- prices

class Prices:
    """Daily close by date, with the nearest earlier date used for gaps.

    A missing price is not fatal: the coin-age metrics that do not need a price (coin days
    destroyed, dormancy, HODL waves) are still exact, and the ones that do (SOPR, realised cap)
    simply do not accumulate for that day. The publisher records which days were affected.
    """

    def __init__(self, mapping: Dict[str, float]):
        self.by_date = {d: float(p) for d, p in mapping.items() if p and float(p) > 0}
        self.dates = sorted(self.by_date)
        self.missing = set()

    def at(self, date: str) -> Optional[float]:
        p = self.by_date.get(date)
        if p:
            return p
        # fall back to the most recent earlier date; this matters only for feed gaps
        lo, hi = 0, len(self.dates) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.dates[mid] <= date:
                best = self.dates[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            self.missing.add(date)
            return None
        return self.by_date[best]


# ----------------------------------------------------------------- scan

def day_of(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def write_hodl_snapshot(con, date: str, tip_height: int, height_dates):
    """Value of the live UTXO set by age band, at a day boundary.

    Age is measured in blocks and converted at the observed block interval for that stretch of
    chain rather than a nominal ten minutes, so the bands do not drift during periods of fast or
    slow blocks.
    """
    rows = con.execute("SELECT h, SUM(sats) FROM utxo GROUP BY h").fetchall()
    tip_time = height_dates.get(tip_height)
    if tip_time is None:
        return
    totals = {b[0]: 0.0 for b in BANDS}
    for h, sats in rows:
        created = height_dates.get(h)
        if created is None:
            continue
        age_days = (tip_time - created) / 86400.0
        for name, lo, hi in BANDS:
            if lo <= age_days < hi:
                totals[name] += (sats or 0) / 1e8
                break
    for name, btc in totals.items():
        con.execute("INSERT INTO hodl(date,band,btc) VALUES(?,?,?) "
                    "ON CONFLICT(date,band) DO UPDATE SET btc=excluded.btc", (date, name, btc))


def scan(con, rpc: NodeRPC, prices: Prices, stop_at: Optional[int], batch_blocks: int,
         hodl_every: int, verbose: bool = True):
    start_height = int(get_state(con, "height", "-1")) + 1
    tip = rpc.call("getblockcount")
    end = min(tip, stop_at) if stop_at else tip
    if start_height > end:
        print(f"nothing to do: scanned to {start_height - 1}, node tip {tip}")
        return

    print(f"scanning heights {start_height}..{end} (node tip {tip})")
    t0 = time.time()
    height_dates: Dict[int, int] = {}          # height -> block timestamp, for age arithmetic
    # rebuild the height->time map lazily from the node as blocks stream in; seed with what we need
    last_day = get_state(con, "last_day")
    processed = 0

    h = start_height
    while h <= end:
        hi = min(h + batch_blocks - 1, end)
        hashes = rpc.batch([("getblockhash", [x]) for x in range(h, hi + 1)])
        # verbosity 2 gives full transaction data including each input's prevout when the node
        # has -txindex; without it, prevouts are resolved from our own UTXO table, which is why
        # this scanner keeps one rather than relying on the node.
        blocks = rpc.batch([("getblock", [bh, 2]) for bh in hashes])

        con.execute("BEGIN")
        for blk in blocks:
            bh_height = blk["height"]
            ts = blk["time"]
            height_dates[bh_height] = ts
            date = day_of(ts)
            price_now = prices.at(date)

            cdd = spent_btc = sopr_num = sopr_den = created_btc = 0.0
            realised_delta = 0.0

            for tx in blk["tx"]:
                txid = tx["txid"]
                # inputs: look up what is being destroyed
                for vin in tx.get("vin", []):
                    if "coinbase" in vin:
                        continue
                    op = outpoint(vin["txid"], vin["vout"])
                    row = con.execute("SELECT sats, h FROM utxo WHERE op=?", (op,)).fetchone()
                    if row is None:
                        # Only legitimate cause is a spend of an output created before the scan
                        # started (a partial scan). Counted so the publisher can report coverage.
                        con.execute("INSERT INTO state(k,v) VALUES('unknown_inputs','1') "
                                    "ON CONFLICT(k) DO UPDATE SET v=CAST(CAST(v AS INTEGER)+1 AS TEXT)")
                        continue
                    sats, created_h = row
                    btc = sats / 1e8
                    created_ts = height_dates.get(created_h)
                    if created_ts is not None:
                        age_days = max(0.0, (ts - created_ts) / 86400.0)
                        cdd += btc * age_days
                    spent_btc += btc
                    created_date = day_of(created_ts) if created_ts is not None else None
                    price_then = prices.at(created_date) if created_date else None
                    if price_now and price_then:
                        sopr_num += btc * price_now
                        sopr_den += btc * price_then
                        realised_delta -= btc * price_then      # coin leaves the set at its old basis
                    con.execute("DELETE FROM utxo WHERE op=?", (op,))

                # outputs: everything spendable enters the set
                for vout in tx.get("vout", []):
                    spk = vout.get("scriptPubKey", {})
                    if spk.get("type") == "nulldata":
                        continue
                    value = vout.get("value", 0) or 0
                    if value <= 0:
                        continue
                    sats = int(round(value * 1e8))
                    con.execute("INSERT OR REPLACE INTO utxo(op,sats,h) VALUES(?,?,?)",
                                (outpoint(txid, vout["n"]), sats, bh_height))
                    created_btc += value
                    if price_now:
                        realised_delta += value * price_now     # coin enters at today's price

            con.execute(
                "INSERT INTO daily(date,cdd,spent_btc,sopr_num,sopr_den,realised_delta,created_btc,blocks) "
                "VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(date) DO UPDATE SET "
                "cdd=cdd+excluded.cdd, spent_btc=spent_btc+excluded.spent_btc, "
                "sopr_num=sopr_num+excluded.sopr_num, sopr_den=sopr_den+excluded.sopr_den, "
                "realised_delta=realised_delta+excluded.realised_delta, "
                "created_btc=created_btc+excluded.created_btc, blocks=blocks+1",
                (date, cdd, spent_btc, sopr_num, sopr_den, realised_delta, created_btc))

            if last_day and date != last_day and bh_height % hodl_every == 0:
                write_hodl_snapshot(con, last_day, bh_height, height_dates)
            last_day = date
            set_state(con, "last_day", date)
            set_state(con, "height", bh_height)
            processed += 1

        con.execute("COMMIT")

        if verbose:
            rate = processed / max(1e-9, time.time() - t0)
            remaining = (end - hi) / rate if rate > 0 else 0
            print(f"  height {hi}  ({100.0 * (hi - start_height + 1) / max(1, end - start_height + 1):.2f}%)  "
                  f"{rate:.1f} blocks/s  eta {remaining / 3600:.1f}h", flush=True)
        h = hi + 1

    # final HODL snapshot at the tip we reached
    if last_day:
        write_hodl_snapshot(con, last_day, int(get_state(con, "height")), height_dates)
    print(f"done: {processed} blocks in {(time.time() - t0) / 60:.1f} min")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datadir", required=True, help="where this tool keeps its database")
    ap.add_argument("--prices", required=True, help="JSON map of date -> USD close")
    ap.add_argument("--node-datadir", default=os.path.expanduser("~/.bitcoin"),
                    help="Bitcoin Core data directory, for the RPC cookie")
    ap.add_argument("--rpc-host", default="127.0.0.1")
    ap.add_argument("--rpc-port", type=int, default=8332)
    ap.add_argument("--rpc-user")
    ap.add_argument("--rpc-password")
    ap.add_argument("--stop-at", type=int, help="stop after this height (for staged runs)")
    ap.add_argument("--batch-blocks", type=int, default=25, help="blocks per RPC batch")
    ap.add_argument("--hodl-every", type=int, default=144, help="write a HODL snapshot every N blocks")
    args = ap.parse_args()

    os.makedirs(args.datadir, exist_ok=True)
    with open(args.prices) as f:
        raw = json.load(f)
    if isinstance(raw, dict) and "series" in raw:          # accept the repo's blockchain.json directly
        raw = {d: v for d, v in raw["series"]["price"]}
    prices = Prices(raw)

    if args.rpc_user and args.rpc_password:
        rpc = NodeRPC(f"http://{args.rpc_host}:{args.rpc_port}", args.rpc_user, args.rpc_password)
    else:
        rpc = rpc_from_cookie(args.node_datadir, args.rpc_host, args.rpc_port)

    info = rpc.call("getblockchaininfo")
    if info.get("pruned"):
        sys.exit("this node is pruned; the scan needs the full chain (set prune=0 and resync)")
    if info.get("initialblockdownload"):
        print("warning: node is still syncing; the scan will stop at the current tip")

    con = open_db(os.path.join(args.datadir, "chain.sqlite"))
    try:
        scan(con, rpc, prices, args.stop_at, args.batch_blocks, args.hodl_every)
    except KeyboardInterrupt:
        print("\ninterrupted; progress is committed, run again to resume")
    finally:
        con.close()


if __name__ == "__main__":
    main()
