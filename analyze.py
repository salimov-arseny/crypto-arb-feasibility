"""Анализ лога наблюдений: агрегаты, два графика, выводы.

Отвечает на вопрос README: какая доля наблюдаемых расхождений выживает
после всех пяти слоёв издержек, и что ограничивает сильнее всего.

Вход:
    logs/observations.csv   наблюдения, пишет scan.py
    logs/books.jsonl        полные стаканы раз в пять минут, пишет scan.py
    data/candles/*.csv      свечи для риска перевода, tools/fetch_candles.py

Выход: отчёт в консоль и два графика в reports/.

Все вычисления - на стандартной библиотеке. matplotlib подключается только
внутри функций рисования: анализ и его тесты не должны зависеть от того,
установлен ли пакет для картинок.

Запуск:
    .venv/bin/python -u analyze.py
    .venv/bin/python -u analyze.py --log logs/observations.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from core.config import load_config
from core.orderbook import OrderBook
from core.risk import (RiskError, ReturnSample, assess, horizon_returns,
                       load_closes, required_net_bps)

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_LOG = ROOT / "logs" / "observations.csv"
DEFAULT_BOOKS = ROOT / "logs" / "books.jsonl"
CANDLES = ROOT / "data" / "candles"
REPORTS = ROOT / "reports"

# Вероятность, для которой считается «цена времени перевода» в разложении.
# 0,9 - обычный уровень: сделка, которая проваливается чаще раза из десяти,
# на практике никого не интересует.
RISK_LEVEL = 0.90

NUMERIC = {
    "skew_sec", "best_ask_buy", "best_bid_sell", "naive_spread_bps",
    "opt_volume", "opt_turnover", "vwap_buy", "vwap_sell",
    "slippage_buy_bps", "slippage_sell_bps", "gross", "fee_buy", "fee_sell",
    "withdrawal", "net", "net_bps", "transfer_time_sec",
    "latency_buy_ms", "latency_sell_ms",
    "rel_volume", "rel_turnover", "rel_gross", "rel_fee_buy",
    "rel_fee_sell", "rel_withdrawal", "rel_net", "rel_net_bps",
}


# --------------------------------------------------------------------------
#  Загрузка
# --------------------------------------------------------------------------

def load_observations(path: str | pathlib.Path) -> list[dict[str, Any]]:
    """Прочитать лог. Пустые поля становятся None, числовые - float.

    Пустое поле - это не ноль. У отбракованного наблюдения чистой прибыли
    нет вовсе, и превратить её в ноль значило бы записать его в сделки
    «ровно в ноль» и исказить распределение.
    """
    rows: list[dict[str, Any]] = []
    with pathlib.Path(path).open(encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            row: dict[str, Any] = {}
            for key, value in raw.items():
                if key in NUMERIC:
                    row[key] = float(value) if value not in ("", None) else None
                else:
                    row[key] = value
            row["sweep"] = int(raw["sweep"])
            rows.append(row)
    return rows


# --------------------------------------------------------------------------
#  Описательные статистики
# --------------------------------------------------------------------------

def percentile(values: list[float], q: float) -> float:
    """Квантиль с линейной интерполяцией между соседними значениями.

    Та же формула, что у numpy по умолчанию: позиция q*(n-1) в отсортированном
    ряду, между двумя ближайшими значениями - линейно.
    """
    if not values:
        raise ValueError("квантиль пустой выборки не определён")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"уровень квантиля должен лежать в [0, 1], получено {q}")
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


@dataclass(frozen=True)
class Summary:
    """Распределение одной величины: то, что README просит про прибыль."""

    n: int
    median: float | None
    p05: float | None
    p95: float | None
    minimum: float | None
    maximum: float | None

    @classmethod
    def of(cls, values: list[float]) -> Summary:
        if not values:
            return cls(0, None, None, None, None, None)
        return cls(n=len(values),
                   median=statistics.median(values),
                   p05=percentile(values, 0.05),
                   p95=percentile(values, 0.95),
                   minimum=min(values),
                   maximum=max(values))


# --------------------------------------------------------------------------
#  Качество данных
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Quality:
    total: int
    by_status: dict[str, int]
    sweeps: int
    first_ts: str
    last_ts: str

    @property
    def ok_share(self) -> float:
        return self.by_status.get("ok", 0) / self.total if self.total else 0.0


def quality(rows: list[dict[str, Any]]) -> Quality:
    """Сколько наблюдений годны и почему остальные отбракованы.

    Доля брака - сама по себе результат: без неё нельзя судить, насколько
    выборка представительна. Если брака много и он неравномерен по биржам,
    выводы по ним неравноценны.
    """
    if not rows:
        return Quality(0, {}, 0, "", "")
    return Quality(
        total=len(rows),
        by_status=dict(Counter(r["status"] for r in rows)),
        sweeps=len({r["sweep"] for r in rows}),
        first_ts=min(r["ts_utc"] for r in rows),
        last_ts=max(r["ts_utc"] for r in rows),
    )


# --------------------------------------------------------------------------
#  Коэффициент выживаемости
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Survival:
    usable: int          # годные наблюдения
    positive: int        # из них с положительным наивным расхождением
    survivors: int       # из них прибыльные после издержек

    @property
    def coefficient(self) -> float | None:
        """Главное число работы.

        Знаменатель - наблюдения с ПОЛОЖИТЕЛЬНЫМ наивным расхождением, то есть
        случаи, когда лучший бид на одной бирже действительно выше лучшего
        аска на другой. Так README описывает расхождение в разделе «Зачем».
        Наблюдение, где расхождения нет вовсе, не может «не выжить» - ему
        нечего терять.
        """
        return self.survivors / self.positive if self.positive else None

    @property
    def coefficient_all(self) -> float | None:
        """Для сравнения: доля прибыльных среди всех годных наблюдений."""
        return self.survivors / self.usable if self.usable else None


def usable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Годные наблюдения: статус ok и посчитанная прибыль."""
    return [r for r in rows if r["status"] == "ok" and r["net"] is not None]


