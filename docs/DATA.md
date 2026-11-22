# 数据层：选栈与契约

## 1. 选栈结论

| 关注点 | 选择 | 理由 | 被否方案 |
| --- | --- | --- | --- |
| 存储介质 | 目录内的文本文件（YAML/JSON） | 单写者、数据量小（快照 <200KB、生成的配置 <100KB）、可 diff 可 review、故障时能直接看 | SQLite 主库：需要模式迁移、外部工具读不了、本项目没有关系查询需求；Redis：多一个常驻组件 |
| 写入原子性 | 同目录 `tmp` + `os.replace` + 目录 fsync | POSIX rename 同文件系统内原子，读方永远看不到半份文件 | 直接覆写（会留下截断窗口）；写数据库事务（杀鸡用牛刀） |
| 自authoring 数据格式 | YAML（订阅池、手工节点） | 人手改、可注释、顺序有语义 | JSON（不可注释）；TOML（生态里没人用，交接成本高） |
| 机器数据格式 | JSON（评分快照、身份映射） | 无注释需求、被脚本整体读写、schema 校验成熟 | NDJSON（本项目不是追加流） |
| 校验 | JSON Schema draft-07 + `jsonschema` | 已在主机就绪；契约文件可被 CI 与运行时共用 | 自写断言（散落各处、无单一契约）；pydantic（为一个快照引入模型层，收益不抵依赖） |
| 生成的配置校验 | 只认内核 `-t` | 配置字段的真源是内核，复制一份规则必然漂移 | 自建 config schema |
| 节点身份 | 显示名 + `stable_id`（type/server/port/sni 的 sha256 前 16 位） | 订阅改显示名不再丢历史分数；显示名仍给人看 | 只用显示名（改名即丢分）；只用 IP:端口（同 IP 多节点会撞） |
| 时间序列 | 最新快照 + 环形归档（gzip，保留 7 份） | 能回答"是变差还是抖动"，且不引入时序库 | 只留最新（无法判断漂移）；Prometheus（另起监控栈，超出数据层） |
| 并发 | 单写者 `flock` | 定时刷新与手动更新可能撞车 | 无锁（两次 merge 交错会互相覆盖） |
| 密钥载体 | 0600 文件，放 `secrets/` | 单机、无 Swarm；docker secrets 在 compose 非 swarm 模式下也只是文件 | 环境变量（会进 `docker inspect`）；外部 vault（多一个单点） |
| 内核缓存 | 内核自管（`cache.db`），不接管 | fake-ip 与选中记忆是内核私有格式 | 迁移到自有 schema（无收益、易被内核版本破坏） |
| 日志 | 容器 json-file 轮转（大小×份数上限） | 宿主不再产生旁路日志 | 宿主 syslog/文件日志（多一处需要清理的东西） |

## 2. 数据契约

| 文件 | 写者 | 读者 | 权限 | 原子性 | 校验 |
| --- | --- | --- | --- | --- | --- |
| `secrets/subscriptions.yaml` | 人 | shell | 0600 | 人工保存即可 | `subscriptions.schema.json` + 名字唯一 |
| `secrets/local-nodes.yaml` | 人 | shell | 0600 | 人工保存即可 | 结构校验（节点列表） |
| `artifacts/scores.json` | shell（评分） | shell（排序）、人 | 0644 | tmp + rename | `scores.schema.json` + 权重和 ≈1 |
| `artifacts/identity.json` | shell（合并） | shell（评分/排序） | 0644 | tmp + rename | `stable_id` 唯一 |
| `artifacts/config.yaml` | shell（合并） | core | 0600 | tmp + rename | 内核 `-t`（唯一权威） |
| `artifacts/purity.json` | shell（纯净度探测） | shell（评分） | 0644 | tmp + rename | 结构校验；过期（>12h）即忽略 |
| `artifacts/probe-config.yaml` | shell（由主配置派生） | probe-core | 0600 | tmp + rename | 内核 `-t`（global 模式、无规则） |
| `run.log` | shell（巡检/调度） | shell（诊断快照）、人 | 0644 | 追加单行 JSON | 不校验（只读展示） |
| `artifacts/archive/scores-*.json.gz` | shell | 人 | 0644 | 先写后改名 | 同上（归档前已校验） |
| `artifacts/archive/purity-*.json.gz` | shell | 人 | 0644 | 先写后改名 | 同上 |
| `cache/cache.db` | core | core | 0600 | 内核自管 | 不校验 |
| `last-failure/` | shell（仅在失败时） | 人 | 0700 | 目录整体保留 | 不校验 |

不变量（任何改动都不得违反）：

1. `artifacts/config.yaml` 是**产物**：只有 shell 写，任何手工修改都会被下一次合并覆盖。
2. 订阅池**顺序有语义**（fallback 的优先顺序），工具不得重排。
3. 节点显示名**冻结**：改名等于新节点（分数与面板选中记录都会断），改名必须与一次完整重评分同批进行。
4. `scores.json` 的 `platforms` 权重之和 + `purity_weight` 必须为 1.0（±0.01）；
   有纯净度数据时 `purity_weight` 默认 0.10，平台权重按比例让出。
5. 新鲜度阈值 = 2 × 刷新周期；超过即视为不可用，排序退回订阅顺序。
6. 密钥类文件（`secrets/`）不得进入任何归档、日志、报错信息。

## 3. 目录分区与挂载

```
DATA_DIR/
├── secrets/       0600  secrets ro  ──► shell（只读挂载）
├── assets/        内核数据文件 ro     ──► core / shell（校验时用）
├── artifacts/     生成物 rw  ◄──────► shell（读写）/ core（只读 config.yaml）
├── cache/         core 私有  ◄──────► core（读写）
├── archive/       归档     rw  ◄──────► shell
└── last-failure/  失败现场 rw  ◄──────► shell
```

面板单独挂载诊断卷，只读 `/diagnostics/state.json`。订阅缓存存放于 `subscription-cache/`，目录权限为 0700，文件权限为 0600。

挂载矩阵（数据卷）：

| 容器 | secrets | assets | artifacts | cache | archive | last-failure |
| --- | --- | --- | --- | --- | --- | --- |
| core | — | ro | ro | rw | — | — |
| shell | ro | ro | rw | — | rw | rw |
| probe-core | — | — | ro（probe-config） | 自有 `probe/` | — | — |
| panel | — | — | — | — | — | — |
| panel | — | — | — | — | — | — |

## 4. 从扁平目录迁移

当前所有文件平铺在一个目录里（`config.yaml`、`subscriptions.yaml`、`local-nodes.yaml`、`scores.json`、`cache.db`、`geoip.metadb`、`geosite.dat`）。迁移要求：

* 幂等：重复执行结果一致，已移动的文件不再产生副作用。
* 只搬不改：迁移不重写内容，不改权限以外的元数据；权限按上表收紧。
* 顺序：先建目录 → 移动 secrets → 移动 artifacts → 移动 cache → 更新 compose 挂载 → 用内核 `-t` 校验生成物 → 起 core → 校验 API 可达。
* 不留副本：迁移完成后旧路径不得残留同名文件（用户明确不要回滚备份）。
