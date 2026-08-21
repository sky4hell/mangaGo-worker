"""
mangaGo Worker — 桌面程序
登录 → 轮询取任务 → 调用本地 AOT 修图管线 → 提交结果
"""
import copy
import os
import re
import shutil
import sys
import json
import time
import threading
import base64 as b64
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import requests
import subprocess
import ctypes
from PIL import Image, ImageTk
import pystray

# pythonw 无控制台时 sys.stdout/stderr 为 None，comic-backend 的 log_utils 会调用 sys.stderr.fileno() 崩溃
# 兜底：指向 devnull，让 logger 有地方写而不崩
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')

# ====== 日志（必须在其他 import 之前，否则 Flask/cozepy 会覆盖配置）======
import logging
_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'worker.log')
# 先清掉 root logger 已有的 handler
for h in logging.root.handlers[:]:
    logging.root.removeHandler(h)
logging.basicConfig(
    filename=_LOG_FILE,
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    filemode='a'
)
def _log(msg):
    logging.info(msg)
    print(msg)

# comic-backend 路径，以便 import 本地 AOT 渲染模块
_BACKEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'comic-backend')
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from local_retouch import call_local_retouch_aot_service, build_ocr_metadata, RETOUCH_CONFIG, build_retouch_config
from config import OCR_CONFIG, DEFAULT_DETECT_SIZE

# ====== 配置 ======
API_BASE = os.environ.get("MANGA_API", "https://zalomanga.com/api")
WORKER_API_BASE = os.environ.get("WORKER_API", "http://localhost:5001/api")
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
    r = _api_session.post(f"{WORKER_API_BASE}{path}", json=data, headers=headers, timeout=30)
    try:
        return r.json()
    except Exception as e:
        _log(f'api_post JSON error: {e} status={r.status_code}')
        return {"code": -1, "message": str(e)}


