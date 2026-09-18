"""Проверка net(V) и поиска оптимального объёма.

Стаканы подобраны так, чтобы оптимум считался руками.

    биржа А, аски (покупаем)      биржа Б, биды (продаём)
    100,00 x 10                   101,00 x 10
    100,10 x 10                   100,90 x 10
    100,20 x 10                   100,80 x 10
    ... шаг 0,10, всего 20 уровней по 10 -> глубина 200 с каждой стороны

Комиссии по 0,1 %, плата за вывод 0,05 монеты.

Где оптимум. Прибыль растёт, пока очередная монета приносит больше,
чем стоит. На k-м уровне продаём по 101 - 0,1k, покупаем по 100 + 0,1k:

    (101 - 0,1k) x 0,999 = (100 + 0,1k) x 1,001
    100,899 - 0,0999k    = 100,1 + 0,1001k
    0,799 = 0,2k   ->   k = 3,995

То есть граница проходит на четвёртом уровне, и оптимум - ровно V = 40.
"""

from __future__ import annotations

import math

import pytest

from core.arbitrage import find_optimum, net_at, profile, search_bounds
from core.costs import ExchangeSpec, InstrumentRules, NetworkSpec, Route
from core.orderbook import OrderBook

LEVELS = 20
QTY = 10.0


def ladder(start: float, step: float, count: int = LEVELS) -> tuple:
    return tuple((round(start + step * i, 6), QTY) for i in range(count))


BOOK_BUY = OrderBook(exchange="А", symbol="COIN/USDT", fetched_at=0.0,
                     asks=ladder(100.0, +0.10),      # покупаем здесь
                     bids=ladder(99.90, -0.10))
BOOK_SELL = OrderBook(exchange="Б", symbol="COIN/USDT", fetched_at=0.0,
                      bids=ladder(101.0, -0.10),     # продаём здесь
                      asks=ladder(101.10, +0.10))

RULES = InstrumentRules(tick_size=0.01, step_size=0.001,
                        min_qty=0.001, min_notional=None)


def make_route(taker: float = 0.001, withdrawal: float = 0.05) -> Route:
    return Route(
        symbol="COIN/USDT",
        buy=ExchangeSpec("a", "А", taker, RULES),
        sell=ExchangeSpec("b", "Б", taker, RULES),
        network=NetworkSpec(coin="COIN", code="NET", withdrawal_fee=withdrawal,
                            min_withdrawal=0.1, block_time_sec=60.0,
                            confirmations=2))


ROUTE = make_route()


# --------------------------------------------------------------------------
#  net(V) на числах, посчитанных руками
# --------------------------------------------------------------------------

def test_net_at_thirty_units() -> None:
    """Объём 30 - ровно три уровня с каждой стороны.

        затраты  C = 10(100,0 + 100,1 + 100,2) = 3003   -> P_buy  = 100,1
        выручка  R = 10(101,0 + 100,9 + 100,8) = 3027   -> P_sell = 100,9
        gross      = 3027 - 3003                        = 24
        fee_buy    = 3003 x 0,001                       =  3,003
        fee_sell   = 3027 x 0,001                       =  3,027
        withdrawal = 0,05 x 100,9                       =  5,045
        net        = 24 - 3,003 - 3,027 - 5,045         = 12,925
    """
    r = net_at(ROUTE, BOOK_BUY, BOOK_SELL, 30.0)
    assert r.price_buy == pytest.approx(100.1)
    assert r.price_sell == pytest.approx(100.9)
    assert r.gross == pytest.approx(24.0)
    assert r.costs.fee_buy == pytest.approx(3.003)
    assert r.costs.fee_sell == pytest.approx(3.027)
    assert r.costs.withdrawal == pytest.approx(5.045)
    assert r.net == pytest.approx(12.925)
    assert r.usable


def test_net_is_negative_at_small_volume() -> None:
    """Нижний край: плата за вывод съедает всё.

        gross = 1 x (101 - 100) = 1
        fees  = 0,1 + 0,101     = 0,201
        вывод = 0,05 x 101      = 5,05
        net   = 1 - 0,201 - 5,05 = -4,251
    """
    r = net_at(ROUTE, BOOK_BUY, BOOK_SELL, 1.0)
    assert r.net == pytest.approx(-4.251)
    assert r.net < 0


