#!/bin/bash
# easy_ontology 内置 QLever 看守（单容器模式，由 entrypoint.sh 后台常驻拉起）
# 与 watch-abox.sh（外置容器版）逻辑同源，差异仅路径：
#   ABox 数据：/app/data（abox-pointer.txt + workspaces/*/files/abox.nt）
#   索引目录：/app/data/qlever-index（随 data 卷持久化，与 marker 同生命周期）
#   二进制：  /qlever-bin/*（依赖库 /qlever-libs/*，仅本进程组生效）
# 逻辑：每 5s 读指针（内容=激活空间 abox.nt 相对路径），比较值 = "指针|目标 stat %y"。
#   指针变化（切空间）或 mtime 变化（物化/上传）-> 杀服务 -> qlever-index 重建 -> 重启。
#   指针缺失 / 目标不存在 / 目标为空 -> 用空文件重建 = 清除索引（0 三元组）。
# 启动自愈：全新卷（索引文件不存在）先按当前指针目标（或空文件）建一次再起服务，
#   保证物化路线在未生成 ABox 前也能返回空结果而不是连接拒绝。
# 陷阱：绝不能用 [ -nt ]（挂载亚秒 mtime 可能被截掉，死循环）；cwd 必须在索引目录。
set -u
ABOX=/app/data
IDX=/app/data/qlever-index
NAME=abox
PORT=7001
cd "$IDX" || exit 1
LOG=$IDX/watch.log
MARKER=$IDX/.abox-indexed-state
EMPTY_NT=$IDX/.empty-abox.nt
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

pointer() { cat "$ABOX/abox-pointer.txt" 2>/dev/null | tr -d '\r\n\t '; }

# 当前比较值：指针|目标mtime（指针空 -> no-pointer；目标 stat 不到 -> missing）
state() {
  local p t
  p=$(pointer)
  [ -n "$p" ] || { echo "no-pointer|none"; return; }
  t=$(stat -c '%y' "/app/data/$p" 2>/dev/null) || { echo "$p|missing"; return; }
  echo "$p|$t"
}

# 选源：指针目标存在且非空用它，否则空文件（=清索引）
pick_src() {
  local p
  p=$(pointer)
  if [ -n "$p" ] && [ -s "/app/data/$p" ]; then
    echo "/app/data/$p"
  else
    : > "$EMPTY_NT"
    echo "$EMPTY_NT"
  fi
}

idx_exists() { ls "$IDX/$NAME.index."* >/dev/null 2>&1; }

# 基线：容器启动以当前状态起算（rename 不改 mtime → 迁移后首启动零重建）
if [ ! -f "$MARKER" ]; then
  state > "$MARKER"
fi

# 启动自愈：全新卷没有索引文件 -> 先建一次（哪怕空），保证服务能起来
if ! idx_exists; then
  log "索引文件缺失（全新卷？），先构建初始索引"
  env LD_LIBRARY_PATH=/qlever-libs /qlever-bin/qlever-index -i "$NAME" -f "$(pick_src)" >> "$LOG" 2>&1 \
    || log "初始索引构建失败（后续变更会重试）"
fi

env LD_LIBRARY_PATH=/qlever-libs /qlever-bin/qlever-server -i "$NAME" -p "$PORT" &
log "服务已启动（$NAME 索引，初始状态 $(cat "$MARKER")）"

while true; do
  sleep 5
  cur=$(state)
  if [ "$cur" != "$(cat "$MARKER" 2>/dev/null)" ]; then
    echo "$cur" > "$MARKER"
    src=$(pick_src)
    if [ "$src" = "$EMPTY_NT" ]; then
      log "abox 缺失或为空（$cur），清除索引（0 三元组）"
    else
      log "abox 变更（$cur），重建索引"
    fi
    pkill -x qlever-server || true
    sleep 1
    if env LD_LIBRARY_PATH=/qlever-libs /qlever-bin/qlever-index -i "$NAME" -f "$src" >> "$LOG" 2>&1; then
      env LD_LIBRARY_PATH=/qlever-libs /qlever-bin/qlever-server -i "$NAME" -p "$PORT" &
      log "重建完成，服务已重启"
    else
      log "重建失败，仍用旧索引重启服务"
      env LD_LIBRARY_PATH=/qlever-libs /qlever-bin/qlever-server -i "$NAME" -p "$PORT" &
    fi
  fi
done
