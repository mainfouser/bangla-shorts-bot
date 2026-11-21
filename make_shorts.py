
import os
import json
import asyncio
import subprocess
import requests
import gspread
import edge_tts
import traceback
import time
import re

from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from moviepy.editor import (
    VideoFileClip,
    AudioFileClip,
    ImageClip,
    CompositeVideoClip,
    CompositeAudioClip,
    concatenate_videoclips
)

import moviepy.audio.fx.all as afx


# =========================================================
# CONFIGURATION
# =========================================================

BRAND_NAME = "Home BD Info  |  homebdinfo.com"

VOICE_NAME = "bn-IN-BashkarNeural"

BGM_VOLUME = 0.10

OUTPUT_FILE = "final_short.mp4"
VOICE_FILE = "voice.mp3"

SHEET_NAME = "Shorts_Queue"

MAX_VIDEO_DURATION = 60

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

CLAIM_TIMEOUT_MINUTES = 90

LOCK_FILE = ".shorts_generator.lock"


# =========================================================
# ENVIRONMENT VALIDATION
# =========================================================

REQUIRED_ENV = [
    "GOOGLE_CREDENTIALS",
    "SHEET_ID",
    "PIXABAY_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DRIVE_FOLDER_ID"
]

for key in REQUIRED_ENV:

    if not os.environ.get(key):
        raise ValueError(
            f"Environment Variable পাওয়া যায়নি: {key}"
        )


# =========================================================
# GOOGLE CONNECTION
# =========================================================

creds_dict = json.loads(
    os.environ["GOOGLE_CREDENTIALS"]
)

creds = Credentials.from_service_account_info(
    creds_dict,
    scopes=SCOPES
)

client = gspread.authorize(creds)

sheet = client.open_by_key(
    os.environ["SHEET_ID"]
).worksheet(SHEET_NAME)

drive_service = build(
    "drive",
    "v3",
    credentials=creds,
    cache_discovery=False
)

DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]


# =========================================================
# GOOGLE SHEET HEADER MAPPING
# =========================================================

headers = sheet.row_values(1)

COL = {
    str(name).strip(): index + 1
    for index, name in enumerate(headers)
    if str(name).strip()
}

REQUIRED_HEADERS = [
    "Post ID",
    "Date Added",
    "Post Title",
    "Post URL",
    "Shorts Title (YT)",
    "Bangla Voiceover Script",
    "Image Search Keywords (English)",
    "Status",
    "Telegram Video File ID",
    "Telegram Message ID",
    "Drive File ID",
    "Drive File URL",
    "Video Status",
    "Approval Status",
    "YouTube Status",
    "YouTube Video ID",
    "YouTube URL",
    "YouTube Published At",
    "Facebook Status",
    "Facebook Video ID",
    "Facebook URL",
    "Facebook Published At",
    "Cleanup After",
    "Cleanup Status",
    "Last Error"
]

missing = [
    h for h in REQUIRED_HEADERS
    if h not in COL
]

if missing:

    raise ValueError(
        "Google Sheet-এ প্রয়োজনীয় Header পাওয়া যায়নি: "
        + ", ".join(missing)
    )


def update_field(row, field, value):

    sheet.update_cell(
        row,
        COL[field],
        value
    )


def update_fields(row, data):

    for field, value in data.items():

        update_field(
            row,
            field,
            value
        )


def get_row_data(row_number):

    values = sheet.row_values(row_number)

    result = {}

    for field, col_number in COL.items():

        index = col_number - 1

        result[field] = (
            values[index]
            if index < len(values)
            else ""
        )

    return result


# =========================================================
# LOCAL LOCK
# =========================================================

def acquire_local_lock():

    if os.path.exists(LOCK_FILE):

        lock_age = (
            time.time()
            - os.path.getmtime(LOCK_FILE)
        )

        if lock_age < CLAIM_TIMEOUT_MINUTES * 60:

            raise RuntimeError(
                "অন্য একটি Shorts Generator ইতিমধ্যে চলছে।"
            )

        os.remove(LOCK_FILE)

    with open(LOCK_FILE, "w") as lock:

        lock.write(
            datetime.now(timezone.utc).isoformat()
        )


