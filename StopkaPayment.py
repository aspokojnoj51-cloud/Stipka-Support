import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

# Логирование
logging.basicConfig(level=logging.INFO)

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPER_ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PORT = int(os.getenv("PORT", 8080))
CARD = os.getenv("CARD", "")

if not BOT_TOKEN or not SUPER_ADMIN_ID:
    raise ValueError("Пожалуйста, укажите BOT_TOKEN и ADMIN_ID в переменных окружения!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Множество активных администраторов
ADMINS = {SUPER_ADMIN_ID}

# База данных тикетов (обращений) в памяти
tickets = {}
ticket_counter = 1

# Режим "чата" у админа: admin_id -> ticket_id, с которым он сейчас переписывается
admin_chat_mode = {}

# Состояния FSM
class AdminStates(StatesGroup):
    waiting_for_reply = State()
    waiting_for_new_admin_id = State()
    waiting_for_remove_admin_id = State()

# --- ВЕБ-СЕРВЕР ДЛЯ UPTIMEROBOT ---
async def handle_ping(request):
    return web.Response(text="Stopka Payments Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {PORT}")

# --- УСТАНОВКА МЕНЮ КОМАНД ---
async def set_main_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Начало"),
        BotCommand(command="tickets", description="Мои обращения"),
    ]
    await bot.set_my_commands(commands)

# --- ПРОВЕРКА НА АДМИНА ---
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

# --- ВСПОМОГАТЕЛЬНОЕ: краткое текстовое содержимое сообщения тикета ---
def item_preview_text(item: dict) -> str:
    if item.get("text"):
        return item["text"]
    if item.get("type") == "photo":
        return "📷 Фото"
    if item.get("type") == "document":
        return "📎 Файл"
    return ""

# --- ВСПОМОГАТЕЛЬНОЕ: статус тикета для АДМИНА (3 цвета) ---
def _last_awaiting(ticket: dict) -> str:
    """Кого ждём дальше: 'client' или 'admin'. Берём явную метку на последнем
    сообщении, если она есть — так это не зависит от точной формулировки
    текста и не ломается, если текст сообщения потом поменяется."""
    if not ticket["messages"]:
        return "admin"
    last_msg = ticket["messages"][-1]
    if "awaiting" in last_msg:
        return last_msg["awaiting"]
    return "client" if last_msg["sender"].startswith("Поддержка") else "admin"

def get_admin_status_symbol(ticket: dict) -> str:
    if ticket["status"] == "closed":
        return "🔴"
    return "🟡" if _last_awaiting(ticket) == "client" else "🟢"

def get_admin_status_label(ticket: dict) -> str:
    if ticket["status"] == "closed":
        return "🔴 Закрыт"
    if _last_awaiting(ticket) == "client":
        return "🟡 Открыт (ответ отправлен, ждём клиента)"
    return "🟢 Открыт (клиент ответил, ждёт вас)"

# --- ВСПОМОГАТЕЛЬНОЕ: отправка длинного текста частями ---
async def send_chunked_text(target_message: types.Message, text: str, parse_mode=None):
    limit = 3500
    if len(text) <= limit:
        await target_message.answer(text, parse_mode=parse_mode)
        return
    for i in range(0, len(text), limit):
        await target_message.answer(text[i:i + limit], parse_mode=parse_mode)

# --- КЛАВИАТУРЫ ПОЛЬЗОВАТЕЛЯ ---
def get_user_main_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗂 Мои обращения", callback_data="user_my_tickets")]
        ]
    )

def get_back_to_main_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="user_main_menu")]
        ]
    )

def get_payment_question_kb(ticket_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да", callback_data=f"pay_q_yes_{ticket_id}")],
            [InlineKeyboardButton(text="❌ Нет", callback_data=f"pay_q_no_{ticket_id}")]
        ]
    )

def get_user_tickets_kb(user_id: int):
    user_tickets = [t_id for t_id, data in tickets.items() if data["user_id"] == user_id]
    buttons = []

    for t_id in user_tickets:
        status_symbol = "🟢" if tickets[t_id]["status"] == "open" else "🔴"
        buttons.append([InlineKeyboardButton(text=f"{status_symbol} Обращение #{t_id}", callback_data=f"user_view_ticket_{t_id}")])

    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="user_main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- КЛАВИАТУРЫ АДМИНА ---
