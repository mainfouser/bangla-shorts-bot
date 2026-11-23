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
# ভয়েস কনফিগারেশন (স্মার্ট, ক্রিস্প ও ন্যাচারাল পুরুষ কণ্ঠ)
# ============================================================
VOICE_NAME = "bn-BD-PradeepNeural"

# গিটহাব ও ফাইল কনফিগারেশন
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot")
SHEET_NAME = "Shorts_Queue"
BGM_FILE = "bgm.mp3"
LOGO_FILE = "logo.png"
BGM_VOLUME = 0.16

# ভিডিও ডাইমেনশন
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 30
MAX_SCROLL_PX = 2600
HOOK_DURATION = 6.0   # প্রথম ৬.০ সেকেন্ড ফুল-স্ক্রিন হুক ক্লিপ
COVER_DURATION = 0.4  # ইউটিউব শর্টসের শুরুতে ০.৪ সেকেন্ডের থাম্বনেইল কভার ফ্রেম

# ফন্ট পাথ (Ubuntu / GitHub Actions - নিশ্চিত বাংলা ফন্ট লোডিং)
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
    # বাংলা উচ্চারণ আরও স্বাভাবিক করতে বিরামচিহ্নের সামান্য বিরতি অপ্টিমাইজেশন
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_sentences_for_tts(script: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"(?<=[।!?\.])\s+", script) if p.strip()]
    return parts if parts else [script]


def get_prosody_for_sentence(sentence: str, idx: int, total: int) -> Tuple[str, str, float]:
    """স্মার্ট, প্রাণবন্ত ও প্রফেশনাল প্রেজেন্টার টোনের জন্য ডাইনামিক স্পিড ও পিচ"""
    if idx == 0:
        # শুরুতে এনার্জেটিক হুক টোন
        return "+14%", "+2Hz", 0.22
    if "?" in sentence or "কীভাবে" in sentence or "কেন" in sentence or "কী" in sentence:
        # প্রশ্নবোধক বাক্যে স্বাভাবিক কৌতূহলী ওঠানামা
        return "+11%", "+4Hz", 0.24
    if idx == total - 1:
        # শেষে আত্মবিশ্বাসী কল-টু-অ্যাকশন ফিনিশিং
        return "+9%", "-1Hz", 0.25
    # মূল তথ্যের বাক্যগুলোতে স্মার্ট ও সাবলীল গতি
    return "+12%", "+1Hz", 0.20


async def _synthesize_chunk(text: str, out_path: str, rate: str, pitch: str):
    communicate = edge_tts.Communicate(text=text, voice=VOICE_NAME, rate=rate, pitch=pitch)
    await communicate.save(out_path)


def generate_natural_voiceover(script: str, work_dir: str) -> Tuple[str, List[Dict]]:
    cleaned = clean_voiceover_script(script)
    sentences = split_sentences_for_tts(cleaned)
    total = len(sentences)

    segment_files = []
    presentation_slides = []
    current_time = 0.0

    for idx, sentence in enumerate(sentences):
        rate, pitch, pause_sec = get_prosody_for_sentence(sentence, idx, total)
        raw_mp3 = os.path.join(work_dir, f"tts_{idx}.mp3")
        wav_seg = os.path.join(work_dir, f"tts_{idx}.wav")
        sil_wav = os.path.join(work_dir, f"sil_{idx}.wav")

        asyncio.run(_synthesize_chunk(sentence, raw_mp3, rate, pitch))

        # স্টুডিও-কোয়ালিটি ভয়েস প্রসেসিং (নয়েজ/হাম রিমুভাল + ক্রিস্প ভোকাল ক্ল্যারিটি + নরমালইজেশন)
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", raw_mp3,
                "-af", "highpass=f=85,equalizer=f=3200:t=q:w=1.5:g=3,acompressor=threshold=-18dB:ratio=2.5:attack=15:release=120,loudnorm=I=-15:TP=-1.5:LRA=9",
                "-ar", "44100", "-ac", "2", wav_seg
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

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

        chunks = split_presentation_chunks(sentence, max_chars=52)
        total_chars = sum(len(c) for c in chunks) or 1
        t_cursor = current_time
        for c_i, ch in enumerate(chunks):
            ch_dur = speech_dur * (len(ch) / total_chars)
            if idx == 0:
                slide_tag = "জানুন আসল বিষয়"
            elif idx == total - 1:
                slide_tag = "বিস্তারিত গাইড"
            elif "?" in sentence:
                slide_tag = "গুরুত্বপূর্ণ প্রশ্ন"
            else:
                slide_tag = f"মূল পয়েন্ট ০{idx}"

            presentation_slides.append(
                {
                    "tag": slide_tag,
                    "text": ch,
                    "start": t_cursor,
                    "end": t_cursor + ch_dur + (pause_sec if c_i == len(chunks) - 1 else 0.0),
                }
            )
            t_cursor += ch_dur

        segment_files.append(wav_seg)
        current_time += speech_dur

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
    return final_voice_wav, presentation_slides


