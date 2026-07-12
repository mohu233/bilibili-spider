"""
YOLO 模型单元测试
==================
测试 YOLO 模型是否能正常加载和推理。

用法：
  1. 找一张图片放到脚本同目录，比如 test.jpg
  2. python test_yolo.py test.jpg

  或者直接拖图片到脚本上运行。
"""

import sys
import os

# 项目根目录（单元测试的上一级）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from classifier.yolo_detector import FurryYOLODetector


def main():
    # 检查参数
    if len(sys.argv) < 2:
        print("用法: python test_yolo.py <图片路径>")
        print("示例: python test_yolo.py test.jpg")
        # 找当前目录有没有图片
        images = [f for f in os.listdir(".") if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
        if images:
            print(f"\n当前目录下的图片: {images}")
            print(f"试试: python test_yolo.py {images[0]}")
        return

    image_path = sys.argv[1]
    if not os.path.exists(image_path):
        print(f"[错误] 找不到图片: {image_path}")
        return

    print("=" * 50)
    print("YOLO 模型测试")
    print("=" * 50)
    print(f"图片: {image_path}")
    print(f"大小: {os.path.getsize(image_path) / 1024:.1f} KB")

    # 初始化检测器
    print("\n正在加载 YOLO 模型...")
    try:
        detector = FurryYOLODetector()
    except Exception as e:
        print(f"[错误] 模型加载失败: {e}")
        print("\n可能的原因：")
        print("  1. models/furry1500x100.pt 不存在")
        print("     请运行: copy E:\\furrydata\\furry1500x100.pt models\\")
        print("  2. ultralytics 未安装")
        print("     请运行: pip install ultralytics")
        return

    # 推理
    print("正在推理...\n")
    is_furry, confidence, class_id = detector.predict(image_path)

    # 输出结果
    print("-" * 50)
    if is_furry:
        print(f"  结果: ✓ 福瑞 (Furry detected!)")
    else:
        if class_id is not None:
            print(f"  结果: × 非福瑞 (Not furry)")
        else:
            print(f"  结果: × 未检测到目标")
    print(f"  置信度: {confidence:.4f}  ({confidence * 100:.2f}%)")
    print(f"  类别ID: {class_id}")
    print(f"  阈值:   {detector.conf_threshold}")
    print("-" * 50)

    # 生成标注图
    output_path = image_path.rsplit(".", 1)[0] + "_result.jpg"
    print(f"\n正在生成标注图...")
    is_furry2, conf2, cls2 = detector.predict_and_draw(image_path, output_path)
    print(f"  已保存: {output_path}")

    print(f"\n完成!")


if __name__ == "__main__":
    main()
