import os
import re
import json
import time
import asyncio
import random
import tempfile
import subprocess
from typing import List, Dict, Optional, Tuple

import requests
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

# ============================================================
# ভয়েস ও ব্র্যান্ড কনফিগারেশন
# ============================================================
VOICE_NAME = "bn-BD-PradeepNeural"
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot")
SHEET_NAME = "Shorts_Queue"
BGM_FILE = "bgm.mp3"
LOGO_FILE = "logo.png"
BGM_VOLUME = 0.16

# ভিডিও ডাইমেনশন ও টাইমিং
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 30
MAX_SCROLL_PX = 2600
HOOK_DURATION = 4.0    # প্রথম ৪.০ সেকেন্ড ফুল-স্ক্রিন হুক ক্লিপ
OUTRO_DURATION = 3.0   # শেষ ৩.০ সেকেন্ড প্রফেশনাল আউট্রো কার্ড
COVER_DURATION = 0.35  # ইউটিউব শর্টসের শুরুতে কভার ফ্রেম

# ফন্ট পাথ (Ubuntu / GitHub Actions)
BENGALI_FONT_PATHS = [
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansBengali-SemiBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansBengali-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSerifBengali-Bold.ttf",
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
    for root, _, files in os.walk("/usr/share/fonts"):
        for f in files:
            if "Bengali" in f and f.endswith((".ttf", ".otf")):
                try:
                    return ImageFont.truetype(os.path.join(root, f), size)
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
    text = re.sub(r" (?:www\.)?homebdinfo\.com ", "হোম বিডি ইনফো ডট কম", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_sentences_for_tts(script: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"(?<=[।!?\.])\s+", script) if p.strip()]
    return parts if parts else [script]


def get_prosody_for_sentence(sentence: str, idx: int, total: int) -> Tuple[str, str, float]:
    if idx == 0:
        return "+14%", "+1Hz", 0.30
    if "?" in sentence or "কীভাবে" in sentence or "কেন" in sentence or "কী" in sentence:
        return "+10%", "+4Hz", 0.30
    if idx == total - 1:
        return "+8%", "-2Hz", 0.35
    return "+11%", "0Hz", 0.24


async def _synthesize_chunk(text: str, out_path: str, rate: str, pitch: str):
    communicate = edge_tts.Communicate(text=text, voice=VOICE_NAME, rate=rate, pitch=pitch)
    await communicate.save(out_path)


def split_kinetic_phrases(sentence: str, max_words: int = 4) -> List[str]:
    words = sentence.split()
    phrases = []
    for i in range(0, len(words), max_words):
        phrases.append(" ".join(words[i:i + max_words]))
    return phrases if phrases else [sentence]


def generate_natural_voiceover(script: str, work_dir: str) -> Tuple[str, List[Dict]]:
    cleaned = clean_voiceover_script(script)
    sentences = split_sentences_for_tts(cleaned)
    total = len(sentences)

    segment_files = []
    kinetic_segments = []
    current_time = 0.0

    for idx, sentence in enumerate(sentences):
        rate, pitch, pause_sec = get_prosody_for_sentence(sentence, idx, total)
        raw_mp3 = os.path.join(work_dir, f"tts_{idx}.mp3")
        wav_seg = os.path.join(work_dir, f"tts_{idx}.wav")
        sil_wav = os.path.join(work_dir, f"sil_{idx}.wav")

        asyncio.run(_synthesize_chunk(sentence, raw_mp3, rate, pitch))

        # স্টুডিও-কোয়ালিটি স্মার্ট ভয়েস প্রসেসিং
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", raw_mp3,
                "-af", "highpass=f=85,equalizer=f=3200:t=q:w=1.5:g=3,acompressor=threshold=-18dB:ratio=3:attack=5:release=50,loudnorm=I=-15:TP=-1.5:LRA=9",
                "-ar", "44100", "-ac", "2", wav_seg
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", wav_seg,
            ],
            capture_output=True, text=True, check=True,
        )
        speech_dur = float(probe.stdout.strip())

        phrases = split_kinetic_phrases(sentence, max_words=4)
        total_chars = sum(len(p) for p in phrases) or 1
        t_cursor = current_time
        for p_i, ph in enumerate(phrases):
            ph_dur = speech_dur * (len(ph) / total_chars)
            kinetic_segments.append(
                {
                    "text": ph,
                    "start": t_cursor,
                    "end": t_cursor + ph_dur + (pause_sec * 0.5 if p_i == len(phrases) - 1 else 0.0),
                    "is_hook": (idx == 0),
                }
            )
            t_cursor += ph_dur

        segment_files.append(wav_seg)
        current_time += speech_dur

        if pause_sec > 0:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                    "-t", f"{pause_sec:.2f}", sil_wav,
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
    return final_voice_wav, kinetic_segments


