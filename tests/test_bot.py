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

    chat_type = "chat"
    permissions = ["read_all_messages", "add_remove_members"]

    async def get_chat(self, chat_id):
        return {"chat_id": chat_id, "title": "Клуб", "link": INVITE, "type": self.chat_type}

    async def get_my_membership(self, chat_id):
        return {"user_id": 1, "is_admin": True, "is_owner": False, "permissions": self.permissions}

    async def remove_member(self, chat_id, user_id, block=False):
        if user_id not in self.members.get(chat_id, []):
            raise MaxApiError(200, "not.success", "not a member")
        self.members[chat_id].remove(user_id)
        self.removed.append((chat_id, user_id))
        return {"success": True}

    async def request(self, method, path, params=None, json=None, **kwargs):
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
    assert "ещё не запускал" in client.texts_to(OWNER)[-1]
    await bot.handle_update(callback(OWNER, "adm:create:55"))
    assert "MAX ID: 55" in client.answers[-1]["message"]["text"]

    await bot.handle_update(callback(OWNER, "adm:extend:55"))
    await bot.handle_update(dm(OWNER, "2099-01-01"))
    async with session_factory() as session:
        u = await UnitOfWork(session).users.get_by_max_id(55)
        # Конец дня по Москве = 20:59:59 UTC
        assert u.is_active and str(u.subscription_end_date) == "2099-01-01 20:59:59"

    await bot.handle_update(callback(OWNER, "adm:revoke:55"))
    assert client.removed == []  # сначала подтверждение
    await bot.handle_update(callback(OWNER, "adm:revoke_yes:55"))
    assert client.removed == [(GROUP_ID, 55)]
    async with session_factory() as session:
        assert not (await UnitOfWork(session).users.get_by_max_id(55)).is_active


async def test_admin_bad_date(bot, client, uow):
    await make_user(uow, 55)
    await bot.handle_update(callback(OWNER, "adm:users"))
    await bot.handle_update(dm(OWNER, "55"))
    await bot.handle_update(callback(OWNER, "adm:extend:55"))
    await bot.handle_update(dm(OWNER, "завтра"))
    assert "Не удалось распознать" in client.texts_to(OWNER)[-1]
    await bot.handle_update(dm(OWNER, "9999-12-31"))
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


@pytest.mark.parametrize("query", ["99999999999999999999999", "²", "-5", "@nobody"])
async def test_admin_search_rejects_garbage(bot, client, session_factory, query):
    await bot.handle_update(callback(OWNER, "adm:users"))
    await bot.handle_update(dm(OWNER, query))
    assert "не найден" in client.texts_to(OWNER)[-1]
    async with session_factory() as session:
        assert await UnitOfWork(session).users.count_total() == 0


async def test_broadcast_too_long_is_rejected(bot, client):
    await bot.handle_update(callback(OWNER, "adm:bc"))
    await bot.handle_update(dm(OWNER, "x" * 4001))
    assert "Слишком длинный" in client.texts_to(OWNER)[-1]


async def test_stranger_kick_failure_alerts_owner(bot, client):
    async def broken(chat_id, user_id, block=False):
        raise MaxApiError(500, "", "boom")

    client.remove_member = broken
    client.members[GROUP_ID] = [77]
    await bot.handle_update({"update_type": "user_added", "chat_id": GROUP_ID,
                             "user": user(77), "inviter_id": None, "is_channel": False})
    assert "77" in client.texts_to(OWNER)[-1]


async def test_markup_in_name_is_stripped(bot, client):
    await bot.handle_update({"update_type": "bot_started", "chat_id": 5,
                             "user": user(10, name="*Ann_[x](y)")})
    assert client.sent[-1][1]["text"].startswith("Annxy,")


async def test_check_setup_ok(bot):
    assert await bot.check_setup() == []


async def test_check_setup_reports_channel_and_missing_rights(bot, client):
    client.chat_type = "channel"
    client.permissions = ["read_all_messages"]
    problems = await bot.check_setup()
    assert len(problems) == 2


async def test_owner_with_expired_subscription_is_not_kicked(settings, session_factory, uow):
    from core.services.worker import run_worker_cycle
    from .conftest import FakeMessenger

    await make_user(uow, OWNER, days_left=-0.1)
    m = FakeMessenger()
    await run_worker_cycle(session_factory, m, settings)
    assert m.removed == []
    async with session_factory() as session:
        assert not (await UnitOfWork(session).users.get_by_max_id(OWNER)).is_active


