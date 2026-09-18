"""Нормализованный стакан и расчёт цены исполнения объёма.

Здесь живёт единственное представление стакана, с которым работает весь
остальной проект. Биржи отдают одни и те же данные четырьмя разными
способами - разными именами полей, разным порядком уровней, разным числом
элементов в уровне. Всё это разнообразие заканчивается в фетчерах; наружу
выходит OrderBook, и дальше про биржи никто не знает.

Главная величина - VWAP, средневзвешенная цена исполнения объёма
(volume-weighted average price). Наивный спред считают по лучшей цене,
но на лучшем уровне стоит ограниченный объём: чтобы купить больше,
приходится забирать следующие уровни, а они дороже. Разница между VWAP
и лучшей ценой и есть проскальзывание - первый из пяти слоёв издержек.
"""

from __future__ import annotations

from dataclasses import dataclass

# Уровень стакана: (цена, количество в базовой валюте).
Level = tuple[float, float]

# Допуск при сравнении объёмов. Остаток после вычитания копится с ошибкой
# округления, и точное сравнение с нулём здесь неуместно: 1e-17 «не хватило»
# означает, что хватило.
VOLUME_TOL = 1e-12


class OrderBookError(ValueError):
    """Стакан не прошёл проверку или запрошено бессмысленное значение."""


@dataclass(frozen=True)
class Execution:
    """Результат исполнения объёма по одной стороне стакана.

    Отдельный тип, а не просто число, нужен ради поля complete. Видимая
    глубина ограничена сотней уровней; если запрошенный объём больше их
    суммы, честной цены исполнения не существует. Вернуть при этом просто
    число - значит соврать: расчёт по частичному исполнению даст цену
    лучше настоящей, то есть завысит прибыль. README называет это прямо -
    для таких объёмов расчёт даёт оценку сверху.
    """

    vwap: float          # средневзвешенная цена исполнения
    volume: float        # сколько удалось исполнить
    cost: float          # сумма в котируемой валюте
    levels_used: int     # сколько уровней задето
    complete: bool       # хватило ли видимой глубины
    requested: float     # сколько просили


