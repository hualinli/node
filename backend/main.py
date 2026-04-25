import json
import logging
import os
import signal
import threading
import time
import asyncio
from contextlib import asynccontextmanager

import uvicorn

# 导入模块化后的组件

from app.config import Config
from app.engine import InferenceEngine
from app.exam import ExamManager
from app.heartbeat import HeartbeatManager
from app.logging_setup import setup_logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


# Bootstrap stderr logging early so import/startup failures are visible in systemd journal.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
bootstrap_logger = logging.getLogger("bootstrap")

try:
    from mindx.sdk import base
except Exception:
    bootstrap_logger.exception("Failed to import mindx.sdk.base during startup")
    raise



# 初始化配置
config = Config("./backend/config.json")
setup_logging(
    log_dir=config.get_path("LOG_DIR", "backend/logs"),
    max_lines=config.get("LOG_MAX_LINES", 100000),
    max_files=config.get("LOG_MAX_FILES", 5),
    level=config.get("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger(__name__)
logger.info(
    "Startup context cwd=%s config=%s log_dir=%s",
    os.getcwd(),
    config.config_path,
    config.get_path("LOG_DIR", "backend/logs"),
)

# 初始化推理引擎
engine = InferenceEngine(config)

# 初始化考试管理器
exam_manager = ExamManager(engine)

# 初始化心跳管理器
heartbeat_manager = HeartbeatManager(config, engine)

# 设置引擎的考试管理器引用
engine.exam_manager = exam_manager

def handle_exam_start():
    """考试开始时同步到控制中心"""
    if getattr(exam_manager, "skip_start_sync", False):
        logger.info("Skip start sync because exam is started by schedule API")
        return

    if exam_manager.exam_running:
        # 格式化开始时间为 ISO 格式
        start_time_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(exam_manager.start_time))
        payload = {
            "action": "start",
            "room_id": exam_manager.classroom_id,
            "subject": exam_manager.subject,
            "start_time": start_time_iso,
            "duration_minutes": int(exam_manager.duration / 60)
        }
        res = heartbeat_manager.sync_task(payload)
        if res and res.get("success"):
            assigned_id = res.get("exam_id")
            if assigned_id is not None:
                exam_manager.exam_id = assigned_id
                logger.info("Exam started, assigned exam_id=%s", exam_manager.exam_id)

def handle_exam_stop():
    """考试结束时同步到控制中心"""
    if exam_manager.exam_id:
        payload = {
            "action": "stop",
            "exam_id": exam_manager.exam_id
        }
        heartbeat_manager.sync_task(payload)
        logger.info("Exam stopped, exam_id=%s", exam_manager.exam_id)

def handle_exam_sync():
    """考试状态或人数同步到控制中心"""
    if not exam_manager.exam_running or exam_manager.exam_id is None:
        return False

    payload = {
        "action": "sync",
        "exam_id": exam_manager.exam_id,
        "examinee_count": exam_manager.get_student_count()
    }
    res = heartbeat_manager.sync_task(payload)
    if res.get("success"):
        logger.info("Exam count synced: %s", payload["examinee_count"])
        return True
    return False

exam_manager.start_callback = handle_exam_start
exam_manager.stop_callback = handle_exam_stop
exam_manager.sync_callback = handle_exam_sync



@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    生命周期管理
    """
    def inject_signal_handler():
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handler = signal.getsignal(sig)
            def custom_handler(signum, frame, old=old_handler):
                engine.exit_event.set()
                engine.inferring_event.clear()
                engine.video_event.clear()
                if callable(old):
                    old(signum, frame)
                elif old == signal.SIG_DFL:
                    if signum == signal.SIGINT:
                        raise KeyboardInterrupt

            signal.signal(sig, custom_handler)

    inject_signal_handler()

    logger.info("Initializing MindX global resources")
    base.mx_init()

    threads = [
        threading.Thread(target=engine.video_reader, daemon=True),
        threading.Thread(target=engine.main_loop, daemon=True),
        threading.Thread(target=engine.post_process_loop, daemon=True),
    ]
    for t in threads:
        t.start()

    heartbeat_manager.start()

    yield

    # 确保关停所有服务
    logger.info("Starting shutdown sequence")
    heartbeat_manager.stop()
    engine.exit_event.set()
    # 如果考试仍在运行，停止考试并取消计时器
    if exam_manager.exam_running:
        exam_manager.cancel_event.set()  # 取消自动停止计时器
        exam_manager.stop_exam()  # 停止考试
    engine.inferring_event.clear()
    engine.video_event.clear()

    # 等待 0.8s，让后台线程安全退出对 NPU 资源的占用，避免 mx_deinit 崩溃
    await asyncio.sleep(0.8)
    try:
        logger.info("Destroying MindX resources")
        base.mx_deinit()
    except Exception as e:
        logger.exception("mx_deinit exception: %s", e)
    logger.info("Service stopped safely")


app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    统一鉴权中间件
    """
    path = request.url.path

    # 静态资源放行，确保样式和脚本能加载
    if path.startswith("/static") or path.startswith("/snapshots"):
        return await call_next(request)

    # 鉴权逻辑：从 Query Param 获取 token
    expected_token = config.get("NODE_TOKEN", "default-node-token")
    provided_token = request.query_params.get("token")

    if provided_token != expected_token:
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "Unauthorized: Invalid or missing token"}
        )

    response = await call_next(request)
    return response

