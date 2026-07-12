"""
测试 _read_xlsx 是否能正确解析 Excel 文件。
直接运行查看输出。 python test_read_xlsx.py
"""
import zipfile
from xml.etree import ElementTree as ET
from io import BytesIO
import os

excel_path = os.path.join(os.path.dirname(__file__), "bilibili_up_export.xlsx")
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

with open(excel_path, "rb") as f:
    data = f.read()

print(f"文件大小: {len(data)} 字节")

with zipfile.ZipFile(BytesIO(data), "r") as zf:
    print(f"ZIP 中的文件列表: {zf.namelist()}")

    for name in zf.namelist():
        if "sheet" in name or "shared" in name:
            raw = zf.read(name)
            print(f"\n--- {name} (前 600 字节) ---")
            print(raw[:600].decode("utf-8", errors="replace"))

    # 用 namespace-aware 方式解析 sheet1
    sheet_xml = zf.read("xl/worksheets/sheet1.xml")
    root = ET.fromstring(sheet_xml)

    # 解析 shared strings
    sst = []
    try:
        ss_xml = zf.read("xl/sharedStrings.xml")
        ss_root = ET.fromstring(ss_xml)
        for si in ss_root.iter(f"{{{NS}}}si"):
            t = si.find(f"{{{NS}}}t")
            if t is not None and t.text:
                sst.append(t.text)
            else:
                parts = []
                for r in si.iter(f"{{{NS}}}r"):
                    t2 = r.find(f"{{{NS}}}t")
                    if t2 is not None and t2.text:
                        parts.append(t2.text)
                sst.append("".join(parts))
    except:
        print("(无 shared strings)")

    # 解析 sheet 第一行（列头）
    print(f"\n=== 解析结果 ===")
    for row_el in root.iter(f"{{{NS}}}row"):
        r = row_el.get("r")
        if r != "1":
            continue
        cols = {}
        for cell in row_el.iter(f"{{{NS}}}c"):
            ref = cell.get("r", "")
            v_el = cell.find(f"{{{NS}}}v")
            value = v_el.text if v_el is not None and v_el.text else ""
            if cell.get("t") == "s" and value and sst:
                idx = int(value)
                value = sst[idx] if idx < len(sst) else f"[OUT_OF_RANGE:{idx}]"

            col_str = "".join(c for c in ref if c.isalpha()).upper()
            col_idx = 0
            for ch in col_str:
                col_idx = col_idx * 26 + (ord(ch) - 64)
            col_idx -= 1
            cols[col_idx] = value
            print(f"  列 {ref} → idx={col_idx} → '{value}'")

        # 按序输出整行
        max_c = max(cols.keys()) + 1
        sorted_row = [cols.get(i, "") for i in range(max_c)]
        print(f"\n  完整第一行: {sorted_row}")
        break
    else:
        print("  未找到 row r=1")
