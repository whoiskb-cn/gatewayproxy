from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
import os
import re
from core.logger import logger

router = APIRouter(prefix="/api/music", tags=["music"])

# go-music-dl 服务地址，支持环境变量配置以便于 Docker 组网
MUSIC_DL_URL = os.getenv("MUSIC_DL_URL", "http://127.0.0.1:8080/music")


def parse_size_value(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return float(raw)
        try:
            return float(raw)
        except ValueError:
            pass

        patterns = {
            r"^([\d.]+)\s*([kK]|[kK]b)$": 1024.0,
            r"^([\d.]+)\s*([mM]|[mM]b)$": 1024.0 ** 2,
            r"^([\d.]+)\s*([gG]|[gG]b)$": 1024.0 ** 3,
            r"^([\d.]+)\s*([tT]|[tT]b)$": 1024.0 ** 4,
            r"^([\d.]+)\s*[bB]$": 1.0,
        }
        for pattern, factor in patterns.items():
            m = re.match(pattern, raw)
            if m:
                try:
                    return float(m.group(1)) * factor
                except ValueError:
                    return 0.0
    return 0.0


def get_item_sort_value(item: dict) -> float:
    if not isinstance(item, dict):
        return 0.0

    for key in ("size", "filesize", "file_size", "size_bytes", "bytes"):
        if key in item:
            size = parse_size_value(item.get(key))
            if size > 0:
                return size

    for key in ("track_count", "song_count", "count", "num_tracks"):
        if key in item:
            try:
                return float(item.get(key) or 0)
            except (TypeError, ValueError):
                pass

    for key in ("duration", "time"):
        if key in item:
            try:
                return float(item.get(key) or 0)
            except (TypeError, ValueError):
                pass

    return 0.0


def sort_items_desc(items: list) -> list:
    if not isinstance(items, list):
        return items
    return sorted(items, key=get_item_sort_value, reverse=True)


def sort_search_response(data):
    if isinstance(data, list):
        return sort_items_desc(data)
    if not isinstance(data, dict):
        return data

    for key in ("songs", "playlists", "items", "tracks"):
        if key in data and isinstance(data[key], list):
            data[key] = sort_items_desc(data[key])

    return data


@router.get("/search")
async def search_music(q: str, type: str = "song", sources: str = Query(None)):
    """搜索音乐"""
    params = {"q": q, "type": type, "format": "json"}
    
    # 如果 sources 是逗号分隔的字符串，直接传递给 go-music-dl
    if sources:
        params["sources"] = sources
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(f"{MUSIC_DL_URL}/search", params=params)
            return JSONResponse(content=sort_search_response(resp.json()), status_code=resp.status_code)
    except Exception as e:
        logger.error(f"[Music] 搜索失败: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

@router.get("/download")
async def download_music(request: Request):
    """
    流式转发下载请求 (智能识别：支持 JSON 和文件流)
    """
    params = dict(request.query_params)
    
    try:
        # 使用长超时
        async with httpx.AsyncClient(timeout=120.0) as client:
            # 发起流式请求
            async with client.stream("GET", f"{MUSIC_DL_URL}/download", params=params) as resp:
                # 检查响应类型
                content_type = resp.headers.get("Content-Type", "")
                
                # 如果是 JSON 响应 (通常是 save_local=1 的成功提示)
                if "application/json" in content_type:
                    # 读取完整 body 并返回 JSONResponse
                    body = await resp.aread()
                    return JSONResponse(content=resp.json(), status_code=resp.status_code)
                
                # 否则作为文件流转发
                headers = dict(resp.headers)
                for h in ["transfer-encoding", "connection"]:
                    headers.pop(h, None)
                
                return StreamingResponse(
                    resp.aiter_bytes(),
                    status_code=resp.status_code,
                    headers=headers
                )
    except Exception as e:
        logger.error(f"[Music] 下载转发失败: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=502)

@router.get("/inspect")
async def inspect_music(request: Request):
    """
    转发预检请求 (获取大小、比特率、有效性)
    """
    params = dict(request.query_params)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{MUSIC_DL_URL}/inspect", params=params)
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        return JSONResponse(content={"valid": False, "error": str(e)}, status_code=200)

@router.get("/settings")
async def get_settings():
    """获取音乐下载器设置"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{MUSIC_DL_URL}/settings")
        return JSONResponse(content=resp.json())

@router.get("/album")
async def get_album(request: Request):
    """转发获取专辑详情请求"""
    params = dict(request.query_params)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{MUSIC_DL_URL}/album", params=params)
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=502)

@router.get("/playlist")
async def get_playlist(request: Request):
    """转发获取歌单详情请求"""
    params = dict(request.query_params)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{MUSIC_DL_URL}/playlist", params=params)
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=502)

