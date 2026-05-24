import httpx
import os
import json
import time
from fastapi import Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from app.routers.config_302 import get_config_302
from app.services.drive115_service import drive115_service
from core.logger import logger
from urllib.parse import urlencode, parse_qsl

# 端口映射
PORT_TO_NAVIDROME_INDEX = {}

def register_navidrome_port(port: int, idx: int):
    PORT_TO_NAVIDROME_INDEX[port] = idx

# 代理 client
proxy_client = httpx.AsyncClient(
    timeout=httpx.Timeout(30.0, connect=5.0, read=120.0),
    follow_redirects=True,
    verify=False,
    limits=httpx.Limits(max_keepalive_connections=200, max_connections=400)
)

# 缓存 Navidrome Token: {cache_key: (token, expiry)}
NAV_TOKEN_CACHE = {}

async def _get_nav_token(base_url, username, password):
    """使用 /auth/login 获取 Navidrome 原生 Bearer Token"""
    cache_key = f"{base_url}_{username}"
    now = time.time()

    if cache_key in NAV_TOKEN_CACHE:
        token, expiry = NAV_TOKEN_CACHE[cache_key]
        if now < expiry:
            logger.debug(f"使用缓存的 Navidrome token: {token}")
            return token

    try:
        # Navidrome 正确的登录端点是 /auth/login（不是 /api/login）
        login_url = f"{base_url}/auth/login"
        resp = await proxy_client.post(login_url, json={"username": username, "password": password}, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("token")
            if token:
                # 缓存 12 小时
                NAV_TOKEN_CACHE[cache_key] = (token, now + 43200)
                logger.info(f"成功登录 Navidrome，获取 token: {token}")
                return token
        logger.error(f"Navidrome 登录失败 ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Navidrome 登录异常: {e}")
    return None

async def handle_navidrome_request(request: Request, path: str, request_port: int, background_tasks):
    cfg = await get_config_302()
    navs = cfg.get("navidromes", [])
    nav_index = PORT_TO_NAVIDROME_INDEX.get(request_port, 0)
    if nav_index >= len(navs): nav_index = 0
    nav_cfg = navs[nav_index] if navs else {}

    base_url = nav_cfg.get("url", "").rstrip("/")
    enabled = nav_cfg.get("enabled", False)

    if not base_url:
        return Response("Navidrome URL not configured.", status_code=502)

    lower_path = path.lower()
    is_stream = "stream" in lower_path or "download" in lower_path

    if is_stream and request.method == "GET" and enabled:
        try:
            item_id = request.query_params.get("id")
            if item_id:
                cfg_user = nav_cfg.get("username")
                cfg_pass = nav_cfg.get("password")

                # === 使用原生 API 获取歌曲真实路径 ===
                song = {}
                if cfg_user and cfg_pass:
                    token = await _get_nav_token(base_url, cfg_user, cfg_pass)
                    if token:
                        try:
                            native_url = f"{base_url}/api/song/{item_id}"
                            headers = {"x-nd-authorization": f"Bearer {token}"}
                            logger.info(f"转发请求到Nav服务器以获取路径信息")
                            resp = await proxy_client.get(native_url, headers=headers, timeout=5.0)
                            if resp.status_code == 200:
                                song = resp.json()
                            else:
                                logger.warning(f"原生 API 获取失败 ({resp.status_code})，尝试 Subsonic 兜底")
                        except Exception as e:
                            logger.warning(f"原生 API 异常: {e}")

                # === Subsonic 兜底 ===
                if not song:
                    try:
                        query_params = dict(parse_qsl(request.scope.get("query_string", b"").decode()))
                        query_params["f"] = "json"
                        rest_prefix = "rest"
                        if "api/v1/reverse/nav" in path: rest_prefix = "api/v1/reverse/nav/rest"
                        sub_url = f"{base_url}/{rest_prefix}/getSong.view?{urlencode(query_params)}"
                        resp = await proxy_client.get(sub_url, timeout=5.0)
                        if resp.status_code == 200:
                            song = resp.json().get("subsonic-response", {}).get("song", {})
                    except: pass

                if song:
                    # 原生 API 的 path 字段：对 strm 文件来说，Navidrome 会读取 strm 内容返回其中的真实路径
                    raw_file_path = song.get("path") or song.get("absolutePath")
                    logger.info(f"Nav源文件: {raw_file_path}")

                    # 地毯式扫描：搜索所有字段中包含 /CloudNAS 的值（兼容旧方案）
                    found_cloud_path = None
                    for key, value in song.items():
                        if isinstance(value, str) and "/CloudNAS" in value:
                            found_cloud_path = value
                            logger.info(f"🎯 发现注入字段 [{key}]: {value}")
                            break

                    if found_cloud_path:
                        logger.info(f"当前文件为strm文件,使用源文件路径: {found_cloud_path}")
                        raw_file_path = found_cloud_path

                    # 提交 115 解析直链
                    user_agent = request.headers.get("user-agent", "")
                    direct_url = await drive115_service.get_navidrome_direct_url(
                        item_id,
                        raw_file_path,
                        user_agent,
                        song.get("title"),
                        nav_index,
                        artist=song.get("artist"),
                        album=song.get("album")
                    )

                    if direct_url:
                        logger.info(f"#Redirect# 🚀 302 劫持重定向成功")
                        return RedirectResponse(url=direct_url, status_code=302)

        except Exception as e:
            logger.error(f"❌ Navidrome 302 处理异常: {e}")

    # 默认透明代理
    return await _proxy_request(request, base_url, path)


async def _proxy_request(request: Request, base_url: str, path: str):
    clean_path = path.lstrip("/")

    if not clean_path:
        redirect_target = "/app/"
        qs = request.scope.get("query_string", b"").decode()
        if qs: redirect_target += f"?{qs}"
        return RedirectResponse(url=redirect_target, status_code=302)

    target_url = f"{base_url}/{clean_path}"
    raw_query = request.scope.get("query_string", b"")
    if raw_query:
        separator = "&" if "?" in target_url else "?"
        target_url += f"{separator}{raw_query.decode('utf-8')}"

    exclude_headers = {"host", "connection", "keep-alive", "transfer-encoding", "upgrade", "content-length"}
    client_headers = {k: v for k, v in request.headers.items() if k.lower() not in exclude_headers}

    try:
        method = request.method
        body = await request.body() if method not in ["GET", "HEAD"] else None
        req = proxy_client.build_request(method, target_url, headers=client_headers, content=body)
        r = await proxy_client.send(req, stream=True)

        resp_headers = dict(r.headers)
        for h in ["content-length", "transfer-encoding", "connection", "server", "date"]:
            resp_headers.pop(h, None)

        return StreamingResponse(
            r.aiter_raw(),
            status_code=r.status_code,
            headers=resp_headers,
            background=BackgroundTask(r.aclose)
        )
    except Exception as e:
        logger.error(f"[NavProxy] 代理异常: {target_url} | {e}")
        return Response(content="Proxy Error", status_code=502)
