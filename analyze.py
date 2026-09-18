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
                       load_closes, required_net_bps, survival_empirical,
                       survival_normal)

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
    group: str = ""          # группа маршрутов по комиссии, если делили
    best: float | None = None  # лучший итог в группе - насколько близко к нулю

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
                  transfer_bps: dict[tuple[str, float], float] | None = None,
                  by_fee: bool = False) -> list[LayerSummary]:
    """Средний вклад каждого слоя по инструментам.

    Берутся только наблюдения с положительным наивным расхождением: вопрос
    README - что съедает расхождение, а там, где его нет, съедать нечего.
    Медиана, а не среднее: распределения тяжелохвостые, и один выброс
    сдвинул бы среднее сильнее, чем тысяча обычных наблюдений.

    С by_fee=True маршруты дополнительно делятся по суммарной комиссии.
    Без этого деления медиана смешивает дешёвые и дорогие маршруты и прячет
    дорогие: при 60 % дешёвых медиана комиссии выходит 20 б.п., и о маршрутах
    с 90 б.п. таблица молчит.
    """
    groups: dict[tuple[str, str], list[tuple[Layers, dict]]] = defaultdict(list)
    for row in usable(rows):
        if not is_positive(row):
            continue
        layers = decompose(row)
        if layers is not None:
            group = fee_group(row) if by_fee else ""
            groups[(group, row["symbol"])].append((layers, row))

    result = []
    for group, symbol in sorted(groups):
        items = groups[(group, symbol)]
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
            group=group,
            best=max(l.net for l, _ in items),
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
#  Дополнительные величины для выводов
# --------------------------------------------------------------------------

def rule_of_three(n: int) -> float | None:
    """Верхняя 95-процентная граница доли, если в n испытаниях успехов ноль.

    Из (1 - p)^n = 0,05 следует p = 1 - 0,05^(1/n) ≈ -ln(0,05)/n ≈ 3/n.
    Ноль выживших - это не ноль вероятности, а «вероятность, скорее всего,
    не больше 3/n». Формула предполагает независимые испытания; где они
    зависимы, n надо брать по числу независимых единиц.
    """
    return 3.0 / n if n > 0 else None


def fee_bps(row: dict[str, Any]) -> float | None:
    """Суммарная комиссия двух сделок маршрута, б.п. оборота.

    Группировка по ней, а не по названию биржи: так анализ не зашивает
    в себя, что дорогая площадка именно Kraken, а берёт это из данных.
    """
    if not row.get("rel_turnover") or row.get("rel_fee_buy") is None:
        return None
    return (row["rel_fee_buy"] + row["rel_fee_sell"]) / row["rel_turnover"] * 10_000


def fee_group(row: dict[str, Any]) -> str:
    value = fee_bps(row)
    return "?" if value is None else f"комиссия {round(value):.0f} б.п."


def failures_by_exchange(rows: list[dict[str, Any]], exchanges: list[str]
                         ) -> tuple[Counter, int]:
    """Какая биржа сколько раз не ответила.

    В строке брака не записано, кто именно отказал, - но это восстанавливается:
    биржа не ответила в снимке по инструменту, если ВСЕ её маршруты в нём
    помечены fetch_failed. Возвращает счётчик и число пар «снимок, инструмент».
    """
    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["sweep"], r["symbol"])].append(r)
    down: Counter = Counter()
    for items in groups.values():
        for ex in exchanges:
            mine = [r for r in items if ex in (r["buy"], r["sell"])]
            if mine and all(r["status"] == "fetch_failed" for r in mine):
                down[ex] += 1
    return down, len(groups)


