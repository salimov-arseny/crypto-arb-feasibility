"""Проверка разбора ответов четырёх бирж.

Тесты работают на настоящих ответах, снятых 2026-09-17 и сохранённых
в tests/fixtures. Придуманный «похожий» JSON проверял бы моё представление
о формате, а не сам формат: ровно на этом и ломаются такие разборщики.

Сети здесь нет. Разбор - чистая функция от ответа, и проверять его
сетевым запросом значит делать тесты медленными и ненадёжными.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from core.orderbook import OrderBook
from fetchers import (BinanceFetcher, BybitFetcher, KrakenFetcher, OkxFetcher,
                      FetchError)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# Лучшие цены из сохранённых ответов, выписанные вручную из файлов.
EXPECTED = {
    "binance": (BinanceFetcher, 76270.00, 76270.01),
    "bybit":   (BybitFetcher,   76258.2,  76258.3),
    "okx":     (OkxFetcher,     76260.0,  76260.1),
    "kraken":  (KrakenFetcher,  76257.30, 76257.40),
}


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}_depth.json").read_text(encoding="utf-8"))


def make(cls) -> object:
    """Фетчер без сети: разбор от транспорта не зависит."""
    return cls(rest_base="https://example.invalid", endpoint="/depth",
               tickers={"BTC/USDT": "TEST"})


def parse_to_book(name: str) -> OrderBook:
    cls, _, _ = EXPECTED[name]
    parsed = make(cls)._parse(load(name))
    return OrderBook.normalize(
        exchange=cls.name, symbol="BTC/USDT",
        raw_bids=parsed.bids, raw_asks=parsed.asks, fetched_at=1_789_684_100.0,
        source_seq=parsed.source_seq, exchange_ts=parsed.exchange_ts)


# --------------------------------------------------------------------------
#  Разбор настоящих ответов
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_parses_real_response(name: str) -> None:
    _, want_bid, want_ask = EXPECTED[name]
    book = parse_to_book(name)
    assert book.best_bid == pytest.approx(want_bid)
    assert book.best_ask == pytest.approx(want_ask)
    assert len(book.bids) == 3
    assert len(book.asks) == 3


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_all_exchanges_give_the_same_shape(name: str) -> None:
    """Смысл шага 3: наружу все четыре выглядят одинаково.

    Разные имена полей, разная вложенность, разное число элементов
    в уровне - и на выходе один и тот же тип с одними и теми же
    свойствами, по которым дальше работает весь проект.
    """
    book = parse_to_book(name)
    assert book.best_bid < book.best_ask
    assert book.spread_bps > 0
    assert book.depth("bid") > 0 and book.depth("ask") > 0
    assert book.buy(0.001).vwap >= book.best_ask
    assert book.sell(0.001).vwap <= book.best_bid


def test_extra_level_fields_are_dropped() -> None:
    """У OKX уровень четырёхэлементный, у Kraken трёхэлементный.

    Наружу и там и там выходит пара (цена, объём).
    """
    for name in ("okx", "kraken"):
        book = parse_to_book(name)
        assert all(len(level) == 2 for level in book.bids + book.asks)


# --------------------------------------------------------------------------
#  Кто что сообщает о себе
# --------------------------------------------------------------------------

def test_exchange_timestamp_present_only_where_exchange_reports_it() -> None:
    """Bybit и OKX сообщают своё время снимка, Binance и Kraken - нет.

    Это не недоработка разбора, а свойство площадок, и на шаге 6 оно
    означает, что рассинхронизацию для половины бирж придётся оценивать
    по нашему локальному времени.
    """
    assert parse_to_book("bybit").exchange_ts is not None
    assert parse_to_book("okx").exchange_ts is not None
    assert parse_to_book("binance").exchange_ts is None
    assert parse_to_book("kraken").exchange_ts is None


def test_exchange_timestamp_is_seconds_not_milliseconds() -> None:
    """Биржи шлют миллисекунды, у нас всё в секундах.

    Ошибка в тысячу раз здесь дала бы рассинхронизацию в десятки лет,
    и на шаге 6 все наблюдения были бы отбракованы.
    """
    ts = parse_to_book("bybit").exchange_ts
    assert 1.7e9 < ts < 2.0e9          # правдоподобное unix-время в секундах


def test_sequence_number_present_where_exchange_reports_it() -> None:
    assert parse_to_book("binance").source_seq is not None
    assert parse_to_book("bybit").source_seq is not None
    assert parse_to_book("okx").source_seq is not None
    assert parse_to_book("kraken").source_seq is None


# --------------------------------------------------------------------------
#  Ошибки: все четыре биржи сообщают о них по-своему, и все со статусом 200
# --------------------------------------------------------------------------

def test_binance_error_in_body() -> None:
    with pytest.raises(FetchError, match="code=-1121"):
        make(BinanceFetcher)._parse({"code": -1121, "msg": "Invalid symbol."})


def test_bybit_error_in_retcode() -> None:
    with pytest.raises(FetchError, match="retCode=10001"):
        make(BybitFetcher)._parse({"retCode": 10001, "retMsg": "params error"})


def test_okx_code_is_a_string_not_a_number() -> None:
    """Ловушка OKX: успех приходит строкой "0".

    Сравнение data["code"] != 0 пропустило бы ошибку, потому что строка
    "1" не равна числу 0 точно так же, как и строка "0".
    """
    ok = load("okx")
    assert ok["code"] == "0"                      # именно строка
    make(OkxFetcher)._parse(ok)                   # разбирается без ошибки

    with pytest.raises(FetchError, match="code=51001"):
        make(OkxFetcher)._parse({"code": "51001", "msg": "Instrument ID does not exist"})


def test_kraken_error_in_array() -> None:
    with pytest.raises(FetchError, match="EQuery:Unknown asset pair"):
        make(KrakenFetcher)._parse({"error": ["EQuery:Unknown asset pair"],
                                    "result": {}})


def test_kraken_internal_pair_name_may_differ_from_requested() -> None:
    """Просим XBTUSDT, а ключом результата может быть другое имя.

    Поэтому берём первый ключ, а не подставляем свой тикер.
    """
    parsed = make(KrakenFetcher)._parse({
        "error": [],
        "result": {"XXBTZUSD": {"bids": [["100.0", "1", 0]],
                                "asks": [["101.0", "1", 0]]}}})
    assert parsed.bids == [["100.0", "1", 0]]


@pytest.mark.parametrize("cls,payload", [
    (BinanceFetcher, {}),
    (BybitFetcher, {"retCode": 0, "result": {}}),
    (OkxFetcher, {"code": "0", "data": []}),
    (KrakenFetcher, {"error": [], "result": {}}),
])
def test_missing_sides_raise_fetch_error(cls, payload) -> None:
    """Ответ без уровней - это отказ, а не пустой стакан."""
    with pytest.raises(FetchError):
        make(cls)._parse(payload)
