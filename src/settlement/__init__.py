"""赛事跨业消费清算后端。

身份与版本语义延续 ``contracts/domain.schema.json`` 中的事件信封：
event_id / event_type / aggregate_type / aggregate_id / occurred_at /
version / summary，业务数据放在 ``payload`` 中。
"""

from .app import App

__all__ = ["App"]
