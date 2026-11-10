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
    5375098801,
    6239058533,
    6758059603,
    7568357675,
    7702852287,
    8039553042,
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
# Image enhance
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

    if mode == "mirror_yellow":
        # Strong golden yellow matching the reels + silver border
        r, g, b = image.split()
        r = ImageEnhance.Brightness(r).enhance(random.uniform(1.18, 1.28))
        g = ImageEnhance.Brightness(g).enhance(random.uniform(1.08, 1.15))
        b = ImageEnhance.Brightness(b).enhance(random.uniform(0.82, 0.90))
        image = Image.merge("RGB", (r, g, b))
        image = ImageEnhance.Contrast(image).enhance(random.uniform(1.06, 1.12))
        image = ImageEnhance.Color(image).enhance(random.uniform(1.05, 1.12))

        # Extra subtle grain
        grain = Image.effect_noise(image.size, random.uniform(6, 12)).convert("L")
        grain = ImageEnhance.Brightness(grain).enhance(random.uniform(0.12, 0.22))
        image = Image.blend(image, grain.convert("RGB"), random.uniform(0.04, 0.08))

        border_size = random.randint(5, 9)
        silver = (192, 192, 192)
        image = ImageOps.expand(image, border=border_size, fill=silver)

    out = io.BytesIO()
    image.save(out, format="JPEG", quality=random.randint(93, 97), optimize=True, progressive=True, subsampling=0)
    out.seek(0)
    return Image.open(out).convert("RGB")

# ------------------------------------------------------
# Video enhance
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

        trim_start = random.uniform(0.40, 0.70)
        trim_end = random.uniform(0.40, 0.70)
        if duration > (trim_start + trim_end + 2.5):
            ss = trim_start
            t = duration - trim_start - trim_end
        else:
            ss = 0.0
            t = max(duration, 0.1)

        speed = random.uniform(0.992, 1.008)
        setpts = 1.0 / speed
        crop_pct = random.uniform(0.012, 0.028)
        target_fps = random.choice([24, 25, 29.97, 30])
        contrast = random.uniform(1.01, 1.035)
        brightness = random.uniform(-0.004, 0.010)
        saturation = random.uniform(0.99, 1.03)
        noise_str = random.randint(2, 5)
        overlay_opacity = random.uniform(0.035, 0.075)

        if mode == "mirror_yellow":
            # Strong golden yellow matching the reels
            warm_rs = random.uniform(0.28, 0.38)
            warm_gs = random.uniform(0.12, 0.18)
            contrast = random.uniform(1.06, 1.12)
            brightness = random.uniform(0.008, 0.018)
            saturation = random.uniform(1.04, 1.10)
            noise_str = random.randint(4, 7)
            border = random.randint(6, 10)
            pad_part = f"pad=iw+{border*2}:ih+{border*2}:{border}:{border}:color=0xC0C0C0,"
        else:
            warm_rs = random.uniform(0.03, 0.07)
            warm_gs = random.uniform(0.01, 0.035)
            pad_part = ""

        vf = (
            f"trim=start={ss}:duration={t},"
            f"setpts=PTS-STARTPTS,"
            f"crop=iw*(1-{crop_pct}):ih*(1-{crop_pct}),"
            f"scale=trunc(iw/2)*2:trunc(ih/2)*2,"
            f"setpts={setpts}*PTS,"
            f"fps={target_fps},"
            f"eq=contrast={contrast}:brightness={brightness}:saturation={saturation},"
            f"colorbalance=rs={warm_rs}:gs={warm_gs}:bs=-0.06,"
            f"unsharp=5:5:0.4:5:5:0.0,"
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
            "-b:a", "160k",
            "-ar", "44100",
            "-ac", "2",
            "-af", f"atempo={speed}",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            "-vf", vf,
            "-max_muxing_queue_size", "9999",
            temp_path
        ]
        result1 = subprocess.run(cmd1, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600, text=True)
        if result1.returncode != 0 or not os.path.exists(temp_path):
            print("===== FFMPEG ERROR (main) =====", flush=True)
            print(result1.stderr[-2000:] if result1.stderr else "No stderr", flush=True)
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
        return _fallback_enhance(input_path, output_path, 0.0, max(duration, 1.0), mode)
    finally:
        for p in [texture_path, temp_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

def _fallback_enhance(input_path: str, output_path: str, ss: float, t: float, mode: str = "normal") -> bool:
    if mode == "mirror_yellow":
        warm = "colorbalance=rs=0.32:gs=0.14:bs=-0.06,"
        border = 7
        pad = f"pad=iw+{border*2}:ih+{border*2}:{border}:{border}:color=0xC0C0C0,"
        eq = "eq=contrast=1.08:brightness=0.012:saturation=1.07,"
        noise = "noise=alls=5:allf=t,"
    else:
        warm = "colorbalance=rs=0.04:gs=0.02:bs=-0.02,"
        pad = ""
        eq = "eq=contrast=1.02:brightness=0.005:saturation=1.015,"
        noise = "noise=alls=3:allf=t,"

    vf = (
        f"trim=start={ss}:duration={t},"
        "setpts=PTS-STARTPTS,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        f"{eq}"
        f"{warm}"
        f"{pad}"
        f"{noise}"
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
        "-crf", "19",
        "-c:a", "aac",
        "-b:a", "160k",
        "-ar", "44100",
        "-ac", "2",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        "-vf", vf,
        "-max_muxing_queue_size", "9999",
        output_path
    ]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=480, text=True)
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
        except Exception:
            if os.path.exists(out):
                try:
                    os.remove(out)
                except Exception:
                    pass
    return current