def is_positive(row: dict[str, Any]) -> bool:
    return row["naive_spread_bps"] is not None and row["naive_spread_bps"] > 0


def is_survivor(row: dict[str, Any]) -> bool:
    """Прибыльно ли наблюдение после первых четырёх слоёв издержек.

    Пятый слой - время перевода - даёт не «да или нет», а вероятность,
    и к выжившим прикладывается отдельно (transfer_risk).
    """
    return row["net"] is not None and row["net"] > 0


def survival(rows: list[dict[str, Any]]) -> Survival:
    good = usable(rows)
    positive = [r for r in good if is_positive(r)]
    survivors = [r for r in positive if is_survivor(r)]
    return Survival(usable=len(good), positive=len(positive),
                    survivors=len(survivors))


def optimum_disagreements(rows: list[dict[str, Any]]) -> int:
    """Сколько наблюдений два оптимума оценили по-разному.

    При любом объёме знак прибыли в деньгах совпадает со знаком прибыли
    в базисных пунктах: они отличаются делением на положительный оборот.
    Значит «существует объём с положительной прибылью» - одно и то же
    утверждение для обеих целей, и два оптимума обязаны согласно отвечать
    на вопрос «прибыльно ли». Расхождение означает, что один из поисков
    не нашёл глобальный максимум, и это повод насторожиться.
    """
    return sum(1 for r in usable(rows)
               if r["rel_net"] is not None
               and (r["net"] > 0) != (r["rel_net"] > 0))


