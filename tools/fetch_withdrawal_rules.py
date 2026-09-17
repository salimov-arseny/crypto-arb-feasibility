"""
Шаг 1: выгрузка параметров вывода из публичных эндпоинтов бирж.

Тарифные страницы бирж не читаются программой: таблицы на них рисует браузер.
Но данные он откуда-то берёт - у части площадок есть публичный JSON-эндпоинт
без авторизации, тот самый, которым пользуется их собственная страница.

Что удалось найти на 2026-09-17:

  Binance  https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll
           отдаёт по каждой монете список сетей с комиссией вывода,
           минимальной суммой и числом подтверждений. Без ключа, 965 монет.
           Эндпоинт не описан в публичной документации: это внутренний
           интерфейс их же сайта. Значит, он может измениться без
           предупреждения - выгрузку надо повторять перед каждым прогоном,
           а не считать вечной.

  OKX      /api/v5/asset/currencies требует ключ (HTTP 401),
           /v2/asset/currency/currencies не существует (HTTP 404).
  Bybit    /v3/public/coin-info/query не существует (HTTP 404).
  Kraken   публичного эндпоинта с комиссиями вывода не найдено.

Результат пишется в формате data/fees_input.yaml, чтобы его вносил
тот же tools/apply_fees.py - с проверкой источника и даты.

Запуск:  .venv/bin/python -u tools/fetch_withdrawal_rules.py
         .venv/bin/python -u tools/apply_fees.py --input data/fees_from_api.yaml
"""

from __future__ import annotations

import pathlib
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "fees_from_api.yaml"

BINANCE_URL = ("https://www.binance.com/bapi/capital/v1/public/capital/"
               "getNetworkCoinAll")

# Какие сети нас интересуют: (монета в конфиге, код сети в конфиге,
# имя сети у Binance).
WANTED = [
    ("BTC", "BTC", "BTC"),
    ("ETH", "ERC20", "ETH"),
    ("ETH", "ARBITRUM", "ARBITRUM"),
    ("SOL", "SOL", "SOL"),
]

# Соответствие полей: наше имя -> имя у Binance.
#   withdrawFee - комиссия вывода в монете
#   withdrawMin - минимальная сумма вывода в монете
#   minConfirm  - подтверждений сети для зачисления депозита
# Есть ещё unLockConfirm - подтверждения для разблокировки средств
# под вывод; нам нужно не оно.
FIELDS = {
    "withdrawal_fee": "withdrawFee",
    "min_withdrawal": "withdrawMin",
    "confirmations": "minConfirm",
}


def fetch_binance() -> list[dict]:
    resp = requests.get(BINANCE_URL, timeout=60,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != "000000":
        raise RuntimeError(f"Binance вернул code={payload.get('code')}")

    by_coin = {c["coin"]: c for c in payload["data"]}
    today = time.strftime("%Y-%m-%d", time.gmtime())
    rows = []

    for coin, code, binance_net in WANTED:
        entry = by_coin.get(coin)
        if entry is None:
            print(f"  ПРОПУСК {coin}: нет такой монеты в ответе")
            continue
        net = next((n for n in entry["networkList"]
                    if n["network"] == binance_net), None)
        if net is None:
            print(f"  ПРОПУСК {coin}/{code}: нет сети {binance_net}")
            continue

        # Если вывод по сети отключён, числа брать нельзя: они не применятся.
        if not net.get("withdrawEnable", False):
            print(f"  ПРОПУСК {coin}/{code}: вывод отключён биржей")
            continue

        for our_field, their_field in FIELDS.items():
            value = net.get(their_field)
            if value is None:
                print(f"  ПРОПУСК {coin}/{code}/{our_field}: поле пустое")
                continue
            value = int(value) if our_field == "confirmations" else float(value)
            rows.append({
                "path": f"networks.{coin}.{code}.{our_field}.binance",
                "value": value,
                "source": BINANCE_URL,
                "checked": today,
                "comment": f"{coin} в сети {net.get('name', binance_net)}",
            })
    return rows


def main() -> int:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"Выгрузка параметров вывода. Время (UTC): {stamp}\n")

    try:
        rows = fetch_binance()
    except Exception as exc:
        print(f"Binance: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    for r in rows:
        print(f"  {r['path']:<46} = {r['value']}")

    lines = [
        "# Значения, выгруженные из публичных эндпоинтов бирж.",
        "# Файл создаётся tools/fetch_withdrawal_rules.py - не править руками.",
        f"# Выгружено: {stamp} UTC",
        "#",
        "# Вносится тем же инструментом, что и значения, собранные вручную:",
        "#   .venv/bin/python -u tools/apply_fees.py --input data/fees_from_api.yaml",
        "",
        "values:",
    ]
    for r in rows:
        lines += [
            f"  # {r['comment']}",
            f"  - path: {r['path']}",
            f"    value: {r['value']}",
            f"    source: {r['source']}",
            f"    checked: {r['checked']}",
            "    account: false",
            "",
        ]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nЗначений выгружено: {len(rows)}. Записано в {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
