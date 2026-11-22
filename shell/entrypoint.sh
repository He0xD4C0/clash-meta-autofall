#!/usr/bin/env bash
# shell 容器的入口：只做模式分发，业务逻辑都在 update.sh / score.py / merge.py 里。
#
#   update    跑一次完整事务后退出（CI、手动触发、首次播种）
#   loop      先跑一次，然后每 REFRESH_INTERVAL 秒重复；SIGTERM 立即退出
#   score     只重算评分（不碰订阅）
#   validate  只校验现有产物（配置交给内核 -t，数据交给 tools/validate.py）
set -euo pipefail

MODE=${1:-update}
SHELL_DIR=${SHELL_DIR:-/opt/shell}
DATA_DIR=${DATA_DIR:-/data}
INTERVAL=${REFRESH_INTERVAL:-21600}

case "$MODE" in
  update)
    exec "$SHELL_DIR/update.sh"
    ;;
  loop)
    trap 'echo "  stopping loop"; exit 0' TERM INT
    while :; do
      "$SHELL_DIR/update.sh" || echo "  update failed, retrying after interval" >&2
      echo "  sleeping ${INTERVAL}s"
      sleep "$INTERVAL" &
      wait $!
    done
    ;;
  init)
    # 首次启动：只合并 + 校验 + 落盘，不重载（此时内核还没起来）
    INIT_ONLY=1 exec "$SHELL_DIR/update.sh"
    ;;
  score)
    exec python3 "$SHELL_DIR/score.py" \
      --platforms "${SCORE_PLATFORMS:-all}" \
      --samples "${SCORE_SAMPLES:-2}" \
      --prefer "${PREFER_REGION:-jp}" \
      --prefer-bonus "${PREFER_BONUS:-5}" \
      --json "${MIHOMO_SCORES:-$DATA_DIR/artifacts/scores.json}"
    ;;
  validate)
    mihomo -t -d "$DATA_DIR/cache" -f "$DATA_DIR/artifacts/config.yaml"
    python3 "${TOOLS_DIR:-/opt/tools}/rules.py" check "$DATA_DIR/artifacts/config.yaml" "$DATA_DIR/cache"
    if [ -n "${MIHOMO_API:-}" ]; then
      python3 "${TOOLS_DIR:-/opt/tools}/rules.py" live "$DATA_DIR/artifacts/config.yaml"
    fi
    exec python3 "${TOOLS_DIR:-/opt/tools}/validate.py" tree "$DATA_DIR"
    ;;
  scheduler)
    # 后台常驻：5 分钟巡检 + 6 小时评分周期
    exec "$SHELL_DIR/scheduler.sh"
    ;;
  watchdog)
    exec python3 "${TOOLS_DIR:-/opt/tools}/watchdog.py"
    ;;
  purity)
    shift || true
    exec python3 "${TOOLS_DIR:-/opt/tools}/purity.py" "$@"
    ;;
  *)
    echo "usage: entrypoint.sh {init|update|loop|score|validate|scheduler|watchdog|purity}" >&2
    exit 2
    ;;
esac
