import os
import json
import datetime
import asyncpg
from astrbot.api import logger


class PostgresManager:
    def __init__(self, postgres_url=None):
        """初始化PostgreSQL连接管理器
        
        Args:
            postgres_url: PostgreSQL连接字符串，格式：
                postgresql://username:password@host:port/database
        """
        self.postgres_url = postgres_url
        self.pool = None

    async def init_pool(self):
        """初始化连接池"""
        if not self.postgres_url:
            raise ValueError("未提供PostgreSQL连接字符串")

        try:
            self.pool = await asyncpg.create_pool(self.postgres_url)
            logger.info("PostgreSQL连接池创建成功")

            # 确保表已创建
            await self.ensure_tables()
            return True
        except Exception as e:
            logger.error(f"创建PostgreSQL连接池失败: {str(e)}")
            return False

    async def close_pool(self):
        """关闭连接池"""
        if self.pool:
            await self.pool.close()
            logger.info("PostgreSQL连接池已关闭")

    async def ensure_tables(self):
        """确保数据库表已创建"""
        async with self.pool.acquire() as conn:
            # 创建提醒表
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS reminders (
                    id SERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    date_time TIMESTAMP NOT NULL,
                    user_name TEXT,
                    repeat_type TEXT,
                    holiday_type TEXT,
                    creator_id TEXT,
                    creator_name TEXT,
                    is_task BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_session_id ON reminders(session_id);
                CREATE INDEX IF NOT EXISTS idx_creator_id ON reminders(creator_id);
            ''')
            logger.info("已确保数据库表结构")

    async def load_reminder_data(self) -> dict:
        """从PostgreSQL加载提醒数据
        
        Returns:
            dict: 与原JSON格式兼容的提醒数据结构
        """
        try:
            if not self.pool:
                await self.init_pool()

            result = {}
            async with self.pool.acquire() as conn:
                rows = await conn.fetch('SELECT * FROM reminders ORDER BY date_time')

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

            logger.info(f"从PostgreSQL加载了提醒数据: {len(result)} 个会话")
            return result
        except Exception as e:
            logger.error(f"从PostgreSQL加载提醒数据失败: {str(e)}")
            return {}

    async def save_reminder_data(self, reminder_data: dict) -> bool:
        """保存提醒数据到PostgreSQL
        
        Args:
            reminder_data: 提醒数据字典
            
        Returns:
            bool: 保存成功返回True，否则False
        """
        try:
            if not self.pool:
                await self.init_pool()

            async with self.pool.acquire() as conn:
                # 开始事务
                async with conn.transaction():
                    # 清空现有数据
                    await conn.execute('DELETE FROM reminders')

                    # 批量插入新数据
                    for session_id, reminders in reminder_data.items():
                        for reminder in reminders:
                            # 跳过无效的提醒
                            if "date_time" not in reminder or not reminder["date_time"]:
                                continue

                            # 解析日期时间字符串
                            try:
                                dt = datetime.datetime.strptime(reminder["date_time"], "%Y-%m-%d %H:%M")
                            except ValueError:
                                logger.error(f"无效的日期时间格式: {reminder.get("date_time", '')}")
                                continue

                            # 插入数据
                            await conn.execute('''
                                               INSERT INTO reminders
                                               (session_id, text, date_time, user_name, repeat_type,
                                                holiday_type, creator_id, creator_name, is_task)
                                               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                                               ''', session_id, reminder['text'], dt,
                                               reminder.get('user_name'), reminder.get('repeat_type'),
                                               reminder.get('holiday_type'), reminder.get('creator_id'),
                                               reminder.get('creator_name'), reminder.get('is_task', False))

            logger.info(f"成功保存提醒数据到PostgreSQL")
            return True
        except Exception as e:
            logger.error(f"保存提醒数据到PostgreSQL失败: {str(e)}")
            return False

    async def add_reminder(self, session_id: str, reminder: dict) -> bool:
        """添加单个提醒到数据库
        
        Args:
            session_id: 会话ID
            reminder: 提醒数据
            
        Returns:
            bool: 添加成功返回True，否则False
        """
        try:
            if not self.pool:
                await self.init_pool()

            # 跳过无效的提醒
            if "date_time" not in reminder or not reminder["date_time"]:
                return False

            # 解析日期时间字符串
            try:
                dt = datetime.datetime.strptime(reminder["date_time"], "%Y-%m-%d %H:%M")
            except ValueError:
                logger.error(f"无效的日期时间格式: {reminder.get("date_time", '')}")
                return False

            async with self.pool.acquire() as conn:
                # 插入数据
                await conn.execute('''
                                   INSERT INTO reminders
                                   (session_id, text, date_time, user_name, repeat_type,
                                    holiday_type, creator_id, creator_name, is_task)
                                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                                   ''', session_id, reminder['text'], dt,
                                   reminder.get('user_name'), reminder.get('repeat_type'),
                                   reminder.get('holiday_type'), reminder.get('creator_id'),
                                   reminder.get('creator_name'), reminder.get('is_task', False))

            logger.info(f"成功添加提醒到PostgreSQL")
            return True
        except Exception as e:
            logger.error(f"添加提醒到PostgreSQL失败: {str(e)}")
            return False

    async def remove_reminder(self, session_id: str, reminder_text: str, date_time: str) -> bool:
        """从数据库中删除指定的提醒
        
        Args:
            session_id: 会话ID
            reminder_text: 提醒内容
            date_time: 提醒时间字符串
            
        Returns:
            bool: 删除成功返回True，否则False
        """
        try:
            if not self.pool:
                await self.init_pool()

            # 解析日期时间字符串
            try:
                dt = datetime.datetime.strptime(date_time, "%Y-%m-%d %H:%M")
            except ValueError:
                logger.error(f"无效的日期时间格式: {date_time}")
                return False

            async with self.pool.acquire() as conn:
                # 删除数据
                result = await conn.execute('''
                                            DELETE
                                            FROM reminders
                                            WHERE session_id = $1
                                              AND text = $2
                                              AND date_time = $3
                                            ''', session_id, reminder_text, dt)

            logger.info(f"从PostgreSQL删除提醒: {result}")
            return True
        except Exception as e:
            logger.error(f"从PostgreSQL删除提醒失败: {str(e)}")
            return False

    async def clear_expired_reminders(self) -> int:
        """清理过期的一次性提醒
        
        Returns:
            int: 清理的记录数量
        """
        try:
            if not self.pool:
                await self.init_pool()

            now = datetime.datetime.now()
            count = 0

            async with self.pool.acquire() as conn:
                # 删除过期的一次性提醒
                result = await conn.execute('''
                                            DELETE
                                            FROM reminders
                                            WHERE (repeat_type IS NULL OR repeat_type = 'none' OR repeat_type = '不重复')
                                              AND date_time < $1
                                            ''', now)

                # 解析删除的行数
                count = int(result.split(' ')[1]) if 'DELETE' in result else 0

            logger.info(f"清理了 {count} 个过期的提醒")
            return count
        except Exception as e:
            logger.error(f"清理过期提醒失败: {str(e)}")
            return 0