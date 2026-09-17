"""Фетчер стакана Binance.

Почему Binance выбран первым. По доступности все четыре площадки на
2026-09-17 равны - ни одна не отказала. Решили два других довода.

Глубина: внутри полосы 5 б.п. от лучшей цены у Binance стоит больше
789 000 USDT по BTC против 202 000 у Kraken. Проскальзывание - главное,
что мы здесь считаем, и на плотном стакане его рост виден, а на
разреженном тонет в пустотах между уровнями.

Формат: у Binance он самый простой из четырёх - уровни лежат прямо
в bids и asks парами [цена, объём]. Первая реализация нормализации
должна быть очевидно верной; сложные случаи разберём на шаге 3,
когда правильное поведение уже будет закреплено тестами.
"""

from __future__ import annotations

import time

import requests

from core.orderbook import OrderBook, OrderBookError
from fetchers.base import FetchError


class BinanceFetcher:
    name = "Binance"

    def __init__(self, rest_base: str, endpoint: str, tickers: dict[str, str],
                 depth: int = 100, timeout: float = 10.0) -> None:
        self.rest_base = rest_base.rstrip("/")
        self.endpoint = endpoint
        self.tickers = tickers
        self.depth = depth
        self.timeout = timeout
        self._session = requests.Session()

    def fetch(self, symbol: str) -> OrderBook:
        ticker = self.tickers.get(symbol)
        if ticker is None:
            raise FetchError(f"{self.name}: нет тикера для {symbol}")

        url = f"{self.rest_base}{self.endpoint}"
        started = time.perf_counter()
        try:
            resp = self._session.get(
                url, params={"symbol": ticker, "limit": self.depth},
                timeout=self.timeout)
        except requests.RequestException as exc:
            raise FetchError(f"{self.name}/{symbol}: запрос не дошёл: "
                             f"{type(exc).__name__}") from exc
        latency_ms = (time.perf_counter() - started) * 1000

        # Момент снимка фиксируем у себя: ответ /api/v3/depth не содержит
        # времени биржи. На шаге 6 по этим меткам отбраковываются
        # наблюдения, где стаканы разных площадок разъехались во времени.
        fetched_at = time.time()

        if resp.status_code != 200:
            raise FetchError(f"{self.name}/{symbol}: HTTP {resp.status_code}: "
                             f"{resp.text[:120]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise FetchError(f"{self.name}/{symbol}: ответ не JSON") from exc

        # Binance сообщает об ошибке полем code в теле ответа, оставляя
        # при этом статус 200. Проверять надо тело, а не статус.
        if "code" in data:
            raise FetchError(f"{self.name}/{symbol}: биржа вернула "
                             f"code={data['code']}: {data.get('msg')}")

        try:
            return OrderBook.normalize(
                exchange=self.name,
                symbol=symbol,
                raw_bids=data["bids"],
                raw_asks=data["asks"],
                fetched_at=fetched_at,
                latency_ms=latency_ms,
                # lastUpdateId - версия стакана у биржи. Пригодится, чтобы
                # заметить, что два подряд снимка на самом деле один и тот же.
                source_seq=data.get("lastUpdateId"),
            )
        except KeyError as exc:
            raise FetchError(f"{self.name}/{symbol}: в ответе нет поля "
                             f"{exc}") from exc
        except OrderBookError as exc:
            raise FetchError(f"{self.name}/{symbol}: стакан не прошёл "
                             f"проверку: {exc}") from exc
