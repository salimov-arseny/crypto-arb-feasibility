"""
Шаг 1: измерение фактического времени блока сетей вывода.

Время блока нужно, чтобы оценить длительность перевода между биржами:

    время перевода = требуемое число подтверждений x время блока

Число подтверждений - политика биржи, его придётся смотреть глазами.
А время блока - свойство сети, и его не обязательно искать в документации:
у публичных узлов можно спросить саму цепочку. Так надёжнее, чем ссылка:
значение получено прогоном, с меткой времени, и воспроизводится повторно.

Меряем среднее по последним N блокам, а не по двум соседним: время блока
случайно, особенно у Bitcoin, где интервалы распределены показательно
и отдельный блок может прийти и через минуту, и через час.

Запуск:  .venv/bin/python -u tools/measure_block_times.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time

import requests

TIMEOUT = 20.0
SAMPLE_BLOCKS = 200          # по скольким блокам усредняем


def rpc(url: str, method: str, params: list | None = None) -> dict:
    resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                    "method": method, "params": params or []},
                         timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error']}")
    return data["result"]


def evm_block_time(url: str, n: int = SAMPLE_BLOCKS) -> dict:
    """Среднее время блока в сети, совместимой с Ethereum."""
    head = int(rpc(url, "eth_blockNumber"), 16)
    newest = rpc(url, "eth_getBlockByNumber", [hex(head), False])
    oldest = rpc(url, "eth_getBlockByNumber", [hex(head - n), False])
    span = int(newest["timestamp"], 16) - int(oldest["timestamp"], 16)
    return {
        "mean_sec": span / n,
        "blocks": n,
        "head": head,
        "method": "eth_getBlockByNumber, разница меток времени",
    }


def solana_block_time() -> dict:
    """Solana отдаёт готовые замеры производительности: слотов за период."""
    url = "https://api.mainnet-beta.solana.com"
    samples = rpc(url, "getRecentPerformanceSamples", [20])
    # Каждый образец: сколько слотов прошло за samplePeriodSecs секунд.
    per_slot = [s["samplePeriodSecs"] / s["numSlots"]
                for s in samples if s["numSlots"]]
    return {
        "mean_sec": statistics.mean(per_slot),
        "median_sec": statistics.median(per_slot),
        "blocks": sum(s["numSlots"] for s in samples),
        "method": "getRecentPerformanceSamples, 20 образцов",
    }


def main() -> int:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"Фактическое время блока. Время запуска (UTC): {stamp}\n")

    targets = [
        ("Solana", "SOL", solana_block_time, None),
        ("Arbitrum One", "ARBITRUM", evm_block_time, "https://arb1.arbitrum.io/rpc"),
        ("Ethereum", "ERC20", evm_block_time, "https://ethereum-rpc.publicnode.com"),
    ]

    out = {"measured_at_utc": stamp, "networks": {}}
    for name, code, fn, url in targets:
        try:
            r = fn(url) if url else fn()
        except Exception as exc:
            print(f"{name:<14} FAIL: {type(exc).__name__}: {str(exc)[:90]}")
            continue
        out["networks"][code] = r
        extra = (f", медиана {r['median_sec']:.4f} с" if "median_sec" in r else "")
        print(f"{name:<14} {r['mean_sec']:.4f} с{extra}")
        print(f"{'':<14} по {r['blocks']:,} блокам; {r['method']}")

    # Bitcoin меряем иначе: целевой интервал задан протоколом и равен
    # 1 209 600 с / 2 016 блоков = 600 с. Фактический средний интервал
    # колеблется вокруг этого значения, потому что сложность
    # пересчитывается раз в 2 016 блоков именно ради удержания цели.
    out["networks"]["BTC"] = {
        "mean_sec": 1_209_600 / 2016,
        "blocks": 2016,
        "method": "целевой интервал протокола: 1 209 600 с на 2 016 блоков",
    }
    print(f"{'Bitcoin':<14} {1_209_600 / 2016:.4f} с (целевой интервал протокола)")

    path = "logs/block_times_raw.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"\nСохранено в {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
