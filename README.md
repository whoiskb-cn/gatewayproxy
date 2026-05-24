# GatewayProxy

一个面向自建媒体服务的反向代理与 302 重定向网关，把 Emby / Navidrome / TingReader（有声书）/ 飞牛影视 等服务的媒体请求接管下来，配合 115 网盘等存储后端做按需直链下发与播放加速；同时集成了一个独立的 Go 音乐下载子服务以及企业微信点歌入口。

> 项目原名 Emby302 / ChillPoster，现已重命名为 **gatewayproxy**。

## 功能特性

- **多协议 302 网关**：同一进程同时挂载多个端口，分别为 Emby、Navidrome、有声书阅读器、飞牛影视提供反向代理与 302 直链改写。
- **115 网盘整合**：支持 Cookie 登录、`pickcode` 解析、秒传上传（多号池调度）、回收站自动清理、strm 解析。
- **路径映射**：把云端路径映射到媒体服务库内路径，便于强提取播放地址。
- **预加载 / 极速播放**：可选的播放预热与极速直连。
- **音乐下载子服务**：Go 编写的 `music-dl`，提供独立 Web UI（默认 `8080`），并由主服务在 `/music` 路由下统一接入。
- **企业微信点歌**：内置 `wechatpy.enterprise` 集成，把企业微信自建应用接入回调地址 `/wechat/music`，在企业微信里发送歌曲名即可触发搜索与下载。
- **Web 管理面板**：基于 FastAPI + 单页 Vue 的管理后台，统一管理上述所有配置。

## 架构概览

```
┌────────────────── Docker 容器 ─────────────────┐
│                                                │
│  Go music-dl (8080)  ◀──── /music ────┐        │
│                                       │        │
│  Python FastAPI                       │        │
│   ├─ UI       :8115  /static/index    │        │
│   └─ Gateway  :8116, 8097, 8533 ...   │        │
│           ▲                           │        │
│           │ 反向代理 / 302 改写        │        │
│           ▼                           │        │
│      Emby / Navidrome / TingReader /  │        │
│      飞牛影视 / 115 网盘                │        │
└────────────────────────────────────────────────┘
```

- `8115`：Web 管理后台
- `8116`：默认 Emby 网关端口（可在配置中按需追加多个）
- 其余端口由 `config/config_302.json` 中各服务的 `proxy_port` 字段决定
- `8080`：内置 Go 音乐下载服务

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
docker compose up -d --build
```

启动后访问 <http://localhost:8115/static/index.html>，使用默认账号 `admin / admin123` 登录后立即修改密码。

### 方式二：本地运行

需求：Python 3.12+、Go 1.25+、ffmpeg。

```bash
# 1. 编译 music-dl
cd music
go build -o music-dl ./cmd/music-dl
./music-dl web --port 8080 --no-browser &

# 2. 启动主服务
cd ..
pip install -r requirements.txt
python main.py
```

## 配置说明

所有可视化配置都保存在 `config/config_302.json`，由 Web 面板管理；首次启动会自动生成示例文件。关键字段：

| 字段 | 说明 |
| --- | --- |
| `drives[]` | 115 网盘账号池（主号 + 秒传小号） |
| `embys[]` | Emby 站点列表，每条对应一个独立网关端口 |
| `navidromes[]` / `tingreaders[]` / `feinius[]` | 同上，分别对应 Navidrome / 有声书阅读器 / 飞牛影视 |
| `admin_username` / `admin_password` | 管理后台登录账号 |
| `wecom_music` | 企业微信自建应用点歌相关凭据（corp_id / agent_id / secret / token / encoding_aes_key） |

> **请务必在公网部署前修改默认密码**。所有敏感字段（Cookie、token、secret 等）都通过 Web 面板填写，不会随仓库一起分发。

## 目录结构

```
gatewayproxy/
├── main.py                # FastAPI 双端口入口（UI + 网关）
├── app/
│   ├── routers/           # config_302 / gateway / 115 / music / *_helper
│   ├── services/          # drive115_service / task_service
│   └── schemas.py
├── core/                  # configs / logger / wechat_music
├── static/                # Web 管理面板（Vue 单页）
├── music/                 # Go 音乐下载子服务
├── config/                # 运行时配置（容器内挂载）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── start.sh
```

## 安全提示

- 仓库中不包含任何真实 Cookie、token、密钥或个人配置，所有配置文件均为占位示例。
- 部署后请第一时间：
  1. 修改 `admin_password`
  2. 用反向代理（Nginx / Caddy）加 HTTPS + IP 白名单
  3. 不要把 `8115` 直接暴露到公网

## 许可证

MIT
