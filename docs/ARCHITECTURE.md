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
         panel :panel-port            api 子路径 ──► core :api-port
                                                 ▲
                                            shell（只走 loopback）
                                                 │
                                    订阅地址 ◄────┘（proxy → direct 回退）
                                                 │
                                          artifacts/config.yaml
                                                 │
                                               core（热重载读取）
```

## 边界

| 职责 | 归属 | 不允许 |
| --- | --- | --- |
| 出站/入站代理、规则匹配、DNS 服务 | core | panel 或 shell 直接转发流量 |
| 订阅抓取、合并、评分、校验、热重载 | shell | 人手工改生成物；panel 触发写入 |
| 展示与手动切换 | panel | 持久化任何策略（策略只在 shell 里） |

## 故障矩阵

| 故障 | 影响 | 自动恢复 |
| --- | --- | --- |
| core 崩溃 | 全部代理中断 | core 自身 restart 策略；shell 用直连继续拉订阅，重载失败则保留旧文件 |
| shell 崩溃 | 保留最后一份配置继续工作，无刷新 | shell 自身 restart 策略；拉起后补跑一次 |
| panel 崩溃 | 仅 UI 不可用 | panel 自身 restart 策略 |
| 订阅源不可用 | 该源节点缺失，其余照常 | 覆盖率下限触发中止（见 PLAN） |
| 热重载被拒 | 继续用内存中的旧配置 | 保留旧文件，下一轮重试 |

## 与出口容器的关系

出口流量拦截（nft/fwmark/tproxy）在独立栈里，只依赖 core 的 tproxy 端口，不读配置、不需要 shell 参与。本仓库的改动不得要求出口栈配合修改。
