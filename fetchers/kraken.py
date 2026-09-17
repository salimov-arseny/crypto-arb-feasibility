"""Фетчер стакана Kraken: /0/public/Depth.

Самый своенравный формат из четырёх.

Ошибку Kraken кладёт в массив error, оставляя HTTP 200 и пустой result -
проверять надо непустоту error.

Стакан лежит не прямо в result, а под ключом с ВНУТРЕННИМ именем пары,
которое не обязано совпадать с запрошенным: просим XBTUSDT, а ключом
может оказаться, например, XXBTZUSD. Поэтому берём первый ключ результата,
а не подставляем свой тикер.

Уровень трёхэлементный: [цена, объём, время]. Время относится к уровню,
а не к снимку целиком, поэтому как время стакана оно не годится - своего
времени снимка Kraken не сообщает.
"""

from __future__ import annotations

from typing import Any

from fetchers.base import FetchError, ParsedBook, RestFetcher


class KrakenFetcher(RestFetcher):
    name = "Kraken"

    def _params(self, ticker: str) -> dict[str, Any]:
        return {"pair": ticker, "count": self.depth}

    def _parse(self, data: Any) -> ParsedBook:
        if data.get("error"):
            raise FetchError(f"{self.name}: error={data['error']}")
        result = data.get("result") or {}
        if not result:
            raise FetchError(f"{self.name}: пустой result")
        # Ключ - внутреннее имя пары. Берём первый, а не наш тикер.
        book = next(iter(result.values()))
        if "bids" not in book or "asks" not in book:
            raise FetchError(f"{self.name}: в стакане нет сторон: {sorted(book)}")
        return ParsedBook(
            bids=book["bids"], asks=book["asks"],
            source_seq=None,          # версии стакана Kraken не сообщает
            exchange_ts=None)         # времени снимка тоже
