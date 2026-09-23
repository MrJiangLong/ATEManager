"""开发包/本地台架专用最小配置：仅提供报告取数所需的 FACTORY_DB_*（环境变量或 .env 注入）。"""

import os


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Settings:
    APP_VERSION = "devkit"
    FACTORY_DB_HOST = os.getenv("FACTORY_DB_HOST", "")
    FACTORY_DB_PORT = _as_int(os.getenv("FACTORY_DB_PORT"), 5432)
    FACTORY_DB_USER = os.getenv("FACTORY_DB_USER", "")
    FACTORY_DB_PASSWORD = os.getenv("FACTORY_DB_PASSWORD", "")


settings = Settings()
