"""Проверка агрегатов analyze.py на синтетическом логе.

Лог собран так, чтобы все ответы считались в уме. Сети и свечей здесь нет.
"""

from __future__ import annotations

import csv

import pytest

from analyze import (Layers, Summary, decompose, is_positive, is_survivor,
                     layer_summary, load_observations, optimal_volumes,
                     optimum_disagreements, percentile, quality, survival,
                     usable)

REL = {"rel_volume": 1.0, "rel_turnover": 10_000.0, "rel_gross": 20.0,
       "rel_fee_buy": 10.0, "rel_fee_sell": 10.0, "rel_withdrawal": 1.0,
       "rel_net": -1.0, "rel_net_bps": -1.0}


def row(sweep: int, status: str, naive: float | None, net: float | None,
        symbol: str = "BTC/USDT", buy: str = "a", sell: str = "b",
        turnover: float | None = 1000.0, **extra) -> dict:
    base = {"sweep": sweep, "ts_utc": f"2026-09-18T10:00:{sweep:02d}+00:00",
            "symbol": symbol, "buy": buy, "sell": sell, "status": status,
            "naive_spread_bps": naive, "net": net,
            "net_bps": None if net is None else net / turnover * 1e4,
            "opt_turnover": turnover, "opt_volume": 1.0, "vwap_sell": 100.0,
            "transfer_time_sec": 600.0}
    if status == "ok":
        base.update(REL)
    else:
        base.update({k: None for k in REL})
    base.update(extra)
    return base


#     наивное   прибыль    годное?  расхождение?  выжил?
LOG = [
    row(1, "ok",             5.0,   -2.0),      # да     да            нет
    row(2, "ok",            30.0,   10.0),      # да     да            ДА
    row(3, "ok",            -1.0,   -5.0),      # да     нет           -
    row(4, "skew_exceeded", None,   None),      # нет
    row(5, "ok",            40.0,   20.0),      # да     да            ДА
    row(6, "fetch_failed",  None,   None),      # нет
]


# --------------------------------------------------------------------------
#  Коэффициент выживаемости
# --------------------------------------------------------------------------

def test_survival_on_hand_counted_log() -> None:
    """Годных 4 (1, 2, 3, 5), с положительным расхождением 3 (1, 2, 5),
    прибыльных из них 2 (2, 5).

        коэффициент           = 2 / 3
        от всех годных        = 2 / 4
    """
    s = survival(LOG)
    assert s.usable == 4
    assert s.positive == 3
    assert s.survivors == 2
    assert s.coefficient == pytest.approx(2 / 3)
    assert s.coefficient_all == pytest.approx(2 / 4)


def test_denominator_excludes_observations_without_a_discrepancy() -> None:
    """Наблюдение 3: расхождения нет вовсе, наивный спред отрицателен.

    Оно не может «не выжить» - ему нечего терять, - поэтому в знаменатель
    главного коэффициента не входит. Так README описывает расхождение:
    лучший бид на одной бирже выше лучшего аска на другой.
    """
    assert not is_positive(LOG[2])
    assert survival(LOG).positive == 3


def test_rejected_observations_are_not_usable() -> None:
    """Брак не входит ни в числитель, ни в знаменатель."""
    assert [r["sweep"] for r in usable(LOG)] == [1, 2, 3, 5]


def test_no_discrepancies_gives_undefined_coefficient_not_zero() -> None:
    """Если положительных расхождений не было вовсе, доля не определена.

    Ноль значил бы «все расхождения погибли», а их просто не было.
    """
    s = survival([row(1, "ok", -1.0, -5.0)])
    assert s.positive == 0
    assert s.coefficient is None


def test_empty_net_is_not_zero() -> None:
    """Пустая прибыль отбракованного наблюдения - не «сделка в ноль»."""
    assert not is_survivor(LOG[3])


# --------------------------------------------------------------------------
#  Качество данных
# --------------------------------------------------------------------------

def test_quality_counts_every_status() -> None:
    q = quality(LOG)
    assert q.total == 6
    assert q.sweeps == 6
    assert q.by_status == {"ok": 4, "skew_exceeded": 1, "fetch_failed": 1}
    assert q.ok_share == pytest.approx(4 / 6)


def test_quality_of_empty_log() -> None:
    assert quality([]).total == 0


# --------------------------------------------------------------------------
#  Разложение по слоям
# --------------------------------------------------------------------------

def test_decomposition_on_hand_computed_row() -> None:
    """Оборот 10 000, всё в б.п. умножением на 10 000 / 10 000 = 1:

        валовая прибыль 20  ->  20 б.п.
        наивное         30
        глубина         30 - 20 = 10
        комиссии        10 + 10 = 20
        вывод            1
        итог            -1
    """
    layers = decompose(row(1, "ok", 30.0, -1.0))
    assert layers.naive == pytest.approx(30.0)
    assert layers.slippage == pytest.approx(10.0)
    assert layers.fees == pytest.approx(20.0)
    assert layers.withdrawal == pytest.approx(1.0)
    assert layers.net == pytest.approx(-1.0)


