"""
mangaGo Worker 打包脚本 — 生成绿色版
用量: python package.py
输出: dist/mangaGo-worker-portable.zip
"""
import os
import sys
import shutil
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, 'dist')
PORTABLE = os.path.join(DIST, 'mangaGo-worker-portable')


def step(msg):
    print(f'\n  [{msg}]')


def main():
    # 1. 清理
    if os.path.exists(DIST):
        shutil.rmtree(DIST)
    os.makedirs(PORTABLE)

    # 2. 复制 Worker
    step('复制 Worker 文件')
    for f in ['worker.py', 'start.bat', 'install.bat', 'README.md', 'requirements.txt']:
        src = os.path.join(ROOT, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(PORTABLE, f))

    # 3. 下载 embedded Python
    step('下载 embedded Python 3.11')
    py_url = 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip'
    py_zip = os.path.join(DIST, 'python-embed.zip')
    py_dir = os.path.join(PORTABLE, 'python')

    if not os.path.exists(py_zip):
        import urllib.request
        urllib.request.urlretrieve(py_url, py_zip)
    shutil.unpack_archive(py_zip, py_dir)

    # 4. 配置 embed Python (启用 pip)
    step('配置 embedded Python')
    pth_file = os.path.join(py_dir, 'python311._pth')
    if os.path.exists(pth_file):
        with open(pth_file, 'r') as f:
            content = f.read()
        content = content.replace('#import site', 'import site')
        # 加 Lib\site-packages 路径
        if 'Lib\\site-packages' not in content:
            content += '\nLib\\site-packages\n'
        with open(pth_file, 'w') as f:
            f.write(content)

    # 5. 安装 pip
    step('安装 pip')
    get_pip = os.path.join(DIST, 'get-pip.py')
    if not os.path.exists(get_pip):
        urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', get_pip)
    py_exe = os.path.join(py_dir, 'python.exe')
    subprocess.run([py_exe, get_pip, '--no-warn-script-location'], cwd=py_dir, check=True)

    # 6. 安装依赖
    step('安装 requests')
    subprocess.run([py_exe, '-m', 'pip', 'install', '--no-warn-script-location',
                    '-r', os.path.join(PORTABLE, 'requirements.txt')], cwd=py_dir, check=True)

    # 7. 写简单的启动脚本（用自带的 python）
    step('生成启动脚本')
    with open(os.path.join(PORTABLE, '启动.bat'), 'w', encoding='utf-8') as f:
        f.write('@echo off\r\n')
        f.write('chcp 65001 >nul\r\n')
        f.write('title mangaGo Worker\r\n')
        f.write('echo 启动 mangaGo Worker...\r\n')
        f.write('echo 请确保翻译服务 (localhost:8001) 已在运行\r\n')
        f.write('start "" "%~dp0python\\python.exe" "%~dp0worker.py"\r\n')

    # 8. 打包
    step('打包 zip')
    zip_path = os.path.join(DIST, 'mangaGo-worker-portable')
    shutil.make_archive(zip_path, 'zip', DIST, 'mangaGo-worker-portable')

    size_mb = os.path.getsize(zip_path + '.zip') / (1024 * 1024)
    print(f'\n  [OK] 打包完成: {zip_path}.zip ({size_mb:.1f} MB)')


if __name__ == '__main__':
    main()
