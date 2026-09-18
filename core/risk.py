"""Риск времени перевода: переживёт ли прибыль дорогу между биржами.

До этого модуля весь расчёт исходил из того, что покупка и продажа
происходят одновременно. Это не так: между ними стоит перевод, от трети
секунды по Solana до получаса по биткоину через Kraken. За это время цена
уходит, и прибыль перестаёт быть числом.

Пусть r_tau - логарифмическая доходность актива за время перевода. Тогда,
как требует README:

    P( net(V) + V * P_sell * r_tau > 0 )  =  P( r_tau > -net / (V * P_sell) )

Справа стоит ПОРОГ: насколько цена может упасть, прежде чем прибыль
обнулится. Для прибыльной сделки порог отрицателен - есть запас; для
убыточной положителен - спасёт только рост.

Две оценки вероятности, и нужны обе.

    Нормальное приближение - гладкое, работает при любом пороге, в том
    числе за пределами наблюдавшегося.

    Эмпирическая оценка - доля исторических случаев, когда доходность
    превысила порог. Ничего не предполагает о форме распределения, но
    за пределами выборки вырождается в 0 или 1, и это надо читать как
    «не встречалось», а не как «невозможно».

README предупреждает, что нормальная оценка оптимистична: хвосты у крипты
тяжелее нормальных. Это верно, но НЕ ВЕЗДЕ, и различие существенно.
Распределение с тяжёлыми хвостами острее в центре: почти вся масса сидит
в узкой части, а лишняя уезжает далеко наружу. Поэтому вблизи двух сигм
в хвосте оказывается МЕНЬШЕ массы, чем у нормального, и там нормальная
оценка пессимистична. Пересечение происходит около трёх сигм, и только
дальше утверждение README начинает работать. Обе стороны этого проверены
тестами.
"""

from __future__ import annotations

import csv
import math
import pathlib
import statistics
from dataclasses import dataclass


class RiskError(ValueError):
    """Оценку риска на этих данных построить нельзя."""


# --------------------------------------------------------------------------
#  Выборка доходностей на горизонте перевода
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ReturnSample:
    """Логарифмические доходности актива за время перевода.

    Доходности считаются НАПРЯМУЮ на нужном горизонте скользящими окнами,
    а не пересчётом минутной волатильности по правилу корня из времени.
    Правило sigma_tau = sigma_1 * sqrt(tau) верно для независимых
    одинаково распределённых приращений; у крипты волатильность
    кластеризуется, приращения зависимы, и проверять это предположение
    дороже, чем просто взять окна нужной длины.
    """

    horizon_sec: float
    interval_sec: float
    returns: tuple[float, ...]
    symbol: str = ""
    # Горизонт короче шага свечей: sigma взята на шаге свечей и служит
    # ОЦЕНКОЙ СВЕРХУ. Дисперсия доходности растёт с горизонтом, а на
    # секундных масштабах вдобавок раздувается микроструктурным шумом -
    # значит такая подстановка риск завышает, а не занижает.
    resolution_limited: bool = False

    @property
    def steps(self) -> int:
        """Сколько свечей укладывается в горизонт."""
        return max(1, round(self.horizon_sec / self.interval_sec))

    @property
    def n(self) -> int:
        """Формальный размер выборки."""
        return len(self.returns)

    @property
    def effective_n(self) -> float:
        """Эффективный размер выборки.

        Окна перекрываются: соседние доходности делят между собой почти
        все свечи и потому зависимы. Независимых наблюдений примерно
        в `steps` раз меньше формального числа. Показывать надо оба:
        иначе выборка из 43 000 перекрывающихся получасовых окон выглядит
        огромной, хотя независимых наблюдений в ней около 1 440.
        """
        return self.n / self.steps

    @property
    def sigma(self) -> float:
        """Стандартное отклонение доходности на горизонте перевода."""
        if self.n < 2:
            raise RiskError(f"для оценки sigma нужно не меньше двух "
                            f"наблюдений, есть {self.n}")
        return statistics.stdev(self.returns)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.returns)

    @property
    def excess_kurtosis(self) -> float:
        """Насколько хвосты тяжелее нормальных.

        У нормального распределения этот показатель равен нулю.
        Положительное значение означает более частые крупные отклонения -
        ровно то, из-за чего нормальная оценка в далёком хвосте
        оказывается оптимистичной.
        """
        if self.n < 4:
            raise RiskError("для оценки куртозиса нужно хотя бы 4 наблюдения")
        mean = self.mean
        sigma = self.sigma
        if sigma == 0:
            raise RiskError("нулевая волатильность: куртозис не определён")
        return statistics.fmean(
            [((x - mean) / sigma) ** 4 for x in self.returns]) - 3.0


