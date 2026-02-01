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
from .tracker import Tracker


class InferenceEngine:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)  # 用于通知新帧
        self.latest_jpeg = None  # 共享的 JPEG 缓存
        self.frame_id = 0  # 当前帧ID
        self.latest_frame_id = 0  # 消费者看到的最新帧ID
        self.original_width = 0
        self.original_height = 0

        # 状态统计
        self.fps = 0.0
        self.frame_times = []
        self.is_inferring = False
        self.last_error = None

        # 信号与队列
        self.inferring_event = threading.Event()  # 控制推理启停
        self.video_event = threading.Event()      # 控制视频流启停
        self.exit_event = threading.Event()

        self.raw_q = queue.Queue(maxsize=self.config.get("QUEUE_SIZE"))  # 原始帧队列 (Reader -> Infer)
        self.result_q = queue.Queue(maxsize=self.config.get("QUEUE_SIZE"))  # 结果队列 (Infer -> PostProcess)

        # 当前视频源（动态设置）
        self.current_video_path = None

        # 跟踪器相关 START----
        self.tracker = Tracker()
        self.tracking_event = threading.Event()
        self.frame_count = 0
        self.max_frames = self.config.get("TRACK_MAX_FRAMES")
        self.final_centers = None
        # 跟踪器相关 END----

        self.exam_manager = None  # 考试管理器引用
    def set_video_source(self, video_path):
        with self.lock:
            self.current_video_path = video_path
            self.last_error = None
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
                self.last_error = f"无法打开视频源: {v_path}"
                print(f" [Error] {self.last_error}")
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
                                self.last_error = None
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
                det_size = tuple(self.config.get("DET_SIZE"))
                det_buffer = np.empty((1, det_size[1], det_size[0], 3), dtype=np.uint8)
                self.is_inferring = True
                self.last_error = None
            except Exception as e:
                self.last_error = f"模型加载失败: {e}"
                print(f" [Error] {self.last_error}")
                self.inferring_event.clear()
                continue

            while self.inferring_event.is_set() and not self.exit_event.is_set():
                try:
                    frame = self.raw_q.get(timeout=0.5)
                except queue.Empty:
                    continue

                h, w = frame.shape[:2]
                self.original_width = w
                self.original_height = h
                det_in = cv2.resize(frame, det_size)
                det_buffer[0] = det_in
                det_raw = det_model.infer(det_buffer)[0]
                conf_thres = self.config.get("CONF_THRES")
                iou_thres = self.config.get("IOU_THRES")
                boxes, _ = post_process_det(det_raw, (w, h), conf_thres, iou_thres, det_size)

                # 跟踪器相关 START----
                if self.tracking_event.is_set():
                    # 跟踪模式：更新跟踪器
                    self.tracker.update(boxes.tolist())
                    self.frame_count += 1
                    if self.frame_count >= self.max_frames:
                        self.final_centers = self.tracker.get_final_centers()
                        self.tracking_event.clear()
                        self.frame_count = 0
                        print(f" [Tracker] 跟踪完成，最终目标数: {len(self.final_centers)}")
                # 跟踪器相关 END----
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

            # 更新异常计数 + 收集异常截图候选并绘图
            anomaly_classes = self.config.get("anomaly_classes", [0,1,2,3])
            snapshot_classes = self.config.get("snapshot_classes", [0,1,2,3])
            timestamp = time.time()
            frame_for_snapshot = None
            raw_frame_for_snapshot = None
            anomaly_map = {}
            anomalies = []
            centers = None
            seat_ids = None
            threshold = self.config.get("anomaly_match_threshold", 50)
            current_frame = None
            if self.exam_manager and self.exam_manager.exam_running:
                raw_frame_for_snapshot = frame.copy()
                if self.final_centers:
                    centers = np.array(list(self.final_centers.values()))
                    seat_ids = list(self.final_centers.keys())
                with self.exam_manager.lock:
                    self.exam_manager.frame_counter += 1
                    current_frame = self.exam_manager.frame_counter

            class_names = self.config.get("class_names")
            class_colors = self.config.get("class_colors")
            for i, box in enumerate(boxes):
                cls_id = cls_ids[i] if i < len(cls_ids) else 0

                matched_seat = None
                if centers is not None and len(centers) > 0:
                    center_x = (box[0] + box[2]) / 2
                    center_y = (box[1] + box[3]) / 2
                    distances = np.linalg.norm(centers - np.array([center_x, center_y]), axis=1)
                    min_dist = np.min(distances)
                    if min_dist <= threshold:
                        closest_idx = np.argmin(distances)
                        matched_seat = seat_ids[closest_idx]

                if matched_seat is not None:
                    if cls_id in anomaly_classes:
                        self.exam_manager.anomaly_counts[matched_seat] = self.exam_manager.anomaly_counts.get(matched_seat, 0) + 1
                    if cls_id in snapshot_classes:
                        if frame_for_snapshot is None:
                            frame_for_snapshot = raw_frame_for_snapshot if raw_frame_for_snapshot is not None else frame.copy()
                        key = (matched_seat, cls_id)
                        if key not in anomaly_map:
                            anomaly_map[key] = {
                                'seat_id': matched_seat,
                                'cls_id': cls_id,
                                'box': box,
                                'center': ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2),
                                'frame_id': self.frame_id
                            }


                # 绘制实时检测框
                color = tuple(class_colors[cls_id]) if cls_id < len(class_colors) else (0, 255, 0)
                cv2.rectangle(frame, (box[0], box[1]), (box[2], box[3]), color, 2)
                label = class_names[cls_id] if cls_id < len(class_names) else "Unknown"
                # cv2.putText(
                #     frame,
                #     label,
                #     (box[0], max(box[1] - 10, 20)),
                #     cv2.FONT_HERSHEY_SIMPLEX,
                #     0.6,
                #     color,
                #     2,
                # )

            if anomaly_map:
                anomalies = list(anomaly_map.values())

            if self.exam_manager and self.exam_manager.exam_running and anomalies:
                # 更新异常截图（仅在有候选时）
                self.exam_manager.update_anomaly_snapshots(
                    frame_for_snapshot if frame_for_snapshot is not None else frame,
                    anomalies,
                    timestamp,
                    current_frame,
                )

                # 绘制标定后的中心点和ID ---- 约降低 3 FPS
                # if self.final_centers:
                #     for track_id, center in self.final_centers.items():
                #         x, y = center
                #         cv2.circle(frame, (x, y), 5, (255, 0, 0), -1)  # 蓝色圆点
                #         cv2.putText(frame, str(track_id), (x + 10, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 0, 0), 2)

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

            # cv2.putText(
            #     frame,
            #     f"Real-time FPS: {self.fps:.2f}",
            #     (30, 40),
            #     cv2.FONT_HERSHEY_SIMPLEX,
            #     1,
            #     (0, 0, 255),
            #     2,
            # )

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
