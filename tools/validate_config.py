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

Подробный список недостающего с единицами и пояснениями собирает другой
инструмент: tools/apply_fees.py --spec -> docs/values_to_fetch.md.

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


def check_types(cfg: dict) -> list[str]:
    """Все заполненные значения обязаны быть числами.

    Поводом стала реальная ошибка: значение 2e-05, записанное в YAML без
    точки в мантиссе, читается как СТРОКА, без всякого предупреждения.
    Такой конфиг выглядит заполненным, а расчёт по нему даёт мусор.
    Проверка типов ловит это на входе, а не в результатах.
    """
    problems = []

    def check(value, label, expect_int=False):
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{label}: {value!r} - это {type(value).__name__}, "
                            f"а должно быть число")
        elif expect_int and not float(value).is_integer():
            problems.append(f"{label}: {value!r} - число подтверждений "
                            f"должно быть целым")
        elif value < 0:
            problems.append(f"{label}: {value!r} - отрицательное значение")

    for key, ex in cfg["exchanges"].items():
        for field in ("taker_fee", "maker_fee"):
            check(ex.get(field), f"exchanges.{key}.{field}")

    for coin, nets in cfg.get("networks", {}).items():
        for net in nets:
            base = f"networks.{coin}.{net['code']}"
            check(net.get("block_time_sec"), f"{base}.block_time_sec")
            for field in ("withdrawal_fee", "min_withdrawal", "confirmations"):
                for exch, v in (net.get(field) or {}).items():
                    check(v, f"{base}.{field}.{exch}",
                          expect_int=(field == "confirmations"))
    return problems


def check_consistency(cfg: dict) -> list[str]:
    """Проверки на противоречия между числами, а не внутри одного числа.

    Комиссия вычитается из выводимой суммы. Значит минимальный вывод обязан
    быть больше комиссии - иначе при выводе минимума получатель не получит
    ничего, и такая пара чисел не может быть верна одновременно.
    """
    problems = []
    for coin, nets in cfg.get("networks", {}).items():
        for net in nets:
            fees = net.get("withdrawal_fee") or {}
            mins = net.get("min_withdrawal") or {}
            for exch in fees:
                fee, minimum = fees.get(exch), mins.get(exch)
                if fee is None or minimum is None:
                    continue
                if not isinstance(fee, (int, float)) or not isinstance(minimum, (int, float)):
                    continue
                if minimum <= fee:
                    problems.append(
                        f"{coin}/{net['code']}/{exch}: минимальный вывод "
                        f"{minimum} не больше комиссии {fee} - при выводе "
                        f"минимума получатель получит {minimum - fee:.8f} {coin}")
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


def collect_pending(cfg: dict) -> tuple[dict[str, list[str]], int]:
    """Собираем всё, что помечено null, - то есть «в работе».

    Возвращаем и строки отчёта, и честное число незаполненных значений.
    Это разные величины: строка «withdrawal_fee -> binance, bybit, kraken, okx»
    выглядит как один пункт, а стоит за ней четыре числа. Считать строки
    вместо значений - значит втрое занижать объём работы.
    """
    pending: dict[str, list[str]] = {"комиссии": [], "сети вывода": []}
    count = 0

    for key, ex in cfg["exchanges"].items():
        if ex.get("taker_fee") is None:
            pending["комиссии"].append(f"{ex['display_name']}: taker_fee")
            count += 1

    for coin, nets in cfg.get("networks", {}).items():
        for net in nets:
            label = f"{coin}/{net['code']}"
            if net.get("block_time_sec") is None:
                pending["сети вывода"].append(f"{label}: block_time_sec")
                count += 1
            for field in ("withdrawal_fee", "min_withdrawal", "confirmations"):
                missing = [e for e, v in (net.get(field) or {}).items() if v is None]
                if missing:
                    pending["сети вывода"].append(
                        f"{label}: {field} -> {', '.join(sorted(missing))}")
                    count += len(missing)
    return pending, count


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

    print("\n2. Типы значений")
    types = check_types(cfg)
    if types:
        for p in types:
            print(f"  ОШИБКА  {p}")
    else:
        print("  порядок: все заполненные значения - числа")

    print("\n3. Противоречия между числами")
    consistency = check_consistency(cfg)
    if consistency:
        for p in consistency:
            print(f"  ВНИМАНИЕ  {p}")
    else:
        print("  порядок: минимальный вывод везде больше комиссии")

    print("\n4. Сверка чисел с биржей")
    drift, skipped = check_against_exchange(cfg, live)
    for p in drift:
        print(f"  РАСХОЖДЕНИЕ  {p}")
    for p in skipped:
        print(f"  ПРОПУЩЕНО    {p}")
    if not drift:
        n = (len(cfg["exchanges"]) * len(cfg["symbols"]) - len(skipped)) * len(RULE_FIELDS)
        print(f"  порядок: {n} значений совпали со значениями биржи")

    print("\n5. Полнота")
    pending, total_pending = collect_pending(cfg)
    lines_pending = sum(len(v) for v in pending.values())
    if total_pending:
        print(f"  в работе: {total_pending} значений "
              f"в {lines_pending} строках отчёта")
        for group, items in pending.items():
            if not items:
                continue
            print(f"\n  {group}:")
            for it in items:
                print(f"    - {it}")
    else:
        print("  порядок: незаполненных значений нет")

    print("\n" + "=" * 72)
    blocking = structure + types + drift
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
