from __future__ import annotations
import json, os
from pathlib import Path

class ResolveError(Exception): pass
class DocResolver:
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
    def resolve(self, doc_id):
        if not isinstance(doc_id,str) or not doc_id or len(doc_id)>512 or doc_id.startswith(('/', '\\')) or '..' in Path(doc_id).parts: raise ResolveError('invalid doc_id')
        rel=self.index.get(doc_id)
        if not isinstance(rel,str): raise KeyError(doc_id)
        p=Path(rel)
        if p.is_absolute() or '..' in p.parts: raise ResolveError('unsafe path')
        candidates=[]
        for root in self.roots:
            q=(root/p).resolve()
            if q==root or root not in q.parents: continue
            candidates.append(q)
        if len(candidates)!=1: raise ResolveError('path outside configured roots')
        try:
            if candidates[0].stat().st_size>self.max_bytes: raise ResolveError('document too large')
            return candidates[0].read_text(encoding='utf-8')
        except FileNotFoundError: raise KeyError(doc_id)
        except (OSError,UnicodeError) as e: raise ResolveError('document unreadable') from e
