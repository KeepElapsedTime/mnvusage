from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import GPUtil
import subprocess
import json
import requests
import time
import os
import threading
from threading import Thread
from pathlib import Path

app = Flask(__name__)
CORS(app)

CONFIG_DIR = Path('config')
CONFIG_PATH = CONFIG_DIR / 'config.json'

DEFAULT_CONFIG = {
    "api_urls": [
        "YOUR_PATH_HERE",
        "YOUR_PATH_HERE2",
        "YOUR_PATH_HERE3"
    ]
}

config_lock = threading.Lock()

def load_config():
    print(f"正在讀取配置文件: {CONFIG_PATH}")
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, 'r') as f:
                config = json.load(f)
                print(f"成功讀取配置: {config}")
                return config
        except Exception as e:
            print(f"讀取配置文件時出錯: {e}")
    
    print(f"使用默認配置: {DEFAULT_CONFIG}")
    return DEFAULT_CONFIG.copy()

def save_config(config):
    try:
        print(f"正在保存配置: {config}")
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        
        if CONFIG_PATH.exists() and not os.access(CONFIG_PATH, os.W_OK):
            print(f"警告: 配置文件 {CONFIG_PATH} 不可寫")
            try:
                os.chmod(CONFIG_PATH, 0o666)
                print("已嘗試更改文件權限")
            except Exception as perm_e:
                print(f"無法更改文件權限: {perm_e}")
        
        temp_path = CONFIG_PATH.with_suffix('.tmp')
        with open(temp_path, 'w') as f:
            json.dump(config, f, indent=2)
        
        if not temp_path.exists() or temp_path.stat().st_size == 0:
            print("警告: 臨時文件寫入失敗或為空")
            return False
            
        temp_path.replace(CONFIG_PATH)
        
        print(f"配置已成功保存到 {CONFIG_PATH}")
        return True
    except Exception as e:
        print(f"保存配置時出錯: {e}")
        import traceback
        traceback.print_exc()
        return False

CONFIG = load_config()

DISCORD_WEBHOOK_URL = "YOUR_WEBHOOK_URL"
GPU_CHECK_INTERVAL = 60
MAX_FAILURES = 3
failure_count = 0

monitor_thread = None

def get_gpu_processes():
    try:
        result = subprocess.run(['nvidia-smi', '--query-compute-apps=pid,used_memory,name', '--format=csv,noheader,nounits'], 
                               capture_output=True, text=True)
        processes = []
        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split(', ')
                if len(parts) >= 3:
                    pid, memory, name = parts[0], parts[1], ', '.join(parts[2:])
                    processes.append({
                        'pid': pid,
                        'memory': memory,
                        'name': name
                    })
        return processes
    except Exception as e:
        print(f"獲取GPU進程時出錯: {e}")
        return []