def api_get(path, params=None):
    headers = {"Authorization": f"Bearer {_token}"} if _token else {}
    _log(f'api_get {path} token_len={len(_token) if _token else 0}')
    r = _api_session.get(f"{WORKER_API_BASE}{path}", params=params, headers=headers, timeout=30)
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
    """优先本地文件，没有则云端下载。
    兼容章节目录命名差异：DB 可能存 '序章'，本地可能是 '章节1_序章'，
    通过 strip '章节X_' 前缀来匹配。"""
    fp = None
    if _LOCAL_DOWNLOADS and local_path:
        fp = os.path.join(_LOCAL_DOWNLOADS, local_path.replace('\\', '/'))
        if os.path.exists(fp):
            _log(f'local_hit: {local_path[:50]}')
            with open(fp, 'rb') as fh:
                data = fh.read()
            # 读时自愈：文件头被 \r\n(0x0D0A) 前缀污染 → 剥离并回写修复
            if data[:2] == b'\r\n':
                fixed = data[2:]
                try:
                    with open(fp, 'wb') as fh:
                        fh.write(fixed)
                    _log(f'fix_crlf_corruption: {local_path[:50]}')
                except Exception as e:
                    _log(f'fix_crlf_corruption_error: {e}')
                return fixed
            return data
        # exact match 失败 → 尝试 strip 章节X_ 前缀匹配
        parent = os.path.dirname(fp)
        fname = os.path.basename(fp)
        if os.path.isdir(parent):
            db_chapter = os.path.basename(parent)
            for d in os.listdir(parent):
                dpath = os.path.join(parent, d)
                if not os.path.isdir(dpath):
                    continue
                stripped = re.sub(r'^章节\d+_', '', d)
                if stripped == db_chapter:
                    candidate = os.path.join(dpath, fname)
                    if os.path.exists(candidate):
                        _log(f'local_hit_fuzzy: {local_path[:50]} -> {d}/{fname}')
                        with open(candidate, 'rb') as fh:
                            return fh.read()
                    break
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
    """本地图片优先 → OCR → 返回结果。合并按钮路径(dualOcr)跑两次OCR取字多/框多的结果"""
    try:
        img_bytes = _get_image_bytes(task['localPath'])
    except Exception as e:
        return {"imageId": task["imageId"], "ocrText": "", "ocrMetadata": None,
                "error": f"获取图片失败: {e}"}

    filename = os.path.basename(task["localPath"])
    ext = os.path.splitext(filename)[1].lower()
    _mime_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                 '.webp': 'image/webp', '.bmp': 'image/bmp', '.gif': 'image/gif'}
    content_type = _mime_map.get(ext, 'image/png')
    files = [("images", (filename, img_bytes, content_type))]
    detect_size = task.get('detectSize') or DEFAULT_DETECT_SIZE
    merge_gap = task.get('mergeGap')
    dual_ocr = bool(task.get('dualOcr'))
    ocr_model = task.get('ocrModel')
    text_threshold = task.get('textThreshold')
    box_threshold = task.get('boxThreshold')
    char_gap_tol = task.get('charGapTolerance')
    char_gap_tol2 = task.get('charGapTolerance2')

    def _build_cfg(invert):
        # 以 OCR_CONFIG 为基础，只覆盖 task 级别参数 + det_invert 极性
        ocr_cfg = copy.deepcopy(OCR_CONFIG)
        ocr_cfg['detector']['detection_size'] = detect_size
        if ocr_model:
            ocr_cfg['ocr']['ocr'] = ocr_model
        if merge_gap is not None:
            ocr_cfg['text_merge']['discard_connection_gap'] = merge_gap
        if text_threshold is not None:
            ocr_cfg['detector']['text_threshold'] = text_threshold
        if box_threshold is not None:
            ocr_cfg['detector']['box_threshold'] = box_threshold
        if char_gap_tol is not None:
            ocr_cfg['text_merge']['char_gap_tolerance'] = char_gap_tol
        if char_gap_tol2 is not None:
            ocr_cfg['text_merge']['char_gap_tolerance2'] = char_gap_tol2
        ocr_cfg['remove_watermark'] = True
        ocr_cfg['detector']['det_invert'] = invert
        return ocr_cfg

    def _run_once(invert):
        config = json.dumps(_build_cfg(invert))
        r = _local_session.post(f"{LOCAL_TRANSLATOR}/review/ocr/with-form/batch/json",
                          files=files,
                          data={"config": config, "save_to_db": "false"},
                          timeout=300)
        if r.status_code != 200:
            raise RuntimeError(f"OCR服务 HTTP {r.status_code}")
        resp_json = r.json()
        ocr_data = resp_json.get("data") if isinstance(resp_json, dict) else None
        if not ocr_data or not isinstance(ocr_data, dict):
            raise RuntimeError(f"OCR服务返回异常: data={type(ocr_data).__name__}")
        results = ocr_data.get("results", [])
        if not results:
            raise RuntimeError("OCR 无结果")
        r0 = results[0]
        if not r0 or not isinstance(r0, dict):
            raise RuntimeError(f"OCR结果异常: {type(r0).__name__}")
        blocks = r0.get("text_blocks", []) or []
        text = r0.get("ocr_text", "") or ""
        return text, blocks

    try:
        if dual_ocr:
            candidates = []
            for invert in (True, False):
                try:
                    candidates.append(_run_once(invert))
                except Exception as e:
                    _log(f'dual_ocr pass det_invert={invert} failed: {e}')
            if not candidates:
                return {"imageId": task["imageId"], "ocrText": "", "ocrMetadata": None,
                        "error": "两次OCR均失败"}
            # 取字多、文本框多的那个结果
            text, blocks = max(candidates, key=lambda x: (len(x[0]), len(x[1])))
        else:
            text, blocks = _run_once(True)
        return {"imageId": task["imageId"],
                "ocrText": text,
                "ocrMetadata": json.dumps(blocks)}
    except Exception as e:
        return {"imageId": task["imageId"], "ocrText": "", "ocrMetadata": None,
                "error": str(e)}


