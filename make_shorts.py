#!/usr/bin/env python3
"""
Bangla YouTube Shorts Automation Script
Repository: mainfouser/bangla-shorts-bot
"""

import os
import re
import json
import time
import asyncio
import tempfile
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional

import requests
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from PIL import Image, ImageDraw, ImageFont

# ==============================================================================
# VOICE CONFIGURATION (সহজে ভয়েস পরিবর্তনের জন্য নিচের অপশনগুলো ব্যবহার করুন)
# ==============================================================================
# ডিফল্ট ভয়েস: "bn-IN-BashkarNeural" (পুরুষ - ভারতীয় বাংলা)
# অন্যান্য জনপ্রিয় বাংলা ভয়েস অপশন:
#   - "bn-BD-PradeepNeural"   (পুরুষ - বাংলাদেশী বাংলা)
#   - "bn-BD-NabanitaNeural"  (নারী - বাংলাদেশী বাংলা)
#   - "bn-IN-TanishaaNeural"  (নারী - ভারতীয় বাংলা)
VOICE_NAME = "bn-IN-BashkarNeural"
VOICE_RATE = "+8%"
VOICE_PITCH = "-2Hz"
# ==============================================================================

# Video & Design Settings
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30
BGM_PATH = "bgm.mp3"
BGM_VOLUME = 0.12  # 12% volume
LOGO_PATH = "logo.png"

# GitHub & Sheet Settings
GITHUB_REPO = "mainfouser/bangla-shorts-bot"
SHEET_NAME = "Shorts_Queue"
TARGET_STATUS = "SCRIPT_READY"

# Required Column Headers
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


def get_env_var(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "").strip()
    if required and not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def connect_google_sheet():
    creds_json = get_env_var("GOOGLE_CREDENTIALS")
    sheet_id = get_env_var("SHEET_ID")
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.worksheet(SHEET_NAME)


def find_bengali_font(size: int) -> ImageFont.FreeTypeFont:
    font_candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in font_candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


async def _synthesize_tts(text: str, out_audio: str) -> None:
    communicate = edge_tts.Communicate(
        text=text,
        voice=VOICE_NAME,
        rate=VOICE_RATE,
        pitch=VOICE_PITCH,
    )
    await communicate.save(out_audio)


def generate_voiceover(script_text: str, out_audio: str) -> float:
    asyncio.run(_synthesize_tts(script_text, out_audio))
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        out_audio,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def split_script_into_chunks(script_text: str, max_chars: int = 85) -> List[str]:
    clean_text = " ".join(script_text.strip().split())
    pattern = re.compile(r"(?<=[।!?\.])\s+")
    sentences = [s.strip() for s in pattern.split(clean_text) if s.strip()]

    chunks: List[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            chunks.append(sentence)
        else:
            words = sentence.split(" ")
            current = ""
            for w in words:
                if not current:
                    current = w
                elif len(current) + 1 + len(w) <= max_chars:
                    current += " " + w
                else:
                    chunks.append(current)
                    current = w
            if current:
                chunks.append(current)

    return chunks if chunks else [clean_text]


def wrap_text_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    words = text.split(" ")
    lines: List[str] = []
    current_line = ""
    for word in words:
        test_line = f"{current_line} {word}".strip()
        bbox = draw.textbbox((0, 0), test_line, font=font)
        line_width = bbox[2] - bbox[0]
        if line_width <= max_width or not current_line:
            current_line = test_line
        else:
            lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)
    return lines


