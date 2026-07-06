# Testing Quant Researching Pipeline

This repository contains a Futu OpenD to Qlib data pipeline for quantitative equity research. The project was built in March 2026 to collect US equity daily bars, normalize them into Qlib-compatible CSV files, and dump them into Qlib's binary data format for factor research and backtesting.

The repository intentionally tracks source code and lightweight configuration only. Downloaded market data, generated Qlib data, PDFs, caches, local IDE settings, and environment files are excluded by `.gitignore`.

## What This Pipeline Does

The pipeline has four stages:

1. Fetch daily US equity data from Futu OpenD.
2. Clean and normalize raw Futu CSV files into Qlib-style per-symbol CSV files.
3. Convert normalized CSV files into Qlib binary feature storage.
4. Verify that Qlib can load the generated dataset.

The main entry point is `run_pipeline.py`, which can execute the full workflow or any individual stage.

## Repository Structure

```text
.
├── config.yaml
├── Pool.md
├── README.md
├── requirements.txt
├── run_pipeline.py
└── data_pipeline/
    ├── __init__.py
    ├── data_processor.py
    ├── futu_fetcher.py
    └── qlib_dumper.py
```

### Important Files

| Path | Purpose |
| --- | --- |
| `run_pipeline.py` | Command-line entry point for `fetch`, `process`, `dump`, `verify`, and `all` modes. |
| `config.yaml` | Runtime configuration for Futu OpenD connection, date range, input/output paths, retry settings, and rate limits. |
| `Pool.md` | US equity universe used by the fetch stage. Symbols are parsed and converted to Futu format such as `US.AAPL`. |
| `data_pipeline/futu_fetcher.py` | Connects to Futu OpenD, fetches daily K-line data, fetches adjustment data, stores progress, and supports retry/resume. |
| `data_pipeline/data_processor.py` | Converts raw Futu CSV files into Qlib-style CSV files with `date`, OHLCV, VWAP, factor, and return fields. |
| `data_pipeline/qlib_dumper.py` | Builds Qlib calendars, instruments, and `.day.bin` feature files. |
| `requirements.txt` | Python dependencies needed by the pipeline. |

Generated files are not committed:

```text
data/
├── raw_csv/       # Raw Futu CSV files and fetch progress
└── qlib_csv/      # Normalized Qlib-style CSV files
```

The final Qlib binary dataset is written to the configured `qlib_data_dir`, which defaults to:

```text
~/.qlib/qlib_data/futu_us/
├── calendars/day.txt
├── instruments/all.txt
└── features/{SYMBOL}/{field}.day.bin
```

## Requirements

- Python 3.10 or newer is recommended.
- Futu OpenD must be running locally or on a reachable host.
- A Futu account with access to the required US equity historical market data.
- Enough Futu historical K-line quota for the stock universe in `Pool.md`.

Install dependencies:

```bash
pip install -r requirements.txt
```

The project imports `qlib` in Python. Depending on your package index, the installable package may be published as `pyqlib` even though the Python module is imported as `qlib`.

## Configuration

Default configuration:

```yaml
futu:
  host: "127.0.0.1"
  port: 11111

data:
  start_date: "2000-01-01"
  end_date: "2026-03-08"
  pool_file: "Pool.md"
  raw_csv_dir: "data/raw_csv"
  qlib_csv_dir: "data/qlib_csv"
  qlib_data_dir: "~/.qlib/qlib_data/futu_us"

fetch:
  delay_seconds: 3
  max_retry: 3
  page_size: 1000
```

The checked-in `config.yaml` contains only local connection settings and paths. It does not contain Futu account credentials, API keys, tokens, or passwords.

## Usage

Run the complete workflow:

```bash
python run_pipeline.py all
```

Run individual stages:

```bash
python run_pipeline.py fetch
python run_pipeline.py process
python run_pipeline.py dump
python run_pipeline.py verify
```

Skip fetching when raw CSV files already exist locally:

```bash
python run_pipeline.py all --skip-fetch
```

Use another configuration file:

```bash
python run_pipeline.py all --config path/to/config.yaml
```

## Workflow Details

### 1. Fetch

`data_pipeline/futu_fetcher.py`:

- Reads symbols from `Pool.md`.
- Converts symbols to Futu format, for example `AAPL` to `US.AAPL`.
- Connects to Futu OpenD with `OpenQuoteContext`.
- Fetches daily adjusted K-line data through `request_history_kline`.
- Fetches adjustment information through `get_rehab`.
- Fetches the US trading calendar through `request_trading_days`.
- Writes raw CSV files under `data/raw_csv/`.
- Stores completed symbols in `data/raw_csv/fetch_progress.json` so interrupted runs can resume.

### 2. Process

`data_pipeline/data_processor.py`:

- Reads raw per-symbol CSV files from `data/raw_csv/`.
- Removes duplicates and sorts by date.
- Produces Qlib-style CSV fields:
  - `date`
  - `open`
  - `close`
  - `high`
  - `low`
  - `volume`
  - `vwap`
  - `factor`
  - `change`
- Calculates VWAP from turnover and volume when turnover is available.
- Converts Futu `change_rate` into decimal returns.
- Uses adjustment data when available and falls back to factor `1.0`.
- Writes normalized files to `data/qlib_csv/`.

### 3. Dump

`data_pipeline/qlib_dumper.py`:

- Builds a global daily trading calendar.
- Writes `calendars/day.txt`.
- Writes `instruments/all.txt`.
- Writes one Qlib `.day.bin` file per symbol and feature.
- Uses the Qlib file feature storage convention where the first float32 value is the start index and the remaining float32 values are feature values.

### 4. Verify

`run_pipeline.py verify`:

- Initializes Qlib with the configured provider URI.
- Loads instruments and sample features.
- Reports data shape, symbol count, NaN ratio, and sample rows.

## Example Qlib Usage

After the dump stage finishes:

```python
import qlib
from qlib.constant import REG_US
from qlib.data import D

qlib.init(provider_uri="~/.qlib/qlib_data/futu_us", region=REG_US)

instruments = D.instruments("all")
fields = ["$open", "$close", "$high", "$low", "$volume", "$vwap"]

df = D.features(
    instruments,
    fields,
    start_time="2020-01-01",
    end_time="2026-03-08",
)

print(df.shape)
print(df.head())
```

## Data and Security Notes

- This repository is configured to exclude `data/`, generated binary files, PDFs, Python caches, virtual environments, `.env` files, and local IDE settings.
- Futu account credentials should not be stored in this repository.
- If you create a private configuration file containing secrets, keep it outside the repository or name it with an `.env` pattern so it is ignored.
- The included `config.yaml` points to local Futu OpenD defaults and does not include sensitive credentials.

## Current Limitations

- The fetch stage is designed for historical daily bars, not intraday or real-time streaming.
- Incremental update logic is limited to fetch resume tracking; the dump stage regenerates output from normalized CSV files.
- The repository contains the data preparation layer only. Factor libraries, model training workflows, portfolio construction, and live trading execution are outside the current scope.
