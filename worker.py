"""
mangaGo Worker — 桌面程序
登录 → 轮询取任务 → 调用本地翻译服务 → 提交结果
"""
import os
import sys
import json
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import requests

# ====== 配置 ======
API_BASE = os.environ.get("MANGA_API", "https://zalomanga.com/api")
LOCAL_TRANSLATOR = "http://localhost:8001"
TRANSLATOR_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  'manga-image-translator', 'server', 'main.py')
POLL_INTERVAL = 5  # 轮询间隔（秒）

# ====== 全局状态 ======
_token = None
_user_info = None
_running = False
_stats = {"ocr": 0, "retouch": 0, "errors": 0}


def api_post(path, data=None):
    headers = {"Authorization": f"Bearer {_token}"} if _token else {}
    r = requests.post(f"{API_BASE}{path}", json=data, headers=headers, timeout=30)
    return r.json()


def api_get(path, params=None):
    headers = {"Authorization": f"Bearer {_token}"} if _token else {}
    r = requests.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=30)
    return r.json()


def do_login(username, password):
    global _token, _user_info
    try:
        r = requests.post(f"{API_BASE}/sysUser/login", json={
            "adminAccount": username, "password": password
        }, timeout=15)
        data = r.json()
        if data.get("code") == 200:
            _token = data.get("data", {}).get("tokens", {}).get("long_token", {}).get("token")
            _user_info = data.get("data", {})
            return True, ""
        return False, data.get("message", "登录失败")
    except Exception as e:
        return False, str(e)


def _build_render_metadata(ocr_metadata_str, translated_str):
    """构建 render-direct 需要的 blocks + 译文"""
    try:
        blocks = json.loads(ocr_metadata_str)
        trans_list = json.loads(translated_str)
        if isinstance(trans_list, str):
            trans_list = [trans_list]
    except Exception:
        return {"image_0": {"blocks": []}}
    for j, b in enumerate(blocks):
        if j < len(trans_list):
            t = trans_list[j]
            txt = t if isinstance(t, str) else (t.get("text", "") if isinstance(t, dict) else "")
            b["text"] = txt
    return {"image_0": {"blocks": blocks}}


# 本地图片目录（上传时同步存到这里，OCR/修图优先本地读）
_LOCAL_DOWNLOADS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'manga-image-translator', 'downloads')


def _get_image_bytes(local_path):
    """优先本地文件，没有则云端下载"""
    if _LOCAL_DOWNLOADS and local_path:
        fp = os.path.join(_LOCAL_DOWNLOADS, local_path.replace('\\', '/'))
        if os.path.exists(fp):
            with open(fp, 'rb') as f:
                return f.read()
    r = requests.get(f"{API_BASE}/downloads/{local_path}", timeout=120)
    r.raise_for_status()
    return r.content


def process_ocr_task(task):
    """本地图片优先 → OCR → 返回结果"""
    try:
        img_bytes = _get_image_bytes(task['localPath'])
    except Exception as e:
        return {"imageId": task["imageId"], "ocrText": "", "ocrMetadata": None,
                "error": f"获取图片失败: {e}"}

    filename = os.path.basename(task["localPath"])
    files = [("images", (filename, r.content, "image/png"))]
    config = json.dumps({
        "ocr": {"ocr": "48px_ctc", "min_text_length": 1},
        "translator": {"translator": "none"},
        "text_merge": {"enabled": True},
        "remove_watermark": True,
        "inpainter": {"inpainter": "none"},
        "renderer": {"renderer": "none", "rtl": True},
        "detector": {"detection_size": 2048, "text_threshold": 0.2, "box_threshold": 0.4}
    })
    try:
        r = requests.post(f"{LOCAL_TRANSLATOR}/review/ocr/with-form/batch/json",
                          files=files,
                          data={"config": config, "save_to_db": "false"},
                          timeout=300)
        if r.status_code != 200:
            return {"imageId": task["imageId"], "ocrText": "", "ocrMetadata": None,
                    "error": f"OCR服务 HTTP {r.status_code}"}
        ocr_data = r.json().get("data", {})
        results = ocr_data.get("results", [])
        if not results:
            return {"imageId": task["imageId"], "ocrText": "", "ocrMetadata": None,
                    "error": "OCR 无结果"}
        r0 = results[0]
        return {"imageId": task["imageId"],
                "ocrText": r0.get("ocr_text", ""),
                "ocrMetadata": json.dumps(r0.get("text_blocks", []))}
    except Exception as e:
        return {"imageId": task["imageId"], "ocrText": "", "ocrMetadata": None,
                "error": str(e)}


