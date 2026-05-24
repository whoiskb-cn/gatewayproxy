import os
import json
from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel
from typing import Optional, Dict, List
from p115client import P115Client 

# [新增] 引入日志模块，用于打印配置状态
from core.logger import logger

# ❌ [重要] 绝对不要在这里导入 task_service，否则会报错 ImportError (循环引用)

router = APIRouter(prefix="/api/config_302", tags=["config_302"])

# 配置文件保存路径
from core.configs import CONFIG_302_FILE
CONFIG_FILE = CONFIG_302_FILE

# ==========================================
# 1. 定义数据模型 (顺序很重要)
# ==========================================

# [第一步] 先定义基础配置类
class Drive115Config(BaseModel):
    name: str = '115'
    cookie: str = ''  # 大号 cookie（存储资源）
    show_cookie: bool = False # 前端辅助字段
    cache_time: int = 600
    enable_sync: bool = False # 同播复制开关
    enable_rapid: bool = False # 秒传开关
    auto_delete: bool = True
    delete_cron: str = '30 3 * * *'
    recycle_code: str = ''
    upload_dir: str = '/gatewayproxy'

    # 秒传小号池配置（适配前端的多账号池设计）
    rapid_mode: str = 'auto'  # 调度策略: auto 自动轮询, 或指定账号索引
    rapid_accounts: list = []  # 小号池: [{"name": "小号1", "cookie": "xxx", "recycle_code": "", "upload_dir": "/gatewayproxy"}]

    # 允许前端发送额外的字段，防止 422 错误
    class Config:
        extra = "ignore"

class Emby302Modes(BaseModel):
    share: bool = False
    path_replace: bool = True
    pickcode: bool = False

class Emby302Preload(BaseModel):
    enabled: bool = False
    count: int = 0
    user: str = 'all'

class Emby302Config(BaseModel):
    name: str = 'Emby'
    url: str = ''
    key: str = ''
    proxy_port: str = '8098'
    modes: Emby302Modes = Emby302Modes()
    preload: Emby302Preload = Emby302Preload()
    rapid_play: bool = False
    path_map: str = ''
    enabled: bool = True
    drive_index: int = -1  # 关联的 115 账号索引，-1 表示使用默认

    # 允许前端发送额外的字段
    class Config:
        extra = "ignore"

class NavidromeConfig(BaseModel):
    name: str = 'Navidrome'
    url: str = ''
    proxy_port: str = '4533'
    path_map: str = ''
    enabled: bool = True
    drive_index: int = 0
    username: str = ''
    password: str = ''

    class Config:
        extra = "ignore"

class TingReaderConfig(BaseModel):
    name: str = 'TingReader'
    url: str = ''
    proxy_port: str = '9527'
    path_map: str = ''
    enabled: bool = True
    drive_index: int = 0
    username: str = ''
    password: str = ''
    api_key: str = ''

    class Config:
        extra = "ignore"

class FeiniuModes(BaseModel):
    share: bool = False
    path_replace: bool = True
    pickcode: bool = False

class FeiniuConfig(BaseModel):
    name: str = 'Feiniu'
    url: str = ''
    proxy_port: str = '8097'
    modes: FeiniuModes = FeiniuModes()
    path_map: str = ''
    enabled: bool = True
    drive_index: int = 0
    username: str = ''
    password: str = ''

    class Config:
        extra = "ignore"

class WeComMusicConfig(BaseModel):
    enabled: bool = False
    corp_id: str = ''
    secret: str = ''
    agent_id: str = ''
    token: str = ''
    encoding_aes_key: str = ''
    proxy: str = ''

    class Config:
        extra = "ignore"

class Config302Payload(BaseModel):
    drives: List[Drive115Config] = [] 
    embys: List[Emby302Config] = []    
    navidromes: List[NavidromeConfig] = []
    tingreaders: List[TingReaderConfig] = []
    feinius: List[FeiniuConfig] = []
    wecom_music: WeComMusicConfig = WeComMusicConfig()
    admin_username: str = "admin"
    admin_password: str = "admin123"

    class Config:
        extra = "ignore"

