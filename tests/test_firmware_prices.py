import asyncio
from pathlib import Path

from aiogram.types import MessageEntity
import pytest

from firmware_bot.config import Settings
from firmware_bot.presentation import customer_text
from firmware_bot.response import SourceMessage
from firmware_bot.telegram import Messenger


def units(text):
    return len(text.encode("utf-16-le")) // 2


def entity_text(text, entity):
    return text.encode("utf-16-le")[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le")


@pytest.mark.parametrize("price,expected", [
    ("6 000 / 9 000 RUB.", "70 / 110 $."),
    ("6000/9000RUB", "70/110$"),
    ("6\u00a0000 / 9\u00a0000 руб.", "70 / 110 $."),
    ("6\u202f000 / 9\u202f000 ₽", "70 / 110 $"),
])
def test_exact_source_price_pair_uses_agreed_dollars(price, expected):
    text = f"Mobis GEN5. 💸 {price}\n- Русификация ШГУ."
    result, _ = customer_text(text, [])
    assert result == f"Mobis GEN5. 💸 {expected}\n- Русификация ШГУ."


def test_utf16_formatting_and_hidden_links_survive_price_edits_without_mutating_source():
    price = "6 000 / 9 000 RUB."
    text = "🚗 Mobis GEN5. 💸 " + price + "\n📋 ИНСТРУКЦИЯ\nСКАЧАТЬ\nhttps://example.com/install"
    entities = [
        MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="123456789"),
        MessageEntity(type="bold", offset=units(text[:text.index(price)]), length=units(price)),
        MessageEntity(type="bold", offset=units(text[:text.index("9 000")]), length=6),
        MessageEntity(type="text_link", offset=units(text[:text.index("СКАЧАТЬ")]), length=7, url="https://drive.google.com/file/d/unchanged"),
        MessageEntity(type="blockquote", offset=0, length=units(text)),
    ]
    original = [e.model_dump() for e in entities]
    result, adjusted = customer_text(text, entities)
    assert entity_text(result, adjusted[0]) == "🚗" and adjusted[0].custom_emoji_id == "123456789"
    assert entity_text(result, adjusted[1]) == "70 / 110 $."
    assert entity_text(result, adjusted[2]) == "110 "
    assert entity_text(result, adjusted[3]) == "СКАЧАТЬ" and adjusted[3].url == entities[3].url
    assert entity_text(result, adjusted[4]) == result
    assert [e.model_dump() for e in entities] == original
    assert "https://example.com/install" in result and price in text


@pytest.mark.parametrize("text", [
    "Пакет FULL — 90 / 160 USD. Пакет ECO — 60 / 95 USD.",
    "9 000 / 6 000 RUB", "16 000 / 9 000 RUB", "6 000 / 19 000 RUB",
    "https://example.com/6000/9000RUB", "Версия V130.220318.STD_H\nСсылка 6000/9000",
])
def test_other_prices_software_versions_and_urls_are_unchanged(text):
    assert customer_text(text, []) == (text, [])


@pytest.mark.parametrize("kind", ["text_link", "code", "pre"])
def test_link_labels_and_code_blocks_are_unchanged(kind):
    text = "6 000 / 9 000 RUB"
    fields = dict(type=kind, offset=0, length=units(text))
    if kind == "text_link":
        fields["url"] = "https://example.com/6000/9000RUB"
    entity = MessageEntity(**fields)
    assert customer_text(text, [entity]) == (text, [entity])


class Capture:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("message", text, kwargs))

    async def send_photo(self, **kwargs):
        self.calls.append(("photo", kwargs["caption"], kwargs))

    async def send_document(self, **kwargs):
        self.calls.append(("document", kwargs["caption"], kwargs))


@pytest.mark.parametrize("kind", [None, "photo", "document"])
def test_customer_delivery_rewrites_text_and_media_captions_with_links(tmp_path, kind):
    text = "Mobis GEN5. 💸 6 000 / 9 000 RUB.\nСКАЧАТЬ"
    link = MessageEntity(type="text_link", offset=units(text[:text.index("СКАЧАТЬ")]), length=7, url="https://example.com/firmware")
    source = SourceMessage(1, text, entities=[link], buttons=[[("Инструкция", "https://example.com/instruction")]], media_kind=kind)
    bot = Capture()
    files = {1: tmp_path / "attachment.bin"} if kind else {}
    asyncio.run(Messenger(bot, Settings()).deliver(dict(chat_id=11, family="IG19.KOR.SOP", variant="STD_H", id=1), [source], files))
    delivery = bot.calls[1]
    assert delivery[0] == (kind or "message")
    assert "70 / 110 $" in delivery[1] and "6 000" not in delivery[1]
    key = "caption_entities" if kind else "entities"
    assert entity_text(delivery[1], delivery[2][key][0]) == "СКАЧАТЬ"
    assert delivery[2][key][0].url == link.url
    assert delivery[2]["reply_markup"].inline_keyboard[0][0].url == "https://example.com/instruction"
    assert source.text == text and source.entities == [link]


def test_shortened_photo_caption_uses_the_actual_delivered_length(tmp_path):
    source = SourceMessage(1, "A" * 1012 + " 6 000 / 9 000 RUB", media_kind="photo")
    assert units(source.text) > 1024
    bot = Capture()
    asyncio.run(Messenger(bot, Settings()).deliver(dict(chat_id=11, family="IG19.KOR.SOP", id=1), [source], {1: Path(tmp_path / "image.jpg")}))
    assert [call[0] for call in bot.calls] == ["message", "photo", "message"]
    assert units(bot.calls[1][1]) <= 1024 and bot.calls[1][1].endswith("70 / 110 $")
