import asyncio
import logging
import aiohttp
import sqlite3
import os
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums.parse_mode import ParseMode
from aiogram.filters import Command
from bs4 import BeautifulSoup


# ========== НАСТРОЙКИ ==========

BOT_TOKEN = "5162326030:AAGLUgrO89qO4VISVs7sAqTR4KLxUcwbne0"                      # <<< ВСТАВИТЬ СЮДА
TARGET_CHAT_ID = -1001552308069             # ID вашей группы
TARGET_THREAD_ID = 3                        # ID ветки (топика)
CHECK_INTERVAL = 100                        # 10 минут


# ========== ИНИЦИАЛИЗАЦИЯ БАЗ ДАННЫХ ==========

def init_db():
    # база, где хранятся отправленные посты
    conn = sqlite3.connect("posted.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            msg_id TEXT PRIMARY KEY,
            channel TEXT,
            timestamp TEXT
        )
    """)
    conn.commit()
    return conn


def init_channels_db():
    # база, где хранятся отслеживаемые каналы
    conn = sqlite3.connect("channels.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            username TEXT PRIMARY KEY
        )
    """)
    conn.commit()

    # при первом запуске добавляем начальный канал
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM channels")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO channels(username) VALUES(?)", ("PolymorphicSNWS",))
        conn.commit()

    return conn


def get_channels(conn):
    rows = conn.execute("SELECT username FROM channels").fetchall()
    return [row[0] for row in rows]


def add_channel(conn, username):
    conn.execute("INSERT OR IGNORE INTO channels VALUES(?)", (username,))
    conn.commit()


def remove_channel(conn, username):
    conn.execute("DELETE FROM channels WHERE username=?", (username,))
    conn.commit()


def is_posted(conn, msg_id):
    cur = conn.cursor()
    cur.execute("SELECT msg_id FROM posts WHERE msg_id=?", (msg_id,))
    return cur.fetchone() is not None


def save_post(conn, msg_id, channel):
    conn.execute(
        "INSERT OR IGNORE INTO posts(msg_id, channel, timestamp) VALUES (?, ?, ?)",
        (msg_id, channel, datetime.now().isoformat())
    )
    conn.commit()


# ========== ПАРСИНГ СТРАНИЦЫ КАНАЛА ==========

async def get_posts_from_channel(channel):
    url = f"https://t.me/s/{channel}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for msg in soup.find_all("div", class_="tgme_widget_message_wrap"):
        msg_id = msg.get("data-post")

        if not msg_id:
            continue

        # текст
        text_block = msg.find("div", class_="tgme_widget_message_text")
        text = text_block.get_text("\n") if text_block else ""

        # медиа
        media_url = None
        photo = msg.find("a", class_="tgme_widget_message_photo_wrap")
        video = msg.find("a", class_="tgme_widget_message_video_player")
        gif = msg.find("video")
        
        if photo and "background-image" in photo.get("style", ""):
            style = photo.get("style")
            media_url = style.split("url(")[1].split(")")[0].strip("'\"")

        if video and "data-video" in video.attrs:
            media_url = video["data-video"]

        if gif and gif.get("src"):
            media_url = gif["src"]

        posts.append({
            "id": msg_id,
            "text": text,
            "media": media_url
        })

    return posts


# ========== ЗАГРУЗКА И ОТПРАВКА МЕДИА ==========