# 挂载静态资源
static_dir = os.path.join(config.get_path("FRONTEND_PATH"), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 挂载异常快照目录
snapshots_dir = config.get_path("SNAPSHOTS_DIR", "snapshots")
os.makedirs(snapshots_dir, exist_ok=True)
app.mount("/snapshots", StaticFiles(directory=snapshots_dir), name="snapshots")

@app.get("/")
async def index():
    index_path = os.path.join(config.get_path("FRONTEND_PATH"), "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse(status_code=404, content={"success": False, "error": f"Index file not found at {index_path}"})

# For test ---- BEGIN
@app.get("/status")
def get_status():
    """获取引擎运行状态和实时 FPS"""
    return {
        "success": True,
        "data": {
            "inferring": engine.inferring_event.is_set(),
            "video_running": engine.video_event.is_set(),
            "is_inferring": engine.is_inferring,
            "fps": round(engine.fps, 2),
        }
    }


@app.get("/cmd/{action}")
def control(action: str):
    """控制推理和视频流启停"""
    if action == "start_inference":
        engine.inferring_event.set()
    elif action == "stop_inference" or action == "stop":
        if exam_manager.exam_running:
            exam_manager.stop_exam(raise_if_not_running=False, reason=f"manual debug action: {action}")
        else:
            engine.video_event.clear()
            engine.inferring_event.clear()
    elif action == "start_video" or action == "start":
        engine.inferring_event.set()
        engine.video_event.set()
    elif action == "stop_video":
        if exam_manager.exam_running:
            exam_manager.stop_exam(raise_if_not_running=False, reason=f"manual debug action: {action}")
        else:
            engine.video_event.clear()
    else:
        return JSONResponse(status_code=400, content={"success": False, "error": "Invalid action"})
    return {"success": True, "action": action}

@app.get("/cmd/set_video/{video_path:path}")
def set_video(video_path: str):
    engine.set_video_source(video_path)
    return {"success": True, "video_path": video_path}
# For test ---- END



#考试管理部分
@app.post("/exam/recalibrate")
def recalibrate_exam():
    try:
        exam_manager.recalibrate()
        return {"success": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.post("/exam/schedule_start")
async def schedule_start_exam(request: Request):
    """由调度器触发开始考试，支持 exam_id 幂等调用。"""
    def _clip_error(msg: str, limit: int = 1024) -> str:
        if not msg:
            return "Unknown error"
        if len(msg) <= limit:
            return msg
        return msg[: limit - 3] + "..."

    request_id = request.headers.get("X-Request-ID", "")
    source_ip = request.headers.get("X-Forwarded-For")
    if not source_ip and request.client:
        source_ip = request.client.host

    try:
        data = await request.json()
    except Exception as e:
        logger.warning(
            "schedule_start invalid json request_id=%s source_ip=%s error=%s",
            request_id,
            source_ip,
            e,
        )
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "Invalid JSON body"},
        )

    subject = data.get("subject")
    duration = data.get("duration")
    classroom_id = data.get("classroom_id")
    exam_id = data.get("exam_id")

    missing_fields = []
    if subject is None or str(subject).strip() == "":
        missing_fields.append("subject")
    if duration is None or str(duration).strip() == "":
        missing_fields.append("duration")
    if classroom_id is None or str(classroom_id).strip() == "":
        missing_fields.append("classroom_id")
    if exam_id is None or str(exam_id).strip() == "":
        missing_fields.append("exam_id")
    if missing_fields:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": f"Missing required fields: {', '.join(missing_fields)}",
            },
        )

    try:
        duration_minutes = int(duration)
        classroom_id = int(classroom_id)
        exam_id = int(exam_id)
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "duration, classroom_id and exam_id must be integers",
            },
        )

    if duration_minutes <= 0:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "duration must be > 0"},
        )

    subject = str(subject).strip()

    # 幂等与冲突检查：同 exam_id 且参数一致视为成功；其他运行中状态视为冲突。
    with exam_manager.lock:
        if exam_manager.exam_running:
            if exam_manager.exam_id == exam_id:
                current_duration_minutes = int((exam_manager.duration or 0) / 60)
                same_payload = (
                    exam_manager.subject == subject
                    and exam_manager.classroom_id == classroom_id
                    and current_duration_minutes == duration_minutes
                )
                if same_payload:
                    logger.info(
                        "schedule_start idempotent success exam_id=%s request_id=%s source_ip=%s",
                        exam_id,
                        request_id,
                        source_ip,
                    )
                    return {"success": True}
                return JSONResponse(
                    status_code=409,
                    content={
                        "success": False,
                        "error": _clip_error(
                            "exam_id is already running with different subject/classroom_id/duration"
                        ),
                    },
                )

            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "error": _clip_error(
                        f"Node is busy with another exam_id={exam_manager.exam_id}"
                    ),
                },
            )

    try:
        # 远程调度开考：跳过本地 start 回调中的“再次向中心申请开考”流程。
        exam_manager.skip_start_sync = True
        exam_manager.start_exam(subject, duration_minutes, classroom_id)
        # 覆盖为调度中心下发的全局 exam_id，供告警和同步链路使用。
        exam_manager.exam_id = exam_id
        logger.info(
            "schedule_start success exam_id=%s subject=%s classroom_id=%s duration_minutes=%s request_id=%s source_ip=%s",
            exam_id,
            subject,
            classroom_id,
            duration_minutes,
            request_id,
            source_ip,
        )
        return {"success": True}
    except Exception as e:
        err = _clip_error(str(e))
        lower_err = err.lower()
        status_code = 500
        if "already running" in lower_err:
            status_code = 409
        elif "invalid" in lower_err or "not found" in lower_err:
            status_code = 400

        logger.exception(
            "schedule_start failed exam_id=%s request_id=%s source_ip=%s error=%s",
            exam_id,
            request_id,
            source_ip,
            err,
        )
        return JSONResponse(
            status_code=status_code,
            content={"success": False, "error": err},
        )
    finally:
        exam_manager.skip_start_sync = False
