import os
import json
import httpx
import asyncio
import traceback
import re
from cachetools import TTLCache
from p115client import P115Client
from p115rsacipher import encrypt, decrypt
from p115pickcode import to_id
from p115client.tool.attr import get_attr
from app.routers.config_302 import get_config_302

# 引入 115 客户端库
from p115client import P115Client
from p115rsacipher import encrypt, decrypt

from app.routers.config_302 import get_config_302
from core.logger import logger

class Drive115Service:
    def __init__(self):
        # 支持多个 115 账号的 client 缓存
        self._clients = {}  # {cookie: client}
        self._cookies = {}  # {drive_index: cookie}

        # === [第一级缓存] ID -> Pickcode (超级直通车) ===
        # 命中这个，直接跳过 Emby 路径查询和 115 文件搜索
        self._id_cache = TTLCache(maxsize=5000, ttl=3600)

        # === [第二级缓存] ID -> Emby物理路径 ===
        # 路径基本不变，缓存24小时
        self._emby_path_cache = TTLCache(maxsize=5000, ttl=86400)

        # === [第三级缓存] Path -> Pickcode ===
        # 知道了路径，不用去 115 搜索，直接拿 Pickcode
        self._path_cache = TTLCache(maxsize=5000, ttl=3600)

        # === [第四级缓存] Pickcode_UA -> 直链URL ===
        # 这既是直链缓存，也是"并发锁"的依据。
        # 如果能在这个缓存里找到某个 pickcode 的记录，说明该文件当前"正在被播放"
        self._url_cache = TTLCache(maxsize=1000, ttl=1200) # 20分钟有效

        # === [第五级缓存] Pickcode -> SHA1信息（用于秒传） ===
        self._sha1_cache = TTLCache(maxsize=1000, ttl=3600)  # SHA1 缓存

        # === [影子目录映射与 ID 缓存] ===
        self._dir_mapping = {}  # 原始目录名 -> 网盘实际名
        self._cid_cache = {}    # 目录全路径 -> 115 CID

        # === [小号池轮询计数器] ===
        self._rapid_account_index = 0  # 当前轮询到的账号索引

        # 并发锁
        self._item_locks = {}
        self._locks_cleanup_lock = asyncio.Lock()
        
        # 全局 HTTP 客户端
        self._http_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True, verify=False)

    async def get_client(self, emby_index: int = 0):
        """
        获取或初始化 P115Client（支持多账号）

        Args:
            emby_index: Emby 配置索引，用于确定使用哪个 115 账号
        """
        cfg = await get_config_302()

        # 获取 Emby 配置，查找关联的 115 索引
        drive_index = 0  # 默认使用第一个 115
        if emby_index is not None and emby_index >= 0:
            embys = cfg.get("embys", [])
            if isinstance(embys, list) and len(embys) > emby_index:
                emby_cfg = embys[emby_index]
                specified_index = emby_cfg.get("drive_index", -1)
                if specified_index >= 0:
                    drive_index = specified_index
                else:
                    # 如果 Emby 没有指定 drive_index，使用相同的索引
                    drive_index = emby_index
            else:
                drive_index = emby_index

        # 适配新的 drives 列表结构
        drives = cfg.get("drives", [])
        drive_cfg = {}

        if isinstance(drives, list) and len(drives) > drive_index:
            drive_cfg = drives[drive_index]
        elif isinstance(drives, list) and len(drives) > 0:
            drive_cfg = drives[0]  # 降级到第一个
        else:
            drive_cfg = cfg.get("drive115", {})

        cookie = drive_cfg.get("cookie", "")
        drive_name = drive_cfg.get("name", f"drives[{drive_index}]")

        if not cookie:
            logger.warning(f"[115] {drive_name} 未配置 cookie")
            return None, {}

        # 检查是否已有该 cookie 的 client，或需要重新创建
        if cookie not in self._clients:
            try:
                self._clients[cookie] = P115Client(cookie)
                self._cookies[cookie] = drive_index  # 用 cookie 做键，drive_index 做值（用于反向查找）
                logger.info(f"[115] 已创建客户端: {drive_name}")
            except Exception as e:
                logger.error(f"[115] 客户端登录失败 ({drive_name}): {e}")
                return None, {}

        return self._clients[cookie], drive_cfg

    async def get_secondary_client(self):
        """获取或初始化小号 P115Client（用于秒传）- 支持多账号池

        返回: (client, drive_cfg, rapid_cookie) 或 (None, {}, None)
        """
        cfg = await get_config_302()

        # 适配新的 drives 列表结构
        drives = cfg.get("drives", [])
        drive_cfg = {}

        if isinstance(drives, list) and len(drives) > 0:
            drive_cfg = drives[0]
        else:
            drive_cfg = cfg.get("drive115", {})

        # 获取小号池
        rapid_accounts = drive_cfg.get("rapid_accounts", [])
        rapid_mode = drive_cfg.get("rapid_mode", "auto")

        if not rapid_accounts:
            return None, {}, None

        # 根据调度策略选择小号
        selected_account = None
        account_index = 0

        if rapid_mode == "auto":
            # 自动轮询：按顺序循环使用小号
            account_index = self._rapid_account_index % len(rapid_accounts)
            self._rapid_account_index += 1  # 为下次请求递增
            selected_account = rapid_accounts[account_index]
        elif rapid_mode in [str(i) for i in range(len(rapid_accounts))]:
            # 指定账号（固定使用某个账号）
            account_index = int(rapid_mode)
            selected_account = rapid_accounts[account_index]
        else:
            # 无效的 rapid_mode，回退到第一个账号
            account_index = 0
            selected_account = rapid_accounts[0]

        rapid_cookie = selected_account.get("cookie", "")
        account_name = selected_account.get("name", f"小号{account_index + 1}")

        if not rapid_cookie:
            return None, {}, None

        try:
            client = P115Client(rapid_cookie)
            logger.info(f"[Rapid] 使用小号: {account_name} (模式: {rapid_mode}, 索引: {account_index}/{len(rapid_accounts)})")
            return client, drive_cfg, rapid_cookie
        except Exception as e:
            logger.error(f"[Rapid] 小号登录失败 ({account_name}): {e}")
            return None, {}, None

    # ==========================================================
    # [修改] 增加 item_name 参数，用于优化日志显示
    # ==========================================================
    async def get_direct_url(self, item_id: str, media_source_id: str = None, user_agent: str = "", item_name: str = None, emby_index: int = 0):
        """[核心入口] 获取播放直链

        Args:
            emby_index: Emby 配置索引，用于确定使用哪个 115 账号
        """

        # 防止并发重复请求同一 Item
        async with self._locks_cleanup_lock:
            if item_id not in self._item_locks:
                self._item_locks[item_id] = asyncio.Lock()
            item_lock = self._item_locks[item_id]

        async with item_lock:
            try:
                client, drive_cfg = await self.get_client(emby_index)
                if not client: return None

                drive_name = drive_cfg.get("name", f"drives[{emby_index}]")
                log_name = item_name if item_name else f"ID: {item_id}"

                pickcode = None

                # 1. 尝试从 ID 缓存获取 Pickcode
                if item_id in self._id_cache:
                    pickcode = self._id_cache[item_id]
                else:
                    # 2. 如果缓存没有，走完整解析流程
                    pickcode = await self._resolve_pickcode_flow(client, item_id, media_source_id, emby_index)
                
                if not pickcode:
                    return None

                # ==========================================================
                # 🚀 核心逻辑：写时复制 (Copy-on-Write) + 秒传
                # ==========================================================

                # 1. 检查开关
                enable_sync = drive_cfg.get('enable_sync', False)
                enable_rapid = drive_cfg.get('enable_rapid', False)

                # 2. 生成当前请求的缓存 Key
                cache_ua = user_agent if user_agent else "NoUA"
                cache_key = f"{pickcode}_{cache_ua}"

                # 3. 检查缓存命中 (如果是同一个人的重复请求，直接返回缓存，不触发复制)
                if cache_key in self._url_cache:
                    logger.info(f"✅ [Cache-{drive_name}] 命中直链缓存: {log_name}")
                    return self._url_cache[cache_key]

                # ==========================================================
                # 🚀 优先尝试秒传（如果启用）
                # ==========================================================
                if enable_rapid:
                    rapid_cache_key = f"rapid_{pickcode}"
                    if rapid_cache_key in self._url_cache:
                        logger.info(f"✅ [Rapid-{drive_name}] 命中秒传缓存: {log_name}")
                        return self._url_cache[rapid_cache_key]

                    logger.info(f"[Rapid-{drive_name}] 🔄 尝试秒传: {log_name}")

                    # 执行秒传
                    result = await self.get_secondary_client()
                    if not result or not result[0]:
                        secondary_client = None
                        sec_cfg = {}
                        rapid_cookie = None
                    else:
                        secondary_client, sec_cfg, rapid_cookie = result

                    if not secondary_client:
                        logger.warning(f"[Rapid] 小号客户端未配置或登录失败（请检查 rapid_cookie）")
                    else:
                        # 获取 SHA1 信息
                        sha1_info = await self._get_file_sha1_and_preupload_info(client, pickcode, user_agent, emby_index)
                        if not sha1_info:
                            logger.warning(f"[Rapid] 无法获取文件 SHA1 信息")
                        else:
                            logger.info(f"[Rapid] 获取 SHA1 成功: {sha1_info['sha1'][:16]}... (size: {sha1_info['size']})")
                            # 获取文件名（从路径中提取）
                            cfg = await get_config_302()
                            embys = cfg.get("embys", [])
                            # 使用传入的 emby_index 获取正确的配置
                            if isinstance(embys, list) and len(embys) > emby_index:
                                emby_cfg = embys[emby_index]
                            else:
                                emby_cfg = next((e for e in embys if e.get('enabled')), embys[0]) if embys else {}
                            file_path = await self._get_emby_file_path(emby_cfg, item_id, media_source_id)
                            filename = os.path.basename(file_path) if file_path else "video.mkv"

                            # 使用配置的上传目录（与小号复用）
                            target_dir = sec_cfg.get('upload_dir', '/gatewayproxy')

                            # 执行秒传
                            rapid_result = await self._rapid_transfer_to_secondary(
                                secondary_client, client, pickcode, sha1_info, filename, target_dir, user_agent
                            )

                            if rapid_result:
                                # 获取小号直链
                                rapid_pickcode = rapid_result['pickcode']
                                rapid_url = await self._fetch_download_url_r302(
                                    rapid_pickcode, user_agent,
                                    cookie=rapid_cookie
                                )

                                if rapid_url:
                                    # 缓存小号直链
                                    self._url_cache[cache_key] = rapid_url
                                    self._url_cache[rapid_cache_key] = rapid_url
                                    # 60秒后删除小号文件（传递 pickcode 作为备用）
                                    asyncio.create_task(
                                        self._delayed_remove_secondary(
                                            secondary_client,
                                            file_id=rapid_result.get('file_id'),
                                            pickcode=rapid_result.get('pickcode')
                                        )
                                    )
                                    logger.info(f"[Rapid] 🚀 返回小号直链: {item_name or pickcode}")
                                    return rapid_url
                                else:
                                    logger.warning(f"[Rapid] 无法获取小号直链")
                            else:
                                logger.warning(f"[Rapid] 秒传 API 返回失败")

                    logger.warning(f"[Rapid] 秒传失败，降级到原有流程")

                # 4. 检测并发 (仅在开关开启时)
                is_busy = False
                if enable_sync:
                    is_busy = self._is_file_busy(pickcode)

                if enable_sync and is_busy:
                    logger.info(f"[Sync-{drive_name}] ⚠️ 检测到并发播放: {log_name} -> 触发写时复制")

                    # --- 执行复制 & 获取新链 ---
                    new_url_data = await self._sync_copy_and_get_link(client, drive_cfg, pickcode, user_agent, emby_index)

                    if new_url_data:
                        new_url = new_url_data['url']
                        new_file_id = new_url_data['file_id']

                        # 写入缓存
                        self._url_cache[cache_key] = new_url

                        # 🔥 启动延迟删除任务 (60秒后删除副本)
                        asyncio.create_task(self._delayed_remove(new_file_id, delay=60))

                        logger.info(f"[Sync-{drive_name}] ✅ 写时复制成功: {log_name}")
                        return new_url
                    else:
                        logger.warning(f"[Sync-{drive_name}] 复制失败，降级使用原文件")

                # ==========================================================
                # 🐢 普通流程：获取原文件直链
                # ==========================================================

                logger.info(f"[115-{drive_name}] 🔄 获取直链: {log_name}")
                final_url = await self._fetch_download_url_r302(pickcode, user_agent, emby_index=emby_index)

                if final_url:
                    # 写入缓存
                    self._url_cache[cache_key] = final_url
                    logger.info(f"[115-{drive_name}] ✅ 直链获取成功: {log_name}")
                    return final_url
                else:
                    logger.warning(f"[115-{drive_name}] ⚠️ 直链获取失败: {log_name}")
                    self._url_cache[cache_key] = final_url
                    return final_url

                return None

            except Exception as e:
                logger.error(f"❌ [302] 异常: {e}")
                traceback.print_exc()
                return None

    async def get_navidrome_direct_url(self, item_id: str, file_path: str, user_agent: str, item_name: str, nav_index: int, artist=None, album=None, drive_type: str = "navidrome"):
        """Navidrome/TingReader 专属直链获取逻辑，支持元数据增强解析"""
        cfg = await get_config_302()
        
        if drive_type == "tingreader":
            apps = cfg.get("tingreaders", [])
        elif drive_type == "feiniu":
            apps = cfg.get("feinius", [])
        else:
            apps = cfg.get("navidromes", [])
            
        if not apps or nav_index >= len(apps): return None
        app_cfg = apps[nav_index]
        drive_index = app_cfg.get("drive_index", 0)

        # 加锁
        async with self._locks_cleanup_lock:
            if item_id not in self._item_locks:
                self._item_locks[item_id] = asyncio.Lock()
            item_lock = self._item_locks[item_id]

        async with item_lock:
            try:
                client, drive_cfg = await self.get_client(drive_index)
                if not client: return None
                
                drive_name = drive_cfg.get("name", f"drives[{drive_index}]")
                log_name = item_name if item_name else f"NaviID: {item_id}"
                
                pickcode = None
                if item_id in self._id_cache:
                    pickcode = self._id_cache[item_id]
                else:
                    modes = app_cfg.get("modes", {})
                    if not isinstance(modes, dict): modes = {"path_replace": True}
                    
                    pickcode_mode = modes.get("pickcode", False)
                    path_replace_mode = modes.get("path_replace", True)

                    # 1. Pickcode 模式 (尝试从 .strm 文件提取)
                    if pickcode_mode and file_path.lower().endswith(".strm"):
                        logger.info(f"[{drive_type}-115] 🍭 Pickcode 模式激活，尝试从 strm 提取")
                        try:
                            # 尝试读取本地文件
                            content = ""
                            if os.path.exists(file_path):
                                with open(file_path, 'r', encoding='utf-8') as f:
                                    content = f.read().strip()
                            elif file_path.startswith("http"):
                                # 如果是网络路径，尝试下载
                                async with httpx.AsyncClient(verify=False) as hc:
                                    r = await hc.get(file_path, timeout=5.0)
                                    if r.status_code == 200:
                                        content = r.text.strip()
                            
                            if content:
                                # 方案 A: strm 内容本身就是 pickcode=xxx
                                match = re.search(r'pickcode=([a-z0-9]+)', content, re.IGNORECASE)
                                if match:
                                    pickcode = match.group(1)
                                    logger.info(f"[{drive_type}-115] ✅ 从 strm 内容提取 pickcode 成功: {pickcode}")
                                else:
                                    # 方案 B: strm 内容是一个 /d/xxx 的链接
                                    match = re.search(r'/d/([a-z0-9]{10,})(\.[a-z0-9]+)?', content, re.IGNORECASE)
                                    if match:
                                        pickcode = match.group(1)
                                        logger.info(f"[{drive_type}-115] ✅ 从 strm 代理链接中提取 pickcode 成功: {pickcode}")
                        except Exception as e:
                            logger.warning(f"[{drive_type}-115] 读取 strm 异常: {e}")

                    # 2. 路径替换模式 (搜索 115)
                    if not pickcode and path_replace_mode:
                        path_map = app_cfg.get("path_map", "")
                        remote_path = file_path.replace("\\", "/")
                        if not remote_path.startswith("/"):
                            remote_path = "/" + remote_path

                        if path_map:
                            mapped_path = self._apply_path_mapping(remote_path, path_map)
                            if mapped_path: 
                                remote_path = mapped_path
                                logger.info(f"[115] 🔄 {drive_type} 路径映射成功: {file_path} => {remote_path}")
                        
                        logger.info(f"[{drive_type}-115] 🔍 准备在 115 中解析路径: {remote_path} (标题: {item_name}, 歌手: {artist}, 专辑: {album})")
                        pickcode = await self._resolve_pickcode_by_path(client, remote_path, title=item_name, artist=artist, album=album)
                    
                    if pickcode:
                        self._id_cache[item_id] = pickcode
                        logger.info(f"[{drive_type}-115] ✅ 最终获取到 Pickcode: {pickcode}")
                    else:
                        logger.warning(f"[{drive_type}-115] ❌ 无法获取 Pickcode (modes: {modes})")
                
                if not pickcode: return None
                
                cache_ua = user_agent if user_agent else "NoUA"
                cache_key = f"{pickcode}_{cache_ua}"
                if cache_key in self._url_cache:
                    logger.info(f"✅ [Cache-{drive_name}] 命中直链缓存: {log_name}")
                    return self._url_cache[cache_key]

                logger.info(f"[115-{drive_name}] 🔄 获取直链: {log_name}")
                final_url = await self._fetch_download_url_r302(
                    pickcode, user_agent, cookie=drive_cfg.get("cookie")
                )
                
                if final_url:
                    self._url_cache[cache_key] = final_url
                    logger.info(f"[115-{drive_name}] ✅ 直链获取成功: {log_name}")
                    return final_url
                else:
                    logger.warning(f"[115-{drive_name}] ⚠️ 直链获取失败: {log_name}")
                    return None
            except Exception as e:
                logger.error(f"❌ [Navi 115] 异常: {e}")
                traceback.print_exc()
                return None

    async def get_direct_url_by_pickcode(self, pickcode: str, user_agent: str, item_name: str = None, drive_index: int = 0):
        """[核心辅助] 通过 pickcode 获取播放直链（跳过解析）"""
        try:
            client, drive_cfg = await self.get_client(drive_index)
            if not client: return None
            
            drive_name = drive_cfg.get("name", f"drives[{drive_index}]")
            log_name = item_name if item_name else f"Pickcode: {pickcode}"
            
            cache_ua = user_agent if user_agent else "NoUA"
            cache_key = f"{pickcode}_{cache_ua}"
            
            # 1. 检查缓存
            if cache_key in self._url_cache:
                logger.info(f"✅ [Cache-{drive_name}] 命中直链缓存: {log_name}")
                return self._url_cache[cache_key]
            
            # 2. 获取直链
            logger.info(f"[115-{drive_name}] 🔄 通过 Pickcode 获取直链: {log_name}")
            final_url = await self._fetch_download_url_r302(
                pickcode, user_agent, cookie=drive_cfg.get("cookie")
            )
            
            if final_url:
                self._url_cache[cache_key] = final_url
                logger.info(f"[115-{drive_name}] ✅ 直链获取成功: {log_name}")
                return final_url
            else:
                logger.warning(f"[115-{drive_name}] ⚠️ 直链获取失败: {log_name}")
                return None
        except Exception as e:
            logger.error(f"❌ [PC-302] 异常: {e}")
            return None

    async def get_direct_url_by_path(self, drive_name: str, file_path: str, user_agent: str):
        """[直连入口] 通过网盘名称和路径直接获取直链"""
        cfg = await get_config_302()
        drives = cfg.get("drives", [])
        
        # 1. 寻找匹配名称的网盘
        drive_index = -1
        target_drive_cfg = None
        for i, d in enumerate(drives):
            if d.get("name") == drive_name:
                drive_index = i
                target_drive_cfg = d
                break
        
        if drive_index == -1:
            logger.warning(f"[115-Direct] 未找到名称为 '{drive_name}' 的网盘配置")
            return None

        # 2. 获取客户端
        client, _ = await self.get_client(drive_index)
        if not client: return None

        # 3. 解析路径并获取 Pickcode
        remote_path = file_path.replace("\\", "/")
        if not remote_path.startswith("/"):
            remote_path = "/" + remote_path
            
        logger.info(f"[115-Direct] 🔍 正在检索网盘 '{drive_name}' 中的路径: {remote_path}")
        
        # 检查解析缓存
        cache_key = f"path_pc_{drive_name}_{remote_path}"
        if cache_key in self._path_cache:
            pickcode = self._path_cache[cache_key]
        else:
            pickcode = await self._resolve_pickcode_by_path(client, remote_path)
            if pickcode:
                self._path_cache[cache_key] = pickcode

        if not pickcode:
            logger.warning(f"[115-Direct] ❌ 在网盘 '{drive_name}' 中未找到物理路径: {remote_path}")
            return None

        # 4. 调用通用的 pickcode 转直链逻辑
        return await self.get_direct_url_by_pickcode(
            pickcode, user_agent, item_name=os.path.basename(remote_path), drive_index=drive_index
        )

    def _is_file_busy(self, pickcode: str) -> bool:
        """检查缓存中是否有该 pickcode 的活跃记录"""
        prefix = f"{pickcode}_"
        for key in self._url_cache.keys():
            if key.startswith(prefix):
                return True
        return False

    async def _resolve_pickcode_flow(self, client, item_id, media_source_id, emby_index: int = 0):
        """解析 Pickcode 的完整流程"""
        cfg = await get_config_302()
        embys = cfg.get("embys", [])

        # 使用传入的 emby_index 获取正确的配置
        if isinstance(embys, list) and len(embys) > emby_index:
            emby_cfg = embys[emby_index]
        else:
            emby_cfg = next((e for e in embys if e.get('enabled')), embys[0]) if embys else {}

        # 获取模式配置
        modes = emby_cfg.get("modes", {})
        pickcode_mode = modes.get("pickcode", False) if isinstance(modes, dict) else False

        # 1. 尝试 Pickcode 模式（从 strm 文件提取 pickcode）
        if pickcode_mode:
            emby_name = emby_cfg.get("name", f"Emby[{emby_index}]")
            pickcode = await self._resolve_pickcode_from_strm(emby_cfg, item_id, media_source_id)
            if pickcode:
                self._id_cache[item_id] = pickcode
                logger.info(f"[115-{emby_name}] ✅ Pickcode 模式: 成功提取 pickcode={pickcode}")
                return pickcode
            else:
                logger.info(f"[115-{emby_name}] Pickcode 模式: 未找到有效的 pickcode URL，尝试其他方式")

        # 2. 原有的路径查找方式
        path_map = emby_cfg.get("path_map", "")

        # 获取 Emby 路径
        file_path = await self._get_emby_file_path(emby_cfg, item_id, media_source_id)
        if not file_path:
            logger.warning(f"[115] ❌ 无法获取 Emby 原始路径 (item_id={item_id})")
            return None
        
        logger.info(f"[115] ✅ 获得 Emby 原始路径: {file_path}")

        # 路径替换模式如果关闭，其实也可以不强求 mapping。增加对原生路径的基本替换
        remote_path = file_path.replace("\\", "/") # 兼容 Windows

        if modes.get("path_replace", True) and path_map:
            mapped_path = self._apply_path_mapping(remote_path, path_map)
            if mapped_path:
                logger.info(f"[115] 🔄 路径映射成功: {remote_path} => {mapped_path}")
                remote_path = mapped_path
            else:
                logger.info(f"[115] ⚠️ 路径映射规则未匹配: {remote_path}，将采用原始路径在网盘中搜索")

        logger.info(f"[115] 🔍 准备在 115 中搜索路径: {remote_path}")
        # 查找 Pickcode
        pickcode = await self._resolve_pickcode_by_path(client, remote_path)
        if pickcode:
            self._id_cache[item_id] = pickcode
            logger.info(f"[115] ✅ 通过路径获取到 Pickcode: {pickcode}")
        else:
            logger.warning(f"[115] ❌ 无法从 115 匹配该路径对应的文件，可能文件不存在或目录名不匹配: {remote_path}")

        return pickcode

    async def _sync_copy_and_get_link(self, client, config, src_pickcode, user_agent, emby_index=None):
        """[同步] 复制文件 -> 通过文件列表获取新 Pickcode -> 获取直链

        Args:
            emby_index: Emby 配置索引，用于确定使用哪个 115 账号的 cookie
        """
        target_dir = config.get('upload_dir', '/gatewayproxy')
        logger.info(f"[Sync] 📁 目标目录: {target_dir}")
        try:
            # 1. [修复] 使用 to_id 直接获取 src_file_id (需要文件头部 import to_id)
            src_file_id = to_id(src_pickcode)

            # 2. 获取目标目录 CID
            target_cid = None
            target_cid_info = client.fs_dir_getid(target_dir)
            logger.info(f"[Sync] 📂 fs_dir_getid({target_dir}) 返回: {target_cid_info}")

            if target_cid_info and target_cid_info.get('id'):
                target_cid = target_cid_info.get('id')
                logger.info(f"[Sync] ✅ 目标目录已存在，CID={target_cid}")
            else:
                logger.info(f"[Sync] 目标目录 {target_dir} 不存在，尝试创建...")
                
                # [修复] fs_mkdir 需要传入名称而不是路径
                # 假设目标是在根目录下，去掉开头的 "/"
                dir_name = target_dir.strip("/")
                
                # 如果配置的是多级路径 (e.g. /A/B)，简单处理取最后一级，或者默认建在根目录
                # 这里为了稳妥，直接在根目录创建该名称的文件夹
                if "/" in dir_name:
                    dir_name = dir_name.split("/")[-1]

                make_resp = client.fs_mkdir(dir_name)  # 默认 pid=0 (根目录)
                
                if make_resp and make_resp.get('state'):
                    # 尝试直接从创建结果中拿 ID
                    target_cid = make_resp.get('data', {}).get('id')
                    
                    # 如果没拿到，再查一次
                    if not target_cid:
                        target_cid_info = client.fs_dir_getid(target_dir)
                        if target_cid_info:
                            target_cid = target_cid_info.get('id')
                else:
                    logger.error(f"[Sync] 创建目录 {dir_name} 失败: {make_resp}")
                    return None

            if not target_cid: 
                logger.error(f"[Sync] 无法获取目标目录 CID: {target_dir}")
                return None

            # 3. 执行复制
            logger.info(f"[Sync] 📋 开始复制: src_id={src_file_id} -> target_cid={target_cid}")
            resp = client.fs_copy(src_file_id, target_cid)
            logger.info(f"[Sync] 📋 复制响应: {resp}")
            if not resp.get('state'):
                logger.error(f"[Sync] ❌ 复制失败: {resp}")
                return None

            # 4. 获取新文件信息 (参照 r302 逻辑)
            # 列出目标目录文件，按修改时间倒序排列 (o=user_ptime, asc=0)
            list_params = {
                "cid": target_cid,
                "o": "user_ptime",
                "asc": 0,
                "limit": 1
            }
            list_resp = client.fs_files(list_params)
            logger.info(f"[Sync] 📋 目标目录文件列表响应: state={list_resp.get('state')}, data_count={len(list_resp.get('data', []))}")
            
            if not list_resp.get('state') or not list_resp.get('data'):
                logger.error(f"[Sync] 复制后获取文件列表失败")
                return None

            # 取列表第一个（即最新的）文件
            new_file_data = list_resp['data'][0]
            new_pickcode = new_file_data.get('pc')
            new_file_id = new_file_data.get('fid')
            file_name = new_file_data.get('n', '')  # 获取文件名

            if not new_pickcode:
                return None

            # 6. 获取直链
            logger.info(f"[Sync] 副本就绪: {new_file_id} | pc: {new_pickcode} | 文件名: {file_name}")
            logger.info(f"[Sync] 📡 开始获取副本直链...")
            direct_url = await self._fetch_download_url_r302(new_pickcode, user_agent, emby_index=emby_index)

            if direct_url:
                url_preview = direct_url[:50] + "..." if len(direct_url) > 50 else direct_url
                logger.info(f"[Sync] ✅ 副本直链获取成功: {url_preview}")
                return {
                    "url": direct_url,
                    "file_id": new_file_id
                }
            else:
                logger.error(f"[Sync] ❌ 副本直链获取失败！")
            return None
        except Exception as e:
            logger.error(f"[Sync] 复制流程异常: {e}")
            traceback.print_exc()
            return None

    async def _delayed_remove(self, file_id, delay=60):
        """延迟删除副本"""
        logger.info(f"[Sync] ⏰ 延迟删除任务已启动，将在 {delay} 秒后清理副本: {file_id}")
        await asyncio.sleep(delay)
        logger.info(f"[Sync] 🗑️ 开始执行删除: {file_id}")
        try:
            client, _ = await self.get_client()
            if client:
                client.fs_delete(file_id)
                logger.info(f"[Sync] 🧹 副本已自动清理: {file_id}")
            else:
                logger.warning(f"[Sync] 删除失败: 无法获取 115 客户端")
        except Exception as e:
            logger.warning(f"[Sync] 副本清理失败: {e}")

    # ==========================================================
    # 🚀 115 秒传功能
    # ==========================================================

    async def _get_file_sha1_and_preupload_info(self, client, pickcode: str, user_agent: str = "", emby_index=None):
        """
        获取文件的 SHA1 信息和预上传所需的验证数据（并行优化）

        Args:
            emby_index: Emby 配置索引，用于确定使用哪个 115 账号的 cookie

        返回: {
            'sha1': '完整文件SHA1',
            'size': 文件大小,
            'direct_url': '大号直链'
        } 或 None
        """
        # 1. 检查缓存
        if pickcode in self._sha1_cache:
            cached = self._sha1_cache[pickcode]
            # 如果缓存中有直链但没有使用正确的 UA，需要重新获取
            if cached.get('direct_url'):
                return cached

        try:
            # 2. 先获取 SHA1 信息
            file_id = to_id(pickcode)
            attr = get_attr(client, file_id)
            if asyncio.iscoroutine(attr):
                attr = await attr
            if not attr or not attr.get('sha1'):
                logger.error(f"[Rapid] 无法获取文件 SHA1: {pickcode}")
                return None

            sha1 = attr['sha1'].upper()
            size = attr.get('size', 0)

            # 3. 获取大号直链
            direct_url = await self._fetch_download_url_r302(pickcode, user_agent, emby_index=emby_index)
            if not direct_url:
                logger.error(f"[Rapid] 无法获取大号直链: {pickcode}")
                return None

            result = {
                'sha1': sha1,
                'size': size,
                'direct_url': direct_url
            }

            # 缓存结果
            self._sha1_cache[pickcode] = result
            return result

        except Exception as e:
            logger.error(f"[Rapid] 获取 SHA1 信息异常: {e}")
            traceback.print_exc()
            return None

    async def _rapid_transfer_to_secondary(self, secondary_client, main_client, pickcode: str,
                                           sha1_info: dict, filename: str, target_dir: str, user_agent: str = ""):
        """
        执行秒传：用 SHA1 信息在小号创建文件引用

        Args:
            secondary_client: 小号 P115Client
            main_client: 大号 P115Client (用于下载验证数据)
            pickcode: 大号文件 pickcode
            sha1_info: SHA1 信息字典
            filename: 文件名
            target_dir: 目标目录

        返回: {
            'pickcode': '小号文件pickcode',
            'file_id': 小号文件ID
        } 或 None
        """
        try:
            # 1. 获取/创建目标目录
            target_cid_info = secondary_client.fs_dir_getid(target_dir)
            if not target_cid_info or not target_cid_info.get('id'):
                # 创建目录
                dir_name = target_dir.strip("/").split("/")[-1]
                make_resp = secondary_client.fs_mkdir(dir_name)
                if make_resp and make_resp.get('state'):
                    target_cid_info = secondary_client.fs_dir_getid(target_dir)

            if not target_cid_info or not target_cid_info.get('id'):
                logger.error(f"[Rapid] 无法获取目标目录: {target_dir}")
                return None

            target_cid = target_cid_info['id']

            # 2. 【优化】复用已获取的直链，避免重复请求（节省约 300ms）
            download_url = sha1_info.get('direct_url')
            if not download_url:
                logger.error(f"[Rapid] SHA1 信息中缺少直链")
                return None

            logger.info(f"[Rapid] 复用已获取的直链: {user_agent[:50] if user_agent else 'EMPTY'}...")

            # 3. 定义范围读取回调函数（用于二次验证）
            # 这个回调会在 status=7 时被调用，需要返回指定范围的 SHA1
            def read_range_callback(sign_check: str) -> str:
                """
                回调函数：接收范围字符串，返回该范围数据的 SHA1
                sign_check 格式: "0-131071" 或 "5110676549-5110864075"

                关键：使用与获取直链时完全相同的 User-Agent
                """
                try:
                    logger.info(f"[Rapid] 开始下载验证数据: 范围={sign_check}")

                    # 使用与获取直链时完全相同的 User-Agent
                    headers = {
                        "Range": f"bytes={sign_check}",
                        "User-Agent": user_agent or "Mozilla/5.0",
                    }

                    # 使用 httpx 发起请求
                    resp = httpx.get(
                        download_url,
                        headers=headers,
                        timeout=60.0,
                        verify=False,
                        follow_redirects=True
                    )

                    logger.info(f"[Rapid] HTTP响应状态: {resp.status_code}")

                    if resp.status_code in (200, 206):
                        import hashlib
                        data = resp.content
                        sha1_hash = hashlib.sha1(data).hexdigest().upper()
                        logger.info(f"[Rapid] 验证数据SHA1计算成功: {sha1_hash[:16]}... (数据长度: {len(data)}字节)")
                        return sha1_hash
                    else:
                        logger.error(f"[Rapid] HTTP请求失败: 状态码={resp.status_code}, 响应={resp.text[:200] if resp.text else 'N/A'}")
                        return ""

                except Exception as e:
                    logger.error(f"[Rapid] 范围读取异常: {e}")
                    traceback.print_exc()
                    return ""

            # 4. 使用 p115client 的 upload_file_init 方法
            # 这个方法会自动处理 status=7 的验证流程
            logger.info(f"[Rapid] 发起秒传请求: SHA1={sha1_info['sha1'][:16]}...")

            result = secondary_client.upload_file_init(
                filename=filename,
                filesize=sha1_info['size'],
                filesha1=sha1_info['sha1'],
                read_range_bytes_or_hash=read_range_callback,  # 传入范围读取回调
                pid=target_cid,
                async_=True
            )

            if asyncio.iscoroutine(result):
                result = await result

            if not result or not result.get('state'):
                logger.error(f"[Rapid] upload_file_init 失败: {result}")
                return None

            # 5. 检查秒传结果
            # result['reuse'] = True 表示秒传成功
            if result.get('reuse'):
                # 注意：pickcode 直接在 result 根级别，不在 data 字段中
                pickcode = result.get('pickcode')
                logger.info(f"[Rapid] ✅ 秒传成功: pickcode={pickcode}")

                # 重要：API 返回的 fileid=0 不是真实文件ID
                # 需要像同播复制一样，列出目标目录获取真实的 fid
                try:
                    list_params = {
                        "cid": target_cid,
                        "o": "user_ptime",  # 按修改时间倒序
                        "asc": 0,
                        "limit": 1
                    }
                    list_resp = secondary_client.fs_files(list_params)

                    if list_resp.get('state') and list_resp.get('data'):
                        new_file_data = list_resp['data'][0]
                        real_file_id = new_file_data.get('fid')  # 真正的文件ID
                        logger.info(f"[Rapid] 获取真实文件ID: {real_file_id}")
                    else:
                        real_file_id = None
                        logger.warning(f"[Rapid] 无法获取真实文件ID")
                except Exception as e:
                    logger.warning(f"[Rapid] 获取真实文件ID异常: {e}")
                    real_file_id = None

                return {
                    'pickcode': pickcode,
                    'file_id': real_file_id  # 使用真实的文件ID
                }
            else:
                # 秒传未命中，需要完整上传
                status = result.get('status', 0)
                logger.warning(f"[Rapid] 秒传未命中 (status={status})，需要完整上传")
                return None

        except Exception as e:
            logger.error(f"[Rapid] 秒传异常: {e}")
            traceback.print_exc()
            return None

    # 常见媒体文件扩展名白名单（支持音频和视频）
    _MEDIA_EXTENSIONS = {
        # 音频
        ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".wav",
        ".wma", ".opus", ".ape", ".dsf", ".dff", ".alac",
        ".aiff", ".aif", ".wv", ".tta", ".mpc",
        # 视频
        ".mp4", ".mkv", ".ts", ".avi", ".mov", ".wmv", ".flv", ".m2ts", ".iso", ".rmvb", ".strm"
    }

    def _is_media_file(self, filename: str) -> bool:
        """检查文件名是否为媒体文件，用于过滤 .json .nfo .jpg 等非媒体文件"""
        if not filename: return False
        ext = os.path.splitext(filename.lower())[1]
        return ext in self._MEDIA_EXTENSIONS

    def _is_strict_file_match(self, file_name, query):
        """精准文件名验证，防止短文件名误报"""
        if not file_name or not query: return False
        fn = file_name.casefold()
        q = query.casefold()
        if fn == q: return True
        
        # 扩展名检查
        if '.' in fn and '.' in q:
            fn_ext = fn.split('.')[-1]
            q_ext = q.split('.')[-1]
            if fn_ext == q_ext:
                # 核心名比对：只要互相包含核心部分即可（应对编号差异）
                fn_core = fn.rsplit('.', 1)[0]
                q_core = q.rsplit('.', 1)[0]
                if fn_core == q_core or q_core in fn_core or fn_core in q_core:
                    return True
        elif '.' not in q:
            # 如果查询没有后缀，则是模糊匹配
            if q in fn: return True
        return False

    async def _delayed_remove_secondary(self, secondary_client, file_id=None, pickcode=None, delay=60):
        """延迟删除小号上的副本

        Args:
            secondary_client: 小号客户端
            file_id: 文件ID（优先使用）
            pickcode: 文件pickcode（file_id 为 None 时使用）
            delay: 延迟秒数
        """
        await asyncio.sleep(delay)
        try:
            # 优先使用 file_id，其次使用 pickcode
            if file_id is not None:
                secondary_client.fs_delete(file_id)
                logger.info(f"[Rapid] 🧹 小号副本已清理: file_id={file_id}")
            elif pickcode:
                # p115client 的 fs_delete 也支持 pickcode
                from p115client import P115Client
                file_id_to_delete = P115Client.to_id(pickcode)
                secondary_client.fs_delete(file_id_to_delete)
                logger.info(f"[Rapid] 🧹 小号副本已清理: pickcode={pickcode}")
            else:
                logger.warning(f"[Rapid] 无法删除副本：缺少 file_id 和 pickcode")
        except Exception as e:
            logger.warning(f"[Rapid] 副本清理失败: {e}")

    async def _fetch_download_url_r302(self, pickcode, user_agent, cookie=None, emby_index=None):
        """调用 115 接口获取直链

        Args:
            pickcode: 文件 pickcode
            user_agent: 用户代理
            cookie: 可选的自定义 cookie（用于获取小号直链）
            emby_index: Emby 配置索引，用于确定使用哪个 115 账号的 cookie
        """
        api_url = "http://proapi.115.com/android/2.0/ufile/download"
        # 针对 115 API 加密参数
        json_payload = f'{{"pick_code":"{pickcode}"}}'
        encrypted_data = encrypt(json_payload.encode("utf-8")).decode("utf-8")

        # 使用传入的 cookie，如果没有则根据 emby_index 获取对应的 cookie
        if not cookie:
            if emby_index is not None:
                # 根据配置查找对应的 cookie
                cfg = await get_config_302()
                embys = cfg.get("embys", [])
                drives = cfg.get("drives", [])

                if isinstance(embys, list) and len(embys) > emby_index:
                    emby_cfg = embys[emby_index]
                    drive_index = emby_cfg.get("drive_index", -1)
                    if drive_index < 0:
                        drive_index = emby_index

                    if isinstance(drives, list) and len(drives) > drive_index:
                        cookie = drives[drive_index].get("cookie", "")

            # 如果还是没找到，使用第一个可用的 cookie（降级）
            if not cookie:
                cookie = next(iter(self._cookies.keys()), None) if self._cookies else None

        if not cookie:
            logger.error("[115] 没有可用的 cookie")
            return None

        headers = {
            "User-Agent": user_agent or "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": cookie
        }

        try:
            # [重要] 115 接口有时很不稳定，这里复用 client
            resp = await self._http_client.post(api_url, data={"data": encrypted_data}, headers=headers)
            if resp.status_code >= 400:
                logger.error(f"[115] 获取直链失败: HTTP {resp.status_code}")
                return None

            resp_json = resp.json()
            if not resp_json.get("state"):
                logger.error(f"[115] 获取直链失败: API state=False, response={resp_json}")
                return None

            decrypted_data = decrypt(resp_json["data"])
            data_obj = json.loads(decrypted_data)
            final_url = data_obj.get("url", {}).get("url") if isinstance(data_obj.get("url"), dict) else data_obj.get("url")

            if not final_url:
                logger.error(f"[115] 获取直链失败: URL 为空, data_obj={data_obj}")
                return None

            return final_url
        except Exception as e:
            logger.error(f"❌ [115] 获取链接异常: {e}")
            return None

    async def _resolve_pickcode_from_strm(self, emby_cfg, item_id, media_source_id):
        """
        Pickcode 模式：从 strm 文件内容提取 pickcode
        strm 文件格式示例：
        http://your-strm-host:3032/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=xxxxxxxxxxxxxxxxx
        或
        http://xxx.xxx/api/xxx?pickcode=xxxxx

        返回: pickcode 字符串 或 None
        """
        try:
            # 1. 获取 Emby 媒体源信息
            base_url = emby_cfg.get("url", "").rstrip("/")
            api_key = emby_cfg.get("key", "")
            if not base_url or not api_key:
                return None

            url = f"{base_url}/emby/Items/{item_id}/PlaybackInfo?api_key={api_key}"

            async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                resp = await client.post(url, json={"Profile": "Unknown"})
                if resp.status_code != 200:
                    return None

                data = resp.json()
                media_sources = data.get("MediaSources", [])

                # 优先使用指定的 media_source_id
                target_source = None
                if media_source_id:
                    for s in media_sources:
                        if s.get("Id") == media_source_id:
                            target_source = s
                            break
                if not target_source and media_sources:
                    target_source = media_sources[0]

                if not target_source:
                    return None

                # 2. 检查是否为 strm 文件或包含 pickcode 的 URL
                media_type = target_source.get("Container", "").lower()
                path = target_source.get("Path", "")

                logger.info(f"[115] Pickcode 模式检测: media_type={media_type}, path={path}")

                # 判断条件优化：
                # 如果开启了 Pickcode 模式，只要路径里看起来像有 Pickcode，就尝试提取
                is_strm_or_pc_url = (
                    media_type == "strm" or
                    path.endswith(".strm") or
                    "pickcode=" in path or
                    "/d/" in path  # 常见的 pickcode 直链代理路径
                )

                if not is_strm_or_pc_url:
                    logger.info(f"[115] ❌ 路径不符合 Pickcode 特征，跳过")
                    return None

                # 3. 尝试从 Path 中直接提取 pickcode (增强正则)
                # 方案 A: 匹配 pickcode=xxxxx
                # 方案 B: 匹配 /d/xxxxx.xxx (常见于各种 115 助手)
                pc_patterns = [
                    r"[?&]pickcode=([a-z0-9]{10,})",
                    r"/d/([a-z0-9]{10,})(\.[a-z0-9]+)?",
                ]

                for pattern in pc_patterns:
                    match = re.search(pattern, path, re.IGNORECASE)
                    if match:
                        extracted_pickcode = match.group(1)
                        if extracted_pickcode:
                            logger.info(f"[115] ✅ 成功从路径提取 Pickcode: {extracted_pickcode}")
                            return extracted_pickcode


                # 4. 否则，读取 strm 文件内容
                # strm 文件路径可能是本地路径或网络路径
                if path.startswith("http://") or path.startswith("https://"):
                    # 网络路径，直接获取内容
                    try:
                        strm_resp = await client.get(path, timeout=5.0)
                        if strm_resp.status_code == 200:
                            strm_content = strm_resp.text.strip()
                        else:
                            return None
                    except:
                        return None
                else:
                    # 本地路径，尝试通过 Emby API 读取文件内容
                    # 尝试多个可能的 API 端点
                    strm_content = None
                    api_endpoints = [
                        f"{base_url}/emby/Items/{item_id}/File?api_key={api_key}",
                        f"{base_url}/emby/Items/{item_id}/Download?api_key={api_key}",
                        f"{base_url}/Videos/{item_id}/stream?static=true&api_key={api_key}",
                    ]

                    for endpoint in api_endpoints:
                        try:
                            logger.info(f"[115] 尝试通过 Emby API 读取 strm 文件: {endpoint}")
                            file_resp = await client.get(endpoint, timeout=5.0)
                            logger.info(f"[115] API 响应状态: {file_resp.status_code}, Content-Type: {file_resp.headers.get('content-type', 'N/A')}")
                            if file_resp.status_code == 200:
                                strm_content = file_resp.text.strip()
                                logger.info(f"[115] ✅ 成功读取 strm 文件内容 (长度: {len(strm_content)}): {strm_content[:100]}")
                                break
                            else:
                                logger.info(f"[115] API 返回状态码: {file_resp.status_code}, 响应: {file_resp.text[:200]}")
                        except Exception as e:
                            logger.info(f"[115] API 请求异常: {e}")

                    if not strm_content:
                        logger.warning(f"[115] ❌ 所有 API 端点都无法读取 strm 文件内容")
                        return None

                # 4. 从 URL 中提取 pickcode
                # 支持多种 URL 格式：
                # - http://xxx/api/xxx?pickcode=xxxxx
                # - http://xxx/api/xxx&pickcode=xxxxx
                # - http://xxx/api/xxx/pickcode=xxxxx
                pickcode_patterns = [
                    r"[?&]pickcode=([^&\s]+)",  # ?pickcode=xxx 或 &pickcode=xxx
                    r"/pickcode/([^/\s]+)",      # /pickcode/xxx
                ]

                for pattern in pickcode_patterns:
                    match = re.search(pattern, strm_content)
                    if match:
                        extracted_pickcode = match.group(1)
                        # 验证 pickcode 格式（通常是字母和数字的组合，长度约 15-20）
                        if extracted_pickcode and len(extracted_pickcode) >= 10:
                            logger.info(f"[115] Pickcode 模式: 从 strm 文件提取 pickcode={extracted_pickcode}")
                            return extracted_pickcode

                logger.info(f"[115] Pickcode 模式: strm 文件内容未包含有效的 pickcode URL: {strm_content[:100]}")
                return None
        except Exception as e:
            logger.warning(f"[115] Pickcode 模式解析异常: {e}")
            return None

    def _normalize_name(self, name):
        """标准化名称：全小写，移除所有非中英文字符和数字，用于跨越括号/空格差异。"""
        if not name: return ""
        # 移除后缀
        core = name.rsplit('.', 1)[0] if '.' in name else name
        # 仅保留字母、数字、中文
        return re.sub(r'[^\w\u4e00-\u9fa5]', '', core).lower()

    async def _resolve_pickcode_by_path(self, client, path, title=None, artist=None, album=None):
        """通过‘闪电直达+原子穿透’双引擎，实现对齐 Emby 的稳健对齐"""
        if path in self._path_cache:
            return self._path_cache[path]

        try:
            path = path.replace("\\", "/").strip("/")
            dir_path = os.path.dirname(path)
            file_name = os.path.basename(path)
            
            # 1. 洁净名候选 (增强剥离逻辑)
            # 移除开头的轨道号: 01 - Title.flac -> Title.flac
            clean_name = re.sub(r'^(\d+[-_]\d+\s*[-._\s]\s*)+|^(\d+\s*[-._\s]\s*)+', '', file_name).strip()
            # 移除后缀名: Title.flac -> Title
            base_name = clean_name.rsplit('.', 1)[0] if '.' in clean_name else clean_name
            
            logger.info(f"[115] 🍭 目标路径: {path} | 搜索候选: {clean_name} (核心: {base_name})")

            # --- 第零阶段：全路径秒杀 (究极快) ---
            try:
                # 尝试直接通过全路径获取文件 ID 和 Pickcode
                file_info = client.fs_getid(path)
                if file_info and file_info.get("id") and str(file_info.get("id")) != "0":
                    # 注意：有些版本的 getid 接口会返回 pc 或 pick_code
                    item_pc = file_info.get("pc") or file_info.get("pick_code")
                    if item_pc:
                        logger.info(f"[115] 🚀 全路径秒杀命中: {path} -> PC:{item_pc}")
                        self._path_cache[path] = item_pc
                        return item_pc
                    else:
                        # 虽然命中了路径但没拿到 pickcode，我们可以记下它的 CID (如果是文件夹)
                        if file_info.get("is_dir") or not file_info.get("fid"):
                            self._cid_cache[path] = file_info.get("id")
            except:
                pass

            # --- 第一阶段：全路径闪电直达 (最快) ---
            target_cid = None
            if dir_path in self._cid_cache:
                target_cid = self._cid_cache[dir_path]
            else:
                try:
                    # 获取父目录 CID
                    dir_info = client.fs_dir_getid(dir_path)
                    if dir_info and dir_info.get("id"):
                        target_cid = dir_info.get("id")
                        logger.info(f"[115] ⚡ 全路径闪电命中: {dir_path} -> CID:{target_cid}")
                except: pass

            path_complete = True
            # --- 第二阶段：原子级路径穿透 (如果直达失败) ---
            if not target_cid:
                logger.info(f"[115] 🚀 直达未中，启动原子对齐模式...")
                parts = [p for p in dir_path.split("/") if p]
                curr_cid = 0
                r_p = ""
                for p in parts:
                    r_p = f"{r_p}/{p}".strip("/")
                    if r_p in self._cid_cache:
                        curr_cid = self._cid_cache[r_p]
                        continue
                    
                    found_cid = None
                    norm_p = self._normalize_name(p)
                    # 1. 列表比对
                    file_list = client.fs_files({"cid": curr_cid, "limit": 1000})
                    if not file_list.get("state"):
                        logger.warning(f"[115] ⚠️ 获取 CID:{curr_cid} 列表失败: {file_list.get('error')}")
                    
                    for item in file_list.get("data", []):
                        # 判断是否为文件夹: 没有 fid 或者 fid 为 0
                        is_folder = not item.get("fid") or str(item.get("fid")) == "0"
                        if is_folder:
                            name = item.get("n", "")
                            i_norm = self._normalize_name(name)
                            # 双向包含：Jay 匹配 Jay (2000)，或者反之
                            if name == p or norm_p == i_norm or (norm_p in i_norm) or (i_norm in norm_p):
                                found_cid = item.get("id") or item.get("cid")
                                break
                    
                    # 2. 局部搜索打捞 (仅当列表未找到时)
                    if not found_cid:
                        s_res = client.fs_search({"search_value": p, "cid": curr_cid, "limit": 10})
                        for item in s_res.get("data", []):
                            is_folder = not item.get("fid") or str(item.get("fid")) == "0"
                            if is_folder:
                                name = item.get("n", "")
                                i_norm = self._normalize_name(name)
                                if norm_p == i_norm or norm_p in i_norm or i_norm in norm_p:
                                    found_cid = item.get("id") or item.get("cid")
                                    break
                    
                    if found_cid:
                        curr_cid = found_cid
                        self._cid_cache[r_p] = curr_cid
                        logger.info(f"[115] 🧩 对齐阶段: '{p}' -> CID:{curr_cid}")
                    else:
                        logger.warning(f"[115] ⚠️ 路径在 '{p}' 处中断，将在 CID:{curr_cid} 进行深度打捞")
                        path_complete = False
                        break
                target_cid = curr_cid
            
            # --- 第三阶段：精准/模糊文件碰撞 ---
            if target_cid is not None:
                if path_complete:
                    self._cid_cache[dir_path] = target_cid
                pc = await self._find_file_in_cid(client, target_cid, clean_name, file_name, title, path)
                if pc: return pc
                
                # --- [新增] 父级跳跃打捞逻辑 ---
                # --- [优化] 父级跳跃打捞逻辑 (多维度碰撞) ---
                logger.info(f"[115] 🏮 深度探测未中，启动 CID:{target_cid} 递归打捞...")
                search_vals = [file_name, clean_name, base_name]
                if title: search_vals.insert(0, title)
                
                # 去重
                search_vals = list(dict.fromkeys([v for v in search_vals if v]))
                
                for s_val in search_vals:
                    if not s_val or len(s_val) < 2: continue
                    logger.debug(f"[115] 🔎 正在尝试搜索打捞: '{s_val}' (CID:{target_cid})")
                    s_res = client.fs_search({"search_value": s_val, "cid": target_cid, "limit": 20})
                    for item in s_res.get("data", []):
                        item_pc = item.get("pc") or item.get("pick_code")
                        if not item_pc: continue
                        
                        f_n = item.get("n", "")
                        # 1. 严格/包含碰撞
                        if self._is_strict_file_match(f_n, file_name) or self._is_strict_file_match(f_n, clean_name):
                            logger.info(f"[115] 🎯 递归打捞命中 (精准): '{f_n}'")
                            self._path_cache[path] = item_pc
                            return item_pc
                        
                        # 2. 脱水核心碰撞
                        i_norm = self._normalize_name(f_n)
                        c_norm = self._normalize_name(clean_name)
                        b_norm = self._normalize_name(base_name)
                        if c_norm in i_norm or i_norm in c_norm or b_norm in i_norm:
                            logger.info(f"[115] 🎯 递归打捞命中 (脱水): '{f_n}'")
                            self._path_cache[path] = item_pc
                            return item_pc

            # --- 第四阶段：元数据影子搜索 (Final Boss) ---
            if artist or album:
                logger.info(f"[115] 🏮 终极模式：按元数据检索库内相似项...")
                # 尝试 专辑、歌手 组合或单独搜索
                shadow_queries = []
                if artist and album: shadow_queries.append(f"{artist} {album}")
                if album: shadow_queries.append(album)
                
                for sq in shadow_queries:
                    if not sq: continue
                    search_res = client.fs_search({"search_value": sq, "limit": 10})
                    for item in search_res.get("data", []):
                        # 如果搜索到的是文件夹
                        is_folder = not item.get("fid") or str(item.get("fid")) == "0"
                        if is_folder:
                            found_cid = item.get("id") or item.get("cid")
                            logger.info(f"[115] 🔍 探知到影子目录: '{item.get('n')}' (CID:{found_cid})")
                            pc = await self._find_file_in_cid(client, found_cid, clean_name, file_name, title, path)
                            if pc:
                                self._cid_cache[dir_path] = found_cid
                                return pc

            # --- 第五阶段：全网盘深度打捞 (Final Frontier) ---
            logger.info(f"[115] 🌌 终极模式：启动全网盘深度打捞 (针对路径彻底对不上的情况)...")
            final_queries = []
            if title and artist: final_queries.append(f"{artist} {title}")
            final_queries.extend([file_name, clean_name, base_name])
            if title: final_queries.append(title)
            
            # 去重且保持顺序
            final_queries = list(dict.fromkeys([v for v in final_queries if v and len(v) > 1]))
            
            for gq in final_queries:
                logger.debug(f"[115] 🌍 全局搜索指令: '{gq}'")
                try:
                    # 不带 CID 限制的全局搜索
                    global_res = client.fs_search({"search_value": gq, "limit": 15})
                    if not global_res or not global_res.get("state"): continue
                    
                    for item in global_res.get("data", []):
                        item_pc = item.get("pc") or item.get("pick_code")
                        if not item_pc: continue
                        
                        item_name = item.get("n", "")
                        # 严格文件名比对 或 标题脱水比对
                        if self._is_strict_file_match(item_name, file_name) or self._is_strict_file_match(item_name, clean_name):
                            logger.info(f"[115] 🎯 全局打捞大获全胜: '{item_name}'")
                            self._path_cache[path] = item_pc
                            return item_pc
                except Exception as g_e:
                    logger.warning(f"[115] 全局搜索异常 ({gq}): {g_e}")

            logger.warning(f"[115] ❌ 路径对齐死档: {path}")
            return None
        except Exception as e:
            logger.error(f"❌ [115] 引擎崩溃: {e}")
            return None

    async def _find_file_in_cid(self, client, cid, clean_name, full_name, title, original_path):
        """三阶段碰撞引擎：1. 严格 -> 2. 核心包含 -> 3. 脱水打捞"""
        resp = client.fs_files({"cid": cid, "limit": 1000})
        items = resp.get("data", [])
        if not items: return None

        qs = [full_name, clean_name]
        if title: qs.append(title)

        # 只保留媒体文件，过滤掉 .json .nfo .jpg 等元数据文件
        media_items = [
            item for item in items
            if (item.get("pc") or item.get("pick_code")) and self._is_media_file(item.get("n", ""))
        ]

        if not media_items:
            logger.debug(f"[115] CID:{cid} 中没有可见媒体文件，跳过")
            return None
        
        # Round 1: 严格匹配
        for item in media_items:
            pc = item.get("pc") or item.get("pick_code")
            f_n = item.get("n")
            for q in qs:
                if self._is_strict_file_match(f_n, q):
                    logger.info(f"[115] ✅ 严格命中: '{f_n}'")
                    self._path_cache[original_path] = pc
                    return pc

        # Round 2: 脱水碰撞 (核心打捞)
        norm_qs = [self._normalize_name(q) for q in qs if q]
        for item in media_items:
            pc = item.get("pc") or item.get("pick_code")
            item_norm = self._normalize_name(item.get("n"))
            for nq in norm_qs:
                if nq and (item_norm == nq or nq in item_norm or item_norm in nq):
                    logger.info(f"[115] 🏮 脱水碰撞成功: '{item.get('n')}' (核心: '{nq}')")
                    self._path_cache[original_path] = pc
                    return pc
        return None

    async def _get_emby_file_path(self, emby_cfg, item_id, media_source_id):
        if item_id in self._emby_path_cache:
            return self._emby_path_cache[item_id]

        base_url = emby_cfg.get("url", "").rstrip("/")
        api_key = emby_cfg.get("key", "")
        if not base_url or not api_key: return None
        
        url = f"{base_url}/emby/Items/{item_id}/PlaybackInfo?api_key={api_key}"
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                resp = await client.post(url, json={"Profile": "Unknown"})
                if resp.status_code != 200:
                    logger.warning(f"[Emby] 获取 PlaybackInfo 失败，状态码: {resp.status_code}")
                    return None
                 
                data = resp.json()
                media_sources = data.get("MediaSources", [])
                target_source = None
                if media_source_id:
                    for s in media_sources:
                        if s.get("Id") == media_source_id: target_source = s; break
                if not target_source and media_sources: target_source = media_sources[0]
                
                if target_source: 
                    path = target_source.get("Path")
                    logger.info(f"[Emby API] 获取到原文件 Path: {path}")
                    if path: self._emby_path_cache[item_id] = path
                    return path
                else:
                    logger.warning(f"[Emby API] PlaybackInfo 中未找到有效的 MediaSource")
        except Exception as e: 
            logger.error(f"[Emby API] 获取 Emby 路径异常: {e}")
        return None

    def _apply_path_mapping(self, file_path, mapping_text):
        if not mapping_text or not file_path: return None
        lines = mapping_text.split('\n')
        valid_rules = [line for line in lines if "=>" in line]
        valid_rules.sort(key=lambda x: len(x.split("=>")[0]), reverse=True)
        for line in valid_rules:
            local_prefix, remote_prefix = line.split("=>")
            local_prefix = local_prefix.strip()
            remote_prefix = remote_prefix.strip()
            if file_path.startswith(local_prefix):
                relative_path = file_path[len(local_prefix):]
                if remote_prefix.endswith("/") and relative_path.startswith("/"):
                    relative_path = relative_path[1:]
                elif not remote_prefix.endswith("/") and not relative_path.startswith("/"):
                    relative_path = "/" + relative_path
                return remote_prefix + relative_path
        return None

    async def execute_cleanup_task(self, drive_config: dict, account_type: str = "main", account_index: int = 0):
        """执行单个 115 账号的清理任务

        Args:
            drive_config: 驱动配置
            account_type: 账号类型 ("main" 主号 或 "rapid" 小号)
            account_index: 账号索引（用于小号池）
        """
        if account_type == "main":
            name = drive_config.get('name', '主号')
            cookie = drive_config.get('cookie')
            upload_dir = drive_config.get('upload_dir', '/gatewayproxy')
            recycle_code = drive_config.get('recycle_code', '')
        else:
            # 小号配置
            rapid_accounts = drive_config.get('rapid_accounts', [])
            if account_index >= len(rapid_accounts):
                return
            account = rapid_accounts[account_index]
            name = account.get('name', f'小号{account_index + 1}')
            cookie = account.get('cookie', '')
            # 小号使用独立配置，如果没有则使用主号配置作为默认值
            upload_dir = account.get('upload_dir', drive_config.get('upload_dir', '/gatewayproxy'))
            recycle_code = account.get('recycle_code', drive_config.get('recycle_code', ''))

        if not cookie: return

        try:
            logger.info(f"[CleanUp] ♻️ 开始清理账号: {name} (类型: {account_type})")
            client = P115Client(cookie)
            target_cid = client.fs_dir_getid(upload_dir).get('id')
            if not target_cid:
                logger.warning(f"[CleanUp] 目录不存在: {upload_dir}")
                return

            deleted_count = 0
            while True:
                resp = client.fs_files({'cid': target_cid, 'limit': 1000})
                if not resp.get('data'): break
                file_list = resp['data']
                if not file_list: break
                fids = [item['fid'] for item in file_list]
                client.fs_delete(fids)
                deleted_count += len(fids)
                logger.info(f"[CleanUp] 已删除 {len(fids)} 个文件...")
                await asyncio.sleep(0.5)

            if deleted_count > 0:
                logger.info(f"[CleanUp] ✅ 目录清理完成: 共删除 {deleted_count} 个文件")
            else:
                logger.info(f"[CleanUp] 目录为空")

            # 清空回收站
            try:
                headers = {
                    "Cookie": cookie,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": "https://115.com",
                    "Referer": "https://115.com/"
                }
                data = {"password": recycle_code} if recycle_code else {}
                async with httpx.AsyncClient(timeout=10.0) as http_client:
                    resp = await http_client.post("https://webapi.115.com/rb/clean", data=data, headers=headers)
                    if resp.json().get("state"):
                        logger.info(f"[CleanUp] 🗑️ 回收站已清空")
                    else:
                        logger.warning(f"[CleanUp] ⚠️ 强制清空失败: {resp.json().get('error')}")
            except Exception as ex_raw:
                 logger.warning(f"[CleanUp] ⚠️ 强制清空异常: {ex_raw}")

        except Exception as e:
            logger.error(f"[CleanUp] ❌ 任务执行异常: {e}")

    async def close(self):
        if self._http_client:
            await self._http_client.aclose()
            logger.info("[115Service] HTTP 客户端已关闭")

drive115_service = Drive115Service()