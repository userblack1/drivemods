# Полная структура @NextModLicenseBot для копии @DriveMods_licensebot

Снимок **24 ноября 2026**. Основание: исходники, чтение службы на VPS, getMe/getMyCommands/getMyDescription/getMyShortDescription/getChatMenuButton/getWebhookInfo. Рабочий бот не перезапускался; сообщения клиентам, платежи и закупки при анализе не выполнялись. Базовый Git-коммит: `caa30c2f2f4e6145ac5b41f12919eb286acb1f3f`, репозиторий https://github.com/userblack1/drivemods.

## 1. Подтверждённая работа

Оригинал: username **NextModLicenseBot**, bot ID **9227738530**, публичное имя **DriveMods License**, BOT_NAME приложения **DriveMods LICENCE**. Служба active, PID 1242, NRestarts=0. Все **19 файлов основного серверного пакета** совпали побайтно с локальными; после нормализации CRLF/LF содержимое совпадает с Git-коммитом. Проверяемые SHA256 — в `nextmod-licensebot-reference.json`. Ещё два модуля банковских уведомлений находятся в том же пакете репозитория, но запускаются отдельными службами.

Режим **operator**: бот принимает ID устройства, предлагает пакет, выдаёт QR, хранит заказ, уведомляет оператора и доставляет утверждённый им файл. **Зачисление в банке, оформление, оплата DriveMods и проверка привязки лицензии выполняются человеком.** Собственного генератора настоящих лицензий, подключённого банковского подтверждения и рабочего сервиса автоматической закупки нет.

| Пакет | Ориентир клиента USD | Фиксированный QR KGS | Лимит закупки USD |
| --- | ---: | ---: | ---: |
| FULL | 100 | 9000 | 90 |
| ECO | 70 | 6500 | 60 |

FULL — русификация с приложениями, ECO — русификация без приложений. Это локальный каталог, а не проверка совместимости автомобиля в DriveMods. USD = закупка + MARKUP_USD=10; суммы QR заданы отдельно, без курса.

Для функционально идентичной копии нужно повторно использовать проверенное ядро, не переписывать его по пересказу. Username, token, bot ID, служба и хранилище нового бота должны отличаться. Старые заказы, пользователи и file_id автоматически не переходят. Обновление 24 ноября 2026: @DriveMods_licensebot создан и запущен; фактическое развёртывание описано в [drivemods-licensebot-deployment.md](drivemods-licensebot-deployment.md).

## 2. Технологии, структура, точки входа

Python 3.12+, проверенный сервер 3.12.16; aiogram 3.31.0, asyncio, SQLite, httpx 0.28.1, python-dotenv 1.2.4, aiohttp 3.14.5, qrcode 8.2/pypng. Все закреплённые зависимости — requirements.lock. requirements.dev.lock добавляет полный набор тестовых зависимостей, включая Pillow/zxing-cpp и Telethon. Telethon нужен боту прошивок/его тестам, для обычного лицензирования не нужен.

Цепочка старта: `server_operator.main → server_settings → Settings.load/problems → single_instance → Database + OperatorSupplier + Service → run_bot → Router/OperatorOrders/TelegramMessenger → polling + worker`.

| Файл drivemods_bot/ | Назначение |
| --- | --- |
| __init__.py | Граница пакета |
| __main__.py | CLI check, doctor, demo, run, identify, prepare-order, complete-order |
| config.py | Settings, minor units, окружение, ограничения, готовность режимов |
| models.py | State, Offer, Quote, SupplierOrder, OrderError/DeliveryRejected, demo-признак |
| telegram.py | Личные сообщения, команды/кнопки/Stars, Messenger, диспетчер и polling |
| operator_ui.py | Операторские команды, загрузка /license, карточки утверждения |
| operator_orders.py | Транзакционная операторская цепочка, QR, ссылки банка/магазина, stale-кнопки |
| storage.py | Схема и миграции SQLite, события, receipts, outbox, захват задач, recovery |
| service.py | Локальное получение ID, котировки, worker, доставка, повторы и сверка |
| supplier.py | Supplier, DemoSupplier, OperatorSupplier, BridgeSupplier, validate_license |
| purchase_policy.py | FULL ≤9000 / ECO ≤6000 центов закупки USD |
| bank_qr.py | Проверка ELQR MBANK, сумма в тыйынах, PNG |
| payment_setup.py | Предварительный расчёт KGS по явно заданному курсу, без оплаты |
| api.py | Telegram-сессия с ограниченными повторами раннего соединения |
| locking.py | Один процесс на DATA_DIR: msvcrt Windows / flock Linux |
| readiness.py | Диагностика локальной подготовки и необязательных сетевых GET/getMe |
| field_order.py | Локальные пакеты и вручную скачанная лицензия без вызова магазина |
| server_operator.py | Production: принудительный operator, запрет автозакупки |
| server_demo.py | Старый отдельный demo-запуск; не текущий production |
| bank_notices.py | Самостоятельный capture-only приём iPhone-уведомлений |
| bank_notice_forwarder.py | Отдельная пересылка непроверенных уведомлений владельцу |

В firmware_bot/ находится **другой** бот прошивок. STD_H/STD_M, каталог прошивок и пользовательская Telethon-сессия не относятся к новому боту лицензий. В tests/ проверяются оба приложения. deploy/ — шаблоны служб, scripts/ — инструменты установки/эксплуатации, docs/ — инструкции/исследования, assets/branding/ — аватары.