@app.post("/exam/start")
async def start_exam(request: Request):
    try:
        data = await request.json()
        subject = data.get("subject")
        duration = data.get("duration")
        classroom_id = data.get("classroom_id")
        if not all([subject, duration, classroom_id]):
            return JSONResponse(status_code=400, content={"success": False, "error": "Missing required fields: subject, duration, classroom_id"})
        exam_manager.start_exam(subject, duration, classroom_id)
        return {"success": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.get("/exam/stop")
def stop_exam():
    try:
        exam_manager.stop_exam()
        return {"success": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.get("/exam/status")
def get_exam_status():
    try:
        remaining_seconds = 0
        if exam_manager.exam_running and exam_manager.start_time:
            elapsed = max(0, time.time() - exam_manager.start_time)
            remaining_seconds = max(0, exam_manager.duration - elapsed)
        return {
            "success": True,
            "exam_running": exam_manager.exam_running,
            "subject": exam_manager.subject,
            "duration": exam_manager.duration,
            "classroom_id": exam_manager.classroom_id,
            "start_time": exam_manager.start_time,
            "student_count": exam_manager.get_student_count(),
            "remaining_seconds": int(remaining_seconds),
            "video_width": engine.original_width,
            "video_height": engine.original_height
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.get("/exam/anomalies")
def get_anomalies():
    try:
        centers = exam_manager.engine.final_centers or {}
        anomalies = exam_manager.anomaly_counts
        # 构建数据：按Id排序，包含坐标和计数
        data = []
        for seat_id in sorted(centers.keys()):
            coord = centers[seat_id]
            count = anomalies.get(seat_id, 0)
            data.append({
                "id": seat_id,
                "coord": f"({coord[0]}, {coord[1]})",
                "count": count
            })
        return {
            "success": True,
            "anomalies": data
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.post("/exam/anomalies/reset")
def reset_anomalies():
    try:
        exam_manager.reset_anomaly_counts()
        return {"success": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.get("/exam/anomalies/images")
def get_anomaly_images(count: int = 10):
    from urllib.parse import quote
    if not exam_manager.exam_running or not exam_manager.current_snapshot_dir:
        exam_id = exam_manager.exam_id or exam_manager.local_exam_id
        return {"success": True, "images": [], "exam_id": exam_id}
    try:
        if not os.path.exists(exam_manager.current_snapshot_dir):
            exam_id = exam_manager.exam_id or exam_manager.local_exam_id
            return {"success": True, "images": [], "exam_id": exam_id}
        files = [f for f in os.listdir(exam_manager.current_snapshot_dir) if f.endswith('.jpg')]
        files.sort(key=lambda x: os.path.getmtime(os.path.join(exam_manager.current_snapshot_dir, x)), reverse=True)
        images = []
        exam_id = exam_manager.exam_id or exam_manager.local_exam_id
        # 使用本地考试标识符作为目录名，确保 URL 能正确访问到静态资源
        dir_encoded = quote(str(exam_manager.local_exam_id))
        for f in files[:count]:
            parts = f.split('_')
            if len(parts) >= 6:
                try:
                    # 修正索引：0:snapshot, 1:seatXX, 2:xXXX, 3:yYYY, 4:clsZ, 5:timestamp
                    seat_x = int(parts[2][1:])
                    seat_y = int(parts[3][1:])
                    images.append({
                        "filename": f,
                        "url": f"/snapshots/{dir_encoded}/{f}",
                        "x": seat_x,
                        "y": seat_y
                    })
                except ValueError:
                    continue
        return {"success": True, "images": images, "exam_id": exam_id}
    except Exception as e:
        return {"success": False, "error": str(e)}



# 教室管理部分
@app.get("/classrooms")
def get_classrooms():
    try:
        with open("classrooms.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"success": True, "classrooms": data.get("classrooms", [])}
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"success": False, "error": "classrooms.json 文件不存在"})
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"success": False, "error": "classrooms.json 文件格式错误"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": f"读取失败: {str(e)}"})

@app.post("/classrooms")
def update_classrooms(data: dict):
    temp_path = f"classrooms.json.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        os.replace(temp_path, "classrooms.json")
        return {"success": True}
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return JSONResponse(status_code=500, content={"success": False, "error": f"更新失败: {str(e)}"})


# 视频流部分
def frame_generator():
    last_sent_id = -1
    try:
        # 使用条件变量等待新帧，避免重复发送，不对消费者限流
        while not engine.exit_event.is_set():
            with engine.condition:
                while engine.frame_id == last_sent_id and not engine.exit_event.is_set():
                    engine.condition.wait(0.1)  # 使用超时允许周期检查 exit_event
                if engine.exit_event.is_set():
                    return
                data = engine.latest_jpeg
                last_sent_id = engine.frame_id

            if data:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + data + b"\r\n")
    except Exception as e:
        logger.exception("frame_generator exception: %s", e)
@app.get("/stream")
def stream():
    return StreamingResponse(
        frame_generator(), media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache"}
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002, timeout_graceful_shutdown=1)