def widest_share_in_costliest_group(rows: list[dict[str, Any]],
                                    top: int = 100) -> tuple[int, int, str] | None:
    """Сколько из `top` самых широких наивных расхождений приходится
    на самую дорогую по комиссиям группу маршрутов."""
    good = [r for r in usable(rows) if is_positive(r) and fee_bps(r) is not None]
    if not good:
        return None
    costliest = max({fee_group(r) for r in good},
                    key=lambda g: float(g.split()[1]))
    widest = sorted(good, key=lambda r: -r["naive_spread_bps"])[:top]
    return sum(1 for r in widest if fee_group(r) == costliest), len(widest), costliest


def fee_threshold_bps(rows: list[dict[str, Any]]) -> float | None:
    """Какая суммарная комиссия дала бы выжить хотя бы одному наблюдению.

    Для каждого наблюдения самой дешёвой группы - что остаётся ДО комиссий:
    наивное минус проскальзывание минус вывод. Максимум этой величины и есть
    порог: при комиссии ниже него выжило бы хоть одно наблюдение. Оценка
    сверху: при меньших комиссиях оптимальный объём сдвинулся бы, но остальные
    слои от этого не уменьшатся.
    """
    good = [r for r in usable(rows) if is_positive(r) and fee_bps(r) is not None]
    if not good:
        return None
    cheapest = min({fee_group(r) for r in good},
                   key=lambda g: float(g.split()[1]))
    values = []
    for r in good:
        if fee_group(r) != cheapest:
            continue
        layers = decompose(r)
        if layers is not None:
            values.append(layers.naive - layers.slippage - layers.withdrawal)
    return max(values) if values else None


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
    down, pairs = failures_by_exchange(rows, list(cfg["exchanges"]))
    if down:
        print("Кто не отвечал (доля пар «снимок, инструмент»):")
        for ex, n in down.most_common():
            print(f"  {ex:<15} {n:>9,}  ({n / pairs * 100:5.1f} % из {pairs:,})")

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
    if s.survivors == 0 and s.positive:
        positive_sweeps = len({r["sweep"] for r in usable(rows) if is_positive(r)})
        print(f"\nВыживших ноль - это не ноль вероятности. Верхняя 95 %-граница доли")
        print(f"по правилу трёх:")
        print(f"  наблюдения как независимые:  3/{s.positive:,} = "
              f"{rule_of_three(s.positive) * 100:.4f} %")
        print(f"  по снимкам (внутри снимка маршруты зависимы): 3/{positive_sweeps:,} = "
              f"{rule_of_three(positive_sweeps) * 100:.3f} %")

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
    print(f"{'группа':<18} {'инструмент':<10} {'набл.':>7} {'наивное':>8} "
          f"{'глубина':>8} {'комиссии':>9} {'вывод':>6} {'перевод':>8} "
          f"{'итог':>8} {'лучший':>8}   главное")
    for ls in layer_summary(rows, transfer, by_fee=True):
        name, value = ls.largest()
        print(f"{ls.group:<18} {ls.symbol:<10} {ls.n:>7,} {ls.naive:>8.2f} "
              f"{ls.slippage:>8.2f} {ls.fees:>9.2f} {ls.withdrawal:>6.2f} "
              f"{fmt(ls.transfer):>8} {ls.net:>8.2f} {fmt(ls.best):>8}   {name}")
    print(f"\n«перевод» - сколько надо заработать сверх нуля, чтобы пережить")
    print(f"перевод с вероятностью {RISK_LEVEL:.0%}, в нормальном приближении.")

    print("\n" + "=" * 78)
    print("ДЛЯ ВЫВОДОВ")
    print("=" * 78)
    wide = widest_share_in_costliest_group(rows)
    if wide:
        hit, total, group = wide
        print(f"Из {total} самых широких наивных расхождений в самой дорогой группе "
              f"({group}): {hit}")
    threshold = fee_threshold_bps(rows)
    if threshold is not None:
        print(f"Что остаётся ДО комиссий в лучшем наблюдении дешёвой группы: "
              f"{threshold:.2f} б.п.")
        print(f"  => чтобы выжило хоть одно, сумма двух комиссий должна быть ниже "
              f"{threshold:.2f} б.п. (~{threshold / 2 / 100:.3f} % на сторону)")
    cheap = [r for r in usable(rows) if is_positive(r) and fee_bps(r) is not None]
    if cheap:
        low_group = min({fee_group(r) for r in cheap}, key=lambda g: float(g.split()[1]))
        in_low = [r["naive_spread_bps"] for r in cheap if fee_group(r) == low_group]
        fee_value = float(low_group.split()[1])
        print(f"Наибольшее наивное расхождение в дешёвой группе: {max(in_low):.2f} б.п.; "
              f"комиссия больше в {fee_value / max(in_low):.2f} раза")
        print(f"Медианное: {statistics.median(in_low):.2f} б.п.; "
              f"комиссия больше в {fee_value / statistics.median(in_low):.0f} раз")

    if samples:
        print("\n" + "=" * 78)
        print("ХВОСТЫ: нормальное приближение против эмпирического")
        print("=" * 78)
        print(f"{'инструмент':<10} {'горизонт':>10} {'sigma, бп':>10} {'незав.':>8} "
              f"{'куртозис':>9} {'P норм.':>9} {'P эмп.':>9} {'завышение':>11}")
        horizons = sorted({(r["symbol"], r["transfer_time_sec"]) for r in usable(rows)
                           if r["transfer_time_sec"] is not None})
        for symbol, horizon in horizons:
            try:
                sample = samples.get(symbol, horizon)
            except RiskError:
                continue
            threshold = -3.0 * sample.sigma
            p_norm = survival_normal(threshold, sample.sigma)
            p_emp = survival_empirical(threshold, sample.returns)
            label = f"{horizon:.2f} с" if horizon < 90 else f"{horizon / 60:.0f} мин"
            print(f"{symbol:<10} {label:>10} {sample.sigma * 1e4:>10.2f} "
                  f"{sample.effective_n:>8,.0f} {sample.excess_kurtosis:>9.1f} "
                  f"{p_norm:>9.5f} {p_emp:>9.5f} {(p_norm - p_emp) * 100:>+9.3f} пп")
        print("Порог -3 сигмы. «незав.» - число независимых наблюдений: окна")
        print("перекрываются, и формальный размер выборки больше в steps раз.")

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


