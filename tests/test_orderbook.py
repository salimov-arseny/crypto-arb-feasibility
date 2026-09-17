"""Проверка нормализации стакана и расчёта цены исполнения.

Все ожидаемые числа посчитаны руками и записаны в тесте вместе с
выкладкой. Тест, где ожидание получено прогоном самой функции, проверяет
не правильность, а неизменность: он поймает случайную правку, но не
исходную ошибку.

Стакан, на котором считаем:

    аски (продают)          биды (покупают)
    100,00 x 2              99,00 x 1
    101,00 x 3              98,00 x 4
    103,00 x 5              95,00 x 5

Числа подобраны так, чтобы деления давали короткие десятичные дроби
и результат можно было проверить в уме.
"""

from __future__ import annotations

import pytest

from core.orderbook import OrderBook, OrderBookError

ASKS = [(100.0, 2.0), (101.0, 3.0), (103.0, 5.0)]
BIDS = [(99.0, 1.0), (98.0, 4.0), (95.0, 5.0)]


@pytest.fixture
def book() -> OrderBook:
    return OrderBook(exchange="ТЕСТ", symbol="X/Y",
                     bids=tuple(BIDS), asks=tuple(ASKS), fetched_at=0.0)


# --------------------------------------------------------------------------
#  Наблюдаемые величины
# --------------------------------------------------------------------------

def test_best_prices_and_spread(book: OrderBook) -> None:
    assert book.best_bid == 99.0
    assert book.best_ask == 100.0
    assert book.mid == 99.5
    assert book.spread == 1.0
    # 1,00 / 99,00 x 10 000 = 101,0101... б.п.
    assert book.spread_bps == pytest.approx(101.0101, abs=1e-4)


def test_depth_sums_visible_levels(book: OrderBook) -> None:
    assert book.depth("ask") == 10.0     # 2 + 3 + 5
    assert book.depth("bid") == 10.0     # 1 + 4 + 5


# --------------------------------------------------------------------------
#  Цена исполнения: примеры, посчитанные руками
# --------------------------------------------------------------------------

def test_buy_within_first_level(book: OrderBook) -> None:
    """Объём умещается на лучшем уровне - VWAP равен лучшей цене."""
    ex = book.buy(2.0)
    assert ex.vwap == 100.0
    assert ex.cost == 200.0
    assert ex.levels_used == 1
    assert ex.complete


def test_buy_eats_second_level_partially(book: OrderBook) -> None:
    """Главный случай: последний уровень съедается не целиком.

    Берём 2 монеты по 100 и 2 монеты по 101 из трёх доступных:
        (2 x 100 + 2 x 101) / 4 = (200 + 202) / 4 = 402 / 4 = 100,50
    """
    ex = book.buy(4.0)
    assert ex.vwap == pytest.approx(100.50)
    assert ex.cost == pytest.approx(402.0)
    assert ex.volume == 4.0
    assert ex.levels_used == 2
    assert ex.complete


def test_vwap_is_not_the_mean_of_prices(book: OrderBook) -> None:
    """Взвешивание идёт по объёму, а не по числу уровней.

        (2 x 100 + 3 x 101) / 5 = (200 + 303) / 5 = 503 / 5 = 100,60
    Среднее самих цен дало бы (100 + 101) / 2 = 100,50 - и это неверно.
    """
    ex = book.buy(5.0)
    assert ex.vwap == pytest.approx(100.60)
    assert ex.vwap != pytest.approx(100.50)


def test_sell_walks_bids_from_the_top(book: OrderBook) -> None:
    """Продажа идёт по бидам, начиная с самого дорогого.

        (1 x 99 + 4 x 98) / 5 = (99 + 392) / 5 = 491 / 5 = 98,20
    """
    ex = book.sell(5.0)
    assert ex.vwap == pytest.approx(98.20)
    assert ex.cost == pytest.approx(491.0)
    assert ex.levels_used == 2


def test_sell_with_repeating_decimal(book: OrderBook) -> None:
    """
        (1 x 99 + 2 x 98) / 3 = (99 + 196) / 3 = 295 / 3 = 98,3333...
    """
    ex = book.sell(3.0)
    assert ex.vwap == pytest.approx(295.0 / 3.0)


def test_buy_exactly_all_visible_depth(book: OrderBook) -> None:
    """
        (2 x 100 + 3 x 101 + 5 x 103) / 10 = (200 + 303 + 515) / 10 = 101,80
    """
    ex = book.buy(10.0)
    assert ex.vwap == pytest.approx(101.80)
    assert ex.complete
    assert ex.levels_used == 3


# --------------------------------------------------------------------------
#  Нехватка глубины
# --------------------------------------------------------------------------

def test_buy_beyond_visible_depth_is_marked_incomplete(book: OrderBook) -> None:
    """Просим 12 при видимых 10.

    Функция обязана сообщить, что глубины не хватило. Молча вернуть цену
    по первым 10 монетам - значит завысить прибыль: настоящая цена
    исполнения двенадцати была бы хуже.
    """
    ex = book.buy(12.0)
    assert not ex.complete
    assert ex.requested == 12.0
    assert ex.volume == 10.0
    assert ex.vwap == pytest.approx(101.80)


