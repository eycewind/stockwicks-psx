import re
from pathlib import Path
from datetime import datetime
import pandas as pd

LOG_PATH = Path("/var/www/stockwicks/data/116/spx0dte_bot.log")
OUT_DIR = Path(__file__).resolve().parent / "spx_option_audit"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TRADES = [
    {"entry_time": "2026-03-11 15:43:01", "exit_time": "2026-03-11 15:46:02", "side": "BUY CALL", "strike": 6770.0, "exp": "2026-03-11"},
    {"entry_time": "2026-03-11 15:39:07", "exit_time": "2026-03-11 15:42:01", "side": "BUY CALL", "strike": 6770.0, "exp": "2026-03-11"},
    {"entry_time": "2026-03-11 15:29:08", "exit_time": "2026-03-11 15:31:28", "side": "BUY CALL", "strike": 6760.0, "exp": "2026-03-11"},
    {"entry_time": "2026-03-11 15:25:01", "exit_time": "2026-03-11 15:27:00", "side": "BUY CALL", "strike": 6760.0, "exp": "2026-03-11"},
    {"entry_time": "2026-03-11 15:18:02", "exit_time": "2026-03-11 15:24:05", "side": "BUY PUT",  "strike": 6760.0, "exp": "2026-03-11"},
    {"entry_time": "2026-03-11 15:08:01", "exit_time": "2026-03-11 15:17:00", "side": "BUY PUT",  "strike": 6760.0, "exp": "2026-03-11"},
    {"entry_time": "2026-03-11 15:05:02", "exit_time": "2026-03-11 15:07:01", "side": "BUY PUT",  "strike": 6760.0, "exp": "2026-03-11"},
    {"entry_time": "2026-03-11 14:52:01", "exit_time": "2026-03-11 15:04:01", "side": "BUY PUT",  "strike": 6765.0, "exp": "2026-03-11"},
    {"entry_time": "2026-03-11 14:48:01", "exit_time": "2026-03-11 14:50:44", "side": "BUY PUT",  "strike": 6765.0, "exp": "2026-03-11"},
    {"entry_time": "2026-03-11 14:41:01", "exit_time": "2026-03-11 14:47:01", "side": "BUY CALL", "strike": 6760.0, "exp": "2026-03-11"},
    {"entry_time": "2026-03-11 14:38:01", "exit_time": "2026-03-11 14:40:56", "side": "BUY CALL", "strike": 6765.0, "exp": "2026-03-11"},
    {"entry_time": "2026-03-11 14:34:01", "exit_time": "2026-03-11 14:37:00", "side": "BUY CALL", "strike": 6765.0, "exp": "2026-03-11"},
]

def exp_to_yymmdd(exp_date: str) -> str:
    return datetime.strptime(exp_date, "%Y-%m-%d").strftime("%y%m%d")

def build_occ(side: str, strike: float, exp_date: str) -> str:
    cp = "C" if "CALL" in side.upper() else "P"
    strike_int = int(round(strike * 1000))
    return f"SPXW{exp_to_yymmdd(exp_date)}{cp}{strike_int:08d}"

def unique_occs(trades):
    occs = []
    for t in trades:
        occ = build_occ(t["side"], t["strike"], t["exp"])
        if occ not in occs:
            occs.append(occ)
    return occs

