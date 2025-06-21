import datetime
import hashlib
import json
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import JobLookupError, ConflictingIdError
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Plain
from .utils import is_outdated, async_save_reminder_data, HolidayManager, parse_datetime

# 使用全局注册表来保存调度器实例
# 现在即使在模块重载后，调度器实例也能保持，我看你还怎么创建新实例（恼）
import sys

if not hasattr(sys, "_GLOBAL_SCHEDULER_REGISTRY"):
    sys._GLOBAL_SCHEDULER_REGISTRY = {
        'scheduler': None
    }
    logger.info("创建全局调度器注册表")
else:
    logger.info("使用现有全局调度器注册表")


class ReminderScheduler:
    def __new__(cls, context, reminder_data, data_file, postgres_url, unique_session=False, all_user_reminds=None):
        # 使用实例属性存储初始化状态
        instance = super(ReminderScheduler, cls).__new__(cls)
        instance._first_init = True  # 首次初始化

        logger.info("创建 ReminderScheduler 实例")
        return instance

    def __init__(self, context, reminder_data, data_file, postgres_url, unique_session=False, all_user_reminds=None):
        self.context = context
        self.reminder_data = reminder_data
        self.data_file = data_file
        self.postgres_url = postgres_url
        self.unique_session = unique_session
        # 新增：保存全员提醒配置
        self.all_user_reminds = all_user_reminds or []

        # 定义微信相关平台列表，用于特殊处理
        self.wechat_platforms = ["gewechat", "wechatpadpro", "wecom"]

        # 从全局注册表获取调度器，如果不存在则创建
        if sys._GLOBAL_SCHEDULER_REGISTRY['scheduler'] is None:
            sys._GLOBAL_SCHEDULER_REGISTRY['scheduler'] = AsyncIOScheduler()
            logger.info("创建新的全局 AsyncIOScheduler 实例")
        else:
            logger.info("使用现有全局 AsyncIOScheduler 实例")

        # 使用全局注册表中的调度器
        self.scheduler = sys._GLOBAL_SCHEDULER_REGISTRY['scheduler']

        # 创建节假日管理器
        self.holiday_manager = HolidayManager()

        # 如果有现有任务且是重新初始化，清理所有现有任务
        if not getattr(self, '_first_init', True) and self.scheduler.get_jobs():
            logger.info("检测到重新初始化，清理现有任务")
            for job in self.scheduler.get_jobs():
                if job.id.startswith("remind_") or job.id.startswith("global_remind_"):
                    try:
                        self.scheduler.remove_job(job.id)
                    except JobLookupError:
                        pass

        # 初始化任务
        self._init_scheduler()

        # 确保调度器运行
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("启动全局 AsyncIOScheduler")

        # 重置首次初始化标志
        self._first_init = False

    def _init_scheduler(self):
        '''初始化定时器'''
        # 定义星期映射
        self.weekday_map = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}

        logger.info(
            f"开始初始化调度器，加载 {sum(len(reminders) for reminders in self.reminder_data.values())} 个提醒/任务")

        # 清理当前实例关联的所有任务
        for job in self.scheduler.get_jobs():
            if job.id.startswith("remind_") or job.id.startswith("global_remind_"):
                try:
                    self.scheduler.remove_job(job.id)
                    logger.info(f"移除现有任务: {job.id}")
                except JobLookupError:
                    pass

        time.sleep(10)

        # 处理全员定时提醒
        for i, reminder_config in enumerate(self.all_user_reminds):
            if isinstance(reminder_config, str):
                try:
                    reminder_config = json.loads(reminder_config)
                except json.JSONDecodeError:
                    logger.error(f"无法解析全员提醒的JSON字符串: {reminder_config}")
                    continue

            content = reminder_config.get("content")
            date_time = reminder_config.get("date_time")
            repeat_type = reminder_config.get("repeat_type")
            holiday_type = reminder_config.get("holiday_type")
            # platform_name:message_type:session_id
            msg_origin = "wecom:FriendMessage:@all"

            if not content or not date_time:
                logger.error(f"全员提醒配置不完整，跳过: {reminder_config}")
                continue

            date_time = parse_datetime(date_time, None)
            dt = datetime.datetime.strptime(date_time, "%Y-%m-%d %H:%M")

            # 创建一个 reminder 对象用于回调
            reminder = {
                "text": content,
                "date_time": dt.strftime("%Y-%m-%d %H:%M"),
                "repeat_type": repeat_type,
                "holiday_type": holiday_type,
                "is_task": False
            }

            # # 兼容旧数据
            # if not repeat_type and "repeat_type" in reminder:
            #     repeat = reminder.get("repeat", "none")
            #     if "_" in repeat:
            #         repeat_type, holiday_type = repeat.split("_", 1)
            #     else:
            #         repeat_type = repeat
            #         holiday_type = None

            # 判断过期
            if (repeat_type == "none" or
                not any(repeat_key in repeat_type for repeat_key in
                        ["daily", "weekly", "monthly", "yearly", "none"])) and is_outdated(reminder):
                logger.info(f"跳过已过期的提醒: {reminder['text']}")
                continue

            # 生成唯一的任务ID
            unique_key = f"{msg_origin}_{reminder['text']}_{reminder['date_time']}"
            job_id = f"global_remind_{hashlib.md5(unique_key.encode()).hexdigest()}"

            try:
                # 根据重复类型设置不同的触发器
                if repeat_type == "daily" and not holiday_type:
                    self.scheduler.add_job(
                        self._reminder_callback,
                        'cron',
                        args=[msg_origin, reminder],
                        hour=dt.hour,
                        minute=dt.minute,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(f"添加每日提醒: {reminder['text']} 时间: {dt.hour}:{dt.minute} ID: {job_id}")
                elif repeat_type == "daily" and holiday_type == "workday":
                    self.scheduler.add_job(
                        self._check_and_execute_workday,
                        'cron',
                        args=[msg_origin, reminder],
                        hour=dt.hour,
                        minute=dt.minute,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(f"添加工作日提醒: {reminder['text']} 时间: {dt.hour}:{dt.minute} ID: {job_id}")
                elif repeat_type == "daily" and holiday_type == "holiday":
                    self.scheduler.add_job(
                        self._check_and_execute_holiday,
                        'cron',
                        args=[msg_origin, reminder],
                        hour=dt.hour,
                        minute=dt.minute,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(f"添加节假日提醒: {reminder['text']} 时间: {dt.hour}:{dt.minute} ID: {job_id}")
                elif repeat_type == "weekly" and not holiday_type:
                    self.scheduler.add_job(
                        self._reminder_callback,
                        'cron',
                        args=[msg_origin, reminder],
                        day_of_week=dt.weekday(),
                        hour=dt.hour,
                        minute=dt.minute,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(
                        f"添加每周提醒: {reminder['text']} 时间: 每周{dt.weekday() + 1} {dt.hour}:{dt.minute} ID: {job_id}")
                elif repeat_type == "weekly" and holiday_type == "workday":
                    self.scheduler.add_job(
                        self._check_and_execute_workday,
                        'cron',
                        args=[msg_origin, reminder],
                        day_of_week=dt.weekday(),
                        hour=dt.hour,
                        minute=dt.minute,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(
                        f"添加每周工作日提醒: {reminder['text']} 时间: 每周{dt.weekday() + 1} {dt.hour}:{dt.minute} ID: {job_id}")
                elif repeat_type == "weekly" and holiday_type == "holiday":
                    self.scheduler.add_job(
                        self._check_and_execute_holiday,
                        'cron',
                        args=[msg_origin, reminder],
                        day_of_week=dt.weekday(),
                        hour=dt.hour,
                        minute=dt.minute,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(
                        f"添加每周节假日提醒: {reminder['text']} 时间: 每周{dt.weekday() + 1} {dt.hour}:{dt.minute} ID: {job_id}")
                elif repeat_type == "monthly" and not holiday_type:
                    self.scheduler.add_job(
                        self._reminder_callback,
                        'cron',
                        args=[msg_origin, reminder],
                        day=dt.day,
                        hour=dt.hour,
                        minute=dt.minute,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(
                        f"添加每月提醒: {reminder['text']} 时间: 每月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
                elif repeat_type == "monthly" and holiday_type == "workday":
                    self.scheduler.add_job(
                        self._check_and_execute_workday,
                        'cron',
                        args=[msg_origin, reminder],
                        day=dt.day,
                        hour=dt.hour,
                        minute=dt.minute,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(
                        f"添加每月工作日提醒: {reminder['text']} 时间: 每月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
                elif repeat_type == "monthly" and holiday_type == "holiday":
                    self.scheduler.add_job(
                        self._check_and_execute_holiday,
                        'cron',
                        args=[msg_origin, reminder],
                        day=dt.day,
                        hour=dt.hour,
                        minute=dt.minute,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(
                        f"添加每月节假日提醒: {reminder['text']} 时间: 每月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
                elif repeat_type == "yearly" and not holiday_type:
                    self.scheduler.add_job(
                        self._reminder_callback,
                        'cron',
                        args=[msg_origin, reminder],
                        month=dt.month,
                        day=dt.day,
                        hour=dt.hour,
                        minute=dt.minute,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(
                        f"添加每年提醒: {reminder['text']} 时间: 每年{dt.month}月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
                elif repeat_type == "yearly" and holiday_type == "workday":
                    self.scheduler.add_job(
                        self._check_and_execute_workday,
                        'cron',
                        args=[msg_origin, reminder],
                        month=dt.month,
                        day=dt.day,
                        hour=dt.hour,
                        minute=dt.minute,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(
                        f"添加每年工作日提醒: {reminder['text']} 时间: 每年{dt.month}月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
                elif repeat_type == "yearly" and holiday_type == "holiday":
                    self.scheduler.add_job(
                        self._check_and_execute_holiday,
                        'cron',
                        args=[msg_origin, reminder],
                        month=dt.month,
                        day=dt.day,
                        hour=dt.hour,
                        minute=dt.minute,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(
                        f"添加每年节假日提醒: {reminder['text']} 时间: 每年{dt.month}月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
                else:
                    self.scheduler.add_job(
                        self._reminder_callback,
                        'date',
                        args=[msg_origin, reminder],
                        run_date=dt,
                        misfire_grace_time=60,
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(
                        f"添加一次性提醒: {reminder['text']} 时间: {dt.strftime('%Y-%m-%d %H:%M')} ID: {job_id}")
            except ConflictingIdError:
                logger.warning(f"任务 ID '{job_id}' 已存在，跳过添加。")
                pass
            except Exception as e:
                logger.error(f"处理全员提醒时出错: '{reminder_config}', 错误: {e}")
                pass

        # 重新添加所有个人/群组任务
        for msg_origin in self.reminder_data:
            for i, reminder in enumerate(self.reminder_data[msg_origin]):
                if "date_time" not in reminder:
                    continue

                date_time = reminder["date_time"]
                date_time = parse_datetime(date_time, None)
                dt = datetime.datetime.strptime(date_time, "%Y-%m-%d %H:%M")

                repeat_type = reminder.get("repeat_type")
                holiday_type = reminder.get("holiday_type")
                # # 兼容旧数据
                # if not repeat_type and "repeat_type" in reminder:
                #     repeat = reminder.get("repeat", "none")
                #     if "_" in repeat:
                #         repeat_type, holiday_type = repeat.split("_", 1)
                #     else:
                #         repeat_type = repeat
                #         holiday_type = None

                # 判断过期
                if (repeat_type == "none" or
                    not any(repeat_key in (repeat_type or "") for repeat_key in
                            ["daily", "weekly", "monthly", "yearly", "none"])) and is_outdated(reminder):
                    logger.info(f"跳过已过期的提醒: {reminder['text']}")
                    continue

                # 生成唯一的任务ID，使用提醒内容的哈希值确保唯一性
                unique_key = f"{msg_origin}_{reminder['text']}_{reminder['date_time']}"
                job_id = f"remind_{hashlib.md5(unique_key.encode()).hexdigest()}"

                try:
                    # 组合判断
                    if repeat_type == "daily" and not holiday_type:
                        self.scheduler.add_job(
                            self._reminder_callback,
                            'cron',
                            args=[msg_origin, reminder],
                            hour=dt.hour,
                            minute=dt.minute,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(f"添加每日提醒: {reminder['text']} 时间: {dt.hour}:{dt.minute} ID: {job_id}")
                    elif repeat_type == "daily" and holiday_type == "workday":
                        self.scheduler.add_job(
                            self._check_and_execute_workday,
                            'cron',
                            args=[msg_origin, reminder],
                            hour=dt.hour,
                            minute=dt.minute,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(f"添加工作日提醒: {reminder['text']} 时间: {dt.hour}:{dt.minute} ID: {job_id}")
                    elif repeat_type == "daily" and holiday_type == "holiday":
                        self.scheduler.add_job(
                            self._check_and_execute_holiday,
                            'cron',
                            args=[msg_origin, reminder],
                            hour=dt.hour,
                            minute=dt.minute,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(f"添加节假日提醒: {reminder['text']} 时间: {dt.hour}:{dt.minute} ID: {job_id}")
                    elif repeat_type == "weekly" and not holiday_type:
                        self.scheduler.add_job(
                            self._reminder_callback,
                            'cron',
                            args=[msg_origin, reminder],
                            day_of_week=dt.weekday(),
                            hour=dt.hour,
                            minute=dt.minute,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(
                            f"添加每周提醒: {reminder['text']} 时间: 每周{dt.weekday() + 1} {dt.hour}:{dt.minute} ID: {job_id}")
                    elif repeat_type == "weekly" and holiday_type == "workday":
                        self.scheduler.add_job(
                            self._check_and_execute_workday,
                            'cron',
                            args=[msg_origin, reminder],
                            day_of_week=dt.weekday(),
                            hour=dt.hour,
                            minute=dt.minute,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(
                            f"添加每周工作日提醒: {reminder['text']} 时间: 每周{dt.weekday() + 1} {dt.hour}:{dt.minute} ID: {job_id}")
                    elif repeat_type == "weekly" and holiday_type == "holiday":
                        self.scheduler.add_job(
                            self._check_and_execute_holiday,
                            'cron',
                            args=[msg_origin, reminder],
                            day_of_week=dt.weekday(),
                            hour=dt.hour,
                            minute=dt.minute,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(
                            f"添加每周节假日提醒: {reminder['text']} 时间: 每周{dt.weekday() + 1} {dt.hour}:{dt.minute} ID: {job_id}")
                    elif repeat_type == "monthly" and not holiday_type:
                        self.scheduler.add_job(
                            self._reminder_callback,
                            'cron',
                            args=[msg_origin, reminder],
                            day=dt.day,
                            hour=dt.hour,
                            minute=dt.minute,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(
                            f"添加每月提醒: {reminder['text']} 时间: 每月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
                    elif repeat_type == "monthly" and holiday_type == "workday":
                        self.scheduler.add_job(
                            self._check_and_execute_workday,
                            'cron',
                            args=[msg_origin, reminder],
                            day=dt.day,
                            hour=dt.hour,
                            minute=dt.minute,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(
                            f"添加每月工作日提醒: {reminder['text']} 时间: 每月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
                    elif repeat_type == "monthly" and holiday_type == "holiday":
                        self.scheduler.add_job(
                            self._check_and_execute_holiday,
                            'cron',
                            args=[msg_origin, reminder],
                            day=dt.day,
                            hour=dt.hour,
                            minute=dt.minute,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(
                            f"添加每月节假日提醒: {reminder['text']} 时间: 每月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
                    elif repeat_type == "yearly" and not holiday_type:
                        self.scheduler.add_job(
                            self._reminder_callback,
                            'cron',
                            args=[msg_origin, reminder],
                            month=dt.month,
                            day=dt.day,
                            hour=dt.hour,
                            minute=dt.minute,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(
                            f"添加每年提醒: {reminder['text']} 时间: 每年{dt.month}月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
                    elif repeat_type == "yearly" and holiday_type == "workday":
                        self.scheduler.add_job(
                            self._check_and_execute_workday,
                            'cron',
                            args=[msg_origin, reminder],
                            month=dt.month,
                            day=dt.day,
                            hour=dt.hour,
                            minute=dt.minute,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(
                            f"添加每年工作日提醒: {reminder['text']} 时间: 每年{dt.month}月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
                    elif repeat_type == "yearly" and holiday_type == "holiday":
                        self.scheduler.add_job(
                            self._check_and_execute_holiday,
                            'cron',
                            args=[msg_origin, reminder],
                            month=dt.month,
                            day=dt.day,
                            hour=dt.hour,
                            minute=dt.minute,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(
                            f"添加每年节假日提醒: {reminder['text']} 时间: 每年{dt.month}月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
                    else:
                        self.scheduler.add_job(
                            self._reminder_callback,
                            'date',
                            args=[msg_origin, reminder],
                            run_date=dt,
                            misfire_grace_time=60,
                            id=job_id,
                            replace_existing=True
                        )
                        logger.info(
                            f"添加一次性提醒: {reminder['text']} 时间: {dt.strftime('%Y-%m-%d %H:%M')} ID: {job_id}")

                except ConflictingIdError:
                    logger.warning(f"任务 ID '{job_id}' 已存在，跳过添加。")
                    pass
                except Exception as e:
                    logger.warning(f"添加定时任务出错: '{reminder_config}', 错误: {e}")
                    pass

    async def _check_and_execute_workday(self, unified_msg_origin: str, reminder: dict):
        '''检查当天是否为工作日，如果是则执行提醒'''
        today = datetime.datetime.now()
        logger.info(f"检查日期 {today.strftime('%Y-%m-%d')} 是否为工作日，提醒内容: {reminder['text']}")

        is_workday = await self.holiday_manager.is_workday(today)
        logger.info(f"日期 {today.strftime('%Y-%m-%d')} 工作日检查结果: {is_workday}")

        if is_workday:
            # 如果是工作日则执行提醒
            logger.info(f"确认今天是工作日，执行提醒: {reminder['text']}")
            await self._reminder_callback(unified_msg_origin, reminder)
        else:
            logger.info(f"今天不是工作日，跳过执行提醒: {reminder['text']}")

    async def _check_and_execute_holiday(self, unified_msg_origin: str, reminder: dict):
        '''检查当天是否为法定节假日，如果是则执行提醒'''
        today = datetime.datetime.now()
        logger.info(f"检查日期 {today.strftime('%Y-%m-%d')} 是否为法定节假日，提醒内容: {reminder['text']}")

        is_holiday = await self.holiday_manager.is_holiday(today)
        logger.info(f"日期 {today.strftime('%Y-%m-%d')} 法定节假日检查结果: {is_holiday}")

        if is_holiday:
            # 如果是法定节假日则执行提醒
            logger.info(f"确认今天是法定节假日，执行提醒: {reminder['text']}")
            await self._reminder_callback(unified_msg_origin, reminder)
        else:
            logger.info(f"今天不是法定节假日，跳过执行提醒: {reminder['text']}")

    async def _reminder_callback(self, unified_msg_origin: str, reminder: dict):
        '''提醒回调函数'''
        # 新增：处理全员提醒
        if unified_msg_origin == "wecom:FriendMessage:@all":
            task_text = reminder.get("text", "")

            # 获取AI提供者并生成回复
            provider = self.context.get_using_provider()
            if provider:
                prompt = (f"请以友好、积极的语言将以下内容发送给用户。")
                try:
                    response = await provider.text_chat(
                        session_id=unified_msg_origin,
                        contexts=task_text,
                        prompt=prompt,
                    )

                    if response.completion_text:
                        message_chain = MessageChain([Plain(response.completion_text)])
                    else:
                        logger.warning("LLM未返回文本。")
                        message_chain = MessageChain([Plain(f"⏰ 提醒：{task_text}")])
                except Exception as e:
                    logger.error(f"生成消息时出错: {str(e)}")
                    message_chain = MessageChain([Plain(f"⏰ 提醒：{task_text}")])

            else:
                message_chain = MessageChain([Plain(f"⏰ 提醒：{task_text}")])
            try:
                send_result = await self.context.send_message(unified_msg_origin, message_chain)
                logger.info(f"全员提醒发送成功: {send_result}")
            except Exception as e:
                logger.error(f"发送全员提醒失败: {str(e)}")
            return  # 结束

        try:
            # 获取当前时间
            current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 检查是否是任务
            is_task = reminder.get("is_task", False)
            task_text = reminder.get("text", "")

            # 判断消息平台类型
            is_qq_platform = "aiocqhttp" in unified_msg_origin
            is_private_chat = ":FriendMessage:" in unified_msg_origin or ":PrivateMessage:" in unified_msg_origin

            # 初始化 contexts 变量
            contexts = []

            # 获取当前对话上下文
            try:
                curr_cid = await self.context.conversation_manager.get_curr_conversation_id(unified_msg_origin)
                if curr_cid:
                    conversation = await self.context.conversation_manager.get_conversation(
                        unified_msg_origin,
                        curr_cid
                    )
                    if conversation and conversation.history:
                        try:
                            contexts = json.loads(conversation.history)
                            logger.info(f"提醒模式：用户对话ID: {curr_cid}, 上下文长度: {len(contexts)}")
                        except json.JSONDecodeError:
                            contexts = []
            except Exception as e:
                logger.error(f"提醒模式：获取对话上下文失败: {str(e)}")
                contexts = []

            from astrbot.api.platform import AstrBotMessage, PlatformMetadata, MessageType, MessageMember
            from astrbot.core.platform.astr_message_event import AstrMessageEvent

            # 创建消息对象
            msg = AstrBotMessage()
            msg.message_str = task_text
            msg.session_id = unified_msg_origin
            msg.type = MessageType.FRIEND_MESSAGE if is_private_chat else MessageType.GROUP_MESSAGE

            # 设置发送者信息
            if "creator_id" in reminder:
                msg.sender = MessageMember(reminder["creator_id"], reminder.get("creator_name", "用户"))
            else:
                msg.sender = MessageMember("unknown", "用户")

            # 设置平台信息
            platform_name = "unknown"
            if is_qq_platform:
                platform_name = "qq"
            elif ":FriendMessage:" in unified_msg_origin:
                platform_name = "wechat"
            elif ":GroupMessage:" in unified_msg_origin:
                platform_name = "wechat"
            elif ":ChannelMessage:" in unified_msg_origin:
                platform_name = "discord"
            msg.platform = PlatformMetadata(platform_name, description=f"{platform_name} platform")

            # 创建事件对象
            event = AstrMessageEvent(
                message_obj=msg,
                platform_meta=msg.platform,
                session_id=unified_msg_origin,
                message_str=task_text
            )

            # 获取原始会话ID
            original_msg_origin = self.get_original_session_id(unified_msg_origin)

            # 如果是QQ平台，直接使用原始会话ID
            if is_qq_platform:
                target_session_id = original_msg_origin
            else:
                target_session_id = unified_msg_origin

            # 获取AI提供者并生成回复
            provider = self.context.get_using_provider()
            if provider:
                if is_task:
                    prompt = (f"现在是 {current_time}，请直接执行以下预设任务：{task_text}\n\n"
                              "请注意：不要提及这是一个预设任务或提醒，你的回复应该像用户刚刚发出了这个请求一样自然。")
                else:
                    prompt = (f"现在是 {current_time}，请用自然的语气提醒用户以下事项：{task_text}\n\n"
                              "请注意：你的回复应该简短、友好，就像一个贴心的助手在和用户对话。")

                try:
                    response = await provider.text_chat(
                        session_id=target_session_id,
                        contexts=contexts,
                        prompt=prompt,
                    )

                    if response.completion_text:
                        message_chain = MessageChain([Plain(response.completion_text)])
                    else:
                        logger.warning("LLM未返回文本。")
                        message_chain = MessageChain([Plain(f"⏰ 提醒：{task_text}")])
                except Exception as e:
                    logger.error(f"生成消息时出错: {str(e)}")
                    message_chain = MessageChain([Plain(f"⏰ 提醒：{task_text}")])

            else:
                message_chain = MessageChain([Plain(f"⏰ 提醒：{task_text}")])

            if not message_chain:
                logger.error("消息链为空")
                message_chain = MessageChain([Plain(f"⏰ 提醒：{task_text}")])

            # 发送消息
            try:
                logger.info(f"开始发送消息: {message_chain}")
                send_result = await self.context.send_message(target_session_id, message_chain)
                logger.info(f"消息发送成功: {send_result}")

                # 如果是一次性提醒/任务，执行完后删除
                if reminder.get("repeat_type") in ["不重复", "none"]:
                    try:
                        # 从提醒数据中删除
                        if unified_msg_origin in self.reminder_data:
                            reminds = self.reminder_data[unified_msg_origin]
                            # 使用更严格的匹配条件
                            self.reminder_data[unified_msg_origin] = [
                                r for r in reminds
                                if not (
                                        r.get("text", "").strip() == reminder.get("text", "").strip() and
                                        r.get("date_time", "").strip() == reminder.get("date_time", "").strip() and
                                        r.get("creator_id", "") == reminder.get("creator_id", "") and
                                        r.get("repeat_type", "") in ["不重复", "none"]
                                )
                            ]
                            logger.info(f"已删除一次性{'任务' if is_task else '提醒'}: {task_text}")

                            # 保存更新后的提醒数据
                            await async_save_reminder_data(self.data_file, self.postgres_url, self.reminder_data)
                            logger.info(f"已保存更新后的提醒数据")
                    except Exception as e:
                        logger.error(f"删除一次性{'任务' if is_task else '提醒'}时出错: {str(e)}")
            except Exception as e:
                logger.error(f"发送消息失败: {str(e)}")
                if isinstance(message_chain, MessageChain):
                    # 获取 MessageChain 中的消息内容
                    try:
                        plain_text = message_chain.get_plain_text() or f"⏰ 提醒：{task_text}"
                    except:
                        plain_text = f"⏰ 提醒：{task_text}"
                    # 直接使用 send_message 而不是 plain_result
                    logger.info(f"开始重新发送消息: {plain_text}")
                    send_result = await self.context.send_message(target_session_id, MessageChain([Plain(plain_text)]))
                else:
                    logger.error("message_chain is not a MessageChain object")
                    send_result = await self.context.send_message(target_session_id,
                                                                  MessageChain([Plain(f"⏰ 提醒：{task_text}")]))
                if send_result:
                    logger.info(f"重新发送消息成功: {send_result}")
                else:
                    logger.error(f"重新发送消息失败: {send_result}")

                # 更新对话历史
                if curr_cid and conversation:
                    try:
                        new_contexts = contexts.copy()
                        new_contexts.append({"role": "system",
                                             "content": f"系统在 {current_time} {'执行了任务' if is_task else '触发了提醒'}: {task_text}"})
                        # 获取 MessageChain 中的消息内容
                        try:
                            plain_text = message_chain.get_plain_text() if isinstance(message_chain,
                                                                                      MessageChain) else f"⏰ 提醒：{task_text}"
                        except:
                            plain_text = f"⏰ 提醒：{task_text}"
                        new_contexts.append({"role": "assistant", "content": plain_text})
                        await self.context.conversation_manager.update_conversation(
                            target_session_id,
                            curr_cid,
                            history=new_contexts
                        )
                    except Exception as e:
                        logger.error(f"更新对话历史失败: {str(e)}")
        except Exception as e:
            logger.error(f"处理提醒回调时出错: {str(e)}")
            error_msg = f"处理提醒时出错：{str(e)}"
            message_chain = MessageChain([Plain(error_msg)])
            try:
                # # 确保 target_session_id 在异常情况下也已定义
                # if 'target_session_id' not in locals():
                #     target_session_id = self.get_original_session_id(unified_msg_origin)
                send_result = await self.context.send_message(target_session_id, message_chain)
                if send_result:
                    logger.info(f"消息发送成功: {send_result}")
            except Exception as e2:
                send_result = await event.plain_result(error_msg)
                if not send_result:
                    logger.error(f"发送错误消息失败: {str(e2)}")

    def add_job(self, msg_origin, reminder, dt):
        '''添加一个定时任务
        Returns:
            bool: 是否成功添加任务
        '''

        # 生成唯一的任务ID，使用提醒内容的哈希值确保唯一性
        unique_key = f"{msg_origin}_{reminder['text']}_{reminder['date_time']}"
        job_id = f"remind_{hashlib.md5(unique_key.encode()).hexdigest()}"

        repeat_type = reminder.get("repeat_type")
        holiday_type = reminder.get("holiday_type")

        try:
            # 根据重复类型设置不同的触发器
            if repeat_type == "daily" and not holiday_type:
                self.scheduler.add_job(
                    self._reminder_callback,
                    'cron',
                    args=[msg_origin, reminder],
                    hour=dt.hour,
                    minute=dt.minute,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(f"添加每日提醒: {reminder['text']} 时间: {dt.hour}:{dt.minute} ID: {job_id}")
            elif repeat_type == "daily" and holiday_type == "workday":
                self.scheduler.add_job(
                    self._check_and_execute_workday,
                    'cron',
                    args=[msg_origin, reminder],
                    hour=dt.hour,
                    minute=dt.minute,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(f"添加工作日提醒: {reminder['text']} 时间: {dt.hour}:{dt.minute} ID: {job_id}")
            elif repeat_type == "daily" and holiday_type == "holiday":
                self.scheduler.add_job(
                    self._check_and_execute_holiday,
                    'cron',
                    args=[msg_origin, reminder],
                    hour=dt.hour,
                    minute=dt.minute,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(f"添加节假日提醒: {reminder['text']} 时间: {dt.hour}:{dt.minute} ID: {job_id}")
            elif repeat_type == "weekly" and not holiday_type:
                self.scheduler.add_job(
                    self._reminder_callback,
                    'cron',
                    args=[msg_origin, reminder],
                    day_of_week=dt.weekday(),
                    hour=dt.hour,
                    minute=dt.minute,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(
                    f"添加每周提醒: {reminder['text']} 时间: 每周{dt.weekday() + 1} {dt.hour}:{dt.minute} ID: {job_id}")
            elif repeat_type == "weekly" and holiday_type == "workday":
                self.scheduler.add_job(
                    self._check_and_execute_workday,
                    'cron',
                    args=[msg_origin, reminder],
                    day_of_week=dt.weekday(),
                    hour=dt.hour,
                    minute=dt.minute,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(
                    f"添加每周工作日提醒: {reminder['text']} 时间: 每周{dt.weekday() + 1} {dt.hour}:{dt.minute} ID: {job_id}")
            elif repeat_type == "weekly" and holiday_type == "holiday":
                self.scheduler.add_job(
                    self._check_and_execute_holiday,
                    'cron',
                    args=[msg_origin, reminder],
                    day_of_week=dt.weekday(),
                    hour=dt.hour,
                    minute=dt.minute,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(
                    f"添加每周节假日提醒: {reminder['text']} 时间: 每周{dt.weekday() + 1} {dt.hour}:{dt.minute} ID: {job_id}")
            elif repeat_type == "monthly" and not holiday_type:
                self.scheduler.add_job(
                    self._reminder_callback,
                    'cron',
                    args=[msg_origin, reminder],
                    day=dt.day,
                    hour=dt.hour,
                    minute=dt.minute,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(
                    f"添加每月提醒: {reminder['text']} 时间: 每月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
            elif repeat_type == "monthly" and holiday_type == "workday":
                self.scheduler.add_job(
                    self._check_and_execute_workday,
                    'cron',
                    args=[msg_origin, reminder],
                    day=dt.day,
                    hour=dt.hour,
                    minute=dt.minute,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(
                    f"添加每月工作日提醒: {reminder['text']} 时间: 每月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
            elif repeat_type == "monthly" and holiday_type == "holiday":
                self.scheduler.add_job(
                    self._check_and_execute_holiday,
                    'cron',
                    args=[msg_origin, reminder],
                    day=dt.day,
                    hour=dt.hour,
                    minute=dt.minute,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(
                    f"添加每月节假日提醒: {reminder['text']} 时间: 每月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
            elif repeat_type == "yearly" and not holiday_type:
                self.scheduler.add_job(
                    self._reminder_callback,
                    'cron',
                    args=[msg_origin, reminder],
                    month=dt.month,
                    day=dt.day,
                    hour=dt.hour,
                    minute=dt.minute,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(
                    f"添加每年提醒: {reminder['text']} 时间: 每年{dt.month}月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
            elif repeat_type == "yearly" and holiday_type == "workday":
                self.scheduler.add_job(
                    self._check_and_execute_workday,
                    'cron',
                    args=[msg_origin, reminder],
                    month=dt.month,
                    day=dt.day,
                    hour=dt.hour,
                    minute=dt.minute,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(
                    f"添加每年工作日提醒: {reminder['text']} 时间: 每年{dt.month}月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
            elif repeat_type == "yearly" and holiday_type == "holiday":
                self.scheduler.add_job(
                    self._check_and_execute_holiday,
                    'cron',
                    args=[msg_origin, reminder],
                    month=dt.month,
                    day=dt.day,
                    hour=dt.hour,
                    minute=dt.minute,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(
                    f"添加每年节假日提醒: {reminder['text']} 时间: 每年{dt.month}月{dt.day}日 {dt.hour}:{dt.minute} ID: {job_id}")
            else:
                self.scheduler.add_job(
                    self._reminder_callback,
                    'date',
                    args=[msg_origin, reminder],
                    run_date=dt,
                    misfire_grace_time=60,
                    id=job_id,
                    replace_existing=True
                )
                logger.info(
                    f"添加一次性提醒: {reminder['text']} 时间: {dt.strftime('%Y-%m-%d %H:%M')} ID: {job_id}")

            return True
        except ConflictingIdError:
            logger.warning(f"任务 ID '{job_id}' 已存在，跳过添加。")
            return True
        except Exception as e:
            logger.error(f"添加定时任务失败: {str(e)}")
            return False

    def remove_job(self, job_id):
        '''删除定时任务'''
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"Successfully removed job: {job_id}")
            return True
        except JobLookupError:
            logger.error(f"Job not found: {job_id}")
            return False

    # 获取会话ID
    def get_session_id(self, unified_msg_origin, reminder):
        """
        根据会话隔离设置，获取正确的会话ID
        
        Args:
            unified_msg_origin: 原始会话ID
            reminder: 提醒/任务数据
            
        Returns:
            str: 处理后的会话ID
        """
        if not self.unique_session:
            return unified_msg_origin

        # 如果启用了会话隔离，并且有创建者ID，则在会话ID中添加用户标识
        creator_id = reminder.get("creator_id")
        if creator_id and ":" in unified_msg_origin:
            # 在群聊环境中添加用户ID
            if (":GroupMessage:" in unified_msg_origin or
                    "@chatroom" in unified_msg_origin or
                    ":ChannelMessage:" in unified_msg_origin):
                # 分割会话ID并在末尾添加用户标识
                parts = unified_msg_origin.rsplit(":", 1)
                if len(parts) == 2:
                    return f"{parts[0]}:{parts[1]}_{creator_id}"

        return unified_msg_origin

    def get_original_session_id(self, session_id):
        """
        从隔离格式的会话ID中提取原始会话ID，用于消息发送
        """
        # 检查是否是微信平台
        is_wechat_platform = any(session_id.startswith(platform) for platform in self.wechat_platforms)

        # 处理微信群聊的特殊情况
        if "@chatroom" in session_id:
            # 微信群聊ID可能有两种格式:
            # 1. platform:GroupMessage:12345678@chatroom_wxid_abc123 (带用户隔离)
            # 2. platform:GroupMessage:12345678@chatroom (原始格式)

            # 提取平台前缀
            platform_prefix = ""
            if ":" in session_id:
                parts = session_id.split(":", 2)
                if len(parts) >= 2:
                    platform_prefix = f"{parts[0]}:{parts[1]}:"

            # 然后处理@chatroom后面的部分
            chatroom_parts = session_id.split("@chatroom")
            if len(chatroom_parts) == 2:
                if chatroom_parts[1].startswith("_"):
                    # 如果有下划线，说明这是带用户隔离的格式
                    room_id = chatroom_parts[0].split(":")[-1]
                    return f"{platform_prefix}{room_id}@chatroom"
                else:
                    # 这已经是原始格式，直接返回
                    return session_id

        # 处理其他平台的情况
        if "_" in session_id and ":" in session_id:
            # 首先判断是否是微信相关平台
            if is_wechat_platform:
                # 微信平台需要特殊处理
                # 因为微信个人ID通常包含下划线，不适合用通用分割方法

                # 但是，如果明确是群聊隔离格式，仍然需要处理
                if "@chatroom_" in session_id:
                    # 这部分已经在上面处理过了
                    pass
                elif ":GroupMessage:" in session_id and "_" in session_id.split(":")[-1]:
                    # 可能是其他格式的群聊隔离
                    parts = session_id.split(":")
                    if len(parts) >= 3:
                        group_parts = parts[-1].rsplit("_", 1)
                        if len(group_parts) == 2:
                            return f"{parts[0]}:{parts[1]}:{group_parts[0]}"

                # 如果没有命中上述规则，返回原始ID
                return session_id
            else:
                # 非微信平台，使用通用规则
                parts = session_id.rsplit(":", 1)
                if len(parts) == 2 and "_" in parts[1]:
                    # 查找最后一个下划线，认为这是会话隔离添加的
                    group_id, user_id = parts[1].rsplit("_", 1)
                    return f"{parts[0]}:{group_id}"

        # 如果不是隔离格式或无法解析，返回原始ID
        return session_id

    # 析构函数不执行操作
    def __del__(self):
        # 不关闭调度器，因为它是全局共享的
        logger.info("ReminderScheduler 实例被销毁")
        pass

    @staticmethod
    def get_scheduler():
        """获取当前的全局调度器实例"""
        return sys._GLOBAL_SCHEDULER_REGISTRY.get('scheduler')
