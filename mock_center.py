from flask import Flask, request, jsonify
import random

app = Flask(__name__)

# 模拟控制中心的存储
current_exam_id = 100

@app.post('/node-api/v1/heartbeat')
def heartbeat():
    token = request.headers.get('X-Node-Token')
    data = request.json

    print(f"--- [Heartbeat] ---")
    print(f"Token: {token}")
    print(f"Payload: {data}")

    if not token:
        return jsonify({"success": False, "error": "Missing X-Node-Token header"}), 401

    return jsonify({"success": True})

@app.post('/node-api/v1/tasks/sync')
def tasks_sync():
    global current_exam_id
    data = request.json
    action = data.get("action")

    print(f"--- [Task Sync] Action: {action} ---")
    print(f"Payload: {data}")

    if action == "start":
        current_exam_id += 1
        print(f"Generated Exam ID: {current_exam_id}")
        return jsonify({
            "success": True,
            "exam_id": current_exam_id
        })
    elif action == "stop":
        print(f"Stopped Exam ID: {data.get('exam_id')}")
        return jsonify({"success": True})
    elif action == "sync":
        print(f"Synced Exam ID: {data.get('exam_id')}, Count: {data.get('examinee_count')}")
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "error": f"Unknown action: {action}"}), 400

@app.post('/node-api/v1/alerts')
def alerts():
    print(f"--- [Alert] ---")
    print(request.json)
    return jsonify({"success": True, "status": "alert_received"})

if __name__ == "__main__":
    # 使用 8080 端口以匹配 config.json 中的默认配置
    print("Mock Control Center starting on port 8080...")
    app.run(host='0.0.0.0', port=8080)
