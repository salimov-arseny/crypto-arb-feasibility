"""Проверка расчёта издержек и ограничений площадок.

Ожидаемые числа посчитаны руками и записаны с выкладкой.
"""

from __future__ import annotations

import pytest

from core.costs import (CostError, ExchangeSpec, InstrumentRules, NetworkSpec,
                        Route, build_route, is_on_tick, network_spec,
                        round_down_to_step, tick_bps)

BUY = ExchangeSpec(key="a", name="Биржа-А", taker_fee=0.001,
                   rules=InstrumentRules(tick_size=0.01, step_size=0.01,
                                         min_qty=0.5, min_notional=5.0))
SELL = ExchangeSpec(key="b", name="Биржа-Б", taker_fee=0.008,
                    rules=InstrumentRules(tick_size=0.01, step_size=0.1,
                                          min_qty=1.0, min_notional=None))
NET = NetworkSpec(coin="COIN", code="NET", withdrawal_fee=0.01,
                  min_withdrawal=2.0, block_time_sec=600.0, confirmations=3)
ROUTE = Route(symbol="COIN/USDT", buy=BUY, sell=SELL, network=NET)


# --------------------------------------------------------------------------
#  Издержки
# --------------------------------------------------------------------------

def test_costs_on_hand_computed_example() -> None:
    """Объём 2, цена покупки 100, цена продажи 101.

        fee_buy    = 2 x 100 x 0,001 = 0,200
        fee_sell   = 2 x 101 x 0,008 = 1,616
        withdrawal = 0,01 x 101      = 1,010
        итого                          2,826
    """
    c = ROUTE.costs(volume=2.0, price_buy=100.0, price_sell=101.0)
    assert c.fee_buy == pytest.approx(0.200)
    assert c.fee_sell == pytest.approx(1.616)
    assert c.withdrawal == pytest.approx(1.010)
    assert c.total == pytest.approx(2.826)
    assert c.variable == pytest.approx(1.816)


def test_trading_fees_scale_with_volume_but_withdrawal_does_not() -> None:
    """Вся задача шага 5 держится на этом различии.

    Удваиваем объём: комиссии сделок удваиваются, плата за вывод
    не меняется.
    """
    one = ROUTE.costs(1.0, 100.0, 101.0)
    two = ROUTE.costs(2.0, 100.0, 101.0)
    assert two.fee_buy == pytest.approx(2 * one.fee_buy)
    assert two.fee_sell == pytest.approx(2 * one.fee_sell)
    assert two.withdrawal == pytest.approx(one.withdrawal)


def test_delivered_volume_is_less_by_withdrawal_fee() -> None:
    """Покупаем 2, доезжает 2 - 0,01 = 1,99."""
    assert ROUTE.delivered_volume(2.0) == pytest.approx(1.99)


@pytest.mark.parametrize("volume", [0.0, -1.0])
def test_nonpositive_volume_rejected(volume: float) -> None:
    with pytest.raises(CostError, match="положительным"):
        ROUTE.costs(volume, 100.0, 101.0)


# --------------------------------------------------------------------------
#  Округление объёма: ловушка плавающей точки
# --------------------------------------------------------------------------

def test_rounding_survives_binary_floating_point() -> None:
    """0.3 // 0.1 в Python даёт 2.0, потому что 0.3/0.1 = 2.9999...

    Наивная реализация превратила бы 0,3 в 0,2 - потеря трети объёма,
    молча и без всякой ошибки.
    """
    assert 0.3 // 0.1 == 2.0                      # вот она, ловушка
    assert round_down_to_step(0.3, 0.1) == pytest.approx(0.3)
    assert round_down_to_step(0.7, 0.1) == pytest.approx(0.7)
    assert round_down_to_step(2.34, 0.1) == pytest.approx(2.3)
    assert round_down_to_step(0.00012345, 0.00001) == pytest.approx(0.00012)


