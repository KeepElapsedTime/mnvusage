from flask import Flask, render_template, jsonify
from flask_cors import CORS
import GPUtil
import time

app = Flask(__name__)
CORS(app)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/gpu_data')
def gpu_data():
    gpus = GPUtil.getGPUs()
    gpu_info = []
    for gpu in gpus:
        gpu_info.append({
            'id': gpu.id,
            'name': gpu.name,
            'load': round(gpu.load * 100, 2),
            'memory_total': gpu.memoryTotal,
            'memory_used': gpu.memoryUsed,
            'memory_free': gpu.memoryFree,
            'temperature': gpu.temperature
        })
    return jsonify(gpu_info)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