class Test115Payload(BaseModel):
    cookie: str

class LoginPayload(BaseModel):
    username: str
    password: str

# 安全验证拦截器
def verify_token(authorization: str = Header(None)):
    if not authorization or authorization != "Bearer nex_gateway_secure_2026":
        raise HTTPException(status_code=401, detail="Unauthorized")

# ==========================================
# 2. 路由逻辑
# ==========================================

@router.post("/login")
async def login_admin(payload: LoginPayload):
    saved_user = "admin"
    saved_pw = "admin123"
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                saved_user = data.get("admin_username", "admin")
                saved_pw = data.get("admin_password", "admin123")
        except:
            pass
    
    if payload.username == saved_user and payload.password == saved_pw:
        return {"status": "ok", "token": "nex_gateway_secure_2026"}
    return {"status": "error"}

@router.get("/get", dependencies=[Depends(verify_token)])
async def get_config_302():
    """读取 302 配置"""
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data
    except Exception as e:
        logger.error(f"读取 302 配置失败: {e}")
        return {}

@router.post("/save", dependencies=[Depends(verify_token)])
async def save_config_302(config: Config302Payload):
    """保存 302 配置"""
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        # 先读取已有配置，避免覆盖掉不在当前 payload 中的额外字段
        existing_data = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
            except Exception:
                existing_data = {}

        save_data = config.dict()
        merged_data = existing_data.copy()
        for key, value in save_data.items():
            if isinstance(value, dict) and isinstance(merged_data.get(key), dict):
                merged_data[key] = {**merged_data.get(key, {}), **value}
            else:
                merged_data[key] = value

        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(merged_data, f, ensure_ascii=False, indent=4)

        # ========================================================
        # 🆕 [新增] 打印详细的功能状态日志 (让你直观看到开关状态)
        # ========================================================
        logger.info(f"============ 302 配置已更新 ============")
        for idx, drive in enumerate(config.drives):
            # 获取开关状态文本
            sync_status = "✅ 开启" if drive.enable_sync else "⭕ 关闭"
            rapid_status = "✅ 开启" if drive.enable_rapid else "⭕ 关闭"
            
            logger.info(f"[账号: {drive.name}] 同播复制: {sync_status} | 秒传模式: {rapid_status}")
            
            if drive.enable_sync:
                logger.info(f"    └─ ⚡ 同播复制策略已生效: 多人观看同一视频时自动生成副本")
                
        logger.info(f"=======================================")

        # ✅ [关键修改] 在函数内部导入，防止循环引用
        # 保存配置后，通知 task_service 刷新清理任务
        try:
            from app.services.task_service import task_service_instance
            task_service_instance.refresh_cleanup_jobs()
        except Exception as e:
            logger.error(f"刷新清理任务失败: {e}")
            
        return {"status": "success", "message": "配置已保存"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")

@router.post("/test_115", dependencies=[Depends(verify_token)])
async def test_115_cookie(payload: Test115Payload):
    """测试 115 Cookie 有效性"""
    if not payload.cookie:
        return {"status": "error", "message": "Cookie 为空"}

    try:
        # 尝试初始化
        client = P115Client(payload.cookie)

        # [修复] 原来的 member_info() 方法不存在
        # 改用 fs_files({'cid': 0}) 获取根目录文件列表
        # 这是一个核心 API，如果能成功返回数据，说明连接和权限都正常
        resp = client.fs_files({'cid': 0, 'limit': 1})

        # 检查返回状态
        if resp and resp.get("state"):
            return {
                "status": "ok",
                "message": "连接成功! Cookie 有效"
            }
        else:
            # 尝试读取错误信息
            err_msg = resp.get("error") if resp else "未知错误"
            return {"status": "error", "message": f"Cookie 无效或已过期 ({err_msg})"}

    except Exception as e:
        return {"status": "error", "message": f"连接异常: {str(e)}"}





class ManualCleanupPayload(BaseModel):
    drive_index: int = 0
    account_type: str = "main"  # "main" 或 "rapid"
    account_index: int = 0


@router.post("/manual_cleanup", dependencies=[Depends(verify_token)])
async def manual_cleanup(payload: ManualCleanupPayload):
    """手动触发 115 清理任务（删除目录 + 清空回收站）"""
    from app.services.drive115_service import drive115_service

    # 读取配置
    config_path = CONFIG_FILE
    if not os.path.exists(config_path):
        return {"status": "error", "message": "配置文件不存在"}

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        drives = data.get("drives", [])
        if not drives:
            return {"status": "error", "message": "没有配置 115 账号"}

        if payload.drive_index >= len(drives):
            return {"status": "error", "message": "账号索引超出范围"}

        drive_config = drives[payload.drive_index]

        # 执行清理
        await drive115_service.execute_cleanup_task(
            drive_config,
            payload.account_type,
            payload.account_index
        )

        account_name = ""
        if payload.account_type == "main":
            account_name = drive_config.get("name", f"主号{payload.drive_index + 1}")
        else:
            rapid_accounts = drive_config.get("rapid_accounts", [])
            if payload.account_index < len(rapid_accounts):
                account_name = rapid_accounts[payload.account_index].get("name", f"小号{payload.account_index + 1}")

        return {"status": "ok", "message": f"清理完成: {account_name}"}

    except Exception as e:
        return {"status": "error", "message": f"清理失败: {str(e)}"}

@router.post("/restart", dependencies=[Depends(verify_token)])
async def restart_container():
    """重启容器 (通过退出进程触发 Docker 重启策略)"""
    logger.info("🚀 收到重启容器指令，应用即将以非正常状态码退出以触发 Docker 重启策略...")
    
    import asyncio
    import os
    
    async def delayed_exit():
        # 给 1 秒时间让 FastAPI 把响应发出去
        await asyncio.sleep(1)
        # 用状态码 1 退出，NAS 上的 Docker 会认为程序崩溃从而强制重启
        os._exit(1)
    
    asyncio.create_task(delayed_exit())
    
    return {"status": "success", "message": "应用正在强制退出触发重启，请等待 10 秒左右手动刷新页面"}


@router.get("/logs", dependencies=[Depends(verify_token)])
async def get_logs(lines: int = 200):
    """读取最近 N 行当前应用日志（仅限本应用 app.log，不包含 Docker 容器/服务的完整 Docker logs）"""
    from core.configs import APP_LOG_FILE
    import collections

    if not os.path.exists(APP_LOG_FILE):
        return {"lines": [], "total": 0, "next_pos": 0}

    try:
        # 高效读取尾部 N 行（大文件友好）
        with open(APP_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            tail = collections.deque(f, maxlen=lines)
        log_lines = [l.rstrip("\n\r") for l in tail if l.strip()]
        next_pos = os.path.getsize(APP_LOG_FILE)
        return {"lines": log_lines, "total": len(log_lines), "next_pos": next_pos}
    except Exception as e:
        return {"lines": [f"读取日志失败: {e}"], "total": 1, "next_pos": 0}


@router.get("/logs/stream")
async def stream_logs(since_pos: int = 0):
    """
    增量拉取当前应用日志（轮询模式）。
    仅返回本应用的 app.log 新增行，不包含 Docker 容器/服务的完整 Docker logs。
    """
    from core.configs import APP_LOG_FILE
    from fastapi.responses import JSONResponse

    if not os.path.exists(APP_LOG_FILE):
        return JSONResponse({"lines": [], "next_pos": since_pos})

    try:
        file_size = os.path.getsize(APP_LOG_FILE)
        if since_pos > file_size:
            since_pos = 0

        with open(APP_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            f.seek(since_pos)
            new_lines = [l.rstrip("\n\r") for l in f if l.strip()]
            next_pos = f.tell()

        return JSONResponse({
            "lines": new_lines,
            "next_pos": next_pos,
        })
    except Exception as e:
        return JSONResponse({"lines": [f"读取失败: {e}"], "next_pos": since_pos})