"""
整理 UID 数据库脚本
====================
从 Excel 读取数据，整理为三个 JSON 库：

  1. matched_done.json     — 已匹配且已有粉丝/关注数据的 UID（可做种子）
  2. matched_pending.json  — 已匹配但尚无粉丝/关注数据的 UID（待BFS遍历）
  3. not_matched.json      — 不符合条件的 UID（标记跳过，防重复消耗）

用法：
  python organize_uids.py

依赖：pip install pandas openpyxl
"""

import os
import sys
import json
from datetime import datetime

try:
    import pandas as pd
except ImportError:
    print("需要安装 pandas: pip install pandas openpyxl")
    sys.exit(1)

# ============================================================
# 配置
# ============================================================

OUTPUT_DIR = "data"

# ============================================================
# 主逻辑
# ============================================================


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    from config import settings

    candidates = [
        settings.EXCEL_FILE,
        os.path.join(script_dir, "统计3.6w.xlsx"),
        os.path.join(script_dir, "bilibili_up_export.xlsx"),
    ]
    excel_path = next((p for p in candidates if os.path.exists(p)), candidates[0])
    excel_file = os.path.basename(excel_path)
    out_dir = os.path.join(script_dir, OUTPUT_DIR)

    if not os.path.exists(excel_path):
        print(f"[错误] 找不到文件: {excel_path}")
        xlsx_files = [f for f in os.listdir(script_dir) if f.endswith(".xlsx")]
        if xlsx_files:
            print(f"  当前目录下的 xlsx: {xlsx_files}")
            print(f"  请改名或修改 EXCEL_FILE 变量")
        return

    os.makedirs(out_dir, exist_ok=True)

    print(f"读取: {excel_file} ...")

    # === pandas 流式读取（比 openpyxl 逐行快 10~50 倍）===
    df = pd.read_excel(excel_path, dtype=str)
    print(f"表头: {list(df.columns)}")
    print(f"行数: {len(df)}")

    # 列名标准化（小写 + 去空格）
    col_map = {str(c).strip().lower(): c for c in df.columns}
    print(f"识别列: {list(col_map.keys())}")

    # === 定位 UID 列 ===
    uid_col = None
    for key in ["uid", "mid", "用户id", "up主id", "up主uid", "up_id"]:
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

    # === 定位其他列 ===
    hit_col = None
    for key in ["hit", "命中", "命中规则", "爬虫命中", "is_furry", "备注"]:
        if key in col_map:
            hit_col = col_map[key]
            break

    name_col = col_map.get("name") or col_map.get("昵称") or col_map.get("用户名")
    fan_col = col_map.get("fan_count") or col_map.get("粉丝数") or col_map.get("粉丝") or col_map.get("follower") or col_map.get("fans")
    follow_col = col_map.get("follow_count") or col_map.get("关注数") or col_map.get("关注") or col_map.get("following")
    avatar_col = col_map.get("avatar_url") or col_map.get("头像") or col_map.get("face")
    intro_col = col_map.get("intro") or col_map.get("简介") or col_map.get("sign")

    print(f"\n  UID列: {uid_col}")
    if name_col:   print(f"  昵称列: {name_col}")
    if hit_col:    print(f"  命中列: {hit_col}")
    if fan_col:    print(f"  粉丝列: {fan_col}")
    if follow_col: print(f"  关注列: {follow_col}")

    # === 清理数据 ===
    df[uid_col] = pd.to_numeric(df[uid_col], errors="coerce")
    df = df.dropna(subset=[uid_col])
    df[uid_col] = df[uid_col].astype(int)

    total = len(df)
    print(f"\n有效 UID: {total}")

    # === 打印前3行示例，看看实际数据长什么样 ===
    print(f"\n前3行数据示例:")
    for i in range(min(3, total)):
        row_data = {col: df[col].iloc[i] for col in df.columns}
        print(f"  {row_data}")

    # === hit 列分布统计（如果有） ===
    if hit_col:
        def _normalize(v):
            return str(v).strip().lower() if not pd.isna(v) else "(空)"
        hit_dist = df[hit_col].apply(_normalize)
        print("\nhit 值分布:")
        for val, cnt in hit_dist.value_counts().head(20).items():
            print(f"  [{val}] → {cnt}")

    # === 分类 ===
    # 说明：这个 Excel 是早期爬虫的结果，所有 UID 都是已确认的福瑞用户。
    # 不依赖 hit 列判断（早期没有统一标记），只需根据是否有粉丝/关注数据来分：
    #   - 有数据 → matched_done.json（有粉丝/关注数，可做BFS种子）
    #   - 无数据 → matched_pending.json（需BFS遍历获取关系）
    matched_done = []
    matched_pending = []

    # 预提取需要的数据列避免反复 iloc
    uids = df[uid_col].tolist()
    names = df[name_col].tolist() if name_col else [None] * total
    hits = df[hit_col].tolist() if hit_col else [None] * total
    fans = df[fan_col].tolist() if fan_col else [None] * total
    follows = df[follow_col].tolist() if follow_col else [None] * total
    avatars = df[avatar_col].tolist() if avatar_col else [None] * total
    intros = df[intro_col].tolist() if intro_col else [None] * total

    for i in range(total):
        uid = int(uids[i])
        name_val = names[i] if names and i < len(names) else None
        hit_val = hits[i] if hits and i < len(hits) else None
        fan_val = fans[i] if fans and i < len(fans) else None
        follow_val = follows[i] if follows and i < len(follows) else None
        avatar_val = avatars[i] if avatars and i < len(avatars) else None
        intro_val = intros[i] if intros and i < len(intros) else None

        # 统一：所有字段都写上，没有就 null
        rec = {"uid": uid}
        rec["name"] = str(name_val).strip() if (name_val and not pd.isna(name_val)) else None
        # 标准化 hit 值：Excel 的「命中规则」列可能有"头像""简介""头像 简介"等文本
        raw_hit = str(hit_val).strip() if (hit_val and not pd.isna(hit_val)) else None
        if raw_hit:
            rec["hit"] = raw_hit
        else:
            rec["hit"] = None
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

        # 所有 Excel 中的 UID 都是已匹配的福瑞
        # 只看有没有粉丝/关注数来判断是 done 还是 pending
        if fan_count is not None or follow_count is not None:
            matched_done.append(rec)
        else:
            matched_pending.append(rec)

    # === 数据完整性报告 ===
    print(f"\n{'=' * 60}")
    print(f"字段完整性报告")
    print(f"{'=' * 60}")
    for label, data_list in [("matched_done.json", matched_done), ("matched_pending.json", matched_pending)]:
        n = len(data_list)
        if n == 0:
            continue
        hit_cnt = sum(1 for r in data_list if r.get("hit"))
        name_cnt = sum(1 for r in data_list if r.get("name"))
        avatar_cnt = sum(1 for r in data_list if r.get("avatar_url"))
        intro_cnt = sum(1 for r in data_list if r.get("intro"))
        fan_cnt = sum(1 for r in data_list if r.get("fan_count") is not None)
        follow_cnt = sum(1 for r in data_list if r.get("follow_count") is not None)
        print(f"\n  {label} ({n} 条)")
        print(f"    hit:         {hit_cnt}/{n}  有命中规则")
        print(f"    name:        {name_cnt}/{n}  有昵称")
        print(f"    avatar_url:  {avatar_cnt}/{n}  有头像URL")
        print(f"    intro:       {intro_cnt}/{n}  有简介")
        print(f"    fan_count:   {fan_cnt}/{n}  有粉丝数")
        print(f"    follow_count:{follow_cnt}/{n}  有关注数")
        if avatar_cnt == 0:
            print(f"    ※ 注意: 旧版 Excel 没有保存头像URL字段，avatar_url 全部为空是正常的")

    # === 输出 JSON ===
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    files = {
        "matched_done.json": (matched_done, "已匹配福瑞，且已有粉丝/关注数据（可直接做BFS种子，以及匹配去重库）"),
        "matched_pending.json": (matched_pending, "已匹配福瑞，但暂无粉丝/关注数据（需要BFS遍历获取）"),
        "not_matched.json": ([], "不符合条件的UID库，由BFS爬虫运行时动态填充，用于标记跳过防止重复消耗算力"),
    }

    print(f"\n{'=' * 60}")
    print(f"分类结果")
    print(f"{'=' * 60}")
    for name, (data, desc) in files.items():
        print(f"  {name:<25s} → {len(data):>6d} 条")
    print(f"  ─────────────────────────────────────────")
    print(f"  总计                                 {total} 条")

    for name, (data, desc) in files.items():
        filepath = os.path.join(out_dir, name)
        output = {
            "meta": {
                "source_file": excel_file,
                "created_at": now_str,
                "total_count": len(data),
                "description": desc,
            },
            "uids": data,
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"  ✓ {name}")

    # === 种子 UID 推荐 ===
    samples = [r["uid"] for r in matched_done[:100]]
    print(f"\n{'=' * 60}")
    print(f"种子 UID 推荐（前100个，可复制到 settings.py）")
    print(f"{'=' * 60}")
    print(f"SEED_UIDS = {json.dumps(samples)}")


if __name__ == "__main__":
    main()
