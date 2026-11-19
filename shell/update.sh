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
fail() { printf 'failed: %s\n' "$*" >&2; exit 1; }

# 单写者：定时循环与手动触发同时进来时，后者立刻让位而不是交错。
exec 9>"$LOCK"
flock -n 9 || { log "another update is running, skipped"; exit 0; }

mkdir -p "$ARTIFACTS" "$CACHE" "$DATA_DIR/archive"
TMP=$(mktemp -d "$ARTIFACTS/.tmp.XXXXXX")
cleanup() {
  if [ -d "$TMP" ]; then
    if [ "$KEEP_FAILURE" = "1" ] && [ -n "${FAILED:-}" ]; then
      dest=$DATA_DIR/last-failure/$(date +%Y%m%dT%H%M%S)
      mkdir -p "$dest"
      cp -a "$TMP"/. "$dest"/ 2>/dev/null || true
      log "failure kept in $dest"
    fi
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

on_error() { FAILED=1; }
trap on_error ERR

[ -f "$SECRETS" ] || fail "no subscription pool at $SECRETS"
[ -n "${MIHOMO_SECRET:-}" ] || fail "MIHOMO_SECRET is empty"

# 1) 订阅池 -> TSV（标签与地址），顺序即优先级
"$PY" - "$SECRETS" <<'PY' >"$TMP/pool.tsv"
import re, sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
entries = doc.get("subscriptions") if isinstance(doc, dict) else None
if not entries:
    raise SystemExit("rejected: pool declares no subscription")
for index, entry in enumerate(entries, 1):
    url = str(entry.get("url") or "").strip()
    if not url:
        continue
    name = re.sub(r"[^\w.\-]+", "_", str(entry.get("name") or f"sub{index}"))
    print(f"{name}\t{url}")
PY

# 2) 逐个下载；先走环境里配置的代理，失败再直连（容器内直连=宿主出口）
SOURCES=()
while IFS=$'\t' read -r name url; do
  [ -n "$url" ] || continue
  if curl -fsS -m 60 -A 'clash-verge/v2.0.0' "$url" -o "$TMP/$name.yaml"; then
    log "fetched $name"
  elif curl -fsS --noproxy '*' -m 60 -A 'clash-verge/v2.0.0' "$url" -o "$TMP/$name.yaml"; then
    log "fetched $name (direct)"
  else
    log "skipped $name (download failed)"
    continue
  fi
  SOURCES+=("$TMP/$name.yaml")
done <"$TMP/pool.tsv"
[ "${#SOURCES[@]}" -gt 0 ] || fail "no subscription could be fetched"

# 3) 合并（scores 若新鲜则按其排序，否则退回订阅顺序）
"$PY" "$SHELL_DIR/merge.py" "$TMP/config.yaml" "${SOURCES[@]}"

# 4) 内核自检：唯一权威的配置校验。geo 数据缺失时先报真因，而不是让 -t 报"配置被拒"
for f in geoip.metadb geosite.dat; do
  [ -s "$DATA_DIR/assets/$f" ] || fail "missing $DATA_DIR/assets/$f (run tools/seed_dev.sh)"
done
cp -f "$DATA_DIR/assets/geoip.metadb" "$DATA_DIR/assets/geosite.dat" "$CACHE"/
mihomo -t -d "$CACHE" -f "$TMP/config.yaml" >/dev/null || fail "kernel rejected the generated config"

reload() {
  # payload 取自传入文件（通常是尚未安装的临时文件）：内核只允许 path 方式重载
  # -d 目录内的路径；重载失败时磁盘配置保持不变。
  "$PY" - "$API" "$SECRET" "$1" <<'PY'
import json, sys, time, urllib.request

api, secret, path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, encoding="utf-8") as fh:
    body = json.dumps({"payload": fh.read()}).encode()
last = None
for _ in range(30):
    req = urllib.request.Request(
        api + "/configs?force=true",
        data=body,
        method="PUT",
        headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=60).read()
        sys.exit(0)
    except Exception as exc:  # 内核可能还没起来，重试到超时
        last = exc
        time.sleep(2)
raise SystemExit(f"reload rejected: {last}")
PY
}

install_config() {
  install -m 600 "$1" "$ARTIFACTS/config.yaml.new"
  mv -f "$ARTIFACTS/config.yaml.new" "$ARTIFACTS/config.yaml"
  # 标记"磁盘上这份配置是我们装的"。内核在缺配置启动时会自己写一个 16 字节的
  # 默认 stub（mixed-port: 7890），不能把它当成"上一份配置"。
  : >"$ARTIFACTS/.config-installed"
}

reload_and_install() {
  # 正常路径：重载成功后才落盘，重载被拒时磁盘保留旧配置。
  if reload "$1"; then
    install_config "$1"
    return 0
  fi
  if [ -f "$ARTIFACTS/.config-installed" ]; then
    fail "reload rejected - keeping the previous on-disk config"
  fi
  # 首次启动：磁盘还没有配置，内核也就不可能热重载。先落盘让内核能起来，明确要求重启。
  install_config "$1"
  fail "reload rejected - installed config for first boot; restart core and re-run update"
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

SECRET=$MIHOMO_SECRET
export MIHOMO_CONFIG=$CONFIG MIHOMO_SECRET=$SECRET MIHOMO_API=$API MIHOMO_SCORES=$SCORES

# 5) 首次启动（INIT_ONLY）：内核尚未运行，只落盘不重载
if [ "${INIT_ONLY:-0}" = "1" ]; then
  install_config "$TMP/config.yaml"
  log "bootstrap config installed (init mode)"
  exit 0
fi

# 5) 先热重载（body 取自临时文件），成功后才落盘：重载被拒时磁盘仍是旧配置
reload_and_install "$TMP/config.yaml"
warm "$ARTIFACTS/config.yaml"

if [ "${SKIP_SCORE:-0}" != "1" ]; then
  log "scoring (platforms=$SCORE_PLATFORMS samples=$SCORE_SAMPLES prefer=$PREFER_REGION)"
  "$PY" "$SHELL_DIR/score.py" --platforms "$SCORE_PLATFORMS" --samples "$SCORE_SAMPLES" \
    --prefer "$PREFER_REGION" --prefer-bonus "$PREFER_BONUS" --json "$SCORES" --top 0
  # 归档：只留最近 7 份（按文件名排序，时间戳前缀保证字典序=时间序）
  stamp=$(date +%Y%m%dT%H%M%S)
  gzip -c "$SCORES" >"$DATA_DIR/archive/scores-$stamp.json.gz"
  ls -1 "$DATA_DIR/archive"/scores-*.json.gz 2>/dev/null | sort | head -n -7 | xargs -r rm -f

  "$PY" "$SHELL_DIR/merge.py" "$TMP/config.yaml" "${SOURCES[@]}"
  mihomo -t -d "$CACHE" -f "$TMP/config.yaml" >/dev/null || fail "kernel rejected the re-ordered config"
  reload_and_install "$TMP/config.yaml"
  warm "$ARTIFACTS/config.yaml"
fi

log "done"