def create_subtitle_overlay_image(text: str, out_png: str) -> None:
    img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = find_bengali_font(size=56)

    max_text_width = VIDEO_WIDTH - 140
    lines = wrap_text_lines(draw, text, font, max_text_width)

    line_heights = []
    line_widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_widths.append(bbox[2] - bbox[0])
        line_heights.append(bbox[3] - bbox[1])

    line_spacing = 18
    total_text_h = sum(line_heights) + line_spacing * max(0, len(lines) - 1)
    box_width = max(line_widths) + 80 if line_widths else 600
    box_height = total_text_h + 60

    box_x1 = (VIDEO_WIDTH - box_width) // 2
    box_y2 = VIDEO_HEIGHT - 260
    box_y1 = box_y2 - box_height
    box_x2 = box_x1 + box_width

    draw.rounded_rectangle(
        [(box_x1, box_y1), (box_x2, box_y2)],
        radius=28,
        fill=(0, 0, 0, 185),
    )

    curr_y = box_y1 + 30
    for idx, line in enumerate(lines):
        lw = line_widths[idx]
        lh = line_heights[idx]
        lx = (VIDEO_WIDTH - lw) // 2
        draw.text((lx + 2, curr_y + 2), line, font=font, fill=(0, 0, 0, 230))
        draw.text((lx, curr_y), line, font=font, fill=(255, 255, 255, 255))
        curr_y += lh + line_spacing

    img.save(out_png, "PNG")


def format_srt_time(seconds: float) -> str:
    ms = int(round((seconds - int(seconds)) * 1000))
    total_sec = int(seconds)
    hrs = total_sec // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"


def build_subtitle_overlays(script_text: str, total_duration: float, work_dir: str) -> List[Tuple[str, float, float]]:
    chunks = split_script_into_chunks(script_text)
    total_chars = sum(max(1, len(c)) for c in chunks)
    overlays: List[Tuple[str, float, float]] = []

    curr_time = 0.0
    for idx, chunk in enumerate(chunks):
        ratio = max(1, len(chunk)) / total_chars
        seg_dur = total_duration * ratio
        end_time = total_duration if idx == len(chunks) - 1 else curr_time + seg_dur
        png_path = os.path.join(work_dir, f"sub_{idx:03d}.png")
        create_subtitle_overlay_image(chunk, png_path)
        overlays.append((png_path, curr_time, end_time))
        curr_time = end_time

    return overlays


def download_pixabay_videos(keywords: str, min_duration: float, work_dir: str) -> List[str]:
    api_key = get_env_var("PIXABAY_API_KEY")
    query_list = [k.strip() for k in keywords.split(",") if k.strip()]
    if not query_list:
        query_list = ["nature", "abstract"]

    downloaded: List[str] = []
    seen_ids = set()
    accumulated_dur = 0.0

    for kw in query_list:
        if accumulated_dur >= min_duration + 4.0:
            break
        params = {
            "key": api_key,
            "q": kw,
            "video_type": "film",
            "orientation": "vertical",
            "per_page": 10,
            "safesearch": "true",
        }
        resp = requests.get("https://pixabay.com/api/videos/", params=params, timeout=30)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])

        if not hits:
            params.pop("orientation", None)
            resp = requests.get("https://pixabay.com/api/videos/", params=params, timeout=30)
            resp.raise_for_status()
            hits = resp.json().get("hits", [])

        for hit in hits:
            vid_id = hit.get("id")
            if vid_id in seen_ids:
                continue
            videos_dict = hit.get("videos", {})
            chosen = (
                videos_dict.get("medium")
                or videos_dict.get("large")
                or videos_dict.get("small")
            )
            if not chosen or not chosen.get("url"):
                continue

            seen_ids.add(vid_id)
            out_file = os.path.join(work_dir, f"clip_{vid_id}.mp4")
            with requests.get(chosen["url"], stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(out_file, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)

            downloaded.append(out_file)
            accumulated_dur += float(hit.get("duration", 8))
            if accumulated_dur >= min_duration + 4.0:
                break

    if not downloaded:
        raise RuntimeError(f"No stock videos found on Pixabay for keywords: {keywords}")

    return downloaded


def render_final_video(
    clips: List[str],
    voice_path: str,
    duration: float,
    overlays: List[Tuple[str, float, float]],
    output_mp4: str,
    work_dir: str,
) -> None:
    norm_clips: List[str] = []
    for idx, clip in enumerate(clips):
        norm_path = os.path.join(work_dir, f"norm_{idx:03d}.mp4")
        vf = (
            f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},fps={VIDEO_FPS},format=yuv420p"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            clip,
            "-an",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            norm_path,
        ]
        subprocess.run(cmd, check=True)
        norm_clips.append(norm_path)

    concat_list_path = os.path.join(work_dir, "concat.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for _ in range(10):
            for nc in norm_clips:
                f.write(f"file '{nc}'\n")

    base_video = os.path.join(work_dir, "base_looped.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list_path,
            "-t",
            f"{duration:.3f}",
            "-c",
            "copy",
            base_video,
        ],
        check=True,
    )

    inputs = ["-i", base_video, "-i", voice_path]
    has_bgm = os.path.exists(BGM_PATH)
    has_logo = os.path.exists(LOGO_PATH)

    next_idx = 2
    bgm_idx = None
    if has_bgm:
        inputs.extend(["-stream_loop", "-1", "-i", BGM_PATH])
        bgm_idx = next_idx
        next_idx += 1

    logo_idx = None
    if has_logo:
        inputs.extend(["-i", LOGO_PATH])
        logo_idx = next_idx
        next_idx += 1

    overlay_indices: List[Tuple[int, float, float]] = []
    for png_path, start_t, end_t in overlays:
        inputs.extend(["-i", png_path])
        overlay_indices.append((next_idx, start_t, end_t))
        next_idx += 1

    filter_parts: List[str] = []
    curr_v = "[0:v]"

    if logo_idx is not None:
        filter_parts.append(f"[{logo_idx}:v]scale=150:-1[logo_scaled]")
        filter_parts.append(f"{curr_v}[logo_scaled]overlay=W-w-40:40[v_logo]")
        curr_v = "[v_logo]"

    for step, (ov_idx, st, et) in enumerate(overlay_indices):
        out_label = f"[v_sub_{step}]"
        filter_parts.append(
            f"{curr_v}[{ov_idx}:v]overlay=0:0:enable='between(t,{st:.3f},{et:.3f})'{out_label}"
        )
        curr_v = out_label

    if bgm_idx is not None:
        filter_parts.append(f"[{bgm_idx}:a]volume={BGM_VOLUME}[bgm_low]")
        filter_parts.append(
            "[1:a][bgm_low]amix=inputs=2:duration=first:dropout_transition=2[a_out]"
        )
        audio_map = "[a_out]"
    else:
        audio_map = "1:a"

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        filter_complex,
        "-map",
        curr_v,
        "-map",
        audio_map,
        "-t",
        f"{duration:.3f}",
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
        output_mp4,
    ]
    subprocess.run(cmd, check=True)