def get_admin_main_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📩 Активные тикеты", callback_data="admin_tickets_open")],
            [InlineKeyboardButton(text="📁 Архив (Закрытые)", callback_data="admin_tickets_closed")],
            [InlineKeyboardButton(text="👥 Управление админами", callback_data="admin_manage_admins")]
        ]
    )

def get_admins_manage_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить админа", callback_data="admin_add_new")],
            [InlineKeyboardButton(text="➖ Удалить админа", callback_data="admin_remove_exist")],
            [InlineKeyboardButton(text="« Назад в меню", callback_data="admin_main_menu")]
        ]
    )

def get_admin_ticket_manage_kb(ticket_id: int):
    is_open = tickets.get(ticket_id, {}).get("status") == "open"
    buttons = [
        [InlineKeyboardButton(text="📜 Посмотреть историю", callback_data=f"history_ticket_{ticket_id}")]
    ]
    if is_open:
        buttons.append([
            InlineKeyboardButton(text="Ответить", callback_data=f"reply_ticket_{ticket_id}"),
            InlineKeyboardButton(text="Закрыть", callback_data=f"close_ticket_{ticket_id}")
        ])
    buttons.append([InlineKeyboardButton(text="« Назад к тикетам", callback_data="admin_tickets_open")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_chat_mode_kb(ticket_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📜 История", callback_data=f"history_ticket_{ticket_id}"),
                InlineKeyboardButton(text="🔒 Закрыть", callback_data=f"close_ticket_{ticket_id}")
            ],
            [InlineKeyboardButton(text="« Назад в админ панель", callback_data="admin_exit_chat")]
        ]
    )


# --- ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()

    if is_admin(message.from_user.id):
        await message.answer(
            "⚙️ **Добро пожаловать в панель поддержки Stopka Payments!**\n\n"
            "Используйте /admin, чтобы открыть панель администратора.\n"
            "А если хотите написать сообщение в поддержку сами (как клиент) — просто напишите его сюда в чат.",
            parse_mode="Markdown"
        )
        return

    await message.answer(
        "👋 **Добро пожаловать в поддержку Stopka Payments!**\n\n"
        "Просто напишите сюда сообщение или отправьте фото/чек — оператор ответит вам прямо в этом чате.",
        parse_mode="Markdown",
        reply_markup=get_user_main_kb()
    )

@dp.message(Command("tickets"))
async def cmd_tickets(message: types.Message):
    user_id = message.from_user.id
    user_tickets = [t_id for t_id, data in tickets.items() if data["user_id"] == user_id]

    if not user_tickets:
        await message.answer("У вас пока нет обращений. Просто напишите сообщение, и мы вам ответим.")
        return

    await message.answer(
        "🗂 **Ваши обращения:**\n\n🟢 — Открыто\n🔴 — Закрыто",
        parse_mode="Markdown",
        reply_markup=get_user_tickets_kb(user_id)
    )

@dp.callback_query(F.data == "user_main_menu")
async def user_main_menu_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "👋 **Главное меню поддержки Stopka Payments**\n\nПросто напишите сообщение в чат в любой момент.",
        parse_mode="Markdown",
        reply_markup=get_user_main_kb()
    )
    await callback.answer()

