from __future__ import annotations

import asyncio
import html
import logging
import re
import threading
from datetime import datetime
from typing import Any, Optional

from app_tracker.config import SETTING_TELEGRAM_ADMIN_IDS, SETTING_TELEGRAM_LAST_START_AT
from app_tracker.core.database import DatabaseManager
from app_tracker.core.productivity import Productivity
from app_tracker.utils import format_duration

try:
    from aiogram import Bot, Dispatcher, F, Router
    from aiogram.enums import ParseMode
    from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
    from aiogram.filters import CommandStart
    from aiogram.methods import SendRichMessage
    from aiogram.types import (
        CallbackQuery,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        InputRichMessage,
        Message,
    )
except ImportError:
    Bot = Dispatcher = F = Router = None
    ParseMode = None
    TelegramAPIError = TelegramBadRequest = Exception
    CommandStart = None
    SendRichMessage = None
    CallbackQuery = InlineKeyboardButton = InlineKeyboardMarkup = InputRichMessage = Message = None

log = logging.getLogger(__name__)

_REFRESH_CALLBACK = "app_tracker:refresh_stats"
_MAX_TABLE_ROWS = 25

class TelegramBotService:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db
        self._token = ""
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._dispatcher: Optional[Dispatcher] = None
        self._lock = threading.RLock()

    def start(self, token: str) -> None:
        token = (token or "").strip()
        with self._lock:
            if self.is_running and token == self._token:
                return
            self.stop()
            self._token = token
            if not token:
                return
            if Bot is None:
                log.error("Telegram integration is disabled: aiogram is not installed.")
                return

            self._thread = threading.Thread(
                target=self._run_thread,
                args=(token,),
                name="TelegramBotService",
                daemon=True,
            )
            self._thread.start()
            log.info("Telegram bot integration is starting.")

    def stop(self) -> None:
        with self._lock:
            dispatcher = self._dispatcher
            loop = self._loop
            thread = self._thread

            if dispatcher is not None and loop is not None and loop.is_running():
                try:
                    future = asyncio.run_coroutine_threadsafe(dispatcher.stop_polling(), loop)
                    future.result(timeout=5)
                except RuntimeError:
                    pass
                except Exception as exc:
                    log.warning("Telegram bot stop request failed: %s", exc)

            if thread is not None and thread.is_alive():
                thread.join(timeout=5)

            self._thread = None
            self._loop = None
            self._dispatcher = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run_thread(self, token: str) -> None:
        try:
            asyncio.run(self._run_bot(token))
        except Exception:
            log.exception("Telegram bot integration stopped with an error.")
        finally:
            with self._lock:
                self._loop = None
                self._dispatcher = None

    async def _run_bot(self, token: str) -> None:
        router = self._build_router()
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        bot = Bot(token=token)

        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._dispatcher = dispatcher

        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
                handle_signals=False,
            )
        finally:
            await bot.session.close()
            log.info("Telegram bot integration stopped.")

    def _build_router(self) -> Router:
        router = Router(name="app_tracker_telegram")

        @router.message(CommandStart())
        async def start_handler(message: Message) -> None:
            user_id = message.from_user.id if message.from_user else None
            if not self._is_allowed(user_id):
                await self._send_access_denied(message.bot, message.chat.id, user_id)
                return
            self.db.set_setting(SETTING_TELEGRAM_LAST_START_AT, datetime.now().isoformat(timespec="seconds"))
            await self._send_stats(message.bot, message.chat.id)

        @router.callback_query(F.data == _REFRESH_CALLBACK)
        async def refresh_handler(callback: CallbackQuery) -> None:
            if not self._is_allowed(callback.from_user.id):
                await callback.answer("Доступ запрещён", show_alert=True)
                return
            await callback.answer("Статистика обновляется...")
            chat_id = callback.message.chat.id if callback.message else callback.from_user.id
            await self._send_stats(callback.bot, chat_id)
        return router

    def _is_allowed(self, user_id: Optional[int]) -> bool:
        if user_id is None:
            return False
        admin_ids = _parse_admin_ids(self.db.get_setting(SETTING_TELEGRAM_ADMIN_IDS, ""))
        return user_id in admin_ids

    async def _send_access_denied(self, bot: Bot, chat_id: int, user_id: Optional[int]) -> None:
        user_id_text = str(user_id) if user_id is not None else "не определён"
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "Доступ к статистике закрыт.\n"
                f"Ваш id: {user_id_text}\n\n"
                "Добавьте этот ID в Settings -> Telegram -> Admin IDs или напишите @daich."
            ),
        )

    async def _send_stats(self, bot: Bot, chat_id: int) -> None:
        markdown = build_rich_stats_markdown(self.db)
        keyboard = _stats_keyboard()
        try:
            await bot(
                SendRichMessage(
                    chat_id=chat_id,
                    rich_message=InputRichMessage(
                        markdown=markdown,
                        skip_entity_detection=True,
                    ),
                    reply_markup=keyboard,
                )
            )
        except (TelegramBadRequest, TelegramAPIError) as exc:
            log.warning("sendRichMessage failed, falling back to sendMessage: %s", exc)
            await bot.send_message(
                chat_id=chat_id,
                text=build_fallback_stats_html(self.db),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )

def build_rich_stats_markdown(db: DatabaseManager) -> str:
    snapshot = _collect_stats(db)
    totals = snapshot["totals"]
    rows = snapshot["rows"]
    visible_rows = rows[:_MAX_TABLE_ROWS]
    hidden_rows = rows[_MAX_TABLE_ROWS:]

    parts = [
        "# Статистика",
        ""
        "В случае багов напишите: [@daich](https://t.me/daich)"
        "",
        f"> Обновлено: **{_escape_md(snapshot['generated_at'])}**",
        "",
        "| Показатель | Сегодня | Неделя |",
        "|:--|--:|--:|",
        f"| Всего | **{_escape_md(format_duration(totals['today']))}** | **{_escape_md(format_duration(totals['week']))}** |",
        f"| Продуктивно | =={_escape_md(format_duration(totals['prod_today']))}== | {_escape_md(format_duration(totals['prod_week']))} |",
        f"| Непродуктивно | ||{_escape_md(format_duration(totals['unprod_today']))}|| | {_escape_md(format_duration(totals['unprod_week']))} |",
        "",
        "---",
        "",
        "## Топ приложений за сегодня",
        "",
    ]

    if visible_rows:
        parts.extend(_apps_table(visible_rows))
    else:
        parts.append("> Пока нет записанной активности за сегодня.")

    if hidden_rows:
        parts.extend([
            "",
            "<details><summary>Показать остальные приложения</summary>",
            "",
            *_apps_table(hidden_rows, start=_MAX_TABLE_ROWS + 1),
            "",
            "</details>",
        ])

    limit_rows = [row for row in rows if row["daily_limit"] or row["weekly_limit"]]
    if limit_rows:
        parts.extend([
            "",
            "## Лимиты",
            "",
            "| Приложение | День | Неделя |",
            "|:--|--:|--:|",
        ])
        for row in limit_rows[:_MAX_TABLE_ROWS]:
            parts.append(
                "| "
                f"{_escape_md(row['name'])} | "
                f"{_escape_md(format_duration(row['daily_limit']) if row['daily_limit'] else '-') } | "
                f"{_escape_md(format_duration(row['weekly_limit']) if row['weekly_limit'] else '-') } |"
            )

    parts.extend([
        "",
        "<footer>Нажмите кнопку ниже, чтобы отправить свежую статистику ещё раз.</footer>",
    ])
    return "\n".join(parts)


