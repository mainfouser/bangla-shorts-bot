import os
import re
import json
import math
import time
import asyncio
import random
import tempfile
import subprocess
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import requests
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

# ============================================================
# ভয়েস কনফিগারেশন (শুধুমাত্র স্মার্ট ও ন্যাচারাল পুরুষ কণ্ঠ)
# সহজেই পরিবর্তন করতে নিচের VOICE_NAME পরিবর্তন করুন:
# - "bn-BD-PradeepNeural" (বাংলাদেশি স্মার্ট পুরুষ কণ্ঠ - রেকমেন্ডেড)
# - "bn-IN-BashkarNeural" (ভারতীয় বাংলা পুরুষ কণ্ঠ)
# ============================================================
VOICE_NAME = "bn-BD-PradeepNeural"

# গিটহাব ও ফাইল কনফিগারেশন
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot")
SHEET_NAME = "Shorts_Queue"
BGM_FILE = "bgm.mp3"
LOGO_FILE = "logo.png"
BGM_VOLUME = 0.17  # ১৭% ব্যাকগ্রাউন্ড মিউজিক ভলিউম

# ভিডিও ডাইমেনশন
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 30
MAX_SCROLL_PX = 2600  # ধীরগতির স্মুথ স্ক্রোলিংয়ের জন্য সর্বোচ্চ স্ক্রল সীমা

# ফন্ট পাথ (Ubuntu / GitHub Actions)
BENGALI_FONT_PATHS = [
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansBengali-Bold.ttf",
]
LATIN_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def find_font(paths: List[str], size: int) -> ImageFont.FreeTypeFont:
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def get_google_sheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
    sheet_id = os.environ.get("SHEET_ID", "").strip()
    if not creds_json or not sheet_id:
        raise RuntimeError("GOOGLE_CREDENTIALS বা SHEET_ID সেট করা নেই।")

    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id).worksheet(SHEET_NAME)


def clean_voiceover_script(text: str) -> str:
    text = str(text or "").strip()
    # ডাবল উচ্চারণ প্রতিরোধ (যেমন: হোম বিডি ইনফো ডট কম homebdinfo.com)
    text = re.sub(
        r"হোম\s*বিডি\s*ইনফো\s*ডট\s*কম\s*\(?homebdinfo\.com\)?",
        "হোম বিডি ইনফো ডট কম",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"homebdinfo\.com\s*\(?হোম\s*বিডি\s*ইনফো\s*ডট\s*কম\)?",
        "হোম বিডি ইনফো ডট কম",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"https?://(?:www\.)?homebdinfo\.com\S*", "হোম বিডি ইনফো ডট কম", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:www\.)?homebdinfo\.com", "হোম বিডি ইনফো ডট কম", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_sentences_for_tts(script: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"(?<=[।!?\.])\s+", script) if p.strip()]
    return parts if parts else [script]


def get_prosody_for_sentence(sentence: str, idx: int, total: int) -> Tuple[str, str, float]:
    """বাক্যের ধরন অনুযায়ী মানুষের মতো ন্যাচারাল গতি, সুর ও বিরতি নির্ধারণ করে।"""
    if idx == 0:
        # Hook বাক্য: প্রাণবন্ত ও আকর্ষণীয়
        return "+12%", "-1Hz", 0.35
    if "?" in sentence or "কীভাবে" in sentence or "কেন" in sentence or "কী" in sentence:
        # প্রশ্নবোধক বাক্য: সুর সামান্য উপরে উঠবে
        return "+8%", "+3Hz", 0.35
    if idx == total - 1:
        # শেষ বাক্য (CTA): গুরুত্ব ও আহ্বান বোঝাতে ভরাট ও ধীর কণ্ঠ
        return "+5%", "-5Hz", 0.40
    # সাধারণ তথ্যমূলক বাক্য
    return "+8%", "-3Hz", 0.28


async def _synthesize_chunk(text: str, out_path: str, rate: str, pitch: str):
    communicate = edge_tts.Communicate(text=text, voice=VOICE_NAME, rate=rate, pitch=pitch)
    await communicate.save(out_path)