def parse_log_for_occ(log_path: Path, occ: str) -> pd.DataFrame:
    rows = []
    ts_re = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    mark_re = re.compile(r"mark(?:=|\s)(?P<mark>\d+(?:\.\d+)?)", re.IGNORECASE)

    with log_path.open("r", errors="ignore") as f:
        for line_no, line in enumerate(f, start=1):
            if occ not in line:
                continue
            ts_m = ts_re.search(line)
            if not ts_m:
                continue
            mark_m = mark_re.search(line)
            ts = pd.to_datetime(ts_m.group("ts"))
            mark = float(mark_m.group("mark")) if mark_m else None
            rows.append({
                "timestamp": ts,
                "minute": ts.floor("min"),
                "occ": occ,
                "mark": mark,
                "line_no": line_no,
                "raw_line": line.strip(),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df = df.sort_values(["timestamp", "line_no"]).reset_index(drop=True)
    return df

def build_minute_presence(raw_df: pd.DataFrame, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    full_minutes = pd.date_range(start_ts.floor("min"), end_ts.floor("min"), freq="1min")
    counts = raw_df.groupby("minute").size().rename("obs_count") if not raw_df.empty else pd.Series(dtype=int)

    out = pd.DataFrame({"minute": full_minutes})
    out = out.merge(counts, on="minute", how="left")
    out["obs_count"] = out["obs_count"].fillna(0).astype(int)
    out["has_real_obs"] = out["obs_count"] > 0
    return out

def build_minute_ohlc(raw_df: pd.DataFrame) -> pd.DataFrame:
    usable = raw_df.dropna(subset=["mark"]).copy()
    if usable.empty:
        return pd.DataFrame(columns=["minute", "open", "high", "low", "close", "obs_count"])

    ohlc = (
        usable.groupby("minute")["mark"]
        .agg(open="first", high="max", low="min", close="last", obs_count="size")
        .reset_index()
        .sort_values("minute")
    )
    return ohlc

def main():
    occs = unique_occs(TRADES)
    trade_df = pd.DataFrame(TRADES)
    trade_df["occ"] = trade_df.apply(lambda r: build_occ(r["side"], r["strike"], r["exp"]), axis=1)

    summary_rows = []

    for occ in occs:
        occ_trades = trade_df[trade_df["occ"] == occ].copy()
        start_ts = pd.to_datetime(occ_trades["entry_time"]).min() - pd.Timedelta(minutes=2)
        end_ts = pd.to_datetime(occ_trades["exit_time"]).max() + pd.Timedelta(minutes=2)

        raw_df = parse_log_for_occ(LOG_PATH, occ)
        if not raw_df.empty:
            raw_df = raw_df[(raw_df["timestamp"] >= start_ts) & (raw_df["timestamp"] <= end_ts)].copy()

        presence_df = build_minute_presence(raw_df, start_ts, end_ts)
        ohlc_df = build_minute_ohlc(raw_df)

        raw_path = OUT_DIR / f"raw_marks_{occ}.csv"
        presence_path = OUT_DIR / f"minute_presence_{occ}.csv"
        ohlc_path = OUT_DIR / f"minute_ohlc_{occ}.csv"

        raw_df.to_csv(raw_path, index=False)
        presence_df.to_csv(presence_path, index=False)
        ohlc_df.to_csv(ohlc_path, index=False)

        total_minutes = len(presence_df)
        covered_minutes = int(presence_df["has_real_obs"].sum())
        coverage_pct = round((covered_minutes / total_minutes * 100.0), 2) if total_minutes else 0.0
        total_raw_obs = 0 if raw_df.empty else len(raw_df)
        total_ohlc_minutes = 0 if ohlc_df.empty else len(ohlc_df)

        if coverage_pct >= 90:
            verdict = "GOOD_1MIN_COVERAGE"
        elif coverage_pct >= 50:
            verdict = "PARTIAL_COVERAGE"
        elif coverage_pct > 0:
            verdict = "SPARSE_NOT_TRUE_1MIN"
        else:
            verdict = "NO_DATA_FOUND"

        summary_rows.append({
            "occ": occ,
            "trade_count": len(occ_trades),
            "window_start": start_ts,
            "window_end": end_ts,
            "total_window_minutes": total_minutes,
            "minutes_with_real_obs": covered_minutes,
            "coverage_pct": coverage_pct,
            "raw_log_observations": total_raw_obs,
            "minutes_with_ohlc": total_ohlc_minutes,
            "verdict": verdict,
        })

    summary_df = pd.DataFrame(summary_rows).sort_values("occ")
    summary_df.to_csv(OUT_DIR / "summary.csv", index=False)

    print(f"\nAudit files written to: {OUT_DIR}\n")
    print(summary_df.to_string(index=False))

if __name__ == "__main__":
    main()