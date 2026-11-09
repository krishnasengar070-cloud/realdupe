import os
import io
import random
import time
import re
import subprocess
import shutil
import sqlite3
import json
import asyncio
from datetime import datetime, date, timedelta
from threading import Thread
from flask import Flask
from dotenv import load_dotenv
from telegram import (
    Update,
    InputMediaPhoto,
    MessageEntity,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    TypeHandler,
    filters,
    ContextTypes,
)
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import yt_dlp
import instaloader

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in .env file!")

# ====================== CONFIG ======================
ADMIN_USERNAME = "pvnuo"
DAILY_FREE_LIMIT = 5
DATA_DIR = os.getenv("DATA_DIR", ".")
DB_FILE = os.path.join(DATA_DIR, "bot_data.db")
LIFETIME_FREE_FILE = os.path.join(DATA_DIR, "lifetime_free.json")

CHANNEL_ID = -1004344831643

HARDCODED_LIFETIME_FREE = [
    6758059603,
    5375098801,
    6239058533,
    7568357675,   # new permanent free user
]
# ====================================================

user_locks = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]


def is_allowed_chat(message, bot_username: str = None) -> bool:
    if not message or not getattr(message, "chat", None):
        return False
    if message.chat.id == CHANNEL_ID:
        return True
    if message.chat.type in ["group", "supergroup"]:
        text = (message.caption or message.text or "").lower()
        mentioned = bot_username and f"@{bot_username}" in text
        is_reply = False
        if message.reply_to_message and message.reply_to_message.from_user:
            if (message.reply_to_message.from_user.is_bot and
                message.reply_to_message.from_user.username and
                message.reply_to_message.from_user.username.lower() == bot_username):
                is_reply = True
        return mentioned or is_reply
    return True


# ------------------------------------------------------
# Lifetime free JSON helpers
# ------------------------------------------------------
def load_lifetime_free_json() -> list:
    if not os.path.exists(LIFETIME_FREE_FILE):
        return []
    try:
        with open(LIFETIME_FREE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return [int(x) for x in data]
    except Exception as e:
        print(f"lifetime_free.json load error: {e}", flush=True)
    return []

def save_lifetime_free_json(user_ids: list):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LIFETIME_FREE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(set(int(x) for x in user_ids)), f, indent=2)
    except Exception as e:
        print(f"lifetime_free.json save error: {e}", flush=True)

def sync_lifetime_free_from_json():
    ids = load_lifetime_free_json()
    for uid in HARDCODED_LIFETIME_FREE:
        if uid not in ids:
            ids.append(uid)
    if not ids:
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for uid in ids:
        c.execute("INSERT OR IGNORE INTO lifetime_free (user_id) VALUES (?)", (uid,))
    conn.commit()
    conn.close()
    save_lifetime_free_json(ids)
    print(f"Restored {len(ids)} lifetime free users from JSON + Hardcoded", flush=True)

