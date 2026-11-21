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
BRAND_NAME = "Home BD Info  |  homebdinfo.com"
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

    selected_hits = [h for h in hits if h["videos"]["medium"]["height"] >= h["videos"]["medium"]["width"]]
    if len(selected_hits) < count:
        selected_hits.extend([h for h in hits if h not in selected_hits])

    for idx, vid in enumerate(selected_hits[:count]):
        vid_data = vid["videos"].get("large") or vid["videos"].get("medium")
        vid_url = vid_data["url"]
        filename = f"clip_{idx}.mp4"
        with open(filename, "wb") as f:
            f.write(requests.get(vid_url).content)
        video_files.append(filename)
    return video_files

# 4. লোগোটিকে সুন্দর গোল ব্যাজে রূপান্তর ও নিচের ব্র্যান্ড বার তৈরি (ব্র্যাকেট-মুক্ত নিরাপদ কোড)
def create_brand_overlay(width=1080, height=1920, output_png="brand_overlay.png"):
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    
    # ক) উপরে ডানদিকে লোগো ব্যাজ বসানো (যদি logo.png থাকে)
    if os.path.exists("logo.png"):
        badge_size = 150
        badge = Image.new("RGBA", (badge_size, badge_size), (0, 0, 0, 0))
        b_draw = ImageDraw.Draw(badge)
        b_draw.ellipse((0, 0, badge_size-1, badge_size-1), fill=(255, 255, 255, 245), outline=(22, 163, 74, 255), width=6)
        
        logo_img = Image.open("logo.png").convert("RGBA")
        inner_size = 105
        logo_img = logo_img.resize((inner_size, inner_size), Image.LANCZOS)
        offset = (badge_size - inner_size) // 2
        badge.paste(logo_img, (offset, offset), logo_img)
        
        canvas.paste(badge, (width - badge_size - 55, 95), badge)

    # খ) নিচের দিকে প্রফেশনাল ব্র্যান্ড পিল/বার
    bar_w, bar_h = 720, 74
    x1 = (width - bar_w) // 2
    y1 = height - 290
    
    draw.rounded_rectangle(
        (x1, y1, x1 + bar_w, y1 + bar_h), 
        radius=37, 
        fill=(15, 23, 42, 205), 
        outline=(22, 163, 74, 240), 
        width=4
    )
    
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 31)
    except Exception:
        font = ImageFont.load_default()
        
    # anchor="mm" দিলে কোনো ব্র্যাকেট ছাড়াই লেখাটি বক্সের ঠিক মাঝখানে বসবে
    center_x = width // 2
    center_y = y1 + (bar_h // 2)
    draw.text((center_x, center_y), BRAND_NAME, font=font, fill=(255, 255, 255, 250), anchor="mm")
    
    canvas.save(output_png)
    return output_png

# 5. Full HD (1080x1920) ব্র্যান্ডেড শর্টস রেন্ডার করা
def create_short_video(audio_path, video_paths, output_path):
    audio = AudioFileClip(audio_path)
    target_duration = audio.duration
    clips = []
    current_duration = 0
    idx = 0
    
    W, H = 1080, 1920
    
    while current_duration < target_duration:
        clip_path = video_paths[idx % len(video_paths)]
        clip = VideoFileClip(clip_path).without_audio()
        
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
    
    overlay_path = create_brand_overlay(W, H)
    brand_clip = ImageClip(overlay_path).set_duration(target_duration)
    
    final_video = CompositeVideoClip([base_video, brand_clip]).set_audio(audio)
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
