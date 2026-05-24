import uvicorn
import os
import asyncio
import json
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager

from app.routers import config_302
from app.routers import gateway

from app.routers.gateway import proxy_client
from app.services.drive115_service import drive115_service
from app.services.task_service import task_service_instance

from core.logger import logger
from core.configs import CONFIG_302_FILE
import logging

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# --- 屏蔽 Uvicorn 默认访问日志 ---
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
# -----------------------------------

@asynccontextmanager
async def lifespan_ui(app: FastAPI):
    logger.info("[System] 正在初始化系统组件...")
    
    task_service_instance.scheduler.start()
    logger.info("[System] ✅ 定时任务调度器已启动")

    task_service_instance.refresh_cleanup_jobs()
    logger.info("[System] ✅ 自动清理任务已加载")
    
    yield
    task_service_instance.scheduler.shutdown()
    logger.info("[System] 系统已关闭")

app = FastAPI(title="GatewayProxy UI", version="1.0.0", lifespan=lifespan_ui)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(config_302.router)

from app.routers import cookie_115
app.include_router(cookie_115.router)

from app.routers import music
app.include_router(music.router)

from core.wechat_music import router as wechat_music_router
app.include_router(wechat_music_router)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html", status_code=302)


@asynccontextmanager
async def lifespan_gateway(app: FastAPI):
    logger.info("[Gateway] 网关服务启动")
    yield
    logger.info("[Gateway] 正在关闭连接池...")
    
    await proxy_client.aclose()
    await drive115_service.close()
    
    logger.info("[Gateway] 网关服务已关闭")

proxy_app = FastAPI(title="GatewayProxy Gateway", lifespan=lifespan_gateway)

proxy_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

proxy_app.include_router(gateway.router)

async def serve_apps():
    gateway_ports = []
    port_to_emby_map = {}
    port_to_navidrome_map = {}
    port_to_tingreader_map = {}
    port_to_feiniu_map = {}
    config_path = CONFIG_302_FILE

    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                embys = data.get("embys", [])
                if embys:
                    for idx, emby in enumerate(embys):
                        if emby.get("enabled", True):
                            cfg_port = emby.get("proxy_port")
                            if cfg_port:
                                port = int(cfg_port)
                                gateway_ports.append(port)
                                port_to_emby_map[port] = idx
                else:
                    cfg_port = data.get("emby", {}).get("proxy_port")
                    if cfg_port:
                        port = int(cfg_port)
                        gateway_ports.append(port)
                        port_to_emby_map[port] = 0

                navidromes = data.get("navidromes", [])
                for idx, nav in enumerate(navidromes):
                    if nav.get("enabled", True):
                        cfg_port = nav.get("proxy_port")
                        if cfg_port:
                            port = int(cfg_port)
                            gateway_ports.append(port)
                            port_to_navidrome_map[port] = idx

                tingreaders = data.get("tingreaders", [])
                for idx, abs_cfg in enumerate(tingreaders):
                    if abs_cfg.get("enabled", True):
                        cfg_port = abs_cfg.get("proxy_port")
                        if cfg_port:
                            port = int(cfg_port)
                            gateway_ports.append(port)
                            port_to_tingreader_map[port] = idx

                feinius = data.get("feinius", [])
                for idx, fn_cfg in enumerate(feinius):
                    if fn_cfg.get("enabled", True):
                        cfg_port = fn_cfg.get("proxy_port")
                        if cfg_port:
                            port = int(cfg_port)
                            gateway_ports.append(port)
                            port_to_feiniu_map[port] = idx
        except Exception as e:
            logger.error(f"[Boot] 读取网关端口失败，将使用默认 8116: {e}")

    if 8116 not in gateway_ports:
        gateway_ports.append(8116)

    logger.info(f">>> [BOOT] UI 服务启动端口: 8115")
    logger.info(f">>> [BOOT] 网关 服务启动端口: {gateway_ports}")

    config_ui = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8115,
        log_level="warning"
    )

    server_ui = uvicorn.Server(config_ui)

    from app.routers.gateway import register_gateway_port
    for port, emby_idx in port_to_emby_map.items():
        register_gateway_port(port, emby_idx)
        logger.info(f">>> [BOOT] 注册端口映射: {port} -> Emby[{emby_idx}]")

    from app.routers.navidrome_helper import register_navidrome_port
    for port, nav_idx in port_to_navidrome_map.items():
        register_navidrome_port(port, nav_idx)
        logger.info(f">>> [BOOT] 注册端口映射: {port} -> Navidrome[{nav_idx}]")

    from app.routers.tingreader_helper import register_tingreader_port
    for port, tr_idx in port_to_tingreader_map.items():
        register_tingreader_port(port, tr_idx)
        logger.info(f">>> [BOOT] 注册端口映射: {port} -> TingReader[{tr_idx}]")

    from app.routers.feiniu_helper import register_feiniu_port
    for port, fn_idx in port_to_feiniu_map.items():
        register_feiniu_port(port, fn_idx)
        logger.info(f">>> [BOOT] 注册端口映射: {port} -> Feiniu[{fn_idx}]")

    gateway_servers = []
    for port in gateway_ports:
        config_gw = uvicorn.Config(
            proxy_app,
            host="0.0.0.0",
            port=port,
            log_level="warning"
        )
        gateway_servers.append(uvicorn.Server(config_gw))

    try:
        servers = [server_ui.serve()] + [gs.serve() for gs in gateway_servers]
        await asyncio.gather(*servers)
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    if not os.path.exists("config"):
        os.makedirs("config")
        logger.info("创建 config 目录")
    
    logger.info(f">>> [BOOT] 程序所在根目录: {os.getcwd()}")
    logger.info(">>> 正在启动双端口服务...")
    logger.info("    - 访问管理后台: http://localhost:8115/static/index.html")

    try:
        asyncio.run(serve_apps())
    except KeyboardInterrupt:
        logger.warning(">>> 服务已停止 (User Stopped)")
    except Exception as e:
        logger.error(f"非正常退出: {e}")
