"""
mnvusage — Monitor NV Usage
Flask app to monitor NVIDIA GPU status and Ollama models.
"""

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from threading import Thread

import GPUtil
import requests
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path("config") / "config.json"
DEFAULT_CONFIG = {"api_urls": []}
config_lock = threading.Lock()

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GPU_CHECK_INTERVAL = int(os.environ.get("GPU_CHECK_INTERVAL", "60"))
MAX_FAILURES = int(os.environ.get("GPU_MAX_FAILURES", "3"))


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to read config: %s", e)
    return DEFAULT_CONFIG.copy()


def save_config(config: dict) -> bool:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(config, f, indent=2)
        tmp.replace(CONFIG_PATH)
        return True
    except Exception as e:
        log.error("Failed to save config: %s", e)
        return False


CONFIG = load_config()

# ── Flask ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)

# ── GPU helpers ───────────────────────────────────────────────────────────────

def get_gpu_processes() -> list:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory,name", "--format=csv,noheader,nounits"],
            capture_output=True, text=True,
        )
        processes = []
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split(", ")
                if len(parts) >= 3:
                    processes.append({
                        "pid": parts[0],
                        "memory": parts[1],
                        "name": ", ".join(parts[2:]),
                    })
        return processes
    except Exception as e:
        log.warning("get_gpu_processes error: %s", e)
        return []


def get_ollama_models() -> dict:
    with config_lock:
        api_urls = [u for u in CONFIG.get("api_urls", []) if u and u.strip()]

    models_list, errors = [], []

    for url in api_urls:
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                errors.append(f"{url}: HTTP {resp.status_code}")
                continue

            data = resp.json()
            items = []
            if isinstance(data, dict) and "models" in data:
                items = data["models"]
            elif isinstance(data, list):
                items = data
            else:
                for v in data.values():
                    if isinstance(v, list):
                        items = v
                        break

            for item in items:
                if isinstance(item, str):
                    item = {"name": item}
                if isinstance(item, dict):
                    item["source"] = url
                    models_list.append(item)

        except Exception as e:
            errors.append(f"{url}: {e}")

    return {"models": models_list, "errors": errors}

# ── Health monitor ────────────────────────────────────────────────────────────

failure_count = 0


def send_discord_alert(message: str):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={
            "content": f"⚠️ **GPU Monitor Alert**: {message}",
            "username": "mnvusage",
        }, timeout=5)
    except Exception as e:
        log.error("Discord alert failed: %s", e)


def restart_containers():
    try:
        subprocess.run(["docker", "restart", "ollama"], check=True)
        subprocess.run(["docker", "restart", os.environ.get("HOSTNAME", "mnvusage")], check=True)
        send_discord_alert("GPU issue detected — restarting Ollama and monitor containers.")
    except Exception as e:
        log.error("restart_containers error: %s", e)
        send_discord_alert(f"Failed to restart containers after GPU issue: {e}")


def check_gpu_health():
    global failure_count
    while True:
        try:
            gpus = GPUtil.getGPUs()
            if not gpus:
                failure_count += 1
                log.warning("No GPU detected. Failure %d/%d", failure_count, MAX_FAILURES)
            else:
                unresponsive = False
                for gpu in gpus:
                    try:
                        _ = gpu.load, gpu.memoryTotal
                    except Exception:
                        unresponsive = True
                        break
                if unresponsive:
                    failure_count += 1
                    log.warning("Unresponsive GPU. Failure %d/%d", failure_count, MAX_FAILURES)
                else:
                    failure_count = 0

            if failure_count >= MAX_FAILURES:
                restart_containers()
                failure_count = 0

        except Exception as e:
            log.error("GPU health check error: %s", e)
            failure_count += 1
            if failure_count >= MAX_FAILURES:
                restart_containers()
                failure_count = 0

        time.sleep(GPU_CHECK_INTERVAL)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/gpu_data")
def gpu_data():
    gpus_info = []
    for gpu in GPUtil.getGPUs():
        gpus_info.append({
            "id": gpu.id,
            "name": gpu.name,
            "load": round(gpu.load * 100, 1),
            "memory_total": gpu.memoryTotal,
            "memory_used": gpu.memoryUsed,
            "memory_free": gpu.memoryFree,
            "temperature": gpu.temperature,
            "processes": get_gpu_processes(),
        })

    model_data = get_ollama_models()

    with config_lock:
        api_urls = CONFIG.get("api_urls", [])

    return jsonify({
        "gpus": gpus_info,
        "ollama_models": model_data["models"],
        "api_errors": model_data["errors"],
        "api_urls": api_urls,
    })


@app.route("/update_urls", methods=["POST"])
def update_urls():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"success": False, "message": "Empty request body"}), 400

    urls = data.get("urls", [])
    if not isinstance(urls, list):
        return jsonify({"success": False, "message": "urls must be an array"}), 400

    # Validate and clean
    cleaned = []
    for url in urls:
        url = url.strip() if isinstance(url, str) else ""
        if url and not (url.startswith("http://") or url.startswith("https://")):
            return jsonify({"success": False, "message": f"Invalid URL: {url}"}), 400
        if url:
            cleaned.append(url)

    with config_lock:
        CONFIG["api_urls"] = cleaned
        ok = save_config(CONFIG)

    return jsonify({"success": ok, "message": "URLs updated" if ok else "Failed to save"})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    Thread(target=check_gpu_health, daemon=True).start()
    log.info("Starting mnvusage on :5000")
    app.run(host="0.0.0.0", port=5000, threaded=True)
