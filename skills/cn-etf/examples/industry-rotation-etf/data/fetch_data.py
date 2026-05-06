"""
行业 ETF 轮动策略数据获取脚本

优先级: ftshare > akshare > Mock（仅离线演示）

用法:
    python3 fetch_data.py --config ../config.yaml --output .
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def fetch(config: dict) -> dict[str, pd.DataFrame]:
    """
    返回 {code: DataFrame(date, open, high, low, close, volume)}
    """
    try:
        return _fetch_via_ftshare(config)
    except Exception as exc:
        print(f"[fetch_data] ftshare 不可用，尝试 akshare ({exc})")

    try:
        return _fetch_via_akshare(config)
    except Exception as exc:
        print(f"[fetch_data] akshare 不可用，回落 Mock 数据 ({exc})")

    return _fetch_mock(config)


def _fetch_via_ftshare(config: dict) -> dict[str, pd.DataFrame]:
    import ftshare

    out = {}
    start = config.get("start_date", "2020-01-01")
    end = config.get("end_date", "2025-12-31")
    for sym in config["symbols"]:
        code = sym["code"]
        df = ftshare.fund_etf_hist_em(symbol=code, start_date=start, end_date=end)
        df = _normalize_columns(df)
        out[code] = df
    return out


def _fetch_via_akshare(config: dict) -> dict[str, pd.DataFrame]:
    import akshare as ak

    out = {}
    start = config.get("start_date", "2020-01-01").replace("-", "")
    end = config.get("end_date", "2025-12-31").replace("-", "")
    for sym in config["symbols"]:
        code = sym["code"].split(".")[0]
        df = ak.fund_etf_hist_em(
            symbol=code,
            period="daily",
            start_date=start,
            end_date=end,
            adjust="hfq",
        )
        df = _normalize_columns(df)
        out[sym["code"]] = df
    return out


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名为 [date, open, high, low, close, volume]，并以 date 为索引。"""
    rename_map = {
        "日期": "date", "date": "date",
        "开盘": "open", "open": "open",
        "最高": "high", "high": "high",
        "最低": "low", "low": "low",
        "收盘": "close", "close": "close",
        "成交量": "volume", "volume": "volume",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def _fetch_mock(config: dict) -> dict[str, pd.DataFrame]:
    """无网络/无库时的 mock 数据，仅用于回测脚手架自检。"""
    import numpy as np

    out = {}
    start = pd.Timestamp(config.get("start_date", "2020-01-01"))
    end = pd.Timestamp(config.get("end_date", "2025-12-31"))
    dates = pd.bdate_range(start, end)

    for sym in config["symbols"]:
        code = sym["code"]
        np.random.seed(hash(code) % (2**31))
        base = 1.0 + (hash(code) % 100) / 100
        vol = 0.012 + (hash(code) % 7) * 0.002
        rets = np.random.normal(0.0003, vol, len(dates))
        prices = base * np.cumprod(1 + rets)
        df = pd.DataFrame(
            {
                "open": prices * (1 + np.random.uniform(-0.003, 0.003, len(dates))),
                "high": prices * (1 + np.abs(np.random.normal(0, vol * 0.6, len(dates)))),
                "low": prices * (1 - np.abs(np.random.normal(0, vol * 0.6, len(dates)))),
                "close": prices,
                "volume": np.random.randint(50_000_000, 500_000_000, len(dates)),
            },
            index=dates,
        )
        df.index.name = "date"
        out[code] = df
    return out


def _save_to_csv(data: dict[str, pd.DataFrame], output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    for code, df in data.items():
        path = output_dir / f"{code.replace('.', '_')}.csv"
        df.to_csv(path)
        print(f"  ✅ {code} → {path} ({len(df)} rows)")


def main():
    import yaml

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="../config.yaml")
    parser.add_argument("--output", default=".")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    data = fetch(config)
    _save_to_csv(data, Path(args.output))


if __name__ == "__main__":
    main()