def release_local_lock():

    if os.path.exists(LOCK_FILE):

        os.remove(LOCK_FILE)


# =========================================================
# BANGLA VOICE GENERATION
# =========================================================

async def generate_bangla_voice(text, output_file):

    natural_text = (
        text.replace("।", "... ")
        .replace("?", "?.. ")
        .replace("!", "!.. ")
    )

    raw_voice = "raw_voice.mp3"

    communicate = edge_tts.Communicate(
        natural_text,
        VOICE_NAME,
        rate="-2%",
        pitch="-3Hz"
    )

    await communicate.save(raw_voice)

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        raw_voice,
        "-af",
        (
            "highpass=f=85,"
            "equalizer=f=250:width_type=h:width=150:g=3,"
            "equalizer=f=3500:width_type=h:width=1000:g=2,"
            "acompressor=threshold=-18dB:ratio=3:attack=5:release=50"
        ),
        "-ar",
        "44100",
        "-b:a",
        "192k",
        output_file
    ]

    subprocess.run(
        ffmpeg_cmd,
        check=True
    )


# =========================================================
# PIXABAY STOCK VIDEO DOWNLOAD
# =========================================================

def download_stock_videos(keywords, count=4):

    api_key = os.environ["PIXABAY_API_KEY"]

    keyword_list = [
        x.strip()
        for x in keywords.split(",")
        if x.strip()
    ]

    if not keyword_list:

        keyword_list = ["technology"]

    base_url = "https://pixabay.com/api/videos/"

    selected = []

    for keyword in keyword_list[:3]:

        params = {
            "key": api_key,
            "q": keyword,
            "per_page": 20,
            "safesearch": "true"
        }

        response = requests.get(
            base_url,
            params=params,
            timeout=60
        )

        response.raise_for_status()

        hits = response.json().get(
            "hits",
            []
        )

        for item in hits:

            videos = item.get(
                "videos",
                {}
            )

            video_data = (
                videos.get("large")
                or videos.get("medium")
                or videos.get("small")
            )

            if not video_data:
                continue

            selected.append(video_data["url"])

            if len(selected) >= count:
                break

        if len(selected) >= count:
            break

    # Fallback
    if not selected:

        params = {
            "key": api_key,
            "q": "technology",
            "per_page": 10,
            "safesearch": "true"
        }

        response = requests.get(
            base_url,
            params=params,
            timeout=60
        )

        response.raise_for_status()

        hits = response.json().get(
            "hits",
            []
        )

        for item in hits:

            videos = item.get(
                "videos",
                {}
            )

            video_data = (
                videos.get("medium")
                or videos.get("small")
            )

            if video_data:
                selected.append(
                    video_data["url"]
                )

            if len(selected) >= count:
                break

    if not selected:

        raise RuntimeError(
            "Pixabay থেকে কোনো ভিডিও পাওয়া যায়নি।"
        )

    files = []

    for idx, video_url in enumerate(selected):

        response = requests.get(
            video_url,
            timeout=120
        )

        response.raise_for_status()

        filename = f"clip_{idx}.mp4"

        with open(filename, "wb") as f:

            f.write(response.content)

        if os.path.getsize(filename) == 0:

            raise RuntimeError(
                "ডাউনলোড করা Pixabay ভিডিও খালি।"
            )

        files.append(filename)

    return files


# =========================================================
# BRAND OVERLAY
# =========================================================

