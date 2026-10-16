import logging
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))


def utcnow() -> datetime:
    """Naive UTC timestamp. Columns are TIMESTAMP WITHOUT TZ; we treat them as UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fmt_msk(value: datetime | None) -> str:
    """naive UTC из БД → московское время для людей."""
    if value is None:
        return "—"
    return value.replace(tzinfo=timezone.utc).astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
