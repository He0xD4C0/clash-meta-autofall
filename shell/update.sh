#!/usr/bin/env bash
# 一次完整的刷新事务：拉订阅 -> 合并 -> 校验 -> 原子安装 -> 热重载 -> 预热
#                    -> 评分 -> 再合并 -> 再安装 -> 再重载 -> 再预热
#
# 全部参数来自环境变量，脚本本身不含密钥、不假定宿主路径：
#   DATA_DIR          数据根（secrets/assets/artifacts/cache/archive/last-failure）
#   MIHOMO_CONFIG     merge/score 读取的配置路径（用来取 secret）
#   MIHOMO_API        内核 API 基址（容器内为 http://core:<port>）
#   MIHOMO_SECRET     内核 API 凭据（必填）
#   SCORE_PLATFORMS / SCORE_SAMPLES / PREFER_REGION / PREFER_BONUS
#   SKIP_SCORE=1      跳过评分阶段（只刷新订阅）
#   KEEP_FAILURE=1    失败时保留现场到 last-failure/
set -euo pipefail

DATA_DIR=${DATA_DIR:-/data}
SHELL_DIR=${SHELL_DIR:-/opt/shell}
TOOLS_DIR=${TOOLS_DIR:-/opt/tools}
PY=${PY:-python3}
API=${MIHOMO_API:-http://127.0.0.1:9090}
API=${API%/}
CONFIG=${MIHOMO_CONFIG:-$DATA_DIR/artifacts/config.yaml}
SECRETS=$DATA_DIR/secrets/subscriptions.yaml
ARTIFACTS=$DATA_DIR/artifacts
CACHE=$DATA_DIR/cache
SCORES=${MIHOMO_SCORES:-$ARTIFACTS/scores.json}
LOCK=$DATA_DIR/.update.lock
KEEP_FAILURE=${KEEP_FAILURE:-0}
SCORE_PLATFORMS=${SCORE_PLATFORMS:-all}
SCORE_SAMPLES=${SCORE_SAMPLES:-2}
PREFER_REGION=${PREFER_REGION:-jp}
PREFER_BONUS=${PREFER_BONUS:-5}

log() { printf '%s\n' "  $*"; }
fail() { printf 'failed: %s\n' "$*" >&2; FAILED=1; exit 1; }

# 单写者：定时循环与手动触发同时进来时，后者立刻让位而不是交错。
exec 9>"$LOCK"
flock -n 9 || { log "another update is running, skipped"; exit 0; }

mkdir -p "$ARTIFACTS" "$CACHE" "$DATA_DIR/archive" "$CACHE/rule-providers"
TMP=$(mktemp -d "$ARTIFACTS/.tmp.XXXXXX")
cleanup() {
  local status=$?
  if [ -n "${DIAGNOSTICS_DIR:-}" ]; then
    local refresh=ok
    if [ "$status" != "0" ]; then refresh=failed; elif [ "${DEGRADED:-0}" = "1" ]; then refresh=degraded; fi
    "$PY" "$TOOLS_DIR/diagnostics.py" "$refresh" || log "diagnostics publication failed"
  fi
  if [ -d "$TMP" ]; then
    if [ "$KEEP_FAILURE" = "1" ] && [ -n "${FAILED:-}" ]; then
      dest=$DATA_DIR/last-failure/$(date +%Y%m%dT%H%M%S)
      mkdir -p "$dest"
      printf "%s\n" "refresh failed" >"$dest/status"
      log "failure kept in $dest"
    fi
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

on_error() { FAILED=1; }
trap on_error ERR

SECRET=${MIHOMO_SECRET:-}
[ -n "$SECRET" ] || fail "MIHOMO_SECRET is empty"
export MIHOMO_CONFIG=$CONFIG MIHOMO_SECRET=$SECRET MIHOMO_API=$API MIHOMO_SCORES=$SCORES

for f in geoip.metadb geosite.dat; do
  if [ -s "$DATA_DIR/assets/$f" ]; then
    install -m 644 "$DATA_DIR/assets/$f" "$CACHE/$f.new"
    mv -f "$CACHE/$f.new" "$CACHE/$f"
  fi
  [ -s "$CACHE/$f" ] || fail "missing geo data: $f (run tools/seed_dev.sh)"
done

validate_candidate() {
  local result=0
  "$PY" "$TOOLS_DIR/rules.py" prepare "$1" "$CACHE" "${@:2}" 2>"$TMP/rules.status" || result=$?
  cat "$TMP/rules.status" >&2
  if grep -q '^degraded:' "$TMP/rules.status"; then DEGRADED=1; fi
  [ "$result" = "0" ] && mihomo -t -d "$CACHE" -f "$1" >/dev/null
}

build_probe() {
  mkdir -p "$DATA_DIR/probe"
  "$PY" "$SHELL_DIR/make_probe_config.py" "$1" "$TMP/probe-config.yaml" --secret "$SECRET" &&
    mihomo -t -d "$DATA_DIR/probe" -f "$TMP/probe-config.yaml" >/dev/null
}

install_probe() {
  mv -f "$TMP/probe-config.yaml" "$ARTIFACTS/probe-config.yaml"
}

use_installed() {
  cp "$ARTIFACTS/config.yaml" "$TMP/config.yaml" &&
    "$PY" "$SHELL_DIR/merge.py" --restore "$TMP/config.yaml" &&
    validate_candidate "$TMP/config.yaml" --use-existing &&
    build_probe "$TMP/config.yaml"
}

install_config() {
  install -m 600 "$1" "$ARTIFACTS/config.yaml.new"
  mv -f "$ARTIFACTS/config.yaml.new" "$ARTIFACTS/config.yaml"
  # 标记"磁盘上这份配置是我们装的"。内核在缺配置启动时会自己写一个 16 字节的
  # 默认 stub（mixed-port: 7890），不能把它当成"上一份配置"。
  : >"$ARTIFACTS/.config-installed"
}

reload_and_install() {
  # The initialized file is replaced only if the running kernel accepts the candidate.
  "$PY" "$TOOLS_DIR/reload.py" "$API" "$SECRET" "$1" ||
    fail "reload rejected - keeping the previous on-disk config"
  install_config "$1"
}

warm() {
  "$PY" - "$API" "$SECRET" "$1" <<'PY'
import sys, time, urllib.parse, urllib.request, yaml
api, secret, path = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = yaml.safe_load(open(path, encoding="utf-8"))

def api_get(p, timeout):
    req = urllib.request.Request(api + p, headers={"Authorization": "Bearer " + secret})
    return urllib.request.urlopen(req, timeout=timeout).read()

for _ in range(30):
    try:
        api_get("/version", 5)
        break
    except Exception:
        time.sleep(1)
for group in cfg.get("proxy-groups") or []:
    if group.get("type") not in ("fallback", "url-test", "load-balance"):
        continue
    query = urllib.parse.urlencode(
        {"url": group.get("url") or "http://www.gstatic.com/generate_204", "timeout": 6000}
    )
    try:
        api_get("/group/" + urllib.parse.quote(str(group["name"]), safe="") + "/delay?" + query, 180)
        print("  probed", group["name"])
    except Exception as exc:
        print("  probe skipped", group["name"], exc)
PY
}

if [ "${INIT_ONLY:-0}" = "1" ] && [ -f "$ARTIFACTS/.config-installed" ] &&
   [ -s "$ARTIFACTS/config.yaml" ] && use_installed; then
  install_probe
  install_config "$TMP/config.yaml"
  log "validated installed configuration (offline bootstrap)"
  DEGRADED=1
  exit 0
fi

SOURCE_CACHE=$DATA_DIR/subscription-cache
install -d -m 700 "$SOURCE_CACHE"
"$PY" "$SHELL_DIR/subscriptions.py" "$SECRETS" "$SOURCE_CACHE" "$TMP/sources" "$TMP/pending" >"$TMP/sources.list" 2>"$TMP/sources.status" || {
  cat "$TMP/sources.status" >&2
  fail "subscription refresh stopped"
}
cat "$TMP/sources.status" >&2
if grep -q "^degraded:" "$TMP/sources.status"; then DEGRADED=1; fi
mapfile -t SOURCES <"$TMP/sources.list"

"$PY" "$SHELL_DIR/merge.py" "$TMP/config.yaml" "${SOURCES[@]}"
rule_options=()
if [ "${INIT_ONLY:-0}" = "1" ]; then rule_options+=(--use-existing); fi
validate_candidate "$TMP/config.yaml" "${rule_options[@]}" || fail "candidate failed validation"
if [ -n "${PROBE_API:-}" ] || [ "${INIT_ONLY:-0}" = "1" ]; then
  build_probe "$TMP/config.yaml" || fail "probe candidate failed validation"
fi

save_subscriptions() {
  shopt -s nullglob
  for source in "$TMP/pending/"*.yaml; do
    chmod 600 "$source"
    mv -f "$source" "$SOURCE_CACHE/"
  done
  for source in "$SOURCE_CACHE/"*.yaml; do
    if ! grep -Fqx -- "${source##*/}" "$TMP/pending/active"; then
      rm -- "$source"
    fi
  done
  shopt -u nullglob
}

if [ "${INIT_ONLY:-0}" = "1" ]; then
  install_probe
  install_config "$TMP/config.yaml"
  save_subscriptions
  log "bootstrap config installed (init mode)"
  exit 0
fi

reload_and_install "$TMP/config.yaml"
save_subscriptions
probe_synced=0
if [ -n "${PROBE_API:-}" ]; then
  if "$PY" "$TOOLS_DIR/reload.py" "$PROBE_API" "$SECRET" \
      "$TMP/probe-config.yaml" --retries 5 --delay 1; then
    install_probe
    probe_synced=1
    log "probe core synced"
  else
    log "probe core unchanged; skipping purity scan"
  fi
fi
warm "$ARTIFACTS/config.yaml"

if [ "${SKIP_SCORE:-0}" != "1" ]; then
  # 纯净度必须在评分之前：score.py 读同一份快照，把住宅/机房/代理/地区虚标算进总分
  if [ "${PURITY_ENABLED:-1}" = "1" ] && [ "$probe_synced" = "1" ]; then
    log "probing purity (exit IP per node, pace ${PURITY_PACE:-1.5}s)"
    purity_file=${MIHOMO_PURITY:-$ARTIFACTS/purity.json}
    if "$PY" "${TOOLS_DIR:-/opt/tools}/purity.py" --pace "${PURITY_PACE:-1.5}" \
      --json "$purity_file" --top 5 && [ -f "$purity_file" ]; then
      stamp_p=$(date +%Y%m%dT%H%M%S)
      gzip -c "$purity_file" >"$DATA_DIR/archive/purity-$stamp_p.json.gz"
      ls -1 "$DATA_DIR/archive"/purity-*.json.gz | sort | head -n -7 | xargs -r rm -f
    else
      log "purity probe unavailable - scoring continues"
    fi
  fi
  log "scoring (platforms=$SCORE_PLATFORMS samples=$SCORE_SAMPLES prefer=$PREFER_REGION)"
  "$PY" "$SHELL_DIR/score.py" --platforms "$SCORE_PLATFORMS" --samples "$SCORE_SAMPLES" \
    --prefer "$PREFER_REGION" --prefer-bonus "$PREFER_BONUS" --json "$SCORES" --top 0
  # 归档：只留最近 7 份（按文件名排序，时间戳前缀保证字典序=时间序）
  stamp=$(date +%Y%m%dT%H%M%S)
  gzip -c "$SCORES" >"$DATA_DIR/archive/scores-$stamp.json.gz"
  ls -1 "$DATA_DIR/archive"/scores-*.json.gz 2>/dev/null | sort | head -n -7 | xargs -r rm -f

  "$PY" "$SHELL_DIR/merge.py" "$TMP/config.yaml" "${SOURCES[@]}"
  validate_candidate "$TMP/config.yaml" --use-existing || fail "reordered candidate failed validation"
  reload_and_install "$TMP/config.yaml"
  warm "$ARTIFACTS/config.yaml"
fi

log "done"
