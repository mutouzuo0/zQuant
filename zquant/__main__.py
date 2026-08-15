"""允许 `python -m zquant` 调用 CLI。"""

from __future__ import annotations

from zquant.cli import app

if __name__ == "__main__":
    app()
