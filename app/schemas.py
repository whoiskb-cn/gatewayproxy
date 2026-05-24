# app/schemas.py
from pydantic import BaseModel
from typing import List, Optional

class Drive115Config(BaseModel):
    name: str = '115'
    cookie: str = ''
    show_cookie: bool = False
    cache_time: int = 600
    enable_sync: bool = False
    enable_rapid: bool = False
    auto_delete: bool = True
    delete_cron: str = '30 3 * * *'
    recycle_code: str = ''
    upload_dir: str = '/gatewayproxy'

    rapid_mode: str = 'auto'
    rapid_accounts: list = []

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
    drive_index: int = -1

    class Config:
        extra = "ignore"

class Config302Payload(BaseModel):
    drives: List[Drive115Config] = []
    embys: List[Emby302Config] = []

class Test115Payload(BaseModel):
    cookie: str

class ManualCleanupPayload(BaseModel):
    drive_index: int = 0
    account_type: str = "main"
    account_index: int = 0