async def safe_send_video(message, path: str, caption: str, unique_id: str) -> bool:
    try:
        path = compress_video_if_needed(path, unique_id)
        with open(path, "rb") as f:
            await message.reply_video(
                video=f,
                caption=caption,
                supports_streaming=True,
                width=None,
                height=None
            )
        return True
    except Exception as e:
        print(f"safe_send_video error: {e}", flush=True)
        try:
            with open(path, "rb") as f:
                await message.reply_document(document=f, caption=caption)
            return True
        except Exception as e2:
            print(f"safe_send_video document fallback failed: {e2}", flush=True)
            return False

# ------------------------------------------------------
# Keyboard
# ------------------------------------------------------
def get_user_keyboard(username: str = None) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton("How to use"), KeyboardButton("About")],
        [KeyboardButton("Buy"), KeyboardButton("Help")],
        [KeyboardButton("Yellow Mode")],
    ]
    if username and username.lower() == ADMIN_USERNAME.lower():
        buttons.append([KeyboardButton("Status"), KeyboardButton("Free All")])
        buttons.append([KeyboardButton("Remove Free All"), KeyboardButton("Free User")])
        buttons.append([KeyboardButton("Remove Free")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# ------------------------------------------------------
# Commands
# ------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username if user else None
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

    add_custom("5456140674028019486")
    add_text(" Welcome to the Media Cleaner Bot!\n\n")
    add_text("Send me any photo or video and I will return a cleaned unique version.\n")
    add_text("You can also send Instagram, TikTok or Twitter/X links.\n\n")
    add_text("Yellow Mode is available — press the button to enable strong warm yellow + silver border.\n\n")
    add_text("Daily free limit: 5 actions.\n")
    add_text("For unlimited access contact @pvnuo or use /buy")

    await update.message.reply_text(
        "".join(text_parts),
        entities=entities,
        reply_markup=get_user_keyboard(username)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username if user else None
    await update.message.reply_text(
        "Commands & Features:\n\n"
        "• Send any photo or video → cleaned unique version\n"
        "• Send Instagram / TikTok / Twitter link → original quality download\n"
        "• Yellow Mode button → strong warm yellow filter + silver border\n"
        "• In groups: mention the bot with the media/link\n"
        "• Channel mode: auto process + delete original\n\n"
        "Admin only: /freeuser, /removefree, /status, /freeall, /removefreeall\n\n"
        "Need help? Contact @pvnuo",
        reply_markup=get_user_keyboard(username)
    )

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username if user else None
    await update.message.reply_text(
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

async def how(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username if user else None
    await update.message.reply_text(
        "How to use:\n\n"
        "1. Send any photo or video directly → bot returns cleaned unique version\n"
        "2. Send Instagram / TikTok / Twitter link → bot downloads original quality\n"
        "3. Press “Yellow Mode” button to turn ON strong warm yellow filter + silver border\n"
        "   - Videos → Strong golden yellow + silver border + all existing features\n"
        "   - Images → Silver border + all existing features\n"
        "4. Press the same button again to turn Yellow Mode OFF\n\n"
        "Daily free limit is 5 actions (resets every 24 hours).\n"
        "Lifetime users and paid users have unlimited access.\n\n"
        "In groups: mention the bot (@botusername) with the media or link.\n"
        "In the channel: just post the media — bot processes and deletes the original automatically.\n\n"
        "Need unlimited access? Contact @pvnuo or use /buy",
        reply_markup=get_user_keyboard(username)
    )

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username if user else None
    await update.message.reply_text(
        "Unlimited Access\n\n"
        "Contact @pvnuo to buy unlimited access.\n"
        "Payment is done via crypto only.\n\n"
        "After payment you will receive lifetime or time-based unlimited access.",
        reply_markup=get_user_keyboard(username)
    )

async def freeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.username.lower() != ADMIN_USERNAME.lower():
        await update.message.reply_text("Admin only.")
        return
    set_global_free(True)
    await update.message.reply_text("✅ Global free mode enabled. Everyone can use the bot without limit.")

async def removefreeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.username.lower() != ADMIN_USERNAME.lower():
        await update.message.reply_text("Admin only.")
        return
    set_global_free(False)
    await update.message.reply_text("✅ Global free mode disabled.")

async def freeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.username.lower() != ADMIN_USERNAME.lower():
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/freeuser @username\n"
            "/freeuser 123456789\n\n"
            "Note: User must have started the bot at least once."
        )
        return
    arg = context.args[0].lstrip("@")
    target_id = None
    if arg.isdigit():
        target_id = int(arg)
    else:
        await update.message.reply_text("Please use numeric user ID for reliability.\nExample: /freeuser 123456789")
        return
    add_lifetime_free(target_id)
    await update.message.reply_text(f"✅ User {target_id} is now lifetime free.")

async def removefree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.username.lower() != ADMIN_USERNAME.lower():
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/removefree @username\n"
            "/removefree 123456789"
        )
        return
    arg = context.args[0].lstrip("@")
    if not arg.isdigit():
        await update.message.reply_text("Please use numeric user ID.\nExample: /removefree 123456789")
        return
    target_id = int(arg)
    remove_lifetime_free(target_id)
    await update.message.reply_text(f"✅ User {target_id} removed from lifetime free.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.username.lower() != ADMIN_USERNAME.lower():
        await update.message.reply_text("Admin only.")
        return
    free_list = get_lifetime_free_list()
    global_free = is_global_free()
    text = (
        f"Bot Status\n\n"
        f"Global Free: {'ON' if global_free else 'OFF'}\n"
        f"Lifetime Free Users: {len(free_list)}\n"
        f"Hardcoded: {HARDCODED_LIFETIME_FREE}\n\n"
        f"List:\n" + "\n".join(str(x) for x in free_list[:50])
    )
    if len(free_list) > 50:
        text += f"\n... and {len(free_list)-50} more"
    await update.message.reply_text(text)

# ------------------------------------------------------
# Handlers
# ------------------------------------------------------
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message or update.channel_post or update.message
    if not message:
        return
    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username if user else None
    is_channel = message.chat and message.chat.id == CHANNEL_ID

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

    lock = get_user_lock(user_id)
    async with lock:
        try:
            photo = message.photo[-1] if message.photo else None
            document = message.document if message.document and message.document.mime_type and "image" in message.document.mime_type else None
            file = await context.bot.get_file(photo.file_id if photo else document.file_id)
            file_bytes = await file.download_as_bytearray()
            image = Image.open(io.BytesIO(file_bytes))
            mode = get_user_mode(user_id)
            enhanced = enhance_image(image, mode=mode)
            out = io.BytesIO()
            enhanced.save(out, format="JPEG", quality=95)
            out.seek(0)
            await message.reply_photo(photo=out, caption="✅ Cleaned unique version")
            if not is_channel:
                increment_usage(user_id)
            if should_delete:
                try:
                    await message.delete()
                except Exception:
                    pass
            else:
                try:
                    await message.delete()
                except Exception:
                    pass
        except Exception as e:
            print(f"handle_image error: {e}", flush=True)
            await message.reply_text("❌ Failed to process the image.")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message or update.channel_post or update.message
    if not message:
        return
    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username if user else None
    is_channel = message.chat and message.chat.id == CHANNEL_ID

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

    lock = get_user_lock(user_id)
    async with lock:
        unique_id = str(int(time.time() * 1000))
        input_path = f"in_{unique_id}.mp4"
        output_path = f"out_{unique_id}.mp4"
        try:
            video = message.video
            document = message.document if message.document and message.document.mime_type and "video" in (message.document.mime_type or "") else None
            file = await context.bot.get_file(video.file_id if video else document.file_id)
            await file.download_to_drive(input_path)
            mode = get_user_mode(user_id)
            ok = enhance_video(input_path, output_path, mode=mode)
            if ok and os.path.exists(output_path):
                caption = "✅ Cleaned unique version"
                if mode == "mirror_yellow":
                    caption = "✅ Cleaned unique version (Yellow Mode)"
                success = await safe_send_video(message, output_path, caption, unique_id)
                if success:
                    if not is_channel:
                        increment_usage(user_id)
                    if should_delete:
                        try:
                            await message.delete()
                        except Exception:
                            pass
                    else:
                        try:
                            await message.delete()
                        except Exception:
                            pass
                else:
                    await message.reply_text("❌ Failed to send the processed video.")
            else:
                await message.reply_text("❌ Failed to process the video.")
        except Exception as e:
            print(f"handle_video error: {e}", flush=True)
            await message.reply_text("❌ Failed to process the video.")
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
def download_instagram(url: str, unique_id: str) -> list:
    paths = []
    folder = f"temp_{unique_id}"
    os.makedirs(folder, exist_ok=True)
    try:
        L = instaloader.Instaloader(
            dirname_pattern=folder,
            filename_pattern="{shortcode}",
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            quiet=True,
        )
        shortcode = None
        if "/p/" in url or "/reel/" in url or "/tv/" in url:
            parts = url.rstrip("/").split("/")
            for i, p in enumerate(parts):
                if p in ("p", "reel", "tv") and i + 1 < len(parts):
                    shortcode = parts[i + 1]
                    break
        if shortcode:
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            L.download_post(post, target=folder)
        else:
            L.download_profile(url.split("/")[-1], profile_pic=False)
        for root, _, files in os.walk(folder):
            for f in files:
                full = os.path.join(root, f)
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mkv", ".webm")):
                    paths.append(full)
    except Exception as e:
        print(f"instaloader error: {e}", flush=True)
    return paths

# ------------------------------------------------------
# TikTok Download
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
# Twitter / X Download
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
                "• Videos → Strong golden Yellow filter + Silver Border + all existing features\n"
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