def test_sell_beyond_visible_depth_is_marked_incomplete(book: OrderBook) -> None:
    ex = book.sell(100.0)
    assert not ex.complete
    assert ex.volume == 10.0


# --------------------------------------------------------------------------
#  Проскальзывание
# --------------------------------------------------------------------------

def test_slippage_grows_with_volume(book: OrderBook) -> None:
    """Проскальзывание покупки 4 монет:
        (100,50 - 100,00) / 100,00 x 10 000 = 50 б.п.
    """
    assert book.slippage_bps(book.buy(4.0), "buy") == pytest.approx(50.0)
    assert book.slippage_bps(book.buy(2.0), "buy") == pytest.approx(0.0)


def test_sell_slippage_is_positive(book: OrderBook) -> None:
    """Для продажи «хуже» значит дешевле, но знак всё равно положительный:
        (99,00 - 98,20) / 99,00 x 10 000 = 80,808... б.п.
    """
    value = book.slippage_bps(book.sell(5.0), "sell")
    assert value == pytest.approx(80.8081, abs=1e-4)


def test_vwap_is_monotone_in_volume(book: OrderBook) -> None:
    """Цена покупки не улучшается с ростом объёма, цена продажи не растёт.

    Это свойство самого стакана, а не конкретных чисел: уровни идут по
    возрастанию цены для покупки и по убыванию для продажи.
    """
    volumes = [0.5, 1.0, 2.0, 4.0, 7.0, 10.0]
    buys = [book.buy(v).vwap for v in volumes]
    sells = [book.sell(v).vwap for v in volumes]
    assert all(a <= b + 1e-12 for a, b in zip(buys, buys[1:]))
    assert all(a >= b - 1e-12 for a, b in zip(sells, sells[1:]))


# --------------------------------------------------------------------------
#  Нормализация сырых данных
# --------------------------------------------------------------------------

def test_normalize_parses_strings_and_sorts(book: OrderBook) -> None:
    """Биржи присылают числа строками и не обязаны соблюдать порядок."""
    ob = OrderBook.normalize(
        exchange="ТЕСТ", symbol="X/Y",
        raw_bids=[["95.0", "5"], ["99.0", "1"], ["98.0", "4"]],
        raw_asks=[["103.0", "5"], ["100.0", "2"], ["101.0", "3"]],
        fetched_at=0.0)
    assert ob.bids == tuple(BIDS)
    assert ob.asks == tuple(ASKS)


def test_normalize_ignores_extra_fields_in_level() -> None:
    """У OKX уровень четырёхэлементный, у Kraken трёхэлементный.

    Лишнее отбрасывается: берём цену и объём, остальное не наше дело.
    """
    ob = OrderBook.normalize(
        exchange="ТЕСТ", symbol="X/Y",
        raw_bids=[["99.0", "1", "1700000000.0"]],            # как у Kraken
        raw_asks=[["100.0", "2", "0", "7"]],                 # как у OKX
        fetched_at=0.0)
    assert ob.bids == ((99.0, 1.0),)
    assert ob.asks == ((100.0, 2.0),)


# --------------------------------------------------------------------------
#  Проверки, которые ловят ошибки разбора
# --------------------------------------------------------------------------

def test_crossed_book_is_rejected() -> None:
    """Лучший бид выше лучшего аска внутри одной биржи невозможен.

    Такая сделка совершилась бы сама. Если мы это видим - перепутаны
    стороны или не отсортированы уровни.
    """
    with pytest.raises(OrderBookError, match="перекрещен"):
        OrderBook(exchange="ТЕСТ", symbol="X/Y",
                  bids=((101.0, 1.0),), asks=((100.0, 1.0),), fetched_at=0.0)


def test_unsorted_levels_are_rejected() -> None:
    with pytest.raises(OrderBookError, match="порядок цен"):
        OrderBook(exchange="ТЕСТ", symbol="X/Y",
                  bids=((98.0, 1.0), (99.0, 1.0)),
                  asks=((100.0, 1.0),), fetched_at=0.0)


def test_duplicate_price_level_is_rejected() -> None:
    """Два уровня с одной ценой в агрегированном стакане - признак того,
    что разбор что-то склеил или продублировал."""
    with pytest.raises(OrderBookError, match="порядок цен"):
        OrderBook(exchange="ТЕСТ", symbol="X/Y",
                  bids=((99.0, 1.0), (99.0, 1.0)),
                  asks=((100.0, 1.0),), fetched_at=0.0)


def test_empty_side_is_rejected() -> None:
    with pytest.raises(OrderBookError, match="пуста"):
        OrderBook(exchange="ТЕСТ", symbol="X/Y",
                  bids=(), asks=((100.0, 1.0),), fetched_at=0.0)


def test_nonpositive_level_is_rejected() -> None:
    with pytest.raises(OrderBookError, match="неположительный"):
        OrderBook(exchange="ТЕСТ", symbol="X/Y",
                  bids=((99.0, 0.0),), asks=((100.0, 1.0),), fetched_at=0.0)


@pytest.mark.parametrize("volume", [0.0, -1.0])
def test_nonpositive_volume_is_rejected(book: OrderBook, volume: float) -> None:
    with pytest.raises(OrderBookError, match="положительным"):
        book.buy(volume)
