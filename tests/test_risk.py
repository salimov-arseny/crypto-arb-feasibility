"""Проверка оценки риска времени перевода.

Ожидаемые числа либо посчитаны руками, либо взяты из табличных значений
нормального распределения.
"""

from __future__ import annotations

import math
import random

import pytest

from core.risk import (ReturnSample, Risk, RiskError, assess, horizon_returns,
                       normal_cdf, normal_quantile, required_net_bps,
                       survival_empirical, survival_normal, threshold_return)


# --------------------------------------------------------------------------
#  Порог: какая доходность обнуляет прибыль
# --------------------------------------------------------------------------

def test_threshold_for_profitable_trade_is_negative() -> None:
    """Прибыль 10 при обороте продажи 2 x 100 = 200.

        r = -net / (V x P_sell) = -10 / 200 = -0,05

    Цена может упасть на 500 б.п., и прибыль всё ещё останется.
    """
    assert threshold_return(net=10.0, volume=2.0, price_sell=100.0) == \
        pytest.approx(-0.05)


def test_threshold_for_losing_trade_is_positive() -> None:
    """Убыточную сделку спасёт только рост цены: -(-10)/200 = +0,05."""
    assert threshold_return(net=-10.0, volume=2.0, price_sell=100.0) == \
        pytest.approx(0.05)


def test_threshold_is_zero_at_break_even() -> None:
    assert threshold_return(net=0.0, volume=2.0, price_sell=100.0) == 0.0


def test_threshold_rejects_nonpositive_notional() -> None:
    with pytest.raises(RiskError, match="оборот"):
        threshold_return(net=1.0, volume=0.0, price_sell=100.0)


# --------------------------------------------------------------------------
#  Нормальное приближение: сверка с табличными значениями
# --------------------------------------------------------------------------

def test_normal_cdf_matches_known_values() -> None:
    assert normal_cdf(0.0) == pytest.approx(0.5)
    assert normal_cdf(1.0) == pytest.approx(0.8413447, abs=1e-7)
    assert normal_cdf(-1.96) == pytest.approx(0.0249979, abs=1e-7)


def test_normal_quantile_inverts_the_cdf() -> None:
    assert normal_quantile(0.5) == pytest.approx(0.0, abs=1e-9)
    assert normal_quantile(0.975) == pytest.approx(1.959964, abs=1e-5)
    for p in (0.01, 0.25, 0.9, 0.99):
        assert normal_cdf(normal_quantile(p)) == pytest.approx(p, abs=1e-9)


def test_survival_at_zero_threshold_is_one_half() -> None:
    """Сделка ровно в ноль: при нулевом сносе шансы симметричны."""
    assert survival_normal(threshold=0.0, sigma=0.01) == pytest.approx(0.5)
    assert survival_normal(threshold=0.0, sigma=0.5) == pytest.approx(0.5)


def test_survival_with_one_sigma_of_slack() -> None:
    """Порог на сигму ниже нуля: P(r > -sigma) = Ф(1) = 0,841."""
    assert survival_normal(threshold=-0.01, sigma=0.01) == \
        pytest.approx(0.8413447, abs=1e-7)


def test_survival_when_one_sigma_of_growth_is_needed() -> None:
    """Порог на сигму выше нуля: P(r > sigma) = 1 - Ф(1) = 0,159."""
    assert survival_normal(threshold=0.01, sigma=0.01) == \
        pytest.approx(0.1586553, abs=1e-7)


def test_survival_with_zero_volatility_is_certain() -> None:
    """Вырожденный случай: цена не меняется, исход определён."""
    assert survival_normal(threshold=-0.01, sigma=0.0) == 1.0
    assert survival_normal(threshold=0.01, sigma=0.0) == 0.0
    assert survival_normal(threshold=0.0, sigma=0.0) == 0.5


def test_negative_sigma_rejected() -> None:
    with pytest.raises(RiskError, match="sigma"):
        survival_normal(threshold=0.0, sigma=-1.0)


# --------------------------------------------------------------------------
#  Обратная задача: сколько надо заработать
# --------------------------------------------------------------------------

def test_required_profit_for_given_odds() -> None:
    """Чтобы пережить перевод с вероятностью 0,975, нужен запас
    в 1,96 сигмы. При sigma = 10 б.п. это 19,6 б.п. прибыли."""
    assert required_net_bps(0.975, sigma=0.001) == \
        pytest.approx(1.959964 * 10.0, abs=1e-3)


def test_required_profit_is_zero_at_even_odds() -> None:
    """Шансы пятьдесят на пятьдесят достигаются при нулевой прибыли."""
    assert required_net_bps(0.5, sigma=0.001) == pytest.approx(0.0, abs=1e-6)


