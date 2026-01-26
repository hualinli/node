from flask import Flask, request
app = Flask(__name__)

@app.post('/node-api/v1/heartbeat')
@app.post('/node-api/v1/tasks/sync')
@app.post('/node-api/v1/alerts')
def echo():
    print(request.json)  # 打印上报内容
    return {"status": "mock_ok"}

app.run(port=9000)
