#!/usr/bin/env python3
"""
Stock Momentum Screener
=======================
Находит акции с сильным движением за предыдущий торговый день.
Генерирует HTML-отчёт с интерактивной таблицей, фильтрами и графиками.

Запуск:
    python app.py --min-change 5 --file tickers.csv
"""

import sys
import csv
import html
import json
import time
import logging
import argparse
import hashlib
import unittest
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

# ─────────────────────────────────────────────
# CONSTANTS & PATHS
# ─────────────────────────────────────────────

LOG_DIR = Path("logs")
CACHE_DIR = Path("cache")
HISTORY_DIR = Path("history")
processed_log = LOG_DIR / "processed.log"

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
RETRY_COUNT = 3
RETRY_DELAY = 5
CACHE_TTL_HOURS = 24


def _init_dirs() -> None:
    """Creates runtime directories and configures logging. Called once from main()."""
    LOG_DIR.mkdir(exist_ok=True)
    CACHE_DIR.mkdir(exist_ok=True)
    HISTORY_DIR.mkdir(exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "errors.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ─────────────────────────────────────────────
# 1. ЗАГРУЗКА ТИКЕРОВ
# ─────────────────────────────────────────────

def load_tickers(filepath: str) -> list[str]:
    """
    Читает CSV-файл с тикерами.
    Ожидает одну колонку 'ticker' (или первую колонку без заголовка).

    Args:
        filepath: путь к CSV-файлу

    Returns:
        Список уникальных тикеров в верхнем регистре
    """
    tickers = []
    try:
        with open(filepath, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)

            # Если первая строка — заголовок
            if header and header[0].strip().lower() in ("ticker", "symbol", "тикер"):
                rows = reader
            else:
                # Первая строка — данные
                if header:
                    tickers.append(header[0].strip().upper())
                rows = reader

            for row in rows:
                if row and row[0].strip():
                    tickers.append(row[0].strip().upper())

    except FileNotFoundError:
        raise FileNotFoundError(f"Ticker file not found: {filepath}")
    except Exception as e:
        raise RuntimeError(f"Error reading ticker file: {e}") from e

    # Убираем дубликаты, сохраняем порядок
    seen = set()
    unique = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    logger.info(f"Загружено {len(unique)} уникальных тикеров из {filepath}")
    return unique


# ─────────────────────────────────────────────
# 2. ОПРЕДЕЛЕНИЕ ТОРГОВОГО ДНЯ
# ─────────────────────────────────────────────

def get_previous_trading_day(offset: int = 1) -> date:
    """
    Возвращает дату предыдущего торгового дня (пропуская выходные).

    Args:
        offset: сколько торговых дней назад (1 = вчера)

    Returns:
        Дата предыдущего торгового дня
    """
    today = datetime.now().date()
    count = 0
    current = today - timedelta(days=1)

    while count < offset:
        # Пропускаем субботу (5) и воскресенье (6)
        if current.weekday() < 5:
            count += 1
        if count < offset:
            current -= timedelta(days=1)

    return current


# ─────────────────────────────────────────────
# 3. КЭШИРОВАНИЕ
# ─────────────────────────────────────────────

def get_cache_key(tickers: list[str], trade_date: date) -> str:
    """Генерирует уникальный ключ кэша на основе тикеров и даты."""
    content = f"{trade_date}|{'|'.join(sorted(tickers))}"
    return hashlib.md5(content.encode()).hexdigest()


def load_from_cache(cache_key: str) -> Optional[pd.DataFrame]:
    """
    Загружает данные из кэша, если они не устарели.

    Returns:
        DataFrame или None, если кэш устарел / не существует
    """
    cache_file = CACHE_DIR / f"{cache_key}.json"
    if not cache_file.exists():
        return None

    try:
        with open(cache_file, "r") as f:
            cached = json.load(f)

        cached_at = datetime.fromisoformat(cached["cached_at"])
        age_hours = (datetime.now() - cached_at).total_seconds() / 3600

        if age_hours > CACHE_TTL_HOURS:
            logger.info("Кэш устарел, будем загружать данные заново")
            return None

        df = pd.DataFrame(cached["data"])
        logger.info(f"Загружено из кэша ({age_hours:.1f} ч. назад)")
        return df

    except Exception as e:
        logger.warning(f"Ошибка чтения кэша: {e}")
        return None


def save_to_cache(cache_key: str, df: pd.DataFrame) -> None:
    """Сохраняет DataFrame в кэш."""
    cache_file = CACHE_DIR / f"{cache_key}.json"
    try:
        data = {
            "cached_at": datetime.now().isoformat(),
            "data": df.to_dict(orient="records"),
        }
        with open(cache_file, "w") as f:
            json.dump(data, f, default=str)
        logger.info(f"Данные сохранены в кэш: {cache_file}")
    except Exception as e:
        logger.warning(f"Ошибка сохранения кэша: {e}")


# ─────────────────────────────────────────────
# 4. ЗАГРУЗКА РЫНОЧНЫХ ДАННЫХ
# ─────────────────────────────────────────────

def fetch_batch(tickers: list[str], start_date: date, end_date: date) -> pd.DataFrame:
    """
    Загружает OHLCV-данные для группы тикеров с повторными попытками.

    Args:
        tickers:    список тикеров
        start_date: начало периода
        end_date:   конец периода (не включается)

    Returns:
        DataFrame с мультиуровневыми колонками
    """
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            df = yf.download(
                tickers,
                start=start_date.isoformat(),
                end=end_date.isoformat(),
                auto_adjust=True,
                progress=False,
                threads=True,
                timeout=30,
            )
            return df
        except Exception as e:
            logger.warning(f"Попытка {attempt}/{RETRY_COUNT} не удалась: {e}")
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY)

    logger.error(f"Не удалось загрузить данные для тикеров: {tickers[:5]}...")
    return pd.DataFrame()


