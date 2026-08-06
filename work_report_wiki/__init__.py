# -*- coding: utf-8 -*-
"""
核心能力：
  - 混合检索（kNN + BM25 + Python 端 RRF）
  - 基于 Claim 级证据组（DNF）的 Wiki 页面投影
  - 答案引擎（JSON Schema + 引用）
"""
from .config import settings

__all__ = ["settings"]
