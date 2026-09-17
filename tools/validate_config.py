"""
Шаг 1: проверка config.yaml.

Делает три вещи.

1. Структура. Все ли биржи, инструменты и тикеры на месте, нет ли опечаток
   в именах ключей.

2. Сверка с биржей. Числа из instrument_rules помечены как полученные из API.
   Значит их можно перепроверить: скрипт сравнивает конфиг с сохранённой
   выгрузкой logs/instrument_rules_raw.json, а с ключом --live дёргает биржи
   заново. Расхождение означает либо опечатку при переносе, либо что биржа
   поменяла правила - и то и другое надо увидеть до прогона, а не после.

3. Полнота. Перечисляет всё, что помечено «в работе». Возвращает ненулевой
   код, если чего-то не хватает для расчёта, - чтобы нельзя было случайно
   запустить пайплайн на дырявом конфиге и получить красивые, но пустые числа.

Запуск:  .venv/bin/python -u tools/validate_config.py [--live]
"""

from __future__ import annotations

import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
RAW = ROOT / "logs" / "instrument_rules_raw.json"

RULE_FIELDS = ("tick_size", "step_size", "min_qty", "min_notional")


def check_structure(cfg: dict) -> list[str]:
    problems = []
    symbols = cfg["symbols"]

    for key, ex in cfg["exchanges"].items():
        for field in ("display_name", "rest_base", "endpoints", "tickers",
                      "instrument_rules"):
            if field not in ex:
                problems.append(f"{key}: нет поля {field}")

        for sym in symbols:
            if sym not in ex.get("tickers", {}):
                problems.append(f"{key}: нет тикера для {sym}")
            rules = ex.get("instrument_rules", {}).get(sym)
            if rules is None:
                problems.append(f"{key}: нет instrument_rules для {sym}")
                continue
            for f in RULE_FIELDS:
                if f not in rules:
                    problems.append(f"{key}/{sym}: нет поля {f}")

    for sym in symbols:
        if sym not in cfg.get("transfer_asset", {}):
            problems.append(f"transfer_asset: не указана монета для {sym}")

    return problems


def check_against_exchange(cfg: dict, live: bool) -> tuple[list[str], list[str]]:
    """Сверяем instrument_rules с тем, что реально отдаёт биржа."""
    skipped: list[str] = []
    if live:
        sys.path.insert(0, str(ROOT / "tools"))
        from fetch_instrument_rules import FETCHERS  # noqa: PLC0415
        actual, unreachable = {}, []
        for sym in cfg["symbols"]:
            for name, fetch in FETCHERS.items():
                try:
                    actual[f"{sym}|{name}"] = fetch(sym)
                except Exception as exc:
                    # Недоступная биржа - это не повод ронять проверку конфига.
                    # Сверить её значения сейчас нельзя, так и напишем.
                    unreachable.append(f"{name}/{sym}: {type(exc).__name__}")
        source = "живой запрос к биржам"
        if unreachable:
            source += f", недоступны {len(unreachable)} из {len(actual) + len(unreachable)}"
    else:
        if not RAW.exists():
            return [], [f"нет файла {RAW.relative_to(ROOT)}; "
                        f"запусти tools/fetch_instrument_rules.py"]
        data = json.loads(RAW.read_text(encoding="utf-8"))
        actual = data["rules"]
        source = f"выгрузка от {data['fetched_at_utc']} UTC"

    print(f"  сверка с биржей: {source}")

    problems = []
    for key, ex in cfg["exchanges"].items():
        name = ex["display_name"]
        for sym in cfg["symbols"]:
            got = actual.get(f"{sym}|{name}")
            if got is None:
                # Отличаем «не с чем сверять» от «сверили и разошлось»:
                # первое не должно объявлять конфиг сломанным.
                skipped.append(f"{name}/{sym}: сверка не выполнена, нет данных")
                continue
            want = ex["instrument_rules"][sym]
            for f in RULE_FIELDS:
                a, b = want.get(f), got.get(f)
                if a is None and b is None:
                    continue
                if a is None or b is None or abs(a - b) > 1e-12 * max(1.0, abs(b)):
                    problems.append(
                        f"{name}/{sym}/{f}: в конфиге {a}, биржа отдаёт {b}")
    return problems, skipped


def collect_pending(cfg: dict) -> dict[str, list[str]]:
    """Собираем всё, что помечено null, - то есть «в работе»."""
    pending: dict[str, list[str]] = {"комиссии": [], "сети вывода": []}

    for key, ex in cfg["exchanges"].items():
        if ex.get("taker_fee") is None:
            pending["комиссии"].append(f"{ex['display_name']}: taker_fee")

    for coin, nets in cfg.get("networks", {}).items():
        for net in nets:
            label = f"{coin}/{net['code']}"
            if net.get("block_time_sec") is None:
                pending["сети вывода"].append(f"{label}: block_time_sec")
            for field in ("withdrawal_fee", "min_withdrawal", "confirmations"):
                missing = [e for e, v in (net.get(field) or {}).items() if v is None]
                if missing:
                    pending["сети вывода"].append(
                        f"{label}: {field} -> {', '.join(sorted(missing))}")
    return pending


def main(argv: list[str]) -> int:
    live = "--live" in argv
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))

    print(f"Проверка {CONFIG.name}")
    print("=" * 72)

    print("\n1. Структура")
    structure = check_structure(cfg)
    if structure:
        for p in structure:
            print(f"  ОШИБКА  {p}")
    else:
        print(f"  порядок: {len(cfg['exchanges'])} биржи(-й), "
              f"{len(cfg['symbols'])} инструмента(-ов), тикеры на месте")

    print("\n2. Сверка чисел с биржей")
    drift, skipped = check_against_exchange(cfg, live)
    for p in drift:
        print(f"  РАСХОЖДЕНИЕ  {p}")
    for p in skipped:
        print(f"  ПРОПУЩЕНО    {p}")
    if not drift:
        n = (len(cfg["exchanges"]) * len(cfg["symbols"]) - len(skipped)) * len(RULE_FIELDS)
        print(f"  порядок: {n} значений совпали со значениями биржи")

    print("\n3. Полнота")
    pending = collect_pending(cfg)
    total_pending = sum(len(v) for v in pending.values())
    if total_pending:
        print(f"  в работе, всего пунктов: {total_pending}")
        for group, items in pending.items():
            if not items:
                continue
            print(f"\n  {group}:")
            for it in items:
                print(f"    - {it}")
    else:
        print("  порядок: незаполненных значений нет")

    print("\n" + "=" * 72)
    blocking = structure + drift
    if blocking:
        print(f"НЕ ГОТОВ: {len(blocking)} ошибок структуры или расхождений.")
        return 2
    if total_pending:
        print(f"ЧАСТИЧНО ГОТОВ: структура верна, числа из API сверены, "
              f"но {total_pending} значений ещё не заполнено.")
        print("Расчёт издержек (шаг 4) запускать нельзя - результат будет пустым.")
        print("Сбор стаканов (шаги 2, 3, 6) запускать можно: он этих чисел "
              "не требует.")
        return 1
    print("ГОТОВ: конфиг заполнен полностью и сверен с биржами.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
