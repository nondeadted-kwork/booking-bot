"""Заявки: текст, телефон, номер заявки, уведомление владельцу, лимит, отмена кнопкой меню."""
import time

import pytest
from aiogram.types import Contact

from bot import keyboards as kb
from tests.harness import CLIENT, OWNER, Harness, make_settings, open_db, sent_to, texts_of


@pytest.fixture
async def db(tmp_path):
    database = await open_db(str(tmp_path / "leads.db"))
    yield database
    await database.close()


async def start(h: Harness) -> None:
    await h.feed(**h.message(CLIENT, "/start"))


async def test_lead_asks_phone_once_and_reaches_owner(db):
    h = Harness(make_settings(), db)
    await start(h)
    calls = await h.feed(**h.message(CLIENT, kb.BTN_LEAD))
    assert "Напишите, что нужно" in texts_of(calls)
    calls = await h.feed(**h.message(CLIENT, "Стрижка в субботу после 18:00, можно?"))
    assert "Оставьте номер" in texts_of(calls)

    contact = Contact(phone_number="+79001234567", first_name="Иван", user_id=CLIENT.id)
    calls = await h.feed(**h.message(CLIENT, contact=contact))
    assert "Заявка №1 принята" in texts_of(calls)
    [to_owner] = sent_to(calls, OWNER)
    assert "Новая заявка #1" in to_owner.text and "+79001234567" in to_owner.text and "субботу" in to_owner.text
    assert any("владелец получил" in c.text for c in sent_to(calls, CLIENT.id))

    # Вторая заявка: телефон уже известен, бот его не спрашивает
    await h.feed(**h.message(CLIENT, kb.BTN_LEAD))
    calls = await h.feed(**h.message(CLIENT, "А борода сколько стоит?"))
    assert "Заявка №2 принята" in texts_of(calls)


async def test_lead_limit_three_per_hour(db):
    h = Harness(make_settings(), db)
    await start(h)
    for i in range(3):
        await db.create_lead(CLIENT.id, f"вопрос {i}", int(time.time()))
    calls = await h.feed(**h.message(CLIENT, kb.BTN_LEAD))
    assert "уже оставили 3 заявки" in texts_of(calls)


async def test_lead_text_validation(db):
    h = Harness(make_settings(), db)
    await start(h)
    await h.feed(**h.message(CLIENT, kb.BTN_LEAD))
    assert "подробнее" in texts_of(await h.feed(**h.message(CLIENT, "ок")))
    assert "Сократите" in texts_of(await h.feed(**h.message(CLIENT, "а" * 501)))
    contact = Contact(phone_number="+79001234567", first_name="Иван", user_id=CLIENT.id)
    assert "текстом" in texts_of(await h.feed(**h.message(CLIENT, contact=contact)))


async def test_menu_button_cancels_lead(db):
    h = Harness(make_settings(), db)
    await start(h)
    await h.feed(**h.message(CLIENT, kb.BTN_LEAD))
    calls = await h.feed(**h.message(CLIENT, kb.BTN_PRICES))
    assert "Услуги и цены" in texts_of(calls)
    calls = await h.feed(**h.message(CLIENT, "просто текст"))
    assert "понимаю кнопки меню" in texts_of(calls)
    assert await db.recent_leads() == []


async def test_lead_html_is_escaped(db):
    h = Harness(make_settings(), db)
    await start(h)
    await db.set_phone(CLIENT.id, "")
    await h.feed(**h.message(CLIENT, kb.BTN_LEAD))
    calls = await h.feed(**h.message(CLIENT, "<b>Скидка</b> & <script>"))
    [to_owner] = sent_to(calls, OWNER)
    assert "&lt;b&gt;Скидка&lt;/b&gt; &amp; &lt;script&gt;" in to_owner.text
