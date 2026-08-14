"""
本地图片服务 — 优先本地、零延迟加载
监听 localhost:7003，从 comic-backend/downloads/ 读取图片
"""
import os
import re
import sys
import tempfile
import requests
import logging
from io import BytesIO
from PIL import Image

# 日志配置必须在 from flask import Flask 之前（Flask 会覆盖 logging 配置）
_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'image_server.log')
logging.basicConfig(filename=_LOG_FILE, level=logging.INFO,
                    format='%(asctime)s %(message)s', filemode='a')


def _log(msg):
    logging.info(msg)
    print(msg)


from flask import Flask, request, send_file, jsonify

app = Flask(__name__)

# 图片根目录：mangaGo-worker/../comic-backend/downloads/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE_ROOT = os.path.join(BASE_DIR, 'comic-backend', 'downloads')
# 缩略图缓存目录：与 downloads 平级，不污染原图目录
THUMB_ROOT = os.path.join(os.path.dirname(IMAGE_ROOT), 'thumb_cache')
# 远程 API 根地址（与 worker.py 共用 MANGA_API 环境变量）
API_BASE = os.environ.get("MANGA_API", "https://zalomanga.com/api")
# 章节目录遍历缓存：comic_dir -> {去前缀章节名: [目录名, ...]}
_DIR_CACHE = {}

if not os.path.isdir(IMAGE_ROOT):
    _log(f'[图片服务] 错误：图片目录不存在 {IMAGE_ROOT}')


@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = '*'
    return response


@app.route('/health')
def health():
    return jsonify({'ok': True})


def _resolve_path(relative_path):
    """解析真实文件路径，兼容本地章节文件夹的 章节N_ 前缀"""
    file_path = os.path.join(IMAGE_ROOT, relative_path)
    if os.path.isfile(file_path):
        return file_path

    parts = relative_path.replace('\\', '/').split('/')
    if len(parts) < 3:
        return None

    comic_dir = os.path.join(IMAGE_ROOT, *parts[:-2])
    chapter_name = parts[-2]
    file_name = parts[-1]

    if not os.path.isdir(comic_dir):
        return None

    mapping = _DIR_CACHE.get(comic_dir)
    if mapping is None:
        mapping = {}
        for entry in os.listdir(comic_dir):
            # 去掉 章节N_ 前缀后归组；同一章节可能同时存在带前缀/不带前缀两个目录，逐个试文件
            mapping.setdefault(re.sub(r'^章节\d+_', '', entry), []).append(entry)
        _DIR_CACHE[comic_dir] = mapping

    for entry in mapping.get(chapter_name, []):
        candidate = os.path.join(comic_dir, entry, file_name)
        if os.path.isfile(candidate):
            return candidate

    return None


@app.route('/images/<path:relative_path>')
def serve_image(relative_path):
    file_path = _resolve_path(relative_path)

    if not file_path:
        # 本地没有 → 从远程服务器获取
        remote_url = f'{API_BASE}/comicCrawlImage/images/{relative_path}'
        try:
            thumb = request.args.get('thumb')
            params = {'thumb': thumb} if thumb else {}
            resp = requests.get(remote_url, timeout=30, params=params)
            if resp.status_code == 200:
                # 原图存本地，下次直接命中
                if not thumb:
                    save_path = os.path.join(IMAGE_ROOT, relative_path)
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    with open(save_path, 'wb') as f:
                        f.write(resp.content)
                    _log(f'[proxy+save] {relative_path} ({len(resp.content)} bytes)')
                else:
                    _log(f'[proxy] {relative_path} ({len(resp.content)} bytes)')
                return send_file(BytesIO(resp.content), mimetype='image/webp')
        except Exception as e:
            _log(f'[proxy error] {relative_path}: {e}')
        _log(f'[404] {relative_path}')
        return jsonify({'error': '文件不存在'}), 404

    _log(f'[200] {relative_path}  ({os.path.getsize(file_path)} bytes)' + (' [thumb]' if request.args.get('thumb') else ''))

    # ?thumb=200 缩略图
    thumb = request.args.get('thumb')
    if thumb:
        try:
            w = int(thumb)
            cache_path = os.path.join(THUMB_ROOT, str(w), relative_path)
            if os.path.isfile(cache_path):
                return send_file(cache_path, mimetype='image/webp')
            img = Image.open(file_path)
            ratio = w / img.width
            h = int(img.height * ratio)
            img = img.resize((w, h), Image.LANCZOS)
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            # 先写唯一临时文件再原子替换，避免并发写半个文件
            fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(cache_path), suffix='.tmp')
            os.close(fd)
            img.save(tmp_path, format='WEBP', quality=80)
            img.close()
            os.replace(tmp_path, cache_path)
            return send_file(cache_path, mimetype='image/webp')
        except Exception as e:
            _log(f'[thumb] {file_path}: {e}')
            # 缩略图失败回退原图

    return send_file(file_path)


if __name__ == '__main__':
    _log(f'[图片服务] 根目录: {IMAGE_ROOT}')
    _log(f'[图片服务] 启动在 http://localhost:7003')
    _log(f'[图片服务] 访问 /log 查看请求日志，/health 探活')
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.WARNING)
    app.run(host='127.0.0.1', port=7003, debug=False)
