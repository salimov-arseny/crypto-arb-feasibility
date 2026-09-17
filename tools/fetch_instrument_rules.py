"""
Шаг 1: выгрузка правил инструментов из публичных эндпоинтов метаданных.

Достаём то, что биржа сообщает о самом инструменте: шаг цены, шаг объёма,
минимальный лот, минимальную сумму ордера. Эти числа не вычисляются из стакана
и не берутся из головы - их публикует сама площадка, поэтому источником
считается эндпоинт, а датой - время запроса.

Отдельно: Kraken - единственная из четырёх бирж, кто публикует торговую
комиссию без авторизации, прямо в /0/public/AssetPairs. У остальных трёх
комиссия лежит за приватным эндпоинтом, и здесь мы её не получим.

Запуск:  .venv/bin/python -u tools/fetch_instrument_rules.py
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import requests

TIMEOUT_SEC = 15.0

# Тикеры берём из той же таблицы, что и на шаге 0, чтобы источник был один.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from check_endpoints import SYMBOLS  # noqa: E402


def get(url: str, params: dict[str, Any]) -> Any:
    resp = requests.get(url, params=params, timeout=TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json()


def binance_rules(symbol: str) -> dict[str, Any]:
    ticker = SYMBOLS[symbol]["Binance"]
    data = get("https://api.binance.com/api/v3/exchangeInfo", {"symbol": ticker})
    info = data["symbols"][0]
    # Правила лежат в списке «фильтров», каждый со своим типом.
    f = {flt["filterType"]: flt for flt in info["filters"]}
    return {
        "tick_size": float(f["PRICE_FILTER"]["tickSize"]),
        "step_size": float(f["LOT_SIZE"]["stepSize"]),
        "min_qty": float(f["LOT_SIZE"]["minQty"]),
        "min_notional": float(f["NOTIONAL"]["minNotional"]),
        "taker_fee": None,          # только за авторизацией
        "source": "GET /api/v3/exchangeInfo",
    }


def bybit_rules(symbol: str) -> dict[str, Any]:
    ticker = SYMBOLS[symbol]["Bybit"]
    data = get("https://api.bybit.com/v5/market/instruments-info",
               {"category": "spot", "symbol": ticker})
    info = data["result"]["list"][0]
    return {
        "tick_size": float(info["priceFilter"]["tickSize"]),
        "step_size": float(info["lotSizeFilter"]["basePrecision"]),
        "min_qty": float(info["lotSizeFilter"]["minOrderQty"]),
        "min_notional": float(info["lotSizeFilter"]["minOrderAmt"]),
        "taker_fee": None,
        "source": "GET /v5/market/instruments-info?category=spot",
    }


def okx_rules(symbol: str) -> dict[str, Any]:
    ticker = SYMBOLS[symbol]["OKX"]
    data = get("https://www.okx.com/api/v5/public/instruments",
               {"instType": "SPOT", "instId": ticker})
    info = data["data"][0]
    return {
        "tick_size": float(info["tickSz"]),
        "step_size": float(info["lotSz"]),
        "min_qty": float(info["minSz"]),
        "min_notional": None,       # OKX не публикует минимум в котируемой валюте
        "taker_fee": None,
        "source": "GET /api/v5/public/instruments?instType=SPOT",
    }


def kraken_rules(symbol: str) -> dict[str, Any]:
    ticker = SYMBOLS[symbol]["Kraken"]
    data = get("https://api.kraken.com/0/public/AssetPairs", {"pair": ticker})
    if data.get("error"):
        raise RuntimeError(f"Kraken: {data['error']}")
    info = next(iter(data["result"].values()))
    # У Kraken есть поля fees и fees_maker - лестница объёмных скидок вида
    # [объём за 30 дней, процент]. Выглядит как публичная комиссия, но на
    # 2026-09-17 они пусты у всех 1450 пар, проверено перебором. То есть
    # ни одна из четырёх бирж торговую комиссию без ключей не отдаёт.
    return {
        "tick_size": float(info["tick_size"]),
        "step_size": 10 ** (-int(info["lot_decimals"])),
        "min_qty": float(info["ordermin"]),
        "min_notional": float(info["costmin"]),
        "taker_fee": None,
        "source": "GET /0/public/AssetPairs",
    }


FETCHERS = {
    "Binance": binance_rules,
    "Bybit": bybit_rules,
    "OKX": okx_rules,
    "Kraken": kraken_rules,
}


def fmt(x: float | None, width: int = 12) -> str:
    if x is None:
        return "—".rjust(width)
    # Шаги бывают очень мелкие (1e-8), обычный формат их съедает.
    return (f"{x:.8f}".rstrip("0").rstrip(".") if x < 1 else f"{x:,.2f}").rjust(width)


def main() -> int:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"Правила инструментов из публичных эндпоинтов. Время (UTC): {stamp}")

    collected: dict[str, dict[str, Any]] = {}

    for symbol in SYMBOLS:
        print(f"\n### {symbol}")
        print("-" * 96)
        print(f"{'биржа':<9} {'шаг цены':>12} {'шаг объёма':>12} "
              f"{'мин. лот':>12} {'мин. сумма':>12} {'тейкер':>10}   источник")
        print("-" * 96)

        for exchange, fetcher in FETCHERS.items():
            try:
                r = fetcher(symbol)
            except Exception as exc:
                print(f"{exchange:<9} FAIL: {type(exc).__name__}: {exc}")
                continue

            collected[f"{symbol}|{exchange}"] = r
            taker = f"{r['taker_fee'] * 100:.4f} %".rjust(10) if r["taker_fee"] else "—".rjust(10)
            print(f"{exchange:<9} {fmt(r['tick_size'])} {fmt(r['step_size'])} "
                  f"{fmt(r['min_qty'])} {fmt(r['min_notional'])} {taker}   {r['source']}")

    print("\nЧего здесь нет и почему:")
    print("  - комиссия тейкера: ни одна из четырёх бирж не отдаёт её без")
    print("    авторизации (/sapi/v1/asset/tradeFee и аналоги). У Kraken поля")
    print("    fees/fees_maker в AssetPairs существуют, но пусты у всех пар;")
    print("  - стоимость вывода и время подтверждения: тоже за авторизацией")
    print("    (/sapi/v1/capital/config/getall, /api/v5/asset/currencies).")
    print("  Эти числа берём из документации вручную, со ссылкой и датой.")

    out = "logs/instrument_rules_raw.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at_utc": stamp, "rules": collected}, fh,
                  ensure_ascii=False, indent=2)
    print(f"\nСырые значения сохранены в {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
