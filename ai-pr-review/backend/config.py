"""应用配置模块 —— 从 .env 文件与环境变量读取所有配置项。

使用方式:
    from backend.config import get_settings
    settings = get_settings()
    print(settings.deepseek_api_key)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# 加载 .env 文件（显式指定路径，不依赖 CWD）
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)


# ---------------------------------------------------------------------------
# 自定义异常
# ---------------------------------------------------------------------------
class SettingsError(Exception):
    """配置错误，附带人类可读的修复提示。"""
    pass


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------
def _get_str(key: str, *, default: str = "", required: bool = False) -> str:
    """从环境变量读取字符串值。"""
    value = os.getenv(key)
    if value is None:
        if required:
            raise SettingsError(
                f"缺少必需的环境变量: {key}\n"
                f"请在 {_ENV_PATH} 文件中设置该值，"
                f"或通过 export {key}=... 导出。\n"
                f"可参考 .env.example 文件了解需要哪些配置。"
            )
        return default
    return value


def _get_int(key: str, *, default: int) -> int:
    """从环境变量读取整数值（自动做类型转换与校验）。"""
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SettingsError(
            f"环境变量 {key} 的值 '{raw}' 不是有效的整数，请修正 .env 文件。"
        )


# ---------------------------------------------------------------------------
# 配置数据类
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    """应用全局配置（不可变，单例）。"""

    # --- DeepSeek ---
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str

    # --- GitHub ---
    github_token: str

    # --- 服务 ---
    backend_url: str

    # --- 上下文限制 ---
    max_files: int
    max_file_lines: int
    max_input_tokens: int

    # --- HTTP ---
    request_timeout_sec: int


# ---------------------------------------------------------------------------
# 单例工厂
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回全局唯一的 Settings 实例。

    首次调用时从环境变量加载并校验；后续调用直接返回缓存结果。
    """
    return Settings(
        deepseek_api_key=_get_str("DEEPSEEK_API_KEY", required=True),
        deepseek_base_url=_get_str(
            "DEEPSEEK_BASE_URL", default="https://api.deepseek.com"
        ),
        deepseek_model=_get_str("DEEPSEEK_MODEL", default="deepseek-v4-pro"),
        github_token=_get_str("GITHUB_TOKEN", default=""),
        backend_url=_get_str("BACKEND_URL", default="http://localhost:8000"),
        max_files=_get_int("MAX_FILES", default=80),
        max_file_lines=_get_int("MAX_FILE_LINES", default=1500),
        max_input_tokens=_get_int("MAX_INPUT_TOKENS", default=300000),
        request_timeout_sec=_get_int("REQUEST_TIMEOUT_SEC", default=180),
    )