# ------------------------------------------------------
# Database
# ------------------------------------------------------
def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS lifetime_free (
            user_id INTEGER PRIMARY KEY
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id INTEGER,
            usage_date TEXT,
            count INTEGER,
            PRIMARY KEY (user_id, usage_date)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS paid_users (
            user_id INTEGER PRIMARY KEY,
            expiry TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_prefs (
            user_id INTEGER PRIMARY KEY,
            mode TEXT DEFAULT 'normal'
        )
    """)
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('global_free', '0')")
    conn.commit()
    conn.close()
    sync_lifetime_free_from_json()

def is_global_free() -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'global_free'")
    row = c.fetchone()
    conn.close()
    return bool(row and row[0] == "1")

def set_global_free(enabled: bool):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('global_free', ?)", 
              ("1" if enabled else "0",))
    conn.commit()
    conn.close()

def is_lifetime_free(user_id: int) -> bool:
    if user_id in HARDCODED_LIFETIME_FREE:
        return True
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM lifetime_free WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row)

def add_lifetime_free(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO lifetime_free (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    ids = load_lifetime_free_json()
    if user_id not in ids:
        ids.append(user_id)
        save_lifetime_free_json(ids)

def remove_lifetime_free(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM lifetime_free WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    ids = load_lifetime_free_json()
    ids = [x for x in ids if x != user_id]
    save_lifetime_free_json(ids)

def get_lifetime_free_list() -> list:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM lifetime_free")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def is_paid_user(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT expiry FROM paid_users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    try:
        expiry = date.fromisoformat(row[0])
        return expiry >= date.today()
    except Exception:
        return False

def add_paid_user(user_id: int, days: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT expiry FROM paid_users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row:
        try:
            current_expiry = date.fromisoformat(row[0])
            if current_expiry > date.today():
                new_expiry = current_expiry + timedelta(days=days)
            else:
                new_expiry = date.today() + timedelta(days=days)
        except Exception:
            new_expiry = date.today() + timedelta(days=days)
    else:
        new_expiry = date.today() + timedelta(days=days)
    c.execute("INSERT OR REPLACE INTO paid_users (user_id, expiry) VALUES (?, ?)", 
              (user_id, new_expiry.isoformat()))
    conn.commit()
    conn.close()
    return new_expiry

def get_today_usage(user_id: int) -> int:
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT count FROM daily_usage WHERE user_id = ? AND usage_date = ?", (user_id, today))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def increment_usage(user_id: int):
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO daily_usage (user_id, usage_date, count) VALUES (?, ?, 1)
        ON CONFLICT(user_id, usage_date) DO UPDATE SET count = count + 1
    """, (user_id, today))
    conn.commit()
    conn.close()

def remaining_quota(user_id: int, username: str = None) -> int:
    if username and username.lower() == ADMIN_USERNAME.lower():
        return 999999
    if is_global_free() or is_lifetime_free(user_id) or is_paid_user(user_id):
        return 999999
    return max(0, DAILY_FREE_LIMIT - get_today_usage(user_id))

def can_use_bot(user_id: int, username: str = None) -> tuple[bool, str]:
    if remaining_quota(user_id, username) > 0:
        return True, ""
    return False, (
        "You have used all 5 free actions for today.\n"
        "Please try again tomorrow or contact @pvnuo for unlimited access.\n\n"
        "Or buy unlimited access:\n"
        "👉 /buy"
    )

# ---------- Yellow Mode preference ----------
def get_user_mode(user_id: int) -> str:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT mode FROM user_prefs WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "normal"

def set_user_mode(user_id: int, mode: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_prefs (user_id, mode) VALUES (?, ?)", (user_id, mode))
    conn.commit()
    conn.close()

# ------------------------------------------------------
# Flask keep-alive
# ------------------------------------------------------
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "Bot is alive and running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()

# ------------------------------------------------------
# Texture for overlay
# ------------------------------------------------------
def create_noise_texture(width: int, height: int, unique_id: str) -> str:
    path = f"texture_{unique_id}.png"
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    for y in range(0, height, 2):
        for x in range(0, width, 2):
            v = random.randint(0, 255)
            for dx in range(2):
                for dy in range(2):
                    if x + dx < width and y + dy < height:
                        pixels[x + dx, y + dy] = (v, v, v)
    for _ in range(width * height // 50):
        x = random.randint(0, width - 1)
        y = random.randint(0, height - 1)
        pixels[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    img = img.filter(ImageFilter.GaussianBlur(radius=random.uniform(0.8, 2.0)))
    img.save(path, "PNG")
    return path

# ------------------------------------------------------
# Image enhance (Silver Border only when Yellow Mode ON)
# ------------------------------------------------------
def enhance_image(image: Image.Image, mode: str = "normal") -> Image.Image:
    if image.mode != "RGB":
        image = image.convert("RGB")

    original_size = image.size
    w, h = original_size

    crop_pct = random.uniform(0.012, 0.03)
    left = int(w * crop_pct * random.uniform(0.2, 0.8))
    top = int(h * crop_pct * random.uniform(0.2, 0.8))
    right = w - int(w * crop_pct * random.uniform(0.2, 0.8))
    bottom = h - int(h * crop_pct * random.uniform(0.2, 0.8))
    image = image.crop((left, top, right, bottom)).resize(original_size, Image.Resampling.LANCZOS)

    angle = random.uniform(0.04, 0.11) * random.choice([-1, 1])
    image = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
    image = image.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=False)

    factor = random.uniform(0.993, 0.998)
    image = image.resize((int(w * factor), int(h * factor)), Image.Resampling.LANCZOS)
    image = image.resize(original_size, Image.Resampling.LANCZOS)

    noise = Image.effect_noise(image.size, random.uniform(2.5, 5.0))
    noise = noise.convert("L")
    noise = ImageEnhance.Brightness(noise).enhance(random.uniform(0.08, 0.16))
    image = Image.blend(image, noise.convert("RGB"), random.uniform(0.04, 0.09))

    r, g, b = image.split()
    r = ImageEnhance.Brightness(r).enhance(random.uniform(0.985, 1.015))
    g = ImageEnhance.Brightness(g).enhance(random.uniform(0.985, 1.015))
    b = ImageEnhance.Brightness(b).enhance(random.uniform(0.985, 1.015))
    image = Image.merge("RGB", (r, g, b))

    image = image.filter(ImageFilter.UnsharpMask(
        radius=random.uniform(0.8, 1.4),
        percent=random.randint(70, 115),
        threshold=1
    ))

    image = ImageEnhance.Contrast(image).enhance(random.uniform(1.03, 1.10))
    image = ImageEnhance.Color(image).enhance(random.uniform(0.97, 1.05))

    r, g, b = image.split()
    r = ImageEnhance.Brightness(r).enhance(random.uniform(1.02, 1.06))
    g = ImageEnhance.Brightness(g).enhance(random.uniform(1.01, 1.03))
    image = Image.merge("RGB", (r, g, b))

    texture = Image.effect_noise(image.size, random.uniform(8, 18)).convert("RGB")
    texture = ImageEnhance.Brightness(texture).enhance(random.uniform(0.4, 0.7))
    image = Image.blend(image, texture, random.uniform(0.025, 0.055))

    # ===== Silver Border ONLY when Yellow Mode is ON =====
    if mode == "mirror_yellow":
        border_size = random.randint(5, 9)
        silver = (192, 192, 192)
        image = ImageOps.expand(image, border=border_size, fill=silver)

    out = io.BytesIO()
    image.save(out, format="JPEG", quality=random.randint(93, 97), optimize=True, progressive=True, subsampling=0)
    out.seek(0)
    return Image.open(out).convert("RGB")

# ------------------------------------------------------
# Video enhance (Yellow + Silver Border when mode ON, NO mirror)
# ------------------------------------------------------
def enhance_video(input_path: str, output_path: str, mode: str = "normal") -> bool:
    if not shutil.which("ffmpeg"):
        print("CRITICAL: ffmpeg binary not found in system PATH.", flush=True)
        return False

    duration = 0.0
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", input_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30
        )
        if probe.stdout.strip():
            duration = float(probe.stdout.strip())
    except Exception as e:
        print(f"ffprobe failed: {e}", flush=True)
        duration = 0.0

    width, height = 1280, 720
    try:
        probe2 = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", input_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20
        )
        if probe2.stdout.strip():
            parts = probe2.stdout.strip().split(",")
            if len(parts) >= 2:
                width, height = int(parts[0]), int(parts[1])
    except Exception:
        pass

    unique_id = str(int(time.time() * 1000))
    texture_path = None
    temp_path = f"temp_vid_{unique_id}.mp4"

    try:
        texture_path = create_noise_texture(min(width, 640), min(height, 360), unique_id)

        trim_start = random.uniform(0.45, 0.75)
        trim_end = random.uniform(0.45, 0.75)
        if duration > (trim_start + trim_end + 2.5):
            ss = trim_start
            t = duration - trim_start - trim_end
        else:
            ss = 0.0
            t = max(duration, 0.1)

        speed = random.uniform(0.975, 1.025)
        setpts = 1.0 / speed
        crop_pct = random.uniform(0.015, 0.032)
        target_fps = random.choice([23.976, 24, 25, 29.97, 30])
        contrast = random.uniform(1.01, 1.04)
        brightness = random.uniform(-0.005, 0.012)
        saturation = random.uniform(0.99, 1.04)
        noise_str = random.randint(3, 7)
        overlay_opacity = random.uniform(0.04, 0.085)

        # Stronger yellow + silver border only when mode ON
        if mode == "mirror_yellow":
            warm_rs = random.uniform(0.18, 0.28)
            warm_gs = random.uniform(0.08, 0.15)
            border = random.randint(6, 10)
            pad_part = f"pad=iw+{border*2}:ih+{border*2}:{border}:{border}:color=0xC0C0C0,"
        else:
            warm_rs = random.uniform(0.03, 0.08)
            warm_gs = random.uniform(0.01, 0.04)
            pad_part = ""

        vf = (
            f"trim=start={ss}:duration={t},"
            f"setpts=PTS-STARTPTS,"
            f"crop=iw*(1-{crop_pct}):ih*(1-{crop_pct}),"
            f"scale=trunc(iw/2)*2:trunc(ih/2)*2,"
            f"setpts={setpts}*PTS,"
            f"fps={target_fps},"
            f"eq=contrast={contrast}:brightness={brightness}:saturation={saturation},"
            f"colorbalance=rs={warm_rs}:gs={warm_gs}:bs=-0.04,"
            f"unsharp=3:3:0.4:2:2:0.2,"
            f"noise=alls={noise_str}:allf=t,"
            f"{pad_part}"
            f"format=yuv420p"
        )

        cmd1 = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-map_metadata", "-1",
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-threads", "2",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            "-af", f"atempo={speed}",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            "-vf", vf,
            temp_path
        ]
        result1 = subprocess.run(cmd1, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600, text=True)
        if result1.returncode != 0 or not os.path.exists(temp_path):
            print("===== FFMPEG ERROR (main) =====", flush=True)
            print(result1.stderr[-2500:] if result1.stderr else "No stderr", flush=True)
            return _fallback_enhance(input_path, output_path, ss, t, mode)

        overlay_vf = (
            f"[1:v]scale={width}:{height},format=rgba,colorchannelmixer=aa={overlay_opacity}[tex];"
            f"[0:v][tex]overlay=0:0:format=auto,format=yuv420p"
        )
        cmd2 = [
            "ffmpeg", "-y",
            "-i", temp_path,
            "-i", texture_path,
            "-filter_complex", overlay_vf,
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-c:a", "copy",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_path
        ]
        result2 = subprocess.run(cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300, text=True)
        if result2.returncode != 0 or not (os.path.exists(output_path) and os.path.getsize(output_path) > 10000):
            if os.path.exists(temp_path):
                shutil.copy(temp_path, output_path)
            else:
                return False
        return os.path.exists(output_path) and os.path.getsize(output_path) > 10000
    except Exception as e:
        print(f"FFmpeg Exception: {e}", flush=True)
        return False
    finally:
        for p in [texture_path, temp_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

def _fallback_enhance(input_path: str, output_path: str, ss: float, t: float, mode: str = "normal") -> bool:
    if mode == "mirror_yellow":
        warm = "colorbalance=rs=0.22:gs=0.11:bs=-0.04,"
        border = 8
        pad = f"pad=iw+{border*2}:ih+{border*2}:{border}:{border}:color=0xC0C0C0,"
    else:
        warm = "colorbalance=rs=0.05:gs=0.02:bs=-0.02,"
        pad = ""

    vf = (
        f"trim=start={ss}:duration={t},"
        "setpts=PTS-STARTPTS,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        "eq=contrast=1.025:brightness=0.006:saturation=1.02,"
        f"{warm}"
        f"{pad}"
        "noise=alls=4:allf=t,"
        "format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-map_metadata", "-1",
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        "-vf", vf,
        output_path
    ]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600, text=True)
        return r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 10000
    except Exception:
        return False

