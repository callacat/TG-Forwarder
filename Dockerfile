# 使用更稳定的 Python 3.13 Slim (Bookworm)
FROM python:3.13-slim-bookworm

# 设置环境变量 (优化 Python 运行)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 安装系统基础依赖 (curl 用于健康检查或下载工具)
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# 设置时区
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 现代化：使用 'uv' 替代 pip (速度快 10-100 倍)
# 这一步会下载 uv 二进制文件
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# 复制依赖并安装
COPY requirements.txt .
# 使用 uv pip install 安装依赖到系统环境
RUN uv pip install --system --no-cache -r requirements.txt

# 复制项目文件
COPY . .

# 创建数据目录并赋权
RUN mkdir -p /app/data && chmod -R 755 /app/data

VOLUME /app/data

# 启动命令
CMD ["python", "ultimate_forwarder.py", "run", "-c", "/app/config.yaml"]