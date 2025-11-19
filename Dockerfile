# 使用官方 Python 3.13 轻量级镜像
FROM python:3.13-slim-bookworm

# 设置工作目录
WORKDIR /app

# 设置环境变量
# PYTHONDONTWRITEBYTECODE: 防止生成 .pyc 文件
# PYTHONUNBUFFERED: 确保日志实时输出，不被缓冲
# TZ: 设置时区
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 安装系统基础工具 (curl 用于调试网络)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# 配置系统时区
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 复制项目所有文件到镜像中
COPY . .

# --- 关键修改 ---
# 直接使用 pip 安装依赖，不使用虚拟环境，不使用多阶段构建
# 这样能确保所有包直接安装在系统路径下，Python 一定能找到
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 创建数据目录并设置权限
RUN mkdir -p /app/data && chmod -R 755 /app/data

# 声明挂载点
VOLUME /app/data

# 启动命令
CMD ["python", "ultimate_forwarder.py", "run", "-c", "/app/config.yaml"]