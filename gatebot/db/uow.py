from sqlalchemy.ext.asyncio import AsyncSession

from db.repositories import BotStateRepository, PaymentRepository, UserRepository


class UnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.payments = PaymentRepository(session)
        self.state = BotStateRepository(session)

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
