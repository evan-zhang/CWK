# -*- coding: utf-8 -*-
"""Wiki 文件夹树

设计原则：
  - 文件夹仅用于导航/组织，**不是安全边界**；Wiki 安全性仍由 Claim 级
    证据组授权判定（wiki权限控制.md）。因此文件夹本身不携带 Grant。
  - path 物化存全路径（/root/child/...），depth 冗余，便于排序与范围查询。
  - 路径/深度计算为纯函数（无 IO），便于单测，不依赖数据库。

DB 辅助函数依赖 db.py；纯逻辑（build_path / compute_depth / normalize_name）
可在无基础设施环境下测试。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .. import db


@dataclass
class WikiFolder:
    id: int
    emp_id: int
    parent_id: int
    name: str
    path: str
    depth: int
    sort_order: int = 0
    page_count: int = 0


def normalize_name(name: str) -> str:
    """清洗文件夹名：去首尾空格，禁止出现路径分隔符，折叠空白。"""
    name = re.sub(r"\s+", " ", (name or "").strip())
    name = name.replace("/", "_").replace("\\", "_")
    return name[:255]


def build_path(parent_path: str, name: str) -> str:
    """由父路径与名称生成物化全路径，保证以 '/' 开头、不含重复斜杠。"""
    parent = (parent_path or "/").rstrip("/")
    return f"{parent}/{name}"


def compute_depth(path: str) -> int:
    """从物化路径推断层级深度（根 '/' -> 0）。"""
    return max(0, path.rstrip("/").count("/"))


class WikiFolderStore:
    """wiki_folders 表的读写辅助（依赖 db.py）。"""

    def __init__(self, emp_id: int):
        self.emp_id = emp_id

    def create_folder(
        self, name: str, parent_id: int = 0, sort_order: int = 0,
    ) -> WikiFolder:
        name = normalize_name(name)
        if not name:
            raise ValueError("folder name required")
        parent = self.get_folder(parent_id) if parent_id else None
        parent_path = parent.path if parent else "/"
        path = build_path(parent_path, name)
        depth = compute_depth(path)
        res = db.execute(
            """
            INSERT INTO wiki_folders
                (emp_id, parent_id, name, path, depth, sort_order, page_count)
            VALUES (:emp_id, :parent_id, :name, :path, :depth, :sort_order, 0)
            """,
            {
                "emp_id": self.emp_id,
                "parent_id": parent_id, "name": name, "path": path,
                "depth": depth, "sort_order": sort_order,
            },
        )
        fid = int(res.lastrowid)
        # 父节点计数 +1
        if parent_id:
            db.execute(
                "UPDATE wiki_folders SET page_count = page_count + 0, updated_at = NOW(3) "
                "WHERE id = :pid AND emp_id = :eid",
                {"pid": parent_id, "eid": self.emp_id},
            )
        return WikiFolder(
            id=fid, emp_id=self.emp_id, parent_id=parent_id,
            name=name, path=path, depth=depth, sort_order=sort_order, page_count=0,
        )

    def get_or_create(self, name: str, parent_id: int = 0) -> WikiFolder:
        row = db.query_all(
            """
            SELECT * FROM wiki_folders
            WHERE emp_id = :eid
              AND parent_id = :parent_id AND name = :name
            LIMIT 1
            """,
            {"eid": self.emp_id, "parent_id": parent_id, "name": normalize_name(name)},
        )
        if row:
            return _row_to_folder(row[0])
        return self.create_folder(name, parent_id=parent_id)

    def get_folder(self, folder_id: int) -> Optional[WikiFolder]:
        if not folder_id:
            return None
        row = db.query_all(
            "SELECT * FROM wiki_folders WHERE id = :fid AND emp_id = :eid LIMIT 1",
            {"fid": folder_id, "eid": self.emp_id},
        )
        return _row_to_folder(row[0]) if row else None

    def list_children(self, parent_id: int = 0) -> List[WikiFolder]:
        rows = db.query_all(
            """
            SELECT * FROM wiki_folders
            WHERE emp_id = :eid AND parent_id = :parent_id
            ORDER BY sort_order ASC, name ASC
            """,
            {"eid": self.emp_id, "parent_id": parent_id},
        )
        return [_row_to_folder(r) for r in rows]

    def render_tree(self, root_id: int = 0) -> str:
        """把文件夹树渲染成紧凑文本（每行一个 path），供分类规划 feed-forward。

        让后续分块在已有树上收敛、复用一致标签。
        """
        rows = db.query_all(
            """
            SELECT path FROM wiki_folders
            WHERE emp_id = :eid AND id <> :rid
            ORDER BY depth ASC, path ASC
            """,
            {"eid": self.emp_id, "rid": root_id},
        )
        return "\n".join(r["path"] for r in rows)

    def move(self, folder_id: int, new_parent_id: int) -> WikiFolder:
        """移动文件夹（含子树 path/depth 重建），并维护父节点 page_count。"""
        f = self.get_folder(folder_id)
        if not f:
            raise ValueError(f"folder {folder_id} not found")
        new_parent = self.get_folder(new_parent_id) if new_parent_id else None
        new_parent_path = new_parent.path if new_parent else "/"
        # 环检测：不能移动到自身子孙下
        if f.path == new_parent_path.rstrip("/") or \
           new_parent_path.rstrip("/").startswith(f.path.rstrip("/") + "/"):
            raise ValueError("folder cycle on move")
        new_path = build_path(new_parent_path, f.name)
        new_depth = compute_depth(new_path)
        old_parent = f.parent_id
        db.execute(
            "UPDATE wiki_folders SET parent_id = :np, path = :np_, depth = :nd, updated_at = NOW(3) "
            "WHERE id = :fid AND emp_id = :eid",
            {"np": new_parent_id, "np_": new_path, "nd": new_depth,
             "fid": folder_id, "eid": self.emp_id},
        )
        self._rebuild_subtree(f.path, new_path, new_depth)
        # page_count 维护：旧父 -1（若非根），新父 +1
        if old_parent:
            db.execute(
                "UPDATE wiki_folders SET page_count = GREATEST(0, page_count - 1), updated_at = NOW(3) "
                "WHERE id = :pid AND emp_id = :eid",
                {"pid": old_parent, "eid": self.emp_id},
            )
        if new_parent_id:
            db.execute(
                "UPDATE wiki_folders SET page_count = page_count + 1, updated_at = NOW(3) "
                "WHERE id = :pid AND emp_id = :eid",
                {"pid": new_parent_id, "eid": self.emp_id},
            )
        f.parent_id, f.path, f.depth = new_parent_id, new_path, new_depth
        return f

    def _rebuild_subtree(self, old_prefix: str, new_prefix: str, new_parent_depth: int) -> None:
        """把以 old_prefix 为前缀的所有子孙路径重写，并修正 depth。"""
        rows = db.query_all(
            "SELECT id, path FROM wiki_folders WHERE emp_id = :eid AND path LIKE :like",
            {"eid": self.emp_id, "like": old_prefix.rstrip("/") + "/%"},
        )
        for r in rows:
            rel = r["path"][len(old_prefix.rstrip("/")):]
            np_ = new_prefix.rstrip("/") + rel
            db.execute(
                "UPDATE wiki_folders SET path = :np, depth = :nd, updated_at = NOW(3) "
                "WHERE id = :rid AND emp_id = :eid",
                {"np": np_, "nd": new_parent_depth + np_.rstrip("/").count("/"),
                 "rid": r["id"], "eid": self.emp_id},
            )

    def increment_page_count(self, folder_id: int, delta: int = 1) -> None:
        if not folder_id:
            return
        db.execute(
            "UPDATE wiki_folders SET page_count = GREATEST(0, page_count + :d), updated_at = NOW(3) "
            "WHERE id = :fid AND emp_id = :eid",
            {"d": delta, "fid": folder_id, "eid": self.emp_id},
        )


def _row_to_folder(row: dict) -> WikiFolder:
    return WikiFolder(
        id=row["id"], emp_id=row["emp_id"],
        parent_id=row["parent_id"], name=row["name"], path=row["path"],
        depth=row["depth"], sort_order=row.get("sort_order", 0),
        page_count=row.get("page_count", 0),
    )
