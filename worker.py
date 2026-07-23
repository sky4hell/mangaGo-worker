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
        r = requests.post(f"{API_BASE}/user/login", json={
            "userAccount": username, "userPassword": password
        }, timeout=15)
        data = r.json()
        if data.get("code") == 200:
            _token = data.get("data", {}).get("token")
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


def process_ocr_task(task):
    """下载图片 → 本地 OCR → 返回结果"""
    img_url = f"{API_BASE}/downloads/{task['localPath']}"
    try:
        r = requests.get(img_url, timeout=120)
        r.raise_for_status()
    except Exception as e:
        return {"imageId": task["imageId"], "ocrText": "", "ocrMetadata": None,
                "error": f"下载失败: {e}"}

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
    img_url = f"{API_BASE}/downloads/{task['localPath']}"
    try:
        r = requests.get(img_url, timeout=120)
        r.raise_for_status()
    except Exception as e:
        return {"imageId": task["imageId"], "outputImage": "", "error": f"下载失败: {e}"}

    filename = os.path.basename(task["localPath"])
    files = [("images", (filename, r.content, "image/png"))]
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


def process_task(task):
    """处理单个任务"""
    ttype = task["type"]
    if ttype == "ocr":
        return process_ocr_task(task)
    elif ttype == "retouch":
        return process_retouch_task(task)
    return None


def worker_loop(status_callback):
    """后台工作线程"""
    global _running, _stats
    last_task_time = time.time()

    while _running:
        try:
            tasks_data = api_get("/worker/poll", {"type": "ocr", "limit": 3})
            if tasks_data.get("code") == 200:
                tasks = tasks_data.get("data", {}).get("tasks", [])
                if tasks:
                    status_callback(f"处理中: OCR × {len(tasks)}")
                    results = [process_task(t) for t in tasks]
                    r = api_post("/worker/submit", {"type": "ocr", "results": results})
                    if r.get("code") == 200:
                        _stats["ocr"] += r.get("data", {}).get("success", 0)
                        _stats["errors"] += r.get("data", {}).get("failed", 0)
                    last_task_time = time.time()

            # OCR 和 retouch 交替处理
            tasks_data = api_get("/worker/poll", {"type": "retouch", "limit": 3})
            if tasks_data.get("code") == 200:
                tasks = tasks_data.get("data", {}).get("tasks", [])
                if tasks:
                    status_callback(f"处理中: 修图 × {len(tasks)}")
                    results = [process_task(t) for t in tasks]
                    r = api_post("/worker/submit", {"type": "retouch", "results": results})
                    if r.get("code") == 200:
                        _stats["retouch"] += r.get("data", {}).get("success", 0)
                        _stats["errors"] += r.get("data", {}).get("failed", 0)
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
        ttk.Label(self.root, text="mangaGo Worker", font=("", 18, "bold")).pack(pady=15)
        ttk.Label(self.root, text="请登录以开始处理任务").pack()
        ttk.Label(self.root, text="账号").pack(pady=(15, 0))
        self.user_entry = ttk.Entry(self.root, width=30)
        self.user_entry.pack()
        ttk.Label(self.root, text="密码").pack(pady=(10, 0))
        self.pass_entry = ttk.Entry(self.root, width=30, show="*")
        self.pass_entry.pack()
        self.pass_entry.bind("<Return>", lambda e: self._do_login())
        ttk.Button(self.root, text="登录", command=self._do_login).pack(pady=15)
        self.login_status = ttk.Label(self.root, text="", foreground="red")
        self.login_status.pack()

    def _do_login(self):
        u = self.user_entry.get().strip()
        p = self.pass_entry.get().strip()
        if not u or not p:
            self.login_status.config(text="请输入账号和密码")
            return
        self.login_status.config(text="登录中...", foreground="gray")
        ok, msg = do_login(u, p)
        if ok:
            self._setup_main()
        else:
            self.login_status.config(text=msg, foreground="red")

    def _setup_main(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.root.title(f"mangaGo Worker — {_user_info.get('userName', '')}")
        self.root.geometry("420x320")

        ttk.Label(self.root, text="mangaGo Worker", font=("", 18, "bold")).pack(pady=10)
        self.status_label = ttk.Label(self.root, text="就绪", font=("", 11))
        self.status_label.pack()

        frame = ttk.Frame(self.root)
        frame.pack(pady=15)
        ttk.Label(frame, text=f"OCR 处理: 0", font=("", 10)).grid(row=0, column=0, padx=10)
        ttk.Label(frame, text=f"修图处理: 0", font=("", 10)).grid(row=0, column=1, padx=10)
        ttk.Label(frame, text=f"错误: 0", foreground="red").grid(row=0, column=2, padx=10)
        self.stats_frame = frame

        self.start_btn = ttk.Button(self.root, text="启动处理", command=self._toggle)
        self.start_btn.pack(pady=15)

        ttk.Label(self.root, text="启动后请保持翻译服务运行（端口8001）", font=("", 8), foreground="gray").pack()
        ttk.Label(self.root, text="可最小化到后台，自动静默处理", font=("", 8), foreground="gray").pack()

    def _toggle(self):
        global _running
        if _running:
            _running = False
            self.start_btn.config(text="启动处理")
            self.status_label.config(text="已停止")
        else:
            _running = True
            self.start_btn.config(text="停止")
            self.status_label.config(text="空闲中")
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