# --------------------------------------------------------------------------
#  Разложение: что съедает расхождение
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Layers:
    """Расхождение и то, что от него отнимает каждый слой, в б.п. оборота.

    Считается в оптимуме по прибыли в базисных пунктах (колонки rel_*):
    оптимум по прибыли в деньгах на убыточных маршрутах вырождается
    в минимальный объём, и плата за вывод, делённая на пять долларов,
    задавила бы всё разложение.
    """

    naive: float        # наивное расхождение по лучшим ценам
    slippage: float     # глубина стакана
    fees: float         # две комиссии тейкера
    withdrawal: float   # плата за вывод
    net: float          # что осталось

    @property
    def residual(self) -> float:
        """Невязка тождества. Должна быть на уровне ошибок округления."""
        return self.naive - self.slippage - self.fees - self.withdrawal - self.net


def decompose(row: dict[str, Any]) -> Layers | None:
    """Разложить одно наблюдение на слои.

    Проскальзывание определяется как разница между наивным расхождением
    и валовой прибылью на единицу оборота. При таком определении

        net = naive - slippage - fees - withdrawal

    выполняется ТОЧНО, по построению, и никакая часть расхождения не теряется
    и не считается дважды.
    """
    turnover = row.get("rel_turnover")
    if not turnover or row.get("rel_gross") is None:
        return None
    bps = 10_000 / turnover
    gross = row["rel_gross"] * bps
    naive = row["naive_spread_bps"]
    return Layers(
        naive=naive,
        slippage=naive - gross,
        fees=(row["rel_fee_buy"] + row["rel_fee_sell"]) * bps,
        withdrawal=row["rel_withdrawal"] * bps,
        net=row["rel_net"] * bps,
    )


@dataclass(frozen=True)
class LayerSummary:
    symbol: str
    n: int
    naive: float
    slippage: float
    fees: float
    withdrawal: float
    transfer: float | None   # цена времени перевода при RISK_LEVEL
    net: float

    def largest(self) -> tuple[str, float]:
        """Какой слой отнимает больше всех - прямой ответ на вопрос README."""
        layers = {"комиссии": self.fees,
                  "глубина стакана": self.slippage,
                  "плата за вывод": self.withdrawal}
        if self.transfer is not None:
            layers["время перевода"] = self.transfer
        name = max(layers, key=layers.get)
        return name, layers[name]


def layer_summary(rows: list[dict[str, Any]],
                  transfer_bps: dict[tuple[str, float], float] | None = None
                  ) -> list[LayerSummary]:
    """Средний вклад каждого слоя по инструментам.

    Берутся только наблюдения с положительным наивным расхождением: вопрос
    README - что съедает расхождение, а там, где его нет, съедать нечего.
    Медиана, а не среднее: распределения тяжелохвостые, и один выброс
    сдвинул бы среднее сильнее, чем тысяча обычных наблюдений.
    """
    by_symbol: dict[str, list[tuple[Layers, dict]]] = defaultdict(list)
    for row in usable(rows):
        if not is_positive(row):
            continue
        layers = decompose(row)
        if layers is not None:
            by_symbol[row["symbol"]].append((layers, row))

    result = []
    for symbol in sorted(by_symbol):
        items = by_symbol[symbol]
        transfer = None
        if transfer_bps:
            values = [transfer_bps[(symbol, row["transfer_time_sec"])]
                      for _, row in items
                      if row["transfer_time_sec"] is not None
                      and (symbol, row["transfer_time_sec"]) in transfer_bps]
            transfer = statistics.median(values) if values else None
        result.append(LayerSummary(
            symbol=symbol,
            n=len(items),
            naive=statistics.median(l.naive for l, _ in items),
            slippage=statistics.median(l.slippage for l, _ in items),
            fees=statistics.median(l.fees for l, _ in items),
            withdrawal=statistics.median(l.withdrawal for l, _ in items),
            transfer=transfer,
            net=statistics.median(l.net for l, _ in items),
        ))
    return result


# --------------------------------------------------------------------------
#  Риск перевода
# --------------------------------------------------------------------------

