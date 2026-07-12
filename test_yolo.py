"""
YOLO 模型单元测试
==================
在 PyCharm 中直接运行，测试 单元测试/ 目录下所有图片。
"""

import sys
import os

# 把项目根目录加入路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from classifier.yolo_detector import FurryYOLODetector


def main():
    test_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "单元测试")

    if not os.path.exists(test_dir):
        print(f"[错误] 找不到图片目录: {test_dir}")
        return

    images = [
        os.path.join(test_dir, f) for f in os.listdir(test_dir)
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ]

    if not images:
        print(f"[错误] 单元测试/ 目录下没有图片")
        return

    print("=" * 50)
    print(f"YOLO 模型测试 — 共 {len(images)} 张图片")
    print("=" * 50)

    print("\n正在加载 YOLO 模型...")
    try:
        detector = FurryYOLODetector()
    except Exception as e:
        print(f"[错误] 模型加载失败: {e}")
        return

    for i, image_path in enumerate(images):
        filename = os.path.basename(image_path)
        size_kb = os.path.getsize(image_path) / 1024

        print(f"\n[{i+1}/{len(images)}] {filename} ({size_kb:.1f} KB)")

        try:
            is_furry, confidence, class_id = detector.predict(image_path)

            if is_furry:
                print(f"  → ✓ 福瑞 (置信度: {confidence:.2%})")
            elif class_id is not None:
                print(f"  → × 非福瑞 (置信度: {confidence:.2%})")
            else:
                print(f"  → × 未检测到目标")

            output_path = image_path.rsplit(".", 1)[0] + "_result.jpg"
            detector.predict_and_draw(image_path, output_path)

        except Exception as e:
            print(f"  → [错误] {e}")

    print(f"\n{'=' * 50}")
    print(f"完成！标注图已保存")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