def get_ollama_models():
    models_list = []
    api_errors = []
    
    with config_lock:
        api_urls = CONFIG.get("api_urls", [])
    
    print(f"正在從以下API URL獲取模型: {api_urls}")
    
    for i, url in enumerate(api_urls):
        if not url or url.strip() == "":
            continue
            
        try:
            print(f"正在請求API {i+1}: {url}")
            response = requests.get(url, timeout=5)
            print(f"API {i+1} 回應狀態碼: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"API {i+1} 回應數據類型: {type(data)}")
                
                if isinstance(data, dict) and "models" in data:
                    models = data.get("models", [])
                    print(f"格式1: 找到 {len(models)} 個模型")
                    
                    for model in models:
                        if isinstance(model, dict):
                            model["source"] = url
                    
                    models_list.extend(models)
                elif isinstance(data, list):
                    print(f"格式2: 找到 {len(data)} 個模型")
                    for item in data:
                        if isinstance(item, dict):
                            item["source"] = url
                            models_list.append(item)
                        elif isinstance(item, str):
                            models_list.append({"name": item, "source": url})
                else:
                    print(f"未知格式，嘗試提取模型")
                    found_models = 0
                    for key, value in data.items():
                        if isinstance(value, list):
                            print(f"在鍵 '{key}' 中找到了列表，有 {len(value)} 個項目")
                            for item in value:
                                if isinstance(item, dict):
                                    item["source"] = url
                                    models_list.append(item)
                                    found_models += 1
                                elif isinstance(item, str):
                                    models_list.append({"name": item, "source": url})
                                    found_models += 1
                    print(f"從未知格式中提取出 {found_models} 個模型")
            else:
                error_msg = f"API {i+1} ({url}): 狀態碼 {response.status_code}"
                api_errors.append(error_msg)
                print(error_msg)
        except Exception as e:
            error_msg = f"API {i+1} ({url}): {str(e)}"
            api_errors.append(error_msg)
            print(error_msg)
    
    print(f"總共收集到 {len(models_list)} 個模型，有 {len(api_errors)} 個錯誤")
    return {
        "models": models_list,
        "errors": api_errors
    }

def restart_containers():
    try:
        print("正在重啟ollama容器...")
        subprocess.run(['docker', 'restart', 'ollama'], check=True)
        
        print("正在重啟監控容器...")
        subprocess.run(['docker', 'restart', os.environ.get('HOSTNAME', 'mnvusage')], check=True)
        
        send_discord_alert("檢測到GPU問題，正在重啟Ollama和監控容器。")
        
        print("容器已重啟")
    except Exception as e:
        print(f"重啟容器時出錯: {e}")
        send_discord_alert(f"GPU問題後重啟容器失敗: {str(e)}")

def send_discord_alert(message):
    try:
        payload = {
            "content": f"⚠️ **GPU監控警報**: {message}",
            "username": "GPU監控器"
        }
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
        print(f"已發送Discord警報: {message}")
    except Exception as e:
        print(f"發送Discord警報時出錯: {e}")

def check_gpu_health():
    global failure_count
    
    while True:
        try:
            gpus = GPUtil.getGPUs()
            if not gpus:
                failure_count += 1
                print(f"未檢測到GPU。失敗計數: {failure_count}/{MAX_FAILURES}")
                if failure_count >= MAX_FAILURES:
                    restart_containers()
                    failure_count = 0
            else:
                any_gpu_unresponsive = False
                for gpu in gpus:
                    try:
                        _ = gpu.load
                        _ = gpu.memoryTotal
                    except Exception:
                        any_gpu_unresponsive = True
                        break
                
                if any_gpu_unresponsive:
                    failure_count += 1
                    print(f"檢測到無響應的GPU。失敗計數: {failure_count}/{MAX_FAILURES}")
                    if failure_count >= MAX_FAILURES:
                        restart_containers()
                        failure_count = 0
                else:
                    failure_count = 0
        except Exception as e:
            print(f"GPU健康檢查時出錯: {e}")
            failure_count += 1
            if failure_count >= MAX_FAILURES:
                restart_containers()
                failure_count = 0
                
        time.sleep(GPU_CHECK_INTERVAL)

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
    
    model_data = get_ollama_models()
    
    with config_lock:
        api_urls = CONFIG.get("api_urls", ["", "", ""])
        while len(api_urls) < 3:
            api_urls.append("")
        api_urls = api_urls[:3]
    
    print(f"返回GPU數據，包含 {len(gpu_info)} 個GPU，{len(model_data.get('models', []))} 個模型，{len(api_urls)} 個URL")
    return jsonify({
        'gpus': gpu_info,
        'ollama_models': model_data.get("models", []),
        'api_errors': model_data.get("errors", []),
        'api_urls': api_urls
    })

@app.route('/update_urls', methods=['POST'])
def update_urls():
    try:
        print("收到更新URL的請求")
        print(f"請求內容類型: {request.content_type}")
        
        raw_data = request.get_data(as_text=True)
        print(f"原始請求數據: {raw_data}")
        
        try:
            data = request.get_json(force=True)
            print(f"解析後的JSON數據: {data}")
        except Exception as e:
            print(f"JSON解析錯誤: {e}")
            return jsonify({"success": False, "message": f"JSON解析錯誤: {str(e)}"}), 400
            
        if not data:
            print("請求數據為空")
            return jsonify({"success": False, "message": "請求數據為空"}), 400
            
        urls = data.get('urls', [])
        print(f"要更新的URL: {urls}")
        
        if not isinstance(urls, list):
            print(f"URLs不是列表類型: {type(urls)}")
            return jsonify({"success": False, "message": "URLs必須是列表類型"}), 400
        
        for url in urls:
            if url and not (url.startswith('http://') or url.startswith('https://')):
                print(f"無效的URL格式: {url}")
                return jsonify({"success": False, "message": f"無效的URL格式: {url}"}), 400
        
        while len(urls) < 3:
            urls.append("")
        urls = urls[:3]
        
        print(f"處理後的URL: {urls}")
        
        with config_lock:
            CONFIG["api_urls"] = urls
            save_success = save_config(CONFIG)
        
        result = {"success": save_success}
        if save_success:
            print("URL更新成功")
            result["message"] = "URL更新成功"
        else:
            print("保存配置失敗")
            result["message"] = "保存配置失敗"
            
        print(f"返回結果: {result}")
        return jsonify(result)
            
    except Exception as e:
        print(f"更新URL時出錯: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": f"錯誤: {str(e)}"}), 500

