"""Общий интерфейс фетчеров и их общая часть.

Каждая биржа получает свой модуль, но наружу все они выглядят одинаково:
дай стакан по инструменту - получи OrderBook. Всё, что специфично для
площадки, остаётся внутри модуля.

Общего у четырёх фетчеров больше, чем различного: запрос, замер задержки,
обработка сетевых сбоев, фиксация момента снимка - одно и то же. Различий
ровно два: какие параметры уходят в запрос и как разобрать ответ. Поэтому
общая часть живёт здесь, а модуль биржи отвечает на два вопроса.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from core.orderbook import OrderBook, OrderBookError


class FetchError(RuntimeError):
    """Биржа не отдала пригодный стакан.

    Отдельный тип нужен, чтобы сборщик на шаге 6 мог отличить «эта площадка
    сейчас не отвечает» от ошибки в нашем коде. Первое - штатная ситуация
    многочасового прогона, второе - повод остановиться.
    """


class Fetcher(Protocol):
    """Что обязан уметь фетчер любой биржи."""

    name: str

    def fetch(self, symbol: str) -> OrderBook:
        """Вернуть нормализованный стакан или возбудить FetchError."""
        ...


@dataclass(frozen=True)
class ParsedBook:
    """Что модуль биржи достаёт из ответа. Сырые уровни, ещё не нормализованные."""

    bids: list
    asks: list
    source_seq: int | None = None
    exchange_ts: float | None = None   # unix-время UTC, если биржа его сообщает


class RestFetcher(ABC):
    """Общая часть: запрос, тайминг, ошибки транспорта."""

    name: str = "?"

    def __init__(self, rest_base: str, endpoint: str, tickers: dict[str, str],
                 depth: int = 100, timeout: float = 10.0,
                 session: requests.Session | None = None) -> None:
        self.rest_base = rest_base.rstrip("/")
        self.endpoint = endpoint
        self.tickers = tickers
        self.depth = depth
        self.timeout = timeout
        # Одна сессия на фетчер: переиспользование TCP-соединения заметно
        # сокращает задержку при многочасовом сборе.
        self._session = session or requests.Session()

    # ---- то, что определяет каждая биржа --------------------------------

    @abstractmethod
    def _params(self, ticker: str) -> dict[str, Any]:
        """Параметры запроса. Имя параметра глубины у всех своё."""

    @abstractmethod
    def _parse(self, data: Any) -> ParsedBook:
        """Достать уровни из ответа или возбудить FetchError.

        Здесь же проверяется код ошибки биржи. Все четыре площадки
        сообщают об ошибке в теле ответа, оставляя HTTP-статус 200,
        поэтому проверка статуса ничего не гарантирует.
        """

    # ---- общая часть ------------------------------------------------------

    def fetch(self, symbol: str) -> OrderBook:
        ticker = self.tickers.get(symbol)
        if ticker is None:
            raise FetchError(f"{self.name}: нет тикера для {symbol}")

        url = f"{self.rest_base}{self.endpoint}"
        started = time.perf_counter()
        try:
            resp = self._session.get(url, params=self._params(ticker),
                                     timeout=self.timeout)
        except requests.RequestException as exc:
            raise FetchError(f"{self.name}/{symbol}: запрос не дошёл: "
                             f"{type(exc).__name__}") from exc
        latency_ms = (time.perf_counter() - started) * 1000

        # Момент снимка по нашим часам. Фиксируем всегда, даже когда биржа
        # сообщает своё время: на шаге 6 по этим меткам отбраковываются
        # наблюдения, где стаканы разных площадок разъехались.
        fetched_at = time.time()

        if resp.status_code != 200:
            raise FetchError(f"{self.name}/{symbol}: HTTP {resp.status_code}: "
                             f"{resp.text[:120]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise FetchError(f"{self.name}/{symbol}: ответ не JSON: "
                             f"{resp.text[:120]}") from exc

        parsed = self._parse(data)

        try:
            return OrderBook.normalize(
                exchange=self.name, symbol=symbol,
                raw_bids=parsed.bids, raw_asks=parsed.asks,
                fetched_at=fetched_at, latency_ms=latency_ms,
                source_seq=parsed.source_seq, exchange_ts=parsed.exchange_ts)
        except OrderBookError as exc:
            raise FetchError(f"{self.name}/{symbol}: стакан не прошёл "
                             f"проверку: {exc}") from exc


def ms_to_unix(value: Any) -> float | None:
    """Миллисекунды с эпохи -> секунды. Биржи шлют их и числом, и строкой."""
    if value in (None, ""):
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None
