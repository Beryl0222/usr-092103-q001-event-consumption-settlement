"""应用门面：把写侧服务、读侧视图与事件存储组装在一起。"""

from __future__ import annotations

from pathlib import Path

from .services import Service
from .store import EventStore
from .views import Principal, Views


class App:
    def __init__(self, store: EventStore | str | Path = ":memory:") -> None:
        if isinstance(store, EventStore):
            self.store = store
        else:
            self.store = EventStore(store)
        self.service = Service(self.store)
        self.views = Views(self.store)

    def close(self) -> None:
        self.store.close()

    # 便捷身份构造
    @staticmethod
    def principal(role: str, principal_id: str | None = None) -> Principal:
        return Principal(role, principal_id)
