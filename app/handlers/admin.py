import sys
import time
import asyncio
from aiogram import Router, F, Bot, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from app.services import database
from config import ADMIN_ID

router = Router()
start_time = time.time()

ADMIN_IDS = [ADMIN_ID]

class AdminState(StatesGroup):
    waiting_for_broadcast_text = State()
    waiting_for_broadcast_target = State() # Выбор аудитории (Всем/Студентам/Преподавателям)
    waiting_for_broadcast_confirm = State() # Выбор звука

@router.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def admin_main(message: types.Message):
    db = await database.get_db()
    async with db.execute("SELECT COUNT(*) FROM users") as cursor:
        count = await cursor.fetchone()
    
    await message.answer(
        f"👨‍💻 **Панель администратора**\n\n"
        f"👥 Всего пользователей в базе: {count[0]}\n\n"
        f"Доступные команды:\n"
        f"/broadcast — запустить рассылку\n"
        f"/reply <id> <текст> — написать пользователю\n"
        f"/status — статус системы\n"
        f"/reboot — перезагрузить бота\n"
        f"/cancel — отмена любого действия"
    )

@router.message(Command("reboot"), F.from_user.id.in_(ADMIN_IDS))
async def admin_reboot(message: types.Message):
    await message.answer("🔄 **Перезагрузка бота...**\nДождитесь лога запуска.")
    await asyncio.sleep(1)
    sys.exit(0) 

@router.callback_query(F.data == "restart_bot")
async def cb_restart(callback: CallbackQuery):
    if callback.from_user.id in ADMIN_IDS:
        await callback.answer("Запускаю процесс перезагрузки...")
        await callback.message.answer("♻️ Бот перезагружается. Подождите около 5 секунд.")
        await asyncio.sleep(1)
        sys.exit(0)
    else:
        await callback.answer("У вас нет прав админа!", show_alert=True)

@router.message(Command("reply"), F.from_user.id.in_(ADMIN_IDS))
async def admin_reply(message: types.Message, bot: Bot):
    args = message.text.split(maxsplit=2)
    
    if len(args) < 3:
        return await message.answer(
            "⚠️ Неверный формат команды.\n"
            "Использование: `/reply <ID пользователя> <текст сообщения>`\n"
            "Пример: `/reply 1234567890 Привет!`"
        )
    
    user_id = args[1]
    reply_text = args[2]
    
    if not user_id.isdigit():
        return await message.answer("⚠️ ID пользователя должен состоять только из цифр.")
        
    try:
        await bot.send_message(chat_id=int(user_id), text=reply_text)
        await message.answer(f"✅ Сообщение успешно отправлено пользователю `{user_id}`.")
    except Exception as e:
        await message.answer(f"❌ Ошибка при отправке сообщения:\n{str(e)}")

@router.message(Command("broadcast"), F.from_user.id.in_(ADMIN_IDS))
async def broadcast_start(message: types.Message, state: FSMContext):
    await message.answer("Введите текст для рассылки или /cancel для отмены:")
    await state.set_state(AdminState.waiting_for_broadcast_text)

@router.message(AdminState.waiting_for_broadcast_text, F.from_user.id.in_(ADMIN_IDS))
async def broadcast_ask_target(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("❌ Рассылка отменена.")

    if not message.text:
        return await message.answer("Пожалуйста, отправьте текстовое сообщение.")

    await state.update_data(broadcast_text=message.text)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Всем", callback_data="bc_target_all")],
        [InlineKeyboardButton(text="🎓 Только студентам", callback_data="bc_target_student")],
        [InlineKeyboardButton(text="👨‍🏫 Только преподавателям", callback_data="bc_target_teacher")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="bc_cancel")]
    ])

    await message.answer("📝 Текст принят. **Кому отправить рассылку?**", reply_markup=kb)
    await state.set_state(AdminState.waiting_for_broadcast_target)

