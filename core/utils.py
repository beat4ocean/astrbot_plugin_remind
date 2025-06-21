import datetime
import json
import os
import re

import aiohttp
import psycopg2
from astrbot.api import logger
from psycopg2.extras import DictCursor

from .database import PostgresManager

# 初始化PostgreSQL管理器
postgres_manager = None


def parse_datetime(datetime_str: str, week: str = None) -> str:
    """
    解析各种格式的时间字符串，并根据需要计算未来的日期时间。

    这个增强版本支持更灵活的时间输入格式，并提供了更清晰的错误处理。

    支持的格式:
    - 标准格式: "08:20", "8:20", "2025-10-01 08:20:00"
    - 无分隔符: "0820", "820" (会补全为 "0820")
    - 中文格式: "8点20", "8点" (无分钟数则默认为0分)
    - 上下午指示: "上午8点20", "下午3点", "晚上8点"

    Args:
        datetime_str: 时间字符串。
        week: 星期几，可选值，不区分大小写和前后空格。
              中文: 周一, 周二, 周三, 周四, 周五, 周六, 周日
              英文: mon, tue, wed, thu, fri, sat, sun

    Returns:
        一个格式为 'YYYY-MM-DD HH:MM' 的未来日期时间字符串。

    Raises:
        ValueError: 如果时间或星期格式无效或无法解析。
    """
    try:
        # --- 1. 初始化和预处理 ---
        datetime_str = datetime_str.strip()

        # --- 2. 解析时间字符串 ---
        # 尝试匹配 "HHMM" 或 "HMM" (如 "820"、"0820") 格式
        if datetime_str.isdigit() and len(datetime_str) in [3, 4]:
            hhmm_str = datetime_str.zfill(4)  # "820" -> "0820"
            hour = int(hhmm_str[:2])
            minute = int(hhmm_str[2:])
        # 尝试匹配 "%Y-%m-%d %H:%M" 格式
        elif ':' in datetime_str and "-" in datetime_str and len(datetime_str) == 16:
            dt = datetime.datetime.strptime(datetime_str, "%Y-%m-%d %H:%M")
            hour = dt.hour
            minute = dt.minute
        # 尝试匹配 "%Y-%m-%d %H:%M:%S" 格式
        elif ':' in datetime_str and "-" in datetime_str and len(datetime_str) == 19:
            dt = datetime.datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S")
            hour = dt.hour
            minute = dt.minute
        else:
            # 否则，使用正则表达式匹配更复杂的格式
            # 模式解释:
            # ^...$            - 匹配整个字符串
            # (?P<am_pm>...)   - 捕获组: 早上/上午/下午/晚上 (可选)
            # (?P<hour>\d{1,2}) - 捕获组: 小时
            # (?:...)?         - 非捕获组: 分钟部分 (可选)
            pattern = re.compile(
                r"^(?P<am_pm>早上|上午|下午|晚上|凌晨)?\s*"
                r"(?P<hour>\d{1,2})\s*"
                r"(?:[:：点]\s*(?P<minute>\d{1,2})?)?\s*$"
            )
            match = pattern.match(datetime_str)

            if not match:
                raise ValueError(f"无法识别的时间格式: '{datetime_str}'")

            groups = match.groupdict()
            am_pm = groups.get('am_pm')
            hour = int(groups['hour'])
            minute = int(groups['minute']) if groups.get('minute') else 0  # 修改这里

            # --- 3. 根据上午/下午调整小时 ---
            if am_pm in ['下午', '晚上']:
                if 1 <= hour < 12:
                    hour += 12
            elif am_pm in ['凌晨']:
                if hour == 12 or hour == 24:
                    hour = 0

        # --- 4. 验证时间范围 ---
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"时间值超出范围: {hour}:{minute}")

        # --- 5. 创建初始 datetime 对象 ---
        today = datetime.datetime.now()
        dt = today.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # --- 6. 如果指定了星期，计算目标日期 ---
        if week:
            week_map = {
                '周一': 0, 'mon': 0, 'monday': 0,
                '周二': 1, 'tue': 1, 'tuesday': 1,
                '周三': 2, 'wed': 2, 'wednesday': 2,
                '周四': 3, 'thu': 3, 'thursday': 3,
                '周五': 4, 'fri': 4, 'friday': 4,
                '周六': 5, 'sat': 5, 'saturday': 5,
                '周日': 6, 'sun': 6, 'sunday': 6,
            }
            week_clean = week.strip().lower()
            if week_clean not in week_map:
                raise ValueError(f"无效的星期格式: '{week}'")

            target_weekday = week_map[week_clean]
            current_weekday = dt.weekday()  # Monday is 0 and Sunday is 6
            days_ahead = target_weekday - current_weekday

            # 如果目标日期在今天之前，或者就是今天但时间已过，则安排在下周
            if days_ahead < 0 or (days_ahead == 0 and dt <= today):
                days_ahead += 7

            dt += datetime.timedelta(days=days_ahead)

        # --- 7. 如果未指定星期且时间已过，则安排在明天 ---
        elif dt <= today:
            dt += datetime.timedelta(days=1)
            logger.info(f"设置的时间已过，自动调整为明天: {dt.strftime('%Y-%m-%d %H:%M')}")

        # --- 8. 返回格式化结果 ---
        return dt.strftime("%Y-%m-%d %H:%M")

    except (ValueError, TypeError) as e:
        raise ValueError("输入错误！：" + str(e)) from e


