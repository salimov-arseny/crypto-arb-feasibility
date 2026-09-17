"""
Шаг 1: перенос комиссий и параметров вывода в config.yaml.

Зачем отдельный инструмент. Числа в конфиге ценны не сами по себе, а вместе
с пометкой, откуда они взяты и когда. Руками это переносить - значит рано или
поздно вписать значение без источника или ошибиться цифрой. Скрипт переносит
значение и источник вместе, одним действием, и отказывается брать значение
без источника.

Работает в два приёма.

  --template  собирает data/fees_input.yaml: по записи на каждое незаполненное
              поле конфига. Поле value пустое, его заполняешь ты. Поле source
              заранее заполнено ссылками из fees.md, если для этого поля они
              там нашлись.

  (без ключа) читает data/fees_input.yaml и вносит заполненные значения
              в config.yaml. Комментарии конфига при этом сохраняются -
              правка идёт через round-trip YAML, а не через пересборку файла.
              К каждому внесённому значению дописывается комментарий
              с источником и датой.

Запуск:  .venv/bin/python -u tools/apply_fees.py --template
         .venv/bin/python -u tools/apply_fees.py [--dry-run]
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
import sys

from ruamel.yaml import YAML

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
FEES_MD = ROOT / "fees.md"
INPUT = ROOT / "data" / "fees_input.yaml"

yaml_rt = YAML()                 # round-trip: сохраняет комментарии и порядок
yaml_rt.preserve_quotes = True
yaml_rt.width = 4096             # иначе длинные комментарии переносятся

# Три настройки ниже нужны, чтобы правка одного значения давала diff в одну
# строку, а не переписывала файл целиком. Без них ruamel перевыравнивает
# списки и заменяет null на пустоту: изменения тонут в шуме форматирования,
# и в истории потом не видно, какое число когда появилось.
yaml_rt.indent(mapping=2, sequence=4, offset=2)


def _represent_none(self, data):
    """Пишем null явно. Пустое значение в YAML означает то же самое,
    но читать конфиг, где половина полей выглядит забытыми, невозможно."""
    return self.represent_scalar("tag:yaml.org,2002:null", "null")


yaml_rt.representer.add_representer(type(None), _represent_none)


# --------------------------------------------------------------------------
#  Ссылки из fees.md
# --------------------------------------------------------------------------

def parse_fees_md() -> dict[str, list[str]]:
    """Собираем ссылки по разделам fees.md.

    Файл содержит не значения, а указания, где смотреть. Раскладываем его
    на разделы по заголовкам и собираем из каждого адреса - чтобы подставить
    их в заготовку как кандидатов в источники. Какой именно адрес дал число,
    решает человек: подставлять это автоматически было бы выдумыванием.
    """
    if not FEES_MD.exists():
        return {}
    sections: dict[str, list[str]] = {}
    current = "начало"
    for line in FEES_MD.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^#{3,4}\s+(.*)$", line.strip())
        if heading:
            current = re.sub(r"[*_`]", "", heading.group(1)).strip()
            sections.setdefault(current, [])
            continue
        for url in re.findall(r"https?://[^\s<>()\[\],]+", line):
            sections.setdefault(current, []).append(url.rstrip(".,"))
    return {k: list(dict.fromkeys(v)) for k, v in sections.items() if v}


def hints_for(exchange: str, field: str, md: dict[str, list[str]]) -> list[str]:
    """Подбираем ссылки из fees.md, относящиеся к бирже и типу значения."""
    urls: list[str] = []
    want_fee = field == "taker_fee"
    for title, links in md.items():
        low = title.lower()
        if exchange.lower() not in low and not (want_fee and "комисс" in low):
            continue
        urls += links
    # Отбрасываем чужие домены: в разделе про комиссии тейкера лежат
    # ссылки сразу на все биржи.
    host = {"binance": "binance.com", "bybit": "bybit.com",
            "okx": "okx.com", "kraken": "kraken.com"}[exchange]
    return [u for u in dict.fromkeys(urls) if host in u]


# --------------------------------------------------------------------------
#  Заготовка
# --------------------------------------------------------------------------

HEADER = """\
# Значения для переноса в config.yaml.
#
# Этот файл заполняешь ты, скрипт только переносит. Правила простые.
#
#   value    - число, ровно как показано на странице. Комиссия тейкера
#              записывается долей: 0,1 % -> 0.001. Комиссия и минимум
#              вывода - в самой монете, не в USDT. Подтверждения - целое.
#   source   - адрес страницы, с которой списано. Одна ссылка, не список.
#              В source_candidates лежат адреса из fees.md - можно взять
#              оттуда, если смотрел именно там.
#   checked  - дата просмотра, ГГГГ-ММ-ДД.
#   account  - true, если значение относится к твоему аккаунту, а не
#              к базовому тарифу (например, действует скидка).
#
# Запись без source или без checked не переносится: значение без
# происхождения в конфиге бесполезно.
#
# Перенести:  .venv/bin/python -u tools/apply_fees.py
"""


def build_template(cfg) -> str:
    md = parse_fees_md()
    rows: list[str] = []

    rows.append("values:")

    for key, ex in cfg["exchanges"].items():
        if ex.get("taker_fee") is not None:
            continue
        rows += _entry(f"exchanges.{key}.taker_fee",
                       f"{ex['display_name']}: комиссия тейкера, долей",
                       hints_for(key, "taker_fee", md))

    for coin, nets in cfg.get("networks", {}).items():
        for net in nets:
            code = net["code"]
            for field, unit in (("withdrawal_fee", f"в {coin}"),
                                ("min_withdrawal", f"в {coin}"),
                                ("confirmations", "целое число блоков")):
                for exch, val in sorted((net.get(field) or {}).items()):
                    if val is not None:
                        continue
                    rows += _entry(
                        f"networks.{coin}.{code}.{field}.{exch}",
                        f"{coin}/{code}, {exch}: {field}, {unit}",
                        hints_for(exch, field, md))

    return HEADER + "\n" + "\n".join(rows) + "\n"


def _entry(path: str, comment: str, candidates: list[str]) -> list[str]:
    lines = [f"  # {comment}",
             f"  - path: {path}",
             "    value: null",
             "    source: null",
             "    checked: null",
             "    account: false"]
    if candidates:
        lines.append("    source_candidates:")
        lines += [f"      - {u}" for u in candidates]
    lines.append("")
    return lines


# --------------------------------------------------------------------------
#  Перенос
# --------------------------------------------------------------------------

def resolve(cfg, path: str):
    """Находим место в конфиге по пути вида networks.BTC.BTC.confirmations.binance.

    Возвращаем (контейнер, ключ), чтобы вызывающий мог и прочитать, и записать.
    """
    parts = path.split(".")
    if parts[0] == "exchanges":
        _, exch, field = parts
        return cfg["exchanges"][exch], field
    if parts[0] == "networks":
        _, coin, code, field, exch = parts
        for net in cfg["networks"][coin]:
            if net["code"] == code:
                return net[field], exch
        raise KeyError(f"нет сети {code} у {coin}")
    raise KeyError(f"неизвестный путь: {path}")


def input_path(argv: list[str]) -> pathlib.Path:
    """--input PATH позволяет прогнать инструмент на черновике."""
    if "--input" in argv:
        return pathlib.Path(argv[argv.index("--input") + 1])
    return INPUT


def main(argv: list[str]) -> int:
    cfg = yaml_rt.load(CONFIG.read_text(encoding="utf-8"))
    src = input_path(argv)

    if "--template" in argv:
        INPUT.parent.mkdir(exist_ok=True)
        INPUT.write_text(build_template(cfg), encoding="utf-8")
        n = build_template(cfg).count("- path:")
        print(f"Заготовка на {n} значений записана в {INPUT.relative_to(ROOT)}")
        md = parse_fees_md()
        print(f"Ссылок разобрано из fees.md: "
              f"{sum(len(v) for v in md.values())} в {len(md)} разделах")
        return 0

    if not src.exists():
        print(f"Нет файла {src}. "
              f"Сначала: tools/apply_fees.py --template", file=sys.stderr)
        return 2

    data = YAML(typ="safe").load(src.read_text(encoding="utf-8"))
    entries = data.get("values") or []
    dry = "--dry-run" in argv

    applied, skipped, refused = [], [], []

    for e in entries:
        path, value = e.get("path"), e.get("value")
        if value is None:
            skipped.append(path)
            continue
        if not e.get("source") or not e.get("checked"):
            refused.append(f"{path}: значение есть, но нет "
                           f"{'source' if not e.get('source') else 'checked'}")
            continue
        try:
            dt.date.fromisoformat(str(e["checked"]))
        except ValueError:
            refused.append(f"{path}: checked не похоже на дату: {e['checked']!r}")
            continue

        container, key = resolve(cfg, path)
        container[key] = value
        note = f"источник: doc, {e['source']}, просмотрено {e['checked']}"
        if e.get("account"):
            note += "; ТАРИФ КОНКРЕТНОГО АККАУНТА, не базовый уровень"
        container.yaml_add_eol_comment(note, key)
        applied.append(f"{path} = {value}")

    for line in applied:
        print(f"  внесено   {line}")
    for line in refused:
        print(f"  ОТКАЗ     {line}")
    if skipped:
        print(f"  пропущено {len(skipped)} записей без значения")

    if refused:
        print("\nНичего не записано: сначала исправь отказы.", file=sys.stderr)
        return 2
    if not applied:
        print("\nНечего вносить: ни одно значение не заполнено.")
        return 1
    if dry:
        print("\n--dry-run: файл не изменён.")
        return 0

    with CONFIG.open("w", encoding="utf-8") as fh:
        yaml_rt.dump(cfg, fh)
    print(f"\nconfig.yaml обновлён, внесено значений: {len(applied)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