def fetch_company_info(ticker: str) -> dict:
    """
    Получает дополнительную информацию о компании (название, сектор, капитализация).
    Возвращает пустой словарь при ошибке.
    """
    try:
        info = yf.Ticker(ticker).info
        return {
            "name": info.get("longName") or info.get("shortName") or ticker,
            "sector": info.get("sector") or "N/A",
            "market_cap": info.get("marketCap") or 0,
        }
    except Exception:
        return {"name": ticker, "sector": "N/A", "market_cap": 0}


def fetch_data(
    tickers: list[str],
    trade_date: date,
    use_cache: bool = True,
    days_history: int = 1,
) -> pd.DataFrame:
    """
    Основная функция загрузки данных.
    Скачивает OHLCV за нужный период, поддерживает кэш и batch-режим.

    Args:
        tickers:      список тикеров
        trade_date:   целевая дата торгов
        use_cache:    использовать ли кэш
        days_history: кол-во дней для загрузки истории

    Returns:
        DataFrame с колонками: ticker, open, high, low, close, volume, prev_close
    """
    cache_key = get_cache_key(tickers, trade_date)

    if use_cache:
        cached_df = load_from_cache(cache_key)
        if cached_df is not None:
            return cached_df

    # Начало загрузки: нужен предыдущий день для расчёта гэпа
    start = trade_date - timedelta(days=days_history + 5)  # +5 для запаса (выходные)
    end = trade_date + timedelta(days=1)

    all_records = []
    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE

    logger.info(f"Загрузка данных: {len(tickers)} тикеров, {total_batches} батчей...")

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i: i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        logger.info(f"  Батч {batch_num}/{total_batches}: {len(batch)} тикеров")

        raw = fetch_batch(batch, start, end)

        if raw.empty:
            logger.warning(f"  Батч {batch_num}: нет данных")
            continue

        # Если один тикер — yfinance возвращает плоские колонки
        if len(batch) == 1:
            ticker = batch[0]
            try:
                if trade_date in raw.index.date or any(d == trade_date for d in raw.index.date):
                    day_data = raw[raw.index.date == trade_date]
                    prev_data = raw[raw.index.date < trade_date]

                    if day_data.empty or prev_data.empty:
                        continue

                    prev_close = float(prev_data["Close"].iloc[-1])
                    row = day_data.iloc[0]

                    all_records.append({
                        "ticker": ticker,
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": int(row["Volume"]),
                        "prev_close": prev_close,
                    })
            except Exception as e:
                logger.warning(f"  Ошибка обработки {ticker}: {e}")
            continue

        # Несколько тикеров — мультиуровневые колонки
        for ticker in batch:
            try:
                if ticker not in raw.columns.get_level_values(1):
                    continue

                t_df = raw.xs(ticker, axis=1, level=1)
                t_df = t_df.dropna(subset=["Close"])

                day_data = t_df[t_df.index.date == trade_date]
                prev_data = t_df[t_df.index.date < trade_date]

                if day_data.empty or prev_data.empty:
                    continue

                prev_close = float(prev_data["Close"].iloc[-1])
                row = day_data.iloc[0]

                all_records.append({
                    "ticker": ticker,
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]),
                    "prev_close": prev_close,
                })

            except Exception as e:
                logger.warning(f"  Ошибка обработки {ticker}: {e}")

    if not all_records:
        logger.warning("Не удалось получить данные ни для одного тикера.")
        return pd.DataFrame()

    df = pd.DataFrame(all_records)
    logger.info(f"Загружено записей: {len(df)}")

    # Сохраняем в кэш
    if use_cache:
        save_to_cache(cache_key, df)

    return df


# ─────────────────────────────────────────────
# 5. РАСЧЁТ МЕТРИК
# ─────────────────────────────────────────────

