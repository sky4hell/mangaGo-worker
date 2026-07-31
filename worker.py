"""
mangaGo Worker — 桌面程序
登录 → 轮询取任务 → 调用本地翻译服务 → 提交结果
"""
import os
import shutil
import sys
import json
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import requests

# ====== 日志 ======
import logging
logging.basicConfig(
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'worker.log'),
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
def _log(msg):
    logging.info(msg)
    print(msg)

# ====== 配置 ======
API_BASE = os.environ.get("MANGA_API", "https://zalomanga.com/api")
LOCAL_TRANSLATOR = "http://localhost:8001"
TRANSLATOR_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  'manga-image-translator', 'server', 'main.py')
POLL_INTERVAL = 5  # 轮询间隔（秒）
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token.txt')

# ====== 全局状态 ======
_token = None
_user_info = None
_running = False
_stats = {"ocr": 0, "retouch": 0, "errors": 0}
_error_log = []  # [(time, imageId, error_msg)]
_lock = threading.Lock()
_api_session = requests.Session()
_local_session = requests.Session()
_local_session.trust_env = False


def api_post(path, data=None):
    headers = {"Authorization": f"Bearer {_token}"} if _token else {}
    r = _api_session.post(f"{API_BASE}{path}", json=data, headers=headers, timeout=30)
    try:
        return r.json()
    except Exception as e:
        _log(f'api_post JSON error: {e} status={r.status_code}')
        return {"code": -1, "message": str(e)}


def api_get(path, params=None):
    headers = {"Authorization": f"Bearer {_token}"} if _token else {}
    _log(f'api_get {path} token_len={len(_token) if _token else 0}')
    r = _api_session.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=30)
    try:
        return r.json()
    except Exception as e:
        _log(f'api_get JSON error: {e} status={r.status_code}')
        return {"code": -1, "message": str(e)}


def do_login(username, password):
    global _token, _user_info
    try:
        r = requests.post(f"{API_BASE}/auth/login", json={
            "adminAccount": username, "password": password
        }, timeout=15)
        data = r.json()
        if data.get("code") == 200:
            tokens = data.get("data", {}).get("tokens", {})
            _token = tokens.get("access_token", {}).get("token")
            _refresh = tokens.get("refresh_token", {}).get("token")
            _user_info = data.get("data", {})
            if _token and _refresh:
                try:
                    with open(TOKEN_FILE, 'w') as tf:
                        tf.write('{"a":"' + _token + '","r":"' + _refresh + '"}')
                except: pass
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
    except Exception as e:
        _log(f'_build_render_metadata error: {e}')
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
    fp = None
    if _LOCAL_DOWNLOADS and local_path:
        fp = os.path.join(_LOCAL_DOWNLOADS, local_path.replace('\\', '/'))
        if os.path.exists(fp):
            _log(f'local_hit: {local_path[:50]}')
            with open(fp, 'rb') as fh:
                return fh.read()
        else:
            _log(f'local_miss: {local_path[:50]}')
    r = _api_session.get(f"{API_BASE}/downloads/{local_path}", timeout=120)
    r.raise_for_status()
    # 下载后存本地，下次直接读盘
    if fp:
        try:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, 'wb') as fh:
                fh.write(r.content)
        except Exception as e:
            _log(f'local_cache_write_error: {e}')
    return r.content


