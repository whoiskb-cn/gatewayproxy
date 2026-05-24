# --- 第一阶段：编译 Go 程序 ---
FROM golang:1.25 AS go-builder
WORKDIR /build
# 路径已改为 ./music
COPY music/go.mod music/go.sum ./
RUN go mod download
COPY music/ .
RUN CGO_ENABLED=0 GOOS=linux go build -o music-dl ./cmd/music-dl

# --- 第二阶段：运行环境 ---
FROM python:3.12-slim

# 使用阿里云镜像源加速
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources

# 安装系统依赖 (ffmpeg 和 tzdata)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai
WORKDIR /app

# 复制并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 复制 Go 编译后的程序 (放在最后，防止被 COPY . . 覆盖)
COPY --from=go-builder /build/music-dl ./music/music-dl

# 赋予执行权限
RUN chmod +x start.sh ./music/music-dl

# 设置环境变量，指向容器内部的 Go 服务
ENV MUSIC_DL_URL=http://127.0.0.1:8090/music

# 暴露端口
EXPOSE 8115 8116 8533 9527 8090

CMD ["./start.sh"]
