import json
import math
import time
import threading
from typing import Optional
import numpy as np
from .tracker import Tracker
import os
import requests
class ExamManager:
    """考试管理器"""

    def __init__(self, engine):
        """初始化考试管理器

        Args:
            engine: 推理引擎实例
        """
        self.engine = engine
        self.lock = threading.RLock()  # 添加锁来保护共享状态，允许重入
        self.cancel_event = threading.Event()  # 用于取消计时器的线程事件
        self.exam_running = False  # 考试是否正在运行
        self.exam_id: Optional[int] = None  # 考试ID (由控制中心分配)
        self.subject: Optional[str] = None  # 考试科目
        self.duration: Optional[int] = None  # 考试时长（秒）
        self.classroom_id: Optional[int] = None  # 教室ID
        self.start_time: Optional[float] = None  # 考试开始时间
        self.timer_thread: Optional[threading.Thread] = None  # 自动停止计时器线程
        self.track_timer: Optional[threading.Timer] = None  # 跟踪启动定时器
        self.student_count = 0  # 考生数 (实际检测到的)
        self.start_callback = None  # 开始考试回调
        self.stop_callback = None  # 停止考试回调
        self.sync_callback = None  # 状态同步回调
        self.anomaly_counts = {}  # 异常情况计数 {seat_id: count}
        self.anomaly_snapshots = {}  # 异常截图跟踪 {seat_id: {cls_id: {'count': int, 'last_time': float}}}
        self.snapshot_cooldown = {}  # 截图冷却 { (seat_id, cls_id): last_snapshot_frame }
        self.frame_counter = 0  # 帧计数器，用于帧数冷却
        self.current_snapshot_dir = None  # 当前考试的截图目录

    def load_classrooms(self):
        """加载教室信息从classrooms.json"""
        try:
            with open("classrooms.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("classrooms", [])
        except Exception as e:
            raise Exception(f"Failed to load classrooms: {str(e)}")

    def get_classroom_url(self, classroom_id: int):
        """根据教室ID获取视频URL

        Args:
            classroom_id: 教室ID

        Returns:
            str: 视频URL

        Raises:
            Exception: 如果教室未找到
        """
        classrooms = self.load_classrooms()
        for classroom in classrooms:
            if classroom["id"] == classroom_id:
                return classroom["url"]
        raise Exception(f"Classroom with id {classroom_id} not found")

    def start_exam(self, subject: str, duration: str, classroom_id: int):
        """开始考试

        Args:
            subject: 考试科目
            duration: 考试时长（分钟）
            classroom_id: 教室ID

        Raises:
            Exception: 如果考试已在运行或输入无效
        """
        with self.lock:  # 加锁保护状态检查和修改
            if self.exam_running:
                raise Exception("An exam is already running")

            try:
                duration_sec = int(duration) * 60
            except ValueError:
                raise Exception("Invalid duration format")

            url = self.get_classroom_url(classroom_id)

            # 设置视频源
            self.engine.set_video_source(url)

            # 开始推理和视频
            self.engine.inferring_event.set()
            self.engine.video_event.set()

            # 记录考试详情
            self.exam_running = True
            self.exam_id = None
            self.subject = subject
            self.duration = duration_sec
            self.classroom_id = classroom_id
            self.start_time = time.time()
            self.student_count = 0  # 重置考生数
            self.anomaly_counts = {}  # 重置异常计数
            self.anomaly_snapshots = {}  # 重置异常截图跟踪
            self.snapshot_cooldown = {}  # 重置截图冷却
            self.frame_counter = 0  # 重置帧计数器

            # 创建考试特定的截图目录
            exam_id = f"{self.subject}_{self.classroom_id}_{int(self.start_time)}"
            self.current_snapshot_dir = f"snapshots/{exam_id}"
            os.makedirs(self.current_snapshot_dir, exist_ok=True)

            # 重置取消事件
            self.cancel_event.clear()

            # 启动计时器线程以在时长后自动停止
            self.timer_thread = threading.Thread(target=self._auto_stop_timer, daemon=True)
            self.timer_thread.start()

            # 启动跟踪定时器
            self.track_timer = threading.Timer(self.engine.config.get("TRACK_DELAY_SECONDS"), self._start_tracking)
            self.track_timer.start()

        # 触发开始考试回调 (锁外执行)
        if self.start_callback:
            try:
                self.start_callback()
            except Exception as e:
                print(f"[ExamManager] Start callback error: {e}")

    def stop_exam(self):
        """停止考试

        Raises:
            Exception: 如果没有考试在运行
        """
        with self.lock:  # 加锁保护状态检查和修改
            if not self.exam_running:
                raise Exception("No exam is currently running")

            # 停止推理和视频
            self.engine.inferring_event.clear()
            self.engine.video_event.clear()

            # 设置取消事件以停止计时器线程
            self.cancel_event.set()

            # 取消跟踪定时器
            if self.track_timer and self.track_timer.is_alive():
                self.track_timer.cancel()
            self.track_timer = None

            # 清空跟踪结果
            self.engine.final_centers = None
            self.engine.tracker = Tracker()  # 重置跟踪器

        # 触发停止考试回调 (在重置状态前，且在锁外执行)
        if self.stop_callback:
            try:
                self.stop_callback()
            except Exception as e:
                print(f"[ExamManager] Stop callback error: {e}")

        with self.lock:
            # 重置考试状态
            self.exam_running = False
            self.exam_id = None
            self.subject = None
            self.duration = None
            self.classroom_id = None
            self.start_time = None
            self.student_count = 0  # 重置考生数
            self.anomaly_counts = {}  # 重置异常计数
            self.anomaly_snapshots = {}  # 重置异常截图跟踪
            self.snapshot_cooldown = {}  # 重置截图冷却
            self.frame_counter = 0  # 重置帧计数器

            # 归档当前考试的截图
            if self.current_snapshot_dir and os.path.exists(self.current_snapshot_dir):
                archive_dir = f"archives/{os.path.basename(self.current_snapshot_dir)}"
                os.makedirs("archives", exist_ok=True)
                os.rename(self.current_snapshot_dir, archive_dir)
                print(f"[Archive] Moved snapshots to {archive_dir}")
            self.current_snapshot_dir = None

            if self.timer_thread and self.timer_thread.is_alive():
                self.timer_thread = None  # 让它自然死亡

    def _start_tracking(self):
        """启动跟踪"""
        with self.lock:
            if self.exam_running:
                # 重新标定时重置异常计数
                self.reset_anomaly_counts()
                self.engine.tracker = Tracker()  # 重置跟踪器
                self.engine.final_centers = None
                self.engine.frame_count = 0
                self.engine.tracking_event.set()
                print(f"[Tracker] Started tracking for {self.engine.max_frames} frames")
                # 启动一个后台线程等待跟踪结束并同步
                threading.Thread(target=self._wait_for_tracking_and_sync, daemon=True).start()

    def recalibrate(self):
        """重新标定"""
        with self.lock:
            if not self.exam_running:
                raise Exception("No exam is currently running")

            # 如果已有定时器在运行，取消它
            if self.track_timer and self.track_timer.is_alive():
                self.track_timer.cancel()

            # 立即开始标定
            self._start_tracking()

    def _wait_for_tracking_and_sync(self):
        """等待跟踪完成并触发同步"""
        # 确保已经开始跟踪
        time.sleep(1)
        # 等待 tracking_event 被引擎清除（表示跟踪完成）
        while self.exam_running and self.engine.tracking_event.is_set():
            time.sleep(1)

        if self.exam_running and self.sync_callback:
            try:
                self.sync_callback()
            except Exception as e:
                print(f"[ExamManager] Sync callback error: {e}")

    def get_student_count(self):
        """获取考生数，根据标定结果的中心点个数"""
        if self.engine.final_centers:
            self.student_count = len(self.engine.final_centers)
        return self.student_count

    def reset_anomaly_counts(self):
        """重置异常计数与相关跟踪状态"""
        with self.lock:
            self.anomaly_counts = {}
            self.anomaly_snapshots = {}
            self.snapshot_cooldown = {}
            self.frame_counter = 0

    def update_anomaly(self, box, cls_id):
        """更新异常计数

        Args:
            box: 检测框 [x1, y1, x2, y2]
            cls_id: 分类ID
        """
        if not self.engine.final_centers:
            return

        # 计算异常框中心点
        center_x = (box[0] + box[2]) / 2
        center_y = (box[1] + box[3]) / 2
        anomaly_center = np.array([center_x, center_y])

        # 获取所有座位中心
        centers = np.array(list(self.engine.final_centers.values()))
        if len(centers) == 0:
            return

        # 计算距离
        distances = np.linalg.norm(centers - anomaly_center, axis=1)
        min_dist = np.min(distances)
        closest_idx = np.argmin(distances)

        # 获取座位ID
        seat_ids = list(self.engine.final_centers.keys())
        closest_seat = seat_ids[closest_idx]

        # 如果距离不超过阈值，增加计数
        threshold = self.engine.config.get("anomaly_match_threshold", 50)
        if min_dist <= threshold:
            self.anomaly_counts[closest_seat] = self.anomaly_counts.get(closest_seat, 0) + 1

    def update_anomaly_snapshots(self, frame, anomalies, timestamp, current_frame=None):
        """更新异常截图逻辑

        Args:
            frame: 当前帧
            anomalies: 预计算的异常列表
            timestamp: 当前时间戳
            current_frame: 当前帧计数（外部传入）
        """
        if not self.engine.final_centers:
            return

        # 帧计数：优先使用外部传入，保证全局帧连续
        if current_frame is None:
            self.frame_counter += 1
            current_frame = self.frame_counter
        else:
            self.frame_counter = current_frame

        # 更新计数并检查截图条件
        for anomaly in anomalies:
            seat_id = anomaly['seat_id']
            cls_id = anomaly['cls_id']

            if seat_id not in self.anomaly_snapshots:
                self.anomaly_snapshots[seat_id] = {}
            if cls_id not in self.anomaly_snapshots[seat_id]:
                self.anomaly_snapshots[seat_id][cls_id] = {'count': 0, 'last_time': timestamp, 'last_frame': None}

            # 连续帧计数：断帧则重置
            last_frame = self.anomaly_snapshots[seat_id][cls_id].get('last_frame')
            if last_frame is None or current_frame != last_frame + 1:
                self.anomaly_snapshots[seat_id][cls_id]['count'] = 1
            else:
                self.anomaly_snapshots[seat_id][cls_id]['count'] += 1
            self.anomaly_snapshots[seat_id][cls_id]['last_time'] = timestamp
            self.anomaly_snapshots[seat_id][cls_id]['last_frame'] = current_frame

            # 检查是否超过阈值且不在冷却期（帧数）
            threshold_frames = self.engine.config.get("snapshot_threshold_frames", 12)
            cooldown_frames = self.engine.config.get("snapshot_cooldown_frames", 720)
            count = self.anomaly_snapshots[seat_id][cls_id]['count']
            cooldown_key = (seat_id, cls_id)
            last_snapshot_frame = self.snapshot_cooldown.get(cooldown_key, 0)

            if (
                count >= threshold_frames
                and (current_frame - last_snapshot_frame) >= cooldown_frames
            ):
                self.take_snapshot(frame.copy(), anomaly, timestamp)
                self.snapshot_cooldown[cooldown_key] = current_frame
                # 重置计数以重新积累
                self.anomaly_snapshots[seat_id][cls_id]['count'] = 0

    def take_snapshot(self, frame, anomaly, timestamp):
        """保存异常截图

        Args:
            frame: 帧图像
            anomaly: 异常信息 {'seat_id', 'cls_id', 'box', 'center'}
            timestamp: 时间戳
        """
        import cv2
        import os

        seat_id = anomaly['seat_id']
        cls_id = anomaly['cls_id']
        box = anomaly['box']
        center = anomaly['center']

        # 获取座位的 x, y 坐标
        seat_center = self.engine.final_centers.get(seat_id, (0, 0))
        seat_x, seat_y = int(seat_center[0]), int(seat_center[1])

        # 在帧上标出座位ID和异常类型
        class_names = self.engine.config.get("class_names", ["Unknown"] * 10)
        label = f"Seat {seat_id}: {class_names[cls_id] if cls_id < len(class_names) else 'Unknown'}"
        cv2.putText(frame, label, (int(center[0]), int(center[1]) - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        cv2.rectangle(frame, (box[0], box[1]), (box[2], box[3]), (0, 0, 255), 2)

        # 保存帧到当前考试目录
        if self.current_snapshot_dir:
            filename = f"{self.current_snapshot_dir}/snapshot_seat{seat_id}_x{seat_x}_y{seat_y}_cls{cls_id}_{int(timestamp)}.jpg"
            cv2.imwrite(filename, frame)
            print(f"[Snapshot] Saved anomaly snapshot: {filename}")

            # 发送异常上报到控制中心
            self._send_alert_to_center(seat_id, cls_id, seat_x, seat_y, filename)
        else:
            print("[Snapshot] No current snapshot directory, skipping save")

    def _send_alert_to_center(self, seat_id, cls_id, x, y, image_path):
        """发送异常上报到控制中心"""
        if not self.exam_id or not self.classroom_id:
            print("[Alert] Missing exam_id or classroom_id, skipping alert")
            return

        # 映射异常类型
        anomaly_type_map = {
            0: "head_abnormal",
            1: "limb_abnormal",
            2: "sleeping",
            3: "standing",
            4: "normal"
        }
        alert_type = anomaly_type_map.get(cls_id, "unknown")

        base_url = self.engine.config.get("CONTROL_CENTER_URL", "http://localhost:8080")
        token = self.engine.config.get("NODE_TOKEN", "default-node-token")
        url = f"{base_url.rstrip('/')}/node-api/v1/alerts"
        headers = {
            "X-Node-Token": token
        }

        try:
            with open(image_path, 'rb') as f:
                files = {'image': f}
                data = {
                    'room_id': self.classroom_id,
                    'exam_id': self.exam_id,
                    'type': alert_type,
                    'seat_number': str(seat_id),
                    'x': x,
                    'y': y
                }
                response = requests.post(url, headers=headers, data=data, files=files, timeout=10)
                if response.status_code == 200:
                    res_data = response.json()
                    if res_data.get("success"):
                        print(f"[Alert] Successfully sent alert for seat {seat_id}, type {alert_type}")
                    else:
                        print(f"[Alert] Alert failed: {res_data}")
                else:
                    print(f"[Alert] HTTP error {response.status_code}: {response.text}")
        except Exception as e:
            print(f"[Alert] Failed to send alert: {e}")

    def _auto_stop_timer(self):
        """自动停止计时器，在考试时长结束后停止考试"""
        if self.duration:
            # 使用事件等待，可被取消
            if self.cancel_event.wait(self.duration):
                # 如果事件被设置（取消），则退出
                return
            # 如果超时，检查是否仍在运行并停止
            with self.lock:
                if self.exam_running:
                    try:
                        self.stop_exam()
                    except Exception as e:
                        print(f"[Timer] Error stopping exam: {e}")
