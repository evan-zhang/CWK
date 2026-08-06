# -*- coding: utf-8 -*-
"""文本切分（langchain RecursiveCharacterTextSplitter，中文友好分隔符）。"""
from __future__ import annotations

from typing import List

from langchain_text_splitters import RecursiveCharacterTextSplitter

from ..config import settings


def make_splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", ".", " ", ""],
        keep_separator=True,
    )


def chunk_text(text: str) -> List[str]:
    return make_splitter().split_text(text)
