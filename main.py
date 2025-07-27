import os
import re
import asyncio
import logging
import subprocess
import time
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pathvalidate import sanitize_filename

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables (Render.com par set karna hai) ---
TELEGRAM_BOT_TOKEN = os.getenv("8219496647:AAG2Oua0cG_2f1lRvI9_6dn61KyH0KXHj-U")
GROUP_CHAT_ID = os.getenv("-1002827212331") # Yeh woh group ID hai jahan updates aur videos bhejega bot.

# Global dictionary to store cancellation events for ongoing processes.
# Key: chat_id, Value: asyncio.Event
cancellation_events = {}

# --- Helper Functions ---

def create_progress_bar(percentage):
    """Generates an ASCII progress bar string."""
    if percentage is None:
        return ""
    
    filled_blocks = int(percentage // 10)
    empty_blocks = 10 - filled_blocks
    bar = f"[`{'▓' * filled_blocks}{'░' * empty_blocks}`]"
    return f"{bar} {percentage:.1f}%"

async def read_yt_dlp_stderr_for_progress(stream, progress_callback, cancellation_event):
    """
    Reads stderr from yt-dlp process to extract progress and call a callback.
    """
    last_update_time = time.time()
    while True:
        line = await stream.readline()
        if not line:
            break
        if cancellation_event.is_set():
            logger.info("Cancellation requested, stopping stderr reader.")
            break

        line_str = line.decode(errors='ignore').strip()
        
        # Regex to parse download progress from yt-dlp stderr
        # Example: [download] 99.9% of 123.45MiB at 1.23MiB/s ETA 00:01
        progress_match = re.search(r'\[download\]\s+(\d+\.\d+)% of (.*?) at (.*?) ETA (.*)', line_str)
        if progress_match:
            percent = float(progress_match.group(1))
            size = progress_match.group(2)
            speed = progress_match.group(3)
            eta = progress_match.group(4)
            
            # Update only if enough time has passed to avoid rate limits
            if time.time() - last_update_time >= 3 or percent >= 99.9: # Update every 3 seconds or when nearly complete
                await progress_callback(percent, speed, eta)
                last_update_time = time.time()
        
        # Check for "already downloaded" message
        elif "has already been downloaded" in line_str:
            await progress_callback(100.0, "", "") # Assume 100% if already downloaded
            break # No more progress updates needed

    logger.info("Stderr reader finished.")


# --- Bot Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message when the /start command is issued."""
    await update.message.reply_text(
        "नमस्ते! मैं एक वीडियो डाउनलोडर बॉट हूँ। मुझे एक .txt file भेजो जिसमें वीडियो लिंक्स हों।"
        "\n\n👉 **File Format:** `Advance - Class-01 | Calculation (Cube & Cube Root) [08-Apr-2024 08:30]: https://link.m3u8`"
        "\n\n**Commands:**"
        "\n`/cancel` - चल रहे वीडियो डाउनलोड प्रोसेस को रद्द करें।"
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancels the ongoing video processing for the current chat."""
    chat_id = str(GROUP_CHAT_ID) # Use string for consistency with dict keys

    if chat_id in cancellation_events and not cancellation_events[chat_id].is_set():
        cancellation_events[chat_id].set() # Set the event to signal cancellation
        logger.info(f"Cancellation requested for chat_id: {chat_id}")
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text="✅ **वीडियो डाउनलोड/अपलोड प्रोसेस रद्द करने का अनुरोध स्वीकार किया गया है।** "
                 "वर्तमान वीडियो को समाप्त करने के बाद अगला प्रोसेस नहीं होगा।",
            parse_mode="Markdown"
        )
    else:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text="❌ **कोई सक्रिय वीडियो डाउनलोड/अपलोड प्रोसेस नहीं है जिसे रद्द किया जा सके।**",
            parse_mode="Markdown"
        )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming .txt documents."""
    user_id = update.message.from_user.id
    chat_id = str(GROUP_CHAT_ID) # Ensure we're using the target group ID for processing.

    if update.message.document and update.message.document.mime_type == "text/plain":
        document = update.message.document
        file_id = document.file_id
        file_name = document.file_name

        await update.message.reply_text(f"फ़ाइल मिली: `{file_name}`. लिंक्स प्रोसेस हो रहे हैं...", parse_mode="Markdown")

        # Create a 'downloads' directory if it doesn't exist
        os.makedirs("downloads", exist_ok=True)
        local_txt_path = os.path.join("downloads", file_name)

        # Initialize a new cancellation event for this process
        cancellation_events[chat_id] = asyncio.Event()

        initial_group_msg_text = f"🤖 Bot `{file_name}` में वीडियो लिंक्स को प्रोसेस करना शुरू कर रहा है।\n" \
                                  "कृपया प्रतीक्षा करें..."
        initial_group_msg = None
        try:
            initial_group_msg = await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=initial_group_msg_text,
                parse_mode="Markdown"
            )
            initial_group_msg_id = initial_group_msg.message_id
            
            # Download the .txt file locally [3, 4, 25, 32, 35]
            new_file = await context.bot.get_file(file_id)
            await new_file.download_to_drive(local_txt_path)
            logger.info(f"Downloaded {file_name} to {local_txt_path}")

            await process_video_links(context, local_txt_path, initial_group_msg_id, cancellation_events[chat_id])
            
            if not cancellation_events[chat_id].is_set():
                await update.message.reply_text("✅ सभी वीडियो सफलतापूर्वक प्रोसेस हो गए हैं!")
                await context.bot.edit_message_text(
                    chat_id=GROUP_CHAT_ID,
                    message_id=initial_group_msg_id,
                    text=f"🎉 `{file_name}` का प्रोसेसिंग पूरा हुआ! सभी वीडियो इस ग्रुप में भेज दिए गए हैं।",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("⛔ वीडियो प्रोसेस रद्द कर दिया गया है।")
                await context.bot.edit_message_text(
                    chat_id=GROUP_CHAT_ID,
                    message_id=initial_group_msg_id,
                    text=f"⛔ `{file_name}` का प्रोसेसिंग रद्द कर दिया गया है।",
                    parse_mode="Markdown"
                )

        except Exception as e:
            logger.error(f"Error processing document {file_name}: {e}", exc_info=True)
            await update.message.reply_text(f"❌ `{file_name}` को प्रोसेस करते समय एक त्रुटि हुई: `{e}`")
            if initial_group_msg:
                await context.bot.edit_message_text(
                    chat_id=GROUP_CHAT_ID,
                    message_id=initial_group_msg_id,
                    text=f"❌ `{file_name}` को प्रोसेस करते समय त्रुटि हुई: `{e}`",
                    parse_mode="Markdown"
                )
            else:
                await context.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=f"❌ फ़ाइल `{file_name}` को प्रोसेस करते समय एक गंभीर त्रुटि हुई: `{e}`",
                    parse_mode="Markdown"
                )
        finally:
            # Clean up the downloaded .txt file
            if os.path.exists(local_txt_path):
                os.remove(local_txt_path)
                logger.info(f"Cleaned up {local_txt_path}")
            # Remove cancellation event for this process
            if chat_id in cancellation_events:
                del cancellation_events[chat_id]
    else:
        await update.message.reply_text("कृपया एक .txt फ़ाइल भेजें।")

async def process_video_links(context: ContextTypes.DEFAULT_TYPE, txt_file_path: str, 
                              initial_group_msg_id: int, cancellation_event: asyncio.Event) -> None:
    """Reads the .txt file, parses links, downloads, and sends videos."""
    with open(txt_file_path, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f if line.strip()] # Read and filter empty lines

    total_videos = len(lines)
    if total_videos == 0:
        await context.bot.edit_message_text(
            chat_id=GROUP_CHAT_ID,
            message_id=initial_group_msg_id,
            text=f"दिए गए फ़ाइल `{os.path.basename(txt_file_path)}` में कोई वैध वीडियो लिंक नहीं है।",
            parse_mode="Markdown"
        )
        return

    for i, line in enumerate(lines):
        if cancellation_event.is_set():
            logger.info(f"Process cancelled after {i} videos.")
            await context.bot.edit_message_text(
                chat_id=GROUP_CHAT_ID,
                message_id=initial_group_msg_id,
                text=f"⛔ `{os.path.basename(txt_file_path)}` का प्रोसेसिंग रद्द कर दिया गया है। ({i}/{total_videos} वीडियो प्रोसेस हुए)",
                parse_mode="Markdown"
            )
            break # Exit the loop if cancelled

        video_num = i + 1
        
        # Regex to parse the line format: Title [Date Time]: URL
        match = re.match(r"^(.*?)\s*\[\d{2}-\w{3}-\d{4}\s\d{2}:\d{2}\]:\s*(https?://.*\.m3u8)$", line)

        if not match:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=f"⚠️ लाइन {video_num}/{total_videos} छोड़ दी गई है (`{os.path.basename(txt_file_path)}`):\n`{line}`\n"
                     "अपेक्षित फ़ॉर्मेट: `Title [DD-Mon-YYYY HH:MM]: https://link.m3u8`",
                parse_mode="Markdown"
            )
            continue

        raw_title = match.group(1).strip()
        video_url = match.group(2).strip()

        # Sanitize the title for filename
        clean_title_for_filename = re.sub(r'\s*\|\s*.*$', '', raw_title) # Remove " | Calculation (...)" part
        clean_title_for_filename = sanitize_filename(clean_title_for_filename) # [5, 9, 16, 21, 22]
        output_filepath = os.path.join("downloads", f"{clean_title_for_filename}.mp4")

        current_status_prefix = f"Processing video {video_num}/{total_videos}:\n`{raw_title}`"
        
        # Update main status message for current video
        await context.bot.edit_message_text(
            chat_id=GROUP_CHAT_ID,
            message_id=initial_group_msg_id,
            text=f"{current_status_prefix}\n⏳ डाउनलोड शुरू हो रहा है...",
            parse_mode="Markdown"
        )

        try:
            # --- Download video using yt-dlp ---
            logger.info(f"Attempting to download video {video_num}/{total_videos}: '{raw_title}' from {video_url}")
            
            command = [
                "yt-dlp",
                "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "--output", output_filepath,
                "--force-overwrites",
                "--no-part",
                "--restrict-filenames",
                video_url
            ]
            
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            # Callback for progress updates
            async def progress_callback(percent, speed, eta):
                if cancellation_event.is_set(): # Stop updates if cancelled
                    return
                progress_bar_str = create_progress_bar(percent)
                extra_info = f" ({speed} - ETA: {eta})" if speed and eta else ""
                await context.bot.edit_message_text(
                    chat_id=GROUP_CHAT_ID,
                    message_id=initial_group_msg_id,
                    text=f"{current_status_prefix}\n⬇️ डाउनलोड हो रहा है: {progress_bar_str}{extra_info}",
                    parse_mode="Markdown"
                )

            stderr_reader_task = asyncio.create_task(
                read_yt_dlp_stderr_for_progress(process.stderr, progress_callback, cancellation_event)
            )

            stdout, _ = await process.communicate() # Wait for yt-dlp to complete

            # Ensure the stderr reader task is cancelled after main process completes
            stderr_reader_task.cancel()
            try:
                await stderr_reader_task # Wait for the task to finish cleaning up
            except asyncio.CancelledError:
                logger.info("Stderr reader task cancelled successfully.")

            if process.returncode != 0:
                error_output = stderr.decode(errors='ignore')
                raise Exception(f"yt-dlp failed with code {process.returncode}: {error_output}")
            
            logger.info(f"Successfully downloaded: {output_filepath}")
            
            # --- Send video to Telegram group ---
            if cancellation_event.is_set(): # Check cancellation again before sending
                raise asyncio.CancelledError("Video sending cancelled by user.")

            await context.bot.edit_message_text(
                chat_id=GROUP_CHAT_ID,
                message_id=initial_group_msg_id,
                text=f"{current_status_prefix}\n⬆️ डाउनलोड पूरा हुआ। वीडियो अपलोड हो रहा है...",
                parse_mode="Markdown"
            )

            if os.path.exists(output_filepath) and os.path.getsize(output_filepath) > 0:
                with open(output_filepath, 'rb') as video_file:
                    await context.bot.send_video(
                        chat_id=GROUP_CHAT_ID,
                        video=video_file,
                        caption=f"🎥 **{raw_title}**",
                        supports_streaming=True,
                        read_timeout=1200,
                        write_timeout=1200,
                        width=1280, # Optional: Specify common video dimensions
                        height=720,
                        parse_mode="Markdown"
                    )
                logger.info(f"Sent video: {output_filepath}")
                # Update status in group to indicate completion for this video
                await context.bot.edit_message_text(
                    chat_id=GROUP_CHAT_ID,
                    message_id=initial_group_msg_id,
                    text=f"✅ {current_status_prefix}\nवीडियो सफलतापूर्वक भेजा गया!",
                    parse_mode="Markdown"
                )
            else:
                raise FileNotFoundError(f"डाउनलोड की गई फ़ाइल नहीं मिली या खाली है: {output_filepath}")

        except asyncio.CancelledError:
            logger.info(f"Video {video_num} processing cancelled: {raw_title}")
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=f"⛔ वीडियो {video_num}/{total_videos} (`{raw_title}`) का प्रोसेसिंग रद्द कर दिया गया।",
                parse_mode="Markdown"
            )
            # Revert main status message to indicate overall progress
            await context.bot.edit_message_text(
                chat_id=GROUP_CHAT_ID,
                message_id=initial_group_msg_id,
                text=f"कुल प्रगति: {video_num-1}/{total_videos} वीडियो प्रोसेस हुए। अगला वीडियो रद्द किया गया।",
                parse_mode="Markdown"
            )
            # Break the loop since the overall process is cancelled
            break

        except Exception as e:
            logger.error(f"Error processing video '{raw_title}': {e}", exc_info=True)
            await context.bot.send_message( # Send new message for error rather than editing main status for clarity
                chat_id=GROUP_CHAT_ID,
                text=f"❌ वीडियो {video_num}/{total_videos} (`{raw_title}`) को प्रोसेस करते समय त्रुटि हुई:\n`{e}`",
                parse_mode="Markdown"
            )
            # Revert main status message to indicate overall progress (if it's not the last video)
            if video_num < total_videos:
                await context.bot.edit_message_text(
                    chat_id=GROUP_CHAT_ID,
                    message_id=initial_group_msg_id,
                    text=f"कुल प्रगति: {video_num}/{total_videos} वीडियो प्रोसेस हुए। अगले वीडियो के साथ जारी है...",
                    parse_mode="Markdown"
                )
        finally:
            # Clean up the downloaded video file
            if os.path.exists(output_filepath):
                os.remove(output_filepath)
                logger.info(f"Cleaned up {output_filepath}")

