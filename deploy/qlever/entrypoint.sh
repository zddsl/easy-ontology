#!/bin/sh
# easy_ontology 单容器入口：后台拉起内置 QLever 看守，前台 exec uvicorn（成为 PID1）。
# uvicorn 退出（容器停止）时内核连带回收看守与 qlever-server。
set -u
mkdir -p /app/data/qlever-index
bash /app/deploy/qlever/watch-builtin.sh >/dev/null 2>&1 &
exec uvicorn backend.main:app --host 0.0.0.0 --port 8000
