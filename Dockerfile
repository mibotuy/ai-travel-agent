# AI 智能出行助手 · 容器化部署
FROM python:3.11-slim

WORKDIR /app

# 先装依赖（利用层缓存）
# 默认走官方 PyPI；受限网络可传入镜像源，例如：
#   --build-arg PIP_INDEX_URL=https://mirrors.tencent.com/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple/
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    --index-url "${PIP_INDEX_URL}"

# 再拷贝源码
COPY . .

# 通过环境变量注入密钥，不把 .env 打进镜像
# 运行：docker run -p 8000:8000 --env-file .env travel-agent
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "python api_server.py"]