def load_closes(path: str | pathlib.Path) -> list[float]:
    """Прочитать цены закрытия из файла, созданного tools/fetch_candles.py."""
    closes: list[float] = []
    with pathlib.Path(path).open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            closes.append(float(row["close"]))
    if len(closes) < 2:
        raise RiskError(f"в файле {path} меньше двух свечей")
    return closes


def horizon_returns(closes: list[float], interval_sec: float,
                    horizon_sec: float, symbol: str = "",
                    allow_resolution_bound: bool = False) -> ReturnSample:
    """Логарифмические доходности за `horizon_sec`, скользящими окнами.

    Окна перекрываются: так из ряда извлекается максимум наблюдений. Цена
    этого - зависимость соседних наблюдений, она учтена в effective_n.

    Горизонт короче шага свечей измерить нельзя: у данных нет такого
    разрешения. По умолчанию это ошибка. С allow_resolution_bound берётся
    доходность за одну свечу и помечается как оценка сверху: дисперсия
    растёт с горизонтом, поэтому риск при такой подстановке завышается,
    а не занижается, - и ошибка получается в безопасную сторону.
    """
    if horizon_sec <= 0:
        raise RiskError(f"горизонт должен быть положительным, "
                        f"получено {horizon_sec}")

    limited = False
    if horizon_sec < interval_sec:
        if not allow_resolution_bound:
            raise RiskError(
                f"горизонт {horizon_sec:g} с короче шага свечей "
                f"{interval_sec:g} с: на этих данных такую волатильность "
                f"не измерить")
        limited = True
        horizon_sec = interval_sec

    steps = max(1, round(horizon_sec / interval_sec))
    if len(closes) <= steps:
        raise RiskError(f"ряда не хватает: {len(closes)} свечей "
                        f"при окне в {steps}")

    returns = tuple(math.log(closes[i + steps] / closes[i])
                    for i in range(len(closes) - steps))
    return ReturnSample(horizon_sec=horizon_sec, interval_sec=interval_sec,
                        returns=returns, symbol=symbol,
                        resolution_limited=limited)


# --------------------------------------------------------------------------
#  Порог и вероятность выживания
# --------------------------------------------------------------------------

def threshold_return(net: float, volume: float, price_sell: float) -> float:
    """Доходность, ниже которой прибыль обнуляется.

        net + V * P_sell * r = 0   =>   r = -net / (V * P_sell)
    """
    notional = volume * price_sell
    if notional <= 0:
        raise RiskError(f"оборот продажи должен быть положительным, "
                        f"получено {notional}")
    return -net / notional