def split_presentation_chunks(sentence: str, max_chars: int = 52) -> List[str]:
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
    tokens = re.findall(r"[A-Za-z0-9\./:\-_@|]+|[^A-Za-z0-9\./:\-_@|]+", text)
    cursor_x = x
    for tok in tokens:
        is_latin = bool(re.match(r"^[A-Za-z0-9\./:\-_@|]+$", tok))
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
    """আকর্ষণীয় হুক শিরোনাম, লোগো ও গ্রেডিয়েন্ট ইফেক্টসহ প্রফেশনাল থাম্বনেইল তৈরি"""
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
        if ratio < 0.25:
            alpha = int(215 - (ratio / 0.25) * 65)
        elif ratio > 0.65:
            alpha = int(150 + ((ratio - 0.65) / 0.35) * 85)
        else:
            alpha = 155
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
        radius=34,
        fill=(220, 38, 38, 245),
        outline=(255, 255, 255, 230),
        width=3,
    )
    bn_badge_font = find_font(BENGALI_FONT_PATHS, 34)
    en_badge_font = find_font(LATIN_FONT_PATHS, 34)
    badge_txt = "গুরুত্বপূর্ণ তথ্য ও নিয়ম"
    btw = measure_mixed_text_width(draw, badge_txt, bn_badge_font, en_badge_font)
    render_mixed_text_line(draw, (VIDEO_WIDTH - btw) // 2, top_y + 13, badge_txt, bn_badge_font, en_badge_font)

    clean_title = re.sub(r"#\w+", "", title).strip()
    bn_title_font = find_font(BENGALI_FONT_PATHS, 68)
    en_title_font = find_font(LATIN_FONT_PATHS, 64)

    lines = wrap_text_by_pixel(draw, clean_title, bn_title_font, en_title_font, 900)
    if len(lines) > 4:
        lines = lines[:4]
        lines[-1] = lines[-1][:22] + "..."

    line_h = 94
    box_pad_y = 48
    box_h = len(lines) * line_h + box_pad_y * 2
    box_y0 = max(top_y + 115, (VIDEO_HEIGHT - box_h) // 2 + 20)
    box_y1 = box_y0 + box_h

    draw.rounded_rectangle(
        [45, box_y0, VIDEO_WIDTH - 45, box_y1],
        radius=36,
        fill=(15, 23, 42, 225),
        outline=(245, 158, 11, 245),
        width=5,
    )

    for idx, ln in enumerate(lines):
        lw = measure_mixed_text_width(draw, ln, bn_title_font, en_title_font)
        lx = (VIDEO_WIDTH - lw) // 2
        ly = box_y0 + box_pad_y + idx * line_h
        txt_color = (250, 204, 21, 255) if idx % 2 == 0 else (255, 255, 255, 255)
        render_mixed_text_line(draw, lx, ly, ln, bn_title_font, en_title_font, fill=txt_color)

    dom_w, dom_h = 620, 78
    dx0 = (VIDEO_WIDTH - dom_w) // 2
    dy0 = VIDEO_HEIGHT - 210
    draw.rounded_rectangle(
        [dx0, dy0, dx0 + dom_w, dy0 + dom_h],
        radius=38,
        fill=(16, 185, 129, 245),
        outline=(255, 255, 255, 240),
        width=4,
    )
    bn_dom = find_font(BENGALI_FONT_PATHS, 36)
    en_dom = find_font(LATIN_FONT_PATHS, 36)
    dom_txt = "www.homebdinfo.com"
    dtw = measure_mixed_text_width(draw, dom_txt, bn_dom, en_dom)
    render_mixed_text_line(draw, (VIDEO_WIDTH - dtw) // 2, dy0 + 17, dom_txt, bn_dom, en_dom)

    canvas.convert("RGB").save(out_jpg, "JPEG", quality=92)


def create_top_header_banner_png(out_path: str):
    """উপরের প্রফেশনাল ব্র্যান্ড ব্যানার: বামে লোগোর জায়গা + উপরে 'সঠিক তথ্য, সহজ জীবন' + নিচে ডোমেইন বার"""
    w, h = 1040, 220
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle(
        [6, 6, w - 6, h - 6],
        radius=34,
        fill=(11, 17, 32, 242),
        outline=(245, 158, 11, 235),
        width=4,
    )

    # বাম পাশে লোগোর জন্য সার্কুলার গ্লো ব্যাকগ্রাউন্ড
    draw.ellipse([26, 26, 194, 194], fill=(255, 255, 255, 245), outline=(16, 185, 129, 255), width=5)

    # ডান পাশে উপরে স্লোগান: "সঠিক তথ্য, সহজ জীবন"
    bn_font = find_font(BENGALI_FONT_PATHS, 46)
    en_font = find_font(LATIN_FONT_PATHS, 44)
    slogan_text = "সঠিক তথ্য, সহজ জীবন"
    right_area_center = 215 + (w - 235) // 2
    sw = measure_mixed_text_width(draw, slogan_text, bn_font, en_font)
    render_mixed_text_line(
        draw,
        right_area_center - sw // 2,
        34,
        slogan_text,
        bn_font,
        en_font,
        fill=(250, 204, 21, 255),
    )

    # নিচে ডোমেইন টাইপরাইটার বক্স ব্যাকগ্রাউন্ড
    box_x0, box_y0 = 240, 115
    box_x1, box_y1 = w - 40, 185
    draw.rounded_rectangle(
        [box_x0, box_y0, box_x1, box_y1],
        radius=28,
        fill=(16, 185, 129, 45),
        outline=(16, 185, 129, 220),
        width=3,
    )

    img.save(out_path, "PNG")


def create_typewriter_frames(work_dir: str) -> List[str]:
    """'www.homebdinfo.com' টাইপরাইটার অ্যানিমেশনের প্রতিটি ফ্রেম PNG তৈরি"""
    full_domain = "www.homebdinfo.com"
    w, h = 740, 66
    bn_font = find_font(BENGALI_FONT_PATHS, 34)
    en_font = find_font(LATIN_FONT_PATHS, 36)

    frames = []
    steps = [full_domain[:i] + "|" for i in range(1, len(full_domain) + 1)]
    steps.extend([full_domain + "|", full_domain, full_domain + "|", full_domain, full_domain + "|"])

    for idx, txt in enumerate(steps):
        fp = os.path.join(work_dir, f"tw_frame_{idx}.png")
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        tw = measure_mixed_text_width(draw, txt, bn_font, en_font)
        render_mixed_text_line(
            draw,
            (w - tw) // 2,
            10,
            txt,
            bn_font,
            en_font,
            fill=(255, 255, 255, 255),
        )
        img.save(fp, "PNG")
        frames.append(fp)

    return frames


def create_presentation_card_png(tag: str, text: str, card_idx: int, out_path: str):
    """সাবটাইটেলের বদলে প্রফেশনাল প্রেজেন্টেশন মোশন গ্রাফিক্স ইনফো-কার্ড তৈরি"""
    canvas_w = 1020
    canvas_h = 340
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bn_font = find_font(BENGALI_FONT_PATHS, 44)
    en_font = find_font(LATIN_FONT_PATHS, 40)
    tag_bn_font = find_font(BENGALI_FONT_PATHS, 28)
    tag_en_font = find_font(LATIN_FONT_PATHS, 28)

    lines = wrap_text_by_pixel(draw, text, bn_font, en_font, 890)
    if len(lines) > 3:
        lines = lines[:3]

    line_height = 62
    pad_top = 64
    pad_bottom = 32
    card_w = 980
    card_h = pad_top + len(lines) * line_height + pad_bottom

    x0 = (canvas_w - card_w) // 2
    y0 = (canvas_h - card_h) // 2
    x1 = x0 + card_w
    y1 = y0 + card_h

    draw.rounded_rectangle(
        [x0 + 6, y0 + 8, x1 + 6, y1 + 8],
        radius=28,
        fill=(0, 0, 0, 140),
    )
    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=28,
        fill=(15, 23, 42, 235),
        outline=(245, 158, 11, 230),
        width=3,
    )

    accent_color = (16, 185, 129, 255) if card_idx % 2 == 0 else (245, 158, 11, 255)
    draw.rounded_rectangle(
        [x0 + 14, y0 + 22, x0 + 28, y1 - 22],
        radius=7,
        fill=accent_color,
    )

    tag_w = measure_mixed_text_width(draw, tag, tag_bn_font, tag_en_font) + 44
    tx0 = x0 + 48
    ty0 = y0 + 14
    draw.rounded_rectangle(
        [tx0, ty0, tx0 + tag_w, ty0 + 42],
        radius=20,
        fill=accent_color,
    )
    tag_text_color = (15, 23, 42, 255)
    render_mixed_text_line(draw, tx0 + 22, ty0 + 5, tag, tag_bn_font, tag_en_font, fill=tag_text_color)

    for i, ln in enumerate(lines):
        lx = x0 + 50
        ly = y0 + pad_top + i * line_height
        render_mixed_text_line(draw, lx, ly, ln, bn_font, en_font, fill=(255, 255, 255, 255))

    img.save(out_path, "PNG")


def create_cta_banner_png(out_path: str):
    """শেষ ৫ সেকেন্ডের কল-টু-অ্যাকশন (CTA) কার্ড PNG"""
    w, h = 960, 175
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bn_font = find_font(BENGALI_FONT_PATHS, 35)
    en_font = find_font(LATIN_FONT_PATHS, 37)

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

    render_mixed_text_line(draw, (w - w1) // 2, 30, line1, bn_font, en_font, fill=(17, 24, 39, 255))
    render_mixed_text_line(draw, (w - w2) // 2, 92, line2, bn_font, en_font, fill=(17, 24, 39, 255))
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
                vid_info = videos.get("large") or videos.get("medium") or videos.get("small")
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
    presentation_slides: List[Dict],
    stock_clip: Optional[str],
    thumbnail_jpg: str,
    output_mp4: str,
    work_dir: str,
):
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

    top_banner_png = os.path.join(work_dir, "top_header_banner.png")
    cta_banner_png = os.path.join(work_dir, "cta_banner.png")
    create_top_header_banner_png(top_banner_png)
    create_cta_banner_png(cta_banner_png)

    create_typewriter_frames(work_dir)
    tw_loop_mov = os.path.join(work_dir, "typewriter_loop.mov")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            "7",
            "-i",
            os.path.join(work_dir, "tw_frame_%d.png"),
            "-c:v",
            "png",
            tw_loop_mov,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    slide_pngs = []
    for idx, seg in enumerate(presentation_slides):
        sp = os.path.join(work_dir, f"slide_{idx}.png")
        create_presentation_card_png(seg["tag"], seg["text"], idx, sp)
        slide_pngs.append((sp, seg["start"], seg["end"]))

    inputs = [
        "-loop", "1", "-t", f"{duration:.2f}", "-i", screenshot_png,
        "-i", voice_wav,
        "-i", top_banner_png,
        "-stream_loop", "-1", "-t", f"{duration:.2f}", "-i", tw_loop_mov,
        "-i", cta_banner_png,
        "-loop", "1", "-t", f"{COVER_DURATION:.2f}", "-i", thumbnail_jpg,
    ]

    next_idx = 6
    bgm_idx = None
    if os.path.exists(BGM_FILE):
        inputs.extend(["-stream_loop", "-1", "-t", f"{duration:.2f}", "-i", BGM_FILE])
        bgm_idx = next_idx
        next_idx += 1

    logo_idx = None
    if os.path.exists(LOGO_FILE):
        inputs.extend(["-loop", "1", "-t", f"{duration:.2f}", "-i", LOGO_FILE])
        logo_idx = next_idx
        next_idx += 1

    stock_idx = None
    if stock_clip and os.path.exists(stock_clip):
        inputs.extend(["-stream_loop", "-1", "-t", f"{HOOK_DURATION:.2f}", "-i", stock_clip])
        stock_idx = next_idx
        next_idx += 1

    slide_start_idx = next_idx
    for sp, _, _ in slide_pngs:
        inputs.extend(["-loop", "1", "-t", f"{duration:.2f}", "-i", sp])

    scroll_dist = max(0, screenshot_h - VIDEO_HEIGHT)
    filter_parts = []

    site_scroll_dur = max(1.0, duration - HOOK_DURATION) if stock_idx is not None else duration
    scroll_start_t = HOOK_DURATION if stock_idx is not None else 0.0

    filter_parts.append(
        f"[0:v]crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:0:'min({scroll_dist}, {scroll_dist}*max(0, t-{scroll_start_t:.2f})/{site_scroll_dur:.2f})',"
        f"scale=w='trunc(iw*(1+0.035*(0.5-0.5*cos(2*PI*t/9)))/2)*2':h='trunc(ih*(1+0.035*(0.5-0.5*cos(2*PI*t/9)))/2)*2':eval=frame,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:'(iw-{VIDEO_WIDTH})/2':'(ih-{VIDEO_HEIGHT})/2',"
        f"eq=brightness=-0.04:contrast=1.06[site_bg]"
    )
    last_v = "site_bg"

    if stock_idx is not None:
        filter_parts.append(
            f"[{stock_idx}:v]scale=1160:2062:force_original_aspect_ratio=increase,"
            f"crop=1160:2062,"
            f"scale=w='trunc(1080*(1+0.06*(t/{HOOK_DURATION:.2f}))/2)*2':h='trunc(1920*(1+0.06*(t/{HOOK_DURATION:.2f}))/2)*2':eval=frame,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:'(iw-{VIDEO_WIDTH})/2':'(ih-{VIDEO_HEIGHT})/2',"
            f"eq=brightness=-0.05:contrast=1.08[hook_full]"
        )
        filter_parts.append(
            f"[{last_v}][hook_full]overlay=0:0:enable='between(t,0,{HOOK_DURATION:.2f})'[v_hooked]"
        )
        last_v = "v_hooked"

    filter_parts.append(f"[{last_v}][2:v]overlay=(W-w)/2:36[v_banner]")
    last_v = "v_banner"

    if logo_idx is not None:
        filter_parts.append(
            f"[{logo_idx}:v]scale=w='trunc(124*(1+0.08*sin(2*PI*t/2.2))/2)*2':h='trunc(124*(1+0.08*sin(2*PI*t/2.2))/2)*2':eval=frame[logo_anim]"
        )
        filter_parts.append(
            f"[{last_v}][logo_anim]overlay='46+(168-w)/2':'62+(168-h)/2'[v_logo]"
        )
        last_v = "v_logo"

    filter_parts.append(f"[{last_v}][3:v]overlay=260:153[v_tw]")
    last_v = "v_tw"

    filter_parts.append(
        f"[{last_v}]drawbox=x=0:y=0:w='iw*(t/{duration:.2f})':h=12:color=#10B981@0.95:t=fill[v_prog]"
    )
    last_v = "v_prog"

    for i, (_, st, en) in enumerate(slide_pngs):
        s_in = slide_start_idx + i
        sc_lbl = f"sl_anim_{i}"
        out_lbl = f"v_sl_{i}"
        filter_parts.append(
            f"[{s_in}:v]scale=w='trunc(1020*(0.96+0.04*min(1,(t-{st:.2f})/0.35))/2)*2':h='trunc(340*(0.96+0.04*min(1,(t-{st:.2f})/0.35))/2)*2':eval=frame[{sc_lbl}]"
        )
        filter_parts.append(
            f"[{last_v}][{sc_lbl}]overlay=(W-w)/2:'1240+45*(1-min(1,(t-{st:.2f})/0.35))':enable='between(t,{st:.2f},{en:.2f})'[{out_lbl}]"
        )
        last_v = out_lbl

    cta_start = max(0.0, duration - 5.2)
    filter_parts.append(
        f"[{last_v}][4:v]overlay=(W-w)/2:1645:enable='between(t,{cta_start:.2f},{duration:.2f})'[v_with_cta]"
    )
    last_v = "v_with_cta"

    filter_parts.append(
        f"[5:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}[thumb_cover];"
        f"[{last_v}][thumb_cover]overlay=0:0:enable='between(t,0,{COVER_DURATION:.2f})'[v_final]"
    )

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

    if thumb_path and os.path.exists(thumb_path):
        thumb_name = os.path.basename(thumb_path)
        with open(thumb_path, "rb") as tf:
            t_resp = requests.post(
                f"{upload_url}?name={thumb_name}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "image/jpeg",
                },
                data=tf,
                timeout=60,
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
        f"💡 <i>Approve দিলে ৩০ মিনিটের মধ্যে পোস্ট হবে, অথবা Schedule বাটনে সময় ঠিক করুন।</i>"
    )
    reply_markup = json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve (30m)", "callback_data": f"APPROVE_{row_num}"},
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
            stock_mp4 = os.path.join(work_dir, "stock.mp4")
            thumb_jpg = os.path.join(work_dir, f"thumb_row_{target_row}.jpg")
            final_mp4 = os.path.join(work_dir, f"bangla_short_row_{target_row}.mp4")

            print("1. Capturing mobile website screenshot...")
            sc_height = capture_mobile_site_screenshot(post_url, screenshot_png)

            print("2. Generating smart studio-processed voiceover & presentation slides...")
            voice_wav, presentation_slides = generate_natural_voiceover(script, work_dir)

            print("3. Fetching stock video clip for 6.0s full-screen hook...")
            downloaded_clip = download_pixabay_clip(keywords, stock_mp4)

            print("4. Generating attractive branded thumbnail...")
            create_thumbnail_jpg(yt_title, screenshot_png, downloaded_clip, thumb_jpg, work_dir)

            print("5. Rendering final branded promotional video...")
            build_final_video(
                screenshot_png=screenshot_png,
                screenshot_h=sc_height,
                voice_wav=voice_wav,
                presentation_slides=presentation_slides,
                stock_clip=downloaded_clip,
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
