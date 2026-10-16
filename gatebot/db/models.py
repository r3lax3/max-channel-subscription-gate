from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, func

from core.utils import utcnow
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    max_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subscription_end_date: Mapped[datetime | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(default=False)
    expiring_notice_sent: Mapped[bool] = mapped_column(default=False, server_default="false")
    # Право на цену «старичка»; сбрасывается навсегда при разрыве подписки
    legacy_pricing: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())

    payments: Mapped[list["Payment"]] = relationship(back_populates="user")

    def __repr__(self) -> str:
        return f"<User(id={self.id}, max={self.max_user_id}, active={self.is_active})>"


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[int] = mapped_column()
    days: Mapped[int] = mapped_column(default=30, server_default="30")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="payments")

    def __repr__(self) -> str:
        return f"<Payment(id={self.id}, user={self.user_id}, status={self.status})>"


class BotState(Base):
    """Служебные значения бота (например, marker long polling)."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
