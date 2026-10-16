import logging

from core.ports import Messenger

logger = logging.getLogger(__name__)


async def alert_owners(messenger: Messenger, owner_ids: list[int], text: str) -> None:
    """Сообщение всем владельцам. Ошибки доставки только логируем."""
    for owner_id in owner_ids:
        try:
            await messenger.send_text(owner_id, text)
        except Exception:
            logger.warning("Failed to alert owner %s", owner_id, exc_info=True)
