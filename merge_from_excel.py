"""
数据合并工具：从 Excel 恢复基础数据，同时保留 BFS 新发现
=====================================================
安全地合并 Excel 数据和当前 JSON 数据，不会丢失任何记录。

合并规则：
  1. 以 Excel 的 36796 条 UID 为基础骨架
  2. 当前 JSON 中的 avatar_url / fan_count / follow_count / update_time 覆盖到对应 UID 上
  3. 当前 JSON 中不在 Excel 里的 UID（BFS 新发现的）追加到对应文件
  4. not_matched.json / error.json 完全保留不动

用法：
  python merge_from_excel.py
"""

import os
import sys
import json
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import pandas as pd
except ImportError:
    print("需要 pandas: pip install pandas")
    sys.exit(1)

from config import settings
from bilibili.json_db import load_db, upsert_to_db, FileLock, _atomic_write, _load_raw


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        settings.EXCEL_FILE,
        os.path.join(script_dir, "统计3.6w.xlsx"),
        os.path.join(script_dir, "bilibili_up_export.xlsx"),
    ]
    excel_path = next((p for p in candidates if os.path.exists(p)), candidates[0])
    out_dir = settings.DATA_DIR

    if not os.path.exists(excel_path):
        print(f"[错误] 找不到 Excel: {excel_path}")
        return

    os.makedirs(out_dir, exist_ok=True)

    print("=" * 60)
    print("  数据合并工具 — 安全合并 Excel + 当前 JSON")
    print("=" * 60)

    # ===== Step 1: 读取 Excel =====
    print("\n[Step 1/4] 读取 Excel...")
    df = pd.read_excel(excel_path, dtype=str)
    col_map = {str(c).strip().lower(): c for c in df.columns}
    print(f"  Excel 原始列: {list(col_map.keys())}")

    # 定位列
    uid_col = None
    for key in ["uid", "mid", "用户id", "up主id"]:
        if key in col_map:
            uid_col = col_map[key]
            break
    if uid_col is None:
        for k, v in col_map.items():
            if "id" in k:
                uid_col = v
                break
    if uid_col is None:
        print("[错误] 找不到 UID 列")
        return

    name_col = col_map.get("name") or col_map.get("昵称")
    hit_col = col_map.get("hit") or col_map.get("命中规则") or col_map.get("命中")
    fan_col = col_map.get("fan_count") or col_map.get("粉丝数") or col_map.get("粉丝")
    follow_col = col_map.get("follow_count") or col_map.get("关注数") or col_map.get("关注")
    avatar_col = col_map.get("avatar_url") or col_map.get("头像")
    intro_col = col_map.get("intro") or col_map.get("简介")

    print(f"  UID列: {uid_col}")
    print(f"  昵称列: {name_col}")
    print(f"  命中列: {hit_col}")
    print(f"  粉丝列: {fan_col}")
    print(f"  关注列: {follow_col}")

    # 清理 UID
    df[uid_col] = pd.to_numeric(df[uid_col], errors="coerce")
    df = df.dropna(subset=[uid_col])
    df[uid_col] = df[uid_col].astype(int)
    total_excel = len(df)
    print(f"  有效 Excel UID: {total_excel}")

    # 构建 Excel 基础记录
    excel_uids = df[uid_col].tolist()
    excel_names = df[name_col].tolist() if name_col else [None] * total_excel
    excel_hits = df[hit_col].tolist() if hit_col else [None] * total_excel
    excel_fans = df[fan_col].tolist() if fan_col else [None] * total_excel
    excel_follows = df[follow_col].tolist() if follow_col else [None] * total_excel
    excel_avatars = df[avatar_col].tolist() if avatar_col else [None] * total_excel
    excel_intros = df[intro_col].tolist() if intro_col else [None] * total_excel

    excel_base: dict = {}  # uid → record
    for i in range(total_excel):
        uid = int(excel_uids[i])
        name_val = excel_names[i] if excel_names and i < len(excel_names) else None
        hit_val = excel_hits[i] if excel_hits and i < len(excel_hits) else None
        fan_val = excel_fans[i] if excel_fans and i < len(excel_fans) else None
        follow_val = excel_follows[i] if excel_follows and i < len(excel_follows) else None
        avatar_val = excel_avatars[i] if excel_avatars and i < len(excel_avatars) else None
        intro_val = excel_intros[i] if excel_intros and i < len(excel_intros) else None

        rec = {"uid": uid}
        rec["name"] = str(name_val).strip() if (name_val and not pd.isna(name_val)) else None
        raw_hit = str(hit_val).strip() if (hit_val and not pd.isna(hit_val)) else None
        rec["hit"] = raw_hit if raw_hit else None
        rec["avatar_url"] = str(avatar_val).strip() if (avatar_val and not pd.isna(avatar_val)) else None
        rec["intro"] = str(intro_val).strip() if (intro_val and not pd.isna(intro_val)) else None

        fan_count = None
        if fan_val and not pd.isna(fan_val):
            try:
                fan_count = int(float(str(fan_val).replace(",", "")))
            except (ValueError, TypeError):
                pass
        rec["fan_count"] = fan_count

        follow_count = None
        if follow_val and not pd.isna(follow_val):
            try:
                follow_count = int(float(str(follow_val).replace(",", "")))
            except (ValueError, TypeError):
                pass
        rec["follow_count"] = follow_count

        rec["update_time"] = None
        excel_base[uid] = rec

    # ===== Step 2: 读取当前 JSON =====
    print("\n[Step 2/4] 读取当前 JSON 数据库...")
    json_files = {
        "matched_done.json": settings.MATCHED_DONE_PATH,
        "matched_pending.json": settings.MATCHED_PENDING_PATH,
    }

    current_data: dict = {}  # uid → record（所有当前 JSON 中的记录）
    current_source: dict = {}  # uid → 文件名
    for name, path in json_files.items():
        records = load_db(path)
        for rec in records:
            uid = rec.get("uid")
            if uid:
                current_data[uid] = rec
                current_source[uid] = name
        print(f"  {name}: {len(records)} 条")

    # ===== Step 3: 合并 =====
    print("\n[Step 3/4] 合并数据...")
    print(f"  Excel 基础: {len(excel_base)} 条")
    print(f"  当前 JSON:  {len(current_data)} 条")

    # 3a: 对于 Excel 中已有的 UID，叠加上当前 JSON 中非空的字段
    merged_count = 0
    for uid, excel_rec in excel_base.items():
        if uid in current_data:
            cur_rec = current_data[uid]
            for field in ["avatar_url", "fan_count", "follow_count", "intro", "hit", "name"]:
                cur_val = cur_rec.get(field)
                if cur_val is not None:
                    excel_rec[field] = cur_val
            # update_time 如果有，也带过来
            cur_time = cur_rec.get("update_time")
            if cur_time:
                excel_rec["update_time"] = cur_time
            merged_count += 1
    print(f"  已叠加更新: {merged_count} 条")

    # 3b: 当前 JSON 中不在 Excel 里的 UID（BFS 新发现），单独列表
    new_uids = {}
    for uid, rec in current_data.items():
        if uid not in excel_base:
            src = current_source.get(uid, "unknown")
            if src not in new_uids:
                new_uids[src] = []
            new_uids[src].append(rec)
    for src, lst in new_uids.items():
        print(f"  BFS 新发现（不在 Excel 中）: {len(lst)} 条 → {src}")

    # 3c: 按粉丝/关注数分 done/pending
    done_records = []
    pending_records = []
    for uid, rec in excel_base.items():
        if rec.get("fan_count") is not None or rec.get("follow_count") is not None:
            done_records.append(rec)
        else:
            pending_records.append(rec)

    # 加上 BFS 新发现的
    for src, lst in new_uids.items():
        for rec in lst:
            if rec.get("fan_count") is not None or rec.get("follow_count") is not None:
                done_records.append(rec)
            else:
                pending_records.append(rec)

    print(f"\n  合并结果:")
    print(f"    matched_done.json:    {len(done_records)} 条")
    print(f"    matched_pending.json: {len(pending_records)} 条")

    # ===== Step 4: 写回 =====
    print("\n[Step 4/4] 写回 JSON 文件...")

    # not_matched.json 和 error.json 不动
    unchanged = []
    for name, path in [("not_matched.json", settings.NOT_MATCHED_PATH),
                        ("error.json", settings.ERROR_PATH)]:
        if os.path.exists(path):
            records = load_db(path)
            unchanged.append((name, len(records)))
            print(f"  {name}: 保留不动 ({len(records)} 条)")

    # 写 matched_done
    n = upsert_to_db(settings.MATCHED_DONE_PATH, done_records,
                     description="已匹配福瑞且已有粉丝/关注数据（BFS种子+去重库）")
    print(f"  matched_done.json: 写入 {len(done_records)} 条")

    # 写 matched_pending
    n = upsert_to_db(settings.MATCHED_PENDING_PATH, pending_records,
                     description="已匹配福瑞但暂无粉丝/关注数据（待BFS遍历）")
    print(f"  matched_pending.json: 写入 {len(pending_records)} 条")

    # ===== 报告 =====
    print(f"\n{'=' * 60}")
    print(f"  合并完成！")
    print(f"  matched_done.json:    {len(done_records)} 条")
    print(f"  matched_pending.json: {len(pending_records)} 条")
    for name, cnt in unchanged:
        print(f"  {name}: {cnt} 条（保留不动）")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