def create_brand_overlay(
    width=1080,
    height=1920,
    output_png="brand_overlay.png"
):

    canvas = Image.new(
        "RGBA",
        (width, height),
        (0, 0, 0, 0)
    )

    draw = ImageDraw.Draw(canvas)

    if os.path.exists("logo.png"):

        badge_size = 150

        badge = Image.new(
            "RGBA",
            (badge_size, badge_size),
            (0, 0, 0, 0)
        )

        b_draw = ImageDraw.Draw(badge)

        b_draw.ellipse(
            (0, 0, badge_size - 1, badge_size - 1),
            fill=(255, 255, 255, 245),
            outline=(22, 163, 74, 255),
            width=6
        )

        logo = Image.open(
            "logo.png"
        ).convert("RGBA")

        inner_size = 105

        logo = logo.resize(
            (inner_size, inner_size),
            Image.LANCZOS
        )

        offset = (
            badge_size - inner_size
        ) // 2

        badge.paste(
            logo,
            (offset, offset),
            logo
        )

        canvas.paste(
            badge,
            (width - badge_size - 55, 95),
            badge
        )

    bar_w = 720
    bar_h = 74

    x1 = (width - bar_w) // 2
    y1 = height - 290

    draw.rounded_rectangle(
        (
            x1,
            y1,
            x1 + bar_w,
            y1 + bar_h
        ),
        radius=37,
        fill=(15, 23, 42, 205),
        outline=(22, 163, 74, 240),
        width=4
    )

    try:

        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            31
        )

    except Exception:

        font = ImageFont.load_default()

    draw.text(
        (
            width // 2,
            y1 + bar_h // 2
        ),
        BRAND_NAME,
        font=font,
        fill=(255, 255, 255, 250),
        anchor="mm"
    )

    canvas.save(output_png)

    return output_png


# =========================================================
# VIDEO RENDERING
# =========================================================

def create_short_video(
    audio_path,
    video_paths,
    output_path
):

    if not video_paths:

        raise RuntimeError(
            "কোনো ভিডিও ক্লিপ পাওয়া যায়নি।"
        )

    voice_audio = AudioFileClip(
        audio_path
    )

    target_duration = min(
        voice_audio.duration,
        MAX_VIDEO_DURATION
    )

    if voice_audio.duration > MAX_VIDEO_DURATION:

        voice_audio.close()

        raise RuntimeError(
            "Voiceover ৬০ সেকেন্ডের বেশি। Script ছোট করুন।"
        )

    clips = []

    base_video = None
    final_video = None
    bgm = None

    W, H = 1080, 1920

    try:

        current_duration = 0
        idx = 0

        while current_duration < target_duration:

            clip_path = video_paths[
                idx % len(video_paths)
            ]

            clip = VideoFileClip(
                clip_path
            ).without_audio()

            if clip.duration <= 0:

                clip.close()

                raise RuntimeError(
                    "ভিডিও ক্লিপের Duration ভুল।"
                )

            clip_ratio = clip.w / clip.h
            target_ratio = W / H

            if clip_ratio > target_ratio:

                clip = clip.resize(
                    height=H
                )

                clip = clip.crop(
                    x_center=clip.w / 2,
                    width=W
                )

            else:

                clip = clip.resize(
                    width=W
                )

                clip = clip.crop(
                    y_center=clip.h / 2,
                    height=H
                )

            clips.append(clip)

            current_duration += clip.duration

            idx += 1

        base_video = concatenate_videoclips(
            clips,
            method="compose"
        ).subclip(
            0,
            target_duration
        )

        overlay_path = create_brand_overlay(
            W,
            H
        )

        brand_clip = ImageClip(
            overlay_path
        ).set_duration(
            target_duration
        )

        if os.path.exists("bgm.mp3"):

            bgm = AudioFileClip(
                "bgm.mp3"
            )

            bgm = afx.audio_loop(
                bgm,
                duration=target_duration
            ).volumex(
                BGM_VOLUME
            )

            bgm = bgm.audio_fadeout(1.5)

            final_audio = CompositeAudioClip(
                [
                    voice_audio,
                    bgm
                ]
            )

        else:

            final_audio = voice_audio

        final_video = CompositeVideoClip(
            [
                base_video,
                brand_clip
            ]
        ).set_audio(
            final_audio
        )

        final_video.write_videofile(
            output_path,
            fps=30,
            codec="libx264",
            audio_codec="aac",
            bitrate="4000k",
            threads=2,
            logger="bar"
        )

    finally:

        if final_video:
            final_video.close()

        if base_video:
            base_video.close()

        if bgm:
            bgm.close()

        voice_audio.close()

        for clip in clips:

            try:
                clip.close()
            except Exception:
                pass


