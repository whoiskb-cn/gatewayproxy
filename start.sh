#!/bin/sh

# 启动音乐下载后端服务 (由子目录 music 运行)
cd /app/music
./music-dl web --port 8090 --no-browser &

# 等待 Go 服务启动
sleep 2

# 启动主管理面板服务
cd /app
python main.py