def calculate_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """
    Добавляет рассчитанные колонки в DataFrame.

    Метрики:
    - percent_change:  движение за день (Close vs Open)
    - gap_percent:     гэп на открытии (Open vs Prev_Close)
    - intraday_range:  размах внутри дня (High vs Low)
    """
    if df.empty:
        return df

    df = df.copy()

    # Защита от деления на ноль
    df["percent_change"] = ((df["close"] - df["open"]) / df["open"].replace(0, float("nan"))) * 100
    df["gap_percent"] = ((df["open"] - df["prev_close"]) / df["prev_close"].replace(0, float("nan"))) * 100
    df["intraday_range"] = ((df["high"] - df["low"]) / df["low"].replace(0, float("nan"))) * 100

    # Округляем до 2 знаков
    for col in ["percent_change", "gap_percent", "intraday_range"]:
        df[col] = df[col].round(2)

    return df


# ─────────────────────────────────────────────
# 6. ЗАГРУЗКА ИНФОРМАЦИИ О КОМПАНИЯХ
# ─────────────────────────────────────────────

def enrich_with_company_info(df: pd.DataFrame) -> pd.DataFrame:
    """
    Добавляет колонки: name, sector, market_cap для каждого тикера.
    Загружает информацию по одному тикеру с логированием прогресса.
    """
    if df.empty:
        return df

    names, sectors, caps = [], [], []
    total = len(df)

    for i, ticker in enumerate(df["ticker"], 1):
        if i % 20 == 0 or i == total:
            logger.info(f"  Инфо о компаниях: {i}/{total}")
        info = fetch_company_info(ticker)
        names.append(info["name"])
        sectors.append(info["sector"])
        caps.append(info["market_cap"])

    df = df.copy()
    df["name"] = names
    df["sector"] = sectors
    df["market_cap"] = caps

    return df


# ─────────────────────────────────────────────
# 7. ФИЛЬТРАЦИЯ
# ─────────────────────────────────────────────

def filter_data(
    df: pd.DataFrame,
    min_change: float = 5.0,
    min_volume: int = 500_000,
    min_close: float = 5.0,
) -> pd.DataFrame:
    """
    Оставляет только акции, удовлетворяющие условиям.

    Args:
        df:         исходный DataFrame
        min_change: минимальное изменение цены в %
        min_volume: минимальный объём торгов
        min_close:  минимальная цена закрытия в USD

    Returns:
        Отфильтрованный DataFrame
    """
    if df.empty:
        return df

    initial = len(df)

    # Убираем строки с пустыми данными
    df = df.dropna(subset=["percent_change", "close", "volume"])

    mask = (
        (df["percent_change"] >= min_change) &
        (df["volume"] >= min_volume) &
        (df["close"] >= min_close)
    )

    filtered = df[mask].copy()
    logger.info(
        f"Фильтрация: {initial} → {len(filtered)} акций "
        f"(change≥{min_change}%, vol≥{min_volume:,}, close≥${min_close})"
    )
    return filtered


# ─────────────────────────────────────────────
# 8. ИСТОРИЯ
# ─────────────────────────────────────────────

def save_history(df: pd.DataFrame, trade_date: date) -> None:
    """
    Сохраняет результаты скрининга в историю (CSV по дням).
    """
    if df.empty:
        return

    history_file = HISTORY_DIR / f"{trade_date}.csv"
    df.to_csv(history_file, index=False)
    logger.info(f"История сохранена: {history_file}")

    # Обновляем сводный файл
    master_file = HISTORY_DIR / "all_history.csv"
    df_with_date = df.copy()
    df_with_date.insert(0, "trade_date", trade_date)

    if master_file.exists():
        existing = pd.read_csv(master_file)
        # Удаляем старые записи за эту дату
        existing = existing[existing["trade_date"] != str(trade_date)]
        combined = pd.concat([existing, df_with_date], ignore_index=True)
    else:
        combined = df_with_date

    combined.to_csv(master_file, index=False)


# ─────────────────────────────────────────────
# 9. ЭКСПОРТ CSV
# ─────────────────────────────────────────────

def save_csv(df: pd.DataFrame, output_path: str = "report.csv") -> None:
    """Сохраняет итоговые данные в CSV."""
    if df.empty:
        logger.warning("Нет данных для экспорта в CSV")
        return
    df.to_csv(output_path, index=False)
    logger.info(f"CSV-экспорт сохранён: {output_path}")


# ─────────────────────────────────────────────
# 10. ФОРМАТИРОВАНИЕ
# ─────────────────────────────────────────────

def format_market_cap(cap: float) -> str:
    """Преобразует число в читаемый формат: 1.23B, 456.7M, и т.д."""
    if not cap or cap == 0:
        return "N/A"
    if cap >= 1_000_000_000_000:
        return f"${cap / 1_000_000_000_000:.2f}T"
    if cap >= 1_000_000_000:
        return f"${cap / 1_000_000_000:.2f}B"
    if cap >= 1_000_000:
        return f"${cap / 1_000_000:.1f}M"
    return f"${cap:,.0f}"