## 3. Сценарий клиента до мелочей

1. Личный чат → /start: запрос device.id/device.id.bin, FULL 9000 сом / ECO 6500 сом, /orders, /terms, /support. В группах роутер не обрабатывает заказ. Inline/WebApp-режимов нет.
2. ID приходит **документом**. Фактически разрешён любой basename с расширением .id/.bin. Размер >0 и ≤1048576 байт. Семантической/криптографической проверки формата устройства здесь нет.
3. Создаются uuid4().hex (32 lowercase hex) и DATA_DIR/orders/<UUID>/device.id.bin. Файл скачивается из Telegram и хешируется SHA256. В DriveMods на этом шаге ничего не отправляется.
4. Для того же user_id+SHA256 возвращается существующий заказ, кроме failed/refunded/cancelled. Лишняя загруженная копия удаляется. Другие пользователи с тем же ID получают собственные заявки. Повтор истёкшего ещё не выбранного quoted-заказа обновляет локальное предложение.
5. uploaded → quoted; предложение действует 1800 секунд. Кнопки `Принять условия · FULL · 100 $` и `... ECO · 70 $`. В выборе показан USD-ориентир, в /start и QR — KGS. /terms также содержит USD-текст. Согласие фиксируется при выборе.
6. Проверяются владелец, стадия, пакет, TTL, лимит закупки. Повтор той же кнопки идемпотентен. После принятия произвольно сменить пакет нельзя.
7. quoted → operator_payment. В одной транзакции сохраняются выбор/цены/terms_at, формируются QR и outbox-сообщения клиенту/операторам. Бот отправляет payment-qr.png **документом**, подпись с UUID/суммой/получателем и URL-кнопку `Открыть оплату в банке`.
8. /pay UUID повторяет сохранённый QR, пока заказ ожидает оплату; проверяется принадлежность и SHA256 PNG. Цена повторно не рассчитывается.
9. Клиент переводит деньги в банке и сообщает номер заказа оператору. Фото, PDF, документ-картинка или «оплатил» не меняют статус оплаты. Бот не распознаёт подлинность чека: все такие изображения считаются неподтверждёнными.
10. После операторской проверки и загрузки файла клиент получает документ **device.license.bin**, номер заказа и инструкцию сохранить имя. На диске operator-файл обычно называется operator-<случайный_UUID>.bin — имя в Telegram задаётся отдельно.
11. /orders показывает последние 10 своих заказов. /resend UUID повторяет уже delivered-файл своего заказа. Клиент сам не снимает review после ошибки первичной доставки.

Нынешний порядок — **сначала ID, затем пакет и оплата**. Вариант «сначала плати, потом пришли ID» не реализован.

## 4. Все команды и права

| Команда | Право | Действие |
| --- | --- | --- |
| /start | Клиент | Приветствие/порядок/тарифы режима |
| /myid | Личный пользователь | Его Telegram ID |
| /terms | Клиент | Условия |
| /support, /paysupport | Клиент | SUPPORT_CONTACT и номер заказа |
| /paymentinfo | Клиент | Текущие QR/цены/ручная сверка |
| /orders | Клиент | 10 своих последних заявок |
| /pay UUID | Владелец заявки | Повтор QR operator_payment |
| /resend UUID | Владелец delivered / ADMIN_IDS | Повтор файла; администратор восстанавливает approved review |
| /admin | ADMIN_IDS | 10 последних заказов всех пользователей |
| /status | ADMIN_IDS | Размещение, режим, флаги, USD-цены и отсутствие автоматической оплаты |
| /ops | ADMIN_IDS + operator | Памятка обработки |
| /device UUID | ADMIN_IDS + operator | Исходный device.id.bin с проверкой хеша |
| /bill UUID USD/KGS СУММА РЕКВИЗИТЫ | ADMIN_IDS + operator | Однократные условия заявки без уже созданного счёта/QR |
| /paid UUID MBANK/OBANK ID_ОПЕРАЦИИ СУММА | ADMIN_IDS + operator | Подготовка банковского кандидата; затем отдельная кнопка |
| /review UUID | ADMIN_IDS + operator | Актуальные кнопки банка/файла либо подсказка доставки |
| Документ .bin, подпись /license UUID ID_ЛИЦЕНЗИИ DM-ОПЛАТА | ADMIN_IDS + operator | Подготовка файла; затем отдельное утверждение |
| /cancelorder UUID ПРИЧИНА | ADMIN_IDS + operator | Только неоплаченная заявка без кандидата банка |
| /reconcile UUID | ADMIN_IDS | Сверка review с charge_id; не механизм обработки operator-заказов |
| /refund UUID + кнопка | ADMIN_IDS + live | Возврат Stars до начала закупки; в operator недоступен |

В публичном меню Telegram ровно семь команд: start, orders, support, paysupport, paymentinfo, terms, myid. Остальные — обработчики, не отдельное меню. Чужие административные команды игнорируются либо получают отказ. Оператор — только ADMIN_IDS; первый написавший клиент владельцем не становится. Владелец должен открыть чат с новым ботом для получения уведомлений.

## 5. Операторская обработка