# =========================================================
# GOOGLE DRIVE UPLOAD
# =========================================================

def upload_to_drive(video_path, title):

    safe_title = re.sub(
        r'[\\/:*?"<>|]',
        "",
        title
    ).strip()

    file_metadata = {
        "name": safe_title[:140] + ".mp4",
        "parents": [
            DRIVE_FOLDER_ID
        ]
    }

    media = MediaFileUpload(
        video_path,
        mimetype="video/mp4",
        resumable=True,
        chunksize=8 * 1024 * 1024
    )

    uploaded = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id,name,webViewLink"
    ).execute()

    file_id = uploaded["id"]

    file_url = uploaded.get(
        "webViewLink",
        f"https://drive.google.com/file/d/{file_id}/view"
    )

    return file_id, file_url


# =========================================================
# TELEGRAM VIDEO + APPROVAL BUTTONS
# =========================================================

def send_to_telegram(
    video_path,
    title,
    row_number
):

    bot_token = os.environ[
        "TELEGRAM_BOT_TOKEN"
    ]

    chat_id = os.environ[
        "TELEGRAM_CHAT_ID"
    ]

    tg_url = (
        f"https://api.telegram.org/"
        f"bot{bot_token}/sendVideo"
    )

    keyboard = {
        "inline_keyboard": [[
            {
                "text": "✅ Approve (YT-এ পোস্ট করুন)",
                "callback_data": f"APPROVE_{row_number}"
            },
            {
                "text": "❌ Reject (বাতিল করুন)",
                "callback_data": f"REJECT_{row_number}"
            }
        ]]
    }

    caption = (
        "🎬 নতুন ব্র্যান্ডেড শর্টস প্রস্তুত!\n\n"
        f"📌 টাইটেল: {title}\n\n"
        "অনুমোদন দিতে নিচের বাটনে ক্লিক করুন:"
    )

    with open(video_path, "rb") as video:

        response = requests.post(
            tg_url,
            data={
                "chat_id": chat_id,
                "caption": caption,
                "reply_markup": json.dumps(keyboard)
            },
            files={
                "video": video
            },
            timeout=600
        )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):

        raise RuntimeError(
            "Telegram Error: " + str(result)
        )

    return result["result"]


# =========================================================
# SELECT FIRST SCRIPT_READY ROW
# =========================================================

def select_row():

    records = sheet.get_all_records()

    for idx, item in enumerate(records):

        row_number = idx + 2

        status = str(
            item.get("Status", "")
        ).strip()

        title = str(
            item.get("Shorts Title (YT)", "")
        ).strip()

        script_text = str(
            item.get("Bangla Voiceover Script", "")
        ).strip()

        if status != "SCRIPT_READY":
            continue

        if not title or not script_text:

            print(
                f"Row {row_number}: Title অথবা Script খালি।"
            )

            continue

        return row_number, item

    return None, None


# =========================================================
# PROCESS ONE SHORT
# =========================================================