class ReturnSamples:
    """Выборки доходностей по горизонтам, с кэшем.

    Горизонтов немного - семь различных, - а выживших может быть много,
    и читать файл свечей заново для каждого было бы расточительно.
    """

    # Горизонт короче этого берём по секундным свечам: у них 12 часов
    # истории против 30 дней у минутных, но на коротком горизонте секундное
    # разрешение важнее длины ряда.
    SECONDS_UP_TO = 120.0

    def __init__(self, cfg: dict[str, Any], candles: pathlib.Path = CANDLES):
        self.cfg = cfg
        self.candles = candles
        self._closes: dict[tuple[str, str], list[float]] = {}
        self._samples: dict[tuple[str, float], ReturnSample] = {}

    def _series(self, symbol: str, interval: str) -> list[float]:
        key = (symbol, interval)
        if key not in self._closes:
            ticker = self.cfg["exchanges"]["binance"]["tickers"][symbol]
            files = sorted(self.candles.glob(f"{ticker}_{interval}_*.csv"))
            if not files:
                raise RiskError(f"нет свечей {ticker} {interval} в {self.candles}")
            self._closes[key] = load_closes(files[-1])   # самая свежая выгрузка
        return self._closes[key]

    def get(self, symbol: str, horizon_sec: float) -> ReturnSample:
        key = (symbol, horizon_sec)
        if key not in self._samples:
            interval, step = (("1s", 1.0) if horizon_sec < self.SECONDS_UP_TO
                              else ("1m", 60.0))
            self._samples[key] = horizon_returns(
                self._series(symbol, interval), step, horizon_sec, symbol,
                allow_resolution_bound=True)
        return self._samples[key]


@dataclass(frozen=True)
class SurvivorRisk:
    row: dict[str, Any]
    p_normal: float | None      # None - риск не определён
    p_empirical: float | None
    reason: str = ""


def transfer_risk(rows: list[dict[str, Any]], samples: ReturnSamples
                  ) -> list[SurvivorRisk]:
    """Вероятность, что прибыль каждого выжившего переживёт перевод.

    Для маршрутов без числа подтверждений - «риск не определён», а не ноль:
    ноль означал бы мгновенный перевод без всякого риска.
    """
    result = []
    for row in usable(rows):
        if not (is_positive(row) and is_survivor(row)):
            continue
        horizon = row["transfer_time_sec"]
        if horizon is None:
            result.append(SurvivorRisk(row, None, None,
                                       "риск не определён: нет числа подтверждений"))
            continue
        try:
            risk = assess(row["net"], row["opt_volume"], row["vwap_sell"],
                          samples.get(row["symbol"], horizon))
        except RiskError as exc:
            result.append(SurvivorRisk(row, None, None, str(exc)))
            continue
        result.append(SurvivorRisk(row, risk.p_normal, risk.p_empirical))
    return result


def transfer_cost_bps(rows: list[dict[str, Any]], samples: ReturnSamples,
                      level: float = RISK_LEVEL) -> dict[tuple[str, float], float]:
    """Цена времени перевода в б.п.: сколько надо заработать сверх нуля,
    чтобы пережить перевод с вероятностью `level`.

    Это пятый слой разложения. Он выражен в тех же единицах, что и первые
    четыре, - поэтому их можно сравнивать и отвечать на вопрос README,
    что ограничивает сильнее.
    """
    costs: dict[tuple[str, float], float] = {}
    for row in usable(rows):
        horizon = row["transfer_time_sec"]
        key = (row["symbol"], horizon)
        if horizon is None or key in costs:
            continue
        try:
            costs[key] = required_net_bps(level,
                                          samples.get(row["symbol"], horizon).sigma)
        except RiskError:
            continue
    return costs


# --------------------------------------------------------------------------
#  Оптимальный объём
# --------------------------------------------------------------------------

def optimal_volumes(rows: list[dict[str, Any]]
                    ) -> dict[tuple[str, str, str], Summary]:
    """Оптимальный оборот по каждому маршруту - только среди выживших.

    У убыточного маршрута оптимума в смысле README нет: прибыль максимальна
    на минимальном объёме, и «оптимальный объём» - это просто ограничение
    площадки. Показывать его как результат было бы неверно.
    """
    groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in usable(rows):
        if is_positive(row) and is_survivor(row):
            groups[(row["symbol"], row["buy"], row["sell"])].append(
                row["opt_turnover"])
    return {key: Summary.of(values) for key, values in groups.items()}


