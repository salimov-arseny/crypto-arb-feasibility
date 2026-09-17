"""Фетчер стакана Binance: /api/v3/depth.

Самый простой формат из четырёх: уровни лежат прямо в bids и asks парами
[цена, объём]. Своего времени снимка не сообщает - только lastUpdateId,
версию стакана.
"""

from __future__ import annotations

from typing import Any

from fetchers.base import FetchError, ParsedBook, RestFetcher


class BinanceFetcher(RestFetcher):
    name = "Binance"

    def _params(self, ticker: str) -> dict[str, Any]:
        return {"symbol": ticker, "limit": self.depth}

    def _parse(self, data: Any) -> ParsedBook:
        # Об ошибке Binance сообщает полем code в теле, оставляя статус 200.
        if "code" in data:
            raise FetchError(f"{self.name}: биржа вернула code={data['code']}: "
                             f"{data.get('msg')}")
        try:
            return ParsedBook(
                bids=data["bids"], asks=data["asks"],
                source_seq=data.get("lastUpdateId"),
                # Времени биржи в ответе нет: остаётся момент получения.
                exchange_ts=None)
        except KeyError as exc:
            raise FetchError(f"{self.name}: в ответе нет поля {exc}") from exc
