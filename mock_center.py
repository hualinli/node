from flask import Flask, request, jsonify
import logging
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.logging_setup import setup_logging

app = Flask(__name__)
setup_logging(log_dir="backend/logs", max_lines=100000, max_files=5, level="INFO")
logger = logging.getLogger(__name__)

# 模拟控制中心的存储
current_exam_id = 100

@app.post('/node-api/v1/heartbeat')
def heartbeat():
    token = request.headers.get('X-Node-Token')
    data = request.json

    logger.info("Heartbeat received token=%s payload=%s", token, data)

    if not token:
        return jsonify({"success": False, "error": "Missing X-Node-Token header"}), 401

    return jsonify({"success": True})

@app.post('/node-api/v1/tasks/sync')
def tasks_sync():
    global current_exam_id
    data = request.json
    action = data.get("action")

    logger.info("Task sync action=%s payload=%s", action, data)

    if action == "start":
        current_exam_id += 1
        logger.info("Generated exam id=%s", current_exam_id)
        return jsonify({
            "success": True,
            "exam_id": current_exam_id
        })
    elif action == "stop":
        logger.info("Stopped exam id=%s", data.get('exam_id'))
        return jsonify({"success": True})
    elif action == "sync":
        logger.info(
            "Synced exam id=%s count=%s",
            data.get('exam_id'),
            data.get('examinee_count'),
        )
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "error": f"Unknown action: {action}"}), 400

@app.post('/node-api/v1/alerts')
def alerts():
    logger.info("Alert received")
    # Handle multipart/form-data
    room_id = request.form.get('room_id')
    exam_id = request.form.get('exam_id')
    alert_type = request.form.get('type')
    seat_number = request.form.get('seat_number')
    x = request.form.get('x')
    y = request.form.get('y')
    image_file = request.files.get('image')

    logger.info(
        "Alert payload room_id=%s exam_id=%s type=%s seat_number=%s x=%s y=%s image=%s",
        room_id,
        exam_id,
        alert_type,
        seat_number,
        x,
        y,
        image_file.filename if image_file else 'None',
    )

    if image_file:
        # Save image to root directory
        filename = f"alert_{exam_id}_{seat_number}_{alert_type}.jpg"
        image_file.save(filename)
        logger.info("Alert image saved as: %s", filename)

    return jsonify({"success": True})

if __name__ == "__main__":
    # 使用 8080 端口以匹配 config.json 中的默认配置
    logger.info("Mock control center starting on port 8080")
    app.run(host='0.0.0.0', port=8080)
