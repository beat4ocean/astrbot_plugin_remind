import datetime

from apscheduler.schedulers.base import JobLookupError
from astrbot.api import logger
from astrbot.api.event import MessageChain, AstrMessageEvent
from astrbot.api.star import StarTools
from astrbot.core.message.message_event_result import MessageChain

from .tools import ReminderTools
from .utils import async_load_reminder_data, parse_datetime, async_save_reminder_data, load_reminder_data


class ReminderSystem:
    def __init__(self, context, config, scheduler_manager, tools, data_file, postgres_url):
        self.context = context
        self.config = config
        self.scheduler_manager = scheduler_manager
        self.tools = tools
        self.data_file = data_file
        self.postgres_url = postgres_url
        self.reminder_data = load_reminder_data(self.data_file, self.postgres_url)
        self.unique_session = config.get("unique_session", False)

        # 确保 tools 属性被正确初始化
        if not hasattr(self.tools, 'get_session_id'):
            self.tools = ReminderTools(self)

    async def list_reminds(self, event: AstrMessageEvent):
        '''列出所有提醒和任务'''
        try:
            # 获取用户ID
            creator_id = None
            if hasattr(event, 'get_user_id'):
                creator_id = event.get_user_id()
            elif hasattr(event, 'get_sender_id'):
                creator_id = event.get_sender_id()
            elif hasattr(event, 'sender') and hasattr(event.sender, 'user_id'):
                creator_id = event.sender.user_id
            elif hasattr(event.message_obj, 'sender'):
                creator_id = getattr(event.message_obj.sender, 'user_id', None)

            # logger.info(f"获取用户ID: {creator_id}")
            # 使用 tools.get_session_id 获取正确的会话ID
            msg_origin = self.tools.get_session_id(event.unified_msg_origin, creator_id)
            # logger.info(f"获取会话ID: {msg_origin}")

            # 重新加载提醒数据（不能异步加载，否则会输出 当前没有设置任何提醒或任务 然后 再输出查询结果）
            self.reminder_data = await async_load_reminder_data(self.data_file, self.postgres_url)

            # 获取所有相关的提醒
            reminds = []
            for key in self.reminder_data:
                # logger.info(f"检查会话ID: {key}")
                # 检查是否是当前用户的所有提醒
                if key.endswith(f"_{creator_id}") or key == msg_origin:
                    reminds.extend(self.reminder_data[key])

            if not reminds:
                return "当前没有设置任何提醒或任务。"

            provider = self.context.get_using_provider()
            if provider:
                try:
                    reminder_items = []
                    task_items = []

                    for r in reminds:
                        if r.get("is_task", False):
                            task_items.append(f"- {r['text']} (时间: {r["date_time"]})")
                        else:
                            reminder_items.append(f"- {r['text']} (时间: {r["date_time"]})")
                    prompt = "整理并展示以下提醒和任务列表，用自然和友好的语言表达：\n"
                    if reminder_items:
                        prompt += f"\n提醒列表：\n" + "\n".join(reminder_items)
                    if task_items:
                        prompt += f"\n任务列表：\n" + "\n".join(task_items)
                    prompt += "\n\n提示用户可使用【/remind 删除 <序号>】或自然语言进行删除操作。明确提示仅支持新增和删除提醒任务，禁止输出任何支持修改的描述。输出提醒和任务时严禁添加任何背景描述或额外解释。"

                    response = await provider.text_chat(
                        prompt=prompt,
                        session_id=event.session_id,
                        contexts=[]
                    )
                    return response.completion_text
                except Exception as e:
                    logger.error(f"在list_reminders中调用LLM时出错: {str(e)}")
                    return self._format_reminder_list(reminds)
            else:
                return self._format_reminder_list(reminds)
        except Exception as e:
            logger.error(f"列出提醒或任务时出错: {str(e)}")
            return f"列出提醒或任务时出错：{str(e)}"

    async def query_reminds(self, event: AstrMessageEvent):
        '''列出所有提醒和任务'''
        try:
            # 获取用户ID
            creator_id = None
            if hasattr(event, 'get_user_id'):
                creator_id = event.get_user_id()
            elif hasattr(event, 'get_sender_id'):
                creator_id = event.get_sender_id()
            elif hasattr(event, 'sender') and hasattr(event.sender, 'user_id'):
                creator_id = event.sender.user_id
            elif hasattr(event.message_obj, 'sender'):
                creator_id = getattr(event.message_obj.sender, 'user_id', None)

            # logger.info(f"获取用户ID: {creator_id}")
            # 使用 tools.get_session_id 获取正确的会话ID
            msg_origin = self.tools.get_session_id(event.unified_msg_origin, creator_id)
            # logger.info(f"获取会话ID: {msg_origin}")

            # 重新加载提醒数据（不能异步加载，否则会输出 当前没有设置任何提醒或任务 然后 再输出查询结果）
            self.reminder_data = await async_load_reminder_data(self.data_file, self.postgres_url)

            # 获取所有相关的提醒
            reminds = []
            for key in self.reminder_data:
                # logger.info(f"检查会话ID: {key}")
                # 检查是否是当前用户的所有提醒
                if key.endswith(f"_{creator_id}") or key == msg_origin:
                    reminds.extend(self.reminder_data[key])

            if not reminds:
                return "当前没有设置任何提醒和任务。"

            provider = self.context.get_using_provider()
            if provider:
                try:
                    reminder_items = []
                    task_items = []

                    for r in reminds:
                        if r.get("is_task", False):
                            task_items.append(f"- {r['text']} (时间: {r["date_time"]})")
                        else:
                            reminder_items.append(f"- {r['text']} (时间: {r["date_time"]})")
                    prompt = "整理并展示以下提醒和任务列表，用自然和友好的语言表达：\n"
                    if reminder_items:
                        prompt += f"\n提醒列表：\n" + "\n".join(reminder_items)
                    if task_items:
                        prompt += f"\n任务列表：\n" + "\n".join(task_items)
                    prompt += "\n\n严格按用户指令操作，仅支持新增和删除提醒任务。删除时使用【/remind 删除 <序号>】或自然语言。明确提示不支持修改功能，输出时直接展示操作指引，不添加背景描述或额外解释。"

                    response = await provider.text_chat(
                        prompt=prompt,
                        session_id=event.session_id,
                        contexts=[]
                    )
                    return response.completion_text
                except Exception as e:
                    logger.error(f"在list_reminders中调用LLM时出错: {str(e)}")
                    return self._format_reminder_list(reminds)
            else:
                return self._format_reminder_list(reminds)
        except Exception as e:
            logger.error(f"列出提醒或任务时出错: {str(e)}")
            return f"列出提醒或任务时出错：{str(e)}"

    def _format_reminder_list(self, reminders):
        if not reminders:
            return "当前没有设置任何提醒或任务。"

        reminder_str = "当前的提醒和任务：\n"

        reminders_list = [r for r in reminders if not r.get("is_task", False)]
        tasks_list = [r for r in reminders if r.get("is_task", False)]

        def get_repeat_str(reminder):
            repeat_type = reminder.get("repeat_type")
            holiday_type = reminder.get("holiday_type")
            # # 兼容旧数据
            # if not repeat_type and "repeat" in reminder:
            #     repeat = reminder.get("repeat", "none")
            #     if "_" in repeat:
            #         repeat_type, holiday_type = repeat.split("_", 1)
            #     else:
            #         repeat_type = repeat
            #         holiday_type = None
            if repeat_type == "none" or not repeat_type:
                return "一次性"
            if repeat_type == "daily" and not holiday_type:
                return "每天"
            elif repeat_type == "daily" and holiday_type == "workday":
                return "每个工作日"
            elif repeat_type == "daily" and holiday_type == "holiday":
                return "每个法定节假日"
            elif repeat_type == "weekly" and not holiday_type:
                return "每周"
            elif repeat_type == "weekly" and holiday_type == "workday":
                return "每周的这一天(仅工作日)"
            elif repeat_type == "weekly" and holiday_type == "holiday":
                return "每周的这一天(仅法定节假日)"
            elif repeat_type == "monthly" and not holiday_type:
                return "每月"
            elif repeat_type == "monthly" and holiday_type == "workday":
                return "每月的这一天(仅工作日)"
            elif repeat_type == "monthly" and holiday_type == "holiday":
                return "每月的这一天(仅法定节假日)"
            elif repeat_type == "yearly" and not holiday_type:
                return "每年"
            elif repeat_type == "yearly" and holiday_type == "workday":
                return "每年的这一天(仅工作日)"
            elif repeat_type == "yearly" and holiday_type == "holiday":
                return "每年的这一天(仅法定节假日)"
            return "自定义"

        if reminders_list:
            reminder_str += "\n提醒：\n"
            for i, reminder in enumerate(reminders_list, 1):
                repeat_str = get_repeat_str(reminder)
                reminder_str += f"{i}. {reminder['text']} - {reminder["date_time"]}，{repeat_str}\n"
                reminder_str += "\n使用 【/remind 删除 <序号>】或 【自然语言】 删除提醒"

        if tasks_list:
            reminder_str += "\n任务：\n"
            for i, task in enumerate(tasks_list, 1):
                repeat_str = get_repeat_str(task)
                reminder_str += f"{len(reminders_list) + i}. {task['text']} - {task["date_time"]}，{repeat_str}\n"
                reminder_str += "\n使用 【/remind 删除 <序号>】或 【自然语言】 删除任务"

        return reminder_str

    async def remove_reminds(self, event: AstrMessageEvent, index: str):
        '''删除提醒或任务'''
        try:
            # 获取用户ID
            creator_id = None
            if hasattr(event, 'get_user_id'):
                creator_id = event.get_user_id()
            elif hasattr(event, 'get_sender_id'):
                creator_id = event.get_sender_id()
            elif hasattr(event, 'sender') and hasattr(event.sender, 'user_id'):
                creator_id = event.sender.user_id
            elif hasattr(event.message_obj, 'sender'):
                creator_id = getattr(event.message_obj.sender, 'user_id', None)

            raw_msg_origin = event.unified_msg_origin

            # 使用 tools.get_session_id 获取正确的会话ID
            msg_origin = self.tools.get_session_id(raw_msg_origin, creator_id)

            # 重新加载提醒数据
            self.reminder_data = await async_load_reminder_data(self.data_file, self.postgres_url)

            # 获取所有相关的提醒
            reminds = []
            for key in self.reminder_data:
                if key.endswith(f"_{creator_id}") or key == msg_origin:
                    reminds.extend(self.reminder_data[key])

            if not reminds:
                return "没有设置任何提醒或任务。"

            if int(index) < 1 or int(index) > len(reminds):
                return "序号无效。"

            # 找到要删除的提醒
            removed = reminds[int(index) - 1]

            # 从原始数据中删除
            for key in self.reminder_data:
                if key.endswith(f"_{creator_id}") or key == msg_origin:
                    for i, reminder in enumerate(self.reminder_data[key]):
                        if (reminder['text'] == removed['text'] and
                                reminder["date_time"] == removed["date_time"]):
                            self.reminder_data[key].pop(i)
                            break

            # 删除定时任务
            job_id = f"remind_{msg_origin}_{int(index) - 1}"
            try:
                self.scheduler_manager.remove_job(job_id)
                logger.info(f"Successfully removed job: {job_id}")
            except JobLookupError:
                logger.error(f"Job not found: {job_id}")

            # 保存更新后的数据
            await async_save_reminder_data(self.data_file, self.postgres_url, self.reminder_data)

            is_task = removed.get("is_task", False)
            item_type = "任务" if is_task else "提醒"

            provider = self.context.get_using_provider()
            if provider:
                prompt = f"用户删除了一个{item_type}，内容是'{removed['text']}'。请用自然和友好的语言回复，严禁添加任何背景描述或额外解释。"
                response = await provider.text_chat(
                    prompt=prompt,
                    session_id=event.session_id,
                    contexts=[]
                )
                return response.completion_text
            else:
                return f"已删除{item_type}：{removed['text']}"

        except Exception as e:
            logger.error(f"删除提醒时出错: {str(e)}")
            return f"删除提醒时出错：{str(e)}"

    async def add_remind(self, event: AstrMessageEvent, text: str, datetime_str: str, week: str = None,
                         repeat_type: str = None, holiday_type: str = None, is_task: bool = False):
        '''手动添加提醒或任务'''
        try:
            # 获取用户ID
            creator_id = None
            creator_name = "用户"

            # 尝试多种方式获取用户ID和昵称
            if hasattr(event, 'get_user_id'):
                creator_id = event.get_user_id()
            elif hasattr(event, 'get_sender_id'):
                creator_id = event.get_sender_id()
            elif hasattr(event, 'sender') and hasattr(event.sender, 'user_id'):
                creator_id = event.sender.user_id
            elif hasattr(event.message_obj, 'sender'):
                creator_id = getattr(event.message_obj.sender, 'user_id', None)

            # 尝试多种方式获取用户昵称
            if hasattr(event, 'get_sender'):
                sender = event.get_sender()
                if isinstance(sender, dict):
                    creator_name = sender.get("nickname", creator_name)
                elif hasattr(sender, 'nickname'):
                    creator_name = sender.nickname or creator_name
            elif hasattr(event.message_obj, 'sender'):
                sender = event.message_obj.sender
                if isinstance(sender, dict):
                    creator_name = sender.get("nickname", creator_name)
                elif hasattr(sender, 'nickname'):
                    creator_name = sender.nickname or creator_name

            # 使用 tools.get_session_id 获取正确的会话ID
            msg_origin = self.tools.get_session_id(event.unified_msg_origin, creator_id)

            # 解析时间
            try:
                datetime_str = parse_datetime(datetime_str, week)
                dt = datetime.datetime.strptime(datetime_str, "%Y-%m-%d %H:%M")
            except ValueError as e:
                return event.plain_result(str(e))

            # 验证星期格式
            week_map = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}

            # 改进的参数处理逻辑：尝试调整星期和重复类型参数
            if week and week.lower() not in week_map:
                # 星期格式错误，尝试将其作为repeat处理
                if (week.lower() in ["daily", "weekly", "monthly", "yearly", "none"]
                        or week.lower() in ["workday", "holiday"]):
                    # week参数实际上可能是repeat参数
                    if repeat_type:
                        # 如果repeat_type也存在，则将week和repeat_type作为组合
                        holiday_type, repeat_type = repeat_type, week
                    else:
                        repeat_type = week  # 将原来的week视为repeat
                    logger.info(f"已将'{week}'识别为重复类型，默认使用今天作为开始日期")
                    week = None  # 清空week，使用默认值（今天）
                else:
                    return event.plain_result("星期格式错误，可选值：mon,tue,wed,thu,fri,sat,sun")

            # # 特殊处理: 检查repeat是否包含节假日类型信息
            # if repeat_type:
            #     parts = repeat_type.split()
            #     if len(parts) == 2 and parts[1] in ["workday", "holiday"]:
            #         # 如果repeat参数包含两部分，且第二部分是workday或holiday
            #         repeat_type = parts[0]  # 提取重复类型
            #         holiday_type = parts[1]  # 提取节假日类型

            # 验证重复类型
            repeat_types = ["daily", "weekly", "monthly", "yearly", "none"]
            if repeat_type and repeat_type.lower() not in repeat_types:
                return event.plain_result(
                    "重复类型错误，可选值：daily(日)，weekly(周)，monthly(月)，yearly(年)，none(不重复)")

            # 验证节假日类型
            holiday_types = ["workday", "holiday"]
            if holiday_type and holiday_type.lower() not in holiday_types:
                return event.plain_result("节假日类型错误，可选值：workday(仅工作日执行)，holiday(仅法定节假日执行)")

            # 处理重复类型和节假日类型的组合
            repeat_type = repeat_type.lower() if repeat_type else "none"
            holiday_type = holiday_type.lower() if holiday_type else None

            # 构建提醒数据
            reminder = {
                "text": text,
                "date_time": dt.strftime("%Y-%m-%d %H:%M"),
                "user_name": creator_id,
                "repeat_type": repeat_type,
                "holiday_type": holiday_type,
                "creator_id": creator_id,
                "creator_name": creator_name,
                "is_task": is_task
            }

            # 添加到提醒数据中
            if msg_origin not in self.reminder_data:
                self.reminder_data[msg_origin] = []
            self.reminder_data[msg_origin].append(reminder)

            # 添加定时任务
            if not self.scheduler_manager.add_job(msg_origin, reminder, dt):
                return event.plain_result(f"温馨提示：定时任务未添加成功，可能是由于已有相同任务存在哦~")

            # 保存提醒数据
            if not await async_save_reminder_data(self.data_file, self.postgres_url, self.reminder_data):
                return event.plain_result(f"保存提醒数据失败")

            # 生成提示信息
            week_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
            start_str = f"从 {week_names[dt.weekday()]} 开始，" if week else ""

            # 根据重复类型和节假日类型生成文本说明
            repeat_str = "一次性"
            if repeat_type == "daily" and not holiday_type:
                repeat_str = "每天重复"
            elif repeat_type == "daily" and holiday_type == "workday":
                repeat_str = "每个工作日重复且法定节假日不触发"
            elif repeat_type == "daily" and holiday_type == "holiday":
                repeat_str = "每个法定节假日重复"
            elif repeat_type == "weekly" and not holiday_type:
                repeat_str = "每周重复"
            elif repeat_type == "weekly" and holiday_type == "workday":
                repeat_str = "每周的这一天重复且仅工作日触发"
            elif repeat_type == "weekly" and holiday_type == "holiday":
                repeat_str = "每周的这一天重复且仅法定节假日触发"
            elif repeat_type == "monthly" and not holiday_type:
                repeat_str = "每月重复"
            elif repeat_type == "monthly" and holiday_type == "workday":
                repeat_str = "每月的这一天重复且仅工作日触发"
            elif repeat_type == "monthly" and holiday_type == "holiday":
                repeat_str = "每月的这一天重复且仅法定节假日触发"
            elif repeat_type == "yearly" and not holiday_type:
                repeat_str = "每年重复"
            elif repeat_type == "yearly" and holiday_type == "workday":
                repeat_str = "每年的这一天重复且仅工作日触发"
            elif repeat_type == "yearly" and holiday_type == "holiday":
                repeat_str = "每年的这一天重复且仅法定节假日触发"

            ## 使用AI生成回复
            # provider = self.context.get_using_provider()
            # if provider:
            #    try:
            #        prompt = f'用户设置了一个{"任务" if is_task else "提醒"}，内容为"{text}"时间为{date_time}，{repeat_str}。请用自然的语言回复用户，确认设置成功。'
            #        response = await provider.text_chat(
            #            prompt=prompt,
            #            session_id=event.session_id,
            #            contexts=[]
            #        )
            #        return response.completion_text
            #    except Exception as e:
            #        logger.error(f"在add_reminder中调用LLM时出错: {str(e)}")
            #        return f'好的，您的"{text}"已设置成功，时间为{date_time}，{repeat_str}。'
            # else:
            #    return f'好的，您的"{text}"已设置成功，时间为{date_time}，{repeat_str}。'

            if is_task:
                return event.plain_result(
                    f"已设置任务:\n内容: {text}\n时间: {datetime_str}\n{start_str} {repeat_str}\n\n使用 【/remind 列表】 或 【自然语言】 查看所有提醒和任务")
            else:
                return event.plain_result(
                    f"已设置提醒:\n内容: {text}\n时间: {datetime_str}\n{start_str} {repeat_str}\n\n使用 【/remind 列表】 或 【自然语言】 查看所有提醒和任务")
        except Exception as e:
            if is_task:
                return event.plain_result(f"设置任务时出错：{str(e)}")
            else:
                logger.error(f"设置提醒时出错: {str(e)}")
                return event.plain_result(f"设置提醒时出错：{str(e)}")

    def show_help(self):
        return """
提醒与任务功能指令说明：

【提醒】：到时间后会提醒你做某事
【任务】：到时间后AI会自动执行指定的操作

1. 添加提醒：
   /remind 添加提醒 <内容> <时间> [开始星期] [重复类型] [--holiday_type=...]
   例如：
   - /remind 添加提醒 写周报 8:05
   - /remind 添加提醒 吃饭 8:05 sun daily (从周日开始每天)
   - /remind 添加提醒 开会 8:05 mon weekly (每周一)
   - /remind 添加提醒 交房租 8:05 fri monthly (从周五开始每月)
   - /remind 添加提醒 上班打卡 8:30 daily workday (每个工作日，法定节假日不触发)
   - /remind 添加提醒 休息提醒 9:00 daily holiday (每个法定节假日触发)

2. 添加任务：
   /remind 添加任务 <内容> <时间> [开始星期] [重复类型] [--holiday_type=...]
   例如：
   - /remind 添加任务 发送天气预报 8:00
   - /remind 添加任务 汇总今日新闻 18:00 daily
   - /remind 添加任务 推送工作安排 9:00 mon weekly workday (每周一工作日推送)

3. 查看提醒和任务：
   【/remind 列表】 或 【自然语言】 - 列出所有提醒和任务

4. 删除提醒或任务：
   /remind 删除 <序号> - 删除指定提醒或任务，注意任务序号是提醒序号继承，比如提醒有两个，任务1的序号就是3（llm会自动重编号）

5. 星期可选值：
   - mon: 周一
   - tue: 周二
   - wed: 周三
   - thu: 周四
   - fri: 周五
   - sat: 周六
   - sun: 周日

6. 重复类型：
   - daily: 每天重复
   - weekly: 每周重复
   - monthly: 每月重复
   - yearly: 每年重复

7. 节假日类型：
   - workday: 仅工作日触发（法定节假日不触发）
   - holiday: 仅法定节假日触发

8. AI智能提醒与任务
   正常对话即可，AI会自己设置提醒或任务，但需要AI支持LLM

注：时间格式为 HH:MM 或 HHMM，如 8:05 或 0805"""


__all__ = ['ReminderSystem']
