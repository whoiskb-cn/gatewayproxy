# -*- coding: utf-8 -*-
"""
微信音乐机器人
支持企业微信消息回调，提供歌曲搜索/专辑搜索、
音乐源选择、翻页浏览和一键下载功能。
"""

import os
import re
import json
import logging
import asyncio
import httpx
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse, Response

# 尝试导入 wechatpy，如果未安装则降级处理
try:
    from wechatpy.enterprise import WeChatClient
    from wechatpy.enterprise.crypto import WeChatCrypto
    from wechatpy.exceptions import InvalidSignatureException
    from wechatpy import parse_message, create_reply
    WECHAT_AVAILABLE = True
except ImportError:
    WECHAT_AVAILABLE = False

LOGGER = logging.getLogger("WeChatMusic")

# 路由前缀 /wechat/music
router = APIRouter(prefix="/wechat/music", tags=["wechat-music"])

# 用户会话存储：{user_id: {"state": ..., ...}}
USER_SESSIONS: dict = {}

# 每页展示的歌曲/专辑数量
PAGE_SIZE = 5

# 所有可用的音乐源（与前端保持一致）
ALL_SOURCES = [
    {"id": "netease",  "name": "网易云音乐"},
    {"id": "qq",       "name": "QQ音乐"},
    {"id": "kugou",    "name": "酷狗音乐"},
    {"id": "kuwo",     "name": "酷我音乐"},
    {"id": "migu",     "name": "咪咕音乐"},
    {"id": "fivesing", "name": "5sing"},
    {"id": "jamendo",  "name": "Jamendo"},
    {"id": "joox",     "name": "JOOX"},
    {"id": "qianqian", "name": "千千音乐"},
    {"id": "soda",     "name": "汽水音乐"},
    {"id": "bilibili", "name": "Bilibili"},
]

# ──────────────────────────────────────────────
# 配置读取
# ──────────────────────────────────────────────

