"""
定位 Excel 文件的单元格实际存储格式
"""
import zipfile
from io import BytesIO
import os
import re

excel_path = os.path.join(os.path.dirname(__file__), "bilibili_up_export.xlsx")

with open(excel_path, "rb") as f:
    data = f.read()

with zipfile.ZipFile(BytesIO(data), "r") as zf:
    sheet_xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8", errors="replace")

    # 只输出前 2000 字节，看 row 1 和 row 2 的结构
    start = sheet_xml.find("<row r=\"1\"")
    if start == -1:
        start = sheet_xml.find('row r="1"')
    end = min(len(sheet_xml), start + 3500)
    print("=== 第1行附近 XML（核心部分） ===")
    print(sheet_xml[max(0, start-100):end])

    print("\n\n=== 第2行附近 XML ===")
    start2 = sheet_xml.find("<row r=\"2\"")
    if start2 == -1:
        start2 = sheet_xml.find('row r="2"')
    end2 = min(len(sheet_xml), start2 + 1500)
    print(sheet_xml[max(0, start2-100):end2])