def test_decomposition_identity_holds_exactly() -> None:
    """net = naive - slippage - fees - withdrawal выполняется по построению.

    Проскальзывание определено как разница между наивным расхождением
    и валовой прибылью, поэтому ни одна часть расхождения не теряется
    и не считается дважды.
    """
    for r in usable(LOG):
        assert decompose(r).residual == pytest.approx(0.0, abs=1e-9)


def test_decomposition_needs_the_relative_optimum() -> None:
    """Без колонок rel_* разложить нельзя - и ответ должен быть None."""
    assert decompose(LOG[3]) is None


def test_largest_layer_answers_the_readme_question() -> None:
    """Главный вопрос выводов: что съедает расхождение сильнее всего."""
    summaries = layer_summary(LOG)
    assert len(summaries) == 1
    name, value = summaries[0].largest()
    assert name == "комиссии"
    assert value == pytest.approx(20.0)


def test_transfer_layer_can_win() -> None:
    """Если перевод дорог, главным ограничением должен стать он."""
    summaries = layer_summary(LOG, transfer_bps={("BTC/USDT", 600.0): 40.0})
    name, value = summaries[0].largest()
    assert name == "время перевода"
    assert value == pytest.approx(40.0)


def test_layers_use_only_positive_discrepancies() -> None:
    """Наблюдение 3 без расхождения в разложение не входит."""
    assert layer_summary(LOG)[0].n == 3


# --------------------------------------------------------------------------
#  Согласованность двух оптимумов
# --------------------------------------------------------------------------

def test_two_optima_must_agree_on_profitability() -> None:
    """Знак прибыли в деньгах и в б.п. совпадает при любом объёме.

    Значит оба поиска обязаны одинаково отвечать на вопрос «прибыльно ли».
    Расхождение означает, что один из них не нашёл максимум.
    """
    agreed = [row(1, "ok", 30.0, 10.0, rel_net=5.0)]
    assert optimum_disagreements(agreed) == 0

    disagreed = [row(1, "ok", 30.0, 10.0, rel_net=-5.0)]
    assert optimum_disagreements(disagreed) == 1


# --------------------------------------------------------------------------
#  Оптимальный объём
# --------------------------------------------------------------------------

def test_optimal_volume_only_among_survivors() -> None:
    """У убыточного маршрута оптимум вырождается в минимальный объём,
    и показывать его как результат нельзя."""
    log = [row(1, "ok", 30.0, 10.0, turnover=5000.0),
           row(2, "ok", 30.0, 12.0, turnover=7000.0),
           row(3, "ok", 30.0, -1.0, turnover=8.0)]      # убыточный
    volumes = optimal_volumes(log)
    summary = volumes[("BTC/USDT", "a", "b")]
    assert summary.n == 2
    assert summary.median == pytest.approx(6000.0)


# --------------------------------------------------------------------------
#  Описательные статистики
# --------------------------------------------------------------------------

def test_percentile_matches_linear_interpolation() -> None:
    """Позиция q(n-1), между соседями - линейно. Как у numpy.

        [1, 2, 3, 4, 5]:  q=0,5 -> 3;  q=0,25 -> 2;  q=0,1 -> 1 + 0,4 = 1,4
    """
    values = [5.0, 1.0, 4.0, 2.0, 3.0]
    assert percentile(values, 0.5) == pytest.approx(3.0)
    assert percentile(values, 0.25) == pytest.approx(2.0)
    assert percentile(values, 0.1) == pytest.approx(1.4)
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 5.0


def test_percentile_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        percentile([], 0.5)
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


def test_summary_of_empty_is_explicit() -> None:
    """Пустая выборка даёт None, а не нули - иначе «нет данных» выглядело
    бы как «прибыль ноль»."""
    s = Summary.of([])
    assert s.n == 0
    assert s.median is None


# --------------------------------------------------------------------------
#  Загрузка
# --------------------------------------------------------------------------

def test_load_turns_empty_fields_into_none(tmp_path) -> None:
    """Пустое поле в csv - это отсутствие значения, а не ноль."""
    path = tmp_path / "obs.csv"
    fields = ["sweep", "ts_utc", "symbol", "buy", "sell", "status",
              "naive_spread_bps", "net"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerow({"sweep": 1, "ts_utc": "t", "symbol": "X", "buy": "a",
                    "sell": "b", "status": "ok",
                    "naive_spread_bps": "1.5", "net": "-2"})
        w.writerow({"sweep": 2, "ts_utc": "t", "symbol": "X", "buy": "a",
                    "sell": "b", "status": "skew_exceeded",
                    "naive_spread_bps": "", "net": ""})
    rows = load_observations(path)
    assert rows[0]["naive_spread_bps"] == pytest.approx(1.5)
    assert rows[0]["net"] == pytest.approx(-2.0)
    assert rows[0]["sweep"] == 1
    assert rows[1]["net"] is None
