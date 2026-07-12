"""
种子权重标记工具
=================
给 JSON 数据库中的每条 UID 记录添加种子权重体系，用于 BFS 优先级遍历。

新增字段：
  - seed_weight:     当前种子权重（默认 0），找到一个福瑞 +1
  - parent_seed:     父级种子 UID（谁发现的这条记录）
  - discovered_uids: 该种子发现的福瑞 UID 列表（关系网）

权重继承规则：
  种子 A（权重 W）遍历时找到一个福瑞 B → B 的初始权重 = W / 10
  优先遍历权重高的种子

最后可将未种过且未排除的 UID 写入 matched_pending.json 作为种子库。

用法：
  python main.py seed-weight          # 初始化权重字段
  python main.py seed-weight --reset  # 重置所有权重
  python main.py seed-weight --repair # 按 discovered_uids 去重并重算权重
"""

import os
import sys
from datetime import datetime
from typing import List, Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from bilibili.json_db import load_db, upsert_to_db, FileLock, _atomic_write, _load_raw


WEIGHT_FILES = [
    ("matched_done.json", settings.MATCHED_DONE_PATH),
    ("matched_pending.json", settings.MATCHED_PENDING_PATH),
    ("not_matched.json", settings.NOT_MATCHED_PATH),
    ("error.json", settings.ERROR_PATH),
]


def add_weight_fields(records: List[dict]) -> int:
    """给记录列表补充 seed_weight / parent_seed / discovered_uids 字段"""
    changed = 0
    for rec in records:
        if "seed_weight" not in rec or rec["seed_weight"] is None:
            rec["seed_weight"] = 0
            changed += 1
        if "parent_seed" not in rec:
            rec["parent_seed"] = None
            changed += 1
        if "discovered_uids" not in rec or rec["discovered_uids"] is None:
            rec["discovered_uids"] = []
            changed += 1
    return changed


def fix_file(filepath: str, name: str) -> tuple:
    """修复单个 JSON 文件，返回 (总条数, 修复条数)"""
    with FileLock(filepath + ".lock"):
        db = _load_raw(filepath)
        records = db.get("uids", [])
        fixed = add_weight_fields(records)
        if fixed:
            db["meta"]["total_count"] = len(records)
            db["meta"]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db["uids"] = records
            _atomic_write(filepath, db)
    return len(records), fixed


def _unique_uids(values) -> List[int]:
    """按出现顺序去重 UID，过滤空值和无法转成 int 的值。"""
    seen = set()
    result = []
    for value in values or []:
        try:
            uid = int(value)
        except (TypeError, ValueError):
            continue
        if not uid or uid in seen:
            continue
        seen.add(uid)
        result.append(uid)
    return result


def repair_weight_file(filepath: str) -> tuple:
    """按 discovered_uids 重算 seed_weight，返回 (记录数, 修改数)。"""
    with FileLock(filepath + ".lock"):
        db = _load_raw(filepath)
        records = db.get("uids", [])
        changed = 0

        for rec in records:
            discovered = _unique_uids(rec.get("discovered_uids"))
            new_weight = len(discovered)
            if rec.get("discovered_uids") != discovered:
                rec["discovered_uids"] = discovered
                changed += 1
            if rec.get("seed_weight") != new_weight:
                rec["seed_weight"] = new_weight
                changed += 1
            if "parent_seed" not in rec:
                rec["parent_seed"] = None
                changed += 1

        if changed:
            db["meta"]["total_count"] = len(records)
            db["meta"]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db["uids"] = records
            _atomic_write(filepath, db)

    return len(records), changed


def repair_weights():
    print(f"\n{'=' * 50}")
    print("  权重修复：seed_weight = 去重后的 discovered_uids 数量")
    print(f"{'=' * 50}")

    total_records = 0
    total_changed = 0
    for name, path in WEIGHT_FILES:
        if not os.path.exists(path):
            print(f"  {name}: 不存在，跳过")
            continue
        count, changed = repair_weight_file(path)
        total_records += count
        total_changed += changed
        print(f"  {name}: {count} 条，修复 {changed} 处")

    print(f"  共检查 {total_records} 条，修复 {total_changed} 处")


