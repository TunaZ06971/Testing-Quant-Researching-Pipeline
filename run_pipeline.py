"""
Futu -> Qlib 数据管线主入口
串联: 数据获取 -> 数据处理 -> qlib 格式转换 -> 数据验证
"""

import argparse
import logging
import sys
from pathlib import Path

from data_pipeline.futu_fetcher import FutuDataFetcher, load_config, parse_stock_pool
from data_pipeline.data_processor import process_all
from data_pipeline.qlib_dumper import dump_to_qlib

logger = logging.getLogger(__name__)


def step_fetch(config_path: str):
    """Step 1: 从 Futu 拉取原始数据"""
    logger.info("=" * 60)
    logger.info("Step 1: 从 Futu OpenD 拉取历史数据")
    logger.info("=" * 60)

    config = load_config(config_path)
    stock_codes = parse_stock_pool(config["data"]["pool_file"])
    logger.info("股票池: %d 只股票", len(stock_codes))

    fetcher = FutuDataFetcher(config)
    fetcher.connect()
    try:
        quota = fetcher.check_quota()
        if quota:
            logger.info("当前K线额度状态已打印")
        fetcher.fetch_all(stock_codes)
    finally:
        fetcher.close()


def step_process(config_path: str):
    """Step 2: 数据清洗与字段转换"""
    logger.info("=" * 60)
    logger.info("Step 2: 数据清洗与字段转换")
    logger.info("=" * 60)
    instruments = process_all(config_path)
    logger.info("处理完成: %d 只股票", len(instruments))


def step_dump(config_path: str):
    """Step 3: 转换为 qlib 二进制格式"""
    logger.info("=" * 60)
    logger.info("Step 3: 转换为 qlib 二进制格式")
    logger.info("=" * 60)
    dump_to_qlib(config_path)


def step_verify(config_path: str):
    """Step 4: 验证 qlib 数据加载"""
    logger.info("=" * 60)
    logger.info("Step 4: 验证 qlib 数据")
    logger.info("=" * 60)

    try:
        import qlib
        from qlib.constant import REG_US
        from qlib.data import D
    except ImportError:
        logger.error("qlib 未安装，请先执行: pip install qlib")
        return False

    config = load_config(config_path)
    qlib_data_dir = str(Path(config["data"]["qlib_data_dir"]).expanduser())

    qlib.init(provider_uri=qlib_data_dir, region=REG_US)
    instruments = D.instruments("all")
    fields = ["$open", "$close", "$high", "$low", "$volume", "$vwap"]

    df = D.features(instruments, fields, start_time="2020-01-01", end_time="2026-03-08")
    logger.info("数据形状: %s", df.shape)
    logger.info("股票数量: %d", df.index.get_level_values(0).nunique())

    n_nan = df.isna().sum().sum()
    n_total = df.size
    nan_ratio = n_nan / n_total if n_total > 0 else 0
    logger.info("NaN 比例: %.2f%% (%d / %d)", nan_ratio * 100, n_nan, n_total)

    logger.info("前 5 行数据:")
    logger.info("\n%s", df.head().to_string())

    if df.empty:
        logger.error("验证失败: 数据为空!")
        return False

    logger.info("验证通过!")
    return True


STEPS = {
    "fetch": step_fetch,
    "process": step_process,
    "dump": step_dump,
    "verify": step_verify,
    "all": None,
}


def main():
    parser = argparse.ArgumentParser(description="Futu -> Qlib 数据管线")
    parser.add_argument(
        "step",
        choices=STEPS.keys(),
        default="all",
        nargs="?",
        help="执行指定步骤: fetch/process/dump/verify/all (默认: all)",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="配置文件路径 (默认: config.yaml)",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="跳过数据获取步骤 (适用于已有原始数据的情况)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.step == "all":
        if not args.skip_fetch:
            step_fetch(args.config)
        else:
            logger.info("跳过 Step 1 (数据获取)")
        step_process(args.config)
        step_dump(args.config)
        step_verify(args.config)
    else:
        STEPS[args.step](args.config)


if __name__ == "__main__":
    main()
