import os
import re
import json
import time
import shutil
import asyncio
import textwrap
import subprocess
import urllib.parse
from pathlib import Path

import requests
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

# ============================================================
# ১. ভয়েস ও ডিজাইন কনফিগারেশন (সহজে পরিবর্তনের জন্য)
# ============================================================
# স্মার্ট পুরুষ কণ্ঠ (প্রস্তাবিত): "bn-BD-PradeepNeural"
# বিকল্প পুরুষ কণ্ঠ: "bn-IN-BashkarNeural"
# নারী কণ্ঠ: "bn-BD-NabanitaNeural" বা "bn-IN-TanishaaNeural"
VOICE_NAME = "bn-BD-PradeepNeural"
BASE_RATE = "+9%"
BASE_PITCH = "-3Hz"

# ভিডিও ও মিউজিক সেটিংস
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 30
BGM_VOLUME = 0.17  # ১৭% ব্যাকগ্রাউন্ড মিউজিক ভলিউম
MAX_SCROLL_PX = 2600  # ধীরগতির স্মুথ স্ক্রোলিংয়ের জন্য সর্বোচ্চ স্ক্রল দূরত্ব

# ব্র্যান্ডিং সেটিংস
SITE_DISPLAY_URL = "homebdinfo.com"
TOP_BADGE_TEXT = f"বিস্তারিত তথ্য ও আবেদন: {SITE_DISPLAY_URL}"
CTA_TITLE_TEXT = "সম্পূর্ণ তথ্য ও সরাসরি আবেদন লিংক পেতে"
CTA_SUBTITLE_TEXT = f"আজই Google-এ সার্চ করুন: {SITE_DISPLAY_URL}"

# ফোল্ডার ও ফাইল পাথ
WORK_DIR = Path("temp_work")
BGM_FILE = Path("bgm.mp3")
LOGO_FILE = Path("logo.png")
SHEET_NAME = "Shorts_Queue"

GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GOOGLE_CREDENTIALS_RAW = os.environ.get("GOOGLE_CREDENTIALS", "")


# ============================================================
# ২. ফন্ট ও টেক্সট রেন্ডারিং হেল্পার (বাংলা + ইংরেজি মিক্সড সাপোর্ট)
# ============================================================
def find_fonts():
    bn_candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansBengali-Bold.ttf",
    ]
    en_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ]
    bn_font = next((p for p in bn_candidates if os.path.exists(p)), None)
    en_font = next((p for p in en_candidates if os.path.exists(p)), bn_font)
    return bn_font, en_font


BN_FONT_PATH, EN_FONT_PATH = find_fonts()


def load_font(path, size):
    try:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    except Exception:
        pass
    return ImageFont.load_default()


def split_by_script(text):
    """বাংলা ও ইংরেজি অংশ আলাদা করে সঠিক ফন্টে রেন্ডার করার জন্য ভাগ করে"""
    tokens = re.findall(r"[A-Za-z0-9._:/-]+|[^A-Za-z0-9._:/-]+", text)
    result = []
    for tok in tokens:
        is_ascii = bool(re.match(r"^[A-Za-z0-9._:/-]+$", tok))
        result.append((tok, is_ascii))
    return result


def measure_mixed_text(draw, text, size):
    bn_f = load_font(BN_FONT_PATH, size)
    en_f = load_font(EN_FONT_PATH, int(size * 0.92))
    total_w = 0
    max_h = size
    for tok, is_ascii in split_by_script(text):
        font = en_f if is_ascii else bn_f
        bbox = draw.textbbox((0, 0), tok, font=font)
        total_w += (bbox[2] - bbox[0])
        max_h = max(max_h, bbox[3] - bbox[1])
    return total_w, max_h


def draw_mixed_text(draw, xy, text, size, fill=(255, 255, 255, 255)):
    x, y = xy
    bn_f = load_font(BN_FONT_PATH, size)
    en_f = load_font(EN_FONT_PATH, int(size * 0.92))
    for tok, is_ascii in split_by_script(text):
        font = en_f if is_ascii else bn_f
        y_offset = int(size * 0.06) if is_ascii else 0
        draw.text((x, y + y_offset), tok, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), tok, font=font)
        x += (bbox[2] - bbox[0])