def process_row(row_number, item):

    # Claim row before work starts
    update_fields(
        row_number,
        {
            "Status": "PROCESSING",
            "Video Status": "GENERATING",
            "Approval Status": "PENDING",
            "Last Error": ""
        }
    )

    # Read latest data after claiming
    item = get_row_data(
        row_number
    )

    title = str(
        item.get("Shorts Title (YT)", "")
    ).strip()

    script_text = str(
        item.get("Bangla Voiceover Script", "")
    ).strip()

    keywords = str(
        item.get("Image Search Keywords (English)", "")
    ).strip()

    if not title or not script_text:

        raise ValueError(
            "Title অথবা Voiceover Script খালি।"
        )

    print(
        f"ভিডিও তৈরি শুরু: Row {row_number} - {title}"
    )

    # Generate voice
    asyncio.run(
        generate_bangla_voice(
            script_text,
            VOICE_FILE
        )
    )

    # Download stock clips
    clips = download_stock_videos(
        keywords
    )

    # Render final video
    create_short_video(
        VOICE_FILE,
        clips,
        OUTPUT_FILE
    )

    if not os.path.exists(OUTPUT_FILE):

        raise RuntimeError(
            "Final video তৈরি হয়নি।"
        )

    if os.path.getsize(OUTPUT_FILE) == 0:

        raise RuntimeError(
            "Final video ফাইল খালি।"
        )

    print(
        "ভিডিও তৈরি সফল।"
    )

    # Refresh Sheet data
    item = get_row_data(
        row_number
    )

    drive_file_id = str(
        item.get("Drive File ID", "")
    ).strip()

    drive_file_url = str(
        item.get("Drive File URL", "")
    ).strip()

    # Upload to Drive only if no ID exists
    if not drive_file_id:

        drive_file_id, drive_file_url = upload_to_drive(
            OUTPUT_FILE,
            title
        )

        update_fields(
            row_number,
            {
                "Drive File ID": drive_file_id,
                "Drive File URL": drive_file_url,
                "Video Status": "DRIVE_UPLOADED"
            }
        )

    print(
        "Google Drive Upload সফল।"
    )

    # Read latest Telegram details
    item = get_row_data(
        row_number
    )

    old_message_id = str(
        item.get("Telegram Message ID", "")
    ).strip()

    # Do not send duplicate Telegram message
    if old_message_id:

        print(
            "Telegram Message ID আগে থেকেই আছে।"
        )

        return

    # Send video to Telegram
    tg_result = send_to_telegram(
        OUTPUT_FILE,
        title,
        row_number
    )

    msg_id = str(
        tg_result["message_id"]
    )

    tg_file_id = str(
        tg_result["video"]["file_id"]
    )

    # Save Telegram result
    update_fields(
        row_number,
        {
            "Telegram Video File ID": tg_file_id,
            "Telegram Message ID": msg_id,
            "Video Status": "READY_FOR_APPROVAL",
            "Approval Status": "PENDING",
            "Status": "PENDING_APPROVAL",
            "Last Error": ""
        }
    )

    print(
        "Telegram-এ ভিডিও পাঠানো সফল।"
    )


# =========================================================
# MAIN
# =========================================================

def main():

    row_number = None

    try:

        acquire_local_lock()

        row_number, item = select_row()

        if row_number is None:

            print(
                "নতুন SCRIPT_READY Row পাওয়া যায়নি।"
            )

            return

        print(
            f"নির্বাচিত Google Sheet Row: {row_number}"
        )

        process_row(
            row_number,
            item
        )

        print(
            "Shorts Processing সম্পন্ন।"
        )

    except Exception as error:

        error_text = str(error)[:1000]

        print(
            "ERROR:",
            error_text
        )

        traceback.print_exc()

        if row_number:

            try:

                update_fields(
                    row_number,
                    {
                        "Video Status": "ERROR",
                        "Status": "ERROR",
                        "Last Error": error_text
                    }
                )

            except Exception as sheet_error:

                print(
                    "Google Sheet Update Error:",
                    str(sheet_error)
                )

        raise

    finally:

        temporary_files = [
            "raw_voice.mp3",
            VOICE_FILE,
            OUTPUT_FILE,
            "brand_overlay.png"
        ]

        for idx in range(20):

            temporary_files.append(
                f"clip_{idx}.mp4"
            )

        for filename in temporary_files:

            if os.path.exists(filename):

                try:
                    os.remove(filename)

                except Exception:
                    pass

        release_local_lock()


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    main()