1. /admin, /ops; /device UUID получает исходник только при совпадении SHA256.
2. Оператор сверяет свою **выписку**: входящее зачисление, получатель, сумма, валюта, настоящий уникальный ID операции. Для FULL `/paid UUID MBANK ID_ИЗ_ВЫПИСКИ 9000.00`, ECO — 6500.00.
3. Банк нормализуется в MBANK/OBANK (восклицательный знак убирается), reference — uppercase. Reference 4–100 ASCII-символов `[A-Za-z0-9._:/-]`, без пробелов. В UNIQUE поле хранится BANK:REFERENCE. Для QR допустим только MBANK, точные KGS/amount_minor; недоплата, переплата, другая валюта блокируются. Для старых не-QR заявок net_usd_cents при REQUIRE_USD_NET_CREDIT=true обязателен и равен retail_cents.
4. /paid только готовит кандидат. Карточка содержит сумму/операцию/покупателя и две кнопки. Подтвердить/отменить должен подготовивший её ADMIN_ID. Повтор идентичного предложения безопасен; другой кандидат сначала отменяется. Истинность введённого банковского ID сам бот не проверяет.
5. Подтверждение → operator_paid; сохраняются confirmed_by/confirmed_at/event, клиент и администраторы уведомляются. Отменить подтверждённую оплату этой кнопкой нельзя.
6. Оператор оформляет нужный ID и пакет в DriveMods, оплачивает FULL ≤90 USD / ECO ≤60 USD и скачивает лицензию. Бот не генерирует и не покупает её сам.
7. Отправляется .bin с подписью `/license UUID ID_ЛИЦЕНЗИИ DM-НОМЕР_ОПЛАТЫ`. Reference лицензии `[A-Za-z0-9_-]{4,100}`, платёж `DM-[A-Z0-9]{6,24}`. Эти ссылки уникальны в БД. Нужны operator_paid, confirmed_at, точная сохранённая сумма, лимит и неизменённый ID.
8. Файл непустой, ≤2097152 байт, расширение .bin. MIME image/* и PDF отсекаются. По содержимому запрещены сигнатуры PDF/PNG/JPEG/GIF, HTML/ошибочный JSON, demo-маркер в не-demo и совпадение SHA256 с исходником. Это **структурные фильтры, не криптографическая проверка DriveMods**.
9. Сохраняются ссылки магазина, staged_by, output_path/output_sha256; состояние operator_file. Карточка просит проверить устройство, пакет и оплаченный счёт. Утверждает загрузивший файл ADMIN_ID; снова проверяются деньги, лимиты и хеши. Отмена возвращает operator_paid и освобождает кандидатные ссылки.
10. Утверждение → ready → sending → delivered. После ошибки /review и административный /resend повторно проверяют утверждённый файл/ссылки/оплату и возвращают ready без закупки.
11. /cancelorder: quoted/operator_payment без bank_reference/confirmed_at, причина 3–300 символов. Ожидающие QR/bill-уведомления отменяются, клиент уведомляется. Это не возврат банковских денег.
12. /bill поддерживает положительные KGS/USD до 100000000 minor units, реквизиты 5–1500 символов. USD должен совпадать с retail_cents. Уже переданные условия молча заменить нельзя; одинаковый повтор идемпотентен. При настроенном QR обычно не нужен.

review_token — первые 12 hex SHA256 JSON-массива вида операции/UUID/актуальных банковских или файловых полей, compare_digest при проверке. Он связывает кнопку с версией кандидата; авторизацию отдельно обеспечивают ADMIN_IDS и автор. Старая кнопка после замены кандидата отвергается. Старый формат без токена требует /review.

## 6. Callback-форматы

`choose:<32hex>:full|eco`, `testpay:<32hex>:full|eco` (demo/admin), `dm:<32hex>:full|eco` (payload live-инвойса), `oppay:<32hex>:<12hex>`, `opdroppay:...`, `opfile:...`, `opdropfile:...`, `refund:<UUID>` (live/admin). URL оплаты — отдельная ссылка, не callback подтверждения.

## 7. Механика QR

Поддерживается конкретный MBANK C2C ELQR: HTTPS pay.payqr.kg, /qr или /qr/, без query, данные в fragment. Percent-decode перед TLV; длина payload 1–2048. Проверяются уникальность/длины тегов, первый 00=01, последний 63, валюта 53=417, тип 01=11/12, 52=9999, имя получателя 59, маршрут 32: c2c.mbank.kg/02/получатель из 12 цифр/13=12.

Тег 54 содержит **тыйыны**: FULL 900000, ECO 650000. Контрольная сумма — последние 4 HEX SHA256 тела uppercase, не EMV CRC16. Получатель и статический C2C-тип сохраняются; в маршруте 12=12 просит банковское приложение фиксировать сумму. Соблюдение этого запроса всеми банками не гарантировано: фактическую сумму проверяет оператор.

PNG: correction Q, box_size=9, border=4, PyPNGImage, атомарная запись через .part. Сохраняются payload/path/SHA256, KGS/amount_minor, details, необязательный qr_rate. Фиксированные суммы задаются обе вместе и без курса; на уровне заказа максимум 100000000 тыйынов. /pay проверяет прежний PNG и не меняет цену.

Это **не уникальный банковский invoice**: QR двух людей с одинаковой суммой/получателем может совпадать. UUID находится в нашей БД/подписи. Две независимые БД двух ботов не имеют общей защиты от повторного зачёта одной банковской операции. При совместном счёте оператор должен сверять операции между ботами; общий ledger потребует отдельной разработки.

## 8. Состояния, БД и файлы

Production: `uploaded → quoted → operator_payment → operator_paid → operator_file → ready → sending → delivered`. Отмена банковского кандидата остаётся в operator_payment; файла — operator_paid; неоплаченного заказа — cancelled; окончательная ошибка доставки — review; административный retry — ready. Общий enum также содержит invoice, checkout, paid, buying, waiting_supplier, failed, refunding, refunded для иных режимов. Ошибка первоначального приёма не обязательно устанавливает failed.

SQLite orders.sqlite3, WAL, Row, BEGIN IMMEDIATE, commit/rollback, timeout=3 сек на операцию. Время Unix seconds; суммы целые minor units; ID uuid4 hex. Состояния меняются с expected-state проверками. Полная схема после миграций приведена в приложении.

| Таблица | Содержимое |
| --- | --- |
| orders | Владелец/чат/исходник/хеш, workflow/state, quote/пакет/цены/Stars/условия, checkout/charge/supplier, output/хеш/file_id, ошибки/повторы/время |
| events | order_id, событие, время и административные действия |
| receipts | Только формат квитанций Stars: charge, payer, payload, currency, amount, resolution |
| notification_outbox | Устойчивые сообщения/QR, idempotency key, вложения/хеши/URL, стадии/повторы |
| operator_orders | Счёт, QR, кандидат и подтверждение банка, ссылки лицензии/платежа поставщику, авторы/утверждения |

UNIQUE: charge_id, supplier_ref, bank_reference, store_license_reference, store_payment_reference. Частичный индекс `(user_id,sha256)` исключает failed/refunded/cancelled. Миграции добавляют workflow/output_sha256, attachment-поля outbox, net_usd_cents, QR/received-поля, исправляют индекс cancelled.

На диске DATA_DIR/orders/<UUID>/device.id.bin, payment-qr.png, operator-<UUID>.bin. В иных режимах output — device.license.bin или DEMO-NOT-A-LICENSE.txt. CLI также использует field-orders/, readiness.json, demo.sqlite3. Историю и Telegram file_id оригинала в новую БД не копировать.

## 9. Очередь, сеть, восстановление

Один процесс на DATA_DIR держит bot.lock (msvcrt/flock). Это не глобальная блокировка токена: разные DATA_DIR не препятствуют ошибочному запуску одного токена дважды. У нового бота должен быть новый token.

Polling — handle_as_tasks=False, входящие update последовательно; отдельно asyncio worker. Приоритет step: уведомление → разрешённая оплаченная закупка → status поставщика → ready-доставка. Заказы выбираются по created_at/next_attempt, outbox по rowid. Пауза 0.2 сек после работы, 2 сек при простое. Гарантии выдачи за фиксированное время нет; длинный outbox может задерживать файлы. Это не очередь источника firmware-бота.

Outbox: pending → sending → sent; неактуальный QR → skipped; ошибки — задержка min(5*2^attempt,300) сек, после 10 — review. Отдельной полной панели outbox review нет (/review относится к заявке). Некоторые прямые notify_admins в иных ветках при ошибке только логируются.

Доставка: до 5 попыток, задержка 30*attempt сек. Forbidden/BadRequest, изменённый хеш — DeliveryRejected и сразу review. Потерянный ответ после принятия файла Telegram может вызвать повтор документа, но не новую закупку; exactly-once доставка не гарантирована.

recover: outbox sending → pending; buying → review (не повторять неизвестную закупку); sending заказа → ready; refunding → review. Кандидаты банка/файла остаются для /review и сами не утверждаются.

LicenseApiSession: keepalive 300 сек, до 5 попыток раннего подключения, connect/sock_connect=6 сек, паузы 0.25*(attempt+1). TLS-ошибка, общий timeout и прочие неопределённые ошибки не повторяют запрос слепо. Supplier POST также не повторяется автоматически.

## 10. Demo и live в том же коде

**operator**: server_operator принудительно задаёт operator/оба STORE=false, очищает bridge-настройки. OperatorSupplier не делает сетевых вызовов магазина. Утверждённый оператором файл переводится в ready без supplier.buy.

**demo**: в Telegram тестовые покупки доступны только ADMIN_IDS; testpay имитирует Stars, DemoSupplier возвращает маркированный текст, выход DEMO-NOT-A-LICENSE.txt. CLI demo автономен. Это не настоящий файл для установки.

**live (не подключён в production)**: invoice XTR, pre_checkout с TTL checkout 180 сек, successful_payment, matched receipts. Сверяются payer/order/plan/currency/amount, terms/quote; demo charge не разрешает закупку. Несопоставимый/второй платёж возвращается в Stars либо refund_review. Перед покупкой проверяются оплата/исходник/котировка/точная цена. Неясный POST → review; /reconcile — GET по idempotency key, не повтор закупки. Status backoff min(15*2^min(attempt,6),900), до 10 ошибок. /refund возможен до начала закупки.

BridgeSupplier — клиент собственного **предполагаемого** HTTP-контракта, не предоставленный DriveMods API. POST v1/quotes, POST v1/orders, GET v1/orders/<ref>, GET v1/orders/by-external-reference/<key>, GET v1/orders/<ref>/license; Bearer, Idempotency-Key=UUID, HTTPS, контроль цены/TTL/размера, без follow_redirects. Рабочего bridge-сервиса и принятой серверной закупки нет. Сохранение карты Link само по себе их не создаёт. PAN/CVC в боте не хранятся.

## 11. Вспомогательные MBANK-уведомления

Отдельные службы drivemods-bank-notices и drivemods-bank-notice-forwarder. Основной бот не запускает их при старте и работает без них.

Receiver на 127.0.0.1:8790: POST /capture (write Bearer), GET /internal/events (localhost+read Bearer), GET /healthz. Через nginx наружу только POST /drivemods-bank-notice. Body ≤8192 байт, text ≤4096, до 60 авторизованных capture/минуту. Раздельные URL-safe read/write ключи 40–100 символов. JSON только text/kind/event_id, UUID проверяется.

Парсер принимает тестированный MBANK-формат «Пополнение ... KGS / отправитель / Карта *.... / Доступно ...», ожидаемую маску карты и положительную сумму. В таблице phone_notices сохраняются digest, seq/notice_id/event_id/kind, minor amount/currency/card_last4/received_at; отправитель и баланс отдельными полями не сохраняются. Повторы устраняются, конфликт event_id отвергается. Ответы capture-only: payment_confirmed=false, automatic_purchase=false, bank_reference_present=false.

Forwarder читает события каждые 3 сек, пишет собственный SQLite cursor и уведомляет владельца. Он **жёстко привязан к bot ID 9227738530**, проверяет получателя в ADMIN_IDS. Для нового токена нужен отдельный экземпляр с параметризованным ожидаемым новым ID и отдельной БД cursor; capture-only сохраняется. Службы/ключи/курсор оригинала не менять. Уведомления не подтверждают клиентские оплаты.

## 12. Развёртывание и настройки копии

Оригинал: VDSka 89.169.7.15, nextmod-license-bot.service, user nextmod-bot, app /opt/nextmod-license-bot, env /etc/nextmod-license-bot.env, data /var/lib/nextmod-license-bot. Базовый deploy/nextmod-license-bot.service содержит **server_demo**; обязательный production drop-in operator.conf заменяет обе команды на server_operator (--check для preflight). Копирование одного базового unit ошибочно.

systemd: Restart=always/5 сек, TimeoutStopSec=40, UMask=0077, StateDirectoryMode=0700, NoNewPrivileges, PrivateTmp, ProtectSystem=strict, ProtectHome, ReadWritePaths DATA_DIR, ограниченные адресные семейства, журнал. Выделенный hosts-файл монтируется read-only; глобальные DNS/TLS не менять.

Созданы и проверены 24 ноября 2026: **drivemods-license-bot.service**, user/group **drivemods-license**, app **/opt/drivemods-license-bot**, env **/etc/drivemods-license-bot.env**, data **/var/lib/drivemods-license-bot**, отдельная .venv/bot.lock. Новая служба прямо вызывает server_operator. Polling не требует нового публичного порта. Не выполняйте создание повторно поверх действующего экземпляра.