def is_outdated(reminder: dict) -> bool:
    '''检查提醒是否过期'''
    if "date_time" in reminder and reminder["date_time"]:  # 确保datetime存在且不为空
        try:
            reminder_time = datetime.datetime.strptime(reminder["date_time"], "%Y-%m-%d %H:%M")
            current_time = datetime.datetime.now()
            # 如果提醒时间已经过去，则认为过期
            is_expired = reminder_time <= current_time
            if is_expired:
                logger.info(f"提醒已过期: {reminder.get('text', '')} 时间: {reminder["date_time"]}")
            return is_expired
        except ValueError:
            # 如果日期格式不正确，记录错误并返回False
            logger.error(f"提醒的日期时间格式错误: {reminder.get("date_time", '')}")
            return False
    return False


# async def close_postgres_manager():
#     """关闭PostgreSQL连接池"""
#     global postgres_manager
#
#     if postgres_manager:
#         await postgres_manager.close_pool()
#         postgres_manager = None


def load_reminder_data(data_file: str, postgres_url: str) -> dict:
    '''首次加载提醒数据：

    - 如果设置了postgres_url，从PostgreSQL加载
    - 否则从本地JSON文件加载
    '''
    global postgres_manager

    # 从 postgres 获取数据
    if postgres_url is not None and postgres_url != "":
        logger.info("从PostgreSQL同步加载数据")
        return load_postgres_data(postgres_url)
    else:
        logger.info("从本地JSON文件加载数据")
        # 从本地JSON文件获取数据
        return load_json_data(data_file)