def render_mixed_text_line(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    bn_font: ImageFont.FreeTypeFont,
    en_font: ImageFont.FreeTypeFont,
    fill=(255, 255, 255, 255),
    stroke_width: int = 0,
    stroke_fill=(0, 0, 0, 255),
):
    tokens = re.findall(r"[A-Za-z0-9\./:\-_@|]+|[^A-Za-z0-9\./:\-_@|]+", text)
    cursor_x = x
    for tok in tokens:
        is_latin = bool(re.match(r"^[A-Za-z0-9\./:\-_@|]+$", tok))
        fnt = en_font if is_latin else bn_font
        draw.text(
            (cursor_x, y),
            tok,
            font=fnt,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        bbox = draw.textbbox((0, 0), tok, font=fnt)
        cursor_x += bbox[2] - bbox[0]


def measure_mixed_text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    bn_font: ImageFont.FreeTypeFont,
    en_font: ImageFont.FreeTypeFont,
) -> int:
    tokens = re.findall(r"[A-Za-z0-9\./:\-_@|]+|[^A-Za-z0-9\./:\-_@|]+", text)
    total_w = 0
    for tok in tokens:
        is_latin = bool(re.match(r"^[A-Za-z0-9\./:\-_@|]+$", tok))
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


def create_thumbnail_jpg(
    title: str,
    screenshot_png: str,
    stock_clip: Optional[str],
    out_jpg: str,
    work_dir: str,
):
    bg_img = None
    if stock_clip and os.path.exists(stock_clip):
        frame_png = os.path.join(work_dir, "thumb_stock_frame.png")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", stock_clip, "-ss", "00:00:00.5", "-vframes", "1", frame_png],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if os.path.exists(frame_png):
                raw = Image.open(frame_png).convert("RGB")
                scale = max(VIDEO_WIDTH / raw.width, VIDEO_HEIGHT / raw.height)
                nw, nh = int(raw.width * scale), int(raw.height * scale)
                raw = raw.resize((nw, nh), Image.Resampling.LANCZOS)
                left = (nw - VIDEO_WIDTH) // 2
                top = (nh - VIDEO_HEIGHT) // 2
                bg_img = raw.crop((left, top, left + VIDEO_WIDTH, top + VIDEO_HEIGHT))
        except Exception:
            bg_img = None

    if bg_img is None:
        sc = Image.open(screenshot_png).convert("RGB")
        bg_img = sc.crop((0, 0, VIDEO_WIDTH, min(VIDEO_HEIGHT, sc.height)))
        if bg_img.height < VIDEO_HEIGHT:
            padded = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (15, 23, 42))
            padded.paste(bg_img, (0, 0))
            bg_img = padded

    canvas = bg_img.convert("RGBA")
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    for y in range(VIDEO_HEIGHT):
        ratio = y / VIDEO_HEIGHT
        alpha = int(215 - (ratio / 0.25) * 65) if ratio < 0.25 else (int(150 + ((ratio - 0.65) / 0.35) * 85) if ratio > 0.65 else 155)
        ov_draw.line([(0, y), (VIDEO_WIDTH, y)], fill=(10, 15, 28, alpha))

    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([18, 18, VIDEO_WIDTH - 18, VIDEO_HEIGHT - 18], outline=(245, 158, 11, 230), width=8)

    top_y = 95
    if os.path.exists(LOGO_FILE):
        try:
            logo = Image.open(LOGO_FILE).convert("RGBA")
            lw = 170
            lh = int(logo.height * (lw / logo.width))
            logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
            canvas.paste(logo, ((VIDEO_WIDTH - lw) // 2, top_y), logo)
            top_y += lh + 30
        except Exception:
            pass

    badge_w, badge_h = 460, 68
    bx0 = (VIDEO_WIDTH - badge_w) // 2
    draw.rounded_rectangle(
        [bx0, top_y, bx0 + badge_w, top_y + badge_h],
        radius=34, fill=(220, 38, 38, 245), outline=(255, 255, 255, 230), width=3,
    )
    bn_badge = find_font(BENGALI_FONT_PATHS, 34)
    en_badge = find_font(LATIN_FONT_PATHS, 34)
    badge_txt = "গুরুত্বপূর্ণ তথ্য ও নিয়ম"
    btw = measure_mixed_text_width(draw, badge_txt, bn_badge, en_badge)
    render_mixed_text_line(draw, (VIDEO_WIDTH - btw) // 2, top_y + 13, badge_txt, bn_badge, en_badge)

    clean_title = re.sub(r"#\w+", "", title).strip()
    bn_title = find_font(BENGALI_FONT_PATHS, 68)
    en_title = find_font(LATIN_FONT_PATHS, 64)
    lines = wrap_text_by_pixel(draw, clean_title, bn_title, en_title, 900)[:4]

    line_h = 94
    box_pad_y = 48
    box_h = len(lines) * line_h + box_pad_y * 2
    box_y0 = max(top_y + 115, (VIDEO_HEIGHT - box_h) // 2 + 20)
    draw.rounded_rectangle(
        [45, box_y0, VIDEO_WIDTH - 45, box_y0 + box_h],
        radius=36, fill=(15, 23, 42, 225), outline=(245, 158, 11, 245), width=5,
    )

    for idx, ln in enumerate(lines):
        lw = measure_mixed_text_width(draw, ln, bn_title, en_title)
        txt_color = (250, 204, 21, 255) if idx % 2 == 0 else (255, 255, 255, 255)
        render_mixed_text_line(draw, (VIDEO_WIDTH - lw) // 2, box_y0 + box_pad_y + idx * line_h, ln, bn_title, en_title, fill=txt_color)

    dom_w, dom_h = 620, 78
    dx0 = (VIDEO_WIDTH - dom_w) // 2
    dy0 = VIDEO_HEIGHT - 210
    draw.rounded_rectangle(
        [dx0, dy0, dx0 + dom_w, dy0 + dom_h],
        radius=38, fill=(16, 185, 129, 245), outline=(255, 255, 255, 240), width=4,
    )
    bn_dom = find_font(BENGALI_FONT_PATHS, 36)
    en_dom = find_font(LATIN_FONT_PATHS, 36)
    dom_txt = "www.homebdinfo.com"
    dtw = measure_mixed_text_width(draw, dom_txt, bn_dom, en_dom)
    render_mixed_text_line(draw, (VIDEO_WIDTH - dtw) // 2, dy0 + 17, dom_txt, bn_dom, en_dom)

    canvas.convert("RGB").save(out_jpg, "JPEG", quality=92)


def create_theme_top_banner_png(out_path: str):
    """ওয়েবসাইটের থিম কালারের (Deep Navy #0F172A + Gold #F59E0B) সাথে ম্যাচ করা স্থির টপ ব্যানার"""
    w, h = VIDEO_WIDTH, 165
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # পুরো উপরের জুড়ে থিম-ম্যাচড ব্যানার বার
    draw.rectangle([0, 0, w, h - 8], fill=(15, 23, 42, 248))
    draw.rectangle([0, h - 8, w, h], fill=(245, 158, 11, 255))

    # বাম পাশে লোগোর জন্য পরিচ্ছন্ন সার্কুলার ব্যাকগ্রাউন্ড (লোগোটি আলাদা লেয়ারে অ্যানিমেট হবে)
    draw.ellipse([48, 22, 162, 136], fill=(255, 255, 255, 250), outline=(245, 158, 11, 255), width=4)

    # ডান পাশে স্থির স্লোগান: "সঠিক তথ্য, সহজ জীবন"
    bn_font = find_font(BENGALI_FONT_PATHS, 48)
    en_font = find_font(LATIN_FONT_PATHS, 46)
    slogan_text = "সঠিক তথ্য, সহজ জীবন"
    center_x = 190 + (w - 220) // 2
    sw = measure_mixed_text_width(draw, slogan_text, bn_font, en_font)
    render_mixed_text_line(
        draw, center_x - sw // 2, 48, slogan_text, bn_font, en_font, fill=(255, 255, 255, 255)
    )

    img.save(out_path, "PNG")


def create_bottom_engagement_loop_mov(work_dir: str) -> str:
    """নিচের ফুটার লুপ: ডোমেইন টাইপ -> বিরতি -> লাইক/কমেন্ট/শেয়ার বাটন অ্যানিমেশন"""
    w, h = 880, 90
    bn_font = find_font(BENGALI_FONT_PATHS, 34)
    en_font = find_font(LATIN_FONT_PATHS, 36)
    full_domain = "www.homebdinfo.com"

    frame_paths = []
    f_idx = 0

    def save_pill(text: str, bg_color: Tuple[int, int, int, int], border_color: Tuple[int, int, int, int], txt_color=(255, 255, 255, 255)):
        nonlocal f_idx
        fp = os.path.join(work_dir, f"footer_f_{f_idx:03d}.png")
        f_idx += 1
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        if text:
            tw = measure_mixed_text_width(draw, text, bn_font, en_font)
            pw = min(w - 20, tw + 80)
            px0 = (w - pw) // 2
            draw.rounded_rectangle(
                [px0, 10, px0 + pw, h - 10],
                radius=35, fill=bg_color, outline=border_color, width=3,
            )
            render_mixed_text_line(draw, (w - tw) // 2, 23, text, bn_font, en_font, fill=txt_color)
        img.save(fp, "PNG")
        frame_paths.append(fp)

    # ১. ডোমেইন টাইপরাইটার অ্যানিমেশন (www.homebdinfo.com)
    for i in range(1, len(full_domain) + 1):
        save_pill(full_domain[:i] + "|", (15, 23, 42, 235), (16, 185, 129, 245))
    for _ in range(10):
        save_pill(full_domain, (15, 23, 42, 235), (16, 185, 129, 245))

    # ২. পরিচ্ছন্ন বিরতি (ফাঁকা ফ্রেম)
    for _ in range(14):
        save_pill("", (0, 0, 0, 0), (0, 0, 0, 0))

    # ৩. শেয়ার, কমেন্ট ও লাইক অ্যানিমেটেড বাটন পিল
    for _ in range(18):
        save_pill("👍 লাইক   |   💬 কমেন্ট   |   ↗️ শেয়ার করুন", (220, 38, 38, 240), (255, 255, 255, 235))

    # ৪. আবার সংক্ষিপ্ত বিরতি
    for _ in range(10):
        save_pill("", (0, 0, 0, 0), (0, 0, 0, 0))

    out_mov = os.path.join(work_dir, "footer_loop.mov")
    subprocess.run(
        [
            "ffmpeg", "-y", "-framerate", "8",
            "-i", os.path.join(work_dir, "footer_f_%03d.png"),
            "-c:v", "png", out_mov,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return out_mov


def create_kinetic_typography_png(text: str, seg_idx: int, is_hook: bool, out_path: str):
    """বড় চ্যানেলের মতো বোল্ড কাইনেটিক মোশন গ্রাফিক্স টাইপোগ্রাফি (কোনো ভারী বক্স ছাড়া)"""
    canvas_w = 1020
    canvas_h = 340
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_size = 64 if is_hook else 56
    bn_font = find_font(BENGALI_FONT_PATHS, font_size)
    en_font = find_font(LATIN_FONT_PATHS, font_size - 4)

    lines = wrap_text_by_pixel(draw, text, bn_font, en_font, 940)[:2]
    line_h = font_size + 24
    total_h = len(lines) * line_h
    start_y = (canvas_h - total_h) // 2

    for i, ln in enumerate(lines):
        lw = measure_mixed_text_width(draw, ln, bn_font, en_font)
        lx = (canvas_w - lw) // 2
        ly = start_y + i * line_h

        # হাই-কনট্রাস্ট হলুদ ও সাদা রঙ + মোটা কালো আউটলাইন (যেমন বড় চ্যানেলগুলোতে থাকে)
        if is_hook or (seg_idx + i) % 2 == 1:
            txt_fill = (250, 204, 21, 255)
        else:
            txt_fill = (255, 255, 255, 255)

        render_mixed_text_line(
            draw,
            lx,
            ly,
            ln,
            bn_font,
            en_font,
            fill=txt_fill,
            stroke_width=6,
            stroke_fill=(10, 15, 28, 255),
        )

    img.save(out_path, "PNG")


def create_outro_card_png(out_path: str):
    """শেষ ৩.০ সেকেন্ডের প্রফেশনাল চ্যানেল আউট্রো কার্ড (লোগো, ডোমেইন, লাইক-কমেন্ট-শেয়ার-সাবস্ক্রাইব)"""
    img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (11, 17, 32, 252))
    draw = ImageDraw.Draw(img)

    # প্রিমিয়াম গোল্ডেন বর্ডার
    draw.rectangle([28, 28, VIDEO_WIDTH - 28, VIDEO_HEIGHT - 28], outline=(245, 158, 11, 240), width=6)

    # উপরে লোগো
    y_cursor = 340
    if os.path.exists(LOGO_FILE):
        try:
            logo = Image.open(LOGO_FILE).convert("RGBA")
            lw = 240
            lh = int(logo.height * (lw / logo.width))
            logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
            draw.ellipse(
                [(VIDEO_WIDTH - lw) // 2 - 20, y_cursor - 20, (VIDEO_WIDTH + lw) // 2 + 20, y_cursor + lh + 20],
                fill=(255, 255, 255, 255),
                outline=(245, 158, 11, 255),
                width=6,
            )
            img.paste(logo, ((VIDEO_WIDTH - lw) // 2, y_cursor), logo)
            y_cursor += lh + 65
        except Exception:
            pass

    bn_big = find_font(BENGALI_FONT_PATHS, 54)
    en_big = find_font(LATIN_FONT_PATHS, 50)
    bn_med = find_font(BENGALI_FONT_PATHS, 38)
    en_med = find_font(LATIN_FONT_PATHS, 38)

    t1 = "সঠিক তথ্য, সহজ জীবন"
    w1 = measure_mixed_text_width(draw, t1, bn_big, en_big)
    render_mixed_text_line(draw, (VIDEO_WIDTH - w1) // 2, y_cursor, t1, bn_big, en_big, fill=(250, 204, 21, 255))
    y_cursor += 90

    t2 = "বিস্তারিত গাইড ও আবেদন লিংক পেতে ভিজিট করুন"
    w2 = measure_mixed_text_width(draw, t2, bn_med, en_med)
    render_mixed_text_line(draw, (VIDEO_WIDTH - w2) // 2, y_cursor, t2, bn_med, en_med, fill=(226, 232, 240, 255))
    y_cursor += 80

    # ওয়েবসাইট বাটন
    bw, bh = 720, 96
    bx0 = (VIDEO_WIDTH - bw) // 2
    draw.rounded_rectangle(
        [bx0, y_cursor, bx0 + bw, y_cursor + bh],
        radius=48, fill=(16, 185, 129, 255), outline=(255, 255, 255, 245), width=4,
    )
    dom_str = "🌐 www.homebdinfo.com"
    dw = measure_mixed_text_width(draw, dom_str, bn_med, en_med)
    render_mixed_text_line(draw, (VIDEO_WIDTH - dw) // 2, y_cursor + 24, dom_str, bn_med, en_med, fill=(255, 255, 255, 255))
    y_cursor += 160

    # লাইক, কমেন্ট, শেয়ার ও ফলো/সাবস্ক্রাইব অ্যাকশন বার
    aw, ah = 920, 110
    ax0 = (VIDEO_WIDTH - aw) // 2
    draw.rounded_rectangle(
        [ax0, y_cursor, ax0 + aw, y_cursor + ah],
        radius=30, fill=(30, 41, 59, 245), outline=(245, 158, 11, 220), width=3,
    )
    act_str = "👍 লাইক   •   💬 কমেন্ট   •   ↗️ শেয়ার   •   🔔 ফলো করুন"
    aw_txt = measure_mixed_text_width(draw, act_str, bn_med, en_med)
    render_mixed_text_line(draw, (VIDEO_WIDTH - aw_txt) // 2, y_cursor + 30, act_str, bn_med, en_med, fill=(255, 255, 255, 255))

    img.save(out_path, "PNG")


def capture_mobile_site_screenshot(post_url: str, out_png: str):
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


def download_multiple_pixabay_clips(keywords: str, work_dir: str, max_clips: int = 4) -> List[str]:
    """মাল্টি-সিন ভিডিওর জন্য Pixabay থেকে একাধিক অ্যাকশন ভিডিও ক্লিপ ডাউনলোড করে"""
    api_key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not api_key:
        return []

    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    kw_list.extend(["smartphone typing", "person working office", "digital technology"])

    downloaded = []
    used_ids = set()

    for kw in kw_list:
        if len(downloaded) >= max_clips:
            break
        try:
            url = (
                f"https://pixabay.com/api/videos/?key={api_key}"
                f"&q={requests.utils.quote(kw)}&per_page=8&safesearch=true"
            )
            resp = requests.get(url, timeout=20).json()
            hits = resp.get("hits", [])
            random.shuffle(hits)
            for hit in hits:
                vid_id = hit.get("id")
                if vid_id in used_ids:
                    continue
                videos = hit.get("videos", {})
                vid_info = videos.get("large") or videos.get("medium") or videos.get("small")
                if vid_info and vid_info.get("url"):
                    out_mp4 = os.path.join(work_dir, f"stock_{len(downloaded)}.mp4")
                    v_data = requests.get(vid_info["url"], timeout=45).content
                    with open(out_mp4, "wb") as f:
                        f.write(v_data)
                    downloaded.append(out_mp4)
                    used_ids.add(vid_id)
                    if len(downloaded) >= max_clips:
                        break
        except Exception:
            continue

    return downloaded


def build_final_video(
    screenshot_png: str,
    screenshot_h: int,
    voice_wav: str,
    kinetic_segments: List[Dict],
    stock_clips: List[str],
    thumbnail_jpg: str,
    output_mp4: str,
    work_dir: str,
):
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", voice_wav,
        ],
        capture_output=True, text=True, check=True,
    )
    voice_dur = float(probe.stdout.strip())
    # ভয়েস শেষ হওয়ার পর ৩.০ সেকেন্ডের আউট্রো কার্ড থাকবে
    total_duration = voice_dur + OUTRO_DURATION

    top_banner_png = os.path.join(work_dir, "theme_top_banner.png")
    outro_png = os.path.join(work_dir, "outro_card.png")
    create_theme_top_banner_png(top_banner_png)
    create_outro_card_png(outro_png)
    footer_mov = create_bottom_engagement_loop_mov(work_dir)

    kinetic_pngs = []
    for idx, seg in enumerate(kinetic_segments):
        kp = os.path.join(work_dir, f"kin_{idx}.png")
        create_kinetic_typography_png(seg["text"], idx, seg["is_hook"], kp)
        kinetic_pngs.append((kp, seg["start"], seg["end"]))

    # ইনপুট ফাইলসমূহ
    inputs = [
        "-loop", "1", "-t", f"{total_duration:.2f}", "-i", screenshot_png,     # স্ক্রলিং ওয়েবসাইট ব্যাকগ্রাউন্ড
        "-i", voice_wav,                                                       # ভয়েসওভার অডিও
        "-i", top_banner_png,                                                  # থিম-ম্যাচড টপ ব্যানার
        "-stream_loop", "-1", "-t", f"{total_duration:.2f}", "-i", footer_mov, # নিচের ডোমেইন ও শেয়ার/কমেন্ট লুপ
        "-i", outro_png,                                                       # শেষ ৩ সেকেন্ডের আউট্রো কার্ড
        "-loop", "1", "-t", f"{COVER_DURATION:.2f}", "-i", thumbnail_jpg,      # শুরুর কভার ফ্রেম
    ]

    next_idx = 6
    bgm_idx = None
    if os.path.exists(BGM_FILE):
        inputs.extend(["-stream_loop", "-1", "-t", f"{total_duration:.2f}", "-i", BGM_FILE])
        bgm_idx = next_idx
        next_idx += 1

    logo_idx = None
    if os.path.exists(LOGO_FILE):
        inputs.extend(["-loop", "1", "-t", f"{total_duration:.2f}", "-i", LOGO_FILE])
        logo_idx = next_idx
        next_idx += 1

    stock_indices = []
    for sc_path in stock_clips:
        inputs.extend(["-stream_loop", "-1", "-t", f"{total_duration:.2f}", "-i", sc_path])
        stock_indices.append(next_idx)
        next_idx += 1

    kin_start_idx = next_idx
    for kp, _, _ in kinetic_pngs:
        inputs.extend(["-loop", "1", "-t", f"{total_duration:.2f}", "-i", kp])

    scroll_dist = max(0, screenshot_h - VIDEO_HEIGHT)
    filter_parts = []

    # বেস লেয়ার: ওয়েবসাইট প্রুফ স্ক্রোলিং (মাঝে মাঝে দেখা যাবে)
    filter_parts.append(
        f"[0:v]crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:0:'min({scroll_dist}, {scroll_dist}*t/{max(1.0, voice_dur):.2f})',"
        f"scale=w='trunc(iw*(1+0.03*(0.5-0.5*cos(2*PI*t/8)))/2)*2':h='trunc(ih*(1+0.03*(0.5-0.5*cos(2*PI*t/8)))/2)*2':eval=frame,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:'(iw-{VIDEO_WIDTH})/2':'(ih-{VIDEO_HEIGHT})/2',"
        f"eq=brightness=-0.06:contrast=1.08[base_site]"
    )
    last_v = "base_site"

    # মাল্টি-সিন ডাইনামিক কাট-অ্যাওয়ে: প্রথম ৪ সেকেন্ড হুক ক্লিপ + পুরো ভিডিওতে ফুল-স্ক্রিন অ্যাকশন ক্লিপ ও ওয়েবসাইটের পরিবর্তন
    if stock_indices:
        # সিন ১: প্রথম ৪.০ সেকেন্ড হাই-ইম্প্যাক্ট হুক ক্লিপ
        s0 = stock_indices[0]
        filter_parts.append(
            f"[{s0}:v]scale=1160:2062:force_original_aspect_ratio=increase,crop=1160:2062,"
            f"scale=w='trunc(1080*(1+0.07*(t/{HOOK_DURATION:.2f}))/2)*2':h='trunc(1920*(1+0.07*(t/{HOOK_DURATION:.2f}))/2)*2':eval=frame,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:'(iw-{VIDEO_WIDTH})/2':'(ih-{VIDEO_HEIGHT})/2',"
            f"eq=brightness=-0.08:contrast=1.10[scene_hook]"
        )
        filter_parts.append(
            f"[{last_v}][scene_hook]overlay=0:0:enable='between(t,0,{HOOK_DURATION:.2f})'[v_s0]"
        )
        last_v = "v_s0"

        # সিন ২, ৩, ৪: ৪ সেকেন্ড পর থেকে প্রতি ৪.৫ সেকেন্ড ওয়েবসাইট ও ৪.৫ সেকেন্ড ফুল-স্ক্রিন অ্যাকশন ক্লিপ পর্যায়ক্রমে চলবে
        t_mark = HOOK_DURATION + 4.2  # প্রথম ৪.২ সেকেন্ড ওয়েবসাইট দেখাবে
        clip_cursor = 1
        while t_mark < voice_dur - 1.5:
            c_idx = stock_indices[clip_cursor % len(stock_indices)]
            clip_end = min(voice_dur, t_mark + 4.5)
            sc_lbl = f"scene_act_{clip_cursor}"
            out_lbl = f"v_act_{clip_cursor}"
            filter_parts.append(
                f"[{c_idx}:v]scale=1160:2062:force_original_aspect_ratio=increase,crop=1160:2062,"
                f"scale=w='trunc(1080*(1+0.05*sin(2*PI*t/6))/2)*2':h='trunc(1920*(1+0.05*sin(2*PI*t/6))/2)*2':eval=frame,"
                f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:'(iw-{VIDEO_WIDTH})/2':'(ih-{VIDEO_HEIGHT})/2',"
                f"eq=brightness=-0.08:contrast=1.10[{sc_lbl}]"
            )
            filter_parts.append(
                f"[{last_v}][{sc_lbl}]overlay=0:0:enable='between(t,{t_mark:.2f},{clip_end:.2f})'[{out_lbl}]"
            )
            last_v = out_lbl
            t_mark = clip_end + 4.2  # মাঝে ৪.২ সেকেন্ড ওয়েবসাইট স্ক্রোল দেখাবে
            clip_cursor += 1

    # নিচের অংশে ডার্ক ভিনিয়েট গ্রেডিয়েন্ট (যাতে কাইনেটিক টেক্সট ও ফুটার স্পষ্ট ফুটে ওঠে)
    filter_parts.append(
        f"[{last_v}]drawbox=x=0:y=1150:w={VIDEO_WIDTH}:h=770:color=black@0.42:t=fill[v_grad]"
    )
    last_v = "v_grad"

    # উপরের স্থির থিম-ম্যাচড ব্র্যান্ড ব্যানার
    filter_parts.append(f"[{last_v}][2:v]overlay=0:0[v_banner]")
    last_v = "v_banner"

    # ব্যানারের বাম পাশে অ্যানিমেটেড লোগো (শুধু লোগোটি পালস অ্যানিমেশন করবে)
    if logo_idx is not None:
        filter_parts.append(
            f"[{logo_idx}:v]scale=w='trunc(88*(1+0.09*sin(2*PI*t/2.0))/2)*2':h='trunc(88*(1+0.09*sin(2*PI*t/2.0))/2)*2':eval=frame[logo_anim]"
        )
        filter_parts.append(
            f"[{last_v}][logo_anim]overlay='48+(114-w)/2':'22+(114-h)/2'[v_logo]"
        )
        last_v = "v_logo"

    # কাইনেটিক মোশন গ্রাফিক্স টাইপোগ্রাফি (পপ-ইন জুম মোশনসহ)
    for i, (_, st, en) in enumerate(kinetic_pngs):
        k_in = kin_start_idx + i
        k_lbl = f"kin_anim_{i}"
        out_lbl = f"v_kin_{i}"
        filter_parts.append(
            f"[{k_in}:v]scale=w='trunc(1020*(0.92+0.08*min(1,(t-{st:.2f})/0.18))/2)*2':h='trunc(340*(0.92+0.08*min(1,(t-{st:.2f})/0.18))/2)*2':eval=frame[{k_lbl}]"
        )
        filter_parts.append(
            f"[{last_v}][{k_lbl}]overlay=(W-w)/2:(1340-h/2):enable='between(t,{st:.2f},{en:.2f})'[{out_lbl}]"
        )
        last_v = out_lbl

    # নিচের অল্টারনেটিং ফুটার (ডোমেইন টাইপ -> বিরতি -> লাইক/কমেন্ট/শেয়ার পিল)
    filter_parts.append(
        f"[{last_v}][3:v]overlay=(W-w)/2:1730:enable='between(t,0,{voice_dur:.2f})'[v_footer]"
    )
    last_v = "v_footer"

    # প্রগ্রেস বার
    filter_parts.append(
        f"[{last_v}]drawbox=x=0:y=165:w='iw*(t/{total_duration:.2f})':h=8:color=#10B981@0.95:t=fill[v_prog]"
    )
    last_v = "v_prog"

    # শেষ ৩.০ সেকেন্ড প্রফেশনাল আউট্রো কার্ড
    filter_parts.append(
        f"[{last_v}][4:v]overlay=0:0:enable='between(t,{voice_dur:.2f},{total_duration:.2f})'[v_outro]"
    )
    last_v = "v_outro"

    # একদম শুরুতে ০.৩৫ সেকেন্ড কভার থাম্বনেইল ফ্রেম
    filter_parts.append(
        f"[5:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}[thumb_cover];"
        f"[{last_v}][thumb_cover]overlay=0:0:enable='between(t,0,{COVER_DURATION:.2f})'[v_final]"
    )

    # অডিও মিক্সিং (ভয়েস + ১৬% ব্যাকগ্রাউন্ড মিউজিক)
    if bgm_idx is not None:
        filter_parts.append(
            f"[{bgm_idx}:a]volume={BGM_VOLUME}[bgm_low];"
            f"[1:a][bgm_low]amix=inputs=2:duration=longest:dropout_transition=2[a_final]"
        )
    else:
        filter_parts.append("[1:a]apad[a_final]")

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[v_final]", "-map", "[a_final]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "22",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-r", str(FPS), "-t", f"{total_duration:.2f}",
        output_mp4,
    ]
    subprocess.run(cmd, check=True)


def upload_to_github_release(video_path: str, thumb_path: str, row_num: int, title: str) -> Tuple[str, str]:
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
        "body": "Automated Bangla YouTube Shorts video and thumbnail release.",
        "draft": False,
        "prerelease": False,
    }
    rel_resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases",
        headers=headers, json=release_payload, timeout=30,
    )
    rel_resp.raise_for_status()
    rel_data = rel_resp.json()
    release_id = str(rel_data["id"])
    upload_url = rel_data["upload_url"].split("{")[0]

    file_name = os.path.basename(video_path)
    with open(video_path, "rb") as f:
        up_resp = requests.post(
            f"{upload_url}?name={file_name}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"},
            data=f, timeout=120,
        )
    up_resp.raise_for_status()
    download_url = up_resp.json()["browser_download_url"]

    if thumb_path and os.path.exists(thumb_path):
        thumb_name = os.path.basename(thumb_path)
        with open(thumb_path, "rb") as tf:
            t_resp = requests.post(
                f"{upload_url}?name={thumb_name}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
                data=tf, timeout=60,
            )
        t_resp.raise_for_status()

    return release_id, download_url


def send_telegram_for_approval(video_path: str, thumb_path: str, row_num: int, title: str, post_url: str) -> Tuple[str, str]:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN বা TELEGRAM_CHAT_ID পাওয়া যায়নি।")

    caption = (
        f"🎬 <b>নতুন Bangla Shorts প্রস্তুত!</b>\n\n"
        f"📌 <b>শিরোনাম:</b> {title}\n"
        f"🔗 <b>পোস্ট:</b> {post_url}\n"
        f"📊 <b>Sheet Row:</b> {row_num}\n\n"
        f"💡 <i>Approve দিলে ইনস্ট্যান্ট পোস্ট হবে, অথবা Schedule বাটনে সময় ঠিক করুন।</i>"
    )
    reply_markup = json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"APPROVE_{row_num}"},
                    {"text": "⏰ Schedule", "callback_data": f"SCHEDULE_{row_num}"},
                ],
                [
                    {"text": "❌ Reject", "callback_data": f"REJECT_{row_num}"},
                ]
            ]
        }
    )

    files = {"video": open(video_path, "rb")}
    if thumb_path and os.path.exists(thumb_path):
        files["thumbnail"] = open(thumb_path, "rb")

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendVideo",
            data={
                "chat_id": chat_id,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            },
            files=files,
            timeout=120,
        )
    finally:
        for f_obj in files.values():
            f_obj.close()

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
            thumb_jpg = os.path.join(work_dir, f"thumb_row_{target_row}.jpg")
            final_mp4 = os.path.join(work_dir, f"bangla_short_row_{target_row}.mp4")

            print("1. Capturing mobile website screenshot...")
            sc_height = capture_mobile_site_screenshot(post_url, screenshot_png)

            print("2. Generating studio-quality voiceover & kinetic segments...")
            voice_wav, kinetic_segments = generate_natural_voiceover(script, work_dir)

            print("3. Fetching multiple action video clips for dynamic multi-scene switching...")
            stock_clips = download_multiple_pixabay_clips(keywords, work_dir, max_clips=4)

            print("4. Generating attractive branded thumbnail...")
            first_clip = stock_clips[0] if stock_clips else None
            create_thumbnail_jpg(yt_title, screenshot_png, first_clip, thumb_jpg, work_dir)

            print("5. Rendering final multi-scene professional short...")
            build_final_video(
                screenshot_png=screenshot_png,
                screenshot_h=sc_height,
                voice_wav=voice_wav,
                kinetic_segments=kinetic_segments,
                stock_clips=stock_clips,
                thumbnail_jpg=thumb_jpg,
                output_mp4=final_mp4,
                work_dir=work_dir,
            )

            print("6. Uploading video and thumbnail to GitHub Releases...")
            rel_id, rel_url = upload_to_github_release(final_mp4, thumb_jpg, target_row, yt_title)

            print("7. Sending to Telegram for approval...")
            tg_file_id, tg_msg_id = send_telegram_for_approval(final_mp4, thumb_jpg, target_row, yt_title, post_url)

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
