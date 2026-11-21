# 集成层：容器化部署、卷持久化、升级路径

## 1. 与生产的隔离矩阵

开发栈与生产栈**不共享任何运行时资源**，因此可以并存：

| 维度 | 生产栈 | 开发栈（本仓库 compose） |
| --- | --- | --- |
| compose 项目 | `mihomo` | `mihomo-stack` |
| 网络 | host（需要 tproxy 与透明拦截） | bridge（不需要透明代理） |
| 端口 | 7897 / 7898 / 9090 / 9091 / 5353 | 127.0.0.1:17897 / 19090 / 19091 |
| 数据 | 宿主目录（平铺，待迁移） | 命名卷 `mihomo-stack-data` |
| capability | NET_ADMIN + NET_RAW + NET_BIND_SERVICE | 全部丢弃（cap_drop: ALL） |
| 入口 | 宿主 nginx（TLS + 反代） | 直连 loopback 端口 |
| 刷新驱动 | 宿主手动执行 | shell 容器内 loop（REFRESH_INTERVAL） |

结论：开发栈起来不会碰到生产的任何端口、卷、网络与容器。

## 2. 服务拓扑

```
 panel :19091 ──(浏览器直连 API)──┐        ui :19093 ──(读快照 + API)──┐
                                  ▼                                  ▼
                             core :19090 (API) / :17897 (mixed，仅 loopback 发布)
                                  ▲                                  ▲
                         shell（唯一写方：拉订阅/合并/纯净度/评分/巡检/热重载）
                                  │
                     probe-core :19092（global 模式，逐节点测出口 IP）
                                  │
                       卷 mihomo-stack-data:/data
```

* **core** 只跑内核：`-d /data/cache -f /data/artifacts/config.yaml`，只读根文件系统。
* **shell** 只跑运行时（alpine + python3 + pyyaml + curl + 内核二进制），业务脚本以只读卷
  `./shell:/opt/shell` 注入，**改策略不重建镜像**。
* 规则层以只读卷 `./rules:/opt/rules` 注入（`MIHOMO_RULESET=/opt/rules/henrychiao.yaml`）；
  `MIHOMO_SUB_RULES=0` 表示只用 curated 规则，`MIHOMO_RULESET_PROXY` 控制 `.mrs` 的下载路径（默认 DIRECT）。
* **panel** 用上游面板镜像，无服务端出网需求，端口只发布到 loopback。
* **probe-core** 与 core 同镜像，但 `mode: global`、无规则、DNS 关闭；配置由
  `shell/make_probe_config.py` 从主配置派生，节点表变化后由 `tools/reload.py` payload 热重载。
  它的混合端口必须 `allow-lan: true`（shell 容器在别的网段），API 只发布到 loopback。
* **ui** 与 shell 同镜像、只读根文件系统，只读 `artifacts/` 与 `run.log`，无写权限。
* 周期任务（`scheduler`）并进 shell 容器：两个独立循环，间隔来自
  `WATCHDOG_INTERVAL`（默认 300s）与 `REFRESH_INTERVAL`（默认 21600s）。

## 3. 卷与持久化

命名卷 `mihomo-stack-data` 挂到 `/data`，子目录即数据契约（见 `docs/DATA.md`）：

```
/data/secrets/       订阅池与手工节点（0600）
/data/assets/        geoip.metadb / geosite.dat（由 tools/seed_dev.sh 播种）
/data/artifacts/     生成的 config.yaml、probe-config.yaml、scores.json、purity.json
/data/probe/         probe-core 的 -d 目录（cache.db）
/data/cache/         内核的 -d 目录：cache.db 与 geo 副本
/data/archive/       评分/纯净度归档（各最多 7 份）
/data/run.log        巡检与调度日志（单行 JSON，ui 读取）
/data/last-failure/  失败现场（KEEP_FAILURE=1 时）
```

持久化的含义：`docker compose down` 后数据仍在；只有 `docker compose down -v` 才会销毁。

## 4. 升级路径

内核升级（最常见）：

```
1. 改 .env 里的 CORE_IMAGE=<新版本>
2. docker compose pull core          # 或本机构建后 docker tag
3. docker compose up -d core         # 仅重建 core，卷不变
4. docker compose run --rm shell validate   # 内核 -t + 数据契约校验
5. docker compose logs --tail=50 core        # 确认没有配置被拒
```

shell 镜像升级：改 `shell/Dockerfile` 或镜像 tag → `docker compose build shell && docker compose up -d shell`。
脚本类改动（merge/score/update 逻辑）不需要重建镜像，改完 `docker compose restart shell` 即可。

