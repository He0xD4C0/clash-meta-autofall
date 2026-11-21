#!/usr/bin/env python3
"""对被选中的节点做短周期连通性巡检，失败就立刻触发组内重新选路。

内核自身的 fallback 健康检查是 300s 一次、且要连续失败 3 次才切换，最坏 15 分钟才
换节点。这个巡检每 5 分钟对"当前选中的节点"打三个目标（组自己的探测地址、gstatic、
AI），两次以上失败就调 /group/<name>/delay 强制重算并切换，把切换延迟压到一个周期内。

    watchdog.py [--targets URL,URL] [--timeout 5]

所有结果追加到 $DATA_DIR/run.log（一行 JSON），退出码恒为 0：它由调度器反复调用，
不该因为某个节点坏掉就把自己弄死。
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import yaml

CONFIG = os.environ.get(
    "MIHOMO_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "artifacts", "config.yaml"),
)
API = os.environ.get("MIHOMO_API", "http://127.0.0.1:9090").rstrip("/")
RUN_LOG = os.environ.get("RUN_LOG", "/data/run.log")
EXTRA_TARGETS = (
    "http://www.gstatic.com/generate_204",
    "https://chatgpt.com",
)


def secret() -> str:
    if os.environ.get("MIHOMO_SECRET"):
        return os.environ["MIHOMO_SECRET"]
    with open(CONFIG, encoding="utf-8") as fh:
        return str((yaml.safe_load(fh) or {}).get("secret") or "")


def api(path: str, timeout: float, token: str, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def journal(entry: dict) -> None:
    entry.setdefault("ts", datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))
    entry.setdefault("phase", "watchdog")
    line = json.dumps(entry, ensure_ascii=False)
    print("  " + line, flush=True)
    try:
        with open(RUN_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def is_healthy(ok: int, total: int) -> bool:
    """严格过半通过才算健康：单站点被墙不足以换节点，整体不可用才换。"""
    return total > 0 and ok * 2 > total


def probe_node(node: str, targets, timeout_ms: int, token: str):
    """返回 (成功数, 明细)。"""
    ok, detail = 0, []
    for url in targets:
        query = urllib.parse.urlencode({"url": url, "timeout": timeout_ms})
        path = "/proxies/" + urllib.parse.quote(node, safe="") + "/delay?" + query
        try:
            delay = api(path, timeout_ms / 1000 + 10, token).get("delay")
            ok += 1
            detail.append({"url": url, "delay": delay})
        except Exception as exc:
            detail.append({"url": url, "error": str(exc)[:80]})
    return ok, detail


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="fast connectivity watchdog for selected nodes")
    ap.add_argument("--targets", default="", help="额外的探测地址，逗号分隔")
    ap.add_argument(
        "--force-targets",
        default="",
        help="替换默认目标列表（演练用：给一个必定失败的地址即可验证切换路径）",
    )
    ap.add_argument("--timeout", type=int, default=5000)
    args = ap.parse_args(argv[1:])

    token = secret()
    with open(CONFIG, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    groups = [
        g for g in (cfg.get("proxy-groups") or [])
        if g.get("type") in ("fallback", "url-test", "load-balance")
    ]
    extra = [t.strip() for t in args.targets.split(",") if t.strip()]
    forced = [t.strip() for t in args.force_targets.split(",") if t.strip()]

    problems = 0
    for group in groups:
        name = str(group.get("name"))
        try:
            now = api("/proxies/" + urllib.parse.quote(name, safe=""), 10, token).get("now")
        except Exception as exc:
            journal({"group": name, "status": "group-unreadable", "detail": str(exc)[:80]})
            problems += 1
            continue
        if not now or now in ("DIRECT", "REJECT"):
            continue
        if forced:
            targets = forced
        else:
            targets = [group.get("url") or "http://www.gstatic.com/generate_204", *EXTRA_TARGETS, *extra]
        ok, detail = probe_node(now, targets, args.timeout, token)
        healthy = is_healthy(ok, len(targets))
        entry = {
            "group": name,
            "node": now,
            "ok": ok,
            "targets": len(targets),
            "status": "healthy" if healthy else "degraded",
        }
        if not healthy:
            problems += 1
            # 强制组重新探测：内核会挑下一个健康成员并在本轮生效
            try:
                query = urllib.parse.urlencode(
                    {"url": group.get("url") or "http://www.gstatic.com/generate_204",
                     "timeout": args.timeout}
                )
                api(
                    "/group/" + urllib.parse.quote(name, safe="") + "/delay?" + query,
                    args.timeout / 1000 + 30,
                    token,
                )
                entry["action"] = "forced-reselect"
                entry["previous"] = now
                try:
                    entry["selected"] = api(
                        "/proxies/" + urllib.parse.quote(name, safe=""), 10, token
                    ).get("now")
                except Exception:
                    pass
            except Exception as exc:
                entry["action"] = f"reselect-failed: {str(exc)[:60]}"
            entry["detail"] = detail
        journal(entry)
    journal({"status": "summary", "groups": len(groups), "problems": problems})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
