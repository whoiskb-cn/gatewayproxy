import httpx
import json
import asyncio
import websockets
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse
from starlette.background import BackgroundTask
from cachetools import TTLCache
from app.routers.config_302 import get_config_302
from core.logger import logger
from app.services.drive115_service import drive115_service 

class ProxyStreamingResponse(StreamingResponse):
    def __init__(self, httpx_response: httpx.Response, *args, **kwargs):
        super().__init__(content=httpx_response.aiter_raw(), *args, **kwargs)
        self.httpx_response = httpx_response

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.httpx_response.aclose()

router = APIRouter(tags=["gateway"])

# ==========================================================
# 端口到 Emby 索引的映射 (启动时填充)
# ==========================================================
PORT_TO_EMBY_INDEX = {}

def register_gateway_port(port: int, emby_index: int):
    """注册网关端口与 Emby 索引的映射关系"""
    PORT_TO_EMBY_INDEX[port] = emby_index

async def get_emby_config_by_port(request_port: int):
    """根据请求端口获取对应的 Emby 配置"""
    cfg = await get_config_302()
    embys = cfg.get("embys", [])

    if not isinstance(embys, list) or len(embys) == 0:
        return cfg.get("emby", {}), -1

    # 根据端口找到对应的 Emby 索引
    emby_index = PORT_TO_EMBY_INDEX.get(request_port, 0)

    # 确保索引有效
    if emby_index >= len(embys):
        emby_index = 0

    target_emby = embys[emby_index]

    if not target_emby.get("enabled"):
        # 如果指定的 Emby 未启用，找第一个启用的
        for i, e in enumerate(embys):
            if e.get("enabled"):
                target_emby = e
                emby_index = i
                break

    if hasattr(target_emby, 'dict'):
        target_emby = target_emby.dict()

    return target_emby, emby_index
preload_dedupe_cache = TTLCache(maxsize=1000, ttl=10)   # 预加载去重
name_cache = TTLCache(maxsize=10000, ttl=86400)         # ID -> 片名 映射表
user_admin_cache = TTLCache(maxsize=1000, ttl=3600)     # 用户ID -> 是否管理员 映射表

# ==========================================================
# 全局 HTTP 客户端
# ==========================================================
proxy_client = httpx.AsyncClient(
    timeout=60.0, 
    follow_redirects=True, 
    verify=False,
    limits=httpx.Limits(max_keepalive_connections=200, max_connections=500)
)

async def get_emby_config():
    """获取默认的 Emby 配置（兼容旧代码）"""
    cfg = await get_config_302()
    embys = cfg.get("embys", [])
    target_emby = None

    if isinstance(embys, list) and len(embys) > 0:
        target_emby = next((e for e in embys if e.get('enabled')), None)
        if not target_emby:
            target_emby = embys[0]

    if not target_emby:
        target_emby = cfg.get("emby", {})

    if hasattr(target_emby, 'dict'):
        target_emby = target_emby.dict()

    base_url = target_emby.get("url", "").rstrip("/")
    api_key = target_emby.get("key", "")

    return base_url, api_key, target_emby