def process_retouch_task(task):
    """下载图片 → 本地修图 → 返回 base64"""
    try:
        img_bytes = _get_image_bytes(task['localPath'])
    except Exception as e:
        return {"imageId": task["imageId"], "outputImage": "", "error": f"获取图片失败: {e}"}

    filename = os.path.basename(task["localPath"])
    files = [("images", (filename, img_bytes, "image/png"))]
    # 用云端的 OCR blocks 坐标 + 译文
    ocr_meta = task.get("ocrMetadata") or "[]"
    translated = task.get("correctedTranslatedText") or task.get("translatedText") or "[]"
    metadata_dict = _build_render_metadata(ocr_meta, translated)
    config = '{"render": {"font_scale_factor": 0.65}}'
    try:
        r = requests.post(f"{LOCAL_TRANSLATOR}/review/render-direct/batch",
                          files=files,
                          data={"config": config, "ocr_metadata": json.dumps(metadata_dict)},
                          timeout=300)
        if r.status_code != 200:
            return {"imageId": task["imageId"], "outputImage": "",
                    "error": f"修图服务 HTTP {r.status_code}"}
        result_data = r.json().get("data", {})
        results = result_data.get("results", [])
        if not results or not results[0] or results[0].get("status") != "success":
            return {"imageId": task["imageId"], "outputImage": "",
                    "error": results[0].get("error", "修图失败") if results else "无结果"}
        return {"imageId": task["imageId"], "outputImage": results[0].get("output_image", "")}
    except Exception as e:
        return {"imageId": task["imageId"], "outputImage": "", "error": str(e)}


def worker_loop(status_callback):
    """后台工作线程"""
    global _running, _stats
    last_task_time = time.time()

    while _running:
        try:
            # 轮询 OCR 任务
            data = api_get("/worker/poll", {"type": "ocr"})
            task = (data.get("data", {}) or {}).get("task") if data.get("code") == 200 else None
            if task:
                pending = task.get("pendingImages", [])
                total = task.get("totalImages", 0)
                for img in pending:
                    status_callback(f"OCR {task['completedCount']+task['failedCount']+1}/{total}")
                    r = process_ocr_task(img)
                    api_post("/worker/submit", {
                        "taskId": task["taskId"], "type": "ocr",
                        "imageId": img["imageId"],
                        "ocrText": r.get("ocrText", ""),
                        "ocrMetadata": r.get("ocrMetadata")
                    })
                    _stats["ocr"] += 1
                last_task_time = time.time()

            # 轮询修图任务
            data = api_get("/worker/poll", {"type": "retouch"})
            task = (data.get("data", {}) or {}).get("task") if data.get("code") == 200 else None
            if task:
                pending = task.get("pendingImages", [])
                total = task.get("totalImages", 0)
                for img in pending:
                    status_callback(f"修图 {task['completedCount']+task['failedCount']+1}/{total}")
                    r = process_retouch_task(img)
                    api_post("/worker/submit", {
                        "taskId": task["taskId"], "type": "retouch",
                        "imageId": img["imageId"],
                        "outputImage": r.get("outputImage", "")
                    })
                    _stats["retouch"] += 1
                last_task_time = time.time()

            if time.time() - last_task_time > 10:
                status_callback("空闲中")
        except requests.ConnectionError:
            status_callback("连接失败，重试中...")
        except Exception as e:
            status_callback(f"错误: {str(e)[:50]}")
            _stats["errors"] += 1

        time.sleep(POLL_INTERVAL)


