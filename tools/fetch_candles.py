"""Выгрузка исторических свечей для оценки волатильности (шаг 7).

Свечи нужны, чтобы оценить, насколько цена уходит за время перевода между
биржами. Берём их у Binance: /api/v3/klines - публичный эндпоинт без
авторизации, отдаёт до 1000 свечей за запрос.

Почему одна биржа, а не все четыре. Оценивается волатильность САМОГО АКТИВА
на горизонте перевода, а не разница между площадками. Цены четырёх бирж на
этих горизонтах различаются на доли базисного пункта - это видно в замерах
шага 3, - и для оценки сигмы выбор площадки не важен. Важна длина ряда,
а у Binance она наибольшая.

Почему в имени файла стоит дата. Закрытая свеча не меняется: биржа историю
не переписывает. Но окно скользит - каждый прогон берёт последние N дней
ОТ СЕГОДНЯ, и без даты в имени повторный запуск молча затёр бы тот набор
данных, на котором посчитаны уже опубликованные числа.

Запуск:
    .venv/bin/python -u tools/fetch_candles.py --interval 1m --days 30
    .venv/bin/python -u tools/fetch_candles.py --interval 1s --hours 12
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "candles"
URL = "https://api.binance.com/api/v3/klines"

LIMIT = 1000          # максимум свечей за один запрос
PAUSE_SEC = 0.25      # троттлинг: лимиты биржи щадим с запасом
ATTEMPTS = 5
TIMEOUT_SEC = 30.0

INTERVAL_SEC = {"1s": 1, "1m": 60, "5m": 300, "15m": 900, "1h": 3600}

sys.path.insert(0, str(ROOT))
from core.config import load_config  # noqa: E402


def get_page(params: dict) -> list:
    """Одна страница с повторами.

    Выгрузка месяца минутных свечей - это полсотни запросов подряд, и
    разрыв на любом из них неизбежен при нестабильном канале. Ронять всю
    закачку из-за одного таймаута нельзя: страницы идут последовательно,
    и потерянная страница означает потерю всего хвоста. Пауза между
    попытками растёт, чтобы не долбить биржу очередью.
    """
    last: Exception | None = None
    for attempt in range(ATTEMPTS):
        try:
            resp = requests.get(URL, timeout=TIMEOUT_SEC, params=params)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last = exc
            wait = PAUSE_SEC * 2 ** attempt
            print(f"\n  попытка {attempt + 1}/{ATTEMPTS} не удалась "
                  f"({type(exc).__name__}), жду {wait:.1f} с", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"страница не получена за {ATTEMPTS} попыток: {last}")


def fetch_range(ticker: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Выкачать свечи постранично: биржа отдаёт не больше 1000 за раз."""
    step_ms = INTERVAL_SEC[interval] * 1000
    rows: list = []
    cursor = start_ms

    while cursor < end_ms:
        batch = get_page({"symbol": ticker, "interval": interval,
                          "startTime": cursor, "endTime": end_ms,
                          "limit": LIMIT})
        if not batch:
            break
        rows.extend(batch)
        # Следующая страница начинается сразу за последней полученной свечой.
        cursor = batch[-1][0] + step_ms
        print(f"\r  {ticker} {interval}: {len(rows):>7} свечей", end="",
              file=sys.stderr, flush=True)
        if len(batch) < LIMIT:
            break
        time.sleep(PAUSE_SEC)
    print(file=sys.stderr)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Выгрузка свечей Binance")
    parser.add_argument("--interval", default="1m", choices=sorted(INTERVAL_SEC))
    parser.add_argument("--days", type=float, default=None)
    parser.add_argument("--hours", type=float, default=None)
    args = parser.parse_args(argv)

    if args.days is None and args.hours is None:
        args.days = 30.0
    span_sec = (args.days or 0) * 86400 + (args.hours or 0) * 3600

    cfg = load_config()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(span_sec * 1000)
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Свечи {args.interval} за {span_sec / 3600:.1f} ч, источник {URL}")

    for symbol in cfg["symbols"]:
        ticker = cfg["exchanges"]["binance"]["tickers"][symbol]
        rows = fetch_range(ticker, args.interval, start_ms, end_ms)
        if not rows:
            print(f"  {symbol}: пусто", file=sys.stderr)
            continue

        # Из свечи берём только время открытия и цену закрытия: для оценки
        # волатильности доходностей этого достаточно, а лишние поля только
        # раздули бы файл.
        path = OUT_DIR / f"{ticker}_{args.interval}_{stamp}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["open_time_ms", "close"])
            for candle in rows:
                writer.writerow([candle[0], candle[4]])

        covered_h = (rows[-1][0] - rows[0][0]) / 3600_000
        gaps = sum(1 for a, b in zip(rows, rows[1:])
                   if b[0] - a[0] != INTERVAL_SEC[args.interval] * 1000)
        print(f"  {symbol:<9} {len(rows):>7} свечей, {covered_h:>7.1f} ч"
              + (f", РАЗРЫВОВ: {gaps}" if gaps else "")
              + f" -> {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
