"""领域错误。

所有违反业务规则的情况都以领域异常表达，服务层不依赖 HTTP 状态码；
接口层再把错误映射为对外响应。
"""


class SettlementError(Exception):
    """领域错误基类。"""

    code = "DOMAIN_ERROR"


class NotFound(SettlementError):
    code = "NOT_FOUND"


class Conflict(SettlementError):
    """并发或重复写入冲突，例如重复核销、重复入账。"""

    code = "CONFLICT"


class DuplicateRedemption(Conflict):
    """同一二维码扫描或同一离线补传请求被再次提交。"""

    code = "DUPLICATE_REDEMPTION"


class WindowClosed(SettlementError):
    """核销时间不在权益有效窗口（赛前/赛中/赛后）内。"""

    code = "WINDOW_CLOSED"


class InsufficientBalance(Conflict):
    """权益剩余可用额度不足（离线双花在补传时被拒绝）。"""

    code = "INSUFFICIENT_BALANCE"


class IllegalState(SettlementError):
    """聚合当前状态不允许该操作，例如对已支付批次再次冻结。"""

    code = "ILLEGAL_STATE"


class ContractError(SettlementError):
    code = "CONTRACT_ERROR"


class AccessDenied(SettlementError):
    """访问者无权读取该数据（商户越权、游客越权）。"""

    code = "ACCESS_DENIED"