def rebuild_seed_pool():
    """
    重建 matched_pending.json。

    规则：
      - matched_done.json 保留为已种过历史，不清空。
      - matched_pending.json 保留未种过种子。
      - not_matched.json / error.json 永远不进入种子库。
      - 如果 pending 中混入了 done / not_matched / error 的 UID，移除。
    """
    print(f"\n{'=' * 50}")
    print("  重建种子库：清理 pending，保留 done 历史")
    print(f"{'=' * 50}")

    # 读取两个源
    done_records = load_db(settings.MATCHED_DONE_PATH)
    pending_records = load_db(settings.MATCHED_PENDING_PATH)
    skip_uids = {r.get("uid") for r in load_db(settings.NOT_MATCHED_PATH) if r.get("uid")}
    error_uids = {r.get("uid") for r in load_db(settings.ERROR_PATH) if r.get("uid")}

    print(f"  matched_done.json:    {len(done_records)} 条")
    print(f"  matched_pending.json: {len(pending_records)} 条")

    # 清理 pending：去重，并排除已种过/排除/报错 UID。
    done_uids = {r.get("uid") for r in done_records if r.get("uid")}
    blocked_uids = done_uids | skip_uids | error_uids
    merged: Dict[int, dict] = {}
    removed = 0
    for rec in pending_records:
        uid = rec.get("uid")
        if not uid:
            continue
        if uid in blocked_uids:
            removed += 1
            continue
        if uid in merged:
            # 保留权重更高的
            if (rec.get("seed_weight") or 0) > (merged[uid].get("seed_weight") or 0):
                merged[uid] = rec
                removed += 1
            else:
                removed += 1
        else:
            merged[uid] = rec

    merged_list = list(merged.values())
    print(f"  清理后 pending: {len(merged_list)} 条，移除 {removed} 条")

    # 按权重降序排列（权重高的排前面）
    merged_list.sort(key=lambda r: -(r.get("seed_weight") or 0))

    # 写入 matched_pending.json
    upsert_to_db(settings.MATCHED_PENDING_PATH, merged_list,
                 description="种子库（合并 done+pending，按权重降序）")
    print(f"  ✓ 已写入 matched_pending.json: {len(merged_list)} 条")
    print(f"  ✓ matched_done.json 保留不动: {len(done_records)} 条")


def main():
    reset = "--reset" in sys.argv
    repair = "--repair" in sys.argv

    print("=" * 60)
    print("  种子权重标记工具")
    print("=" * 60)
    if repair:
        print("  模式: 按 discovered_uids 修复权重")
    elif reset:
        print("  模式: 重置所有权重为 0")
    else:
        print("  模式: 初始化权重字段")
    print()

    total_records = 0
    total_fixed = 0

    for name, path in WEIGHT_FILES:
        if not os.path.exists(path):
            print(f"  {name}: 不存在，跳过")
            continue
        n_records, n_fixed = fix_file(path, name)
        total_records += n_records
        total_fixed += n_fixed
        print(f"  {name}: {n_records} 条，{'重置' if reset else '初始化'} {n_fixed} 条")

    if reset:
        # 重置所有权重为 0
        for name, path in WEIGHT_FILES:
            if not os.path.exists(path):
                continue
            with FileLock(path + ".lock"):
                db = _load_raw(path)
                for rec in db.get("uids", []):
                    rec["seed_weight"] = 0
                    rec["parent_seed"] = None
                    rec["discovered_uids"] = []
                db["meta"]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _atomic_write(path, db)
            print(f"  → {name}: 权重已重置")

    print(f"\n  共检查 {total_records} 条，处理 {total_fixed} 条")

    if repair:
        repair_weights()
        print(f"\n{'=' * 60}")
        print(f"  权重修复完成！")
        print(f"{'=' * 60}")
        return

    # 重建种子库
    print()
    rebuild_seed_pool()

    print(f"\n{'=' * 60}")
    print(f"  完成！")
    print(f"  后续使用: python main.py crawl  # BFS 会自动优先高权重种子")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
