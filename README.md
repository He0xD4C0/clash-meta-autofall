# mihomo-stack

> **此栈已经迁移到[zashboard-autofallback](https://github.com/He0xD4C0/zashboard-autofallback.git)，本项目归档**

一个自托管的 mihomo 运行栈：**core 容器只跑内核**，订阅抓取、配置生成、节点评分与热重载由独立的 **shell 容器**承担，**panel** 提供定制 Zashboard，含节点控制和评分、纯净度、巡检诊断，**probe-core** 是只在测出口 IP 时使用的 global 模式内核。

节点评价由三部分组成：多站点连通性（可用性/延迟/抖动）、IP 纯净度（住宅/机房/被判定代理，含地区虚标校验）、地区偏好加成；结果写进 `artifacts/scores.json` 并决定组内顺序。选中节点由 `tools/watchdog.py` 每 5 分钟巡检，失败即触发组内重选。

本仓库只放代码、schema 与文档。**运行期数据不在仓库里**（含订阅令牌与节点密码），通过 `DATA_DIR` 指向的目录挂载进容器。

## 目录

```
compose.yaml          # core + probe-core + shell + panel + 一次性 init
rules/                # 规则层快照（来自 HenryChiao/MIHOMO_YAMLS，见 rules/SOURCE.md）
shell/                # 管理逻辑：拉订阅 / 合并 / 评分 / 热重载（bind mount 进 shell 容器）
tools/                # purity（纯净度）/ watchdog（巡检）/ diagnostics（诊断快照）/ validate（契约校验）
tools/validate.py     # 数据校验（schema 校验 CLI）
schemas/              # 数据契约：scores.json / 订阅池清单
tests/                # 单元测试与 fixture
docs/                 # 架构、数据契约、决策记录、阶段计划
```

## 数据目录（DATA_DIR）

```
secrets/      订阅令牌、手工节点（0600，只读挂载给 shell）
artifacts/    生成的 config.yaml / probe-config.yaml / scores.json / purity.json（读写）
cache/        内核缓存（fake-ip / 选中记忆），只给 core
subscription-cache/  各源最近一次通过校验的订阅，仅供 shell 使用
archive/      评分归档环形保留
last-failure/ 最近一次失败的现场（成功即清）
```

## 快速开始（开发栈）

```
cp .env.example .env            # 填 DEV_API_SECRET
bash tools/seed_dev.sh          # 播种 geo 文件与订阅池（一次性）
docker compose up -d --build --remove-orphans  # init 先验证配置和规则，core 才启动
docker compose run --rm shell update   # 也可等 shell 的 scheduler 自己跑
```

端口只发布到 loopback（默认 19090 API / 17897 mixed / 19091 面板 / 19092 探测内核 API）。
访问 `http://127.0.0.1:19091`，配置后端为面板的同源地址，路径填 `/mihomo-api`，
密码填 `DEV_API_SECRET`。通过导航的「诊断」查看评分、纯净度和巡检。
面板构建固定到 [Zashboard fork](https://github.com/He0xD4C0/zashboard-autofallback) 的提交。
开发时用 `ZASHBOARD_CONTEXT` 指向本地检出。

自定义网桥时一并设置 `MIHOMO_BRIDGE_SUBNET` 和 `MIHOMO_BRIDGE_GATEWAY`。
容器识别到的网关会加入 mixed 端口的来源白名单。首次启动需要订阅和规则源可达；
已有有效配置和规则时，即使订阅源离线也可原样启动。

如果容器网桥无法访问规则源，在可直连规则源的机器上准备规则包。该机器需要 `curl`、
同版本的 `mihomo` 和 `requirements-dev.txt` 中的 Python 依赖。在运行 Compose 的宿主机上，
将生成的 `rule-providers/` 目录复制到数据卷后再启动。生成和导入使用同一版本的规则清单。

```bash
RULE_BUNDLE_DIR=$(mktemp -d)
python3 tools/rules.py seed rules/henrychiao.yaml "$RULE_BUNDLE_DIR"
# 若在其他机器生成，先把目录复制到部署宿主机，再设置 RULE_BUNDLE_DIR。
docker run --rm --network none -v mihomo-stack_data:/data \
  -v "$RULE_BUNDLE_DIR/rule-providers:/seed:ro" alpine:3.24 \
  sh -ec 'mkdir -p /data/cache/rule-providers && cp -a /seed/. /data/cache/rule-providers/'
```

| 服务 | 作用 | 周期 |
| --- | --- | --- |
| `core` | 内核，唯一承载业务流量 | 常驻 |
| `probe-core` | 同批节点、`mode: global`，只用于逐节点测出口 IP | 常驻（按需切 GLOBAL） |
| `shell` | 订阅→合并→纯净度→评分→重排→热重载；5 分钟巡检 | 300s / 21600s |
| `panel` | Zashboard（手动切组、看连接、评分与巡检诊断） | 常驻 |
内核镜像默认取上游 `metacubex/mihomo:<version>`；用本机构建的同版本镜像时，同时覆盖
`CORE_IMAGE` 与 `CORE_BIN`（二进制路径不同），见 `.env.example`。

## 校验

```
python3 tools/validate.py subs   "$DATA_DIR/secrets/subscriptions.yaml"
python3 tools/validate.py scores "$DATA_DIR/artifacts/scores.json"
python3 -m unittest discover -s tests -v
```

## 状态

数据层选栈与阶段计划见 `docs/DATA.md`、`docs/PLAN.md`，决策记录见 `docs/DECISIONS.md`。
尚未实现的阶段在计划里标注为未开始，不要把未完成的函数留成空壳。
