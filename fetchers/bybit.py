"""Фетчер стакана Bybit: /v5/market/orderbook?category=spot.

Уровни лежат под однобуквенными именами: b - биды, a - аски. Из четырёх
площадок Bybit сообщает о себе больше всех: своё время снимка в result.ts
(миллисекунды), версию стакана в result.u и порядковый номер в result.seq.
"""

from __future__ import annotations

from typing import Any

from fetchers.base import FetchError, ParsedBook, RestFetcher, ms_to_unix


class BybitFetcher(RestFetcher):
    name = "Bybit"

    def _params(self, ticker: str) -> dict[str, Any]:
        return {"category": "spot", "symbol": ticker, "limit": self.depth}

    def _parse(self, data: Any) -> ParsedBook:
        # Успех - это retCode == 0, а не HTTP 200.
        if data.get("retCode") != 0:
            raise FetchError(f"{self.name}: retCode={data.get('retCode')}: "
                             f"{data.get('retMsg')}")
        result = data.get("result") or {}
        if "b" not in result or "a" not in result:
            raise FetchError(f"{self.name}: в result нет сторон b и a: "
                             f"{sorted(result)}")
        return ParsedBook(
            bids=result["b"], asks=result["a"],
            source_seq=result.get("u"),
            exchange_ts=ms_to_unix(result.get("ts")))
