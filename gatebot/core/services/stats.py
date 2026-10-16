from dataclasses import dataclass

from db.uow import UnitOfWork


@dataclass
class StatsSnapshot:
    total_users: int
    active_subscribers: int
    inactive_users: int
    expiring_soon: int
    paid_count: int
    pending_count: int
    total_revenue: int
    revenue_30d: int


class StatsService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def collect(self) -> StatsSnapshot:
        total = await self.uow.users.count_total()
        active = await self.uow.users.count_active_subscribers()
        return StatsSnapshot(
            total_users=total,
            active_subscribers=active,
            inactive_users=total - active,
            expiring_soon=await self.uow.users.count_expiring_within(7),
            paid_count=await self.uow.payments.count_by_status("success"),
            pending_count=await self.uow.payments.count_by_status("pending"),
            total_revenue=await self.uow.payments.sum_revenue(),
            revenue_30d=await self.uow.payments.sum_revenue(days_back=30),
        )
