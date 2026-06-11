# 📊 Stock Momentum Screener

A CLI tool that scans a list of stock tickers, calculates momentum metrics for the previous trading day, and generates an interactive HTML report.

Built with Python · Uses Yahoo Finance API · Docker-ready

---

## Features

- **Batch downloads** — fetches up to 50 tickers per request via `yfinance`
- **Smart caching** — 24-hour JSON cache to avoid redundant API calls
- **Retry logic** — 3 automatic retries on network errors
- **Momentum metrics** — % change, gap %, intraday range %
- **Company enrichment** — name, sector, market cap per ticker
- **Interactive HTML report** — sortable table, sector filter, CSV export (DataTables + Bootstrap 5)
- **CSV + history** — daily CSV snapshots and cumulative history log
- **Docker support** — runs in a container with no local setup

---

## Tech Stack

| Tool | Purpose |
|------|---------|
| Python 3.11+ | Core language |
| yfinance | Market data (Yahoo Finance) |
| pandas | Data processing |
| Bootstrap 5 + DataTables | HTML report UI |
| Docker | Containerized execution |

---

## Installation

```bash
git clone https://github.com/vitalii-malcov/stock-momentum-screener.git
cd stock-momentum-screener

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
```

---

## Usage

### Basic run (default: min 5% change, yesterday's data)
```bash
python app.py --file tickers.csv
```

### Custom thresholds
```bash
python app.py --min-change 3 --min-volume 1000000 --min-close 10
```

### With cache (recommended for repeated runs)
```bash
python app.py --min-change 5 --file tickers.csv --use-cache
```

### Skip company info enrichment (faster)
```bash
python app.py --no-info --use-cache
```

### Analyse 2 trading days ago
```bash
python app.py --days 2
```

---

## CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--file` | `tickers.csv` | Path to CSV file with tickers |
| `--min-change` | `5.0` | Minimum % price change |
| `--min-volume` | `500000` | Minimum trading volume |
| `--min-close` | `5.0` | Minimum closing price (USD) |
| `--days` | `1` | Trading days offset from today |
| `--use-cache` | off | Enable 24h JSON cache |
| `--no-info` | off | Skip company info (faster) |
| `--output` | `report.html` | Output HTML filename |

---

## Output

After a successful run:

```
report.html       — interactive report (open in browser)
report.csv        — filtered results as CSV
history/          — daily snapshots + all_history.csv
logs/             — errors.log, processed.log
```

**Report preview:**

```
Ticker  Company          Sector       Close    Volume   % Change  Gap %
──────────────────────────────────────────────────────────────────────
NVDA    NVIDIA Corp      Technology   $875.40  42.1M    +12.3%    +3.1%
AMD     Advanced Micro   Technology   $162.80  18.7M    +8.7%     +1.2%
META    Meta Platforms   Technology   $492.10  12.3M    +6.1%     +0.8%
```

---

## Docker

```bash
# Build
docker build -t screener .

# Run with defaults
docker run screener

# Run with custom settings
docker run screener --min-change 3 --use-cache
```

---

## tickers.csv format

```
ticker
AAPL
MSFT
GOOGL
```

The file ships with 3000+ US stock tickers. You can replace it with any list.

---

## Project Structure

```
stock-momentum-screener/
├── app.py            # Main application
├── requirements.txt  # Python dependencies
├── Dockerfile        # Container configuration
├── tickers.csv       # Default ticker list (3100+ tickers)
└── README.md
```

---

## License

MIT — free to use and modify.