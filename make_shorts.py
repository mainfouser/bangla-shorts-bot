import os
import json
import base64
import asyncio
import requests
import gspread
import edge_tts
from PIL import Image, ImageDraw, ImageFont
from google.oauth2.service_account import Credentials
from moviepy.editor import VideoFileClip, AudioFileClip, ImageClip, CompositeVideoClip, concatenate_videoclips

# ================= ব্র্যান্ডিং কনফিগারেশন =================
BRAND_NAME = "Home BD Info | homebdinfo.com"  # ভিডিওর নিচে আপনার ব্র্যান্ডের নাম দেখাবে
VOICE_NAME = "bn-BD-PradeepNeural"          # ন্যাচারাল পুরুষ কণ্ঠ (প্রদীপ)
# ==========================================================

# 1. Google Sheet কানেকশন
creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
scopes = [
    base64.b64decode("aHR0cHM6Ly93d3cuZ29vZ2xlYXBpcy5jb20vYXV0aC9zcHJlYWRzaGVldHM=").decode("utf-8"),
    base64.b64decode("aHR0cHM6Ly93d3cuZ29vZ2xlYXBpcy5jb20vYXV0aC9kcml2ZQ==").decode("utf-8")
]
creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
client = gspread.authorize(creds)
sheet = client.open_by_key(os.environ["SHEET_ID"]).worksheet("Shorts_Queue")

# 2. ন্যাচারাল পুরুষ কণ্ঠে বাংলা ভয়েসওভার তৈরি
async def generate_bangla_voice(text, output_file):
    # rate="+5%" দিলে কণ্ঠ আরও স্বাভাবিক ও প্রাণবন্ত শোনায়
    communicate = edge_tts.Communicate(text, VOICE_NAME, rate="+5%", pitch="+0Hz")
    await communicate.save(output_file)

# 3. Pixabay থেকে হাই-কোয়ালিটি স্টক ভিডিও ডাউনলোড
def download_stock_videos(keywords, count=4):
    api_key = os.environ["PIXABAY_API_KEY"]
    video_files = []
    first_keyword = keywords.split(",").pop(0).strip() if keywords else "technology"
    query = requests.utils.quote(first_keyword)
    base_pixabay = base64.b64decode("aHR0cHM6Ly9waXhhYmF5LmNvbS9hcGkvdmlkZW9zLw==").decode("utf-8")
    url = f"{base_pixabay}?key={api_key}&q={query}&per_page=15"
    
    res = requests.get(url).json()
    hits = res.get("hits", [])
    if not hits:
        res = requests.get(f"{base_pixabay}?key={api_key}&q=abstract+modern&per_page=15").json()
        hits = res.get("hits", [])

    # পোর্ট্রেট ভিডিও আগে নেওয়া, যাতে জুম হয়ে ফেটে না যায়
    selected_hits = [h for h in hits if h["videos"]["medium"]["height"] >= h["videos"]["medium"]["width"]]
    if len(selected_hits) < count:
        selected_hits.extend([h for h in hits if h not in selected_hits])

    for idx, vid in enumerate(selected_hits[:count]):
        # বড় ও পরিষ্কার রেজোলিউশন নেওয়া
        vid_data = vid["videos"].get("large") or vid["videos"].get("medium")
        vid_url = vid_data["url"]
        filename = f"clip_{idx}.mp4"
        with open(filename, "wb") as f:
            f.write(requests.get(vid_url).content)
        video_files.append(filename)
    return video_files

