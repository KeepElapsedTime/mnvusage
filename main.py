from flask import Flask, render_template, jsonify
from flask_cors import CORS
import GPUtil
import subprocess
import json

app = Flask(__name__)
CORS(app)

def get_gpu_processes():
    try:
        result = subprocess.run(['nvidia-smi', '--query-compute-apps=pid,used_memory,name', '--format=csv,noheader,nounits'], capture_output=True, text=True)
        processes = []
        for line in result.stdout.strip().split('\n'):
            if line:
                pid, memory, name = line.split(', ')
                processes.append({
                    'pid': pid,
                    'memory': memory,
                    'name': name
                })
        return processes
    except Exception as e:
        print(f"Error getting GPU processes: {e}")
        return []

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
            'temperature': gpu.temperature,
            'processes': get_gpu_processes()
        })
    return jsonify(gpu_info)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)