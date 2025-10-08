
# --- imghdr 兼容补丁（Python 3.13+ 环境安全）---
try:
    import imghdr
except ModuleNotFoundError:
    import mimetypes
    import types
    imghdr = types.SimpleNamespace()
    imghdr.what = lambda path: (mimetypes.guess_type(path)[0] or '').split('/')[-1]
# --------------------------------------------------

import logging
import re
import random
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Updater, CommandHandler, MessageHandler, Filters, 
                          CallbackContext, CallbackQueryHandler, ConversationHandler)
import sqlite3
from functools import wraps
from collections import defaultdict

# 配置日志
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # 降低日志级别，避免Render.com日志过多
)
logger = logging.getLogger(__name__)

# 状态常量，用于对话处理
SET_WINNING_IMAGE, SET_LOSING_IMAGE = 1, 2

# 骰子表情映射表
DICE_EMOJI_MAP = {
    '🎲': None,  # 随机骰子
    '1️⃣': 1,
    '2️⃣': 2,
    '3️⃣': 3,
    '4️⃣': 4,
    '5️⃣': 5,
    '6️⃣': 6
}

# 数据库操作辅助函数
# 使用上下文管理器，自动管理连接生命周期
from contextlib import contextmanager

@contextmanager
def get_db_connection():
    conn = None
    try:
        # 在Render.com上使用持久化路径存储数据库
        db_path = os.path.join(os.getcwd(), 'secondhand_bot.db')
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row  # 支持按列名访问
        yield conn  # 提供连接给with语句使用
    except sqlite3.Error as e:
        logger.error(f"数据库连接错误: {str(e)}")
        raise  # 抛出错误让调用方处理
    finally:
        if conn:
            conn.close()  # 确保最终关闭连接，无论是否出错

# 数据库初始化
def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        
        # 用户表
        c.execute('''CREATE TABLE IF NOT EXISTS users
                     (id INTEGER PRIMARY KEY, 
                      user_id INTEGER UNIQUE,
                      username TEXT,
                      chat_id INTEGER,
                      balance INTEGER,
                      registration_time DATETIME,
                      is_admin INTEGER DEFAULT 0,
                      is_super_admin INTEGER DEFAULT 0)''')
        
        # 投注表
        c.execute('''CREATE TABLE IF NOT EXISTS bets
                     (id INTEGER PRIMARY KEY,
                      user_id INTEGER,
                      round_id TEXT,
                      bet_type TEXT,
                      bet_value TEXT,
                      amount INTEGER,
                      bet_time DATETIME,
                      result TEXT DEFAULT NULL,
                      payout INTEGER DEFAULT 0,
                      status TEXT DEFAULT 'active')''')
        
        # 轮次表
        c.execute('''CREATE TABLE IF NOT EXISTS rounds
                     (id TEXT PRIMARY KEY,
                      start_time DATETIME,
                      end_time DATETIME,
                      dice_result TEXT,
                      status TEXT DEFAULT 'open')''')
        
        # 系统设置表
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'")
        if c.fetchone():
            c.execute("PRAGMA table_info(settings)")
            columns = [column[1] for column in c.fetchall()]
            if 'allow_irrelevant_msgs' not in columns:
                c.execute("ALTER TABLE settings ADD COLUMN allow_irrelevant_msgs INTEGER DEFAULT 0")
            if 'odds_daxiao' not in columns:
                c.execute("ALTER TABLE settings ADD COLUMN odds_daxiao INTEGER DEFAULT 2")
            if 'odds_hezhi' not in columns:
                c.execute("ALTER TABLE settings ADD COLUMN odds_hezhi INTEGER DEFAULT 7")
            if 'odds_baozi' not in columns:
                c.execute("ALTER TABLE settings ADD COLUMN odds_baozi INTEGER DEFAULT 11")
            if 'betting_active' not in columns:
                c.execute("ALTER TABLE settings ADD COLUMN betting_active INTEGER DEFAULT 1")
        else:
            c.execute('''CREATE TABLE settings
                         (id INTEGER PRIMARY KEY,
                          min_bet INTEGER DEFAULT 1000,
                          max_bet INTEGER DEFAULT 30000,
                          max_size_odd_even_bets INTEGER DEFAULT 2,
                          max_sum_bets INTEGER DEFAULT 3,
                          max_leopard_bets INTEGER DEFAULT 1,
                          allow_irrelevant_msgs INTEGER DEFAULT 0,
                          odds_daxiao INTEGER DEFAULT 2,
                          odds_hezhi INTEGER DEFAULT 7,
                          odds_baozi INTEGER DEFAULT 11,
                          betting_active INTEGER DEFAULT 1)''')
        
        # 收支记录表
        c.execute('''CREATE TABLE IF NOT EXISTS balance_logs
                     (id INTEGER PRIMARY KEY,
                      user_id INTEGER,
                      amount INTEGER,
                      type TEXT,
                      operator_id INTEGER,
                      create_time DATETIME)''')
        
        # 联系方式表
        c.execute('''CREATE TABLE IF NOT EXISTS contacts
                     (id INTEGER PRIMARY KEY,
                      type TEXT UNIQUE,
                      contact_info TEXT,
                      update_time DATETIME,
                      updated_by INTEGER)''')
        
        # 开奖媒体表
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='winning_media'")
        if c.fetchone():
            c.execute("PRAGMA table_info(winning_media)")
            columns = [column[1] for column in c.fetchall()]
            if 'media_type' not in columns:
                c.execute("ALTER TABLE winning_media ADD COLUMN media_type TEXT DEFAULT 'win'")
        else:
            c.execute('''CREATE TABLE winning_media
                         (id INTEGER PRIMARY KEY,
                          file_id TEXT,
                          file_type TEXT,
                          media_type TEXT DEFAULT 'win',
                          added_time DATETIME,
                          added_by INTEGER)''')
        
        # 检查是否有系统设置
        c.execute("SELECT * FROM settings")
        if c.fetchone() is None:
            c.execute("INSERT INTO settings (min_bet, max_bet, max_size_odd_even_bets, max_sum_bets, max_leopard_bets, allow_irrelevant_msgs, odds_daxiao, odds_hezhi, odds_baozi, betting_active) VALUES (1000, 30000, 2, 3, 1, 0, 2, 7, 11, 1)")
        
        # 初始化联系方式
        c.execute("SELECT * FROM contacts WHERE type = 'top_up'")
        if not c.fetchone():
            c.execute("INSERT INTO contacts (type, contact_info, update_time, updated_by) VALUES ('top_up', '请联系管理员设置上分方式', ?, 0)", 
                     (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))
        
        c.execute("SELECT * FROM contacts WHERE type = 'withdraw'")
        if not c.fetchone():
            c.execute("INSERT INTO contacts (type, contact_info, update_time, updated_by) VALUES ('withdraw', '请联系管理员设置下分方式', ?, 0)", 
                     (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))
        
        c.execute("SELECT * FROM contacts WHERE type = 'banker'")
        if not c.fetchone():
            c.execute("INSERT INTO contacts (type, contact_info, update_time, updated_by) VALUES ('banker', '请联系管理员设置庄家联系方式', ?, 0)", 
                     (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))
        
        conn.commit()

# 初始化数据库
init_db()

# 全局变量 - 当前轮次ID
current_round_id = None

# 获取当前活跃轮次
def get_active_round():
    global current_round_id
    with get_db_connection() as conn:
        active_round = conn.execute(
            "SELECT id FROM rounds WHERE status = 'open' ORDER BY start_time DESC LIMIT 1"
        ).fetchone()
        
        if active_round:
            current_round_id = active_round['id']
            return current_round_id
    return None

# 获取投注状态
def get_betting_status():
    with get_db_connection() as conn:
        status = conn.execute("SELECT betting_active FROM settings").fetchone()['betting_active']
        return status == 1

# 设置投注状态
def set_betting_status(active: bool):
    with get_db_connection() as conn:
        conn.execute("UPDATE settings SET betting_active = ?", (1 if active else 0,))
        conn.commit()

# 创建主菜单键盘，包含投注状态控制
def get_main_menu_keyboard():
    betting_active = get_betting_status()
    
    # 状态控制按钮
    status_buttons = []
    if betting_active:
        status_buttons.append(InlineKeyboardButton("🔴 停止投注", callback_data='stop_betting'))
    else:
        status_buttons.append(InlineKeyboardButton("🟢 开始投注", callback_data='start_betting'))
    
    # 主菜单其他按钮
    main_buttons = [
        [
            InlineKeyboardButton("上分/下分", callback_data='top_up_withdraw'),
            InlineKeyboardButton("我的余额", callback_data='my_balance'),
        ],
        [
            InlineKeyboardButton("我的投注", callback_data='my_bets'),
            InlineKeyboardButton("当前庄家", callback_data='current_banker'),
        ],
        [
            InlineKeyboardButton("当前赔率", callback_data='odds_settings'),
            InlineKeyboardButton("投注记录", callback_data='bet_records'),
        ],
        [
            InlineKeyboardButton("最新开奖", callback_data='latest_result'),
            InlineKeyboardButton("帮助中心", callback_data='help_center'),
        ],
        [InlineKeyboardButton("返回主页", callback_data='main_menu')]
    ]
    
    # 将状态按钮放在最上方
    return [status_buttons] + main_buttons

# 创建帮助中心键盘
def get_help_center_keyboard():
    return [
        [InlineKeyboardButton("返回上一页", callback_data='main_menu')]
    ]

# 创建赔率设置键盘
def get_odds_settings_keyboard():
    return [
        [InlineKeyboardButton("显示当前赔率", callback_data='show_odds')],
        [InlineKeyboardButton("返回上一页", callback_data='main_menu')]
    ]

