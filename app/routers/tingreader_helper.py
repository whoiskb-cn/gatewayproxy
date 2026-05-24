"""
TingReader 反向代理助手

核心功能：
1. 透明反代 TingReader 的所有 HTTP / WebSocket 请求
2. 拦截 /api/stream/:chapterId 播放请求
3. 对 .strm 路径模式文件，自动获取 115 直链并 302 重定向
4. 拦截 chapters API 缓存章节 ID -> 文件路径映射
"""

import asyncio
import json
import os
import re
import httpx
import websockets
from fastapi import Request, Response, WebSocket
from fastapi.responses import StreamingResponse, RedirectResponse
from starlette.background import BackgroundTask
from app.routers.config_302 import get_config_302
from app.services.drive115_service import drive115_service
from core.logger import logger


# ========== 流式代理响应 ==========
class ProxyStreamingResponse(StreamingResponse):
    """封装 httpx 流式响应，确保连接正确关闭"""
    def __init__(self, httpx_response: httpx.Response, *args, **kwargs):
        super().__init__(content=httpx_response.aiter_raw(), *args, **kwargs)
        self.httpx_response = httpx_response

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.httpx_response.aclose()


# ========== 端口映射 ==========
PORT_TO_TINGREADER_INDEX = {}

def register_tingreader_port(port: int, idx: int):
    """注册端口与 TingReader 配置索引的映射"""
    PORT_TO_TINGREADER_INDEX[port] = idx


# ========== 章节路径缓存（持久化） ==========
CACHE_FILE = "config/tingreader_chapter_cache.json"

