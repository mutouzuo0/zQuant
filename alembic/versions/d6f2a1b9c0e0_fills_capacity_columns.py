"""fills capacity columns (8.4.4 / M2-P4)

Revision ID: d6f2a1b9c0e0
Revises: 140c83845744
Create Date: 2026-08-16 16:00:00.000000

为 report.html 容量证据（8.4.4）: fills 增列 bar_volume / participation_rate
（BrokerSim 成交时记录; 设计 8.3.3 回写升版见 D4）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d6f2a1b9c0e0"
down_revision: Union[str, Sequence[str], None] = "140c83845744"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("fills", sa.Column("bar_volume", sa.Float(), nullable=False, server_default="0.0"))
    op.add_column(
        "fills", sa.Column("participation_rate", sa.Float(), nullable=False, server_default="0.0")
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("fills", "participation_rate")
    op.drop_column("fills", "bar_volume")
