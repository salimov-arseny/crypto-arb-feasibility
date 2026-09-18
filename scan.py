"""Цикл сбора наблюдений: снимаем стаканы, считаем маршруты, пишем csv.

Это то, ради чего написано всё предыдущее. Один снимок не отвечает на
вопрос README: какая доля наблюдаемых расхождений выживает после издержек.
Выживают всплески, а не среднее, и увидеть их можно только в логе за
несколько часов.

Наблюдение - это один маршрут в один момент времени: купить инструмент
на бирже A, перевести, продать на бирже B. За один снимок получается
столько наблюдений, сколько есть упорядоченных пар бирж, умноженное
на число инструментов.

Запуск:
    .venv/bin/python -u scan.py --minutes 180
    .venv/bin/python -u scan.py --once            # один снимок, для проверки
    .venv/bin/python -u scan.py --out logs/my.csv
"""

from __future__ import annotations

import argparse
import csv
import itertools
import pathlib
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.arbitrage import find_optimum
from core.config import load_config
from core.costs import CostError, Route, build_route
from core.orderbook import OrderBook
from fetchers import FetchError, build_all

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "logs" / "observations.csv"

# Как часто сбрасывать файл на диск. Многочасовой прогон обязан пережить
# падение: буфер в несколько десятков строк - это секунды наблюдений.
FLUSH_EVERY = 50

COLUMNS = [
    "sweep", "ts_utc", "symbol", "buy", "sell", "network", "status",
    "skew_sec", "best_ask_buy", "best_bid_sell", "naive_spread_bps",
    "opt_volume", "opt_turnover", "vwap_buy", "vwap_sell",
    "slippage_buy_bps", "slippage_sell_bps",
    "gross", "fee_buy", "fee_sell", "withdrawal", "net", "net_bps",
    "complete", "feasible", "transfer_time_sec",
    "latency_buy_ms", "latency_sell_ms",
]

# Статусы наблюдения. Отбракованные пишутся в лог наравне с годными:
# доля брака - сама по себе результат, без неё нельзя судить,
# насколько лог представителен.
STATUS_OK = "ok"
STATUS_SKEW = "skew_exceeded"       # снимки разъехались во времени
STATUS_NO_BOOK = "fetch_failed"     # одна из бирж не ответила
STATUS_NO_OPT = "no_optimum"        # искать было негде


@dataclass
class Tally:
    sweeps: int = 0
    rows: int = 0
    ok: int = 0
    skew: int = 0
    no_book: int = 0
    no_opt: int = 0
    profitable: int = 0


def sweep_books(fetchers: dict[str, Any], symbols: list[str]
                ) -> tuple[dict[tuple[str, str], OrderBook], dict[tuple[str, str], str]]:
    """Снять все стаканы параллельно.

    Параллельно, а не по очереди, по измеренной причине: последовательный
    обход четырёх бирж занимал от 1,2 с в обычном случае до 11,4 с, когда
    одна площадка подвисала, и разброс меток доходил до 0,84 с при допуске
    в 1,0 с. При параллельном сборе разброс сжимается до худшей одиночной
    задержки, а зависшая биржа задерживает только себя.
    """
    tasks = [(key, symbol) for key in fetchers for symbol in symbols]
    books: dict[tuple[str, str], OrderBook] = {}
    failures: dict[tuple[str, str], str] = {}

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(fetchers[k].fetch, s): (k, s) for k, s in tasks}
        for future, key in futures.items():
            try:
                books[key] = future.result()
            except FetchError as exc:
                failures[key] = str(exc)
            except Exception as exc:                # noqa: BLE001
                failures[key] = f"{type(exc).__name__}: {exc}"
    return books, failures


def symbol_skew(books: dict[tuple[str, str], OrderBook], symbol: str) -> float | None:
    """Разброс времени между снимками разных бирж по одному инструменту.

    Считается ТОЛЬКО по нашим меткам fetched_at. Смешивать их с временем,
    сообщённым биржей, нельзя: сверка на шаге 3 показала, что локальные
    часы отстают на 0,7-0,9 с, то есть почти на весь допуск. Между двумя
    нашими метками этот сдвиг сокращается, между нашей и биржевой - нет.
    """
    stamps = [b.fetched_at for (_, s), b in books.items() if s == symbol]
    if len(stamps) < 2:
        return None
    return max(stamps) - min(stamps)