# 管理员权限装饰器
def admin_required(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        user_id = update.effective_user.id
        logger.debug(f"检查管理员权限 - 用户ID: {user_id}")
        
        with get_db_connection() as conn:
            user = conn.execute('SELECT is_admin, is_super_admin FROM users WHERE user_id = ?', (user_id,)).fetchone()
            
            if not user:
                logger.debug(f"用户未注册 - 用户ID: {user_id}")
                update.effective_message.reply_text('你没有权限执行此操作（未注册）')
                return
                
            if user['is_admin'] == 0 and user['is_super_admin'] == 0:
                logger.debug(f"非管理员尝试执行管理员操作 - 用户ID: {user_id}")
                update.effective_message.reply_text('你没有权限执行此操作（非管理员）')
                return
                
            logger.debug(f"管理员权限验证通过 - 用户ID: {user_id}")
            return func(update, context, *args, **kwargs)
    return wrapped

# 超级管理员权限装饰器
def super_admin_required(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        user_id = update.effective_user.id
        with get_db_connection() as conn:
            user = conn.execute('SELECT is_super_admin FROM users WHERE user_id = ?', (user_id,)).fetchone()
            
            if not user or user['is_super_admin'] == 0:
                update.effective_message.reply_text('你没有权限执行此操作（需要超级管理员权限）')
                return
            return func(update, context, *args, **kwargs)
    return wrapped

# 注册用户
def start(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    with get_db_connection() as conn:
        existing_user = conn.execute('SELECT * FROM users WHERE user_id = ?', (user.id,)).fetchone()
        
        if existing_user:
            reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
            update.message.reply_text(
                f'您好！您已注册，ID是：{existing_user["id"]}\n当前余额：{existing_user["balance"]:.2f} KS',
                reply_markup=reply_markup
            )
        else:
            registration_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn.execute(
                'INSERT INTO users (user_id, username, chat_id, balance, registration_time) VALUES (?, ?, ?, ?, ?)',
                (user.id, user.username, chat_id, 0, registration_time)
            )
            
            user_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            if user_count == 1:
                conn.execute('UPDATE users SET is_super_admin = 1, is_admin = 1 WHERE user_id = ?', (user.id,))
            
            new_user = conn.execute('SELECT * FROM users WHERE user_id = ?', (user.id,)).fetchone()
            conn.commit()
            
            reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
            update.message.reply_text(
                f'您好，欢迎使用二手娱乐打手机器人！\n您的ID是：{new_user["id"]}\n初始余额：0.00 KS',
                reply_markup=reply_markup
            )

# 上下分操作
@admin_required
def adjust_balance(update: Update, context: CallbackContext) -> None:
    message = update.effective_message
    target_user_id = None
    amount = 0
    operator_user = update.effective_user  # 操作管理员
    
    try:
        with get_db_connection() as conn:
            # 获取管理员在users表中的ID
            operator_data = conn.execute(
                'SELECT id FROM users WHERE user_id = ?',
                (operator_user.id,)
            ).fetchone()
            
            if not operator_data:
                message.reply_text('操作失败：操作员未注册')
                return
            
            operator_db_id = operator_data['id']
            
            # 处理引用消息的情况
            if message.reply_to_message and message.reply_to_message.from_user:
                target_user = message.reply_to_message.from_user
                target_user_data = conn.execute(
                    'SELECT id, balance, username FROM users WHERE user_id = ?',
                    (target_user.id,)
                ).fetchone()
                
                if not target_user_data:
                    message.reply_text('该用户未注册，请让其先发送 /start 注册')
                    return
                
                target_user_id = target_user_data['id']
                target_username = target_user_data['username'] or f"ID{target_user_id}"
                current_balance = target_user_data['balance']
                
                # 解析金额
                try:
                    amount_text = message.text.strip()
                    if re.match(r'^[+-]\d+$', amount_text):
                        amount = int(amount_text)
                    else:
                        message.reply_text('无效的金额格式，请仅输入带正负号的数字（如+1000或-2000）')
                        return
                except ValueError:
                    message.reply_text('无效的金额格式，请仅输入数字（如+1000或-2000）')
                    return
            else:
                # 处理指定ID的情况
                text = message.text.strip()
                pattern = re.compile(r'^ID(\d+)\s*([+-]\d+)$')
                match = pattern.match(text)
                
                if not match:
                    message.reply_text('无效的格式，请使用 "IDxxx +xxx" 或 "IDxxx -xxx" 格式（如 ID123 +5000）')
                    return
                
                target_user_id = int(match.group(1))
                amount = int(match.group(2))
                
                # 检查用户是否存在
                target_user_data = conn.execute(
                    'SELECT balance, username FROM users WHERE id = ?',
                    (target_user_id,)
                ).fetchone()
                
                if not target_user_data:
                    message.reply_text(f'ID为 {target_user_id} 的用户不存在')
                    return
                
                target_username = target_user_data['username'] or f"ID{target_user_id}"
                current_balance = target_user_data['balance']
            
            # 检查下分是否余额不足
            if amount < 0 and current_balance + amount < 0:
                message.reply_text(f'操作失败，用户 {target_username} 当前余额 {current_balance:.2f} KS，无法下分 {abs(amount):.2f} KS')
                return
            
            # 更新余额
            new_balance = current_balance + amount
            conn.execute(
                'UPDATE users SET balance = ? WHERE id = ?',
                (new_balance, target_user_id)
            )
            
            # 记录上下分到收支表
            log_type = 'recharge' if amount > 0 else 'withdraw'
            create_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn.execute(
                '''INSERT INTO balance_logs (user_id, amount, type, operator_id, create_time)
                   VALUES (?, ?, ?, ?, ?)''',
                (target_user_id, amount, log_type, operator_db_id, create_time)
            )
            
            conn.commit()
            
            # 构建操作成功消息
            amount_text = f"+{amount:.2f}" if amount > 0 else f"{amount:.2f}"
            reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
            message.reply_text(f'✅ 操作成功！\n'
                            f'👤 目标用户：{target_username}（ID{target_user_id}）\n'
                            f'💰 原余额：{current_balance:.2f} KS\n'
                            f'📊 变动金额：{amount_text} KS\n'
                            f'💵 新余额：{new_balance:.2f} KS\n'
                            f'👑 操作员：@{operator_user.username or f"管理员{operator_db_id}"}',
                            reply_markup=reply_markup)
                            
    except Exception as e:
        logger.error(f"上下分操作失败: {str(e)}")
        message.reply_text(f'操作失败: {str(e)}')

# 设置是否允许无关消息
@admin_required
def set_allow_irrelevant(update: Update, context: CallbackContext) -> None:
    # 检查参数是否存在
    if not context.args:
        update.message.reply_text('参数错误！请使用：\n/chat 允许（开启自由聊天）\n/chat 禁止（仅保留投注和指令）')
        return
    
    # 合并所有参数，提高容错性
    param = ' '.join(context.args).strip()
    
    # 支持更多可能的输入格式
    allow = None
    if param in ['允许', '开启', 'yes', 'y', '1']:
        allow = 1
    elif param in ['禁止', '关闭', 'no', 'n', '0']:
        allow = 0
    else:
        update.message.reply_text('参数错误！请使用：\n/chat 允许（开启自由聊天）\n/chat 禁止（仅保留投注和指令）')
        return
    
    # 保存当前消息ID，用于后续删除
    admin_msg_id = update.effective_message.message_id
    chat_id = update.effective_chat.id
    
    # 执行数据库更新
    try:
        with get_db_connection() as conn:
            # 执行更新
            conn.execute('UPDATE settings SET allow_irrelevant_msgs = ?', (allow,))
            conn.commit()
            
            # 验证更新结果
            new_setting = conn.execute('SELECT allow_irrelevant_msgs FROM settings').fetchone()
            
            # 确认更新成功
            if new_setting and new_setting['allow_irrelevant_msgs'] == allow:
                status = "已允许" if allow else "已禁止"
                response = f"{status}无关消息。\n"
                if allow:
                    response += "现在可以自由聊天，所有消息都将被保留。"
                else:
                    response += "系统将实时删除所有无关消息，只保留投注内容和机器人指令。"
                
                # 发送提示消息并计划3秒后删除
                sent_msg = update.message.reply_text(
                    response,
                    reply_markup=InlineKeyboardMarkup(get_help_center_keyboard())
                )
                
                # 3秒后删除管理员命令消息和提示消息
                context.job_queue.run_once(
                    lambda ctx: delete_messages(ctx, chat_id, [admin_msg_id, sent_msg.message_id]),
                    3,
                    context=context
                )
            else:
                update.message.reply_text('设置失败，数据库未更新，请重试')
    except Exception as e:
        logger.error(f"/chat命令执行出错: {str(e)}")
        update.message.reply_text(f'设置失败，错误信息: {str(e)}')

# 删除消息的辅助函数
def delete_messages(context: CallbackContext, chat_id: int, message_ids: list):
    for msg_id in message_ids:
        try:
            context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            logger.debug(f"已删除消息 ID: {msg_id}")
        except Exception as e:
            logger.warning(f"删除消息 {msg_id} 失败: {e}")

# 处理无关消息
def handle_irrelevant_message(update: Update, context: CallbackContext) -> None:
    message = update.effective_message
    message_text = message.text.strip() if message.text else ""
    
    # 如果是"取消"指令，交给专门的处理函数
    if message_text == '取消':
        cancel_bet(update, context)
        return
    
    # 检查是否允许无关消息
    with get_db_connection() as conn:
        setting = conn.execute('SELECT allow_irrelevant_msgs FROM settings').fetchone()
        allow_irrelevant = setting['allow_irrelevant_msgs'] if setting else 0
    
    # 如果不允许无关消息，实时删除消息
    if allow_irrelevant == 0:
        try:
            # 检查消息是否是有效投注或命令
            is_valid_command = message_text.startswith('/')
            is_valid_bet = parse_bet(message_text) is not None
            is_system_cmd = message_text in ['1', '22', '33']  # 系统特殊指令
            
            # 只删除真正的无关消息
            if not is_valid_command and not is_valid_bet and not is_system_cmd:
                # 立即删除无关消息
                message.delete()
                logger.debug(f"已实时删除无关消息: {message_text}")
                
                # 可选：发送一个短暂的提示，告知用户消息已被删除
                notification = message.reply_text("无关消息已被自动删除")
                # 3秒后删除提示消息
                context.job_queue.run_once(
                    lambda ctx: delete_messages(ctx, message.chat_id, [notification.message_id]),
                    3,
                    context=context
                )
        except Exception as e:
            logger.error(f"删除无关消息失败: {e}")

# 查询账户信息
def check_balance(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    
    with get_db_connection() as conn:
        db_user = conn.execute('SELECT id, balance, username FROM users WHERE user_id = ?', (user.id,)).fetchone()
        if not db_user:
            update.message.reply_text('您还没有注册，请发送 /start 进行注册')
            return
        
        user_id = db_user['id']
        current_balance = db_user['balance']
        username = db_user['username'] or f"用户{user_id}"
        
        today_start = datetime.now().strftime('%Y-%m-%d 00:00:00')
        today_end = datetime.now().strftime('%Y-%m-%d 23:59:59')
        
        total_recharge = conn.execute(
            '''SELECT COALESCE(SUM(amount), 0) as total 
               FROM balance_logs 
               WHERE user_id = ? AND type = 'recharge' AND amount > 0 
               AND create_time BETWEEN ? AND ?''',
            (user_id, today_start, today_end)
        ).fetchone()['total']
        
        total_withdraw = conn.execute(
            '''SELECT COALESCE(ABS(SUM(amount)), 0) as total 
               FROM balance_logs 
               WHERE user_id = ? AND type = 'withdraw' AND amount < 0 
               AND create_time BETWEEN ? AND ?''',
            (user_id, today_start, today_end)
        ).fetchone()['total']
        
        total_payout = conn.execute(
            '''SELECT COALESCE(SUM(amount), 0) as total 
               FROM balance_logs 
               WHERE user_id = ? AND type = 'payout' AND amount > 0 
               AND create_time BETWEEN ? AND ?''',
            (user_id, today_start, today_end)
        ).fetchone()['total']
        
        total_bet = conn.execute(
            '''SELECT COALESCE(ABS(SUM(amount)), 0) as total 
               FROM balance_logs 
               WHERE user_id = ? AND type = 'bet' AND amount < 0 
               AND create_time BETWEEN ? AND ?''',
            (user_id, today_start, today_end)
        ).fetchone()['total']
        
        profit_loss = total_payout - total_bet
        profit_loss_text = f"+{profit_loss:.2f}" if profit_loss >= 0 else f"{profit_loss:.2f}"
        
        response = f"👤 您的账户信息（{username}）\n"
        response += f"📅 统计时间：{today_start.split(' ')[0]} 00:00 至今\n"
        response += f"💵 当日总充值：{total_recharge:.2f} KS\n"
        response += f"💸 当日总提款：{total_withdraw:.2f} KS\n"
        response += f"📊 当日盈亏：{profit_loss_text} KS\n"
        response += f"💰 当前余额：{current_balance:.2f} KS"
        
        reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
        update.message.reply_text(response, reply_markup=reply_markup)

# 查自己24小时内投注记录
def check_my_bet_history(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    
    with get_db_connection() as conn:
        user = conn.execute('SELECT id FROM users WHERE user_id = ?', (user_id,)).fetchone()
        if not user:
            update.message.reply_text('您还没有注册，请发送 /start 进行注册')
            return
        
        time_24h_ago = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        bets = conn.execute(
            '''SELECT b.round_id, b.bet_type, b.bet_value, b.amount, b.bet_time, b.result, b.payout, r.dice_result, b.status
               FROM bets b
               JOIN rounds r ON b.round_id = r.id
               WHERE b.user_id = ? AND b.bet_time >= ?
               ORDER BY b.bet_time DESC''',
            (user['id'], time_24h_ago)
        ).fetchall()
        
        if not bets:
            response = '您在24小时内没有投注记录'
        else:
            response = "您24小时内的投注记录：\n"
            for bet in bets:
                status_text = "已取消" if bet['status'] == 'cancelled' else ""
                result_text = "中奖" if bet['result'] == 'win' else "未中奖" if bet['result'] else "未开奖"
                if status_text:
                    result_text = status_text
                payout_text = f"，派彩：{bet['payout']:.2f} KS" if bet['payout'] else ""
                response += f"- 期号：{bet['round_id']}，类型：{bet['bet_type']} {bet['bet_value']}，金额：{bet['amount']:.2f} KS，时间：{bet['bet_time']}，结果：{result_text}{payout_text}\n"
        
        if len(response) > 4000:
            response = response[:4000] + "\n...记录过长，已截断"
        
        reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
        update.message.reply_text(response, reply_markup=reply_markup)

# 查所有玩家24小时内历史投注记录（仅管理员）
@admin_required
def check_all_bet_history(update: Update, context: CallbackContext) -> None:
    with get_db_connection() as conn:
        time_24h_ago = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        bets = conn.execute(
            '''SELECT b.round_id, u.id as user_id, u.username, b.bet_type, b.bet_value, b.amount, 
                      b.bet_time, b.result, b.payout, r.dice_result, b.status
               FROM bets b
               JOIN users u ON b.user_id = u.id
               JOIN rounds r ON b.round_id = r.id
               WHERE b.bet_time >= ?
               ORDER BY b.bet_time DESC''',
            (time_24h_ago,)
        ).fetchall()
        
        if not bets:
            response = '24小时内没有投注记录'
        else:
            response = "所有玩家24小时内的投注记录：\n"
            for bet in bets:
                status_text = "已取消" if bet['status'] == 'cancelled' else ""
                result_text = "中奖" if bet['result'] == 'win' else "未中奖" if bet['result'] else "未开奖"
                if status_text:
                    result_text = status_text
                payout_text = f"，派彩：{bet['payout']:.2f} KS" if bet['payout'] else ""
                username = bet['username'] or f"ID{bet['user_id']}"
                response += f"- 期号：{bet['round_id']}，用户：ID{bet['user_id']} {username}，类型：{bet['bet_type']} {bet['bet_value']}，金额：{bet['amount']:.2f} KS，结果：{result_text}{payout_text}\n"
        
        if len(response) > 4000:
            response = response[:4000] + "\n...记录过长，已截断"
        
        reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
        update.message.reply_text(response, reply_markup=reply_markup)

# 检查全量数据
def check_total_data(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    
    with get_db_connection() as conn:
        user_data = conn.execute('SELECT id FROM users WHERE user_id = ?', (user.id,)).fetchone()
        if not user_data:
            update.message.reply_text('您还没有注册，请发送 /start 进行注册')
            return
        
        total_recharge = conn.execute(
            '''SELECT COALESCE(SUM(amount), 0) as total 
               FROM balance_logs 
               WHERE type = 'recharge' AND amount > 0'''
        ).fetchone()['total']
        
        total_withdraw = conn.execute(
            '''SELECT COALESCE(ABS(SUM(amount)), 0) as total 
               FROM balance_logs 
               WHERE type = 'withdraw' AND amount < 0'''
        ).fetchone()['total']
        
        total_balance = conn.execute(
            '''SELECT COALESCE(SUM(balance), 0) as total 
               FROM users'''
        ).fetchone()['total']
        
        total_payout = conn.execute(
            '''SELECT COALESCE(SUM(amount), 0) as total 
               FROM balance_logs 
               WHERE type = 'payout' AND amount > 0'''
        ).fetchone()['total']
        
        total_bet = conn.execute(
            '''SELECT COALESCE(ABS(SUM(amount)), 0) as total 
               FROM balance_logs 
               WHERE type = 'bet' AND amount < 0'''
        ).fetchone()['total']
        
        total_profit_loss = total_payout - total_bet
        total_profit_loss_text = f"+{total_profit_loss:.2f}" if total_profit_loss >= 0 else f"{total_profit_loss:.2f}"
        
        stats_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        response = f"🔧 系统全量数据统计（截至 {stats_time}）\n"
        response += f"💵 系统总充值：{total_recharge:.2f} KS\n"
        response += f"💸 系统总提款：{total_withdraw:.2f} KS\n"
        response += f"📊 系统总盈亏：{total_profit_loss_text} KS\n"
        response += f"💰 系统总余额：{total_balance:.2f} KS\n"
        response += f"⚠️  数据说明：总盈亏 = 总派彩金额 - 总下注金额"
        
        reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
        update.message.reply_text(response, reply_markup=reply_markup)

# 检查是否有未完成的轮次
def check_pending_round() -> bool:
    with get_db_connection() as conn:
        pending_round = conn.execute(
            "SELECT id FROM rounds WHERE status = 'open' OR (status = 'waiting' AND end_time IS NULL)"
        ).fetchone()
        return pending_round is not None

# 创建新的投注轮次
def create_new_round() -> str:
    global current_round_id
    
    if check_pending_round():
        return None
    
    today = datetime.now().strftime('%Y%m%d')
    
    with get_db_connection() as conn:
        today_rounds = conn.execute(
            "SELECT COUNT(*) FROM rounds WHERE id LIKE ?",
            (f"{today}%",)
        ).fetchone()[0]
        
        round_number = today_rounds + 1
        round_id = f"{today}{str(round_number).zfill(3)}"
        
        start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            "INSERT INTO rounds (id, start_time, status) VALUES (?, ?, 'open')",
            (round_id, start_time)
        )
        
        conn.commit()
        
        current_round_id = round_id
        logger.info(f"创建了新轮次: {current_round_id}")
        return round_id

# 开启投注
@admin_required
def open_betting(update: Update, context: CallbackContext) -> None:
    # 检查是否已经开启
    if get_betting_status():
        update.effective_message.reply_text("投注已经处于开启状态")
        return
    
    # 开启投注
    set_betting_status(True)
    
    # 检查是否有活跃轮次，没有则创建
    active_round_id = get_active_round()
    if not active_round_id:
        active_round_id = create_new_round()
    
    reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
    update.effective_message.reply_text(
        f"🟢 投注已开启\n当前期号: {active_round_id}",
        reply_markup=reply_markup
    )

# 停止投注
@admin_required
def stop_betting(update: Update, context: CallbackContext) -> None:
    # 检查是否已经关闭
    if not get_betting_status():
        update.effective_message.reply_text("投注已经处于停止状态")
        return
    
    # 停止投注
    set_betting_status(False)
    
    reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
    update.effective_message.reply_text(
        "🔴 投注已停止，请等待当前轮次结束",
        reply_markup=reply_markup
    )

# 处理下注
def process_bet(update: Update, context: CallbackContext) -> None:
    # 检查投注是否开启
    if not get_betting_status():
        update.effective_message.reply_text("当前投注已停止，请等待管理员开启")
        return
        
    user = update.effective_user
    message_text = update.effective_message.text.strip()
    
    # 如果是数字1，则查询余额
    if message_text == '1':
        check_balance(update, context)
        return
    
    # 如果是数字22，则查询个人投注记录
    if message_text == '22':
        check_my_bet_history(update, context)
        return
    
    # 如果是数字33，且用户是管理员，则查询所有投注记录
    if message_text == '33':
        # 先检查用户是否为管理员
        with get_db_connection() as conn:
            user_data = conn.execute('SELECT is_admin, is_super_admin FROM users WHERE user_id = ?', (user.id,)).fetchone()
            
            if user_data and (user_data['is_admin'] == 1 or user_data['is_super_admin'] == 1):
                check_all_bet_history(update, context)
            else:
                update.message.reply_text('你没有权限执行此操作（非管理员）')
        return
    
    try:
        with get_db_connection() as conn:
            # 检查用户是否注册
            db_user = conn.execute('SELECT id, balance, username FROM users WHERE user_id = ?', (user.id,)).fetchone()
            if not db_user:
                update.message.reply_text('您还没有注册，请发送 /start 进行注册')
                return
            
            user_id = db_user['id']
            user_balance = db_user['balance']
            username = db_user['username'] or f"用户{user_id}"
            
            settings = conn.execute('''SELECT min_bet, max_bet, max_size_odd_even_bets, 
                                             max_sum_bets, max_leopard_bets FROM settings''').fetchone()
            min_bet = settings['min_bet']
            max_bet = settings['max_bet']
            max_size_odd_even = settings['max_size_odd_even_bets']
            max_sum = settings['max_sum_bets']
            max_leopard = settings['max_leopard_bets']
            
            # 优先检查并创建活跃轮次
            global current_round_id
            current_round_id = get_active_round()
            if not current_round_id:
                current_round_id = create_new_round()
                if not current_round_id:
                    update.message.reply_text('创建新轮次失败，请稍后再试')
                    return
            
            # 获取当前轮次用户已下注数量
            current_bets = conn.execute(
                "SELECT bet_type FROM bets WHERE user_id = ? AND round_id = ? AND status = 'active'",
                (user_id, current_round_id)
            ).fetchall()
            
            # 统计当前各类投注数量
            current_size_odd_even = sum(1 for bet in current_bets if bet['bet_type'] in ['大', '小', '单', '双'])
            current_sum = sum(1 for bet in current_bets if bet['bet_type'] == '和值')
            current_leopard = sum(1 for bet in current_bets if bet['bet_type'] == '豹子')
            
            # 解析下注内容
            bets = parse_bet(message_text)
            if not bets:
                # 根据设置决定是否删除无效消息
                allow_irrelevant = conn.execute('SELECT allow_irrelevant_msgs FROM settings').fetchone()['allow_irrelevant_msgs']
                
                # 只对看起来像投注但格式错误的消息发送提示
                if re.search(r'(大|小|单|双|豹子|\d+)\s*\d+', message_text):
                    update.message.reply_text(
                        '无效的下注格式，请检查后重新下注\n正确格式示例：大单1000、11 5000、豹子2000'
                    )
                elif allow_irrelevant == 0:
                    try:
                        update.effective_message.delete()
                        notification = update.effective_message.reply_text("无关消息已被自动删除")
                        context.job_queue.run_once(
                            lambda ctx: delete_messages(ctx, update.effective_chat.id, [notification.message_id]),
                            3,
                            context=context
                        )
                    except Exception as e:
                        logger.error(f"删除无关消息失败: {e}")
                return
            
            # 统计新下注各类投注数量
            new_size_odd_even = sum(1 for bet in bets if bet['type'] in ['大', '小', '单', '双'])
            new_sum = sum(1 for bet in bets if bet['type'] == '和值')
            new_leopard = sum(1 for bet in bets if bet['type'] == '豹子')
            
            # 检查是否超过最大注数限制
            if (current_size_odd_even + new_size_odd_even) > max_size_odd_even or \
               (current_sum + new_sum) > max_sum or \
               (current_leopard + new_leopard) > max_leopard:
                update.message.reply_text('已超出娱乐规则请重新下注\n最多：大小单双共2注 + 和值3注 + 豹子1注')
                return
            
            # 检查是否有对立下注（大小或单双同时下注）
            existing_types = [bet['bet_type'] for bet in current_bets]
            new_types = [bet['type'] for bet in bets]
            
            all_types = existing_types + new_types
            if ('大' in all_types and '小' in all_types) or ('单' in all_types and '双' in all_types):
                update.message.reply_text('大小或单双不允许同时下注，请重新下注')
                return
            
            # 检查总金额是否足够
            total_amount = sum(bet['amount'] for bet in bets)
            if user_balance < total_amount:
                update.message.reply_text('宝宝、余额不足请管理上分、谢谢！')
                return
            
            # 检查每注金额是否符合限制
            for bet in bets:
                if bet['amount'] < min_bet or bet['amount'] > max_bet:
                    update.message.reply_text(f'单注金额必须在 {min_bet} - {max_bet} KS 之间，请重新下注')
                    return
            
            # 扣减用户余额
            new_balance = user_balance - total_amount
            conn.execute(
                "UPDATE users SET balance = ? WHERE id = ?",
                (new_balance, user_id)
            )
            
            # 记录下注 + 记录下注到收支表
            bet_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            create_time = bet_time
            bet_details = []
            for bet in bets:
                # 记录投注表
                conn.execute(
                    '''INSERT INTO bets (user_id, round_id, bet_type, bet_value, amount, bet_time, status)
                       VALUES (?, ?, ?, ?, ?, ?, 'active')''',
                    (user_id, current_round_id, bet['type'], bet['value'], bet['amount'], bet_time)
                )
                # 记录收支表
                conn.execute(
                    '''INSERT INTO balance_logs (user_id, amount, type, operator_id, create_time)
                       VALUES (?, ?, 'bet', 0, ?)''',
                    (user_id, -bet['amount'], create_time)
                )
                bet_details.append(f"{bet['type']} {bet['value'] if bet['value'] else ''}{bet['amount']:.2f}")
            
            conn.commit()
            
            # 构建下注成功消息
            bet_text = "，".join(bet_details)
            response = f"✅ 下注成功！\n"
            response += f"🎯 期号：{current_round_id}\n"
            response += f"👤 用户：{username}\n"
            response += f"🎲 内容：{bet_text} KS\n"
            response += f"💰 余额：{new_balance:.2f} KS\n"
            response += f"提示：等待其他用户发送三个骰子表情（1️⃣-6️⃣）完成开奖"
            
            update.message.reply_text(response)
            logger.info(f"用户 {user_id} 在下注期号 {current_round_id} 成功，总下注金额：{total_amount:.2f} KS")
            
    except Exception as e:
        logger.error(f"处理下注时出错: {str(e)}")
        update.message.reply_text("处理下注时出错，请稍后再试")

# 解析下注内容
def parse_bet(text: str) -> list:
    bets = []
    clean_text = re.sub(r'\s+', ' ', text.strip())
    
    # 1. 先解析组合投注+单独大小单双+豹子
    type_amount_pattern = re.compile(r'(大单|大双|小单|小双|大|小|单|双|豹子)\s*(\d+)')
    type_matches = type_amount_pattern.findall(clean_text)
    
    for bet_type, amount_str in type_matches:
        try:
            amount = int(amount_str)
        except ValueError:
            continue  # 跳过无效金额
        
        # 处理组合投注
        if bet_type in ['大单', '大双', '小单', '小双']:
            size = bet_type[0]  # 大/小
            odd_even = bet_type[1]  # 单/双
            bets.append({'type': size, 'value': '', 'amount': amount})
            bets.append({'type': odd_even, 'value': '', 'amount': amount})
            # 从文本中移除已解析的组合投注
            clean_text = clean_text.replace(f'{bet_type}{amount_str}', '').replace(f'{bet_type} {amount_str}', '')
        else:
            # 单独投注
            bets.append({'type': bet_type, 'value': '', 'amount': amount})
            clean_text = clean_text.replace(f'{bet_type}{amount_str}', '').replace(f'{bet_type} {amount_str}', '')
    
    # 2. 解析和值
    sum_pattern = re.compile(r'(\d{1,2})\s*(\d+)')
    sum_matches = sum_pattern.findall(clean_text.strip())
    
    for sum_val_str, amount_str in sum_matches:
        try:
            sum_val = int(sum_val_str)
            amount = int(amount_str)
        except ValueError:
            continue  # 跳过无效数字
        
        # 验证和值范围（3-18）
        if 3 <= sum_val <= 18:
            bets.append({'type': '和值', 'value': str(sum_val), 'amount': amount})
    
    # 去重
    unique_bets = []
    seen = set()
    for bet in bets:
        key = (bet['type'], bet['value'], bet['amount'])
        if key not in seen:
            seen.add(key)
            unique_bets.append(bet)
    
    return unique_bets if unique_bets else None

# 处理骰子结果
def handle_dice(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    message = update.effective_message
    dice_values = []
    is_complete = False
    
    # 先检查活跃轮次，无则提示下注
    active_round_id = get_active_round()
    if not active_round_id:
        message.reply_text('当前没有活跃的投注轮次，请先进行下注来开启新轮次~')
        return
    
    # 处理Telegram内置骰子
    if message.dice:
        # 初始化骰子序列存储
        if 'dice_sequence' not in context.user_data:
            context.user_data['dice_sequence'] = []
        
        # 添加当前骰子值
        context.user_data['dice_sequence'].append(message.dice.value)
        logger.info(f"收到内置骰子值: {message.dice.value}, 当前序列: {context.user_data['dice_sequence']}")
        
        # 当收集到3个骰子值时处理
        if len(context.user_data['dice_sequence']) == 3:
            dice_values = context.user_data['dice_sequence']
            is_complete = True
            # 清空序列
            del context.user_data['dice_sequence']
        else:
            # 提示还需要多少个骰子
            remaining = 3 - len(context.user_data['dice_sequence'])
            message.reply_text(f'已收到{len(context.user_data["dice_sequence"])}个骰子，还需要{remaining}个即可开奖')
            return
    else:
        # 处理骰子表情
        emojis = re.findall(r'[🎲1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣]', message.text or '')
        
        # 检查是否包含三个骰子相关表情
        if len(emojis) == 3:
            # 解析骰子表情为数字
            for emoji in emojis:
                if emoji == '🎲':  # 随机骰子，生成1-6的随机数
                    dice_val = random.randint(1, 6)
                    dice_values.append(dice_val)
                else:
                    dice_values.append(DICE_EMOJI_MAP.get(emoji, 0))
            is_complete = True
        else:
            message.reply_text('请发送三个骰子表情（1️⃣-6️⃣或🎲）来完成开奖')
            return
    
    # 验证是否收集到完整的三个骰子值
    if not is_complete or len(dice_values) != 3:
        message.reply_text('未能识别完整的三个骰子，请重试')
        return
    
    # 验证骰子值是否有效
    if any(v < 1 or v > 6 for v in dice_values):
        message.reply_text('无效的骰子值，请确保所有骰子值在1-6之间')
        return
    
    # 检查用户是否注册
    with get_db_connection() as conn:
        db_user = conn.execute('SELECT id FROM users WHERE user_id = ?', (user.id,)).fetchone()
        if not db_user:
            message.reply_text('您还没有注册，请发送 /start 进行注册')
            return
        
        # 再次确认轮次状态
        round_data = conn.execute(
            "SELECT status, dice_result FROM rounds WHERE id = ?",
            (active_round_id,)
        ).fetchone()
        
        if not round_data or round_data['status'] != 'open':
            message.reply_text(f'当前轮次已关闭，无法提交骰子结果\n期号: {active_round_id}')
            return
        
        # 检查是否已经有骰子结果
        if round_data['dice_result']:
            message.reply_text(f'本轮已经有骰子结果：{round_data["dice_result"]}\n期号: {active_round_id}')
            return
        
        # 记录骰子结果
        dice_result = f"{dice_values[0]},{dice_values[1]},{dice_values[2]}"
        logger.info(f"完整骰子结果: {dice_result}, 开始结算期号: {active_round_id}")
        
        # 更新轮次状态
        end_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            "UPDATE rounds SET status = 'closed', end_time = ?, dice_result = ? WHERE id = ?",
            (end_time, dice_result, active_round_id)
        )
        
        # 结算所有投注，获取是否有中奖者
        has_winners = settle_bets(active_round_id, dice_result, conn)
        
        conn.commit()
        
        # 获取中奖记录用于通知
        winning_bets = conn.execute(
            '''SELECT b.amount, b.payout, u.username, u.id as user_id, u.chat_id, 
                      b.bet_type, b.bet_value, ub.balance as before_balance, 
                      (ub.balance + b.payout) as after_balance
               FROM bets b
               JOIN users u ON b.user_id = u.id
               JOIN (SELECT id, balance FROM users) ub ON b.user_id = ub.id
               WHERE b.round_id = ? AND b.result = 'win' AND b.status = 'active'
               ORDER BY u.id, b.payout DESC''',
            (active_round_id,)
        ).fetchall()
        
        # 解析骰子结果
        total = sum(dice_values)
        is_leopard = len(set(dice_values)) == 1  # 是否豹子
        size = "大" if total > 10 else "小"
        odd_even = "单" if total % 2 == 1 else "双"
        
        # 构建开奖结果消息
        result_text = f"🎊 本期开奖结果公示 🎊\n"
        result_text += f"📅 开奖时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        result_text += f"🎫 本期期号：{active_round_id}\n"
        result_text += f"🎲 骰子结果：{dice_result}（总和：{total}）\n"
        result_text += f"🏆 结果判定：{size}、{odd_even}\n"
        if is_leopard:
            result_text += "✨ 特殊结果：豹子！\n"
        
        # 投掷用户信息
        throw_user = f"@{user.username}" if user.username else f"用户{user.id}"
        full_name = user.full_name if user.full_name else ""
        if full_name:
            throw_user += f"（{full_name}）"
        result_text += f"🎯 投掷用户：{throw_user}\n"
        
        # 中奖记录：按用户ID分组
        result_text += "\n💸 本期中奖记录：\n"
        if winning_bets:
            # 用字典按用户ID分组
            user_bets_dict = defaultdict(list)
            for bet in winning_bets:
                user_bets_dict[bet['user_id']].append(bet)
            
            # 遍历分组后的用户中奖记录
            for user_id, bets in user_bets_dict.items():
                # 获取用户基础信息
                first_bet = bets[0]
                username = f"@{first_bet['username']}" if first_bet['username'] else f"ID{user_id}"
                before_balance = first_bet['before_balance']
                after_balance = first_bet['after_balance']
                total_payout = sum(bet['payout'] for bet in bets)
                
                # 构建用户中奖信息头部
                result_text += f"- 中奖用户：ID{user_id}（{username}）\n"
                result_text += f"  - 投注内容："
                
                # 合并同一用户的多笔投注内容
                bet_contents = []
                for bet in bets:
                    bet_content = f"{bet['bet_type']}"
                    if bet['bet_value']:
                        bet_content += f" {bet['bet_value']}"
                    bet_content += f"（{bet['amount']:.2f} KS）"
                    bet_contents.append(bet_content)
                result_text += "、".join(bet_contents) + "\n"
                
                # 计算总投注金额
                total_bet_amount = sum(bet['amount'] for bet in bets)
                result_text += f"  - 总投注金额：{total_bet_amount:.2f} KS，总派彩金额：{total_payout:.2f} KS\n"
                # 修改账户变动显示格式
                result_text += f"  - 账户变动：下注前: {before_balance} KS-派彩后{after_balance} KS-实时余额：{after_balance} KS\n\n"
        
        else:
            result_text += "🌝本期无人中奖\n\n"
        
        result_text += "🎉 未中奖用户可关注下一期投注，祝好运～"
        
        # 创建主菜单键盘
        reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
        
        # 根据是否有中奖者选择对应的媒体
        if has_winners:
            media_id, media_type = get_winning_media('win')
        else:
            media_id, media_type = get_winning_media('lose')
        
        # 发送带开奖信息的媒体
        if media_id and media_type == 'photo':
            message.reply_photo(
                photo=media_id,
                caption=result_text,
                reply_markup=reply_markup
            )
        elif media_id and media_type == 'video':
            message.reply_video(
                video=media_id,
                caption=result_text,
                reply_markup=reply_markup
            )
        elif media_id and media_type == 'sticker':
            # 发送贴纸后再发送文字
            message.reply_sticker(sticker=media_id)
            message.reply_text(result_text, reply_markup=reply_markup)
        elif media_id and media_type == 'animation':
            message.reply_animation(
                animation=media_id,
                caption=result_text,
                reply_markup=reply_markup
            )
        else:
            # 未知类型，直接发送文字
            message.reply_text(result_text, reply_markup=reply_markup)
        
        logger.info(f"期号 {active_round_id} 已完成开奖结算")
        
        # 重置当前轮次ID
        global current_round_id
        current_round_id = None

# 结算投注
def settle_bets(round_id: str, dice_result: str, conn: sqlite3.Connection) -> bool:
    try:
        # 获取赔率设置
        settings = conn.execute('''SELECT odds_daxiao, odds_hezhi, odds_baozi 
                                 FROM settings''').fetchone()
        odds_daxiao = settings['odds_daxiao']
        odds_hezhi = settings['odds_hezhi']
        odds_baozi = settings['odds_baozi']
        
        # 解析骰子结果
        dice_nums = list(map(int, dice_result.split(',')))
        total = sum(dice_nums)
        is_leopard = len(set(dice_nums)) == 1  # 是否豹子
        size = "大" if total > 10 else "小"
        odd_even = "单" if total % 2 == 1 else "双"
        
        logger.info(f"开始结算期号 {round_id}，骰子结果: {dice_result}，总和: {total}")
        
        # 获取本轮所有有效投注
        bets = conn.execute(
            "SELECT id, user_id, bet_type, bet_value, amount FROM bets WHERE round_id = ? AND status = 'active'",
            (round_id,)
        ).fetchall()
        
        if not bets:
            logger.info(f"期号 {round_id} 没有有效投注记录，无需结算")
            return False
        
        create_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        has_winners = False
        
        # 处理每一笔投注
        for bet in bets:
            bet_id = bet['id']
            user_id = bet['user_id']
            bet_type = bet['bet_type']
            bet_value = bet['bet_value']
            amount = bet['amount']
            
            result = 'lose'
            payout = 0
            
            # 如果是豹子，只有豹子投注中奖
            if is_leopard:
                if bet_type == '豹子':
                    result = 'win'
                    payout = amount * odds_baozi  # 使用数据库中的豹子赔率
                    has_winners = True
                    logger.info(f"投注 {bet_id} 中奖，豹子赔率，派彩 {payout:.2f} KS")
            else:
                # 处理大小单双
                if bet_type in ['大', '小', '单', '双']:
                    if (bet_type == '大' and size == '大') or \
                       (bet_type == '小' and size == '小') or \
                       (bet_type == '单' and odd_even == '单') or \
                       (bet_type == '双' and odd_even == '双'):
                        result = 'win'
                        payout = amount * odds_daxiao  # 使用数据库中的大小单双赔率
                        has_winners = True
                        logger.info(f"投注 {bet_id} 中奖，{bet_type} 赔率，派彩 {payout:.2f} KS")
                
                # 处理和值
                elif bet_type == '和值' and bet_value == str(total):
                    result = 'win'
                    payout = amount * odds_hezhi  # 使用数据库中的和值赔率
                    has_winners = True
                    logger.info(f"投注 {bet_id} 中奖，和值赔率，派彩 {payout:.2f} KS")
            
            # 更新投注结果
            conn.execute(
                "UPDATE bets SET result = ?, payout = ? WHERE id = ?",
                (result, payout, bet_id)
            )
            
            # 如果中奖，增加用户余额 + 记录派彩到收支表
            if result == 'win' and payout > 0:
                # 获取用户当前余额
                user = conn.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()
                new_balance = user['balance'] + payout
                
                # 更新用户余额
                conn.execute(
                    "UPDATE users SET balance = ? WHERE id = ?",
                    (new_balance, user_id)
                )
                
                # 记录派彩到收支表
                conn.execute(
                    '''INSERT INTO balance_logs (user_id, amount, type, operator_id, create_time)
                       VALUES (?, ?, 'payout', 0, ?)''',
                    (user_id, payout, create_time)
                )
                
                logger.info(f"用户 {user_id} 余额更新: {user['balance']:.2f} → {new_balance:.2f} KS")
        
        return has_winners
                
    except Exception as e:
        logger.error(f"结算过程出错: {str(e)}")
        # 回滚事务以防数据不一致
        conn.rollback()
        raise

# 新增管理员（通过用户名）
@super_admin_required
def add_admin_by_username(update: Update, context: CallbackContext) -> None:
    if not context.args or len(context.args) != 1:
        update.message.reply_text('请使用 /jiaren @用户名 格式，例如：/jiaren @Nian288')
        return
    
    username = context.args[0].strip()
    
    # 处理@符号
    if username.startswith('@'):
        username = username[1:]  # 移除@符号
    
    with get_db_connection() as conn:
        # 使用更灵活的查询方式
        user = conn.execute(
            'SELECT id, username, is_admin FROM users WHERE username = ? OR user_id = ?',
            (username, username)
        ).fetchone()
        
        if not user:
            update.message.reply_text(f'用户名 @{username} 不存在')
            return
        
        # 检查是否已经是管理员
        if user['is_admin'] == 1:
            update.message.reply_text(f'用户 @{username} 已经是管理员')
            return
        
        # 设置为管理员
        conn.execute('UPDATE users SET is_admin = 1 WHERE id = ?', (user['id'],))
        conn.commit()
        
        # 验证更新结果
        updated_user = conn.execute('SELECT is_admin FROM users WHERE id = ?', (user['id'],)).fetchone()
        
        if updated_user and updated_user['is_admin'] == 1:
            # 返回帮助中心
            help_text = get_help_text()
            reply_markup = InlineKeyboardMarkup(get_help_center_keyboard())
            update.message.reply_text(f'已成功设置用户 @{username} 为上下分管理员', reply_markup=reply_markup)
            update.message.reply_text(help_text, reply_markup=reply_markup)
        else:
            update.message.reply_text(f'设置管理员失败，请重试')

# 移除管理员权限（通过用户名）
@super_admin_required
def remove_admin_by_username(update: Update, context: CallbackContext) -> None:
    if not context.args or len(context.args) != 1:
        update.message.reply_text('请使用 /shan @用户名 格式，例如：/shan @Nian288')
        return
    
    username = context.args[0].strip()
    
    # 处理@符号
    if username.startswith('@'):
        username = username[1:]  # 移除@符号
    
    with get_db_connection() as conn:
        # 使用更灵活的查询方式
        user = conn.execute(
            'SELECT id, username, is_super_admin, is_admin FROM users WHERE username = ? OR user_id = ?',
            (username, username)
        ).fetchone()
        
        if not user:
            update.message.reply_text(f'用户名 @{username} 不存在')
            return
        
        # 不能移除超级管理员
        if user['is_super_admin'] == 1:
            update.message.reply_text('不能移除超级管理员')
            return
        
        # 检查是否是管理员
        if user['is_admin'] == 0:
            update.message.reply_text(f'用户 @{username} 不是管理员')
            return
        
        # 移除管理员权限
        conn.execute('UPDATE users SET is_admin = 0 WHERE id = ?', (user['id'],))
        conn.commit()
        
        # 验证更新结果
        updated_user = conn.execute('SELECT is_admin FROM users WHERE id = ?', (user['id'],)).fetchone()
        
        if updated_user and updated_user['is_admin'] == 0:
            # 返回帮助中心
            help_text = get_help_text()
            reply_markup = InlineKeyboardMarkup(get_help_center_keyboard())
            update.message.reply_text(f'已成功移除用户 @{username} 的管理员权限', reply_markup=reply_markup)
            update.message.reply_text(help_text, reply_markup=reply_markup)
        else:
            update.message.reply_text(f'移除管理员权限失败，请重试')

# 修改下注金额限制
@admin_required
def set_bet_limits(update: Update, context: CallbackContext) -> None:
    if not context.args or len(context.args) != 4 or context.args[0] != '最小' or context.args[2] != '最大':
        update.message.reply_text('请使用 /setlimits 最小 1000 最大 30000 格式来设置下注金额限制')
        return
    
    try:
        min_bet = int(context.args[1])
        max_bet = int(context.args[3])
    except:
        update.message.reply_text('无效的金额格式，金额必须是数字')
        return
    
    if min_bet <= 0 or max_bet <= min_bet:
        update.message.reply_text('无效的金额范围，最小金额必须大于0，最大金额必须大于最小金额')
        return
    
    with get_db_connection() as conn:
        conn.execute('UPDATE settings SET min_bet = ?, max_bet = ?', (min_bet, max_bet))
        conn.commit()
        
        # 获取当前设置确认更新成功
        new_settings = conn.execute('SELECT min_bet, max_bet FROM settings').fetchone()
        # 获取所有用户的chat_id，发送通知
        users = conn.execute('SELECT DISTINCT chat_id FROM users').fetchall()
        
        # 返回帮助中心
        help_text = get_help_text()
        reply_markup = InlineKeyboardMarkup(get_help_center_keyboard())
        message = f'✅ 系统通知：下注金额限制已更新\n新的限制为 {new_settings["min_bet"]} - {new_settings["max_bet"]} KS'
        update.message.reply_text(message, reply_markup=reply_markup)
        update.message.reply_text(help_text, reply_markup=reply_markup)
        
        # 向所有用户发送通知
        for user in users:
            try:
                context.bot.send_message(chat_id=user['chat_id'], text=message)
            except Exception as e:
                logger.error(f"发送系统通知失败：{e}")

# 处理按钮回调
def button_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()  # 确认收到回调
    
    # 处理投注状态控制
    if query.data == 'start_betting':
        open_betting(update, context)
        return
    elif query.data == 'stop_betting':
        stop_betting(update, context)
        return
    
    user = update.effective_user
    
    with get_db_connection() as conn:
        # 检查用户是否注册
        user_data = conn.execute('SELECT id, username FROM users WHERE user_id = ?', (user.id,)).fetchone()
        if not user_data:
            query.edit_message_text(text="您还没有注册，请发送 /start 进行注册")
            return
        
        user_id = user_data['id']
        username = user_data['username'] or f"用户{user_id}"
        
        # 返回主菜单
        if query.data == 'main_menu':
            main_menu_text = "欢迎使用二手娱乐打手机器人，请选择以下功能："
            reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
            query.edit_message_text(text=main_menu_text, reply_markup=reply_markup)
            return
        
        # 帮助中心
        if query.data == 'help_center':
            help_text = get_help_text()
            reply_markup = InlineKeyboardMarkup(get_help_center_keyboard())
            query.edit_message_text(text=help_text, reply_markup=reply_markup)
            return
        
        # 赔率设置
        if query.data == 'odds_settings':
            response = "🎲 赔率设置\n\n"
            response += "您可以进行以下操作：\n"
            response += "1. 发送 /show 查看当前赔率\n"
            response += "2. 发送 /set <赔率名称> <值> 设置赔率\n"
            response += "   例如: /set daxiao 2"
            reply_markup = InlineKeyboardMarkup(get_odds_settings_keyboard())
            query.edit_message_text(text=response, reply_markup=reply_markup)
            return
        
        # 显示当前赔率
        if query.data == 'show_odds':
            settings = conn.execute('''SELECT odds_daxiao, odds_hezhi, odds_baozi 
                                     FROM settings''').fetchone()
            response = "当前赔率设置：\n"
            response += f"daxiao (大小单双): {settings['odds_daxiao']}\n"
            response += f"hezhi (和值): {settings['odds_hezhi']}\n"
            response += f"baozi (豹子): {settings['odds_baozi']}\n\n"
            response += "设置赔率格式：/set <赔率名称> <值>\n"
            response += "例如: /set daxiao 2"
            reply_markup = InlineKeyboardMarkup(get_odds_settings_keyboard())
            query.edit_message_text(text=response, reply_markup=reply_markup)
            return
        
        # 投注记录
        if query.data == 'bet_records':
            # 调用已有的查询投注记录功能
            time_24h_ago = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
            bets = conn.execute(
                '''SELECT b.round_id, b.bet_type, b.bet_value, b.amount, b.bet_time, b.result, b.payout, r.dice_result, b.status
                   FROM bets b
                   JOIN rounds r ON b.round_id = r.id
                   WHERE b.user_id = ? AND b.bet_time >= ?
                   ORDER BY b.bet_time DESC''',
                (user_id, time_24h_ago)
            ).fetchall()
            
            if not bets:
                response = '您在24小时内没有投注记录'
            else:
                response = "您24小时内的投注记录：\n"
                for bet in bets:
                    status_text = "已取消" if bet['status'] == 'cancelled' else ""
                    result_text = "中奖" if bet['result'] == 'win' else "未中奖" if bet['result'] else "未开奖"
                    if status_text:
                        result_text = status_text
                    payout_text = f"，派彩：{bet['payout']:.2f} KS" if bet['payout'] else ""
                    response += f"- 期号：{bet['round_id']}，类型：{bet['bet_type']} {bet['bet_value']}，金额：{bet['amount']:.2f} KS，时间：{bet['bet_time']}，结果：{result_text}{payout_text}\n"
            
            reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
            query.edit_message_text(text=response, reply_markup=reply_markup)
            return
        
        # 最新开奖
        if query.data == 'latest_result':
            # 获取最近5期的开奖结果
            latest_rounds = conn.execute(
                '''SELECT id, start_time, end_time, dice_result, status
                   FROM rounds 
                   WHERE status = 'closed' AND dice_result IS NOT NULL
                   ORDER BY end_time DESC
                   LIMIT 5''',
            ).fetchall()
            
            if not latest_rounds:
                response = '暂无开奖记录'
            else:
                response = "最新开奖记录（最近5期）：\n\n"
                for round_data in latest_rounds:
                    dice_result = round_data['dice_result']
                    dice_nums = list(map(int, dice_result.split(',')))
                    total = sum(dice_nums)
                    is_leopard = len(set(dice_nums)) == 1
                    size = "大" if total > 10 else "小"
                    odd_even = "单" if total % 2 == 1 else "双"
                    
                    response += f"期号：{round_data['id']}\n"
                    response += f"开奖时间：{round_data['end_time']}\n"
                    response += f"骰子结果：{dice_result}（总和：{total}）\n"
                    response += f"判定结果：{size}、{odd_even}"
                    if is_leopard:
                        response += "，豹子！"
                    response += "\n\n"
            
            reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
            query.edit_message_text(text=response, reply_markup=reply_markup)
            return
        
        # 根据按钮类型处理
        if query.data == 'top_up_withdraw':
            # 获取上分和下分联系方式
            top_up_contact = conn.execute("SELECT contact_info FROM contacts WHERE type = 'top_up'").fetchone()['contact_info']
            withdraw_contact = conn.execute("SELECT contact_info FROM contacts WHERE type = 'withdraw'").fetchone()['contact_info']
            
            response = f"💸 上分/下分方式\n\n"
            response += f"📥 上分联系方式：\n{top_up_contact}\n\n"
            response += f"📤 下分联系方式：\n{withdraw_contact}"
            reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
            query.edit_message_text(text=response, reply_markup=reply_markup)
            
        elif query.data == 'my_balance':
            # 查询用户余额
            balance = conn.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()['balance']
            
            # 计算当日盈亏
            today_start = datetime.now().strftime('%Y-%m-%d 00:00:00')
            today_end = datetime.now().strftime('%Y-%m-%d 23:59:59')
            
            total_payout = conn.execute(
                '''SELECT COALESCE(SUM(amount), 0) as total 
                   FROM balance_logs 
                   WHERE user_id = ? AND type = 'payout' AND amount > 0 
                   AND create_time BETWEEN ? AND ?''',
                (user_id, today_start, today_end)
            ).fetchone()['total']
            
            total_bet = conn.execute(
                '''SELECT COALESCE(ABS(SUM(amount)), 0) as total 
                   FROM balance_logs 
                   WHERE user_id = ? AND type = 'bet' AND amount < 0 
                   AND create_time BETWEEN ? AND ?''',
                (user_id, today_start, today_end)
            ).fetchone()['total']
            
            profit_loss = total_payout - total_bet
            profit_loss_text = f"+{profit_loss:.2f}" if profit_loss >= 0 else f"{profit_loss:.2f}"
            
            response = f"💰 我的余额\n\n"
            response += f"👤 用户：{username}\n"
            response += f"💵 当前余额：{balance:.2f} KS\n"
            response += f"📊 今日盈亏：{profit_loss_text} KS"
            reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
            query.edit_message_text(text=response, reply_markup=reply_markup)
            
        elif query.data == 'my_bets':
            # 查询用户最近的投注记录
            bets = conn.execute(
                '''SELECT b.round_id, b.bet_type, b.bet_value, b.amount, b.bet_time, b.result, b.payout, b.status
                   FROM bets b
                   WHERE b.user_id = ?
                   ORDER BY b.bet_time DESC
                   LIMIT 10''',
                (user_id,)
            ).fetchall()
            
            if not bets:
                response = "您暂无投注记录"
            else:
                response = f"🎲 我的投注记录（最近10条）\n\n"
                for bet in bets:
                    status_text = "已取消" if bet['status'] == 'cancelled' else ""
                    result_text = "中奖" if bet['result'] == 'win' else "未中奖" if bet['result'] else "未开奖"
                    if status_text:
                        result_text = status_text
                    payout_text = f"，派彩：{bet['payout']:.2f} KS" if bet['payout'] else ""
                    response += f"- 期号：{bet['round_id']}，类型：{bet['bet_type']} {bet['bet_value']}，金额：{bet['amount']:.2f} KS，时间：{bet['bet_time']}，结果：{result_text}{payout_text}\n"
            
            reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
            query.edit_message_text(text=response, reply_markup=reply_markup)
            
        elif query.data == 'current_banker':
            # 获取庄家联系方式
            banker_contact = conn.execute("SELECT contact_info FROM contacts WHERE type = 'banker'").fetchone()['contact_info']
            
            response = f"👑 当前庄家联系方式\n\n{banker_contact}"
            reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
            query.edit_message_text(text=response, reply_markup=reply_markup)

# 统一设置联系方式（上分、下分、庄家）
@admin_required
def set_contact(update: Update, context: CallbackContext) -> None:
    if not context.args or len(context.args) < 1:
        update.message.reply_text('请使用 /jia @用户名 格式，例如：/jia @Nian288')
        return
    
    # 提取用户名
    contact_info = context.args[0].strip()
    
    # 确保联系方式以@开头
    if not contact_info.startswith('@'):
        contact_info = '@' + contact_info
    
    with get_db_connection() as conn:
        # 获取操作员ID
        operator_id = conn.execute('SELECT id FROM users WHERE user_id = ?', (update.effective_user.id,)).fetchone()['id']
        update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 同时更新三种联系方式
        conn.execute(
            '''UPDATE contacts 
               SET contact_info = ?, update_time = ?, updated_by = ? 
               WHERE type = 'top_up' ''',
            (contact_info, update_time, operator_id)
        )
        
        conn.execute(
            '''UPDATE contacts 
               SET contact_info = ?, update_time = ?, updated_by = ? 
               WHERE type = 'withdraw' ''',
            (contact_info, update_time, operator_id)
        )
        
        conn.execute(
            '''UPDATE contacts 
               SET contact_info = ?, update_time = ?, updated_by = ? 
               WHERE type = 'banker' ''',
            (contact_info, update_time, operator_id)
        )
        
        conn.commit()
        
        # 返回帮助中心
        help_text = get_help_text()
        reply_markup = InlineKeyboardMarkup(get_help_center_keyboard())
        update.message.reply_text(f'已成功更新上下分及庄家联系方式为：{contact_info}', reply_markup=reply_markup)
        update.message.reply_text(help_text, reply_markup=reply_markup)

# 删除联系方式（上分、下分、庄家）
@admin_required
def delete_contact(update: Update, context: CallbackContext) -> None:
    if not context.args or len(context.args) < 1:
        update.message.reply_text('请使用 /Delete @用户名 格式，例如：/Delete @Nian288')
        return
    
    # 提取用户名
    contact_info = context.args[0].strip()
    
    # 确保联系方式以@开头
    if not contact_info.startswith('@'):
        contact_info = '@' + contact_info
    
    with get_db_connection() as conn:
        # 检查该联系方式是否存在
        top_up_exists = conn.execute(
            "SELECT id FROM contacts WHERE type = 'top_up' AND contact_info = ?",
            (contact_info,)
        ).fetchone()
        
        withdraw_exists = conn.execute(
            "SELECT id FROM contacts WHERE type = 'withdraw' AND contact_info = ?",
            (contact_info,)
        ).fetchone()
        
        banker_exists = conn.execute(
            "SELECT id FROM contacts WHERE type = 'banker' AND contact_info = ?",
            (contact_info,)
        ).fetchone()
        
        if not top_up_exists and not withdraw_exists and not banker_exists:
            update.message.reply_text(f'未找到联系方式：{contact_info}')
            return
        
        # 获取操作员ID
        operator_id = conn.execute('SELECT id FROM users WHERE user_id = ?', (update.effective_user.id,)).fetchone()['id']
        update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 重置为默认值
        default_top_up = '请联系管理员设置上分方式'
        default_withdraw = '请联系管理员设置下分方式'
        default_banker = '请联系管理员设置庄家联系方式'
        
        # 同时删除三种联系方式（重置为默认值）
        if top_up_exists:
            conn.execute(
                '''UPDATE contacts 
                   SET contact_info = ?, update_time = ?, updated_by = ? 
                   WHERE type = 'top_up' ''',
                (default_top_up, update_time, operator_id)
            )
        
        if withdraw_exists:
            conn.execute(
                '''UPDATE contacts 
                   SET contact_info = ?, update_time = ?, updated_by = ? 
                   WHERE type = 'withdraw' ''',
                (default_withdraw, update_time, operator_id)
            )
        
        if banker_exists:
            conn.execute(
                '''UPDATE contacts 
                   SET contact_info = ?, update_time = ?, updated_by = ? 
                   WHERE type = 'banker' ''',
                (default_banker, update_time, operator_id)
            )
        
        conn.commit()
        
        # 返回帮助中心
        help_text = get_help_text()
        reply_markup = InlineKeyboardMarkup(get_help_center_keyboard())
        update.message.reply_text(f'已成功删除联系方式：{contact_info}', reply_markup=reply_markup)
        update.message.reply_text(help_text, reply_markup=reply_markup)

# 查看管理员列表
@admin_required
def check_admins(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    
    with get_db_connection() as conn:
        # 检查用户是否是超级管理员
        is_super = conn.execute(
            'SELECT is_super_admin FROM users WHERE user_id = ?',
            (user.id,)
        ).fetchone()['is_super_admin']
        
        # 查询所有管理员
        if is_super:
            # 超级管理员可以看到所有管理员，包括超级管理员
            admins = conn.execute(
                '''SELECT id, username, user_id, is_super_admin 
                   FROM users 
                   WHERE is_admin = 1 OR is_super_admin = 1
                   ORDER BY is_super_admin DESC, id'''
            ).fetchall()
        else:
            # 普通管理员只能看到其他普通管理员，看不到超级管理员
            admins = conn.execute(
                '''SELECT id, username, user_id 
                   FROM users 
                   WHERE is_admin = 1 AND is_super_admin = 0
                   ORDER BY id'''
            ).fetchall()
        
        if not admins:
            response = '当前没有管理员'
        else:
            if is_super:
                response = "📋 所有管理员列表（包括超级管理员）：\n\n"
                for admin in admins:
                    role = "超级管理员" if admin['is_super_admin'] == 1 else "普通管理员"
                    username = f"@{admin['username']}" if admin['username'] else f"ID{admin['id']}"
                    response += f"- ID{admin['id']}：{username}（{role}）\n"
            else:
                response = "📋 当前管理员列表：\n\n"
                for admin in admins:
                    username = f"@{admin['username']}" if admin['username'] else f"ID{admin['id']}"
                    response += f"- ID{admin['id']}：{username}\n"
        
        # 返回帮助中心
        help_text = get_help_text()
        reply_markup = InlineKeyboardMarkup(get_help_center_keyboard())
        update.message.reply_text(response, reply_markup=reply_markup)
        update.message.reply_text(help_text, reply_markup=reply_markup)

# 获取帮助文本
def get_help_text():
    return """
    📋 二手娱乐打手机器人操作指南
    
    一、基础功能
    /start - 注册账号，生成唯一ID
             示例：发送 /start 即可注册账号
    /help  - 查看帮助信息
             示例：发送 /help 即可查看所有指令说明
    
    二、账户管理
    1      - 查询账户详情（含当日充值、提款、盈亏、当前余额）
             示例：发送 1 即可查看自己的账户信息
    /totaldata - 查询系统全量数据（所有注册用户均可使用）
             示例：发送 /totaldata 即可查看系统总数据
    
    三、投注相关
    22     - 查自己24小时内投注记录
             示例：发送 22 即可查看自己的投注记录
    33     - 查所有玩家24小时内历史投注记录（仅管理员）
             示例：发送 33 即可查看所有玩家的投注记录
    
    四、下注方式
    1. 大小单双：
       - 大1000、单 2000、小3000（空格不影响）
       示例：发送 "大1000" 或 "单 2000" 即可下注
    2. 组合投注：
       - 大单2000（=大2000+单2000）
       - 小双 3000（=小3000+双3000）
       示例：发送 "大单2000" 即可同时下注大2000和单2000
    3. 和值：
       - 11 1000、8 5000（必须用空格分隔数字和金额）
       示例：发送 "11 1000" 即下注和值为11，金额1000
    4. 豹子：
       - 豹子1000、豹子 2000（空格不影响）
       示例：发送 "豹子1000" 即下注豹子，金额1000
    
    五、开奖方式
    1. 一次性发送三个骰子表情（1️⃣-6️⃣或🎲的任意组合）
       示例：发送 "1️⃣2️⃣3️⃣" 即可开奖
    2. 连续发送三个Telegram内置骰子，系统会自动识别并在收到第三个时开奖
       示例：连续发送三次🎲表情即可开奖
    
    六、撤销投注
    取消 - 引用指定投注消息发送"取消"，可撤掉指定投注内容、保留其余的投注
           直接发送"取消"，可取消当前没有开奖前投注的所有投注内容、投注金额原路返回账户
           示例：引用自己的投注消息后发送"取消"或直接发送"取消"
    
    七、管理员指令
    1. 上下分操作：
       - 引用用户消息后发送 "+金额" 或 "-金额" - 给用户上下分
         示例：引用用户消息后发送 "+1000" 给该用户上分1000
       - 发送 "IDxxx +金额" 或 "IDxxx -金额" - 给指定ID用户上下分
         示例：发送 "ID123 +5000" 给ID123的用户上分5000
       - /kou @用户名 金额 - 给指定用户名用户下分
         示例：/kou @Nian288 1000 给用户@Nian288下分1000
    2. 权限管理：
       - /cha - 查询当前所有管理员
         示例：发送 /cha 即可查看所有管理员
       - /jiaren @用户名 - 新增管理员（仅超级管理员）
         示例：/jiaren @Nian288 把@Nian288设为管理员
       - /shan @用户名 - 移除管理员（仅超级管理员）
         示例：/shan @Nian288 移除@Nian288的管理员权限
    3. 联系方式管理：
       - /jia @用户名 - 统一设置上分、下分和庄家联系方式
         示例：/jia @Nian288 设置@Nian288为上下分和庄家联系人
       - /Delete @用户名 - 统一删除上分、下分和庄家联系方式
         示例：/Delete @Nian288 移除@Nian288的联系方式设置
    4. 系统设置：
       - /setlimits 最小 1000 最大 30000 - 修改下注金额限制
         示例：/setlimits 最小 500 最大 50000 设置最小下注500，最大50000
       - /shetu - 设置开奖时显示的图片、视频等媒体
         示例：发送 /shetu 后，再发送一张图片即可设置为开奖图片
       - /tihuan - 替换或清除开奖媒体
         示例：发送 /tihuan 后，选择"清除图片"按钮即可清除所有开奖媒体
       - /chayue - 查看所有账户有两位数的余额列表
         示例：发送 /chayue 即可查看余额为10-99的账户
       - /qingchu confirm - 清空所有账户余额（仅超级管理员）
         示例：发送 /qingchu confirm 确认清空所有用户余额
       - /chat 允许/禁止 - 设置是否允许无关消息
         示例：/chat 允许 开启自由聊天；/chat 禁止 关闭无关消息
       - /open - 开启投注
         示例：发送 /open 开启新一轮投注
       - /stop - 停止投注
         示例：发送 /stop 暂停当前投注
    5. 赔率管理：
       - /show - 显示当前赔率设置
         示例：发送 /show 查看当前赔率
       - /set <赔率名称> <值> - 设置赔率
         示例：/set daxiao 2 设置大小单双赔率为2
    """

# 帮助命令
def help_command(update: Update, context: CallbackContext) -> None:
    help_text = get_help_text()
    reply_markup = InlineKeyboardMarkup(get_help_center_keyboard())
    update.message.reply_text(help_text, reply_markup=reply_markup)

# 开始设置开奖媒体
@admin_required
def start_set_winning_media(update: Update, context: CallbackContext) -> int:
    update.message.reply_text('请回复图片、视频、贴纸或动画表情来设置为开奖时显示的媒体（第一张为中奖图，第二张为不中奖图）')
    return SET_WINNING_IMAGE

# 处理中奖媒体
def handle_winning_image(update: Update, context: CallbackContext) -> int:
    message = update.effective_message
    file_id = None
    file_type = None
    
    # 检查消息类型
    if message.photo:
        # 取最高分辨率的图片
        file_id = message.photo[-1].file_id
        file_type = 'photo'
    elif message.video:
        file_id = message.video.file_id
        file_type = 'video'
    elif message.sticker:
        file_id = message.sticker.file_id
        file_type = 'sticker'
    elif message.animation:
        file_id = message.animation.file_id
        file_type = 'animation'
    else:
        update.message.reply_text('不支持的媒体类型，请发送图片、视频、贴图或动画表情作为第一张中奖媒体')
        return SET_WINNING_IMAGE
    
    # 保存中奖媒体到数据库
    with get_db_connection() as conn:
        # 获取操作员ID
        operator_id = conn.execute('SELECT id FROM users WHERE user_id = ?', (update.effective_user.id,)).fetchone()['id']
        added_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 先删除旧的中奖媒体
        conn.execute("DELETE FROM winning_media WHERE media_type = 'win'")
        
        # 添加新的中奖媒体
        conn.execute(
            '''INSERT INTO winning_media (file_id, file_type, media_type, added_time, added_by)
               VALUES (?, ?, 'win', ?, ?)''',
            (file_id, file_type, added_time, operator_id)
        )
        
        conn.commit()
        
        update.message.reply_text('已成功添加中奖媒体，请发送第二张图片、视频、贴纸或动画表情作为不中奖媒体')
        return SET_LOSING_IMAGE

# 处理不中奖媒体
def handle_losing_image(update: Update, context: CallbackContext) -> int:
    message = update.effective_message
    file_id = None
    file_type = None
    
    # 检查消息类型
    if message.photo:
        # 取最高分辨率的图片
        file_id = message.photo[-1].file_id
        file_type = 'photo'
    elif message.video:
        file_id = message.video.file_id
        file_type = 'video'
    elif message.sticker:
        file_id = message.sticker.file_id
        file_type = 'sticker'
    elif message.animation:
        file_id = message.animation.file_id
        file_type = 'animation'
    else:
        update.message.reply_text('不支持的媒体类型，请发送图片、视频、贴图或动画表情作为第二张不中奖媒体')
        return SET_LOSING_IMAGE
    
    # 保存不中奖媒体到数据库
    with get_db_connection() as conn:
        # 获取操作员ID
        operator_id = conn.execute('SELECT id FROM users WHERE user_id = ?', (update.effective_user.id,)).fetchone()['id']
        added_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 先删除旧的不中奖媒体
        conn.execute("DELETE FROM winning_media WHERE media_type = 'lose'")
        
        # 添加新的不中奖媒体
        conn.execute(
            '''INSERT INTO winning_media (file_id, file_type, media_type, added_time, added_by)
               VALUES (?, ?, 'lose', ?, ?)''',
            (file_id, file_type, added_time, operator_id)
        )
        
        conn.commit()
        
        # 返回帮助中心
        help_text = get_help_text()
        reply_markup = InlineKeyboardMarkup(get_help_center_keyboard())
        update.message.reply_text('已成功添加不中奖媒体！', reply_markup=reply_markup)
        update.message.reply_text(help_text, reply_markup=reply_markup)
        
        return ConversationHandler.END

# 取消设置媒体
def cancel_set_media(update: Update, context: CallbackContext) -> int:
    update.message.reply_text('已取消设置开奖媒体')
    return ConversationHandler.END

# 替换或清除开奖媒体
@admin_required
def replace_winning_media(update: Update, context: CallbackContext) -> None:
    # 创建清除和返回按钮
    keyboard = [
        [InlineKeyboardButton("清除中奖图片/视频", callback_data='clear_win_media')],
        [InlineKeyboardButton("清除不中奖图片/视频", callback_data='clear_lose_media')],
        [InlineKeyboardButton("返回帮助中心", callback_data='help_center')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text('请选择操作：', reply_markup=reply_markup)

# 清除开奖媒体
def clear_winning_media(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()
    
    media_type = 'win' if query.data == 'clear_win_media' else 'lose'
    media_name = '中奖' if media_type == 'win' else '不中奖'
    
    with get_db_connection() as conn:
        conn.execute('DELETE FROM winning_media WHERE media_type = ?', (media_type,))
        conn.commit()
        
        # 返回帮助中心
        help_text = get_help_text()
        reply_markup = InlineKeyboardMarkup(get_help_center_keyboard())
        query.edit_message_text(text=f'已成功清除所有{media_name}图片/视频！', reply_markup=reply_markup)
        
        # 发送帮助中心信息
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text=help_text,
            reply_markup=reply_markup
        )

# 查看两位数余额账户
@admin_required
def view_two_digit_balances(update: Update, context: CallbackContext) -> None:
    with get_db_connection() as conn:
        # 查询余额为两位数的用户（10-99之间）
        users = conn.execute(
            '''SELECT id, username, user_id, balance 
               FROM users 
               WHERE balance >= 10 AND balance <= 99
               ORDER BY balance DESC'''
        ).fetchall()
        
        if not users:
            response = '没有余额为两位数的账户'
        else:
            response = "📋 余额为两位数的账户列表：\n\n"
            for user in users:
                username = f"@{user['username']}" if user['username'] else f"未知用户"
                response += f"- ID{user['id']}：{username}，余额：{user['balance']:.2f} KS\n"
        
        # 返回帮助中心
        help_text = get_help_text()
        reply_markup = InlineKeyboardMarkup(get_help_center_keyboard())
        update.message.reply_text(response, reply_markup=reply_markup)
        update.message.reply_text(help_text, reply_markup=reply_markup)

# 指定用户名下分
@admin_required
def deduct_by_username(update: Update, context: CallbackContext) -> None:
    # 解析命令格式：/kou @用户名 金额
    command = update.message.text.strip()
    pattern = re.compile(r'^/kou\s+@(.+?)\s+(\d+)$')
    match = pattern.match(command)
    
    if not match:
        update.message.reply_text('请使用 /kou @用户名 金额 格式，例如：/kou @Nian288 1000')
        return
    
    username = match.group(1).strip()
    try:
        amount = int(match.group(2))
        if amount <= 0:
            update.message.reply_text('金额必须为正数')
            return
    except ValueError:
        update.message.reply_text('无效的金额格式')
        return
    
    with get_db_connection() as conn:
        # 查找用户
        user = conn.execute(
            'SELECT id, balance, username FROM users WHERE username = ?',
            (username,)
        ).fetchone()
        
        # 如果通过用户名没找到，尝试通过user_id查找
        if not user:
            try:
                user_id = int(username)
                user = conn.execute(
                    'SELECT id, balance, username FROM users WHERE user_id = ?',
                    (user_id,)
                ).fetchone()
            except ValueError:
                pass
        
        if not user:
            update.message.reply_text(f'用户名 @{username} 不存在')
            return
        
        user_id = user['id']
        current_balance = user['balance']
        target_username = user['username'] or f"ID{user_id}"
        
        # 检查余额是否足够
        if current_balance < amount:
            update.message.reply_text(f'操作失败，用户 @{target_username} 当前余额 {current_balance:.2f} KS，无法下分 {amount:.2f} KS')
            return
        
        # 获取操作员ID
        operator_id = conn.execute('SELECT id FROM users WHERE user_id = ?', (update.effective_user.id,)).fetchone()['id']
        
        # 更新余额（下分操作，金额为负数）
        new_balance = current_balance - amount
        conn.execute(
            'UPDATE users SET balance = ? WHERE id = ?',
            (new_balance, user_id)
        )
        
        # 记录下分到收支表
        create_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            '''INSERT INTO balance_logs (user_id, amount, type, operator_id, create_time)
               VALUES (?, ?, 'withdraw', ?, ?)''',
            (user_id, -amount, operator_id, create_time)
        )
        
        conn.commit()
        
        # 返回帮助中心
        help_text = get_help_text()
        reply_markup = InlineKeyboardMarkup(get_main_menu_keyboard())
        update.message.reply_text(f'✅ 下分成功！\n'
                              f'👤 目标用户：@{target_username}（ID{user_id}）\n'
                              f'💰 原余额：{current_balance:.2f} KS\n'
                              f'📊 下分金额：{amount:.2f} KS\n'
                              f'💵 新余额：{new_balance:.2f} KS\n'
                              f'👑 操作员：@{update.effective_user.username or f"管理员{operator_id}"}',
                              reply_markup=reply_markup)
        update.message.reply_text(help_text, reply_markup=reply_markup)

# 清空所有用户余额（仅超级管理员）
@super_admin_required
def clear_all_balances(update: Update, context: CallbackContext) -> None:
    # 二次确认保护
    if not context.args or context.args[0] != 'confirm':
        update.message.reply_text('⚠️ 警告：此操作将清空所有用户的余额！\n如果确定要执行，请使用 /qingchu confirm 命令')
        return
    
    with get_db_connection() as conn:
        # 记录所有用户清空前的余额
        users = conn.execute('SELECT id, balance FROM users').fetchall()
        operator_id = conn.execute('SELECT id FROM users WHERE user_id = ?', (update.effective_user.id,)).fetchone()['id']
        create_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 记录余额日志并清空余额
        for user in users:
            if user['balance'] > 0:
                # 记录清空操作到收支表
                conn.execute(
                    '''INSERT INTO balance_logs (user_id, amount, type, operator_id, create_time)
                       VALUES (?, ?, 'admin_clear', ?, ?)''',
                    (user['id'], -user['balance'], operator_id, create_time)
                )
        
        # 清空所有用户余额
        conn.execute('UPDATE users SET balance = 0')
        conn.commit()
        
        # 返回帮助中心
        help_text = get_help_text()
        reply_markup = InlineKeyboardMarkup(get_help_center_keyboard())
        update.message.reply_text('已成功清空所有用户的余额', reply_markup=reply_markup)
        update.message.reply_text(help_text, reply_markup=reply_markup)

# 撤销投注
def cancel_bet(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    message = update.effective_message
    
    # 检查用户是否注册
    with get_db_connection() as conn:
        db_user = conn.execute('SELECT id, username FROM users WHERE user_id = ?', (user.id,)).fetchone()
        if not db_user:
            message.reply_text('您还没有注册，请发送 /start 进行注册')
            return
        
        user_id = db_user['id']
        username = db_user['username'] or f"用户{user_id}"
        
        # 获取当前活跃轮次
        active_round_id = get_active_round()
        if not active_round_id:
            message.reply_text('当前没有活跃的投注轮次，无需撤销')
            return
        
        # 检查轮次是否仍可撤销（必须是open状态）
        round_status = conn.execute(
            "SELECT status FROM rounds WHERE id = ?",
            (active_round_id,)
        ).fetchone()['status']
        
        if round_status != 'open':
            message.reply_text(f'当前轮次 {active_round_id} 已结束，无法撤销投注')
            return
        
        total_refund = 0
        is_specific = False  # 是否是撤销特定投注
        
        # 检查是否引用了某条消息
        if message.reply_to_message:
            is_specific = True
            # 尝试撤销引用的特定投注
            replied_text = message.reply_to_message.text or ""
            
            # 从回复消息中提取投注信息
            bet_info = parse_bet(replied_text)
            if not bet_info:
                message.reply_text('无法识别引用消息中的投注内容，撤销失败')
                return
            
            # 查找匹配的投注
            for bet in bet_info:
                # 查找对应的投注记录
                bet_record = conn.execute(
                    '''SELECT id, amount FROM bets 
                       WHERE user_id = ? AND round_id = ? AND bet_type = ? AND bet_value = ? 
                       AND amount = ? AND status = 'active' ''',
                    (user_id, active_round_id, bet['type'], bet['value'], bet['amount'])
                ).fetchone()
                
                if bet_record:
                    # 标记投注为已取消
                    conn.execute(
                        "UPDATE bets SET status = 'cancelled' WHERE id = ?",
                        (bet_record['id'],)
                    )
                    
                    # 退还金额
                    total_refund += bet_record['amount']
                    
                    # 记录退款到收支表
                    create_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    conn.execute(
                        '''INSERT INTO balance_logs (user_id, amount, type, operator_id, create_time)
                           VALUES (?, ?, 'refund', 0, ?)''',
                        (user_id, bet_record['amount'], create_time)
                    )
        else:
            # 撤销当前轮次所有投注
            bets = conn.execute(
                '''SELECT id, amount FROM bets 
                   WHERE user_id = ? AND round_id = ? AND status = 'active' ''',
                (user_id, active_round_id)
            ).fetchall()
            
            if not bets:
                message.reply_text(f'您在当前轮次 {active_round_id} 没有投注，无需撤销')
                return
            
            # 计算总退款金额
            total_refund = sum(bet['amount'] for bet in bets)
            
            # 标记所有投注为已取消
            for bet in bets:
                conn.execute(
                    "UPDATE bets SET status = 'cancelled' WHERE id = ?",
                    (bet['id'],)
                )
            
            # 记录退款到收支表
            create_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn.execute(
                '''INSERT INTO balance_logs (user_id, amount, type, operator_id, create_time)
                   VALUES (?, ?, 'refund', 0, ?)''',
                (user_id, total_refund, create_time)
            )
        
        if total_refund > 0:
            # 更新用户余额
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE id = ?",
                (total_refund, user_id)
            )
            
            conn.commit()
            
            response = f"✅ 撤销成功！\n"
            response += f"🎯 期号：{active_round_id}\n"
            response += f"👤 用户：{username}\n"
            response += f"💰 已退还金额：{total_refund:.2f} KS\n"
            
            if is_specific:
                response += "📝 已撤销指定投注，保留其他投注"
            else:
                response += "📝 已撤销当前轮次所有投注"
            
            message.reply_text(response)
            logger.info(f"用户 {user_id} 撤销了期号 {active_round_id} 的投注，退款 {total_refund:.2f} KS")
        else:
            message.reply_text('未找到可撤销的投注')

# 显示当前赔率
@admin_required
def show_current_odds(update: Update, context: CallbackContext) -> None:
    with get_db_connection() as conn:
        settings = conn.execute('''SELECT odds_daxiao, odds_hezhi, odds_baozi 
                                 FROM settings''').fetchone()
        
        response = "当前赔率设置：\n"
        response += f"daxiao (大小单双): {settings['odds_daxiao']}\n"
        response += f"hezhi (和值): {settings['odds_hezhi']}\n"
        response += f"baozi (豹子): {settings['odds_baozi']}\n\n"
        response += "设置赔率格式：/set <赔率名称> <值>\n"
        response += "例如: /set daxiao 2"
        
        reply_markup = InlineKeyboardMarkup(get_odds_settings_keyboard())
        update.message.reply_text(response, reply_markup=reply_markup)

# 设置赔率值
@super_admin_required
def set_odds_value(update: Update, context: CallbackContext) -> None:
    if len(context.args) < 2:
        update.message.reply_text('参数错误，请使用格式：/set <赔率名称> <值>\n例如: /set daxiao 2')
        return
    
    odds_name = context.args[0].lower()
    try:
        odds_value = int(context.args[1])
        if odds_value <= 0:
            update.message.reply_text('赔率值必须为正数')
            return
    except ValueError:
        update.message.reply_text('赔率值必须为整数')
        return
    
    # 验证赔率名称
    valid_names = ['daxiao', 'hezhi', 'baozi']
    if odds_name not in valid_names:
        update.message.reply_text(f'无效的赔率名称，有效名称: {", ".join(valid_names)}')
        return
    
    # 更新数据库
    with get_db_connection() as conn:
        conn.execute(f"UPDATE settings SET odds_{odds_name} = ?", (odds_value,))
        conn.commit()
        
        update.message.reply_text(f'成功更新{odds_name}赔率为: {odds_value}')

# 获取开奖媒体
def get_winning_media(media_type: str = 'win'):
    with get_db_connection() as conn:
        media = conn.execute(
            "SELECT file_id, file_type FROM winning_media WHERE media_type = ? ORDER BY added_time DESC LIMIT 1",
            (media_type,)
        ).fetchone()
        
        return (media['file_id'], media['file_type']) if media else (None, None)

# 检查轮次是否活跃
def check_round_active(round_id: str) -> bool:
    with get_db_connection() as conn:
        round_data = conn.execute(
            "SELECT status FROM rounds WHERE id = ?",
            (round_id,)
        ).fetchone()
        return round_data and round_data['status'] == 'open'

# 主函数
def main() -> None:
    # 从环境变量获取机器人令牌
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        logger.error("未设置TELEGRAM_BOT_TOKEN环境变量")
        return
    
    # 获取Webhook URL和端口
    webhook_url = os.environ.get('WEBHOOK_URL')
    port = int(os.environ.get('PORT', 8443))  # Render.com使用PORT环境变量
    
    updater = Updater(token)
    
    dp = updater.dispatcher
    
    # 设置开奖媒体的对话处理
    set_media_conv = ConversationHandler(
        entry_points=[CommandHandler('shetu', start_set_winning_media)],
        states={
            SET_WINNING_IMAGE: [MessageHandler(Filters.photo | Filters.video | Filters.sticker | Filters.animation, handle_winning_image)],
            SET_LOSING_IMAGE: [MessageHandler(Filters.photo | Filters.video | Filters.sticker | Filters.animation, handle_losing_image)]
        },
        fallbacks=[CommandHandler('cancel', cancel_set_media)]
    )
    
    # 注册命令处理器 - 按优先级排序
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("balance", check_balance))
    dp.add_handler(CommandHandler("mybets", check_my_bet_history))
    dp.add_handler(CommandHandler("allbets", check_all_bet_history))
    dp.add_handler(CommandHandler("totaldata", check_total_data))
    dp.add_handler(CommandHandler("setlimits", set_bet_limits))
    dp.add_handler(CommandHandler("jiaren", add_admin_by_username))
    dp.add_handler(CommandHandler("shan", remove_admin_by_username))
    dp.add_handler(CommandHandler("cha", check_admins))
    dp.add_handler(CommandHandler("jia", set_contact))
    dp.add_handler(CommandHandler("Delete", delete_contact))
    dp.add_handler(CommandHandler("tihuan", replace_winning_media))
    dp.add_handler(CommandHandler("chayue", view_two_digit_balances))
    dp.add_handler(CommandHandler("kou", deduct_by_username))
    dp.add_handler(CommandHandler("qingchu", clear_all_balances))
    dp.add_handler(CommandHandler("chat", set_allow_irrelevant))
    dp.add_handler(CommandHandler("show", show_current_odds))
    dp.add_handler(CommandHandler("set", set_odds_value))
    dp.add_handler(CommandHandler("open", open_betting))
    dp.add_handler(CommandHandler("stop", stop_betting))
    dp.add_handler(set_media_conv)
    
    # 优先处理取消命令，特别是引用消息的情况
    dp.add_handler(MessageHandler(Filters.reply & Filters.text & Filters.regex('^取消$'), cancel_bet))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^取消$'), cancel_bet))

    # 然后处理1查询余额、22查询个人投注、33查询所有投注
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^1$'), check_balance))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^22$'), check_my_bet_history))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex('^33$'), check_all_bet_history))

    # 再处理骰子
    dp.add_handler(MessageHandler(Filters.dice, handle_dice))

    # 最后处理上下分操作
    dp.add_handler(MessageHandler(Filters.reply & Filters.text & ~Filters.command, adjust_balance))
    dp.add_handler(MessageHandler(Filters.text & Filters.regex(r'^ID\d+\s*[+-]\d+$'), adjust_balance))
    
    # 先处理明显无关的消息
    dp.add_handler(MessageHandler(
        Filters.text & ~Filters.command & 
        ~Filters.regex(r'(大|小|单|双|豹子|\d+)\s*\d+'),  # 排除可能包含投注关键词的消息
        handle_irrelevant_message
    ))
    
    # 仅处理可能包含投注的消息
    dp.add_handler(MessageHandler(
        Filters.text & ~Filters.command & 
        Filters.regex(r'(大|小|单|双|豹子|\d+)\s*\d+'),  # 只处理包含投注关键词的消息
        process_bet
    ))
    
    # 最后处理其他文本消息
    dp.add_handler(MessageHandler(Filters.text, handle_irrelevant_message))
    
    # 注册回调处理器
    dp.add_handler(CallbackQueryHandler(button_callback))
    dp.add_handler(CallbackQueryHandler(clear_winning_media, pattern='^clear_win_media$|^clear_lose_media$'))
    
    # 在Render.com上使用Webhook而不是长轮询
    if webhook_url:
        updater.start_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,
            webhook_url=f"{webhook_url}/{token}"
        )
        logger.info(f"Webhook started on port {port}, URL: {webhook_url}/{token}")
    else:
        # 本地开发时使用长轮询
        updater.start_polling()
        logger.info("Polling started")
    
    updater.idle()

if __name__ == '__main__':
    main()
