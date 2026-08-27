#!/bin/bash
# easy_ontology x QLever 看守脚本 v2：多工作空间指针版（qlever 容器内运行）
# 挂载：easy_ontology/data -> /abox:ro（整棵 data），materialize -> /data（索引与日志）
# 逻辑：每 5s 读 /abox/abox-pointer.txt（内容=激活空间 abox.nt 相对路径，如
#   workspaces/HD_SAAS/files/abox.nt），比较值 = "指针内容|目标 stat %y 全精度时间戳字符串"。
#   指针变化（切空间）或目标 mtime 变化（物化/上传）-> 杀服务 -> qlever-index 重建 -> 重启。
#   指针缺失 / 目标不存在 / 目标为空 -> 用空文件重建 = 清除索引（0 三元组）。
# 陷阱：绝不能用 [ -nt ]（Windows 挂载亚秒 mtime 会被截掉，死循环）；cwd 必须 /data。
cd /data || exit 1
LOG=/data/watch.log
MARKER=/data/.abox-indexed-state      # 新文件名！旧 .abox-indexed-at 语义不同，绝不复用
EMPTY_NT=/data/.empty-abox.nt
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

pointer() { cat /abox/abox-pointer.txt 2>/dev/null | tr -d '\r\n\t '; }

# 当前比较值：指针|目标mtime（指针空 -> no-pointer；目标 stat 不到 -> missing）
state() {
  local p t
  p=$(pointer)
  [ -n "$p" ] || { echo "no-pointer|none"; return; }
  t=$(stat -c '%y' "/abox/$p" 2>/dev/null) || { echo "$p|missing"; return; }
  echo "$p|$t"
}

# 基线：容器启动以当前状态起算（rename 不改 mtime → 迁移后首启动零重建）
if [ ! -f "$MARKER" ]; then
  state > "$MARKER"
fi

/qlever/qlever-server -i hd -p 7001 &
log "服务已启动（hd 索引，初始状态 $(cat "$MARKER")）"

while true; do
  sleep 5
  cur=$(state)
  if [ "$cur" != "$(cat "$MARKER" 2>/dev/null)" ]; then
    echo "$cur" > "$MARKER"
    p=$(pointer)
    src=""
    if [ -n "$p" ] && [ -s "/abox/$p" ]; then
      src="/abox/$p"
      log "abox 变更（$cur），重建索引"
    else
      : > "$EMPTY_NT"
      src="$EMPTY_NT"
      log "abox 缺失或为空（$cur），清除索引（0 三元组）"
    fi
    pkill -x qlever-server || true
    sleep 1
    if /qlever/qlever-index -i hd -f "$src" >> "$LOG" 2>&1; then
      /qlever/qlever-server -i hd -p 7001 &
      log "重建完成，服务已重启"
    else
      log "重建失败，仍用旧索引重启服务"
      /qlever/qlever-server -i hd -p 7001 &
    fi
  fi
done
