import os
import json
import time
import random
import asyncio
import requests
import subprocess
import gspread
import edge_tts
from google.oauth2.service_account import Credentials
from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips

# ============================================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ============================================================
SHEET_NAME = "Shorts_Queue"
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDENTIALS")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot")

VOICE_NAME = "bn-BD-NabanitaNeural"


# ============================================================
# 2. GOOGLE SHEET CONNECTION
# ============================================================
def get_sheet():
    if not GOOGLE_CREDS_JSON or not SPREADSHEET_ID:
        raise ValueError("GOOGLE_CREDENTIALS বা SPREADSHEET_ID পাওয়া যায়নি।")
    
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def get_header_map(sheet):
    headers = sheet.row_values(1)
    return {header.strip(): idx + 1 for idx, header in enumerate(headers)}


def update_cells(sheet, row_num, header_map, updates):
    cells_to_update = []
    for col_name, value in updates.items():
        if col_name in header_map:
            col_idx = header_map[col_name]
            cells_to_update.append(gspread.Cell(row=row_num, col=col_idx, value=str(value)))
    if cells_to_update:
        sheet.update_cells(cells_to_update)


# ============================================================
# 3. AUDIO GENERATION (EDGE-TTS + FFMPEG)
# ============================================================
async def _generate_tts(text, output_file):
    communicate = edge_tts.Communicate(text, VOICE_NAME)
    await communicate.save(output_file)


def create_voiceover(script_text):
    raw_audio = "raw_voice.mp3"
    final_audio = "voice.mp3"
    
    asyncio.run(_generate_tts(script_text, raw_audio))
    
    cmd = [
        "ffmpeg", "-y", "-i", raw_audio,
        "-ar", "44100", "-ac", "1", "-b:a", "192k",
        final_audio
    ]
    subprocess.run(cmd, check=True)
    return final_audio


# ============================================================
# 4. STOCK VIDEO DOWNLOADER (PEXELS)
# ============================================================
def download_pexels_videos(keywords, target_duration):
    if not PEXELS_API_KEY:
        raise ValueError("PEXELS_API_KEY পাওয়া যায়নি।")
        
    headers = {"Authorization": PEXELS_API_KEY}
    downloaded_files = []
    total_dur = 0.0
    
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    if not kw_list:
        kw_list = ["bangladesh", "nature", "technology"]

    idx = 0
    while total_dur < target_duration and idx < 15:
        query = kw_list[idx % len(kw_list)]
        url = f"https://api.pexels.com/videos/search?query={query}&orientation=portrait&size=medium&per_page=5"
        res = requests.get(url, headers=headers, timeout=15)
        
        if res.status_code == 200:
            videos = res.json().get("videos", [])
            if videos:
                video = random.choice(videos)
                video_files = video.get("video_files", [])
                chosen_file = next(
                    (vf for vf in video_files if vf.get("width", 0) >= 720 and vf.get("file_type") == "video/mp4"),
                    video_files[0] if video_files else None
                )
                if chosen_file:
                    v_url = chosen_file["link"]
                    file_name = f"clip_{idx}.mp4"
                    with requests.get(v_url, stream=True, timeout=30) as r:
                        r.raise_for_status()
                        with open(file_name, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                    
                    clip = VideoFileClip(file_name)
                    total_dur += clip.duration
                    clip.close()
                    downloaded_files.append(file_name)
        idx += 1

    if not downloaded_files:
        raise RuntimeError("Pexels থেকে কোনো ব্যাকগ্রাউন্ড ভিডিও ডাউনলোড করা যায়নি।")
        
    return downloaded_files


# ============================================================
# 5. VIDEO ASSEMBLY (MOVIEPY)
# ============================================================
def build_short_video(audio_path, clip_paths, output_path="final_short.mp4"):
    audio = AudioFileClip(audio_path)
    target_duration = audio.duration + 0.5
    
    clips = []
    current_dur = 0.0
    
    for path in clip_paths:
        if current_dur >= target_duration:
            break
        clip = VideoFileClip(path).without_audio()
        
        clip = clip.resize(height=1920)
        if clip.w < 1080:
            clip = clip.resize(width=1080)
        clip = clip.crop(x_center=clip.w / 2, y_center=clip.h / 2, width=1080, height=1920)
        
        needed = target_duration - current_dur
        if clip.duration > needed:
            clip = clip.subclip(0, needed)
            
        clips.append(clip)
        current_dur += clip.duration

    final_video = concatenate_videoclips(clips, method="compose")
    final_video = final_video.set_audio(audio)
    
    final_video.write_videofile(
        output_path,
        fps=30,
        codec="libx264",
        audio_codec="aac",
        threads=4,
        preset="medium"
    )
    
    audio.close()
    final_video.close()
    for c in clips:
        c.close()
        
    print("ভিডিও তৈরি সফল।")
    return output_path


# ============================================================
# 6. UPLOAD TO GITHUB RELEASES (REPLACES GOOGLE DRIVE)
# ============================================================
def upload_to_github_release(file_path, file_name):
    if not GITHUB_TOKEN:
        raise ValueError("GITHUB_TOKEN পাওয়া যায়নি।")

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    tag_name = f"vid-{int(time.time())}"
    release_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    payload = {
        "tag_name": tag_name,
        "name": f"Shorts Video {tag_name}",
        "body": "Temporary video storage for Shorts automation",
        "draft": False,
        "prerelease": True
    }

    res = requests.post(release_url, headers=headers, json=payload, timeout=30)
    res.raise_for_status()
    release_data = res.json()

    release_id = str(release_data["id"])
    upload_url = release_data["upload_url"].split("{")[0]

    upload_headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "video/mp4"
    }
    
    with open(file_path, "rb") as f:
        up_res = requests.post(
            upload_url,
            headers=upload_headers,
            params={"name": file_name},
            data=f,
            timeout=120
        )
        up_res.raise_for_status()
        asset_data = up_res.json()

    download_url = asset_data["browser_download_url"]
    print(f"GitHub Release আপলোড সফল: {download_url}")
    return release_id, download_url


