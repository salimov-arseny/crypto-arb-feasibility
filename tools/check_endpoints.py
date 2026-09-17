"""
Шаг 0: проверка доступности публичных эндпоинтов бирж из README.

Скрипт ничего не считает и ничего не сохраняет в конфиг. Он отвечает на один
вопрос: какие площадки реально отдают стакан с этой машины через текущий канал.
От ответа зависит состав config.yaml и список пар бирж, между которыми вообще
имеет смысл искать расхождения.

Запуск:  .venv/bin/python tools/check_endpoints.py
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

# Инструмент для проверки берём один и самый ликвидный: BTC/USDT.
# Если не работает он, не заработает ничего.
PROBE_SYMBOL = "BTC/USDT"


@dataclass
class Probe:
    """Одна проверка: куда идём и как понять, что ответ осмысленный."""

    exchange: str
    label: str            # что за эндпоинт по смыслу: стакан / метаданные
    url: str
    params: dict[str, Any]
    # Функция разбора: получает распарсенный JSON, возвращает
    # (ок?, человекочитаемое пояснение).
    parse: Callable[[Any], tuple[bool, str]]


@dataclass
class Result:
    probe: Probe
    ok: bool = False
    http_status: int | None = None
    detail: str = ""
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def median_ms(self) -> float | None:
        return statistics.median(self.latencies_ms) if self.latencies_ms else None


# ---------------------------------------------------------------------------
# Разборщики ответов.
#
# Главная мысль: HTTP 200 не означает, что данные есть. Kraken на неизвестную
# пару отвечает кодом 200 и непустым полем "error". Bybit и OKX кладут свой код
# ошибки в тело ответа. Поэтому признак успеха здесь всегда один и тот же —
# в ответе лежит стакан, и в нём есть уровни с обеих сторон.
# ---------------------------------------------------------------------------

def _levels_summary(bids: list, asks: list) -> tuple[bool, str]:
    if not bids or not asks:
        return False, "стакан пуст с одной из сторон"
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    return True, (
        f"уровней bid/ask: {len(bids)}/{len(asks)}, "
        f"лучший бид {best_bid:,.2f}, лучший аск {best_ask:,.2f}"
    )


def parse_binance_depth(data: Any) -> tuple[bool, str]:
    if "code" in data:
        return False, f"биржа вернула ошибку: {data}"
    return _levels_summary(data.get("bids", []), data.get("asks", []))


def parse_binance_info(data: Any) -> tuple[bool, str]:
    symbols = data.get("symbols", [])
    if not symbols:
        return False, f"нет поля symbols: {str(data)[:120]}"
    filters = {f["filterType"] for f in symbols[0].get("filters", [])}
    return True, f"символ {symbols[0]['symbol']}, фильтров: {len(filters)}"


def parse_bybit(data: Any) -> tuple[bool, str]:
    if data.get("retCode") != 0:
        return False, f"retCode={data.get('retCode')}: {data.get('retMsg')}"
    result = data.get("result", {})
    return _levels_summary(result.get("b", []), result.get("a", []))


def parse_okx(data: Any) -> tuple[bool, str]:
    if data.get("code") != "0":
        return False, f"code={data.get('code')}: {data.get('msg')}"
    books = data.get("data", [])
    if not books:
        return False, "пустой массив data"
    return _levels_summary(books[0].get("bids", []), books[0].get("asks", []))


def parse_kraken(data: Any) -> tuple[bool, str]:
    # Kraken отдаёт 200 даже на ошибку — смотрим в тело.
    if data.get("error"):
        return False, f"error={data['error']}"
    result = data.get("result", {})
    if not result:
        return False, "пустой result"
    # Ключ результата — внутреннее имя пары, оно может отличаться от запрошенного.
    pair_key = next(iter(result))
    book = result[pair_key]
    ok, detail = _levels_summary(book.get("bids", []), book.get("asks", []))
    return ok, f"внутреннее имя пары {pair_key!r}, {detail}"


# ---------------------------------------------------------------------------
# Список проверок. Эндпоинты — ровно те, что перечислены в README.
#
# Обрати внимание на имена инструмента: BTCUSDT у Binance и Bybit, BTC-USDT
# у OKX, XBTUSDT у Kraken (там историческое обозначение биткоина — XBT).
# Универсального тикера не существует, и это первая же вещь, которая
# на шаге 3 превратится в таблицу соответствий в конфиге.
# ---------------------------------------------------------------------------

PROBES: list[Probe] = [
    Probe(
        exchange="Binance",
        label="стакан /api/v3/depth",
        url="https://api.binance.com/api/v3/depth",
        params={"symbol": "BTCUSDT", "limit": 100},
        parse=parse_binance_depth,
    ),
    Probe(
        exchange="Binance",
        label="метаданные /api/v3/exchangeInfo",
        url="https://api.binance.com/api/v3/exchangeInfo",
        params={"symbol": "BTCUSDT"},
        parse=parse_binance_info,
    ),
    Probe(
        exchange="Bybit",
        label="стакан /v5/market/orderbook",
        url="https://api.bybit.com/v5/market/orderbook",
        params={"category": "spot", "symbol": "BTCUSDT", "limit": 100},
        parse=parse_bybit,
    ),
    Probe(
        exchange="OKX",
        label="стакан /api/v5/market/books",
        url="https://www.okx.com/api/v5/market/books",
        params={"instId": "BTC-USDT", "sz": 100},
        parse=parse_okx,
    ),
    Probe(
        exchange="Kraken",
        label="стакан /0/public/Depth",
        url="https://api.kraken.com/0/public/Depth",
        params={"pair": "XBTUSDT", "count": 100},
        parse=parse_kraken,
    ),
]


def run_probe(probe: Probe) -> Result:
    res = Result(probe=probe)

    for attempt in range(ATTEMPTS):
        started = time.perf_counter()
        try:
            resp = requests.get(probe.url, params=probe.params, timeout=TIMEOUT_SEC)
            elapsed_ms = (time.perf_counter() - started) * 1000
            res.latencies_ms.append(elapsed_ms)
            res.http_status = resp.status_code

            if resp.status_code == 451:
                res.ok, res.detail = False, "HTTP 451: отказ по региону (геоблокировка)"
                break
            if resp.status_code == 403:
                res.ok, res.detail = False, "HTTP 403: доступ запрещён (вероятно, регион)"
                break
            if resp.status_code == 429:
                res.ok, res.detail = False, "HTTP 429: превышен лимит запросов"
                break
            if resp.status_code != 200:
                res.ok, res.detail = False, f"HTTP {resp.status_code}: {resp.text[:120]}"
                continue

            try:
                data = resp.json()
            except json.JSONDecodeError:
                res.ok, res.detail = False, f"ответ не JSON: {resp.text[:120]}"
                continue

            res.ok, res.detail = probe.parse(data)
            if res.ok:
                continue  # добираем замеры латентности
            break

        except requests.exceptions.Timeout:
            res.ok = False
            res.detail = f"таймаут > {TIMEOUT_SEC:.0f} c"
        except requests.exceptions.SSLError as exc:
            res.ok = False
            res.detail = f"ошибка TLS: {type(exc).__name__}"
            break
        except requests.exceptions.ConnectionError as exc:
            # Сюда попадают и провал DNS, и обрыв соединения — для нас это
            # разные причины, но одинаково означает «площадки нет».
            res.ok = False
            res.detail = f"сеть недоступна: {type(exc).__name__}"
            break

        if attempt < ATTEMPTS - 1:
            time.sleep(0.3)  # не долбим биржу очередью запросов

    return res


def main() -> int:
    print(f"Проверка эндпоинтов, инструмент {PROBE_SYMBOL}, "
          f"попыток на эндпоинт: {ATTEMPTS}, таймаут {TIMEOUT_SEC:.0f} c")
    print(f"Время запуска (UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}")
    print("=" * 78)

    results = [run_probe(p) for p in PROBES]

    for res in results:
        mark = "OK  " if res.ok else "FAIL"
        lat = f"{res.median_ms:6.0f} мс" if res.median_ms is not None else "    — "
        print(f"[{mark}] {res.probe.exchange:<8} {lat}  {res.probe.label}")
        print(f"         {res.detail}")

    print("=" * 78)

    # Сводка по биржам: биржа считается доступной, если у неё работает стакан.
    ok_exchanges = sorted({
        r.probe.exchange for r in results
        if r.ok and r.probe.label.startswith("стакан")
    })
    all_exchanges = sorted({p.exchange for p in PROBES})
    failed = [e for e in all_exchanges if e not in ok_exchanges]

    print(f"Доступны стаканы: {', '.join(ok_exchanges) if ok_exchanges else 'ни одной'}")
    if failed:
        print(f"Недоступны:       {', '.join(failed)}")

    n = len(ok_exchanges)
    print(f"Пар бирж для сравнения: {n * (n - 1) // 2} (из {n} доступных площадок)")

    return 0 if n >= 2 else 1


if __name__ == "__main__":
    sys.exit(main())
