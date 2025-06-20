import asyncio
import os

from astrbot.api import llm_tool
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import command, command_group, EventMessageType, PermissionType, event_message_type
from astrbot.api.message_components import *
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember, MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.manager import Personality
from astrbot.core.star.star_handler import star_handlers_registry, EventType

from .core.reminder import ReminderSystem
from .core.scheduler import ReminderScheduler
from .core.tools import ReminderTools
from .core.utils import load_reminder_data


@register("astrbot_plugin_remind", "beat4ocean", "智能提醒、任务插件", "0.0.2")
class Main(Star):
    @classmethod
    def info(cls):
        return {
            "name": "astrbot_plugin_remind",
            "version": "0.0.2",
            "description": "智能提醒、任务插件",
            "author": "beat4ocean"
        }

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)

        # 保存配置
        self.config = config or {}
        self.unique_session = self.config.get("unique_session", False)
        # 新增：获取全员提醒配置
        self.all_user_reminds = self.config.get("all_user_reminds", [])

        # 配置PostgreSQL
        self.postgres_url = self.config.get("postgres_url", "")
        if self.postgres_url != "":
            logger.info("将使用PostgreSQL保存提醒数据")
        else:
            logger.info("将使用本地JSON文件保存提醒数据")

        # 使用data目录下的数据文件，而非插件自身目录
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                "astrbot_plugin_remind")
        os.makedirs(os.path.join(data_dir, "remind_data"), exist_ok=True)
        self.data_file = os.path.join(data_dir, "remind_data", "remind_data.json")

        # 使用同步方法加载数据
        self.reminder_data = load_reminder_data(self.data_file, self.postgres_url)

        # 初始化调度器
        self.scheduler_manager = ReminderScheduler(
            self.context,
            self.reminder_data,
            self.data_file,
            self.postgres_url,
            self.unique_session,
            self.all_user_reminds
        )

        # 初始化工具
        self.tools = ReminderTools(self)

        # 初始化提醒系统
        self.reminder_system = ReminderSystem(
            self.context,
            self.config,
            self.scheduler_manager,
            self.tools,
            self.data_file,
            self.postgres_url
        )

        # 记录配置信息
        logger.info(f"智能提醒插件启动成功，会话隔离：{'启用' if self.unique_session else '禁用'}")
        if self.all_user_reminds:
            logger.info(f"已加载 {len(self.all_user_reminds)} 个全员提醒。")

        self.cd = 10  # 默认冷却时间为 10 秒
        self.last_usage = {}  # 存储每个用户上次使用指令的时间
        self.semaphore = asyncio.Semaphore(10)  # 限制并发请求数量为 10

    # ========== 命令行开始 ==========
    # 命令组必须定义在主类中
    @command_group("remind")
    def remind(self):
        '''提醒和任务相关命令'''
        pass

    @remind.command("列表")
    async def list_reminds(self, event: AstrMessageEvent):
        '''列出所有提醒和任务'''
        try:
            result = await self.reminder_system.list_reminds(event)
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"列出提醒或任务时出错: {str(e)}")
            yield event.plain_result(f"列出提醒或任务时出错：{str(e)}")

    @remind.command("添加提醒")
    async def add_remind(self, event: AstrMessageEvent, text: str, date_time: str, week: str = None,
                         repeat_type: str = None, holiday_type: str = None):
        '''手动添加提醒'''
        result = await self.reminder_system.add_remind(event, text, date_time, week, repeat_type, holiday_type, False)
        return result

    @remind.command("添加任务")
    async def add_task(self, event: AstrMessageEvent, text: str, date_time: str, week: str = None,
                       repeat_type: str = None, holiday_type: str = None):
        '''手动添加任务'''
        result = await self.reminder_system.add_remind(event, text, date_time, week, repeat_type, holiday_type, True)
        return result

    @remind.command("删除")
    async def remove_reminds(self, event: AstrMessageEvent, index: str):
        '''删除提醒或任务'''
        result = await self.reminder_system.remove_reminds(event, index)
        yield event.plain_result(result)

    @remind.command("帮助")
    async def show_help(self):
        '''显示帮助信息'''
        help_text = self.reminder_system.show_help()
        return help_text

    # ========== 命令行结束 ==========

    # ========== LLM 开始 ==========
    @filter.llm_tool(name="query_reminds")
    async def query_reminds(self, event: AstrMessageEvent):
        '''查询所有提醒和任务'''
        try:
            # 调用工具类设置提醒
            result = await self.reminder_system.query_reminds(event)
            logger.info(f"查询提醒和任务结果:\n{result[:50]}...")
            return result

        except Exception as e:
            logger.error(f"查询提醒或任务时出错: {str(e)}")
            return f"查询提醒或任务失败：{str(e)}"

    @filter.llm_tool(name="set_remind")
    async def set_remind(self, event: AstrMessageEvent, text: str, date_time: str, repeat_type: str = None,
                         holiday_type: str = None):
        '''设置一个提醒，到时间时会提醒用户

        Args:
            text(string): 提醒内容
            date_time(string): 提醒时间，格式为 %Y-%m-%d %H:%M
            repeat_type(string): 重复类型，可选值：daily(每天)，weekly(每周)，monthly(每月)，yearly(每年)，none(不重复)
            holiday_type(string): 可选，节假日类型：workday(仅工作日执行)，holiday(仅法定节假日执行)
        '''
        try:
            # 调用工具类设置提醒
            result = await self.tools.set_remind(event, text, date_time, repeat_type, holiday_type)
            logger.info(f"设置提醒结果:\n{result}")
            return result

        except Exception as e:
            logger.error(f"设置提醒时出错: {str(e)}")
            return f"设置提醒失败：{str(e)}"

    @filter.llm_tool(name="set_task")
    async def set_task(self, event: AstrMessageEvent, text: str, date_time: str, repeat_type: str = None,
                       holiday_type: str = None):
        '''设置一个任务，到时间后会让AI执行该任务
        
        Args:
            text(string): 任务内容，AI将执行的操作，如果是调用其他llm函数，请告诉ai（比如，请调用llm函数，内容是...）
            date_time(string): 任务执行时间，格式为 %Y-%m-%d %H:%M
            repeat_type(string): 重复类型，可选值：daily(每天)，weekly(每周)，monthly(每月)，yearly(每年)，none(不重复)
            holiday_type(string): 可选，节假日类型：workday(仅工作日执行)，holiday(仅法定节假日执行)
        '''
        try:
            # 确保任务内容包含必要的指令
            if not text.startswith("请调用llm函数"):
                text = f"请调用llm函数，{text}"

            # 调用工具类设置任务
            result = await self.tools.set_task(event, text, date_time, repeat_type, holiday_type)
            logger.info(f"设置任务结果:\n{result}")
            return result

        except Exception as e:
            logger.error(f"设置任务时出错: {str(e)}")
            return f"设置任务失败：{str(e)}"

    @filter.llm_tool(name="delete_remind")
    async def delete_remind(self, event: AstrMessageEvent, index: str):
        '''删除符合条件的提醒或任务，不支持修改提醒或任务内容
        
        Args:
            index(string): 需要删除的提醒或任务的数字序号,例如：1
        '''
        try:
            result = await self.tools.delete_remind(event, index)
            logger.info(f"删除提醒或任务结果:\n{result}")
            return result
        except Exception as e:
            logger.error(f"删除提醒或任务时出错: {str(e)}")
            return f"删除提醒或任务失败：{str(e)}"
    # ========== LLM 结束 ==========
