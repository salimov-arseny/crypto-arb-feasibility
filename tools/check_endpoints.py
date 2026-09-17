"""
Шаг 0: проверка доступности публичных эндпоинтов бирж из README.

Скрипт ничего не считает и ничего не сохраняет в конфиг. Он отвечает на два
вопроса: какие площадки реально отдают стакан с этой машины через текущий канал
и какая у них глубина по каждому интересующему нас инструменту. От ответа
зависит состав config.yaml и список пар бирж, между которыми вообще имеет
смысл искать расхождения.

Запуск:  .venv/bin/python tools/check_endpoints.py [BTC/USDT ...]
         без аргументов проверяются все инструменты из SYMBOLS.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

# Сколько раз дёргаем каждый эндпоинт. Одного раза мало: разовый таймаут
# через VPN ничего не доказывает, а медиана по трём попыткам уже о чём-то говорит.
ATTEMPTS = 3
TIMEOUT_SEC = 10.0
DEPTH_LEVELS = 100  # столько уровней просим у всех бирж, как заявлено в README

# ---------------------------------------------------------------------------
# Таблица соответствий тикеров.
#
# Универсального имени инструмента не существует: BTCUSDT у Binance и Bybit,
# BTC-USDT у OKX, а Kraken исторически обозначает биткоин как XBT. Держать это
# в одном месте нужно с самого начала, иначе на шаге 3 каждый фетчер обзаведётся
# своим собственным способом склеивать строки.
# ---------------------------------------------------------------------------

SYMBOLS: dict[str, dict[str, str]] = {
    "BTC/USDT": {
        "Binance": "BTCUSDT",
        "Bybit": "BTCUSDT",
        "OKX": "BTC-USDT",
        "Kraken": "XBTUSDT",
    },
    "ETH/USDT": {
        "Binance": "ETHUSDT",
        "Bybit": "ETHUSDT",
        "OKX": "ETH-USDT",
        "Kraken": "ETHUSDT",
    },
    "SOL/USDT": {
        "Binance": "SOLUSDT",
        "Bybit": "SOLUSDT",
        "OKX": "SOL-USDT",
        "Kraken": "SOLUSDT",
    },
}


@dataclass
class Book:
    """Нормализованный результат проверки: уровни как (цена, количество)."""

    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]

    @property
    def best_bid(self) -> float:
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        return self.asks[0][0]

    def ask_notional(self) -> float:
        """Сколько USDT нужно, чтобы выкупить весь видимый ask-стакан."""
        return sum(price * qty for price, qty in self.asks)

    def ask_reach_bps(self) -> float:
        """Как далеко от лучшей цены достают полученные уровни продажи, в б.п.

        Это ключевая поправка. Биржи отдают 100 уровней, а не 100 уровней
        одинаковой ширины: шаг цены у BTCUSDT и SOLUSDT одинаковый (0,01 USDT),
        но для биткоина по 76 768 сотня шагов подряд - это 0,0013 % цены
        (0,13 б.п.), а для солана по 101,34 - 0,99 % (98,7 б.п.), в 760 раз
        шире. Поэтому сравнивать объём «по всем уровням» между инструментами
        нельзя: это разные ценовые окна. Фактический охват ещё и больше
        идеального, потому что уровни идут с пропусками, - его и меряем.
        """
        return (self.asks[-1][0] - self.best_ask) / self.best_ask * 10_000

    def ask_depth_within(self, bps: float) -> tuple[float, bool]:
        """Объём продажи в USDT внутри полосы bps от лучшего аска.

        Возвращает (объём, достаёт_ли_стакан_до_границы). Если не достаёт,
        число - оценка снизу: настоящий объём в полосе больше видимого.
        Это ровно та «ограниченная глубина», о которой предупреждает README.
        """
        limit = self.best_ask * (1 + bps / 10_000)
        notional = sum(p * q for p, q in self.asks if p <= limit)
        return notional, self.ask_reach_bps() >= bps

    def spread_bps(self) -> float:
        """Собственный спред биржи в базисных пунктах (1 б.п. = 0,01 %)."""
        return (self.best_ask - self.best_bid) / self.best_bid * 10_000


@dataclass
class Probe:
    exchange: str
    label: str
    url: str
    params: dict[str, Any]
    # Разбор ответа: получает распарсенный JSON, возвращает (ок?, пояснение, стакан).
    parse: Callable[[Any], tuple[bool, str, Book | None]]


@dataclass
class Result:
    """Итог по эндпоинту.

    Попытки считаются по отдельности. «Один раз из трёх подвис» и «не отвечает
    вообще» - разные диагнозы: первое означает нестабильный канал, второе -
    что площадки для нас нет. Успехом считается хотя бы один валидный стакан,
    но число неудач сохраняется и попадает в отчёт.
    """

    probe: Probe
    http_status: int | None = None
    book: Book | None = None                              # первый удачный стакан
    latencies_ms: list[float] = field(default_factory=list)
    successes: int = 0
    failures: list[str] = field(default_factory=list)     # причины неудачных попыток

    @property
    def ok(self) -> bool:
        return self.book is not None

    @property
    def attempts(self) -> int:
        return self.successes + len(self.failures)

    @property
    def detail(self) -> str:
        if self.failures:
            # Показываем разные причины, не повторяя одну и ту же трижды.
            reasons = ", ".join(dict.fromkeys(self.failures))
            return f"{self.successes}/{self.attempts} удачно; неудачи: {reasons}"
        return f"{self.successes}/{self.attempts} удачно"

    @property
    def median_ms(self) -> float | None:
        return statistics.median(self.latencies_ms) if self.latencies_ms else None


# ---------------------------------------------------------------------------
# Разборщики ответов.
#
# Главная мысль: HTTP 200 не означает, что данные есть. Kraken на неизвестную
# пару отвечает кодом 200 и непустым полем "error". Bybit и OKX кладут свой код
# ошибки в тело ответа. Поэтому признак успеха здесь всегда один и тот же -
# в ответе лежит стакан, и в нём есть уровни с обеих сторон.
#
# Формат уровня тоже у всех свой: [цена, объём] у Binance и Bybit,
# [цена, объём, ликвидации, число ордеров] у OKX, [цена, объём, время] у Kraken.
# Первые два поля везём одинаково, остальное отбрасываем.
# ---------------------------------------------------------------------------

def _to_book(bids: list, asks: list) -> tuple[bool, str, Book | None]:
    if not bids or not asks:
        return False, "стакан пуст с одной из сторон", None
    book = Book(
        bids=[(float(p), float(q)) for p, q, *_ in bids],
        asks=[(float(p), float(q)) for p, q, *_ in asks],
    )
    return True, f"уровней bid/ask: {len(book.bids)}/{len(book.asks)}", book


def parse_binance(data: Any) -> tuple[bool, str, Book | None]:
    if "code" in data:
        return False, f"биржа вернула ошибку: {data}", None
    return _to_book(data.get("bids", []), data.get("asks", []))


def parse_bybit(data: Any) -> tuple[bool, str, Book | None]:
    if data.get("retCode") != 0:
        return False, f"retCode={data.get('retCode')}: {data.get('retMsg')}", None
    result = data.get("result", {})
    return _to_book(result.get("b", []), result.get("a", []))


def parse_okx(data: Any) -> tuple[bool, str, Book | None]:
    if data.get("code") != "0":
        return False, f"code={data.get('code')}: {data.get('msg')}", None
    books = data.get("data", [])
    if not books:
        return False, "пустой массив data", None
    return _to_book(books[0].get("bids", []), books[0].get("asks", []))


def parse_kraken(data: Any) -> tuple[bool, str, Book | None]:
    # Kraken отдаёт 200 даже на ошибку - смотрим в тело.
    if data.get("error"):
        return False, f"error={data['error']}", None
    result = data.get("result", {})
    if not result:
        return False, "пустой result", None
    # Ключ результата - внутреннее имя пары, оно может отличаться от запрошенного.
    pair_key = next(iter(result))
    book_raw = result[pair_key]
    ok, detail, book = _to_book(book_raw.get("bids", []), book_raw.get("asks", []))
    return ok, f"внутреннее имя пары {pair_key!r}, {detail}", book


def build_probes(symbol: str) -> list[Probe]:
    """Эндпоинты - ровно те, что перечислены в README."""
    tickers = SYMBOLS[symbol]
    return [
        Probe(
            exchange="Binance",
            label="/api/v3/depth",
            url="https://api.binance.com/api/v3/depth",
            params={"symbol": tickers["Binance"], "limit": DEPTH_LEVELS},
            parse=parse_binance,
        ),
        Probe(
            exchange="Bybit",
            label="/v5/market/orderbook",
            url="https://api.bybit.com/v5/market/orderbook",
            params={"category": "spot", "symbol": tickers["Bybit"], "limit": DEPTH_LEVELS},
            parse=parse_bybit,
        ),
        Probe(
            exchange="OKX",
            label="/api/v5/market/books",
            url="https://www.okx.com/api/v5/market/books",
            params={"instId": tickers["OKX"], "sz": DEPTH_LEVELS},
            parse=parse_okx,
        ),
        Probe(
            exchange="Kraken",
            label="/0/public/Depth",
            url="https://api.kraken.com/0/public/Depth",
            params={"pair": tickers["Kraken"], "count": DEPTH_LEVELS},
            parse=parse_kraken,
        ),
    ]


def run_probe(probe: Probe) -> Result:
    res = Result(probe=probe)

    for attempt in range(ATTEMPTS):
        started = time.perf_counter()
        try:
            resp = requests.get(probe.url, params=probe.params, timeout=TIMEOUT_SEC)
            res.latencies_ms.append((time.perf_counter() - started) * 1000)
            res.http_status = resp.status_code

            # Отказ по региону или по лимиту не лечится повтором - выходим сразу.
            hard = {
                451: "HTTP 451: отказ по региону (геоблокировка)",
                403: "HTTP 403: доступ запрещён (вероятно, регион)",
                429: "HTTP 429: превышен лимит запросов",
            }
            if resp.status_code in hard:
                res.failures.append(hard[resp.status_code])
                break
            if resp.status_code != 200:
                res.failures.append(f"HTTP {resp.status_code}: {resp.text[:80]}")
                continue

            try:
                data = resp.json()
            except json.JSONDecodeError:
                res.failures.append(f"ответ не JSON: {resp.text[:80]}")
                continue

            ok, detail, book = probe.parse(data)
            if ok and book is not None:
                res.successes += 1
                if res.book is None:
                    res.book = book
                continue          # добираем замеры латентности
            res.failures.append(detail)
            break                 # осмысленный отказ биржи повтором не лечится

        except requests.exceptions.Timeout:
            res.failures.append(f"таймаут > {TIMEOUT_SEC:.0f} c")
        except requests.exceptions.SSLError as exc:
            res.failures.append(f"ошибка TLS: {type(exc).__name__}")
            break
        except requests.exceptions.ConnectionError as exc:
            # Сюда попадают и провал DNS, и обрыв соединения - для нас это
            # разные причины, но одинаково означает, что запрос не дошёл.
            res.failures.append(f"запрос не дошёл: {type(exc).__name__}")

        if attempt < ATTEMPTS - 1:
            time.sleep(0.3)       # не долбим биржу очередью запросов

    return res


# Полоса, в которой сравниваем глубину инструментов между собой.
# 5 б.п. = 0,05 % - величина порядка типичного наблюдаемого расхождения,
# то есть ровно тот масштаб, на котором нам и придётся исполнять объём.
BAND_BPS = 5.0


def check_symbol(symbol: str) -> list[Result]:
    print(f"\n### {symbol}")
    print("-" * 104)
    print(f"{'биржа':<9} {'тикер':<10} {'ответ':>7}  {'уровни':>8}  "
          f"{'лучший бид':>12} {'лучший аск':>12} {'спред':>10} "
          f"{'охват ask':>11}  {'объём в ' + str(int(BAND_BPS)) + ' б.п.':>18}")
    print("-" * 104)

    results = []
    for probe in build_probes(symbol):
        res = run_probe(probe)
        results.append(res)

        ticker = SYMBOLS[symbol][probe.exchange]
        lat = f"{res.median_ms:.0f} мс" if res.median_ms is not None else "—"

        if res.ok and res.book is not None:
            b = res.book
            band_notional, reaches = b.ask_depth_within(BAND_BPS)
            # ">" означает: стакан кончился раньше границы полосы,
            # настоящий объём внутри неё больше - это оценка снизу.
            band = f"{'' if reaches else '>'}{band_notional:,.0f} USDT"
            print(f"{probe.exchange:<9} {ticker:<10} {lat:>7}  "
                  f"{len(b.bids):>3}/{len(b.asks):<4}  "
                  f"{b.best_bid:>12,.4f} {b.best_ask:>12,.4f} "
                  f"{b.spread_bps():>6.2f} б.п. {b.ask_reach_bps():>7.2f} б.п.  "
                  f"{band:>18}")
            if res.failures:
                print(f"{'':>30}  внимание: {res.detail}")
        else:
            print(f"{probe.exchange:<9} {ticker:<10} {lat:>7}  FAIL: {res.detail}")

    return results


def main(argv: list[str]) -> int:
    symbols = argv[1:] or list(SYMBOLS)
    unknown = [s for s in symbols if s not in SYMBOLS]
    if unknown:
        print(f"Неизвестные инструменты: {', '.join(unknown)}", file=sys.stderr)
        return 2

    print(f"Проверка эндпоинтов бирж. Попыток на эндпоинт: {ATTEMPTS}, "
          f"таймаут {TIMEOUT_SEC:.0f} c, запрошено уровней: {DEPTH_LEVELS}")
    print(f"Время запуска (UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}")
    print(f"«Охват ask» — как далеко от лучшей цены достают 100 полученных уровней.")
    print(f"«Объём в {BAND_BPS:.0f} б.п.» — сколько USDT продают внутри полосы "
          f"{BAND_BPS / 100:.2f} % от лучшего аска;")
    print(f"   знак > означает, что стакан кончился раньше границы, то есть это "
          f"оценка снизу.")

    by_symbol = {s: check_symbol(s) for s in symbols}

    print("\n" + "=" * 94)
    all_exchanges = sorted({p.exchange for s in symbols for p in build_probes(s)})

    # Биржа годится для работы, только если отдаёт стакан по всем инструментам:
    # иначе сетка наблюдений получится дырявой.
    everywhere = sorted(
        e for e in all_exchanges
        if all(any(r.probe.exchange == e and r.ok for r in by_symbol[s]) for s in symbols)
    )
    partial = [e for e in all_exchanges if e not in everywhere]

    print(f"Отдают стакан по всем инструментам: "
          f"{', '.join(everywhere) if everywhere else 'ни одной'}")
    if partial:
        print(f"Отдают частично или не отдают:      {', '.join(partial)}")

    n = len(everywhere)
    print(f"Пар бирж для сравнения: {n * (n - 1) // 2} "
          f"(из {n} площадок) x {len(symbols)} инструмента = "
          f"{n * (n - 1) // 2 * len(symbols)} направлений наблюдения")

    return 0 if n >= 2 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
