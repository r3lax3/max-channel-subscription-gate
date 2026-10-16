"""Обработка событий MAX: меню пользователя, оплата, контроль входа, админка."""
import asyncio
import logging
import weakref
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import texts
from core.config import Settings
from core.ports import PaymentLinkFactory
from core.pricing import TARIFFS, monthly_price, tariff_price
from core.services.payment import PaymentService
from core.services.stats import StatsService
from core.services.subscription import SubscriptionService
from core.services.worker import EXPIRING_NOTICE_DAYS
from db.models import User
from db.uow import UnitOfWork
from core.notify import alert_owners
from maxbot import ui
from maxbot.client import MaxClient
from maxbot.messenger import MaxMessenger

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))

UPDATE_TYPES = [
    "bot_started", "message_created", "message_callback",
    "user_added", "bot_added", "bot_removed",
]


@dataclass
class AdminState:
    # Что ждём текстом от админа: "broadcast" | "search" | "extend" | None
    waiting: str | None = None
    selected_max_id: int | None = None
    broadcast_text: str | None = None
    broadcast_images: list[dict] = field(default_factory=list)

    @property
    def have_broadcast(self) -> bool:
        return bool(self.broadcast_text or self.broadcast_images)


class GateBot:
    def __init__(
        self,
        client: MaxClient,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        links: PaymentLinkFactory,
    ) -> None:
        self.client = client
        self.messenger = MaxMessenger(client)
        self.settings = settings
        self.session_factory = session_factory
        self.links = links
        self.admin_states: dict[int, AdminState] = {}
        self._chat_links: dict[int, str] = {}
        self._background: set[asyncio.Task] = set()
        # Слабые ссылки: замок живёт, пока его кто-то держит или ждёт.
        self._locks: weakref.WeakValueDictionary[object, asyncio.Lock] = weakref.WeakValueDictionary()

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.settings.owner_ids

    async def handle_update(self, update: dict) -> None:
        """Никогда не бросает: одно кривое событие не должно останавливать приём."""
        try:
            await self._dispatch(update)
        except Exception:
            logger.exception("Failed to handle update %r", str(update)[:500])

    async def _dispatch(self, update: dict) -> None:
        if not isinstance(update, dict):
            return
        handler = {
            "bot_started": self.on_bot_started,
            "message_created": self.on_message,
            "message_callback": self.on_callback,
            "user_added": self.on_user_added,
            "bot_added": self.on_bot_added,
            "bot_removed": self.on_bot_removed,
        }.get(str(update.get("update_type")))
        if handler is None:
            return
        # События одного пользователя — строго по очереди (ввод админа, затем
        # нажатие кнопки), разных пользователей — параллельно.
        key = str(_update_owner(update))
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        async with lock:
            await handler(update)

    # ---------- общее ----------

    async def _register(self, uow: UnitOfWork, max_user: dict) -> User:
        user = await uow.users.get_or_create(
            max_user["user_id"], max_user.get("username"), _first_name(max_user)
        )
        await uow.commit()
        return user

    async def _monthly_price(self, user: User | None) -> int:
        return monthly_price(self.settings, SubscriptionService.is_legacy_client(user))

    async def invite_links(self) -> list[str]:
        if self.settings.invite_links:
            return self.settings.invite_links
        result = []
        for chat_id in self.settings.gated_chat_ids:
            if chat_id not in self._chat_links:
                try:
                    chat_link = (await self.client.get_chat(chat_id)).get("link")
                except Exception:
                    logger.exception("Failed to get link of chat %s", chat_id)
                    chat_link = None
                if not chat_link:
                    continue
                self._chat_links[chat_id] = chat_link
            result.append(self._chat_links[chat_id])
        return result

    async def _active_screen(self, user: User) -> dict:
        paid = SubscriptionService.has_active_subscription(user)
        return ui.subscription_active(
            user.subscription_end_date if paid else None, await self.invite_links(),
            EXPIRING_NOTICE_DAYS, self.settings.support_link,
        )

    async def start_screen(self, uow: UnitOfWork, max_user: dict) -> dict:
        user = await self._register(uow, max_user)
        if SubscriptionService.has_access(user):
            return await self._active_screen(user)
        return ui.main_menu(
            user.first_name or "", await self._monthly_price(user), self.settings.support_link
        )

    async def send(self, user_id: int, body: dict) -> None:
        await self.client.send_message(user_id=user_id, body=body)

    async def on_subscription_activated(self, user: User) -> None:
        """После оплаты — новое сообщение (с уведомлением) со ссылками на клуб."""
        await self.send(user.max_user_id, await self._active_screen(user))

    async def check_setup(self) -> list[str]:
        """Проверка закрытых чатов: это группы и у бота есть нужные права."""
        problems = []
        for chat_id in self.settings.gated_chat_ids:
            try:
                chat = await self.client.get_chat(chat_id)
                me = await self.client.get_my_membership(chat_id)
            except Exception as exc:
                problems.append(f"чат {chat_id}: бот не видит чат ({exc}) — он точно в группе?")
                continue
            if chat.get("type") != "chat":
                problems.append(
                    f"чат {chat_id} — это {chat.get('type')}, а не группа: удалять участников бот не сможет"
                )
            if not me.get("is_owner"):
                missing = {"read_all_messages", "add_remove_members"} - set(me.get("permissions") or [])
                if missing:
                    problems.append(f"чат {chat_id}: боту не хватает прав администратора: {', '.join(sorted(missing))}")
        return problems

    # ---------- события пользователя ----------

    async def on_bot_started(self, update: dict) -> None:
        async with self.session_factory() as session:
            body = await self.start_screen(UnitOfWork(session), update["user"])
        await self.send(update["user"]["user_id"], body)

    async def on_message(self, update: dict) -> None:
        message = update.get("message") or {}
        if (message.get("recipient") or {}).get("chat_type") != "dialog":
            return
        sender = message.get("sender") or {}
        user_id = sender.get("user_id")
        if user_id is None or sender.get("is_bot"):
            return
        text = ((message.get("body") or {}).get("text") or "").strip()

        if text == "/id":
            await self.messenger.send_text(user_id, texts.YOUR_ID.format(user_id=user_id))
            return

        if self.is_owner(user_id):
            if text == "/admin":
                self.admin_states[user_id] = AdminState()
                await self.send(user_id, ui.admin_menu())
                return
            state = self.admin_states.get(user_id)
            if state and state.waiting and not text.startswith("/start"):
                await self.on_admin_input(user_id, state, message)
                return

        async with self.session_factory() as session:
            body = await self.start_screen(UnitOfWork(session), sender)
        await self.send(user_id, body)

    async def on_callback(self, update: dict) -> None:
        callback = update.get("callback") or {}
        payload = callback.get("payload") or ""
        max_user = callback.get("user") or {}
        user_id = max_user.get("user_id")

        if payload.startswith("adm:"):
            if not self.is_owner(user_id):
                await self.client.answer_callback(callback["callback_id"], notification="Нет доступа")
                return
            body, notification = await self.on_admin_callback(user_id, payload)
        else:
            body, notification = await self.on_user_callback(max_user, payload), None
        await self.client.answer_callback(
            callback["callback_id"], message=body, notification=notification
        )

    async def on_user_callback(self, max_user: dict, payload: str) -> dict:
        async with self.session_factory() as session:
            uow = UnitOfWork(session)
            user = await self._register(uow, max_user)
            if payload == "menu":
                return await self.start_screen(uow, max_user)
            base = await self._monthly_price(user)
            prices = {t.months: tariff_price(base, t) for t in TARIFFS}
            if payload in ("pay", "renew"):
                return ui.tariff_menu(prices, back="menu")
            tariff = ui.tariff_for(payload)
            if tariff is None:
                return await self.start_screen(uow, max_user)
            try:
                _, url = await PaymentService(uow, self.links).create_payment_link(
                    max_user_id=user.max_user_id,
                    amount=prices[tariff.months],
                    days=tariff.days,
                    product_name=texts.PRODAMUS_PRODUCT_NAME.format(title=tariff.title.lower()),
                )
            except Exception:
                logger.exception("Payment link failed for %s", user.max_user_id)
                return ui.payment_error()
            return ui.payment_link(url)

    # ---------- контроль входа ----------

    async def on_user_added(self, update: dict) -> None:
        chat_id = update.get("chat_id")
        if chat_id not in self.settings.gated_chat_ids:
            return
        max_user = update.get("user") or {}
        user_id = max_user.get("user_id")
        if user_id is None or max_user.get("is_bot") or self.is_owner(user_id):
            return
        async with self.session_factory() as session:
            uow = UnitOfWork(session)
            if self.is_owner(update.get("inviter_id")):
                # Добавил владелец — значит, свой: бесплатный доступ, чтобы ни
                # проверка входа, ни сверка участников его не выгнали.
                await uow.users.get_or_create(
                    user_id, max_user.get("username"), _first_name(max_user)
                )
                await uow.commit()
                await SubscriptionService(uow, self.messenger, self.settings).set_free_access(
                    user_id, True
                )
                logger.info("User %s added to %s by owner — free access", user_id, chat_id)
                return
            user = await uow.users.get_by_max_id(user_id)
            if SubscriptionService.has_access(user):
                logger.info("User %s joined %s with access", user_id, chat_id)
                return
        logger.info("User %s joined %s without subscription — removing", user_id, chat_id)
        try:
            await self.messenger.remove_member(chat_id, user_id)
        except Exception:
            logger.exception("Failed to remove stranger %s from %s", user_id, chat_id)
            await alert_owners(
                self.messenger, self.settings.owner_ids,
                texts.STRANGER_KICK_FAILED.format(user_id=user_id),
            )
            return
        try:
            await self.messenger.send_text(user_id, texts.JOIN_DENIED)
        except Exception:
            # Человек мог ни разу не запускать бота — написать ему нельзя.
            logger.info("Could not DM %s about denied join", user_id)

    async def on_bot_added(self, update: dict) -> None:
        chat_id = update.get("chat_id")
        kind = "канал" if update.get("is_channel") else "групповой чат"
        title = ""
        try:
            title = (await self.client.get_chat(chat_id)).get("title", "")
        except Exception:
            logger.exception("Failed to get chat %s", chat_id)
        gated = chat_id in self.settings.gated_chat_ids
        note = (
            "Он уже в списке закрытых (gated_chat_ids)." if gated
            else "Чтобы бот закрыл в него вход, добавьте этот chat_id в gated_chat_ids."
        )
        if update.get("is_channel"):
            note += "\n⚠️ Удалять подписчиков канала бот в MAX не может — закрыть можно только групповой чат."
        logger.info("Bot added to %s %s (%s)", kind, chat_id, title)
        await alert_owners(
            self.messenger, self.settings.owner_ids,
            f"Бота добавили в {kind} «{ui.plain(title)}».\nchat_id: `{chat_id}`\n{note}",
        )

    async def on_bot_removed(self, update: dict) -> None:
        chat_id = update.get("chat_id")
        logger.warning("Bot removed from chat %s", chat_id)
        if chat_id in self.settings.gated_chat_ids:
            await alert_owners(
                self.messenger, self.settings.owner_ids,
                texts.BOT_REMOVED_FROM_CLUB.format(chat_id=chat_id),
            )

    # ---------- админка ----------

    def _state(self, user_id: int) -> AdminState:
        return self.admin_states.setdefault(user_id, AdminState())

    async def on_admin_callback(self, admin_id: int, payload: str) -> tuple[dict, str | None]:
        state = self._state(admin_id)
        action = payload.removeprefix("adm:")

        if action == "menu":
            state.waiting = None
            return ui.admin_menu(), None
        if action == "bc":
            state.waiting = "broadcast"
            return ui.admin_broadcast(state.have_broadcast), None
        if action == "bc_clear":
            state.broadcast_text, state.broadcast_images = None, []
            return ui.admin_broadcast(False), "ok"
        if action == "bc_send":
            if not state.have_broadcast:
                return ui.admin_broadcast(False), texts.ADMIN_BROADCAST_EMPTY
            body = _broadcast_body(state)
            state.broadcast_text, state.broadcast_images = None, []
            state.waiting = None
            # Рассылка идёт в фоне, чтобы не задерживать обработку остальных событий.
            self._spawn(self._broadcast(admin_id, body))
            return ui.admin_text(texts.ADMIN_BROADCAST_STARTED), None
        if action == "users":
            state.waiting = "search"
            return ui.admin_prompt(texts.ADMIN_USER_PROMPT), None
        if action.startswith("create:"):
            # Карточку по MAX ID заводят заранее — чтобы выдать доступ «своим»
            # до того, как они запустят бота.
            max_id = _parse_max_id(action.removeprefix("create:"))
            if max_id is None:
                return ui.admin_menu(), texts.ADMIN_USER_NOT_FOUND
            async with self.session_factory() as session:
                uow = UnitOfWork(session)
                await uow.users.get_or_create(max_id)
                await uow.commit()
                state.selected_max_id, state.waiting = max_id, None
                return await self._user_card(uow, max_id), None
        if action == "stats":
            state.waiting = None
            async with self.session_factory() as session:
                snap = await StatsService(UnitOfWork(session)).collect()
            return ui.admin_text(texts.ADMIN_STATS.format(**snap.__dict__)), None

        # Действия карточки несут MAX ID в payload: кнопка на старой карточке
        # действует на «своего» пользователя, а не на последнего открытого.
        verb, _, raw_id = action.partition(":")
        target = _parse_max_id(raw_id)
        if target is None:
            return ui.admin_menu(), texts.ADMIN_USER_NOT_FOUND
        if verb == "extend":
            state.waiting, state.selected_max_id = "extend", target
            return ui.admin_prompt(texts.ADMIN_EXTEND_PROMPT), None
        if verb == "revoke":
            return ui.admin_confirm_revoke(target), None
        if verb == "card":
            async with self.session_factory() as session:
                return await self._user_card(UnitOfWork(session), target), None

        async with self.session_factory() as session:
            uow = UnitOfWork(session)
            service = SubscriptionService(uow, self.messenger, self.settings)
            user = await uow.users.get_by_max_id(target)
            notification = None
            if user is None:
                notification = texts.ADMIN_USER_NOT_FOUND
            elif verb == "revoke_yes":
                done = await service.revoke(target)
                notification = texts.ADMIN_REVOKE_DONE if done else texts.ADMIN_REVOKE_FAILED
            elif verb == "legacy":
                updated = await service.set_legacy_pricing(target, not user.legacy_pricing)
                notification = (
                    texts.ADMIN_LEGACY_ENABLED if updated.legacy_pricing else texts.ADMIN_LEGACY_DISABLED
                )
            elif verb == "free":
                updated = await service.set_free_access(target, not user.free_access)
                if updated is None:
                    notification = texts.ADMIN_REVOKE_FAILED
                else:
                    notification = (
                        texts.ADMIN_FREE_ENABLED if updated.free_access else texts.ADMIN_FREE_DISABLED
                    )
                    if updated.free_access:
                        self._spawn(self._notify_access(updated))
            return await self._user_card(uow, target), notification

    async def _notify_access(self, user: User) -> None:
        """Пользователю выдали доступ вручную — присылаем ссылку на клуб."""
        try:
            await self.on_subscription_activated(user)
        except Exception:
            logger.info("Could not send club link to %s", user.max_user_id, exc_info=True)

    async def on_admin_input(self, admin_id: int, state: AdminState, message: dict) -> None:
        body = message.get("body") or {}
        text = (body.get("text") or "").strip()

        if state.waiting == "broadcast":
            images = [
                {"type": "image", "payload": {"token": a["payload"]["token"]}}
                for a in body.get("attachments") or []
                if a.get("type") == "image" and (a.get("payload") or {}).get("token")
            ]
            if not text and not images:
                await self.send(admin_id, ui.admin_broadcast(state.have_broadcast))
                return
            if len(text) > ui.MAX_TEXT_LENGTH:
                await self.send(admin_id, ui.admin_prompt(
                    texts.ADMIN_BROADCAST_TOO_LONG.format(limit=ui.MAX_TEXT_LENGTH, length=len(text))
                ))
                return
            state.broadcast_text, state.broadcast_images = text or None, images
            await self.send(admin_id, ui.admin_broadcast(True))
            return

        async with self.session_factory() as session:
            uow = UnitOfWork(session)
            if state.waiting == "search":
                user = await _find_user(uow, text)
                if user is None:
                    max_id = _parse_max_id(text)
                    await self.send(admin_id, ui.admin_user_missing(max_id) if max_id else
                                    ui.admin_prompt(texts.ADMIN_USER_NOT_FOUND))
                    return
                state.selected_max_id = user.max_user_id
                state.waiting = None
                await self.send(admin_id, await self._user_card(uow, user.max_user_id))
                return

            if state.waiting == "extend":
                try:
                    day = datetime.strptime(text, "%Y-%m-%d").date()
                except ValueError:
                    day = None
                if day is None or day.year > 2099:
                    await self.send(admin_id, ui.admin_prompt(texts.ADMIN_EXTEND_BAD_DATE))
                    return
                service = SubscriptionService(uow, self.messenger, self.settings)
                user = await service.set_end_date(state.selected_max_id, end_of_day_msk(day))
                state.waiting = None
                done = (texts.ADMIN_EXTEND_DONE.format(date=day.strftime("%d.%m.%Y"))
                        if user else texts.ADMIN_REVOKE_FAILED)
                await self.messenger.send_text(admin_id, done)
                if user is not None and SubscriptionService.has_active_subscription(user):
                    self._spawn(self._notify_access(user))
                await self.send(admin_id, await self._user_card(uow, state.selected_max_id))

    async def _user_card(self, uow: UnitOfWork, max_user_id: int) -> dict:
        user = await uow.users.get_by_max_id(max_user_id)
        if user is None:
            return ui.admin_text(texts.ADMIN_USER_NOT_FOUND)
        card = texts.ADMIN_USER_CARD.format(
            id=user.id,
            max_user_id=user.max_user_id,
            first_name=ui.plain(user.first_name) or "—",
            username=f"@{ui.plain(user.username)}" if user.username else "—",
            is_active="да" if SubscriptionService.has_active_subscription(user) else "нет",
            free_access="да" if user.free_access else "нет",
            legacy_pricing="да" if user.legacy_pricing else "нет",
            subscription_end_date=ui.fmt_date(user.subscription_end_date),
            created_at=ui.fmt_date(user.created_at),
        )
        return ui.admin_user_card(card, user)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _broadcast(self, admin_id: int, body: dict) -> None:
        async with self.session_factory() as session:
            user_ids = [u.max_user_id for u in await UnitOfWork(session).users.get_all()]
        ok = failed = 0
        for user_id in user_ids:
            try:
                await self.send(user_id, body)
                ok += 1
            except Exception:
                failed += 1
        logger.info("Broadcast done: ok=%d failed=%d", ok, failed)
        try:
            await self.messenger.send_text(
                admin_id, texts.ADMIN_BROADCAST_DONE.format(ok=ok, failed=failed)
            )
        except Exception:
            logger.warning("Failed to report broadcast result to %s", admin_id, exc_info=True)


