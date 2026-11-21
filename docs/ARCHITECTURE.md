# 架构（路线 B：core + shell + panel）

## 目标

* **core** 只跑内核：无 shell、只读根文件系统、仅保留内核所需 capability。
* **shell** 承担全部管理：拉订阅、合并策略、评分、校验、热重载、定时刷新。
* **panel** 只提供 Web 面板，不含管理逻辑。

## 依赖链

```
            ┌──────────── 浏览器 ────────────┐
            │                                │
        (TLS 反代)                      (TLS 反代)
            │                                │
    panel :panel-port          ui :ui-port（评分/纯净度/选路）
            │                                │        ▲
            └──────────► core :api-port ◄────┘        │
                              ▲                       │
                          shell（只走 loopback）───────┘
                              │
                订阅地址 ◄─────┘（proxy → direct 回退）
                              │
                    artifacts/config.yaml ──► core（热重载）
                              │
                    artifacts/probe-config.yaml ──► probe-core（global 模式）
                              │                              ▲
                        purity.py ──(逐节点切换 GLOBAL)──────┘
```

* **probe-core**：同一批节点、`mode: global`、无规则的第二个内核。主内核有规则与
  fallback 组，无法表达"这条请求走某个节点"；global 模式下切 `GLOBAL` 就能按节点发请求，
  这是拿到节点真实出口 IP 的唯一可靠办法。
* **scheduler**（并进 shell 容器）：两个独立循环——5 分钟巡检、6 小时
  `拉订阅 → 合并 → 纯净度 → 评分 → 重排 → 热重载`。不用 crontab，间隔全来自环境变量。
* **ui**：自建只读面板，聚合评分与纯净度（现成内核面板只看得到组和节点）。

## 边界

| 职责 | 归属 | 不允许 |
| --- | --- | --- |
| 出站/入站代理、规则匹配、DNS 服务 | core | panel 或 shell 直接转发流量 |
| 订阅抓取、合并、评分、校验、热重载 | shell | 人手工改生成物；panel 触发写入 |
| 展示与手动切换 | panel | 持久化任何策略（策略只在 shell 里） |
| 评分、纯净度、巡检、周期任务 | shell | 由 panel/ui 触发写入 |
| 按节点发请求测出口 IP | probe-core | 承载真实业务流量（它只是探测实例） |

## 故障矩阵

| 故障 | 影响 | 自动恢复 |
| --- | --- | --- |
| core 崩溃 | 全部代理中断 | core 自身 restart 策略；shell 用直连继续拉订阅，重载失败则保留旧文件 |
| shell 崩溃 | 保留最后一份配置继续工作，无刷新 | shell 自身 restart 策略；拉起后补跑一次 |
| panel 崩溃 | 仅 UI 不可用 | panel 自身 restart 策略 |
| 订阅源不可用 | 该源节点缺失，其余照常 | 覆盖率下限触发中止（见 PLAN） |
| 热重载被拒 | 继续用内存中的旧配置 | 保留旧文件，下一轮重试 |
| 选中节点劣化 | 单站点不可用，其余照常 | watchdog 5 分钟巡检，≥半数目标失败即强制组内重选 |
| 纯净度接口不可用 | 评分退化为纯连通性 | purity 快照过期即被忽略（12h），不阻塞评分 |

## 与出口容器的关系

出口流量拦截（nft/fwmark/tproxy）在独立栈里，只依赖 core 的 tproxy 端口，不读配置、不需要 shell 参与。本仓库的改动不得要求出口栈配合修改。
