"""Telegram polling transport. No web server or public URL required."""

import asyncio
import logging
import sqlite3
import sys
from html import escape

from telegram import BotCommand, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError, TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from .config import ConfigError, Settings
from .service import CashService
from .storage import Store
from .views import dashboard, keyboard, history_view
from .history import report_day

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, service: CashService):
        self.service = service

    async def send_report(self, bot, user_id: int, text: str) -> None:
        rows = []
        # Copy buttons work entirely in the client and send no callback to the bot.
        if len(text) <= 256:
            rows.append([InlineKeyboardButton("📋 Скопіювати", copy_text=CopyTextButton(text))])
        else:
            rows.append([InlineKeyboardButton("📋 Як скопіювати весь звіт", callback_data="report_copy_help")])
        rows.append([InlineKeyboardButton("✅ Скопійовано — видалити", callback_data="report_delete")])
        await bot.send_message(chat_id=user_id, text=text,
                               reply_markup=InlineKeyboardMarkup(rows))

    async def delete_report(self, query, user_id: int) -> None:
        message = query.message
        with self.service.store.transaction() as tx:
            main_message_id = tx.state(user_id).main_message_id
        markup = getattr(message, "reply_markup", None)
        if (message is None or message.message_id == main_message_id or markup is None
                or not any(button.callback_data == "report_delete"
                           for row in markup.inline_keyboard for button in row)):
            await query.answer("Це не повідомлення зі звітом.")
            return
        try:
            await message.delete()
        except BadRequest as error:
            if "message to delete not found" not in str(error).lower():
                await query.answer("Не вдалося видалити звіт. Спробуй ще раз.", show_alert=True)
                return
        except TelegramError:
            await query.answer("Не вдалося видалити звіт. Спробуй ще раз.", show_alert=True)
            return
        await query.answer("Звіт видалено.")

    async def cleanup_message(self, bot, user_id: int, message_id: int) -> None:
        try:
            await bot.delete_message(chat_id=user_id, message_id=message_id)
        except TelegramError:
            logger.warning("Не вдалося видалити службове повідомлення.")

    async def refresh(self, bot, user_id: int, *, move_to_bottom: bool = False) -> None:
        with self.service.store.transaction() as tx:
            tx.prune_reports(report_day()[:7])
            state, data = tx.state(user_id), tx.cash()
            reports = tx.reports()
        if state.current_group and state.current_group.startswith("saved:"):
            _, day, page = state.current_group.split(":")
            saved = next((item["text"] for item in reports if item["day"] == day), None)
            text = escape(saved) if saved is not None else "Звіт уже недоступний. Історія містить лише поточний місяць."
            rows = [[("⬅️ Назад", f"history:{page}")]]
        elif state.current_group and state.current_group.startswith("history:"):
            text, rows = history_view(reports, state)
        else:
            text = dashboard(data, state, sum(item["net_income"] for item in reports))
            rows = keyboard(data, state)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=action) for label, action in row]
            for row in rows
        ])
        old_message_id = state.main_message_id
        if old_message_id is not None and not move_to_bottom:
            try:
                await bot.edit_message_text(chat_id=user_id, message_id=state.main_message_id,
                                            text=text, parse_mode=ParseMode.HTML, reply_markup=markup)
                return
            except BadRequest as error:
                description = str(error).lower()
                if "message is not modified" in description:
                    return
                if "message to edit not found" not in description and "message can't be edited" not in description:
                    raise
        message = await bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)
        with self.service.store.transaction() as tx:
            state = tx.state(user_id)
            state.main_message_id = message.message_id
            tx.save(state)
        if old_message_id is not None and old_message_id != message.message_id:
            await self.cleanup_message(bot, user_id, old_message_id)

    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, chat = update.effective_user, update.effective_chat
        query = update.callback_query
        if user is None or chat is None:
            if query:
                await query.answer("⛔ Немає доступу.")
            return
        if not self.service.authorized(user.id, chat.id, chat.type):
            if query:
                await query.answer("⛔ Немає доступу.", show_alert=True)
            elif chat.type == "private":
                await context.bot.send_message(chat_id=chat.id, text="⛔ Немає доступу.")
            return
        if query:
            if query.data == "report_copy_help":
                await query.answer(
                    "На ПК: натисни правою кнопкою на текст звіту → «Копіювати текст». "
                    "На телефоні: затисни повідомлення → «Копіювати». "
                    "Після вставлення звіту натисни «Скопійовано — видалити».",
                    show_alert=True)
                return
            if query.data == "report_delete":
                await self.delete_report(query, user.id)
                return
            try:
                await query.answer()
            except TelegramError:
                logger.warning("Не вдалося підтвердити натискання кнопки.")
            outcome = self.service.process(user.id, update.update_id, action=query.data or "",
                                           message_id=query.message.message_id)
        elif update.message:
            outcome = self.service.process(user.id, update.update_id, text=update.message.text or "")
            command = (update.message.text or "").strip().split(maxsplit=1)
            if outcome.refresh and command and command[0].split("@")[0].lower() == "/start":
                menu_message = await context.bot.send_message(
                    chat_id=user.id, text="Каса та історія звітів доступні через кнопки внизу 👇",
                    reply_markup=ReplyKeyboardMarkup(
                        [["💰 Каса", "📚 Історія звітів"]], resize_keyboard=True, is_persistent=True))
                await self.cleanup_message(context.bot, user.id, menu_message.message_id)
        else:
            return
        if update.message:
            await self.cleanup_message(context.bot, user.id, update.message.message_id)
        if outcome.report_text is not None:
            await self.send_report(context.bot, user.id, outcome.report_text)
        if outcome.refresh:
            await self.refresh(context.bot, user.id,
                               move_to_bottom=update.message is not None or outcome.report_text is not None)

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Exception text can include request URLs containing the bot token.
        logger.error("Помилка обробки: %s", type(context.error).__name__)
        if isinstance(context.error, Conflict):
            logger.error("Зупиніть інший запущений екземпляр цього бота.")
            context.application.stop_running()
            return
        if isinstance(update, Update) and update.effective_user and update.effective_chat:
            user, chat = update.effective_user, update.effective_chat
            if self.service.authorized(user.id, chat.id, chat.type):
                try:
                    await context.bot.send_message(chat_id=user.id,
                        text="❌ Не вдалося завершити дію. /start — оновити касу. Якщо суму не збережено, введи її ще раз.")
                except TelegramError:
                    logger.warning("Telegram тимчасово недоступний.")


