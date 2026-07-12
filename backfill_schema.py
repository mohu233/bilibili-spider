"""
字段补齐工具
=============
扫描三个 JSON 数据库，补全所有记录的统一字段格式。

确保每条记录都有统一字段，缺失的填 null / 默认值：
  uid, name, hit, avatar_url, intro, fan_count, follow_count,
  seed_weight, parent_seed, discovered_uids, update_time

用法：
  python backfill_schema.py
"""

import os
import sys
import json
import shutil
from datetime import datetime
from typing import List, Dict

from config import settings
from bilibili.json_db import load_db, FileLock, _atomic_write, _load_raw

# 标准字段列表
SCHEMA_FIELDS = [
    "uid", "name", "hit", "avatar_url", "intro", "fan_count", "follow_count",
    "seed_weight", "parent_seed", "discovered_uids", "update_time",
]

DEFAULTS = {
    "seed_weight": 0,
    "parent_seed": None,
    "discovered_uids": [],
}


def fix_file(filepath: str, name_label: str) -> int:
    """
    修复单个 JSON 文件，补齐缺失字段。

    Returns:
        修复的记录数
    """
    with FileLock(filepath + ".lock"):
        db = _load_raw(filepath)
        records = db.get("uids", [])
        fixed = 0

        for i, rec in enumerate(records):
            changed = False
            for field in SCHEMA_FIELDS:
                if field not in rec:
                    default = DEFAULTS.get(field)
                    rec[field] = list(default) if isinstance(default, list) else default
                    changed = True
                elif field == "discovered_uids" and rec[field] is None:
                    rec[field] = []
                    changed = True
                elif rec[field] is None and field == "update_time":
                    # update_time 为 null 是正常的，不需处理
                    pass

            # 确保字段顺序一致，方便人工查看
            ordered = {"uid": rec.get("uid")}
            for field in SCHEMA_FIELDS[1:]:
                ordered[field] = rec.get(field)
            records[i] = ordered

            if changed:
                fixed += 1

        # 无论是否有修复，都做一次格式化保存（确保字段顺序一致）
        db["meta"]["total_count"] = len(records)
        db["meta"]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db["uids"] = records
        _atomic_write(filepath, db)

        return fixed


def main():
    print("=" * 60)
    print("  字段补齐工具 — 统一所有 JSON 为 8 字段格式")
    print("=" * 60)

    paths = [
        ("matched_done.json", settings.MATCHED_DONE_PATH),
        ("matched_pending.json", settings.MATCHED_PENDING_PATH),
        ("not_matched.json", settings.NOT_MATCHED_PATH),
        ("error.json", settings.ERROR_PATH),
    ]

    total_fixed = 0
    total_records = 0

    for name, path in paths:
        if not os.path.exists(path):
            print(f"\n{name}: 不存在，跳过")
            continue

        records = load_db(path)
        total_records += len(records)
        fixed = fix_file(path, name)
        total_fixed += fixed

        if fixed > 0:
            print(f"\n{name}: {len(records)} 条记录，修复 {fixed} 条")
        else:
            print(f"\n{name}: {len(records)} 条记录，格式正确 ✓")

    print(f"\n{'=' * 60}")
    print(f"完成！共检查 {total_records} 条记录，修复 {total_fixed} 条")
    if total_fixed > 0:
        print("提示：被空壳覆盖的原始数据无法恢复，建议重新 organize 或 update")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