async def download_file(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read()


async def send_post(bot: Bot, post, channel):
    text_to_send = f"💬 <b>{channel}</b>\n\n{post['text']}" if post["text"] else f"💬 <b>{channel}</b>"

    # если есть фото/видео
    if post["media"]:
        data = await download_file(post["media"])
        ext = post["media"].split(".")[-1]

        filename = f"temp.{ext}"
        with open(filename, "wb") as f:
            f.write(data)

        try:
            if ext.lower() in ["jpg", "jpeg", "png", "webp"]:
                await bot.send_photo(
                    TARGET_CHAT_ID,
                    message_thread_id=TARGET_THREAD_ID,
                    photo=types.FSInputFile(filename),
                    caption=text_to_send,
                    parse_mode=ParseMode.HTML
                )
            elif ext.lower() in ["mp4", "mov"]:
                await bot.send_video(
                    TARGET_CHAT_ID,
                    message_thread_id=TARGET_THREAD_ID,
                    video=types.FSInputFile(filename),
                    caption=text_to_send,
                    parse_mode=ParseMode.HTML
                )
            else:
                # неизвестный файл — отправляем как документ
                await bot.send_document(
                    TARGET_CHAT_ID,
                    message_thread_id=TARGET_THREAD_ID,
                    document=types.FSInputFile(filename),
                    caption=text_to_send,
                    parse_mode=ParseMode.HTML
                )
        finally:
            os.remove(filename)

    else:
        await bot.send_message(
            TARGET_CHAT_ID,
            message_thread_id=TARGET_THREAD_ID,
            text=text_to_send,
            parse_mode=ParseMode.HTML
        )


# ========== РЕПОСТЕР ==========

async def repost_worker(bot: Bot):
    posts_db = init_db()
    channels_db = init_channels_db()

    while True:
        channels = get_channels(channels_db)

        print("\n===== НАЧАЛО ПРОВЕРКИ =====")

        for channel in channels:
            print(f"\n[SCAN] {channel}")

            try:
                posts = await get_posts_from_channel(channel)
            except Exception as e:
                print(f"[ERROR] Не удалось загрузить канал {channel}: {e}")
                continue

            print(f"  найдено {len(posts)} сообщений")
            print("  проверка...")

            posts.sort(key=lambda x: x["id"])

            for post in posts:
                msg_id = post["id"]

                if is_posted(posts_db, msg_id):
                    print(f"    = пропущено → {channel}/{msg_id}")
                    continue

                print(f"    ✓ новое → {channel}/{msg_id} (отправка...)")

                try:
                    await send_post(bot, post, channel)
                    save_post(posts_db, msg_id, channel)
                    print("      ✓ отправлено")
                except Exception as e:
                    print(f"[ERROR] Ошибка отправки: {e}")

        print("\n===== ОЖИДАНИЕ =====")

        # таймер
        for sec in range(CHECK_INTERVAL, 0, -1):
            m = sec // 60
            s = sec % 60
            print(f"\rСледующая проверка через {m:02d}:{s:02d}", end="")
            await asyncio.sleep(1)
        print()


# ========== КОМАНДЫ АДМИНА ==========

router = Router()

@router.message(Command("listchannels"))
async def cmd_list(msg: types.Message):
    conn = init_channels_db()
    channels = get_channels(conn)

    text = "📡 <b>Отслеживаемые каналы:</b>\n" + "\n".join(f"• {c}" for c in channels)
    await msg.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("addchannel"))
async def cmd_add(msg: types.Message):
    conn = init_channels_db()

    args = msg.text.split()
    if len(args) < 2:
        return await msg.answer("Использование:\n/addchannel <username>")

    username = args[1].replace("@", "")
    add_channel(conn, username)

    await msg.answer(f"➕ Канал <b>{username}</b> добавлен.", parse_mode=ParseMode.HTML)


@router.message(Command("removechannel"))
async def cmd_remove(msg: types.Message):
    conn = init_channels_db()

    args = msg.text.split()
    if len(args) < 2:
        return await msg.answer("Использование:\n/removechannel <username>")

    username = args[1].replace("@", "")
    remove_channel(conn, username)

    await msg.answer(f"❌ Канал <b>{username}</b> удалён.", parse_mode=ParseMode.HTML)


# ========== ЗАПУСК ==========

async def main():
    logging.basicConfig(level=logging.INFO)

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    asyncio.create_task(repost_worker(bot))

    print("[BOT STARTED]")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