def build_fallback_stats_html(db: DatabaseManager) -> str:
    snapshot = _collect_stats(db)
    totals = snapshot["totals"]
    rows = snapshot["rows"][:10]
    lines = [
        "<b>AppTracker: статистика</b>",
        f"Обновлено: {html.escape(snapshot['generated_at'])}",
        "",
        f"Всего сегодня: <b>{html.escape(format_duration(totals['today']))}</b>",
        f"Всего за неделю: <b>{html.escape(format_duration(totals['week']))}</b>",
        f"Продуктивно сегодня: {html.escape(format_duration(totals['prod_today']))}",
        f"Непродуктивно сегодня: {html.escape(format_duration(totals['unprod_today']))}",
        "",
        "<b>Топ приложений</b>",
    ]
    for idx, row in enumerate(rows, start=1):
        lines.append(
            f"{idx}. {html.escape(row['name'])}: "
            f"{html.escape(format_duration(row['today']))} сегодня, "
            f"{html.escape(format_duration(row['week']))} за неделю"
        )
    return "\n".join(lines)


def _collect_stats(db: DatabaseManager) -> dict[str, Any]:
    summary = db.get_usage_summary()
    limits = db.get_all_limits()
    totals = {
        "today": 0,
        "week": 0,
        "prod_today": 0,
        "prod_week": 0,
        "unprod_today": 0,
        "unprod_week": 0,
    }
    rows = []

    for app_id, data in summary.items():
        today = int(data.get("today_display", data.get("today", 0)) or 0)
        week = int(data.get("week_display", data.get("week", 0)) or 0)
        prod = data.get("prod", Productivity.UNKNOWN)
        totals["today"] += today
        totals["week"] += week
        if prod == Productivity.PRODUCTIVE:
            totals["prod_today"] += today
            totals["prod_week"] += week
        elif prod == Productivity.UNPRODUCTIVE:
            totals["unprod_today"] += today
            totals["unprod_week"] += week

        limit = limits.get(app_id, {})
        rows.append({
            "name": str(data.get("name") or "N/A"),
            "today": today,
            "week": week,
            "status": _productivity_status(prod),
            "daily_limit": limit.get("daily"),
            "weekly_limit": limit.get("weekly"),
        })

    rows.sort(key=lambda item: (item["today"], item["week"], item["name"].lower()), reverse=True)
    return {
        "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        "totals": totals,
        "rows": rows,
    }


def _apps_table(rows: list[dict[str, Any]], *, start: int = 1) -> list[str]:
    table = [
        "| # | Приложение | Сегодня | Неделя | Статус |",
        "|--:|:--|--:|--:|:--|",
    ]
    for idx, row in enumerate(rows, start=start):
        table.append(
            "| "
            f"{idx} | "
            f"{_escape_md(row['name'])} | "
            f"{_escape_md(format_duration(row['today']))} | "
            f"{_escape_md(format_duration(row['week']))} | "
            f"{_escape_md(row['status'])} |"
        )
    return table


def _productivity_status(value: Productivity) -> str:
    if value == Productivity.PRODUCTIVE:
        return "Продуктивно"
    if value == Productivity.UNPRODUCTIVE:
        return "Непродуктивно"
    return "Неизвестно"


def _stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Обновить статистику", callback_data=_REFRESH_CALLBACK)]
        ]
    )


def _parse_admin_ids(value: object) -> set[int]:
    ids: set[int] = set()
    for item in re.split(r"[\s,;]+", str(value or "").strip()):
        if item.isdigit():
            ids.add(int(item))
    return ids


def _escape_md(value: object) -> str:
    text = str(value)
    replacements = {
        "\\": "\\\\",
        "|": "\\|",
        "`": "\\`",
        "*": "\\*",
        "_": "\\_",
        "[": "\\[",
        "]": "\\]",
        "(": "\\(",
        ")": "\\)",
        "#": "\\#",
        ">": "\\>",
    }
    return "".join(replacements.get(char, char) for char in text).replace("\n", " ")