def compress_video_if_needed(path: str, unique_id: str, max_mb: float = 45.0) -> str:
    if not os.path.exists(path) or not shutil.which("ffmpeg"):
        return path
    size = os.path.getsize(path)
    limit = int(max_mb * 1024 * 1024)
    if size <= limit:
        return path
    crfs = [23, 26, 28, 30, 32, 35]
    current = path
    for i, crf in enumerate(crfs):
        out = f"comp_{unique_id}_{i}.mp4"
        cmd = [
            "ffmpeg", "-y", "-i", current,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2", "-crf", str(crf),
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            out
        ]
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 10000:
                if current != path and os.path.exists(current):
                    try:
                        os.remove(current)
                    except Exception:
                        pass
                current = out
                if os.path.getsize(current) <= limit:
                    return current
        except Exception as e:
            print(f"compress pass {i}: {e}", flush=True)
    return current

async def safe_send_video(message, path: str, caption: str, unique_id: str, also_to_channel: bool = False, context=None):
    path = compress_video_if_needed(path, unique_id, max_mb=45.0)
    if not os.path.exists(path):
        return False
    size = os.path.getsize(path)
    force_doc = size > 40 * 1024 * 1024
    try:
        with open(path, "rb") as vid:
            if force_doc:
                await message.reply_document(
                    document=vid,
                    caption=caption + " (sent as file)",
                    filename=os.path.basename(path) or "video.mp4",
                    read_timeout=120, write_timeout=120, connect_timeout=60,
                )
            else:
                await message.reply_video(
                    video=vid, caption=caption, supports_streaming=True,
                    read_timeout=120, write_timeout=120, connect_timeout=60,
                )
        
        # Auto post to channel ONLY if admin
        if also_to_channel and context:
            try:
                with open(path, "rb") as vid2:
                    if force_doc:
                        await context.bot.send_document(
                            chat_id=CHANNEL_ID,
                            document=vid2,
                            caption=caption,
                            filename=os.path.basename(path) or "video.mp4",
                            read_timeout=120, write_timeout=120, connect_timeout=60,
                        )
                    else:
                        await context.bot.send_video(
                            chat_id=CHANNEL_ID,
                            video=vid2,
                            caption=caption,
                            supports_streaming=True,
                            read_timeout=120, write_timeout=120, connect_timeout=60,
                        )
            except Exception as e:
                print(f"Channel video post failed: {e}", flush=True)
        
        return True
    except Exception as e:
        print(f"safe_send_video first try: {e}", flush=True)
        try:
            path2 = compress_video_if_needed(path, unique_id + "_r", max_mb=38.0)
            with open(path2, "rb") as vid:
                await message.reply_document(
                    document=vid,
                    caption=caption + " (sent as file)",
                    filename=os.path.basename(path2) or "video.mp4",
                    read_timeout=120, write_timeout=120, connect_timeout=60,
                )
            if also_to_channel and context:
                try:
                    with open(path2, "rb") as vid2:
                        await context.bot.send_document(
                            chat_id=CHANNEL_ID,
                            document=vid2,
                            caption=caption,
                            filename=os.path.basename(path2) or "video.mp4",
                        )
                except Exception:
                    pass
            return True
        except Exception as e2:
            print(f"document fallback failed: {e2}", flush=True)
            return False

# ------------------------------------------------------
# Admin Commands
# ------------------------------------------------------
async def freeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    set_global_free(True)
    await update.message.reply_text("✅ Bot is now FREE for everyone.")

async def removefreeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    set_global_free(False)
    await update.message.reply_text("✅ Daily limit mode is now active again.")

async def freeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/freeuser @username\n"
            "/freeuser 123456789\n\n"
            "Note: User must have started the bot at least once."
        )
        return
    target = context.args[0].lstrip("@")
    try:
        if target.isdigit():
            target_id = int(target)
            chat = await context.bot.get_chat(target_id)
        else:
            chat = await context.bot.get_chat(f"@{target}")
            target_id = chat.id
        add_lifetime_free(target_id)
        await update.message.reply_text(f"✅ @{chat.username or target_id} now has lifetime unlimited access (saved permanently).")
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="🎉 You now have unlimited access to this bot forever!\nEnjoy."
            )
        except Exception:
            pass
    except Exception:
        await update.message.reply_text(
            f"❌ Could not find user.\n\n"
            f"Possible reasons:\n"
            f"• User has never started this bot\n"
            f"• Username is incorrect\n\n"
            f"Solution: Ask the user to send /start to the bot first, then try again.\n"
            f"Or use their numeric User ID."
        )

