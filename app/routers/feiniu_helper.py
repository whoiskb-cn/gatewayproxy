import httpx
import os
import json
import time
import re
import asyncio
import hashlib
from fastapi import Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from starlette.background import BackgroundTask
from cachetools import TTLCache
from app.routers.config_302 import get_config_302
from app.services.drive115_service import drive115_service
from core.logger import logger
from urllib.parse import urlparse, urljoin

# 缓存视频元数据: {guid: {"path": str, "idx": int}}
FN_META_CACHE = TTLCache(maxsize=3000, ttl=7200)
# 端口映射
PORT_TO_FEINIU_INDEX = {}

def register_feiniu_port(port: int, idx: int):
    PORT_TO_FEINIU_INDEX[port] = idx

# 基础转发组件
from app.routers.gateway import proxy_client, ProxyStreamingResponse

def _rewrite_location(location_url: str, request: Request, base_url: str):
    if not location_url: return location_url
    try:
        p_loc = urlparse(location_url)
        p_base = urlparse(base_url)
        if not p_loc.netloc or p_loc.netloc == p_base.netloc:
            gw_host = request.url.netloc
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            new_loc = urljoin(f"{scheme}://{gw_host}", p_loc.path)
            if p_loc.query: new_loc += f"?{p_loc.query}"
            return new_loc
    except: pass
    return location_url

def _build_target_url(base_url_str, path, request):
    p_base = urlparse(base_url_str)
    base_root = f"{p_base.scheme}://{p_base.netloc}"
    clean_path = path.lstrip("/")
    prefix = p_base.path.rstrip("/")
    if prefix and not f"/{clean_path}".startswith(prefix):
        target_url = f"{base_root}{prefix}/{clean_path}"
    else:
        target_url = f"{base_root}/{clean_path}"
    query = request.scope.get("query_string", b"").decode()
    if query: target_url += ("&" if "?" in target_url else "?") + query
    return target_url

async def handle_feiniu_request(request: Request, path: str, request_port: int, background_tasks):
    lower_path = path.lower()
    
    # 1. 播放重定向
    if "fn_red" in lower_path:
        item_id = path.split("fn_red/", 1)[1].split(".", 1)[0]
        return await _handle_feiniu_real_redirect(request, item_id)

    # 获取配置
    cfg = await get_config_302()
    feinius = cfg.get("feinius", [])
    fn_idx = PORT_TO_FEINIU_INDEX.get(request_port, 0)
    if fn_idx >= len(feinius): fn_idx = 0
    fn_cfg = feinius[fn_idx] if feinius else {}
    base_url = fn_cfg.get("url", "").rstrip("/")
    
    if not base_url: return Response(status_code=502)

    # 2. 智能根路径与跳转处理 (修复登录空白)
    if not path or path == "/":
        ua_lower = ua.lower()
        if "mozilla" in ua_lower or "applewebkit" in ua_lower:
             return RedirectResponse(url="/v/", status_code=302)
        return Response(status_code=200, headers={"Accept-Ranges": "bytes", "Content-Type": "text/html"})

    if path.strip("/") == "v":
        ua_lower = ua.lower()
        if "mozilla" in ua_lower or "applewebkit" in ua_lower:
             return await _proxy_request(request, base_url, path)
        return Response(status_code=200, headers={"Accept-Ranges": "bytes", "Content-Type": "text/html"})

    # 拦截核心点播接口
    if request.method == "POST":
        if "api/v1/play/info" in lower_path:
            return await _intercept_feiniu_play_info(request, base_url, path, fn_idx)
        if "api/v1/stream" in lower_path:
            return await _intercept_feiniu_stream_post(request, base_url, path, fn_idx)

    return await _proxy_request(request, base_url, path)

async def _proxy_request(request: Request, base_url: str, path: str):
    target_url = _build_target_url(base_url, path, request)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ["host", "content-length", "connection"]}
    try:
        req = proxy_client.build_request(request.method, target_url, headers=headers,
                                         content=await request.body() if request.method not in ["GET", "HEAD"] else None)
        r = await proxy_client.send(req, stream=True)
        rh = dict(r.headers)
        if "location" in rh: rh["location"] = _rewrite_location(rh["location"], request, base_url)
        for h in ["transfer-encoding", "connection", "content-encoding", "content-length"]: rh.pop(h, None)
        return ProxyStreamingResponse(r, status_code=r.status_code, headers=rh)
    except Exception:
        return Response(status_code=502)

async def _intercept_feiniu_play_info(request: Request, base_url: str, path: str, fn_idx: int):
    target_url = _build_target_url(base_url, path, request)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ["host", "content-length", "connection"]}
    try:
        r = await proxy_client.post(target_url, headers=headers, content=await request.body())
        rh = dict(r.headers)
        for h in ["content-length", "content-encoding", "transfer-encoding"]: rh.pop(h, None)
        if r.status_code == 200:
            try:
                data = r.json()
                item = data.get("data", {})
                if item: item["play_config"] = None 
                content = json.dumps(data, ensure_ascii=False).encode('utf-8')
                return Response(content=content, status_code=200, headers=rh, media_type="application/json")
            except: pass
        return Response(content=r.content, status_code=r.status_code, headers=rh)
    except: return await _proxy_request(request, base_url, path)

