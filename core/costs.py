"""Издержки сделки и ограничения площадок.

Здесь считается, сколько стоит провести объём V по маршруту «купить на A,
перевести, продать на B», и допустим ли такой объём вообще. Прибыль тут
не считается - это работа arbitrage.py.

Три слоя издержек из README:

    fee_buy    = V * P_buy(V)  * taker_fee_A     растёт с объёмом
    fee_sell   = V * P_sell(V) * taker_fee_B     растёт с объёмом
    withdrawal = withdrawal_fee(монета) * P_sell НЕ зависит от объёма

Последняя строка и создаёт задачу поиска оптимума: фиксированная плата
за вывод съедает всю прибыль на малых объёмах, а на больших размывается.

Направление маршрута несимметрично, и это главный источник ошибок.
Покупаем на A, выводим С БИРЖИ A, зачисляется НА БИРЖУ B, продаём на B.
Значит комиссия за вывод и минимальная сумма вывода - параметры
отправителя A, а число подтверждений для зачисления - параметр
получателя B.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any


class CostError(ValueError):
    """Ошибка в постановке задачи: нет нужных данных или объём бессмыслен."""


# --------------------------------------------------------------------------
#  Округление по шагу
# --------------------------------------------------------------------------

def round_down_to_step(value: float, step: float) -> float:
    """Округлить объём вниз до кратного шагу.

    Вниз, а не к ближайшему: округление вверх дало бы объём, которого может
    не оказаться ни в стакане, ни на балансе.

    Считаем через Decimal, потому что с плавающей точкой это место ломается
    молча. Пример: 0.3 // 0.1 в Python даёт 2.0, так как 0.3/0.1 = 2.9999...,
    и наивное округление превратило бы 0,3 в 0,2 - потеря трети объёма.
    """
    if step <= 0:
        raise CostError(f"шаг объёма должен быть положительным, получено {step}")
    if value < 0:
        raise CostError(f"объём не может быть отрицательным: {value}")
    # str() даёт то десятичное представление, которое имел в виду человек,
    # а не двоичное приближение.
    units = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(ROUND_DOWN)
    return float(units * Decimal(str(step)))


def round_up_to_step(value: float, step: float) -> float:
    """Округлить объём вверх до кратного шагу.

    Нужно для нижней границы поиска: округление минимально допустимого
    объёма ВНИЗ дало бы объём меньше минимально допустимого. Считаем
    через Decimal по той же причине, что и round_down_to_step.
    """
    if step <= 0:
        raise CostError(f"шаг объёма должен быть положительным, получено {step}")
    if value < 0:
        raise CostError(f"объём не может быть отрицательным: {value}")
    units = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(ROUND_UP)
    return float(units * Decimal(str(step)))


def is_on_tick(price: float, tick: float) -> bool:
    """Кратна ли цена шагу котировки.

    Применяется к цене ЗАЯВКИ. К средневзвешенной цене исполнения это
    требование не относится: VWAP - среднее нескольких цен, и кратным
    тику быть не обязано.
    """
    if tick <= 0:
        raise CostError(f"шаг цены должен быть положительным, получено {tick}")
    remainder = Decimal(str(price)) % Decimal(str(tick))
    return remainder == 0


def tick_bps(tick: float, price: float) -> float:
    """Один шаг цены в базисных пунктах - пол наблюдаемого спреда.

    Для SOL по цене около 100 шаг 0,01 даёт 0,99 б.п., и спред у́же этого
    физически невозможен. Для BTC по 76 000 тот же шаг даёт 0,0013 б.п.
    и ни на что не влияет.
    """
    return tick / price * 10_000


# --------------------------------------------------------------------------
#  Параметры площадок и сетей
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class InstrumentRules:
    """Ограничения площадки по инструменту."""

    tick_size: float
    step_size: float
    min_qty: float
    min_notional: float | None   # OKX его не публикует; тогда ограничения нет


@dataclass(frozen=True)
class ExchangeSpec:
    key: str
    name: str
    taker_fee: float
    rules: InstrumentRules


@dataclass(frozen=True)
class NetworkSpec:
    """Сеть перевода. Параметры взяты с той стороны, которой они принадлежат."""

    coin: str
    code: str
    withdrawal_fee: float      # в монете, у биржи-ОТПРАВИТЕЛЯ
    min_withdrawal: float      # в монете, у биржи-ОТПРАВИТЕЛЯ
    block_time_sec: float
    confirmations: int | None  # у биржи-ПОЛУЧАТЕЛЯ; None, если неизвестно

    @property
    def transfer_time_sec(self) -> float | None:
        """Длительность перевода = подтверждения x время блока."""
        if self.confirmations is None:
            return None
        return self.confirmations * self.block_time_sec


# --------------------------------------------------------------------------
#  Результаты
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Costs:
    """Издержки одной сделки объёма V, всё в котируемой валюте."""

    fee_buy: float
    fee_sell: float
    withdrawal: float

    @property
    def total(self) -> float:
        return self.fee_buy + self.fee_sell + self.withdrawal

    @property
    def variable(self) -> float:
        """Часть, растущая с объёмом."""
        return self.fee_buy + self.fee_sell


@dataclass(frozen=True)
class Feasibility:
    """Допустим ли объём. reasons пуст тогда и только тогда, когда допустим."""

    volume: float
    reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.reasons


# --------------------------------------------------------------------------
#  Маршрут
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    """Купить на `buy`, перевести сетью `network`, продать на `sell`."""

    symbol: str
    buy: ExchangeSpec
    sell: ExchangeSpec
    network: NetworkSpec

    def __str__(self) -> str:
        return (f"{self.buy.name} -> {self.sell.name} "
                f"{self.symbol} ({self.network.code})")

    # ---- издержки --------------------------------------------------------

    def costs(self, volume: float, price_buy: float, price_sell: float) -> Costs:
        """Издержки объёма V при заданных ценах исполнения.

        price_buy и price_sell - это VWAP, а не лучшие цены: проскальзывание
        уже учтено в них самих (core/orderbook.py).
        """
        if volume <= 0:
            raise CostError(f"объём должен быть положительным, получено {volume}")
        return Costs(
            fee_buy=volume * price_buy * self.buy.taker_fee,
            fee_sell=volume * price_sell * self.sell.taker_fee,
            # Плата за вывод берётся в монете у отправителя; в деньги её
            # переводим по цене продажи, как записано в README.
            withdrawal=self.network.withdrawal_fee * price_sell,
        )

    def delivered_volume(self, volume: float) -> float:
        """Сколько монеты реально доедет до биржи-получателя."""
        return volume - self.network.withdrawal_fee

    # ---- ограничения -----------------------------------------------------

    @property
    def volume_step(self) -> float:
        """Шаг объёма, годный для обеих площадок.

        Объём обязан быть кратен шагу и на покупке, и на продаже. У всех
        четырёх бирж шаги - степени десятки, поэтому более крупный шаг
        кратен более мелкому, и достаточно взять больший из двух.
        """
        return max(self.buy.rules.step_size, self.sell.rules.step_size)

    def min_volume(self, price_buy: float) -> float:
        """Наименьший допустимый объём - максимум из четырёх ограничений."""
        limits = [
            self.buy.rules.min_qty,
            self.sell.rules.min_qty,
            self.network.min_withdrawal,
        ]
        if self.buy.rules.min_notional is not None:
            limits.append(self.buy.rules.min_notional / price_buy)
        return max(limits)

    def round_volume(self, volume: float) -> float:
        return round_down_to_step(volume, self.volume_step)

    def check(self, volume: float, price_buy: float,
              price_sell: float) -> Feasibility:
        """Собрать все причины, по которым объём недопустим.

        Собрать ВСЕ, а не вернуть первую: при поиске оптимума на шаге 5
        полезно видеть, какое именно ограничение связывает, а их может
        связывать сразу несколько.
        """
        reasons: list[str] = []

        if volume <= 0:
            return Feasibility(volume, ("объём не положителен",))

        if volume < self.buy.rules.min_qty:
            reasons.append(f"меньше минимального лота на покупке "
                           f"({self.buy.rules.min_qty} {self.network.coin} "
                           f"у {self.buy.name})")
        if volume < self.sell.rules.min_qty:
            reasons.append(f"меньше минимального лота на продаже "
                           f"({self.sell.rules.min_qty} {self.network.coin} "
                           f"у {self.sell.name})")

        notional_buy = volume * price_buy
        if (self.buy.rules.min_notional is not None
                and notional_buy < self.buy.rules.min_notional):
            reasons.append(f"сумма покупки {notional_buy:.2f} меньше минимальной "
                           f"({self.buy.rules.min_notional} у {self.buy.name})")

        notional_sell = self.delivered_volume(volume) * price_sell
        if (self.sell.rules.min_notional is not None
                and notional_sell < self.sell.rules.min_notional):
            reasons.append(f"сумма продажи {notional_sell:.2f} меньше минимальной "
                           f"({self.sell.rules.min_notional} у {self.sell.name})")

        if volume < self.network.min_withdrawal:
            reasons.append(f"меньше минимальной суммы вывода "
                           f"({self.network.min_withdrawal} {self.network.coin} "
                           f"у {self.buy.name})")

        # Вырожденный случай: комиссия съедает весь перевод. Проверяем
        # отдельно, потому что на шаге 5 такой объём дал бы отрицательную
        # выручку и выглядел бы как обычный убыток, а не как невозможность.
        if self.delivered_volume(volume) <= 0:
            reasons.append(f"комиссия за вывод {self.network.withdrawal_fee} "
                           f"{self.network.coin} не меньше самого объёма")

        rounded = self.round_volume(volume)
        if abs(rounded - volume) > 1e-15:
            reasons.append(f"объём не кратен шагу {self.volume_step}: "
                           f"ближайший допустимый {rounded}")

        return Feasibility(volume, tuple(reasons))


# --------------------------------------------------------------------------
#  Сборка из конфига
# --------------------------------------------------------------------------

def exchange_spec(cfg: dict[str, Any], key: str, symbol: str) -> ExchangeSpec:
    ex = cfg["exchanges"][key]
    if ex.get("taker_fee") is None:
        raise CostError(f"у биржи {key} не заполнена taker_fee")
    r = ex["instrument_rules"][symbol]
    return ExchangeSpec(
        key=key, name=ex["display_name"], taker_fee=float(ex["taker_fee"]),
        rules=InstrumentRules(tick_size=float(r["tick_size"]),
                              step_size=float(r["step_size"]),
                              min_qty=float(r["min_qty"]),
                              min_notional=(None if r["min_notional"] is None
                                            else float(r["min_notional"]))))


def network_spec(cfg: dict[str, Any], coin: str, code: str,
                 sender: str, receiver: str) -> NetworkSpec:
    """Собрать параметры сети для конкретного направления перевода.

    sender и receiver - ключи бирж. Комиссия и минимум берутся у
    отправителя, подтверждения у получателя: платит за вывод тот, кто
    выводит, а ждёт подтверждений тот, кто зачисляет.
    """
    for net in cfg["networks"][coin]:
        if net["code"] != code:
            continue
        fee = (net["withdrawal_fee"] or {}).get(sender)
        minimum = (net["min_withdrawal"] or {}).get(sender)
        if fee is None or minimum is None:
            raise CostError(f"нет параметров вывода {coin}/{code} у {sender}")
        if net.get("block_time_sec") is None:
            raise CostError(f"нет времени блока для {coin}/{code}")
        confirmations = (net["confirmations"] or {}).get(receiver)
        return NetworkSpec(
            coin=coin, code=code,
            withdrawal_fee=float(fee), min_withdrawal=float(minimum),
            block_time_sec=float(net["block_time_sec"]),
            confirmations=(None if confirmations is None else int(confirmations)))
    raise CostError(f"нет сети {code} для монеты {coin}")


def build_route(cfg: dict[str, Any], symbol: str, buy_key: str, sell_key: str,
                network_code: str | None = None) -> Route:
    """Собрать маршрут по конфигу.

    Если сеть не указана, берётся самая дешёвая из доступных отправителю:
    плата за вывод не зависит от объёма, поэтому на малых объёмах выбор
    сети решает исход.
    """
    coin = cfg["transfer_asset"][symbol]
    buy = exchange_spec(cfg, buy_key, symbol)
    sell = exchange_spec(cfg, sell_key, symbol)

    if network_code is not None:
        net = network_spec(cfg, coin, network_code, buy_key, sell_key)
        return Route(symbol=symbol, buy=buy, sell=sell, network=net)

    candidates: list[NetworkSpec] = []
    for entry in cfg["networks"][coin]:
        try:
            candidates.append(
                network_spec(cfg, coin, entry["code"], buy_key, sell_key))
        except CostError:
            continue          # у этого отправителя сеть не заполнена
    if not candidates:
        raise CostError(f"нет ни одной заполненной сети для {coin} "
                        f"с отправителем {buy_key}")
    return Route(symbol=symbol, buy=buy, sell=sell,
                 network=min(candidates, key=lambda n: n.withdrawal_fee))
