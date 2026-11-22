# 规则层来源与更新

## 来源

* 上游仓库：`HenryChiao/MIHOMO_YAMLS`（AGPL-3.0），文件 `THEYAMLS/General_Config/HenryChiao/MihomoProMax.yaml`
* 本仓库只取其中的 **`rule-providers`（43 个）与 `rules`（45 条）**，其余一律不用：
  上游的 54 个策略组、DNS、端口、TUN、hosts、profile 等由本项目自己的 `shell/merge.py` 决定。
* 当前快照对应的上游提交：`39fc598d3ed20530da711f13ab6a7a2e98aa6cb7`（2026-11-20 抓取）
* 快照文件：`rules/henrychiao.yaml`（规则里的目标组名保持上游原样）

## 目标组映射（在 `shell/merge.py` 的 `TARGET_MAP` 里）

| 上游目标 | 本项目落点 | 说明 |
| --- | --- | --- |
| `直接连接` / `国内流量` / `DIRECT` / `抖快书定位` | `DIRECT` | 国内与局域网类，直连 |
| `人工智能` | `AI出口` | 复用已有的 AI fallback 组（按 chatgpt 探测） |
| `REJECT` | `REJECT` | 广告/追踪拦截 |
| 其余（`国外流量`/`兜底流量`/`默认代理`/`国际媒体`/`油管视频`/`奈飞视频`/`Emby服`…） | `出口` | 默认走主出口组 |

映射表是本项目的一处策略决定，改它等于改流量归属，改动前先看一遍 `docs/DECISIONS.md` 里 U3 的记录。

## 规则集文件

* 对实际被配置引用的规则，shell 下载并在独立内核中校验，随后以本地文件形式交给业务内核。
* GitHub 的仓库 `/raw/` 路径按同一提交或分支直连 `raw.githubusercontent.com`。GitHub 专用规则采用 [blackmatrix7 的 Clash GitHub 规则](https://github.com/blackmatrix7/ios_rule_script/tree/master/rule/Clash/GitHub)，保留原策略组映射。
* UK WiFi 规则使用原仓库迁移后的 [UK-wifi-call.list](https://github.com/HenryChiao/wificalling/blob/main/rules/UK-wifi-call.list)。
* 启动和刷新复用完整的已验证规则包。新规则下载或校验失败时继续使用原版本。检查运行时各规则是否加载可用 `docker compose run --rm shell validate`。

## 更新步骤

```bash
# 1) 取上游当前提交并下载
SHA=$(git ls-remote https://github.com/HenryChiao/MIHOMO_YAMLS.git refs/heads/main | awk '{print $1}')
curl -fsS "https://raw.githubusercontent.com/HenryChiao/MIHOMO_YAMLS/$SHA/THEYAMLS/General_Config/HenryChiao/MihomoProMax.yaml" -o /tmp/upstream.yaml

# 2) 只抽规则层（保留目标组名），写入快照
python3 - <<'PY'
import yaml
src = yaml.safe_load(open("/tmp/upstream.yaml", encoding="utf-8"))
doc = {"rules": src["rules"], "rule-providers": src["rule-providers"]}
print(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=120))
PY

# 3) 把输出人工合进 rules/henrychiao.yaml（保留文件头注释），并更新上面的提交号
# 4) 校验：内核 -t 必须通过
docker compose run --rm shell validate
```

## 许可

上游为 AGPL-3.0。本仓库只内嵌其规则清单（域名/规则集引用），不是代码派生；如需分发，保留本文件的来源与许可说明。