async def test_owner_added_member_is_kept_and_owner_decides(bot, client, session_factory):
    client.members[GROUP_ID] = [77]
    await bot.handle_update({"update_type": "user_added", "chat_id": GROUP_ID,
                             "user": user(77), "inviter_id": OWNER, "is_channel": False})
    await asyncio.gather(*bot._background)
    assert client.removed == []
    owner_body = client.sent[-1][1]
    assert client.sent[-1][0] == OWNER and "77" in owner_body["text"]
    async with session_factory() as session:
        assert not (await UnitOfWork(session).users.get_by_max_id(77)).free_access

    await bot.handle_update(callback(OWNER, "adm:free:77"))
    await asyncio.gather(*bot._background)
    assert INVITE in client.texts_to(77)[-1]


async def test_free_access_user_not_told_access_closed(settings, session_factory, uow):
    from core.services.worker import run_worker_cycle
    from texts import SUBSCRIPTION_EXPIRED
    from .conftest import FakeMessenger

    await make_user(uow, 88, days_left=-0.1, free_access=True)
    m = FakeMessenger()
    await run_worker_cycle(session_factory, m, settings)
    assert m.removed == [] and (88, SUBSCRIPTION_EXPIRED) not in m.sent


async def test_lapsed_subscriber_loses_legacy_price(bot, client, uow):
    await make_user(uow, 10, days_left=-0.01, legacy_pricing=True)
    await bot.handle_update(callback(10, "pay"))
    assert "2000" in client.answers[-1]["message"]["text"]


def forward(admin, sender):
    upd = dm(admin, "")
    upd["message"]["link"] = {"type": "forward", "message": {"mid": "x", "text": "привет"}}
    if sender is not None:
        upd["message"]["link"]["sender"] = sender
    return upd


async def test_forward_opens_card_for_unknown_user(bot, client, session_factory):
    await bot.handle_update(forward(OWNER, user(555, name="Ольга")))
    card = client.texts_to(OWNER)[-1]
    assert "555" in card and "Ольга" in card
    async with session_factory() as session:
        u = await UnitOfWork(session).users.get_by_max_id(555)
        assert u is not None and u.username == "u555"


async def test_forward_works_from_user_search_prompt(bot, client):
    await bot.handle_update(callback(OWNER, "adm:users"))
    assert "Перешлите" in client.answers[-1]["message"]["text"]
    await bot.handle_update(forward(OWNER, user(556)))
    assert "556" in client.texts_to(OWNER)[-1]


async def test_forward_with_hidden_author_asks_for_id(bot, client, session_factory):
    await bot.handle_update(forward(OWNER, None))
    assert "не показал" in client.texts_to(OWNER)[-1]
    await bot.handle_update(dm(OWNER, "557"))  # дальше можно ввести ID
    assert "557" in client.texts_to(OWNER)[-1]
    async with session_factory() as session:
        assert await UnitOfWork(session).users.count_total() == 0


async def test_forward_from_non_owner_is_ignored_as_admin_action(bot, client, session_factory):
    await bot.handle_update(forward(42, user(558)))
    async with session_factory() as session:
        assert await UnitOfWork(session).users.get_by_max_id(558) is None


async def test_forward_in_broadcast_mode_is_broadcast_text(bot, client, session_factory):
    await bot.handle_update(callback(OWNER, "adm:bc"))
    upd = forward(OWNER, user(559))
    upd["message"]["body"]["text"] = "Текст рассылки"
    await bot.handle_update(upd)
    async with session_factory() as session:
        assert await UnitOfWork(session).users.get_by_max_id(559) is None


async def test_free_access_link_failure_tells_owner(bot, client):
    original = client.send_message

    async def refuse_560(*, user_id=None, chat_id=None, body):
        if user_id == 560:
            raise MaxApiError(403, "chat.denied", "user has not started the bot")
        return await original(user_id=user_id, chat_id=chat_id, body=body)

    client.send_message = refuse_560
    await bot.handle_update(forward(OWNER, user(560)))
    await bot.handle_update(callback(OWNER, "adm:free:560"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert any("не запускал бота" in t for t in client.texts_to(OWNER))
