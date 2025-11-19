# 阶段 1: 构建环境 (Builder)
FROM python:3.13-slim-bookworm AS builder

# 设置环境变量，禁用不必要的输出
ENV PYTHONUNBUFFERED=1

# 复制 uv 二进制文件
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# 复制依赖定义文件
COPY requirements.txt .

# --- 修复关键点 ---
# 在 Builder 阶段显式设置 VIRTUAL_ENV 和 PATH
# 这样 uv pip install 会自动检测并安装到该虚拟环境中，无需 --python 参数
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 创建虚拟环境并安装依赖
# 1. uv venv 创建环境
# 2. uv pip install 自动安装到上述 ENV 指定的环境中
RUN uv venv /opt/venv && \
    uv pip install --no-cache -r requirements.txt

# 阶段 2: 运行环境 (Runtime)
FROM python:3.13-slim-bookworm

# 设置环境变量
# VIRTUAL_ENV: 显式声明虚拟环境路径
# PATH: 优先使用虚拟环境中的二进制文件
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# 安装运行时系统工具
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# 设置时区
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 从构建阶段复制填充好的虚拟环境
COPY --from=builder /opt/venv /opt/venv

# 复制项目源代码
COPY . .

# 数据目录权限
RUN mkdir -p /app/data && chmod -R 755 /app/data

VOLUME /app/data

# 启动命令
CMD ["python", "ultimate_forwarder.py", "run", "-c", "/app/config.yaml"]