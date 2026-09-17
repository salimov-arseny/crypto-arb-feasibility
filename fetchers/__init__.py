"""Сборка фетчеров по конфигу.

Одно место, где имя биржи из config.yaml превращается в объект. Если
площадка появится или исчезнет, правится только REGISTRY.
"""

from __future__ import annotations

from typing import Any

from fetchers.base import FetchError, Fetcher, ParsedBook, RestFetcher
from fetchers.binance import BinanceFetcher
from fetchers.bybit import BybitFetcher
from fetchers.kraken import KrakenFetcher
from fetchers.okx import OkxFetcher

REGISTRY: dict[str, type[RestFetcher]] = {
    "binance": BinanceFetcher,
    "bybit": BybitFetcher,
    "okx": OkxFetcher,
    "kraken": KrakenFetcher,
}

__all__ = ["FetchError", "Fetcher", "ParsedBook", "RestFetcher", "REGISTRY",
           "BinanceFetcher", "BybitFetcher", "OkxFetcher", "KrakenFetcher",
           "build_all"]


def build_all(cfg: dict[str, Any]) -> dict[str, RestFetcher]:
    """Собрать фетчеры всех бирж, перечисленных в конфиге."""
    depth = cfg["meta"]["orderbook_depth"]
    timeout = cfg["scan"]["timeout_sec"]
    built: dict[str, RestFetcher] = {}
    for key, ex in cfg["exchanges"].items():
        cls = REGISTRY.get(key)
        if cls is None:
            raise KeyError(f"нет фетчера для биржи {key!r}; "
                           f"известны: {sorted(REGISTRY)}")
        built[key] = cls(rest_base=ex["rest_base"],
                         endpoint=ex["endpoints"]["orderbook"],
                         tickers=ex["tickers"], depth=depth, timeout=timeout)
    return built