def create_banner_png(out_path, text, width=960, height=96, font_size=34,
                      bg_color=(15, 23, 42, 235), border_color=(56, 189, 248, 255),
                      text_color=(255, 255, 255, 255)):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [4, 4, width - 4, height - 4],
        radius=24,
        fill=bg_color,
        outline=border_color,
        width=4
    )
    tw, th = measure_mixed_text(draw, text, font_size)
    tx = max(24, (width - tw) // 2)
    ty = max(12, (height - th) // 2 - 4)
    draw_mixed_text(draw, (tx, ty), text, font_size, fill=text_color)
    img.save(out_path, "PNG")


def create_title_card_png(out_path, title_text, width=960, height=220):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [6, 6, width - 6, height - 6],
        radius=28,
        fill=(10, 15, 30, 230),
        outline=(250, 204, 21, 255),
        width=4
    )
    tag = "গুরুত্বপূর্ণ আপডেট • homebdinfo.com"
    draw_mixed_text(draw, (36, 22), tag, 26, fill=(250, 204, 21, 255))

    wrapped = textwrap.wrap(title_text, width=34)[:3]
    y_cursor = 68
    for line in wrapped:
        draw_mixed_text(draw, (36, y_cursor), line, 36, fill=(255, 255, 255, 255))
        y_cursor += 46
    img.save(out_path, "PNG")


def create_cta_card_png(out_path, width=980, height=230):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [6, 6, width - 6, height - 6],
        radius=30,
        fill=(17, 24, 39, 245),
        outline=(34, 197, 94, 255),
        width=5
    )
    tw1, _ = measure_mixed_text(draw, CTA_TITLE_TEXT, 34)
    draw_mixed_text(draw, ((width - tw1) // 2, 34), CTA_TITLE_TEXT, 34, fill=(255, 255, 255, 255))

    tw2, _ = measure_mixed_text(draw, CTA_SUBTITLE_TEXT, 38)
    draw_mixed_text(draw, ((width - tw2) // 2, 105), CTA_SUBTITLE_TEXT, 38, fill=(74, 222, 128, 255))
    img.save(out_path, "PNG")


# ============================================================
# ৩. গুগল শিট কানেকশন
# ============================================================
def get_google_sheet():
    if not GOOGLE_CREDENTIALS_RAW or not SHEET_ID:
        raise ValueError("GOOGLE_CREDENTIALS অথবা SHEET_ID সেট করা নেই।")
    creds_dict = json.loads(GOOGLE_CREDENTIALS_RAW)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)


def get_header_map(sheet):
    headers = sheet.row_values(1)
    return {h.strip(): idx + 1 for idx, h in enumerate(headers) if h.strip()}


def update_row_cells(sheet, row_idx, header_map, updates):
    for col_name, val in updates.items():
        if col_name in header_map:
            sheet.update_cell(row_idx, header_map[col_name], str(val))


# ============================================================
# ৪. স্ক্রিপ্ট ক্লিনিং ও ন্যাচারাল ভয়েসওভার জেনারেশন
# ============================================================
def clean_script_for_tts(script_text):
    text = str(script_text or "").strip()
    text = re.sub(
        r"(হোম\s*বিডি\s*ইনফো\s*ডট\s*কম)\s*(www\.)?homebdinfo\.com",
        r"\1",
        text,
        flags=re.IGNORECASE
    )
    text = re.sub(
        r"(www\.)?homebdinfo\.com\s*(হোম\s*বিডি\s*ইনফো\s*ডট\s*কম)",
        r"\2",
        text,
        flags=re.IGNORECASE
    )
    text = re.sub(r"https?://(www\.)?homebdinfo\.com\S*", "হোম বিডি ইনফো ডট কম", text, flags=re.IGNORECASE)
    text = re.sub(r"(www\.)?homebdinfo\.com", "হোম বিডি ইনফো ডট কম", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_sentences(text):
    parts = re.split(r"(?<=[।!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


async def synthesize_expressive_voice(sentences, work_dir):
    """প্রতিটি বাক্যের ধরন অনুযায়ী কণ্ঠের ওঠানামা ও বিরতি দিয়ে অডিও তৈরি করে"""
    segment_files = []
    timings = []
    current_time = 0.0
    total = len(sentences)

    for idx, sentence in enumerate(sentences):
        seg_mp3 = work_dir / f"seg_{idx:03d}.mp3"
        seg_wav = work_dir / f"seg_{idx:03d}.wav"

        if idx == 0:
            rate = "+12%"
            pitch = "-1Hz"
            pause_sec = 0.35
        elif sentence.endswith("?"):
            rate = "+9%"
            pitch = "+3Hz"
            pause_sec = 0.40
        elif idx == total - 1:
            rate = "+5%"
            pitch = "-4Hz"
            pause_sec = 0.45
        else:
            rate = "+9%" if idx % 2 == 1 else "+7%"
            pitch = "-3Hz" if idx % 2 == 1 else "-2Hz"
            pause_sec = 0.28

        communicate = edge_tts.Communicate(
            text=sentence,
            voice=VOICE_NAME,
            rate=rate,
            pitch=pitch
        )
        await communicate.save(str(seg_mp3))

        subprocess.run([
            "ffmpeg", "-y", "-i", str(seg_mp3),
            "-af", f"apad=pad_dur={pause_sec}",
            "-ar", "44100", "-ac", "2",
            str(seg_wav)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        dur = get_media_duration(seg_wav)
        timings.append({
            "text": sentence,
            "start": current_time,
            "end": current_time + dur
        })
        current_time += dur
        segment_files.append(seg_wav)

    concat_list = work_dir / "audio_list.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for wav in segment_files:
            f.write(f"file '{wav.resolve()}'\n")

    final_voice = work_dir / "voice_full.wav"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c", "copy", str(final_voice)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return final_voice, timings, current_time


def get_media_duration(file_path):
    res = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)
    ], capture_output=True, text=True, check=True)
    return float(res.stdout.strip())


# ============================================================
# ৫. ওয়েবসাইটের মোবাইল স্ক্রিনশট ও স্টক ফুটেজ সংগ্রহ
# ============================================================
def capture_mobile_screenshot(post_url, out_png):
    target_url = post_url if post_url.startswith("http") else f"https://www.{SITE_DISPLAY_URL}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                device_scale_factor=2.8,
                is_mobile=True,
                has_touch=True,
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Mobile Safari/537.36"
                )
            )
            page = context.new_page()
            page.goto(target_url, wait_until="networkidle", timeout=35000)
            page.wait_for_timeout(2000)

            page.add_style_tag(content="""
                body { zoom: 1.08 !important; }
                header, .sticky, .fixed-top, [id*='cookie'], [class*='popup'] {
                    position: static !important;
                }
            """)
            page.wait_for_timeout(800)
            page.screenshot(path=str(out_png), full_page=True)
            browser.close()
            return True
    except Exception as e:
        print(f"Screenshot warning: {e}")
        fallback = Image.new("RGB", (1080, 3200), (15, 23, 42))
        fallback.save(out_png)
        return False


def download_pixabay_clip(keywords_str, work_dir):
    if not PIXABAY_API_KEY:
        return None
    keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]
    if not keywords:
        keywords = ["technology mobile", "information"]

    for kw in keywords:
        try:
            q = urllib.parse.quote(kw)
            url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={q}&per_page=5"
            resp = requests.get(url, timeout=15).json()
            hits = resp.get("hits", [])
            if hits:
                videos = hits[0].get("videos", {})
                vid_url = (
                    videos.get("medium", {}).get("url")
                    or videos.get("small", {}).get("url")
                )
                if vid_url:
                    out_mp4 = work_dir / "stock_clip.mp4"
                    r = requests.get(vid_url, stream=True, timeout=30)
                    with open(out_mp4, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                    return out_mp4
        except Exception as e:
            print(f"Pixabay search warning ({kw}): {e}")
    return None


# ============================================================
# ৬. SRT সাবটাইটেল তৈরি
# ============================================================
def format_srt_time(seconds):
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def generate_srt(timings, srt_path):
    idx = 1
    with open(srt_path, "w", encoding="utf-8") as f:
        for item in timings:
            text = item["text"]
            start = item["start"]
            end = item["end"]
            words = text.split()
            chunks = [" ".join(words[i:i + 7]) for i in range(0, len(words), 7)]
            if not chunks:
                continue
            chunk_dur = (end - start) / len(chunks)
            for c_i, chunk in enumerate(chunks):
                c_start = start + c_i * chunk_dur
                c_end = start + (c_i + 1) * chunk_dur
                wrapped = "\n".join(textwrap.wrap(chunk, width=32))
                f.write(f"{idx}\n")
                f.write(f"{format_srt_time(c_start)} --> {format_srt_time(c_end)}\n")
                f.write(f"{wrapped}\n\n")
                idx += 1


# ============================================================
# ৭. চূড়ান্ত ভিডিও রেন্ডারিং (ধীর স্ক্রোলিং + মোশন গ্রাফিক্স + প্রোগ্রেস বার)
# ============================================================
def render_final_video(work_dir, screenshot_png, stock_mp4, voice_wav, srt_path,
                       total_duration, shorts_title, out_mp4):
    raw_img = Image.open(screenshot_png).convert("RGB")
    scale_ratio = VIDEO_WIDTH / raw_img.width
    new_h = int(raw_img.height * scale_ratio)
    resized_img = raw_img.resize((VIDEO_WIDTH, new_h), Image.Resampling.LANCZOS)

    effective_h = min(new_h, VIDEO_HEIGHT + MAX_SCROLL_PX)
    if effective_h < VIDEO_HEIGHT + 400:
        padded = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT + 500), (15, 23, 42))
        padded.paste(resized_img, (0, 0))
        resized_img = padded
        effective_h = VIDEO_HEIGHT + 500
    else:
        resized_img = resized_img.crop((0, 0, VIDEO_WIDTH, effective_h))

    prepared_bg = work_dir / "bg_scroll.png"
    resized_img.save(prepared_bg, "PNG")

    top_banner_png = work_dir / "top_banner.png"
    title_card_png = work_dir / "title_card.png"
    cta_card_png = work_dir / "cta_card.png"

    create_banner_png(top_banner_png, TOP_BADGE_TEXT, width=940, height=84, font_size=32)
    create_title_card_png(title_card_png, shorts_title, width=960, height=220)
    create_cta_card_png(cta_card_png, width=980, height=220)

    scroll_distance = max(0, effective_h - VIDEO_HEIGHT)
    cta_start = max(0.0, total_duration - 5.5)

    inputs = [
        "-loop", "1", "-t", f"{total_duration:.2f}", "-i", str(prepared_bg),
        "-i", str(voice_wav),
        "-loop", "1", "-t", f"{total_duration:.2f}", "-i", str(top_banner_png),
        "-loop", "1", "-t", f"{total_duration:.2f}", "-i", str(title_card_png),
        "-loop", "1", "-t", f"{total_duration:.2f}", "-i", str(cta_card_png),
    ]

    next_idx = 5
    has_stock = stock_mp4 is not None and stock_mp4.exists()
    if has_stock:
        inputs.extend(["-stream_loop", "-1", "-t", f"{total_duration:.2f}", "-i", str(stock_mp4)])
        stock_idx = next_idx
        next_idx += 1

    has_logo = LOGO_FILE.exists()
    if has_logo:
        inputs.extend(["-loop", "1", "-t", f"{total_duration:.2f}", "-i", str(LOGO_FILE)])
        logo_idx = next_idx
        next_idx += 1

    has_bgm = BGM_FILE.exists()
    if has_bgm:
        inputs.extend(["-stream_loop", "-1", "-t", f"{total_duration:.2f}", "-i", str(BGM_FILE)])
        bgm_idx = next_idx
        next_idx += 1

    filters = []
    filters.append(
        f"[0:v]crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:0:'{scroll_distance}*(t/{total_duration:.2f})',"
        f"eq=brightness=-0.06:contrast=1.04[bg]"
    )
    current_v = "[bg]"

    if has_stock:
        filters.append(
            f"[{stock_idx}:v]scale=660:370:force_original_aspect_ratio=increase,"
            f"crop=660:370,pad=672:382:6:6:color=#38bdf8[stock_box]"
        )
        filters.append(
            f"{current_v}[stock_box]overlay=(W-w)/2:1020:"
            f"enable='between(t,3.0,{max(3.5, cta_start - 0.5):.2f})'[v_stock]"
        )
        current_v = "[v_stock]"

    filters.append(f"{current_v}[2:v]overlay=(W-w)/2:42[v_top]")
    current_v = "[v_top]"

    if has_logo:
        filters.append(f"[{logo_idx}:v]scale=96:96[logo_s]")
        filters.append(f"{current_v}[logo_s]overlay=W-w-36:145[v_logo]")
        current_v = "[v_logo]"

    filters.append(
        f"{current_v}[3:v]overlay=(W-w)/2:'if(lt(t,0.8), 160+(1-(t/0.8))*120, 160)':"
        f"enable='between(t,0.2,4.5)'[v_title]"
    )
    current_v = "[v_title]"

    filters.append(
        f"{current_v}[4:v]overlay=(W-w)/2:1060:enable='gte(t,{cta_start:.2f})'[v_cta]"
    )
    current_v = "[v_cta]"

    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
    style = (
        "FontName=Noto Sans Bengali,FontSize=15,PrimaryColour=&H00FFFFFF,"
        "BackColour=&HC8000000,BorderStyle=4,Outline=1,Shadow=0,"
        "Alignment=2,MarginV=95,MarginL=35,MarginR=35"
    )
    filters.append(f"{current_v}subtitles='{srt_escaped}':force_style='{style}'[v_sub]")
    current_v = "[v_sub]"

    filters.append(
        f"color=c=#22c55e:s={VIDEO_WIDTH}x14:d={total_duration:.2f}[pbar]"
    )
    filters.append(
        f"{current_v}[pbar]overlay=x='-W+(W*(t/{total_duration:.2f}))':y=0[v_out]"
    )

    if has_bgm:
        filters.append(
            f"[{bgm_idx}:a]volume={BGM_VOLUME}[bgm_low];"
            f"[1:a][bgm_low]amix=inputs=2:duration=first:dropout_transition=2[a_out]"
        )
        audio_map = "[a_out]"
    else:
        audio_map = "1:a"

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[v_out]",
        "-map", audio_map,
        "-c:v", "libx264", "-preset", "medium", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-r", str(FPS),
        "-t", f"{total_duration:.2f}",
        str(out_mp4)
    ]

    subprocess.run(cmd, check=True)