@dataclass(frozen=True)
class OrderBook:
    """Стакан одной биржи по одному инструменту в один момент времени.

    Уровни хранятся упорядоченными и проверенными: биды по убыванию цены,
    аски по возрастанию. Порядок - не деталь оформления, а предусловие
    расчёта: обход стакана «от лучшей цены» опирается именно на него.
    """

    exchange: str
    symbol: str
    bids: tuple[Level, ...]      # по убыванию цены
    asks: tuple[Level, ...]      # по возрастанию цены
    fetched_at: float            # момент получения у нас, unix-время UTC
    latency_ms: float | None = None
    source_seq: int | None = None   # версия стакана, если биржа её сообщает
    exchange_ts: float | None = None  # время снимка по часам биржи, unix UTC

    def __post_init__(self) -> None:
        _validate_side(self.bids, "bids", descending=True, where=self._where)
        _validate_side(self.asks, "asks", descending=False, where=self._where)
        # Перекрещенный стакан внутри одной биржи невозможен: он означает,
        # что кто-то готов продать дешевле, чем другой готов купить, и
        # сделка совершилась бы сама. Если мы такое видим - это ошибка
        # разбора: перепутаны стороны или не отсортированы уровни.
        if self.best_bid >= self.best_ask:
            raise OrderBookError(
                f"{self._where}: стакан перекрещен, лучший бид "
                f"{self.best_bid} не меньше лучшего аска {self.best_ask}")

    @property
    def _where(self) -> str:
        return f"{self.exchange}/{self.symbol}"

    # ---- наблюдаемые величины -------------------------------------------

    @property
    def best_bid(self) -> float:
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        return self.asks[0][0]

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def spread_bps(self) -> float:
        """Спред в базисных пунктах: 1 б.п. = 0,01 % = 0,0001."""
        return self.spread / self.best_bid * 10_000

    @property
    def exchange_lag_sec(self) -> float | None:
        """Сколько прошло между отметкой биржи и моментом получения у нас.

        Биржа сообщает своё время не всегда: Bybit и OKX сообщают, Binance
        и Kraken - нет. Где сообщает, эта величина точнее для оценки
        рассинхронизации снимков, чем наше локальное время: в неё не входит
        сетевая задержка. Где не сообщает, остаётся None, и на шаге 6
        придётся обходиться моментом получения.
        """
        if self.exchange_ts is None:
            return None
        return self.fetched_at - self.exchange_ts

    def depth(self, side: str) -> float:
        """Суммарный объём видимых уровней стороны, в базовой валюте."""
        levels = self.asks if side == "ask" else self.bids
        return sum(qty for _, qty in levels)

    # ---- цена исполнения -------------------------------------------------

    def buy(self, volume: float) -> Execution:
        """Купить `volume` базовой валюты, забирая аски снизу вверх.

        Покупатель платит то, что просят продавцы, - значит идём по аскам,
        начиная с самого дешёвого.
        """
        return _walk(self.asks, volume, self._where, "покупки")

    def sell(self, volume: float) -> Execution:
        """Продать `volume` базовой валюты, отдавая бидам сверху вниз.

        Продавец получает то, что дают покупатели, - значит идём по бидам,
        начиная с самого дорогого.
        """
        return _walk(self.bids, volume, self._where, "продажи")

    def slippage_bps(self, execution: Execution, side: str) -> float:
        """Насколько цена исполнения хуже лучшей, в базисных пунктах.

        Для покупки «хуже» значит дороже, для продажи - дешевле. Знак
        всегда неотрицательный: проскальзывание не бывает в пользу
        исполнителя.
        """
        if side == "buy":
            return (execution.vwap - self.best_ask) / self.best_ask * 10_000
        if side == "sell":
            return (self.best_bid - execution.vwap) / self.best_bid * 10_000
        raise OrderBookError(f"сторона должна быть buy или sell, а не {side!r}")

    # ---- сохранение и восстановление -------------------------------------

    def to_record(self) -> dict:
        """Стакан в виде словаря для записи в файл.

        Нужно ради графика net(V): лог наблюдений хранит только оптимум
        каждого наблюдения - одну точку, - а кривая строится по целому
        стакану. Поэтому сборщик периодически сохраняет стаканы, и
        анализ восстанавливает их отсюда же.
        """
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "fetched_at": self.fetched_at,
            "latency_ms": self.latency_ms,
            "source_seq": self.source_seq,
            "exchange_ts": self.exchange_ts,
            "bids": [list(level) for level in self.bids],
            "asks": [list(level) for level in self.asks],
        }

    @classmethod
    def from_record(cls, record: dict) -> OrderBook:
        """Восстановить стакан из словаря, записанного to_record.

        Через конструктор, а не в обход него: сохранённый стакан проходит
        те же проверки, что и свежий, и испорченный файл не просочится
        в анализ тихо.
        """
        return cls(
            exchange=record["exchange"],
            symbol=record["symbol"],
            bids=tuple((float(p), float(q)) for p, q in record["bids"]),
            asks=tuple((float(p), float(q)) for p, q in record["asks"]),
            fetched_at=float(record["fetched_at"]),
            latency_ms=record.get("latency_ms"),
            source_seq=record.get("source_seq"),
            exchange_ts=record.get("exchange_ts"),
        )

    # ---- построение ------------------------------------------------------

    @classmethod
    def normalize(
        cls,
        exchange: str,
        symbol: str,
        raw_bids: list,
        raw_asks: list,
        fetched_at: float,
        latency_ms: float | None = None,
        source_seq: int | None = None,
        exchange_ts: float | None = None,
    ) -> OrderBook:
        """Собрать стакан из сырых уровней биржи.

        Уровень приходит списком, и число элементов в нём у всех разное:
        [цена, объём] у Binance и Bybit, [цена, объём, ликвидации, ордера]
        у OKX, [цена, объём, время] у Kraken. Берём первые два и
        отбрасываем остальное. Цены и объёмы приходят строками - приводим
        к числам здесь, один раз, чтобы дальше по проекту не гадать о типе.

        Сортируем сами, не полагаясь на биржу: заявленный порядок - это
        обещание документации, а проверка предусловия стоит одну строку.
        """
        bids = sorted((_level(x, exchange, symbol) for x in raw_bids),
                      key=lambda lv: lv[0], reverse=True)
        asks = sorted((_level(x, exchange, symbol) for x in raw_asks),
                      key=lambda lv: lv[0])
        return cls(exchange=exchange, symbol=symbol,
                   bids=tuple(bids), asks=tuple(asks),
                   fetched_at=fetched_at, latency_ms=latency_ms,
                   source_seq=source_seq, exchange_ts=exchange_ts)


# --------------------------------------------------------------------------
#  Внутреннее
# --------------------------------------------------------------------------

def _level(raw, exchange: str, symbol: str) -> Level:
    try:
        price, qty = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError) as exc:
        raise OrderBookError(
            f"{exchange}/{symbol}: уровень не разбирается: {raw!r}") from exc
    return price, qty


def _validate_side(levels: tuple[Level, ...], name: str,
                   descending: bool, where: str) -> None:
    if not levels:
        raise OrderBookError(f"{where}: сторона {name} пуста")

    previous: float | None = None
    for price, qty in levels:
        if price <= 0 or qty <= 0:
            raise OrderBookError(
                f"{where}: неположительный уровень в {name}: ({price}, {qty})")
        if previous is not None:
            # Строгий порядок, а не нестрогий: два уровня с одинаковой ценой
            # в агрегированном стакане означают, что разбор что-то склеил
            # или продублировал.
            wrong = price >= previous if descending else price <= previous
            if wrong:
                raise OrderBookError(
                    f"{where}: нарушен порядок цен в {name}: "
                    f"{previous} затем {price}")
        previous = price


def _walk(levels: tuple[Level, ...], volume: float,
          where: str, what: str) -> Execution:
    """Пройти по уровням, забирая объём, пока не наберётся `volume`."""
    if volume <= 0:
        raise OrderBookError(f"{where}: объём {what} должен быть положительным, "
                             f"получено {volume}")

    remaining = volume
    cost = 0.0
    levels_used = 0

    for price, qty in levels:
        # На последнем задетом уровне берём только недостающую часть,
        # а не весь его объём. Это ровно то место, где чаще всего ошибаются.
        taken = qty if qty < remaining else remaining
        cost += price * taken
        remaining -= taken
        levels_used += 1
        if remaining <= VOLUME_TOL * volume:
            remaining = 0.0
            break

    filled = volume - remaining
    return Execution(
        vwap=cost / filled,
        volume=filled,
        cost=cost,
        levels_used=levels_used,
        complete=remaining == 0.0,
        requested=volume,
    )