# ============================================================
# 7. SEND TO TELEGRAM FOR APPROVAL
# ============================================================
def send_telegram_approval(video_path, row_num, title, download_url):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN বা TELEGRAM_CHAT_ID পাওয়া যায়নি।")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    
    caption = (
        f"🎬 <b>নতুন Shorts ভিডিও প্রস্তুত!</b>\n\n"
        f"📌 <b>Title:</b> {title}\n"
        f"📊 <b>Sheet Row:</b> {row_num}\n"
        f"🔗 <a href='{download_url}'>Direct Video Link</a>\n\n"
        f"পাবলিশ করার জন্য নিচে Approve বাটনে ক্লিক করুন:"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"APPROVE_{row_num}"},
                {"text": "❌ Reject", "callback_data": f"REJECT_{row_num}"}
            ]
        ]
    }

    with open(video_path, "rb") as video_file:
        res = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(reply_markup)
            },
            files={"video": video_file},
            timeout=120
        )
        res.raise_for_status()
        result = res.json()

    tg_file_id = result["result"]["video"]["file_id"]
    tg_msg_id = result["result"]["message_id"]
    print(f"Telegram-এ পাঠানো হয়েছে। Message ID: {tg_msg_id}")
    return tg_file_id, tg_msg_id


# ============================================================
# 8. MAIN EXECUTION FLOW
# ============================================================
def process_row(sheet, row_num, row_data, header_map):
    title = row_data[header_map["Shorts Title (YT)"] - 1]
    script = row_data[header_map["Bangla Voiceover Script"] - 1]
    keywords = row_data[header_map["Image Search Keywords (English)"] - 1]

    print(f"ভিডিও তৈরি শুরু: Row {row_num} - {title}")

    update_cells(sheet, row_num, header_map, {
        "Status": "PROCESSING",
        "Video Status": "GENERATING"
    })

    audio_path = create_voiceover(script)
    audio_clip = AudioFileClip(audio_path)
    duration = audio_clip.duration
    audio_clip.close()

    clip_paths = download_pexels_videos(keywords, duration)
    video_path = build_short_video(audio_path, clip_paths, "final_short.mp4")

    file_name = f"short_row_{row_num}_{int(time.time())}.mp4"
    release_id, release_url = upload_to_github_release(video_path, file_name)

    tg_file_id, tg_msg_id = send_telegram_approval(video_path, row_num, title, release_url)

    update_cells(sheet, row_num, header_map, {
        "Status": "PENDING_APPROVAL",
        "Video Status": "READY",
        "Drive File ID": release_id,
        "Drive File URL": release_url,
        "Telegram Video File ID": tg_file_id,
        "Telegram Message ID": tg_msg_id,
        "Last Error": ""
    })
    print(f"Row {row_num} এর সম্পূর্ণ প্রক্রিয়া সফলভাবে শেষ হয়েছে!")


def main():
    sheet = get_sheet()
    header_map = get_header_map(sheet)
    all_rows = sheet.get_all_values()

    selected_row = None
    selected_data = None

    for idx in range(1, len(all_rows)):
        row = all_rows[idx]
        status = row[header_map["Status"] - 1].strip() if len(row) >= header_map["Status"] else ""
        if status == "SCRIPT_READY":
            selected_row = idx + 1
            selected_data = row
            break

    if not selected_row:
        print("প্রসেস করার মতো কোনো SCRIPT_READY রো পাওয়া যায়নি।")
        return

    print(f"নির্বাচিত Google Sheet Row: {selected_row}")

    try:
        process_row(sheet, selected_row, selected_data, header_map)
    except Exception as e:
        error_msg = str(e)[:1000]
        print(f"ERROR: {error_msg}")
        update_cells(sheet, selected_row, header_map, {
            "Status": "ERROR",
            "Video Status": "FAILED",
            "Last Error": error_msg
        })
        raise


if __name__ == "__main__":
    main()
