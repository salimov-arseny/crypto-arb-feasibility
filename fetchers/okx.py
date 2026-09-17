"""Фетчер стакана OKX: /api/v5/market/books.

Две особенности. Код успеха приходит СТРОКОЙ "0", а не числом - сравнение
с нулём здесь не сработает. И сам стакан завёрнут в массив data, хотя
инструмент запрашивается один.

Уровень четырёхэлементный: [цена, объём, ликвидации, число ордеров].
Лишнее отбрасывает нормализация.
"""

from __future__ import annotations

from typing import Any

from fetchers.base import FetchError, ParsedBook, RestFetcher, ms_to_unix


class OkxFetcher(RestFetcher):
    name = "OKX"

    def _params(self, ticker: str) -> dict[str, Any]:
        return {"instId": ticker, "sz": self.depth}

    def _parse(self, data: Any) -> ParsedBook:
        # Именно "0" строкой: data.get("code") != 0 пропустило бы ошибку.
        if data.get("code") != "0":
            raise FetchError(f"{self.name}: code={data.get('code')}: "
                             f"{data.get('msg')}")
        books = data.get("data") or []
        if not books:
            raise FetchError(f"{self.name}: массив data пуст")
        book = books[0]
        if "bids" not in book or "asks" not in book:
            raise FetchError(f"{self.name}: в data[0] нет сторон: {sorted(book)}")
        return ParsedBook(
            bids=book["bids"], asks=book["asks"],
            source_seq=book.get("seqId"),
            exchange_ts=ms_to_unix(book.get("ts")))
