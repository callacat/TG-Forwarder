# TG-Forwarder v3 入口镜像（R7/R1：HEALTHCHECK 检测账号健康）
# 与 v2 同基座 python:3.13-slim-bookworm，保持多架构 amd64+arm64
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# curl 保留给 HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/* && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 依赖全 pin（R7）；镜像内构建一次成型，禁本地构建（D7）
COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt

# v3 包 + 入口脚本（v2 单文件已退役，不再 COPY 全量）
COPY tg_forwarder/ ./tg_forwarder/
COPY main.py ./
COPY config_template.yaml ./

RUN mkdir -p /app/data && chmod 755 /app/data

VOLUME /app/data

# HEALTHCHECK：/health 由 v3 Web 服务暴露，账号全灭时返回 unhealthy
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD curl -sf http://127.0.0.1:8080/health || exit 1

CMD ["python", "main.py", "run", "-c", "/app/config.yaml"]
