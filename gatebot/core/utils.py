import logging
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC timestamp. Columns are TIMESTAMP WITHOUT TZ; we treat them as UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
