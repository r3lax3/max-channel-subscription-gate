import asyncio
from datetime import timedelta

import pytest

from core.utils import utcnow
from db.uow import UnitOfWork
from maxbot.bot import GateBot
from maxbot.client import MaxApiError

from .conftest import GROUP_ID, make_user

OWNER = 1
INVITE = "https://max.ru/join/club"


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, dict]] = []
        self.answers: list[dict] = []
        self.removed: list[tuple[int, int]] = []
        self.members: dict[int, list[int]] = {}

    async def send_message(self, *, user_id=None, chat_id=None, body):
        self.sent.append((user_id, body))
        return {"message": {}}

    async def answer_callback(self, callback_id, *, message=None, notification=None):
        self.answers.append({"id": callback_id, "message": message, "notification": notification})
        return {"success": True}

    async def get_chat(self, chat_id):
        return {"chat_id": chat_id, "title": "Клуб", "link": INVITE}

    async def remove_member(self, chat_id, user_id, block=False):
        if user_id not in self.members.get(chat_id, []):
            raise MaxApiError(200, "not.success", "not a member")
        self.members[chat_id].remove(user_id)
        self.removed.append((chat_id, user_id))
        return {"success": True}

    async def request(self, method, path, params=None, json=None, timeout=35):
        chat_id = int(path.split("/")[2])
        uid = int(params["user_ids"])
        return {"members": [{"user_id": uid}] if uid in self.members.get(chat_id, []) else []}

    def texts_to(self, user_id):
        return [body.get("text", "") for uid, body in self.sent if uid == user_id]


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def bot(client, settings, session_factory, links):
    settings.gated_chat_ids = [GROUP_ID]
    return GateBot(client, settings, session_factory, links)


def user(uid, name="Анна"):
    return {"user_id": uid, "first_name": name, "is_bot": False, "username": f"u{uid}"}


def dm(uid, text, attachments=None):
    return {
        "update_type": "message_created",
        "message": {
            "sender": user(uid),
            "recipient": {"chat_type": "dialog", "user_id": 0},
            "body": {"mid": f"m{uid}{text}", "text": text, "attachments": attachments or []},
        },
    }


def callback(uid, payload):
    return {
        "update_type": "message_callback",
        "callback": {"callback_id": f"cb-{payload}", "payload": payload, "user": user(uid)},
        "message": {},
    }


def buttons(body):
    rows = body["attachments"][0]["payload"]["buttons"] if body.get("attachments") else []
    return [b for row in rows for b in row]


async def test_start_shows_main_menu_and_registers(bot, client, session_factory):
    await bot.handle_update({"update_type": "bot_started", "chat_id": 5, "user": user(10)})
    uid, body = client.sent[-1]
    assert uid == 10 and "Анна" in body["text"] and "2000" in body["text"]
    assert any(b.get("payload") == "pay" for b in buttons(body))
    async with session_factory() as session:
        assert (await UnitOfWork(session).users.get_by_max_id(10)).first_name == "Анна"


async def test_tariff_click_creates_payment_link(bot, client, links):
    await bot.handle_update(callback(10, "pay"))
    assert any(b.get("payload") == "tariff:3" for b in buttons(client.answers[-1]["message"]))
    await bot.handle_update(callback(10, "tariff:3"))
    assert links.calls[-1]["amount"] == 5400 and links.calls[-1]["customer_extra"] == "max:10"
    pay = [b for b in buttons(client.answers[-1]["message"]) if b["type"] == "link"]
    assert pay[0]["url"].startswith("https://pay.test/")


async def test_payment_link_error_is_shown(bot, client, links):
    async def boom(**kwargs):
        raise RuntimeError("prodamus down")

    links.create_payment_link = boom
    await bot.handle_update(callback(10, "tariff:1"))
    assert "Не удалось" in client.answers[-1]["message"]["text"]


async def test_active_subscriber_gets_invite_link(bot, client, uow):
    await make_user(uow, 10, days_left=10)
    await bot.handle_update(dm(10, "/start"))
    assert INVITE in client.texts_to(10)[-1]


async def test_activation_notification_contains_link(bot, client, uow):
    u = await make_user(uow, 10, days_left=30)
    await bot.on_subscription_activated(u)
    assert INVITE in client.texts_to(10)[-1]


