# Какие значения нужно достать и откуда

Сгенерировано `tools/apply_fees.py --spec` из config.yaml. Не редактировать руками: файл перезаписывается.

## Что это за величины

**`taker_fee` — комиссия тейкера.** Единицы: доля, не проценты: 0,1 % записывается как 0.001.

Спот, обычный аккаунт без VIP-уровня и без скидки за удержание токена биржи. Нужен именно тейкер: арбитраж забирает ликвидность из стакана, а не ставит лимитные заявки.

**`withdrawal_fee` — комиссия за вывод.** Единицы: в самой монете, не в USDT: 0.00002 BTC, а не 1.53 USDT.

Фиксированная плата за перевод. От объёма сделки не зависит - именно поэтому она делает мелкий арбитраж убыточным и задаёт нижнюю границу оптимального объёма.

**`min_withdrawal` — минимальная сумма вывода.** Единицы: в самой монете.

Если посчитанный оптимальный объём окажется меньше этого порога, сделка невозможна физически.

**`confirmations` — число подтверждений сети.** Единицы: целое число блоков.

Сколько блоков биржа ждёт, прежде чем зачислить депозит. Умноженное на время блока, даёт длительность перевода - тот самый горизонт, за который цена успевает уйти.

---

## Что нужно по каждой бирже

### Binance

Уже есть — 13 значений:

- [x] `taker_fee` = 0.001
- [x] `withdrawal_fee` для BTC / Bitcoin = 2e-05
- [x] `min_withdrawal` для BTC / Bitcoin = 0.0001
- [x] `confirmations` для BTC / Bitcoin = 1
- [x] `withdrawal_fee` для ETH / Ethereum = 7e-05
- [x] `min_withdrawal` для ETH / Ethereum = 0.002
- [x] `confirmations` для ETH / Ethereum = 6
- [x] `withdrawal_fee` для ETH / Arbitrum One = 2e-05
- [x] `min_withdrawal` для ETH / Arbitrum One = 0.0003
- [x] `confirmations` для ETH / Arbitrum One = 120
- [x] `withdrawal_fee` для SOL / Solana = 0.001
- [x] `min_withdrawal` для SOL / Solana = 0.01
- [x] `confirmations` для SOL / Solana = 1

### Bybit

Нужно достать — 13 значений:

- [ ] `taker_fee`
- [ ] `withdrawal_fee` для BTC / Bitcoin
- [ ] `min_withdrawal` для BTC / Bitcoin
- [ ] `confirmations` для BTC / Bitcoin
- [ ] `withdrawal_fee` для ETH / Ethereum
- [ ] `min_withdrawal` для ETH / Ethereum
- [ ] `confirmations` для ETH / Ethereum
- [ ] `withdrawal_fee` для ETH / Arbitrum One
- [ ] `min_withdrawal` для ETH / Arbitrum One
- [ ] `confirmations` для ETH / Arbitrum One
- [ ] `withdrawal_fee` для SOL / Solana
- [ ] `min_withdrawal` для SOL / Solana
- [ ] `confirmations` для SOL / Solana

### OKX

Нужно достать — 13 значений:

- [ ] `taker_fee`
- [ ] `withdrawal_fee` для BTC / Bitcoin
- [ ] `min_withdrawal` для BTC / Bitcoin
- [ ] `confirmations` для BTC / Bitcoin
- [ ] `withdrawal_fee` для ETH / Ethereum
- [ ] `min_withdrawal` для ETH / Ethereum
- [ ] `confirmations` для ETH / Ethereum
- [ ] `withdrawal_fee` для ETH / Arbitrum One
- [ ] `min_withdrawal` для ETH / Arbitrum One
- [ ] `confirmations` для ETH / Arbitrum One
- [ ] `withdrawal_fee` для SOL / Solana
- [ ] `min_withdrawal` для SOL / Solana
- [ ] `confirmations` для SOL / Solana

### Kraken

Нужно достать — 13 значений:

- [ ] `taker_fee`
- [ ] `withdrawal_fee` для BTC / Bitcoin
- [ ] `min_withdrawal` для BTC / Bitcoin
- [ ] `confirmations` для BTC / Bitcoin
- [ ] `withdrawal_fee` для ETH / Ethereum
- [ ] `min_withdrawal` для ETH / Ethereum
- [ ] `confirmations` для ETH / Ethereum
- [ ] `withdrawal_fee` для ETH / Arbitrum One
- [ ] `min_withdrawal` для ETH / Arbitrum One
- [ ] `confirmations` для ETH / Arbitrum One
- [ ] `withdrawal_fee` для SOL / Solana
- [ ] `min_withdrawal` для SOL / Solana
- [ ] `confirmations` для SOL / Solana

---

## Итого

Нужно достать: **39**. Уже есть: **13**.

## Чем это можно достать, а чем нельзя

Проверено 2026-09-17 прямыми запросами.

**Работает: публичный JSON-эндпоинт.** У Binance нашёлся адрес `/bapi/capital/v1/public/capital/getNetworkCoinAll` - тот самый, которым пользуется их собственная страница комиссий. Без авторизации, 965 монет, все три поля сразу. Отсюда взяты 12 значений Binance. Эндпоинт не описан в документации, то есть может измениться без предупреждения.

**Не работает: разбор тарифных страниц по ссылке.** Проверены страницы Binance, Bybit, OKX и две статьи справки Kraken - все отдают только оболочку, таблицы рисует браузер уже у тебя. Парсер, получающий такую ссылку, увидит пустую страницу.

**Не работает: авторизованные эндпоинты.** `/api/v5/asset/currencies` у OKX отвечает `Request header OK-ACCESS-KEY can not be empty`. README запрещает ключи.

Значит, для оставшихся трёх бирж нужен один из двух путей: найти такой же публичный JSON-адрес, как у Binance, - или сохранить страницу целиком (`Ctrl+S`) и положить файл в папку проекта, тогда разберу локально.
