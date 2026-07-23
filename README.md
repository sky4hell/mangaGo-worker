# mangaGo Worker

漫画平台客户端程序 — 用本机 GPU 处理云端的 OCR 和修图任务。

## 工作流程

```
① 打开 Worker → 登录管理后台账号
② 点击"启动处理"
③ 自动轮询云端 → 下载图片 → 本地 GPU 处理 → 上传结果
④ 可最小化到后台静默运行
```

## 依赖

- **Python 3.10+** + pip
- **manga-image-translator**（翻译服务，需在 `../manga-image-translator/` 启动）

## 快速开始

```bash
# 首次使用：安装依赖
install.bat

# 日常使用：双击启动
start.bat

# 或命令行
python worker.py

# 连接本地测试服务器（非生产环境）
set MANGA_API=http://localhost:5000/api
python worker.py
```

## 连接云端

Worker 默认连接 `https://zalomanga.com/api`。

本地测试时设置环境变量：
```bash
set MANGA_API=http://localhost:5000/api
```

## 打包分发

```bash
# 与 manga-image-translator 目录平级
mangaGo/
├── mangaGo-worker/       # 本目录
└── manga-image-translator/   # 翻译服务 + venv + models

# 压缩即可分发
tar -czf mangaGo-worker.tar.gz mangaGo/
```
