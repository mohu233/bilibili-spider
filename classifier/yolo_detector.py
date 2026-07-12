"""
YOLO 兽图头像检测器
===================
基于 ultralytics YOLOv8 的图片分类。

使用方式：
    detector = FurryYOLODetector()
    is_furry = detector.predict("path/to/avatar.jpg")
"""

import os
import sys
from typing import Optional

import cv2

from config import settings


class FurryYOLODetector:
    """
    YOLO 兽图检测器。

    加载训练好的 furry1500x200.pt 模型，对头像图片进行推理，
    判断是否为福瑞相关图片。
    """

    def __init__(self, model_path: Optional[str] = None, conf_threshold: Optional[float] = None):
        """
        初始化检测器。

        Args:
            model_path: .pt 模型文件路径，默认使用 settings.YOLO_MODEL_PATH
            conf_threshold: 置信度阈值，默认使用 settings.YOLO_CONF_THRESHOLD
        """
        self.model_path = model_path or settings.YOLO_MODEL_PATH
        self.conf_threshold = conf_threshold if conf_threshold is not None else settings.YOLO_CONF_THRESHOLD
        self.model = None  # 延迟加载

    def _lazy_load(self):
        """延迟加载模型（避免 import 时卡住）"""
        if self.model is not None:
            return
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(
                f"YOLO 模型文件不存在: {self.model_path}\n"
                f"请将 furry1500x200.pt 放到 models/ 目录下"
            )
        print(f"  [YOLO] 加载模型: {self.model_path}")
        from ultralytics import YOLO
        self.model = YOLO(self.model_path)
        print(f"  [YOLO] 模型加载完成")

    def predict(self, image_path: str) -> tuple:
        """
        检测单张图片是否为福瑞。

        Args:
            image_path: 图片路径

        Returns:
            (is_furry, confidence, class_id)
            - is_furry: True/False
            - confidence: 最高置信度得分
            - class_id: 检测到的类别 ID（0 或 1 代表福瑞）
        """
        self._lazy_load()

        if not os.path.exists(image_path):
            print(f"    [YOLO] 图片不存在: {image_path}")
            return False, 0.0, None

        # 读取图片
        image = cv2.imread(image_path, cv2.IMREAD_COLOR)
        if image is None:
            print(f"    [YOLO] 无法读取图片: {image_path}")
            return False, 0.0, None

        # YOLO 推理
        results = self.model(image)

        # 解析结果
        classes = results[0].boxes.cls.tolist()
        confs = results[0].boxes.conf.tolist()

        if not classes:
            return False, 0.0, None

        # 找到最高置信度的检测结果
        max_conf = max(confs)
        max_idx = confs.index(max_conf)
        class_id = int(classes[max_idx])

        # 判断是否为福瑞（类别 0 或 1 代表 furry）
        is_furry = (class_id in (0, 1)) and (max_conf >= self.conf_threshold)

        return is_furry, max_conf, class_id

    def predict_and_draw(self, image_path: str, output_path: Optional[str] = None) -> tuple:
        """
        检测并生成标注图片。

        Args:
            image_path: 输入图片路径
            output_path: 标注图片输出路径（None 则不保存）

        Returns:
            (is_furry, confidence, class_id)
        """
        self._lazy_load()

        if not os.path.exists(image_path):
            return False, 0.0, None

        image = cv2.imread(image_path, cv2.IMREAD_COLOR)
        if image is None:
            return False, 0.0, None

        results = self.model(image)

        # 绘制标注
        annotated = results[0].plot()
        if output_path:
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            cv2.imwrite(output_path, annotated)

        classes = results[0].boxes.cls.tolist()
        confs = results[0].boxes.conf.tolist()

        if not classes:
            return False, 0.0, None

        max_conf = max(confs)
        max_idx = confs.index(max_conf)
        class_id = int(classes[max_idx])
        is_furry = (class_id in (0, 1)) and (max_conf >= self.conf_threshold)

        return is_furry, max_conf, class_id
