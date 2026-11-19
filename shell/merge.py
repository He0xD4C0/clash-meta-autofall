#!/usr/bin/env python3
"""Merge local overrides into a pool of downloaded Clash subscriptions.

Every local policy decision lives in this file, so both a subscription
refresh (update.sh) and a container restart reproduce the same result, and
the GUI/panel can only change what is declared here:

  * the pool is declared in `subscriptions.yaml` (file order = preference
    order); `update.sh` fetches every entry and hands the files here in that
    same order. The first entry is the skeleton: its proxy-groups and rules
    are kept, so only it decides which extra groups the panel shows. Later
    entries contribute nodes only, which is why two pools cannot fight over
    a group name. A node name collision gets its pool tag prefixed
    (`[tag] name`), so the first pool keeps its names and the GUI's stored
    selections survive adding another subscription

  * TUN / inbound surfaces coming from the subscription are stripped
  * transparent proxy pinned on TPROXY_PORT (consumed by the egress container in a
    separate stack,
    which lives in the kernel and is therefore untouched by GUI edits)
  * DNS pinned to domestic DoH only (223.5.5.5 + doh.pub), except the AI
    domain set, whose poisoned answers from domestic resolvers would break the
    AI group health check. No global overseas fallback: it is only reachable
    through `出口`, and its answers for CN CDN names are foreign edges, which
    then miss GEOIP,CN and get proxied back
  * sniffer enabled: intercepted flows are IP-only otherwise, which would
    make the subscription's DOMAIN rules inert. `override-destination` is on
    because peer-supplied IPs can be DNS-poisoned; the sniffed SNI is then the
    only trustworthy destination.
  * `出口` fallback group materialised, MATCH routed to it, and the
    subscription's url-test/fallback health checks tightened to 300s
  * `AI出口` fallback group for OpenAI/ChatGPT domains: Cloudflare closes the
    TLS handshake for exit IPs it dislikes, and the generic gstatic probe
    cannot see that, so this group is health-checked against
    AI_HEALTH_URL and only ever holds nodes that really reach ChatGPT
  * Baidu / Bilibili domain families are pinned DIRECT ahead of the
    subscription's rules, so a sniffed flow that no rule covers cannot fall
    through to MATCH and come back from abroad
  * Steam is split by endpoint instead of proxied wholesale (see STEAM_RULES):
    depot CDN, CM, client-update and static hosts leave directly, the blocked
    handful (api/store/community) via `出口`. Proxying all of it made Steam
    report `"countrycode":"US"` and hand the client overseas download edges
  * banned regions (`BAN_NODE`) and pseudo-nodes (`INFO_NODE`) are deleted from
    the node table and from every group, so no rule, fallback or GUI pick can
    reach them
  * `local-nodes.yaml` holds nodes no subscription publishes (hand-converted
    share URIs); they are merged ahead of the subscription nodes and into
    `出口` / `AI出口`, because a refresh rebuilds both from scratch
  * everything above is re-asserted on the finished config (`_assert_policy`),
    so a subscription refresh can never install a file that quietly lost the
    AI routing, the SNI override or the final MATCH rule
"""

import copy
import json
import os
import re
import sys
import time

import yaml

PLACEHOLDER_MARKERS = ("未授权", "已过期", "需要续费", "请先关闭代理")
MIN_PROXIES = 20

# 运行期参数一律由环境变量给出，仓库里不出现任何密钥或固定端口。
# MIHOMO_SECRET 必填：它是面板与 shell 访问内核 API 的唯一凭据。
API_SECRET = os.environ.get("MIHOMO_SECRET", "")
API_LISTEN = os.environ.get("MIHOMO_API_LISTEN", "127.0.0.1:9090")
MIXED_PORT = int(os.environ.get("MIHOMO_MIXED_PORT", "7897"))
# 透明代理端口由出口容器消费；开发栈没有出口容器时留空即可不生成该键。
TPROXY_PORT = os.environ.get("MIHOMO_TPROXY_PORT", "7898")

# Nodes that no subscription publishes (hand-converted share URIs). Read on
# every merge so a subscription refresh cannot drop them.
LOCAL_NODES = os.environ.get(
    "MIHOMO_LOCAL_NODES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "local-nodes.yaml"),
)

EXIT_GROUP = "出口"
INFO_NODE = re.compile(r"剩余流量|距离下次重置|套餐到期|官网：")
# Nodes whose name matches this are removed from the node table and every group,
# so neither rules, the fallback group nor the GUI can ever select them.
BAN_NODE = re.compile(r"香港|🇭🇰")