def test_required_profit_rejects_impossible_probabilities() -> None:
    for p in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(RiskError, match="вероятность"):
            required_net_bps(p, sigma=0.001)


# --------------------------------------------------------------------------
#  Эмпирическая оценка
# --------------------------------------------------------------------------

def test_empirical_is_a_plain_count() -> None:
    """Семь наблюдений, три строго больше нуля -> 3/7."""
    returns = [-0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03]
    assert survival_empirical(0.0, returns) == pytest.approx(3 / 7)


def test_empirical_saturates_outside_the_sample() -> None:
    """За пределами наблюдавшегося оценка вырождается.

    Это «в выборке не встречалось», а не «невозможно», и различать
    эти утверждения обязательно.
    """
    returns = [-0.01, 0.0, 0.01]
    assert survival_empirical(0.5, returns) == 0.0
    assert survival_empirical(-0.5, returns) == 1.0


def test_empty_sample_rejected() -> None:
    with pytest.raises(RiskError, match="пустая"):
        survival_empirical(0.0, [])


# --------------------------------------------------------------------------
#  Доходности на горизонте
# --------------------------------------------------------------------------

def test_horizon_returns_on_a_hand_computable_series() -> None:
    """Цены 100, 110, 121 - рост на 10 % каждый шаг.

    Окно в одну свечу: ln(110/100) = ln(1,1) и ln(121/110) = ln(1,1).
    """
    sample = horizon_returns([100.0, 110.0, 121.0], interval_sec=60,
                             horizon_sec=60)
    assert sample.n == 2
    assert all(r == pytest.approx(math.log(1.1)) for r in sample.returns)


def test_horizon_of_two_steps_compounds() -> None:
    """Окно в две свечи: ln(121/100) = ln(1,21) = 2 ln(1,1)."""
    sample = horizon_returns([100.0, 110.0, 121.0], interval_sec=60,
                             horizon_sec=120)
    assert sample.n == 1
    assert sample.returns[0] == pytest.approx(2 * math.log(1.1))


def test_horizon_shorter_than_candle_is_refused_by_default() -> None:
    """Перевод по Solana занимает 0,32 с - короче даже секундной свечи.

    Молча подставить шаг свечи нельзя: это завысило бы риск. По умолчанию
    ошибка, чтобы такое место нельзя было проскочить не заметив.
    """
    with pytest.raises(RiskError, match="короче шага свечей"):
        horizon_returns([100.0, 101.0, 102.0], interval_sec=1.0,
                        horizon_sec=0.32)


def test_horizon_shorter_than_candle_can_be_bounded_explicitly() -> None:
    """Явно запрошенная оценка сверху помечается как таковая.

    Дисперсия доходности растёт с горизонтом, поэтому sigma за секунду
    не меньше sigma за треть секунды: подстановка ошибается в безопасную
    сторону - завышает риск, а не занижает.
    """
    sample = horizon_returns([100.0, 101.0, 102.0], interval_sec=1.0,
                             horizon_sec=0.32, allow_resolution_bound=True)
    assert sample.resolution_limited
    assert sample.horizon_sec == 1.0


def test_horizon_longer_than_series_is_refused() -> None:
    with pytest.raises(RiskError, match="ряда не хватает"):
        horizon_returns([100.0, 101.0], interval_sec=60, horizon_sec=6000)


def test_effective_sample_size_accounts_for_overlap() -> None:
    """Окна перекрываются, соседние наблюдения зависимы.

    Ряд из 101 цены при окне в 10 свечей даёт 91 наблюдение, но
    независимых среди них около 9. Показывать надо оба числа: иначе
    выборка выглядит в десять раз надёжнее, чем есть.
    """
    closes = [100.0 * (1.001 ** i) for i in range(101)]
    sample = horizon_returns(closes, interval_sec=60, horizon_sec=600)
    assert sample.steps == 10
    assert sample.n == 91
    assert sample.effective_n == pytest.approx(9.1)


# --------------------------------------------------------------------------
#  Утверждение README про тяжёлые хвосты - и где оно работает
# --------------------------------------------------------------------------

def fat_tailed_sample(seed: int = 42) -> ReturnSample:
    """Смесь двух нормальных: обычные периоды и редкие бурные.

    Классический способ получить тяжёлые хвосты при конечной дисперсии.
    Зерно фиксировано, чтобы тест был воспроизводим.
    """
    rng = random.Random(seed)
    values = [rng.gauss(0.0, 0.002) if rng.random() > 0.02
              else rng.gauss(0.0, 0.02) for _ in range(20_000)]
    return ReturnSample(horizon_sec=600.0, interval_sec=60.0,
                        returns=tuple(values))