def load_config() -> dict:
    """读取系统配置"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, "config", "config_302.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            LOGGER.error(f"读取配置失败: {e}")
    return {}


def get_wecom_conf() -> dict:
    """获取企业微信配置节点"""
    return load_config().get("wecom_music", {})


def get_music_api_url() -> str:
    """获取音乐后端地址（Go 引擎）。优先使用环境变量，其次读取配置文件。"""
    env_url = os.getenv("MUSIC_DL_URL")
    if env_url:
        return env_url.rstrip("/")

    conf = load_config()
    return conf.get("music_api_url", "http://0.0.0.0:8090/music").rstrip("/")


# ──────────────────────────────────────────────
# 企业微信客户端
# ──────────────────────────────────────────────

def get_wechat_client():
    """初始化企业微信客户端"""
    if not WECHAT_AVAILABLE:
        LOGGER.error("wechatpy 未安装，请执行: pip install wechatpy")
        return None, None

    conf = get_wecom_conf()
    if not conf.get("enabled"):
        return None, None

    corp_id  = conf.get("corp_id", "")
    secret   = conf.get("secret", "")
    agent_id = conf.get("agent_id", "")

    if not (corp_id and secret and agent_id):
        LOGGER.warning("企业微信配置不完整 (corp_id/secret/agent_id)")
        return None, None

    try:
        client = WeChatClient(corp_id, secret)

        # 代理支持（可选）
        proxy = conf.get("proxy", "")
        if proxy:
            if proxy.startswith("http://") or proxy.startswith("https://"):
                base = proxy.rstrip("/")
                if not base.endswith("/cgi-bin"):
                    base += "/cgi-bin"
                client.API_BASE_URL = base + "/"
                LOGGER.info(f"微信使用 API 反向代理: {base}")
        return client, agent_id
    except Exception as e:
        LOGGER.error(f"企业微信客户端初始化失败: {e}")
        return None, None


def send_wecom_msg(text: str, to_user: str = "@all"):
    """主动推送企业微信消息"""
    client, agent_id = get_wechat_client()
    if not client:
        LOGGER.warning("企业微信客户端不可用，跳过消息推送")
        return
    try:
        client.message.send_text(agent_id, to_user, text)
        LOGGER.info(f"微信消息已推送到 {to_user}: {text[:40]}...")
    except Exception as e:
        LOGGER.error(f"微信消息推送失败: {e}")


# ──────────────────────────────────────────────
# 格式化工具函数
# ──────────────────────────────────────────────

def format_source_menu() -> str:
    """格式化音乐源选择菜单"""
    lines = ["🎵 请选择音乐源 (回复序号，0=全部):\n" + "─" * 22]
    for i, src in enumerate(ALL_SOURCES, 1):
        lines.append(f"{i}. {src['name']}")
    lines.append("\n0. 全部音乐源")
    return "\n".join(lines)


def parse_size_value(value) -> float:
    """解析可能的人类可读大小或数字大小，返回字节数。"""
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


def format_size_display(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("size", "filesize", "file_size", "size_bytes", "bytes"):
        if key in item:
            size = parse_size_value(item.get(key))
            if size <= 0:
                continue
            if size >= 1024 ** 3:
                return f"{size / 1024 ** 3:.2f}GB"
            if size >= 1024 ** 2:
                return f"{size / 1024 ** 2:.1f}MB"
            if size >= 1024:
                return f"{size / 1024:.1f}KB"
            return f"{size:.0f}B"
    return ""


def sort_items_desc(items: list) -> list:
    if not isinstance(items, list):
        return items
    return sorted(items, key=get_item_sort_value, reverse=True)


def format_song_results(items: list, page: int, search_type: str = "song") -> str:
    """格式化歌曲/专辑搜索结果（分页）"""
    total   = len(items)
    start   = page * PAGE_SIZE
    end     = min(start + PAGE_SIZE, total)
    page_items = items[start:end]

    icon  = "🎵" if search_type == "song" else "💿"
    title = "歌曲" if search_type == "song" else "专辑"

    lines = [f"{icon} {title}搜索结果 ({start + 1}–{end} / 共 {total} 条):\n" + "─" * 22]
    for i, item in enumerate(page_items, 1):
        name   = item.get("name") or item.get("Name") or "未知"
        artist = item.get("artist") or item.get("Artist") or ""
        album  = item.get("album") or item.get("Album") or ""
        source = (item.get("source") or item.get("Source") or "").upper()
        size   = format_size_display(item)

        lines.append(f"【{i}】{name}")
        if artist:
            lines.append(f"   歌手: {artist}")
        if search_type == "song" and album:
            lines.append(f"   专辑: {album}")
        if source:
            lines.append(f"   来源: {source}")
        if size:
            lines.append(f"   大小: {size}")

    nav = []
    if start > 0:
        nav.append("P=上一页")
    if end < total:
        nav.append("N=下一页")
    nav.append("回复序号下载")

    lines.append("\n" + " | ".join(nav))
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 音乐 API 调用
# ──────────────────────────────────────────────

async def search_music(keyword: str, sources: list, search_type: str = "song") -> list:
    """调用后端搜索接口，返回歌曲或专辑列表"""
    api_url  = get_music_api_url()
    src_str  = ",".join(sources)
    params   = {
        "q":       keyword,
        "sources": src_str,
        "type":    search_type,
        "format":  "json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{api_url}/search", params=params)
            data = resp.json()
            if search_type == "song":
                return sort_items_desc(data.get("songs") or [])
            else:
                return sort_items_desc(data.get("playlists") or [])
    except Exception as e:
        LOGGER.error(f"音乐搜索失败: {e}")
        return []


async def do_download(song: dict) -> tuple[bool, str]:
    """触发后端单曲下载"""
    api_url = get_music_api_url()
    params  = {
        "id":     song.get("id") or song.get("ID") or "",
        "source": song.get("source") or song.get("Source") or "",
        "name":   song.get("name") or song.get("Name") or "",
        "artist": song.get("artist") or song.get("Artist") or "",
        "album":  song.get("album") or song.get("Album") or "",
        "cover":  song.get("cover") or song.get("Cover") or "",
        "save_local": "1",  # 确保文件保存到本地
        "embed": "1",       # 嵌入元数据（歌词/封面）
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.get(f"{api_url}/download", params=params)

            # 检查响应状态
            if resp.status_code == 200:
                # 检查Content-Type
                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    # JSON响应：文件已保存到本地
                    try:
                        data = resp.json()
                        if data.get("status") == "ok":
                            return True, data.get("filename", "未知文件名")
                        else:
                            return False, data.get("error", "下载失败")
                    except Exception as e:
                        LOGGER.error(f"解析JSON响应失败: {e}")
                        return False, "响应解析失败"
                else:
                    # 文件流响应：直接返回成功（文件名从响应头获取）
                    content_disposition = resp.headers.get("Content-Disposition", "")
                    filename = "未知文件名"
                    if "filename=" in content_disposition:
                        import re
                        match = re.search(r'filename[^;=\n]*=(([\'"]).*?\2|[^;\n]*)', content_disposition)
                        if match:
                            filename = match.group(1).strip('\'"')
                    return True, filename
            else:
                # 尝试解析错误信息
                try:
                    data = resp.json()
                    return False, data.get("error", f"HTTP {resp.status_code}")
                except:
                    return False, f"HTTP {resp.status_code}"

    except Exception as e:
        LOGGER.error(f"下载请求失败: {e}")
        return False, str(e)


async def fetch_album_songs(album: dict) -> list:
    """获取专辑内的歌曲列表"""
    api_url = get_music_api_url()
    params  = {
        "id":     album.get("id") or album.get("ID") or "",
        "source": album.get("source") or album.get("Source") or "",
        "format": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{api_url}/album", params=params)
            data = resp.json()
            return sort_items_desc(data.get("songs") or [])
    except Exception as e:
        LOGGER.error(f"获取专辑歌曲失败: {e}")
        return []


# ──────────────────────────────────────────────
# 核心消息处理逻辑
# ──────────────────────────────────────────────

async def handle_message(user_id: str, content: str) -> str:
    """
    处理用户发来的文本消息，返回回复内容字符串。
    所有多轮会话均通过 USER_SESSIONS[user_id] 管理。
    """
    content = content.strip()
    session = USER_SESSIONS.get(user_id, {})
    state   = session.get("state", "IDLE")

    # ── 帮助指令（任何状态下均可触发）──
    if content in ("help", "帮助", "?", "？"):
        USER_SESSIONS.pop(user_id, None)
        return (
            "🎵 音乐下载机器人 使用说明\n"
            + "─" * 22 + "\n"
            "🔍 发起搜索\n"
            "  · 发送 歌曲名 → 搜索单曲\n"
            "  · 发送 专辑 专辑名 → 搜索专辑\n"
            "    示例：专辑 依然范特西\n\n"
            "📋 搜索流程\n"
            "  1) 选音乐源：0=全部，1-N=指定源\n"
            "  2) 浏览结果：N=下一页 P=上一页\n"
            "     （大小写均可）\n"
            "  3) 回复序号：\n"
            "     · 单曲 → 直接下载\n"
            "     · 专辑 → 列出专辑歌曲，\n"
            "       再回复序号下载具体歌曲\n\n"
            "🛠 其它指令\n"
            "  · 取消 / 退出 / q → 中断当前操作\n"
            "  · 帮助 / help / ? → 再次查看本说明"
        )

    # ── 取消/重置指令 ──
    if content in ("取消", "退出", "cancel", "q"):
        USER_SESSIONS.pop(user_id, None)
        return "✅ 已取消当前操作"

    # ═════════════════════════════════════════
    # 状态机处理
    # ═════════════════════════════════════════

    # ── 状态 SELECT_SOURCE：等待用户选择音乐源 ──
    if state == "SELECT_SOURCE":
        keyword     = session.get("keyword", "")
        search_type = session.get("search_type", "song")

        selected_sources: list[str] = []

        if content == "0":
            # 全部音乐源
            selected_sources = [s["id"] for s in ALL_SOURCES]
        elif content.isdigit():
            idx = int(content) - 1
            if 0 <= idx < len(ALL_SOURCES):
                selected_sources = [ALL_SOURCES[idx]["id"]]
            else:
                return f"⚠️ 序号超出范围，请输入 0–{len(ALL_SOURCES)}"
        else:
            return f"请回复数字序号选择音乐源 (0=全部，1–{len(ALL_SOURCES)}=指定源)\n输入 取消 退出"

        src_names = "、".join(
            s["name"] for s in ALL_SOURCES if s["id"] in selected_sources
        ) if len(selected_sources) < len(ALL_SOURCES) else "全部音乐源"

        # 发起搜索（异步推送结果）
        send_wecom_msg(f"🔍 正在用 [{src_names}] 搜索「{keyword}」，请稍候...", to_user=user_id)

        USER_SESSIONS.pop(user_id, None)
        asyncio.create_task(
            _do_search_and_push(user_id, keyword, selected_sources, search_type)
        )
        return None  # 不再同步回复，结果走主动推送

    # ── 状态 SHOW_RESULTS：展示搜索结果、翻页、选择 ──
    if state == "SHOW_RESULTS":
        items       = session.get("items", [])
        page        = session.get("page", 0)
        search_type = session.get("search_type", "song")
        keyword     = session.get("keyword", "")
        total       = len(items)

        # 翻页
        if content.upper() == "N":
            new_page = page + 1
            if new_page * PAGE_SIZE < total:
                session["page"] = new_page
                USER_SESSIONS[user_id] = session
                return format_song_results(items, new_page, search_type)
            else:
                return "📄 已经是最后一页了"

        if content.upper() == "P":
            new_page = page - 1
            if new_page >= 0:
                session["page"] = new_page
                USER_SESSIONS[user_id] = session
                return format_song_results(items, new_page, search_type)
            else:
                return "📄 已经是第一页了"

        # 选择条目
        if content.isdigit():
            page_items = items[page * PAGE_SIZE: min((page + 1) * PAGE_SIZE, total)]
            relative_idx = int(content) - 1
            if 0 <= relative_idx < len(page_items):
                idx = page * PAGE_SIZE + relative_idx
                selected = items[idx]

                if search_type == "album":
                    # 进入专辑详情：先拉取歌曲列表，再展示给用户下载
                    album_name = selected.get("name") or selected.get("Name") or "未知专辑"
                    send_wecom_msg(f"⏳ 正在加载专辑「{album_name}」的歌曲列表...", to_user=user_id)
                    USER_SESSIONS.pop(user_id, None)
                    asyncio.create_task(
                        _do_load_album_and_push(user_id, selected)
                    )
                    return None  # 走主动推送

                else:
                    # 单曲：直接下载
                    name   = selected.get("name") or selected.get("Name") or "未知"
                    artist = selected.get("artist") or selected.get("Artist") or ""
                    label  = f"{name}" + (f" - {artist}" if artist else "")
                    send_wecom_msg(f"⬇️ 正在下载「{label}」，请稍候...", to_user=user_id)
                    asyncio.create_task(
                        _do_download_and_push(user_id, selected)
                    )
                    return None  # 走主动推送
            else:
                return f"⚠️ 序号无效，请输入 1–{min(PAGE_SIZE, total - page * PAGE_SIZE)}"

        return "输入无效。N=下一页 P=上一页 数字=选择 取消=退出"

    # ── 状态 SHOW_ALBUM_SONGS：专辑内歌曲浏览、下载 ──
    if state == "SHOW_ALBUM_SONGS":
        items = session.get("items", [])
        page  = session.get("page", 0)
        total = len(items)

        if content.upper() == "N":
            new_page = page + 1
            if new_page * PAGE_SIZE < total:
                session["page"] = new_page
                USER_SESSIONS[user_id] = session
                return format_song_results(items, new_page, "song")
            else:
                return "📄 已经是最后一页了"

        if content.upper() == "P":
            new_page = page - 1
            if new_page >= 0:
                session["page"] = new_page
                USER_SESSIONS[user_id] = session
                return format_song_results(items, new_page, "song")
            else:
                return "📄 已经是第一页了"

        if content.isdigit():
            page_items = items[page * PAGE_SIZE: min((page + 1) * PAGE_SIZE, total)]
            relative_idx = int(content) - 1
            if 0 <= relative_idx < len(page_items):
                idx = page * PAGE_SIZE + relative_idx
                selected = items[idx]
                name     = selected.get("name") or selected.get("Name") or "未知"
                artist   = selected.get("artist") or selected.get("Artist") or ""
                label    = f"{name}" + (f" - {artist}" if artist else "")
                send_wecom_msg(f"⬇️ 正在下载「{label}」，请稍候...", to_user=user_id)
                asyncio.create_task(_do_download_and_push(user_id, selected))
                return None
            else:
                return f"⚠️ 序号无效，请输入 1–{min(PAGE_SIZE, total - page * PAGE_SIZE)}"

        return "输入无效。N=下一页 P=上一页 数字=下载 取消=退出"

    # 专辑模式
    if content.startswith("专辑 ") or content.startswith("专辑　"):
        keyword = content[3:].strip()
        if not keyword:
            return "请输入专辑名称，例如：专辑 依然范特西"
        USER_SESSIONS[user_id] = {
            "state":       "SELECT_SOURCE",
            "keyword":     keyword,
            "search_type": "album",
        }
        return f"💿 搜索专辑「{keyword}」\n\n" + format_source_menu()

    # 单曲模式（任何其他文本视为歌曲名）
    if content:
        USER_SESSIONS[user_id] = {
            "state":       "SELECT_SOURCE",
            "keyword":     content,
            "search_type": "song",
        }
        return f"🎵 搜索歌曲「{content}」\n\n" + format_source_menu()

    return "发送歌曲名称开始搜索，或发送 帮助 查看说明"


# ──────────────────────────────────────────────
# 异步后台任务
# ──────────────────────────────────────────────

async def _do_search_and_push(user_id: str, keyword: str, sources: list, search_type: str):
    """后台执行搜索并主动推送结果"""
    try:
        items = await search_music(keyword, sources, search_type)
        if not items:
            send_wecom_msg(f"😔 未找到「{keyword}」相关{'专辑' if search_type == 'album' else '歌曲'}", to_user=user_id)
            return

        USER_SESSIONS[user_id] = {
            "state":       "SHOW_RESULTS",
            "items":       items,
            "page":        0,
            "keyword":     keyword,
            "search_type": search_type,
        }
        send_wecom_msg(format_song_results(items, 0, search_type), to_user=user_id)
    except Exception as e:
        LOGGER.error(f"搜索推送任务异常: {e}")
        send_wecom_msg(f"❌ 搜索出错: {e}", to_user=user_id)


async def _do_load_album_and_push(user_id: str, album: dict):
    """后台获取专辑歌曲列表并推送"""
    try:
        songs = await fetch_album_songs(album)
        album_name = album.get("name") or album.get("Name") or "未知专辑"

        if not songs:
            send_wecom_msg(f"😔 专辑「{album_name}」暂无歌曲", to_user=user_id)
            return

        USER_SESSIONS[user_id] = {
            "state": "SHOW_ALBUM_SONGS",
            "items": songs,
            "page":  0,
        }
        header = f"💿 专辑「{album_name}」共 {len(songs)} 首歌曲\n"
        send_wecom_msg(header + format_song_results(songs, 0, "song"), to_user=user_id)
    except Exception as e:
        LOGGER.error(f"加载专辑歌曲任务异常: {e}")
        send_wecom_msg(f"❌ 获取专辑失败: {e}", to_user=user_id)


async def _do_download_and_push(user_id: str, song: dict):
    """后台执行下载并推送结果"""
    try:
        ok, result = await do_download(song)
        name   = song.get("name") or song.get("Name") or "未知"
        artist = song.get("artist") or song.get("Artist") or ""
        label  = f"{name}" + (f" - {artist}" if artist else "")

        if ok:
            send_wecom_msg(f"✅ 下载成功！\n🎵 {label}\n📁 文件名: {result}", to_user=user_id)
            USER_SESSIONS.pop(user_id, None)
        else:
            send_wecom_msg(
                f"❌ 下载失败: {result}\n🎵 {label}\n请重新回复序号下载，或发送 取消 退出",
                to_user=user_id,
            )
    except Exception as e:
        LOGGER.error(f"下载推送任务异常: {e}")
        send_wecom_msg(f"❌ 下载出错: {e}", to_user=user_id)


# ──────────────────────────────────────────────
# FastAPI 路由
# ──────────────────────────────────────────────

@router.get("")
async def verify_callback(
    msg_signature: str = Query(None),
    timestamp: str     = Query(None),
    nonce: str         = Query(None),
    echostr: str       = Query(None),
):
    """企业微信回调 URL 验证"""
    if not WECHAT_AVAILABLE:
        return Response(content="wechatpy not installed", status_code=500)

    conf = get_wecom_conf()
    token            = conf.get("token", "")
    encoding_aes_key = conf.get("encoding_aes_key", "")
    corp_id          = conf.get("corp_id", "")

    if not all([token, encoding_aes_key, corp_id]):
        return Response(content="config missing", status_code=500)

    try:
        crypto = WeChatCrypto(token, encoding_aes_key, corp_id)
        decrypted = crypto.check_signature(msg_signature, timestamp, nonce, echostr)
        return Response(content=decrypted)
    except InvalidSignatureException:
        return Response(content="invalid signature", status_code=403)
    except Exception as e:
        LOGGER.error(f"回调验证异常: {e}")
        return Response(content="error", status_code=500)


@router.post("")
async def receive_message(
    request: Request,
    msg_signature: str = Query(None),
    timestamp: str     = Query(None),
    nonce: str         = Query(None),
):
    """接收并处理企业微信消息"""
    if not WECHAT_AVAILABLE:
        return Response(content="success")

    conf = get_wecom_conf()
    token            = conf.get("token", "")
    encoding_aes_key = conf.get("encoding_aes_key", "")
    corp_id          = conf.get("corp_id", "")

    if not all([token, encoding_aes_key, corp_id]):
        LOGGER.warning("企业微信配置不完整，忽略消息")
        return Response(content="success")

    try:
        body         = await request.body()
        crypto       = WeChatCrypto(token, encoding_aes_key, corp_id)
        decrypted_xml = crypto.decrypt_message(body, msg_signature, timestamp, nonce)
        msg          = parse_message(decrypted_xml)

        # 仅处理文本消息和菜单点击事件
        if msg.type == "text":
            content = msg.content.strip()
        elif msg.type == "event" and msg.event == "click":
            content = msg.key
        elif msg.type == "event" and msg.event == "subscribe":
            content = "帮助"
        else:
            return Response(content="success")

        user_id = getattr(msg, "source", None) or getattr(msg, "sender", None)
        if not user_id:
            LOGGER.warning("无法识别微信消息来源，忽略消息")
            return Response(content="success")

        # 处理消息并获取回复
        reply_content = await handle_message(user_id, content)

        # reply_content 为 None 表示后台任务已推送，无需同步回复
        if reply_content is None:
            return Response(content="success")

        # 同步回复
        reply = create_reply(reply_content, msg)
        encrypted_xml = crypto.encrypt_message(reply.render(), nonce, timestamp)
        return Response(content=encrypted_xml, media_type="application/xml")

    except InvalidSignatureException:
        LOGGER.warning("消息签名验证失败")
        return Response(content="success")
    except Exception as e:
        LOGGER.exception(f"消息处理异常: {e}")
        return Response(content="success")


@router.get("/status")
async def wechat_status():
    """检查微信机器人配置状态"""
    conf    = get_wecom_conf()
    enabled = conf.get("enabled", False)
    has_cfg = all([
        conf.get("corp_id"),
        conf.get("secret"),
        conf.get("agent_id"),
        conf.get("token"),
        conf.get("encoding_aes_key"),
    ])
    return JSONResponse({
        "enabled":           enabled,
        "config_complete":   has_cfg,
        "wechatpy_installed": WECHAT_AVAILABLE,
        "active_sessions":   len(USER_SESSIONS),
    })
