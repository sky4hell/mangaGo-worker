"""
本地图片服务 — 优先本地、零延迟加载
监听 localhost:5001，从 comic-backend/downloads/ 读取图片
"""
import os
import re
import sys
import requests
from flask import Flask, request, send_file, jsonify
from io import BytesIO
from PIL import Image

app = Flask(__name__)

# 图片根目录：mangaGo-worker/../comic-backend/downloads/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE_ROOT = os.path.join(BASE_DIR, 'comic-backend', 'downloads')

if not os.path.isdir(IMAGE_ROOT):
    print(f'[图片服务] 错误：图片目录不存在 {IMAGE_ROOT}')


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

    for entry in os.listdir(comic_dir):
        # 去掉 章节N_ 前缀后比较
        normalized = re.sub(r'^章节\d+_', '', entry)
        if normalized == chapter_name:
            candidate = os.path.join(comic_dir, entry, file_name)
            if os.path.isfile(candidate):
                return candidate

    return None


@app.route('/images/<path:relative_path>')
def serve_image(relative_path):
    file_path = _resolve_path(relative_path)

    if not file_path:
        # 本地没有 → 从远程服务器获取
        remote_url = f'https://zalomanga.com/api/comicCrawlImage/images/{relative_path}'
        try:
            thumb = request.args.get('thumb')
            params = {'thumb': thumb} if thumb else {}
            resp = requests.get(remote_url, timeout=30, params=params)
            if resp.status_code == 200:
                print(f'[proxy] {relative_path} ({len(resp.content)} bytes)')
                return send_file(BytesIO(resp.content), mimetype='image/webp')
        except Exception as e:
            print(f'[proxy error] {relative_path}: {e}')
        print(f'[404] {relative_path}')
        return jsonify({'error': '文件不存在'}), 404

    print(f'[200] {relative_path}  ({os.path.getsize(file_path)} bytes)' + (' [thumb]' if request.args.get('thumb') else ''))

    # ?thumb=200 缩略图
    thumb = request.args.get('thumb')
    if thumb:
        try:
            w = int(thumb)
            img = Image.open(file_path)
            ratio = w / img.width
            h = int(img.height * ratio)
            img = img.resize((w, h), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format='WEBP', quality=80)
            buf.seek(0)
            img.close()
            return send_file(buf, mimetype='image/webp')
        except Exception as e:
            print(f'[thumb] {file_path}: {e}')
            # 缩略图失败回退原图

    return send_file(file_path)


if __name__ == '__main__':
    print(f'[图片服务] 根目录: {IMAGE_ROOT}')
    print(f'[图片服务] 启动在 http://localhost:5001')
    print(f'[图片服务] 访问 /log 查看请求日志，/health 探活')
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.WARNING)
    app.run(host='127.0.0.1', port=5001, debug=False)
