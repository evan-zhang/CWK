from __future__ import annotations
import json, os
from pathlib import Path

class ResolveError(Exception): pass
class DocResolver:
    """Resolve snapshot-backed documents from configured local roots.

    This is a snapshot-backed read; NAS-backed SHA chain verification is a
    later migration item. The existing index, containment, size, and UTF-8
    checks still apply.
    """
    def __init__(self, roots=None, index_path=None, max_bytes=2_000_000):
        self.roots=tuple(Path(x).resolve() for x in (roots if roots is not None else os.getenv('RAG_SOURCE_ROOTS','').split(os.pathsep) if os.getenv('RAG_SOURCE_ROOTS') else []))
        self.index_path=Path(index_path or os.getenv('RAG_DOC_INDEX','')).resolve() if (index_path or os.getenv('RAG_DOC_INDEX')) else None
        self.max_bytes=max_bytes
        self.index=self._load()
    def _load(self):
        if not self.index_path: return {}
        if not self.index_path.is_file() or self.index_path.stat().st_size>self.max_bytes: raise ResolveError('invalid index')
        try: data=json.loads(self.index_path.read_text(encoding='utf-8'))
        except (OSError,UnicodeError,json.JSONDecodeError) as e: raise ResolveError('invalid index') from e
        return data if isinstance(data,dict) else (_ for _ in ()).throw(ResolveError('invalid index'))
    def bank_of(self, doc_id, default):
        """Return the bank that owns an indexed doc_id, or None when it is not indexed.

        Snapshot paths are laid out as ``<bank>/<file>``.  A single-segment path
        belongs to the deployment's default bank, matching a one-bank index.
        """
        rel=self.index.get(doc_id) if isinstance(doc_id,str) else None
        if not isinstance(rel,str): return None
        parts=Path(rel).parts
        return parts[0] if len(parts)>1 else default
    def resolve(self, doc_id):
        if not isinstance(doc_id,str) or not doc_id or len(doc_id)>512 or doc_id.startswith(('/', '\\')) or '..' in Path(doc_id).parts: raise ResolveError('invalid doc_id')
        rel=self.index.get(doc_id)
        if not isinstance(rel,str): raise KeyError(doc_id)
        p=Path(rel)
        if p.is_absolute() or '..' in p.parts: raise ResolveError('unsafe path')
        candidates=[]
        for root in self.roots:
            q=(root/p).resolve()
            # 安全检查不变：解析后仍必须落在某个配置根的内部，否则跳过。
            if q==root or root not in q.parents: continue
            # 配了多个根时，只有真实存在的那个才算候选。RT-070 的过渡期里
            # 「本地副本」和「NAS 挂载点」两个根并存，两种映射的相对路径形态不同，
            # 各自只在自己的根下存在；原来按「恰好落在一个根内」判定，会把这种情况
            # 一律判成越界，于是切换必须制造一段读不到原文的窗口。
            if q.exists(): candidates.append(q)
        if not candidates: raise KeyError(doc_id)
        if len(candidates)>1: raise ResolveError('document exists under more than one root')
        try:
            if candidates[0].stat().st_size>self.max_bytes: raise ResolveError('document too large')
            return candidates[0].read_text(encoding='utf-8')
        except FileNotFoundError: raise KeyError(doc_id)
        except (OSError,UnicodeError) as e: raise ResolveError('document unreadable') from e


    def read(self, doc_id, *, offset=0, length=65536):
        """Return a Unicode-character page from a resolved snapshot document."""
        text = self.resolve(doc_id)
        total_chars = len(text)
        if offset < 0 or offset > total_chars:
            raise ValueError("offset out of range")
        page = text[offset:offset + length]
        return {
            "doc_id": doc_id,
            "text": page,
            "offset": offset,
            "eof": offset + len(page) >= total_chars,
            "total_chars": total_chars,
        }
