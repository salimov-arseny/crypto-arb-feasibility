Для каждого значения из вашего чеклиста подобраны прямые публичные ссылки, где эти параметры отображаются **без необходимости входа в аккаунт**.

---

### 1. Комиссии тейкера (`taker_fee`)

* **Bybit** (`exchanges.bybit.taker_fee`):
  * **Ссылка:** Bybit Fee Structure (Help Center) - https://www.bybit.com/en/help-center/article/Trading-Fee-Structure или страница тарифов Bybit Trading Fees - https://www.bybit.com/en/fees
  * **Что смотреть:** Вкладка **Spot**, строка **Non-VIP** / **VIP 0** $\rightarrow$ колонка **Taker Fee Rate** (0.1000%).
* **OKX** (`exchanges.okx.taker_fee`):
  * **Ссылка:** OKX Trading Fees - https://www.okx.com/fees
  * **Что смотреть:** Вкладка **Spot**, раздел **Regular Users (Lv 1)** $\rightarrow$ колонка **Taker** (0.100%).
* **Kraken** (`exchanges.kraken.taker_fee`)
  * **Ссылка:** Kraken Pro Fee Schedule - https://www.kraken.com/features/fee-schedule
  * **Что смотреть:** Таблица **Spot Crypto**, уровень **$0 – $2,500** (или Tier 1) $\rightarrow$ колонка **Taker**.

---

### 2. Свойства самих сетей (`block_time_sec`)

Эти значения не зависят от биржи, это параметры протоколов консенсуса:

* **BTC (Bitcoin)** (~600 с):
  * **Ссылка:** Bitcoin Whitepaper & Dev Guide - https://developer.bitcoin.org/reference/block_chain.html/ Blockchain.com BTC Stats - https://www.blockchain.com/explorer/charts/target-block-time
  * **Параметр:** Target block interval — 10 минут (600 секунд).
* **ETH (ERC20)** (12 с):
  * **Ссылка:** Ethereum.org Block Time & Slots - https://ethereum.org/en/developers/docs/blocks/#block-time
  * **Параметр:** Slot duration в консенсусе Proof-of-Stake — 12 секунд.
* **ETH (Arbitrum One)** (~0.25 с):
  * **Ссылка:** Arbitrum Official Docs: Block Time - https://docs.arbitrum.io/how-arbitrum-works/inside-arbitrum-nitro#sequencer-and-blocks
  * **Параметр:** Блоки секвенсера Nitro генерируются примерно раз в 250 мс (0.25 с).
* **SOL (Solana)** (~0.4 с):
  * **Ссылка:** Solana Official Docs: Terminology / Slot - https://docs.solanalabs.com/terminology#slot
  * **Параметр:** Целевое время слота (Slot Time Target) — 400 мс (0.4 с).

---

### 3. Биржи: комиссии вывода, минималка и подтверждения

Каждая ссылка ниже покрывает сразу связку **`withdrawal_fee`**, **`min_withdrawal`** и/или **`confirmations`** для всех четырёх монет (BTC, ETH, Arbitrum, SOL).

#### **Binance** (доступно без авторизации)
* **Все параметры (`withdrawal_fee`, `min_withdrawal`, `confirmations`):**
  * **Ссылка:** Binance Crypto Fee Schedule - https://www.binance.com/en/fee/cryptoFee 
  * **Как смотреть:** В поле поиска вводите монету (`BTC`, `ETH`, `SOL`), нажимаете на строку и разворачиваете сеть (`BTC`, `ETH/ERC20`, `Arbitrum One`, `SOL`).
  * В выпадающем списке явно видны три колонки:
    1. **Deposit Arrival (Confirmations)** — число блоков для зачисления.
    2. **Minimum Withdrawal** — минимальный вывод в монете.
    3. **Withdrawal Fee** — комиссия сети в монете.

#### **Bybit**
* **Комиссии и минимальный вывод (`withdrawal_fee`, `min_withdrawal`):**
  * **Ссылка:** Bybit Coin Info / Withdrawal Fees - https://www.bybit.com/en/fees (раздел Deposit and Withdrawal) или статья базы знаний Bybit Deposit & Withdrawal Fees FAQ - https://www.bybit.com/en/help-center/article/Bybit-Deposit-and-Withdrawal-Fees.
  * **Альтернатива (без входа):** Bybit Web Withdrawal Calculator - https://www.bybit.com/en/fiat-deposit/withdraw (выбрать Crypto).
* **Число подтверждений (`confirmations`):**
  * **Ссылка:** Bybit Help: How Long Does Deposit Take? - https://www.bybit.com/en/help-center/article/Cryptocurrency-Deposit-FAQ
  * **Что смотреть:** Таблица требуемых сетевых подтверждений (BTC = 1, ETH = 12, SOL = 1).

#### **OKX**
* **Комиссии и минимальный вывод (`withdrawal_fee`, `min_withdrawal`):**
  * **Ссылка:** OKX Fees Page (раздел Withdrawal) - https://www.okx.com/fees $\rightarrow$ переключитесь на вкладку **Withdrawal fees**.
  * **Как смотреть:** В таблице выберите актив (`BTC`, `ETH`, `SOL`) и сеть (`Bitcoin`, `ERC20`, `Arbitrum One`, `Solana`). Там приведены диапазон комиссии и минимальная сумма.
* **Число подтверждений (`confirmations`):**
  * **Ссылка:** OKX Support: Deposit Confirmation Rules - https://www.okx.com/help/why-has-my-deposit-not-been-credited
  * **Что смотреть:** Регламент времени депозита и количества блоков для подтверждения по каждой сети.

#### **Kraken**
* **Комиссии и минимальный вывод (`withdrawal_fee`, `min_withdrawal`):**
  * **Ссылка:** Kraken Support: Cryptocurrency withdrawal fees and minimums - https://support.kraken.com/hc/en-us/articles/360000767986-Cryptocurrency-withdrawal-fees-and-minimums
  * **Что смотреть:** Официальная таблица с поиском по монетам. В строках BTC, ETH (с разделением на ERC20 и Arbitrum) и SOL указаны точные фиксированные значения **Fee** и **Minimum**.
* **Число подтверждений (`confirmations`):**
  * **Ссылка:** Kraken Support: Cryptocurrency deposit processing times - https://support.kraken.com/hc/en-us/articles/203325283-Cryptocurrency-deposit-processing-times
  * **Что смотреть:** Таблица всех поддерживаемых монет, колонка **Network confirmations required** (например, для Bitcoin там указано `4 confirmations`, для Ethereum — `12 confirmations`).