#!/usr/bin/env python3
"""
Bangla YouTube Shorts Automation Script (make_shorts.py)
Repository: mainfouser/bangla-shorts-bot

Required Environment Variables / GitHub Secrets:
- GOOGLE_CREDENTIALS: JSON string of Google Service Account credentials
- SHEET_ID: Google Sheet ID containing 'Shorts_Queue'
- PIXABAY_API_KEY: Pixabay API key for vertical stock videos
- TELEGRAM_BOT_TOKEN: Telegram Bot API token
- TELEGRAM_CHAT_ID: Target Telegram chat/channel ID
- GITHUB_TOKEN: GitHub token with contents:write permission
- GITHUB_REPOSITORY: (Optional, defaults to 'mainfouser/bangla-shorts-bot')
"""

import os
import re
import json
import time
import math
import asyncio
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from PIL import Image, ImageDraw, ImageFont
import numpy as np

# MoviePy v1 / v2 compatible imports
try:
    from moviepy.editor import (
        VideoFileClip,
        AudioFileClip,
        ImageClip,
        CompositeVideoClip,
        CompositeAudioClip,
        concatenate_videoclips,
        afx,
    )
    MOVIEPY_V2 = False
except ImportError:
    from moviepy import (
        VideoFileClip,
        AudioFileClip,
        ImageClip,
        CompositeVideoClip,
        CompositeAudioClip,
        concatenate_videoclips,
        afx,
    )
    MOVIEPY_V2 = True


# ==============================================================================
# ভয়েস এবং অডিও কনফিগারেশন (VOICE & AUDIO SETTINGS)
# আপনি নিচের VOICE_NAME পরিবর্তন করে সহজেই অন্য বাংলা ভয়েস ব্যবহার করতে পারবেন:
#   - "bn-IN-BashkarNeural"   (ভারতীয় বাংলা পুরুষ কণ্ঠ - ডিফল্ট)
#   - "bn-BD-PradeepNeural"   (বাংলাদেশি বাংলা পুরুষ কণ্ঠ)
#   - "bn-IN-TanishaaNeural"  (ভারতীয় বাংলা নারী কণ্ঠ)
#   - "bn-BD-NabanitaNeural"  (বাংলাদেশি বাংলা নারী কণ্ঠ)
# ==============================================================================
VOICE_NAME = "bn-IN-BashkarNeural"
VOICE_RATE = "+8%"
VOICE_PITCH = "-2Hz"

# ==============================================================================
# সাধারণ কনফিগারেশন (GENERAL SETTINGS)
# ==============================================================================
SHEET_TAB_NAME = "Shorts_Queue"
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
FPS = 30
BGM_FILE = "bgm.mp3"
BGM_VOLUME = 0.12  # ১২% ব্যাকগ্রাউন্ড মিউজিক ভলিউম
LOGO_FILE = "logo.png"
WORK_DIR = Path("temp_shorts_build")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot")

# Google Sheet কলামের নামসমূহ
COL_TITLE = "Shorts Title (YT)"
COL_SCRIPT = "Bangla Voiceover Script"
COL_KEYWORDS = "Image Search Keywords (English)"
COL_STATUS = "Status"
COL_VIDEO_STATUS = "Video Status"
COL_DRIVE_ID = "Drive File ID"
COL_DRIVE_URL = "Drive File URL"
COL_TG_FILE_ID = "Telegram Video File ID"
COL_TG_MSG_ID = "Telegram Message ID"
COL_LAST_ERROR = "Last Error"


def get_gspread_client():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
    if not creds_raw:
        raise ValueError("GOOGLE_CREDENTIALS সিক্রেট পাওয়া যায়নি।")
    creds_dict = json.loads(creds_raw)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(credentials)


def find_bengali_font(font_size: int) -> ImageFont.FreeTypeFont:
    """সিস্টেমে ইনস্টল করা Noto Sans Bengali ফন্ট খুঁজে বের করে।"""
    candidate_paths = [
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/noto/NotoSansBengali-Regular.ttf",
    ]
    for path in candidate_paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, font_size)

    # ফোল্ডার স্ক্যান করে fallback
    for root, _, files in os.walk("/usr/share/fonts"):
        for f in files:
            if "NotoSansBengali" in f and f.endswith((".ttf", ".otf")):
                return ImageFont.truetype(os.path.join(root, f), font_size)

    return ImageFont.load_default()


