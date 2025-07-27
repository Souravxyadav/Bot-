import os
import requests
import subprocess
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# Env vars from Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # -1002827212331

bot = Bot(token=BOT_TOKEN)

def download_video_from_m3u8(url, output_file):
    cmd = f"streamlink --default-stream best {url} -o \"{output_file}\""
    result = subprocess.run(cmd, shell=True)
    return result.returncode == 0

async def start(update, context):
    await update.message.reply_text("👋 Hello! Send me a .txt file with video URLs.")

async def handle_txt(update, context):
    file = await update.message.document.get_file()
    txt_path = "input.txt"
    await file.download_to_drive(txt_path)

    with open(txt_path, "r", encoding='utf-8') as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        try:
            if ":" not in line: continue
            title, url = line.split(":", 1)
            filename = f"{title.strip()}.mp4"

            await context.bot.send_message(chat_id=CHAT_ID, text=f"⏳ Downloading: *{title.strip()}*", parse_mode=ParseMode.MARKDOWN)
            
            success = download_video_from_m3u8(url.strip(), filename)
            if success:
                await context.bot.send_video(chat_id=CHAT_ID, video=open(filename, 'rb'), caption=f"🎬 {title.strip()}")
                os.remove(filename)
            else:
                await context.bot.send_message(chat_id=CHAT_ID, text=f"❌ Failed to download: {title.strip()}")
        except Exception as e:
            await context.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Error in line {i+1}: {str(e)}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.FILE_EXTENSION("txt"), handle_txt))
    app.run_polling()

if __name__ == '__main__':
    main()