def _blocked(name: str) -> bool:
    """Pseudo-nodes (流量/到期提示) and banned regions are never selectable."""
    return bool(INFO_NODE.search(name) or BAN_NODE.search(name))


def _local_nodes() -> list:
    """Host-only nodes, in file order (they are tried first, see `main`)."""
    if not os.path.exists(LOCAL_NODES):
        return []
    with open(LOCAL_NODES, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or []
    entries = doc.get("proxies") if isinstance(doc, dict) else doc
    if not isinstance(entries, list):
        raise SystemExit(f"rejected: {LOCAL_NODES} is not a proxy list")
    return [p for p in entries if isinstance(p, dict) and p.get("name")]


def _load_source(path: str, tag: str) -> dict:
    """Read one pool entry and reject the shapes a refresh can produce.

    A dead subscription still answers 200 with a single "expired" pseudo-node
    (or with no node at all), so both cases fail here instead of installing a
    pool entry that silently contributes nothing.
    """
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise SystemExit(f"rejected: {tag} subscription is not a YAML mapping")
    entries = [p for p in (doc.get("proxies") or []) if isinstance(p, dict) and p.get("name")]
    if not entries:
        raise SystemExit(f"rejected: {tag} subscription published no proxies")
    bad = [str(p["name"]) for p in entries if any(m in str(p["name"]) for m in PLACEHOLDER_MARKERS)]
    if bad:
        raise SystemExit(f"rejected: {tag} subscription has a placeholder node: {bad[0]}")
    return doc


def _unique(base: str, seen: set) -> str:
    candidate, index = base, 2
    while candidate in seen:
        candidate = f"{base} #{index}"
        index += 1
    return candidate


def _merge(tagged: list) -> list:
    """Flatten the pool in declaration order.

    Order is the preference order of `出口` / `AI出口`, so the first pool that
    has a healthy node serves the traffic. Only colliding names are rewritten:
    renaming every node would drop the GUI's stored selections on the day a
    second subscription is added.
    """
    seen = set()
    merged = []
    for tag, entries in tagged:
        for proxy in entries:
            name = str(proxy["name"])
            if name in seen:
                name = _unique(f"[{tag}] {name}", seen)
            seen.add(name)
            merged.append({**proxy, "name": name})
    return merged


HEALTH_URL = "http://www.gstatic.com/generate_204"
HEALTH_INTERVAL = 300

# Cloudflare fronts ChatGPT and answers a flagged exit IP by closing the TLS
# handshake (curl: "SSL_ERROR_ZERO_RETURN" / schannel: "failed to receive
# handshake"). gstatic still replies 204 from such a node, so the generic
# probe cannot detect it; this group probes ChatGPT itself instead.
AI_GROUP = "AI出口"
# Probe the real page, not /cdn-cgi/trace: a node can answer trace while
# Cloudflare still resets the chatgpt.com vhost (measured on this provider),
# so the root URL is what separates usable exits from blocked ones.
AI_HEALTH_URL = "https://chatgpt.com"
AI_HEALTH_STATUS = 200
AI_DOMAINS = (
    "chatgpt.com",
    "chat.com",
    "openai.com",
    "oaistatic.com",
    "oaiusercontent.com",
    "sora.com",
)
AI_RULES = [f"DOMAIN-SUFFIX,{d},{AI_GROUP}" for d in AI_DOMAINS]

# 定向站点组：部分边缘/CDN 会按出口 IP 在 TLS 层直接断开，而通用探针（gstatic 204）
# 看不出这种差异，于是 fallback 会一直停在"对普通站点可用、对该站点不可用"的节点上。
# 仓库不内置任何具体供应商信息：域名与健康检查地址都在运行期给出，
# MIHOMO_SITE_DOMAINS 为空时既不生成该组，也不生成对应规则。
SITE_GROUP = os.environ.get("MIHOMO_SITE_GROUP", "Site出口")
SITE_HEALTH_STATUS = int(os.environ.get("MIHOMO_SITE_HEALTH_STATUS", "200"))


def _site_config():
    """(enabled, health_url, rules) - 域名与探针必须成对出现。"""
    domains = tuple(
        d.strip().lower()
        for d in os.environ.get("MIHOMO_SITE_DOMAINS", "").split(",")
        if d.strip()
    )
    url = os.environ.get("MIHOMO_SITE_HEALTH_URL", "").strip()
    if domains and not url:
        raise SystemExit(
            "rejected: MIHOMO_SITE_DOMAINS is set but MIHOMO_SITE_HEALTH_URL is empty - "
            "the group would be probed against a URL that cannot see the block"
        )
    return bool(domains), url, [f"DOMAIN-SUFFIX,{d},{SITE_GROUP}" for d in domains]


SITE_ENABLED, SITE_HEALTH_URL, SITE_RULES = _site_config()

# score.py measures every node per platform (and adds a small bonus for the
# preferred region). A fallback group serves its first healthy member, so
# ordering by that score instead of subscription order is what makes selection
# quality-driven. A stale file is ignored rather than trusted.
SCORES = os.environ.get(
    "MIHOMO_SCORES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "scores.json"),
)
# 数据契约：阈值 = 2 × 刷新周期（docs/DATA.md）。默认刷新 6h -> 12h。
SCORES_MAX_AGE = int(os.environ.get("MIHOMO_SCORES_MAX_AGE", str(12 * 3600)))

# Domains that must always leave from this node's own network. GEOIP,CN cannot
# cover them on its own: a sniffed flow has no destination IP, so a domain that
# the subscription has no rule for falls through to MATCH and gets proxied
# (observed on i1/i3.hdslb.com). bilibili.tv is deliberately absent - that one
# is the overseas site and belongs on the exit group.
CN_DIRECT_DOMAINS = (
    "baidu.com",
    "bdurl.net",
    "bdurl.cn",
    "bdstatic.com",
    "bdimg.com",
    "bilibili.com",
    "bilivideo.com",
    "hdslb.com",
    "hdslb.org",
    "biliapi.net",
    "biliimg.com",
    "biligame.com",
    "bili2233.cn",
    "b23.tv",
)
CN_DIRECT_RULES = [f"DOMAIN-SUFFIX,{d},DIRECT" for d in CN_DIRECT_DOMAINS]

# --- Steam ---------------------------------------------------------------
# Measured from this node: only a few Steam endpoints are blocked
# (api.steampowered.com 0/8 attempts, steamcommunity.com connect timeout,
# store.steampowered.com 1/3), while depot CDN, client-update, CM
# (steamserver.net) and the static hosts answer directly. Proxying the whole
# client - which is what MATCH does - also made Steam answer with
# `"countrycode":"US"` and hand out overseas edges (Fastly Tokyo, Alibaba HK),
# so every game download crossed the exit node. Split it by endpoint instead.
#
# CM servers are also dialled by cached IP without a DNS lookup, so Valve's own
# prefixes stay pinned DIRECT too; a CM reached abroad is what moves the login
# cell - and with it the download region - out of CN.
STEAM_DIRECT_DOMAINS = (
    "steamcontent.com",      # steampipe depot CDN: all game downloads
    "steamserver.net",       # CM / cell servers
    "steamstatic.com",       # client-update, store and community statics
    "steamusercontent.com",  # screenshots, workshop images
    "steamgames.com",        # partner assets the store pulls
)
STEAM_DIRECT_HOSTS = (
    "steamcdn-a.akamaihd.net",
    "media.steampowered.com",
    "login.steampowered.com",  # answers 302 directly
)
STEAM_PROXY_DOMAINS = (
    "steamcommunity.com",  # connect timeout direct
    "steampowered.com",    # api / store / checkout / help time out or flap
    "steam-chat.com",      # friend chat + voice
)
# AS32590 (Valve), IPv4 only, live BGP data collapsed to containing prefixes.
STEAM_DIRECT_CIDRS = (
    "45.121.184.0/24",
    "103.10.124.0/23",
    "103.28.54.0/24",
    "146.66.152.0/24",
    "146.66.155.0/24",
    "155.133.224.0/21",
    "155.133.236.0/22",
    "155.133.240.0/23",
    "155.133.244.0/24",
    "155.133.246.0/24",
    "155.133.248.0/22",
    "155.133.252.0/24",
    "155.133.254.0/23",
    "162.254.192.0/21",
    "185.25.180.0/24",
    "185.25.182.0/23",
    "192.69.96.0/22",
    "205.196.6.0/24",
    "208.64.200.0/22",
    "208.78.164.0/22",
)
# Domain rules before the CIDR block: the proxy entries must be able to win
# over a broad Valve prefix, and both must precede the subscription's
# DOMAIN-KEYWORD rules (google2.cdn.steampipe... matched `google` before).
STEAM_RULES = (
    [f"DOMAIN,{h},DIRECT" for h in STEAM_DIRECT_HOSTS]
    + [f"DOMAIN-SUFFIX,{d},DIRECT" for d in STEAM_DIRECT_DOMAINS]
    + [f"DOMAIN-SUFFIX,{d},{EXIT_GROUP}" for d in STEAM_PROXY_DOMAINS]
    + [f"IP-CIDR,{c},DIRECT,no-resolve" for c in STEAM_DIRECT_CIDRS]
)
PINNED_RULES = (SITE_RULES if SITE_ENABLED else []) + AI_RULES + CN_DIRECT_RULES + STEAM_RULES


def _assert_policy(cfg: dict) -> None:
    """Refuse to write a config that lost a pinned policy.

    A subscription refresh rebuilds this file from scratch, so a regression in
    the code above would silently hand peers a working-looking config without
    the AI routing or the SNI destination override. Fail before install
    instead, leaving the previous config.yaml in place.
    """
    groups = {
        g.get("name"): g for g in cfg.get("proxy-groups") or [] if isinstance(g, dict)
    }
    rules = [r for r in cfg.get("rules") or [] if isinstance(r, str)]
    lost = []
    if (groups.get(EXIT_GROUP) or {}).get("url") != HEALTH_URL:
        lost.append(f"{EXIT_GROUP} health check")
    ai = groups.get(AI_GROUP) or {}
    if ai.get("url") != AI_HEALTH_URL or ai.get("expected-status") != AI_HEALTH_STATUS:
        lost.append(f"{AI_GROUP} health check")
    if SITE_ENABLED:
        site = groups.get(SITE_GROUP) or {}
        if (
            site.get("url") != SITE_HEALTH_URL
            or site.get("expected-status") != SITE_HEALTH_STATUS
        ):
            lost.append(f"{SITE_GROUP} health check")
    if (cfg.get("sniffer") or {}).get("override-destination") is not True:
        lost.append("sniffer.override-destination")
    dns = cfg.get("dns") or {}
    resolvers = [
        *(dns.get("nameserver") or []),
        *(dns.get("direct-nameserver") or []),
    ]
    if "fallback" in dns or any(
        marker in str(u) for u in resolvers for marker in DNS_OVERSEAS
    ):
        lost.append("domestic-only DNS resolvers")
    if rules[: len(PINNED_RULES)] != PINNED_RULES:
        lost.append("AI / CN-direct / Steam rules not first")
    if not rules or rules[-1] != f"MATCH,{EXIT_GROUP}":
        lost.append("final MATCH rule")
    if lost:
        raise SystemExit("rejected: generated config lost pinned policy: " + ", ".join(lost))


DNS_RESOLVERS = [
    "https://223.5.5.5/dns-query",
    "https://doh.pub/dns-query",
]
# A resolver outside CN can only be reached through `出口`, so its answers look
# like overseas traffic: i1.hdslb.com resolved to cdn20.com / 14.128.1.28 (JP)
# instead of Tencent's 43.171.80.28, which then missed GEOIP,CN and got proxied.
DNS_OVERSEAS = ("1.1.1.1", "8.8.8.8", "8.8.4.4", "cloudflare-dns.com", "dns.google")

DNS_OVERRIDE = {
    "enable": True,
    "listen": "0.0.0.0:5353",
    "ipv6": False,
    "enhanced-mode": "normal",
    "respect-rules": True,
    "use-hosts": True,
    "use-system-hosts": True,
    "default-nameserver": ["223.5.5.5", "119.29.29.29"],
    "proxy-server-nameserver": ["https://223.5.5.5/dns-query"],
    "nameserver": list(DNS_RESOLVERS),
    "direct-nameserver": list(DNS_RESOLVERS),
    # Domestic resolvers answer blocked names with sinkhole IPs (223.5.5.5
    # returns 31.13.73.9 for chatgpt.com), which breaks the AI group's health
    # check. Those names only get a trustworthy answer from a resolver reached
    # through `出口`, so scope the overseas resolver to them instead of
    # falling back to it globally.
    "nameserver-policy": {
        f"+.{d}": ["https://1.1.1.1/dns-query"] for d in AI_DOMAINS
    },
}

SNIFFER = {
    "enable": True,
    "force-dns-mapping": True,
    "parse-pure-ip": True,
    # Peers resolve names themselves and hand us whatever IP they got; a
    # poisoned answer (chatgpt.com -> 104.244.46.21, 69.171.224.36) makes the
    # exit node dial the wrong host and the TLS handshake is closed
    # (schannel: "failed to receive handshake"). Overriding the destination
    # with the sniffed SNI makes the exit node dial the real domain instead.
    "override-destination": True,
    "sniff": {
        "TLS": {"ports": [443, 8443]},
        "HTTP": {"ports": [80, "8080-8880"]},
        "QUIC": {"ports": [443, 8443]},
    },
}


def _score_table() -> dict:
    """name -> (final, {platform: score + region bonus}); {} when missing/stale."""
    try:
        if time.time() - os.path.getmtime(SCORES) > SCORES_MAX_AGE:
            return {}
        with open(SCORES, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    table = {}
    for name, info in (doc.get("nodes") or {}).items():
        try:
            bonus = float(info.get("bonus") or 0.0)
            platforms = {
                key: float(cell.get("score") or 0.0) + bonus
                for key, cell in (info.get("platforms") or {}).items()
            }
            final = float(info.get("final") or 0.0)
        except (AttributeError, TypeError, ValueError):
            # 快照被外部改坏时只丢这一条，不让整轮刷新失败
            print(f"note: ignoring malformed score entry {name!r}", file=sys.stderr)
            continue
        table[str(name)] = (final, platforms)
    return table


def _rank(nodes: list, scores: dict, platform=None) -> list:
    """Best score first; ties and unscored nodes keep the subscription order."""
    if not scores:
        return list(nodes)

    def key(name: str):
        entry = scores.get(name)
        if entry is None:
            return (1, 0.0)
        final, platforms = entry
        return (0, -(platforms.get(platform, 0.0) if platform else final))

    return sorted(nodes, key=key)


def main(dst: str, srcs: list) -> None:
    docs = []
    for path in srcs:
        tag = os.path.splitext(os.path.basename(path))[0]
        docs.append((tag, _load_source(path, tag)))

    # The first pool entry is the skeleton: its proxy-groups and rules are
    # kept as-is, and the later entries only add nodes to the pool. Two pools
    # declaring the same group name would otherwise fail the config check.
    cfg = docs[0][1]
    proxies = _merge([(tag, doc.get("proxies") or []) for tag, doc in docs])
    names = [str(p["name"]) for p in proxies]
    # Per-entry sanity lives in _load_source; this is the pool-level guard: a
    # refresh that lost every node must not install a config whose fallback
    # groups have nothing to dial.
    if len(names) < MIN_PROXIES:
        raise SystemExit(f"rejected: pool has only {len(names)} proxies (< {MIN_PROXIES})")

    for key in (
        "port",
        "socks-port",
        "redir-port",
        "tproxy-port",
        "listeners",
        "tun",
        "secret",
        "authentication",
        "external-controller-tls",
        "proxy-providers",
        "rule-providers",
    ):
        cfg.pop(key, None)

    cfg["mixed-port"] = MIXED_PORT
    cfg["allow-lan"] = True
    cfg["bind-address"] = "*"
    cfg["lan-allowed-ips"] = ["127.0.0.0/8", "10.0.0.0/8"]
    # Loopback only: nginx terminates TLS on 443 and proxies /dns-query here.
    # The DoH handler is mounted outside the secret-authenticated group, so it
    # must never be reachable from anything but the local nginx.
    cfg["external-controller"] = API_LISTEN
    cfg["external-doh-server"] = "/dns-query"
    if not API_SECRET:
        raise SystemExit("rejected: MIHOMO_SECRET is empty - the API would be unauthenticated")
    cfg["secret"] = API_SECRET
    cfg["external-controller-cors"] = {
        "allow-origins": ["*"],
        "allow-private-network": True,
    }
    cfg["ipv6"] = False
    cfg["geo-auto-update"] = False
    cfg["profile"] = {"store-selected": True, "store-fake-ip": True}
    cfg["tproxy-port"] = int(TPROXY_PORT) if TPROXY_PORT else None
    if cfg["tproxy-port"] is None:
        cfg.pop("tproxy-port")
    cfg["sniffer"] = copy.deepcopy(SNIFFER)
    cfg["dns"] = copy.deepcopy(DNS_OVERRIDE)

    # --- exit group: first healthy node in subscription order -------------
    # Host-only nodes lead the list: a fallback group serves traffic from its
    # first healthy member, so file order is the actual preference order.
    local = [p for p in _local_nodes() if not _blocked(str(p["name"]))]
    local_names = [str(p["name"]) for p in local]
    nodes = local_names + [
        n for n in names if n and not _blocked(n) and n not in local_names
    ]
    if not nodes:
        raise SystemExit("rejected: no usable nodes for exit group")

    groups = []
    for g in cfg.get("proxy-groups") or []:
        if not isinstance(g, dict) or g.get("name") in (EXIT_GROUP, AI_GROUP, SITE_GROUP):
            continue
        members = g.get("proxies")
        if isinstance(members, list):
            g["proxies"] = [p for p in members if not _blocked(str(p))]
            if not g["proxies"]:
                continue  # nothing selectable left in this group
        if g.get("type") in ("url-test", "fallback", "load-balance"):
            g.setdefault("url", HEALTH_URL)
            interval = g.get("interval")
            if not isinstance(interval, int) or interval > HEALTH_INTERVAL:
                g["interval"] = HEALTH_INTERVAL
        groups.append(g)

    scores = _score_table()
    if not scores:
        print(
            f"note: {os.path.basename(SCORES)} missing or older than "
            f"{SCORES_MAX_AGE // 3600}h - keeping subscription order",
            file=sys.stderr,
        )
    elif not any(n in scores for n in nodes):
        print(
            "note: no node in the pool has a score (names changed?) - "
            "keeping subscription order",
            file=sys.stderr,
        )

    exit_group = {
        "name": EXIT_GROUP,
        "type": "fallback",
        "proxies": _rank(nodes, scores),
        "url": HEALTH_URL,
        "interval": HEALTH_INTERVAL,
        "timeout": 3000,
        "max-failed-times": 3,
        "expected-status": 204,
        "lazy": False,
    }
    ai_group = {
        "name": AI_GROUP,
        "type": "fallback",
        "proxies": _rank(nodes, scores, "ai"),
        "url": AI_HEALTH_URL,
        "interval": HEALTH_INTERVAL,
        "timeout": 5000,
        "max-failed-times": 3,
        "expected-status": AI_HEALTH_STATUS,
        "lazy": False,
    }
    site_group = {
        "name": SITE_GROUP,
        "type": "fallback",
        "proxies": _rank(nodes, scores, "site"),
        "url": SITE_HEALTH_URL,
        "interval": HEALTH_INTERVAL,
        "timeout": 5000,
        "max-failed-times": 3,
        "expected-status": SITE_HEALTH_STATUS,
        "lazy": False,
    }
    cfg["proxy-groups"] = (
        [exit_group, ai_group] + ([site_group] if SITE_ENABLED else []) + groups
    )

    # --- rules: AI domains -> AI出口, CN statics -> DIRECT, MATCH -> 出口 ---
    rules = [r for r in (cfg.get("rules") or []) if isinstance(r, str)]
    rules = [r for r in rules if r.split(",")[0].strip().upper() != "MATCH"]
    # drop rules this file owns so a re-merge stays idempotent
    owned = (AI_GROUP, SITE_GROUP)
    rules = [
        r
        for r in rules
        if not set(owned) & {t.strip() for t in r.split(",")}
    ]
    rules = [r for r in rules if r not in CN_DIRECT_RULES]
    rules = [r for r in rules if r not in STEAM_RULES]
    rules = [
        (
            f"IP-CIDR,1.1.1.1/32,{EXIT_GROUP},no-resolve"
            if r.split(",")[0].strip().upper() == "IP-CIDR"
            and r.split(",")[1].strip() == "1.1.1.1/32"
            else r
        )
        for r in rules
    ]
    rules.append(f"MATCH,{EXIT_GROUP}")
    cfg["rules"] = list(PINNED_RULES) + rules

    # banned regions and pseudo-nodes never reach the node table at all
    cfg["proxies"] = local + [
        p
        for p in proxies
        if not _blocked(str(p.get("name", ""))) and str(p.get("name", "")) not in local_names
    ]

    _assert_policy(cfg)

    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)
    os.replace(tmp, dst)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(
            f"usage: {sys.argv[0]} <output.yaml> <subscription.yaml> [more.yaml ...]"
        )
    main(sys.argv[1], sys.argv[2:])
