# syntax=docker/dockerfile:1
# easy_ontology 多 target 构建：
#   base  = python3.11 + jdk17 + ontop-cli（dev/prod 共用基底）
#   dev   = 依赖内置、代码 bind mount、uvicorn 热重载（日常开发）
#   playground-build = node20 构建 Playground 静态产物（仅 prod 用）
#   prod  = 源码+Playground 产物打进镜像（正式交付）

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG NPM_REGISTRY=https://registry.npmmirror.com

############################
# base：python + jdk17 + ontop-cli
############################
FROM python:3.11-slim-bookworm AS base
    # bookworm 固定：trixie 仓库已下架 openjdk-17
    # 国内网络：apt 换阿里云源（bookworm 的 deb822 格式）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources || true
    # jdk-headless 带 javac：DbPing 需要容器内编译；procps 供 psutil/进程树杀
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jdk-headless procps curl \
    && rm -rf /var/lib/apt/lists/*
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
# prod：全量打进镜像
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
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