def blank_row(sweep: int, ts: str, symbol: str, buy: str, sell: str,
              status: str, **extra: Any) -> dict[str, Any]:
    row = {c: "" for c in COLUMNS}
    row.update(sweep=sweep, ts_utc=ts, symbol=symbol, buy=buy, sell=sell,
               status=status, **extra)
    return row


def observe(route: Route, book_buy: OrderBook, book_sell: OrderBook,
            sweep: int, ts: str, skew: float) -> dict[str, Any]:
    """Одно наблюдение: наивное расхождение и результат поиска оптимума."""
    naive_bps = ((book_sell.best_bid - book_buy.best_ask)
                 / book_buy.best_ask * 10_000)

    row = blank_row(sweep, ts, route.symbol, route.buy.key, route.sell.key,
                    STATUS_OK,
                    network=route.network.code,
                    skew_sec=round(skew, 4),
                    best_ask_buy=book_buy.best_ask,
                    best_bid_sell=book_sell.best_bid,
                    naive_spread_bps=round(naive_bps, 4),
                    transfer_time_sec=route.network.transfer_time_sec or "",
                    latency_buy_ms=round(book_buy.latency_ms or 0, 1),
                    latency_sell_ms=round(book_sell.latency_ms or 0, 1))

    # Цель - максимум прибыли в деньгах, как и спрашивает README.
    optimum = find_optimum(route, book_buy, book_sell, objective="net")
    if optimum.best is None:
        row["status"] = STATUS_NO_OPT
        return row

    best = optimum.best
    row.update(
        opt_volume=best.volume,
        opt_turnover=round(best.turnover, 4),
        vwap_buy=best.price_buy,
        vwap_sell=best.price_sell,
        slippage_buy_bps=round(
            book_buy.slippage_bps(book_buy.buy(best.volume), "buy"), 4),
        slippage_sell_bps=round(
            book_sell.slippage_bps(book_sell.sell(best.volume), "sell"), 4),
        gross=round(best.gross, 6),
        fee_buy=round(best.costs.fee_buy, 6),
        fee_sell=round(best.costs.fee_sell, 6),
        withdrawal=round(best.costs.withdrawal, 6),
        net=round(best.net, 6),
        net_bps=round(best.net_bps, 4),
        complete=int(best.complete),
        feasible=int(best.feasible),
    )
    return row


