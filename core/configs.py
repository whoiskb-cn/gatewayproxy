# core/configs.py
import os
import json

# 获取项目根目录 (假设 core/configs.py 在 project/core/ 下，往上两层就是根目录)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 定义关键目录
CONFIG_DIR = os.path.join(BASE_DIR, "config")
DEFAULTS_DIR = os.path.join(BASE_DIR, "defaults")
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
LAYOUTS_DIR = os.path.join(BASE_DIR, "layouts")
BACKUPS_DIR = os.path.join(BASE_DIR, "backups")

# 定义关键文件路径
APP_LOG_FILE = os.path.join(CONFIG_DIR, "app.log")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")
CONFIG_302_FILE = os.path.join(CONFIG_DIR, "config_302.json")
AUTH_FILE = os.path.join(CONFIG_DIR, "auth.json")
TRANSLATIONS_FILE = os.path.join(CONFIG_DIR, "translations.json")
TASKS_FILE = os.path.join(CONFIG_DIR, "tasks.json")
LICENSE_FILE = os.path.join(CONFIG_DIR, "license.json")
RSS_TASKS_FILE = os.path.join(CONFIG_DIR, "rss_tasks.json")
RSS_CONFIG_FILE = os.path.join(CONFIG_DIR, "rss_settings.json")
WEBHOOK_CONFIG_FILE = os.path.join(CONFIG_DIR, "webhook.json")
DEVICE_ID_FILE = os.path.join(CONFIG_DIR, "device_id.txt")

# 确保必要的目录存在
def ensure_directories():
    for d in [CONFIG_DIR, FONTS_DIR, TEMPLATES_DIR, LAYOUTS_DIR, BACKUPS_DIR]:
        if not os.path.exists(d):
            try:
                os.makedirs(d)
            except:
                pass

ensure_directories()

# 初始化默认配置文件
def initialize_configs():
    default_302_config = {
        "drives": [
            {
                "name": "115网盘_示例",
                "cookie": "",
                "show_cookie": False,
                "cache_time": 600,
                "enable_sync": False,
                "enable_rapid": False,
                "auto_delete": True,
                "delete_cron": "30 3 * * *",
                "recycle_code": "",
                "upload_dir": "/gatewayproxy",
                "rapid_mode": "auto",
                "rapid_accounts": []
            }
        ],
        "embys": [],
        "navidromes": [],
        "tingreaders": [],
        "admin_username": "admin",
        "admin_password": "admin123"
    }
    
    if not os.path.exists(CONFIG_302_FILE):
        try:
            with open(CONFIG_302_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_302_config, f, ensure_ascii=False, indent=4)
            print(f"已创建默认配置文件: {CONFIG_302_FILE}")
        except Exception as e:
            print(f"创建默认配置文件失败: {e}")

initialize_configs()

# === [新增] 全局配置类，修复 import 错误 ===
class GlobalConfig:
    def __init__(self):
        self.proxy_url = None
        self.load()

    def load(self):
        """尝试从 settings.json 加载代理配置"""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 假设 settings.json 里有一个 proxy_url 字段
                    self.proxy_url = data.get("proxy_url")
            except Exception:
                pass

# 实例化对象，供其他模块 import
global_config = GlobalConfig()