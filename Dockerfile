# 使用官方 Python 3.10 slim 镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt ./

# 安装依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制所有项目文件到工作目录
COPY . .

# 设置时区 (可选，但建议)
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 设置数据卷 (可选，用于持久化)
VOLUME /app/data

# 默认启动命令
# 容器启动时将运行 `python ultimate_forwarder.py run -c config.yaml`
# 确保你通过 -v 将 config.yaml 和 data 目录挂载进来
CMD ["python", "ultimate_forwarder.py", "run", "-c", "config.yaml"]