_INT64_MAX = 2**63 - 1


def _parse_max_id(text: str) -> int | None:
    text = text.strip()
    if not (text.isascii() and text.isdigit()):
        return None
    value = int(text)
    return value if 0 < value <= _INT64_MAX else None


async def _find_user(uow: UnitOfWork, query: str) -> User | None:
    max_id = _parse_max_id(query)
    if max_id is not None:
        return await uow.users.get_by_max_id(max_id)
    query = query.strip()
    if not query or len(query) > 255:
        return None
    return await uow.users.get_by_username(query)


def end_of_day_msk(day: date) -> datetime:
    """Конец указанного дня по Москве — в naive UTC, как хранится в БД."""
    return (
        datetime.combine(day, time(23, 59, 59), tzinfo=MSK)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def _update_owner(update: dict) -> object:
    for path in (("callback", "user"), ("message", "sender"), ("user",)):
        node: Any = update
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get("user_id") is not None:
            return node["user_id"]
    return ("chat", update.get("chat_id"))


def _broadcast_body(state: AdminState) -> dict[str, Any]:
    body: dict[str, Any] = {"format": "markdown", "attachments": state.broadcast_images}
    if state.broadcast_text:
        body["text"] = state.broadcast_text
    return body


def _first_name(max_user: dict) -> str | None:
    return max_user.get("first_name") or max_user.get("name")
