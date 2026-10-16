import hashlib
import hmac
import json
import logging
from datetime import timedelta
from urllib.parse import quote

import aiohttp

from core.config import Settings
from core.utils import utcnow

logger = logging.getLogger(__name__)

LINK_EXPIRATION_HOURS = 48


class ProdamusClient:
    """Создаёт платёжные ссылки Prodamus."""

    def __init__(self, settings: Settings) -> None:
        self.domain = settings.prodamus_domain
        self.secret = settings.prodamus_secret_key
        self.bot_link = settings.bot_link

    async def create_payment_link(
        self,
        order_id: int,
        amount: int,
        customer_extra: int,
        product_name: str,
    ) -> str:
        expires_at = utcnow() + timedelta(hours=LINK_EXPIRATION_HOURS)

        params = {
            "do": "link",
            "products": [
                {
                    "name": product_name,
                    "price": amount,
                    "quantity": 1,
                    "paymentMethod": 4,
                    "paymentObject": 4,
                }
            ],
            "order_id": order_id,
            "customer_extra": customer_extra,
            "link_expired": expires_at.strftime("%Y-%m-%d %H:%M"),
            "paid_content": self.bot_link,
            "sys": "",
        }

        signature = _create_hmac(params, self.secret)
        params["signature"] = signature

        query = _http_build_query(params)
        url = f"https://{self.domain}/?{query}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                payment_link = (await resp.text()).strip()

        logger.info("Payment link created for order %s", order_id)
        return payment_link


def _create_hmac(data: dict, key: str) -> str:
    normalized = _stringify_and_sort(data)
    data_str = json.dumps(
        normalized,
        separators=(",", ":"),
        ensure_ascii=False,
    ).replace("/", "\\/")
    return hmac.new(
        key.encode("utf-8"),
        data_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _stringify_and_sort(data):
    if isinstance(data, dict):
        return {k: _stringify_and_sort(v) for k, v in sorted(data.items())}
    if isinstance(data, list):
        return [_stringify_and_sort(item) for item in data]
    return str(data)


def _http_build_query(data: dict, prefix: str = "") -> str:
    parts: list[str] = []
    for key, value in data.items():
        full_key = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            parts.append(_http_build_query(value, full_key))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                item_key = f"{full_key}[{i}]"
                if isinstance(item, dict):
                    parts.append(_http_build_query(item, item_key))
                else:
                    parts.append(f"{quote(item_key)}={quote(str(item))}")
        else:
            parts.append(f"{quote(full_key)}={quote(str(value))}")
    return "&".join(parts)


def verify_webhook_signature(form: dict[str, str], signature: str, key: str) -> bool:
    """Проверяет заголовок `Sign` вебхука Prodamus.

    Prodamus подписывает тело так же, как ссылку: HMAC-SHA256 от JSON с
    отсортированными ключами. Плоские ключи формы (`products[0][name]`)
    сначала собираются обратно во вложенную структуру.
    """
    if not signature or not key:
        return False
    expected = _create_hmac(_unflatten(form), key)
    return hmac.compare_digest(expected, signature.strip().lower())


def _unflatten(form: dict[str, str]) -> dict:
    root: dict = {}
    for flat_key, value in form.items():
        head, *rest = flat_key.replace("]", "").split("[")
        node = root
        path = [head, *rest]
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return _dicts_to_lists(root)


def _dicts_to_lists(node):
    if not isinstance(node, dict):
        return node
    converted = {k: _dicts_to_lists(v) for k, v in node.items()}
    keys = list(converted)
    if keys and keys == [str(i) for i in range(len(keys))]:
        return [converted[k] for k in keys]
    return converted
