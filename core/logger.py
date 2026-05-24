import sys
import os
import logging
# 假设你的 core/configs.py 中有这两个变量，保留原导入
from .configs import APP_LOG_FILE, CONFIG_DIR

class LoggerWriter:
    def __init__(self, writer):
        self.writer = writer
        self.log_file = None
        self._open_file()

    def _open_file(self):
        try:
            if not os.path.exists(CONFIG_DIR):
                os.makedirs(CONFIG_DIR)
            # buffering=1 使用行缓冲，确保写入及时
            self.log_file = open(APP_LOG_FILE, "a", encoding="utf-8", buffering=1)
        except:
            pass

    def write(self, message):
        # 过滤掉频繁的心跳日志，防止 Web 端日志刷屏
        if "GET /api/progress" in message or "GET /api/system_logs" in message:
            return
        
        # 1. 写入原始控制台 (Docker/后台可见)
        if self.writer:
            try:
                self.writer.write(message)
                self.writer.flush()
            except:
                pass
            
        # 2. 写入日志文件 (Web 端可见)
        if self.log_file:
            try:
                self.log_file.write(message)
                self.log_file.flush()
            except:
                pass

    def flush(self):
        if self.writer:
            try: self.writer.flush()
            except: pass
        if self.log_file:
            try: self.log_file.flush()
            except: pass

    def isatty(self):
        return getattr(self.writer, 'isatty', lambda: False)()

def setup_logging():
    # ★★★ 核心修复：立即劫持标准输出 ★★★
    # 只有当 stdout 不是 LoggerWriter 时才劫持，防止重复劫持
    if not isinstance(sys.stdout, LoggerWriter):
        sys.stdout = LoggerWriter(sys.stdout)
        sys.stderr = LoggerWriter(sys.stderr)

    # 修复 'Logger' object has no attribute 'trace'
    TRACE_LEVEL_NUM = 5 
    logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")
    def trace(self, message, *args, **kws):
        if self.isEnabledFor(TRACE_LEVEL_NUM):
            self._log(TRACE_LEVEL_NUM, message, args, **kws)
    
    # 防止重复添加方法
    if not hasattr(logging.Logger, "trace"):
        logging.Logger.trace = trace

    # 配置 logger，强制使用 sys.stdout (已被劫持)
    # 这会配置 root logger，使得所有 logger.info() 都输出到 LoggerWriter
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        stream=sys.stdout, # 明确指定流，此时 sys.stdout 已经是 LoggerWriter 了
        force=True         # 强制覆盖之前的配置
    )
    logging.info(">>> [System] 日志系统初始化完成")

# =========================================================
#  新增部分：执行初始化 并 导出 main.py 需要的 logger 对象
# =========================================================

# 1. 在模块导入时立即执行配置，确保 stdout 被劫持
setup_logging()

# 2. 定义 logger 对象 (main.py 中 from core.logger import logger 就是找它)
logger = logging.getLogger("gatewayproxy")
logger.setLevel(logging.INFO)