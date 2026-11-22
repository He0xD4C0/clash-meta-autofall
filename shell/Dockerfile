# shell 容器：只做运行时，不含策略。
#  - 业务脚本 bind mount 进 /opt/shell（改脚本不用重建镜像）
#  - 内核二进制作进镜像，供 `mihomo -t` 校验生成的配置
#  - 不挂 docker.sock：容器不能重启别的容器，重载一律走内核 API
ARG CORE_IMAGE=metacubex/mihomo:v1.19.33
# 上游镜像的二进制路径是 /mihomo；本机构建的同版本镜像在 /usr/local/bin/mihomo
ARG CORE_BIN=/mihomo
FROM ${CORE_IMAGE} AS core

FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf

# COPY 里引用的 ARG 必须在所在 stage 内重新声明，否则会被展开成空串
ARG CORE_BIN=/mihomo

RUN apk add --no-cache \
      bash \
      curl \
      python3 \
      py3-yaml \
      py3-jsonschema \
      tzdata \
    && mkdir -p /opt/shell /opt/tools /data

COPY --from=core ${CORE_BIN} /usr/local/bin/mihomo

# 入口脚本烘进镜像（基础设施，不随策略改动）；策略脚本由 compose 挂载到 /opt/shell
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# 不要用管道包住版本检查：管道会吞掉退出码，二进制缺失时构建仍然"成功"
RUN chmod +x /usr/local/bin/entrypoint.sh && mihomo -v

ENV TZ=Asia/Shanghai \
    DATA_DIR=/data \
    SHELL_DIR=/opt/shell \
    TOOLS_DIR=/opt/tools \
    REFRESH_INTERVAL=21600

WORKDIR /data
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["update"]
