#!/bin/bash
# 开发环境外置 QLever 容器重建脚本（正式镜像不需要它——Dockerfile 已按 digest 内置 QLever）。
#
# 什么时候用：qlever 容器被删/换机器/引擎重装后，跑本脚本原样重建。
# 镜像按 digest 钉死（与 Dockerfile 内置路线同一份），重建永不漂移——
# 想升级 QLever 时：先改下面的 QLEVER_IMAGE 为新 digest → 重建 →
# 再跑同目录 regression-limit.sh 对拍回归，PASS 才算升级成功。
#
# 注意：原容器没设自启策略（引擎重启要手动 docker start qlever），
# 重建时顺手加上 --restart unless-stopped，免得每次手动拉。
set -eu

# 可按环境覆盖：MATERIALIZE_DIR=... DATA_DIR=... bash run-dev-container.sh
MATERIALIZE_DIR="${MATERIALIZE_DIR:-D:/LLM/Ontology/materialize}"
DATA_DIR="${DATA_DIR:-D:/LLM/Ontology/easy_ontology/data}"
QLEVER_IMAGE="adfreiburg/qlever@sha256:119355a81be22d3fb5d61d2236986bd2c6351c5c3fc82c0b2b293a7e08c83724"

if docker ps -a --format '{{.Names}}' | grep -qx qlever; then
  if [ "${1:-}" = "--force" ]; then
    echo "移除旧容器..."
    docker rm -f qlever
  else
    echo "容器 qlever 已存在；确认重建请用: bash run-dev-container.sh --force" >&2
    exit 1
  fi
fi

for d in "$MATERIALIZE_DIR" "$DATA_DIR"; do
  if [ ! -d "$d" ]; then
    echo "目录不存在：$d（用环境变量 MATERIALIZE_DIR / DATA_DIR 指定）" >&2
    exit 1
  fi
done

docker run -d --name qlever \
  -p 7001:7001 \
  -v "$MATERIALIZE_DIR":/data \
  -v "$DATA_DIR":/abox:ro \
  --entrypoint bash \
  --restart unless-stopped \
  "$QLEVER_IMAGE" /data/watch-abox.sh

echo "== 完成，确认服务参数（应看到 -j 3 与 60s 超时）："
sleep 3
docker exec qlever bash -c 'cat /proc/$(pidof qlever-server)/cmdline | tr "\0" " "; echo' || true
echo "== 记得跑一遍回归：bash $(dirname "$0")/regression-limit.sh"
