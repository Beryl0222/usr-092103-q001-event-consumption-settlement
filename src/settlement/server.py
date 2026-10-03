"""HTTP 服务启动入口。

    python3 -m src.settlement.server --db ./settlement.db --port 8080
不传 --db 时使用内存库（进程退出即清空，仅适合联调演示）。
"""

from __future__ import annotations

import argparse

from .app import App
from .httpapi import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="赛事跨业消费清算后端")
    parser.add_argument("--db", default=":memory:", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    app = App(args.db)
    try:
        serve(app, args.host, args.port)
    finally:
        app.close()


if __name__ == "__main__":
    main()