async def _intercept_feiniu_stream_post(request: Request, base_url: str, path: str, fn_idx: int):
    target_url = _build_target_url(base_url, path, request)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ["host", "content-length", "connection"]}
    try:
        r = await proxy_client.post(target_url, headers=headers, content=await request.body())
        rh = dict(r.headers)
        for h in ["content-length", "content-encoding", "transfer-encoding"]: rh.pop(h, None)
        if r.status_code == 200:
            try:
                resp_json = r.json()
                v_data = resp_json.get("data", {})
                qualities = v_data.get("qualities") or v_data.get("direct_link_qualities") or []
                file_obj = v_data.get("file_stream", {})
                file_path = file_obj.get("path")
                
                if qualities and file_path:
                    guid = v_data.get("guid") or hashlib.md5(file_path.encode()).hexdigest()[:16]
                    FN_META_CACHE[guid] = {"path": file_path, "idx": fn_idx}
                    
                    orig_ext = os.path.splitext(file_path)[1].lower()
                    ext = ".mkv" if orig_ext == ".strm" or not orig_ext else orig_ext
                    
                    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
                    gw_host = request.url.netloc
                    
                    # 稳定路径
                    hijack_path = f"/fn_red/{guid}{ext}"
                    hijack_full_url = f"{scheme}://{gw_host}{hijack_path}"
                    
                    for q in qualities: q["url"] = hijack_full_url
                    if file_obj:
                        file_obj["uri"] = hijack_full_url
                        file_obj["url"] = hijack_full_url
                        file_obj["host"] = gw_host
                    
                    logger.info(f"[飞牛网关] 🔗 注入劫持路径: {guid}{ext}")
                    content = json.dumps(resp_json, ensure_ascii=False).encode('utf-8')
                    return Response(content=content, status_code=200, headers=rh, media_type="application/json")
            except: pass
        return Response(content=r.content, status_code=r.status_code, headers=rh)
    except: return await _proxy_request(request, base_url, path)

async def _handle_feiniu_real_redirect(request: Request, item_id: str):
    meta = FN_META_CACHE.get(item_id)
    if not meta: return Response(status_code=410)
    
    file_path = meta["path"]
    fn_idx = meta["idx"]
    real_ua = request.headers.get("user-agent", "")
    
    logger.info(f"[飞牛网关] 🔄 提取 115 直链 (针对 UA: {real_ua[:30]}...)")
    
    real_url = await drive115_service.get_navidrome_direct_url(
        f"fn_{item_id[:8]}", file_path, real_ua, "Video", fn_idx, drive_type='feiniu'
    )
    
    if real_url:
        logger.info(f"[飞牛网关] ✅ 下发重定向流")
        resp = RedirectResponse(url=real_url, status_code=302)
        resp.headers.update({
            "Referrer-Policy": "no-referrer",
            "Access-Control-Allow-Origin": "*",
            "Content-Type": "video/x-matroska"
        })
        return resp
    return Response(status_code=502)

async def handle_feiniu_websocket(websocket: WebSocket, path: str, request_port: int):
    cfg = await get_config_302()
    feinius = cfg.get("feinius", [])
    fn_idx = PORT_TO_FEINIU_INDEX.get(request_port, 0)
    target_cfg = feinius[fn_idx] if fn_idx < len(feinius) else {}
    base_url = target_cfg.get("url", "").rstrip("/")
    if not base_url: return await websocket.close()
    p = urlparse(base_url)
    ws_url = f"{'ws' if p.scheme == 'http' else 'wss'}://{p.netloc}{p.path.rstrip('/')}{path if path.startswith('/') else '/'+path}"
    if websocket.scope.get("query_string"): ws_url += f"?{websocket.scope.get('query_string').decode()}"
    headers = {k.decode(): v.decode() for k, v in websocket.scope.get("headers", []) 
               if k.decode().lower() not in ["host", "upgrade", "connection", "sec-websocket-key", "sec-websocket-version"]}
    try:
        import websockets
        async with websockets.connect(ws_url, extra_headers=headers) as target_ws:
            await websocket.accept()
            async def f1():
                async for m in target_ws:
                    if isinstance(m, str): await websocket.send_text(m)
                    else: await websocket.send_bytes(m)
            async def f2():
                while True:
                    m = await websocket.receive()
                    if "text" in m: await target_ws.send(m["text"])
                    elif "bytes" in m: await target_ws.send(m["bytes"])
            await asyncio.gather(f1(), f2())
    except:
        try: await websocket.close()
        except: pass