def ru(value: float, digits: int = 2, sign: bool = False) -> str:
    """Число для подписи на русском: пробел между разрядами, запятая
    в дробной части, типографский минус. «7,444» в русском тексте
    читается как семь целых, а не семь тысяч."""
    text = f"{value:{'+' if sign else ''},.{digits}f}"
    return (text.replace(",", "\u00a0").replace(".", ",")
            .replace("-", "\u2212"))


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


def closest_route(cfg: dict[str, Any], books: dict, symbol: str):
    """Снимок и маршрут, ближе всего подошедшие к прибыли.

    Критерий - наибольший чистый спред в оптимуме по б.п., а не ширина
    наивного расхождения. Первая версия отбирала по ширине и рассуждала
    «если прибыли нет даже там, её нет нигде». Лог это опроверг: самые
    широкие расхождения почти всегда идут через Kraken (96 из 100 самых
    широких) - у него разреженный стакан, - но там же комиссия тейкера
    0,8 %, и эти случаи оказывались от прибыли ДАЛЬШЕ всех: чистый спред
    около -80 б.п. против -15 у лучших маршрутов без Kraken.
    """
    from core.arbitrage import find_optimum
    from core.costs import CostError, build_route

    display = {ex["display_name"]: key for key, ex in cfg["exchanges"].items()}
    best = None
    for sweep, snapshot in books.items():
        present = [name for (name, sym) in snapshot if sym == symbol]
        for buy_name in present:
            for sell_name in present:
                if buy_name == sell_name:
                    continue
                try:
                    route = build_route(cfg, symbol, display[buy_name],
                                        display[sell_name])
                except (CostError, KeyError):
                    continue
                bb = snapshot[(buy_name, symbol)]
                bs = snapshot[(sell_name, symbol)]
                found = find_optimum(route, bb, bs, objective="net_bps").best
                if found is None:
                    continue
                if best is None or found.net_bps > best[0]:
                    naive = (bs.best_bid - bb.best_ask) / bb.best_ask * 10_000
                    best = (found.net_bps, naive, sweep, route, bb, bs)
    return best


