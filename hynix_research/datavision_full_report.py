#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import math
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from statsmodels.tsa.stattools import adfuller

BASE = "https://data.binance.vision/data/futures/um"
SYMBOL_A = "SKHYUSDT"
SYMBOL_B = "SKHYNIXUSDT"
INTERVAL = "5m"
STEP_MS = 5 * 60 * 1000
START_DATE = date(2026, 7, 1)
COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]

@dataclass(frozen=True)
class Params:
    window: int
    entry_z: float
    entry_bps: float
    exit_bps: float
    max_hold: int
    session_filter: str
    stop_z: float = 5.0
    stop_bps: float = 180.0

class Downloader:
    def __init__(self, timeout: int = 30, retries: int = 5):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0 HynixResearch/1.0"})
        self.timeout = timeout
        self.retries = retries
        self.manifest: list[dict] = []

    def get(self, url: str) -> bytes | None:
        last = ""
        for attempt in range(self.retries):
            try:
                r = self.s.get(url, timeout=self.timeout)
                if r.status_code == 404:
                    self.manifest.append({"url": url, "status": 404, "bytes": 0})
                    return None
                r.raise_for_status()
                self.manifest.append({"url": url, "status": r.status_code, "bytes": len(r.content)})
                return r.content
            except Exception as exc:
                last = repr(exc)
                if attempt + 1 < self.retries:
                    time.sleep(min(2 ** attempt, 8))
        self.manifest.append({"url": url, "status": "error", "error": last, "bytes": 0})
        raise RuntimeError(f"download failed: {url}: {last}")

def normalize_epoch_ms(values: pd.Series) -> pd.Series:
    x = pd.to_numeric(values, errors="coerce")
    x = np.where(x > 10**14, x / 1000.0, x)
    return pd.Series(x, index=values.index).round().astype("Int64")

