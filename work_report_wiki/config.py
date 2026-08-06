# -*- coding: utf-8 -*-
"""配置加载：读取 config/aiwiki-v3.yaml，支持 ${ENV_VAR} 插值。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml

# 加载 .env（密钥集中放此处，已被根 .gitignore 的 .* 规则忽略，不进 git）
try:
    from dotenv import load_dotenv

    _DOTENV_PATH = Path(__file__).resolve().parent / ".env"
    if _DOTENV_PATH.exists():
        load_dotenv(_DOTENV_PATH)
except Exception:  # pragma: no cover - python-dotenv 为可选依赖
    pass

_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "aiwiki-v3.yaml"
_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _interpolate(obj: Any) -> Any:
    """递归把字符串中的 ${VAR:-default} 替换为环境变量值。"""
    if isinstance(obj, str):
        def repl(m: "re.Match[str]") -> str:
            token = m.group(1)
            if ":-" in token:
                name, default = token.split(":-", 1)
            else:
                name, default = token, ""
            return os.environ.get(name, default)

        return _ENV_PATTERN.sub(repl, obj)
    if isinstance(obj, dict):
        return {k: _interpolate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate(v) for v in obj]
    return obj


class Settings:
    """配置访问器。优先读 YAML 显式字段。"""

    def __init__(self, cfg: Dict[str, Any]):
        self._cfg = cfg

    def section(self, name: str) -> Dict[str, Any]:
        return self._cfg.get(name, {}) or {}

    # ---- storage / mysql ----
    @property
    def mysql(self) -> Dict[str, Any]:
        return self.section("storage")["mysql"]

    @property
    def db_url(self) -> str:
        m = self.mysql
        pwd = f":{m['password']}" if m.get("password") else ""
        return (
            f"mysql+pymysql://{m['user']}{pwd}@{m['host']}:{m['port']}/{m['database']}"
            f"?charset={m.get('charset', 'utf8mb4')}"
        )

    @property
    def db_name(self) -> str:
        return self.mysql.get("database", "wiki_v3")

    # ---- vector_store / elasticsearch ----
    @property
    def es(self) -> Dict[str, Any]:
        return self.section("vector_store")["elasticsearch"]

    @property
    def es_host(self) -> str:
        return self.es.get("host", "127.0.0.1")

    @property
    def es_port(self) -> int:
        return int(self.es.get("port", 9200))

    @property
    def es_scheme(self) -> str:
        return self.es.get("scheme", "http")

    @property
    def es_user(self) -> str:
        return self.es.get("user", "")

    @property
    def es_password(self) -> str:
        return self.es.get("password", "")

    @property
    def es_index(self) -> str:
        return self.es.get("index", "wiki_chunks")

    @property
    def es_embed_dims(self) -> int:
        return int(self.es.get("embed_dims", 1024))

    @property
    def es_analyzer(self) -> str:
        return self.es.get("analyzer", "standard")

    # ---- embedding ----
    @property
    def embedding(self) -> Dict[str, Any]:
        return self.section("embedding")

    @property
    def embedding_model(self) -> str:
        return self.embedding.get("model", "text-embedding-v4")

    @property
    def embedding_api_key(self) -> str:
        return self.embedding.get("api_key", "")

    @property
    def embedding_base_url(self) -> str:
        return self.embedding.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    # ---- ai ----
    @property
    def ai(self) -> Dict[str, Any]:
        return self.section("ai")

    @property
    def ai_url(self) -> str:
        return self.ai.get("url", "")

    @property
    def ai_user_key(self) -> str:
        return self.ai.get("user_key", "")

    @property
    def ai_model_type(self) -> str:
        return self.ai.get("model_type", "qwen-max")

    @property
    def ai_temperature(self) -> float:
        return float(self.ai.get("temperature", 0.1))

    @property
    def ai_biz_code(self) -> str:
        return self.ai.get("biz_code", "aiwiki_v3")

    @property
    def ai_max_output_tokens(self) -> int:
        return int(self.ai.get("max_output_tokens", 2048))

    @property
    def ai_json_mode(self) -> int:
        return int(self.ai.get("json_mode", 1))

    @property
    def ai_top_p(self) -> float:
        return float(self.ai.get("top_p", 0.9))

    # ---- ingest ----
    @property
    def chunk_size(self) -> int:
        return int(self.section("ingest").get("chunk_size", 800))

    @property
    def chunk_overlap(self) -> int:
        return int(self.section("ingest").get("chunk_overlap", 120))

    # ---- qa ----
    @property
    def qa(self) -> Dict[str, Any]:
        return self.section("qa")

    @property
    def top_k(self) -> int:
        return int(self.qa.get("top_k", 6))

    @property
    def bm25_weight(self) -> float:
        return float(self.qa.get("bm25_weight", 0.3))

    @property
    def vector_weight(self) -> float:
        return float(self.qa.get("vector_weight", 0.7))

    # ---- grant ----
    @property
    def grant(self) -> Dict[str, Any]:
        return self.section("grant")

    @property
    def grant_actions(self) -> list:
        return self.grant.get("actions", ["ask", "read", "cite"])

    @property
    def acl_cache_ttl(self) -> int:
        return int(self.grant.get("acl_cache_ttl_seconds", 300))


def _load() -> Settings:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Settings(_interpolate(raw))


settings = _load()
