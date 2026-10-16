import os
import subprocess
import sys
from pathlib import Path

GATEBOT = Path(__file__).resolve().parent.parent / "gatebot"


def test_migrations_match_models(tmp_path):
    """alembic upgrade head на чистой БД даёт схему без расхождений с моделями."""
    env = {**os.environ, "database_url": f"sqlite+aiosqlite:///{tmp_path}/m.db"}
    run = lambda *args: subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=GATEBOT, env=env,
        capture_output=True, text=True,
    )
    upgrade = run("upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr
    check = run("check")
    assert check.returncode == 0, check.stdout + check.stderr