Новый приватный env, права 600; token новый, ADMIN_IDS/SUPPORT_CONTACT задаёт владелец. Несекретные параметры:

```dotenv
BOT_TOKEN=
ADMIN_IDS=
SUPPORT_CONTACT=
BOT_NAME=DriveMods License
BOT_INSTANCE_LABEL=Сервер VDSka · DriveMods_licensebot
APP_MODE=operator
DATA_DIR=/var/lib/drivemods-license-bot
FULL_COST_USD=90.00
ECO_COST_USD=60.00
MARKUP_USD=10.00
STORE_UPLOADS_ENABLED=false
STORE_PURCHASES_ENABLED=false
REQUIRE_USD_NET_CREDIT=true
RECEIVING_BANK=MBANK
MBANK_QR_ENABLED=true
MBANK_QR_SOURCE_PATH=/var/lib/drivemods-license-bot/bank-qr/mbank/source.txt
MBANK_QR_FULL_KGS=9000.00
MBANK_QR_ECO_KGS=6500.00
MAX_UPLOAD_BYTES=1048576
MAX_LICENSE_BYTES=2097152
QUOTE_TTL_SECONDS=1800
```

MBANK_QR_KGS_PER_USD опустить; Stars отсутствуют/0; bridge не подключать. Настоящий payload QR получателя передать защищённо и проверить; выдуманный/тестовый QR непригоден. Старый env целиком не копировать. .env.example — пустой перечень, числовые пустые строки могут ломать парсинг. Локальный safe demo env создать setup_dev_env.py **до тестов**, проверить также родительские env и переменные процесса (load_dotenv override=False).

