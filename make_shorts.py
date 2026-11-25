import os
import re
import gc
import json
import math
import time
import asyncio
import tempfile
import subprocess
from urllib.parse import urljoin
from typing import List, Dict, Optional, Tuple

import requests
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from playwright.sync_api import sync_playwright

VOICE_NAME = "bn-BD-PradeepNeural"
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot")
SHEET_NAME = "Shorts_Queue"
BGM_FILE = "bgm.mp3"
LOGO_FILE = "logo.png"

# BGM volume: balanced so it stays audible under voiceover and rises gently in outro
BGM_VOICE_VOLUME = 0.085
BGM_OUTRO_VOLUME = 0.14

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 30
MAX_SCROLL_PX = 2600
HOOK_DURATION = 4.0
COVER_DURATION = 0.35
OUTRO_DURATION = 4.5

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


def _spell_domain_for_tts(domain_str: str) -> str:
    parts = [p.strip() for p in domain_str.strip().split(".") if p.strip()]
    if not parts:
        return domain_str
    spoken_parts = []
    for part in parts:
        low = part.lower()
        if low == "www":
            spoken_parts.append("ডাব্লিউ ডাব্লিউ ডাব্লিউ")
        elif low == "gov":
            spoken_parts.append("গভ")
        elif low == "bd":
            spoken_parts.append("বিডি")
        elif low == "com":
            spoken_parts.append("কম")
        elif low == "org":
            spoken_parts.append("ওআরজি")
        elif low == "net":
            spoken_parts.append("নেট")
        elif low == "edu":
            spoken_parts.append("ইডিইউ")
        elif re.fullmatch(r"[a-z]{1,4}", low):
            spoken_parts.append(" ".join(list(low)))
        else:
            spoken_parts.append(part)
    return " ডট ".join(spoken_parts)


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
    text = re.sub(r"(?:www\.)?homebdinfo\.com", "হোম বিডি ইনফো ডট কম", text, flags=re.IGNORECASE)

    def _replace_url(match):
        raw_u = match.group(0)
        cleaned_u = re.sub(r"^https?://", "", raw_u, flags=re.IGNORECASE)
        host_and_path = cleaned_u.split("/", 1)
        host_spoken = _spell_domain_for_tts(host_and_path[0])
        if len(host_and_path) > 1 and host_and_path[1].strip():
            return f"{host_spoken} পোর্টাল"
        return host_spoken

    text = re.sub(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=]+", _replace_url, text)
    text = re.sub(
        r"\b(?:[a-z0-9-]+\.)+(?:gov\.bd|com\.bd|org\.bd|edu\.bd|bd|com|org|net|info)\b",
        lambda m: _spell_domain_for_tts(m.group(0)),
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(r"\s*[—–-]\s*", ", ", text)
    text = re.sub(r"\s*:\s*", ": ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_clauses_for_tts(script: str) -> List[Dict]:
    raw_sentences = [p.strip() for p in re.split(r"(?<=[।!?\.])\s+", script) if p.strip()]
    if not raw_sentences:
        raw_sentences = [script]

    clauses = []
    total_s = len(raw_sentences)
    for s_idx, sent in enumerate(raw_sentences):
        sub_parts = [sp.strip() for sp in re.split(r"(?<=[,;:])\s+", sent) if sp.strip()]
        if not sub_parts:
            sub_parts = [sent]

        merged = []
        for sp in sub_parts:
            if merged and len(sp) < 10:
                merged[-1] = f"{merged[-1]} {sp}".strip()
            else:
                merged.append(sp)

        for c_idx, clause_txt in enumerate(merged):
            is_sentence_end = (c_idx == len(merged) - 1)
            clauses.append(
                {
                    "text": clause_txt,
                    "sent_idx": s_idx,
                    "total_sents": total_s,
                    "is_hook": (s_idx == 0 and c_idx == 0),
                    "is_sentence_end": is_sentence_end,
                }
            )
    return clauses


def get_expressive_prosody(clause_info: Dict) -> Tuple[str, str, float]:
    txt = clause_info["text"]
    s_idx = clause_info["sent_idx"]
    total_s = clause_info["total_sents"]
    is_hook = clause_info["is_hook"]
    is_end = clause_info["is_sentence_end"]

    pause = 0.42 if is_end else 0.18

    if is_hook:
        return "+2%", "+4Hz", 0.40

    if "?" in txt or any(w in txt for w in ["কীভাবে", "কেন", "কী", "কত", "কখন", "আদৌ"]):
        return "+1%", "+5Hz", pause + 0.05

    if any(w in txt for w in ["সতর্ক", "জরুরি", "অবশ্যই", "মনে রাখবেন", "ভুল", "জরিমানা", "আইন", "নিষেধ", "বাতিল"]):
        return "-8%", "-2Hz", pause + 0.06

    if any(w in txt for w in ["প্রথম", "দ্বিতীয়", "তৃতীয়", "ধাপ", "ক্যাটাগরি", "সবুজ", "হলুদ", "কমলা", "লাল"]):
        return "-5%", "+2Hz", pause + 0.04

    if s_idx >= max(1, total_s - 1) and is_end:
        return "-7%", "-2Hz", 0.48

    pattern_idx = (s_idx + (0 if is_end else 1)) % 4
    if pattern_idx == 0:
        return "-3%", "+2Hz", pause
    elif pattern_idx == 1:
        return "-6%", "-1Hz", pause
    elif pattern_idx == 2:
        return "-2%", "+3Hz", pause
    else:
        return "-5%", "+0Hz", pause


async def _synthesize_phrase(text: str, out_path: str, rate: str, pitch: str, voice: str = VOICE_NAME):
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
    await communicate.save(out_path)


def split_kinetic_chunks(sentence: str, max_chars: int = 36) -> List[str]:
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
    clauses = split_clauses_for_tts(cleaned)

    segment_files = []
    kinetic_segments = []
    current_time = 0.0

    for idx, c_info in enumerate(clauses):
        clause_text = c_info["text"]
        rate, pitch, end_pause = get_expressive_prosody(c_info)

        raw_mp3 = os.path.join(work_dir, f"tts_{idx}.mp3")
        wav_seg = os.path.join(work_dir, f"tts_{idx}.wav")

        asyncio.run(_synthesize_phrase(clause_text, raw_mp3, rate, pitch, voice=VOICE_NAME))

        subprocess.run(
            ["ffmpeg", "-y", "-threads", "2", "-i", raw_mp3, "-ar", "44100", "-ac", "2", wav_seg],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if os.path.exists(raw_mp3):
            os.remove(raw_mp3)

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", wav_seg,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        dur = float(probe.stdout.strip())
        segment_files.append(wav_seg)

        clause_start = current_time
        current_time += dur

        chunks = split_kinetic_chunks(clause_text, max_chars=36)
        total_chars = sum(len(c) for c in chunks) or 1
        t_cursor = clause_start
        for c_i, ch in enumerate(chunks):
            ch_dur = dur * (len(ch) / total_chars)
            kinetic_segments.append(
                {
                    "text": ch,
                    "start": t_cursor,
                    "end": t_cursor + ch_dur + (end_pause if c_i == len(chunks) - 1 else 0.0),
                    "is_hook": (c_info["is_hook"] and c_i == 0),
                }
            )
            t_cursor += ch_dur

        if end_pause > 0:
            sil_wav = os.path.join(work_dir, f"sil_{idx}.wav")
            subprocess.run(
                [
                    "ffmpeg", "-y", "-threads", "2", "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{end_pause:.2f}", sil_wav,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            segment_files.append(sil_wav)
            current_time += end_pause

    concat_list = os.path.join(work_dir, "audio_concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for sf in segment_files:
            f.write(f"file '{sf}'\n")

    raw_concat_wav = os.path.join(work_dir, "voiceover_raw.wav")
    final_voice_wav = os.path.join(work_dir, "voiceover_full.wav")

    subprocess.run(
        ["ffmpeg", "-y", "-threads", "2", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", raw_concat_wav],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", "-i", raw_concat_wav,
            "-af", (
                "highpass=f=85,equalizer=f=210:t=q:w=1.0:g=2.8,"
                "equalizer=f=3200:t=q:w=1.4:g=2.2,"
                "acompressor=threshold=-17dB:ratio=2.5:attack=6:release=80,"
                "loudnorm=I=-15:TP=-1.5:LRA=11"
            ),
            "-ar", "44100", "-ac", "2", final_voice_wav,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for sf in segment_files:
        if os.path.exists(sf):
            os.remove(sf)
    if os.path.exists(raw_concat_wav):
        os.remove(raw_concat_wav)

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


def create_minimal_watermark_png(out_path: str):
    w, h = 460, 88
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [4, 8, w - 4, h - 8],
        radius=38,
        fill=(6, 45, 34, 210),
        outline=(250, 204, 21, 210),
        width=2,
    )
    text_x = 28
    if os.path.exists(LOGO_FILE):
        try:
            logo = Image.open(LOGO_FILE).convert("RGBA")
            lw = 58
            lh = int(logo.height * (lw / logo.width))
            logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
            draw.ellipse([14, 14, 76, 76], fill=(255, 255, 255, 245))
            img.paste(logo, (16, (h - lh) // 2), logo)
            text_x = 90
        except Exception:
            pass
    bn_font = find_font(BENGALI_FONT_PATHS, 30)
    en_font = find_font(LATIN_FONT_PATHS, 30)
    render_mixed_text_line(draw, text_x, 25, "homebdinfo.com", bn_font, en_font, fill=(255, 255, 255, 250))
    img.save(out_path, "PNG")


def create_clean_subtitle_png(text: str, is_hook: bool, out_path: str):
    canvas_w = 1040
    canvas_h = 340
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_size = 58 if is_hook else 48
    if len(text) > 38:
        font_size = 42

    bn_font = find_font(BENGALI_FONT_PATHS, font_size)
    en_font = find_font(LATIN_FONT_PATHS, font_size - 4)
    lines = wrap_text_by_pixel(draw, text, bn_font, en_font, 880)
    if len(lines) > 2:
        lines = lines[:2]
    line_h = font_size + 18
    total_h = len(lines) * line_h
    start_y = (canvas_h - total_h) // 2
    max_line_w = max((measure_mixed_text_width(draw, ln, bn_font, en_font) for ln in lines), default=400)
    pad_x, pad_y = 38, 20
    box_x0 = max(20, (canvas_w - max_line_w) // 2 - pad_x)
    box_x1 = min(canvas_w - 20, (canvas_w + max_line_w) // 2 + pad_x)
    box_y0 = max(10, start_y - pad_y)
    box_y1 = min(canvas_h - 10, start_y + total_h + pad_y - 8)
    border_col = (250, 204, 21, 225) if is_hook else (255, 255, 255, 110)
    draw.rounded_rectangle(
        [box_x0, box_y0, box_x1, box_y1],
        radius=26,
        fill=(8, 16, 14, 215),
        outline=border_col,
        width=3 if is_hook else 2,
    )
    for i, ln in enumerate(lines):
        lw = measure_mixed_text_width(draw, ln, bn_font, en_font)
        lx = (canvas_w - lw) // 2
        ly = start_y + i * line_h
        txt_fill = (250, 204, 21, 255) if (is_hook or i == 1) else (255, 255, 255, 255)
        render_mixed_text_line(
            draw, lx, ly, ln, bn_font, en_font,
            fill=txt_fill, stroke_width=3, stroke_fill=(0, 0, 0, 255)
        )
    img.save(out_path, "PNG")


def create_thumbnail_jpg(
    title: str,
    screenshot_png: str,
    visual_clips: List[str],
    out_jpg: str,
    work_dir: str,
):
    bg_img = None
    first_clip = visual_clips[0] if visual_clips else None
    if first_clip and os.path.exists(first_clip):
        frame_png = os.path.join(work_dir, "thumb_stock_frame.png")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-threads", "2", "-i", first_clip, "-ss", "00:00:00.5", "-vframes", "1", frame_png],
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
                os.remove(frame_png)
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
        alpha = (
            int(200 - (ratio / 0.25) * 55)
            if ratio < 0.25
            else (int(145 + ((ratio - 0.65) / 0.35) * 85) if ratio > 0.65 else 150)
        )
        ov_draw.line([(0, y), (VIDEO_WIDTH, y)], fill=(6, 35, 27, alpha))
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    top_y = 110
    if os.path.exists(LOGO_FILE):
        try:
            logo = Image.open(LOGO_FILE).convert("RGBA")
            lw = 165
            lh = int(logo.height * (lw / logo.width))
            logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
            canvas.paste(logo, ((VIDEO_WIDTH - lw) // 2, top_y), logo)
            top_y += lh + 35
        except Exception:
            pass

    clean_title = re.sub(r"#\w+", "", title).strip()
    bn_title_font = find_font(BENGALI_FONT_PATHS, 70)
    en_title_font = find_font(LATIN_FONT_PATHS, 66)
    lines = wrap_text_by_pixel(draw, clean_title, bn_title_font, en_title_font, 900)
    if len(lines) > 4:
        lines = lines[:4]
        lines[-1] = lines[-1][:22] + "..."

    line_h = 96
    box_pad_y = 46
    box_h = len(lines) * line_h + box_pad_y * 2
    box_y0 = max(top_y + 60, (VIDEO_HEIGHT - box_h) // 2)
    box_y1 = box_y0 + box_h
    draw.rounded_rectangle(
        [48, box_y0, VIDEO_WIDTH - 48, box_y1],
        radius=34,
        fill=(6, 65, 49, 230),
        outline=(250, 204, 21, 240),
        width=4,
    )

    for idx, ln in enumerate(lines):
        lw = measure_mixed_text_width(draw, ln, bn_title_font, en_title_font)
        lx = (VIDEO_WIDTH - lw) // 2
        ly = box_y0 + box_pad_y + idx * line_h
        txt_color = (250, 204, 21, 255) if idx % 2 == 0 else (255, 255, 255, 255)
        render_mixed_text_line(draw, lx, ly, ln, bn_title_font, en_title_font, fill=txt_color)

    canvas.convert("RGB").save(out_jpg, "JPEG", quality=92)
    del canvas, overlay, bg_img
    gc.collect()


def _elastic_ease_out(x: float) -> float:
    x = max(0.0, min(1.0, x))
    if x == 0.0 or x == 1.0:
        return x
    c4 = (2.0 * math.pi) / 3.0
    return (2.0 ** (-10.0 * x)) * math.sin((x * 10.0 - 0.75) * c4) + 1.0


def _draw_premium_outro_backdrop(t: float) -> Image.Image:
    # Rich multi-tone vertical gradient with subtle golden/emerald ambient orbs
    top_rgb = (4, 34, 28)
    bot_rgb = (6, 62, 48)
    bg = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (*top_rgb, 255))
    draw = ImageDraw.Draw(bg)

    for y in range(0, VIDEO_HEIGHT, 4):
        p = y / VIDEO_HEIGHT
        r = int(top_rgb[0] + (bot_rgb[0] - top_rgb[0]) * p)
        g = int(top_rgb[1] + (bot_rgb[1] - top_rgb[1]) * p)
        b = int(top_rgb[2] + (bot_rgb[2] - top_rgb[2]) * p)
        draw.rectangle([0, y, VIDEO_WIDTH, y + 4], fill=(r, g, b, 255))

    # Subtle diagonal golden light streaks
    for idx, base_x in enumerate([-180, 320, 820]):
        shift_x = int(base_x + 36 * math.sin(2.0 * math.pi * 0.45 * t + idx))
        poly = [
            (shift_x, 0),
            (shift_x + 130, 0),
            (shift_x - 210, VIDEO_HEIGHT),
            (shift_x - 340, VIDEO_HEIGHT),
        ]
        draw.polygon(poly, fill=(250, 204, 21, 11))

    # Glowing orbs behind logo and lower CTA zone
    orb1_r = int(310 + 34 * math.sin(2.0 * math.pi * 1.1 * t))
    draw.ellipse(
        [VIDEO_WIDTH // 2 - orb1_r, 430 - orb1_r, VIDEO_WIDTH // 2 + orb1_r, 430 + orb1_r],
        fill=(16, 185, 129, 36),
    )
    orb2_r = int(260 + 28 * math.cos(2.0 * math.pi * 1.0 * t))
    draw.ellipse(
        [VIDEO_WIDTH // 2 - orb2_r, 1280 - orb2_r, VIDEO_WIDTH // 2 + orb2_r, 1280 + orb2_r],
        fill=(250, 204, 21, 18),
    )

    # Outer & inner premium frame border
    draw.rounded_rectangle(
        [28, 28, VIDEO_WIDTH - 28, VIDEO_HEIGHT - 28],
        radius=54,
        outline=(250, 204, 21, 95),
        width=3,
    )
    draw.rounded_rectangle(
        [44, 44, VIDEO_WIDTH - 44, VIDEO_HEIGHT - 44],
        radius=44,
        outline=(255, 255, 255, 35),
        width=1,
    )
    return bg


def build_animated_outro_clip(out_mp4: str, work_dir: str, duration: float = OUTRO_DURATION):
    frames_dir = os.path.join(work_dir, "outro_frames")
    os.makedirs(frames_dir, exist_ok=True)
    n_frames = max(1, int(round(duration * FPS)))

    logo_src = None
    if os.path.exists(LOGO_FILE):
        try:
            logo_src = Image.open(LOGO_FILE).convert("RGBA")
        except Exception:
            logo_src = None

    bn_tag = find_font(BENGALI_FONT_PATHS, 28)
    en_tag = find_font(LATIN_FONT_PATHS, 26)
    bn_h1 = find_font(BENGALI_FONT_PATHS, 54)
    en_h1 = find_font(LATIN_FONT_PATHS, 50)
    bn_pill = find_font(BENGALI_FONT_PATHS, 31)
    en_pill = find_font(LATIN_FONT_PATHS, 29)
    bn_sub = find_font(BENGALI_FONT_PATHS, 35)
    en_sub = find_font(LATIN_FONT_PATHS, 35)
    bn_dom = find_font(BENGALI_FONT_PATHS, 46)
    en_dom = find_font(LATIN_FONT_PATHS, 48)

    action_items = [
        ("লাইক", (239, 68, 68, 255)),
        ("কমেন্ট", (59, 130, 246, 255)),
        ("শেয়ার", (16, 185, 129, 255)),
        ("ফলো", (245, 158, 11, 255)),
    ]

    for f_idx in range(n_frames):
        t = f_idx / FPS

        p_logo = min(1.0, t / 0.65)
        logo_pop = _elastic_ease_out(p_logo)

        p_fast = min(1.0, max(0.0, (t - 0.20) / 0.50))
        ease_fast = 1.0 - (1.0 - p_fast) ** 3

        p_slow = min(1.0, max(0.0, (t - 0.85) / 0.65))
        ease_slow = 1.0 - (1.0 - p_slow) ** 3

        p_dom = min(1.0, max(0.0, (t - 1.15) / 0.65))
        ease_dom = _elastic_ease_out(p_dom)

        pulse = 1.0 + 0.035 * math.sin(2.0 * math.pi * 1.3 * t)
        dom_glow_alpha = int(140 + 85 * math.sin(2.0 * math.pi * 1.6 * t))
        fade_out_alpha = 1.0
        if duration - t < 0.35:
            fade_out_alpha = max(0.0, (duration - t) / 0.35)

        img = _draw_premium_outro_backdrop(t)
        draw = ImageDraw.Draw(img)

        # Top brand pill
        top_pill_w, top_pill_h = 370, 56
        tpx0 = (VIDEO_WIDTH - top_pill_w) // 2
        tpy0 = 155
        draw.rounded_rectangle(
            [tpx0, tpy0, tpx0 + top_pill_w, tpy0 + top_pill_h],
            radius=28,
            fill=(12, 74, 57, 225),
            outline=(250, 204, 21, 180),
            width=2,
        )
        top_label = "HOME BD INFO • শর্টস"
        tlw = measure_mixed_text_width(draw, top_label, bn_tag, en_tag)
        render_mixed_text_line(draw, (VIDEO_WIDTH - tlw) // 2, tpy0 + 11, top_label, bn_tag, en_tag, fill=(250, 204, 21, 255))

        # Center logo medallion with rotating/pulsing gold ring
        cy = 285
        ring_cx, ring_cy = VIDEO_WIDTH // 2, cy + 125
        outer_ring_r = int(148 * pulse)
        draw.ellipse(
            [ring_cx - outer_ring_r, ring_cy - outer_ring_r, ring_cx + outer_ring_r, ring_cy + outer_ring_r],
            outline=(250, 204, 21, 220),
            width=5,
        )
        inner_ring_r = int(124 * (1.0 + 0.015 * math.cos(2.0 * math.pi * 1.5 * t)))
        draw.ellipse(
            [ring_cx - inner_ring_r, ring_cy - inner_ring_r, ring_cx + inner_ring_r, ring_cy + inner_ring_r],
            fill=(255, 255, 255, 250),
            outline=(16, 185, 129, 200),
            width=3,
        )

        if logo_src is not None:
            logo_scale = max(0.25, logo_pop) * pulse
            lw = max(24, int(190 * logo_scale))
            lh = max(24, int(logo_src.height * (lw / logo_src.width)))
            resized_logo = logo_src.resize((lw, lh), Image.Resampling.LANCZOS)
            img.paste(resized_logo, (ring_cx - lw // 2, ring_cy - lh // 2), resized_logo)

        # Glassmorphic showcase card for CTA & domain
        card_x0, card_y0 = 64, 625
        card_x1, card_y1 = VIDEO_WIDTH - 64, 1525
        draw.rounded_rectangle(
            [card_x0, card_y0, card_x1, card_y1],
            radius=40,
            fill=(8, 42, 33, 215),
            outline=(250, 204, 21, 130),
            width=3,
        )

        h1 = "তথ্যটি উপকারী মনে হলে"
        w1 = measure_mixed_text_width(draw, h1, bn_h1, en_h1)
        h1_y = int(card_y0 + 58 + (1.0 - ease_fast) * 36)
        render_mixed_text_line(draw, (VIDEO_WIDTH - w1) // 2, h1_y, h1, bn_h1, en_h1, fill=(255, 255, 255, 255))

        # 4 clean 2x2 action pills with pure geometric icons (zero missing font glyphs)
        grid_top = card_y0 + 168
        pill_w, pill_h = 410, 88
        gap_x, gap_y = 26, 22
        total_grid_w = pill_w * 2 + gap_x
        grid_left = (VIDEO_WIDTH - total_grid_w) // 2

        for idx, (label_bn, accent_rgba) in enumerate(action_items):
            r_idx = idx // 2
            c_idx = idx % 2
            p_item = min(1.0, max(0.0, (t - (0.35 + idx * 0.10)) / 0.45))
            item_pop = _elastic_ease_out(p_item)
            item_wave = 1.0 + 0.022 * math.sin(2.0 * math.pi * 1.7 * max(0.0, t - 0.9) + idx * 0.8)
            sc = (0.78 + 0.22 * max(0.0, item_pop)) * item_wave

            pw = int(pill_w * sc)
            ph = int(pill_h * sc)
            base_px = grid_left + c_idx * (pill_w + gap_x)
            base_py = grid_top + r_idx * (pill_h + gap_y)
            px0 = base_px + (pill_w - pw) // 2
            py0 = base_py + (pill_h - ph) // 2

            draw.rounded_rectangle(
                [px0, py0, px0 + pw, py0 + ph],
                radius=max(12, ph // 2),
                fill=(15, 68, 53, 240),
                outline=(250, 204, 21, 175),
                width=2,
            )

            # Geometric badge icon on left of each pill
            badge_r = max(12, int(24 * sc))
            bcx = px0 + int(48 * sc)
            bcy = py0 + ph // 2
            draw.ellipse([bcx - badge_r, bcy - badge_r, bcx + badge_r, bcy + badge_r], fill=accent_rgba)
            if idx == 0:  # Like: crisp star/diamond mark
                d_r = max(6, int(10 * sc))
                draw.polygon([(bcx, bcy - d_r), (bcx + d_r, bcy), (bcx, bcy + d_r), (bcx - d_r, bcy)], fill=(255, 255, 255, 255))
            elif idx == 1:  # Comment: chat dot
                d_r = max(6, int(9 * sc))
                draw.ellipse([bcx - d_r, bcy - d_r, bcx + d_r, bcy + d_r], fill=(255, 255, 255, 255))
            elif idx == 2:  # Share: right triangle arrow
                d_r = max(6, int(10 * sc))
                draw.polygon([(bcx - d_r // 2, bcy - d_r), (bcx + d_r, bcy), (bcx - d_r // 2, bcy + d_r)], fill=(255, 255, 255, 255))
            else:  # Follow: plus cross
                d_r = max(6, int(10 * sc))
                lw_plus = max(2, int(4 * sc))
                draw.line([(bcx - d_r, bcy), (bcx + d_r, bcy)], fill=(255, 255, 255, 255), width=lw_plus)
                draw.line([(bcx, bcy - d_r), (bcx, bcy + d_r)], fill=(255, 255, 255, 255), width=lw_plus)

            txt_w = measure_mixed_text_width(draw, label_bn, bn_pill, en_pill)
            render_mixed_text_line(
                draw,
                px0 + int(84 * sc) + max(0, (pw - int(96 * sc) - txt_w) // 2),
                py0 + (ph - 36) // 2,
                label_bn,
                bn_pill,
                en_pill,
                fill=(255, 255, 255, 255),
            )

        # Divider line
        div_y = grid_top + pill_h * 2 + gap_y + 38
        draw.line([(card_x0 + 90, div_y), (card_x1 - 90, div_y)], fill=(250, 204, 21, 90), width=2)

        sub_txt = "বিস্তারিত গাইড ও লিংক পেতে ভিজিট করুন"
        sw = measure_mixed_text_width(draw, sub_txt, bn_sub, en_sub)
        sub_y = int(div_y + 30 + (1.0 - ease_slow) * 32)
        render_mixed_text_line(draw, (VIDEO_WIDTH - sw) // 2, sub_y, sub_txt, bn_sub, en_sub, fill=(226, 232, 240, 255))

        # Premium glowing domain button
        dw, dh = 720, 108
        dom_scale = 0.82 + 0.18 * max(0.0, ease_dom)
        cur_dw = int(dw * dom_scale)
        cur_dh = int(dh * dom_scale)
        dx0 = (VIDEO_WIDTH - cur_dw) // 2
        dy0 = int(div_y + 92 + (1.0 - min(1.0, ease_dom)) * 36 + (dh - cur_dh) // 2)

        draw.rounded_rectangle(
            [dx0 - 7, dy0 - 7, dx0 + cur_dw + 7, dy0 + cur_dh + 7],
            radius=max(16, (cur_dh + 14) // 2),
            outline=(250, 204, 21, dom_glow_alpha),
            width=4,
        )
        draw.rounded_rectangle(
            [dx0, dy0, dx0 + cur_dw, dy0 + cur_dh],
            radius=max(14, cur_dh // 2),
            fill=(16, 185, 129, 255),
            outline=(255, 255, 255, 220),
            width=3,
        )
        dom_txt = "homebdinfo.com"
        dom_w = measure_mixed_text_width(draw, dom_txt, bn_dom, en_dom)
        render_mixed_text_line(draw, (VIDEO_WIDTH - dom_w) // 2, dy0 + 25, dom_txt, bn_dom, en_dom, fill=(255, 255, 255, 255))

        if fade_out_alpha < 1.0:
            blackout = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (4, 34, 28, int((1.0 - fade_out_alpha) * 235)))
            img = Image.alpha_composite(img, blackout)

        frame_path = os.path.join(frames_dir, f"outro_{f_idx:04d}.png")
        img.convert("RGB").save(frame_path, "PNG")
        del img, draw

    if logo_src is not None:
        logo_src.close()

    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2",
            "-framerate", str(FPS),
            "-i", os.path.join(frames_dir, "outro_%04d.png"),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-t", f"{duration:.2f}", out_mp4,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for fn in os.listdir(frames_dir):
        fp = os.path.join(frames_dir, fn)
        if os.path.exists(fp):
            os.remove(fp)
    if os.path.exists(frames_dir):
        os.rmdir(frames_dir)
    gc.collect()


def _encode_lightweight_motion_clip(image_path: str, out_mp4: str, duration: int = 8):
    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", "-loop", "1", "-t", str(duration), "-i", image_path,
            "-vf", (
                f"scale=w='trunc({VIDEO_WIDTH}(1+0.07*(t/{duration}))/2)*2':h=-2,"
                f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(iw-{VIDEO_WIDTH})/2:(ih-{VIDEO_HEIGHT})/2"
            ),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-t", str(duration), out_mp4,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def create_dynamic_topic_graphic_clip(
    headline: str,
    bullet_points: List[str],
    work_dir: str,
    clip_idx: int,
) -> Optional[str]:
    try:
        card_png = os.path.join(work_dir, f"motion_card_{clip_idx}.png")
        clip_mp4 = os.path.join(work_dir, f"motion_card_{clip_idx}.mp4")
        img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (6, 36, 28, 255))
        draw = ImageDraw.Draw(img)

        for gx in range(0, VIDEO_WIDTH, 90):
            draw.line([(gx, 0), (gx, VIDEO_HEIGHT)], fill=(255, 255, 255, 14), width=1)
        for gy in range(0, VIDEO_HEIGHT, 90):
            draw.line([(0, gy), (VIDEO_WIDTH, gy)], fill=(255, 255, 255, 14), width=1)

        badge_w, badge_h = 420, 66
        bx0 = (VIDEO_WIDTH - badge_w) // 2
        by0 = 220
        draw.rounded_rectangle([bx0, by0, bx0 + badge_w, by0 + badge_h], radius=33, fill=(250, 204, 21, 255))
        bn_badge = find_font(BENGALI_FONT_PATHS, 32)
        en_badge = find_font(LATIN_FONT_PATHS, 30)
        b_txt = "গুরুত্বপূর্ণ তথ্য ও আপডেট"
        bw = measure_mixed_text_width(draw, b_txt, bn_badge, en_badge)
        render_mixed_text_line(draw, (VIDEO_WIDTH - bw) // 2, by0 + 14, b_txt, bn_badge, en_badge, fill=(6, 36, 28, 255))

        card_x0, card_y0 = 60, 320
        card_x1, card_y1 = VIDEO_WIDTH - 60, 910
        draw.rounded_rectangle(
            [card_x0, card_y0, card_x1, card_y1],
            radius=32,
            fill=(10, 56, 44, 240),
            outline=(250, 204, 21, 230),
            width=4,
        )

        clean_head = re.sub(r"#\w+", "", headline).strip()
        bn_h = find_font(BENGALI_FONT_PATHS, 46)
        en_h = find_font(LATIN_FONT_PATHS, 42)
        h_lines = wrap_text_by_pixel(draw, clean_head, bn_h, en_h, VIDEO_WIDTH - 190)[:2]
        cur_y = card_y0 + 38
        for hl in h_lines:
            hw = measure_mixed_text_width(draw, hl, bn_h, en_h)
            render_mixed_text_line(draw, (VIDEO_WIDTH - hw) // 2, cur_y, hl, bn_h, en_h, fill=(250, 204, 21, 255))
            cur_y += 64

        draw.line([(card_x0 + 40, cur_y + 12), (card_x1 - 40, cur_y + 12)], fill=(255, 255, 255, 70), width=2)
        cur_y += 38

        bn_pt = find_font(BENGALI_FONT_PATHS, 36)
        en_pt = find_font(LATIN_FONT_PATHS, 34)
        valid_pts = [p.strip() for p in bullet_points if len(p.strip()) > 6][:3]
        for pt in valid_pts:
            if cur_y > card_y1 - 80:
                break
            pt_lines = wrap_text_by_pixel(draw, f"• {pt}", bn_pt, en_pt, VIDEO_WIDTH - 200)[:2]
            for pl in pt_lines:
                render_mixed_text_line(draw, card_x0 + 45, cur_y, pl, bn_pt, en_pt, fill=(255, 255, 255, 245))
                cur_y += 48
            cur_y += 14

        img.convert("RGB").save(card_png, "PNG")
        del img, draw
        gc.collect()
        _encode_lightweight_motion_clip(card_png, clip_mp4, duration=8)
        if os.path.exists(card_png):
            os.remove(card_png)
        gc.collect()
        return clip_mp4
    except Exception:
        return None


def capture_site_and_extract_images(post_url: str, out_png: str, work_dir: str) -> Tuple[int, List[str], List[str]]:
    target_url = post_url if post_url.startswith("http") else "https://homebdinfo.com"
    image_urls = []
    article_headings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2.0,
            is_mobile=True,
            has_touch=True,
            user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        )
        page = context.new_page()
        ad_domains = [
            "doubleclick.net", "googlesyndication.com", "googleadservices.com",
            "adservice.google", "adnxs.com", "popads", "monetag", "adsterra", "propellerads"
        ]
        page.route(
            "**/*",
            lambda route: route.abort()
            if any(d in route.request.url.lower() for d in ad_domains)
            else route.continue_(),
        )
        try:
            page.goto(target_url, wait_until="networkidle", timeout=45000)
        except Exception:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1600)

        page.evaluate(
            """() => {
                document.body.style.zoom = "1.08";
                const adSelectors = [
                    'ins.adsbygoogle', '.adsbygoogle', '[id*="google_ads"]', '[id*="ad-"]',
                    '[class*="ad-container"]', '[class*="adsense"]', '[class*="banner-ad"]',
                    '[id*="cookie"]', '[class*="cookie"]', '[class*="popup"]', '[class*="modal"]',
                    'iframe', '[data-ad-client]', '[data-ad-slot]'
                ];
                adSelectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => el.remove());
                });
            }"""
        )
        page.wait_for_timeout(500)

        extracted = page.evaluate(
            """() => {
                const container = document.querySelector('article, .post-body, .entry-content, main') || document.body;
                const imgs = Array.from(container.querySelectorAll('img')).map(img => ({
                    src: img.currentSrc || img.src || img.getAttribute('data-src') || '',
                    w: img.naturalWidth || img.width || 0,
                    h: img.naturalHeight || img.height || 0
                }));
                const headings = Array.from(container.querySelectorAll('h2, h3'))
                    .map(h => (h.innerText || '').trim())
                    .filter(t => t.length > 8 && t.length < 90 && !t.includes('লেখায় যা আছে'));
                return { imgs, headings };
            }"""
        )
        article_headings = extracted.get("headings", [])[:6]
        seen_urls = set()
        for item in extracted.get("imgs", []):
            src = item.get("src", "").strip()
            if not src or src.startswith("data:"):
                continue
            full_src = urljoin(target_url, src)
            lower_src = full_src.lower()
            if any(skip in lower_src for skip in ["logo", "icon", "avatar", "emoji", "banner-ad", "doubleclick"]):
                continue
            if item.get("w", 0) >= 260 and item.get("h", 0) >= 150 and full_src not in seen_urls:
                seen_urls.add(full_src)
                image_urls.append(full_src)

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

    tint = Image.new("RGBA", img.size, (8, 18, 14, 38))
    img = Image.alpha_composite(img.convert("RGBA"), tint).convert("RGB")
    img.save(out_png, "PNG")
    final_sc_height = img.height
    del img, tint
    gc.collect()

    article_clips = []
    for idx, img_u in enumerate(image_urls[:3]):
        try:
            resp = requests.get(img_u, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200 and len(resp.content) > 4096:
                raw_img_p = os.path.join(work_dir, f"art_raw_{idx}.png")
                framed_img_p = os.path.join(work_dir, f"art_framed_{idx}.png")
                clip_mp4_p = os.path.join(work_dir, f"art_clip_{idx}.mp4")
                with open(raw_img_p, "wb") as f:
                    f.write(resp.content)
                src_im = Image.open(raw_img_p).convert("RGB")
                if src_im.width < 240 or src_im.height < 130:
                    src_im.close()
                    os.remove(raw_img_p)
                    continue

                bg = src_im.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS)
                bg = bg.filter(ImageFilter.GaussianBlur(radius=28))
                dark_bg = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (6, 28, 22, 160))
                canvas = Image.alpha_composite(bg.convert("RGBA"), dark_bg)
                target_w = VIDEO_WIDTH - 80
                target_h = int(src_im.height * (target_w / src_im.width))
                if target_h > 900:
                    target_h = 900
                    target_w = int(src_im.width * (target_h / src_im.height))
                fg = src_im.resize((target_w, target_h), Image.Resampling.LANCZOS)
                fx = (VIDEO_WIDTH - target_w) // 2
                fy = 350 if target_h <= 560 else 270
                draw_c = ImageDraw.Draw(canvas)
                draw_c.rounded_rectangle(
                    [fx - 8, fy - 8, fx + target_w + 8, fy + target_h + 8],
                    radius=26,
                    fill=(250, 204, 21, 235),
                )
                canvas.paste(fg, (fx, fy))
                canvas.convert("RGB").save(framed_img_p, "PNG")
                del src_im, bg, dark_bg, canvas, fg, draw_c
                gc.collect()

                _encode_lightweight_motion_clip(framed_img_p, clip_mp4_p, duration=8)
                article_clips.append(clip_mp4_p)
                for tmp_p in (raw_img_p, framed_img_p):
                    if os.path.exists(tmp_p):
                        os.remove(tmp_p)
                gc.collect()
        except Exception:
            continue

    return final_sc_height, article_clips, article_headings


def download_relevant_stock_clips(keywords: str, work_dir: str, max_clips: int = 2) -> List[str]:
    if max_clips <= 0:
        return []
    api_key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not api_key:
        return []
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    if not kw_list:
        return []

    banned_tags = {
        "animal", "monkey", "baboon", "ape", "fire", "flame", "wildfire",
        "dog", "cat", "bird", "insect", "spider", "snake", "skull", "blood"
    }
    downloaded = []
    seen_ids = set()

    for kw in kw_list[:4]:
        if len(downloaded) >= max_clips:
            break
        try:
            url = (
                f"https://pixabay.com/api/videos/?key={api_key}"
                f"&q={requests.utils.quote(kw)}&order=relevant&per_page=6&safesearch=true"
            )
            resp = requests.get(url, timeout=20).json()
            hits = resp.get("hits", [])
            for hit in hits:
                if len(downloaded) >= max_clips:
                    break
                vid_id = hit.get("id")
                if vid_id in seen_ids:
                    continue
                tags_str = str(hit.get("tags", "")).lower()
                if any(b in tags_str for b in banned_tags):
                    continue
                videos = hit.get("videos", {})
                vid_info = videos.get("medium") or videos.get("small") or videos.get("tiny")
                if vid_info and vid_info.get("url"):
                    raw_p = os.path.join(work_dir, f"raw_stock_{len(downloaded)}.mp4")
                    norm_p = os.path.join(work_dir, f"norm_stock_{len(downloaded)}.mp4")
                    v_data = requests.get(vid_info["url"], timeout=45).content
                    with open(raw_p, "wb") as f:
                        f.write(v_data)
                    del v_data

                    subprocess.run(
                        [
                            "ffmpeg", "-y", "-threads", "2", "-i", raw_p,
                            "-vf", f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},eq=brightness=-0.06:contrast=1.06",
                            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-r", str(FPS),
                            "-t", "8", norm_p,
                        ],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    if os.path.exists(raw_p):
                        os.remove(raw_p)
                    downloaded.append(norm_p)
                    seen_ids.add(vid_id)
                    gc.collect()
        except Exception:
            continue
    return downloaded


def build_subtitle_overlay_track(kinetic_segments: List[Dict], voice_dur: float, work_dir: str) -> str:
    sub_dir = os.path.join(work_dir, "sub_frames")
    os.makedirs(sub_dir, exist_ok=True)
    blank_png = os.path.join(sub_dir, "blank.png")
    Image.new("RGBA", (1040, 340), (0, 0, 0, 0)).save(blank_png, "PNG")

    seg_pngs = []
    for idx, seg in enumerate(kinetic_segments):
        kp = os.path.join(sub_dir, f"sub_{idx}.png")
        create_clean_subtitle_png(seg["text"], seg["is_hook"], kp)
        seg_pngs.append((kp, seg["start"], min(seg["end"], voice_dur)))

    concat_file = os.path.join(sub_dir, "sub_concat.txt")
    cursor = 0.0
    entries = []
    for kp, st, en in seg_pngs:
        if st >= voice_dur or en <= st:
            continue
        if st > cursor + 0.02:
            entries.append((blank_png, st - cursor))
        entries.append((kp, en - st))
        cursor = en
    if cursor < voice_dur:
        entries.append((blank_png, voice_dur - cursor))
    if not entries:
        entries.append((blank_png, voice_dur))

    with open(concat_file, "w", encoding="utf-8") as f:
        for img_path, dur in entries:
            f.write(f"file '{img_path}'\n")
            f.write(f"duration {dur:.3f}\n")
        f.write(f"file '{entries[-1][0]}'\n")

    sub_mov = os.path.join(work_dir, "kinetic_track.mov")
    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", "-f", "concat", "-safe", "0", "-i", concat_file,
            "-vf", "fps=15", "-c:v", "png", sub_mov,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    gc.collect()
    return sub_mov


def build_final_video(
    screenshot_png: str,
    screenshot_h: int,
    voice_wav: str,
    kinetic_segments: List[Dict],
    visual_clips: List[str],
    thumbnail_jpg: str,
    output_mp4: str,
    work_dir: str,
):
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", voice_wav,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    voice_dur = float(probe.stdout.strip())
    total_duration = voice_dur + OUTRO_DURATION

    watermark_png = os.path.join(work_dir, "watermark_pill.png")
    outro_mp4 = os.path.join(work_dir, "outro_animated.mp4")
    create_minimal_watermark_png(watermark_png)
    build_animated_outro_clip(outro_mp4, work_dir, duration=OUTRO_DURATION)
    sub_mov = build_subtitle_overlay_track(kinetic_segments, voice_dur, work_dir)

    base_mp4 = os.path.join(work_dir, "base_layer.mp4")
    base_inputs = ["-loop", "1", "-t", f"{voice_dur:.2f}", "-i", screenshot_png]
    clip_indices = []
    next_i = 1
    for cp in visual_clips[:4]:
        if os.path.exists(cp):
            base_inputs.extend(["-stream_loop", "-1", "-t", f"{voice_dur:.2f}", "-i", cp])
            clip_indices.append(next_i)
            next_i += 1

    scroll_dist = max(0, screenshot_h - VIDEO_HEIGHT)
    base_filters = [
        f"[0:v]crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:0:'min({scroll_dist}, {scroll_dist}*(t/{max(1.0, voice_dur):.2f}))'[site_bg]"
    ]
    last_v = "site_bg"
    scene_windows = [
        (0.0, HOOK_DURATION),
        (9.0, 15.0),
        (20.0, 26.0),
        (30.5, voice_dur),
    ]
    if clip_indices:
        for s_i, (win_start, win_end) in enumerate(scene_windows):
            if win_start >= voice_dur:
                break
            actual_end = min(win_end, voice_dur)
            c_in = clip_indices[s_i % len(clip_indices)]
            out_lbl = f"v_scene_{s_i}"
            base_filters.append(
                f"[{last_v}][{c_in}:v]overlay=0:0:enable='between(t,{win_start:.2f},{actual_end:.2f})'[{out_lbl}]"
            )
            last_v = out_lbl

    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", *base_inputs,
            "-filter_complex", ";".join(base_filters),
            "-map", f"[{last_v}]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-t", f"{voice_dur:.2f}", base_mp4,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    gc.collect()

    final_inputs = [
        "-i", base_mp4,
        "-i", voice_wav,
        "-loop", "1", "-t", f"{total_duration:.2f}", "-i", watermark_png,
        "-i", sub_mov,
        "-i", outro_mp4,
        "-loop", "1", "-t", f"{COVER_DURATION:.2f}", "-i", thumbnail_jpg,
    ]
    bgm_idx = None
    if os.path.exists(BGM_FILE):
        final_inputs.extend(["-stream_loop", "-1", "-t", f"{total_duration:.2f}", "-i", BGM_FILE])
        bgm_idx = 6

    f_parts = [
        f"[0:v]tpad=stop_mode=clone:stop_duration={OUTRO_DURATION:.2f}[v_base]",
        f"[v_base][2:v]overlay=32:48:enable='between(t,0,{voice_dur:.2f})'[v_wm]",
        f"[v_wm][3:v]overlay=(W-w)/2:1340:enable='between(t,0,{voice_dur:.2f})'[v_sub]",
        f"[v_sub]drawbox=x=0:y=0:w='iw*(t/{total_duration:.2f})':h=8:color=#FACC15@0.90:t=fill[v_prog]",
        f"[v_prog][4:v]overlay=0:0:enable='between(t,{voice_dur:.2f},{total_duration:.2f})'[v_outro]",
        f"[5:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}[thumb_cover]",
        f"[v_outro][thumb_cover]overlay=0:0:enable='between(t,0,{COVER_DURATION:.2f})'[v_final]",
    ]
    if bgm_idx is not None:
        outro_fade_start = max(0.0, OUTRO_DURATION - 1.2)
        f_parts.extend([
            f"[{bgm_idx}:a]atrim=0:{voice_dur:.2f},volume={BGM_VOICE_VOLUME},afade=t=in:st=0:d=0.6[bgm_body]",
            f"[{bgm_idx}:a]atrim={voice_dur:.2f}:{total_duration:.2f},asetpts=PTS-STARTPTS,volume={BGM_OUTRO_VOLUME},afade=t=out:st={outro_fade_start:.2f}:d=1.2[bgm_outro]",
            f"[bgm_body][bgm_outro]concat=n=2:v=0:a=1[bgm_clean]",
            f"[1:a]apad=pad_dur={OUTRO_DURATION:.2f}[v_pad]",
            f"[v_pad][bgm_clean]amix=inputs=2:duration=first:weights='1.0 1.0':dropout_transition=2,volume=1.15[a_final]",
        ])
    else:
        f_parts.append(f"[1:a]apad=pad_dur={OUTRO_DURATION:.2f}[a_final]")

    subprocess.run(
        [
            "ffmpeg", "-y", "-threads", "2", *final_inputs,
            "-filter_complex", ";".join(f_parts),
            "-map", "[v_final]", "-map", "[a_final]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-r", str(FPS),
            "-t", f"{total_duration:.2f}", output_mp4,
        ],
        check=True,
    )
    gc.collect()


def delete_previous_github_release(old_release_id: str):
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token or not old_release_id or not str(old_release_id).strip().isdigit():
        return
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = requests.delete(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/{str(old_release_id).strip()}",
            headers=headers,
            timeout=30,
        )
        if resp.status_code in (200, 204):
            print(f"Previous GitHub release {old_release_id} deleted.")
    except Exception as e:
        print(f"Warning: Could not delete previous release {old_release_id}: {e}")


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
        f"💡 Approve দিলে ইনস্ট্যান্ট পোস্ট হবে, Schedule-এ সময় দিন, অথবা প্রয়োজন অনুযায়ী Regenerate বা Reject চাপুন।"
    )
    reply_markup = json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve (Instant)", "callback_data": f"APPROVE_{row_num}"},
                    {"text": "⏰ Schedule", "callback_data": f"SCHEDULE_{row_num}"},
                ],
                [
                    {"text": "🔄 Regenerate", "callback_data": f"REGENERATE_{row_num}"},
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
    old_release_id = (
        row_data[col_map["Drive File ID"] - 1].strip()
        if "Drive File ID" in col_map and len(row_data) >= col_map["Drive File ID"]
        else ""
    )
    if old_release_id:
        delete_previous_github_release(old_release_id)

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

            print("1. Capturing ad-free site screenshot & extracting article graphics...")
            sc_height, article_clips, article_headings = capture_site_and_extract_images(post_url, screenshot_png, work_dir)

            print("2. Generating natural, human-like voiceover...")
            voice_wav, kinetic_segments = generate_natural_voiceover(script, work_dir)

            print("3. Preparing universal topic-relevant visual clips...")
            visual_clips = list(article_clips)
            if len(visual_clips) < 2:
                bullets = article_headings if len(article_headings) >= 2 else split_clauses_for_tts(clean_voiceover_script(script))[1:4]
                bullet_texts = [b["text"] if isinstance(b, dict) else str(b) for b in bullets]
                info_card_clip = create_dynamic_topic_graphic_clip(yt_title, bullet_texts, work_dir, len(visual_clips))
                if info_card_clip:
                    visual_clips.append(info_card_clip)

            needed_stock = max(0, 3 - len(visual_clips))
            if needed_stock > 0:
                stock_clips = download_relevant_stock_clips(keywords, work_dir, max_clips=needed_stock)
                visual_clips.extend(stock_clips)

            print("4. Generating clean branded thumbnail...")
            create_thumbnail_jpg(yt_title, screenshot_png, visual_clips, thumb_jpg, work_dir)

            print("5. Rendering clean final video...")
            build_final_video(
                screenshot_png=screenshot_png,
                screenshot_h=sc_height,
                voice_wav=voice_wav,
                kinetic_segments=kinetic_segments,
                visual_clips=visual_clips,
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
