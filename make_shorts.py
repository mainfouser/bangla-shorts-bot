import os
import re
import json
import time
import random
import asyncio
import textwrap
import subprocess
from pathlib import Path
from datetime import datetime

import requests
import edge_tts
import gspread
from google.oauth2.service_account import Credentials
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from playwright.sync_api import sync_playwright

# ==============================================================================
# ১. ভয়েস ও ব্র্যান্ডিং কনফিগারেশন (এখান থেকে সহজেই পরিবর্তন করতে পারবেন)
# ==============================================================================
# স্মার্ট পুরুষ কণ্ঠ: "bn-BD-PradeepNeural" অথবা "bn-IN-BashkarNeural"
# নারী কণ্ঠ (প্রয়োজনে): "bn-BD-NabanitaNeural" অথবা "bn-IN-TanishaaNeural"
VOICE_NAME = "bn-BD-PradeepNeural"
VOICE_RATE = "+12%"   # কথা বলার গতি (+10% থেকে +15% স্মার্ট ও প্রাণবন্ত শোনায়)
VOICE_PITCH = "-4Hz"  # কণ্ঠস্বর একটু গম্ভীর ও ভরাট করতে (-3Hz থেকে -5Hz)

SITE_DOMAIN = "homebdinfo.com"
SITE_URL_DEFAULT = "https://www.homebdinfo.com"

# ==============================================================================
# ২. এনভায়রনমেন্ট ও পাথ সেটআপ
# ==============================================================================
SHEET_ID = os.environ.get("SHEET_ID", "").strip()
GOOGLE_CREDENTIALS_RAW = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot").strip()

SHEET_NAME = "Shorts_Queue"
WIDTH = 1080
HEIGHT = 1920
FPS = 30

WORK_DIR = Path("temp_shorts")
WORK_DIR.mkdir(parents=True, exist_ok=True)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansBengali-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def get_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


# ==============================================================================
# ৩. Google Sheet কানেকশন
# ==============================================================================
def connect_sheet():
    if not GOOGLE_CREDENTIALS_RAW or not SHEET_ID:
        raise RuntimeError("GOOGLE_CREDENTIALS অথবা SHEET_ID সেট করা নেই।")
    creds_dict = json.loads(GOOGLE_CREDENTIALS_RAW)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SHEET_ID)
    return spreadsheet.worksheet(SHEET_NAME)


def get_header_map(sheet) -> dict:
    headers = sheet.row_values(1)
    return {str(h).strip(): idx + 1 for idx, h in enumerate(headers) if str(h).strip()}


def update_cells(sheet, row_idx: int, header_map: dict, updates: dict):
    for col_name, val in updates.items():
        if col_name in header_map:
            sheet.update_cell(row_idx, header_map[col_name], str(val))


# ==============================================================================
# ৪. বাংলা ভয়েসওভার ও টাইমিং তৈরি (edge-tts)
# ==============================================================================
def split_script_into_chunks(script_text: str) -> list:
    cleaned = re.sub(r"\s+", " ", str(script_text or "")).strip()
    if not cleaned:
        return []
    pattern = re.compile(r"(?<=[।!?\.])\s+")
    raw_sentences = [s.strip() for s in pattern.split(cleaned) if s.strip()]
    chunks = []
    for sentence in raw_sentences:
        words = sentence.split()
        if len(words) <= 9:
            chunks.append(sentence)
        else:
            for i in range(0, len(words), 8):
                part = " ".join(words[i : i + 8]).strip()
                if part:
                    chunks.append(part)
    return chunks


async def generate_tts_with_timings(chunks: list, output_audio: Path) -> list:
    full_text = " ".join(chunks)
    communicate = edge_tts.Communicate(
        text=full_text,
        voice=VOICE_NAME,
        rate=VOICE_RATE,
        pitch=VOICE_PITCH,
    )
    word_boundaries = []
    with open(output_audio, "wb") as f:
        async for msg in communicate.stream():
            if msg["type"] == "audio":
                f.write(msg["data"])
            elif msg["type"] == "WordBoundary":
                start_sec = msg["offset"] / 10_000_000.0
                dur_sec = msg["duration"] / 10_000_000.0
                word_boundaries.append((start_sec, start_sec + dur_sec))

    total_duration = get_media_duration(output_audio)
    if total_duration <= 0:
        total_duration = max(10.0, len(full_text.split()) * 0.42)

    # শব্দ সংখ্যার অনুপাতে প্রতিটি সাবটাইটেল চাঙ্কের নিখুঁত সময় নির্ধারণ
    total_words = sum(max(1, len(c.split())) for c in chunks)
    timed_chunks = []
    cursor = 0.0
    for idx, chunk in enumerate(chunks):
        w_count = max(1, len(chunk.split()))
        if idx == len(chunks) - 1:
            end_time = total_duration
        else:
            share = (w_count / total_words) * total_duration
            end_time = min(total_duration, cursor + share)
        timed_chunks.append((cursor, end_time, chunk))
        cursor = end_time
    return timed_chunks


