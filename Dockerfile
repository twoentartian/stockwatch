# StockWatch —— 美股监控 + 持仓跟踪
#
# 构建：  docker build -t stockwatch .
# 运行：  docker run -d --name stockwatch -p 8765:8765 \
#             -v ./data:/data -e PUID=1000 -e PGID=1000 -e TZ=Europe/Berlin stockwatch
#
# **所有需要持久化的东西都在 /data**：stockwatch.db（全部数据）和
# config.json（全部设置，含 SMTP 密码和两个 API Key）。不挂卷的话容器一删数据就没了。

FROM python:3.12-slim

# tzdata：判断美股交易时段要用真实时区，基础镜像里没有
# curl：给 HEALTHCHECK 用
# gosu：入口脚本以 root 修好 /data 的属主后，靠它降权运行
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl gosu \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STOCKWATCH_HOST=0.0.0.0 \
    STOCKWATCH_PORT=8765 \
    STOCKWATCH_CONFIG=/data/config.json \
    STOCKWATCH_DB=/data/stockwatch.db \
    PUID=10001 \
    PGID=10001 \
    TZ=Europe/Berlin

WORKDIR /app

# 依赖单独一层：只要 requirements.txt 没动，改代码重建就不用再装一遍
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py entrypoint.sh ./
COPY templates/ ./templates/
COPY static/ ./static/

# 入口脚本在 Windows 上编辑过的话可能带 CRLF，sh 会报一堆 "\r: not found"；
# 从 zip 解压到群晖时可执行位也可能丢。两件事都在这里兜住。
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod 755 /app/entrypoint.sh \
 && mkdir -p /data

# 注意这里**没有** USER 指令：容器以 root 启动，由 entrypoint.sh 改好 /data 的属主后
# 用 gosu 降到 PUID/PGID。进程本身不以 root 跑。
# 不要在 compose 里写 user:——那样入口脚本没权限改属主，挂载目录不对就起不来。
VOLUME ["/data"]
EXPOSE 8765

# 探活打的是真接口而不是首页：首页是静态模板，后台调度器挂了它照样返回 200
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/api/state > /dev/null || exit 1

ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["python", "app.py"]
