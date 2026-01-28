from flask import Flask, request, jsonify

app = Flask(__name__)

@app.post('/node-api/v1/heartbeat')
def heartbeat():
    token = request.headers.get('X-Node-Token')
    data = request.json

    print(f"--- Received Heartbeat ---")
    print(f"Token: {token}")
    print(f"Payload: {data}")

    if not token:
        return jsonify({"success": False, "error": "Missing X-Node-Token header"}), 401

    return jsonify({"success": True})

@app.post('/node-api/v1/tasks/sync')
@app.post('/node-api/v1/alerts')
def echo():
    print(f"--- Received Request at {request.path} ---")
    print(request.json)
    return jsonify({"success": True, "status": "mock_ok"})

if __name__ == "__main__":
    # 使用 8080 端口以匹配 config.json 中的默认配置
    app.run(host='0.0.0.0', port=8080)