# --------------------------------------------------------------------------
#  Отчёт
# --------------------------------------------------------------------------

def fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    return "—" if value is None else f"{value:,.{digits}f}{suffix}"


def report(rows: list[dict[str, Any]], cfg: dict[str, Any],
           samples: ReturnSamples | None) -> None:
    q = quality(rows)
    print("=" * 78)
    print("КАЧЕСТВО ДАННЫХ")
    print("=" * 78)
    print(f"Период:      {q.first_ts}  ->  {q.last_ts}")
    print(f"Снимков:     {q.sweeps:,}")
    print(f"Наблюдений:  {q.total:,}")
    for status, count in sorted(q.by_status.items(), key=lambda kv: -kv[1]):
        print(f"  {status:<15} {count:>9,}  ({count / q.total * 100:5.1f} %)")

    bad = optimum_disagreements(rows)
    if bad:
        print(f"\nВНИМАНИЕ: в {bad} наблюдениях два оптимума разошлись "
              f"в оценке прибыльности - поиск где-то не нашёл максимум.")

    s = survival(rows)
    print("\n" + "=" * 78)
    print("КОЭФФИЦИЕНТ ВЫЖИВАЕМОСТИ")
    print("=" * 78)
    print(f"Годных наблюдений:                     {s.usable:>9,}")
    print(f"  с положительным наивным расхождением: {s.positive:>9,}")
    print(f"  из них прибыльных после издержек:     {s.survivors:>9,}")
    print(f"\nКоэффициент выживаемости: "
          f"{fmt(None if s.coefficient is None else s.coefficient * 100, 4, ' %')}"
          f"   ({s.survivors} из {s.positive})")
    print(f"Для сравнения, от всех годных:  "
          f"{fmt(None if s.coefficient_all is None else s.coefficient_all * 100, 4, ' %')}")

    good = usable(rows)
    survivors = [r for r in good if is_positive(r) and is_survivor(r)]
    print("\n" + "=" * 78)
    print("ЧИСТАЯ ПРИБЫЛЬ ВЫЖИВШИХ")
    print("=" * 78)
    if survivors:
        for label, values in (("б.п.", [r["net_bps"] for r in survivors]),
                              ("USDT", [r["net"] for r in survivors])):
            d = Summary.of(values)
            print(f"  {label}: медиана {fmt(d.median)}, 95-й процентиль "
                  f"{fmt(d.p95)}, максимум {fmt(d.maximum)}")
    else:
        print("  Выживших нет: ни одно наблюдение не дало прибыли после издержек.")

    print("\n" + "=" * 78)
    print("НАИВНОЕ РАСХОЖДЕНИЕ И ЧИСТЫЙ СПРЕД, б.п.")
    print("=" * 78)
    print(f"{'инструмент':<10} {'набл.':>8} {'наивное: медиана':>17} {'p95':>8} "
          f"{'макс':>8} {'чистый: медиана':>16} {'макс':>8}")
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for r in good:
        if is_positive(r) and r["rel_net_bps"] is not None:
            by_symbol[r["symbol"]].append(r)
    for symbol in sorted(by_symbol):
        items = by_symbol[symbol]
        naive = Summary.of([r["naive_spread_bps"] for r in items])
        net = Summary.of([r["rel_net_bps"] for r in items])
        print(f"{symbol:<10} {naive.n:>8,} {fmt(naive.median):>17} "
              f"{fmt(naive.p95):>8} {fmt(naive.maximum):>8} "
              f"{fmt(net.median):>16} {fmt(net.maximum):>8}")

    transfer = transfer_cost_bps(rows, samples) if samples else {}
    print("\n" + "=" * 78)
    print("ЧТО СЪЕДАЕТ РАСХОЖДЕНИЕ, медиана по наблюдениям, б.п. оборота")
    print("=" * 78)
    print(f"{'инструмент':<10} {'набл.':>7} {'наивное':>9} {'глубина':>9} "
          f"{'комиссии':>9} {'вывод':>8} {'перевод':>9} {'итог':>9}   главное")
    for ls in layer_summary(rows, transfer):
        name, value = ls.largest()
        print(f"{ls.symbol:<10} {ls.n:>7,} {ls.naive:>9.2f} {ls.slippage:>9.2f} "
              f"{ls.fees:>9.2f} {ls.withdrawal:>8.2f} {fmt(ls.transfer):>9} "
              f"{ls.net:>9.2f}   {name}")
    print(f"\n«перевод» - сколько надо заработать сверх нуля, чтобы пережить")
    print(f"перевод с вероятностью {RISK_LEVEL:.0%}, в нормальном приближении.")

    if survivors and samples:
        print("\n" + "=" * 78)
        print("ВЫЖИВЕТ ЛИ ПРИБЫЛЬ ПЕРЕВОД")
        print("=" * 78)
        risks = transfer_risk(rows, samples)
        known = [x for x in risks if x.p_normal is not None]
        unknown = [x for x in risks if x.p_normal is None]
        if known:
            print(f"  Ожидаемое число выживших с учётом риска:")
            print(f"    по нормальному приближению: "
                  f"{sum(x.p_normal for x in known):.1f} из {len(known)}")
            print(f"    по эмпирическому:           "
                  f"{sum(x.p_empirical for x in known):.1f} из {len(known)}")
        if unknown:
            print(f"  Риск не определён для {len(unknown)} выживших "
                  f"(нет числа подтверждений у биржи-получателя).")

    volumes = optimal_volumes(rows)
    if volumes:
        print("\n" + "=" * 78)
        print("ОПТИМАЛЬНЫЙ ОБЪЁМ ВЫЖИВШИХ, оборот в USDT")
        print("=" * 78)
        for (symbol, buy, sell), d in sorted(volumes.items()):
            print(f"  {symbol:<9} {buy:>7} -> {sell:<7} медиана {fmt(d.median, 0)}"
                  f"  (наблюдений {d.n})")