@router.callback_query(AdminState.waiting_for_broadcast_target, F.data.startswith("bc_target_") | (F.data == "bc_cancel"))
async def broadcast_ask_sound(callback: CallbackQuery, state: FSMContext):
    if callback.data == "bc_cancel":
        await state.clear()
        return await callback.message.edit_text("❌ Рассылка отменена.")

    target = callback.data.replace("bc_target_", "")
    await state.update_data(broadcast_target=target)

    target_labels = {
        "all": "👥 Всем",
        "student": "🎓 Студентам",
        "teacher": "👨‍🏫 Преподавателям"
    }

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔊 Со звуком", callback_data="bc_sound")],
        [InlineKeyboardButton(text="🔕 Без звука (Ночью)", callback_data="bc_silent")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="bc_cancel")]
    ])

    await callback.message.edit_text(
        f"Выбрана аудитория: **{target_labels.get(target)}**.\nКак отправить сообщение?", 
        reply_markup=kb
    )
    await state.set_state(AdminState.waiting_for_broadcast_confirm)

@router.callback_query(AdminState.waiting_for_broadcast_confirm, F.data.in_(["bc_sound", "bc_silent", "bc_cancel"]))
async def broadcast_execute(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if callback.data == "bc_cancel":
        await state.clear()
        return await callback.message.edit_text("❌ Рассылка отменена.")

    data = await state.get_data()
    text = data.get("broadcast_text")
    target = data.get("broadcast_target", "all")
    is_silent = (callback.data == "bc_silent")
    
    await callback.message.edit_text("🚀 Рассылка запущена... Подсчет пользователей...")
    
    db = await database.get_db()

    try:
        if target == "all":
            async with db.execute("SELECT user_id FROM users") as cursor:
                users = await cursor.fetchall()
        else:
            async with db.execute("SELECT user_id FROM users WHERE role = ?", (target,)) as cursor:
                users = await cursor.fetchall()
    except Exception as e:
        await state.clear()
        return await callback.message.answer(
            f"⚠️ **Ошибка базы данных!** Возможно, в вашей таблице `users` нет колонки `role`.\n"
            f"Текст ошибки: `{e}`\nРассылка отменена."
        )

    count_success = 0
    count_error = 0

    for user in users:
        try:
            await bot.send_message(
                chat_id=int(user[0]), 
                text=text,
                disable_notification=is_silent
            )
            count_success += 1
            await asyncio.sleep(0.05)
        except Exception:
            count_error += 1

    await state.clear()
    
    type_str = "🔕 Без звука" if is_silent else "🔊 Со звуком"
    target_labels = {"all": "👥 Всем", "student": "🎓 Студентам", "teacher": "👨‍🏫 Преподавателям"}
    
    await callback.message.answer(
        f"✅ **Рассылка завершена!**\n\n"
        f"🎯 Аудитория: {target_labels.get(target)}\n"
        f"🔊 Тип: {type_str}\n"
        f"📈 Успешно: {count_success}\n"
        f"📉 Ошибок: {count_error}"
    )

@router.message(Command("status"), F.from_user.id.in_(ADMIN_IDS))
async def admin_status(message: types.Message):
    uptime = time.time() - start_time
    up_str = time.strftime("%H:%M:%S", time.gmtime(uptime))
    
    user_count = "Неизвестно"
    try:
        db = await database.get_db()
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            count = await cursor.fetchone()
            user_count = count[0]
        db_status = "✅ Подключена"
    except Exception:
        db_status = "❌ Ошибка БД"

    await message.answer(
        f"📊 **Статус системы**\n\n"
        f"👥 Пользователей: `{user_count}`\n"
        f"⏳ Аптайм: `{up_str}`\n"
        f"🗄 База данных: {db_status}\n"
        f"🐍 Python: `{sys.version.split()[0]}`\n"
        f"🤖 Версия aiogram: `3.x`"
    )