def normal_cdf(x: float) -> float:
    """Функция распределения стандартного нормального закона.

    Через math.erf: формула точная, и одной зависимостью меньше.
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def survival_normal(threshold: float, sigma: float, mu: float = 0.0) -> float:
    """P(r > threshold) в нормальном приближении.

    Снос mu по умолчанию нулевой, и это осознанный выбор, а не упущение.
    На горизонте в минуты ожидаемое изменение цены неотличимо от нуля:
    стандартная ошибка оценки сноса на таком отрезке на порядки больше
    самого сноса. Оценивать его - значит подмешивать шум вместо
    информации.
    """
    if sigma < 0:
        raise RiskError(f"sigma не может быть отрицательной: {sigma}")
    if sigma == 0:
        # Вырожденный случай: цена не меняется, исход определён.
        if threshold < mu:
            return 1.0
        return 0.5 if threshold == mu else 0.0
    return 1.0 - normal_cdf((threshold - mu) / sigma)


def survival_empirical(threshold: float,
                       returns: tuple[float, ...] | list[float]) -> float:
    """Доля исторических доходностей, превысивших порог."""
    if not returns:
        raise RiskError("пустая выборка доходностей")
    return sum(1 for r in returns if r > threshold) / len(returns)


@dataclass(frozen=True)
class Risk:
    """Оценка того, переживёт ли прибыль перевод."""

    horizon_sec: float
    threshold: float          # доходность, обнуляющая прибыль
    sigma: float              # волатильность на горизонте перевода
    p_normal: float
    p_empirical: float
    sample: ReturnSample

    @property
    def threshold_bps(self) -> float:
        return self.threshold * 10_000

    @property
    def sigma_bps(self) -> float:
        return self.sigma * 10_000

    @property
    def threshold_in_sigmas(self) -> float:
        """Порог в сигмах - главная величина для чтения результата.

        Именно она говорит, где мы находимся относительно точки, в которой
        нормальное приближение перестаёт быть безопасным: около трёх сигм.
        """
        if self.sigma == 0:
            return math.inf if self.threshold > 0 else -math.inf
        return self.threshold / self.sigma

    @property
    def normal_is_optimistic(self) -> bool:
        """Завышает ли нормальное приближение шансы на этих данных."""
        return self.p_normal > self.p_empirical

    @property
    def beyond_sample(self) -> bool:
        """Лежит ли порог за пределами наблюдавшегося.

        Если да, эмпирическая оценка выродилась в 0 или 1 и говорит лишь
        о том, что таких случаев в выборке не было. Опираться на неё как
        на вероятность нельзя.
        """
        return self.p_empirical in (0.0, 1.0)

    @property
    def is_upper_bound(self) -> bool:
        """Взята ли sigma с более длинного горизонта, чем нужно."""
        return self.sample.resolution_limited


def assess(net: float, volume: float, price_sell: float,
           sample: ReturnSample) -> Risk:
    """Собрать обе оценки вероятности для конкретной возможности."""
    threshold = threshold_return(net, volume, price_sell)
    sigma = sample.sigma
    return Risk(
        horizon_sec=sample.horizon_sec,
        threshold=threshold,
        sigma=sigma,
        p_normal=survival_normal(threshold, sigma),
        p_empirical=survival_empirical(threshold, sample.returns),
        sample=sample,
    )


def required_net_bps(probability: float, sigma: float) -> float:
    """Какая прибыль нужна, чтобы пережить перевод с заданной вероятностью.

    Обратная задача, и на практике она полезнее прямой. Прямая отвечает
    «какова вероятность при такой прибыли», обратная - «сколько надо
    заработать, чтобы шансы были приемлемыми». Ответ в базисных пунктах
    от оборота продажи, в нормальном приближении.

        P(r > -net/N) = p   =>   -net/N = z_{1-p} * sigma
        =>  net/N = -z_{1-p} * sigma = z_p * sigma
    """
    if not 0.0 < probability < 1.0:
        raise RiskError(f"вероятность должна лежать строго между 0 и 1, "
                        f"получено {probability}")
    if sigma < 0:
        raise RiskError(f"sigma не может быть отрицательной: {sigma}")
    return normal_quantile(probability) * sigma * 10_000


def normal_quantile(p: float) -> float:
    """Квантиль стандартного нормального распределения.

    Двоичный поиск по функции распределения: она монотонна, точность
    машинная за полсотни шагов. Готовая обратная функция ошибок в
    стандартной библиотеке отсутствует, а тянуть ради неё scipy - лишняя
    зависимость там, где хватает пятнадцати строк.
    """
    if not 0.0 < p < 1.0:
        raise RiskError(f"квантиль определён строго внутри (0, 1), "
                        f"получено {p}")
    low, high = -40.0, 40.0
    for _ in range(200):
        mid = (low + high) / 2
        if normal_cdf(mid) < p:
            low = mid
        else:
            high = mid
    return (low + high) / 2
