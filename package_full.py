"""
mangaGo 完整版打包 — 开箱即用（Worker + 翻译服务 + 模型）
输出: dist/mangaGo-full.zip (~7GB)
"""
import os, sys, shutil, subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(ROOT)
DIST = os.path.join(ROOT, 'dist')
TRANSLATOR = os.path.join(PARENT, 'manga-image-translator')
FULL = os.path.join(DIST, 'mangaGo-full')


def step(msg):
    print(f'\n  [{msg}]')


def main():
    if os.path.exists(DIST):
        shutil.rmtree(DIST)
    os.makedirs(FULL)

    # 1. 翻译服务（直接复制，不压缩）
    step('复制翻译服务（venv + 模型，较慢...）')
    dst_translator = os.path.join(FULL, 'manga-image-translator')
    for item in os.listdir(TRANSLATOR):
        if item in ('__pycache__', '.git', 'logs', 'log', 'result', 'downloads', 'ps_images'):
            continue
        src_path = os.path.join(TRANSLATOR, item)
        dst_path = os.path.join(dst_translator, item)
        print(f'    copying {item}...')
        if os.path.isdir(src_path):
            shutil.copytree(src_path, dst_path, ignore=shutil.ignore_patterns('__pycache__', '.git', '*.log'))
        else:
            shutil.copy2(src_path, dst_path)

    # 2. Worker 程序
    step('复制 Worker')
    dst_worker = os.path.join(FULL, 'mangaGo-worker')
    os.makedirs(dst_worker)
    for f in ['worker.py', 'requirements.txt', 'token.txt']:
        src = os.path.join(ROOT, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst_worker, f))

    # 3. 一键启动脚本
    step('生成启动脚本')
    with open(os.path.join(FULL, '启动.bat'), 'w', encoding='utf-8') as f:
        f.write('@echo off\r\n')
        f.write('chcp 65001 >nul\r\n')
        f.write('title mangaGo\r\n')
        f.write('echo =========================================\r\n')
        f.write('echo   mangaGo 启动中...\r\n')
        f.write('echo =========================================\r\n')
        f.write('echo.\r\n')
        f.write('echo [1/2] 启动翻译服务 :8001...\r\n')
        f.write('start "mangaGo-Translator" "%~dp0manga-image-translator\\venv\\Scripts\\python.exe" "%~dp0manga-image-translator\\server\\main.py" --port 8001 --use-gpu --models-ttl=3600\r\n')
        f.write('echo 等待翻译服务就绪...\r\n')
        f.write('echo.\r\n')
        f.write('echo [2/2] 启动 Worker...\r\n')
        f.write('start "mangaGo-Worker" "%~dp0manga-image-translator\\venv\\Scripts\\python.exe" "%~dp0mangaGo-worker\\worker.py"\r\n')
        f.write('echo.\r\n')
        f.write('echo 启动完成！Worker 窗口将弹出。\r\n')
        f.write('pause\r\n')

    # 4. 打包（7z 更小）
    step('打包 7z（如果 7z 不可用则打包 zip）')
    print('    这可能需要 10-20 分钟...')
    seven_zip = '7z'
    zip_path = os.path.join(DIST, 'mangaGo-full')
    try:
        subprocess.run([seven_zip, 'a', '-mx=5', zip_path + '.7z', FULL],
                       check=True, timeout=3600)
        ext = '.7z'
    except Exception:
        print('    7z 不可用，改用 zip...')
        shutil.make_archive(zip_path, 'zip', DIST, 'mangaGo-full')
        ext = '.zip'

    size = os.path.getsize(zip_path + ext)
    print(f'\n  [OK] 打包完成: {zip_path}{ext} ({size / (1024**3):.1f} GB)')

    # 删除临时目录
    shutil.rmtree(FULL)


if __name__ == '__main__':
    main()