def test_fat_tails_are_detected_by_kurtosis() -> None:
    """У нормального распределения избыточный куртозис равен нулю."""
    assert fat_tailed_sample().excess_kurtosis > 3.0

    rng = random.Random(1)
    thin = ReturnSample(horizon_sec=600.0, interval_sec=60.0,
                        returns=tuple(rng.gauss(0.0, 0.002)
                                      for _ in range(20_000)))
    assert abs(thin.excess_kurtosis) < 0.3


def test_fat_tails_do_not_bite_yet_at_two_sigma() -> None:
    """Важная оговорка: «тяжёлые хвосты» не значит «хуже везде».

    Смесь с тяжёлыми хвостами ОСТРЕЕ в центре: почти вся масса сидит
    в узкой компоненте, лишняя уезжает далеко наружу. Поэтому на двух
    сигмах в хвосте оказывается МЕНЬШЕ массы, чем у нормального закона,
    и там нормальная оценка выживания пессимистична.

    Пересечение - примерно на трёх сигмах. Не зная этого, легко написать
    проверку на двух сигмах, увидеть падение и решить, что ошибка в коде.
    """
    sample = fat_tailed_sample()
    threshold = -2.0 * sample.sigma
    p_normal = survival_normal(threshold, sample.sigma)
    p_empirical = survival_empirical(threshold, sample.returns)

    assert p_normal == pytest.approx(0.9772, abs=1e-3)       # Ф(2)
    assert p_empirical > p_normal


def test_normal_approximation_is_optimistic_far_in_the_tail() -> None:
    """Проверка утверждения README там, где оно работает.

    Порог на три сигмы ниже нуля. Выживание означает «цена не упала
    слишком сильно», и за три сигмы тяжёлый левый хвост даёт больше
    массы ниже порога, чем предсказывает нормальный закон. Значит
    истинная вероятность выживания НИЖЕ нормальной оценки - а именно
    нормальная оценка и попала бы в выводы.
    """
    sample = fat_tailed_sample()
    threshold = -3.0 * sample.sigma
    p_normal = survival_normal(threshold, sample.sigma)
    p_empirical = survival_empirical(threshold, sample.returns)

    assert p_normal == pytest.approx(0.99865, abs=1e-4)      # Ф(3)
    assert p_empirical < p_normal
    assert p_normal - p_empirical > 0.002


def test_tail_probability_ratio_shows_how_much_heavier() -> None:
    """Насколько именно хвост тяжелее нормального, в разах.

    На четырёх сигмах нормальный закон отводит провалу вероятность около
    3 на 100 000. У смеси она на порядки больше - и это ровно та разница,
    из-за которой нормальная оценка риска вводит в заблуждение.
    """
    sample = fat_tailed_sample()
    threshold = -4.0 * sample.sigma
    fail_normal = 1.0 - survival_normal(threshold, sample.sigma)
    fail_empirical = 1.0 - survival_empirical(threshold, sample.returns)

    assert fail_normal == pytest.approx(3.17e-5, rel=0.05)
    assert fail_empirical / fail_normal > 10.0


# --------------------------------------------------------------------------
#  Сборка
# --------------------------------------------------------------------------

def test_assess_puts_both_estimates_side_by_side() -> None:
    sample = fat_tailed_sample()
    risk = assess(net=1.0, volume=1.0, price_sell=100.0, sample=sample)

    assert isinstance(risk, Risk)
    assert risk.threshold == pytest.approx(-0.01)        # -1/100
    assert risk.threshold_bps == pytest.approx(-100.0)
    assert 0.0 <= risk.p_normal <= 1.0
    assert 0.0 <= risk.p_empirical <= 1.0
    assert risk.threshold_in_sigmas == pytest.approx(
        risk.threshold / risk.sigma)


def test_assess_flags_where_normal_is_optimistic() -> None:
    """Флаг нужен, чтобы в выводах не опереться молча на завышенные шансы."""
    sample = fat_tailed_sample()
    far = assess(net=3.0 * sample.sigma * 100.0, volume=1.0,
                 price_sell=100.0, sample=sample)
    assert far.threshold_in_sigmas == pytest.approx(-3.0, abs=0.01)
    assert far.normal_is_optimistic


def test_beyond_sample_is_flagged() -> None:
    """Порог за пределами наблюдавшегося - эмпирическая оценка выродилась."""
    sample = ReturnSample(horizon_sec=600.0, interval_sec=60.0,
                          returns=(-0.01, 0.0, 0.01))
    risk = assess(net=1000.0, volume=1.0, price_sell=100.0, sample=sample)
    assert risk.p_empirical == 1.0
    assert risk.beyond_sample
