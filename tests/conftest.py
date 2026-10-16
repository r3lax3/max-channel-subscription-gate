from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.config import Settings
from core.utils import utcnow
from db.models import Base
from db.uow import UnitOfWork

GROUP_ID = -1001
CHANNEL_ID = -1002


class FakeMessenger:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.removed: list[tuple[int, int]] = []
        self.fail_remove_for: set[int] = set()
        self.members: dict[int, list[int]] = {}

    async def send_text(self, user_id: int, text: str) -> None:
        self.sent.append((user_id, text))

    async def remove_member(self, chat_id: int, user_id: int) -> None:
        if user_id in self.fail_remove_for:
            raise RuntimeError("remove failed")
        self.removed.append((chat_id, user_id))

    async def list_plain_member_ids(self, chat_id: int) -> list[int]:
        return self.members.get(chat_id, [])


class FakeLinks:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_payment_link(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return f"https://pay.test/{kwargs['order_id']}"


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        bot_token="test-token",
        owner_ids=[1],
        support_link="https://max.ru/support",
        bot_link="https://max.ru/test_bot",
        gated_chat_ids=[GROUP_ID, CHANNEL_ID],
        subscription_price=2000,
        subscription_price_legacy=1500,
        prodamus_domain="test.payform.ru",
        prodamus_secret_key="test_secret",
    )


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def uow(session_factory):
    async with session_factory() as session:
        yield UnitOfWork(session)


@pytest.fixture
def messenger():
    return FakeMessenger()


@pytest.fixture
def links():
    return FakeLinks()


async def make_user(uow, max_user_id: int, *, days_left: float | None = None, **fields):
    user = await uow.users.get_or_create(max_user_id, fields.pop("username", None))
    if days_left is not None:
        user.is_active = True
        user.subscription_end_date = utcnow() + timedelta(days=days_left)
    for name, value in fields.items():
        setattr(user, name, value)
    await uow.commit()
    return user
