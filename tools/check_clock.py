"""Сверка локальных часов с часами бирж.

Зачем. На шаге 6 наблюдения отбраковываются по разбросу времени между
снимками разных площадок, и допуск там - около секунды. Если наши часы
сдвинуты относительно биржевых на сопоставимую величину, отбраковка
начнёт врать.

Тонкость, ради которой этот файл и появился. Сдвиг часов сокращается
сам собой, если сравнивать между собой две НАШИ метки времени: обе
сдвинуты одинаково. Но если сравнить время, сообщённое биржей, с нашим
моментом получения, сдвиг войдёт в результат целиком. А сообщают своё
время не все: Bybit и OKX сообщают, Binance и Kraken - нет. Значит
смешивать источники времени нельзя, и это ограничение надо знать
числом, а не на словах.

Метод. Засекаем время до и после запроса, а время биржи сравниваем
с серединой интервала. Так задержка сети в оценку не входит - при
условии, что запрос и ответ идут примерно одинаково долго.

Запуск:  .venv/bin/python -u tools/check_clock.py
"""

from __future__ import annotations

import statistics
import sys
import time

import requests

TIMEOUT = 15.0
HEADERS = {"User-Agent": "Mozilla/5.0"}

SOURCES = [
    ("Binance", "https://api.binance.com/api/v3/time",
     lambda d: d["serverTime"] / 1000),
    ("Bybit", "https://api.bybit.com/v5/market/time",
     lambda d: int(d["result"]["timeNano"]) / 1e9),
    ("OKX", "https://www.okx.com/api/v5/public/time",
     lambda d: float(d["data"][0]["ts"]) / 1000),
    # Kraken отдаёт время целыми секундами, поэтому его оценка грубее
    # остальных примерно на полсекунды - это разрешение, а не расхождение.
    ("Kraken", "https://api.kraken.com/0/public/Time",
     lambda d: float(d["result"]["unixtime"])),
]


def main() -> int:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"Сверка часов. Время запуска (UTC): {stamp}")
    print("Положительный сдвиг означает, что часы биржи впереди наших.\n")

    offsets: list[tuple[str, float]] = []
    for name, url, extract in SOURCES:
        try:
            before = time.time()
            resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
            after = time.time()
            resp.raise_for_status()
            server = extract(resp.json())
        except Exception as exc:
            print(f"  {name:<8} {type(exc).__name__}: {str(exc)[:60]}")
            continue
        offset = server - (before + after) / 2
        offsets.append((name, offset))
        print(f"  {name:<8} {offset * 1000:+8.0f} мс "
              f"(запрос шёл {(after - before) * 1000:.0f} мс)")

    if len(offsets) < 2:
        print("\nМало источников, вывод делать не на чем.")
        return 1

    values = [v for _, v in offsets]
    mean = statistics.mean(values)
    print(f"\n  Средний сдвиг наших часов: {mean * 1000:+.0f} мс")
    print(f"  Разброс оценок между биржами: {(max(values) - min(values)) * 1000:.0f} мс")

    print("\nЧто из этого следует для шага 6:")
    print("  - разброс между снимками, посчитанный по НАШИМ меткам времени,")
    print("    от сдвига часов не зависит: он сокращается;")
    print(f"  - сравнение времени биржи с нашим даёт ошибку около "
          f"{abs(mean) * 1000:.0f} мс,")
    print("    поэтому источники времени смешивать нельзя.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
