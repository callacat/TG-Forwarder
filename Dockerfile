# 阶段 1: 构建环境 (Builder)
# 使用多阶段构建减小最终镜像体积，分离构建工具和运行时环境
FROM python:3.13-slim-bookworm AS builder

# 设置环境变量，禁用不必要的输出
ENV PYTHONUNBUFFERED=1

# 复制 uv 二进制文件 (速度极快的 Python 包管理器)
# uv 不需要 Python 环境即可运行，非常适合构建阶段
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# 复制依赖定义文件
COPY requirements.txt .

# 创建虚拟环境并安装依赖
# 1. 创建虚拟环境到 /opt/venv
# 2. 激活环境并安装依赖
# 3. --no-cache: 避免缓存文件占用空间 (虽然多阶段构建会丢弃这层，但在构建过程中节省空间)
RUN uv venv /opt/venv && \
    . /opt/venv/bin/activate && \
    uv pip install --no-cache -r requirements.txt

# 阶段 2: 运行环境 (Runtime)
# 最终镜像，只包含运行时必需的文件
FROM python:3.13-slim-bookworm

# 设置环境变量
# PATH: 将虚拟环境的 bin 目录加入 PATH，确保直接使用 venv 中的 python 和库
# PYTHONDONTWRITEBYTECODE: 防止生成 .pyc 文件
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# 安装运行时必需的系统工具
# curl: 用于可能的健康检查或网络测试
# rm -rf: 清理 apt 缓存，减小体积
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# 设置时区
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 从构建阶段 (builder) 复制整个虚拟环境
# 这一步将所有 Python 依赖 (telethon, pydantic, fastapi 等) 带入最终镜像
COPY --from=builder /opt/venv /opt/venv

# 复制项目源代码
COPY . .

# 创建数据目录并赋权
RUN mkdir -p /app/data && chmod -R 755 /app/data

VOLUME /app/data

# 启动命令
# 由于 PATH 已设置，这里的 python 自动指向 /opt/venv/bin/python
CMD ["python", "ultimate_forwarder.py", "run", "-c", "/app/config.yaml"]