def upload_to_github_release(video_path: str, row_num: int, title: str) -> Tuple[str, str]:
    token = get_env_var("GITHUB_TOKEN")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    tag_name = f"shorts-row-{row_num}-{int(time.time())}"
    release_payload = {
        "tag_name": tag_name,
        "name": f"Short #{row_num}: {title[:60]}",
        "body": f"Auto-generated Bangla YouTube Short for Sheet Row {row_num}.",
        "draft": False,
        "prerelease": False,
    }

    rel_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    rel_resp = requests.post(rel_url, headers=headers, json=release_payload, timeout=30)
    rel_resp.raise_for_status()
    rel_data = rel_resp.json()

    release_id = str(rel_data["id"])
    upload_url_template = rel_data["upload_url"]
    upload_base = upload_url_template.split("{")[0]

    asset_name = os.path.basename(video_path)
    upload_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "video/mp4",
    }
    with open(video_path, "rb") as f:
        up_resp = requests.post(
            f"{upload_base}?name={asset_name}",
            headers=upload_headers,
            data=f,
            timeout=120,
        )
    up_resp.raise_for_status()
    asset_data = up_resp.json()
    download_url = asset_data["browser_download_url"]

    return release_id, download_url


def send_telegram_for_approval(
    video_path: str,
    row_num: int,
    title: str,
    download_url: str,
) -> Tuple[str, str]:
    bot_token = get_env_var("TELEGRAM_BOT_TOKEN")
    chat_id = get_env_var("TELEGRAM_CHAT_ID")

    api_url = f"https://api.telegram.org/bot{bot_token}/sendVideo"
    caption = (
        f"🎬 নতুন Bangla Short তৈরি হয়েছে (Row #{row_num})\n\n"
        f"📌 Title: {title}\n"
        f"🔗 Download: {download_url}"
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
        resp = requests.post(
            api_url,
            data={
                "chat_id": chat_id,
                "caption": caption,
                "reply_markup": json.dumps(reply_markup),
            },
            files={"video": vf},
            timeout=120,
        )
    resp.raise_for_status()
    res_json = resp.json()
    if not res_json.get("ok"):
        raise RuntimeError(f"Telegram API error: {res_json}")

    msg = res_json["result"]
    msg_id = str(msg["message_id"])
    file_id = str(msg.get("video", {}).get("file_id", ""))
    return file_id, msg_id


def main() -> None:
    worksheet = connect_google_sheet()
    headers = worksheet.row_values(1)
    col_map = {h.strip(): idx + 1 for idx, h in enumerate(headers)}

    required_cols = [
        COL_TITLE,
        COL_SCRIPT,
        COL_KEYWORDS,
        COL_STATUS,
        COL_VIDEO_STATUS,
        COL_DRIVE_ID,
        COL_DRIVE_URL,
        COL_TG_FILE_ID,
        COL_TG_MSG_ID,
        COL_LAST_ERROR,
    ]
    for rc in required_cols:
        if rc not in col_map:
            raise RuntimeError(f"Missing required column in Google Sheet: {rc}")

    all_rows = worksheet.get_all_values()
    processed_count = 0

    for row_idx in range(2, len(all_rows) + 1):
        row = all_rows[row_idx - 1]

        def get_cell(col_name: str) -> str:
            c_idx = col_map[col_name] - 1
            return row[c_idx].strip() if c_idx < len(row) else ""

        status = get_cell(COL_STATUS)
        if status != TARGET_STATUS:
            continue

        title = get_cell(COL_TITLE) or f"Bangla Short #{row_idx}"
        script_text = get_cell(COL_SCRIPT)
        keywords = get_cell(COL_KEYWORDS)

        if not script_text:
            worksheet.update_cell(row_idx, col_map[COL_LAST_ERROR], "Empty script text")
            continue

        print(f"Processing Row #{row_idx}: {title}")
        worksheet.update_cell(row_idx, col_map[COL_VIDEO_STATUS], "PROCESSING")
        worksheet.update_cell(row_idx, col_map[COL_LAST_ERROR], "")

        try:
            with tempfile.TemporaryDirectory() as work_dir:
                voice_mp3 = os.path.join(work_dir, "voice.mp3")
                duration = generate_voiceover(script_text, voice_mp3)

                overlays = build_subtitle_overlays(script_text, duration, work_dir)
                clips = download_pixabay_videos(keywords, duration, work_dir)

                final_mp4 = os.path.join(work_dir, f"short_row_{row_idx}.mp4")
                render_final_video(clips, voice_mp3, duration, overlays, final_mp4, work_dir)

                release_id, download_url = upload_to_github_release(final_mp4, row_idx, title)
                tg_file_id, tg_msg_id = send_telegram_for_approval(
                    final_mp4, row_idx, title, download_url
                )

                worksheet.update_cell(row_idx, col_map[COL_DRIVE_ID], release_id)
                worksheet.update_cell(row_idx, col_map[COL_DRIVE_URL], download_url)
                worksheet.update_cell(row_idx, col_map[COL_TG_FILE_ID], tg_file_id)
                worksheet.update_cell(row_idx, col_map[COL_TG_MSG_ID], tg_msg_id)
                worksheet.update_cell(row_idx, col_map[COL_VIDEO_STATUS], "GENERATED")
                worksheet.update_cell(row_idx, col_map[COL_STATUS], "PENDING_APPROVAL")
                processed_count += 1
                print(f"Completed Row #{row_idx} successfully.")

        except Exception as exc:
            err_msg = str(exc)[:450]
            print(f"Error on Row #{row_idx}: {err_msg}")
            worksheet.update_cell(row_idx, col_map[COL_VIDEO_STATUS], "FAILED")
            worksheet.update_cell(row_idx, col_map[COL_LAST_ERROR], err_msg)

    print(f"Done. Total rows processed: {processed_count}")


if __name__ == "__main__":
    main()
