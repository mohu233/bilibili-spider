"""
JSON 数据库读写工具
====================
管理三个 UID 数据库的读写：
  - matched_done.json     — 已匹配福瑞 + 已遍历关系
  - matched_pending.json  — 已匹配福瑞 + 待遍历关系
  - not_matched.json      — 不符合条件（跳过库）

安全特性：
  - 文件锁（防止并发写损坏）
  - .bak 自动备份（写前备份，最多保留 3 份）
  - 原子写入（先写 .tmp 再 rename）
"""

import os
import json
import shutil
import time
import errno
from datetime import datetime
from typing import List, Dict, Optional, Set

from config import settings

# 最大备份保留数
MAX_BACKUPS = 3


# ============================================================
# 文件锁（基于跨进程文件锁）
# ============================================================

class FileLock:
    """简易文件锁，防止多进程/线程并发写同一文件。"""

    def __init__(self, lock_path: str, timeout: float = 30):
        self.lock_path = lock_path
        self.timeout = timeout
        self._fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                # 以独占创建模式打开，保证原子性
                self._fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
                if time.time() > deadline:
                    # 锁超时，强制删除（可能上次崩溃遗留）
                    try:
                        os.remove(self.lock_path)
                    except OSError:
                        pass
                    continue
                time.sleep(0.1)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fd is not None:
            os.close(self._fd)
        try:
            os.remove(self.lock_path)
        except OSError:
            pass


# ============================================================
# 备份
# ============================================================

def _rotate_backup(filepath: str):
    """轮转备份：将当前文件备份为 .bak / .bak2 / .bak3"""
    if not os.path.exists(filepath):
        return
    # 删最旧的
    oldest = filepath + f".bak{MAX_BACKUPS}"
    if os.path.exists(oldest):
        try:
            os.remove(oldest)
        except OSError:
            pass
    # 依次后移
    for i in range(MAX_BACKUPS - 1, 0, -1):
        old_path = filepath + f".bak{i}" if i > 1 else filepath + ".bak"
        new_path = filepath + f".bak{i + 1}"
        if os.path.exists(old_path):
            try:
                shutil.move(old_path, new_path)
            except OSError:
                pass
    # 当前 → .bak
    try:
        shutil.copy2(filepath, filepath + ".bak")
    except OSError:
        pass


# ============================================================
# 原子写入
# ============================================================

def _atomic_write(filepath: str, data: dict):
    """原子写入：先写 .tmp → 备份原文件 → rename 覆盖"""
    tmp_path = filepath + ".tmp"
    # 写临时文件
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # 备份原文件
    _rotate_backup(filepath)
    # 原子替换
    shutil.move(tmp_path, filepath)


def _load_raw(filepath: str) -> dict:
    """加载原始 JSON 文件，不存在返回默认结构"""
    if not os.path.exists(filepath):
        return {
            "meta": {
                "source_file": os.path.basename(filepath),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_count": 0,
            },
            "uids": [],
        }
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {
            "meta": {
                "source_file": os.path.basename(filepath),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_count": 0,
                "note": "文件损坏已重建",
            },
            "uids": [],
        }


# ============================================================
# 公开 API
# ============================================================

def load_db(filepath: str) -> List[dict]:
    """加载 JSON 数据库，返回 uid 列表"""
    db = _load_raw(filepath)
    return db.get("uids", [])


def load_uid_set(filepath: str) -> Set[int]:
    """只返回 UID 的 set（快速查重用）"""
    return {r["uid"] for r in load_db(filepath) if r.get("uid")}


def load_uid_map(filepath: str) -> Dict[int, dict]:
    """返回 {uid: record} 映射"""
    return {r["uid"]: r for r in load_db(filepath) if r.get("uid")}


def upsert_to_db(filepath: str, records: List[dict], description: str = "") -> int:
    """
    向 JSON 数据库写入/更新记录（upsert 语义）。
    如果 UID 已存在则更新字段，不存在则追加。
    自动填充 update_time 字段。
    带文件锁 + 原子写入 + 自动备份。

    Args:
        filepath: JSON 文件路径
        records: 要写入的记录列表
        description: 首次创建时的描述

    Returns:
        更新+新增的总条数
    """
    if not records:
        return 0

    lock_path = filepath + ".lock"
    with FileLock(lock_path):
        db = _load_raw(filepath)

        # 记录元描述
        if description and "description" not in db["meta"]:
            db["meta"]["description"] = description

        # 建立 uid → index 映射
        existing_map: Dict[int, int] = {}
        for i, r in enumerate(db["uids"]):
            uid = r.get("uid")
            if uid:
                existing_map[uid] = i

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        changed = 0

        for rec in records:
            uid = rec.get("uid")
            if not uid:
                continue

            # 强制设置 update_time
            rec["update_time"] = now_str

            if uid in existing_map:
                # 更新已有记录：覆盖非 None 的字段
                idx = existing_map[uid]
                for k, v in rec.items():
                    if v is not None:
                        db["uids"][idx][k] = v
                changed += 1
            else:
                # 新增记录
                db["uids"].append(rec)
                existing_map[uid] = len(db["uids"]) - 1
                changed += 1

        if changed == 0:
            return 0

        db["meta"]["total_count"] = len(db["uids"])
        db["meta"]["updated_at"] = now_str

        # 原子写入（带锁保护）
        _atomic_write(filepath, db)

    return changed


def remove_from_db(filepath: str, uids: List[int]) -> int:
    """
    从 JSON 数据库中移除指定 UID。
    带文件锁 + 原子写入 + 自动备份。

    Args:
        filepath: JSON 文件路径
        uids: 要移除的 UID 列表

    Returns:
        实际移除条数
    """
    if not uids:
        return 0

    uid_set = set(uids)
    lock_path = filepath + ".lock"
    with FileLock(lock_path):
        db = _load_raw(filepath)
        before = len(db["uids"])
        db["uids"] = [r for r in db["uids"] if r.get("uid") not in uid_set]
        removed = before - len(db["uids"])

        if removed == 0:
            return 0

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db["meta"]["total_count"] = len(db["uids"])
        db["meta"]["updated_at"] = now_str
        _atomic_write(filepath, db)

    return removed
