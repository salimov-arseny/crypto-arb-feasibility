Комиссии на вывод, минимальные лимиты и число подтверждений сети лежат в модулях управления активами (**Asset / Funding**). В отличие от котировок, эти модули у большинства бирж закрыты за авторизацией (требуют API-ключ с правами `Read-Only`), поскольку параметры вывода могут зависеть от юрисдикции и уровня верификации (KYC).

Ниже подробно описано, **какие именно эндпоинты существуют, какие из них публичные, а где обязательно нужен ключ**, а также **по каким ключам в JSON доставать эти данные**.

---

### 1. Kraken

У Kraken ситуация обратная конкурентам: **комиссии мейкера/тейкера полностью открыты**, а параметров вывода в публичном REST API нет вообще (биржа официально публикует их в виде CSV).

#### А. Taker Fee (100% публично, ключ не нужен)
* **Метод / URL:**
  ```text
  GET https://api.kraken.com/0/public/AssetPairs?pair=XXBTZUSD,XETHZUSD,SOLUSD
  ```
* **Где искать в JSON:**
  В ответе внутри объекта каждой пары лежит массив:
  * `"fees"`: `[[0, 0.40], [10000, 0.35], ...]` — нулевой элемент массива содержит комиссию тейкера для базового объема (0.40%).

#### Б. Withdrawal Fee, Min Withdrawal, Confirmations
* В официальном REST API Kraken **нет публичного JSON-эндпоинта** для параметров вывода.
* Поддержка Kraken прямо указывает в документации: эти данные выгружаются разработчикам в машиночитаемом виде через **официальные CSV-файлы**:
  * Файл минималок и комиссий на вывод: [Kraken Withdrawal Minimum & Fees CSV](https://support.kraken.com/hc/en-us/articles/360000767986)
  * Файл подтверждений сети: [Kraken Minimum Confirmations CSV](https://support.kraken.com/hc/en-us/articles/203325283)
* Через API данные по комиссии за вывод можно получить только персональным запросом:
  `POST https://api.kraken.com/0/private/WithdrawInfo` *(требует API-ключ)*.

---

### 2. Bybit (API v5)

На Bybit публичны только рыночные цены. Все спецификации монет и торговые комиссии требуют **API-ключ (Read-only)**.

#### А. Withdrawal Fee, Min Withdrawal, Confirmations
* **Метод / URL:**
  ```text
  GET https://api.bybit.com/v5/asset/coin/query-info?coin=BTC
  ```
  *(Можно опустить параметр `coin`, чтобы получить сразу всю базу по всем монетам)*.
* **Требования:** Заголовки `X-BAPI-API-KEY`, `X-BAPI-SIGN`, `X-BAPI-TIMESTAMP`.
* **Где искать в JSON:**
  Ответ возвращает массив сетей `chains`:
  ```json
  "result": {
    "rows": [
      {
        "coin": "ETH",
        "chains": [
          {
            "chainType": "Arbitrum One",
            "withdrawFee": "0.0001",
            "withdrawMin": "0.001",
            "minConfirm": "20"
          },
          {
            "chainType": "ETH",
            "withdrawFee": "0.0015",
            "withdrawMin": "0.005",
            "minConfirm": "12"
          }
        ]
      }
    ]
  }
  ```

#### Б. Taker Fee
* **Метод / URL:**
  ```text
  GET https://api.bybit.com/v5/user/fee-rate?category=spot&symbol=BTCUSDT
  ```
* **Где искать в JSON:** Поле `takerFeeRate` (например, `"0.001"` = 0.10%).

---

### 3. OKX (API v5)

У OKX модуль `Funding` строго защищен: при попытке вызвать эндпоинт без ключей возвращается `401 Unauthorized` / код `50119`.

#### А. Withdrawal Fee, Min Withdrawal, Confirmations
* **Метод / URL:**
  ```text
  GET https://www.okx.com/api/v5/asset/currencies?ccy=BTC,ETH,SOL
  ```
* **Требования:** Заголовки `OK-ACCESS-KEY`, `OK-ACCESS-SIGN`, `OK-ACCESS-TIMESTAMP`, `OK-ACCESS-PASSPHRASE`.
* **Где искать в JSON:**
  Ответ содержит массив `data`, где для каждой сети идет отдельный объект:
  * `chain` — название сети (например, `ETH-Arbitrum One`, `ETH-ERC20`, `BTC-Bitcoin`).
  * `minFee` / `maxFee` — комиссия за вывод.
  * `minWd` — минимальная сумма вывода.
  * `minDepArrivalConfirm` — количество подтверждений сети для зачисления на баланс.
  * `minWdUnlockConfirm` — количество подтверждений сети для разблокировки вывода.

#### Б. Taker Fee
* **Метод / URL:**
  ```text
  GET https://www.okx.com/api/v5/account/trade-fee?instType=SPOT
  ```
* **Где искать в JSON:** Поле `taker` (для базового уровня вернет `"0.0010"` = 0.10%).

---

### Резюме: как проще всего это автоматизировать

1. **Если нет возможности завести аккаунты:**
   * **Taker fee** по Kraken забирайте напрямую через `https://api.kraken.com/0/public/AssetPairs` (он полностью открыт).
   * Базовые **Taker fees** Bybit и OKX лучше просто зашить константой `0.001` (0.10%), так как на базовом уровне они фиксированы годами и не меняются.
   * Параметры сетей (вывод/подтверждения) без ключей в виде чистого JSON получить из официальных API невозможно — придется либо парсить официальные CSV-таблицы Kraken и открытые HTML-страницы статусов Bybit/OKX, либо использовать открытые агрегаторы комиссий (например, GitHub-дампы библиотек вроде `ccxt` или открытые JSON-базы `chaincosts.com`).
2. **Если можете зарегистрировать пустые аккаунты:**
   * Создайте в каждом аккаунте по одному API-ключу с правами **только на чтение (Read-Only)** и без привязки к балансу.
   * Тогда два простых запроса: `GET /v5/asset/coin/query-info` (Bybit) и `GET /api/v5/asset/currencies` (OKX) отдадут полный массив актуальных комиссий и подтверждений сетей.