"""Проверка правил измерения, закодированных в scan.py.

Сети здесь нет: проверяются чистые функции, которые решают, что считать
наблюдением и что считать браком.
"""

from __future__ import annotations

import pytest

from core.orderbook import OrderBook
from scan import COLUMNS, STATUS_SKEW, blank_row, symbol_skew


def book(exchange: str, fetched_at: float, exchange_ts: float | None = None
         ) -> OrderBook:
    return OrderBook(exchange=exchange, symbol="X/Y",
                     bids=((99.0, 1.0),), asks=((100.0, 1.0),),
                     fetched_at=fetched_at, exchange_ts=exchange_ts)


def test_skew_is_spread_between_our_own_timestamps() -> None:
    books = {("a", "X/Y"): book("A", 1000.00),
             ("b", "X/Y"): book("B", 1000.30),
             ("c", "X/Y"): book("C", 1000.45)}
    assert symbol_skew(books, "X/Y") == pytest.approx(0.45)


def test_skew_ignores_exchange_clock() -> None:
    """Главное правило измерения времени в проекте.

    Сверка на шаге 3 показала, что локальные часы отстают от биржевых
    на 0,7-0,9 с - почти на весь допуск в 1,0 с. Между двумя нашими
    метками этот сдвиг сокращается, между нашей и биржевой - нет.
    Поэтому разброс считается только по fetched_at, и время, сообщённое
    биржей, на него влиять не должно.
    """
    books = {("a", "X/Y"): book("A", 1000.0, exchange_ts=1000.9),
             ("b", "X/Y"): book("B", 1000.2, exchange_ts=500.0)}
    assert symbol_skew(books, "X/Y") == pytest.approx(0.2)


def test_skew_needs_at_least_two_books() -> None:
    """С одной биржей сравнивать не с чем - это не ноль, а неизвестность."""
    assert symbol_skew({("a", "X/Y"): book("A", 1000.0)}, "X/Y") is None
    assert symbol_skew({}, "X/Y") is None


def test_skew_separates_instruments() -> None:
    """Снимки разных инструментов между собой не сравниваются."""
    books = {("a", "X/Y"): book("A", 1000.0),
             ("b", "X/Y"): book("B", 1000.1),
             ("a", "Z/Y"): book("A", 2000.0)}
    assert symbol_skew(books, "X/Y") == pytest.approx(0.1)
    assert symbol_skew(books, "Z/Y") is None


def test_rejected_observation_is_written_not_dropped() -> None:
    """Доля брака - сама по себе результат.

    Отбракованное наблюдение попадает в лог со своим статусом и
    измеренным разбросом, а не исчезает: иначе нельзя судить, насколько
    лог представителен.
    """
    row = blank_row(7, "2026-09-18T10:00:00+00:00", "X/Y", "a", "b",
                    STATUS_SKEW, skew_sec=1.4)
    assert row["status"] == STATUS_SKEW
    assert row["skew_sec"] == 1.4
    assert row["sweep"] == 7
    # Все колонки присутствуют, незаполненные пусты - csv остаётся ровным.
    assert set(row) == set(COLUMNS)
    assert row["net"] == ""