# ============================================================
# ৮. GitHub Releases আপলোড ও Telegram নোটিফিকেশন
# ============================================================
def upload_to_github_release(video_path, row_idx, title):
    if not GITHUB_TOKEN:
        raise ValueError("GITHUB_TOKEN পাওয়া যায়নি।")

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    tag_name = f"short-row-{row_idx}-{int(time.time())}"
    release_payload = {
        "tag_name": tag_name,
        "name": f"Shorts Row {row_idx}: {title[:60]}",
        "body": f"Auto-generated Bangla YouTube Short for row {row_idx}.",
        "draft": False,
        "prerelease": False
    }

    rel_resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases",
        headers=headers,
        json=release_payload,
        timeout=30
    )
    rel_resp.raise_for_status()
    rel_data = rel_resp.json()
    release_id = rel_data["id"]
    upload_url = rel_data["upload_url"].split("{")[0]

    file_name = video_path.name
    upload_headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "video/mp4"
    }
    with open(video_path, "rb") as f:
        up_resp = requests.post(
            f"{upload_url}?name={urllib.parse.quote(file_name)}",
            headers=upload_headers,
            data=f,
            timeout=120
        )
    up_resp.raise_for_status()
    asset_data = up_resp.json()
    return str(release_id), asset_data["browser_download_url"]


