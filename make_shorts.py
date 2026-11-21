import os
import json
import asyncio
import requests
import gspread
import edge_tts
from oauth2client.service_account import ServiceAccountCredentials
from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips

# 1. Google Sheet Connection
creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(os.environ["SHEET_ID"]).worksheet("Shorts_Queue")

# 2. Bangla Voiceover Generation (Microsoft Edge TTS)
async def generate_bangla_voice(text, output_file):
    communicate = edge_tts.Communicate(text, "bn-BD-NabanitaNeural")
    await communicate.save(output_file)

# 3. Download Stock Videos from Pixabay
def download_stock_videos(keywords, count=4):
    api_key = os.environ["PIXABAY_API_KEY"]
    video_files = []
    first_keyword = keywords.split(",").strip()
    query = requests.utils.quote(first_keyword)
    url = f"https://pixabay.com/api/videos/?key={api_key}&q={query}&per_page=10"
    
    res = requests.get(url).json()
    hits = res.get("hits", [])
    
    if not hits:
        url = f"https://pixabay.com/api/videos/?key={api_key}&q=abstract+technology&per_page=10"
        res = requests.get(url).json()
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

# 4. Render 9:16 Shorts Video
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

# 5. Send Video to Telegram for Approval
def send_to_telegram(video_path, title, row_index):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{bot_token}/sendVideo"
    
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve (পোস্ট করুন)", "callback_data": f"APPROVE_{row_index}"},
            {"text": "❌ Reject (বাতিল করুন)", "callback_data": f"REJECT_{row_index}"}
        ]]
    }
    
    with open(video_path, "rb") as vid:
        payload = {
            "chat_id": chat_id,
            "caption": f"🎬 নতুন শর্টস প্রস্তুত!\n\n📌 টাইটেল: {title}\n\nঅনুমোদন দিতে নিচের বাটনে ক্লিক করুন:",
            "reply_markup": json.dumps(keyboard)
        }
        res = requests.post(url, data=payload, files={"video": vid}).json()
        return res

# Main Execution
def main():
    rows = sheet.get_all_values()
    for i in range(1, len(rows)):
        row = rows[i]
        status = row
        if status == "SCRIPT_READY":
            row_num = i + 1
            title = row
            script_text = row
            keywords = row
            
            print(f"Processing started (Row {row_num}): {title}")
            
            asyncio.run(generate_bangla_voice(script_text, "voice.mp3"))
            clips = download_stock_videos(keywords)
            create_short_video("voice.mp3", clips, "final_short.mp4")
            
            tg_res = send_to_telegram("final_short.mp4", title, row_num)
            
            if tg_res.get("ok"):
                msg_id = str(tg_res["result"]["message_id"])
                sheet.update_cell(row_num, 8, "PENDING_APPROVAL")
                sheet.update_cell(row_num, 10, msg_id)
                print("Successfully sent to Telegram!")
            break

if __name__ == "__main__":
    main()