async def generate_tts_and_subtitles(script_text: str, audio_path: Path):
    """edge-tts দিয়ে বাংলা ভয়েসওভার ও ওয়ার্ড/সেন্টেন্স টাইমিং তৈরি করে।"""
    communicate = edge_tts.Communicate(
        text=script_text,
        voice=VOICE_NAME,
        rate=VOICE_RATE,
        pitch=VOICE_PITCH,
    )
    word_boundaries = []
    with open(audio_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                # edge-tts offset এবং duration ১০০-ন্যানোসেকেন্ড ইউনিটে দেয়
                start_sec = chunk["offset"] / 10_000_000.0
                dur_sec = chunk["duration"] / 10_000_000.0
                word_boundaries.append(
                    {
                        "text": chunk["text"],
                        "start": start_sec,
                        "end": start_sec + dur_sec,
                    }
                )
    return word_boundaries


def build_subtitle_chunks(script_text: str, word_boundaries: list, total_duration: float):
    """সাবটাইটেল প্রদর্শনের জন্য ছোট ছোট পড়ার উপযোগী চাঙ্ক তৈরি করে।"""
    if word_boundaries:
        chunks = []
        current_words = []
        chunk_start = word_boundaries[0]["start"]

        for idx, wb in enumerate(word_boundaries):
            current_words.append(wb["text"])
            chunk_end = wb["end"]
            joined = " ".join(current_words)
            is_sentence_end = any(wb["text"].endswith(p) for p in ["।", "!", "?", ".", ","])

            if len(current_words) >= 6 or len(joined) >= 38 or is_sentence_end or idx == len(word_boundaries) - 1:
                chunks.append(
                    {
                        "text": joined.strip(),
                        "start": chunk_start,
                        "end": max(chunk_end, chunk_start + 0.4),
                    }
                )
                current_words = []
                if idx + 1 < len(word_boundaries):
                    chunk_start = word_boundaries[idx + 1]["start"]
        return chunks

    # Fallback: যদি WordBoundary ইভেন্ট না আসে তবে বাক্য অনুযায়ী সময় ভাগ করা হবে
    raw_parts = [p.strip() for p in re.split(r"(?<=[।!?\.])\s+|\n+", script) if p.strip()]
+", script_text) if p.strip()]
    if not raw_parts:
        raw_parts = [script_text.strip()]

    # বড় বাক্যগুলোকে ছোট অংশে ভাগ করা
    segments = []
    for part in raw_parts:
        words = part.split()
        for i in range(0, len(words), 6):
            segments.append(" ".join(words[i : i + 6]))

    total_chars = sum(len(s) for s in segments) or 1
    chunks = []
    cursor = 0.0
    for seg in segments:
        seg_dur = (len(seg) / total_chars) * total_duration
        chunks.append({"text": seg, "start": cursor, "end": cursor + seg_dur})
        cursor += seg_dur
    return chunks


def render_subtitle_image(text: str, output_png: Path):
    """নিচের দিকে কালো ট্রান্সপারেন্ট বক্সে স্পষ্ট বাংলা সাবটাইটেল ইমেজ তৈরি করে।"""
    img = Image.new("RGBA", (TARGET_WIDTH, TARGET_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = find_bengali_font(54)

    max_text_width = TARGET_WIDTH - 140
    words = text.split()
    lines = []
    current_line = ""

    for word in words:
        test_line = f"{current_line} {word}".strip()
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if (bbox[2] - bbox[0]) <= max_text_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)

    if not lines:
        lines = [text]

    line_heights = []
    line_widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_widths.append(bbox[2] - bbox[0])
        line_heights.append(bbox[3] - bbox[1])

    line_spacing = 18
    pad_x = 44
    pad_y = 28
    box_width = min(TARGET_WIDTH - 80, max(line_widths) + pad_x * 2)
    box_height = sum(line_heights) + line_spacing * (len(lines) - 1) + pad_y * 2

    box_x1 = (TARGET_WIDTH - box_width) // 2
    box_y2 = TARGET_HEIGHT - 230  # নিচের ইউটিউব UI এড়িয়ে নিরাপদ অবস্থান
    box_y1 = box_y2 - box_height
    box_x2 = box_x1 + box_width

    # কালো সেমি-ট্রান্সপারেন্ট রাউন্ডেড বক্স (Alpha = 185)
    draw.rounded_rectangle(
        [box_x1, box_y1, box_x2, box_y2],
        radius=26,
        fill=(0, 0, 0, 185),
        outline=(255, 255, 255, 45),
        width=2,
    )

    y_cursor = box_y1 + pad_y
    for idx, line in enumerate(lines):
        w = line_widths[idx]
        h = line_heights[idx]
        x_pos = (TARGET_WIDTH - w) // 2
        # হালকা শ্যাডো + সাদা টেক্সট
        draw.text((x_pos + 2, y_cursor + 2), line, font=font, fill=(0, 0, 0, 220))
        draw.text((x_pos, y_cursor), line, font=font, fill=(255, 255, 255, 255))
        y_cursor += h + line_spacing

    img.save(output_png, "PNG")


def fetch_pixabay_videos(keywords: str, required_duration: float, dest_dir: Path) -> list:
    """Pixabay API থেকে ভার্টিক্যাল স্টক ভিডিও খুঁজে ডাউনলোড করে।"""
    api_key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not api_key:
        raise ValueError("PIXABAY_API_KEY সিক্রেট পাওয়া যায়নি।")

    queries = [keywords.strip(), "nature abstract background", "city sky timelapse"]
    video_urls = []

    for q in queries:
        if not q:
            continue
        params = {
            "key": api_key,
            "q": q,
            "orientation": "vertical",
            "per_page": 10,
            "safesearch": "true",
        }
        resp = requests.get("https://pixabay.com/api/videos/", params=params, timeout=30)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
        for hit in hits:
            videos = hit.get("videos", {})
            # FHD বা HD ভার্টিক্যাল ভিডিও অগ্রাধিকার
            chosen = videos.get("large") or videos.get("medium") or videos.get("small")
            if chosen and chosen.get("url"):
                url = chosen["url"]
                if url not in video_urls:
                    video_urls.append(url)
        if len(video_urls) >= 4:
            break

    if not video_urls:
        raise RuntimeError(f"Pixabay-তে '{keywords}' এর জন্য কোনো ভিডিও পাওয়া যায়নি।")

    downloaded_paths = []
    accumulated_dur = 0.0
    for idx, url in enumerate(video_urls):
        out_path = dest_dir / f"clip_{idx}.mp4"
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
        downloaded_paths.append(out_path)
        try:
            with VideoFileClip(str(out_path)) as vc:
                accumulated_dur += vc.duration or 5.0
        except Exception:
            accumulated_dur += 5.0

        if accumulated_dur >= required_duration + 3.0 and len(downloaded_paths) >= 2:
            break

    return downloaded_paths


def fit_clip_to_vertical(clip: VideoFileClip) -> VideoFileClip:
    """যেকোনো ভিডিও ক্লিপকে 1080x1920 রেজোলিউশনে ক্রপ ও রিসাইজ করে।"""
    w, h = clip.size
    target_ratio = TARGET_WIDTH / TARGET_HEIGHT
    current_ratio = w / h

    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        x1 = (w - new_w) // 2
        if MOVIEPY_V2:
            clip = clip.cropped(x1=x1, y1=0, x2=x1 + new_w, y2=h)
        else:
            clip = clip.crop(x1=x1, y1=0, x2=x1 + new_w, y2=h)
    elif current_ratio < target_ratio:
        new_h = int(w / target_ratio)
        y1 = (h - new_h) // 2
        if MOVIEPY_V2:
            clip = clip.cropped(x1=0, y1=y1, x2=w, y2=y1 + new_h)
        else:
            clip = clip.crop(x1=0, y1=y1, x2=w, y2=y1 + new_h)

    if MOVIEPY_V2:
        return clip.resized((TARGET_WIDTH, TARGET_HEIGHT))
    return clip.resize((TARGET_WIDTH, TARGET_HEIGHT))


def compose_final_video(
    clip_paths: list,
    voice_path: Path,
    subtitle_chunks: list,
    output_mp4: Path,
    temp_dir: Path,
):
    """ভয়েসওভার, ব্যাকগ্রাউন্ড ভিডিও, BGM, লোগো ও সাবটাইটেল একত্রিত করে চূড়ান্ত MP4 বানায়।"""
    voice_audio = AudioFileClip(str(voice_path))
    total_duration = voice_audio.duration + 0.6

    # ভিডিও ক্লিপগুলো জোড়া দেওয়া ও প্রয়োজনে লুপ করা
    base_clips = []
    for p in clip_paths:
        c = VideoFileClip(str(p))
        c = c.without_audio()
        c = fit_clip_to_vertical(c)
        base_clips.append(c)

    stitched = concatenate_videoclips(base_clips, method="compose")
    if stitched.duration < total_duration:
        loops = math.ceil(total_duration / stitched.duration)
        stitched = concatenate_videoclips([stitched] * loops, method="compose")

    if MOVIEPY_V2:
        bg_video = stitched.subclipped(0, total_duration)
    else:
        bg_video = stitched.subclip(0, total_duration)

    layers = [bg_video]

    # উপরে ডান কোণায় logo.png ওয়াটারমার্ক যুক্ত করা
    if os.path.exists(LOGO_FILE):
        logo_clip = ImageClip(LOGO_FILE)
        logo_target_w = 150
        scale_factor = logo_target_w / logo_clip.w
        if MOVIEPY_V2:
            logo_clip = (
                logo_clip.resized(scale_factor)
                .with_duration(total_duration)
                .with_position((TARGET_WIDTH - logo_target_w - 40, 45))
            )
        else:
            logo_clip = (
                logo_clip.resize(scale_factor)
                .set_duration(total_duration)
                .set_position((TARGET_WIDTH - logo_target_w - 40, 45))
            )
        layers.append(logo_clip)

    # সাবটাইটেল ওভারলে লেয়ার যুক্ত করা
    sub_clips = []
    for idx, ch in enumerate(subtitle_chunks):
        sub_png = temp_dir / f"sub_{idx}.png"
        render_subtitle_image(ch["text"], sub_png)
        start_t = min(ch["start"], total_duration - 0.1)
        end_t = min(ch["end"], total_duration)
        dur_t = max(0.25, end_t - start_t)
        if MOVIEPY_V2:
            sc = ImageClip(str(sub_png)).with_start(start_t).with_duration(dur_t)
        else:
            sc = ImageClip(str(sub_png)).set_start(start_t).set_duration(dur_t)
        sub_clips.append(sc)
        layers.append(sc)

    final_video = CompositeVideoClip(layers, size=(TARGET_WIDTH, TARGET_HEIGHT))

    # অডিও মিক্সিং (ভয়েসওভার + ১২% ভলিউমে লুপকৃত bgm.mp3)
    audio_tracks = [voice_audio]
    bgm_audio = None
    if os.path.exists(BGM_FILE):
        bgm_raw = AudioFileClip(BGM_FILE)
        if bgm_raw.duration < total_duration:
            bgm_loops = math.ceil(total_duration / bgm_raw.duration)
            if MOVIEPY_V2:
                bgm_audio = bgm_raw.with_effects([afx.AudioLoop(duration=total_duration)])
            else:
                bgm_audio = afx.audio_loop(bgm_raw, duration=total_duration)
        else:
            if MOVIEPY_V2:
                bgm_audio = bgm_raw.subclipped(0, total_duration)
            else:
                bgm_audio = bgm_raw.subclip(0, total_duration)

        if MOVIEPY_V2:
            bgm_audio = bgm_audio.with_volume_scaled(BGM_VOLUME)
        else:
            bgm_audio = bgm_audio.volumex(BGM_VOLUME)
        audio_tracks.append(bgm_audio)

    mixed_audio = CompositeAudioClip(audio_tracks)
    if MOVIEPY_V2:
        final_video = final_video.with_audio(mixed_audio)
    else:
        final_video = final_video.set_audio(mixed_audio)

    final_video.write_videofile(
        str(output_mp4),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        preset="medium",
        threads=4,
        logger=None,
    )

    # রিসোর্স ক্লোজ করা
    final_video.close()
    mixed_audio.close()
    voice_audio.close()
    if bgm_audio:
        bgm_audio.close()
    for c in base_clips:
        c.close()
    for sc in sub_clips:
        sc.close()


def upload_to_github_release(video_path: Path, row_num: int, title: str):
    """GitHub Releases-এ ভিডিও আপলোড করে Release ID এবং Direct Download URL রিটার্ন করে।"""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise ValueError("GITHUB_TOKEN পাওয়া যায়নি।")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tag_name = f"short-row-{row_num}-{timestamp}"
    release_payload = {
        "tag_name": tag_name,
        "name": f"Short #{row_num}: {title[:60]}",
        "body": f"Auto-generated Bangla YouTube Short for sheet row {row_num}.",
        "draft": False,
        "prerelease": False,
    }

    create_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    rel_resp = requests.post(create_url, headers=headers, json=release_payload, timeout=30)
    rel_resp.raise_for_status()
    rel_data = rel_resp.json()

    release_id = str(rel_data["id"])
    upload_url_template = rel_data["upload_url"]
    upload_base = upload_url_template.split("{")[0]
    asset_name = video_path.name

    upload_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "video/mp4",
    }

    with open(video_path, "rb") as f:
        up_resp = requests.post(
            f"{upload_base}?name={asset_name}",
            headers=upload_headers,
            data=f.read(),
            timeout=120,
        )
    up_resp.raise_for_status()
    asset_data = up_resp.json()
    download_url = asset_data["browser_download_url"]

    return release_id, download_url


def send_telegram_approval(video_path: Path, row_num: int, title: str, download_url: str):
    """টেলিগ্রামে ভিডিও পাঠিয়ে নিচে APPROVE_{row} ও REJECT_{row} ইনলাইন বাটন যুক্ত করে।"""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN অথবা TELEGRAM_CHAT_ID পাওয়া যায়নি।")

    tg_api_url = f"https://api.telegram.org/bot{bot_token}/sendVideo"
    caption = (
        f"🎬 *নতুন বাংলা শর্টস প্রস্তুত!*

"
        f"📌 *Row:* `{row_num}`
"
        f"📝 *Title:* {title}
"
        f"🔗 [Direct Download]({download_url})

"
        f"ইউটিউবে আপলোডের জন্য নিচের বাটন থেকে অ্যাপ্রুভ বা রিজেক্ট করুন:"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"APPROVE_{row_num}"},
                {"text": "❌ Reject", "callback_data": f"REJECT_{row_num}"},
            ]
        ]
    }

    with open(video_path, "rb") as vf:
        files = {"video": (video_path.name, vf, "video/mp4")}
        data = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": "Markdown",
            "reply_markup": json.dumps(reply_markup),
        }
        resp = requests.post(tg_api_url, data=data, files=files, timeout=120)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")

    msg = result["result"]
    tg_message_id = str(msg["message_id"])
    tg_video_file_id = msg.get("video", {}).get("file_id", "")

    return tg_video_file_id, tg_message_id