async def removefree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/removefree @username\n"
            "/removefree 123456789"
        )
        return
    target = context.args[0].lstrip("@")
    try:
        if target.isdigit():
            target_id = int(target)
            chat = await context.bot.get_chat(target_id)
        else:
            chat = await context.bot.get_chat(f"@{target}")
            target_id = chat.id
        remove_lifetime_free(target_id)
        await update.message.reply_text(f"✅ Removed lifetime free from @{chat.username or target_id}")
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="Your unlimited access has been removed.\nYou are now on the free daily limit (5 actions per day)."
            )
        except Exception:
            pass
    except Exception:
        await update.message.reply_text(
            f"❌ Could not find user.\n\n"
            f"User must have started the bot at least once.\n"
            f"Try using their numeric User ID instead."
        )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    global_free = is_global_free()
    free_list = get_lifetime_free_list()
    text = f"📊 Bot Status\n\n"
    text += f"FFmpeg Installed: {'YES ✅' if shutil.which('ffmpeg') else 'NO ❌'}\n"
    text += f"Global Free Mode: {'ON ✅' if global_free else 'OFF (Daily limit active)'}\n"
    text += f"Daily Free Limit: {DAILY_FREE_LIMIT} (image + video + link)\n"
    text += f"Data Dir: {DATA_DIR}\n\n"
    text += f"Lifetime Free Users ({len(free_list)}):\n"
    if free_list:
        for uid in free_list:
            username_str = ""
            try:
                chat = await context.bot.get_chat(uid)
                if chat.username:
                    username_str = f" (@{chat.username})"
                elif chat.first_name:
                    username_str = f" ({chat.first_name})"
            except Exception:
                pass
            text += f"• {uid}{username_str}\n"
    else:
        text += "None\n"
    await update.message.reply_text(text)

# ------------------------------------------------------
# Buy — CRYPTO ONLY
# ------------------------------------------------------
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💎 Unlimited Access (Crypto Only)\n\n"
        "Pay with Solana (SOL):\n\n"
        "`7vxg8qL8Mc3zG5TH9GoQSuxTAVnUS5BaacWkPrNL2wYe`\n\n"
        "• $5  → 30 days unlimited\n"
        "• $12 → 3 months unlimited\n\n"
        "After payment send screenshot to @pvnuo\n"
        "Access will be activated manually.\n\n"
        "For any help contact @pvnuo",
        parse_mode="Markdown"
    )

# ------------------------------------------------------
# Keyboard
# ------------------------------------------------------
def get_user_keyboard(username: str = None):
    keyboard = [
        [KeyboardButton("How to use"), KeyboardButton("About")],
        [KeyboardButton("Buy"), KeyboardButton("Help")],
        [KeyboardButton("Yellow Mode")],
    ]
    if username and username.lower() == ADMIN_USERNAME.lower():
        keyboard.append([KeyboardButton("Status"), KeyboardButton("Free All")])
        keyboard.append([KeyboardButton("Remove Free All"), KeyboardButton("Free User")])
        keyboard.append([KeyboardButton("Remove Free")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ------------------------------------------------------
# Commands (long versions with custom emoji)
# ------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_parts = []
    entities = []
    current_offset = 0

    def add_text(s: str):
        nonlocal current_offset
        text_parts.append(s)
        current_offset += len(s.encode("utf-16-le")) // 2

    def add_custom(emoji_id: str, placeholder: str = "🔥"):
        nonlocal current_offset
        text_parts.append(placeholder)
        length = len(placeholder.encode("utf-16-le")) // 2
        entities.append(MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=current_offset,
            length=length,
            custom_emoji_id=str(emoji_id)
        ))
        current_offset += length

    add_text("Welcome!\n\n")
    add_text("Send any image or video and I will clean it for you.\n")
    add_text("This helps remove AI labels and reduces the chance of unoriginal content strikes when you repost.\n\n")
    add_text("You can also send any ")
    add_custom("6201514939530618666")
    add_text(" Instagram, ")
    add_custom("6204008550298033491")
    add_text(" TikTok or ")
    add_custom("6203873525116183636")
    add_text(" Twitter/X link and I will download it in original quality.\n\n")
    add_text("🚨 Note: No tool can completely remove AI signatures from every file. Results vary by platform and content, but this bot works well on many images & videos.\n\n")
    add_text("New: Press Yellow Mode for stronger yellowish filter + silver border on videos (and silver border on images).\n\n")
    add_text("In groups: mention me with the photo, video or link.\n\n")
    add_text("Free users get 5 actions per day.\n")
    add_text("Unlimited → /buy (crypto only)\n\n")
    add_text("For any help contact @pvnuo ")
    add_custom("6201528571756815592")

    text = "".join(text_parts)
    await update.message.reply_text(text, entities=entities, reply_markup=get_user_keyboard(update.effective_user.username if update.effective_user else None))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_parts = []
    entities = []
    current_offset = 0

    def add_text(s: str):
        nonlocal current_offset
        text_parts.append(s)
        current_offset += len(s.encode("utf-16-le")) // 2

    def add_custom(emoji_id: str, placeholder: str = "🔥"):
        nonlocal current_offset
        text_parts.append(placeholder)
        length = len(placeholder.encode("utf-16-le")) // 2
        entities.append(MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=current_offset,
            length=length,
            custom_emoji_id=str(emoji_id)
        ))
        current_offset += length

    add_text("Available Commands:\n\n")
    add_text("/start  – Start the bot\n")
    add_text("/help   – Show this help\n")
    add_text("/about  – About this bot\n")
    add_text("/how    – How it works\n")
    add_text("/buy    – Buy unlimited access (crypto)\n\n")
    add_text("How to use:\n")
    add_text("• Send any photo → get a unique cleaned version\n")
    add_text("• Send any video → get a unique high-quality version\n")
    add_text("• Send ")
    add_custom("6201514939530618666")
    add_text(" Instagram / ")
    add_custom("6204008550298033491")
    add_text(" TikTok / ")
    add_custom("6203873525116183636")
    add_text(" Twitter/X link → download media in original quality\n\n")
    add_text("Yellow Mode: Stronger yellow + silver border on videos, silver border on images.\n\n")
    add_text("In groups: mention me with the photo, video or link.\n\n")
    add_text("Free users get 5 actions per day (image + video + link).\n")
    add_text("For any help contact @pvnuo")

    text = "".join(text_parts)
    await update.message.reply_text(text, entities=entities, reply_markup=get_user_keyboard(update.effective_user.username if update.effective_user else None))

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_parts = []
    entities = []
    current_offset = 0

    def add_text(s: str):
        nonlocal current_offset
        text_parts.append(s)
        current_offset += len(s.encode("utf-16-le")) // 2

    def add_custom(emoji_id: str, placeholder: str = "🔥"):
        nonlocal current_offset
        text_parts.append(placeholder)
        length = len(placeholder.encode("utf-16-le")) // 2
        entities.append(MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=current_offset,
            length=length,
            custom_emoji_id=str(emoji_id)
        ))
        current_offset += length

    add_text("About this Bot:\n\n")
    add_text("• Send any image or video and get a cleaned unique version.\n")
    add_text("• Helps reduce AI labels and unoriginal content strikes while keeping high quality.\n")
    add_text("• You can also send any ")
    add_custom("6201514939530618666")
    add_text(" Instagram, ")
    add_custom("6204008550298033491")
    add_text(" TikTok or ")
    add_custom("6203873525116183636")
    add_text(" Twitter/X link and I will download the media in original quality.\n\n")
    add_text("Yellow Mode available for stronger yellowish look + silver border.\n\n")
    add_text("In groups: mention me with the photo, video or link.\n\n")
    add_text("For any help contact @pvnuo ")
    add_custom("6201528571756815592")

    text = "".join(text_parts)
    await update.message.reply_text(text, entities=entities, reply_markup=get_user_keyboard(update.effective_user.username if update.effective_user else None))