# ====== GUI ======
class WorkerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("mangaGo Worker")
        self.root.geometry("420x350")
        self.root.resizable(False, False)
        self._setup_login()
        self.root.mainloop()

    def _setup_login(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.root.geometry("420x200")
        ttk.Label(self.root, text="mangaGo Worker", font=("", 18, "bold")).pack(pady=20)
        self.login_status = ttk.Label(self.root, text="", foreground="gray")
        self.login_status.pack(pady=5)
        self.login_btn = ttk.Button(self.root, text="网页登录", command=self._start_web_login)
        self.login_btn.pack(pady=15)
        ttk.Label(self.root, text="点击登录后在浏览器中完成认证", font=("", 9), foreground="gray").pack()
        # 读本地缓存 token
        threading.Thread(target=self._try_cached_token, daemon=True).start()

    def _try_cached_token(self):
        global _token, _user_info
        try:
            with open('token.txt', 'r') as f:
                tk = f.read().strip()
            if tk:
                import urllib.request
                req = urllib.request.Request(f"{API_BASE}/worker/poll?type=ocr",
                    headers={"Authorization": f"Bearer {tk}"})
                r = urllib.request.urlopen(req, timeout=10)
                if r.status != 401:
                    _token = tk
                    self.root.after(0, self._setup_main)
                    return
        except Exception:
            pass
        self.root.after(0, lambda: self.login_status.config(text="请登录"))

    def _start_web_login(self):
        import http.server
        self.login_status.config(text="等待浏览器认证...", foreground="blue")
        self.login_btn.config(state="disabled")
        # 启动本地回调服务
        parent = self
        class CallbackHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                global _token, _user_info
                tk = self.path.lstrip('/?token=')
                _token = tk
                parent.root.after(0, parent._setup_main)
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write('<html><body><h2>✅ 登录成功</h2><p>可关闭此页面</p></body></html>'.encode())
                # 保存 token
                try:
                    with open('token.txt', 'w') as f:
                        f.write(tk)
                except Exception:
                    pass
        svr = http.server.HTTPServer(('localhost', 9527), CallbackHandler)
        threading.Thread(target=lambda: (svr.handle_request(), svr.server_close()), daemon=True).start()
        import webbrowser
        webbrowser.open(f"{API_BASE.replace('/api', '')}/admin?worker_callback=9527")

    def _setup_main(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.root.title(f"mangaGo Worker — 已连接")
        self.root.geometry("420x350")

        ttk.Label(self.root, text="mangaGo Worker", font=("", 18, "bold")).pack(pady=10)
        self.status_label = ttk.Label(self.root, text="就绪", font=("", 11))
        self.status_label.pack()

        frame = ttk.Frame(self.root)
        frame.pack(pady=15)
        ttk.Label(frame, text=f"OCR 处理: 0", font=("", 10)).grid(row=0, column=0, padx=10)
        ttk.Label(frame, text=f"修图处理: 0", font=("", 10)).grid(row=0, column=1, padx=10)
        ttk.Label(frame, text=f"错误: 0", foreground="red").grid(row=0, column=2, padx=10)
        self.stats_frame = frame

        ttk.Label(self.root, text="翻译服务启动中...", font=("", 9), foreground="blue").pack(pady=5)
        ttk.Label(self.root, text="可最小化到后台，自动静默处理", font=("", 8), foreground="gray").pack()
        # 自动启动翻译服务 + 开始处理
        threading.Thread(target=self._auto_start, daemon=True).start()

    def _start_translator(self):
        """后台启动翻译服务"""
        import subprocess
        venv_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'manga-image-translator', 'venv', 'Scripts', 'python.exe')
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        subprocess.Popen([venv_py, TRANSLATOR_SCRIPT, '--port', '8001', '--use-gpu', '--models-ttl=3600'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=si)

    def _auto_start(self):
        self.update_status("启动翻译服务中...")
        self._start_translator()
        import time
        for i in range(30):
            time.sleep(2)
            try:
                import urllib.request
                urllib.request.urlopen("http://localhost:8001/docs", timeout=3)
                break
            except Exception:
                self.update_status(f"等待翻译服务就绪 ({i*2}s)...")
        global _running
        _running = True
        self.update_status("空闲中")
        threading.Thread(target=worker_loop, args=(self.update_status,), daemon=True).start()
        threading.Thread(target=self._stats_updater, daemon=True).start()

    def update_status(self, msg):
        self.root.after(0, lambda: self.status_label.config(text=msg))

    def _stats_updater(self):
        while _running:
            self.root.after(0, self._refresh_stats)
            time.sleep(3)

    def _refresh_stats(self):
        for w in self.stats_frame.winfo_children():
            w.destroy()
        ttk.Label(self.stats_frame, text=f"OCR: {_stats['ocr']}", font=("", 10)).grid(row=0, column=0, padx=10)
        ttk.Label(self.stats_frame, text=f"修图: {_stats['retouch']}", font=("", 10)).grid(row=0, column=1, padx=10)
        ttk.Label(self.stats_frame, text=f"错误: {_stats['errors']}", foreground="red").grid(row=0, column=2, padx=10)


if __name__ == "__main__":
    WorkerApp()
