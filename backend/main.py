import json
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
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mindx.sdk import base



# 初始化配置
config = Config("./backend/config.json")

# 初始化推理引擎
engine = InferenceEngine(config)

# 初始化考试管理器
exam_manager = ExamManager(engine)

# 设置引擎的考试管理器引用
engine.exam_manager = exam_manager



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

    print(" [System] 初始化 MindX 全局资源...")
    base.mx_init()

    threads = [
        threading.Thread(target=engine.video_reader, daemon=True),
        threading.Thread(target=engine.main_loop, daemon=True),
        threading.Thread(target=engine.post_process_loop, daemon=True),
    ]
    for t in threads:
        t.start()

    yield

    # 确保关停所有服务
    print(" [System] 正在执行销毁流程...")
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
        print(" [System] 正在销毁 MindX 资源...")
        base.mx_deinit()
    except Exception as e:
        print(f" [Error] mx_deinit 异常: {e}")
    print(" [System] 服务已安全停止。")


app = FastAPI(lifespan=lifespan)

# 挂载静态资源
static_dir = os.path.join(config.get_path("FRONTEND_PATH"), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def index():
    index_path = os.path.join(config.get_path("FRONTEND_PATH"), "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse(status_code=404, content={"success": False, "error": f"Index file not found at {index_path}"})


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
        engine.video_event.clear()
        engine.inferring_event.clear()
    elif action == "start_video" or action == "start":
        engine.inferring_event.set()
        engine.video_event.set()
    elif action == "stop_video":
        engine.video_event.clear()
    else:
        return JSONResponse(status_code=400, content={"success": False, "error": "Invalid action"})
    return {"success": True, "action": action}

@app.get("/cmd/set_video/{video_path:path}")
def set_video(video_path: str):
    engine.set_video_source(video_path)
    return {"success": True, "video_path": video_path}










#考试管理部分
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
        return {
            "success": True,
            "exam_running": exam_manager.exam_running,
            "subject": exam_manager.subject,
            "duration": exam_manager.duration,
            "classroom_id": exam_manager.classroom_id,
            "start_time": exam_manager.start_time,
            "student_count": exam_manager.get_student_count()
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
    except Exception:
        pass
@app.get("/stream")
def stream():
    return StreamingResponse(
        frame_generator(), media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache"}
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002, timeout_graceful_shutdown=1)