def generate_natural_voiceover(script: str, work_dir: str) -> Tuple[str, List[Dict]]:
    """প্রতিটি বাক্য আলাদাভাবে ন্যাচারাল টোনে তৈরি করে FFmpeg দিয়ে জোড়া লাগায় এবং সাবটাইটেল টাইমিং বের করে।"""
    cleaned = clean_voiceover_script(script)
    sentences = split_sentences_for_tts(cleaned)
    total = len(sentences)

    segment_files = []
    subtitle_segments = []
    current_time = 0.0

    for idx, sentence in enumerate(sentences):
        rate, pitch, pause_sec = get_prosody_for_sentence(sentence, idx, total)
        raw_mp3 = os.path.join(work_dir, f"tts_{idx}.mp3")
        wav_seg = os.path.join(work_dir, f"tts_{idx}.wav")
        sil_wav = os.path.join(work_dir, f"sil_{idx}.wav")

        asyncio.run(_synthesize_chunk(sentence, raw_mp3, rate, pitch))

        # MP3 থেকে 44100Hz Stereo WAV এ কনভার্ট
        subprocess.run(
            ["ffmpeg", "-y", "-i", raw_mp3, "-ar", "44100", "-ac", "2", wav_seg],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # অডিওর দৈর্ঘ্য পরিমাপ
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                wav_seg,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        speech_dur = float(probe.stdout.strip())

        # ছোট সাবটাইটেল চাঙ্কে ভাগ করা যাতে স্ক্রিনে লেখা কখনো না কাটে
        chunks = split_subtitle_chunks(sentence, max_chars=48)
        total_chars = sum(len(c) for c in chunks) or 1
        t_cursor = current_time
        for ch in chunks:
            ch_dur = speech_dur * (len(ch) / total_chars)
            subtitle_segments.append(
                {
                    "text": ch,
                    "start": t_cursor,
                    "end": t_cursor + ch_dur,
                }
            )
            t_cursor += ch_dur

        segment_files.append(wav_seg)
        current_time += speech_dur

        # স্বাভাবিক বিরতি (Silence) যুক্ত করা
        if pause_sec > 0:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=44100:cl=stereo",
                    "-t",
                    f"{pause_sec:.2f}",
                    sil_wav,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            segment_files.append(sil_wav)
            current_time += pause_sec

    concat_list = os.path.join(work_dir, "audio_concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for sf in segment_files:
            f.write(f"file '{sf}'\n")

    final_voice_wav = os.path.join(work_dir, "voiceover_full.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", final_voice_wav],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return final_voice_wav, subtitle_segments


def split_subtitle_chunks(sentence: str, max_chars: int = 48) -> List[str]:
    words = sentence.split()
    chunks = []
    cur = []
    cur_len = 0
    for w in words:
        if cur and cur_len + len(w) + 1 > max_chars:
            chunks.append(" ".join(cur))
            cur = [w]
            cur_len = len(w)
        else:
            cur.append(w)
            cur_len += len(w) + (1 if cur_len > 0 else 0)
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def render_mixed_text_line(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    bn_font: ImageFont.FreeTypeFont,
    en_font: ImageFont.FreeTypeFont,
    fill=(255, 255, 255, 255),
):
    """বাংলা ও ইংরেজি অক্ষর আলাদা ফন্টে রেন্ডার করে যাতে ইংরেজি অক্ষর সাদা বক্স না হয়।"""
    tokens = re.findall(r"[A-Za-z0-9\./:\-_@]+|[^A-Za-z0-9\./:\-_@]+", text)
    cursor_x = x
    for tok in tokens:
        is_latin = bool(re.match(r"^[A-Za-z0-9\./:\-_@]+$", tok))
        fnt = en_font if is_latin else bn_font
        draw.text((cursor_x, y), tok, font=fnt, fill=fill)
        bbox = draw.textbbox((0, 0), tok, font=fnt)
        cursor_x += bbox[2] - bbox[0]


def measure_mixed_text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    bn_font: ImageFont.FreeTypeFont,
    en_font: ImageFont.FreeTypeFont,
) -> int:
    tokens = re.findall(r"[A-Za-z0-9\./:\-_@]+|[^A-Za-z0-9\./:\-_@]+", text)
    total_w = 0
    for tok in tokens:
        is_latin = bool(re.match(r"^[A-Za-z0-9\./:\-_@]+$", tok))
        fnt = en_font if is_latin else bn_font
        bbox = draw.textbbox((0, 0), tok, font=fnt)
        total_w += bbox[2] - bbox[0]
    return total_w


def wrap_text_by_pixel(
    draw: ImageDraw.ImageDraw,
    text: str,
    bn_font: ImageFont.FreeTypeFont,
    en_font: ImageFont.FreeTypeFont,
    max_width: int,
) -> List[str]:
    words = text.split()
    lines = []
    cur = []
    for w in words:
        test = " ".join(cur + [w])
        if cur and measure_mixed_text_width(draw, test, bn_font, en_font) > max_width:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return lines


def create_subtitle_png(text: str, out_path: str):
    """Pillow দিয়ে নিরাপদ মার্জিনসহ ট্রান্সপারেন্ট PNG সাবটাইটেল কার্ড তৈরি করে (লেখা কখনো কাটবে না)।"""
    canvas_w = 1000
    canvas_h = 280
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bn_font = find_font(BENGALI_FONT_PATHS, 42)
    en_font = find_font(LATIN_FONT_PATHS, 40)

    max_text_w = 880
    lines = wrap_text_by_pixel(draw, text, bn_font, en_font, max_text_w)
    if len(lines) > 3:
        lines = lines[:3]

    line_height = 58
    pad_x = 36
    pad_y = 22

    max_line_w = max(
        (measure_mixed_text_width(draw, ln, bn_font, en_font) for ln in lines),
        default=200,
    )
    box_w = min(canvas_w - 20, max_line_w + pad_x * 2)
    box_h = len(lines) * line_height + pad_y * 2

    box_x0 = (canvas_w - box_w) // 2
    box_y0 = (canvas_h - box_h) // 2
    box_x1 = box_x0 + box_w
    box_y1 = box_y0 + box_h

    # হালকা স্বচ্ছ কালো ব্যাকগ্রাউন্ড যাতে পেছনের ওয়েবসাইটও দেখা যায়
    draw.rounded_rectangle(
        [box_x0, box_y0, box_x1, box_y1],
        radius=24,
        fill=(12, 16, 24, 195),
        outline=(255, 255, 255, 70),
        width=2,
    )

    for i, ln in enumerate(lines):
        lw = measure_mixed_text_width(draw, ln, bn_font, en_font)
        lx = (canvas_w - lw) // 2
        ly = box_y0 + pad_y + i * line_height
        render_mixed_text_line(draw, lx, ly, ln, bn_font, en_font, fill=(255, 255, 255, 255))

    img.save(out_path, "PNG")


def create_top_banner_png(out_path: str):
    """উপরের প্রমোশন ব্যাজ PNG তৈরি করে (বাংলা + ইংরেজি ফন্ট সাপোর্টসহ)।"""
    w, h = 920, 96
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bn_font = find_font(BENGALI_FONT_PATHS, 34)
    en_font = find_font(LATIN_FONT_PATHS, 34)

    draw.rounded_rectangle(
        [10, 10, w - 10, h - 10],
        radius=40,
        fill=(11, 79, 108, 230),
        outline=(255, 255, 255, 140),
        width=3,
    )
    text = "বিস্তারিত তথ্য ও আবেদন: homebdinfo.com"
    tw = measure_mixed_text_width(draw, text, bn_font, en_font)
    render_mixed_text_line(draw, (w - tw) // 2, 26, text, bn_font, en_font, fill=(255, 255, 255, 255))
    img.save(out_path, "PNG")


def create_cta_banner_png(out_path: str):
    """শেষ ৫ সেকেন্ডের কল-টু-অ্যাকশন (CTA) কার্ড PNG তৈরি করে।"""
    w, h = 960, 180
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bn_font = find_font(BENGALI_FONT_PATHS, 36)
    en_font = find_font(LATIN_FONT_PATHS, 38)

    draw.rounded_rectangle(
        [10, 10, w - 10, h - 10],
        radius=28,
        fill=(245, 158, 11, 245),
        outline=(255, 255, 255, 220),
        width=4,
    )
    line1 = "সম্পূর্ণ তথ্য ও অনলাইন লিংক পেতে"
    line2 = "Google-এ সার্চ করুন: homebdinfo.com"

    w1 = measure_mixed_text_width(draw, line1, bn_font, en_font)
    w2 = measure_mixed_text_width(draw, line2, bn_font, en_font)

    render_mixed_text_line(draw, (w - w1) // 2, 32, line1, bn_font, en_font, fill=(17, 24, 39, 255))
    render_mixed_text_line(draw, (w - w2) // 2, 96, line2, bn_font, en_font, fill=(17, 24, 39, 255))
    img.save(out_path, "PNG")


def capture_mobile_site_screenshot(post_url: str, out_png: str):
    """Playwright দিয়ে খাঁটি মোবাইল ভিউপোর্টে ওয়েবসাইটের বড় ও স্পষ্ট স্ক্রিনশট নেয়।"""
    target_url = post_url if post_url.startswith("http") else "https://www.homebdinfo.com"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2.8,
            is_mobile=True,
            has_touch=True,
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            page.goto(target_url, wait_until="networkidle", timeout=45000)
        except Exception:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)

        page.wait_for_timeout(2500)
        # পপআপ বা কুকি ব্যানার থাকলে লুকানো এবং টেক্সট জুম সামান্য বাড়ানো
        page.evaluate(
            """() => {
                document.body.style.zoom = "1.10";
                const selectors = ['[id*="cookie"]', '[class*="cookie"]', '[class*="popup"]', '.adsbygoogle'];
                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => el.style.display = 'none');
                });
            }"""
        )
        page.wait_for_timeout(1000)
        page.screenshot(path=out_png, full_page=True)
        browser.close()

    # ইমেজটিকে 1080px প্রস্থে রিসাইজ করা এবং সর্বোচ্চ উচ্চতা সীমিত রাখা
    img = Image.open(out_png).convert("RGB")
    scale = VIDEO_WIDTH / img.width
    new_h = int(img.height * scale)
    img = img.resize((VIDEO_WIDTH, new_h), Image.Resampling.LANCZOS)

    max_allowed_h = VIDEO_HEIGHT + MAX_SCROLL_PX
    if img.height > max_allowed_h:
        img = img.crop((0, 0, VIDEO_WIDTH, max_allowed_h))
    elif img.height < VIDEO_HEIGHT + 400:
        padded = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT + 400), (245, 247, 250))
        padded.paste(img, (0, 0))
        img = padded

    img.save(out_png, "PNG")
    return img.height


def download_pixabay_clip(keywords: str, out_mp4: str) -> Optional[str]:
    api_key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not api_key:
        return None

    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    if not kw_list:
        kw_list = ["technology", "smartphone", "office"]

    for kw in kw_list:
        try:
            url = (
                f"https://pixabay.com/api/videos/?key={api_key}"
                f"&q={requests.utils.quote(kw)}&per_page=5&safesearch=true"
            )
            resp = requests.get(url, timeout=20).json()
            hits = resp.get("hits", [])
            if hits:
                chosen = random.choice(hits[:3])
                videos = chosen.get("videos", {})
                vid_info = videos.get("medium") or videos.get("small") or videos.get("large")
                if vid_info and vid_info.get("url"):
                    v_data = requests.get(vid_info["url"], timeout=40).content
                    with open(out_mp4, "wb") as f:
                        f.write(v_data)
                    return out_mp4
        except Exception:
            continue
    return None


def build_final_video(
    screenshot_png: str,
    screenshot_h: int,
    voice_wav: str,
    subtitle_segments: List[Dict],
    stock_clip: Optional[str],
    output_mp4: str,
    work_dir: str,
):
    # ভয়েসওভারের মোট দৈর্ঘ্য
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            voice_wav,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    duration = float(probe.stdout.strip()) + 0.8

    top_banner_png = os.path.join(work_dir, "top_banner.png")
    cta_banner_png = os.path.join(work_dir, "cta_banner.png")
    create_top_banner_png(top_banner_png)
    create_cta_banner_png(cta_banner_png)

    # সাবটাইটেল PNG গুলো তৈরি করা
    sub_pngs = []
    for idx, seg in enumerate(subtitle_segments):
        sp = os.path.join(work_dir, f"sub_{idx}.png")
        create_subtitle_png(seg["text"], sp)
        sub_pngs.append((sp, seg["start"], seg["end"]))

    # FFmpeg ইনপুট ও ফিল্টার গ্রাফ তৈরি
    inputs = [
        "-loop", "1", "-t", f"{duration:.2f}", "-i", screenshot_png,  # [0] স্ক্রলিং সাইট ব্যাকগ্রাউন্ড
        "-i", voice_wav,                                              # [1] ভয়েসওভার অডিও
        "-i", top_banner_png,                                         # [2] উপরের প্রমোশন ব্যানার
        "-i", cta_banner_png,                                         # [3] শেষের CTA ব্যানার
    ]

    next_idx = 4
    bgm_idx = None
    if os.path.exists(BGM_FILE):
        inputs.extend(["-stream_loop", "-1", "-t", f"{duration:.2f}", "-i", BGM_FILE])
        bgm_idx = next_idx
        next_idx += 1

    logo_idx = None
    if os.path.exists(LOGO_FILE):
        inputs.extend(["-i", LOGO_FILE])
        logo_idx = next_idx
        next_idx += 1

    stock_idx = None
    if stock_clip and os.path.exists(stock_clip):
        inputs.extend(["-stream_loop", "-1", "-t", f"{duration:.2f}", "-i", stock_clip])
        stock_idx = next_idx
        next_idx += 1

    sub_start_idx = next_idx
    for sp, _, _ in sub_pngs:
        inputs.extend(["-i", sp])

    scroll_dist = max(0, screenshot_h - VIDEO_HEIGHT)
    filter_parts = []

    # [0] ব্যাকগ্রাউন্ড ওয়েবসাইট ধীরগতির স্মুথ স্ক্রোলিং + হালকা কন্ট্রাস্ট টিউনিং
    filter_parts.append(
        f"[0:v]crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:0:'min({scroll_dist}, {scroll_dist}*(t/{duration:.2f}))',"
        f"eq=brightness=-0.04:contrast=1.04[bg]"
    )
    last_v = "bg"

    # মাঝখানে ছোট রাউন্ডেড স্টক ভিডিও কার্ড (যদি পাওয়া যায়)
    if stock_idx is not None:
        filter_parts.append(
            f"[{stock_idx}:v]scale=640:360:force_original_aspect_ratio=increase,"
            f"crop=640:360,pad=652:372:6:6:color=white@0.85[card]"
        )
        filter_parts.append(
            f"[{last_v}][card]overlay=(W-w)/2:880:enable='between(t,2.5,{max(3.0, duration - 5.5):.2f})'[v_card]"
        )
        last_v = "v_card"

    # উপরের প্রমোশন ব্যানার
    filter_parts.append(f"[{last_v}][2:v]overlay=(W-w)/2:55[v_top]")
    last_v = "v_top"

    # লোগো ওয়াটারমার্ক (উপরে ডান কোণায়)
    if logo_idx is not None:
        filter_parts.append(f"[{logo_idx}:v]scale=110:-1[logo_s]")
        filter_parts.append(f"[{last_v}][logo_s]overlay=W-w-35:175[v_logo]")
        last_v = "v_logo"

    # অ্যানিমেটেড টাইমলাইন প্রোগ্রেস বার (একদম উপরে)
    filter_parts.append(
        f"[{last_v}]drawbox=x=0:y=0:w='iw*(t/{duration:.2f})':h=14:color=#10B981@0.95:t=fill[v_prog]"
    )
    last_v = "v_prog"

    # সাবটাইটেল PNG ওভারলে (প্রতিটি চাঙ্ক নিজস্ব সময়ে)
    for i, (_, st, en) in enumerate(sub_pngs):
        s_in = sub_start_idx + i
        out_lbl = f"v_sub_{i}"
        filter_parts.append(
            f"[{last_v}][{s_in}:v]overlay=(W-w)/2:1330:enable='between(t,{st:.2f},{en:.2f})'[{out_lbl}]"
        )
        last_v = out_lbl

    # শেষ ৫ সেকেন্ডে কল-টু-অ্যাকশন (CTA) ব্যানার
    cta_start = max(0.0, duration - 5.2)
    filter_parts.append(
        f"[{last_v}][3:v]overlay=(W-w)/2:1630:enable='between(t,{cta_start:.2f},{duration:.2f})'[v_final]"
    )

    # অডিও মিক্সিং (ভয়েস + ১৭% ব্যাকগ্রাউন্ড মিউজিক)
    if bgm_idx is not None:
        filter_parts.append(
            f"[{bgm_idx}:a]volume={BGM_VOLUME}[bgm_low];"
            f"[1:a][bgm_low]amix=inputs=2:duration=first:dropout_transition=2[a_final]"
        )
    else:
        filter_parts.append("[1:a]acopy[a_final]")

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        filter_complex,
        "-map",
        "[v_final]",
        "-map",
        "[a_final]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-r",
        str(FPS),
        "-t",
        f"{duration:.2f}",
        output_mp4,
    ]
    subprocess.run(cmd, check=True)