# --------------------------------------------------------------------------
#  Графики
# --------------------------------------------------------------------------
#
#  Оформление по одним правилам для обоих графиков. Один ряд на панель,
#  поэтому легенды нет - ряд называет заголовок панели. Инструменты
#  разнесены по панелям, а не наложены: у них разные масштабы, и общая
#  ось задавила бы мелкие. Сетка и оси приглушены, данные - нет.

SURFACE = "#fcfcfb"
INK = "#0b0b0b"          # основной текст
INK_2 = "#52514e"        # подписи
MUTED = "#898781"        # оси, опорные линии
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
SERIES = "#2a78d6"       # единственный цвет данных


def _style(ax, title: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=11, color=INK, pad=8)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.xaxis.label.set_color(INK_2)
    ax.yaxis.label.set_color(INK_2)


def load_books(path: str | pathlib.Path
               ) -> dict[int, dict[tuple[str, str], OrderBook]]:
    """Сохранённые стаканы, по снимкам: {снимок: {(биржа, инструмент): стакан}}."""
    by_sweep: dict[int, dict[tuple[str, str], OrderBook]] = defaultdict(dict)
    with pathlib.Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            book = OrderBook.from_record(record)
            by_sweep[record["sweep"]][(book.exchange, book.symbol)] = book
    return by_sweep


