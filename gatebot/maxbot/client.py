"""Тонкий асинхронный клиент MAX Bot API (https://dev.max.ru/docs-api).

Только нужные боту методы. Ответы — dict, как в API. Ошибки — MaxApiError.
"""
import asyncio
import logging
import ssl
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://platform-api2.max.ru"
# platform-api2 подписан Russian Trusted Root CA (Минцифры), которого нет в
# стандартном trust store — доверяем ему точечно, только для этого клиента.
CA_FILE = Path(__file__).resolve().parent.parent / "certs" / "russian_trusted_root_ca.pem"

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


class MaxApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def make_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=CA_FILE)
    return ctx


class MaxClient:
    def __init__(self, token: str, base_url: str = BASE_URL, rps: float = 25) -> None:
        self._token = token
        self._base_url = base_url
        self._session: aiohttp.ClientSession | None = None
        self._min_interval = 1 / rps
        self._last_request = 0.0
        self._rate_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self._base_url,
                headers={"Authorization": self._token},
                connector=aiohttp.TCPConnector(ssl=make_ssl_context()),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def _throttle(self) -> None:
        # Лимит API — 30 rps на бота; держим запас.
        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            wait = self._last_request + self._min_interval - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = loop.time()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float = 35,
    ) -> dict:
        params = {k: _query_value(v) for k, v in (params or {}).items() if v is not None}
        session = await self._get_session()
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            await self._throttle()
            try:
                async with session.request(
                    method, path, params=params, json=json,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    data = await resp.json(content_type=None) if resp.content_length != 0 else {}
                    if resp.status in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                        await asyncio.sleep(attempt)
                        continue
                    if resp.status >= 400:
                        data = data or {}
                        raise MaxApiError(resp.status, data.get("code", ""), data.get("message", ""))
                    return data or {}
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                if attempt == _MAX_ATTEMPTS:
                    raise
                logger.warning("MAX %s %s: network error, retry %d", method, path, attempt)
                await asyncio.sleep(attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _check_success(result: dict, what: str) -> dict:
        # SimpleQueryResult: HTTP 200 даже при неуспехе, смотрим поле success.
        if result.get("success") is False:
            raise MaxApiError(200, "not.success", f"{what}: {result.get('message', '')}")
        return result

    # --- бот ---

    async def get_me(self) -> dict:
        return await self.request("GET", "/me")

    async def set_commands(self, commands: list[dict[str, str]]) -> dict:
        return await self.request("PATCH", "/me/commands", json={"commands": commands})

    # --- обновления ---

    async def get_updates(
        self, marker: int | None, timeout: int = 30, types: list[str] | None = None
    ) -> dict:
        return await self.request(
            "GET", "/updates",
            params={"marker": marker, "timeout": timeout, "limit": 100,
                    "types": ",".join(types) if types else None},
            timeout=timeout + 15,
        )

    async def get_subscriptions(self) -> list[dict]:
        return (await self.request("GET", "/subscriptions")).get("subscriptions", [])

    async def subscribe(self, url: str, secret: str, update_types: list[str]) -> dict:
        result = await self.request(
            "POST", "/subscriptions",
            json={"url": url, "secret": secret, "update_types": update_types},
        )
        return self._check_success(result, "subscribe")

    async def unsubscribe(self, url: str) -> dict:
        return self._check_success(
            await self.request("DELETE", "/subscriptions", params={"url": url}), "unsubscribe"
        )

    # --- сообщения ---

    async def send_message(
        self, *, user_id: int | None = None, chat_id: int | None = None, body: dict
    ) -> dict:
        return await self.request(
            "POST", "/messages",
            params={"user_id": user_id, "chat_id": chat_id, "disable_link_preview": True},
            json=body,
        )

    async def edit_message(self, message_id: str, body: dict) -> dict:
        return self._check_success(
            await self.request("PUT", "/messages", params={"message_id": message_id}, json=body),
            "edit_message",
        )

    async def answer_callback(
        self, callback_id: str, *, message: dict | None = None, notification: str | None = None
    ) -> dict:
        payload: dict[str, Any] = {}
        if message is not None:
            payload["message"] = message
        if notification is not None:
            payload["notification"] = notification
        return self._check_success(
            await self.request("POST", "/answers", params={"callback_id": callback_id}, json=payload),
            "answer_callback",
        )

    # --- чаты ---

    async def get_chat(self, chat_id: int) -> dict:
        return await self.request("GET", f"/chats/{chat_id}")

    async def get_members(self, chat_id: int) -> list[dict]:
        members: list[dict] = []
        marker = None
        while True:
            page = await self.request(
                "GET", f"/chats/{chat_id}/members", params={"count": 100, "marker": marker}
            )
            members.extend(page.get("members", []))
            marker = page.get("marker")
            if not marker:
                return members

    async def remove_member(self, chat_id: int, user_id: int, block: bool = False) -> dict:
        return self._check_success(
            await self.request(
                "DELETE", f"/chats/{chat_id}/members",
                params={"user_id": user_id, "block": block},
            ),
            "remove_member",
        )


def _query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value