def build_application(settings: Settings) -> Application:
    service = CashService(Store(settings.database_path), settings.allowed_users, settings.hide_zero_balances)
    transport = TelegramBot(service)

    async def startup(application: Application) -> None:
        await application.bot.delete_webhook(drop_pending_updates=False)
        await application.bot.set_my_commands([
            BotCommand("start", "Відкрити або оновити касу"),
            BotCommand("cancel", "Скасувати введення"),
            BotCommand("report", "Готовий звіт"),
            BotCommand("help", "Як користуватися касою"),
        ])
        logger.info("Бот запущено. Надішліть /start у Telegram. Ctrl+C — зупинити.")

    application = (Application.builder().token(settings.token).concurrent_updates(False)
                   .post_init(startup).build())
    application.add_handler(CallbackQueryHandler(transport.handle))
    application.add_handler(MessageHandler(filters.ALL, transport.handle))
    application.add_error_handler(transport.error)
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    logging.getLogger("telegram").setLevel(logging.CRITICAL)
    try:
        settings = Settings.load()
        asyncio.set_event_loop(asyncio.new_event_loop())  # Python 3.14 has no implicit loop.
        app = build_application(settings)
        app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False,
                        timeout=30, bootstrap_retries=0)
    except ConfigError as error:
        print(f"Налаштування: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except InvalidToken:
        print("Telegram відхилив токен. Перевірте TELEGRAM_TOKEN у .env.", file=sys.stderr)
        raise SystemExit(1) from None
    except NetworkError:
        print("Не вдалося підключитися до Telegram. Перевірте інтернет і повторіть запуск.", file=sys.stderr)
        raise SystemExit(1) from None
    except (TelegramError, OSError, sqlite3.Error) as error:
        print(f"Не вдалося запустити бота ({type(error).__name__}). Перевірте налаштування і доступ до бази.", file=sys.stderr)
        raise SystemExit(1) from None