def test_net_is_negative_at_full_depth() -> None:
    """Верхний край: проскальзывание закрывает спред и уводит его в минус.

        P_buy  = среднее 100,0..101,9 = 100,95   -> C = 20190
        P_sell = среднее 101,0.. 99,1 = 100,05   -> R = 20010
        gross  = 20010 - 20190 = -180
        net    = -180 - 20,19 - 20,01 - 5,0025 = -225,2025
    """
    r = net_at(ROUTE, BOOK_BUY, BOOK_SELL, 200.0)
    assert r.price_buy == pytest.approx(100.95)
    assert r.price_sell == pytest.approx(100.05)
    assert r.gross == pytest.approx(-180.0)
    assert r.net == pytest.approx(-225.2025)


def test_net_bps_relates_profit_to_turnover(  ) -> None:
    """12,925 / (30 x 100,1) x 10 000 = 43,04 б.п."""
    r = net_at(ROUTE, BOOK_BUY, BOOK_SELL, 30.0)
    assert r.turnover == pytest.approx(3003.0)
    assert r.net_bps == pytest.approx(12.925 / 3003.0 * 1e4)


# --------------------------------------------------------------------------
#  Оптимум существует и находится
# --------------------------------------------------------------------------

def test_optimum_is_where_the_marginal_unit_stops_paying() -> None:
    """Предельное условие дало V = 40. Проверяем, что поиск туда и приходит.

        C = 10(100,0 + 100,1 + 100,2 + 100,3) = 4006   -> P_buy  = 100,15
        R = 10(101,0 + 100,9 + 100,8 + 100,7) = 4034   -> P_sell = 100,85
        gross = 28
        net   = 28 - 4,006 - 4,034 - 5,0425 = 14,9175
    """
    hand = net_at(ROUTE, BOOK_BUY, BOOK_SELL, 40.0)
    assert hand.net == pytest.approx(14.9175)

    found = find_optimum(ROUTE, BOOK_BUY, BOOK_SELL)
    assert found.profitable
    assert found.best.volume == pytest.approx(40.0, abs=0.5)
    assert found.best.net == pytest.approx(14.9175, abs=1e-3)


def test_optimum_is_no_worse_than_brute_force() -> None:
    """Независимая проверка: перебор по мелкой сетке не должен найти лучше.

    Именно «не хуже», а не «столько же». Равномерная сетка из 4000 точек
    не обязана попасть в излом кусочно-линейной функции, а уточняющий
    поиск в него попадает. Требовать совпадения значило бы требовать,
    чтобы поиск был ровно так же неточен, как перебор.
    """
    low, high = search_bounds(ROUTE, BOOK_BUY, BOOK_SELL)
    sweep = max(net_at(ROUTE, BOOK_BUY, BOOK_SELL,
                       ROUTE.round_volume(low + (high - low) * i / 4000)).net
                for i in range(1, 4001))
    found = find_optimum(ROUTE, BOOK_BUY, BOOK_SELL)
    assert found.best.net >= sweep - 1e-9
    assert found.best.net == pytest.approx(sweep, rel=1e-4)


def test_net_is_better_at_optimum_than_at_either_edge() -> None:
    """То, ради чего оптимум вообще ищется."""
    low, high = search_bounds(ROUTE, BOOK_BUY, BOOK_SELL)
    found = find_optimum(ROUTE, BOOK_BUY, BOOK_SELL)
    at_low = net_at(ROUTE, BOOK_BUY, BOOK_SELL, low)
    at_high = net_at(ROUTE, BOOK_BUY, BOOK_SELL, high)
    assert found.best.net > at_low.net
    assert found.best.net > at_high.net


def test_net_is_unimodal_rises_then_falls() -> None:
    """Вогнутость проверяем численно, а не ссылкой на теорему.

    Затраты выпуклы, выручка вогнута, значит их разность вогнута - но
    слагаемое с платой за вывод формально это портит. На сетке
    у разностей соседних значений должна быть ровно одна смена знака.
    """
    values = [r.net for r in profile(ROUTE, BOOK_BUY, BOOK_SELL, points=200)]
    diffs = [b - a for a, b in zip(values, values[1:])]
    sign_changes = sum(1 for a, b in zip(diffs, diffs[1:])
                       if a > 0 >= b or a < 0 <= b)
    assert sign_changes == 1


# --------------------------------------------------------------------------
#  Когда возможности нет
# --------------------------------------------------------------------------

def test_no_opportunity_when_fees_eat_the_spread() -> None:
    """Комиссия 0,8 % против спреда в 1 % - прибыли не остаётся нигде.

    Ответ должен быть «возможности нет», а не «вот оптимум с убытком».
    """
    route = make_route(taker=0.008)
    found = find_optimum(route, BOOK_BUY, BOOK_SELL)
    assert not found.profitable
    assert found.best is None or found.best.net < 0


