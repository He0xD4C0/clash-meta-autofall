#!/usr/bin/env python3
"""自建状态面板：把评分、纯净度、选路与巡检日志合成一个页面。

之所以自建而不是只靠现成面板：内核面板只能看到"组与节点"，看不到分数、IP 纯净度、
地区虚标与巡检历史——这些恰好是这套 shell 的核心产出。

    ui.py [--port 8080]

路由：
    /            单页（15 秒自刷新，无外部资源）
    /state.json  聚合数据（面板自己用，也方便脚本取）
"""

import argparse
import html
import json
import os
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_DIR = os.environ.get("DATA_DIR", "/data")
API = os.environ.get("MIHOMO_API", "http://127.0.0.1:9090").rstrip("/")
ARTIFACTS = os.path.join(DATA_DIR, "artifacts")
SCORES = os.environ.get("MIHOMO_SCORES", os.path.join(ARTIFACTS, "scores.json"))
PURITY = os.environ.get("MIHOMO_PURITY", os.path.join(ARTIFACTS, "purity.json"))
RUN_LOG = os.environ.get("RUN_LOG", os.path.join(DATA_DIR, "run.log"))
# 组名从配置里读：除了我们托管的 出口/AI出口/Site出口，订阅骨架还可能带自己的组
FALLBACK_GROUPS = ("出口", "AI出口", "Site出口")


def _secret() -> str:
    path = os.environ.get("MIHOMO_CONFIG", os.path.join(ARTIFACTS, "config.yaml"))
    try:
        import yaml

        with open(path, encoding="utf-8") as fh:
            return str((yaml.safe_load(fh) or {}).get("secret") or "")
    except Exception:
        return ""


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _group_names() -> list:
    path = os.environ.get("MIHOMO_CONFIG", os.path.join(ARTIFACTS, "config.yaml"))
    try:
        import yaml

        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        names = [
            str(g.get("name"))
            for g in (doc.get("proxy-groups") or [])
            if isinstance(g, dict) and g.get("name")
        ]
        ordered = [n for n in FALLBACK_GROUPS if n in names]
        return ordered + [n for n in names if n not in ordered]
    except Exception:
        return list(FALLBACK_GROUPS)


def _api(path: str):
    req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + _secret()})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def _tail(path: str, lines: int = 12) -> list:
    try:
        with open(path, encoding="utf-8") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-lines:]]
    except OSError:
        return []


def state() -> dict:
    scores = _read_json(SCORES)
    purity = _read_json(PURITY)
    proxies = {}
    try:
        proxies = _api("/proxies")["proxies"]
    except Exception:
        pass

    groups = []
    for name in _group_names():
        info = proxies.get(name) or {}
        if info:
            groups.append(
                {
                    "name": name,
                    "now": info.get("now"),
                    "members": len(info.get("all") or []),
                    "type": info.get("type"),
                }
            )

    nodes = []
    for name, info in (scores.get("nodes") or {}).items():
        pur = (info.get("purity") or {}) if isinstance(info, dict) else {}
        nodes.append(
            {
                "name": name,
                "final": info.get("final"),
                "base": info.get("overall"),
                "region": info.get("region"),
                "bonus": info.get("bonus"),
                "purity": pur.get("score"),
                "ip": pur.get("ip"),
                "country": pur.get("country_code"),
                "region_match": pur.get("region_match"),
                "reason": pur.get("reason"),
                "platforms": {
                    k: (v or {}).get("score")
                    for k, v in (info.get("platforms") or {}).items()
                },
            }
        )
    nodes.sort(key=lambda n: (n.get("final") or 0), reverse=True)

    return {
        "generated": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "score_snapshot": scores.get("generated"),
        "purity_snapshot": purity.get("generated"),
        "platforms": list((scores.get("platforms") or {}).keys()),
        "purity_weight": scores.get("purity_weight"),
        "groups": groups,
        "nodes": nodes,
        "run_log": _tail(RUN_LOG, 14),
    }


