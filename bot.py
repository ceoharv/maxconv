import io
import logging
import zipfile

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import config
from converter import (WebSession, auto_discover, convert_via_server,
                       load_found_endpoint, parse_sessions, session_info)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

bot = Bot(config.bot_token,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

pending: dict[int, list[WebSession]] = {}


def allowed(user_id: int) -> bool:
    return not config.allowed_users or user_id in config.allowed_users


def add_sessions(user_id: int, sessions: list[WebSession]) -> int:
    have = {s.token for s in pending.setdefault(user_id, [])}
    fresh = [s for s in sessions if s.token not in have]
    pending[user_id].extend(fresh)
    return len(fresh)


async def read_document(msg: Message) -> str:
    file = await bot.get_file(msg.document.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    data = buf.getvalue()
    name = (msg.document.file_name or "").lower()
    if name.endswith(".zip"):
        out = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if not member.endswith("/"):
                    out.append(zf.read(member).decode("utf-8", "ignore"))
        return "\n".join(out)
    return data.decode("utf-8", "ignore")


def menu(n: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"▶️ Сконвертировать всё ({n} шт) в файл",
              callback_data="conv_file")
    kb.button(text="🔎 Автопоиск эндпоинта", callback_data="discover")
    kb.button(text="📋 Инфо о сессиях", callback_data="info")
    kb.button(text="🗑 Очистить очередь", callback_data="clear")
    kb.adjust(1)
    return kb


@dp.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    await msg.answer(
        "Конвертер сессий MAX (web → android), batch-режим.\n\n"
        "Кидай файлы (.txt / .zip) — накоплю сессии, сам найду эндпоинт "
        "и сконвертирую всё в готовый .txt-файл.\n\n/cancel — очистить"
    )


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message) -> None:
    pending.pop(msg.from_user.id, None)
    await msg.answer("Очередь очищена.")


@dp.message(F.document)
async def receive_document(msg: Message) -> None:
    if not allowed(msg.from_user.id):
        return
    try:
        content = await read_document(msg)
    except Exception as exc:  # noqa: BLE001
        await msg.answer(f"❌ Не прочитал файл: {exc}")
        return
    sessions = parse_sessions(content)
    if not sessions:
        await msg.answer("❌ Сессий в файле не найдено.")
        return
    added = add_sessions(msg.from_user.id, sessions)
    total = len(pending[msg.from_user.id])
    await msg.answer(
        f"Файл прочитан, новых сессий: <b>{added}</b>\n"
        f"В очереди всего: <b>{total}</b>",
        reply_markup=menu(total).as_markup(),
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def receive_text(msg: Message) -> None:
    if not allowed(msg.from_user.id):
        return
    sessions = parse_sessions(msg.text)
    if not sessions:
        await msg.answer("Не вижу сессий в сообщении.")
        return
    added = add_sessions(msg.from_user.id, sessions)
    total = len(pending[msg.from_user.id])
    await msg.answer(
        f"Новых сессий: <b>{added}</b>\nВ очереди всего: <b>{total}</b>",
        reply_markup=menu(total).as_markup(),
    )


@dp.callback_query(F.data.in_({"conv_file", "discover", "info", "clear"}))
async def process(cb: CallbackQuery) -> None:
    sessions = pending.get(cb.from_user.id) or []

    if cb.data == "clear":
        pending.pop(cb.from_user.id, None)
        await cb.message.answer("Очередь очищена.")
        await cb.answer()
        return

    if not sessions:
        await cb.answer("Очередь пуста — кинь файлы.", show_alert=True)
        return

    if cb.data == "info":
        report = "\n\n".join(
            f"<b>#{i + 1}</b>\n{session_info(s)}"
            for i, s in enumerate(sessions[:15])
        )
        await cb.message.answer(f"<pre>{report}</pre>")
        await cb.answer()
        return

    if cb.data == "discover":
        await cb.answer("Ищу...", show_alert=False)
        await cb.message.answer("🔎 Автопоиск эндпоинта, до пары минут...")
        report = await auto_discover(sessions[0])
        if config.max_auth_url:
            await cb.message.answer(
                f"✅ Найден и сохранён:\n<code>{config.max_auth_url}</code>\n\n"
                f"Жми «Сконвертировать всё»."
            )
        else:
            await cb.message.answer(
                "❌ Рабочий адрес не найден. Отчёт:\n"
                f"<pre>{report[:3500]}</pre>"
            )
        return

    # conv_file
    await cb.answer("Работаю...", show_alert=False)

    if not config.max_auth_url:
        await cb.message.answer(
            "🤖 Эндпоинта ещё нет — запускаю автопоиск (до пары минут)..."
        )
        report = await auto_discover(sessions[0])
        if not config.max_auth_url:
            await cb.message.answer(
                "❌ Автопоиск не нашёл рабочий адрес. Отчёт:\n"
                f"<pre>{report[:3500]}</pre>"
            )
            return
        await cb.message.answer(
            f"✅ Эндпоинт найден и сохранён:\n"
            f"<code>{config.max_auth_url}</code>"
        )

    await cb.answer("Конвертирую...", show_alert=False)
    lines = ["# статус | viewerId | device_id | токен"]
    ok_count = 0
    for i, s in enumerate(sessions, 1):
        ok, result = await convert_via_server(s)
        if ok:
            ok_count += 1
            lines.append(f"OK | {s.viewer_id or '?'} | {s.device_id or '?'} "
                         f"| {result}")
        else:
            lines.append(f"FAIL | {s.viewer_id or '?'} | {s.device_id or '?'} "
                         f"| {s.token} | ошибка: {result[:120]}")

    await cb.message.answer(
        f"Готово: <b>{ok_count}</b> из {len(sessions)}. Файл ниже."
    )
    await cb.message.answer_document(BufferedInputFile(
        "\n".join(lines).encode("utf-8"),
        filename="max_android_sessions.txt",
    ))


async def main() -> None:
    load_found_endpoint()
    if config.max_auth_url:
        logging.info("Эндпоинт из прошлого запуска: %s", config.max_auth_url)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())