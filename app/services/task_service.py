import os
import json
import asyncio
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core.logger import logger
from app.services.drive115_service import drive115_service

class TaskService:
    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.cleanup_job_ids = []

    def refresh_cleanup_jobs(self):
        """
        读取 302 配置，重新注册所有 115 清理任务（主号 + 小号）
        """
        # 1. 先移除所有旧的清理任务
        for job_id in self.cleanup_job_ids:
            try:
                self.scheduler.remove_job(job_id)
            except: pass
        self.cleanup_job_ids = []

        # 2. 读取配置
        config_path = "config/config_302.json"
        if not os.path.exists(config_path):
            return

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 兼容旧配置结构 (转换为列表)
            drives = data.get("drives", [])
            if not drives and data.get("drive115"):
                drives = [data.get("drive115")]

            logger.info(f"[Scheduler] 正在刷新 115 清理任务，检测到 {len(drives)} 个配置...")

            job_counter = 0
            for idx, drive in enumerate(drives):
                # 检查是否启用自动删除
                if not drive.get("auto_delete", False):
                    continue

                cron_exp = drive.get("delete_cron", "30 3 * * *") # 默认凌晨3:30
                drive_name = drive.get("name", f"主号{idx + 1}")

                # ===== 主号清理任务 =====
                job_id = f"cleanup_main_{idx}"
                self._add_cleanup_job(job_id, drive, "main", 0, cron_exp, drive_name)
                job_counter += 1

                # ===== 小号清理任务（如果启用了秒传）=====
                rapid_accounts = drive.get("rapid_accounts", [])
                if rapid_accounts and drive.get("enable_rapid", False):
                    for r_idx, rapid_acc in enumerate(rapid_accounts):
                        # 只有配置了 cookie 的小号才添加清理任务
                        if rapid_acc.get("cookie"):
                            r_job_id = f"cleanup_rapid_{idx}_{r_idx}"
                            r_name = rapid_acc.get("name", f"小号{r_idx + 1}")
                            self._add_cleanup_job(r_job_id, drive, "rapid", r_idx, cron_exp, r_name)
                            job_counter += 1

            logger.info(f"[Scheduler] ✅ 已添加 {job_counter} 个清理任务")

        except Exception as e:
            logger.error(f"[Scheduler] 读取 302 配置失败: {e}")

    def _add_cleanup_job(self, job_id: str, drive_config: dict, account_type: str, account_index: int, cron_exp: str, name: str):
        """添加单个清理任务到调度器"""
        def cleanup_wrapper():
            try:
                # 尝试获取当前线程的事件循环
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                # 执行异步任务
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        drive115_service.execute_cleanup_task(drive_config, account_type, account_index),
                        loop
                    )
                else:
                    loop.run_until_complete(drive115_service.execute_cleanup_task(drive_config, account_type, account_index))

            except Exception as e:
                logger.error(f"[Cleanup] 任务执行异常: {e}")

        try:
            self.scheduler.add_job(
                cleanup_wrapper,
                CronTrigger.from_crontab(cron_exp),
                id=job_id,
                replace_existing=True
            )
            self.cleanup_job_ids.append(job_id)
            logger.info(f"[Scheduler] ✅ 已添加清理任务: [{name}] @ {cron_exp}")
        except Exception as e:
            logger.error(f"[Scheduler] Cron 格式错误或添加失败: {e}")

task_service_instance = TaskService()