def test_rounding_goes_down_not_to_nearest() -> None:
    """Вверх округлять нельзя: такого объёма может не быть ни в стакане,
    ни на балансе."""
    assert round_down_to_step(2.39, 0.1) == pytest.approx(2.3)
    assert round_down_to_step(2.399999, 0.1) == pytest.approx(2.3)


def test_route_step_is_the_coarser_of_the_two() -> None:
    """Объём обязан быть кратен шагу на обеих площадках: 0,01 и 0,1 -> 0,1."""
    assert ROUTE.volume_step == pytest.approx(0.1)
    assert ROUTE.round_volume(2.34) == pytest.approx(2.3)


def test_zero_step_rejected() -> None:
    with pytest.raises(CostError, match="шаг объёма"):
        round_down_to_step(1.0, 0.0)


# --------------------------------------------------------------------------
#  Шаг цены
# --------------------------------------------------------------------------

def test_tick_applies_to_order_price_not_to_vwap() -> None:
    assert is_on_tick(100.01, 0.01)
    assert not is_on_tick(100.005, 0.01)


def test_tick_in_bps_shows_where_it_matters() -> None:
    """Один и тот же шаг 0,01 значит совершенно разное для разных цен.

        SOL по 100:     0,01 / 100     x 10 000 = 1,00 б.п.
        BTC по 76 624:  0,01 / 76 624  x 10 000 = 0,0013 б.п.
    """
    assert tick_bps(0.01, 100.0) == pytest.approx(1.0)
    assert tick_bps(0.01, 76_624.18) == pytest.approx(0.0013, abs=1e-4)


# --------------------------------------------------------------------------
#  Ограничения
# --------------------------------------------------------------------------

def test_minimum_volume_is_the_binding_constraint() -> None:
    """Четыре ограничения при цене покупки 100:

        минимальный лот на покупке    0,5
        минимальный лот на продаже    1,0
        минимальная сумма вывода      2,0   <- связывает
        минимальная сумма ордера      5 / 100 = 0,05
    """
    assert ROUTE.min_volume(price_buy=100.0) == pytest.approx(2.0)


def test_minimum_volume_can_be_bound_by_notional_at_low_price() -> None:
    """При цене 1 минимальная сумма ордера 5 требует уже 5 монет."""
    assert ROUTE.min_volume(price_buy=1.0) == pytest.approx(5.0)


def test_check_collects_all_reasons_not_just_the_first() -> None:
    """Объём 0,3 нарушает сразу три ограничения.

    При поиске оптимума на шаге 5 полезно видеть, какое именно
    ограничение связывает, а их может быть несколько сразу.
    """
    result = ROUTE.check(0.3, price_buy=100.0, price_sell=101.0)
    assert not result.ok
    assert len(result.reasons) == 3
    assert any("лота на покупке" in r for r in result.reasons)
    assert any("лота на продаже" in r for r in result.reasons)
    assert any("суммы вывода" in r for r in result.reasons)


def test_check_passes_for_admissible_volume() -> None:
    assert ROUTE.check(2.0, 100.0, 101.0).ok


def test_check_rejects_volume_not_on_step() -> None:
    result = ROUTE.check(2.34, 100.0, 101.0)
    assert not result.ok
    assert any("не кратен шагу" in r for r in result.reasons)


def test_check_rejects_volume_eaten_entirely_by_withdrawal_fee() -> None:
    """Вырожденный случай: вывели меньше, чем стоит вывод.

    Отдельная проверка нужна, потому что иначе на шаге 5 такой объём
    выглядел бы обычным убытком, а не невозможностью сделки.
    """
    route = Route(symbol="COIN/USDT", buy=BUY, sell=SELL,
                  network=NetworkSpec(coin="COIN", code="NET",
                                      withdrawal_fee=5.0, min_withdrawal=0.0,
                                      block_time_sec=600.0, confirmations=1))
    result = route.check(5.0, 100.0, 101.0)
    assert any("не меньше самого объёма" in r for r in result.reasons)


# --------------------------------------------------------------------------
#  Время перевода
# --------------------------------------------------------------------------

