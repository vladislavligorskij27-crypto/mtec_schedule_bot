import datetime
import hashlib
import asyncio
import logging
import os
import re

from aiogram import Router, F, types, Bot
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, KeyboardButton, ReplyKeyboardMarkup, FSInputFile, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards import builders as kb
from app.services import database
from app.services import scraper
from config import GROUPS_FILE, MENTORS_FILE, CALLS_1, CALLS_2, ARCHIVE_DIR, LOGO_PATH, MINSK_TZ

router = Router()

MONTHS_RU = {
    "01": "Январь", "02": "Февраль", "03": "Март", "04": "Апрель", 
    "05": "Май", "06": "Июнь", "07": "Июль", "08": "Август", 
    "09": "Сентябрь", "10": "Октябрь", "11": "Ноябрь", "12": "Декабрь"
}

class CuratorStates(StatesGroup):
    waiting_for_group_msg = State()

def load_list(filename):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []

def _sync_save_archive(img_bytes, path):
    with open(path, "wb") as f:
        f.write(img_bytes)

async def save_schedule_to_archive(img_bytes, target, date_str):
    try:
        day, month, year = date_str.split(".")
        path = os.path.join(ARCHIVE_DIR, year, month)
        os.makedirs(path, exist_ok=True)
        
        target_safe = str(target).replace(" ", "_").replace(".", "")
        file_name = f"{date_str}_{target_safe}.png"
        file_path = os.path.join(path, file_name)
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _sync_save_archive, img_bytes, file_path)
        return True
    except Exception as e:
        logging.error(f"❌ Ошибка сохранения в архив: {e}")
        return False

@router.callback_query(F.data == "role_student")
async def select_student(callback: CallbackQuery):
    await callback.answer()
    groups = load_list(GROUPS_FILE)
    await callback.message.edit_text("Выбери префикс группы:", reply_markup=kb.group_prefixes_keyboard(groups))

@router.callback_query(F.data == "role_teacher")
async def select_teacher(callback: CallbackQuery):
    await callback.answer()
    mentors = load_list(MENTORS_FILE)
    await callback.message.edit_text("Первая буква фамилии:", reply_markup=kb.alphabet_keyboard(mentors))

@router.callback_query(F.data.startswith("pref_"))
async def select_group_by_prefix(callback: CallbackQuery):
    await callback.answer()
    prefix = callback.data.replace("pref_", "")
    groups = load_list(GROUPS_FILE)
    await callback.message.edit_text(f"Группы на {prefix}:", reply_markup=kb.groups_by_prefix_keyboard(groups, prefix))

@router.callback_query(F.data.startswith("let_"))
async def select_mentor_by_letter(callback: CallbackQuery):
    await callback.answer()
    letter = callback.data.replace("let_", "")
    mentors = load_list(MENTORS_FILE)
    await callback.message.edit_text(f"Преподаватели на {letter}:", reply_markup=kb.mentors_by_letter_keyboard(mentors, letter))

@router.callback_query(F.data == "back_to_prefixes")
async def back_to_prefixes(callback: CallbackQuery):
    await callback.answer()
    groups = load_list(GROUPS_FILE)
    await callback.message.edit_text("Выбери префикс группы:", reply_markup=kb.group_prefixes_keyboard(groups))

