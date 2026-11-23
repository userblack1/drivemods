from aiogram.types import Update

from scripts.connect_telegram import matching_owners


def update(user_id, text, chat_type="private", is_bot=False):
    return Update.model_validate({
        "update_id": user_id,
        "message": {
            "message_id": 1, "date": 1794050000,
            "chat": {"id": user_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": is_bot, "first_name": "Test owner"},
            "text": text,
        },
    })


def test_owner_binding_requires_the_exact_private_challenge():
    updates = [update(11, "/start"), update(12, "/start setup_wrong"),
               update(13, "/start setup_expected", "group"),
               update(14, "/start setup_expected", is_bot=True),
               update(15, "/start setup_expected")]
    assert set(matching_owners(updates, "expected")) == {15}


def test_multiple_matching_accounts_are_not_silently_collapsed_to_one():
    owners = matching_owners([update(11, "/start setup_expected"), update(12, "/start setup_expected")], "expected")
    assert len(owners) == 2


def test_repeated_update_from_same_owner_is_idempotent():
    value = update(11, "/start setup_expected")
    assert set(matching_owners([value, value], "expected")) == {11}
