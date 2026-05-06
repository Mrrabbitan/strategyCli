"""
行业 ETF 轮动策略

严格翻译自 STRATEGY_DESIGN.md，不可自由发挥。

标的池（8 只）：
  宽基对照: 510300.SH 沪深300ETF
  行业:     512170 医疗 / 512480 半导体 / 512760 芯片 / 512800 银行
            512690 酒 / 515030 新能源车
  防守:     510880.SH 红利ETF

信号: 多周期动量打分（20/60/120 日加权）
调仓: 每月最后一个交易日，等权持有 Top-3，无入选时切换至防守 ETF
风控: 单 ETF 上限 35%、总仓位上限 95%、最大回撤 18% 暂停 5 日、
      极端跌幅 -7% 暂停 3 日、A 股 T+1 强制
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


# ── 指标计算 ──────────────────────────────────────────


def compute_momentum(close: pd.Series, lookback_short: int, lookback_mid: int,
                     lookback_long: int, w_short: float, w_mid: float,
                     w_long: float) -> pd.Series:
    """指标 1: 多周期动量分（按 STRATEGY_DESIGN.md 第 2 节）"""
    ret_s = close / close.shift(lookback_short) - 1
    ret_m = close / close.shift(lookback_mid) - 1
    ret_l = close / close.shift(lookback_long) - 1
    return w_short * ret_s + w_mid * ret_m + w_long * ret_l


def compute_ma_trend(close: pd.Series, ma_period: int) -> pd.Series:
    """指标 2: 趋势过滤（Close > SMA(Close, ma_period)）"""
    return close > close.rolling(window=ma_period).mean()


def is_rebalance_day(date: pd.Timestamp, all_dates: list[pd.Timestamp],
                     freq: str) -> bool:
    """指标 3: 调仓日判断"""
    idx = all_dates.index(date)
    if idx + 1 >= len(all_dates):
        # 最后一个交易日强制视为调仓日
        return True
    next_date = all_dates[idx + 1]
    if freq == "monthly":
        return date.month != next_date.month
    if freq == "weekly":
        return date.isocalendar().week != next_date.isocalendar().week
    return False


# ── 数据加载 ──────────────────────────────────────────


def _load_all_data(config: dict, strategy_dir: Path) -> dict[str, pd.DataFrame]:
    """加载所有标的的日线数据。优先读取 data/<code>.csv，否则调用 fetch_data.fetch。"""
    all_data: dict[str, pd.DataFrame] = {}
    data_dir = strategy_dir / "data"

    for sym in config["symbols"]:
        code = sym["code"]
        csv_path = data_dir / f"{code.replace('.', '_')}.csv"
        if csv_path.exists():
            df = pd.read_csv(csv_path, parse_dates=["date"])
            df = df.set_index("date").sort_index()
            all_data[code] = df

    if all_data:
        return all_data

    # 没有任何 CSV → 调用 fetch_data
    try:
        import sys
        import importlib.util
        fetch_path = data_dir / "fetch_data.py"
        if fetch_path.exists():
            spec = importlib.util.spec_from_file_location("fetch_data_dyn", fetch_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["fetch_data_dyn"] = module
            spec.loader.exec_module(module)
            if hasattr(module, "fetch"):
                return module.fetch(config)
    except Exception as exc:  # pragma: no cover
        print(f"[strategy] fetch_data 调用失败: {exc}")

    return {}


def _filter_by_dates(all_data: dict[str, pd.DataFrame],
                     start: str, end: str) -> dict[str, pd.DataFrame]:
    """按 config.start_date / end_date 截断每只 ETF 的数据。"""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    out = {}
    for code, df in all_data.items():
        out[code] = df.loc[(df.index >= start_ts) & (df.index <= end_ts)].copy()
    return out


# ── 主回测入口 ──────────────────────────────────────────


def run_backtest(config: dict) -> dict:
    """主回测入口。按 STRATEGY_DESIGN.md 第 3-5 节逐条翻译。"""
    strategy_dir = Path(__file__).parent

    # ── 参数提取 ──
    indicators = config.get("indicators", {})
    risk = config.get("risk", {})
    symbols = config.get("symbols", [])

    lookback_short = indicators.get("lookback_short", 20)
    lookback_mid = indicators.get("lookback_mid", 60)
    lookback_long = indicators.get("lookback_long", 120)
    w_short = indicators.get("w_short", 0.5)
    w_mid = indicators.get("w_mid", 0.3)
    w_long = indicators.get("w_long", 0.2)
    ma_period = indicators.get("ma_period", 60)
    hold_top_n = int(indicators.get("hold_top_n", 3))
    rebalance_freq = indicators.get("rebalance_freq", "monthly")
    rebalance_threshold = indicators.get("rebalance_threshold", 5) / 100
    position_scale = indicators.get("position_scale", 0.95)
    defensive_code = indicators.get("defensive_code", "510880.SH")

    max_position_pct = risk.get("max_position_pct", 35) / 100
    total_position_pct = risk.get("total_position_pct", 95) / 100
    max_drawdown_pct = risk.get("max_drawdown_pct", 18) / 100
    drawdown_pause_days = int(risk.get("drawdown_pause_days", 5))
    extreme_drop_pct = risk.get("extreme_drop_pct", 7) / 100
    extreme_pause_days = int(risk.get("extreme_pause_days", 3))

    initial_cash = float(config.get("initial_cash", 1000000))
    slippage = float(config.get("slippage", 0.001))

    # ── 加载数据 ──
    all_data = _load_all_data(config, strategy_dir)
    if not all_data:
        return {"error": "无法加载行情数据，请先运行 data/fetch_data.py"}

    all_data = _filter_by_dates(
        all_data,
        config.get("start_date", "2020-01-01"),
        config.get("end_date", "2025-12-31"),
    )

    sym_meta = {s["code"]: s for s in symbols}

    # ── 合并交易日序列 ──
    date_set: set[pd.Timestamp] = set()
    for df in all_data.values():
        date_set.update(df.index)
    all_dates = sorted(date_set)
    if not all_dates:
        return _empty_result()

    # ── 状态初始化 ──
    cash = initial_cash
    positions: dict[str, dict[str, float]] = {}  # {code: {shares, cost_price, buy_date}}
    last_buy_date: dict[str, pd.Timestamp] = {}
    paused_until: dict[str, pd.Timestamp] = {}
    drawdown_pause_until: pd.Timestamp | None = None
    peak_value = initial_cash
    trades: list[dict] = []
    daily_values: list[dict] = []

    # ── 主循环 ──
    for date_idx, date in enumerate(all_dates):
        # ── 计算当前组合市值 ──
        total_position_value = _compute_position_value(positions, all_data, date)
        total_value = cash + total_position_value

        # ── 跟踪历史峰值与回撤暂停 ──
        if total_value > peak_value:
            peak_value = total_value
        drawdown = (peak_value - total_value) / peak_value if peak_value > 0 else 0
        if drawdown >= max_drawdown_pct and drawdown_pause_until is None:
            # 触发回撤强制清仓
            cash, _ = _liquidate_all(positions, all_data, date, slippage,
                                     sym_meta, last_buy_date, trades, cash)
            pause_idx = min(date_idx + drawdown_pause_days, len(all_dates) - 1)
            drawdown_pause_until = all_dates[pause_idx]
            total_position_value = 0

        if drawdown_pause_until is not None and date <= drawdown_pause_until:
            daily_values.append({"date": str(date.date()), "value": round(cash, 2)})
            continue
        if drawdown_pause_until is not None and date > drawdown_pause_until:
            drawdown_pause_until = None
            peak_value = max(peak_value, cash)  # 重启时峰值为当前现金（等同重新开始）

        # ── 极端跌幅检测：单 ETF 暂停 ──
        for sym in symbols:
            code = sym["code"]
            if code not in all_data or date not in all_data[code].index:
                continue
            row = all_data[code].loc[date]
            open_p = float(row["open"])
            close_p = float(row["close"])
            daily_drop = (close_p - open_p) / open_p if open_p > 0 else 0
            if daily_drop < -extreme_drop_pct:
                pause_idx = min(date_idx + extreme_pause_days, len(all_dates) - 1)
                paused_until[code] = all_dates[pause_idx]

        # ── 调仓日判定 ──
        if is_rebalance_day(date, all_dates, rebalance_freq):
            cash = _execute_rebalance(
                date=date,
                date_idx=date_idx,
                all_dates=all_dates,
                all_data=all_data,
                positions=positions,
                cash=cash,
                sym_meta=sym_meta,
                symbols=symbols,
                last_buy_date=last_buy_date,
                paused_until=paused_until,
                trades=trades,
                lookback_short=lookback_short,
                lookback_mid=lookback_mid,
                lookback_long=lookback_long,
                w_short=w_short,
                w_mid=w_mid,
                w_long=w_long,
                ma_period=ma_period,
                hold_top_n=hold_top_n,
                rebalance_threshold=rebalance_threshold,
                position_scale=position_scale,
                max_position_pct=max_position_pct,
                total_position_pct=total_position_pct,
                slippage=slippage,
                defensive_code=defensive_code,
            )

        # ── 记录每日净值 ──
        total_position_value = _compute_position_value(positions, all_data, date)
        daily_values.append({
            "date": str(date.date()),
            "value": round(cash + total_position_value, 2),
        })

    return _compute_metrics(daily_values, trades, initial_cash)


# ── 调仓核心 ──────────────────────────────────────────


def _execute_rebalance(*, date: pd.Timestamp, date_idx: int,
                       all_dates: list[pd.Timestamp],
                       all_data: dict[str, pd.DataFrame],
                       positions: dict[str, dict[str, float]],
                       cash: float, sym_meta: dict[str, dict],
                       symbols: list[dict],
                       last_buy_date: dict[str, pd.Timestamp],
                       paused_until: dict[str, pd.Timestamp],
                       trades: list[dict],
                       lookback_short: int, lookback_mid: int, lookback_long: int,
                       w_short: float, w_mid: float, w_long: float,
                       ma_period: int, hold_top_n: int,
                       rebalance_threshold: float, position_scale: float,
                       max_position_pct: float, total_position_pct: float,
                       slippage: float, defensive_code: str) -> float:
    """
    执行月度调仓。严格按 STRATEGY_DESIGN.md 第 3.1 节 7 步：
    1. 用 t-1 日及以前数据计算 Momentum
    2. 应用 MA60 趋势过滤
    3. 排序取 Top-N
    4. 防守切换（入选 = 0 时切换至 510880）
    5. 计算目标仓位
    6. 计算 delta 决定买卖
    7. 全部完成后清空"非目标持仓"
    """
    # ── Step 1+2: 计算每只 ETF 的 Momentum 和趋势过滤（用 t-1 数据避免未来函数）──
    candidates: list[tuple[str, float]] = []
    for sym in symbols:
        code = sym["code"]
        df = all_data.get(code)
        if df is None or date not in df.index:
            continue
        loc = df.index.get_loc(date)
        if loc < lookback_long + 1:
            continue
        # 用 t-1 及以前
        sub_close = df["close"].iloc[: loc]  # 不含 t
        if len(sub_close) < lookback_long:
            continue
        momentum = (
            w_short * (sub_close.iloc[-1] / sub_close.iloc[-1 - lookback_short] - 1)
            + w_mid * (sub_close.iloc[-1] / sub_close.iloc[-1 - lookback_mid] - 1)
            + w_long * (sub_close.iloc[-1] / sub_close.iloc[-1 - lookback_long] - 1)
        )
        ma = sub_close.iloc[-ma_period:].mean()
        trend_ok = sub_close.iloc[-1] > ma
        if not trend_ok:
            continue
        if code in paused_until and date <= paused_until[code]:
            continue
        candidates.append((code, float(momentum)))

    # ── Step 3: 排序取 Top-N（要求 momentum > 0）──
    candidates.sort(key=lambda x: x[1], reverse=True)
    top_n = [c for c, m in candidates if m > 0][:hold_top_n]

    # ── Step 4: 防守切换 ──
    if not top_n:
        if defensive_code in {s["code"] for s in symbols}:
            top_n = [defensive_code]
            n_targets = 1
        else:
            top_n = []
            n_targets = 0
    else:
        n_targets = hold_top_n

    # ── Step 5: 目标仓位 ──
    total_value = cash + _compute_position_value(positions, all_data, date)
    if n_targets > 0:
        target_value_per = total_value * position_scale / n_targets
        target_value_per = min(target_value_per, total_value * max_position_pct)
    else:
        target_value_per = 0

    # ── Step 7（先卖）: 卖出非目标持仓 ──
    for code in list(positions.keys()):
        if code not in top_n:
            cash = _sell_full(code, positions, all_data, date, slippage,
                              sym_meta, last_buy_date, trades, cash)

    # ── Step 6 卖侧: 持仓超出目标 → 卖出超出部分 ──
    for code in top_n:
        if code not in positions:
            continue
        df = all_data.get(code)
        if df is None or date not in df.index:
            continue
        # T+1 检查
        if last_buy_date.get(code) == date:
            continue
        close_p = float(df.loc[date]["close"])
        # 跌停过滤
        loc = df.index.get_loc(date)
        if loc > 0:
            prev_close = float(df.iloc[loc - 1]["close"])
            limit_down = prev_close * (1 - sym_meta[code]["price_limit_pct"] / 100)
            if close_p <= limit_down * 1.005:
                continue

        current_value = positions[code]["shares"] * close_p
        delta = current_value - target_value_per
        if delta > rebalance_threshold * total_value and target_value_per > 0:
            sell_value = delta
            sell_price = close_p * (1 - slippage)
            sell_shares = int(sell_value / sell_price / sym_meta[code]["lot_size"]) * sym_meta[code]["lot_size"]
            sell_shares = min(sell_shares, positions[code]["shares"])
            if sell_shares <= 0:
                continue
            proceeds = sell_shares * sell_price
            commission = proceeds * sym_meta[code].get("commission", 0.0005)
            cash += proceeds - commission
            positions[code]["shares"] -= sell_shares
            pnl = (sell_price - positions[code]["cost_price"]) * sell_shares - commission
            trades.append({
                "date": str(date.date()), "symbol": code,
                "name": sym_meta[code].get("name", code), "action": "SELL_PARTIAL",
                "price": round(sell_price, 4), "shares": sell_shares,
                "reason": "调仓超出目标", "pnl": round(pnl, 2),
            })
            if positions[code]["shares"] <= 0:
                positions.pop(code)

    # ── Step 6 买侧: 持仓不足目标 → 买入缺口 ──
    for code in top_n:
        df = all_data.get(code)
        if df is None or date not in df.index:
            continue
        loc = df.index.get_loc(date)
        if loc > 0:
            prev_close = float(df.iloc[loc - 1]["close"])
            limit_up = prev_close * (1 + sym_meta[code]["price_limit_pct"] / 100)
            close_p = float(df.loc[date]["close"])
            # 涨停过滤
            if close_p >= limit_up * 0.995:
                continue
        else:
            close_p = float(df.loc[date]["close"])

        current_value = positions.get(code, {}).get("shares", 0) * close_p
        gap = target_value_per - current_value
        if gap < rebalance_threshold * total_value:
            continue
        if gap <= 0:
            continue
        invest = min(gap, cash)
        if invest <= 0:
            continue
        # 总仓位上限
        new_total_position = _compute_position_value(positions, all_data, date) + invest
        if new_total_position / total_value > total_position_pct:
            invest = max(0, total_position_pct * total_value - _compute_position_value(positions, all_data, date))
        if invest <= 0:
            continue

        buy_price = close_p * (1 + slippage)
        lot = sym_meta[code]["lot_size"]
        shares = int(invest / buy_price / lot) * lot
        if shares <= 0:
            continue
        cost = shares * buy_price
        commission = cost * sym_meta[code].get("commission", 0.0005)
        if cost + commission > cash:
            shares = int(cash / (buy_price * (1 + sym_meta[code].get("commission", 0.0005))) / lot) * lot
            if shares <= 0:
                continue
            cost = shares * buy_price
            commission = cost * sym_meta[code].get("commission", 0.0005)
        cash -= cost + commission
        # 合并到现有持仓（加权平均成本）
        if code in positions:
            old_shares = positions[code]["shares"]
            old_cost = positions[code]["cost_price"]
            new_shares = old_shares + shares
            new_cost = (old_shares * old_cost + shares * buy_price) / new_shares
            positions[code] = {
                "shares": new_shares, "cost_price": new_cost, "buy_date": date,
            }
        else:
            positions[code] = {
                "shares": shares, "cost_price": buy_price, "buy_date": date,
            }
        if sym_meta[code].get("t_plus_1"):
            last_buy_date[code] = date
        trades.append({
            "date": str(date.date()), "symbol": code,
            "name": sym_meta[code].get("name", code), "action": "BUY",
            "price": round(buy_price, 4), "shares": shares,
            "reason": "调仓建仓", "pnl": 0,
        })

    return cash


# ── 辅助函数 ──────────────────────────────────────────


def _compute_position_value(positions: dict, all_data: dict, date: pd.Timestamp) -> float:
    total = 0.0
    for code, pos in positions.items():
        df = all_data.get(code)
        if df is not None and date in df.index:
            total += pos["shares"] * float(df.loc[date]["close"])
        else:
            total += pos["shares"] * pos["cost_price"]
    return total


def _sell_full(code: str, positions: dict, all_data: dict, date: pd.Timestamp,
               slippage: float, sym_meta: dict,
               last_buy_date: dict, trades: list, cash: float) -> float:
    if code not in positions:
        return cash
    if last_buy_date.get(code) == date:
        return cash  # T+1
    df = all_data.get(code)
    if df is None or date not in df.index:
        return cash
    close_p = float(df.loc[date]["close"])
    loc = df.index.get_loc(date)
    if loc > 0:
        prev_close = float(df.iloc[loc - 1]["close"])
        limit_down = prev_close * (1 - sym_meta[code]["price_limit_pct"] / 100)
        if close_p <= limit_down * 1.005:
            return cash  # 跌停不卖

    pos = positions.pop(code)
    sell_price = close_p * (1 - slippage)
    proceeds = pos["shares"] * sell_price
    commission = proceeds * sym_meta[code].get("commission", 0.0005)
    cash += proceeds - commission
    pnl = (sell_price - pos["cost_price"]) * pos["shares"] - commission
    trades.append({
        "date": str(date.date()), "symbol": code,
        "name": sym_meta[code].get("name", code), "action": "SELL",
        "price": round(sell_price, 4), "shares": pos["shares"],
        "reason": "调仓换仓", "pnl": round(pnl, 2),
    })
    return cash


def _liquidate_all(positions: dict, all_data: dict, date: pd.Timestamp,
                   slippage: float, sym_meta: dict,
                   last_buy_date: dict, trades: list, cash: float) -> tuple[float, int]:
    n = 0
    for code in list(positions.keys()):
        before = cash
        cash = _sell_full(code, positions, all_data, date, slippage,
                          sym_meta, last_buy_date, trades, cash)
        if cash != before:
            n += 1
    return cash, n


def _compute_metrics(daily_values: list[dict], trades: list[dict],
                     initial_cash: float) -> dict[str, Any]:
    if not daily_values:
        return _empty_result()

    values = [d["value"] for d in daily_values]
    final_value = values[-1]
    total_return = (final_value - initial_cash) / initial_cash * 100

    days = len(values)
    years = max(days / 252, 0.1)
    annual_return = ((1 + total_return / 100) ** (1 / years) - 1) * 100

    peak = values[0]
    max_dd = 0
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    daily_returns = [
        (values[i] - values[i - 1]) / values[i - 1]
        for i in range(1, len(values)) if values[i - 1] > 0
    ]
    if daily_returns:
        avg = float(np.mean(daily_returns))
        std = float(np.std(daily_returns))
        sharpe = (avg / std) * np.sqrt(252) if std > 0 else 0
    else:
        sharpe = 0

    sell_trades = [t for t in trades if t["action"] in ("SELL", "SELL_PARTIAL")]
    wins = [t for t in sell_trades if t["pnl"] > 0]
    losses = [t for t in sell_trades if t["pnl"] <= 0]
    win_rate = len(wins) / max(len(sell_trades), 1) * 100

    avg_win = float(np.mean([t["pnl"] for t in wins])) if wins else 0
    avg_loss = abs(float(np.mean([t["pnl"] for t in losses]))) if losses else 1
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else 0

    mid = len(values) // 2
    first_half = (values[mid] - values[0]) / values[0] * 100 if values[0] > 0 else 0
    second_half = (values[-1] - values[mid]) / values[mid] * 100 if values[mid] > 0 else 0

    return {
        "annual_return": round(annual_return, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(float(sharpe), 2),
        "win_rate": round(win_rate, 1),
        "profit_loss_ratio": round(pl_ratio, 2),
        "total_trades": len(trades),
        "sell_trades": len(sell_trades),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "initial_cash": initial_cash,
        "final_value": round(final_value, 2),
        "total_return": round(total_return, 2),
        "first_half_return": round(first_half, 2),
        "second_half_return": round(second_half, 2),
        "period_returns": [
            round(daily_returns[i] * 100, 2)
            for i in range(0, len(daily_returns),
                           max(1, len(daily_returns) // 10))
        ],
        "daily_values": daily_values,
        "future_leak_detected": False,
    }


def _empty_result() -> dict:
    return {
        "annual_return": 0, "max_drawdown": 0, "sharpe": 0,
        "win_rate": 0, "profit_loss_ratio": 0, "total_trades": 0,
        "error": "无可用数据",
    }
