import threading
import time
import requests
import json
import logging

class HeartbeatManager:
    """
    负责向控制中心定期上报节点状态和心跳信息
    """

    def __init__(self, config, engine):
        self.config = config
        self.engine = engine
        self.stop_event = threading.Event()
        self.thread = None

        # 从配置中获取参数
        self.base_url = self.config.get("CONTROL_CENTER_URL", "http://localhost:8080")
        self.token = self.config.get("NODE_TOKEN", "default-node-token")
        self.interval = self.config.get("HEARTBEAT_INTERVAL", 10)

        self.logger = logging.getLogger("Heartbeat")

    def sync_task(self, payload: dict):
        """
        同步考试状态到控制中心
        URL: /node-api/v1/tasks/sync
        """
        url = f"{self.base_url.rstrip('/')}/node-api/v1/tasks/sync"
        headers = {
            "X-Node-Token": self.token,
            "Content-Type": "application/json"
        }
        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=5
            )
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"Task sync failed with status code: {response.status_code}")
                return {"success": False, "error": f"HTTP {response.status_code}"}
        except Exception as e:
            self.logger.error(f"Task sync request failed: {e}")
            return {"success": False, "error": str(e)}

    def start(self):
        """启动心跳线程"""
        if self.thread and self.thread.is_alive():
            return

        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.logger.info(f"Heartbeat service started. Target: {self.base_url}")

    def stop(self):
        """停止心跳线程"""
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
            self.thread = None
        self.logger.info("Heartbeat service stopped.")

    def _get_node_status(self):
        """
        确定节点当前状态
        枚举: idle (空闲), busy (推理中), error (异常)
        """
        # 检查引擎是否有记录的错误
        if getattr(self.engine, 'last_error', None):
            return "error"

        # 如果引擎正在推理，则为 busy
        if self.engine.is_inferring:
            return "busy"

        return "idle"

    def _get_details(self):
        """
        获取详细的工作负载信息
        """
        details = {
            "fps": round(self.engine.fps, 2),
            "video_running": self.engine.video_event.is_set(),
            "inferring": self.engine.inferring_event.is_set(),
            "current_video": self.engine.current_video_path,
            "last_error": getattr(self.engine, 'last_error', None),
        }

        # 如果关联了考试管理器，添加考试相关信息
        if self.engine.exam_manager:
            exam = self.engine.exam_manager
            details.update({
                "exam_running": exam.exam_running,
                "subject": exam.subject,
                "classroom_id": exam.classroom_id,
                "student_count": exam.get_student_count()
            })

        return details

    def _run(self):
        """核心上报循环"""
        url = f"{self.base_url.rstrip('/')}/node-api/v1/heartbeat"
        headers = {
            "X-Node-Token": self.token,
            "Content-Type": "application/json"
        }

        while not self.stop_event.is_set():
            try:
                payload = {
                    "status": self._get_node_status(),
                    "details": self._get_details()
                }

                response = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=5
                )

                if response.status_code == 200:
                    res_data = response.json()
                    if not res_data.get("success"):
                        self.logger.warning(f"Heartbeat reported success=false: {res_data}")
                else:
                    self.logger.error(f"Heartbeat failed with status code: {response.status_code}")

            except requests.exceptions.RequestException as e:
                self.logger.error(f"Heartbeat request failed: {e}")
            except Exception as e:
                self.logger.error(f"Unexpected error in heartbeat: {e}")

            # 等待下一次上报，支持快速退出
            self.stop_event.wait(self.interval)