@app.route('/test_write', methods=['GET'])
def test_write():
    try:
        import os
        from pathlib import Path
        
        response_data = {
            "success": True,
            "tests": []
        }
        
        curr_dir = os.getcwd()
        response_data["tests"].append({
            "name": "當前工作目錄",
            "result": curr_dir,
            "success": True
        })
        
        try:
            import pwd
            username = pwd.getpwuid(os.getuid()).pw_name
            response_data["tests"].append({
                "name": "當前用戶",
                "result": username,
                "success": True
            })
        except:
            response_data["tests"].append({
                "name": "當前用戶",
                "result": "無法獲取",
                "success": False
            })
        
        test_file = Path('test_write.txt')
        try:
            with open(test_file, 'w') as f:
                f.write("Test write at " + time.strftime("%Y-%m-%d %H:%M:%S"))
            
            response_data["tests"].append({
                "name": "測試文件寫入",
                "result": f"成功寫入 {test_file}",
                "success": True
            })
            
            with open(test_file, 'r') as f:
                content = f.read()
                
            response_data["tests"].append({
                "name": "測試文件讀取",
                "result": content,
                "success": True
            })
            
        except Exception as e:
            response_data["tests"].append({
                "name": "測試文件寫入",
                "result": f"失敗: {str(e)}",
                "success": False
            })
        
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if CONFIG_DIR.exists():
                response_data["tests"].append({
                    "name": "配置目錄",
                    "result": f"已存在: {CONFIG_DIR}",
                    "success": True
                })
            else:
                response_data["tests"].append({
                    "name": "配置目錄",
                    "result": f"無法創建: {CONFIG_DIR}",
                    "success": False
                })
        except Exception as e:
            response_data["tests"].append({
                "name": "配置目錄",
                "result": f"錯誤: {str(e)}",
                "success": False
            })
        
        test_config = {
            "test": True,
            "timestamp": time.time()
        }
        try:
            test_config_file = CONFIG_DIR / 'test_config.json'
            with open(test_config_file, 'w') as f:
                json.dump(test_config, f)
                
            response_data["tests"].append({
                "name": "配置文件寫入",
                "result": f"成功寫入 {test_config_file}",
                "success": True
            })
        except Exception as e:
            response_data["tests"].append({
                "name": "配置文件寫入",
                "result": f"失敗: {str(e)}",
                "success": False
            })
        
        return jsonify(response_data)
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        })

if __name__ == '__main__':
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    monitor_thread = Thread(target=check_gpu_health, daemon=True)
    monitor_thread.start()
    print("已啟動GPU健康監控線程")
    
    print("正在啟動Web服務...")
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