def upload_to_github_release(video_path: str, row_num: int, title: str) -> Tuple[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN পাওয়া যায়নি।")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    tag_name = f"shorts-row-{row_num}-{int(time.time())}"
    release_payload = {
        "tag_name": tag_name,
        "name": f"Shorts Row {row_num}: {title[:60]}",
        "body": "Automated Bangla YouTube Shorts video release.",
        "draft": False,
        "prerelease": False,
    }
    rel_resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases",
        headers=headers,
        json=release_payload,
        timeout=30,
    )
    rel_resp.raise_for_status()
    rel_data = rel_resp.json()
    release_id = str(rel_data["id"])
    upload_url = rel_data["upload_url"].split("{")[0]

    file_name = os.path.basename(video_path)
    with open(video_path, "rb") as f:
        up_resp = requests.post(
            f"{upload_url}?name={file_name}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "video/mp4",
            },
            data=f,
            timeout=120,
        )
    up_resp.raise_for_status()
    download_url = up_resp.json()["browser_download_url"]
    return release_id, download_url


def send_telegram_for_approval(video_path: str, row_num: int, title: str, post_url: str) -> Tuple[str, str]:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN বা TELEGRAM_CHAT_ID পাওয়া যায়নি।")

    caption = (
        f"🎬 <b>নতুন Bangla Shorts প্রস্তুত!</b>\n\n"
        f"📌 <b>শিরোনাম:</b> {title}\n"
        f"🔗 <b>পোস্ট:</b> {post_url}\n"
        f"📊 <b>Sheet Row:</b> {row_num}"
    )
    reply_markup = json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"APPROVE_{row_num}"},
                    {"text": "❌ Reject", "callback_data": f"REJECT_{row_num}"},
                ]
            ]
        }
    )

    with open(video_path, "rb") as vf:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendVideo",
            data={
                "chat_id": chat_id,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            },
            files={"video": vf},
            timeout=120,
        )
    res_json = resp.json()
    if not res_json.get("ok"):
        raise RuntimeError(f"Telegram sendVideo Error: {res_json}")

    msg = res_json["result"]
    msg_id = str(msg["message_id"])
    file_id = str(msg.get("video", {}).get("file_id", ""))
    return file_id, msg_id


