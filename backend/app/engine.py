import gc
import os
import queue
import threading
import time

import cv2
import numpy as np
from mindx.sdk import base

from .config import Config
from .models import MindXModel, post_process_det


class InferenceEngine:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)  # 用于通知新帧
        self.latest_jpeg = None  # 共享的 JPEG 缓存
        self.frame_id = 0  # 当前帧ID
        self.latest_frame_id = 0  # 消费者看到的最新帧ID

        # 状态统计
        self.fps = 0.0
        self.frame_times = []
        self.is_inferring = False

        # 信号与队列
        self.inferring_event = threading.Event()  # 控制推理启停
        self.video_event = threading.Event()      # 控制视频流启停
        self.exit_event = threading.Event()

        self.raw_q = queue.Queue(maxsize=self.config.get("QUEUE_SIZE"))  # 原始帧队列 (Reader -> Infer)
        self.result_q = queue.Queue(maxsize=self.config.get("QUEUE_SIZE"))  # 结果队列 (Infer -> PostProcess)

        # 当前视频源（动态设置）
        self.current_video_path = None

    def set_video_source(self, video_path):
        with self.lock:
            self.current_video_path = video_path
        if self.video_event.is_set():
            self.video_event.clear()
            time.sleep(0.1)
            self.video_event.set()  # 触发 reader 重新打开


    def video_reader(self):
        self.cap = None
        while not self.exit_event.is_set():
            if not self.video_event.is_set():
                # 停止时释放视频捕获对象
                if self.cap and self.cap.isOpened():
                    print(" [VideoReader] 正在释放视频源...")
                    self.cap.release()
                    self.cap = None
                time.sleep(0.1)
                continue
            v_path = self.current_video_path
            if not v_path:
                time.sleep(0.1)
                continue
            print(f" [VideoReader] 使用视频源: {v_path}")

            self.cap = cv2.VideoCapture(v_path)
            if not self.cap.isOpened():
                print(f" [Error] 无法打开视频源: {v_path}")
                self.video_event.clear()
                continue

            fps = self.cap.get(cv2.CAP_PROP_FPS)
            if fps <= 0:
                fps = 24
            frame_interval = 1.0 / fps

            consecutive_failures = 0
            max_consecutive_failures = 10  # 超时阈值：连续 10 次读取失败则尝试重连
            while (
                self.cap.isOpened()
                and self.video_event.is_set()
                and not self.exit_event.is_set()
            ):
                t_start = time.perf_counter()
                ret, frame = self.cap.read()
                if not ret:
                    consecutive_failures += 1
                    if consecutive_failures > max_consecutive_failures:
                        print(f" [Error] 连续 {max_consecutive_failures} 次读取失败，尝试重连...")
                        # 尝试重连，最多 3 次，每次间隔 1 秒
                        reconnected = False
                        for attempt in range(3):
                            time.sleep(1)  # 等待 1 秒再重试
                            self.cap = cv2.VideoCapture(v_path)
                            if self.cap.isOpened():
                                print(f" [VideoReader] 重连成功")
                                consecutive_failures = 0
                                reconnected = True
                                break
                            print(f" [Error] 重连失败 (尝试 {attempt + 1}/3)")
                        if not reconnected:
                            print(f" [Error] 重连最终失败")
                            break
                    continue
                else:
                    consecutive_failures = 0

                if self.raw_q.full():
                    try:
                        self.raw_q.get_nowait()
                    except queue.Empty:
                        pass

                try:
                    self.raw_q.put_nowait(frame)
                except queue.Full:
                    pass

                # 按照视频原生 FPS 节奏控制读取速度
                t_elapsed = time.perf_counter() - t_start
                wait_time = frame_interval - t_elapsed
                if wait_time > 0:
                    time.sleep(wait_time)
            if self.cap:
                self.cap.release()
                self.cap = None

            for q in [self.raw_q, self.result_q]:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break

    def main_loop(self):
        while not self.exit_event.is_set():
            if not self.inferring_event.is_set():
                self.is_inferring = False
                time.sleep(0.1)
                continue

            device_id = self.config.get("DEVICE_ID")
            print(f" [NPU] 正在加载模型资源 (Device {device_id})...")
            try:
                det_model = MindXModel(self.config.get_path("DET_MODEL_PATH"), device_id)
                cls_model = MindXModel(self.config.get_path("CLS_MODEL_PATH"), device_id)
                cls_size = tuple(self.config.get("CLS_SIZE"))
                cls_batch = self.config.get("CLS_BATCH")
                cls_buffer = np.zeros(
                    (cls_batch, cls_size[1], cls_size[0], 3), dtype=np.uint8
                )
                self.is_inferring = True
            except Exception as e:
                print(f" [Error] 模型加载失败: {e}")
                self.inferring_event.clear()
                continue

            while self.inferring_event.is_set() and not self.exit_event.is_set():
                try:
                    frame = self.raw_q.get(timeout=0.5)
                except queue.Empty:
                    continue

                h, w = frame.shape[:2]
                det_size = tuple(self.config.get("DET_SIZE"))
                det_in = cv2.resize(frame, det_size)
                det_raw = det_model.infer(np.expand_dims(det_in, axis=0))[0]
                conf_thres = self.config.get("CONF_THRES")
                iou_thres = self.config.get("IOU_THRES")
                boxes, _ = post_process_det(det_raw, (w, h), conf_thres, iou_thres, det_size)

                cls_ids = []
                if len(boxes) > 0:
                    cls_size = tuple(self.config.get("CLS_SIZE"))
                    crops = [
                        cv2.resize(
                            frame[max(0, b[1]) : b[3], max(0, b[0]) : b[2]], cls_size
                        )
                        for b in boxes
                        if (b[3] - b[1]) > 0 and (b[2] - b[0]) > 0
                    ]
                    cls_batch = self.config.get("CLS_BATCH")
                    for i in range(0, len(crops), cls_batch):
                        chunk = crops[i : i + cls_batch]
                        cls_buffer[: len(chunk)] = chunk
                        res = cls_model.infer(cls_buffer)[0]
                        cls_ids.extend(np.argmax(res[: len(chunk)], axis=1).tolist())

                # 将推理结果放入结果队列，交给绘图线程
                try:
                    self.result_q.put(
                        (frame, boxes, cls_ids), timeout=0.5
                    )
                except queue.Full:
                    continue

            # 释放 NPU 资源
            print(" [NPU] 正在释放模型资源...")
            try:
                del det_model
                del cls_model
            except:
                pass

            self.is_inferring = False
            print(" [NPU] 推理已停止")

    def post_process_loop(self):
        """后处理线程"""
        while not self.exit_event.is_set():
            try:
                frame, boxes, cls_ids = self.result_q.get(timeout=0.5)
            except queue.Empty:
                if not self.inferring_event.is_set():
                    with self.lock:
                        self.latest_jpeg = None
                    self.fps = 0.0
                    self.frame_times = []
                continue

            # 1. 绘图逻辑

            # 绘制实时检测框
            class_names = self.config.get("class_names")
            class_colors = self.config.get("class_colors")
            for i, box in enumerate(boxes):
                cls_id = cls_ids[i] if i < len(cls_ids) else 0
                color = tuple(class_colors[cls_id]) if cls_id < len(class_colors) else (0, 255, 0)
                cv2.rectangle(frame, (box[0], box[1]), (box[2], box[3]), color, 2)
                label = class_names[cls_id] if cls_id < len(class_names) else "Unknown"
                cv2.putText(
                    frame,
                    label,
                    (box[0], max(box[1] - 10, 20)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    color,
                    2,
                )

            # 2. FPS 统计
            now = time.perf_counter()
            fps_window_size = self.config.get("FPS_WINDOW_SIZE")
            self.frame_times.append(now)
            if len(self.frame_times) > fps_window_size:
                self.frame_times.pop(0)
            if len(self.frame_times) > 1:
                self.fps = len(self.frame_times) / (
                    self.frame_times[-1] - self.frame_times[0]
                )

            cv2.putText(
                frame,
                f"Real-time FPS: {self.fps:.2f}",
                (30, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (0, 0, 255),
                2,
            )

            # 3. 核心优化：预先压缩 JPEG 并缓存，所有客户端共享这一个压缩结果
            jpeg_quality = self.config.get("JPEG_QUALITY")
            jpeg_width = self.config.get("JPEG_WIDTH")
            if jpeg_width is not None and jpeg_width > 0:
                h, w = frame.shape[:2]
                aspect_ratio = h / w
                new_h = int(jpeg_width * aspect_ratio)
                frame = cv2.resize(frame, (jpeg_width, new_h))
            ret, buffer = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
            )
            if ret:
                with self.lock:
                    self.latest_jpeg = buffer.tobytes()
                    self.frame_id += 1
                    self.condition.notify_all()