面板升级：`docker compose pull panel && docker compose up -d panel`。

原则：**镜像版本与数据卷解耦**。升级只替换容器，`/data` 始终复用；失败时回退到旧镜像 tag 即可，不需要配置回滚。

## 5. 集成层决策（对应 DECISIONS.md 的 I 段）

| ID | 结论 | 状态 |
| --- | --- | --- |
| I1 | shell 从生成的配置里解析 API secret；不注入为配置文件内容之外的来源 | 已实现 |
| I2 | 面板到内核走 API 端口（开发栈直连 loopback；生产走 nginx 子路径） | 已实现（开发栈）/ 待生产切换 |
| I3 | 拉订阅：先走环境代理，失败回退直连；订阅域名定向规则留待生产 | 已实现 |
| I4 | 评分覆盖范围（是否含 UDP/QUIC） | 待确认 |
| I5 | 评分可见性（静态报告页） | 未开始 |
| I6 | 失败通知（webhook） | 未开始 |
| I7 | DoH 与 API 同端口的暴露面 | 维持现状 |

## 6. 已知限制（开发栈）

* 没有透明代理与出口容器：`MIHOMO_TPROXY_PORT` 留空，生成的配置不含 `tproxy-port`。
* 没有 TLS 与反代：面板直接访问 `127.0.0.1:19091`，后端指向 `http://127.0.0.1:19090`（内核已开 CORS）。
* 离线播种的节点全部不可达：评分会得到 0 分，这是预期行为，用来验证"全 0 时排序退回订阅顺序"。

## 6.1 实测踩到的四个约束（已写进实现）

1. **热重载只能用 payload，不能用 path。** 内核只允许重载 `-d` 目录内的路径（`/data/artifacts` 不在其中，
   且 `SAFE_PATHS` 环境变量在本版本未生效），改成 `PUT /configs {"payload": <yaml>}` 后 204 成功。
   因为 payload 就是刚安装的文件内容，内存与磁盘不会分叉。
2. **首次启动顺序**：core 在配置尚不存在时会以内置默认配置启动（API 绑在 loopback，shell 连不上）。
   正确顺序是 `seed_dev.sh` → `shell update`（可一次性 `run --rm`）→ `up -d`；若已颠倒，`compose restart core` 即可。
3. **panel 镜像需要可写 `/data` 与 `/config`**（内部用 caddy 起静态站点），强制只读会 `exec ... operation not permitted`。
   面板不持有密钥（后端地址与 secret 存浏览器），故保留上游默认权限，仅加 `no-new-privileges`。
4. **评分只跑平台子集时必须重新归一权重**，否则 `scores.json` 的权重和不再等于 1.0、契约校验会失败。
   `score.py` 现在写出的是按当次子集归一后的权重。
5. **首次启动必须先把配置落盘再起内核**：内核在配置缺失时不会报错，而是自己写一个 16 字节的默认 stub
   （`mixed-port: 7890`）并绑 loopback 运行，导致 shell 既连不上 API 也没法热重载。现在由一次性
   `init` 服务（`command: ["init"]`）在 core 之前完成"合并 + 校验 + 落盘"，core 用
   `depends_on: init: condition: service_completed_successfully` 等它。
   "磁盘上有没有我们装的配置"用标记文件 `artifacts/.config-installed` 判断，不能用文件是否存在/大小
   （stub 也是文件）。
6. **`COPY --from` 引用的 ARG 必须在目标 stage 内重新声明**，否则会展开成空串并把目录当成文件复制过来；
   镜像里的版本检查也不能走管道（`| head -1` 会吞掉退出码，缺二进制时构建仍然"成功"）。
7. **tools 挂到 `/opt/tools` 后，schema 目录不再位于父目录**，容器内需要 `SCHEMAS_DIR=/opt/schemas`
   （compose 已挂载 `./schemas:/opt/schemas:ro`）。
8. **离线开发池用 `file://`**（curl 支持 file 协议），因此订阅池 schema 允许 `file://`；生产仍必须是
   http(s)，这一点写在 schema 的 description 里。

## 7. 常用命令

```
docker compose ps
docker compose run --rm shell update         # 手动跑一次完整事务
docker compose run --rm shell score          # 只重算评分
docker compose run --rm shell validate       # 内核 -t + 数据契约
docker compose logs -f shell
docker compose down                          # 停栈，卷保留
docker compose down -v                       # 停栈并销毁数据卷（不可恢复）
```
