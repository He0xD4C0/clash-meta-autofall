#!/usr/bin/env bash
# 周期任务：短周期巡检 + 长周期「纯净度→评分→重排→热重载」。
#
# 用两个独立循环而不是 crontab：容器里不需要 cron 守护进程，间隔全部来自环境变量，
# 每次任务失败只影响本轮（不会让整个容器退出），日志统一进容器 stdout 与 run.log。
set -uo pipefail

PY=${PY:-python3}
TOOLS_DIR=${TOOLS_DIR:-/opt/tools}
SHELL_DIR=${SHELL_DIR:-/opt/shell}
WATCHDOG_INTERVAL=${WATCHDOG_INTERVAL:-300}
REFRESH_INTERVAL=${REFRESH_INTERVAL:-21600}

log() { printf '[%s] scheduler: %s\n' "$(date '+%H:%M:%S')" "$*"; }

watchdog_loop() {
  # 刚启动时先等一轮，让内核把组和节点准备好
  sleep "${WATCHDOG_WARMUP:-30}"
  while :; do
    "$PY" "$TOOLS_DIR/watchdog.py" 2>&1 | sed 's/^/  /' || log "watchdog failed"
    if [ -n "${DIAGNOSTICS_DIR:-}" ]; then
      "$PY" "$TOOLS_DIR/diagnostics.py" || log "diagnostics publication failed"
    fi
    sleep "$WATCHDOG_INTERVAL"
  done
}

score_loop() {
  while :; do
    log "score cycle start (interval ${REFRESH_INTERVAL}s)"
    # 完整事务：拉订阅 → 合并 → 纯净度 → 评分 → 重排 → 热重载
    "$SHELL_DIR/update.sh" || log "score cycle failed"
    log "score cycle done"
    sleep "$REFRESH_INTERVAL"
  done
}

trap 'log "terminating"; kill 0 2>/dev/null; exit 0' TERM INT
log "started: watchdog=${WATCHDOG_INTERVAL}s score=${REFRESH_INTERVAL}s"
watchdog_loop &
score_loop &
wait
