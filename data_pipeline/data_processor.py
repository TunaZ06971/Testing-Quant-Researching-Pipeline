"""
数据处理模块
将 Futu 拉取的原始 CSV 转换为 qlib 期望的格式:
  - 股票代码映射 (US.AAPL -> AAPL)
  - 字段选择与计算 (vwap, change, factor)
  - NaN 处理、日期排序
  - 输出每只股票一个 CSV: date,open,close,high,low,volume,vwap,factor,change
"""

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger(__name__)

QLIB_COLUMNS = ["date", "open", "close", "high", "low", "volume", "vwap", "factor", "change"]


def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _build_rehab_series(rehab_path: Path) -> pd.Series | None:
    """从复权因子 CSV 构建日期索引的 factor 序列"""
    if not rehab_path.exists():
        return None
    try:
        df = pd.read_csv(rehab_path)
        if df.empty:
            return None
        # Futu get_rehab 返回的列: ex_div_date, split_ratio, per_cash_div, ...
        # 需要根据实际列名处理。常见的是 forward_adj_factorA
        # 如果有 forward_adj_factorA 列直接使用
        if "forward_adj_factorA" in df.columns:
            df["ex_div_date"] = pd.to_datetime(df["ex_div_date"])
            return df.set_index("ex_div_date")["forward_adj_factorA"]
    except Exception as e:
        logger.warning("解析复权因子失败 %s: %s", rehab_path, e)
    return None


def _compute_cumulative_factor(rehab_series: pd.Series | None, dates: pd.DatetimeIndex) -> pd.Series:
    """
    从除权除息事件构建每日累计复权因子。
    如果没有复权数据，返回全 1 序列。
    """
    factors = pd.Series(1.0, index=dates)
    if rehab_series is None or rehab_series.empty:
        return factors
    for event_date, adj_factor in rehab_series.items():
        factors.loc[factors.index >= event_date] *= adj_factor
    return factors


def process_single_stock(
    raw_csv_path: Path,
    rehab_csv_path: Path,
    output_path: Path,
) -> dict | None:
    """
    处理单只股票：原始 CSV -> qlib 格式 CSV
    返回 {symbol, start_date, end_date} 或 None
    """
    symbol = raw_csv_path.stem

    try:
        df = pd.read_csv(raw_csv_path)
    except Exception as e:
        logger.error("读取 %s 失败: %s", raw_csv_path, e)
        return None

    if df.empty:
        logger.warning("%s 数据为空，跳过", symbol)
        return None

    df["date"] = pd.to_datetime(df["time_key"]).dt.strftime("%Y-%m-%d")
    df["date_dt"] = pd.to_datetime(df["date"])

    df = df.sort_values("date_dt").drop_duplicates(subset=["date_dt"], keep="last")

    # OHLCV 直接使用前复权价格
    for col in ["open", "close", "high", "low", "volume"]:
        if col not in df.columns:
            logger.error("%s 缺少列 %s", symbol, col)
            return None

    # vwap = turnover / volume, volume=0 时用 close 填充
    if "turnover" in df.columns:
        df["vwap"] = np.where(
            df["volume"] > 0,
            df["turnover"] / df["volume"],
            df["close"],
        )
    else:
        df["vwap"] = df["close"]

    # change = change_rate / 100
    if "change_rate" in df.columns:
        df["change"] = df["change_rate"] / 100.0
    else:
        df["change"] = df["close"].pct_change()

    # factor: 尝试从复权因子文件获取，否则全 1
    rehab_series = _build_rehab_series(rehab_csv_path)
    df["factor"] = _compute_cumulative_factor(
        rehab_series, df["date_dt"]
    ).values

    result = df[["date", "open", "close", "high", "low", "volume", "vwap", "factor", "change"]].copy()

    result = result.dropna(subset=["open", "close", "high", "low"])

    result["volume"] = result["volume"].fillna(0).astype(np.int64)
    result["vwap"] = result["vwap"].fillna(result["close"])
    result["change"] = result["change"].fillna(0.0)
    result["factor"] = result["factor"].fillna(1.0)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False)

    start_date = result["date"].iloc[0]
    end_date = result["date"].iloc[-1]
    logger.info("%s: %d 行, %s ~ %s", symbol, len(result), start_date, end_date)

    return {"symbol": symbol, "start_date": start_date, "end_date": end_date}


def process_all(config_path: str = "config.yaml"):
    """处理所有原始 CSV，输出 qlib 格式"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = load_config(config_path)
    raw_dir = Path(config["data"]["raw_csv_dir"])
    qlib_csv_dir = Path(config["data"]["qlib_csv_dir"])
    qlib_csv_dir.mkdir(parents=True, exist_ok=True)

    csv_files = sorted(raw_dir.glob("*.csv"))
    csv_files = [f for f in csv_files if not f.stem.endswith("_rehab")]

    instruments = []
    processed = 0

    for csv_path in csv_files:
        symbol = csv_path.stem
        rehab_path = raw_dir / f"{symbol}_rehab.csv"
        output_path = qlib_csv_dir / f"{symbol}.csv"

        info = process_single_stock(csv_path, rehab_path, output_path)
        if info:
            instruments.append(info)
            processed += 1

    instruments_path = qlib_csv_dir / "instruments_info.json"
    with open(instruments_path, "w") as f:
        json.dump(instruments, f, indent=2)

    logger.info("数据处理完成: 共 %d 只股票", processed)
    return instruments


if __name__ == "__main__":
    process_all()