## 13. Известные несоответствия и пределы идентичности

- getMyDescription/getMyShortDescription оригинала сообщают NextMod/demo. Это устаревший профиль при фактическом operator. scripts/connect_telegram.py тоже записывает старый BOT_NAME и эти demo-тексты; после привязки нового token задать правильное имя/профиль, а не менять рабочий режим по описанию.
- Нет криптопроверки лицензии, распознавания фейков чеков, собственного генератора, банка API, автосписания Visa, общего межботового ledger или автоматического переноса истории.
- Нет гарантии нулевого простоя/фиксированного ожидания/exactly-once доставки. Нет общего rate-limit загрузок ID, полной панели outbox review и автоматической retention-очистки; после неудачной загрузки могут оставаться папки.
- Старые deploy_* и server_ops используют пути оригинала. Без адаптации их запуск может изменить старый бот. connect_telegram/identify читают updates: использовать только новый token при остановленном его poller.
- Новому боту нужен отдельный BotFather token. Telegram API_ID/API_HASH и Telethon-сессия для лицензий не нужны.
- Создание BotFather-бота, выдача его token, доступ второго ноутбука по SSH и новая реальная платная выдача этим анализом **не выполнены**. Прохождение тестов не заменяет эти подключения.

## 14. Приёмка будущей копии