# 4. ব্র্যান্ডিং ওয়াটারমার্ক ব্যানার তৈরি (Pillow দিয়ে স্বচ্ছ ব্যানার)
def create_brand_overlay(width=1080, height=1920, output_png="brand_overlay.png"):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # নিচের দিকে সেমি-ট্রান্সপারেন্ট কালো ব্র্যান্ড পিল/বার আঁকা
    bar_w, bar_h = 680, 76
    x1 = (width - bar_w) // 2
    y1 = height - 220
    draw.rounded_rectangle([x1, y1, x1 + bar_w, y1 + bar_h], radius=38, fill=(15, 23, 42, 195), outline=(59, 130, 246, 230), width=3)
    
    # ব্র্যান্ডের নাম লেখা
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
    except:
        font = ImageFont.load_default()
        
    bbox = draw.textbbox((0, 0), BRAND_NAME, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.text(((width - text_w) // 2, y1 + (bar_h - text_h) // 2 - 4), BRAND_NAME, font=font, fill=(255, 255, 255, 245))
    
    img.save(output_png)
    return output_png

# 5. Full HD (1080x1920) ব্র্যান্ডেড শর্টস রেন্ডার করা
def create_short_video(audio_path, video_paths, output_path):
    audio = AudioFileClip(audio_path)
    target_duration = audio.duration
    clips = []
    current_duration = 0
    idx = 0
    
    W, H = 1080, 1920  # Full HD 9:16 রেজোলিউশন
    
    while current_duration < target_duration:
        clip_path = video_paths[idx % len(video_paths)]
        clip = VideoFileClip(clip_path).without_audio()
        
        # অনুপাত ঠিক রেখে ক্রপ করা যাতে ভিডিও চ্যাপ্টা বা ব্লার না হয়
        clip_ratio = clip.w / clip.h
        target_ratio = W / H
        
        if clip_ratio > target_ratio:
            clip = clip.resize(height=H)
            clip = clip.crop(x_center=clip.w / 2, width=W)
        else:
            clip = clip.resize(width=W)
            clip = clip.crop(y_center=clip.h / 2, height=H)
            
        clips.append(clip)
        current_duration += clip.duration
        idx += 1

    base_video = concatenate_videoclips(clips, method="compose").subclip(0, target_duration)
    
    # ব্র্যান্ড ওভারলে যুক্ত করা
    overlay_path = create_brand_overlay(W, H)
    brand_clip = ImageClip(overlay_path).set_duration(target_duration)
    layers = [base_video, brand_clip]
    
    # যদি রিপোজিটরিতে logo.png আপলোড করা থাকে, তবে উপরে ডানদিকে লোগো বসাবে
    if os.path.exists("logo.png"):
        logo = ImageClip("logo.png").resize(width=160).set_duration(target_duration).set_position((W - 210, 80))
        layers.append(logo)
        
    final_video = CompositeVideoClip(layers).set_audio(audio)
    final_video.write_videofile(output_path, fps=30, codec="libx264", audio_codec="aac", bitrate="4000k")

# 6. টেলিগ্রামে ভিডিও ও বাটন পাঠানো
def send_to_telegram(video_path, title, row_index):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    tg_host = base64.b64decode("aHR0cHM6Ly9hcGkudGVsZWdyYW0ub3Jn").decode("utf-8")
    tg_base = f"{tg_host}/bot{bot_token}/sendVideo"
    
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve (YT-এ পোস্ট করুন)", "callback_data": f"APPROVE_{row_index}"},
            {"text": "❌ Reject (বাতিল করুন)", "callback_data": f"REJECT_{row_index}"}
        ]]
    }
    
    with open(video_path, "rb") as vid:
        payload = {
            "chat_id": chat_id,
            "caption": f"🎬 নতুন ব্র্যান্ডেড শর্টস প্রস্তুত!\n\n📌 টাইটেল: {title}\n\nঅনুমোদন দিতে নিচের বাটনে ক্লিক করুন:",
            "reply_markup": json.dumps(keyboard)
        }
        return requests.post(tg_base, data=payload, files={"video": vid}).json()

def main():
    records = sheet.get_all_records()
    for idx, item in enumerate(records):
        row_num = idx + 2
        status = str(item.get("Status", "")).strip()
        
        if status == "SCRIPT_READY":
            title = str(item.get("Shorts Title (YT)", ""))
            script_text = str(item.get("Bangla Voiceover Script", ""))
            keywords = str(item.get("Image Search Keywords (English)", "bangladesh"))
            
            print(f"প্রসেসিং শুরু হচ্ছে (Row {row_num}): {title}")
            asyncio.run(generate_bangla_voice(script_text, "voice.mp3"))
            clips = download_stock_videos(keywords)
            create_short_video("voice.mp3", clips, "final_short.mp4")
            
            tg_res = send_to_telegram("final_short.mp4", title, row_num)
            if tg_res.get("ok"):
                msg_id = str(tg_res["result"]["message_id"])
                tg_file_id = str(tg_res["result"]["video"]["file_id"])
                sheet.update_cell(row_num, 8, "PENDING_APPROVAL")
                sheet.update_cell(row_num, 9, tg_file_id)
                sheet.update_cell(row_num, 10, msg_id)
                print("সফলভাবে টেলিগ্রামে পাঠানো হয়েছে!")
            break

if __name__ == "__main__":
    main()