def send_telegram_approval(video_path, row_idx, shorts_title, post_url, download_url):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN বা TELEGRAM_CHAT_ID সেট করা নেই।")

    caption = (
        f"🎬 <b>নতুন Bangla Short প্রস্তুত!</b>\n\n"
        f"📌 <b>শিরোনাম:</b> {shorts_title}\n"
        f"📊 <b>Sheet Row:</b> {row_idx}\n"
        f"🔗 <b>Post URL:</b> {post_url}\n"
        f"⬇️ <b>Download:</b> <a href='{download_url}'>GitHub Release MP4</a>\n\n"
        f"ভিডিওটি যাচাই করে নিচের বাটনে ক্লিক করুন:"
    )

    reply_markup = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"APPROVE_{row_idx}"},
            {"text": "❌ Reject", "callback_data": f"REJECT_{row_idx}"}
        ]]
    }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    with open(video_path, "rb") as vf:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(reply_markup)
            },
            files={"video": (video_path.name, vf, "video/mp4")},
            timeout=120
        )
    resp.raise_for_status()
    result = resp.json()
    msg = result.get("result", {})
    video_file_id = msg.get("video", {}).get("file_id", "")
    message_id = msg.get("message_id", "")
    return str(video_file_id), str(message_id)


# ============================================================
# ৯. মেইন এক্সিকিউশন ফ্লো
# ============================================================
def main():
    sheet = get_google_sheet()
    header_map = get_header_map(sheet)
    all_rows = sheet.get_all_values()

    target_row_idx = None
    target_row_data = None

    for idx in range(2, len(all_rows) + 1):
        row = all_rows[idx - 1]
        status_col = header_map.get("Status", 0) - 1
        if status_col >= 0 and status_col < len(row):
            if row[status_col].strip() == "SCRIPT_READY":
                target_row_idx = idx
                target_row_data = row
                break

    if not target_row_idx:
        print("কোনো SCRIPT_READY রো পাওয়া যায়নি।")
        return

    def get_col(name, default=""):
        col_i = header_map.get(name, 0) - 1
        if 0 <= col_i < len(target_row_data):
            return target_row_data[col_i].strip()
        return default

    shorts_title = get_col("Shorts Title (YT)") or get_col("Post Title") or "গুরুত্বপূর্ণ তথ্য"
    raw_script = get_col("Bangla Voiceover Script")
    keywords = get_col("Image Search Keywords (English)")
    post_url = get_col("Post URL")

    if not raw_script:
        update_row_cells(sheet, target_row_idx, header_map, {
            "Status": "ERROR",
            "Last Error": "Bangla Voiceover Script খালি রয়েছে।"
        })
        return

    print(f"Processing Row {target_row_idx}: {shorts_title}")
    update_row_cells(sheet, target_row_idx, header_map, {
        "Status": "PROCESSING",
        "Video Status": "GENERATING"
    })

    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    try:
        clean_script = clean_script_for_tts(raw_script)
        sentences = split_sentences(clean_script)
        voice_wav, timings, total_duration = asyncio.run(
            synthesize_expressive_voice(sentences, WORK_DIR)
        )

        srt_path = WORK_DIR / "subtitles.srt"
        generate_srt(timings, srt_path)

        screenshot_png = WORK_DIR / "site_mobile.png"
        capture_mobile_screenshot(post_url, screenshot_png)
        stock_mp4 = download_pixabay_clip(keywords, WORK_DIR)

        final_mp4 = WORK_DIR / f"bangla_short_row_{target_row_idx}.mp4"
        render_final_video(
            WORK_DIR, screenshot_png, stock_mp4, voice_wav, srt_path,
            total_duration, shorts_title, final_mp4
        )

        release_id, download_url = upload_to_github_release(
            final_mp4, target_row_idx, shorts_title
        )

        tg_file_id, tg_msg_id = send_telegram_approval(
            final_mp4, target_row_idx, shorts_title, post_url, download_url
        )

        update_row_cells(sheet, target_row_idx, header_map, {
            "Status": "PENDING_APPROVAL",
            "Video Status": "COMPLETED",
            "Drive File ID": release_id,
            "Drive File URL": download_url,
            "Telegram Video File ID": tg_file_id,
            "Telegram Message ID": tg_msg_id,
            "Last Error": ""
        })
        print(f"Row {target_row_idx} সফলভাবে সম্পন্ন হয়েছে!")

    except Exception as e:
        err_msg = str(e)[:1000]
        print(f"Error processing row {target_row_idx}: {err_msg}")
        update_row_cells(sheet, target_row_idx, header_map, {
            "Status": "ERROR",
            "Video Status": "FAILED",
            "Last Error": err_msg
        })
        raise


if __name__ == "__main__":
    main()