1. Подтверждены новый username/getMe ID !=9227738530, имя, команды, новый приватный token; старый бот не изменён.
2. 19 core-файлов совпадают canonical SHA256 либо каждое допустимое отличие объяснено. Бизнес-ядро не переписано.
3. Полный pytest/аудит/CI. На базовой передаче 383 теста; реальное число получить из выбранного коммита. Ошибки/пропуски не маскировать.
4. Обе цепочки FULL/ECO через fake aiogram + временную БД: ID → quoted → QR 900000/650000 тыйынов → кандидат банка → подтверждение → .bin → утверждение → delivered.
5. Покрыты неверная сумма/валюта/банк, чужие права, дубли, stale-кнопки, хеши, PDF/HTML/demo/source-as-license, cancel/TTL/retry/restart.
6. Новые служба/venv/БД/user/env/lock изолированы; старые PID/код/данные/настройки не меняются. Preflight server_operator --check не запускает polling/закупку.
7. Живой demo допускается только с новым token и отдельным тестовым DATA_DIR; затем остановить именно новый demo-worker. Production — новая пустая БД operator; синтетические /paid/reference туда не помещать.
8. Реальная выдача требует настоящего зачисления и файла от оператора. В рамках копирования не выполнять неразрешённую закупку/перевод. Отметить отсутствие платной проверки, если она не проведена.
9. Итог: t.me/DriveMods_licensebot, новый ID без token, SHA, пути/служба, проверки, работа двух пакетов, ограничения и оставшиеся внешние доступы.


## Приложение A. Полная SQL-схема после миграций

Получено из Database + OperatorOrders на пустой синтетической БД. Производственные клиентские строки не читались.

```sql
CREATE UNIQUE INDEX orders_device_user ON orders(user_id,sha256) WHERE state NOT IN ('failed','refunded','cancelled');
```

```sql
CREATE INDEX orders_user ON orders(user_id,created_at);
```

```sql
CREATE INDEX orders_work ON orders(state,next_attempt);
```

```sql
CREATE TABLE events (
                    id INTEGER PRIMARY KEY, order_id TEXT NOT NULL,
                    event TEXT NOT NULL, created_at REAL NOT NULL
                );
```

```sql
CREATE TABLE notification_outbox (
                    id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, body TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT
                , attachment_path TEXT, attachment_sha256 TEXT, payment_url TEXT);
```

```sql
CREATE TABLE operator_orders (
                order_id TEXT PRIMARY KEY, currency TEXT, amount_minor INTEGER, details TEXT,
                bank TEXT, bank_reference TEXT UNIQUE, candidate_by INTEGER,
                confirmed_by INTEGER, confirmed_at REAL,
                store_license_reference TEXT UNIQUE, store_payment_reference TEXT UNIQUE,
                staged_by INTEGER, approved_by INTEGER, approved_at REAL
            , net_usd_cents INTEGER, qr_payload TEXT, qr_path TEXT, qr_sha256 TEXT, qr_rate TEXT, received_amount_minor INTEGER, received_currency TEXT);
```

```sql
CREATE TABLE orders (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                    filename TEXT NOT NULL, source_path TEXT NOT NULL, sha256 TEXT NOT NULL,
                    state TEXT NOT NULL, quote_json TEXT, plan TEXT, cost_cents INTEGER,
                    retail_cents INTEGER, stars INTEGER, terms_at REAL,
                    checkout_id TEXT, checkout_until REAL, charge_id TEXT UNIQUE,
                    supplier_ref TEXT UNIQUE, action_url TEXT, output_path TEXT,
                    telegram_file_id TEXT, last_error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                , workflow TEXT NOT NULL DEFAULT 'legacy', output_sha256 TEXT);
```

```sql
CREATE TABLE receipts (
                    charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    payload TEXT NOT NULL, currency TEXT NOT NULL, amount INTEGER NOT NULL,
                    resolution TEXT NOT NULL DEFAULT 'received', created_at REAL NOT NULL
                );
```

## Приложение B. Все 203 символа с привязкой к коду

AST-классы, функции и вложенные обработчики; номера строк относятся к базовому коммиту.

### drivemods_bot/__init__.py

Определений нет.

### drivemods_bot/__main__.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `ConsoleMessenger` | class | 16 |
| `ConsoleMessenger.delivered` | async function | 17 |
| `ConsoleMessenger.notify_admins` | async function | 21 |
| `demo` | async function | 25 |
| `identify` | async function | 49 |
| `main` | function | 65 |

### drivemods_bot/api.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `LicenseApiSession` | class | 12 |
| `LicenseApiSession.__init__` | function | 13 |
| `LicenseApiSession.make_request` | async function | 20 |
| `make_bot` | function | 44 |

### drivemods_bot/bank_notice_forwarder.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `notice_text` | function | 13 |
| `main` | async function | 23 |