def _load_cache():
    """从磁盘加载章节缓存"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_cache():
    """将章节缓存持久化到磁盘"""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(CHAPTER_CACHE, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"[TingReader] 保存章节缓存失败: {e}")

# 章节 ID -> 文件路径 的全局缓存
CHAPTER_CACHE = _load_cache()


# ========== HTTP 代理客户端 ==========
proxy_client = httpx.AsyncClient(
    timeout=httpx.Timeout(60.0, connect=5.0, read=120.0),
    follow_redirects=False,
    verify=False,
    limits=httpx.Limits(max_keepalive_connections=200, max_connections=500)
)


# ========== 辅助函数 ==========
def _is_cloud_audio_path(path: str) -> bool:
    """判断路径是否为云盘音频路径（非 strm）"""
    if not path:
        return False
    lower = path.lower()
    has_cloud = "/cloudnas/" in lower or "/clouddrive/" in lower
    is_audio = lower.endswith((".flac", ".mp3", ".wav", ".m4a", ".ogg", ".ape", ".aac", ".wma", ".alac", ".opus"))
    return has_cloud and is_audio


def _extract_strm_path(error_message: str):
    """
    从 TingReader 400 错误消息中提取 strm 文件的实际内容（云盘路径）

    TingReader 源码 (media.rs L423-424):
      if url.is_empty() || !url.starts_with("http") {
          return Err(TingError::InvalidRequest(
              format!("Invalid strm file content: '{}'", url)
          ));
      }

    错误消息格式: "Invalid request: Invalid strm file content: '/CloudNAS/...flac'"
    """
    match = re.search(r"strm file content:\s*'([^']+)'", error_message)
    if match:
        return match.group(1).strip()
    return None


def _get_tingreader_config(cfg: dict, request_port: int):
    """根据请求端口获取对应的 TingReader 配置"""
    apps = cfg.get("tingreaders", [])
    app_index = PORT_TO_TINGREADER_INDEX.get(request_port, 0)
    if app_index >= len(apps):
        app_index = 0
    app_cfg = apps[app_index] if apps else {}
    return app_cfg, app_index


def _build_proxy_headers(request: Request):
    """构建转发请求头（移除 hop-by-hop 头部）"""
    exclude = {"host", "content-length", "connection", "transfer-encoding", "upgrade"}
    return {k: v for k, v in request.headers.items() if k.lower() not in exclude}


# ========== 核心：HTTP 请求处理 ==========
async def handle_tingreader_request(request: Request, path: str, request_port: int, background_tasks):
    cfg = await get_config_302()
    app_cfg, app_index = _get_tingreader_config(cfg, request_port)

    base_url = app_cfg.get("url", "").rstrip("/")
    enabled = app_cfg.get("enabled", False)
    app_name = app_cfg.get("name", f"TingReader[{app_index}]")

    if not base_url:
        return Response(f"{app_name} URL not configured.", status_code=502)
    if not enabled:
        return Response(f"{app_name} is disabled.", status_code=503)

    # 补前导斜杠，确保路径匹配一致
    lower_path = ("/" + path).lower()
    headers = _build_proxy_headers(request)

    # --------------------------------------------------
    # 1. 拦截 /api/stream/:chapterId 播放请求
    # --------------------------------------------------
    is_stream = "/api/stream/" in lower_path
    if is_stream and request.method in ("GET", "HEAD"):
        # 提取 chapterId (路由格式: api/stream/:chapterId)
        # path 可能是 "api/stream/xxx" 或 "/api/stream/xxx"
        chapter_id = None
        for sep in ("api/stream/", "/api/stream/"):
            if sep in path:
                chapter_id = path.split(sep)[-1].split("?")[0].strip("/")
                break

        if chapter_id:
            result = await _handle_stream_request(
                request, chapter_id, base_url, headers,
                app_name, app_index
            )
            if result:
                return result

    # --------------------------------------------------
    # 2. 拦截 chapters 列表 API，缓存章节路径
    #    路由: /api/books/:id/chapters 或 /api/v1/books/:id/chapters
    # --------------------------------------------------
    is_chapters_api = "/chapters" in lower_path and "/books/" in lower_path and request.method == "GET"
    if is_chapters_api:
        return await _intercept_chapters_api(
            request, base_url, path, headers, background_tasks
        )

    # --------------------------------------------------
    # 3. 默认：透明反向代理
    # --------------------------------------------------
    return await _proxy_request(request, base_url, path, headers, app_name)


async def _handle_stream_request(
    request: Request, chapter_id: str, base_url: str,
    headers: dict, app_name: str, app_index: int
):
    """
    处理 /api/stream/:chapterId 播放请求

    策略：
    1. 查缓存 -> 如果路径是直接的云盘音频文件 -> 直接获取 115 直链
    2. 查缓存 -> 如果路径是 .strm 文件 -> 转发给 TingReader 解析
    3. 无缓存 -> 也转发给 TingReader，看是否触发 strm 400 错误
    """
    cached_path = CHAPTER_CACHE.get(chapter_id)
    user_agent = request.headers.get("user-agent", "")

    # ---- 情况 A: 缓存命中且为云盘音频文件（非 strm），直接获取直链 ----
    if cached_path and _is_cloud_audio_path(cached_path) and not cached_path.lower().endswith(".strm"):
        filename = os.path.basename(cached_path)
        direct_url = await drive115_service.get_navidrome_direct_url(
            chapter_id, cached_path, user_agent, filename,
            app_index, drive_type="tingreader"
        )
        if direct_url:
            logger.info(f"[{app_name}] ✅ 缓存命中，代理 115 流")
            return await _proxy_115_stream(request, direct_url)

    # ---- 情况 B & C: 可能是 strm 文件或无缓存，转发给 TingReader ----
    clean_path = f"api/stream/{chapter_id}"
    target_url = f"{base_url}/{clean_path}"
    query_string = request.scope.get("query_string", b"")
    if query_string:
        target_url += f"?{query_string.decode('utf-8')}"

    try:
        req = proxy_client.build_request("GET", target_url, headers=headers)
        r = await proxy_client.send(req, stream=True)

        # 如果 TingReader 返回 400，可能是 strm 路径模式导致的
        if r.status_code == 400:
            await r.aread()
            await r.aclose()

            cloud_path = None
            try:
                error_data = r.json()
                message = error_data.get("message", "")
                cloud_path = _extract_strm_path(message)
                if cloud_path:
                    logger.info(f"[{app_name}] 📂 从 strm 错误中提取到路径: {cloud_path}")
            except Exception:
                pass

            # 成功提取到云盘路径，获取 115 直链
            if cloud_path:
                filename = os.path.basename(cloud_path)
                direct_url = await drive115_service.get_navidrome_direct_url(
                    chapter_id, cloud_path, user_agent, filename,
                    app_index, drive_type="tingreader"
                )
                if direct_url:
                    logger.info(f"[{app_name}] ✅ strm 解析成功，代理 115 流")
                    return await _proxy_115_stream(request, direct_url)

            # 如果提取失败或获取直链失败，返回原始 400 错误
            resp_headers = {
                k: v for k, v in r.headers.items()
                if k.lower() not in {"transfer-encoding", "connection", "content-encoding"}
            }
            return Response(content=r.content, status_code=400, headers=resp_headers)

        # TingReader 正常响应（200/206 等），流式转发
        resp_headers = {
            k: v for k, v in r.headers.items()
            if k.lower() not in {"transfer-encoding", "connection"}
        }
        # 添加跨域支持和头部暴露
        resp_headers["Access-Control-Allow-Origin"] = "*"
        resp_headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges"
        return ProxyStreamingResponse(r, status_code=r.status_code, headers=resp_headers)

    except Exception as e:
        logger.error(f"[{app_name}] 播放请求转发失败: {e}")
        return None  # 回退到默认代理


async def _proxy_115_stream(request: Request, url: str):
    """
    代理 115 直链流，解决 302 跨域、IP 绑定、UA 和 Content-Disposition 问题
    """
    headers = {
        "User-Agent": request.headers.get("user-agent", "Mozilla/5.0"),
        "Range": request.headers.get("range", ""),
    }
    if not headers["Range"]:
        headers.pop("Range")

    try:
        # 115 直链可能对 Referer 有校验，通常设置为 115.com 较安全
        # headers["Referer"] = "https://115.com/"

        # [修复] 必须使用原始请求的方法（如 HEAD），否则获取不到 Content-Length/Range 头部
        req = proxy_client.build_request(request.method, url, headers=headers)
        r = await proxy_client.send(req, stream=(request.method != "HEAD"))

        # 构造响应头
        resp_headers = {}
        # 允许转发的媒体相关头部
        allow_headers = {
            "content-type", "content-length", "content-range",
            "accept-ranges", "last-modified", "etag", "cache-control"
        }

        for k, v in r.headers.items():
            kl = k.lower()
            if kl in allow_headers:
                resp_headers[k] = v

        # 修正 Content-Type：115 经常对 .m4a 返回 audio/mpeg，导致部分浏览器播放失败
        if ".m4a" in url.lower().split('?')[0] and resp_headers.get("Content-Type") == "audio/mpeg":
            resp_headers["Content-Type"] = "audio/mp4"

        # 强制设置为 inline，解决 115 默认 attachment 导致浏览器触发下载而非播放的问题
        resp_headers["Content-Disposition"] = "inline"

        # 跨域支持和头部暴露
        resp_headers["Access-Control-Allow-Origin"] = "*"
        resp_headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        resp_headers["Access-Control-Allow-Headers"] = "*"
        # [核心] 必须暴露这些头部，前端播放器才能读取到总时长
        resp_headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges"

        if request.method == "HEAD":
            return Response(status_code=r.status_code, headers=resp_headers)

        return ProxyStreamingResponse(r, status_code=r.status_code, headers=resp_headers)
    except Exception as e:
        logger.error(f"[TingReader] 代理 115 流失败: {e}")
        return Response("Proxy 115 Error", status_code=502)


async def _intercept_chapters_api(
    request: Request, base_url: str, path: str,
    headers: dict, background_tasks
):
    """
    拦截 /api/books/:id/chapters 响应，缓存章节 ID -> 路径映射

    TingReader 返回的章节格式 (ChapterResponse):
    {
        "id": "6df06407-b4b2-...",
        "bookId": "...",
        "path": "/data/audiobooks/.../01.strm",
        ...
    }
    """
    clean_path = path.lstrip("/")
    target_url = f"{base_url}/{clean_path}"
    query_string = request.scope.get("query_string", b"")
    if query_string:
        target_url += f"?{query_string.decode('utf-8')}"

    try:
        req = proxy_client.build_request("GET", target_url, headers=headers)
        r = await proxy_client.send(req, stream=False)

        # 尝试解析并缓存章节路径
        if r.status_code == 200:
            try:
                data = r.json()
                chapters = data if isinstance(data, list) else data.get("chapters", [])
                updated = False
                for ch in chapters:
                    if not isinstance(ch, dict):
                        continue
                    c_id = ch.get("id")
                    c_path = ch.get("path")
                    if c_id and c_path and CHAPTER_CACHE.get(str(c_id)) != c_path:
                        CHAPTER_CACHE[str(c_id)] = c_path
                        updated = True
                if updated:
                    count = len(CHAPTER_CACHE)
                    logger.info(f"[TingReader] 📦 章节缓存已更新，当前共 {count} 条记录")
                    background_tasks.add_task(_save_cache)
            except Exception as e:
                logger.warning(f"[TingReader] 解析章节响应失败: {e}")

        # 原样返回响应给客户端
        resp_headers = {
            k: v for k, v in r.headers.items()
            if k.lower() not in {"transfer-encoding", "content-length", "connection", "content-encoding"}
        }
        return Response(content=r.content, status_code=r.status_code, headers=resp_headers)
    except Exception as e:
        logger.error(f"[TingReader] chapters API 转发失败: {e}")
        return Response("Proxy Error", status_code=502)


async def _proxy_request(request: Request, base_url: str, path: str, headers: dict, app_name: str):
    """通用透明反向代理"""
    clean_path = path.lstrip("/")
    target_url = f"{base_url}/{clean_path}" if clean_path else base_url
    query_string = request.scope.get("query_string", b"")
    if query_string:
        target_url += f"?{query_string.decode('utf-8')}"

    try:
        if request.method in ("GET", "HEAD", "OPTIONS"):
            req = proxy_client.build_request(request.method, target_url, headers=headers)
        else:
            body = await request.body()
            req = proxy_client.build_request(
                request.method, target_url, headers=headers, content=body
            )

        r = await proxy_client.send(req, stream=True)
        resp_headers = {
            k: v for k, v in r.headers.items()
            if k.lower() not in {"transfer-encoding", "connection"}
        }

        # 统一添加 CORS 支持
        resp_headers["Access-Control-Allow-Origin"] = "*"

        # 修改 CSP 头，允许从任意源加载媒体（302 到 115 直链需要）
        csp_key = next((k for k in resp_headers if k.lower() == "content-security-policy"), None)
        if csp_key:
            csp = resp_headers[csp_key]
            csp = re.sub(r"media-src\s+[^;]+", "media-src *", csp)
            csp = re.sub(r"connect-src\s+[^;]+", "connect-src *", csp)
            resp_headers[csp_key] = csp

        return ProxyStreamingResponse(r, status_code=r.status_code, headers=resp_headers)
    except Exception as e:
        logger.error(f"[{app_name}] 代理请求失败 -> {target_url}: {e}")
        return Response(f"502 Bad Gateway: {e}", status_code=502)


# ========== WebSocket 代理 ==========
async def handle_tingreader_websocket(client_ws: WebSocket, path: str, request_port: int):
    cfg = await get_config_302()
    app_cfg, _ = _get_tingreader_config(cfg, request_port)
    base_url = app_cfg.get("url", "").rstrip("/")
    app_name = app_cfg.get("name", "TingReader")

    if not base_url:
        await client_ws.close()
        return

    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    clean_path = path.lstrip("/")
    upstream_url = f"{ws_url}/{clean_path}" if clean_path else ws_url
    if client_ws.query_params:
        upstream_url += f"?{client_ws.query_params}"

    try:
        async with websockets.connect(upstream_url) as server_ws:
            async def client_to_server():
                try:
                    while True:
                        msg = await client_ws.receive()
                        if "text" in msg and msg["text"] is not None:
                            await server_ws.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await server_ws.send(msg["bytes"])
                        elif msg.get("type") == "websocket.disconnect":
                            break
                except Exception:
                    pass

            async def server_to_client():
                try:
                    async for message in server_ws:
                        if isinstance(message, str):
                            await client_ws.send_text(message)
                        else:
                            await client_ws.send_bytes(message)
                except Exception:
                    pass

            await asyncio.gather(client_to_server(), server_to_client())
    except Exception as e:
        logger.error(f"[{app_name}] WebSocket 连接失败: {e}")
    finally:
        try:
            await client_ws.close()
        except Exception:
            pass