async def how(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_parts = []
    entities = []
    current_offset = 0

    def add_text(s: str):
        nonlocal current_offset
        text_parts.append(s)
        current_offset += len(s.encode("utf-16-le")) // 2

    def add_custom(emoji_id: str, placeholder: str = "🔥"):
        nonlocal current_offset
        text_parts.append(placeholder)
        length = len(placeholder.encode("utf-16-le")) // 2
        entities.append(MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=current_offset,
            length=length,
            custom_emoji_id=str(emoji_id)
        ))
        current_offset += length

    add_text("How it works:\n\n")
    add_text("1. Send a photo or video directly → bot cleans metadata + applies unique fingerprint (crop + texture overlay + speed + warm tone + fps) so platforms see it as original.\n\n")
    add_text("2. Yellow Mode:\n")
    add_text("   • Press the Yellow Mode button to turn it ON.\n")
    add_text("   • When ON:\n")
    add_text("     - Videos get stronger yellowish filter + thin silver border\n")
    add_text("     - Images get thin silver border\n")
    add_text("   • Press the button again to turn it OFF (back to normal processing).\n\n")
    add_text("3. Send Instagram / TikTok / Twitter-X link → bot downloads original quality media.\n")
    add_text("4. In groups you must mention the bot or reply to its message.\n\n")
    add_text("Free users get 5 actions/day. Unlimited plans available via /buy (crypto only).\n\n")
    add_text("For any help contact @pvnuo")

    text = "".join(text_parts)
    await update.message.reply_text(text, entities=entities, reply_markup=get_user_keyboard(update.effective_user.username if update.effective_user else None))

# ------------------------------------------------------
# Image handler
# ------------------------------------------------------
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message or update.channel_post or update.message
    if not message:
        return

    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username if user else None
    is_channel = message.chat and message.chat.id == CHANNEL_ID

    mode = get_user_mode(user_id) if not is_channel else "normal"
    is_admin = bool(username and username.lower() == ADMIN_USERNAME.lower())

    if not is_channel:
        lock = get_user_lock(user_id)
        async with lock:
            left = remaining_quota(user_id, username)
            if left <= 0:
                await message.reply_text(
                    "You have used all 5 free actions for today.\n"
                    "Please try again tomorrow or contact @pvnuo for unlimited access.\n\n"
                    "👉 /buy"
                )
                return

            bot_username = (await context.bot.get_me()).username.lower()
            if not is_allowed_chat(message, bot_username):
                return

            status_msg = await message.reply_text("⌛")
            start_time = time.time()
            try:
                if message.photo:
                    file = await context.bot.get_file(message.photo[-1].file_id)
                elif message.document and message.document.mime_type and "image" in message.document.mime_type:
                    file = await context.bot.get_file(message.document.file_id)
                else:
                    await status_msg.delete()
                    return

                image_bytes = await file.download_as_bytearray()
                image = Image.open(io.BytesIO(image_bytes))
                result = enhance_image(image, mode=mode)
                output = io.BytesIO()
                quality = random.randint(93, 97)
                result.save(output, format="JPEG", quality=quality, optimize=True, progressive=True, subsampling=0)
                output.seek(0)
                elapsed = round(time.time() - start_time, 2)
                await message.reply_photo(
                    photo=output,
                    caption=f"✅ Unique version ready\n⏱ {elapsed}s | Quality: {quality}",
                )
                increment_usage(user_id)

                # Auto post to channel ONLY if admin
                if is_admin:
                    try:
                        output.seek(0)
                        await context.bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=output,
                            caption=f"✅ Unique version ready | Quality: {quality}"
                        )
                    except Exception as e:
                        print(f"Channel image post failed: {e}", flush=True)

                # Auto delete original message if admin
                if is_admin:
                    try:
                        await message.delete()
                    except Exception as e:
                        print(f"Failed to delete original message: {e}", flush=True)

                try:
                    await status_msg.delete()
                except Exception:
                    pass
            except Exception as e:
                print(f"Image error: {e}", flush=True)
                try:
                    await status_msg.edit_text("❌ Failed to process the image.")
                except Exception:
                    await message.reply_text("❌ Failed to process the image.")
    else:
        status_msg = await message.reply_text("⌛")
        start_time = time.time()
        try:
            if message.photo:
                file = await context.bot.get_file(message.photo[-1].file_id)
            elif message.document and message.document.mime_type and "image" in message.document.mime_type:
                file = await context.bot.get_file(message.document.file_id)
            else:
                await status_msg.delete()
                return

            image_bytes = await file.download_as_bytearray()
            image = Image.open(io.BytesIO(image_bytes))
            result = enhance_image(image, mode="normal")
            output = io.BytesIO()
            quality = random.randint(93, 97)
            result.save(output, format="JPEG", quality=quality, optimize=True, progressive=True, subsampling=0)
            output.seek(0)
            elapsed = round(time.time() - start_time, 2)
            await message.reply_photo(
                photo=output,
                caption=f"✅ Unique version ready\n⏱ {elapsed}s | Quality: {quality}",
            )
            try:
                await status_msg.delete()
                await message.delete()
            except Exception:
                pass
        except Exception as e:
            print(f"Channel image error: {e}", flush=True)
            try:
                await status_msg.edit_text("❌ Failed.")
            except Exception:
                pass