def test_transfer_time_is_confirmations_times_block_time() -> None:
    """3 подтверждения x 600 с = 1800 с = 30 минут."""
    assert NET.transfer_time_sec == pytest.approx(1800.0)


def test_transfer_time_unknown_when_confirmations_missing() -> None:
    """Для шести значений в конфиге подтверждений нет.

    Это должно давать None, а не ноль: ноль означал бы мгновенный
    перевод и обнулил бы риск на шаге 7.
    """
    net = NetworkSpec(coin="COIN", code="NET", withdrawal_fee=0.01,
                      min_withdrawal=2.0, block_time_sec=600.0,
                      confirmations=None)
    assert net.transfer_time_sec is None


# --------------------------------------------------------------------------
#  Несимметричность направления
# --------------------------------------------------------------------------

CFG = {
    "transfer_asset": {"COIN/USDT": "COIN"},
    "exchanges": {
        "a": {"display_name": "А", "taker_fee": 0.001,
              "instrument_rules": {"COIN/USDT": {"tick_size": 0.01,
                                                 "step_size": 0.01,
                                                 "min_qty": 0.5,
                                                 "min_notional": 5.0}}},
        "b": {"display_name": "Б", "taker_fee": 0.008,
              "instrument_rules": {"COIN/USDT": {"tick_size": 0.01,
                                                 "step_size": 0.1,
                                                 "min_qty": 1.0,
                                                 "min_notional": None}}},
    },
    "networks": {"COIN": [
        {"name": "Дорогая", "code": "SLOW", "block_time_sec": 600.0,
         "withdrawal_fee": {"a": 0.01, "b": 0.02},
         "min_withdrawal": {"a": 2.0, "b": 2.0},
         "confirmations": {"a": 1, "b": 3}},
        {"name": "Дешёвая", "code": "FAST", "block_time_sec": 1.0,
         "withdrawal_fee": {"a": 0.001, "b": 0.002},
         "min_withdrawal": {"a": 0.5, "b": 0.5},
         "confirmations": {"a": 10, "b": 20}},
    ]},
}


def test_withdrawal_belongs_to_sender_confirmations_to_receiver() -> None:
    """Главная несимметричность маршрута.

    Покупаем на А, выводим С А, зачисляется НА Б. Значит комиссия
    и минимум вывода - у А, а подтверждения - у Б.
    """
    net = network_spec(CFG, "COIN", "SLOW", sender="a", receiver="b")
    assert net.withdrawal_fee == pytest.approx(0.01)   # у отправителя А
    assert net.min_withdrawal == pytest.approx(2.0)    # у отправителя А
    assert net.confirmations == 3                      # у получателя Б

    # Обратное направление даёт другие числа - в этом и смысл.
    back = network_spec(CFG, "COIN", "SLOW", sender="b", receiver="a")
    assert back.withdrawal_fee == pytest.approx(0.02)
    assert back.confirmations == 1


def test_build_route_picks_the_cheapest_network_by_default() -> None:
    """Плата за вывод не зависит от объёма, поэтому на малых объёмах
    выбор сети решает исход."""
    route = build_route(CFG, "COIN/USDT", "a", "b")
    assert route.network.code == "FAST"
    assert route.network.withdrawal_fee == pytest.approx(0.001)


def test_build_route_honours_explicit_network() -> None:
    route = build_route(CFG, "COIN/USDT", "a", "b", network_code="SLOW")
    assert route.network.code == "SLOW"


def test_missing_taker_fee_is_an_error_not_a_zero() -> None:
    """Незаполненная комиссия должна ломать расчёт, а не считаться нулём:
    нулевая комиссия дала бы красивую прибыль из ничего."""
    cfg = {**CFG, "exchanges": {**CFG["exchanges"],
                                "a": {**CFG["exchanges"]["a"], "taker_fee": None}}}
    with pytest.raises(CostError, match="taker_fee"):
        build_route(cfg, "COIN/USDT", "a", "b")
