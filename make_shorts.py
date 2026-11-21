import os
import json
import asyncio
import requests
import gspread
import edge_tts
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips

# 1. URL যাতে না কাটে সেজন্য ভেঙে Scope তৈরি করা হলো
creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
base_auth = "https://" + "://googleapis.com" + "/auth/"
scopes = [
    base_auth + "spreadsheets",
    base_auth + "drive"
]
creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
client = gspread.authorize(creds)
sheet = client.open_by_key(os.environ["SHEET_ID"]).worksheet("Shorts_Queue")
drive_service = build("drive", "v3", credentials=creds)

# 2. বাংলা ভয়েসওভার তৈরি (Microsoft Edge TTS)
async def generate_bangla_voice(text, output_file):
    communicate = edge_tts.Communicate(text, "bn-BD-NabanitaNeural")
    await communicate.save(output_file)

# 3. Pixabay থেকে স্টক ভিডিও ডাউনলোড
def download_stock_videos(keywords, count=4):
    api_key = os.environ["PIXABAY_API_KEY"]
    video_files = []
    first_keyword = keywords.split(",").strip()
    query = requests.utils.quote(first_keyword)
    base_pixabay = "https://" + "pixabay.com" + "/api/videos/"
    url = f"{base_pixabay}?key={api_key}&q={query}&per_page=10"
    
    res = requests.get(url).json()
    hits = res.get("hits", [])
    if not hits:
        res = requests.get(f"{base_pixabay}?key={api_key}&q=abstract+technology&per_page=10").json()
        hits = res.get("hits", [])

    selected_hits = [h for h in hits if h["videos"]["medium"]["height"] > h["videos"]["medium"]["width"]]
    if len(selected_hits) < count:
        selected_hits.extend([h for h in hits if h not in selected_hits])

    for idx, vid in enumerate(selected_hits[:count]):
        vid_url = vid["videos"]["medium"]["url"]
        filename = f"clip_{idx}.mp4"
        with open(filename, "wb") as f:
            f.write(requests.get(vid_url).content)
        video_files.append(filename)
    return video_files

# 4. ভিডিও ও অডিও জোড়া লাগিয়ে 9:16 শর্টস রেন্ডার করা
def create_short_video(audio_path, video_paths, output_path):
    audio = AudioFileClip(audio_path)
    target_duration = audio.duration
    clips = []
    current_duration = 0
    idx = 0
    
    while current_duration < target_duration:
        clip_path = video_paths[idx % len(video_paths)]
        clip = VideoFileClip(clip_path).without_audio()
        clip = clip.resize(height=1280)
        if clip.w > 720:
            clip = clip.crop(x_center=clip.w/2, width=720)
        elif clip.w < 720:
            clip = clip.resize(width=720)
            clip = clip.crop(y_center=clip.h/2, height=1280)
        clips.append(clip)
        current_duration += clip.duration
        idx += 1

    final_video = concatenate_videoclips(clips, method="compose").subclip(0, target_duration)
    final_video = final_video.set_audio(audio)
    final_video.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")

# 5. Google Drive-এ ভিডিও আপলোড করে File ID নেওয়া
def upload_to_drive(file_path, file_name):
    file_metadata = {
        "name": file_name,
        "parents": [os.environ["DRIVE_FOLDER_ID"]]
    }
    media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True)
    uploaded_file = drive_service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    return uploaded_file.get("id")

# 6. টেলিগ্রামে ভিডিও ও বাটন পাঠানো
def send_to_telegram(video_path, title, row_index):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    tg_base = "https://" + "api.telegram.org" + f"/bot{bot_token}/sendVideo"
    
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve (YT-এ পোস্ট করুন)", "callback_data": f"APPROVE_{row_index}"},
            {"text": "❌ Reject (বাতিল করুন)", "callback_data": f"REJECT_{row_index}"}
        ]]
    }
    
    with open(video_path, "rb") as vid:
        payload = {
            "chat_id": chat_id,
            "caption": f"🎬 নতুন শর্টস প্রস্তুত!\n\n📌 টাইটেল: {title}\n\nঅনুমোদন দিতে নিচের বাটনে ক্লিক করুন:",
            "reply_markup": json.dumps(keyboard)
        }
        return requests.post(tg_base, data=payload, files={"video": vid}).json()

def main():
    rows = sheet.get_all_values()
    for i in range(1, len(rows)):
        row = rows[i]
        # কলাম ইনডেক্স: 4=Shorts Title, 5=Script, 6=Keywords, 7=Status
        if len(row) > 7 and row == "SCRIPT_READY":
            row_num = i + 1
            title = row
            script_text = row
            keywords = row
            
            print(f"প্রসেসিং শুরু হচ্ছে (Row {row_num}): {title}")
            asyncio.run(generate_bangla_voice(script_text, "voice.mp3"))
            clips = download_stock_videos(keywords)
            create_short_video("voice.mp3", clips, "final_short.mp4")
            
            # Drive-এ সেভ করা
            drive_file_id = upload_to_drive("final_short.mp4", f"short_row_{row_num}.mp4")
            
            # টেলিগ্রামে পাঠানো
            tg_res = send_to_telegram("final_short.mp4", title, row_num)
            if tg_res.get("ok"):
                msg_id = str(tg_res["result"]["message_id"])
                sheet.update_cell(row_num, 8, "PENDING_APPROVAL")
                sheet.update_cell(row_num, 9, drive_file_id)
                sheet.update_cell(row_num, 10, msg_id)
                print("সফলভাবে Drive-এ সেভ ও টেলিগ্রামে পাঠানো হয়েছে!")
            break

if __name__ == "__main__":
    main()
