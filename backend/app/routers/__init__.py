"""路由装配：一个模块一个业务域。"""

from . import auth, clients, masters, metrics, products, records, repairs, routing, sessions, v1

__all__ = [
    "auth",
    "v1",
    "masters",
    "routing",
    "products",
    "records",
    "repairs",
    "clients",
    "sessions",
    "metrics",
]
