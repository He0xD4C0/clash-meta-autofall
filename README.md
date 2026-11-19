# mihomo-stack

一个自托管的 mihomo 运行栈：**core 容器只跑内核**，订阅抓取、配置生成、节点评分与热重载由独立的 **shell 容器**承担，**panel 容器**只提供 Web 面板。

本仓库只放代码、schema 与文档。**运行期数据不在仓库里**（含订阅令牌与节点密码），通过 `DATA_DIR` 指向的目录挂载进容器。

## 目录

```
compose.yaml          # core + shell + panel 三服务
shell/                # 管理逻辑：拉订阅 / 合并 / 评分 / 热重载（bind mount 进 shell 容器）
tools/validate.py     # 数据校验（schema 校验 CLI）
schemas/              # 数据契约：scores.json / 订阅池清单
tests/                # 单元测试与 fixture
docs/                 # 架构、数据契约、决策记录、阶段计划
```

## 数据目录（DATA_DIR）

```
secrets/      订阅令牌、手工节点（0600，只读挂载给 shell）
artifacts/    生成的 config.yaml、scores.json、评分归档（读写）
cache/        内核缓存（fake-ip / 选中记忆），只给 core
archive/      评分归档环形保留
last-failure/ 最近一次失败的现场（成功即清）
```

## 快速开始（开发栈）

```
cp .env.example .env            # 填 DEV_API_SECRET
bash tools/seed_dev.sh          # 播种 geo 文件与订阅池（一次性）
docker compose up -d --build    # init 服务先落盘配置，core 才启动
docker compose run --rm shell update   # 也可等 shell 的 loop 自己跑
```

端口只发布到 loopback（默认 19090 API / 17897 mixed / 19091 面板），与生产栈不冲突。
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