# ==================================================================
# [辅助] 检查用户是否为管理员
# ==================================================================
async def is_user_admin(user_id: str) -> bool:
    """
    检查指定用户是否为 Emby 管理员
    使用缓存避免频繁查询 Emby API
    """
    if not user_id:
        return False

    # 检查缓存
    if user_id in user_admin_cache:
        return user_admin_cache[user_id]

    base_url, api_key, _ = await get_emby_config()
    if not base_url or not api_key:
        return False

    try:
        # 查询 Emby 用户信息 API
        url = f"{base_url}/Users/{user_id}"
        headers = {"X-Emby-Token": api_key}

        resp = await proxy_client.get(url, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            user_data = resp.json()
            # Emby 用户对象中 Policy.IsAdministrator 表示是否为管理员
            is_admin = user_data.get("Policy", {}).get("IsAdministrator", False)
            user_admin_cache[user_id] = is_admin
            logger.debug(f"[Preload] 用户 {user_id} 管理员状态: {is_admin}")
            return is_admin
    except Exception as e:
        logger.warning(f"[Preload] 检查用户管理员状态失败: {e}")

    # 默认返回 False（非管理员）
    user_admin_cache[user_id] = False
    return False

# ==================================================================
# [辅助] 生成人性化的标题
# ==================================================================
def get_friendly_name(item_data: dict) -> str:
    """从 Emby 数据中提取 中文标题/剧集号"""
    try:
        name = item_data.get("Name", "未知标题")
        item_type = item_data.get("Type")
        year = item_data.get("ProductionYear", "")
        
        if item_type == "Episode":
            series_name = item_data.get("SeriesName", "")
            season_idx = item_data.get("ParentIndexNumber", "?")
            episode_idx = item_data.get("IndexNumber", "?")
            return f"📺 {series_name} S{season_idx}E{episode_idx} - {name}"
        elif item_type == "Movie":
            return f"🎬 {name} ({year})"
        else:
            return f"{name}"
    except:
        return item_data.get("Name", "Unknown")

# ==================================================================
# [辅助] 后台预加载任务
# ==================================================================
async def _preload_rapid_transfer(item_id: str, user_agent: str, item_name: str, emby_index: int = 0):
    """后台预加载任务 - 直接调用 get_direct_url 获取直链"""
    try:
        cfg = await get_config_302()
        embys = cfg.get("embys", [])
        emby_name = embys[emby_index].get("name", f"Emby[{emby_index}]") if emby_index < len(embys) else f"Emby[{emby_index}]"

        logger.info(f"[Preload-{emby_name}] 🔄 后台预加载开始: {item_name}")
        result = await drive115_service.get_direct_url(
            item_id,
            media_source_id=None,
            user_agent=user_agent,
            item_name=item_name,
            emby_index=emby_index
        )
        if result:
            logger.info(f"[Preload-{emby_name}] ✅ 后台预加载成功: {item_name}")
        else:
            logger.warning(f"[Preload-{emby_name}] ⚠️ 后台预加载失败: {item_name}")
    except Exception as e:
        logger.warning(f"[Preload] 后台预加载异常 ({item_name}): {e}")

# ==================================================================
# [辅助] 响应解析任务 (缓存名字)
# ==================================================================
async def handle_response_parsing(data: any, user_agent: str, preload_count: int = 0, item_id: str = None):
    """
    解析 Emby API 响应，缓存名字

    注意：预加载已在 PlaybackInfo 触发时直接执行，这里只处理名字缓存
    """
    try:
        if not isinstance(data, dict): return

        # === 场景 A: 列表页 (首页/媒体库) ===
        if "Items" in data and isinstance(data["Items"], list):
            for item in data["Items"]:
                item_id = item.get("Id")
                if item_id:
                    name_cache[item_id] = get_friendly_name(item)
            return

        # === 场景 B: 单个详情页 ===
        # 优先使用传入的 item_id (PlaybackInfo 响应没有 Id 字段)
        data_item_id = data.get("Id")
        final_item_id = item_id or data_item_id

        item_type = data.get("Type")

        if not final_item_id: return

        # 缓存名字
        friendly_name = name_cache.get(final_item_id)
        if not friendly_name:
            friendly_name = get_friendly_name(data) if item_type else f"ID: {final_item_id}"
            name_cache[final_item_id] = friendly_name

    except Exception as e:
        logger.debug(f"[Preload] 响应解析异常: {e}")

# ==================================================================
# WebSocket
# ==================================================================
@router.websocket("/embywebsocket")
async def websocket_endpoint(client_ws: WebSocket):
    await client_ws.accept()
    base_url, _, _ = await get_emby_config()
    
    if not base_url:
        logger.warning("[WS] 未配置 Emby URL，关闭连接")
        await client_ws.close()
        return

    ws_base_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    upstream_url = f"{ws_base_url}/embywebsocket"
    
    if client_ws.query_params:
        upstream_url += f"?{client_ws.query_params}"

    try:
        async with websockets.connect(upstream_url) as server_ws:
            async def client_to_server():
                try:
                    while True:
                        data = await client_ws.receive_text()
                        await server_ws.send(data)
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logger.warning(f"[WS] 客户端异常断开: {e}")

            async def server_to_client():
                try:
                    async for message in server_ws:
                        await client_ws.send_text(message)
                except websockets.exceptions.ConnectionClosed:
                    pass
                except Exception as e:
                    if "Unexpected ASGI message" in str(e):
                        pass 
                    else:
                        logger.warning(f"[WS] 服务端异常断开: {e}")

            await asyncio.gather(client_to_server(), server_to_client())

    except Exception as e:
        logger.error(f"[WS] 连接 Emby 失败: {upstream_url} | 原因: {e}")
    finally:
        try:
            await client_ws.close()
        except: 
            pass

@router.websocket("/{path:path}")
async def generic_websocket_endpoint(client_ws: WebSocket, path: str):
    await client_ws.accept()
    request_port = None
    if hasattr(client_ws, 'scope') and 'server' in client_ws.scope:
        request_port = client_ws.scope['server'][1]

    # ======== TingReader Websocket ========
    from app.routers.tingreader_helper import PORT_TO_TINGREADER_INDEX, handle_tingreader_websocket
    if request_port in PORT_TO_TINGREADER_INDEX:
        await handle_tingreader_websocket(client_ws, path, request_port)
        return

    # ======== Feiniu Websocket ========
    from app.routers.feiniu_helper import PORT_TO_FEINIU_INDEX, handle_feiniu_websocket
    if request_port in PORT_TO_FEINIU_INDEX:
        await handle_feiniu_websocket(client_ws, path, request_port)
        return

    # 未配置明确的 WS 转发，直接关闭
    await client_ws.close()

# ==================================================================
# HTTP 网关主逻辑
# ==================================================================
@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
async def emby_gateway(request: Request, path: str, background_tasks: BackgroundTasks):
    request_port = None
    if hasattr(request, 'scope') and 'server' in request.scope:
        request_port = request.scope['server'][1]  # (host, port)

    # ======== 115 驱动直连访问 (格式: api/DriveName/FilePath) ========
    if path.startswith("api/") and "/" in path[4:]:
        parts = path.lstrip("/").split("/")
        # 兼容性检查：确保至少有 api/DriveName/Path 三段
        if len(parts) >= 3 and parts[0] == "api":
            drive_name = parts[1]
            file_path = "/".join(parts[2:])
            
            # 基础验证：检查 drive_name 是否在配置的 drives 列表中
            cfg = await get_config_302()
            drives = cfg.get("drives", [])
            is_valid_drive = any(d.get("name") == drive_name for d in drives)
            
            if is_valid_drive:
                user_agent = request.headers.get("user-agent", "")
                logger.info(f"[Gateway-Direct] 📥 拦截到网盘直连请求: [{drive_name}] 路径: {file_path}")
                
                direct_url = await drive115_service.get_direct_url_by_path(drive_name, file_path, user_agent)
                if direct_url:
                    logger.info(f"[Gateway-Direct] 🚀 成功获取直链: {file_path}")
                    resp = RedirectResponse(url=direct_url, status_code=302)
                    resp.headers["Referrer-Policy"] = "no-referrer"
                    return resp
                else:
                    logger.warning(f"[Gateway-Direct] ❌ 在网盘 [{drive_name}] 中未找到物理路径: {file_path}")
                    # 如果未找到，我们不在这里阻断，而是返回 404 告知驱动内找不到
                    return Response(f"File not found in cloud drive [{drive_name}]: {file_path}", status_code=404)

    # ======== 新增段落：Navidrome ========
    from app.routers.navidrome_helper import PORT_TO_NAVIDROME_INDEX, handle_navidrome_request
    if request_port in PORT_TO_NAVIDROME_INDEX:
        return await handle_navidrome_request(request, path, request_port, background_tasks)
    # ==================================

    # ======== 新增段落：TingReader ========
    from app.routers.tingreader_helper import PORT_TO_TINGREADER_INDEX, handle_tingreader_request
    if request_port in PORT_TO_TINGREADER_INDEX:
        return await handle_tingreader_request(request, path, request_port, background_tasks)
    # ==================================

    # ======== 新增段落：Feiniu ========
    from app.routers.feiniu_helper import PORT_TO_FEINIU_INDEX, handle_feiniu_request
    if request_port in PORT_TO_FEINIU_INDEX:
        return await handle_feiniu_request(request, path, request_port, background_tasks)
    # ==================================

    if path.startswith("api/") or path.startswith("static/") or path.startswith("fonts/"):
        return Response(status_code=404)
    if path == "" or path == "/":
        return RedirectResponse(url="/web/index.html")

    if "api/danmu" in path:
        return Response(status_code=204)

    # 根据端口获取对应的 Emby 配置
    emby_cfg, emby_index = await get_emby_config_by_port(request_port)
    base_url = emby_cfg.get("url", "").rstrip("/")
    api_key = emby_cfg.get("key", "")
    emby_name = emby_cfg.get("name", f"Emby[{emby_index}]")

    if not base_url:
        return Response("Emby URL not configured.", status_code=502)

    # -----------------------------
    # 2. 302 播放逻辑
    # -----------------------------
    lower_path = path.lower()
    is_playback = "videos" in lower_path and ("stream" in lower_path or "original" in lower_path)
    enable_302 = emby_cfg.get("enabled", False)

    if is_playback and enable_302 and request.method == "GET":
        try:
            parts = path.split("/")
            item_id = None
            for i, part in enumerate(parts):
                if part.lower() == "videos":
                    if i + 1 < len(parts):
                        item_id = parts[i+1]
                        break

            media_source_id = request.query_params.get("mediaSourceId") or request.query_params.get("MediaSourceId")
            user_agent = request.headers.get("user-agent", "")

            if item_id:
                # 尝试从缓存获取名字
                display_name = name_cache.get(item_id, f"ID: {item_id}")

                logger.info(f"[Gateway-{emby_name}] 🎬 收到播放请求: {display_name}")

                # === [修改] 传递 emby_index，让服务使用正确的 115 账号 ===
                direct_url = await drive115_service.get_direct_url(
                    item_id,
                    media_source_id,
                    user_agent,
                    item_name=display_name,
                    emby_index=emby_index
                )

                if direct_url:
                    logger.info(f"[Gateway-{emby_name}] 🚀 302 重定向 -> 115 直链")
                    resp = RedirectResponse(url=direct_url, status_code=302)
                    resp.headers["Referrer-Policy"] = "no-referrer"
                    return resp
                else:
                    logger.info(f"[Gateway-{emby_name}] ⚠️ 302 匹配失败，降级为 Emby 中转")
        except Exception as e:
            logger.error(f"[Gateway] 302 处理异常: {e}")

    # -----------------------------
    # 3. 反向代理转发
    # -----------------------------
    clean_path = path.lstrip("/")
    target_url = f"{base_url}/{clean_path}"
    
    raw_query = request.scope.get("query_string", b"")
    if raw_query:
        target_url += f"?{raw_query.decode('utf-8')}"

    try:
        remove_headers = {"host", "content-length", "connection", "transfer-encoding", "upgrade"}
        if request.method == "GET":
            remove_headers.add("content-type")

        headers = {
            k: v for k, v in request.headers.items() 
            if k.lower() not in remove_headers
        }

        exclude_keywords = [
            "/Images", "/PlaybackInfo", "/Intros", "/ThemeMedia", 
            "/Counts", "/Sessions", "/ScheduledTasks"
        ]
        
        should_parse_response = (
            request.method == "GET" and
            ("Users" in path.split("/") or "Users/" in path) and
            ("Items" in path.split("/")) and
            not any(sub in path for sub in exclude_keywords)
        )
        
        preload_cfg = emby_cfg.get("preload", {})
        enable_preload = preload_cfg.get("enabled", False)
        preload_user = preload_cfg.get("user", "all")  # "all" 或 "admin"
        preload_count = preload_cfg.get("count", 0)  # 预缓存版本数（预留）

        # ==================================================================
        # 辅助函数：检查预加载权限
        # ==================================================================
        async def should_trigger_preload() -> bool:
            """检查当前请求是否应该触发预加载"""
            if not enable_preload:
                return False

            # 如果设置为仅管理员，检查用户是否为管理员
            if preload_user == "admin":
                # 从路径中提取用户 ID: /Users/{userId}/Items/...
                if "Users/" in path:
                    try:
                        parts = path.split("/")
                        if "Users" in parts:
                            idx = parts.index("Users")
                            if idx + 1 < len(parts):
                                user_id = parts[idx + 1]
                                is_admin = await is_user_admin(user_id)
                                if not is_admin:
                                    logger.debug(f"[Preload] 用户 {user_id} 非管理员，跳过预加载")
                                    return False
                    except Exception as e:
                        logger.warning(f"[Preload] 检查用户权限失败: {e}")
                        return False

            return True

        # ==================================================================
        # PlaybackInfo 触发逻辑 - 直接后台预加载
        # ==================================================================
        low_path_preload = path.lower()
        if enable_preload and "playbackinfo" in low_path_preload and "items" in low_path_preload:
            try:
                # 检查用户权限
                if not await should_trigger_preload():
                    pass  # 静默跳过
                else:
                    import re
                    # 兼容类似 /emby/items/xxxx/playbackinfo、Items/xxxx/playbackinfo 等结构
                    match = re.search(r'items/([\w\-]+)/playbackinfo', low_path_preload)
                    if match:
                        item_id = match.group(1)
                        if item_id and item_id not in preload_dedupe_cache:
                            preload_dedupe_cache[item_id] = True
                            ua = request.headers.get("user-agent", "")
                            p_name = name_cache.get(item_id, f"ID: {item_id}")

                            logger.info(f"[Preload-{emby_name}] 🖱️ 点击播放/详情，触发预加载 ({p_name})")

                            # 直接后台预加载，不等待响应
                            asyncio.create_task(
                                _preload_rapid_transfer(item_id, ua, p_name, emby_index)
                            )
            except Exception as e:
                logger.error(f"[Preload] PlaybackInfo 触发失败: {e}")

        # === 模式 A: 需要解析响应（列表页和详情页） ===
        if should_parse_response:
            req = proxy_client.build_request(request.method, target_url, headers=headers, content=None)
            r = await proxy_client.send(req, stream=False)

            try:
                data = r.json()
                ua = request.headers.get("user-agent", "")
                
                # 触发之前的缓存任务
                background_tasks.add_task(handle_response_parsing, data, ua, preload_count)

                # 单集详情页预加载触发（排除列表页，只预加载用户点进去的单集）
                if enable_preload and await should_trigger_preload():
                    if isinstance(data, dict) and "Items" not in data:
                        iid = data.get("Id")
                        itype = data.get("Type", "")
                        # 仅对单独的视频进行预加载
                        if iid and itype in ["Movie", "Episode"]:
                            if iid not in preload_dedupe_cache:
                                preload_dedupe_cache[iid] = True
                                p_name = name_cache.get(iid, get_friendly_name(data))
                                logger.info(f"[Preload-{emby_name}] 🖱️ 探知到详情页，提前触发后台预加载 ({p_name})")
                                asyncio.create_task(
                                    _preload_rapid_transfer(iid, ua, p_name, emby_index)
                                )
                            
            except Exception as e:
                if r.status_code != 200:
                    pass
                else:
                    logger.warning(f"[Gateway] 响应解析警告: {e}")

            response_headers = dict(r.headers)
            for key in ["transfer-encoding", "content-length", "connection", "content-encoding"]:
                response_headers.pop(key, None)

            return Response(content=r.content, status_code=r.status_code, headers=response_headers)

        # === 模式 B: 普通/流式请求 ===
        req = None
        if request.method in ["GET", "HEAD", "OPTIONS"]:
            req = proxy_client.build_request(request.method, target_url, headers=headers, content=None)
        elif not is_playback:
            body_content = await request.body()
            req = proxy_client.build_request(request.method, target_url, headers=headers, content=body_content)
        else:
            req = proxy_client.build_request(request.method, target_url, headers=headers, content=request.stream())
        
        r = await proxy_client.send(req, stream=True)
        
        if "Similar" in path and r.status_code >= 500:
            await r.aclose()
            # 静默处理，部分剧集没有相关推荐属于正常情况
            return JSONResponse(content={"Items": [], "TotalRecordCount": 0})

        response_headers = dict(r.headers)
        for key in ["transfer-encoding", "content-length", "connection"]:
            response_headers.pop(key, None)

        return ProxyStreamingResponse(
            r,
            status_code=r.status_code,
            headers=response_headers
        )
    except Exception as e:
        logger.error(f"[Gateway] 转发失败: {e}")
        return Response(f"Gateway Error: {e}", status_code=502)