import datetime
from typing import Union
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from astrbot.api import logger
from .utils import async_save_reminder_data, async_load_reminder_data


class ReminderTools:
    def __init__(self, star_instance):
        self.star = star_instance
        self.context = star_instance.context
        self.reminder_data = star_instance.reminder_data
        self.data_file = star_instance.data_file
        self.postgres_url = star_instance.postgres_url
        self.scheduler_manager = star_instance.scheduler_manager
        self.unique_session = star_instance.unique_session

        # 确保 reminder_data 是一个字典
        if not isinstance(self.reminder_data, dict):
            self.reminder_data = {}
            self.star.reminder_data = self.reminder_data

    def get_session_id(self, msg_origin, creator_id=None):
        """
        根据会话隔离设置，获取正确的会话ID
        
        Args:
            msg_origin: 原始会话ID
            creator_id: 创建者ID
            
        Returns:
            str: 处理后的会话ID
        """
        if not self.unique_session:
            return msg_origin

        # 如果启用了会话隔离，并且有创建者ID，则在会话ID中添加用户标识
        if creator_id:
            # 在群聊环境中添加用户ID
            if (":GroupMessage:" in msg_origin or
                    "@chatroom" in msg_origin or
                    ":ChannelMessage:" in msg_origin):
                # 分割会话ID并在末尾添加用户标识
                parts = msg_origin.rsplit(":", 1)
                if len(parts) == 2:
                    return f"{parts[0]}:{parts[1]}_{creator_id}"
            # 在私聊环境中添加用户ID
            elif ":PrivateMessage:" in msg_origin:
                # 分割会话ID并在末尾添加用户标识
                parts = msg_origin.rsplit(":", 1)
                if len(parts) == 2:
                    return f"{parts[0]}:{parts[1]}_{creator_id}"
            # 其他类型的消息，直接添加用户标识
            else:
                return f"{msg_origin}_{creator_id}"

        return msg_origin

    async def set_remind(self, event: Union[AstrMessageEvent, Context], text: str, date_time: str,
                         repeat_type: str = None, holiday_type: str = None):
        '''设置一个提醒
        
        Args:
            event:
            text(string): 提醒内容
            date_time(string): 提醒时间，格式为 %Y-%m-%d %H:%M
            repeat_type(string): 重复类型，可选值：daily(每天)，weekly(每周)，monthly(每月)，yearly(每年)，none(不重复)
            holiday_type(string): 可选，节假日类型：workday(仅工作日执行)，holiday(仅法定节假日执行)
        '''
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

            # 使用 get_session_id 获取正确的会话ID
            msg_origin = self.get_session_id(event.unified_msg_origin, creator_id)

            # 解析时间
            try:
                dt = datetime.datetime.strptime(date_time, "%Y-%m-%d %H:%M")
            except ValueError as e:
                return event.plain_result(str(e))

            # 特殊处理: 检查repeat是否包含节假日类型信息
            if repeat_type:
                parts = repeat_type.split()
                if len(parts) == 2 and parts[1] in ["workday", "holiday"]:
                    # 如果repeat参数包含两部分，且第二部分是workday或holiday
                    repeat_type = parts[0]  # 提取重复类型
                    holiday_type = parts[1]  # 提取节假日类型

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
                "date_time": date_time,
                "user_name": creator_id,
                "repeat_type": repeat_type,
                "holiday_type": holiday_type,
                "creator_id": creator_id,
                "creator_name": creator_name,
                "is_task": False
            }

            # 添加到提醒数据中
            if msg_origin not in self.reminder_data:
                self.reminder_data[msg_origin] = []
            self.reminder_data[msg_origin].append(reminder)

            # 添加定时任务
            if not self.scheduler_manager.add_job(msg_origin, reminder, dt):
                return event.plain_result(f"添加定时任务失败")

            # 保存提醒数据
            if not await async_save_reminder_data(self.data_file, self.postgres_url, self.reminder_data):
                return event.plain_result(f"保存提醒数据失败")

            # 构建提示信息
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

            return f"已设置提醒:\n内容: {text}\n时间: {date_time} {repeat_str}\n\n使用 【/remind 列表】 或 【自然语言】 查看所有提醒"

        except Exception as e:
            logger.error(f"设置提醒时出错: {str(e)}")
            return f"设置提醒时出错：{str(e)}"

    async def set_task(self, event: Union[AstrMessageEvent, Context], text: str, date_time: str,
                       repeat_type: str = None, holiday_type: str = None):
        '''设置一个任务，到时间后会让AI执行该任务
        
        Args:
            text(string): 任务内容，AI将执行的操作
            date_time(string): 任务执行时间，格式为 %Y-%m-%d %H:%M
            repeat_type(string): 重复类型，可选值：daily(每天)，weekly(每周)，monthly(每月)，yearly(每年)，none(不重复)
            holiday_type(string): 可选，节假日类型：workday(仅工作日执行)，holiday(仅法定节假日执行)
        '''
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

            # 使用 get_session_id 获取正确的会话ID
            msg_origin = self.get_session_id(event.unified_msg_origin, creator_id)

            # 解析时间
            try:
                dt = datetime.datetime.strptime(date_time, "%Y-%m-%d %H:%M")
            except ValueError as e:
                return event.plain_result(str(e))

            # 特殊处理: 检查repeat是否包含节假日类型信息
            if repeat_type:
                parts = repeat_type.split()
                if len(parts) == 2 and parts[1] in ["workday", "holiday"]:
                    # 如果repeat参数包含两部分，且第二部分是workday或holiday
                    repeat_type = parts[0]  # 提取重复类型
                    holiday_type = parts[1]  # 提取节假日类型

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

            # 构建任务数据
            task = {
                "text": text,
                "date_time": date_time,
                "user_name": creator_id,
                "repeat_type": repeat_type,
                "holiday_type": holiday_type,
                "creator_id": creator_id,
                "creator_name": creator_name,
                "is_task": True  # 标记为任务，不是提醒
            }

            # 添加任务到提醒数据中
            if msg_origin not in self.reminder_data:
                self.reminder_data[msg_origin] = []
            self.reminder_data[msg_origin].append(task)

            # 添加定时任务
            if not self.scheduler_manager.add_job(msg_origin, task, dt):
                return event.plain_result(f"添加定时任务失败")

            # 保存提醒数据
            if not await async_save_reminder_data(self.data_file, self.postgres_url, self.reminder_data):
                return event.plain_result(f"保存提醒数据失败")

            # 构建提示信息
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

            return f"已设置任务:\n内容: {text}\n时间: {date_time} {repeat_str}\n\n使用 【/remind 列表】 或 【自然语言】 查看所有任务"

        except Exception as e:
            logger.error(f"设置任务时出错: {str(e)}")
            return f"设置任务时出错：{str(e)}"

    async def delete_remind(self, event: AstrMessageEvent, index: str):
        '''删除符合条件的提醒或任务
        
        Args:
            index(string): 需要删除的提醒或任务的数字序号,例如：1
        '''
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
            msg_origin = self.get_session_id(raw_msg_origin, creator_id)

            # 重新加载提醒数据
            self.reminder_data = await async_load_reminder_data(self.data_file, self.postgres_url)

            # 获取所有相关的提醒
            reminds = []
            for key in self.reminder_data:
                if key.endswith(f"_{creator_id}") or key == msg_origin:
                    reminds.extend(self.reminder_data[key])

            if not reminds:
                return "没有设置任何提醒和任务。"

            if int(index) < 1 or int(index) > len(reminds):
                return "序号无效。"

            # 找到要删除的提醒
            to_delete_remind = reminds[int(index) - 1]

            # 从原始数据中删除
            for key in self.reminder_data:
                if key.endswith(f"_{creator_id}") or key == msg_origin:
                    for i, reminder in enumerate(self.reminder_data[key]):
                        if (reminder['text'] == to_delete_remind['text'] and
                                reminder["date_time"] == to_delete_remind["date_time"]):
                            self.reminder_data[key].pop(i)
                            break

            # 删除定时提醒
            job_id = f"remind_{msg_origin}_{int(index) - 1}"
            try:
                self.scheduler_manager.remove_job(job_id)
                logger.info(f"Successfully delete job: {job_id}")
            except Exception as e:
                logger.error(f"Job not found: {job_id}")

            # 保存更新后的数据
            await async_save_reminder_data(self.data_file, self.postgres_url, self.reminder_data)

            is_task = to_delete_remind.get("is_task", False)
            item_type = "任务" if is_task else "提醒"

            provider = self.context.get_using_provider()
            if provider:
                prompt = f"用户删除了一个{item_type}，内容是'{to_delete_remind['text']}'。请用自然的语言回复删除操作。直接发出对话内容，不要有其他的背景描述。"
                response = await provider.text_chat(
                    prompt=prompt,
                    session_id=event.session_id,
                    contexts=[]
                )
                return response.completion_text
            else:
                return f"已删除{item_type}：{to_delete_remind['text']}"

        except Exception as e:
            logger.error(f"删除提醒或任务时出错: {str(e)}")
            return f"删除提醒或任务时出错：{str(e)}"
