# syntax=docker/dockerfile:1
# easy_ontology 多 target 构建：
#   base  = python3.11 + jdk17 + ontop-cli（dev/prod 共用基底）
#   dev   = 依赖内置、代码 bind mount、uvicorn 热重载（日常开发）
#   playground-build = node20 构建 Playground 静态产物（仅 prod 用）
#   prod  = 源码+Playground 产物打进镜像（正式交付）

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG NPM_REGISTRY=https://registry.npmmirror.com

############################
# jdk：Temurin 17 整目录 COPY（免 apt——大体积 JDK 安装步骤在部分环境不稳；
# 含 javac 供 DbPing 容器内编译，jammy 的 glibc 与 bookworm 向下兼容）
############################
FROM eclipse-temurin:17-jdk-jammy AS jdk

############################
# base：python + jdk17 + ontop-cli
############################
# trixie 固定：QLever 二进制要求 glibc>=2.38（bookworm 只有 2.36）；
# JDK 走 Temurin COPY，不依赖发行版源（trixie 源已无 openjdk-17 也不碍事）
FROM python:3.11-slim-trixie AS base
    # 国内网络：apt 换阿里云源（deb822 格式）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources || true
    # procps 供 pkill（看守脚本杀 qlever-server）；JDK 走上面的 COPY 不走 apt
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=jdk /opt/java/openjdk /opt/jdk
ENV JAVA_HOME=/opt/jdk
ENV PATH="/opt/jdk/bin:$PATH"
WORKDIR /app
COPY ontop-cli-5.5.0 /app/ontop-cli-5.5.0
ENV ONTOP_HOME=/app/ontop-cli-5.5.0
RUN chmod +x /app/ontop-cli-5.5.0/ontop

############################
# dev：依赖内置，源码由 compose bind mount 进来
############################
FROM base AS dev
ARG PIP_INDEX_URL
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /tmp/requirements.txt
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/backend"]

############################
# playground-build：npm 构建 Playground（fork 源码，产物给 prod）
############################
FROM node:20-slim AS playground-build
ARG NPM_REGISTRY
WORKDIR /build
COPY playground/package.json playground/package-lock.json ./
RUN npm ci --registry="$NPM_REGISTRY"
COPY playground/ ./
ARG VITE_BASE_PATH=/playground/
RUN VITE_BASE_PATH="$VITE_BASE_PATH" npm run build

############################
# qlever：物化引擎二进制与运行库（固定 digest 防上游漂移）
# 只取 qlever-index/qlever-server 与其依赖库（ICU74 等 bookworm 源里没有），
# 不引入官方镜像的 entrypoint 与其他内容
############################
FROM adfreiburg/qlever@sha256:119355a81be22d3fb5d61d2236986bd2c6351c5c3fc82c0b2b293a7e08c83724 AS qlever

############################
# prod：全量打进镜像（内置 QLever，单容器同时提供虚拟+物化路线）
############################
FROM base AS prod
ARG PIP_INDEX_URL
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /tmp/requirements.txt
COPY backend /app/backend
COPY tools /app/tools
# 默认配置打进镜像：docker run 单容器即可跑；compose 挂载同名文件可覆盖
COPY config.yaml /app/config.yaml
COPY --from=playground-build /build/build /app/backend/static/playground
# QLever：二进制 + DT_NEEDED 精确清单（boost1.83/ICU74 等 bookworm 没有或版本更旧的；
# glibc/libm/libgcc/openssl 用基底自带的，绝不覆盖——拷 Ubuntu 的 glibc 会符号崩溃）
COPY --from=qlever /qlever/qlever-index /qlever/qlever-server /qlever-bin/
COPY --from=qlever /usr/lib/x86_64-linux-gnu/libboost_iostreams.so.1.83* \
                   /usr/lib/x86_64-linux-gnu/libboost_program_options.so.1.83* \
                   /usr/lib/x86_64-linux-gnu/libboost_url.so.1.83* \
                   /usr/lib/x86_64-linux-gnu/libgomp.so.1* \
                   /usr/lib/x86_64-linux-gnu/libicudata.so.74* \
                   /usr/lib/x86_64-linux-gnu/libicui18n.so.74* \
                   /usr/lib/x86_64-linux-gnu/libicuuc.so.74* \
                   /usr/lib/x86_64-linux-gnu/libjemalloc.so.2* \
                   /usr/lib/x86_64-linux-gnu/libstdc++.so.6* \
                   /usr/lib/x86_64-linux-gnu/liburing.so.2* \
                   /usr/lib/x86_64-linux-gnu/libzstd.so.1* \
                   /qlever-libs/
COPY deploy/qlever/watch-builtin.sh deploy/qlever/entrypoint.sh /app/deploy/qlever/
# 物化路线指向容器内内置 QLever（源码里的默认值给开发期外置容器用）
RUN chmod +x /app/deploy/qlever/*.sh /qlever-bin/* \
    && sed -i 's|host.docker.internal:7001|127.0.0.1:7001|' /app/config.yaml
EXPOSE 8000
CMD ["/app/deploy/qlever/entrypoint.sh"]