async def test_stranger_joining_gated_chat_is_removed(bot, client):
    client.members[GROUP_ID] = [77]
    await bot.handle_update({"update_type": "user_added", "chat_id": GROUP_ID,
                             "user": user(77), "inviter_id": None, "is_channel": False})
    assert client.removed == [(GROUP_ID, 77)]
    assert client.texts_to(77)


async def test_subscriber_joining_is_allowed(bot, client, uow):
    await make_user(uow, 10, days_left=10)
    client.members[GROUP_ID] = [10]
    await bot.handle_update({"update_type": "user_added", "chat_id": GROUP_ID,
                             "user": user(10), "inviter_id": None, "is_channel": False})
    assert client.removed == []


@pytest.mark.parametrize("update", [
    {"chat_id": GROUP_ID, "user": user(OWNER), "inviter_id": None},
    {"chat_id": GROUP_ID, "user": user(77), "inviter_id": OWNER},
    {"chat_id": -999, "user": user(77), "inviter_id": None},
])
async def test_join_exemptions(bot, client, update):
    client.members[update["chat_id"]] = [update["user"]["user_id"]]
    await bot.handle_update({"update_type": "user_added", "is_channel": False, **update})
    assert client.removed == []


async def test_group_messages_are_ignored(bot, client):
    update = dm(10, "/start")
    update["message"]["recipient"] = {"chat_type": "chat", "chat_id": GROUP_ID}
    await bot.handle_update(update)
    assert client.sent == []


async def test_id_command(bot, client):
    await bot.handle_update(dm(10, "/id"))
    assert "10" in client.texts_to(10)[-1]


async def test_non_owner_cannot_use_admin(bot, client):
    await bot.handle_update(dm(10, "/admin"))
    assert "Админ" not in client.texts_to(10)[-1]
    await bot.handle_update(callback(10, "adm:stats"))
    assert client.answers[-1]["notification"] == "Нет доступа"


async def test_admin_extend_and_revoke(bot, client, session_factory):
    client.members[GROUP_ID] = [55]
    await bot.handle_update(dm(OWNER, "/admin"))
    await bot.handle_update(callback(OWNER, "adm:users"))
    await bot.handle_update(dm(OWNER, "55"))
    assert "MAX ID: 55" in client.texts_to(OWNER)[-1]

    await bot.handle_update(callback(OWNER, "adm:extend"))
    await bot.handle_update(dm(OWNER, "2099-01-01"))
    async with session_factory() as session:
        u = await UnitOfWork(session).users.get_by_max_id(55)
        assert u.is_active and u.subscription_end_date.year == 2099

    await bot.handle_update(callback(OWNER, "adm:revoke"))
    assert client.removed == [(GROUP_ID, 55)]
    async with session_factory() as session:
        assert not (await UnitOfWork(session).users.get_by_max_id(55)).is_active


async def test_admin_bad_date(bot, client):
    await bot.handle_update(callback(OWNER, "adm:users"))
    await bot.handle_update(dm(OWNER, "55"))
    await bot.handle_update(callback(OWNER, "adm:extend"))
    await bot.handle_update(dm(OWNER, "завтра"))
    assert "Не удалось распознать" in client.texts_to(OWNER)[-1]


async def test_admin_broadcast(bot, client, uow):
    await make_user(uow, 10)
    await make_user(uow, 11)
    await bot.handle_update(callback(OWNER, "adm:bc"))
    await bot.handle_update(dm(OWNER, "Всем привет"))
    await bot.handle_update(callback(OWNER, "adm:bc_send"))
    await asyncio.gather(*bot._background)
    assert client.texts_to(10) == ["Всем привет"] and client.texts_to(11) == ["Всем привет"]
    assert "успешно — 2" in client.texts_to(OWNER)[-1]


async def test_admin_stats(bot, client, uow):
    await make_user(uow, 10, days_left=3)
    await bot.handle_update(callback(OWNER, "adm:stats"))
    assert "Активных подписчиков: 1" in client.answers[-1]["message"]["text"]


async def test_bot_added_notifies_owner(bot, client):
    await bot.handle_update({"update_type": "bot_added", "chat_id": -555, "is_channel": True,
                             "user": user(OWNER)})
    text = client.texts_to(OWNER)[-1]
    assert "-555" in text and "канал" in text