@router.callback_query(F.data.startswith(("set_std_", "set_tch_")))
async def save_role_logic(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    if callback.data.startswith("set_std_"):
        role = "student"
        target = callback.data.replace("set_std_", "")
        await database.save_user(user_id, role, target)
        await callback.message.answer(f"✅ Готово! Группа: {target}", reply_markup=kb.main_menu(role))
        await callback.message.delete()
    else:
        role = "teacher"
        mentors = load_list(MENTORS_FILE)
        idx = int(callback.data.replace("set_tch_", ""))
        target = mentors[idx]
        await database.save_user(user_id, role, target)
        await callback.message.edit_text(f"Выбрано: {target}\nВы являетесь куратором?", reply_markup=kb.curator_choice_keyboard())

@router.callback_query(F.data == "is_curator_yes")
async def curator_yes(callback: CallbackQuery):
    await callback.answer()
    groups = load_list(GROUPS_FILE)
    await callback.message.edit_text("Выберите группу:", reply_markup=kb.group_prefixes_keyboard(groups, mode="cur"))

@router.callback_query(F.data.startswith("curpref_"))
async def select_curator_group_prefix(callback: CallbackQuery):
    await callback.answer()
    prefix = callback.data.replace("curpref_", "")
    groups = load_list(GROUPS_FILE)
    await callback.message.edit_text(f"Группы на {prefix}:", reply_markup=kb.groups_by_prefix_keyboard(groups, prefix, mode="cur"))

@router.callback_query(F.data.startswith("set_cur_"))
async def set_curator_group_final(callback: CallbackQuery):
    await callback.answer()
    group_name = callback.data.replace("set_cur_", "")
    await database.set_curator(callback.from_user.id, group_name)
    await callback.message.answer(f"✅ Профиль куратора {group_name} настроен!", reply_markup=kb.main_menu("teacher", is_curator=True))
    await callback.message.delete()

@router.callback_query(F.data == "is_curator_no")
async def curator_no(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("✅ Регистрация завершена!", reply_markup=kb.main_menu("teacher", is_curator=False))
    await callback.message.delete()

@router.message(F.text == "📂 Архив расписания")
async def cmd_archive_start(message: types.Message):
    user = await database.get_user(message.from_user.id)
    if not user: return
    target_safe = user['target'].replace(' ', '_')
    available_months = set()
    
    if os.path.exists(ARCHIVE_DIR):
        for year in os.listdir(ARCHIVE_DIR):
            y_p = os.path.join(ARCHIVE_DIR, year)
            if not os.path.isdir(y_p): continue
            for month in os.listdir(y_p):
                m_p = os.path.join(y_p, month)
                if os.path.isdir(m_p) and any(target_safe in f for f in os.listdir(m_p)):
                    available_months.add((year, month))
    
    if not available_months:
        return await message.answer("📭 Ваш архив пока пуст.")
    
    builder = InlineKeyboardBuilder()
    for y, m in sorted(list(available_months), reverse=True):
        builder.button(text=f"📁 {MONTHS_RU.get(m, m)} {y}", callback_data=f"arch_m_{y}_{m}")
    await message.answer("📂 Выберите месяц:", reply_markup=builder.adjust(1).as_markup())

@router.callback_query(F.data.startswith("arch_m_"))
async def arch_show_month(callback: CallbackQuery):
    await callback.answer()
    _, _, year, month = callback.data.split("_")
    user = await database.get_user(callback.from_user.id)
    if not user: return
    
    target_safe = user['target'].replace(' ', '_')
    path = os.path.join(ARCHIVE_DIR, year, month)
    
    if not os.path.exists(path):
        return await callback.message.edit_text("Ошибка: папка не найдена")

    files = sorted([f for f in os.listdir(path) if target_safe in f])
    builder = InlineKeyboardBuilder()
    
    for f in files:
        date_part = f.split("_")[0] 
        builder.button(text=f"📄 {date_part.split('.')[0]} число", callback_data=f"af_{year}_{month}_{date_part}")
    
    builder.button(text="⬅️ Назад", callback_data="back_to_arch_list")
    
    role_label = "Преподаватель" if user['role'] == "teacher" else "Группа"
    await callback.message.edit_text(
        f"📅 Архив ({role_label}: {user['target']}) за {MONTHS_RU.get(month)}:", 
        reply_markup=builder.adjust(3).as_markup()
    )

@router.callback_query(F.data == "back_to_arch_list")
async def back_to_arch_list(callback: CallbackQuery):
    await callback.answer()
    await cmd_archive_start(callback.message)
    await callback.message.delete()

@router.callback_query(F.data.startswith("af_"))
async def arch_send_file(callback: CallbackQuery):
    await callback.answer()
    _, year, month, date_str = callback.data.split("_")
    user = await database.get_user(callback.from_user.id)
    if not user: return
    
    target_safe = user['target'].replace(' ', '_')
    filename = f"{date_str}_{target_safe}.png"
    file_path = os.path.join(ARCHIVE_DIR, year, month, filename)
    
    if os.path.exists(file_path):
        label = "👨‍🏫 Преподаватель" if user['role'] == "teacher" else "👥 Группа"
        await callback.message.answer_photo(
            FSInputFile(file_path), 
            caption=f"🗓 **Архивное расписание**\n{label}: {user['target']}\n📅 Дата: {date_str}",
            parse_mode="Markdown"
        )
    else:
        await callback.answer("⚠️ Файл не найден", show_alert=True)

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    user = await database.get_user(message.from_user.id)
    name = message.from_user.first_name
    
    if user:
        return await message.answer(
            f"👋 **С возвращением, {name}!**\n\nРад тебя видеть.",
            reply_markup=kb.main_menu(user['role'], bool(user['curator_group'])),
            parse_mode="Markdown"
        )
    
    welcome_text = (
        f"👋 **Привет, {name}!**\n\n"
        f"Я — твой помощник по расписанию **МТЭК**.\n\n"
        f"✨ **Что я умею:**\n"
        f"├ 📅 **Расписание** на любой день\n"
        f"├ 🏛 **Поиск** свободных кабинетов\n"
        f"├ 🔔 **Уведомления** об изменениях\n"
        f"└ 📋 **Звонки** всегда под рукой\n\n"
        f"🚀 *Давай настроим твой профиль:* "
    )
    await message.answer(text=welcome_text, reply_markup=kb.start_keyboard(), parse_mode="Markdown")

@router.message(F.text.in_(["📅 На сегодня", "⏭️ На завтра", "➡️ На понедельник"]))
async def send_sched(message: types.Message):
    user = await database.get_user(message.from_user.id)
    if not user: return
    
    now = datetime.datetime.now(MINSK_TZ)
    offset = 0
    if "завтра" in message.text: offset = 1
    elif "понедельник" in message.text: offset = (7 - now.weekday())
    
    date_str = (now + datetime.timedelta(days=offset)).strftime("%d.%m.%Y")
    
    data = await scraper.get_schedule(user['target'], user['role'], date_str)
    
    if data and data != "error_logic":
        data_hash = scraper.get_data_hash(data)
        cached_file_id = scraper.IMAGE_CACHE.get(data_hash)
        
        label = "👨‍🏫 Преподаватель" if user['role'] == "teacher" else "👥 Группа"
        caption = f"📅 Расписание на {date_str}\n{label}: {user['target']}"
        
        if cached_file_id:
            await message.answer_photo(photo=cached_file_id, caption=caption, parse_mode="Markdown")
        else:
            wait = await message.answer("⏳ Отрисовка расписания...")
            loop = asyncio.get_running_loop()
            photo = await loop.run_in_executor(None, scraper.create_schedule_png, data, user['target'], date_str)
            img_bytes = photo.read()
            
            await save_schedule_to_archive(img_bytes, user['target'], date_str)
            
            sent_msg = await message.answer_photo(
                BufferedInputFile(img_bytes, filename="sched.png"), 
                caption=caption,
                parse_mode="Markdown"
            )
            scraper.IMAGE_CACHE[data_hash] = sent_msg.photo[-1].file_id
            await wait.delete()
    else:
        await message.answer(f"❌ Данных на {date_str} нет.")

@router.message(F.text == "🔔 Звонки")
async def calls(message: types.Message):
    album = MediaGroupBuilder(caption="🕘 Расписание звонков")
    album.add_photo(media=FSInputFile(CALLS_1))
    album.add_photo(media=FSInputFile(CALLS_2))
    await message.answer_media_group(media=album.build())

@router.message(F.text == "👨‍🏫 Меню куратора")
async def open_curator_menu(message: types.Message):
    user = await database.get_user(message.from_user.id)
    if user and user['curator_group']:
        await message.answer(
            f"🛠 Меню куратора группы {user['curator_group']}", 
            reply_markup=kb.group_management_kb(user['curator_group'])
        )
    else:
        await message.answer("❌ У вас не настроена группа для кураторства.")

@router.message(F.text.startswith("📢 Сообщение группе"))
async def curator_msg_start(message: types.Message, state: FSMContext):
    await message.answer("Введите текст сообщения для группы. Его получат все студенты этой группы.", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True))
    await state.set_state(CuratorStates.waiting_for_group_msg)

@router.message(CuratorStates.waiting_for_group_msg)
async def curator_msg_send(message: types.Message, state: FSMContext, bot: Bot):
    if message.text == "❌ Отмена":
        user = await database.get_user(message.from_user.id)
        await state.clear()
        return await message.answer("Отменено.", reply_markup=kb.group_management_kb(user['curator_group']))

    user = await database.get_user(message.from_user.id)
    group = user['curator_group']
    
    db = await database.get_db()
    async with db.execute("SELECT user_id FROM users WHERE target = ?", (group,)) as cursor:
        students = await cursor.fetchall()

    count = 0
    for s in students:
        try:
            await bot.send_message(s[0], f"👨‍🏫 Сообщение от куратора:\n\n{message.text}")
            count += 1
        except: pass
    
    await message.answer(f"Сообщение отправлено {count} студентам.", reply_markup=kb.group_management_kb(group))
    await state.clear()

@router.message(F.text == "⬅️ Назад в меню")
async def back_to_main(message: types.Message):
    user = await database.get_user(message.from_user.id)
    if not user: return await message.answer("Ошибка. Введите /start")
    await message.answer("Главное меню:", reply_markup=kb.main_menu(user['role'], bool(user['curator_group'])))

@router.message(F.text == "⚙️ Настройки")
async def show_settings(message: types.Message):
    user = await database.get_user(message.from_user.id)
    if not user: return
    await message.answer("Ваши настройки:", reply_markup=kb.settings_keyboard(user['notifications']))

@router.callback_query(F.data == "toggle_notif")
async def toggle_notif(callback: CallbackQuery):
    await callback.answer()
    new_state = await database.toggle_notifications(callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=kb.settings_keyboard(new_state))

@router.callback_query(F.data == "reset_setup")
async def reset_setup(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("Профиль сброшен. Выберите роль:", reply_markup=kb.start_keyboard())

async def send_morning_schedule(bot: Bot):
    now = datetime.datetime.now(MINSK_TZ)
    date_str = now.strftime("%d.%m.%Y")
    
    await scraper.build_global_cache(date_str)
    
    db = await database.get_db()
    async with db.execute("SELECT user_id, target, role FROM users WHERE notifications = 1") as cursor:
        users = await cursor.fetchall()
        
    target_to_users = {}
    for u in users:
        key = (u['target'], u['role'])
        if key not in target_to_users:
            target_to_users[key] = []
        target_to_users[key].append(u['user_id'])

    for (target, role), uids in target_to_users.items():
        data = await scraper.get_schedule(target, role, date_str)
        if not data or data == "error_logic": continue
        
        data_hash = scraper.get_data_hash(data)
        file_id = scraper.IMAGE_CACHE.get(data_hash)
        caption = f"☀️ **Доброе утро!**\nВаше расписание на сегодня ({date_str})"

        if not file_id:
            # Отрисовываем 1 раз на всю группу
            loop = asyncio.get_running_loop()
            photo = await loop.run_in_executor(None, scraper.create_schedule_png, data, target, date_str)
            img_bytes = photo.read()
            
            try:
                msg = await bot.send_photo(
                    uids[0], 
                    BufferedInputFile(img_bytes, filename=f"morning_{date_str}.png"), 
                    caption=caption, 
                    parse_mode="Markdown"
                )
                file_id = msg.photo[-1].file_id
                scraper.IMAGE_CACHE[data_hash] = file_id
                await asyncio.sleep(0.05)
            except Exception:
                pass
            
            if file_id:
                for uid in uids[1:]:
                    try:
                        await bot.send_photo(uid, file_id, caption=caption, parse_mode="Markdown")
                        await asyncio.sleep(0.05)
                    except: pass
        else:
            for uid in uids:
                try:
                    await bot.send_photo(uid, file_id, caption=caption, parse_mode="Markdown")
                    await asyncio.sleep(0.05)
                except: pass

async def scheduled_check_updates(bot: Bot):
    now = datetime.datetime.now(MINSK_TZ)
    date_to_check = now.strftime("%d.%m.%Y")
    if now.hour >= 16:
        date_to_check = (now + datetime.timedelta(days=1)).strftime("%d.%m.%Y")

    await scraper.build_global_cache(date_to_check)

    yesterday = (now - datetime.timedelta(days=1)).strftime("%d.%m.%Y")
    if yesterday in scraper.GLOBAL_SCHEDULE_CACHE:
        del scraper.GLOBAL_SCHEDULE_CACHE[yesterday]
    if yesterday in scraper.rooms_data_cache:
        del scraper.rooms_data_cache[yesterday]
    if now.hour == 3 and now.minute < 10:
        scraper.IMAGE_CACHE.clear()

    db = await database.get_db()
    async with db.execute("SELECT DISTINCT target, role FROM users WHERE notifications = 1") as cursor:
        unique_targets = await cursor.fetchall()

    new_hashes = {}
    for t in unique_targets:
        target, role = t['target'], t['role']
        base_hash = await scraper.get_user_specific_hash(target, role, date_to_check)
        if base_hash:
            new_hashes[target] = f"{date_to_check}_{base_hash}"

    async with db.execute("SELECT user_id, target, last_hash, pending_hash FROM users WHERE notifications = 1") as cursor:
        users = await cursor.fetchall()

    for user in users:
        u_id, target, last_h, pend_h = user['user_id'], user['target'], user['last_hash'], user['pending_hash']
        curr_h = new_hashes.get(target)
        if not curr_h: continue

        old_date = last_h.split("_")[0] if last_h and "_" in last_h else None
        if old_date != date_to_check:
            await database.update_last_hash(u_id, curr_h)
            continue

        if curr_h == last_h:
            if pend_h is not None:
                await database.update_pending_hash(u_id, None)
            continue 

        if curr_h == pend_h:
            try:
                await bot.send_message(u_id, f"🔔 Расписание для **{target}** на **{date_to_check}** обновилось!")
                await database.update_last_hash(u_id, curr_h)
                await asyncio.sleep(0.05)
            except: pass
        else:
            await database.update_pending_hash(u_id, curr_h)

@router.message(lambda m: m.text and any(x in m.text for x in ["Сегодня", "Завтра", "Пн"]))
async def curator_schedule_view_final(message: types.Message):
    user = await database.get_user(message.from_user.id)
    if not user or not user['curator_group']: return
    group = user['curator_group']
    if not re.search(rf"{re.escape(group)}", message.text): return
    
    now = datetime.datetime.now(MINSK_TZ)
    offset = 0
    if "Завтра" in message.text: offset = 1
    elif "Пн" in message.text: offset = (7 - now.weekday())
    
    date_str = (now + datetime.timedelta(days=offset)).strftime("%d.%m.%Y")
    
    data = await scraper.get_schedule(group, "student", date_str)
    if data and data != "error_logic":
        data_hash = scraper.get_data_hash(data)
        cached_file_id = scraper.IMAGE_CACHE.get(data_hash)
        caption = f"👨‍🏫 Группа {group}\n📅 {date_str}"
        
        if cached_file_id:
            await message.answer_photo(photo=cached_file_id, caption=caption)
        else:
            wait = await message.answer(f"⏳ Загрузка расписания {group}...")
            loop = asyncio.get_running_loop()
            photo = await loop.run_in_executor(None, scraper.create_schedule_png, data, group, date_str)
            img_bytes = photo.read()
            await save_schedule_to_archive(img_bytes, group, date_str)
            
            sent_msg = await message.answer_photo(BufferedInputFile(img_bytes, filename="sched.png"), caption=caption)
            scraper.IMAGE_CACHE[data_hash] = sent_msg.photo[-1].file_id
            await wait.delete()
    else:
        await message.answer(f"❌ На {date_str} данных нет.")

@router.message(F.text == "ℹ️ Инфо")
async def info_command(message: types.Message):
    info_text = (
        "🏛 **MTEC Schedule Bot**\n"
        "*Официальный цифровой сервис для участников образовательного процесса*\n\n"
        "Информационная система разработана для обеспечения оперативного доступа к учебному расписанию учреждения образования «МТЭК».\n\n"
        "🚀 **Ключевые преимущества:**\n"
        "• **In-Memory Cache:** Выдача результатов за 0.01 сек.\n"
        "• **Синхронизация:** Прямая интеграция с официальным порталом.\n"
        "• **Скорость:** Оптимизированная генерация и кэширование карт.\n"
        "• **Интеллект:** Поиск свободных аудиторий в режиме реального времени.\n"
        "• **Архив:** Доступ к ранее опубликованным данным расписания.\n\n"
        "👨‍💻 **Разработчик:** Лигорский Владислав Николаевич\n\n"
        "🛡 *Версия системы: 3.0.0 (High-Performance)*\n"
        "📎 *Техническая поддержка осуществляется через раздел «Обратная связь».*"
    )
    await message.answer(info_text, parse_mode="Markdown")

@router.message(F.text == "🔍 Где препод?")
async def select_mentor_for_search(message: types.Message):
    mentors = load_list(MENTORS_FILE)
    await message.answer("Первая буква фамилии:", reply_markup=kb.alphabet_keyboard(mentors, mode="search"))

@router.callback_query(F.data == "search_mentor_back")
async def search_mentor_back(callback: types.CallbackQuery):
    await callback.answer()
    mentors = load_list(MENTORS_FILE)
    await callback.message.edit_text("Первая буква фамилии:", reply_markup=kb.alphabet_keyboard(mentors, mode="search"))

@router.callback_query(F.data.startswith("srch_let_"))
async def search_mentor_by_letter(callback: types.CallbackQuery):
    await callback.answer()
    letter = callback.data.replace("srch_let_", "")
    mentors = load_list(MENTORS_FILE)
    await callback.message.edit_text(f"Преподаватели на {letter}:", reply_markup=kb.mentors_by_letter_keyboard(mentors, letter, mode="search"))

@router.callback_query(F.data.startswith("srch_ment_"))
async def process_mentor_search_final(callback: types.CallbackQuery):
    await callback.answer()
    idx = int(callback.data.replace("srch_ment_", ""))
    mentors = load_list(MENTORS_FILE)
    name = mentors[idx]
    
    date_str = datetime.datetime.now(MINSK_TZ).strftime("%d.%m.%Y")
    
    data = await scraper.get_schedule(name, "teacher", date_str)
    
    if data and data != "error_logic":
        res = [f"✅ **{name}** найден(а):\n"]
        for item in data:
            res.append(f"📍 Пара №{item['para']} | Каб. **{item['room']}**\n└ {item['info'].replace('\n', ' | ')}")
        await callback.message.edit_text("\n\n".join(res), parse_mode="Markdown")
    else:
        await callback.message.edit_text(f"❌ На сегодня данных для **{name}** нет.", parse_mode="Markdown")