def get_media_duration(file_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(file_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(res.stdout.strip())
    except Exception:
        return 0.0


# ==============================================================================
# ৫. ওয়েবসাইটের লাইভ মোবাইল স্ক্রিনশট ক্যাপচার (Playwright)
# ==============================================================================
def capture_website_screenshot(post_url: str, out_img: Path) -> Path:
    target_url = post_url.strip() if post_url and post_url.startswith("http") else SITE_URL_DEFAULT
    print(f"[INFO] ওয়েবসাইট স্ক্রিনশট নেওয়া হচ্ছে: {target_url}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 540, "height": 960},
                device_scale_factor=2,
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Mobile Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(target_url, wait_until="networkidle", timeout=35000)
            page.wait_for_timeout(1500)
            # পপআপ বা কুকি ব্যানার থাকলে সরানোর হালকা চেষ্টা
            page.evaluate(
                """() => {
                    document.querySelectorAll('[class*="cookie"], [id*="cookie"], [class*="popup"]').forEach(el => el.remove());
                }"""
            )
            page.screenshot(path=str(out_img), full_page=True)
            browser.close()
    except Exception as e:
        print(f"[WARN] ওয়েবসাইট স্ক্রিনশট ব্যর্থ ({e}), ফলব্যাক ব্যাকগ্রাউন্ড তৈরি হচ্ছে।")
        create_fallback_site_bg(out_img, target_url)

    # স্ক্রিনশট প্রসেস: প্রস্থ ১০৮০ পিক্সেল এবং উচ্চতা কমপক্ষে ২৬০০ পিক্সেল রাখা যাতে সুন্দরভাবে স্ক্রল হয়
    img = Image.open(out_img).convert("RGB")
    ratio = WIDTH / float(img.width)
    new_h = int(img.height * ratio)
    img = img.resize((WIDTH, max(HEIGHT + 600, min(new_h, 3600))), Image.Resampling.LANCZOS)
    img.save(out_img, quality=92)
    return out_img


def create_fallback_site_bg(out_img: Path, url_text: str):
    img = Image.new("RGB", (WIDTH, 2600), (15, 23, 42))
    draw = ImageDraw.Draw(img)
    for y in range(0, 2600, 120):
        draw.rectangle([60, y + 40, WIDTH - 60, y + 110], fill=(30, 41, 59))
    font = get_font(44)
    draw.text((100, 200), f"Visit: {url_text}", fill=(248, 250, 252), font=font)
    img.save(out_img)


# ==============================================================================
# ৬. Pixabay থেকে প্রাসঙ্গিক স্টক ভিডিও সংগ্রহ
# ==============================================================================
def fetch_pixabay_clips(keywords_str: str, max_clips: int = 3) -> list:
    if not PIXABAY_API_KEY:
        return []
    raw_kws = [k.strip() for k in str(keywords_str or "").split(",") if k.strip()]
    if not raw_kws:
        raw_kws = ["information technology", "mobile smartphone"]

    downloaded = []
    for idx, kw in enumerate(raw_kws[:max_clips]):
        try:
            params = {
                "key": PIXABAY_API_KEY,
                "q": kw,
                "video_type": "film",
                "per_page": 5,
                "safesearch": "true",
            }
            resp = requests.get("https://pixabay.com/api/videos/", params=params, timeout=20)
            data = resp.json()
            hits = data.get("hits", [])
            if not hits:
                continue
            random.shuffle(hits)
            chosen = hits[0]
            videos = chosen.get("videos", {})
            video_url = (
                videos.get("medium", {}).get("url")
                or videos.get("small", {}).get("url")
                or videos.get("large", {}).get("url")
            )
            if not video_url:
                continue
            clip_path = WORK_DIR / f"stock_{idx}.mp4"
            with requests.get(video_url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(clip_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            if clip_path.exists() and clip_path.stat().st_size > 10000:
                downloaded.append(clip_path)
        except Exception as e:
            print(f"[WARN] Pixabay ক্লিপ ডাউনলোড ত্রুটি ({kw}): {e}")
    return downloaded


# ==============================================================================
# ৭. টপ ব্র্যান্ডিং ব্যানার, সাবটাইটেল ও CTA ওভারলে ইমেজ তৈরি
# ==============================================================================
def create_top_banner_overlay(post_title: str, out_path: Path):
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # উপরের দিকে সাইট প্রমোশন পিল ব্যানার
    pill_box = [70, 75, WIDTH - 70, 175]
    draw.rounded_rectangle(pill_box, radius=45, fill=(15, 23, 42, 235), outline=(56, 189, 248, 255), width=4)

    font_site = get_font(36)
    banner_text = f"বিস্তারিত পড়ুন: {SITE_DOMAIN}"
    bbox = draw.textbbox((0, 0), banner_text, font=font_site)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        ((WIDTH - tw) // 2, 75 + (100 - th) // 2 - 4),
        banner_text,
        fill=(250, 204, 21, 255),
        font=font_site,
    )

    # পোস্টের সংক্ষিপ্ত শিরোনাম ব্যাজ (স্টক কার্ডের ঠিক উপরে)
    short_title = (post_title[:52] + "...") if len(post_title) > 55 else post_title
    if short_title:
        title_box = [70, 200, WIDTH - 70, 290]
        draw.rounded_rectangle(title_box, radius=24, fill=(30, 41, 59, 220))
        font_title = get_font(32)
        tb = draw.textbbox((0, 0), short_title, font=font_title)
        ttw = tb[2] - tb[0]
        tth = tb[3] - tb[1]
        draw.text(
            ((WIDTH - ttw) // 2, 200 + (90 - tth) // 2 - 4),
            short_title,
            fill=(255, 255, 255, 255),
            font=font_title,
        )

    canvas.save(out_path)


def create_cta_overlay(out_path: Path):
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # শেষ ৫ সেকেন্ডের জন্য ভিজিট CTA কার্ড
    box = [80, 960, WIDTH - 80, 1170]
    draw.rounded_rectangle(box, radius=32, fill=(220, 38, 38, 240), outline=(255, 255, 255, 255), width=4)

    font_big = get_font(42)
    font_small = get_font(34)

    line1 = "সম্পূর্ণ তথ্য ও আবেদন লিংক পেতে"
    line2 = f"Google-এ সার্চ করুন: {SITE_DOMAIN}"

    b1 = draw.textbbox((0, 0), line1, font=font_small)
    b2 = draw.textbbox((0, 0), line2, font=font_big)

    draw.text(((WIDTH - (b1[2] - b1[0])) // 2, 995), line1, fill=(255, 255, 255, 255), font=font_small)
    draw.text(((WIDTH - (b2[2] - b2[0])) // 2, 1065), line2, fill=(254, 240, 138, 255), font=font_big)

    canvas.save(out_path)


def create_subtitle_image(text: str, out_path: Path):
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    font = get_font(46)

    wrapped_lines = textwrap.wrap(text, width=28)
    if not wrapped_lines:
        canvas.save(out_path)
        return

    line_heights = []
    max_w = 0
    for line in wrapped_lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        max_w = max(max_w, w)
        line_heights.append(h + 16)

    total_h = sum(line_heights) + 44
    box_w = min(WIDTH - 80, max_w + 80)
    x1 = (WIDTH - box_w) // 2
    y2 = HEIGHT - 230
    y1 = y2 - total_h
    x2 = x1 + box_w

    draw.rounded_rectangle([x1, y1, x2, y2], radius=28, fill=(0, 0, 0, 210), outline=(255, 255, 255, 90), width=2)

    curr_y = y1 + 22
    for idx, line in enumerate(wrapped_lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        lx = (WIDTH - lw) // 2
        draw.text((lx, curr_y), line, fill=(255, 255, 255, 255), font=font)
        curr_y += line_heights[idx]

    canvas.save(out_path)


# ==============================================================================
# ৮. হাইব্রিড ভিডিও রেন্ডারিং (সাইট স্ক্রল ব্যাকগ্রাউন্ড + কার্ড ফুটেজ + অডিও)
# ==============================================================================
def render_hybrid_video(
    site_screenshot: Path,
    stock_clips: list,
    voice_audio: Path,
    timed_chunks: list,
    post_title: str,
    final_output: Path,
):
    duration = get_media_duration(voice_audio) + 0.6
    if duration < 5.0:
        duration = 15.0

    # ১) ব্যাকগ্রাউন্ড: ওয়েবসাইটের স্ক্রিনশট ধীরে ধীরে উপর থেকে নিচে স্ক্রল করবে
    bg_video = WORK_DIR / "bg_scroll.mp4"
    site_img = Image.open(site_screenshot)
    img_h = site_img.height
    scroll_dist = max(0, img_h - HEIGHT)

    scroll_filter = (
        f"scale={WIDTH}:{img_h},"
        f"crop={WIDTH}:{HEIGHT}:0:'min({scroll_dist}, ({scroll_dist})*(t/{duration:.2f}))',"
        f"eq=brightness=-0.06:saturation=1.05"
    )
    cmd_bg = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(site_screenshot),
        "-vf",
        scroll_filter,
        "-t",
        f"{duration:.2f}",
        "-r",
        str(FPS),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(bg_video),
    ]
    subprocess.run(cmd_bg, check=True)

    # ২) মাঝখানের ফ্রেম করা স্টক ভিডিও (যদি Pixabay ক্লিপ পাওয়া যায়)
    card_video = WORK_DIR / "card_clips.mp4"
    has_stock_card = False
    if stock_clips:
        concat_list = WORK_DIR / "clips.txt"
        norm_clips = []
        per_clip_dur = max(3.5, duration / len(stock_clips))
        for i, clip in enumerate(stock_clips):
            norm_p = WORK_DIR / f"norm_{i}.mp4"
            # কার্ডের সাইজ: 920x540 (রাউন্ডেড ফিলের জন্য)
            vf_card = (
                "scale=920:540:force_original_aspect_ratio=increase,"
                "crop=920:540,setsar=1"
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-stream_loop",
                    "-1",
                    "-i",
                    str(clip),
                    "-t",
                    f"{per_clip_dur:.2f}",
                    "-vf",
                    vf_card,
                    "-r",
                    str(FPS),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(norm_p),
                ],
                check=True,
            )
            norm_clips.append(norm_p)

        with open(concat_list, "w", encoding="utf-8") as f:
            for p in norm_clips:
                f.write(f"file '{p.resolve()}'\n")

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-t",
                f"{duration:.2f}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(card_video),
            ],
            check=True,
        )
        has_stock_card = True

    # ৩) ওভারলে ইমেজ তৈরি (টপ ব্যানার, CTA, সাবটাইটেল)
    top_banner_png = WORK_DIR / "top_banner.png"
    cta_png = WORK_DIR / "cta_banner.png"
    create_top_banner_overlay(post_title, top_banner_png)
    create_cta_overlay(cta_png)

    sub_files = []
    for idx, (st, et, chunk_txt) in enumerate(timed_chunks):
        sp = WORK_DIR / f"sub_{idx}.png"
        create_subtitle_image(chunk_txt, sp)
        sub_files.append((st, et, sp))

    # ৪) সব লেয়ার একত্রিত করা (FFmpeg Filter Complex)
    inputs = ["-i", str(bg_video)]
    input_idx = 1

    if has_stock_card:
        inputs.extend(["-i", str(card_video)])
        card_idx = input_idx
        input_idx += 1

    inputs.extend(["-i", str(top_banner_png)])
    banner_idx = input_idx
    input_idx += 1

    inputs.extend(["-i", str(cta_png)])
    cta_idx = input_idx
    input_idx += 1

    has_logo = os.path.exists("logo.png")
    if has_logo:
        inputs.extend(["-i", "logo.png"])
        logo_idx = input_idx
        input_idx += 1

    sub_indices = []
    for st, et, sp in sub_files:
        inputs.extend(["-i", str(sp)])
        sub_indices.append((st, et, input_idx))
        input_idx += 1

    # অডিও ইনপুট
    inputs.extend(["-i", str(voice_audio)])
    voice_idx = input_idx
    input_idx += 1

    has_bgm = os.path.exists("bgm.mp3")
    if has_bgm:
        inputs.extend(["-stream_loop", "-1", "-i", "bgm.mp3"])
        bgm_idx = input_idx
        input_idx += 1

    filters = []
    last_v = "[0:v]"

    # মাঝখানে স্টক ভিডিও কার্ড বর্ডারসহ বসানো (y=325 পজিশনে)
    if has_stock_card:
        filters.append(f"[{card_idx}:v]pad=936:556:8:8:color=0x38BDF8[framed_card]")
        filters.append(f"{last_v}[framed_card]overlay=(W-w)/2:325:shortest=1[v_card]")
        last_v = "[v_card]"

    # টপ ব্র্যান্ডিং ব্যানার ওভারলে
    filters.append(f"{last_v}[{banner_idx}:v]overlay=0:0[v_banner]")
    last_v = "[v_banner]"

    # লোগো ওয়াটারমার্ক (নিচে ডান কোণায় বা ব্যানারের নিচে)
    if has_logo:
        filters.append(f"[{logo_idx}:v]scale=110:-1[lg]")
        filters.append(f"{last_v}[lg]overlay=W-w-45:H-h-45[v_logo]")
        last_v = "[v_logo]"

    # সাবটাইটেল ওভারলে
    for i, (st, et, s_idx) in enumerate(sub_indices):
        out_label = f"[v_sub_{i}]"
        filters.append(
            f"{last_v}[{s_idx}:v]overlay=0:0:enable='between(t,{st:.2f},{et:.2f})'{out_label}"
        )
        last_v = out_label

    # শেষ ৫ সেকেন্ডে CTA ব্যানার ওভারলে
    cta_start = max(0.0, duration - 5.0)
    filters.append(
        f"{last_v}[{cta_idx}:v]overlay=0:0:enable='between(t,{cta_start:.2f},{duration:.2f})'[v_final]"
    )

    # অডিও মিক্সিং (ভয়েস + ১২% ভলিউমে BGM)
    if has_bgm:
        filters.append(f"[{bgm_idx}:a]volume=0.12[bgm_low]")
        filters.append(
            f"[{voice_idx}:a][bgm_low]amix=inputs=2:duration=first:dropout_transition=2[a_final]"
        )
    else:
        filters.append(f"[{voice_idx}:a]anull[a_final]")

    cmd_render = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[v_final]",
        "-map",
        "[a_final]",
        "-t",
        f"{duration:.2f}",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "22",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-pix_fmt",
        "yuv420p",
        str(final_output),
    ]
    subprocess.run(cmd_render, check=True)


# ==============================================================================
# ৯. GitHub Releases-এ ভিডিও আপলোড এবং Telegram-এ পাঠানো
# ==============================================================================
def upload_to_github_release(video_path: Path, row_idx: int, title: str) -> tuple:
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN পাওয়া যায়নি।")

    tag_name = f"short-row-{row_idx}-{int(time.time())}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    release_payload = {
        "tag_name": tag_name,
        "name": f"Shorts Row {row_idx}: {title[:60]}",
        "body": f"Auto-generated YouTube Short for row {row_idx}",
        "draft": False,
        "prerelease": False,
    }
    rel_resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases",
        headers=headers,
        json=release_payload,
        timeout=30,
    )
    rel_resp.raise_for_status()
    rel_data = rel_resp.json()
    release_id = str(rel_data["id"])
    upload_url_template = rel_data["upload_url"].split("{")[0]

    asset_name = video_path.name
    upload_headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "video/mp4",
    }
    with open(video_path, "rb") as vf:
        asset_resp = requests.post(
            f"{upload_url_template}?name={asset_name}",
            headers=upload_headers,
            data=vf,
            timeout=120,
        )
    asset_resp.raise_for_status()
    download_url = asset_resp.json()["browser_download_url"]
    return release_id, download_url


def send_video_to_telegram(
    video_path: Path, row_idx: int, shorts_title: str, post_url: str, download_url: str
) -> tuple:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN অথবা TELEGRAM_CHAT_ID পাওয়া যায়নি।")

    caption = (
        f"🎬 <b>নতুন YouTube Short প্রস্তুত!</b>\n\n"
        f"📌 <b>Title:</b> {shorts_title}\n"
        f"🔗 <b>Post:</b> {post_url}\n"
        f"📊 <b>Sheet Row:</b> {row_idx}\n"
        f"⬇️ <a href=\"{download_url}\">Direct Download</a>"
    )
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"APPROVE_{row_idx}"},
                {"text": "❌ Reject", "callback_data": f"REJECT_{row_idx}"},
            ]
        ]
    }
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    with open(video_path, "rb") as vf:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(reply_markup),
            },
            files={"video": (video_path.name, vf, "video/mp4")},
            timeout=120,
        )
    res_json = resp.json()
    if not res_json.get("ok"):
        raise RuntimeError(f"Telegram sendVideo ব্যর্থ: {res_json}")

    msg = res_json["result"]
    tg_msg_id = str(msg["message_id"])
    tg_file_id = str(msg.get("video", {}).get("file_id", ""))
    return tg_file_id, tg_msg_id