def update_sheet_cells(worksheet, col_map: dict, row_num: int, updates: dict):
    """নির্দিষ্ট রো-এর একাধিক কলাম আপডেট করে।"""
    for col_name, value in updates.items():
        if col_name in col_map:
            worksheet.update_cell(row_num, col_map[col_name], value)


def process_row(worksheet, col_map: dict, row_num: int, row_data: dict):
    title = str(row_data.get(COL_TITLE, "")).strip() or f"Bangla Short #{row_num}"
    script = str(row_data.get(COL_SCRIPT, "")).strip()
    keywords = str(row_data.get(COL_KEYWORDS, "")).strip() or "nature landscape"

    if not script:
        raise ValueError("Bangla Voiceover Script কলামটি খালি রয়েছে।")

    row_temp = WORK_DIR / f"row_{row_num}"
    if row_temp.exists():
        for item in row_temp.iterdir():
            item.unlink()
    row_temp.mkdir(parents=True, exist_ok=True)

    # প্রসেসিং শুরু মার্ক করা
    update_sheet_cells(
        worksheet,
        col_map,
        row_num,
        {
            COL_STATUS: "PROCESSING",
            COL_VIDEO_STATUS: "GENERATING",
            COL_LAST_ERROR: "",
        },
    )

    # ১. ভয়েসওভার তৈরি
    voice_mp3 = row_temp / "voiceover.mp3"
    word_boundaries = asyncio.run(generate_tts_and_subtitles(script, voice_mp3))

    with AudioFileClip(str(voice_mp3)) as va:
        voice_dur = va.duration

    # ২. সাবটাইটেল চাঙ্ক তৈরি
    subtitle_chunks = build_subtitle_chunks(script, word_boundaries, voice_dur)

    # ৩. Pixabay থেকে ভিডিও ডাউনলোড
    clip_paths = fetch_pixabay_videos(keywords, voice_dur, row_temp)

    # ৪. ভিডিও কম্পোজিশন
    output_mp4 = row_temp / f"bangla_short_row_{row_num}.mp4"
    compose_final_video(clip_paths, voice_mp3, subtitle_chunks, output_mp4, row_temp)

    # ৫. GitHub Releases-এ আপলোড
    release_id, download_url = upload_to_github_release(output_mp4, row_num, title)

    # ৬. Telegram-এ অ্যাপ্রুভালের জন্য পাঠানো
    tg_file_id, tg_msg_id = send_telegram_approval(output_mp4, row_num, title, download_url)

    # ৭. Google Sheet আপডেট
    update_sheet_cells(
        worksheet,
        col_map,
        row_num,
        {
            COL_STATUS: "PENDING_APPROVAL",
            COL_VIDEO_STATUS: "READY_FOR_REVIEW",
            COL_DRIVE_ID: release_id,
            COL_DRIVE_URL: download_url,
            COL_TG_FILE_ID: tg_file_id,
            COL_TG_MSG_ID: tg_msg_id,
            COL_LAST_ERROR: "",
        },
    )
    print(f"[SUCCESS] Row {row_num} সফলভাবে সম্পন্ন হয়েছে।")