def process_ocr_task(task):
    """本地图片优先 → OCR → 返回结果"""
    try:
        img_bytes = _get_image_bytes(task['localPath'])
    except Exception as e:
        return {"imageId": task["imageId"], "ocrText": "", "ocrMetadata": None,
                "error": f"获取图片失败: {e}"}

    filename = os.path.basename(task["localPath"])
    files = [("images", (filename, img_bytes, "image/png"))]
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
        r = _local_session.post(f"{LOCAL_TRANSLATOR}/review/ocr/with-form/batch/json",
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
    # 与本地 AOT 修图保持一致参数
    config = json.dumps({
        "render": {
            "font_scale_factor": 3.0,
            "line_spacing_ratio": 1.3,
            "text_padding_ratio": 1.0,
            "font_size_offset": 15,
            "font_size": 100
        },
        "text_merge": {"enabled": True},
        "mask_expand_ratio": 0.05
    })
    try:
        r = _local_session.post(f"{LOCAL_TRANSLATOR}/review/render-direct/batch",
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


def _handle_token_expired(data, parent):
    """检测 401 过期，停止循环、清 token、回登录页"""
    global _running, _token
    if data.get("code") != 401:
        return False
    if not _running:
        return True  # 另一个循环已处理
    _log('token expired, returning to login')
    _running = False
    _token = None
    try:
        os.remove(TOKEN_FILE)
    except Exception as e:
        _log(f'token_expired remove file error: {e}')
    parent.root.after(0, parent._setup_login)
    return True


def api_submit_batch(task_id, task_type, results):
    """批量提交处理结果"""
    return api_post("/worker/submitBatch", {
        "taskId": task_id,
        "type": task_type,
        "results": results
    })


def ocr_loop(parent):
    """OCR 轮询线程 — 3并发 + 批量提交"""
    global _running, _stats
    OCR_WORKERS = 3
    from concurrent.futures import ThreadPoolExecutor, as_completed

    while _running:
        has_task = False
        try:
            data = api_get("/worker/poll", {"type": "ocr"})
            if _handle_token_expired(data, parent):
                break
            task = (data.get("data", {}) or {}).get("task") if data.get("code") == 200 else None
            _log(f'poll ocr: code={data.get("code")} task={bool(task)} pending={len(task.get("pendingImages",[])) if task else 0}')
            if task:
                has_task = True
                pending = task.get("pendingImages", [])
                total = task.get("totalImages", 0)
                processed = task.get("completedCount", 0) + task.get("failedCount", 0)
                task_id = task["taskId"]
                parent.root.after(0, lambda tid=task_id, t=total:
                    parent._show_ocr_row(tid, t))

                pool = ThreadPoolExecutor(max_workers=OCR_WORKERS)
                try:
                    for i in range(0, len(pending), OCR_WORKERS):
                        batch = pending[i:i + OCR_WORKERS]

                        # 并发 OCR（_get_image_bytes 内部已处理本地缓存+下载）
                        futures = {pool.submit(process_ocr_task, img): img for img in batch}
                        results = []
                        for f in as_completed(futures):
                            try:
                                results.append(f.result())
                            except Exception as e:
                                img = futures[f]
                                results.append({
                                    "imageId": img["imageId"], "ocrText": "", "ocrMetadata": None,
                                    "error": str(e)
                                })

                        # 批量提交
                        api_submit_batch(task_id, "ocr", results)

                        # 更新统计
                        for r in results:
                            err = r.get("error", "")
                            if err:
                                with _lock:
                                    _stats["errors"] += 1
                                    _error_log.insert(0, (time.strftime("%H:%M:%S"), r["imageId"], err))
                                    if len(_error_log) > 50:
                                        _error_log.pop()
                            else:
                                with _lock:
                                    _stats["ocr"] += 1

                        # 更新 GUI 进度
                        done = processed + i + len(batch)
                        parent.root.after(0, lambda p=done, t=total:
                            parent._update_ocr_row(p, t))
                finally:
                    pool.shutdown()

            else:
                parent.root.after(0, parent._hide_ocr_row)
        except requests.ConnectionError:
            parent.update_status("连接失败，重试中...")
        except Exception as e:
            _log(f"OCR_LOOP ERROR: {e}")
            with _lock:
                _stats["errors"] += 1

        if not has_task:
            time.sleep(POLL_INTERVAL)


def retouch_loop(parent):
    """修图轮询线程 — 2并发 + 批量提交"""
    global _running, _stats
    RETOUCH_WORKERS = 2
    from concurrent.futures import ThreadPoolExecutor, as_completed

    while _running:
        has_task = False
        try:
            data = api_get("/worker/poll", {"type": "retouch"})
            if _handle_token_expired(data, parent):
                break
            task = (data.get("data", {}) or {}).get("task") if data.get("code") == 200 else None
            _log(f'poll retouch: code={data.get("code")} task={bool(task)} pending={len(task.get("pendingImages",[])) if task else 0}')
            if task:
                has_task = True
                pending = task.get("pendingImages", [])
                total = task.get("totalImages", 0)
                processed = task.get("completedCount", 0) + task.get("failedCount", 0)
                task_id = task["taskId"]
                parent.root.after(0, lambda tid=task_id, t=total:
                    parent._show_retouch_row(tid, t))

                pool = ThreadPoolExecutor(max_workers=RETOUCH_WORKERS)
                try:
                    for i in range(0, len(pending), RETOUCH_WORKERS):
                        batch = pending[i:i + RETOUCH_WORKERS]

                        # 并发修图
                        futures = {pool.submit(process_retouch_task, img): img for img in batch}
                        results = []
                        for f in as_completed(futures):
                            try:
                                results.append(f.result())
                            except Exception as e:
                                img = futures[f]
                                results.append({
                                    "imageId": img["imageId"], "outputImage": "",
                                    "error": str(e)
                                })

                        # 批量提交
                        api_submit_batch(task_id, "retouch", results)

                        # 更新统计
                        for r in results:
                            err = r.get("error", "")
                            if err:
                                with _lock:
                                    _stats["errors"] += 1
                                    _error_log.insert(0, (time.strftime("%H:%M:%S"), r["imageId"], err))
                                    if len(_error_log) > 50:
                                        _error_log.pop()
                            else:
                                with _lock:
                                    _stats["retouch"] += 1

                        # 更新 GUI 进度
                        done = processed + i + len(batch)
                        parent.root.after(0, lambda p=done, t=total:
                            parent._update_retouch_row(p, t))
                finally:
                    pool.shutdown()

            else:
                parent.root.after(0, parent._hide_retouch_row)
        except requests.ConnectionError:
            parent.update_status("连接失败，重试中...")
        except Exception as e:
            _log(f"RETOUCH_LOOP ERROR: {e}")
            with _lock:
                _stats["errors"] += 1

        if not has_task:
            time.sleep(POLL_INTERVAL)


# ====== GUI ======
class WorkerApp:
    def __init__(self):
        self.error_text = None
        self.import_status = None
        self.root = tk.Tk()
        self.root.title("mangaGo Worker")
        self.root.geometry("420x200")
        self.root.resizable(False, False)
        self._setup_login()
        self.root.mainloop()

    def _import_images(self):
        folder = filedialog.askdirectory(title="选择漫画文件夹（包含章节子目录）")
        if not folder:
            return
        # 漫画名 = 选中的文件夹名
        comic_name = os.path.basename(folder)
        self.import_path.set(folder)
        dest_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 'manga-image-translator', 'downloads')
        def do_import():
            copied = 0
            errors = 0
            for chapter_dir in os.listdir(folder):
                chapter_path = os.path.join(folder, chapter_dir)
                if not os.path.isdir(chapter_path):
                    continue
                for f in os.listdir(chapter_path):
                    if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp')):
                        src = os.path.join(chapter_path, f)
                        dst = os.path.join(dest_root, comic_name, chapter_dir, f)
                        try:
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copy2(src, dst)
                            copied += 1
                        except Exception as e:
                            _log(f'import_copy_error: {src} → {dst}: {e}')
                            errors += 1
            self.root.after(0, lambda: self.import_status.config(
                text=f"已导入 {copied} 张" + (f"，{errors} 失败" if errors else ""), foreground="green" if not errors else "orange"))
        self.import_status.config(text="导入中...", foreground="blue")
        threading.Thread(target=do_import, daemon=True).start()

    def _clear_import(self):
        """清空导入的图片"""
        folder = self.import_path.get()
        if not folder:
            return
        comic_name = os.path.basename(folder)
        dest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'manga-image-translator', 'downloads', comic_name)
        if os.path.exists(dest):
            try:
                shutil.rmtree(dest)
            except Exception as e:
                _log(f'clear_import_error: {e}')
        self.import_path.set("")
        self.import_status.config(text="已清空", foreground="gray")

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

    def _try_refresh_token():
        try:
            with open(TOKEN_FILE, 'r') as f:
                import json as _j; saved = _j.loads(f.read())
            rt = saved.get('r', '')
            if not rt: return None
            r = requests.post(f"{API_BASE}/auth/refresh", json={"refresh_token": rt}, timeout=15)
            d = r.json()
            if d.get("code") == 200:
                at = d.get("data",{}).get("tokens",{}).get("access_token",{}).get("token")
                new_rt = d.get("data",{}).get("tokens",{}).get("refresh_token",{}).get("token")
                with open(TOKEN_FILE, 'w') as f:
                    f.write('{"a":"' + at + '","r":"' + new_rt + '"}')
                return at
        except: pass
        return None

    def _try_cached_token(self):
        global _token, _user_info
        try:
            with open(TOKEN_FILE, 'r') as f:
                tk = f.read().strip()
            if tk:
                r = requests.get(f"{API_BASE}/worker/poll", params={"type": "ocr"},
                    headers={"Authorization": f"Bearer {tk}"}, timeout=10)
                data = r.json()
                if data.get('code') != 401:
                    _token = tk
                    self.root.after(0, self._setup_main)
                    return
        except Exception as e:
            _log(f'cached_token: {e}')
        self.root.after(0, lambda: self.login_status.config(text="请登录"))

    def _start_web_login(self):
        global _token, _user_info
        self.login_status.config(text="等待浏览器认证...", foreground="blue")
        self.login_btn.config(state="disabled")
        import uuid, webbrowser, urllib.request, time
        worker_id = str(uuid.uuid4())[:8]
        _log(f'worker_id: {worker_id}')
        self.login_status.config(text=f"连接码: {worker_id}")
        webbrowser.open(f"{API_BASE.replace('/api', '')}/admin?connect={worker_id}")
        parent = self
        def poll_token():
            global _token
            for i in range(120):
                time.sleep(2)
                try:
                    r = requests.get(f"{API_BASE}/worker/token",
                        params={"id": worker_id},
                        headers={'User-Agent': 'mangaGo-Worker/1.0'},
                        timeout=10)
                    data = r.json()
                    tk = (data.get('data') or {}).get('token')
                    if tk:
                        _log(f'poll_token: got token len={len(tk)}')
                        _token = tk
                        try:
                            with open(TOKEN_FILE, 'w') as f:
                                f.write(tk)
                        except Exception as e:
                            _log(f'poll_token write error: {e}')
                        parent.root.after(0, parent._setup_main)
                        return
                except Exception as e:
                    _log(f'poll_token: {e}')
            self.root.after(0, lambda: parent.login_status.config(text="连接超时，请重试"))
            self.root.after(0, lambda: parent.login_btn.config(state="normal"))
        threading.Thread(target=poll_token, daemon=True).start()

    def _open_downloads(self):
        """打开 downloads 目录"""
        downloads = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 'manga-image-translator', 'downloads')
        os.makedirs(downloads, exist_ok=True)
        os.startfile(downloads)

    def _show_ocr_row(self, task_id, total):
        self.ocr_task_id_label.config(text=task_id[:12])
        self.ocr_progress_bar.config(maximum=total, value=0)
        self.ocr_progress_text.config(text=f"0/{total}")
        self.ocr_row_visible = True
        self.update_status("处理中")

    def _update_ocr_row(self, current, total):
        self.ocr_progress_bar.config(value=current)
        self.ocr_progress_text.config(text=f"{current}/{total}")

    def _hide_ocr_row(self):
        self.ocr_task_id_label.config(text="--")
        self.ocr_progress_bar.config(value=0)
        self.ocr_progress_text.config(text="--")
        self.ocr_row_visible = False
        if not self.retouch_row_visible:
            self.update_status("空闲中")

    def _show_retouch_row(self, task_id, total):
        self.retouch_task_id_label.config(text=task_id[:12])
        self.retouch_progress_bar.config(maximum=total, value=0)
        self.retouch_progress_text.config(text=f"0/{total}")
        self.retouch_row_visible = True
        self.update_status("处理中")

    def _update_retouch_row(self, current, total):
        self.retouch_progress_bar.config(value=current)
        self.retouch_progress_text.config(text=f"{current}/{total}")

    def _hide_retouch_row(self):
        self.retouch_task_id_label.config(text="--")
        self.retouch_progress_bar.config(value=0)
        self.retouch_progress_text.config(text="--")
        self.retouch_row_visible = False
        if not self.ocr_row_visible:
            self.update_status("空闲中")

    def _setup_main(self):
        for w in self.root.winfo_children():
            w.destroy()
        self.root.title("mangaGo Worker")
        self.root.geometry("500x450")
        self.root.minsize(420, 380)
        self.root.resizable(True, True)

        # ====== 顶部标题栏 ======
        top_bar = tk.Frame(self.root, bg="#303133", height=36)
        top_bar.pack(fill="x")
        top_bar.pack_propagate(False)
        tk.Label(top_bar, text="  mangaGo Worker", bg="#303133", fg="white",
                 font=("", 12, "bold")).pack(side="left")
        self.top_status = tk.Label(top_bar, text="就绪  ", bg="#303133", fg="#c0c4cc",
                                   font=("", 10))
        self.top_status.pack(side="right")

        # ====== 本地服务状态条 ======
        self.service_bar = tk.Label(self.root, text="本地服务启动中...",
                                    bg="#409eff", fg="white", font=("", 10),
                                    anchor="center", pady=3)
        self.service_bar.pack(fill="x", padx=10, pady=(4, 0))

        # ====== 本地图片卡片 ======
        import_card = ttk.LabelFrame(self.root, text="本地图片")
        import_card.pack(fill="x", padx=10, pady=(8, 0))
        row = ttk.Frame(import_card)
        row.pack(fill="x", padx=5, pady=5)
        self.import_path = tk.StringVar()
        ttk.Entry(row, textvariable=self.import_path, state="readonly").pack(
            side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(row, text="浏览", command=self._import_images, width=6).pack(side="left", padx=2)
        ttk.Button(row, text="清空", command=self._clear_import, width=6).pack(side="left", padx=2)
        ttk.Button(row, text="📂", command=self._open_downloads, width=4).pack(side="left", padx=2)
        self.import_status = ttk.Label(import_card, text="", foreground="gray")
        self.import_status.pack(anchor="w", padx=5, pady=(0, 5))

        # ====== 任务卡片 ======
        self.task_card = ttk.LabelFrame(self.root, text="任务")
        self.task_card.pack(fill="x", padx=10, pady=(4, 0))
        self.task_card_visible = True
        self.ocr_row_visible = False
        self.retouch_row_visible = False

        self.ocr_row = ttk.Frame(self.task_card)
        ttk.Label(self.ocr_row, text="OCR", font=("", 10), width=6).pack(side="left")
        self.ocr_task_id_label = ttk.Label(self.ocr_row, text="--", foreground="gray", font=("", 9))
        self.ocr_task_id_label.pack(side="left", padx=5)
        self.ocr_progress_text = ttk.Label(self.ocr_row, text="--", width=8)
        self.ocr_progress_text.pack(side="right")
        self.ocr_progress_bar = ttk.Progressbar(self.ocr_row, mode="determinate", length=160)
        self.ocr_progress_bar.pack(side="right", padx=5)
        self.ocr_row.pack(fill="x", padx=5, pady=1)

        self.retouch_row = ttk.Frame(self.task_card)
        ttk.Label(self.retouch_row, text="修图", font=("", 10), width=6).pack(side="left")
        self.retouch_task_id_label = ttk.Label(self.retouch_row, text="--", foreground="gray", font=("", 9))
        self.retouch_task_id_label.pack(side="left", padx=5)
        self.retouch_progress_text = ttk.Label(self.retouch_row, text="--", width=8)
        self.retouch_progress_text.pack(side="right")
        self.retouch_progress_bar = ttk.Progressbar(self.retouch_row, mode="determinate", length=160)
        self.retouch_progress_bar.pack(side="right", padx=5)
        self.retouch_row.pack(fill="x", padx=5, pady=1)

        # ====== 错误日志卡片 ======
        err_frame = ttk.LabelFrame(self.root, text="错误日志（最近50条）")
        err_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.error_text = tk.Text(err_frame, height=6, font=("Consolas", 8), state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(err_frame, command=self.error_text.yview)
        self.error_text.configure(yscrollcommand=scrollbar.set)
        self.error_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        threading.Thread(target=self._auto_start, daemon=True).start()

    def _start_translator(self):
        import subprocess
        venv_pyw = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'manga-image-translator', 'venv', 'Scripts', 'pythonw.exe')
        env = os.environ.copy()
        env['PYTHONW_SUPPRESS_STDERR'] = '1'
        p = subprocess.Popen([venv_pyw, TRANSLATOR_SCRIPT, '--port', '8001', '--use-gpu', '--models-ttl=3600'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    def _update_translator_status(self, text, color="#409eff"):
        self.root.after(0, lambda: self.service_bar.config(text=text, bg=color))

    def _auto_start(self):
        _log('auto_start: begin')
        self._update_translator_status("本地服务启动中...")
        self._start_translator()
        translator_ready = False
        for i in range(30):
            time.sleep(2)
            try:
                import urllib.request
                urllib.request.urlopen("http://localhost:8001/docs", timeout=3)
                _log(f'auto_start: translator ready at {i*2}s')
                self._update_translator_status("本地服务已就绪", "#67c23a")
                translator_ready = True
                break
            except Exception as e:
                _log(f'auto_start: wait {i*2}s err={e}')
                self._update_translator_status(f"等待本地服务 ({i*2}s)...", "#e6a23c")
        if not translator_ready:
            self._update_translator_status("本地服务启动失败，请检查翻译环境", "#f56c6c")
            _log('auto_start: translator failed to start after 60s')
            return
        global _running
        _running = True
        _log('auto_start: done, starting ocr_loop + retouch_loop')
        self.update_status("空闲中")
        threading.Thread(target=ocr_loop, args=(self,), daemon=True).start()
        threading.Thread(target=retouch_loop, args=(self,), daemon=True).start()
        threading.Thread(target=self._log_updater, daemon=True).start()
        threading.Thread(target=self._translator_watchdog, daemon=True).start()

    def _translator_watchdog(self):
        """后台监控本地服务健康，挂了自动重启"""
        import urllib.request
        while _running:
            time.sleep(30)
            try:
                r = urllib.request.urlopen("http://localhost:8001/docs", timeout=5)
                if r.status != 200:
                    raise Exception(f"status {r.status}")
            except Exception as e:
                _log(f"watchdog: translator down ({e}), restarting...")
                self._update_translator_status("本地服务异常，重启中...", "#e6a23c")
                self._start_translator()
                time.sleep(10)
                self._update_translator_status("本地服务已就绪", "#67c23a")

    def update_status(self, msg):
        self.root.after(0, lambda: self.top_status.config(text=msg + "  "))

    def _log_updater(self):
        while _running:
            self.root.after(0, self._refresh_errors)
            time.sleep(3)

    def _refresh_errors(self):
        if not self.error_text:
            return
        with _lock:
            snapshot = list(_error_log[:20])
        self.error_text.configure(state="normal")
        self.error_text.delete(1.0, "end")
        for t, iid, err in snapshot:
            self.error_text.insert("end", f"[{t}] img#{iid} {err[:80]}\n")
        self.error_text.configure(state="disabled")


if __name__ == "__main__":
    # 单实例锁
    import socket
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(('127.0.0.1', 19527))
    except socket.error:
        import tkinter.messagebox as mb
        mb.showwarning("mangaGo Worker", "Worker 已在运行中")
        sys.exit(0)
    WorkerApp()