# ==============================================================================
# ১০. মূল প্রসেসিং লুপ
# ==============================================================================
def main():
    sheet = connect_sheet()
    header_map = get_header_map(sheet)
    all_rows = sheet.get_all_values()

    if len(all_rows) < 2:
        print("[INFO] শিটে কোনো ডেটা নেই।")
        return

    status_col = header_map.get("Status")
    if not status_col:
        raise RuntimeError("শিটে 'Status' কলাম পাওয়া যায়নি।")

    target_row_idx = None
    row_data = None
    for idx in range(2, len(all_rows) + 1):
        row = all_rows[idx - 1]
        st_val = row[status_col - 1].strip() if len(row) >= status_col else ""
        if st_val == "SCRIPT_READY":
            target_row_idx = idx
            row_data = row
            break

    if not target_row_idx:
        print("[INFO] প্রসেস করার মতো কোনো SCRIPT_READY রো পাওয়া যায়নি।")
        return

    def get_col(name: str) -> str:
        c_idx = header_map.get(name)
        if not c_idx or len(row_data) < c_idx:
            return ""
        return str(row_data[c_idx - 1]).strip()

    post_title = get_col("Post Title")
    post_url = get_col("Post URL")
    shorts_title = get_col("Shorts Title (YT)") or post_title or "Bangla Info Short"
    script_text = get_col("Bangla Voiceover Script")
    keywords_str = get_col("Image Search Keywords (English)")

    print(f"[INFO] Row {target_row_idx} প্রসেসিং শুরু হচ্ছে: {shorts_title}")
    update_cells(
        sheet,
        target_row_idx,
        header_map,
        {"Status": "PROCESSING", "Video Status": "GENERATING", "Last Error": ""},
    )

    try:
        if not script_text:
            raise ValueError("Bangla Voiceover Script খালি রয়েছে।")

        # ১) স্ক্রিপ্ট চাঙ্ক ও ভয়েসওভার তৈরি
        chunks = split_script_into_chunks(script_text)
        voice_path = WORK_DIR / f"voice_{target_row_idx}.mp3"
        timed_chunks = asyncio.run(generate_tts_with_timings(chunks, voice_path))

        # ২) ওয়েবসাইট স্ক্রিনশট ও Pixabay স্টক ক্লিপ সংগ্রহ
        site_shot_path = WORK_DIR / f"site_{target_row_idx}.png"
        capture_website_screenshot(post_url, site_shot_path)
        stock_clips = fetch_pixabay_clips(keywords_str, max_clips=3)

        # ৩) হাইব্রিড ভিডিও রেন্ডার
        final_video = WORK_DIR / f"short_row_{target_row_idx}.mp4"
        render_hybrid_video(
            site_screenshot=site_shot_path,
            stock_clips=stock_clips,
            voice_audio=voice_path,
            timed_chunks=timed_chunks,
            post_title=post_title or shorts_title,
            final_output=final_video,
        )

        # ৪) GitHub Release-এ আপলোড এবং Telegram-এ প্রেরণ
        release_id, download_url = upload_to_github_release(
            final_video, target_row_idx, shorts_title
        )
        tg_file_id, tg_msg_id = send_video_to_telegram(
            final_video, target_row_idx, shorts_title, post_url, download_url
        )

        update_cells(
            sheet,
            target_row_idx,
            header_map,
            {
                "Status": "PENDING_APPROVAL",
                "Video Status": "READY",
                "Drive File ID": release_id,
                "Drive File URL": download_url,
                "Telegram Video File ID": tg_file_id,
                "Telegram Message ID": tg_msg_id,
                "Last Error": "",
            },
        )
        print(f"[SUCCESS] Row {target_row_idx} সফলভাবে সম্পন্ন হয়েছে!")

    except Exception as exc:
        err_msg = str(exc)[:900]
        print(f"[ERROR] Row {target_row_idx} ব্যর্থ: {err_msg}")
        update_cells(
            sheet,
            target_row_idx,
            header_map,
            {
                "Status": "ERROR",
                "Video Status": "FAILED",
                "Last Error": err_msg,
            },
        )
        raise


if __name__ == "__main__":
    main()
