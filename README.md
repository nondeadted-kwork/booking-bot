# Бот записи на услуги · Telegram booking bot

**RU** · [EN below](#english)

> Живое демо: **@your_demo_bot** · видео 40 сек: [ссылка] · записаться можно прямо сейчас, оплата тестовая

![Скриншот](docs/screenshot.png)

## Задача

Барбершоп, салон, репетитор, автосервис: записи идут через личку и звонки, администратор
переписывает их в тетрадь, клиенты забывают прийти. Нужен бот, который сам записывает на свободное
время, берёт предоплату, напоминает клиенту и показывает владельцу расписание.

## Решение

**Клиент**
- услуга → день → время за 3 нажатия; показываются только окна, куда услуга влезает целиком;
- номер телефона — одной кнопкой «Отправить номер», спрашивается один раз;
- онлайн-оплата через Telegram Payments (ЮKassa) или «оплачу на месте»;
- напоминание за час с кнопками «Приду» / «Не смогу»;
- «Мои записи» — посмотреть и отменить.

**Владелец** (`/admin`)
- расписание на сегодня, завтра и неделю с суммами; карточка записи по `/b12`;
- уведомление о каждой новой записи, отмене и подтверждении визита;
- выходные дни одной кнопкой;
- рассылка всем клиентам (с учётом лимитов Telegram и тех, кто заблокировал бота);
- статистика за 30 дней и выгрузка всех записей в CSV для Excel.

**Надёжность.** Этим бот отличается от «собранного за вечер»:
- два человека жмут одно время одновременно → запись получает один, второму бот предлагает
  другое время (транзакция `BEGIN IMMEDIATE` + проверка пересечений, покрыто тестом на гонку);
- слот под онлайн-оплату держится 15 минут, потом освобождается сам;
- деньги пришли, а слот за это время заняли → клиенту честное сообщение, владельцу — сигнал на возврат;
- состояние записи хранится в кнопках и в базе, а не в памяти: после перезапуска бота старые
  кнопки работают, напоминания не теряются и не дублируются;
- любое необработанное исключение: клиент видит «что-то пошло не так», владелец получает
  алерт в Telegram (не чаще раза в 5 минут на один тип ошибки).

## Стек

Python 3.12 · aiogram 3 · SQLite (aiosqlite, WAL) · Docker · pytest.
Около 1 900 строк кода и 21 тест, включая сквозной сценарий записи и оплаты без реального Telegram.
Внешних сервисов, кроме Telegram, не нужно. Запускается на VPS за 200 ₽/мес.

```
bot/
  config.py       услуги, график, настройки из .env
  slots.py        расчёт свободных окон (чистые функции)
  db.py           SQLite: записи, брони, гонки, статистика
  reminders.py    фоновый цикл: напоминания и снятие неоплаченных броней
  handlers/
    client.py     путь клиента и оплата
    admin.py      панель владельца
  app.py          middleware и глобальный обработчик ошибок
tests/            слоты, база, сквозной сценарий
```

## Запуск локально

```bash
cp .env.example .env            # вставьте BOT_TOKEN и свой OWNER_IDS
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                          # 21 тест, ~2 сек
python -m bot
```

## Деплой на VPS (Timeweb / Beget / aeza, Ubuntu 24.04)

```bash
ssh root@IP
curl -fsSL https://get.docker.com | sh
git clone https://github.com/nondeadted-kwork/booking-bot.git && cd booking-bot
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f bot      # должно быть «Started @your_bot»
```

Обновление: `git pull && docker compose up -d --build`. Бэкап базы: `deploy/backup.sh` в cron.
Вариант без Docker: `deploy/booking-bot.service` (systemd).

**Онлайн-оплата.** @BotFather → `/mybots` → бот → Payments → ЮKassa → «Подключить тестовый».
Полученный токен вставьте в `PAYMENT_TOKEN`. Для боевых платежей нужен договор с ЮKassa,
токен меняется на боевой, код остаётся тот же.

## Под заказчика

| Что поменять | Где |
|---|---|
| Услуги, цены, длительность | `bot/config.py` → `SERVICES` |
| Часы работы, рабочие дни, шаг сетки | `bot/config.py` → `Schedule` |
| Название, адрес, телефон | `.env` |
| Несколько мастеров | + таблица `masters` и шаг выбора мастера, около 2 часов работы |
| Google Таблица вместо CSV | ещё около 1 часа работы |

## Что проверено поломкой

Подробно в [BREAK_IT.md](BREAK_IT.md): падение сервера, пропадание сети, повреждённая база,
неверный токен оплаты, старые кнопки, битые данные, клиент заблокировал бота.

---

<a name="english"></a>
## English

**Telegram bot that books clients into free time slots, takes prepayment and reminds them an hour before.**
Built for barbershops, salons, tutors, repair shops: anyone who books clients by hand today.

- **Client:** service → day → time in 3 taps, phone number via Telegram contact button,
  online payment (Telegram Payments) or pay on site, reminder with “I’ll be there / Cancel” buttons.
- **Owner panel:** today/tomorrow/week schedule with totals, instant notifications, days off,
  broadcast to all clients, 30-day stats, CSV export.
- **Reliability:** race-safe booking (`BEGIN IMMEDIATE` transaction, covered by a concurrency test),
  15-minute payment holds that release themselves, state kept in callback data and SQLite, so the bot
  survives restarts without losing reminders; a global error handler alerts the owner in Telegram.

**Stack:** Python 3.12, aiogram 3, SQLite, Docker. About 1,900 lines of code, 21 tests including
an end-to-end booking and payment flow with a mocked Bot API. Runs on a $3/month VPS.

```bash
cp .env.example .env && docker compose up -d --build
```

Live demo: **@your_demo_bot** (demo mode: the owner panel is open to everyone, other clients’ names are masked).