# ------------------------------------------------------
# Video handler
# ------------------------------------------------------
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message or update.channel_post or update.message
    if not message:
        return

    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username if user else None
    is_channel = message.chat and message.chat.id == CHANNEL_ID

    mode = get_user_mode(user_id) if not is_channel else "normal"
    is_admin = bool(username and username.lower() == ADMIN_USERNAME.lower())

    if not is_channel:
        lock = get_user_lock(user_id)
        async with lock:
            left = remaining_quota(user_id, username)
            if left <= 0:
                await message.reply_text(
                    "You have used all 5 free actions for today.\n"
                    "Please try again tomorrow or contact @pvnuo for unlimited access.\n\n"
                    "👉 /buy"
                )
                return

            bot_username = (await context.bot.get_me()).username.lower()
            if not is_allowed_chat(message, bot_username):
                return

            status_msg = await message.reply_text("⌛")

            if not shutil.which("ffmpeg"):
                await status_msg.edit_text("❌ Server error: FFmpeg not installed.")
                return

            target_obj = message.video or (message.document if message.document and "video" in (message.document.mime_type or "") else None)
            if not target_obj:
                await status_msg.edit_text("❌ Unsupported video format.")
                return

            if target_obj.file_size and target_obj.file_size > 20 * 1024 * 1024:
                size_mb = round(target_obj.file_size / (1024 * 1024), 1)
                await status_msg.edit_text(
                    f"❌ File too large ({size_mb} MB).\n"
                    f"Telegram Bot API limits bot downloads to 20 MB max.\n"
                    f"Please compress or trim the video before sending."
                )
                return

            unique_id = str(int(time.time() * 1000))
            input_path = f"input_{unique_id}.mp4"
            output_path = f"output_{unique_id}.mp4"
            try:
                file = await context.bot.get_file(target_obj.file_id)
                await file.download_to_drive(input_path)
                success = enhance_video(input_path, output_path, mode=mode)

                caption = "✅ Here’s your modified video"
                if mode == "mirror_yellow":
                    caption = "✅ Yellow Mode + Silver Border applied"

                if success and os.path.exists(output_path):
                    # only send to channel if admin
                    ok = await safe_send_video(
                        message, output_path, caption, unique_id, 
                        also_to_channel=is_admin, 
                        context=context
                    )
                    if ok:
                        increment_usage(user_id)

                        # Auto delete original message if admin
                        if is_admin:
                            try:
                                await message.delete()
                            except Exception as e:
                                print(f"Failed to delete original message: {e}", flush=True)

                        try:
                            await status_msg.delete()
                        except Exception:
                            pass
                    else:
                        await status_msg.edit_text("❌ Failed to send the video.")
                else:
                    await status_msg.edit_text("❌ Failed to process the video.")
            except Exception as e:
                print(f"Video error: {e}", flush=True)
                try:
                    await status_msg.edit_text("❌ Failed to process the video.")
                except Exception:
                    pass
            finally:
                for p in [input_path, output_path]:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                for f in os.listdir("."):
                    if f.startswith(f"comp_{unique_id}") or f.startswith(f"texture_{unique_id}") or f.startswith(f"temp_vid_{unique_id}"):
                        try:
                            os.remove(f)
                        except Exception:
                            pass
    else:
        status_msg = await message.reply_text("⌛")
        if not shutil.which("ffmpeg"):
            await status_msg.edit_text("❌ FFmpeg missing.")
            return
        target_obj = message.video or (message.document if message.document and "video" in (message.document.mime_type or "") else None)
        if not target_obj:
            await status_msg.edit_text("❌ Unsupported.")
            return
        unique_id = str(int(time.time() * 1000))
        input_path = f"input_{unique_id}.mp4"
        output_path = f"output_{unique_id}.mp4"
        try:
            file = await context.bot.get_file(target_obj.file_id)
            await file.download_to_drive(input_path)
            success = enhance_video(input_path, output_path, mode="normal")
            if success and os.path.exists(output_path):
                ok = await safe_send_video(message, output_path, "✅ Here’s your modified video", unique_id)
                if ok:
                    try:
                        await status_msg.delete()
                        await message.delete()
                    except Exception:
                        pass
                else:
                    await status_msg.edit_text("❌ Failed to send.")
            else:
                await status_msg.edit_text("❌ Failed.")
        except Exception as e:
            print(f"Channel video error: {e}", flush=True)
            try:
                await status_msg.edit_text("❌ Failed.")
            except Exception:
                pass
        finally:
            for p in [input_path, output_path]:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

# ------------------------------------------------------
# Instagram Download
# ------------------------------------------------------
def download_instagram(url: str, unique_id: str):
    L = instaloader.Instaloader(
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        max_connection_attempts=3,
    )
    shortcode = None
    if "/p/" in url:
        shortcode = url.split("/p/")[1].split("/")[0].split("?")[0]
    elif "/reel/" in url:
        shortcode = url.split("/reel/")[1].split("/")[0].split("?")[0]
    elif "/tv/" in url:
        shortcode = url.split("/tv/")[1].split("/")[0].split("?")[0]
    if not shortcode:
        return []
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
    except Exception as e:
        print(f"Instaloader error: {e}", flush=True)
        return []
    paths = []
    folder = f"temp_{unique_id}"
    os.makedirs(folder, exist_ok=True)
    try:
        L.download_post(post, target=folder)
        for file in os.listdir(folder):
            full = os.path.join(folder, file)
            if file.endswith((".jpg", ".jpeg", ".png", ".webp", ".mp4")):
                paths.append(full)
    except Exception as e:
        print(f"Download post error: {e}", flush=True)
    return paths

