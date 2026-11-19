# 阶段计划（数据层优先）

原则：数据层先落地并自证（可校验、可重复、可回放），再做集成层，最后动 UI。每一步都要有可执行的验收命令，不靠"看起来对"。

## P0 仓库骨架与数据契约（本次）

交付：

* 仓库结构、`.gitignore`（运行期数据与密钥不入库）
* `schemas/scores.schema.json`、`schemas/subscriptions.schema.json`
* `tools/validate.py`（`scores` / `subs` / `tree` 三个子命令，只读、不打印令牌）
* `tests/fixtures/*` 与 `tests/test_validate.py`
* `docs/DATA.md`、`docs/DECISIONS.md`、`docs/PLAN.md`

验收：

```
python3 -m unittest discover -s tests -v          # 全绿
python3 tools/validate.py scores tests/fixtures/scores.good.json
python3 tools/validate.py subs   tests/fixtures/subscriptions.good.yaml
python3 tools/validate.py subs   tests/fixtures/subscriptions.bad.yaml ; test $? -eq 1
```

## P1 目录分区与迁移

目标：把扁平目录拆成 `secrets/ assets/ artifacts/ cache/ archive/ last-failure/`，并收紧权限。

交付：`tools/migrate_layout.py`（幂等）、更新后的 compose 挂载矩阵、`docs/DATA.md` 的迁移段落实测记录。

验收：

```
python3 tools/migrate_layout.py --data-dir "$DATA_DIR" --dry-run   # 打印计划，无写操作
python3 tools/migrate_layout.py --data-dir "$DATA_DIR"             # 执行
python3 tools/migrate_layout.py --data-dir "$DATA_DIR"             # 再跑一次：no-op
python3 tools/validate.py tree "$DATA_DIR"
<内核二进制> -t -d "$DATA_DIR/artifacts" -f "$DATA_DIR/artifacts/config.yaml"
curl -s -H "Authorization: Bearer $secret" http://127.0.0.1:$api_port/version
```

## P2 稳定节点身份

目标：评分与排序不再依赖显示名。`identity.json` 记录 `stable_id -> 显示名` 的当代表；评分快照以 `stable_id` 为键、附显示名。

验收：

* 同一订阅连续两轮合并，`identity.json` 的 `stable_id` 集合不变。
* 人为把某节点显示名加后缀，重跑评分后其历史分数仍落在同一 `stable_id` 上。

## P3 原子写与并发锁

目标：所有写入走 `tmp + rename + fsync(dir)`，整轮更新持 `flock`。

验收：

* 并发跑两次更新：第二次立即退出（锁占用），配置文件无损坏。
* 在写入窗口内反复读取目标文件，永远读到完整 JSON/YAML（可用 `tests/test_atomic.py` 的循环读校验）。

## P4 校验接入流水线

目标：合并与评分写完即校验；校验失败不安装、不改动当前配置。

验收：

* 注入一份坏快照 → 合并拒绝执行、旧配置与旧快照保持可用。
* 注入权重和 ≠ 1 的快照 → 同上。

## P5 归档与留存

目标：评分快照环形归档 7 份；`last-failure/` 成功即清；日志交给容器轮转。

验收：连续跑 9 轮（可用伪造时间戳驱动），归档目录始终 ≤7 份；成功一轮后 `last-failure/` 不存在。

## P6 数据层验收（端到端）

目标：用真实订阅跑一轮完整流程，确认数据层所有不变量成立。

验收：

```
§ 生成的配置能过内核 -t
§ 三组组内顺序来自最新快照（分数降序 + 偏好地区加分）
§ 面板/API 报告的分组顺序与文件一致
§ secrets/ 在 shell 容器内只读；core 读不到 secrets/
§ 归档目录有且仅有 ≤7 份快照
```

---

## 后续（数据层之外，仅登记不排期）

* 集成层：shell 容器化、面板替换、订阅域名定向规则、评分可见性、通知。
* 事务层：两阶段重载（T2 待实测）、覆盖率下限、预热失败降级。
* UI 层：面板选型与访问控制、手选边界。