def parse_kline_zip(blob: bytes, symbol: str, source_url: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(f"no csv in {source_url}")
        raw = pd.read_csv(zf.open(names[0]), header=None, dtype=str)
    if raw.shape[1] < 12:
        raise RuntimeError(f"unexpected columns {raw.shape[1]} in {source_url}")
    raw = raw.iloc[:, :12]
    raw.columns = COLUMNS
    raw["open_time"] = normalize_epoch_ms(raw["open_time"])
    raw = raw[raw["open_time"].notna()].copy()
    raw["close_time"] = normalize_epoch_ms(raw["close_time"])
    for c in ["open", "high", "low", "close", "volume", "quote_volume",
              "taker_buy_volume", "taker_buy_quote_volume"]:
        raw[c] = pd.to_numeric(raw[c], errors="coerce")
    raw["trades"] = pd.to_numeric(raw["trades"], errors="coerce").fillna(0).astype(int)
    raw["symbol"] = symbol
    raw["source_url"] = source_url
    raw["time"] = pd.to_datetime(raw["open_time"].astype("int64"), unit="ms", utc=True)
    return raw

def parse_funding_zip(blob: bytes, symbol: str, source_url: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            return pd.DataFrame()
        raw = pd.read_csv(zf.open(names[0]))
    lower = {str(c).lower(): c for c in raw.columns}
    tcol = next((c for k, c in lower.items() if "calc_time" in k or "fundingtime" in k or k == "time"), None)
    rcol = next((c for k, c in lower.items() if "funding" in k and "rate" in k), None)
    if tcol is None or rcol is None:
        raw = pd.read_csv(io.BytesIO(zipfile.ZipFile(io.BytesIO(blob)).read(names[0])), header=None)
        if raw.shape[1] < 3:
            return pd.DataFrame()
        tcol, rcol = raw.columns[0], raw.columns[-1]
    out = pd.DataFrame()
    out["time_ms"] = normalize_epoch_ms(raw[tcol])
    out["funding_rate"] = pd.to_numeric(raw[rcol], errors="coerce")
    out = out.dropna()
    out["time"] = pd.to_datetime(out["time_ms"].astype("int64"), unit="ms", utc=True)
    out["symbol"] = symbol
    out["source_url"] = source_url
    return out.sort_values("time").drop_duplicates(["symbol", "time"])

def daterange(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)

def fetch_daily_klines(dl: Downloader, symbol: str, end_date: date) -> pd.DataFrame:
    frames = []
    for d in daterange(START_DATE, end_date):
        ds = d.isoformat()
        url = f"{BASE}/daily/klines/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{ds}.zip"
        blob = dl.get(url)
        if blob:
            frames.append(parse_kline_zip(blob, symbol, url))
    if not frames:
        raise RuntimeError(f"no kline archives for {symbol}")
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("open_time").drop_duplicates("open_time", keep="last")
    return df.reset_index(drop=True)

def fetch_monthly_funding(dl: Downloader, symbol: str, months: list[str]) -> pd.DataFrame:
    frames = []
    for ym in months:
        url = f"{BASE}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{ym}.zip"
        blob = dl.get(url)
        if blob:
            f = parse_funding_zip(blob, symbol, url)
            if not f.empty:
                frames.append(f)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def session_label(ts: pd.Timestamp) -> str:
    if ts.weekday() >= 5:
        return "WEEKEND"
    mins = ts.hour * 60 + ts.minute
    if 0 <= mins < 390:
        return "KR_ACTIVE"
    if 810 <= mins < 1200:
        return "US_ACTIVE"
    return "OTHER"

def align(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    aa = a[["open_time", "time", "open", "high", "low", "close", "volume", "quote_volume"]].copy()
    bb = b[["open_time", "time", "open", "high", "low", "close", "volume", "quote_volume"]].copy()
    aa = aa.rename(columns={c: f"a_{c}" for c in aa.columns if c not in ["open_time", "time"]})
    bb = bb.rename(columns={c: f"b_{c}" for c in bb.columns if c not in ["open_time", "time"]})
    x = aa.merge(bb.drop(columns=["time"]), on="open_time", how="inner", validate="one_to_one")
    x = x.sort_values("open_time").reset_index(drop=True)
    x["time"] = pd.to_datetime(x["open_time"], unit="ms", utc=True)
    x["session"] = x["time"].map(session_label)
    x["ratio"] = x["a_close"] / x["b_close"]
    x["log_ratio"] = np.log(x["ratio"])
    x["ret_a"] = np.log(x["a_close"]).diff()
    x["ret_b"] = np.log(x["b_close"]).diff()
    x["ratio_change_bps"] = x["log_ratio"].diff() * 10000.0
    return x

def add_features(df: pd.DataFrame, window: int) -> pd.DataFrame:
    x = df.copy()
    mean = x["log_ratio"].rolling(window, min_periods=window).mean().shift(1)
    std = x["log_ratio"].rolling(window, min_periods=window).std(ddof=1).shift(1)
    x[f"mean_{window}"] = mean
    x[f"std_{window}"] = std
    residual = x["log_ratio"] - mean
    x[f"premium_bps_{window}"] = np.expm1(residual) * 10000.0
    x[f"z_{window}"] = residual / std.replace(0, np.nan)
    return x

def allowed_session(label: str, filt: str) -> bool:
    if filt == "ALL":
        return True
    if filt == "ACTIVE":
        return label in {"KR_ACTIVE", "US_ACTIVE"}
    return label == filt

def max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    curve = np.r_[0.0, equity]
    peak = np.maximum.accumulate(curve)
    return float(np.max(peak - curve))

def metrics_from_trades(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {
            "trades": 0, "net_return": 0.0, "gross_return": 0.0, "max_drawdown": 0.0,
            "win_rate": 0.0, "avg_trade": 0.0, "median_trade": 0.0,
            "profit_factor": 0.0, "median_hold_bars": 0.0,
        }
    net = trades["net_return"].to_numpy(float)
    gross = trades["gross_return"].to_numpy(float)
    eq = np.cumsum(net)
    wins = net[net > 0]
    losses = net[net < 0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and abs(losses.sum()) > 0 else float("inf")
    return {
        "trades": int(len(trades)),
        "net_return": float(net.sum()),
        "gross_return": float(gross.sum()),
        "max_drawdown": max_drawdown(eq),
        "win_rate": float((net > 0).mean()),
        "avg_trade": float(net.mean()),
        "median_trade": float(np.median(net)),
        "profit_factor": pf,
        "median_hold_bars": float(np.median(trades["hold_bars"])),
    }

def backtest(featured: pd.DataFrame, params: Params, cost_bps: float,
             start_idx: int, end_idx: int) -> tuple[dict, pd.DataFrame]:
    prem_col = f"premium_bps_{params.window}"
    z_col = f"z_{params.window}"
    n = len(featured)
    start_idx = max(start_idx, params.window)
    end_idx = min(end_idx, n - 2)
    pos = None
    rows: list[dict] = []
    for i in range(start_idx, end_idx + 1):
        prem = float(featured.at[i, prem_col]) if pd.notna(featured.at[i, prem_col]) else math.nan
        z = float(featured.at[i, z_col]) if pd.notna(featured.at[i, z_col]) else math.nan
        if not np.isfinite(prem) or not np.isfinite(z):
            continue
        if pos is None:
            if not allowed_session(str(featured.at[i, "session"]), params.session_filter):
                continue
            if abs(z) < params.entry_z or abs(prem) < params.entry_bps:
                continue
            direction = -1 if prem > 0 else 1
            entry_idx = i + 1
            pos = {
                "direction": direction,
                "entry_idx": entry_idx,
                "entry_time": featured.at[entry_idx, "time"],
                "entry_a": float(featured.at[entry_idx, "a_open"]),
                "entry_b": float(featured.at[entry_idx, "b_open"]),
                "entry_signal_premium_bps": prem,
                "entry_signal_z": z,
                "entry_session": str(featured.at[i, "session"]),
                "entry_sign": int(np.sign(prem)),
            }
            continue
        hold = i - pos["entry_idx"] + 1
        reverted = abs(prem) <= params.exit_bps
        adverse_stop = (int(np.sign(prem)) == pos["entry_sign"] and
                        (abs(z) >= params.stop_z or abs(prem) >= params.stop_bps))
        timeout = hold >= params.max_hold
        if not (reverted or adverse_stop or timeout):
            continue
        exit_idx = i + 1
        exit_a = float(featured.at[exit_idx, "a_open"])
        exit_b = float(featured.at[exit_idx, "b_open"])
        ret_a = exit_a / pos["entry_a"] - 1.0
        ret_b = exit_b / pos["entry_b"] - 1.0
        gross = pos["direction"] * 0.5 * (ret_a - ret_b)
        cost = 2.0 * cost_bps / 10000.0
        reason = "REVERT" if reverted else ("STOP" if adverse_stop else "TIME")
        rows.append({**pos, "exit_idx": exit_idx, "exit_time": featured.at[exit_idx, "time"],
                     "exit_a": exit_a, "exit_b": exit_b,
                     "exit_signal_premium_bps": prem, "exit_signal_z": z,
                     "hold_bars": hold, "hold_minutes": hold * 5, "reason": reason,
                     "gross_return": gross, "cost_return": cost, "net_return": gross - cost})
        pos = None
    if pos is not None:
        exit_idx = min(end_idx + 1, n - 1)
        exit_a = float(featured.at[exit_idx, "a_open"])
        exit_b = float(featured.at[exit_idx, "b_open"])
        ret_a = exit_a / pos["entry_a"] - 1.0
        ret_b = exit_b / pos["entry_b"] - 1.0
        gross = pos["direction"] * 0.5 * (ret_a - ret_b)
        cost = 2.0 * cost_bps / 10000.0
        rows.append({**pos, "exit_idx": exit_idx, "exit_time": featured.at[exit_idx, "time"],
                     "exit_a": exit_a, "exit_b": exit_b,
                     "exit_signal_premium_bps": float(featured.at[exit_idx, prem_col]),
                     "exit_signal_z": float(featured.at[exit_idx, z_col]),
                     "hold_bars": max(1, exit_idx - pos["entry_idx"]),
                     "hold_minutes": max(1, exit_idx - pos["entry_idx"]) * 5,
                     "reason": "END", "gross_return": gross, "cost_return": cost,
                     "net_return": gross - cost})
    trades = pd.DataFrame(rows)
    return metrics_from_trades(trades), trades

def threshold_reversion(featured: pd.DataFrame, window: int, threshold: float,
                        exit_bps: float = 10.0, horizon: int = 72) -> pd.DataFrame:
    p = featured[f"premium_bps_{window}"].to_numpy(float)
    times = featured["time"].to_numpy()
    rows = []
    i = window + 1
    while i < len(p) - 1:
        if (not np.isfinite(p[i]) or abs(p[i]) < threshold or
                (np.isfinite(p[i - 1]) and abs(p[i - 1]) >= threshold)):
            i += 1
            continue
        sign = 1 if p[i] > 0 else -1
        end = min(len(p) - 1, i + horizon)
        hit = None
        for j in range(i + 1, end + 1):
            if np.isfinite(p[j]) and abs(p[j]) <= exit_bps:
                hit = j
                break
        rows.append({"threshold_bps": threshold,
                     "direction": "SKHY_PREMIUM" if sign > 0 else "SKHY_DISCOUNT",
                     "start_time": pd.Timestamp(times[i]), "entry_premium_bps": p[i],
                     "reverted": hit is not None,
                     "bars_to_exit": (hit - i) if hit is not None else np.nan,
                     "minutes_to_exit": (hit - i) * 5 if hit is not None else np.nan,
                     "min_abs_premium_within_horizon": float(np.nanmin(np.abs(p[i + 1:end + 1])))})
        i = (hit + 1) if hit is not None else (end + 1)
    return pd.DataFrame(rows)

def summarize_reversion(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    out = []
    for (thr, direction), g in events.groupby(["threshold_bps", "direction"], dropna=False):
        rev = g[g["reverted"]]
        out.append({"threshold_bps": thr, "direction": direction, "events": len(g),
                    "reversion_rate_6h": float(g["reverted"].mean()),
                    "median_minutes_to_10bps": float(rev["minutes_to_exit"].median()) if len(rev) else np.nan,
                    "p75_minutes_to_10bps": float(rev["minutes_to_exit"].quantile(0.75)) if len(rev) else np.nan,
                    "median_entry_abs_bps": float(g["entry_premium_bps"].abs().median())})
    for thr, g in events.groupby("threshold_bps"):
        rev = g[g["reverted"]]
        out.append({"threshold_bps": thr, "direction": "BOTH", "events": len(g),
                    "reversion_rate_6h": float(g["reverted"].mean()),
                    "median_minutes_to_10bps": float(rev["minutes_to_exit"].median()) if len(rev) else np.nan,
                    "p75_minutes_to_10bps": float(rev["minutes_to_exit"].quantile(0.75)) if len(rev) else np.nan,
                    "median_entry_abs_bps": float(g["entry_premium_bps"].abs().median())})
    return pd.DataFrame(out).sort_values(["threshold_bps", "direction"]).reset_index(drop=True)

def stationarity_stats(series: pd.Series) -> dict:
    s = series.replace([np.inf, -np.inf], np.nan).dropna()
    result = {"adf_stat": np.nan, "adf_p": np.nan,
              "half_life_bars": np.nan, "half_life_minutes": np.nan}
    if len(s) < 100:
        return result
    try:
        adf = adfuller(s.to_numpy(), autolag="AIC")
        result["adf_stat"] = float(adf[0])
        result["adf_p"] = float(adf[1])
    except Exception:
        pass
    try:
        x = s.shift(1).dropna()
        y = s.diff().dropna().reindex(x.index)
        beta = float(np.polyfit(x.to_numpy(), y.to_numpy(), 1)[0])
        if beta < 0:
            hl = -math.log(2.0) / beta
            result["half_life_bars"] = float(hl)
            result["half_life_minutes"] = float(hl * 5)
    except Exception:
        pass
    return result

def lead_lag(df: pd.DataFrame, max_lag: int = 12) -> pd.DataFrame:
    rows = []
    for lag in range(-max_lag, max_lag + 1):
        corr = df["ret_a"].corr(df["ret_b"].shift(-lag))
        rows.append({"lag_bars": lag, "lag_minutes": lag * 5, "corr_a_now_b_later": corr})
    return pd.DataFrame(rows)

def distribution_table(featured: pd.DataFrame, window: int) -> pd.DataFrame:
    col = f"premium_bps_{window}"
    rows = []
    for name, g in [("ALL", featured)] + list(featured.groupby("session")):
        s = g[col].dropna()
        if s.empty:
            continue
        rows.append({"session": name, "count": int(len(s)), "mean_bps": float(s.mean()),
                     "median_bps": float(s.median()), "std_bps": float(s.std(ddof=1)),
                     "p01_bps": float(s.quantile(0.01)), "p05_bps": float(s.quantile(0.05)),
                     "p10_bps": float(s.quantile(0.10)), "p25_bps": float(s.quantile(0.25)),
                     "p75_bps": float(s.quantile(0.75)), "p90_bps": float(s.quantile(0.90)),
                     "p95_bps": float(s.quantile(0.95)), "p99_bps": float(s.quantile(0.99)),
                     "abs_p50_bps": float(s.abs().quantile(0.50)),
                     "abs_p75_bps": float(s.abs().quantile(0.75)),
                     "abs_p90_bps": float(s.abs().quantile(0.90)),
                     "abs_p95_bps": float(s.abs().quantile(0.95)),
                     "abs_p99_bps": float(s.abs().quantile(0.99))})
    return pd.DataFrame(rows)

def quality(df: pd.DataFrame, name: str) -> dict:
    if df.empty:
        return {"name": name, "rows": 0}
    times = df["time"].sort_values()
    diffs = times.diff().dropna().dt.total_seconds() / 60.0
    return {"name": name, "rows": int(len(df)), "first": times.iloc[0].isoformat(),
            "last": times.iloc[-1].isoformat(),
            "duplicates": int(df["open_time"].duplicated().sum()) if "open_time" in df else 0,
            "non_5m_gaps": int((diffs != 5).sum()),
            "largest_gap_minutes": float(diffs.max()) if len(diffs) else 0.0}

def plot_outputs(featured: pd.DataFrame, window: int, selected: Params,
                 trades: pd.DataFrame, cost_table: pd.DataFrame, out: Path) -> None:
    prem = featured.set_index("time")[f"premium_bps_{window}"]
    plt.figure(figsize=(15, 6))
    plt.plot(prem.index, prem.to_numpy(), linewidth=0.8)
    plt.axhline(0, linewidth=0.8)
    plt.axhline(selected.entry_bps, linestyle="--", linewidth=0.8)
    plt.axhline(-selected.entry_bps, linestyle="--", linewidth=0.8)
    plt.axhline(selected.exit_bps, linestyle=":", linewidth=0.8)
    plt.axhline(-selected.exit_bps, linestyle=":", linewidth=0.8)
    plt.title(f"Dynamic premium, {window}-bar baseline (bps)")
    plt.xlabel("UTC")
    plt.ylabel("Premium (bps)")
    plt.tight_layout()
    plt.savefig(out / "premium_full_history.png", dpi=160)
    plt.close()
    s = prem.dropna()
    plt.figure(figsize=(10, 6))
    plt.hist(s.to_numpy(), bins=100)
    plt.title("Dynamic premium distribution")
    plt.xlabel("Premium (bps)")
    plt.ylabel("Count")
    plt.tight_layout()
    plt.savefig(out / "premium_distribution.png", dpi=160)
    plt.close()
    plt.figure(figsize=(10, 6))
    plt.plot(cost_table["cost_bps"], cost_table["net_return_pct"], marker="o")
    plt.axhline(0, linewidth=0.8)
    plt.title("Selected strategy cost sensitivity")
    plt.xlabel("Per-leg per-side cost (bps)")
    plt.ylabel("Cumulative net return on gross notional (%)")
    plt.tight_layout()
    plt.savefig(out / "cost_sensitivity.png", dpi=160)
    plt.close()
    if not trades.empty:
        eq = trades["net_return"].cumsum() * 100.0
        plt.figure(figsize=(12, 6))
        plt.plot(pd.to_datetime(trades["exit_time"]), eq.to_numpy(), marker=".")
        plt.axhline(0, linewidth=0.8)
        plt.title("Out-of-sample cumulative net return")
        plt.xlabel("UTC")
        plt.ylabel("Return on gross notional (%)")
        plt.tight_layout()
        plt.savefig(out / "oos_equity.png", dpi=160)
        plt.close()

def pct(x: float) -> str:
    return f"{100.0 * x:.4f}%"

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default="hynix_5m_report")
    ap.add_argument("--end-date", default=None)
    args = ap.parse_args()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "raw").mkdir(exist_ok=True)
    end_date = date.fromisoformat(args.end_date) if args.end_date else (datetime.now(timezone.utc).date() - timedelta(days=1))
    if end_date < START_DATE:
        raise RuntimeError("end date before start date")
    dl = Downloader()
    a = fetch_daily_klines(dl, SYMBOL_A, end_date)
    b = fetch_daily_klines(dl, SYMBOL_B, end_date)
    a.to_csv(out / "raw" / f"{SYMBOL_A}_5m.csv", index=False)
    b.to_csv(out / "raw" / f"{SYMBOL_B}_5m.csv", index=False)
    aligned = align(a, b)
    if len(aligned) < 1000:
        raise RuntimeError(f"too few common bars: {len(aligned)}")
    aligned.to_csv(out / "aligned_5m.csv", index=False)
    windows = [144, 288, 576]
    featured_by_window = {w: add_features(aligned, w) for w in windows}
    split = int(len(aligned) * 0.60)
    split_time = aligned.at[split, "time"]
    grid_rows = []
    for w in windows:
        f = featured_by_window[w]
        for entry_z in [2.0, 2.5, 3.0]:
            for entry_bps in [30.0, 40.0, 50.0, 60.0, 80.0]:
                for exit_bps in [5.0, 10.0, 15.0]:
                    for max_hold in [24, 48, 72]:
                        for sess in ["ALL", "ACTIVE", "KR_ACTIVE", "US_ACTIVE"]:
                            p = Params(window=w, entry_z=entry_z, entry_bps=entry_bps,
                                       exit_bps=exit_bps, max_hold=max_hold, session_filter=sess)
                            train_m, _ = backtest(f, p, 2.0, w, split - 1)
                            score = train_m["net_return"] - 0.75 * train_m["max_drawdown"]
                            if train_m["trades"] < 8:
                                score = -999.0
                            grid_rows.append({**asdict(p),
                                              **{f"train_{k}": v for k, v in train_m.items()},
                                              "train_score": score})
    grid = pd.DataFrame(grid_rows).sort_values(["train_score", "train_net_return"], ascending=False)
    grid.to_csv(out / "parameter_grid_train.csv", index=False)
    best = grid.iloc[0]
    selected = Params(window=int(best["window"]), entry_z=float(best["entry_z"]),
                      entry_bps=float(best["entry_bps"]), exit_bps=float(best["exit_bps"]),
                      max_hold=int(best["max_hold"]), session_filter=str(best["session_filter"]),
                      stop_z=float(best["stop_z"]), stop_bps=float(best["stop_bps"]))
    fsel = featured_by_window[selected.window]
    train_m, train_t = backtest(fsel, selected, 2.0, selected.window, split - 1)
    test_m, test_t = backtest(fsel, selected, 2.0, split, len(fsel) - 2)
    full_m, full_t = backtest(fsel, selected, 2.0, selected.window, len(fsel) - 2)
    train_t.to_csv(out / "selected_train_trades.csv", index=False)
    test_t.to_csv(out / "selected_test_trades.csv", index=False)
    full_t.to_csv(out / "selected_full_trades.csv", index=False)
    costs = []
    for c in [0, 1, 2, 3, 5, 8, 10]:
        m, _ = backtest(fsel, selected, float(c), selected.window, len(fsel) - 2)
        costs.append({"cost_bps": c, **m, "net_return_pct": 100.0 * m["net_return"],
                      "max_drawdown_pct": 100.0 * m["max_drawdown"],
                      "avg_trade_pct": 100.0 * m["avg_trade"]})
    cost_table = pd.DataFrame(costs)
    cost_table.to_csv(out / "cost_sensitivity.csv", index=False)
    diag_window = 288
    fdiag = featured_by_window[diag_window]
    dist = distribution_table(fdiag, diag_window)
    dist.to_csv(out / "premium_distribution_stats.csv", index=False)
    all_events = []
    for threshold in [20, 30, 40, 50, 60, 80, 100, 120, 150]:
        ev = threshold_reversion(fdiag, diag_window, float(threshold), exit_bps=10.0, horizon=72)
        if not ev.empty:
            all_events.append(ev)
    events = pd.concat(all_events, ignore_index=True) if all_events else pd.DataFrame()
    events.to_csv(out / "reversion_events.csv", index=False)
    rev_summary = summarize_reversion(events)
    rev_summary.to_csv(out / "reversion_threshold_summary.csv", index=False)
    ll = lead_lag(aligned)
    ll.to_csv(out / "lead_lag.csv", index=False)
    static_ratio = float(aligned["ratio"].median())
    aligned["static_premium_bps"] = (aligned["ratio"] / static_ratio - 1.0) * 10000.0
    aligned.to_csv(out / "aligned_5m_with_static_premium.csv", index=False)
    stationarity = stationarity_stats(aligned["log_ratio"] - aligned["log_ratio"].mean())
    dynamic_stationarity = stationarity_stats(fdiag[f"premium_bps_{diag_window}"] / 10000.0)
    corr = float(aligned["ret_a"].corr(aligned["ret_b"]))
    best_lag = ll.iloc[ll["corr_a_now_b_later"].abs().idxmax()].to_dict()
    months = sorted({d.strftime("%Y-%m") for d in daterange(START_DATE, end_date)})
    fa = fetch_monthly_funding(dl, SYMBOL_A, months)
    fb = fetch_monthly_funding(dl, SYMBOL_B, months)
    if not fa.empty:
        fa.to_csv(out / "raw" / f"{SYMBOL_A}_funding.csv", index=False)
    if not fb.empty:
        fb.to_csv(out / "raw" / f"{SYMBOL_B}_funding.csv", index=False)
    funding_summary = {}
    if not fa.empty and not fb.empty:
        fm = fa[["time", "funding_rate"]].rename(columns={"funding_rate": "a_rate"}).merge(
            fb[["time", "funding_rate"]].rename(columns={"funding_rate": "b_rate"}), on="time", how="inner")
        fm["a_minus_b"] = fm["a_rate"] - fm["b_rate"]
        fm.to_csv(out / "funding_aligned.csv", index=False)
        funding_summary = {"events": int(len(fm)),
                           "mean_a": float(fm["a_rate"].mean()) if len(fm) else np.nan,
                           "mean_b": float(fm["b_rate"].mean()) if len(fm) else np.nan,
                           "mean_a_minus_b": float(fm["a_minus_b"].mean()) if len(fm) else np.nan,
                           "sum_a_minus_b": float(fm["a_minus_b"].sum()) if len(fm) else np.nan,
                           "first": fm["time"].min().isoformat() if len(fm) else None,
                           "last": fm["time"].max().isoformat() if len(fm) else None}
    direction_stats = []
    if not test_t.empty:
        for direction, g in test_t.groupby("direction"):
            m = metrics_from_trades(g)
            direction_stats.append({"direction": "LONG_SKHY_SHORT_SKHYNIX" if direction == 1 else "SHORT_SKHY_LONG_SKHYNIX", **m})
    pd.DataFrame(direction_stats).to_csv(out / "oos_direction_stats.csv", index=False)
    latest = {"time": aligned.iloc[-1]["time"].isoformat(),
              "a_close": float(aligned.iloc[-1]["a_close"]),
              "b_close": float(aligned.iloc[-1]["b_close"]),
              "raw_ratio": float(aligned.iloc[-1]["ratio"]),
              "static_premium_bps": float(aligned.iloc[-1]["static_premium_bps"]),
              "dynamic_premium_bps_288": float(fdiag.iloc[-1][f"premium_bps_{diag_window}"]),
              "z_288": float(fdiag.iloc[-1][f"z_{diag_window}"]),
              "session": str(aligned.iloc[-1]["session"])}
    summary = {"data": {"archive_end_date": end_date.isoformat(), "common_rows": int(len(aligned)),
                         "common_first": aligned["time"].iloc[0].isoformat(),
                         "common_last": aligned["time"].iloc[-1].isoformat(),
                         "split_time": split_time.isoformat(),
                         "quality": [quality(a, SYMBOL_A), quality(b, SYMBOL_B), quality(aligned, "COMMON")]},
               "relationship": {"median_scale_ratio_a_over_b": static_ratio,
                                "same_bar_log_return_correlation": corr,
                                "best_lead_lag": best_lag,
                                "static_log_ratio_stationarity": stationarity,
                                "dynamic_premium_stationarity": dynamic_stationarity},
               "selected_params": asdict(selected), "selected_train": train_m,
               "selected_test": test_m, "selected_full": full_m,
               "funding_summary": funding_summary, "latest": latest}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    (out / "download_manifest.json").write_text(json.dumps(dl.manifest, ensure_ascii=False, indent=2))
    strategy_config = {"premium_definition": "10000 * (SKHY/SKHYNIX ratio divided by prior rolling ratio mean - 1)",
                       "interval": "5m", "window_bars": selected.window,
                       "entry_z": selected.entry_z, "entry_premium_bps": selected.entry_bps,
                       "exit_premium_bps": selected.exit_bps, "stop_z": selected.stop_z,
                       "stop_premium_bps": selected.stop_bps, "max_hold_bars": selected.max_hold,
                       "max_hold_minutes": selected.max_hold * 5,
                       "session_filter": selected.session_filter,
                       "execution": "signal on close; execute at next bar open; equal gross notional weights",
                       "cost_assumption": "2 bps per leg per side; 4 bps round trip on total gross notional"}
    (out / "strategy_config.json").write_text(json.dumps(strategy_config, ensure_ascii=False, indent=2))
    plot_outputs(fdiag, diag_window, selected, test_t, cost_table, out)
    all_dist = dist[dist["session"] == "ALL"].iloc[0].to_dict()
    rev_both = rev_summary[rev_summary["direction"] == "BOTH"].copy()
    rev_lines = []
    for _, r in rev_both.iterrows():
        rev_lines.append(f"| {r['threshold_bps']:.0f} | {int(r['events'])} | {100*r['reversion_rate_6h']:.1f}% | {r['median_minutes_to_10bps']:.0f} | {r['p75_minutes_to_10bps']:.0f} |")
    cost_lines = []
    for _, r in cost_table.iterrows():
        cost_lines.append(f"| {r['cost_bps']:.0f} | {int(r['trades'])} | {r['net_return_pct']:.4f}% | {r['max_drawdown_pct']:.4f}% | {100*r['win_rate']:.1f}% | {r['avg_trade_pct']:.4f}% |")
    report = f"""# Binance SKHYUSDT / SKHYNIXUSDT 全历史 5 分钟价差研究

## 1. 数据边界

- 数据源：Binance Data Vision USDⓈ-M Futures 官方日度归档。
- 严格共同样本：**{len(aligned):,} 根 5 分钟 K 线**。
- 共同区间：**{aligned['time'].iloc[0].isoformat()} — {aligned['time'].iloc[-1].isoformat()}**。
- 不向前填充、不插值，只保留同一 `open_time` 两边都存在的 K 线。
- 回测信号在收盘生成，下一根开盘执行。
- 收益按总毛名义计算：每腿占 50%，两腿合计毛名义为 1。

## 2. 价差定义

两个合约价格单位不同，不能直接相减。使用：

```text
ratio_t = SKHYUSDT_t / SKHYNIXUSDT_t
dynamic_premium_bps =
    10000 × [ratio_t / exp(过去 window 根 log(ratio) 均值) - 1]
```

滚动中枢和标准差均 `shift(1)`，当前 K 线不会参与自己的基准计算。

- 全样本中位换算比例 `SKHY / SKHYNIX`：**{static_ratio:.8f}**。
- 同一 5 分钟对数收益相关性：**{corr:.4f}**。
- 静态 log-ratio ADF p 值：**{stationarity['adf_p']:.6f}**。
- 288 根动态残差 ADF p 值：**{dynamic_stationarity['adf_p']:.6f}**。
- 动态残差估计半衰期：**{dynamic_stationarity['half_life_minutes']:.1f} 分钟**。

## 3. 288 根（24 小时）动态溢价分布

| 指标 | bps | 百分比 |
|---|---:|---:|
| 均值 | {all_dist['mean_bps']:.2f} | {all_dist['mean_bps']/100:.4f}% |
| 中位数 | {all_dist['median_bps']:.2f} | {all_dist['median_bps']/100:.4f}% |
| 标准差 | {all_dist['std_bps']:.2f} | {all_dist['std_bps']/100:.4f}% |
| 绝对溢价 P50 | {all_dist['abs_p50_bps']:.2f} | {all_dist['abs_p50_bps']/100:.4f}% |
| 绝对溢价 P75 | {all_dist['abs_p75_bps']:.2f} | {all_dist['abs_p75_bps']/100:.4f}% |
| 绝对溢价 P90 | {all_dist['abs_p90_bps']:.2f} | {all_dist['abs_p90_bps']/100:.4f}% |
| 绝对溢价 P95 | {all_dist['abs_p95_bps']:.2f} | {all_dist['abs_p95_bps']/100:.4f}% |
| 绝对溢价 P99 | {all_dist['abs_p99_bps']:.2f} | {all_dist['abs_p99_bps']/100:.4f}% |
| 有符号 P05 | {all_dist['p05_bps']:.2f} | {all_dist['p05_bps']/100:.4f}% |
| 有符号 P95 | {all_dist['p95_bps']:.2f} | {all_dist['p95_bps']/100:.4f}% |

## 4. 阈值触发后 6 小时内回到 ±10 bps 的统计

| 入场绝对溢价（bps） | 事件数 | 6小时回归率 | 回归中位分钟 | 回归P75分钟 |
|---:|---:|---:|---:|---:|
{chr(10).join(rev_lines)}

## 5. 训练集选择出的策略

```json
{json.dumps(strategy_config, ensure_ascii=False, indent=2)}
```

训练集（前 60%）：

- 交易数：**{train_m['trades']}**
- 净收益：**{pct(train_m['net_return'])}**
- 最大回撤：**{pct(train_m['max_drawdown'])}**
- 胜率：**{100*train_m['win_rate']:.1f}%**
- 平均每笔：**{pct(train_m['avg_trade'])}**

严格样本外测试集（后 40%）：

- 交易数：**{test_m['trades']}**
- 净收益：**{pct(test_m['net_return'])}**
- 最大回撤：**{pct(test_m['max_drawdown'])}**
- 胜率：**{100*test_m['win_rate']:.1f}%**
- 平均每笔：**{pct(test_m['avg_trade'])}**
- 利润因子：**{test_m['profit_factor']:.3f}**
- 中位持仓：**{test_m['median_hold_bars']*5:.0f} 分钟**

## 6. 成本敏感性

每腿每边成本包含手续费与滑点。按总毛名义口径，往返成本等于 `2 × 单腿单边成本`。

| 每腿单边成本（bps） | 交易数 | 累计净收益 | 最大回撤 | 胜率 | 平均每笔 |
|---:|---:|---:|---:|---:|---:|
{chr(10).join(cost_lines)}

## 7. 实盘解释

- `premium > 0`：SKHY 相对贵，方向为 **空 SKHYUSDT / 多 SKHYNIXUSDT**。
- `premium < 0`：SKHY 相对便宜，方向为 **多 SKHYUSDT / 空 SKHYNIXUSDT**。
- K 线结果是历史代理价差，不含真实 bid/ask、排队、冲击成本和单腿先成交风险。
- 资金费率归档只覆盖已发布完整月份；`funding_summary` 单独保存，不能假定未来相同。
- 两份合约对应不同地区的价格发现，滚动中枢必须持续更新，不能长期固定换算比例。
"""
    (out / "REPORT.md").write_text(report)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

if __name__ == "__main__":
    main()