# ------------------------------------------------------
# TikTok Download (FULL)
# ------------------------------------------------------
async def download_tiktok(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, is_channel: bool = False):
    text_parts = []
    entities = []
    current_offset = 0

    def add_text(s: str):
        nonlocal current_offset
        text_parts.append(s)
        current_offset += len(s.encode("utf-16-le")) // 2

    def add_custom(emoji_id: str, placeholder: str = "🔥"):
        nonlocal current_offset
        text_parts.append(placeholder)
        length = len(placeholder.encode("utf-16-le")) // 2
        entities.append(MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=current_offset,
            length=length,
            custom_emoji_id=str(emoji_id)
        ))
        current_offset += length

    add_text("Downloading from ")
    add_custom("6204008550298033491")
    add_text(" TikTok...")

    status_text = await update.message.reply_text("".join(text_parts), entities=entities)
    status_emoji = await update.message.reply_text("⌛")

    unique_id = str(int(time.time() * 1000))
    folder = f"tt_{unique_id}_dir"
    os.makedirs(folder, exist_ok=True)
    paths = []
    opened_files = []
    try:
        ydl_opts = {
            "outtmpl": os.path.join(folder, f"tt_{unique_id}.%(ext)s"),
            "format": "bv*[height<=1080]+ba/b[height<=1080]/best[height<=1080]/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 60,
            "retries": 10,
            "fragment_retries": 10,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Referer": "https://www.tiktok.com/",
            },
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                base = filename.rsplit(".", 1)[0]
                for ext in [".mp4", ".mkv", ".webm", ".mp3", ".m4a"]:
                    candidate = base + ext
                    if os.path.exists(candidate):
                        paths.append(candidate)
                        break
        except Exception as e:
            print(f"yt-dlp TikTok: {e}", flush=True)
        has_video = any(p.lower().endswith((".mp4", ".mkv", ".webm")) for p in paths)
        if not has_video:
            try:
                cmd = ["gallery-dl", "--dest", folder, "--filename", "{id}_{num}.{extension}", "--no-mtime", url]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
                if result.returncode == 0:
                    for root, _, files in os.walk(folder):
                        for f in files:
                            full = os.path.join(root, f)
                            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mp3")):
                                paths.append(full)
            except Exception as e:
                print(f"gallery-dl error: {e}", flush=True)
        images = [p for p in paths if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
        videos = [p for p in paths if p.lower().endswith((".mp4", ".mkv", ".webm"))]
        audios = [p for p in paths if p.lower().endswith((".mp3", ".m4a"))]
        success = False
        if images:
            media = []
            for p in images[:10]:
                f = open(p, "rb")
                opened_files.append(f)
                media.append(InputMediaPhoto(f))
            await update.message.reply_media_group(media=media)
            success = True
        for v in videos:
            ok = await safe_send_video(update.message, v, "✅ Here’s your TikTok video", unique_id)
            if ok:
                success = True
        if not success and audios:
            with open(audios[0], "rb") as a:
                await update.message.reply_audio(audio=a, caption="🎵 TikTok audio")
            success = True
        if success:
            try:
                await status_text.delete()
                await status_emoji.delete()
            except Exception:
                pass
            if is_channel:
                try:
                    await update.message.delete()
                except Exception:
                    pass
        else:
            try:
                await status_text.edit_text("❌ Failed to download. Make sure the link is public.")
                await status_emoji.delete()
            except Exception:
                pass
    except Exception as e:
        print(f"TikTok error: {e}", flush=True)
        try:
            await status_text.edit_text(f"❌ Failed to download TikTok.\n{str(e)[:120]}")
            await status_emoji.delete()
        except Exception:
            pass
    finally:
        for f in opened_files:
            try:
                f.close()
            except Exception:
                pass
        if os.path.exists(folder):
            try:
                shutil.rmtree(folder, ignore_errors=True)
            except Exception:
                pass

# ------------------------------------------------------
# Twitter / X Download (FULL)
# ------------------------------------------------------
async def download_twitter(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, is_channel: bool = False):
    text_parts = []
    entities = []
    current_offset = 0

    def add_text(s: str):
        nonlocal current_offset
        text_parts.append(s)
        current_offset += len(s.encode("utf-16-le")) // 2

    def add_custom(emoji_id: str, placeholder: str = "🔥"):
        nonlocal current_offset
        text_parts.append(placeholder)
        length = len(placeholder.encode("utf-16-le")) // 2
        entities.append(MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=current_offset,
            length=length,
            custom_emoji_id=str(emoji_id)
        ))
        current_offset += length

    add_text("Downloading from ")
    add_custom("6203873525116183636")
    add_text(" Twitter/X...")

    status_text = await update.message.reply_text("".join(text_parts), entities=entities)
    status_emoji = await update.message.reply_text("⌛")

    unique_id = str(int(time.time() * 1000))
    folder = f"tw_{unique_id}_dir"
    os.makedirs(folder, exist_ok=True)
    paths = []
    opened_files = []
    try:
        ydl_opts = {
            "outtmpl": os.path.join(folder, f"tw_{unique_id}_%(id)s.%(ext)s"),
            "format": "bv*[height<=1080]+ba/b[height<=1080]/best[height<=1080]/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 60,
            "retries": 10,
            "fragment_retries": 10,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Referer": "https://x.com/",
            },
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if "entries" in info:
                    for entry in info["entries"]:
                        if entry:
                            filename = ydl.prepare_filename(entry)
                            base = filename.rsplit(".", 1)[0]
                            for ext in [".mp4", ".mkv", ".webm", ".jpg", ".jpeg", ".png", ".webp"]:
                                candidate = base + ext
                                if os.path.exists(candidate):
                                    paths.append(candidate)
                else:
                    filename = ydl.prepare_filename(info)
                    base = filename.rsplit(".", 1)[0]
                    for ext in [".mp4", ".mkv", ".webm", ".jpg", ".jpeg", ".png", ".webp"]:
                        candidate = base + ext
                        if os.path.exists(candidate):
                            paths.append(candidate)
                            break
        except Exception as e:
            print(f"yt-dlp Twitter: {e}", flush=True)
        if not paths:
            try:
                cmd = ["gallery-dl", "--dest", folder, "--filename", "{id}_{num}.{extension}", "--no-mtime", url]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
                if result.returncode == 0:
                    for root, _, files in os.walk(folder):
                        for f in files:
                            full = os.path.join(root, f)
                            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mkv", ".webm")):
                                paths.append(full)
            except Exception as e:
                print(f"gallery-dl Twitter error: {e}", flush=True)
        images = [p for p in paths if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
        videos = [p for p in paths if p.lower().endswith((".mp4", ".mkv", ".webm"))]
        success = False
        if images:
            media = []
            for p in images[:10]:
                f = open(p, "rb")
                opened_files.append(f)
                media.append(InputMediaPhoto(f))
            await update.message.reply_media_group(media=media)
            success = True
        for v in videos:
            ok = await safe_send_video(update.message, v, "✅ Here’s your Twitter/X video", unique_id)
            if ok:
                success = True
        if success:
            try:
                await status_text.delete()
                await status_emoji.delete()
            except Exception:
                pass
            if is_channel:
                try:
                    await update.message.delete()
                except Exception:
                    pass
        else:
            try:
                await status_text.edit_text("❌ Failed to download. Make sure the link is public.")
                await status_emoji.delete()
            except Exception:
                pass
    except Exception as e:
        print(f"Twitter error: {e}", flush=True)
        try:
            await status_text.edit_text(f"❌ Failed to download Twitter/X.\n{str(e)[:120]}")
            await status_emoji.delete()
        except Exception:
            pass
    finally:
        for f in opened_files:
            try:
                f.close()
            except Exception:
                pass
        if os.path.exists(folder):
            try:
                shutil.rmtree(folder, ignore_errors=True)
            except Exception:
                pass

# ------------------------------------------------------
# Text handler
# ------------------------------------------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message or update.channel_post or update.message
    if not message or not message.text:
        return
    text = message.text.strip()
    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username if user else None
    is_channel = message.chat and message.chat.id == CHANNEL_ID

    if text == "How to use":
        await how(update, context)
        return
    if text == "About":
        await about(update, context)
        return
    if text == "Buy":
        await buy(update, context)
        return
    if text == "Help":
        await help_command(update, context)
        return
    if text == "Status":
        await status(update, context)
        return
    if text == "Free All":
        await freeall(update, context)
        return
    if text == "Remove Free All":
        await removefreeall(update, context)
        return
    if text == "Free User":
        await update.message.reply_text(
            "Usage:\n"
            "/freeuser @username\n"
            "/freeuser 123456789\n\n"
            "Note: User must have started the bot at least once."
        )
        return
    if text == "Remove Free":
        await update.message.reply_text(
            "Usage:\n"
            "/removefree @username\n"
            "/removefree 123456789"
        )
        return

    # Yellow Mode toggle (English only)
    if text == "Yellow Mode":
        current = get_user_mode(user_id)
        if current == "mirror_yellow":
            set_user_mode(user_id, "normal")
            await message.reply_text(
                "Yellow Mode OFF.\n"
                "Videos and images will now use normal processing (no yellow filter, no silver border)."
            )
        else:
            set_user_mode(user_id, "mirror_yellow")
            await message.reply_text(
                "Yellow Mode ON ✅\n\n"
                "From now on:\n"
                "• Videos → Strong Yellow filter + Silver Border + all existing features\n"
                "• Images → Silver Border + all existing features\n\n"
                "Press Yellow Mode again to turn it off."
            )
        return

    if not text.startswith("http"):
        await message.reply_text(
            "About this Bot:\n\n"
            "• Just send me any image or video and I will give you a cleaned unique version.\n"
            "• The changes help reduce AI labels and unoriginal content strikes while keeping high visual quality.\n"
            "• You can also send any Instagram, TikTok or Twitter/X link and I will download the media for you in original quality.\n\n"
            "Yellow Mode: Press the button to enable strong yellow + silver border.\n\n"
            "In groups: You must mention me with the photo, video or link.\n"
            "In the channel: just post — bot processes and deletes the original.\n\n"
            "For any help contact @pvnuo",
            reply_markup=get_user_keyboard(username)
        )
        return

    if not is_channel:
        allowed, limit_msg = can_use_bot(user_id, username)
        if not allowed:
            await message.reply_text(limit_msg)
            return

    bot_username = (await context.bot.get_me()).username.lower()
    if not is_allowed_chat(message, bot_username):
        return

    is_admin = bool(username and username.lower() == ADMIN_USERNAME.lower())
    should_delete = is_channel or is_admin

    lower = text.lower()
    if "twitter.com" in lower or "x.com" in lower or "t.co" in lower:
        if not is_channel:
            increment_usage(user_id)
        await download_twitter(update, context, text, is_channel=should_delete)
        return
    if "tiktok.com" in lower or "vt.tiktok.com" in lower or "vm.tiktok.com" in lower:
        if not is_channel:
            increment_usage(user_id)
        await download_tiktok(update, context, text, is_channel=should_delete)
        return
    if "instagram.com" in lower:
        if not is_channel:
            increment_usage(user_id)
        clean_url = re.sub(r"[?&](utm_|igshid|stkn|igsh)=[^&]+", "", text)
        clean_url = clean_url.split("?")[0].rstrip("/")

        text_parts = []
        entities = []
        current_offset = 0

        def add_text(s: str):
            nonlocal current_offset
            text_parts.append(s)
            current_offset += len(s.encode("utf-16-le")) // 2

        def add_custom(emoji_id: str, placeholder: str = "🔥"):
            nonlocal current_offset
            text_parts.append(placeholder)
            length = len(placeholder.encode("utf-16-le")) // 2
            entities.append(MessageEntity(
                type=MessageEntity.CUSTOM_EMOJI,
                offset=current_offset,
                length=length,
                custom_emoji_id=str(emoji_id)
            ))
            current_offset += length

        add_text("Downloading from ")
        add_custom("6201514939530618666")
        add_text(" Instagram...")

        status_text = await message.reply_text("".join(text_parts), entities=entities)
        status_emoji = await message.reply_text("⌛")

        unique_id = str(int(time.time() * 1000))
        paths = []
        try:
            paths = download_instagram(clean_url, unique_id)
            if not paths:
                ydl_opts = {
                    "outtmpl": f"ig_{unique_id}.%(ext)s",
                    "quiet": True,
                    "no_warnings": True,
                    "format": "bv*+ba/b",
                    "merge_output_format": "mp4",
                    "postprocessors": [{
                        "key": "FFmpegVideoConvertor",
                        "preferedformat": "mp4",
                    }],
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(clean_url, download=True)
                    filename = ydl.prepare_filename(info)
                    base = filename.rsplit(".", 1)[0]
                    for ext in [".mp4", ".mkv", ".webm"]:
                        candidate = base + ext
                        if os.path.exists(candidate):
                            paths.append(candidate)
                            break
                    if not paths and os.path.exists(filename):
                        paths = [filename]
            if paths:
                images = [p for p in paths if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
                videos = [p for p in paths if p.lower().endswith((".mp4", ".mkv", ".webm"))]
                if images:
                    media = [InputMediaPhoto(open(p, "rb")) for p in images[:10]]
                    await message.reply_media_group(media=media)
                for v in videos:
                    await safe_send_video(message, v, "✨ Instagram video", unique_id)
                try:
                    await status_text.delete()
                    await status_emoji.delete()
                except Exception:
                    pass
                if is_channel:
                    try:
                        await message.delete()
                    except Exception:
                        pass
            else:
                try:
                    await status_text.edit_text("❌ Failed. Make sure link is public.")
                    await status_emoji.delete()
                except Exception:
                    pass
        except Exception as e:
            print(f"IG error: {e}", flush=True)
            try:
                await status_text.edit_text("❌ Failed to download.")
                await status_emoji.delete()
            except Exception:
                pass
        finally:
            for p in paths:
                try:
                    os.remove(p)
                except Exception:
                    pass
            folder = f"temp_{unique_id}"
            if os.path.exists(folder):
                try:
                    shutil.rmtree(folder, ignore_errors=True)
                except Exception:
                    pass
            for f in os.listdir("."):
                if f.startswith(f"ig_{unique_id}"):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
        return
    await message.reply_text("❌ Only 📸 Instagram, 🎵 TikTok and 𝕏 Twitter/X links are supported.")

# ------------------------------------------------------
# Main
# ------------------------------------------------------
def main():
    init_db()
    keep_alive()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(60.0)
        .read_timeout(120.0)
        .write_timeout(120.0)
        .pool_timeout(60.0)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("how", how))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("freeall", freeall))
    app.add_handler(CommandHandler("removefreeall", removefreeall))
    app.add_handler(CommandHandler("freeuser", freeuser))
    app.add_handler(CommandHandler("removefree", removefree))
    app.add_handler(CommandHandler("status", status))

    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_handler(MessageHandler(filters.Chat(CHANNEL_ID) & (filters.PHOTO | filters.Document.IMAGE), handle_image))
    app.add_handler(MessageHandler(filters.Chat(CHANNEL_ID) & (filters.VIDEO | filters.Document.VIDEO), handle_video))
    app.add_handler(MessageHandler(filters.Chat(CHANNEL_ID) & filters.TEXT & ~filters.COMMAND, handle_text))

    try:
        app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & (filters.PHOTO | filters.Document.IMAGE), handle_image))
        app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & (filters.VIDEO | filters.Document.VIDEO), handle_video))
        app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & filters.TEXT & ~filters.COMMAND, handle_text))
        print("Registered CHANNEL_POST filters", flush=True)
    except Exception as e:
        print(f"UpdateType.CHANNEL_POST not available: {e}", flush=True)

    async def channel_post_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
        post = update.channel_post
        if not post:
            return
        if post.chat.id != CHANNEL_ID:
            return
        if post.photo or (post.document and post.document.mime_type and "image" in (post.document.mime_type or "")):
            await handle_image(update, context)
        elif post.video or (post.document and post.document.mime_type and "video" in (post.document.mime_type or "")):
            await handle_video(update, context)
        elif post.text:
            await handle_text(update, context)

    app.add_handler(TypeHandler(Update, channel_post_router), group=1)

    async def raw_update_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
        kind = "channel_post" if update.channel_post else (
            "message" if update.message else (
            "edited_channel_post" if update.edited_channel_post else "other"
        ))
        chat_id = update.effective_chat.id if update.effective_chat else None
        print(f"[RAW] type={kind} chat_id={chat_id}", flush=True)

    app.add_handler(TypeHandler(Update, raw_update_logger), group=-1)

    print("Bot started successfully...", flush=True)
    print(f"Channel mode active for ID: {CHANNEL_ID}", flush=True)
    print(f"Hardcoded lifetime free: {HARDCODED_LIFETIME_FREE}", flush=True)
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "channel_post", "edited_channel_post"]
    )

if __name__ == "__main__":
    main()