@dp.callback_query(F.data == "user_my_tickets")
async def process_my_tickets(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_tickets = [t_id for t_id, data in tickets.items() if data["user_id"] == user_id]

    if not user_tickets:
        await callback.message.edit_text(
            "У вас пока нет обращений. Просто напишите сообщение, и мы вам ответим.",
            reply_markup=get_back_to_main_kb()
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        "🗂 **Ваши обращения:**\n\n🟢 — Открыто\n🔴 — Закрыто",
        parse_mode="Markdown",
        reply_markup=get_user_tickets_kb(user_id)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("user_view_ticket_"))
async def user_view_ticket(callback: types.CallbackQuery):
    ticket_id = int(callback.data.split("_")[3])
    ticket = tickets.get(ticket_id)

    if not ticket:
        await callback.answer("Обращение не найдено.")
        return

    status_str = "🟢 Открыто" if ticket["status"] == "open" else "🔴 Закрыто"

    msg_history = f"🎫 **Обращение #{ticket_id}** (Статус: {status_str})\n\n"
    msg_history += "📜 **История переписки:**\n"

    if not ticket["messages"]:
        msg_history += "_Сообщений пока нет._\n"
    else:
        for item in ticket["messages"]:
            msg_history += f"• **{item['sender']}**: {item_preview_text(item)}\n"

    kb_buttons = [[InlineKeyboardButton(text="« К списку обращений", callback_data="user_my_tickets")]]

    await callback.message.edit_text(
        msg_history,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    )
    await callback.answer()


# --- АДМИН-ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "⚙️ **Админ панель — Stopka Payments**",
        parse_mode="Markdown",
        reply_markup=get_admin_main_kb()
    )

