"""Экраны бота: функции возвращают тело сообщения MAX (NewMessageBody)."""
from datetime import datetime

import texts
from core.pricing import TARIFFS, Tariff
from core.utils import fmt_msk


MAX_TEXT_LENGTH = 4000
_MARKUP_CHARS = str.maketrans("", "", "*_~+^`[]()#>\\")


def plain(value: str | None, limit: int = 64) -> str:
    """Чужой текст (имя, название чата) без символов разметки — чтобы не сломать markdown."""
    return (value or "").translate(_MARKUP_CHARS).strip()[:limit]


def screen(text: str, rows: list[list[dict]] | None = None) -> dict:
    body: dict = {"text": text, "format": "markdown"}
    body["attachments"] = (
        [{"type": "inline_keyboard", "payload": {"buttons": rows}}] if rows else []
    )
    return body


def cb(text: str, payload: str) -> dict:
    return {"type": "callback", "text": text, "payload": payload}


def link(text: str, url: str) -> dict:
    return {"type": "link", "text": text, "url": url}


def _support_row(support_link: str) -> list[list[dict]]:
    return [[link(texts.BTN_SUPPORT, support_link)]] if support_link else []




# --- пользователь ---

def main_menu(first_name: str, price_month: int, support_link: str) -> dict:
    return screen(
        texts.WELCOME.format(first_name=plain(first_name) or "Здравствуйте", price_month=price_month),
        [[cb(texts.BTN_PAY, "pay")], *_support_row(support_link)],
    )


def tariff_menu(prices: dict[int, int], back: str) -> dict:
    rows = []
    for tariff in TARIFFS:
        template = texts.BTN_TARIFF_DISCOUNT if tariff.discount_percent else texts.BTN_TARIFF
        label = template.format(
            title=tariff.title, price=prices[tariff.months], discount=tariff.discount_percent
        )
        rows.append([cb(label, f"tariff:{tariff.months}")])
    rows.append([cb(texts.BTN_BACK, back)])
    return screen(
        texts.TARIFF_MENU.format(
            price_month=prices[1], price_quarter=prices[3], price_half_year=prices[6]
        ),
        rows,
    )


def payment_link(pay_url: str) -> dict:
    return screen(
        texts.PAYMENT_INFO,
        [[link(texts.BTN_PAY_LINK, pay_url)], [cb(texts.BTN_BACK, "pay")]],
    )


def payment_error() -> dict:
    return screen(texts.PAYMENT_ERROR, [[cb(texts.BTN_BACK, "pay")]])


def subscription_active(
    end: datetime | None, invite_links: list[str], notice_days: int, support_link: str
) -> dict:
    links = "\n".join(
        f"[Ссылка для входа в клуб]({url})" if len(invite_links) == 1
        else f"[Ссылка для входа №{i}]({url})"
        for i, url in enumerate(invite_links, 1)
    ) or "Ссылка на клуб скоро появится — напишите в поддержку."
    return screen(
        (texts.SUBSCRIPTION_ACTIVE.format(
            subscription_end=fmt_msk(end), invite_links=links, notice_days=notice_days
        ) if end else texts.FREE_ACCESS_ACTIVE.format(invite_links=links)),
        # Бесплатный доступ продлевать нечего.
        [*([[cb(texts.BTN_RENEW, "renew")]] if end else []), *_support_row(support_link)],
    )


def tariff_for(payload: str) -> Tariff | None:
    try:
        months = int(payload.split(":", 1)[1])
    except (IndexError, ValueError):
        return None
    return next((t for t in TARIFFS if t.months == months), None)


# --- админка ---

def admin_menu() -> dict:
    return screen(texts.ADMIN_MAIN, [
        [cb(texts.BTN_ADMIN_BROADCAST, "adm:bc")],
        [cb(texts.BTN_ADMIN_USERS, "adm:users")],
        [cb(texts.BTN_ADMIN_STATS, "adm:stats")],
    ])


def admin_back_row() -> list[dict]:
    return [cb(texts.BTN_BACK, "adm:menu")]


def admin_broadcast(have_content: bool) -> dict:
    rows = []
    if have_content:
        rows.append([cb(texts.BTN_ADMIN_SEND, "adm:bc_send"), cb(texts.BTN_ADMIN_CLEAR, "adm:bc_clear")])
    rows.append(admin_back_row())
    return screen(
        texts.ADMIN_BROADCAST_READY if have_content else texts.ADMIN_BROADCAST_PROMPT, rows
    )


def admin_prompt(text: str) -> dict:
    return screen(text, [admin_back_row()])


def admin_user_card(card: str, user) -> dict:
    uid = user.max_user_id
    return screen(card, [
        [cb(texts.BTN_ADMIN_EXTEND, f"adm:extend:{uid}")],
        [cb(texts.BTN_ADMIN_FREE_OFF if user.free_access else texts.BTN_ADMIN_FREE_ON, f"adm:free:{uid}")],
        [cb(texts.BTN_ADMIN_LEGACY_OFF if user.legacy_pricing else texts.BTN_ADMIN_LEGACY_ON, f"adm:legacy:{uid}")],
        [cb(texts.BTN_ADMIN_REVOKE, f"adm:revoke:{uid}")],
        [cb(texts.BTN_ADMIN_FIND_ANOTHER, "adm:users")],
        admin_back_row(),
    ])


def admin_confirm_revoke(max_user_id: int) -> dict:
    return screen(texts.ADMIN_REVOKE_CONFIRM.format(max_user_id=max_user_id), [
        [cb(texts.BTN_ADMIN_REVOKE_YES, f"adm:revoke_yes:{max_user_id}")],
        [cb(texts.BTN_BACK, f"adm:card:{max_user_id}")],
    ])


def admin_user_missing(max_id: int) -> dict:
    return screen(texts.ADMIN_USER_MISSING.format(max_user_id=max_id), [
        [cb(texts.BTN_ADMIN_CREATE, f"adm:create:{max_id}")],
        [cb(texts.BTN_ADMIN_FIND_ANOTHER, "adm:users")],
        admin_back_row(),
    ])


def owner_added_member(max_user_id: int, name: str) -> dict:
    return screen(texts.OWNER_ADDED_MEMBER.format(name=name or "—", max_user_id=max_user_id), [
        [cb(texts.BTN_ADMIN_FREE_ON, f"adm:free:{max_user_id}")],
        [cb(texts.BTN_ADMIN_OPEN_CARD, f"adm:card:{max_user_id}")],
    ])


def admin_text(text: str) -> dict:
    return screen(text, [admin_back_row()])