# --- Main Bot Setup ---

def main() -> None:
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment variable set nahi hai. Exit ho raha hai.")
        exit(1)
    if not GROUP_CHAT_ID:
        logger.error("GROUP_CHAT_ID environment variable set nahi hai. Exit ho raha hai.")
        exit(1)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    port = int(os.environ.get("PORT", "8443"))

    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    # Message handlers: listens for any document, then checks if it's text/plain
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # --- Webhook setup for Render.com deployment ---
    WEBHOOK_URL = os.getenv("WEBHOOK_URL") 
    
    if WEBHOOK_URL:
        logger.info(f"Webhook par chal raha hai: {WEBHOOK_URL} port {port} par")
        application.run_webhook(
            listen="0.0.0.0", # Sabhi available interfaces par suno
            port=port,
            url_path=TELEGRAM_BOT_TOKEN, # Telegram ko URL path mein token chahiye webhooks ke liye
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}"
        )
    else:
        logger.error("WEBHOOK_URL environment variable set nahi hai. Webhook nahi chal sakta. "
                     "Yeh bot webhook deployment (jaise Render.com) ke liye banaya gaya hai. "
                     "Long polling ka prayas karega, lekin yeh Render ke free tier ke liye उपयुक्त nahi hai.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    os.makedirs("downloads", exist_ok=True) # Ensure 'downloads' directory exists
    main()