def test_no_opportunity_when_withdrawal_exceeds_any_gain() -> None:
    """Плата за вывод больше, чем можно заработать на всей глубине."""
    route = make_route(withdrawal=5.0)
    found = find_optimum(route, BOOK_BUY, BOOK_SELL)
    assert not found.profitable


# --------------------------------------------------------------------------
#  Границы поиска
# --------------------------------------------------------------------------

def test_search_never_goes_beyond_visible_depth() -> None:
    """За пределами стакана расчёт завышает прибыль - туда нельзя."""
    low, high = search_bounds(ROUTE, BOOK_BUY, BOOK_SELL)
    assert high <= min(BOOK_BUY.depth("ask"), BOOK_SELL.depth("bid"))
    found = find_optimum(ROUTE, BOOK_BUY, BOOK_SELL)
    assert found.best.volume <= high
    assert found.best.complete


def test_no_bounds_when_minimum_exceeds_depth() -> None:
    """Минимальный вывод больше всей видимой глубины - искать негде."""
    route = Route(symbol="COIN/USDT",
                  buy=ExchangeSpec("a", "А", 0.001, RULES),
                  sell=ExchangeSpec("b", "Б", 0.001, RULES),
                  network=NetworkSpec(coin="COIN", code="NET",
                                      withdrawal_fee=0.05, min_withdrawal=1e6,
                                      block_time_sec=60.0, confirmations=2))
    assert search_bounds(route, BOOK_BUY, BOOK_SELL) is None
    found = find_optimum(route, BOOK_BUY, BOOK_SELL)
    assert found.best is None
    assert "глубины" in found.reason


def test_result_beyond_depth_is_marked_incomplete() -> None:
    """Если всё же посчитать объём больше глубины, он помечается негодным."""
    r = net_at(ROUTE, BOOK_BUY, BOOK_SELL, 500.0)
    assert not r.complete
    assert not r.usable


def test_optimum_volume_is_on_the_exchange_step() -> None:
    found = find_optimum(ROUTE, BOOK_BUY, BOOK_SELL)
    assert ROUTE.round_volume(found.best.volume) == pytest.approx(
        found.best.volume)
    assert found.best.feasible


def test_search_is_cheap() -> None:
    """Сетка плюс уточнение - сотни вычислений, а не десятки тысяч."""
    found = find_optimum(ROUTE, BOOK_BUY, BOOK_SELL)
    assert found.evaluated < 500
    assert math.isfinite(found.best.net)


# --------------------------------------------------------------------------
#  Две цели поиска
# --------------------------------------------------------------------------

def test_two_objectives_agree_on_a_profitable_route() -> None:
    """Там, где прибыль есть, обе цели дают осмысленный ответ."""
    by_net = find_optimum(ROUTE, BOOK_BUY, BOOK_SELL, objective="net")
    by_bps = find_optimum(ROUTE, BOOK_BUY, BOOK_SELL, objective="net_bps")
    assert by_net.profitable and by_bps.profitable
    assert by_net.best.net >= by_bps.best.net          # цель «деньги» не хуже в деньгах
    assert by_bps.best.net_bps >= by_net.best.net_bps  # и наоборот


def test_net_objective_degenerates_on_an_unprofitable_route() -> None:
    """Главная причина, по которой цель пришлось сделать выбираемой.

    Когда прибыли нет нигде, net(V) монотонно убывает и максимум садится
    на нижнюю границу: «оптимальный объём» оказывается минимально
    допустимым, а net в базисных пунктах - огромным по модулю, потому
    что фиксированная плата за вывод делится на крошечный оборот.
    Цель net_bps даёт объём побольше и величину, пригодную как мера
    недостающего спреда.
    """
    route = make_route(taker=0.008)
    low, _ = search_bounds(route, BOOK_BUY, BOOK_SELL)
    by_net = find_optimum(route, BOOK_BUY, BOOK_SELL, objective="net")
    by_bps = find_optimum(route, BOOK_BUY, BOOK_SELL, objective="net_bps")

    assert not by_net.profitable and not by_bps.profitable
    assert by_net.best.volume == pytest.approx(low)      # села на границу
    assert by_bps.best.volume > by_net.best.volume
    assert by_bps.best.net_bps > by_net.best.net_bps


def test_unknown_objective_rejected() -> None:
    with pytest.raises(ValueError, match="цель"):
        find_optimum(ROUTE, BOOK_BUY, BOOK_SELL, objective="прибыль")
