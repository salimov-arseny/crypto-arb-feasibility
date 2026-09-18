"""Чистая прибыль net(V) и поиск оптимального объёма.

    net(V) = V * (P_sell(V) - P_buy(V)) - fee_buy - fee_sell - withdrawal

Почему максимум существует. Плата за вывод фиксирована: на малых объёмах
она съедает всю прибыль. Проскальзывание растёт с объёмом: на больших
объёмах оно закрывает спред. Между двумя краями лежит оптимум.

Почему он единственный. Затраты на покупку - сумма по аскам в порядке
возрастания цены, то есть ВЫПУКЛАЯ функция объёма. Выручка от продажи -
сумма по бидам в порядке убывания, то есть ВОГНУТАЯ. Заметив, что
V * P_buy(V) это ровно затраты C(V), а V * P_sell(V) ровно выручка R(V),
формулу можно переписать:

    net(V) = R(V) * (1 - f_sell) - C(V) * (1 + f_buy) - w * P_sell(V)

Первые два слагаемых дают вогнутую минус выпуклую, то есть вогнутую
функцию. Значит максимум один, и поиск по сетке с уточнением законен.

Оговорка: последнее слагаемое от V слегка зависит и строгую вогнутость
формально портит. Измерение на шаге 4 показало, что эффект порядка
1e-5 базисного пункта, поэтому на практике им можно пренебречь -
но проверять вогнутость надо численно, а не ссылкой на теорему.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.costs import Costs, Route, round_up_to_step
from core.orderbook import OrderBook

# Сколько точек в грубой сетке и сколько шагов уточнения. Сетка
# геометрическая: объёмы интересны на разных порядках, и равномерная
# сетка тратила бы почти все точки на самые большие значения.
GRID_POINTS = 64
REFINE_STEPS = 60


@dataclass(frozen=True)
class NetResult:
    """Чистая прибыль при конкретном объёме и всё, из чего она сложилась."""

    volume: float
    price_buy: float          # VWAP покупки
    price_sell: float         # VWAP продажи
    gross: float
    costs: Costs
    net: float
    complete: bool            # хватило ли видимой глубины с обеих сторон
    feasible: bool            # прошёл ли объём ограничения площадок
    reasons: tuple[str, ...]  # почему не прошёл

    @property
    def turnover(self) -> float:
        """Оборот по цене покупки - база для перевода в базисные пункты."""
        return self.volume * self.price_buy

    @property
    def net_bps(self) -> float:
        return self.net / self.turnover * 10_000

    @property
    def usable(self) -> bool:
        """Годится ли результат как ответ: допустим и посчитан по реальной глубине."""
        return self.feasible and self.complete


# Две разные цели поиска, и путать их нельзя.
#
#   "net"     - максимум прибыли в деньгах. Это и есть вопрос README:
#               при каком объёме прибыль максимальна.
#   "net_bps" - максимум прибыли, отнесённой к обороту.
#
# На прибыльном маршруте обе цели осмысленны. На убыточном первая
# вырождается: net(V) монотонно убывает, максимум садится на нижнюю
# границу, и «оптимальный объём» оказывается минимально допустимым -
# скажем, 8 USDT, где вся плата за вывод приходится на восемь долларов
# оборота и даёт -2000 б.п. Число верное, но бессмысленное как мера
# недостающего спреда. Для вопроса «насколько не дотягивает маршрут»
# нужна вторая цель.
OBJECTIVES = ("net", "net_bps")


@dataclass(frozen=True)
class Optimum:
    """Итог поиска по маршруту."""

    route: Route
    best: NetResult | None
    bounds: tuple[float, float] | None   # где искали
    evaluated: int
    objective: str = "net"
    reason: str = ""                     # если искать было негде

    @property
    def profitable(self) -> bool:
        """Прибыльна ли возможность.

        Отрицательный максимум - это не «вот оптимум с убытком», а ответ
        «возможности нет». Различать обязательно: в коэффициент
        выживаемости на шаге 8 попадают только прибыльные.
        """
        return self.best is not None and self.best.net > 0


def net_at(route: Route, book_buy: OrderBook, book_sell: OrderBook,
           volume: float) -> NetResult:
    """Посчитать net(V) при заданном объёме.

    Покупаем `volume` по аскам биржи-отправителя, продаём по бидам
    биржи-получателя. Обе цены - средневзвешенные, проскальзывание
    в них уже учтено.
    """
    execution_buy = book_buy.buy(volume)
    execution_sell = book_sell.sell(volume)

    price_buy = execution_buy.vwap
    price_sell = execution_sell.vwap
    gross = volume * (price_sell - price_buy)
    costs = route.costs(volume, price_buy, price_sell)
    check = route.check(volume, price_buy, price_sell)

    return NetResult(
        volume=volume,
        price_buy=price_buy,
        price_sell=price_sell,
        gross=gross,
        costs=costs,
        net=gross - costs.total,
        # Если глубины не хватило хотя бы с одной стороны, цена исполнения
        # получилась лучше настоящей, и прибыль завышена. README называет
        # это оценкой сверху; наружу это выходит флагом, а не молчанием.
        complete=execution_buy.complete and execution_sell.complete,
        feasible=check.ok,
        reasons=check.reasons,
    )


def search_bounds(route: Route, book_buy: OrderBook,
                  book_sell: OrderBook) -> tuple[float, float] | None:
    """Границы поиска: от минимально допустимого объёма до видимой глубины.

    Снизу связывают ограничения площадок - минимальный лот, минимальная
    сумма ордера, минимальная сумма вывода. Сверху - глубина стакана:
    за её пределами расчёт даёт завышенную прибыль, и заходить туда
    нельзя даже ради красивого графика.
    """
    # Вверх до ближайшего допустимого шага: округление вниз дало бы объём
    # меньше минимально допустимого.
    low = round_up_to_step(route.min_volume(book_buy.best_ask),
                           route.volume_step)
    high = route.round_volume(min(book_buy.depth("ask"), book_sell.depth("bid")))

    if high < low:
        return None
    return low, high


def _geometric_grid(low: float, high: float, points: int) -> list[float]:
    """Геометрическая сетка: объёмы интересны на разных порядках сразу."""
    if points < 2 or low <= 0 or high <= low:
        return [low]
    ratio = (high / low) ** (1 / (points - 1))
    return [low * ratio ** i for i in range(points)]


def find_optimum(route: Route, book_buy: OrderBook, book_sell: OrderBook,
                 grid_points: int = GRID_POINTS,
                 refine_steps: int = REFINE_STEPS,
                 objective: str = "net") -> Optimum:
    """Найти объём, максимизирующий net(V).

    Сначала грубая геометрическая сетка, потом тернарный поиск внутри
    интервала, где нашёлся лучший узел. Тернарный поиск законен, потому
    что функция вогнута; сетка перед ним нужна, чтобы не упереться
    в плоский участок кусочно-линейной функции на первом же шаге.

    В конце объём округляется до шага площадки, и проверяются соседние
    допустимые объёмы: округление могло сдвинуть результат.
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"цель должна быть одной из {OBJECTIVES}, "
                         f"получено {objective!r}")

    bounds = search_bounds(route, book_buy, book_sell)
    if bounds is None:
        return Optimum(route=route, best=None, bounds=None, evaluated=0,
                       objective=objective,
                       reason="минимально допустимый объём больше видимой "
                              "глубины стакана")

    low, high = bounds
    evaluated = 0
    score = (lambda r: r.net) if objective == "net" else (lambda r: r.net_bps)

    def snap(volume: float) -> float:
        """Привести произвольный объём к ближайшему допустимому снизу.

        Узлы сетки и середины интервалов - произвольные числа вроде
        39,91533..., а площадка принимает только объёмы, кратные шагу.
        Округлять надо ЗДЕСЬ, до вычисления: если этого не делать,
        каждый пробный объём окажется недопустимым, все значения станут
        минус бесконечностью и поиск сядет на границу.
        """
        return max(low, min(high, route.round_volume(volume)))

    def value(volume: float) -> float:
        """Значение для сравнения. Недопустимые объёмы выбывают."""
        nonlocal evaluated
        evaluated += 1
        result = net_at(route, book_buy, book_sell, snap(volume))
        return score(result) if result.usable else -math.inf

    grid = _geometric_grid(low, high, grid_points)
    values = [value(v) for v in grid]
    best_index = max(range(len(grid)), key=lambda i: values[i])

    if values[best_index] == -math.inf:
        return Optimum(route=route, best=None, bounds=bounds,
                       evaluated=evaluated, objective=objective,
                       reason="ни один объём в границах не прошёл ограничения")

    # Уточняем внутри интервала между соседями лучшего узла.
    left = grid[best_index - 1] if best_index > 0 else low
    right = grid[best_index + 1] if best_index + 1 < len(grid) else high

    for _ in range(refine_steps):
        if right - left <= route.volume_step:
            break
        third = (right - left) / 3
        m1, m2 = left + third, right - third
        if value(m1) < value(m2):
            left = m1
        else:
            right = m2

    # Округление до шага может сдвинуть результат, поэтому проверяем
    # и соседние допустимые объёмы, а не только сам округлённый.
    centre = snap((left + right) / 2)
    step = route.volume_step
    candidates = {snap(grid[best_index]), centre,
                  snap(centre - step), snap(centre + step)}

    best: NetResult | None = None
    for volume in sorted(candidates):
        if volume < low or volume > high:
            continue
        evaluated += 1
        result = net_at(route, book_buy, book_sell, volume)
        if not result.usable:
            continue
        if best is None or score(result) > score(best):
            best = result

    if best is None:
        return Optimum(route=route, best=None, bounds=bounds,
                       evaluated=evaluated, objective=objective,
                       reason="после округления до шага допустимых объёмов "
                              "не осталось")
    return Optimum(route=route, best=best, bounds=bounds,
                   evaluated=evaluated, objective=objective)


def profile(route: Route, book_buy: OrderBook, book_sell: OrderBook,
            points: int = GRID_POINTS) -> list[NetResult]:
    """net(V) по сетке целиком - для графика на шаге 8 и для проверок."""
    bounds = search_bounds(route, book_buy, book_sell)
    if bounds is None:
        return []
    low, high = bounds
    return [net_at(route, book_buy, book_sell,
                   max(low, min(high, route.round_volume(v))))
            for v in _geometric_grid(low, high, points)]