def widest_route(cfg: dict[str, Any], books: dict, symbol: str):
    """Снимок и маршрут с наибольшим наивным расхождением по инструменту.

    Для графика net(V) берём самый благоприятный случай из сохранённых:
    если прибыль не появляется даже там, она не появляется нигде.
    """
    from core.costs import CostError, build_route

    display = {ex["display_name"]: key for key, ex in cfg["exchanges"].items()}
    best = None
    for sweep, snapshot in books.items():
        present = [name for (name, sym) in snapshot if sym == symbol]
        for buy_name in present:
            for sell_name in present:
                if buy_name == sell_name:
                    continue
                bb = snapshot[(buy_name, symbol)]
                bs = snapshot[(sell_name, symbol)]
                naive = (bs.best_bid - bb.best_ask) / bb.best_ask * 10_000
                if best is not None and naive <= best[0]:
                    continue
                try:
                    route = build_route(cfg, symbol, display[buy_name],
                                        display[sell_name])
                except (CostError, KeyError):
                    continue
                best = (naive, sweep, route, bb, bs)
    return best


def plot_net_vs_volume(cfg: dict[str, Any], books_path: pathlib.Path,
                       out: pathlib.Path) -> pathlib.Path | None:
    """График 1 из README: net(V) как функция объёма.

    По одной панели на инструмент, для самого благоприятного из сохранённых
    случаев. Ось объёма логарифмическая: интересное происходит на разных
    порядках - плата за вывод решает на десятках долларов, проскальзывание
    на сотнях тысяч.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from core.arbitrage import find_optimum, profile

    books = load_books(books_path)
    cases = [(s, widest_route(cfg, books, s)) for s in cfg["symbols"]]
    cases = [(s, c) for s, c in cases if c is not None]
    if not cases:
        return None

    fig, axes = plt.subplots(1, len(cases), figsize=(4.2 * len(cases), 3.8),
                             facecolor=SURFACE)
    axes = axes if len(cases) > 1 else [axes]

    for ax, (symbol, (naive, sweep, route, bb, bs)) in zip(axes, cases):
        points = [r for r in profile(route, bb, bs, points=200) if r.usable]
        if not points:
            continue
        ax.plot([r.turnover for r in points], [r.net for r in points],
                color=SERIES, linewidth=2)
        ax.axhline(0, color=MUTED, linewidth=1, linestyle=(0, (4, 3)))
        ax.set_xscale("log")
        # Прибыль меняется на несколько порядков: от единиц USDT на малых
        # объёмах до тысяч минус на больших. На линейной оси вся область
        # у левого края, где и стоит максимум, сплющивается в прямую.
        # Симметричная логарифмическая шкала линейна около нуля и
        # логарифмична дальше - видно и начало кривой, и её обвал.
        ax.set_yscale("symlog", linthresh=1.0)

        opt = find_optimum(route, bb, bs, objective="net").best
        if opt is not None:
            ax.plot([opt.turnover], [opt.net], "o", markersize=8,
                    color=SERIES, markeredgecolor=SURFACE, markeredgewidth=2)
            # Подпись - в пустую область над кривой, со стрелкой к точке:
            # рядом с точкой текст пересекался с самой кривой.
            ax.annotate(f"максимум {opt.net:+.2f} USDT\nпри обороте {opt.turnover:,.0f} USDT",
                        (opt.turnover, opt.net), xycoords="data",
                        xytext=(0.40, 0.78), textcoords="axes fraction",
                        fontsize=8.5, color=INK_2,
                        arrowprops=dict(arrowstyle="-", color=MUTED,
                                        linewidth=0.8))

        _style(ax, f"{symbol}: {route.buy.name} → {route.sell.name}")
        ax.set_xlabel("оборот сделки, USDT")
        # Внутри области графика, в свободном левом нижнем углу: над
        # панелью эта строка налезала на заголовок.
        ax.text(0.03, 0.04, f"наивное расхождение {naive:+.2f} б.п.",
                transform=ax.transAxes, fontsize=8.5, color=MUTED,
                va="bottom", ha="left")
    axes[0].set_ylabel("чистая прибыль net(V), USDT")

    fig.suptitle("Чистая прибыль как функция объёма — самый широкий "
                 "из сохранённых случаев", x=0.01, ha="left", fontsize=12,
                 color=INK)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return out


def plot_net_spread_hist(rows: list[dict[str, Any]],
                         out: pathlib.Path) -> pathlib.Path | None:
    """График 2 из README: гистограмма чистого спреда.

    Чистый спред - прибыль в б.п. оборота в оптимуме по б.п. (колонки rel_*):
    в оптимуме по деньгам убыточные маршруты вырождаются в минимальный объём
    и дали бы пик на тысячах минус б.п., задавив всё остальное.

    Только наблюдения с положительным наивным расхождением - тот же
    знаменатель, что у коэффициента выживаемости. Вертикальная линия на нуле -
    граница выживания: всё правее неё - выжившие.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    by_symbol: dict[str, list[float]] = defaultdict(list)
    for r in usable(rows):
        if is_positive(r) and r["rel_net_bps"] is not None:
            by_symbol[r["symbol"]].append(r["rel_net_bps"])
    if not by_symbol:
        return None

    symbols = sorted(by_symbol)
    fig, axes = plt.subplots(len(symbols), 1, figsize=(8, 2.4 * len(symbols)),
                             sharex=True, facecolor=SURFACE)
    axes = axes if len(symbols) > 1 else [axes]

    everything = [v for vs in by_symbol.values() for v in vs]
    low = min(min(everything), 0.0)
    high = max(max(everything), 0.0)
    bins = [low + (high - low) * i / 60 for i in range(61)]

    for ax, symbol in zip(axes, symbols):
        values = by_symbol[symbol]
        ax.hist(values, bins=bins, color=SERIES, edgecolor=SURFACE,
                linewidth=1)
        ax.axvline(0, color=INK_2, linewidth=1.2)
        survivors = sum(1 for v in values if v > 0)
        _style(ax, symbol)
        ax.text(1.0, 1.02, f"наблюдений {len(values):,}, "
                           f"выше нуля {survivors:,}",
                transform=ax.transAxes, fontsize=8.5, color=MUTED,
                va="bottom", ha="right")
        ax.set_ylabel("наблюдений")
    axes[-1].set_xlabel("чистый спред после всех издержек, б.п. оборота")
    # Справа от нуля оставлено поле под подпись: вплотную к краю она
    # вылезала за область графика, а слева налезала на столбцы у -20.
    pad = 0.22 * (high - low)
    for ax in axes:
        ax.set_xlim(low - 0.02 * (high - low), max(high, 0.0) + pad)
    axes[0].annotate("граница выживания", (0, 1), xycoords=("data", "axes fraction"),
                     xytext=(6, -12), textcoords="offset points",
                     ha="left", fontsize=8.5, color=INK_2)

    fig.suptitle("Чистый спред по наблюдениям с положительным "
                 "наивным расхождением", x=0.01, ha="left", fontsize=12,
                 color=INK)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Анализ лога наблюдений")
    parser.add_argument("--log", type=pathlib.Path, default=DEFAULT_LOG)
    parser.add_argument("--books", type=pathlib.Path, default=DEFAULT_BOOKS)
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args(argv)

    if not args.log.exists():
        print(f"Нет лога {args.log}. Сначала соберите наблюдения: "
              f".venv/bin/python -u scan.py --minutes 180", file=sys.stderr)
        return 2

    cfg = load_config()
    rows = load_observations(args.log)
    samples = ReturnSamples(cfg) if CANDLES.exists() else None
    report(rows, cfg, samples)

    if not args.no_plots:
        print("\n" + "=" * 78)
        print("ГРАФИКИ")
        print("=" * 78)
        made = [plot_net_spread_hist(rows, REPORTS / "net_spread_hist.png")]
        if args.books.exists():
            made.append(plot_net_vs_volume(cfg, args.books,
                                           REPORTS / "net_vs_volume.png"))
        else:
            print(f"  нет {args.books} - график net(V) не построен")
        for path in made:
            if path is not None:
                print(f"  {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