def load_postgres_data(postgres_url):
    result = {}

    # 建立数据库连接
    conn = psycopg2.connect(postgres_url)
    try:
        # 使用 DictCursor 以便通过列名访问数据
        with conn.cursor(cursor_factory=DictCursor) as cursor:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS reminders
            (
                id           SERIAL PRIMARY KEY,
                session_id   TEXT      NOT NULL,
                text         TEXT      NOT NULL,
                date_time    TIMESTAMP NOT NULL,
                user_name    TEXT,
                repeat_type  TEXT,
                holiday_type TEXT,
                creator_id   TEXT,
                creator_name TEXT,
                is_task      BOOLEAN   DEFAULT FALSE,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_session_id ON reminders (session_id);
            CREATE INDEX IF NOT EXISTS idx_creator_id ON reminders (creator_id);
                           """)
            # 执行查询
            cursor.execute('SELECT * FROM reminders ORDER BY date_time')
            rows = cursor.fetchall()

            for row in rows:
                session_id = row['session_id']

                if session_id not in result:
                    result[session_id] = []

                # 将数据库记录转换为字典格式
                reminder = {
                    'id': row['id'],
                    'text': row['text'],
                    "date_time": row["date_time"].strftime("%Y-%m-%d %H:%M"),
                    'user_name': row['user_name'],
                    'repeat_type': row['repeat_type'],
                    'holiday_type': row['holiday_type'],
                    'creator_id': row['creator_id'],
                    'creator_name': row['creator_name'],
                    'is_task': row['is_task']
                }

                result[session_id].append(reminder)

    finally:
        # 关闭连接
        conn.close()

    return result


def load_json_data(data_file):
    try:
        # 确保数据目录存在
        data_dir = os.path.dirname(data_file)
        if data_dir and not os.path.exists(data_dir):
            os.makedirs(data_dir, exist_ok=True)
            logger.info(f"创建数据目录: {data_dir}")

        # 如果文件不存在或为空，创建新的空数据文件
        if not os.path.exists(data_file) or os.path.getsize(data_file) == 0:
            with open(data_file, "w", encoding='utf-8') as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
            return {}

        # 尝试读取并解析JSON数据
        try:
            with open(data_file, "r", encoding='utf-8') as f:
                content = f.read().strip()
                if not content:  # 如果文件为空
                    return {}
                data = json.loads(content)
                if not isinstance(data, dict):
                    logger.error("提醒数据格式错误，重置为空字典")
                    return {}
                return data
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析错误: {str(e)}，重置提醒数据")
            # 备份损坏的文件
            if os.path.exists(data_file):
                backup_file = f"{data_file}.bak"
                try:
                    os.rename(data_file, backup_file)
                    logger.info(f"已备份损坏的数据文件到: {backup_file}")
                except Exception as e:
                    logger.error(f"备份数据文件失败: {str(e)}")

            # 创建新的空数据文件
            with open(data_file, "w", encoding='utf-8') as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
            return {}

    except Exception as e:
        logger.error(f"加载提醒数据失败: {str(e)}")
        return {}


async def init_postgres_manager(postgres_url=None):
    """异步初始化PostgreSQL管理器

    Args:
        postgres_url: PostgreSQL连接字符串
    """
    global postgres_manager

    if postgres_manager is None:
        postgres_manager = PostgresManager(postgres_url)
        await postgres_manager.init_pool()

    return postgres_manager


async def async_load_reminder_data(data_file: str, postgres_url: str) -> dict:
    '''异步加载提醒数据

    - 如果设置了postgres_url，从PostgreSQL加载
    - 否则从本地JSON文件加载
    '''
    global postgres_manager

    # 从 postgres 获取数据
    if postgres_url is not None and postgres_url != "":
        logger.info("检测到PostgreSQL配置，将异步加载数据")
        try:
            if postgres_manager is None:
                postgres_manager = await init_postgres_manager(postgres_url)
            return await postgres_manager.load_reminder_data()
        except Exception as e:
            logger.error(f"加载PostgreSQL数据失败: {str(e)}")
            return {}

    # 从本地JSON文件获取数据
    return load_json_data(data_file)


async def async_save_reminder_data(data_file: str, postgres_url: str, reminder_data: dict) -> bool:
    '''保存提醒数据
    
    - 如果设置了postgres_url，保存到PostgreSQL
    - 否则保存到本地JSON文件
    '''
    global postgres_manager

    if postgres_url is not None and postgres_url != "":
        try:
            # 如果postgres_manager未初始化，执行初始化
            if postgres_manager is None:
                postgres_manager = await init_postgres_manager(postgres_url)

            # 保存到PostgreSQL
            return await postgres_manager.async_save_reminder_data(reminder_data)
        except Exception as e:
            logger.error(f"保存数据到PostgreSQL失败: {str(e)}")
            return False

    # 以下是原有的JSON文件保存逻辑
    try:
        # 确保数据目录存在
        data_dir = os.path.dirname(data_file)
        if data_dir and not os.path.exists(data_dir):
            os.makedirs(data_dir, exist_ok=True)
            logger.info(f"创建数据目录: {data_dir}")

        # 在保存前清理过期的一次性任务和无效数据
        for group in list(reminder_data.keys()):
            # 只清理过期的一次性任务，兼容新旧结构
            def is_one_time(r):
                repeat_type = r.get("repeat_type")
                # if not repeat_type and "repeat" in r:
                #     repeat = r.get("repeat", "none")
                #     if "_" in repeat:
                #         repeat_type, _ = repeat.split("_", 1)
                #     else:
                #         repeat_type = repeat
                return repeat_type in [None, "none", "不重复"]

            reminder_data[group] = [
                r for r in reminder_data[group]
                if "date_time" in r and r["date_time"] and not (is_one_time(r) and is_outdated(r))
            ]
            # 如果群组没有任何提醒了，删除这个群组的条目
            if not reminder_data[group]:
                del reminder_data[group]

        # 确保数据是有效的字典格式
        if not isinstance(reminder_data, dict):
            logger.error("提醒数据格式错误，重置为空字典")
            reminder_data = {}

        # 保存数据
        with open(data_file, "w", encoding='utf-8') as f:
            json.dump(reminder_data, f, ensure_ascii=False, indent=2)

        logger.info(f"成功保存提醒数据到: {data_file}")
        return True

    except Exception as e:
        logger.error(f"保存提醒数据失败: {str(e)}")
        return False


# 法定节假日相关功能
class HolidayManager:
    def __init__(self):
        # 确保目录存在
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                "astrbot_plugin_remind")
        os.makedirs(os.path.join(data_dir, "holiday_data"), exist_ok=True)
        self.holiday_cache_file = os.path.join(data_dir, "holiday_data", "holiday_cache.json")
        self.holiday_data = self._load_holiday_data()

    def _load_holiday_data(self) -> dict:
        """加载节假日数据缓存"""
        if not os.path.exists(self.holiday_cache_file):
            return {}

        try:
            with open(self.holiday_cache_file, "r", encoding='utf-8') as f:
                data = json.load(f)

            # 检查数据是否过期（缓存超过30天更新一次）
            if "last_update" in data:
                last_update = datetime.datetime.fromisoformat(data["last_update"])
                now = datetime.datetime.now()
                if (now - last_update).days > 30:
                    logger.info("节假日数据缓存已过期，需要更新")
                    return {}

            return data
        except Exception as e:
            logger.error(f"加载节假日数据缓存失败: {e}")
            return {}

    async def _save_holiday_data(self):
        """保存节假日数据缓存"""
        try:
            # 添加最后更新时间
            self.holiday_data["last_update"] = datetime.datetime.now().isoformat()

            with open(self.holiday_cache_file, "w", encoding='utf-8') as f:
                json.dump(self.holiday_data, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存节假日数据缓存失败: {e}")

    async def fetch_holiday_data(self, year: int = None) -> dict:
        """获取指定年份的节假日数据
        
        Args:
            year: 年份，默认为当前年份
            
        Returns:
            dict: 节假日数据，格式为 {日期字符串: 布尔值}
                  布尔值说明: True-法定节假日, False-调休工作日（需要补班的周末）
        """
        if year is None:
            year = datetime.datetime.now().year

        # 如果缓存中已有数据则直接返回
        year_key = str(year)
        if year_key in self.holiday_data and "data" in self.holiday_data[year_key]:
            return self.holiday_data[year_key]["data"]

        # 否则从API获取
        try:
            # 使用 http://timor.tech/api/holiday/year/{year} 接口获取数据
            url = f"http://timor.tech/api/holiday/year/{year}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.error(f"获取节假日数据失败，状态码: {response.status}")
                        return {}

                    json_data = await response.json()

                    if json_data.get("code") != 0:
                        logger.error(f"获取节假日数据失败: {json_data.get('msg')}")
                        return {}

                    holiday_data = {}
                    for date_str, info in json_data.get("holiday", {}).items():
                        holiday_data[date_str] = info.get("holiday")

                    # 缓存数据
                    if year_key not in self.holiday_data:
                        self.holiday_data[year_key] = {}
                    self.holiday_data[year_key]["data"] = holiday_data
                    await self._save_holiday_data()

                    return holiday_data
        except Exception as e:
            logger.error(f"获取节假日数据出错: {e}")
            return {}

    async def is_holiday(self, date: datetime.datetime = None) -> bool:
        """判断指定日期是否为法定节假日
        
        Args:
            date: 日期，默认为当天
            
        Returns:
            bool: 是否为法定节假日
        """
        if date is None:
            date = datetime.datetime.now()

        year = date.year
        # 获取完整日期和不含年份的日期
        full_date_str = date.strftime("%Y-%m-%d")
        short_date_str = date.strftime("%m-%d")

        # 获取该年份的节假日数据
        holiday_data = await self.fetch_holiday_data(year)

        # 判断是否在节假日数据中，使用不含年份的短日期格式
        if short_date_str in holiday_data:
            # 如果值为True，表示法定节假日
            is_holiday = holiday_data[short_date_str] == True
            return is_holiday

        # 如果不在特殊日期列表中，则根据是否为周末判断
        if date.weekday() >= 5:  # 5和6分别是周六和周日
            return True

        return False

    async def is_workday(self, date: datetime.datetime = None) -> bool:
        """判断指定日期是否为工作日
        
        Args:
            date: 日期，默认为当天
            
        Returns:
            bool: 是否为工作日
        """
        if date is None:
            date = datetime.datetime.now()

        year = date.year
        # 获取完整日期和不含年份的日期
        full_date_str = date.strftime("%Y-%m-%d")
        short_date_str = date.strftime("%m-%d")

        # 获取该年份的节假日数据
        holiday_data = await self.fetch_holiday_data(year)

        # 判断是否在节假日数据中，使用不含年份的短日期格式
        if short_date_str in holiday_data:
            # 如果值为False，表示调休工作日（需要补班的周末）
            # 如果值为True，表示法定节假日
            is_workday = holiday_data[short_date_str] == False
            return is_workday

        # 如果是周末且不在节假日数据中，则不是工作日
        if date.weekday() >= 5:  # 5和6分别是周六和周日
            return False

        return True


if __name__ == '__main__':
    datetime_str = "800"
    # datetime_str = "8：00"
    # datetime_str = "8:00"
    # datetime_str = "2024-06-01 08:00"
    print(parse_datetime(datetime_str))
