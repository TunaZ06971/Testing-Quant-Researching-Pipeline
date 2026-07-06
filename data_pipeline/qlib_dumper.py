"""
Qlib 二进制数据转换模块
将处理好的 per-stock CSV 转换为 qlib 二进制格式:
  - calendars/day.txt
  - instruments/all.txt
  - features/{SYMBOL}/*.day.bin

qlib bin 文件格式 (与 qlib FileFeatureStorage 一致):
  - 前 4 字节: start_index 存为 float32 LE，表示该股票在全局日历中的起始偏移
  - 后续字节: (end_index - start_index + 1) x float32 LE 数据值
  - end_index 由文件大小推算: start_index + file_size/4 - 2
"""

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger(__name__)

FEATURE_FIELDS = ["open", "close", "high", "low", "volume", "vwap", "factor", "change"]


def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _build_calendar(qlib_csv_dir: Path, trading_days_path: Path | None) -> list[str]:
    """
    构建全局交易日历。
    优先使用 Futu 拉取的交易日历，否则从所有 CSV 中合并日期。
    """
    if trading_days_path and trading_days_path.exists():
        with open(trading_days_path, "r") as f:
            days = json.load(f)
        logger.info("使用 Futu 交易日历: %d 天", len(days))
        return sorted(days)

    logger.info("从 CSV 数据合并交易日历...")
    all_dates = set()
    for csv_path in qlib_csv_dir.glob("*.csv"):
        if csv_path.stem == "instruments_info":
            continue
        df = pd.read_csv(csv_path, usecols=["date"])
        all_dates.update(df["date"].tolist())
    calendar = sorted(all_dates)
    logger.info("从数据中提取到 %d 个交易日", len(calendar))
    return calendar


def _write_calendar(calendar: list[str], output_dir: Path):
    cal_dir = output_dir / "calendars"
    cal_dir.mkdir(parents=True, exist_ok=True)
    cal_path = cal_dir / "day.txt"
    with open(cal_path, "w") as f:
        for date in calendar:
            f.write(f"{date}\n")
    logger.info("日历已写入 %s (%d 天)", cal_path, len(calendar))


def _write_instruments(instruments: list[dict], output_dir: Path):
    inst_dir = output_dir / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    inst_path = inst_dir / "all.txt"
    with open(inst_path, "w") as f:
        for info in sorted(instruments, key=lambda x: x["symbol"]):
            f.write(f"{info['symbol']}\t{info['start_date']}\t{info['end_date']}\n")
    logger.info("证券列表已写入 %s (%d 只)", inst_path, len(instruments))


def _write_feature_bin(
    values: np.ndarray,
    start_index: int,
    bin_path: Path,
):
    """写入单个 feature 的 bin 文件（与 qlib FileFeatureStorage 格式一致）"""
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    with open(bin_path, "wb") as f:
        np.hstack([start_index, values]).astype("<f").tofile(f)


def _dump_stock_features(
    csv_path: Path,
    calendar: list[str],
    date_to_idx: dict[str, int],
    output_dir: Path,
) -> dict | None:
    """将单只股票的 CSV 转为 bin 文件"""
    symbol = csv_path.stem
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        logger.error("读取 %s 失败: %s", csv_path, e)
        return None

    if df.empty:
        return None

    df["date"] = df["date"].astype(str).str[:10]
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")

    stock_dates = df["date"].tolist()
    valid_dates = [d for d in stock_dates if d in date_to_idx]
    if not valid_dates:
        logger.warning("%s 无有效交易日数据", symbol)
        return None

    start_index = date_to_idx[valid_dates[0]]
    end_index = date_to_idx[valid_dates[-1]]
    n_days = end_index - start_index + 1

    features_dir = output_dir / "features" / symbol

    df_indexed = df.set_index("date")

    for field in FEATURE_FIELDS:
        arr = np.full(n_days, np.nan, dtype=np.float32)
        if field not in df_indexed.columns:
            logger.warning("%s 缺少字段 %s, 填充 NaN", symbol, field)
        else:
            for date in valid_dates:
                idx = date_to_idx[date] - start_index
                arr[idx] = float(df_indexed.loc[date, field])

        bin_path = features_dir / f"{field}.day.bin"
        _write_feature_bin(arr, start_index, bin_path)

    return {
        "symbol": symbol,
        "start_date": valid_dates[0],
        "end_date": valid_dates[-1],
    }


def dump_to_qlib(config_path: str = "config.yaml"):
    """主转换流程: CSV -> qlib bin"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = load_config(config_path)
    qlib_csv_dir = Path(config["data"]["qlib_csv_dir"])
    raw_csv_dir = Path(config["data"]["raw_csv_dir"])
    output_dir = Path(config["data"]["qlib_data_dir"]).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    trading_days_path = raw_csv_dir / "trading_days.json"
    calendar = _build_calendar(qlib_csv_dir, trading_days_path)
    if not calendar:
        logger.error("无法构建交易日历，中止")
        return

    _write_calendar(calendar, output_dir)
    date_to_idx = {d: i for i, d in enumerate(calendar)}

    csv_files = sorted(qlib_csv_dir.glob("*.csv"))
    csv_files = [f for f in csv_files if f.stem != "instruments_info"]

    instruments = []
    for i, csv_path in enumerate(csv_files):
        info = _dump_stock_features(csv_path, calendar, date_to_idx, output_dir)
        if info:
            instruments.append(info)
        if (i + 1) % 50 == 0:
            logger.info("进度: %d/%d", i + 1, len(csv_files))

    _write_instruments(instruments, output_dir)

    logger.info(
        "转换完成: %d 只股票, 日历 %d 天, 输出目录 %s",
        len(instruments),
        len(calendar),
        output_dir,
    )


if __name__ == "__main__":
    dump_to_qlib()