def format_volume(vol: int) -> str:
    """Форматирует объём с разделителями тысяч."""
    if vol >= 1_000_000:
        return f"{vol / 1_000_000:.1f}M"
    if vol >= 1_000:
        return f"{vol / 1_000:.0f}K"
    return str(vol)


# ─────────────────────────────────────────────
# 11. ГЕНЕРАЦИЯ HTML
# ─────────────────────────────────────────────

def generate_html(
    df: pd.DataFrame,
    trade_date: date,
    output_path: str = "report.html",
    min_change: float = 5.0,
) -> None:
    """
    Генерирует HTML-отчёт с Bootstrap-дизайном, сортировкой, поиском и пагинацией.

    Args:
        df:          отфильтрованный DataFrame
        trade_date:  дата торгов
        output_path: путь для сохранения HTML
        min_change:  пороговое значение для заголовка
    """
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Строки таблицы ──
    rows_html = ""

    for _, row in df.iterrows():
        pct = row.get("percent_change", 0)
        gap = row.get("gap_percent", 0)
        rng = row.get("intraday_range", 0)
        ticker = row["ticker"]
        ticker_e = html.escape(str(ticker))
        name_e   = html.escape(str(row.get("name", ticker)))
        sector_e = html.escape(str(row.get("sector", "N/A")))

        # Цвет строки по % изменению
        if pct >= 10:
            row_class = "table-success"
        elif pct >= 5:
            row_class = "table-light-green"
        elif pct < 0:
            row_class = "row-negative"
        else:
            row_class = ""

        # Знак для Gap
        gap_sign = "+" if gap >= 0 else ""
        pct_sign = "+" if pct >= 0 else ""

        yahoo_url = f"https://finance.yahoo.com/quote/{ticker}"
        chart_url = f"https://finance.yahoo.com/chart/{ticker}"

        cap_str = format_market_cap(row.get("market_cap", 0))
        vol_str = format_volume(int(row.get("volume", 0)))

        rows_html += f"""
        <tr class="{row_class}"
            data-ticker="{ticker_e}"
            data-sector="{sector_e}"
            data-cap="{row.get('market_cap', 0)}"
            data-volume="{row.get('volume', 0)}">
            <td>
                <a href="{yahoo_url}" target="_blank" class="ticker-link fw-bold">{ticker_e}</a>
            </td>
            <td class="company-name">{name_e}</td>
            <td><span class="badge sector-badge">{sector_e}</span></td>
            <td>${row.get('open', 0):.2f}</td>
            <td>${row.get('high', 0):.2f}</td>
            <td>${row.get('low', 0):.2f}</td>
            <td class="fw-semibold">${row.get('close', 0):.2f}</td>
            <td>{vol_str}</td>
            <td class="{'positive-cell' if pct >= 0 else 'negative-cell'} fw-bold">
                {pct_sign}{pct:.2f}%
            </td>
            <td class="{'positive-cell' if gap >= 0 else 'negative-cell'}">
                {gap_sign}{gap:.2f}%
            </td>
            <td>{rng:.2f}%</td>
            <td>{cap_str}</td>
            <td>
                <a href="{chart_url}" target="_blank" class="btn btn-sm btn-outline-primary">
                    📈 Chart
                </a>
            </td>
        </tr>
        """

    # ── Список секторов для фильтра ──
    sectors = sorted(df["sector"].dropna().unique().tolist()) if not df.empty else []
    sector_options = '<option value="">All Sectors</option>\n'
    for s in sectors:
        sector_options += f'<option value="{s}">{s}</option>\n'

    # ── Полный HTML ──
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Stock Momentum Report — {trade_date}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.datatables.net/1.13.7/css/dataTables.bootstrap5.min.css" rel="stylesheet">
    <style>
        body {{
            background: #0d1117;
            color: #c9d1d9;
            font-family: 'Segoe UI', system-ui, sans-serif;
            font-size: 0.88rem;
        }}
        .navbar-brand {{
            font-size: 1.4rem;
            font-weight: 700;
            letter-spacing: -0.5px;
        }}
        .header-card {{
            background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
            border: 1px solid #30363d;
            border-radius: 12px;
        }}
        .stat-box {{
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 16px 24px;
            text-align: center;
        }}
        .stat-box .value {{
            font-size: 2rem;
            font-weight: 700;
            color: #58a6ff;
        }}
        .stat-box .label {{
            font-size: 0.75rem;
            color: #8b949e;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .main-table-wrapper {{
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 12px;
            overflow: hidden;
        }}
        table.dataTable {{
            border-collapse: collapse !important;
            color: #c9d1d9 !important;
        }}
        table.dataTable thead th {{
            background: #21262d !important;
            border-bottom: 2px solid #30363d !important;
            color: #8b949e !important;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            cursor: pointer;
            white-space: nowrap;
        }}
        table.dataTable thead th:hover {{
            background: #30363d !important;
            color: #c9d1d9 !important;
        }}
        table.dataTable tbody tr {{
            border-bottom: 1px solid #21262d;
            transition: background 0.15s;
        }}
        table.dataTable tbody tr:hover {{
            background: #1f2937 !important;
        }}
        table.dataTable tbody td {{
            padding: 10px 12px !important;
            vertical-align: middle;
            border: none !important;
        }}
        /* Перекрываем стили Bootstrap для зелёных/красных строк */
        .table-success td {{ background-color: #0d2818 !important; }}
        .table-light-green td {{ background-color: #0a1f12 !important; }}
        .row-negative td {{ background-color: #1e0a0a !important; }}
        .positive-cell {{ color: #3fb950 !important; }}
        .negative-cell {{ color: #f85149 !important; }}
        .ticker-link {{
            color: #58a6ff;
            text-decoration: none;
            font-size: 0.9rem;
        }}
        .ticker-link:hover {{ text-decoration: underline; }}
        .company-name {{
            max-width: 180px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            color: #8b949e;
        }}
        .sector-badge {{
            background: #21262d;
            color: #8b949e;
            font-size: 0.7rem;
            border: 1px solid #30363d;
            border-radius: 4px;
        }}
        /* DataTables кастомизация */
        .dataTables_wrapper .dataTables_length label,
        .dataTables_wrapper .dataTables_filter label,
        .dataTables_wrapper .dataTables_info,
        .dataTables_wrapper .dataTables_paginate .paginate_button {{
            color: #8b949e !important;
        }}
        .dataTables_wrapper .dataTables_filter input {{
            background: #21262d;
            border: 1px solid #30363d;
            color: #c9d1d9;
            border-radius: 6px;
            padding: 4px 10px;
        }}
        .dataTables_wrapper .dataTables_length select {{
            background: #21262d;
            border: 1px solid #30363d;
            color: #c9d1d9;
            border-radius: 4px;
        }}
        .dataTables_wrapper .paginate_button {{
            background: #21262d !important;
            border: 1px solid #30363d !important;
            color: #8b949e !important;
            border-radius: 4px !important;
            margin: 0 2px;
        }}
        .dataTables_wrapper .paginate_button.current,
        .dataTables_wrapper .paginate_button:hover {{
            background: #58a6ff !important;
            color: #000 !important;
            border-color: #58a6ff !important;
        }}
        /* Фильтры */
        .filters-bar {{
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 10px;
            padding: 16px 20px;
        }}
        .filters-bar select,
        .filters-bar input {{
            background: #21262d;
            border: 1px solid #30363d;
            color: #c9d1d9;
            border-radius: 6px;
            padding: 6px 12px;
            font-size: 0.82rem;
        }}
        .filters-bar label {{
            font-size: 0.75rem;
            color: #8b949e;
            text-transform: uppercase;
            letter-spacing: 0.4px;
        }}
        .btn-export {{
            background: #238636;
            border: 1px solid #2ea043;
            color: #fff;
            border-radius: 6px;
            padding: 6px 16px;
            font-size: 0.82rem;
            cursor: pointer;
        }}
        .btn-export:hover {{ background: #2ea043; }}
        .legend-item {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 0.78rem;
            color: #8b949e;
        }}
        .legend-dot {{
            width: 12px; height: 12px;
            border-radius: 3px;
        }}
        footer {{
            color: #8b949e;
            font-size: 0.75rem;
        }}
        @media (max-width: 768px) {{
            .company-name {{ max-width: 100px; }}
            .stat-box .value {{ font-size: 1.4rem; }}
        }}
    </style>
</head>
<body>

<!-- NAVBAR -->
<nav class="navbar navbar-dark" style="background:#161b22; border-bottom:1px solid #30363d;">
    <div class="container-fluid px-4">
        <span class="navbar-brand">
            📊 Stock Momentum Screener
        </span>
        <span class="text-muted small">
            Trading date: <strong class="text-light">{trade_date}</strong>
            &nbsp;|&nbsp;Generated: {generated_at}
        </span>
    </div>
</nav>

<div class="container-fluid px-4 py-4">

    <!-- STATS -->
    <div class="row g-3 mb-4">
        <div class="col-6 col-md-3">
            <div class="stat-box">
                <div class="value">{len(df)}</div>
                <div class="label">Stocks Found</div>
            </div>
        </div>
        <div class="col-6 col-md-3">
            <div class="stat-box">
                <div class="value">{df['percent_change'].max():.1f}%</div>
                <div class="label">Max Change</div>
            </div>
        </div>
        <div class="col-6 col-md-3">
            <div class="stat-box">
                <div class="value">{df['percent_change'].mean():.1f}%</div>
                <div class="label">Avg Change</div>
            </div>
        </div>
        <div class="col-6 col-md-3">
            <div class="stat-box">
                <div class="value">{min_change}%</div>
                <div class="label">Min Threshold</div>
            </div>
        </div>
    </div>

    <!-- ФИЛЬТРЫ -->
    <div class="filters-bar mb-3">
        <div class="row g-3 align-items-end">
            <div class="col-12 col-md-3">
                <label class="mb-1">Sector</label>
                <select id="sectorFilter" class="form-select form-select-sm w-100">
                    {sector_options}
                </select>
            </div>
            <div class="col-6 col-md-2">
                <label class="mb-1">Min Volume</label>
                <input type="number" id="volFilter" class="form-control form-control-sm"
                       placeholder="e.g. 1000000" value="">
            </div>
            <div class="col-6 col-md-2">
                <label class="mb-1">Min Market Cap ($)</label>
                <input type="number" id="capFilter" class="form-control form-control-sm"
                       placeholder="e.g. 500000000" value="">
            </div>
            <div class="col-6 col-md-2">
                <button class="btn btn-sm btn-primary w-100" onclick="applyFilters()">Apply Filters</button>
            </div>
            <div class="col-6 col-md-2">
                <button class="btn btn-sm btn-secondary w-100" onclick="resetFilters()">Reset</button>
            </div>
            <div class="col-12 col-md-1 text-end">
                <button class="btn-export w-100" onclick="exportCSV()">⬇ CSV</button>
            </div>
        </div>
    </div>

    <!-- ЛЕГЕНДА -->
    <div class="d-flex gap-4 mb-3 ms-1">
        <span class="legend-item">
            <span class="legend-dot" style="background:#0d2818;border:1px solid #2ea043;"></span>
            Change ≥ 10%
        </span>
        <span class="legend-item">
            <span class="legend-dot" style="background:#0a1f12;border:1px solid #388e3c;"></span>
            Change 5–10%
        </span>
        <span class="legend-item">
            <span class="legend-dot" style="background:#1e0a0a;border:1px solid #f85149;"></span>
            Negative
        </span>
    </div>

    <!-- ТАБЛИЦА -->
    <div class="main-table-wrapper mb-4">
        <div class="p-3">
            <table id="stockTable" class="table table-sm w-100">
                <thead>
                    <tr>
                        <th>Ticker</th>
                        <th>Company</th>
                        <th>Sector</th>
                        <th>Open</th>
                        <th>High</th>
                        <th>Low</th>
                        <th>Close</th>
                        <th>Volume</th>
                        <th>% Change</th>
                        <th>Gap %</th>
                        <th>Range %</th>
                        <th>Market Cap</th>
                        <th>Chart</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>
    </div>

    <!-- ФУТЕР -->
    <footer class="text-center py-3">
        Stock Momentum Screener &nbsp;|&nbsp; Data via Yahoo Finance &nbsp;|&nbsp;
        Generated {generated_at} &nbsp;|&nbsp;
        <em>For informational purposes only. Not financial advice.</em>
    </footer>

</div>

<!-- SCRIPTS -->
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.7/js/jquery.dataTables.min.js"></script>
<script src="https://cdn.datatables.net/1.13.7/js/dataTables.bootstrap5.min.js"></script>
<script>
$(document).ready(function() {{
    window.table = $('#stockTable').DataTable({{
        pageLength: 25,
        order: [[8, 'desc']],   // Сортировка по % Change
        columnDefs: [
            {{ orderable: false, targets: [12] }}  // Колонка Chart не сортируется
        ],
        language: {{
            search: "🔍 Search:",
            lengthMenu: "Show _MENU_ rows",
            info: "Showing _START_–_END_ of _TOTAL_ stocks",
            paginate: {{ previous: "‹", next: "›" }}
        }}
    }});
}});

// Применить фильтры по сектору, объёму и капитализации
function applyFilters() {{
    var sector = document.getElementById('sectorFilter').value;
    var minVol  = parseFloat(document.getElementById('volFilter').value) || 0;
    var minCap  = parseFloat(document.getElementById('capFilter').value) || 0;

    $.fn.dataTable.ext.search = [];

    $.fn.dataTable.ext.search.push(function(settings, data, dataIndex, rowData, counter) {{
        var row = window.table.row(dataIndex).node();
        var rowSector = $(row).data('sector') || '';
        var rowVol    = parseFloat($(row).data('volume')) || 0;
        var rowCap    = parseFloat($(row).data('cap')) || 0;

        if (sector && rowSector !== sector) return false;
        if (rowVol < minVol) return false;
        if (minCap > 0 && rowCap < minCap) return false;
        return true;
    }});

    window.table.draw();
}}

function resetFilters() {{
    document.getElementById('sectorFilter').value = '';
    document.getElementById('volFilter').value    = '';
    document.getElementById('capFilter').value    = '';
    $.fn.dataTable.ext.search = [];
    window.table.draw();
}}

// Экспорт видимых строк в CSV
function exportCSV() {{
    var rows = [['Ticker','Company','Sector','Open','High','Low','Close','Volume','%Change','Gap%','Range%','MarketCap']];
    window.table.rows({{ search: 'applied' }}).every(function() {{
        var d = this.data();
        rows.push([
            $(d[0]).text().trim(),
            d[1], d[2], d[3], d[4], d[5], d[6], d[7], d[8], d[9], d[10], d[11]
        ]);
    }});

    var csv = rows.map(function(r) {{
        return r.map(function(c) {{
            c = String(c).replace(/"/g, '""');
            return '"' + c + '"';
        }}).join(',');
    }}).join('\\n');

    var blob = new Blob([csv], {{ type: 'text/csv;charset=utf-8;' }});
    var url  = URL.createObjectURL(blob);
    var a    = document.createElement('a');
    a.href   = url;
    a.download = 'momentum_report_{trade_date}.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}}
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"HTML-отчёт сохранён: {output_path} ({len(df)} строк)")


# ─────────────────────────────────────────────
# 12. UNIT-ТЕСТЫ
# ─────────────────────────────────────────────

class TestStockScreener(unittest.TestCase):
    """Unit tests for core screener functions."""

    def _make_df(self, data: list[dict]) -> pd.DataFrame:
        return pd.DataFrame(data)

    def test_percent_change(self):
        df = self._make_df([
            {"ticker": "AAPL", "open": 100.0, "high": 110.0, "low": 99.0, "close": 108.0, "volume": 1_000_000, "prev_close": 98.0},
            {"ticker": "TSLA", "open": 200.0, "high": 220.0, "low": 195.0, "close": 190.0, "volume": 2_000_000, "prev_close": 195.0},
        ])
        result = calculate_metrics(df)
        self.assertAlmostEqual(result.loc[0, "percent_change"], 8.0, places=1)
        self.assertAlmostEqual(result.loc[1, "percent_change"], -5.0, places=1)

    def test_gap_percent(self):
        df = self._make_df([
            {"ticker": "X", "open": 110.0, "high": 115.0, "low": 108.0, "close": 112.0, "volume": 600_000, "prev_close": 100.0},
        ])
        result = calculate_metrics(df)
        self.assertAlmostEqual(result.loc[0, "gap_percent"], 10.0, places=1)

    def test_filter_by_change(self):
        df = self._make_df([
            {"ticker": "A", "open": 100, "high": 120, "low": 99, "close": 115, "volume": 1_000_000, "prev_close": 100, "percent_change": 15.0, "gap_percent": 0, "intraday_range": 10},
            {"ticker": "B", "open": 100, "high": 105, "low": 99, "close": 103, "volume": 1_000_000, "prev_close": 100, "percent_change": 3.0,  "gap_percent": 0, "intraday_range": 5},
        ])
        result = filter_data(df, min_change=5.0, min_volume=500_000, min_close=5.0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["ticker"], "A")

    def test_filter_by_volume(self):
        df = self._make_df([
            {"ticker": "C", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100_000, "prev_close": 10, "percent_change": 10.0, "gap_percent": 0, "intraday_range": 10},
        ])
        result = filter_data(df, min_change=5.0, min_volume=500_000, min_close=5.0)
        self.assertEqual(len(result), 0)

    def test_empty_dataframe(self):
        df = pd.DataFrame()
        result = calculate_metrics(df)
        self.assertTrue(result.empty)
        result2 = filter_data(df)
        self.assertTrue(result2.empty)

    def test_zero_open(self):
        df = self._make_df([
            {"ticker": "Z", "open": 0, "high": 5, "low": 0, "close": 5, "volume": 600_000, "prev_close": 4},
        ])
        result = calculate_metrics(df)
        self.assertTrue(pd.isna(result.loc[0, "percent_change"]))

    def test_format_market_cap(self):
        self.assertEqual(format_market_cap(1_500_000_000), "$1.50B")
        self.assertEqual(format_market_cap(250_000_000), "$250.0M")
        self.assertEqual(format_market_cap(0), "N/A")


def run_tests() -> bool:
    """Запускает unit-тесты и возвращает True при успехе."""
    print("\n" + "=" * 50)
    print("Запуск unit-тестов...")
    print("=" * 50)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestStockScreener)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


# ─────────────────────────────────────────────
# 13. CLI
# ─────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    """Парсит аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Stock Momentum Screener — находит акции с сильным движением",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python app.py
  python app.py --min-change 5 --file tickers.csv
  python app.py --min-change 3 --min-volume 1000000 --days 2 --use-cache
  python app.py --test
        """,
    )
    parser.add_argument("--file",       default="tickers.csv",  help="CSV-файл с тикерами")
    parser.add_argument("--min-change", type=float, default=5.0, help="Минимальный %% изменения (default: 5)")
    parser.add_argument("--min-volume", type=int, default=500_000, help="Минимальный объём (default: 500000)")
    parser.add_argument("--min-close",  type=float, default=5.0,   help="Минимальная цена закрытия (default: 5)")
    parser.add_argument("--days",       type=int, default=1,        help="Смещение дней назад (default: 1 = вчера)")
    parser.add_argument("--use-cache",  action="store_true",         help="Использовать кэш (1 день)")
    parser.add_argument("--output",     default="report.html",       help="Имя выходного HTML-файла")
    parser.add_argument("--no-info",    action="store_true",         help="Не загружать инфо о компаниях (быстрее)")
    parser.add_argument("--test",       action="store_true",         help="Запустить unit-тесты и выйти")
    return parser.parse_args()


# ─────────────────────────────────────────────
# 14. MAIN
# ─────────────────────────────────────────────

def main() -> None:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()

    # Режим тестов
    if args.test:
        success = run_tests()
        sys.exit(0 if success else 1)

    _init_dirs()

    print("=" * 60)
    print("  📊 Stock Momentum Screener")
    print("=" * 60)

    # ── Шаг 1: загрузить тикеры ──
    try:
        tickers = load_tickers(args.file)
    except (FileNotFoundError, RuntimeError) as e:
        logger.error(str(e))
        sys.exit(1)
    if not tickers:
        logger.error("Список тикеров пуст.")
        sys.exit(1)

    # ── Шаг 2: определить дату ──
    trade_date = get_previous_trading_day(args.days)
    print(f"\n  📅 Торговая дата: {trade_date}")
    print(f"  📋 Тикеров: {len(tickers)}")
    print(f"  🎯 Мин. изменение: {args.min_change}%")
    print(f"  📦 Кэш: {'включён' if args.use_cache else 'выключен'}")
    print()

    # ── Шаг 3: загрузить данные ──
    df = fetch_data(tickers, trade_date, use_cache=args.use_cache)

    if df.empty:
        logger.error("Нет данных для обработки. Проверьте тикеры и дату.")
        print("\n⚠️  Возможные причины:")
        print("   • Сегодня выходной или праздник (нет торгов)")
        print("   • Проблема с интернет-соединением")
        print("   • Неверный формат тикеров в CSV")
        sys.exit(1)

    # ── Шаг 4: рассчитать метрики ──
    df = calculate_metrics(df)

    # ── Шаг 5: загрузить информацию о компаниях ──
    if not args.no_info:
        print("\n  🏢 Загрузка информации о компаниях...")
        df = enrich_with_company_info(df)
    else:
        df["name"] = df["ticker"]
        df["sector"] = "N/A"
        df["market_cap"] = 0

    # ── Шаг 6: фильтровать ──
    df_filtered = filter_data(df, args.min_change, args.min_volume, args.min_close)

    if df_filtered.empty:
        print(f"\n⚠️  Ни одна акция не прошла фильтр (мин. изменение: {args.min_change}%)")
        print("   Попробуйте уменьшить --min-change")
        sys.exit(0)

    # ── Шаг 7: сортировка ──
    df_filtered = df_filtered.sort_values("percent_change", ascending=False).reset_index(drop=True)

    # ── Шаг 8: сохранить историю и CSV ──
    save_history(df_filtered, trade_date)
    csv_output = args.output.replace(".html", ".csv")
    save_csv(df_filtered, csv_output)

    # ── Шаг 9: сгенерировать HTML ──
    generate_html(df_filtered, trade_date, args.output, args.min_change)

    # Логируем обработанные тикеры
    with open(processed_log, "a") as f:
        f.write(f"\n[{datetime.now()}] Обработано {len(df_filtered)} акций за {trade_date}\n")
        for t in df_filtered["ticker"]:
            f.write(f"  {t}\n")

    # ── Итоговый отчёт ──
    print("\n" + "=" * 60)
    print(f"  ✅ Готово! Найдено акций: {len(df_filtered)}")
    print(f"  📄 HTML:  {args.output}")
    print(f"  📊 CSV:   {csv_output}")
    print(f"  💾 История: history/")
    print(f"  📝 Логи:    logs/")
    print("=" * 60)

    # Топ-5 для быстрого просмотра
    print("\n  🏆 ТОП-5 акций:")
    top5 = df_filtered.head(5)[["ticker", "close", "percent_change", "volume"]]
    for _, r in top5.iterrows():
        print(f"     {r['ticker']:>6}  ${r['close']:.2f}  +{r['percent_change']:.1f}%  vol={format_volume(int(r['volume']))}")
    print()


if __name__ == "__main__":
    main()
