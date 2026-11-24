# DriveMods: Telegram-бот лицензий

Основной проект — [@NextModLicenseBot](https://t.me/NextModLicenseBot), отображаемое имя DriveMods LICENCE. Python 3.12, aiogram, SQLite. Здесь также находятся бот прошивок и вспомогательный приёмник банковских уведомлений. Состояние на 23 ноября 2026 — в [HANDOFF.md](HANDOFF.md), совместная разработка — в [CONTRIBUTING.md](CONTRIBUTING.md).

Рабочая версия запущена на VDSka в **режиме оператора**: QR MBANK FULL 9000 KGS / ECO 6500 KGS. Поступление проверяет и лицензию покупает оператор. Push-уведомление пока не подтверждает оплату заказа; автоматическая закупка DriveMods отключена.

## Установка и проверка на втором ноутбуке

Нужны Git, Python **3.12 x64**, интернет для зависимостей и доступ к репозиторию. Производственные секреты для разработки не нужны.

```powershell
git clone https://github.com/userblack1/drivemods.git
cd <ПАПКА_ПРОЕКТА>
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.dev.lock
.\.venv\Scripts\python.exe scripts/setup_dev_env.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m drivemods_bot demo
```

`requirements.firmware.lock` включает основной `requirements.lock` и Telethon для тестов прошивок. Только лицензиям достаточно `requirements.lock`; полный набор тестов требует requirements.dev.lock, включающий оба набора и библиотеки оптической проверки QR. На Linux: `python3.12` и `.venv/bin/python` вместо Windows-путей.

`demo` работает локально с синтетическим ID и имитацией оплаты, создаёт `DEMO-NOT-A-LICENSE.txt` в `data/dev/`, не использует Telegram, Visa или магазин. Генератор `.env` отказывается перезаписывать существующий файл. `prepare.ps1` также готовит окружение, не копируя рабочие секреты.

## Настройка и запуск

`.env.example` — справочник имён с **пустыми** значениями, а не готовая конфигурация. Не копируйте его целиком в `.env`: пустые числа вызовут ошибку. Используйте генератор для demo. Для производства заполните обязательные значения, а неиспользуемые необязательные переменные опустите, чтобы действовали значения по умолчанию.

Для разработки в Telegram создайте **отдельного** тестового бота в BotFather, внесите его BOT_TOKEN и свой числовой ADMIN_IDS в локальный `.env`. Сохраните APP_MODE=demo, STORE_UPLOADS_ENABLED=false, STORE_PURCHASES_ENABLED=false, MBANK_QR_ENABLED=false. Затем:

```powershell
.\.venv\Scripts\python.exe -m drivemods_bot check
.\.venv\Scripts\python.exe -m drivemods_bot doctor
.\.venv\Scripts\python.exe -m drivemods_bot run
```

`doctor` без `--online` проверяет локальную подготовку. Отсутствие токена допускает офлайн-demo, но не Telegram-запуск. `identify` читает Telegram updates: не используйте его с действующим токеном параллельно серверу. Не запускайте рабочий токен на двух ноутбуках; `start.ps1 -LocalFallback` допустим только после согласованной остановки серверной службы.

Производственный режим требует APP_MODE=operator, BOT_TOKEN, ADMIN_IDS, SUPPORT_CONTACT, DATA_DIR, RECEIVING_BANK=MBANK, MBANK_QR_ENABLED=true, MBANK_QR_SOURCE_PATH к отдельно переданному исходному QR, MBANK_QR_FULL_KGS=9000.00, MBANK_QR_ECO_KGS=6500.00. При фиксированных суммах MBANK_QR_KGS_PER_USD не задаётся. FULL_COST_USD=90.00, ECO_COST_USD=60.00 — лимиты закупки; MARKUP_USD=10.00 даёт прежние USD-ориентиры клиента 100/70. QR-суммы задаются отдельно. Обе STORE_*_ENABLED остаются false. См. [инструкцию оператора](docs/operator-guide.md).

Рабочая точка входа — `python -m drivemods_bot.server_operator`; это действие ответственного за развёртывание, не установка ноутбука. Скрипты `scripts/deploy_*.py` изменяют сервер: для подготовки окружения их не запускать.

## Структура

| Каталог | Назначение |
| --- | --- |
| drivemods_bot/ | Заказы, тарифы, QR, SQLite, операторская проверка и доставка |
| crypto_checkout/ | Изолированный прототип сверки TRON/Binance и автомата закупки; реальные закупки не подключены |
| firmware_bot/ | @DriveModsSWBot: очередь, проверка источника, выдача прошивок |
| tests/ | Автономные тесты с временными базами и подставными клиентами |
| deploy/ | Шаблоны systemd/nginx и пустые переменные окружения |
| scripts/ | Подготовка, аудит, сборка и отдельные эксплуатационные инструменты |
| docs/ | Инструкции и исторические исследования интеграций |
| assets/branding/ | Аватары; личные изображения-референсы исключены |

Зависимости — в pyproject.toml; закреплённые версии — requirements*.lock. requirements.dev.lock добавляет Pillow/zxing-cpp для проверки QR по изображению. Основные: aiogram 3.31.0, httpx 0.28.1, python-dotenv, qrcode/pypng; прошивки — Telethon 1.45.0; тесты — pytest. SSH-помощнику scripts/server_ops.py отдельно нужен Paramiko и доступ к VPS; это не зависимость самого бота. Его локальные библиотеки из data/ не передаются.

Прошивки не запускайте с серверной Telegram-сессией. Для разработки нужны другой бот, тестовый аккаунт и отдельный DATA_DIR; переменные в .firmware.env.example, детали в [docs/firmware-bot.md](docs/firmware-bot.md).

## Проверки перед отправкой

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/handoff_audit.py --history
git diff --check
git status --short
```

Аудит выводит имена файлов/правила без значений. Он дополняет ручное ревью diff. CI проверяет код без производственных секретов и не развёртывает его.

Репозиторий владельца: https://github.com/userblack1/drivemods. По прямому указанию владельца 23 ноября 2026 он остаётся публичным. В него передаются только проверенные исходники; секреты и клиентские данные передаются отдельно.

## Прототип криптоплатежей

`python -m crypto_checkout demo` прогоняет десять синтетических заказов через проверку платежа, тестовую закупку и доставку TXT. Реальные деньги и лицензии не используются. `python -m crypto_checkout inspect --address ПУБЛИЧНЫЙ_TRON_АДРЕС` читает mainnet; торговля, вывод и Visa недоступны этой команде. Новые библиотеки для основного прототипа не требуются.

На сервере отдельно работает `drivemods-crypto-observer.service`: только наблюдение за адресом, без подтверждения заказов и закупок. Telegram-боты остаются в прежнем режиме. Ограничения общего адреса Binance, отдельная проверка доступного депозита, алгоритм Visa и отсутствующее подключение DriveMods подробно описаны в [docs/crypto-checkout-algorithm.md](docs/crypto-checkout-algorithm.md).

## Сборка конвейера лицензий и проверка запуска

`crypto_checkout.application` собирает загрузку Device ID, персональные счета, проверку TRON/Binance, последовательную закупку и отдельную доставку. Настройки перечислены с пустыми значениями в [deploy/crypto-checkout.env.example](deploy/crypto-checkout.env.example). Храните заполненный файл отдельно с ограниченным доступом. Рабочий токен в репозиторий не копируйте.

```powershell
.\.venv\Scripts\python.exe -m crypto_checkout.application --check --env-file ПУТЬ_К_ЗАЩИЩЁННОМУ_ФАЙЛУ
```

Проверка не создаёт базу, счета или лицензии и не списывает деньги. `ready: false` сопровождается конкретным перечнем недостающих подключений. Настоящий серверный исполнитель Visa и проверенная привязка нового Device ID к ответу поставщика пока отсутствуют; пройденная оплата одного прежнего заказа через браузер не включает их автоматически. Не запускайте `--run` с рабочим токеном рядом с действующей службой. Полная инструкция: [docs/crypto-application-bootstrap.md](docs/crypto-application-bootstrap.md); фактические проверки: [docs/crypto-live-readiness.md](docs/crypto-live-readiness.md).

Полный архив чистого проверенного коммита без Git-истории, секретов и рабочих данных собирается командой `python scripts/build_crypto_release.py`. В отличие от старого field-kit, он включает весь пакет `crypto_checkout`, настройки запуска и проверки. Сборка отказывается брать незакоммиченные изменения или создавать архив поверх существующего файла. Контрольные суммы находятся в manifest внутри ZIP и отдельном `.sha256`.