def process_retouch_batch(batch_tasks):
    """批量 AOT 修图：与后台管理"批量本地修图"走完全相同的管线"""
    results = []
    image_paths = []
    batch = []

    for task in batch_tasks:
        local_path = task.get('localPath', '')
        fp = os.path.join(_LOCAL_DOWNLOADS, local_path.replace('\\', '/'))
        if not os.path.exists(fp):
            try:
                _get_image_bytes(local_path)  # 下载到 _LOCAL_DOWNLOADS
            except Exception as e:
                results.append({"imageId": task["imageId"], "outputImage": "", "error": f"下载失败: {e}"})
                continue
        if os.path.exists(fp):
            image_paths.append(fp)
            batch.append(task)
        else:
            results.append({"imageId": task["imageId"], "outputImage": "", "error": "图片文件不存在"})

    if not batch:
        return results

    ocr_metadata = build_ocr_metadata(batch, False)
    retouch_config = build_retouch_config()
    aot_results = call_local_retouch_aot_service(image_paths, ocr_metadata, 1, retouch_config, batch)

    for i, r in enumerate(aot_results):
        if i < len(batch):
            img_id = batch[i]["imageId"]
            if r.get("status") == "success":
                results.append({"imageId": img_id, "outputImage": r.get("output_image", "") or ""})
            else:
                results.append({"imageId": img_id, "outputImage": "", "error": r.get("error", "修图失败")})

    return results


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
            _log(f'poll ocr: code={data.get("code")} task={bool(task)} taskId={task.get("taskId","-") if task else "-"} pending={len(task.get("pendingImages",[])) if task else 0}')
            if task:
                task_id = task.get("taskId")
                pending = task.get("pendingImages", [])
                total = task.get("totalImages", 0)
                if total == 0 or len(pending) == 0:
                    # 空任务或全部已处理，提交空批次触发服务端标记完成
                    api_submit_batch(task_id, "ocr", [])
                    time.sleep(2)
                    continue
                has_task = True
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
                        done = task.get("completedCount", 0) + task.get("failedCount", 0) + i + len(batch)
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
        else:
            time.sleep(1)  # 任务完成后短暂冷却，避免高频轮询


def retouch_loop(parent):
    """修图轮询线程 — AOT 批量修图"""
    global _running, _stats
    BATCH_SIZE = 2

    while _running:
        has_task = False
        try:
            data = api_get("/worker/poll", {"type": "retouch"})
            if _handle_token_expired(data, parent):
                break
            task = (data.get("data", {}) or {}).get("task") if data.get("code") == 200 else None
            _log(f'poll retouch: code={data.get("code")} task={bool(task)} taskId={task.get("taskId","-") if task else "-"} pending={len(task.get("pendingImages",[])) if task else 0}')
            if task:
                task_id = task.get("taskId")
                pending = task.get("pendingImages", [])
                total = task.get("totalImages", 0)
                if total == 0 or len(pending) == 0:
                    # 空任务或全部已处理，提交空批次触发服务端标记完成
                    api_submit_batch(task_id, "retouch", [])
                    time.sleep(2)
                    continue
                has_task = True
                parent.root.after(0, lambda tid=task_id, t=total:
                    parent._show_retouch_row(tid, t))

                for i in range(0, len(pending), BATCH_SIZE):
                    batch = pending[i:i + BATCH_SIZE]

                    results = process_retouch_batch(batch)
                    api_submit_batch(task_id, "retouch", results)

                    for r in results:
                        if r.get("error"):
                            with _lock:
                                _stats["errors"] += 1
                                _error_log.insert(0, (time.strftime("%H:%M:%S"), r["imageId"], r["error"]))
                                if len(_error_log) > 50:
                                    _error_log.pop()
                        else:
                            with _lock:
                                _stats["retouch"] += 1

                    done = task.get("completedCount", 0) + task.get("failedCount", 0) + i + len(batch)
                    parent.root.after(0, lambda p=done, t=total:
                        parent._update_retouch_row(p, t))

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
        else:
            time.sleep(1)  # 任务完成后短暂冷却，避免高频轮询