@dp.callback_query(F.data == "admin_main_menu")
async def admin_main_menu_cb(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    admin_chat_mode.pop(callback.from_user.id, None)
    await state.clear()
    await callback.message.edit_text(
        "⚙️ **Админ панель — Stopka Payments**",
        parse_mode="Markdown",
        reply_markup=get_admin_main_kb()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_exit_chat")
async def admin_exit_chat(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    admin_chat_mode.pop(callback.from_user.id, None)
    await state.clear()
    await callback.message.edit_text(
        "⚙️ **Админ панель — Stopka Payments**",
        parse_mode="Markdown",
        reply_markup=get_admin_main_kb()
    )
    await callback.answer()

# Управление списком администраторов
@dp.callback_query(F.data == "admin_manage_admins")
async def admin_manage_admins(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    admins_list_text = "\n".join([f"• `{a_id}`" for a_id in ADMINS])
    await callback.message.edit_text(
        f"👥 **Список текущих администраторов:**\n\n{admins_list_text}\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=get_admins_manage_kb()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_add_new")
async def admin_add_new_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await state.set_state(AdminStates.waiting_for_new_admin_id)
    await callback.message.answer("📝 Введите **Telegram ID** пользователя, которого хотите сделать админом:")
    await callback.answer()

@dp.message(AdminStates.waiting_for_new_admin_id)
async def process_add_new_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    if not message.text or not message.text.isdigit():
        await message.answer("❌ Telegram ID должен состоять только из цифр. Попробуйте еще раз:")
        return

    new_admin_id = int(message.text)
    ADMINS.add(new_admin_id)
    await state.clear()
    await message.answer(f"✅ Пользователь `{new_admin_id}` успешно добавлен в список администраторов!", parse_mode="Markdown")

@dp.callback_query(F.data == "admin_remove_exist")
async def admin_remove_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await state.set_state(AdminStates.waiting_for_remove_admin_id)
    await callback.message.answer("🗑 Введите **Telegram ID** админа, которого нужно удалить:")
    await callback.answer()

@dp.message(AdminStates.waiting_for_remove_admin_id)
async def process_remove_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    if not message.text or not message.text.isdigit():
        await message.answer("❌ Telegram ID должен состоять только из цифр:")
        return

    remove_id = int(message.text)
    if remove_id == SUPER_ADMIN_ID:
        await message.answer("❌ Нельзя удалить главного администратора!")
        await state.clear()
        return

    if remove_id in ADMINS:
        ADMINS.remove(remove_id)
        admin_chat_mode.pop(remove_id, None)
        await message.answer(f"✅ Администратор `{remove_id}` удален.", parse_mode="Markdown")
    else:
        await message.answer("❌ Пользователь с таким ID не найден в списке админов.")

    await state.clear()

# Списки тикетов (Открытые / Архив)
@dp.callback_query(F.data.in_({"admin_tickets_open", "admin_tickets_closed"}))
async def admin_list_tickets(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    status_filter = "open" if callback.data == "admin_tickets_open" else "closed"
    filtered_tickets = {t_id: data for t_id, data in tickets.items() if data["status"] == status_filter}

    if status_filter == "open":
        title = (
            "📩 **Активные тикеты:**\n\n"
            "🟢 — клиент написал, ждёт ответа админа\n"
            "🟡 — ответ отправлен, ждём клиента"
        )
    else:
        title = "📁 **Архив тикетов:**\n\n🔴 — закрыт"

    if not filtered_tickets:
        await callback.message.edit_text(
            f"{title}\n\nСписок пуст.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад в меню", callback_data="admin_main_menu")]
            ])
        )
        return

    buttons = []
    for t_id, t_data in filtered_tickets.items():
        symbol = get_admin_status_symbol(t_data)
        buttons.append([InlineKeyboardButton(
            text=f"{symbol} Тикет #{t_id} (Сообщений: {len(t_data['messages'])})",
            callback_data=f"view_ticket_{t_id}"
        )])

    buttons.append([InlineKeyboardButton(text="« Назад в меню", callback_data="admin_main_menu")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(title, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("view_ticket_"))
async def admin_view_ticket(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)

    if not ticket:
        await callback.answer("Тикет не найден.")
        return

    status_txt = get_admin_status_label(ticket)

    await callback.message.edit_text(
        f"🎫 **Тикет #{ticket_id}**\n"
        f"Статус: {status_txt}\n"
        f"Пользователь ID: `{ticket['user_id']}`\n"
        f"Сообщений в истории: {len(ticket['messages'])}\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=get_admin_ticket_manage_kb(ticket_id)
    )
    await callback.answer()

# Просмотр истории сообщений админом:
# текст (пользователь + админ) — одним общим сообщением,
# фото/файлы — отдельными сообщениями после него.
@dp.callback_query(F.data.startswith("history_ticket_"))
async def admin_view_ticket_history(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)

    if not ticket:
        await callback.answer("Тикет не найден.")
        return

    if not ticket["messages"]:
        await callback.message.answer(f"📜 **История переписки — Тикет #{ticket_id}:**\n\n_Сообщений пока нет._", parse_mode="Markdown")
        await callback.answer()
        return

    text_lines = []
    media_items = []

    for item in ticket["messages"]:
        sender_label = item['sender']
        if sender_label == "Пользователь":
            sender_label = f"Пользователь (`{ticket['user_id']}`)"
        if item.get("type") == "text":
            text_lines.append(f"👤 {sender_label}:\n{item['text']}")
        else:
            label = "📷 Фото" if item.get("type") == "photo" else "📎 Файл"
            suffix = f" — {item['text']}" if item.get("text") else ""
            text_lines.append(f"👤 {sender_label}: [{label}]{suffix}")
            media_items.append(item)

    combined_text = f"📜 **История переписки — Тикет #{ticket_id}:**\n\n" + "\n\n".join(text_lines)
    await send_chunked_text(callback.message, combined_text, parse_mode="Markdown")

    if media_items:
        await callback.message.answer("📎 **Вложения из этого тикета:**", parse_mode="Markdown")
        for item in media_items:
            caption = item["sender"]
            if item.get("text"):
                caption += f":\n{item['text']}"
            try:
                if item.get("type") == "photo" and item.get("file_id"):
                    await callback.message.answer_photo(photo=item["file_id"], caption=caption)
                elif item.get("type") == "document" and item.get("file_id"):
                    await callback.message.answer_document(document=item["file_id"], caption=caption)
            except Exception as e:
                logging.error(f"Ошибка отображения вложения тикета #{ticket_id}: {e}")

    await callback.answer()

@dp.callback_query(F.data.startswith("reply_ticket_"))
async def start_reply_ticket(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)

    if not ticket or ticket["status"] != "open":
        await callback.answer("Тикет закрыт или не найден.", show_alert=True)
        return

    admin_id = callback.from_user.id
    admin_chat_mode[admin_id] = ticket_id

    await state.update_data(reply_ticket_id=ticket_id)
    await state.set_state(AdminStates.waiting_for_reply)

    await callback.message.answer(
        f"💬 **Режим чата включён — Тикет #{ticket_id}**\n\n"
        "Просто пишите сообщения (текст, фото, файлы) — они сразу уходят пользователю, как в обычном чате.\n"
        "Чтобы выйти — нажмите «Назад в админ панель».",
        parse_mode="Markdown",
        reply_markup=get_admin_chat_mode_kb(ticket_id)
    )
    await callback.answer()

@dp.message(AdminStates.waiting_for_reply)
async def send_reply_to_user(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    admin_id = message.from_user.id
    data = await state.get_data()
    ticket_id = data.get("reply_ticket_id")
    ticket = tickets.get(ticket_id)

    if ticket and ticket["status"] == "open":
        user_id = ticket["user_id"]
        try:
            await message.send_copy(chat_id=user_id)

            if message.photo:
                msg_type = "photo"
                file_id = message.photo[-1].file_id
                text_content = message.caption or ""
            elif message.document:
                msg_type = "document"
                file_id = message.document.file_id
                text_content = message.caption or (message.document.file_name or "")
            else:
                msg_type = "text"
                file_id = None
                text_content = message.text or message.caption or ""

            ticket["messages"].append({
                "sender": f"Поддержка (Админ {admin_id})",
                "type": msg_type,
                "text": text_content,
                "file_id": file_id
            })
            # Режим чата остаётся включённым — состояние НЕ сбрасываем,
            # чтобы админ мог продолжать переписку как в обычном чате,
            # без повторного нажатия "Ответить".
        except Exception as e:
            await message.answer(f"❌ Не удалось отправить сообщение пользователю: {e}")
    else:
        await message.answer("⚠️ Тикет закрыт или не найден — режим чата выключен.")
        admin_chat_mode.pop(admin_id, None)
        await state.clear()

@dp.callback_query(F.data.startswith("close_ticket_"))
async def close_ticket(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)
    admin_id = callback.from_user.id

    if ticket and ticket["status"] == "open":
        user_id = ticket["user_id"]
        ticket["status"] = "closed"

        try:
            await bot.send_message(
                user_id,
                f"🔒 Ваше **Обращение #{ticket_id}** было закрыто администратором.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        if admin_chat_mode.get(admin_id) == ticket_id:
            admin_chat_mode.pop(admin_id, None)
            await state.clear()

        await callback.message.edit_text(
            f"🔴 **Тикет #{ticket_id} перенесен в архив.**",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад в админ панель", callback_data="admin_main_menu")]
            ])
        )
        await callback.answer()
    else:
        await callback.answer("Тикет уже закрыт.")


# --- ОБЫЧНЫЕ СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ (без команд) ---
# Пользователь просто пишет в бота, как в обычный чат с человеком.
# Сообщение автоматически попадает в его текущее открытое обращение,
# либо создаёт новое, если открытого ещё нет.
# ВАЖНО: этот хендлер должен быть зарегистрирован последним,
# чтобы не перехватывать команды и состояния FSM админа.
@dp.message(F.text | F.photo | F.document)
async def handle_user_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    # Админ/владелец тоже может писать в поддержку как обычный клиент —
    # его сообщения (когда он НЕ в состоянии ответа на чужой тикет —
    # это отсекается более ранними хендлерами FSM) создают/пополняют
    # его собственное обращение точно так же, как у обычного пользователя.
    global ticket_counter
    open_tickets = [t_id for t_id, data in tickets.items() if data["user_id"] == user_id and data["status"] == "open"]

    is_new_ticket = False
    if open_tickets:
        ticket_id = open_tickets[-1]
    else:
        ticket_id = ticket_counter
        ticket_counter += 1
        tickets[ticket_id] = {
            "user_id": user_id,
            "status": "open",
            "messages": []
        }
        is_new_ticket = True

    ticket = tickets[ticket_id]

    if message.photo:
        msg_type = "photo"
        file_id = message.photo[-1].file_id
        text_content = message.caption or ""
    elif message.document:
        msg_type = "document"
        file_id = message.document.file_id
        text_content = message.caption or (message.document.file_name or "")
    else:
        msg_type = "text"
        file_id = None
        text_content = message.text or ""

    ticket["messages"].append({
        "sender": "Пользователь",
        "type": msg_type,
        "text": text_content,
        "file_id": file_id
    })

    user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"
    header = (
        f"🆕 **Новый тикет #{ticket_id}** от {user_info}:" if is_new_ticket
        else f"📩 **Новое сообщение в Тикете #{ticket_id}** от {user_info}:"
    )

    # Уведомление о тикете видят только администраторы — пользователю ничего не сообщаем,
    # чтобы для него это выглядело как обычный чат с человеком.
    # Если пишет сам админ/владелец — не дублируем уведомление ему самому.
    for admin_id in ADMINS:
        if admin_id == user_id:
            continue
        try:
            if admin_chat_mode.get(admin_id) == ticket_id:
                # Админ уже находится в режиме чата именно с этим тикетом —
                # показываем сообщение как есть, без заголовка и кнопок,
                # чтобы это выглядело как живой диалог в Telegram.
                await message.copy_to(chat_id=admin_id)
            else:
                await bot.send_message(admin_id, header, parse_mode="Markdown")
                await message.copy_to(chat_id=admin_id, reply_markup=get_admin_ticket_manage_kb(ticket_id))
        except Exception as e:
            logging.error(f"Ошибка отправки админу {admin_id}: {e}")

    if is_new_ticket:
        # Новое обращение — сразу уточняем, по какому оно поводу.
        # Само сообщение пользователя уже сохранено в истории тикета выше,
        # этот вопрос — просто следующий шаг диалога, а не замена сообщения.
        question_text = "Вы по поводу оплаты?"
        await message.answer(question_text, reply_markup=get_payment_question_kb(ticket_id))
        ticket["messages"].append({
            "sender": "Поддержка (Бот)",
            "type": "text",
            "text": question_text,
            "file_id": None
        })
    elif ticket.pop("awaiting_payment_screenshot", False):
        # Пользователь ответил своим сообщением (скриншотом или чем-то ещё)
        # на просьбу отправить скриншот оплаты — сообщаем, что администрация свяжется.
        followup_text = "Администрация скоро с вами свяжется."
        await message.answer(followup_text)
        ticket["messages"].append({
            "sender": "Поддержка (Бот)",
            "type": "text",
            "text": followup_text,
            "file_id": None
        })


@dp.callback_query(F.data.startswith("pay_q_yes_"))
async def payment_question_yes(callback: types.CallbackQuery):
    ticket_id = int(callback.data.split("_")[3])
    ticket = tickets.get(ticket_id)
    if not ticket or ticket["user_id"] != callback.from_user.id:
        await callback.answer()
        return

    card_text = CARD if CARD else "номер карты пока не настроен — напишите нам, мы вышлем его вручную"
    reply_text = (
        f"Отправьте сумму за вашу подписку на номер карты: `{card_text}`\n\n"
        "Следующим сообщением отправьте скриншот с оплатой."
    )

    await callback.message.edit_text(reply_text, parse_mode="Markdown")
    ticket["messages"].append({
        "sender": "Поддержка (Бот)",
        "type": "text",
        "text": reply_text,
        "file_id": None
    })
    # Ждём от пользователя следующее сообщение (скриншот оплаты) —
    # на него бот ответит отдельным авто-сообщением в handle_user_message.
    ticket["awaiting_payment_screenshot"] = True
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_q_no_"))
async def payment_question_no(callback: types.CallbackQuery):
    ticket_id = int(callback.data.split("_")[3])
    ticket = tickets.get(ticket_id)
    if not ticket or ticket["user_id"] != callback.from_user.id:
        await callback.answer()
        return

    reply_text = "Администратор скоро вам ответит."

    try:
        await callback.message.delete()
    except Exception:
        pass

    await bot.send_message(callback.from_user.id, reply_text)

    ticket["messages"].append({
        "sender": "Поддержка (Бот)",
        "type": "text",
        "text": reply_text,
        "file_id": None,
        "awaiting": "admin"
    })
    await callback.answer()


# --- ЗАПУСК ---
async def main():
    await set_main_commands(bot)
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