### drivemods_bot/bank_notices.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `CreditNotice` | class | 32 |
| `normalize` | function | 38 |
| `parse_credit` | function | 42 |
| `NoticeStore` | class | 56 |
| `NoticeStore.__init__` | function | 57 |
| `NoticeStore.connect` | function | 71 |
| `NoticeStore.capture` | function | 76 |
| `NoticeStore.after` | function | 95 |
| `create_app` | function | 102 |
| `create_app.no_cache` | async function | 110 |
| `create_app.authorized` | function | 119 |
| `create_app.capture` | async function | 123 |
| `create_app.events` | async function | 165 |
| `create_app.health` | async function | 176 |
| `main` | function | 185 |

### drivemods_bot/bank_qr.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `parse_fields` | function | 13 |
| `encode_fields` | function | 29 |
| `checksum` | function | 38 |
| `MBankQr` | class | 43 |
| `MBankQr.parse` | function | 47 |
| `MBankQr.read` | function | 78 |
| `MBankQr.recipient` | function | 82 |
| `MBankQr.amount_minor` | function | 86 |
| `MBankQr.with_amount` | function | 90 |
| `MBankQr._url` | function | 107 |
| `MBankQr.url` | function | 110 |
| `MBankQr.write_png` | function | 113 |

### drivemods_bot/config.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `cents` | function | 13 |
| `Settings` | class | 24 |
| `Settings.load` | function | 54 |
| `Settings.cost` | function | 91 |
| `Settings.stars` | function | 94 |
| `Settings.problems` | function | 97 |

### drivemods_bot/field_order.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `prepare_packet` | function | 13 |
| `complete_packet` | function | 47 |

### drivemods_bot/locking.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `single_instance` | function | 8 |

### drivemods_bot/models.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `State` | class | 6 |
| `OrderError` | class | 27 |
| `DeliveryRejected` | class | 31 |
| `is_demo_order` | function | 35 |
| `Offer` | class | 40 |
| `Quote` | class | 49 |
| `Quote.dumps` | function | 54 |
| `Quote.loads` | function | 58 |
| `SupplierOrder` | class | 64 |

### drivemods_bot/operator_orders.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `review_token` | function | 17 |
| `OperatorOrders` | class | 25 |
| `OperatorOrders.__init__` | function | 26 |
| `OperatorOrders._enabled` | function | 46 |
| `OperatorOrders._admin` | function | 50 |
| `OperatorOrders._row` | function | 55 |
| `OperatorOrders.get` | function | 65 |
| `OperatorOrders._admins` | function | 70 |
| `OperatorOrders.select` | function | 74 |
| `OperatorOrders._issue_qr` | function | 113 |
| `OperatorOrders.qr_caption` | function | 148 |
| `OperatorOrders.customer_qr` | function | 158 |
| `OperatorOrders.bill` | function | 173 |
| `OperatorOrders.cancel_unpaid` | function | 199 |
| `OperatorOrders._check_net_credit` | function | 218 |
| `OperatorOrders._check_credit` | function | 224 |
| `OperatorOrders.propose_payment` | function | 236 |
| `OperatorOrders.confirm_payment` | function | 263 |
| `OperatorOrders.stage_license` | function | 294 |
| `OperatorOrders.approve_license` | function | 318 |
| `OperatorOrders.retry_delivery` | function | 347 |

### drivemods_bot/operator_ui.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `buttons` | function | 16 |
| `show_license_review` | async function | 21 |
| `show_payment_review` | async function | 37 |
| `accept_license` | async function | 59 |
| `register_operator_handlers` | function | 82 |
| `register_operator_handlers.allowed` | function | 83 |
| `register_operator_handlers.help_operator` | async function | 87 |
| `register_operator_handlers.device` | async function | 119 |
| `register_operator_handlers.bill` | async function | 136 |
| `register_operator_handlers.paid` | async function | 150 |
| `register_operator_handlers.review` | async function | 170 |
| `register_operator_handlers.cancel_order` | async function | 195 |
| `register_operator_handlers.confirm` | async function | 208 |

### drivemods_bot/payment_setup.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `amount_kgs_minor` | function | 9 |
| `readiness` | function | 30 |

### drivemods_bot/purchase_policy.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `purchase_limit` | function | 5 |
| `check_purchase_budget` | function | 12 |

### drivemods_bot/readiness.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `inspect_local` | function | 16 |
| `inspect_online` | async function | 57 |
| `doctor` | async function | 79 |

### drivemods_bot/server_demo.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `server_settings` | function | 20 |
| `check` | function | 28 |
| `main` | function | 38 |

### drivemods_bot/server_operator.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `server_settings` | function | 16 |
| `main` | function | 24 |

### drivemods_bot/service.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `Messenger` | class | 16 |
| `Messenger.delivered` | async function | 17 |
| `Messenger.notify_admins` | async function | 18 |
| `Messenger.notified` | async function | 19 |
| `Messenger.payment_qr` | async function | 20 |
| `Service` | class | 23 |
| `Service.__init__` | function | 24 |
| `Service.allocate` | function | 29 |
| `Service.upload` | async function | 35 |
| `Service._prepare_purchase` | async function | 55 |
| `Service._accept_receipt` | async function | 72 |
| `Service.step` | async function | 98 |
| `Service.reconcile` | async function | 169 |
| `Service.worker` | async function | 181 |