PAGE = """<!doctype html><meta charset=utf-8>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mihomo-stack · 选路与纯净度</title>
<style>
 body{{font:13px/1.5 system-ui,-apple-system,'Noto Sans SC',sans-serif;margin:0;padding:18px;background:#0f1115;color:#e6e6e6}}
 h1{{font-size:15px;margin:0 0 12px;font-weight:600;color:#9ad}}
 .cards{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}}
 .card{{background:#171a21;border:1px solid #262b36;border-radius:8px;padding:10px 12px;min-width:210px}}
 .card b{{display:block;font-size:11px;color:#8b93a7;font-weight:500;margin-bottom:4px}}
 .card span{{font-size:13px}}
 table{{border-collapse:collapse;width:100%;background:#141821;border:1px solid #262b36;border-radius:8px;overflow:hidden}}
 th,td{{padding:5px 8px;text-align:left;border-bottom:1px solid #1e232d;white-space:nowrap}}
 th{{background:#1b2029;color:#9aa4b8;font-weight:500;position:sticky;top:0}}
 tr:hover td{{background:#1a1f29}}
 td.num{{text-align:right;font-variant-numeric:tabular-nums}}
 .ok{{color:#6cc38a}} .warn{{color:#e0b04a}} .bad{{color:#e06c6c}} .dim{{color:#6b7486}}
 .bar{{display:inline-block;height:6px;border-radius:3px;background:#2b3b52;vertical-align:middle}}
 h2{{font-size:13px;color:#9aa4b8;margin:16px 0 6px;font-weight:500}}
 pre{{background:#12161d;border:1px solid #232935;border-radius:8px;padding:10px;overflow:auto;max-height:220px;font-size:11px;color:#9aa4b8;margin:0}}
</style>
<h1>mihomo-stack · 选路与纯净度　<span class=dim id=stamp>{stamp}</span></h1>
<div class=cards>{cards}</div>
<table><thead><tr>
<th>#</th><th>节点</th><th>final</th><th>base</th><th>纯净度</th><th>地区</th><th>实际</th>{platform_heads}</tr></thead>
<tbody>{rows}</tbody></table>
<h2>最近巡检 / 刷新（run.log）</h2>
<pre>{log}</pre>
<script>setTimeout(()=>location.reload(),15000)</script>
"""


def _cls(value, good_high=True, warn=60):
    if value is None:
        return "dim"
    if good_high:
        return "ok" if value >= warn else ("warn" if value >= warn * 0.6 else "bad")
    return "ok" if value <= warn else "bad"


def render(st: dict) -> str:
    cards = []
    for g in st["groups"]:
        cards.append(
            f"<div class=card><b>{html.escape(g['name'])} · {html.escape(str(g.get('type')))}"
            f" ({g.get('members')} 节点)</b><span>{html.escape(str(g.get('now')))}</span></div>"
        )
    if not cards:
        cards.append("<div class=card><b>组</b><span class=bad>内核 API 不可达</span></div>")

    def cell(value):
        return "<td class=num>" + ("-" if value is None else f"{value:.0f}") + "</td>"

    platforms = st["platforms"]
    heads = "".join(f"<th>{html.escape(p[:6])}</th>" for p in platforms)
    rows = []
    for index, n in enumerate(st["nodes"], 1):
        purity = n.get("purity")
        match = n.get("region_match")
        mark = "" if match is None else (" ✓" if match else " ✗")
        cells = "".join(cell(n["platforms"].get(p)) for p in platforms)
        rows.append(
            "<tr>"
            f"<td class=num>{index}</td>"
            f"<td>{html.escape(n['name'])}</td>"
            f"<td class=num><b>{n['final'] if n['final'] is not None else '-'}</b></td>"
            f"<td class=num>{n['base'] if n['base'] is not None else '-'}</td>"
            f"<td class=\"num {_cls(purity)}\">{'' if purity is None else int(purity)}</td>"
            f"<td>{html.escape(str(n.get('region')))}</td>"
            f"<td>{html.escape(str(n.get('country') or '--'))}{mark}</td>"
            f"{cells}"
            "</tr>"
        )
    return PAGE.format(
        stamp=html.escape(str(st.get("score_snapshot") or "")),
        cards="".join(cards),
        platform_heads=heads,
        rows="".join(rows) or "<tr><td colspan=8 class=dim>暂无评分数据</td></tr>",
        log=html.escape("\n".join(st.get("run_log") or []) or "（空）"),
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/state.json"):
            body = json.dumps(state(), ensure_ascii=False).encode()
            ctype = "application/json; charset=utf-8"
        elif self.path in ("/", "/index.html"):
            body = render(state()).encode()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="mihomo-stack dashboard")
    ap.add_argument("--port", type=int, default=int(os.environ.get("UI_PORT", "8080")))
    args = ap.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"ui listening on :{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
