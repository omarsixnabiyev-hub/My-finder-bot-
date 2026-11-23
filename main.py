import asyncio
import logging
import random
import re
import os
from datetime import datetime, date, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
from telethon import TelegramClient, functions
from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameNotOccupiedError

# ==========================================
# ⚙️ НАСТРОЙКИ 
# ==========================================
API_ID = 33952577
API_HASH = "17e71b8962c1ff0ec51f43d1d0d84818"
ADMIN_ID = 7671622682
BOT_TOKEN = "9137311860:AAGRF90WEywmqxkSevQ1HXp7MoSc8rBpkFE"
CHANNEL_URL = "https://t.me/твой_канал"

FREE_LIMIT = 5          
PREMIUM_PRICE = 30      
PREMIUM_DAYS = 30       
DB_NAME = "bot_database.db"
SESSION_NAME = "worker_session" 

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ==========================================
# 🗄️ БАЗА ДАННЫХ (SQLite)
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                premium_until TEXT,
                free_used INTEGER DEFAULT 0,
                last_reset TEXT
            )
        """)
        await db.commit()

async def register_user(user_id: int, username: str):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username, last_reset) VALUES (?, ?, ?)", (user_id, username or "", today))
        await db.commit()

async def check_limit(user_id: int) -> tuple[bool, str, int]:
    if user_id == ADMIN_ID:
        return True, "admin", 999
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT premium_until, free_used, last_reset FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            await register_user(user_id, "")
            return True, "new", FREE_LIMIT
        premium_until, free_used, last_reset = row
        if premium_until and datetime.fromisoformat(premium_until) > datetime.utcnow():
            return True, "premium", 999
        if last_reset != today:
            await db.execute("UPDATE users SET free_used = 0, last_reset = ? WHERE user_id = ?", (today, user_id))
            await db.commit()
            free_used = 0
        if free_used >= FREE_LIMIT:
            return False, "limit", 0
        return True, "free", FREE_LIMIT - free_used

async def increment_usage(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET free_used = free_used + 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def give_premium(user_id: int, days: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        now = datetime.utcnow()
        existing = row[0]
        start = datetime.fromisoformat(existing) if existing and datetime.fromisoformat(existing) > now else now
        new_until = start + timedelta(days=days)
        await db.execute("UPDATE users SET premium_until = ? WHERE user_id = ?", (new_until.isoformat(), user_id))
        await db.commit()
        return True

# ==========================================
# 🧠 ГЕНЕРАТОР И ФИЛЬТР ЮЗЕРНЕЙМОВ
# ==========================================
ROOTS = ["dark", "neon", "wolf", "king", "gold", "blue", "life", "time", "code", "hero", "nova", "luna", "aura", "zero", "echo", "rift", "flux", "core", "dash", "shop", "game", "crypto", "alpha", "beta", "pro", "top", "moon", "star", "fire", "ice", "sky", "wild", "cyber"]
SUFFIXES = ["ly", "er", "io", "x", "us", "on", "ic", "ify", "eo"]
VOWELS = "aeiouy"
CONSONANTS = "bcdfghjklmnpqrstvwxz"

def generate_candidates(count: int = 50) -> list[str]:
    candidates = set()
    while len(candidates) < count:
        choice = random.random()
        if choice < 0.4:
            root = random.choice(ROOTS)
            word = root if random.random() < 0.5 else root + random.choice(SUFFIXES)
        else:
            length = random.choice([4, 5, 6, 7])
            word = ""
            for i in range(length):
                word += random.choice(CONSONANTS) if i % 2 == 0 else random.choice(VOWELS)
        if 4 <= len(word) <= 7:
            candidates.add(word)
    return list(candidates)

def score_username(username: str) -> tuple[bool, str]:
    u = username.lower().strip()
    if len(u) < 4 or len(u) > 7: return False, "длина"
    if not re.fullmatch(r"[a-z]+", u): return False, "цифры/символы"
    if len(set(u)) != len(u): return False, "повторы"
    vowels = set("aeiouy")
    pattern = "".join("v" if ch in vowels else "c" for ch in u)
    if re.search(r"(vvv|ccc)", pattern): return False, "плохое чередование"
    return True, f"длина {len(u)}, звучное"

# ==========================================
# 🕵️‍♂️ ПУЛ АККАУНТОВ TELETHON (ПРОВЕРКА)
# ==========================================
class AccountPool:
    def __init__(self):
        self.client = None
        self.queue = asyncio.Queue()

    async def start(self):
        self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            logging.info("ТРЕБУЕТСЯ АВТОРИЗАЦИЯ! Введи номер и код в консоли.")
            await self.client.start()
        self.queue.put_nowait(self.client)
        logging.info("✅ Аккаунт для проверок готов!")

    async def check(self, username: str) -> dict:
        client = await self.queue.get()
        is_flood = False
        try:
            result = await client(functions.account.CheckUsernameRequest(username=username))
            return {"status": "available" if result else "taken", "username": username}
        except UsernameInvalidError:
            return {"status": "invalid", "username": username}
        except UsernameNotOccupiedError:
            return {"status": "available", "username": username}
        except FloodWaitError as e:
            is_flood = True
            logging.warning(f"🛑 FloodWait {e.seconds} сек. Ждем...")
            asyncio.create_task(self._cooldown(client, e.seconds))
            return {"status": "flood", "username": username}
        finally:
            if not is_flood:
                await asyncio.sleep(3) 
                self.queue.put_nowait(client)

    async def _cooldown(self, client, seconds):
        await asyncio.sleep(seconds)
        self.queue.put_nowait(client)

# ==========================================
# 🤖 БОТ AIOGRAM (ХЕНДЛЕРЫ)
# ==========================================
router = Router()
class AdminStates(StatesGroup):
    waiting_user_id = State()
    waiting_days = State()

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Найти красивый юзернейм", callback_data="btn_search")],
        [InlineKeyboardButton(text="💎 Купить Премиум", callback_data="btn_buy_premium")],
        [InlineKeyboardButton(text="📢 Наш канал", url=CHANNEL_URL)]
    ])

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await register_user(message.from_user.id, message.from_user.username or "")
    text = f"👋 Привет! Я ищу красивые свободные юзернеймы.\n\n🎁 Бесплатно: {FREE_LIMIT} в день\n💎 Премиум: безлимит за {PREMIUM_PRICE} ⭐"
    await message.answer(text, reply_markup=main_menu())

@router.callback_query(F.data == "btn_search")
async def btn_search(callback: types.CallbackQuery, pool: AccountPool):
    allowed, reason, left = await check_limit(callback.from_user.id)
    if not allowed:
        return await callback.message.answer("❌ Лимит исчерпан. Купи Премиум!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Купить Премиум", callback_data="btn_buy_premium")]]))
    
    await callback.answer()
    msg = await callback.message.answer("🔎 Ищу красивые и свободные юзернеймы...")
    
    candidates = generate_candidates(50)
    found = None
    for username in candidates:
        is_nice, why = score_username(username)
        if not is_nice: continue
        res = await pool.check(username)
        if res["status"] == "available":
            found = (username, why)
            break
        elif res["status"] == "flood":
            return await msg.edit_text("⏳ Сервера Telegram перегружены. Попробуй позже (попытка не списана).")
            
    if found:
        if reason == "free": await increment_usage(callback.from_user.id)
        await msg.edit_text(f"🟢 <b>Найден свободный юзернейм!</b>\n\n👉 <code>@{found[0]}</code>\n\n✨ {found[1]}", parse_mode="HTML")
    else:
        await msg.edit_text("😔 Не удалось найти свободный вариант. Попробуй еще раз!")

@router.callback_query(F.data == "btn_buy_premium")
async def btn_buy_premium(callback: types.CallbackQuery):
    await callback.message.answer_invoice(
        title="Премиум на 1 месяц", description="Безлимитная проверка", payload="premium_1_month",
        provider_token="", currency="XTR", prices=[LabeledPrice(label="Премиум", amount=PREMIUM_PRICE)], start_parameter="premium"
    )
    await callback.answer()

@router.pre_checkout_query()
async def process_pre_checkout(q: types.PreCheckoutQuery):
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    if message.successful_payment.invoice_payload == "premium_1_month":
        await give_premium(message.from_user.id, PREMIUM_DAYS)
        await message.answer(f"🎉 Оплата прошла! Вам выдан Премиум на {PREMIUM_DAYS} дней.")

@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Вы не создатель этого бота.")
    await message.answer("🛠 Админ-панель", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👑 Выдать Премиум", callback_data="admin_give_premium")]]))

@router.callback_query(F.data == "admin_give_premium")
async def admin_give_premium(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.edit_text("Введите ID пользователя:")
    await state.set_state(AdminStates.waiting_user_id)

@router.message(AdminStates.waiting_user_id)
async def process_admin_id(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Нужны цифры!")
    await state.update_data(target_id=int(message.text))
    await message.answer("Введите количество дней:")
    await state.set_state(AdminStates.waiting_days)

@router.message(AdminStates.waiting_days)
async def process_admin_days(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Нужно число!")
    data = await state.get_data()
    success = await give_premium(data["target_id"], int(message.text))
    await message.answer("✅ Успешно!" if success else "❌ Юзер не найден.")
    await state.clear()

# ==========================================
# 🚀 ЗАПУСК ВСЕГО
# ==========================================
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    
    pool = AccountPool()
    await pool.start()
    dp["pool"] = pool
    
    print("🚀 БОТ УСПЕШНО ЗАПУЩЕН! Ищи его в Телеграме.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
