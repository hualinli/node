import json
import time
import threading
from typing import Optional

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
        self.subject: Optional[str] = None  # 考试科目
        self.duration: Optional[int] = None  # 考试时长（秒）
        self.classroom_id: Optional[int] = None  # 教室ID
        self.start_time: Optional[float] = None  # 考试开始时间
        self.timer_thread: Optional[threading.Thread] = None  # 自动停止计时器线程

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
            self.subject = subject
            self.duration = duration_sec
            self.classroom_id = classroom_id
            self.start_time = time.time()

            # 重置取消事件
            self.cancel_event.clear()

            # 启动计时器线程以在时长后自动停止
            self.timer_thread = threading.Thread(target=self._auto_stop_timer, daemon=True)
            self.timer_thread.start()

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

            # 重置考试状态
            self.exam_running = False
            self.subject = None
            self.duration = None
            self.classroom_id = None
            self.start_time = None
            if self.timer_thread and self.timer_thread.is_alive():
                self.timer_thread = None  # 让它自然死亡

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