def main():
    sheet_id = os.environ.get("SHEET_ID", "").strip()
    if not sheet_id:
        raise ValueError("SHEET_ID সিক্রেট পাওয়া যায়নি।")

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    client = get_gspread_client()
    spreadsheet = client.open_by_key(sheet_id)
    worksheet = spreadsheet.worksheet(SHEET_TAB_NAME)

    headers = worksheet.row_values(1)
    col_map = {name.strip(): idx + 1 for idx, name in enumerate(headers)}

    all_records = worksheet.get_all_records()
    processed_count = 0

    for idx, record in enumerate(all_records):
        row_num = idx + 2  # হেডার রো ১ নম্বর, ডেটা ২ নম্বর থেকে শুরু
        status = str(record.get(COL_STATUS, "")).strip().upper()

        if status == "SCRIPT_READY":
            print(f"[INFO] প্রসেসিং শুরু হচ্ছে -> Row {row_num}...")
            try:
                process_row(worksheet, col_map, row_num, record)
                processed_count += 1
            except Exception as err:
                err_msg = f"{type(err).__name__}: {err}"
                print(f"[ERROR] Row {row_num} ব্যর্থ হয়েছে: {err_msg}")
                traceback.print_exc()
                update_sheet_cells(
                    worksheet,
                    col_map,
                    row_num,
                    {
                        COL_STATUS: "ERROR",
                        COL_VIDEO_STATUS: "FAILED",
                        COL_LAST_ERROR: err_msg[:450],
                    },
                )

    print(f"[DONE] মোট {processed_count}টি রো প্রসেস করা হয়েছে।")


if __name__ == "__main__":
    main()