### drivemods_bot/storage.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `Database` | class | 10 |
| `Database.__init__` | function | 11 |
| `Database.connect` | function | 61 |
| `Database._event` | function | 74 |
| `Database.get` | function | 77 |
| `Database.recent` | function | 84 |
| `Database.add` | function | 92 |
| `Database.update` | function | 103 |
| `Database.choose` | function | 118 |
| `Database.precheckout` | function | 138 |
| `Database.paid` | function | 151 |
| `Database.record_receipt` | function | 167 |
| `Database.resolve_receipt` | function | 177 |
| `Database.has_confirmed_payment` | function | 181 |
| `Database.claim` | function | 199 |
| `Database._notice` | function | 208 |
| `Database.claim_notice` | function | 213 |
| `Database.finish_notice` | function | 221 |
| `Database.recover` | function | 231 |

### drivemods_bot/supplier.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `Supplier` | class | 17 |
| `Supplier.quote` | async function | 18 |
| `Supplier.buy` | async function | 19 |
| `Supplier.status` | async function | 20 |
| `Supplier.reconcile` | async function | 21 |
| `Supplier.download` | async function | 22 |
| `Supplier.close` | async function | 23 |
| `local_catalog` | function | 26 |
| `DemoSupplier` | class | 36 |
| `DemoSupplier.quote` | async function | 39 |
| `DemoSupplier.buy` | async function | 45 |
| `DemoSupplier.status` | async function | 48 |
| `DemoSupplier.reconcile` | async function | 51 |
| `DemoSupplier.download` | async function | 55 |
| `DemoSupplier.close` | async function | 59 |
| `OperatorSupplier` | class | 63 |
| `OperatorSupplier.quote` | async function | 65 |
| `OperatorSupplier.buy` | async function | 68 |
| `OperatorSupplier.status` | async function | 71 |
| `OperatorSupplier.reconcile` | async function | 74 |
| `OperatorSupplier.download` | async function | 77 |
| `OperatorSupplier.close` | async function | 80 |
| `BridgeSupplier` | class | 84 |
| `BridgeSupplier.__init__` | function | 90 |
| `BridgeSupplier.quote` | async function | 100 |
| `BridgeSupplier._receipt` | function | 125 |
| `BridgeSupplier.buy` | async function | 137 |
| `BridgeSupplier.status` | async function | 158 |
| `BridgeSupplier.reconcile` | async function | 161 |
| `BridgeSupplier.download` | async function | 167 |
| `BridgeSupplier.close` | async function | 179 |
| `validate_license` | function | 183 |

### drivemods_bot/telegram.py

| Символ | Вид | Строка |
| --- | --- | ---: |
| `usd` | function | 46 |
| `order_summary` | function | 50 |
| `parse_parts` | function | 55 |
| `TelegramMessenger` | class | 62 |
| `TelegramMessenger.__init__` | function | 63 |
| `TelegramMessenger.delivered` | async function | 66 |
| `TelegramMessenger.notify_admins` | async function | 78 |
| `TelegramMessenger.notified` | async function | 85 |
| `TelegramMessenger.payment_qr` | async function | 88 |
| `make_router` | function | 94 |
| `make_router.admin` | function | 102 |
| `make_router.terms` | function | 105 |
| `make_router.show_offers` | async function | 126 |
| `make_router.start` | async function | 148 |
| `make_router.myid` | async function | 168 |
| `make_router.show_terms` | async function | 172 |
| `make_router.support` | async function | 176 |
| `make_router.payment_info` | async function | 181 |
| `make_router.orders` | async function | 206 |
| `make_router.payment_qr` | async function | 211 |
| `make_router.admin_orders` | async function | 222 |
| `make_router.status` | async function | 230 |
| `make_router.reconcile` | async function | 250 |
| `make_router.resend` | async function | 266 |
| `make_router.refund` | async function | 292 |
| `make_router.confirm_refund` | async function | 308 |
| `make_router.receipt_photo` | async function | 331 |
| `make_router.document` | async function | 335 |
| `make_router.choose` | async function | 373 |
| `make_router.testpay` | async function | 400 |
| `make_router.precheckout` | async function | 419 |
| `make_router.payment` | async function | 433 |
| `make_router.other_text` | async function | 471 |
| `make_router.handler_error` | async function | 477 |
| `run_bot` | async function | 491 |

## Приложение C. Фактические несекретные настройки

null означает отсутствие в env с применением default Settings, а не пустое значение для копирования. Token, реальные ADMIN_IDS/SUPPORT_CONTACT и payload QR не публикуются.

```json
{
  "APP_MODE": "operator",
  "BOT_NAME": "DriveMods LICENCE",
  "BOT_INSTANCE_LABEL": "Сервер VDSka",
  "DATA_DIR": "/var/lib/nextmod-license-bot",
  "FULL_COST_USD": "90.00",
  "ECO_COST_USD": "60.00",
  "MARKUP_USD": "10.00",
  "FULL_PRICE_STARS": null,
  "ECO_PRICE_STARS": null,
  "STORE_UPLOADS_ENABLED": "false",
  "STORE_PURCHASES_ENABLED": "false",
  "REQUIRE_USD_NET_CREDIT": "true",
  "RECEIVING_BANK": "MBANK",
  "MBANK_QR_ENABLED": "true",
  "MBANK_QR_FULL_KGS": "9000.00",
  "MBANK_QR_ECO_KGS": "6500.00",
  "MBANK_QR_KGS_PER_USD": "",
  "MAX_UPLOAD_BYTES": null,
  "MAX_LICENSE_BYTES": null,
  "QUOTE_TTL_SECONDS": null
}
```