def plot_net_vs_volume(cfg: dict[str, Any], books_path: pathlib.Path,
                       out: pathlib.Path) -> pathlib.Path | None:
    """График 1 из README: net(V) как функция объёма.

    По одной панели на инструмент, для случая, ближе всего подошедшего
    к прибыли среди сохранённых стаканов. Ось объёма логарифмическая: интересное происходит на разных
    порядках - плата за вывод решает на десятках долларов, проскальзывание
    на сотнях тысяч.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from core.arbitrage import find_optimum, profile, search_bounds

    books = load_books(books_path)
    cases = [(s, closest_route(cfg, books, s)) for s in cfg["symbols"]]
    cases = [(s, c) for s, c in cases if c is not None]
    if not cases:
        return None

    fig, axes = plt.subplots(1, len(cases), figsize=(4.2 * len(cases), 3.8),
                             facecolor=SURFACE)
    axes = axes if len(cases) > 1 else [axes]

    for ax, (symbol, (best_bps, naive, sweep, route, bb, bs)) in zip(axes, cases):
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
            # Если максимум сидит на нижней границе, это не найденный
            # оптимум, а минимальный допустимый объём: net(V) убывает
            # монотонно, и внутреннего максимума, какой ожидает README,
            # нет. Подпись обязана говорить это прямо.
            low, _ = search_bounds(route, bb, bs)
            at_bound = abs(opt.volume - low) <= route.volume_step * 1.5
            where = "на минимальном объёме" if at_bound else "в оптимуме"
            label = (f"максимум {ru(opt.net, sign=True)} USDT\n{where}\n"
                     f"(оборот {ru(opt.turnover, 0)} USDT)")
            # Подпись - в пустую область над кривой, со стрелкой к точке:
            # рядом с точкой текст пересекался с самой кривой.
            ax.annotate(label,
                        (opt.turnover, opt.net), xycoords="data",
                        xytext=(0.40, 0.78), textcoords="axes fraction",
                        fontsize=8.5, color=INK_2,
                        arrowprops=dict(arrowstyle="-", color=MUTED,
                                        linewidth=0.8))

        _style(ax, f"{symbol}: {route.buy.name} → {route.sell.name}")
        ax.title.set_position((0.0, 1.0))
        ax.set_title(ax.get_title(loc="left"), loc="left", fontsize=11,
                     color=INK, pad=24)
        ax.set_xlabel("оборот сделки, USDT")
        # Подзаголовок - между заголовком и областью графика. Внизу
        # области его перечёркивала кривая, над панелью он налезал
        # на заголовок; поднятый заголовок освобождает для него место.
        ax.text(0.0, 1.02, f"наивное {ru(naive, sign=True)} б.п. · "
                           f"чистый в лучшем случае {ru(best_bps, sign=True)} б.п.",
                transform=ax.transAxes, fontsize=8.5, color=MUTED,
                va="bottom", ha="left")
    axes[0].set_ylabel("чистая прибыль net(V), USDT")

    fig.suptitle("Чистая прибыль как функция объёма — случай, ближе всего "
                 "подошедший к прибыли", x=0.01, ha="left", fontsize=12,
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
        # Главное число панели - насколько близко к нулю что-то подходило.
        # Без него видно лишь, что выживших нет, но не видно, сколько
        # не хватило.
        ax.text(1.0, 1.02, f"наблюдений {ru(len(values), 0)} · "
                           f"выше нуля {ru(survivors, 0)} · "
                           f"лучшее {ru(max(values), sign=True)} б.п.",
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
