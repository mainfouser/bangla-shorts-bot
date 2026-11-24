import os
import re
import json
import math
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

VOICE_NAME = "bn-BD-PradeepNeural"
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot")
SHEET_NAME = "Shorts_Queue"
BGM_FILE = "bgm.mp3"
LOGO_FILE = "logo.png"
BGM_VOLUME = 0.17

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 30
MAX_SCROLL_PX = 2600
HOOK_DURATION = 4.0
COVER_DURATION = 0.4
OUTRO_DURATION = 3.0

BENGALI_FONT_PATHS = [
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansBengali-SemiBold.ttf",
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
    text = re.sub(r"হোম\s*বিডি\s*ইনফো\s*ডট\s*কম\s*\(?homebdinfo\.com\)?", "হোম বিডি ইনফো ডট কম", text, flags=re.IGNORECASE)
    text = re.sub(r"homebdinfo\.com\s*\(?হোম\s*বিডি\s*ইনফো\s*ডট\s*কম\)?", "হোম বিডি ইনফো ডট কম", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://(?:www\.)?homebdinfo\.com\S*", "হোম বিডি ইনফো ডট কম", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:www\.)?homebdinfo\.com", "হোম বিডি ইনফো ডট কম", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_sentences_for_tts(script: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"(?<=[।!?\.])\s+", script) if p.strip()]
    return parts if parts else [script]


def get_prosody_for_sentence(sentence: str, idx: int, total: int) -> Tuple[str, str, float]:
    if idx == 0:
        return "+14%", "+0Hz", 0.30
    if "?" in sentence or "কীভাবে" in sentence or "কেন" in sentence or "কী" in sentence:
        return "+10%", "+3Hz", 0.30
    if idx == total - 1:
        return "+8%", "-3Hz", 0.35
    return "+11%", "-1Hz", 0.24


async def _synthesize_chunk(text: str, out_path: str, rate: str, pitch: str):
    communicate = edge_tts.Communicate(text=text, voice=VOICE_NAME, rate=rate, pitch=pitch)
    await communicate.save(out_path)


def split_kinetic_chunks(sentence: str, max_chars: int = 34) -> List[str]:
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

        subprocess.run(
            ["ffmpeg", "-y", "-i", raw_mp3, "-ar", "44100", "-ac", "2", wav_seg],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", wav_seg],
            capture_output=True,
            text=True,
            check=True,
        )
        speech_dur = float(probe.stdout.strip())

        chunks = split_kinetic_chunks(sentence, max_chars=34)
        total_chars = sum(len(c) for c in chunks) or 1
        t_cursor = current_time
        for c_i, ch in enumerate(chunks):
            ch_dur = speech_dur * (len(ch) / total_chars)
            kinetic_segments.append(
                {
                    "text": ch,
                    "start": t_cursor,
                    "end": t_cursor + ch_dur + (pause_sec if c_i == len(chunks) - 1 else 0.0),
                    "is_hook": (idx == 0),
                }
            )
            t_cursor += ch_dur

        segment_files.append(wav_seg)
        current_time += speech_dur

        if pause_sec > 0:
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{pause_sec:.2f}", sil_wav],
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

    raw_concat_wav = os.path.join(work_dir, "voiceover_raw.wav")
    final_voice_wav = os.path.join(work_dir, "voiceover_full.wav")

    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", raw_concat_wav],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    subprocess.run(
        [
            "ffmpeg", "-y", "-i", raw_concat_wav,
            "-af", "highpass=f=85,equalizer=f=3200:t=q:w=1.2:g=3,acompressor=threshold=-18dB:ratio=3:attack=5:release=60,loudnorm=I=-15:TP=-1.5:LRA=11",
            "-ar", "44100", "-ac", "2", final_voice_wav,
        ],
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
    tokens = re.findall(r"[A-Za-z0-9\./:\-_@|•]+|[^A-Za-z0-9\./:\-_@|•]+", text)
    cursor_x = x
    for tok in tokens:
        is_latin = bool(re.match(r"^[A-Za-z0-9\./:\-_@|•]+$", tok))
        fnt = en_font if is_latin else bn_font
        draw.text((cursor_x, y), tok, font=fnt, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
        bbox = draw.textbbox((0, 0), tok, font=fnt)
        cursor_x += bbox[2] - bbox[0]


def measure_mixed_text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    bn_font: ImageFont.FreeTypeFont,
    en_font: ImageFont.FreeTypeFont,
) -> int:
    tokens = re.findall(r"[A-Za-z0-9\./:\-_@|•]+|[^A-Za-z0-9\./:\-_@|•]+", text)
    total_w = 0
    for tok in tokens:
        is_latin = bool(re.match(r"^[A-Za-z0-9\./:\-_@|•]+$", tok))
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
    stock_clips: List[str],
    out_jpg: str,
    work_dir: str,
):
    bg_img = None
    first_clip = stock_clips[0] if stock_clips else None

    if first_clip and os.path.exists(first_clip):
        frame_png = os.path.join(work_dir, "thumb_stock_frame.png")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", first_clip, "-ss", "00:00:00.5", "-vframes", "1", frame_png],
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
            padded = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (6, 78, 59))
            padded.paste(bg_img, (0, 0))
            bg_img = padded

    canvas = bg_img.convert("RGBA")
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    for y in range(VIDEO_HEIGHT):
        ratio = y / VIDEO_HEIGHT
        alpha = int(215 - (ratio / 0.25) * 65) if ratio < 0.25 else (int(150 + ((ratio - 0.65) / 0.35) * 85) if ratio > 0.65 else 155)
        ov_draw.line([(0, y), (VIDEO_WIDTH, y)], fill=(6, 40, 30, alpha))
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    draw.rectangle([18, 18, VIDEO_WIDTH - 18, VIDEO_HEIGHT - 18], outline=(16, 185, 129, 240), width=8)
    top_y = 95

    if os.path.exists(LOGO_FILE):
        try:
            logo = Image.open(LOGO_FILE).convert("RGBA")
            lw = 175
            lh = int(logo.height * (lw / logo.width))
            logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
            canvas.paste(logo, ((VIDEO_WIDTH - lw) // 2, top_y), logo)
            top_y += lh + 30
        except Exception:
            pass

    badge_w, badge_h = 460, 68
    bx0 = (VIDEO_WIDTH - badge_w) // 2
    draw.rounded_rectangle([bx0, top_y, bx0 + badge_w, top_y + badge_h], radius=34, fill=(220, 38, 38, 245), outline=(255, 255, 255, 230), width=3)
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
    draw.rounded_rectangle([45, box_y0, VIDEO_WIDTH - 45, box_y1], radius=36, fill=(6, 78, 59, 235), outline=(250, 204, 21, 245), width=5)

    for idx, ln in enumerate(lines):
        lw = measure_mixed_text_width(draw, ln, bn_title_font, en_title_font)
        lx = (VIDEO_WIDTH - lw) // 2
        ly = box_y0 + box_pad_y + idx * line_h
        txt_color = (250, 204, 21, 255) if idx % 2 == 0 else (255, 255, 255, 255)
        render_mixed_text_line(draw, lx, ly, ln, bn_title_font, en_title_font, fill=txt_color)

    dom_w, dom_h = 620, 78
    dx0 = (VIDEO_WIDTH - dom_w) // 2
    dy0 = VIDEO_HEIGHT - 210
    draw.rounded_rectangle([dx0, dy0, dx0 + dom_w, dy0 + dom_h], radius=38, fill=(16, 185, 129, 245), outline=(255, 255, 255, 240), width=4)
    bn_dom = find_font(BENGALI_FONT_PATHS, 36)
    en_dom = find_font(LATIN_FONT_PATHS, 36)
    dom_txt = "homebdinfo.com"
    dtw = measure_mixed_text_width(draw, dom_txt, bn_dom, en_dom)
    render_mixed_text_line(draw, (VIDEO_WIDTH - dtw) // 2, dy0 + 17, dom_txt, bn_dom, en_dom)

    canvas.convert("RGB").save(out_jpg, "JPEG", quality=92)


def create_theme_matched_top_banner_png(out_path: str):
    """ওয়েবসাইটের আসল ডার্ক এমেরাল্ড গ্রিন (#064E3B) থিমের সাথে ম্যাচ করা প্রিমিয়াম টপ ব্যানার"""
    w, h = 1080, 185
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for y in range(h - 10):
        ratio = y / (h - 10)
        r = int(6 + ratio * 10)
        g = int(78 + ratio * 35)
        b = int(59 + ratio * 25)
        draw.line([(0, y), (w, y)], fill=(r, g, b, 250))

    draw.rectangle([0, h - 10, w, h], fill=(250, 204, 21, 255))
    draw.ellipse([28, 16, 164, 152], fill=(255, 255, 255, 255), outline=(250, 204, 21, 255), width=5)

    bn_main = find_font(BENGALI_FONT_PATHS, 52)
    en_main = find_font(LATIN_FONT_PATHS, 48)
    slogan_text = "সঠিক তথ্য, সহজ জীবন"
    right_center = 185 + (w - 205) // 2
    sw = measure_mixed_text_width(draw, slogan_text, bn_main, en_main)
    render_mixed_text_line(draw, right_center - sw // 2, 22, slogan_text, bn_main, en_main, fill=(255, 255, 255, 255))

    bn_sub = find_font(BENGALI_FONT_PATHS, 29)
    en_sub = find_font(LATIN_FONT_PATHS, 29)
    sub_text = "Home BD Info • নাগরিক সেবা ও প্রযুক্তির বিশ্বস্ত গাইড"
    sub_w = measure_mixed_text_width(draw, sub_text, bn_sub, en_sub)
    pill_x0 = right_center - sub_w // 2 - 24
    pill_x1 = right_center + sub_w // 2 + 24
    draw.rounded_rectangle([pill_x0, 96, pill_x1, 146], radius=24, fill=(4, 47, 36, 220), outline=(16, 185, 129, 200), width=2)
    render_mixed_text_line(draw, right_center - sub_w // 2, 104, sub_text, bn_sub, en_sub, fill=(250, 204, 21, 255))

    img.save(out_path, "PNG")


def create_bottom_alternating_footer_mov(work_dir: str, out_mov: str):
    """নিচের স্মার্ট অল্টারনেটিং ফুটার (কোনো বক্স চিহ্ন ছাড়া পরিচ্ছন্ন বাংলা ও ইংরেজি ফন্ট)"""
    w, h = 920, 96
    bn_font = find_font(BENGALI_FONT_PATHS, 34)
    en_font = find_font(LATIN_FONT_PATHS, 36)
    frames_dir = os.path.join(work_dir, "footer_frames")
    os.makedirs(frames_dir, exist_ok=True)
    frame_idx = 0

    def save_frame(im: Image.Image, repeat: int = 1):
        nonlocal frame_idx
        for _ in range(repeat):
            im.save(os.path.join(frames_dir, f"ff_{frame_idx:04d}.png"), "PNG")
            frame_idx += 1

    domain = "homebdinfo.com"
    for i in range(1, len(domain) + 1):
        txt = domain[:i] + "|"
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([110, 10, w - 110, h - 10], radius=38, fill=(6, 78, 59, 240), outline=(250, 204, 21, 245), width=4)
        tw = measure_mixed_text_width(d, txt, bn_font, en_font)
        render_mixed_text_line(d, (w - tw) // 2, 24, txt, bn_font, en_font, fill=(255, 255, 255, 255))
        save_frame(im, repeat=2)

    im_full = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d_full = ImageDraw.Draw(im_full)
    d_full.rounded_rectangle([110, 10, w - 110, h - 10], radius=38, fill=(6, 78, 59, 240), outline=(250, 204, 21, 245), width=4)
    tw_full = measure_mixed_text_width(d_full, domain, bn_font, en_font)
    render_mixed_text_line(d_full, (w - tw_full) // 2, 24, domain, bn_font, en_font, fill=(250, 204, 21, 255))
    save_frame(im_full, repeat=20)

    im_blank = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    save_frame(im_blank, repeat=25)

    cta_line = "লাইক দিন   •   কমেন্ট করুন   •   শেয়ার করুন"
    for pulse in range(35):
        im_cta = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        dc = ImageDraw.Draw(im_cta)
        border_col = (250, 204, 21, 255) if (pulse // 5) % 2 == 0 else (255, 255, 255, 255)
        dc.rounded_rectangle([40, 8, w - 40, h - 8], radius=40, fill=(220, 38, 38, 245), outline=border_col, width=4)
        cw = measure_mixed_text_width(dc, cta_line, bn_font, en_font)
        render_mixed_text_line(dc, (w - cw) // 2, 23, cta_line, bn_font, en_font, fill=(255, 255, 255, 255))
        save_frame(im_cta, repeat=1)

    save_frame(im_blank, repeat=15)

    subprocess.run(
        ["ffmpeg", "-y", "-framerate", "10", "-i", os.path.join(frames_dir, "ff_%04d.png"), "-c:v", "png", out_mov],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def create_kinetic_typography_png(text: str, seg_idx: int, is_hook: bool, out_path: str):
    """পর্দার মাঝামাঝি-নিচে বোল্ড কাইনেটিক মোশন গ্রাফিক্স টাইপোগ্রাফি"""
    canvas_w = 1040
    canvas_h = 340
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_size = 62 if is_hook else 54
    bn_font = find_font(BENGALI_FONT_PATHS, font_size)
    en_font = find_font(LATIN_FONT_PATHS, font_size - 4)

    lines = wrap_text_by_pixel(draw, text, bn_font, en_font, 940)
    if len(lines) > 3:
        lines = lines[:3]

    line_h = font_size + 26
    total_h = len(lines) * line_h
    start_y = (canvas_h - total_h) // 2

    for i, ln in enumerate(lines):
        lw = measure_mixed_text_width(draw, ln, bn_font, en_font)
        lx = (canvas_w - lw) // 2
        ly = start_y + i * line_h
        pad_x, pad_y = 28, 10
        draw.rounded_rectangle(
            [lx - pad_x, ly - pad_y, lx + lw + pad_x, ly + font_size + pad_y],
            radius=22,
            fill=(6, 45, 34, 215),
            outline=(16, 185, 129, 180),
            width=2,
        )
        txt_fill = (250, 204, 21, 255) if (is_hook or i == 1) else (255, 255, 255, 255)
        render_mixed_text_line(draw, lx, ly, ln, bn_font, en_font, fill=txt_fill, stroke_width=3, stroke_fill=(0, 0, 0, 255))

    img.save(out_path, "PNG")


def create_outro_card_png(out_path: str):
    """শেষ ৩.০ সেকেন্ডের প্রফেশনাল চ্যানেল আউট্রো কার্ড"""
    img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (6, 78, 59, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([24, 24, VIDEO_WIDTH - 24, VIDEO_HEIGHT - 24], outline=(250, 204, 21, 245), width=8)

    cy = 340
    if os.path.exists(LOGO_FILE):
        try:
            logo = Image.open(LOGO_FILE).convert("RGBA")
            lw = 240
            lh = int(logo.height * (lw / logo.width))
            logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
            draw.ellipse(
                [(VIDEO_WIDTH - lw) // 2 - 25, cy - 25, (VIDEO_WIDTH + lw) // 2 + 25, cy + lh + 25],
                fill=(255, 255, 255, 255),
                outline=(250, 204, 21, 255),
                width=6,
            )
            img.paste(logo, ((VIDEO_WIDTH - lw) // 2, cy), logo)
            cy += lh + 70
        except Exception:
            pass

    bn_h1 = find_font(BENGALI_FONT_PATHS, 56)
    en_h1 = find_font(LATIN_FONT_PATHS, 52)
    h1 = "তথ্যটি উপকারী মনে হলে"
    w1 = measure_mixed_text_width(draw, h1, bn_h1, en_h1)
    render_mixed_text_line(draw, (VIDEO_WIDTH - w1) // 2, cy, h1, bn_h1, en_h1, fill=(255, 255, 255, 255))

    cy += 90
    btn_w, btn_h = 840, 110
    bx0 = (VIDEO_WIDTH - btn_w) // 2
    draw.rounded_rectangle([bx0, cy, bx0 + btn_w, cy + btn_h], radius=50, fill=(220, 38, 38, 255), outline=(255, 255, 255, 240), width=4)
    bn_btn = find_font(BENGALI_FONT_PATHS, 42)
    en_btn = find_font(LATIN_FONT_PATHS, 40)
    btn_txt = "লাইক  •  কমেন্ট  •  শেয়ার  •  ফলো করুন"
    bw = measure_mixed_text_width(draw, btn_txt, bn_btn, en_btn)
    render_mixed_text_line(draw, (VIDEO_WIDTH - bw) // 2, cy + 28, btn_txt, bn_btn, en_btn, fill=(255, 255, 255, 255))

    cy += 170
    bn_sub = find_font(BENGALI_FONT_PATHS, 38)
    en_sub = find_font(LATIN_FONT_PATHS, 38)
    sub_txt = "বিস্তারিত গাইড ও অনলাইন আবেদন লিংক পেতে ভিজিট করুন:"
    sw = measure_mixed_text_width(draw, sub_txt, bn_sub, en_sub)
    render_mixed_text_line(draw, (VIDEO_WIDTH - sw) // 2, cy, sub_txt, bn_sub, en_sub, fill=(250, 204, 21, 255))

    cy += 75
    dw, dh = 720, 105
    dx0 = (VIDEO_WIDTH - dw) // 2
    draw.rounded_rectangle([dx0, cy, dx0 + dw, cy + dh], radius=48, fill=(16, 185, 129, 255), outline=(255, 255, 255, 255), width=5)
    bn_dom = find_font(BENGALI_FONT_PATHS, 44)
    en_dom = find_font(LATIN_FONT_PATHS, 46)
    dom_txt = "homebdinfo.com"
    dom_w = measure_mixed_text_width(draw, dom_txt, bn_dom, en_dom)
    render_mixed_text_line(draw, (VIDEO_WIDTH - dom_w) // 2, cy + 24, dom_txt, bn_dom, en_dom, fill=(255, 255, 255, 255))

    img.save(out_path, "PNG")


def capture_mobile_site_screenshot(post_url: str, out_png: str) -> int:
    target_url = post_url if post_url.startswith("http") else "https://homebdinfo.com"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2.8,
            is_mobile=True,
            has_touch=True,
            user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
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
    api_key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not api_key:
        return []
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    fallback_kws = ["smartphone typing", "office work", "digital technology", "bangladesh city"]
    search_kws = kw_list + fallback_kws
    downloaded = []
    seen_ids = set()

    for kw in search_kws:
        if len(downloaded) >= max_clips:
            break
        try:
            url = f"https://pixabay.com/api/videos/?key={api_key}&q={requests.utils.quote(kw)}&per_page=8&safesearch=true"
            resp = requests.get(url, timeout=20).json()
            hits = resp.get("hits", [])
            random.shuffle(hits)
            for hit in hits:
                if len(downloaded) >= max_clips:
                    break
                vid_id = hit.get("id")
                if vid_id in seen_ids:
                    continue
                videos = hit.get("videos", {})
                vid_info = videos.get("large") or videos.get("medium") or videos.get("small")
                if vid_info and vid_info.get("url"):
                    out_p = os.path.join(work_dir, f"stock_scene_{len(downloaded)}.mp4")
                    v_data = requests.get(vid_info["url"], timeout=45).content
                    with open(out_p, "wb") as f:
                        f.write(v_data)
                    downloaded.append(out_p)
                    seen_ids.add(vid_id)
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
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", voice_wav],
        capture_output=True,
        text=True,
        check=True,
    )
    voice_dur = float(probe.stdout.strip())
    total_duration = voice_dur + OUTRO_DURATION

    top_banner_png = os.path.join(work_dir, "top_theme_banner.png")
    outro_png = os.path.join(work_dir, "outro_card.png")
    footer_mov = os.path.join(work_dir, "alternating_footer.mov")

    create_theme_matched_top_banner_png(top_banner_png)
    create_outro_card_png(outro_png)
    create_bottom_alternating_footer_mov(work_dir, footer_mov)

    kin_pngs = []
    for idx, seg in enumerate(kinetic_segments):
        kp = os.path.join(work_dir, f"kin_{idx}.png")
        create_kinetic_typography_png(seg["text"], idx, seg["is_hook"], kp)
        kin_pngs.append((kp, seg["start"], seg["end"]))

    inputs = [
        "-loop", "1", "-t", f"{total_duration:.2f}", "-i", screenshot_png,
        "-i", voice_wav,
        "-loop", "1", "-t", f"{total_duration:.2f}", "-i", top_banner_png,
        "-stream_loop", "-1", "-t", f"{total_duration:.2f}", "-i", footer_mov,
        "-loop", "1", "-t", f"{total_duration:.2f}", "-i", outro_png,
        "-loop", "1", "-t", f"{COVER_DURATION:.2f}", "-i", thumbnail_jpg,
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

    clip_indices = []
    for cp in stock_clips:
        if os.path.exists(cp):
            inputs.extend(["-stream_loop", "-1", "-t", f"{total_duration:.2f}", "-i", cp])
            clip_indices.append(next_idx)
            next_idx += 1

    kin_start_idx = next_idx
    for kp, _, _ in kin_pngs:
        inputs.extend(["-loop", "1", "-t", f"{total_duration:.2f}", "-i", kp])

    scroll_dist = max(0, screenshot_h - VIDEO_HEIGHT)
    filter_parts = []
    filter_parts.append(
        f"[0:v]crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:0:'min({scroll_dist}, {scroll_dist}*(t/{max(1.0, voice_dur):.2f}))',"
        f"eq=brightness=-0.04:contrast=1.06[site_bg]"
    )
    last_v = "site_bg"

    scene_windows = [
        (0.0, HOOK_DURATION),
        (8.5, 14.0),
        (18.5, 24.5),
        (29.0, voice_dur),
    ]

    if clip_indices:
        for s_i, (win_start, win_end) in enumerate(scene_windows):
            if win_start >= voice_dur:
                break
            actual_end = min(win_end, voice_dur)
            c_in = clip_indices[s_i % len(clip_indices)]
            sc_lbl = f"scene_clip_{s_i}"
            out_lbl = f"v_scene_{s_i}"
            dur_span = max(1.0, actual_end - win_start)
            filter_parts.append(
                f"[{c_in}:v]scale=1160:2062:force_original_aspect_ratio=increase,"
                f"crop=1160:2062,"
                f"scale=w='trunc(1080*(1+0.05*sin(PI*(t-{win_start:.2f})/{dur_span:.2f}))/2)*2':h='trunc(1920*(1+0.05*sin(PI*(t-{win_start:.2f})/{dur_span:.2f}))/2)*2':eval=frame,"
                f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:'(iw-{VIDEO_WIDTH})/2':'(ih-{VIDEO_HEIGHT})/2',"
                f"eq=brightness=-0.05:contrast=1.08[{sc_lbl}]"
            )
            filter_parts.append(
                f"[{last_v}][{sc_lbl}]overlay=0:0:enable='between(t,{win_start:.2f},{actual_end:.2f})'[{out_lbl}]"
            )
            last_v = out_lbl

    filter_parts.append(f"[{last_v}][2:v]overlay=0:0:enable='between(t,0,{voice_dur:.2f})'[v_banner]")
    last_v = "v_banner"

    if logo_idx is not None:
        filter_parts.append(
            f"[{logo_idx}:v]scale=w='trunc(108*(1+0.08*sin(2*PI*t/2.0))/2)*2':h='trunc(108*(1+0.08*sin(2*PI*t/2.0))/2)*2':eval=frame[logo_anim]"
        )
        filter_parts.append(
            f"[{last_v}][logo_anim]overlay='28+(136-w)/2':'16+(136-h)/2':enable='between(t,0,{voice_dur:.2f})'[v_logo]"
        )
        last_v = "v_logo"

    for i, (_, st, en) in enumerate(kin_pngs):
        if st >= voice_dur:
            break
        actual_en = min(en, voice_dur)
        k_in = kin_start_idx + i
        out_lbl = f"v_kin{i}"
        filter_parts.append(
            f"[{last_v}][{k_in}:v]overlay=(W-w)/2:1260:enable='between(t,{st:.2f},{actual_en:.2f})'[{out_lbl}]"
        )
        last_v = out_lbl

    filter_parts.append(
        f"[{last_v}][3:v]overlay=(W-w)/2:1680:enable='between(t,0,{voice_dur:.2f})'[v_footer]"
    )
    last_v = "v_footer"

    filter_parts.append(
        f"[{last_v}]drawbox=x=0:y=0:w='iw*(t/{total_duration:.2f})':h=10:color=#FACC15@0.95:t=fill[v_prog]"
    )
    last_v = "v_prog"

    filter_parts.append(
        f"[{last_v}][4:v]overlay=0:0:enable='between(t,{voice_dur:.2f},{total_duration:.2f})'[v_with_outro]"
    )
    last_v = "v_with_outro"

    filter_parts.append(
        f"[5:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}[thumb_cover];"
        f"[{last_v}][thumb_cover]overlay=0:0:enable='between(t,0,{COVER_DURATION:.2f})'[v_final]"
    )

    if bgm_idx is not None:
        filter_parts.append(
            f"[{bgm_idx}:a]volume={BGM_VOLUME}[bgm_low];"
            f"[1:a]apad=pad_dur={OUTRO_DURATION:.2f}[v_pad];"
            f"[v_pad][bgm_low]amix=inputs=2:duration=first:dropout_transition=2[a_final]"
        )
    else:
        filter_parts.append(f"[1:a]apad=pad_dur={OUTRO_DURATION:.2f}[a_final]")

    filter_complex = ";".join(filter_parts)
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[v_final]", "-map", "[a_final]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-r", str(FPS),
        "-t", f"{total_duration:.2f}", output_mp4,
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
    rel_resp = requests.post(f"https://api.github.com/repos/{GITHUB_REPO}/releases", headers=headers, json=release_payload, timeout=30)
    rel_resp.raise_for_status()
    rel_data = rel_resp.json()
    release_id = str(rel_data["id"])
    upload_url = rel_data["upload_url"].split("{")[0]

    file_name = os.path.basename(video_path)
    with open(video_path, "rb") as f:
        up_resp = requests.post(
            f"{upload_url}?name={file_name}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"},
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
                headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
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
        f"🎬 নতুন Bangla Shorts প্রস্তুত!\n\n"
        f"📌 শিরোনাম: {title}\n"
        f"🔗 পোস্ট: {post_url}\n"
        f"📊 Sheet Row: {row_num}\n\n"
        f"💡 Approve দিলে ইনস্ট্যান্ট পোস্ট হবে, অথবা Schedule বাটনে সময় ঠিক করুন।"
    )
    reply_markup = json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve (Instant)", "callback_data": f"APPROVE_{row_num}"},
                    {"text": "⏰ Schedule", "callback_data": f"SCHEDULE_{row_num}"},
                ],
                [
                    {"text": "❌ Reject", "callback_data": f"REJECT_{row_num}"},
                ],
            ]
        }
    )

    files = {"video": open(video_path, "rb")}
    if thumb_path and os.path.exists(thumb_path):
        files["thumbnail"] = open(thumb_path, "rb")

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendVideo",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML", "reply_markup": reply_markup},
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
    return str(msg.get("video", {}).get("file_id", "")), str(msg["message_id"])


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
            else "https://homebdinfo.com"
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

            print("3. Fetching multiple action video clips...")
            downloaded_clips = download_multiple_pixabay_clips(keywords, work_dir, max_clips=4)

            print("4. Generating attractive branded thumbnail...")
            create_thumbnail_jpg(yt_title, screenshot_png, downloaded_clips, thumb_jpg, work_dir)

            print("5. Rendering final branded video...")
            build_final_video(
                screenshot_png=screenshot_png,
                screenshot_h=sc_height,
                voice_wav=voice_wav,
                kinetic_segments=kinetic_segments,
                stock_clips=downloaded_clips,
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