def run(cfg: dict[str, Any], out_path: pathlib.Path, minutes: float | None,
        once: bool) -> Tally:
    fetchers = build_all(cfg)
    symbols = cfg["symbols"]
    interval = float(cfg["scan"]["interval_sec"])
    max_skew = float(cfg["scan"]["max_snapshot_skew_sec"])
    pairs = list(itertools.permutations(cfg["exchanges"], 2))

    # Маршруты строятся один раз: конфиг за время прогона не меняется,
    # а выбор самой дешёвой сети от стакана не зависит.
    routes: dict[tuple[str, str, str], Route] = {}
    for symbol in symbols:
        for buy, sell in pairs:
            try:
                routes[(symbol, buy, sell)] = build_route(cfg, symbol, buy, sell)
            except CostError as exc:
                print(f"  маршрут {buy}->{sell} {symbol} пропущен: {exc}",
                      file=sys.stderr)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not out_path.exists() or out_path.stat().st_size == 0

    stop = {"now": False}

    def handle_stop(signum, frame):      # noqa: ANN001, ARG001
        stop["now"] = True
        print("\nПолучен сигнал остановки, дописываю файл...", file=sys.stderr)

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    # Прогревочный снимок, в лог не попадает.
    #
    # Первый запрос к каждой бирже платит за установку соединения и
    # рукопожатие TLS. В замере это дало разброс меток 10,2 с при
    # медиане 0,30 с на последующих снимках - то есть весь брак по
    # времени приходился ровно на первый снимок. Это систематический
    # артефакт измерения, а не свойство рынка, и в логе ему не место.
    print("Прогрев соединений...", file=sys.stderr)
    warm_started = time.time()
    _, warm_failures = sweep_books(fetchers, symbols)
    print(f"  заняло {time.time() - warm_started:.2f} с"
          + (f", не ответили: {len(warm_failures)}" if warm_failures else ""),
          file=sys.stderr)

    tally = Tally()
    deadline = None if minutes is None else time.time() + minutes * 60

    with out_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        if is_new:
            writer.writeheader()

        while not stop["now"]:
            started = time.time()
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            tally.sweeps += 1

            books, failures = sweep_books(fetchers, symbols)

            for symbol in symbols:
                skew = symbol_skew(books, symbol)
                for buy, sell in pairs:
                    route = routes.get((symbol, buy, sell))
                    if route is None:
                        continue
                    book_buy = books.get((buy, symbol))
                    book_sell = books.get((sell, symbol))

                    if book_buy is None or book_sell is None:
                        writer.writerow(blank_row(
                            tally.sweeps, ts, symbol, buy, sell, STATUS_NO_BOOK,
                            network=route.network.code))
                        tally.no_book += 1
                    elif skew is not None and skew > max_skew:
                        # Снимки разъехались во времени: расхождение между
                        # ними может быть артефактом опроса, а не рынка.
                        # Записываем с пометкой, чтобы доля брака была видна.
                        writer.writerow(blank_row(
                            tally.sweeps, ts, symbol, buy, sell, STATUS_SKEW,
                            network=route.network.code,
                            skew_sec=round(skew, 4)))
                        tally.skew += 1
                    else:
                        row = observe(route, book_buy, book_sell,
                                      tally.sweeps, ts, skew or 0.0)
                        writer.writerow(row)
                        if row["status"] == STATUS_OK:
                            tally.ok += 1
                            if row["net"] != "" and float(row["net"]) > 0:
                                tally.profitable += 1
                        else:
                            tally.no_opt += 1
                    tally.rows += 1

            if tally.rows % FLUSH_EVERY < len(pairs) * len(symbols):
                fh.flush()

            elapsed = time.time() - started
            print(f"\rснимков {tally.sweeps}, строк {tally.rows}, "
                  f"годных {tally.ok}, прибыльных {tally.profitable}, "
                  f"брак по времени {tally.skew}, без стакана {tally.no_book}, "
                  f"снимок занял {elapsed:.2f} с   ",
                  end="", file=sys.stderr, flush=True)

            if failures and tally.sweeps == 1:
                for key, reason in failures.items():
                    print(f"\n  {key}: {reason}", file=sys.stderr)

            if once or (deadline is not None and time.time() >= deadline):
                break

            # Спим остаток интервала, а не интервал целиком: иначе шаг
            # сбора поплывёт на время, потраченное на снимок и счёт.
            rest = interval - (time.time() - started)
            while rest > 0 and not stop["now"]:
                nap = min(0.25, rest)      # дробим, чтобы Ctrl+C отвечал сразу
                time.sleep(nap)
                rest -= nap

    print(file=sys.stderr)
    return tally


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сбор наблюдений в csv")
    parser.add_argument("--minutes", type=float, default=None,
                        help="сколько собирать; без него - до Ctrl+C")
    parser.add_argument("--once", action="store_true",
                        help="один снимок и выход")
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT,
                        help=f"куда писать (по умолчанию {DEFAULT_OUT})")
    args = parser.parse_args(argv)

    cfg = load_config()
    print(f"Сбор в {args.out}")
    print(f"Интервал {cfg['scan']['interval_sec']} с, "
          f"допуск на разброс снимков {cfg['scan']['max_snapshot_skew_sec']} с, "
          f"глубина {cfg['meta']['orderbook_depth']} уровней")

    tally = run(cfg, args.out, args.minutes, args.once)

    print(f"\nСнимков: {tally.sweeps}. Строк записано: {tally.rows}.")
    if tally.rows:
        print(f"  годных наблюдений:       {tally.ok:>7} "
              f"({tally.ok / tally.rows * 100:.1f} %)")
        print(f"  прибыльных из них:       {tally.profitable:>7} "
              f"({tally.profitable / tally.ok * 100:.2f} %)"
              if tally.ok else "  прибыльных из них:         0")
        print(f"  брак по разбросу времени:{tally.skew:>7} "
              f"({tally.skew / tally.rows * 100:.1f} %)")
        print(f"  без стакана:             {tally.no_book:>7} "
              f"({tally.no_book / tally.rows * 100:.1f} %)")
        print(f"  негде искать оптимум:    {tally.no_opt:>7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