# ====== GUI ======
class WorkerApp:
    def __init__(self):
        self.error_text = None
        self.import_status = None
        self.tray_icon = None
        # 设置 AppUserModelID：否则 pythonw 启动时任务栏按钮会套用 pythonw.exe 图标，而不是窗口图标
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('mangaGo.Worker.1')
        except Exception as e:
            _log(f'设置 AppUserModelID 失败: {e}')
        self.root = tk.Tk()
        self.root.title("mangaGo Worker")
        self.root.geometry("420x200")
        self.root.resizable(False, False)
        self._set_icon()
        self._setup_tray()
        self._setup_login()
        # 点 X → 最小化到托盘，不退出
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.root.mainloop()
        # 真正退出（托盘菜单"退出"）→ 联动关闭翻译(8001)和图片(7003)服务
        self._stop_tray()
        self._kill_sibling_services()

    def _set_icon(self):
        """设置窗口图标（任务栏/左上角）"""
        base = os.path.dirname(os.path.abspath(__file__))
        ico_path = os.path.join(base, 'mangaGo.ico')
        icon_path = os.path.join(base, 'mangaGo.png')
        # 任务栏按钮图标：Windows 需要 .ico + iconbitmap
        if os.path.isfile(ico_path):
            try:
                self.root.iconbitmap(ico_path)
            except Exception as e:
                _log(f'设置窗口图标(ico)失败: {e}')
        # 左上角标题栏图标：iconphoto（PNG，更清晰）
        if os.path.isfile(icon_path):
            try:
                img = Image.open(icon_path)
                self._icon = ImageTk.PhotoImage(img)   # 存到 self，防止被 GC 回收图标消失
                self.root.iconphoto(True, self._icon)
            except Exception as e:
                _log(f'设置窗口图标(png)失败: {e}')

    def _hide_to_tray(self):
        """点窗口 X → 隐藏到托盘；托盘不可用时保持关闭"""
        if self.tray_icon is None:
            self._do_quit()
            return
        self.root.withdraw()

    def _restore_window(self):
        self.root.deiconify()
        self.root.lift()

    def _show_window(self, icon=None, item=None):
        self.root.after(0, self._restore_window)

    def _do_quit(self):
        self.root.destroy()

    def _quit(self, icon=None, item=None):
        """托盘菜单'退出' → 结束 mainloop，触发联动关闭"""
        self.root.after(0, self._do_quit)

    def _setup_tray(self):
        """创建系统托盘图标（右键：显示窗口 / 退出）"""
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mangaGo.png')
        if not os.path.isfile(icon_path):
            _log('托盘图标文件不存在，跳过托盘: ' + icon_path)
            return
        try:
            img = Image.open(icon_path)
            menu = pystray.Menu(
                pystray.MenuItem('显示窗口', self._show_window, default=True, visible=False),
                pystray.MenuItem('退出', self._quit),
            )
            self.tray_icon = pystray.Icon('mangaGo', img, 'mangaGo Worker', menu)
            self.tray_icon.run_detached()
        except Exception as e:
            _log(f'托盘创建失败: {e}')
            self.tray_icon = None

    def _stop_tray(self):
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception as e:
                _log(f'托盘停止失败: {e}')

    def _kill_sibling_services(self):
        """关闭翻译服务(8001)和图片服务(7003)，避免下次启动端口冲突"""
        for port in (8001, 7003):
            try:
                out = subprocess.run(
                    ['netstat', '-ano'],
                    capture_output=True, text=True, timeout=10,
                ).stdout
            except Exception as e:
                _log(f'kill_sibling netstat 失败 (port {port}): {e}')
                continue
            for line in out.splitlines():
                if f':{port} ' not in line or 'LISTENING' not in line:
                    continue
                parts = line.split()
                if not parts:
                    continue
                pid = parts[-1]
                try:
                    subprocess.run(
                        ['taskkill', '/F', '/T', '/PID', pid],
                        capture_output=True, text=True, timeout=10,
                    )
                    _log(f'已联动关闭服务 :{port} (PID {pid})')
                except Exception as e:
                    _log(f'kill_sibling taskkill 失败 (port {port}, pid {pid}): {e}')

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

    def _try_refresh_token(self, refresh_token):
        """用 refresh_token 换新 access_token，成功返回新 token，失败返回 None"""
        try:
            r = requests.post(f"{API_BASE}/auth/refresh", json={"refresh_token": refresh_token}, timeout=15)
            d = r.json()
            if d.get("code") == 200:
                at = d.get("data",{}).get("tokens",{}).get("access_token",{}).get("token")
                new_rt = d.get("data",{}).get("tokens",{}).get("refresh_token",{}).get("token")
                if at:
                    with open(TOKEN_FILE, 'w') as f:
                        f.write(json.dumps({"a": at, "r": new_rt}))
                    return at
        except Exception as e:
            _log(f'refresh_token: {e}')
        return None

    def _try_cached_token(self):
        global _token, _user_info
        try:
            with open(TOKEN_FILE, 'r') as f:
                raw = f.read().strip()
            if not raw:
                raise ValueError('empty token file')
            # 兼容两种格式：JSON {"a":"...", "r":"..."} 或 原始 JWT 字符串
            tk = raw
            rt = None
            if raw.startswith('{'):
                try:
                    saved = json.loads(raw)
                    tk = saved.get('a', '')
                    rt = saved.get('r', '')
                except Exception:
                    pass
            if tk:
                r = requests.get(f"{WORKER_API_BASE}/worker/poll", params={"type": "ocr"},
                    headers={"Authorization": f"Bearer {tk}"}, timeout=10)
                data = r.json()
                if data.get('code') == 401 and rt:
                    # access token 过期，尝试 refresh
                    _log('cached_token: access expired, trying refresh')
                    new_at = self._try_refresh_token(rt)
                    if new_at:
                        tk = new_at
                        r = requests.get(f"{WORKER_API_BASE}/worker/poll", params={"type": "ocr"},
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
                    r = requests.get(f"{WORKER_API_BASE}/worker/token",
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
        self.ocr_task_id_label.config(text=task_id)
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
        self.retouch_task_id_label.config(text=task_id)
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
        self.root.geometry("620x450")
        self.root.minsize(520, 380)
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
        self.ocr_task_id_label = ttk.Label(self.ocr_row, text="--", foreground="gray", font=("", 9), width=24)
        self.ocr_task_id_label.pack(side="left", padx=5)
        self.ocr_progress_text = ttk.Label(self.ocr_row, text="--", width=8)
        self.ocr_progress_text.pack(side="right")
        self.ocr_progress_bar = ttk.Progressbar(self.ocr_row, mode="determinate", length=260)
        self.ocr_progress_bar.pack(side="right", padx=5)
        self.ocr_row.pack(fill="x", padx=5, pady=1)

        self.retouch_row = ttk.Frame(self.task_card)
        ttk.Label(self.retouch_row, text="修图", font=("", 10), width=6).pack(side="left")
        self.retouch_task_id_label = ttk.Label(self.retouch_row, text="--", foreground="gray", font=("", 9), width=24)
        self.retouch_task_id_label.pack(side="left", padx=5)
        self.retouch_progress_text = ttk.Label(self.retouch_row, text="--", width=8)
        self.retouch_progress_text.pack(side="right")
        self.retouch_progress_bar = ttk.Progressbar(self.retouch_row, mode="determinate", length=260)
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

    _translator_process = None

    def _start_translator(self):
        import subprocess
        # kill 旧进程避免僵尸堆积
        if self._translator_process:
            try:
                self._translator_process.kill()
            except Exception:
                pass
        venv_pyw = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'manga-image-translator', 'venv', 'Scripts', 'pythonw.exe')
        # stderr 写入日志文件，方便排查启动失败
        translator_err = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'translator_stderr.log'), 'a')
        env = os.environ.copy()
        env['PYTHONW_SUPPRESS_STDERR'] = '1'
        self._translator_process = subprocess.Popen(
            [venv_pyw, TRANSLATOR_SCRIPT, '--port', '8001', '--use-gpu', '--models-ttl=3600'],
            stdout=translator_err, stderr=translator_err, env=env)

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
                # 等重启后验证
                restarted = False
                for i in range(10):
                    time.sleep(2)
                    try:
                        urllib.request.urlopen("http://localhost:8001/docs", timeout=3)
                        _log(f"watchdog: translator restarted after {i*2}s")
                        self._update_translator_status("本地服务已就绪", "#67c23a")
                        restarted = True
                        break
                    except Exception:
                        pass
                if not restarted:
                    _log("watchdog: translator failed to restart")
                    self._update_translator_status("本地服务重启失败", "#f56c6c")

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
