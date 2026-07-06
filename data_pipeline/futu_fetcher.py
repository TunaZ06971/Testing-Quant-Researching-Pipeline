"""
Futu OpenD 数据获取模块
从 Futu 拉取美股历史日K线(前复权)、复权因子、交易日历，
支持分页、限速、断点续传、错误重试。
"""

import json
import logging
import os
import re
import time
from pathlib import Path

import pandas as pd
import yaml
from futu import (
    AuType,
    KLType,
    KL_FIELD,
    Market,
    OpenQuoteContext,
    RET_OK,
    TrdMarket,
)

logger = logging.getLogger(__name__)


def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def parse_stock_pool(pool_path: str) -> list[str]:
    """解析 Pool.md，返回 Futu 格式的股票代码列表 (如 ['US.AAPL', ...])"""
    with open(pool_path, "r") as f:
        content = f.read()
    raw_codes = re.findall(r"[A-Z][A-Z0-9.]+", content)
    codes = []
    for code in raw_codes:
        futu_code = f"US.{code}" if not code.startswith("US.") else code
        codes.append(futu_code)
    return list(dict.fromkeys(codes))


class FutuDataFetcher:
    def __init__(self, config: dict):
        self.cfg = config
        self.host = config["futu"]["host"]
        self.port = config["futu"]["port"]
        self.start_date = config["data"]["start_date"]
        self.end_date = config["data"]["end_date"]
        self.raw_csv_dir = Path(config["data"]["raw_csv_dir"])
        self.delay = config["fetch"]["delay_seconds"]
        self.max_retry = config["fetch"]["max_retry"]
        self.page_size = config["fetch"]["page_size"]
        self.progress_file = self.raw_csv_dir / "fetch_progress.json"
        self.quote_ctx = None

    def connect(self):
        logger.info("连接 Futu OpenD %s:%s", self.host, self.port)
        self.quote_ctx = OpenQuoteContext(host=self.host, port=self.port)

    def close(self):
        if self.quote_ctx:
            self.quote_ctx.close()
            logger.info("已关闭 Futu 连接")

    def _load_progress(self) -> set[str]:
        if self.progress_file.exists():
            with open(self.progress_file, "r") as f:
                return set(json.load(f))
        return set()

    def _save_progress(self, done: set[str]):
        self.progress_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.progress_file, "w") as f:
            json.dump(sorted(done), f, indent=2)

    def check_quota(self):
        ret, data = self.quote_ctx.get_history_kl_quota(get_detail=True)
        if ret == RET_OK:
            logger.info("K线额度: 已用 %s, 剩余 %s", data[0], data[1])
            return data
        logger.warning("查询额度失败: %s", data)
        return None

    def fetch_trading_days(self) -> list[str]:
        """获取美股交易日历"""
        ret, data = self.quote_ctx.request_trading_days(
            market=TrdMarket.US,
            start=self.start_date,
            end=self.end_date,
        )
        if ret == RET_OK:
            trading_days = [str(d) for d in data]
            logger.info("获取到 %d 个交易日", len(trading_days))
            return trading_days
        logger.error("获取交易日历失败: %s", data)
        return []

    def _fetch_kline_with_retry(self, code: str) -> pd.DataFrame | None:
        """带分页和重试的K线拉取"""
        all_data = []
        page_req_key = None

        for attempt in range(self.max_retry):
            try:
                while True:
                    ret, data, page_req_key = self.quote_ctx.request_history_kline(
                        code,
                        start=self.start_date,
                        end=self.end_date,
                        ktype=KLType.K_DAY,
                        autype=AuType.QFQ,
                        fields=[KL_FIELD.ALL],
                        max_count=self.page_size,
                        page_req_key=page_req_key,
                    )
                    if ret != RET_OK:
                        raise RuntimeError(f"API 错误: {data}")
                    all_data.append(data)
                    if page_req_key is None:
                        break
                    time.sleep(0.5)

                if all_data:
                    return pd.concat(all_data, ignore_index=True)
                return None

            except Exception as e:
                logger.warning(
                    "%s 第 %d 次尝试失败: %s", code, attempt + 1, e
                )
                if attempt < self.max_retry - 1:
                    wait = self.delay * (attempt + 1)
                    logger.info("等待 %d 秒后重试...", wait)
                    time.sleep(wait)
                    page_req_key = None
                    all_data = []
                else:
                    logger.error("%s 拉取失败，已耗尽重试次数", code)
                    return None

    def _fetch_rehab(self, code: str) -> pd.DataFrame | None:
        """获取复权因子（不消耗K线额度）"""
        for attempt in range(self.max_retry):
            ret, data = self.quote_ctx.get_rehab(code)
            if ret == RET_OK:
                return data
            logger.warning("%s 复权因子第 %d 次尝试失败: %s", code, attempt + 1, data)
            if attempt < self.max_retry - 1:
                time.sleep(self.delay)
        return None

    def fetch_all(self, stock_codes: list[str]):
        """拉取所有股票数据，支持断点续传"""
        self.raw_csv_dir.mkdir(parents=True, exist_ok=True)
        done = self._load_progress()
        total = len(stock_codes)
        skipped = 0

        trading_days = self.fetch_trading_days()
        if trading_days:
            cal_path = self.raw_csv_dir / "trading_days.json"
            with open(cal_path, "w") as f:
                json.dump(trading_days, f)
            logger.info("交易日历已保存到 %s", cal_path)

        for i, code in enumerate(stock_codes):
            if code in done:
                skipped += 1
                continue

            logger.info("[%d/%d] 拉取 %s (跳过 %d 只已完成)", i + 1, total, code, skipped)

            kline_df = self._fetch_kline_with_retry(code)
            if kline_df is None or kline_df.empty:
                logger.warning("%s 无数据，跳过", code)
                continue

            rehab_df = self._fetch_rehab(code)

            symbol = code.replace("US.", "")
            csv_path = self.raw_csv_dir / f"{symbol}.csv"
            kline_df.to_csv(csv_path, index=False)

            if rehab_df is not None and not rehab_df.empty:
                rehab_path = self.raw_csv_dir / f"{symbol}_rehab.csv"
                rehab_df.to_csv(rehab_path, index=False)

            done.add(code)
            self._save_progress(done)

            if i < total - 1:
                time.sleep(self.delay)

        logger.info("数据拉取完成: 共 %d 只, 本次拉取 %d 只, 跳过 %d 只",
                     total, total - skipped, skipped)


def run_fetch(config_path: str = "config.yaml"):
    """独立运行数据获取"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = load_config(config_path)
    stock_codes = parse_stock_pool(config["data"]["pool_file"])
    logger.info("从股票池解析到 %d 只股票", len(stock_codes))

    fetcher = FutuDataFetcher(config)
    fetcher.connect()
    try:
        fetcher.check_quota()
        fetcher.fetch_all(stock_codes)
    finally:
        fetcher.close()


if __name__ == "__main__":
    run_fetch()