def main():
    sheet = get_google_sheet()
    headers = [str(h).strip() for h in sheet.row_values(1)]
    col_map = {h: idx + 1 for idx, h in enumerate(headers)}

    all_rows = sheet.get_all_values()
    target_row = None
    row_data = None

    for r_idx in range(2, len(all_rows) + 1):
        row = all_rows[r_idx - 1]
        status_val = row[col_map["Status"] - 1].strip() if len(row) >= col_map["Status"] else ""
        if status_val == "SCRIPT_READY":
            target_row = r_idx
            row_data = row
            break

    if not target_row:
        print("SCRIPT_READY স্ট্যাটাসযুক্ত কোনো রো পাওয়া যায়নি।")
        return

    print(f"Processing Row {target_row}...")
    sheet.update_cell(target_row, col_map["Status"], "PROCESSING")
    sheet.update_cell(target_row, col_map["Video Status"], "GENERATING")

    try:
        yt_title = row_data[col_map["Shorts Title (YT)"] - 1].strip()
        script = row_data[col_map["Bangla Voiceover Script"] - 1].strip()
        keywords = row_data[col_map["Image Search Keywords (English)"] - 1].strip()
        post_url = (
            row_data[col_map["Post URL"] - 1].strip()
            if "Post URL" in col_map and len(row_data) >= col_map["Post URL"]
            else "https://www.homebdinfo.com"
        )

        if not script:
            raise ValueError("Bangla Voiceover Script খালি রয়েছে।")

        with tempfile.TemporaryDirectory() as work_dir:
            screenshot_png = os.path.join(work_dir, "site_mobile.png")
            stock_mp4 = os.path.join(work_dir, "stock.mp4")
            final_mp4 = os.path.join(work_dir, f"bangla_short_row_{target_row}.mp4")

            print("1. Capturing mobile website screenshot...")
            sc_height = capture_mobile_site_screenshot(post_url, screenshot_png)

            print("2. Generating natural male voiceover...")
            voice_wav, subtitle_segments = generate_natural_voiceover(script, work_dir)

            print("3. Fetching stock video clip...")
            downloaded_clip = download_pixabay_clip(keywords, stock_mp4)

            print("4. Rendering final promotional video...")
            build_final_video(
                screenshot_png=screenshot_png,
                screenshot_h=sc_height,
                voice_wav=voice_wav,
                subtitle_segments=subtitle_segments,
                stock_clip=downloaded_clip,
                output_mp4=final_mp4,
                work_dir=work_dir,
            )

            print("5. Uploading to GitHub Releases...")
            rel_id, rel_url = upload_to_github_release(final_mp4, target_row, yt_title)

            print("6. Sending to Telegram for approval...")
            tg_file_id, tg_msg_id = send_telegram_for_approval(final_mp4, target_row, yt_title, post_url)

            sheet.update_cell(target_row, col_map["Drive File ID"], rel_id)
            sheet.update_cell(target_row, col_map["Drive File URL"], rel_url)
            sheet.update_cell(target_row, col_map["Telegram Video File ID"], tg_file_id)
            sheet.update_cell(target_row, col_map["Telegram Message ID"], tg_msg_id)
            sheet.update_cell(target_row, col_map["Video Status"], "COMPLETED")
            sheet.update_cell(target_row, col_map["Status"], "PENDING_APPROVAL")
            sheet.update_cell(target_row, col_map["Last Error"], "")
            print(f"Row {target_row} completed successfully!")

    except Exception as e:
        err_msg = str(e)[:900]
        print(f"Error on Row {target_row}: {err_msg}")
        sheet.update_cell(target_row, col_map["Status"], "ERROR")
        sheet.update_cell(target_row, col_map["Video Status"], "FAILED")
        sheet.update_cell(target_row, col_map["Last Error"], err_msg)
        raise


if __name__ == "__main__":
    main()
