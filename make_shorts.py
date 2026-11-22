import os
import re
import json
import math
import time
import asyncio
import textwrap
import requests
import gspread
import edge_tts
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright
from oauth2client.service_account import ServiceAccountCredentials
from moviepy.editor import (
    VideoFileClip,
    ImageClip,
    AudioFileClip,
    CompositeVideoClip,
    CompositeAudioClip,
    concatenate_videoclips,
    concatenate_audioclips,
    afx
)

# ==============================================================================
# ভয়েস কনফিগারেশন (সহজ পরিবর্তন নোট)
# ==============================================================================
# নিচের VOICE_NAME পরিবর্তন করে আপনি সহজেই অন্য ভয়েস ব্যবহার করতে পারেন:
# ১. "bn-BD-PradeepNeural"   -> বাংলাদেশি স্মার্ট পুরুষ কণ্ঠ (ডিফল্ট - সবচেয়ে ন্যাচারাল)
# ২. "bn-IN-BashkarNeural"   -> ভারতীয় পুরুষ কণ্ঠ
# ৩. "bn-IN-TanishaaNeural"  -> ভারতীয় নারী কণ্ঠ
# ৪. "bn-BD-NabanitaNeural"  -> বাংলাদেশি নারী কণ্ঠ
VOICE_NAME = "bn-BD-PradeepNeural"
# ==============================================================================

GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SHEET_NAME = "Shorts_Queue"
WIDTH = 1080
HEIGHT = 1920
FPS = 30
WORK_DIR = "temp_build"
os.makedirs(WORK_DIR, exist_ok=True)


def get_bangla_font(size):
    font_paths = [
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in font_paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def connect_google_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)


def get_header_map(sheet):
    headers = sheet.row_values(1)
    return {str(h).strip(): idx + 1 for idx, h in enumerate(headers) if str(h).strip()}


def split_sentences(script_text):
    clean_text = re.sub(r"\s+", " ", str(script_text)).strip()
    parts = [p.strip() for p in re.split(r"(?<=[।!?])\s+", clean_text) if p.strip()]
    return parts if parts else [clean_text]


async def synthesize_dynamic_voice(sentences, out_dir):
    audio_segments = []
    timings = []
    current_time = 0.0
    total = len(sentences)

    for idx, sentence in enumerate(sentences):
        # কণ্ঠের স্বাভাবিক ওঠানামা (Dynamic Prosody) নির্ধারণ
        if idx == 0:
            # প্রথম বাক্য (Hook): একটু দ্রুত ও প্রাণবন্ত
            rate = "+13%"
            pitch = "-1Hz"
            pause_after = 0.25
        elif "?" in sentence:
            # প্রশ্নবোধক বাক্য: সুর কিছুটা উপরে তুলে মানুষের মতো প্রশ্ন করা
            rate = "+9%"
            pitch = "+4Hz"
            pause_after = 0.35
        elif idx == total - 1:
            # শেষ বাক্য (CTA): গুরুত্ব বোঝাতে কিছুটা ধীর ও ভরাট কণ্ঠ
            rate = "+5%"
            pitch = "-5Hz"
            pause_after = 0.30
        else:
            # মাঝের তথ্যমূলক বাক্য: স্বাভাবিক ওঠানামা ও স্পষ্ট উচ্চারণ
            rate = "+8%" if idx % 2 == 1 else "+10%"
            pitch = "-4Hz" if idx % 2 == 1 else "-2Hz"
            pause_after = 0.28

        # বাক্যের ভেতরের কমার (,) জায়গায় স্বাভাবিক শ্বাস নেওয়ার বিরতি
        tts_text = sentence.replace(",", " , ")
        seg_path = os.path.join(out_dir, f"voice_seg_{idx}.mp3")

        communicate = edge_tts.Communicate(
            text=tts_text,
            voice=VOICE_NAME,
            rate=rate,
            pitch=pitch
        )
        await communicate.save(seg_path)

        clip = AudioFileClip(seg_path)
        seg_dur = clip.duration
        audio_segments.append(clip)

        timings.append({
            "text": sentence,
            "start": current_time,
            "end": current_time + seg_dur + pause_after
        })
        current_time += seg_dur + pause_after

    # নীরবতা (Natural Pause) সহ সব অডিও জোড়া লাগানো
    final_clips = []
    for i, clip in enumerate(audio_segments):
        final_clips.append(clip)
        pause = timings[i]["end"] - (timings[i]["start"] + clip.duration)
        if pause > 0.02:
            # শেষের অংশ মিউট করে নীরব ক্লিপ তৈরি
            silence = clip.subclip(0, min(0.05, clip.duration)).volumex(0.0)
            repeats = max(1, int(pause / silence.duration))
            for _ in range(repeats):
                final_clips.append(silence)

    combined = concatenate_audioclips(final_clips)
    master_voice_path = os.path.join(out_dir, "master_voice.mp3")
    combined.write_audiofile(master_voice_path, fps=44100, logger=None)

    for c in audio_segments:
        c.close()
    combined.close()

    return master_voice_path, timings


def capture_mobile_screenshot(post_url, out_path):
    # খাঁটি স্মার্টফোন ভিউপোর্ট (390x844) ও হাই-ডিপিআই স্কেল (2.8x = ~1092px প্রস্থ)
    # এতে ওয়েবসাইট মোবাইল ভার্সনে লোড হবে এবং বাংলা লেখাগুলো অনেক বড় ও স্পষ্ট দেখাবে
    mobile_ua = (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    )
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                device_scale_factor=2.8,
                is_mobile=True,
                has_touch=True,
                user_agent=mobile_ua
            )
            page = context.new_page()
            target = post_url if post_url.startswith("http") else "https://www.homebdinfo.com"
            page.goto(target, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(2500)

            # পপআপ বা কুকি ব্যানার থাকলে লুকানো এবং ফন্ট সামান্য বড় করা
            page.evaluate("""() => {
                document.body.style.zoom = "1.12";
                const ads = document.querySelectorAll('ins, .adsbygoogle, [id*="popup"], [class*="cookie"]');
                ads.forEach(el => el.style.display = 'none');
            }""")
            page.wait_for_timeout(800)

            # পুরো আর্টিকেলের লম্বা স্ক্রিনশট নেওয়া (সর্বোচ্চ ৩২০০ পিক্সেল উচ্চতা পর্যন্ত)
            page.screenshot(path=out_path, full_page=True)
            browser.close()

        img = Image.open(out_path).convert("RGB")
        w, h = img.size
        new_h = int(h * (WIDTH / w))
        img = img.resize((WIDTH, new_h), Image.Resampling.LANCZOS)
        if img.height > 3600:
            img = img.crop((0, 0, WIDTH, 3600))
        img.save(out_path, quality=95)
        return out_path
    except Exception as e:
        print(f"Screenshot fallback triggered: {e}")
        fallback = Image.new("RGB", (WIDTH, 2400), (245, 247, 250))
        draw = ImageDraw.Draw(fallback)
        font_big = get_bangla_font(64)
        draw.text((120, 400), "www.homebdinfo.com", fill=(26, 115, 232), font=font_big)
        fallback.save(out_path)
        return out_path


def download_pixabay_clips(keywords_str, out_dir):
    if not PIXABAY_API_KEY:
        return []
    keywords = [k.strip() for k in str(keywords_str).split(",") if k.strip()]
    if not keywords:
        keywords = ["technology", "education"]

    downloaded = []
    for idx, kw in enumerate(keywords[:3]):
        try:
            url = (
                f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}"
                f"&q={requests.utils.quote(kw)}&per_page=3&safesearch=true"
            )
            res = requests.get(url, timeout=20).json()
            hits = res.get("hits", [])
            if hits:
                videos = hits[0].get("videos", {})
                vid_url = (
                    videos.get("medium", {}).get("url")
                    or videos.get("small", {}).get("url")
                )
                if vid_url:
                    vid_path = os.path.join(out_dir, f"stock_{idx}.mp4")
                    r = requests.get(vid_url, timeout=40)
                    with open(vid_path, "wb") as f:
                        f.write(r.content)
                    downloaded.append(vid_path)
        except Exception as e:
            print(f"Pixabay error for {kw}: {e}")
    return downloaded


def create_overlay_image(text, width, height, font_size=46, bg_rgba=(15, 23, 42, 215), text_color=(255, 255, 255), border_color=None):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [(0, 0), (width - 1, height - 1)],
        radius=28,
        fill=bg_rgba,
        outline=border_color,
        width=4 if border_color else 0
    )
    font = get_bangla_font(font_size)
    wrapped = textwrap.fill(text, width=28)
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center", spacing=12)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (width - tw) // 2
    y = (height - th) // 2
    draw.multiline_text((x, y), wrapped, fill=text_color, font=font, align="center", spacing=12)
    return img


def build_video(row_data, row_num):
    post_url = row_data.get("Post URL", "https://www.homebdinfo.com")
    script_text = row_data.get("Bangla Voiceover Script", "")
    keywords_str = row_data.get("Image Search Keywords (English)", "")

    # ১. ডায়নামিক ন্যাচারাল ভয়েস তৈরি
    sentences = split_sentences(script_text)
    voice_path, timings = asyncio.run(synthesize_dynamic_voice(sentences, WORK_DIR))
    voice_clip = AudioFileClip(voice_path)
    total_duration = voice_clip.duration + 0.5

    # ২. মোবাইল ভিউ স্ক্রিনশট ও স্মুথ অটো-স্ক্রল ব্যাকগ্রাউন্ড
    ss_path = os.path.join(WORK_DIR, f"mobile_ss_{row_num}.jpg")
    capture_mobile_screenshot(post_url, ss_path)
    ss_img = Image.open(ss_path)
    ss_h = ss_img.height

    base_ss_clip = ImageClip(ss_path).set_duration(total_duration)
    if ss_h > HEIGHT:
        max_scroll = ss_h - HEIGHT
        scrolling_bg = base_ss_clip.set_position(
            lambda t: (0, -int((t / total_duration) * max_scroll))
        )
    else:
        scrolling_bg = base_ss_clip.resize(height=HEIGHT).set_position(("center", "center"))

    # ওয়েবসাইটের লেখা স্পষ্ট রাখার জন্য খুবই হালকা টিন্ট (মাত্র ১৫% কালো)
    tint_img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 38))
    tint_path = os.path.join(WORK_DIR, "tint.png")
    tint_img.save(tint_path)
    tint_clip = ImageClip(tint_path).set_duration(total_duration)

    layers = [scrolling_bg, tint_clip]

    # ৩. উপরের সাইট প্রমোশন ব্যাজ
    top_badge_img = create_overlay_image(
        "বিস্তারিত পড়ুন: homebdinfo.com",
        width=760,
        height=86,
        font_size=38,
        bg_rgba=(26, 115, 232, 235),
        text_color=(255, 255, 255),
        border_color=(255, 255, 255, 200)
    )
    top_badge_path = os.path.join(WORK_DIR, "top_badge.png")
    top_badge_img.save(top_badge_path)
    layers.append(
        ImageClip(top_badge_path)
        .set_duration(total_duration)
        .set_position((60, 75))
    )

    # ৪. উপরের ডান কোণায় logo.png ওয়াটারমার্ক
    if os.path.exists("logo.png"):
        logo_clip = (
            ImageClip("logo.png")
            .resize(width=145)
            .set_duration(total_duration)
            .set_position((WIDTH - 185, 65))
        )
        layers.append(logo_clip)

    # ৫. মাঝখানের নিচের দিকে ছোট ফ্রেম করা স্টক ভিডিও কার্ড (যাতে উপরের পোস্ট স্পষ্ট পড়া যায়)
    stock_files = download_pixabay_clips(keywords_str, WORK_DIR)
    if stock_files:
        card_w, card_h = 660, 370
        sub_clips = []
        per_clip_dur = total_duration / len(stock_files)
        for sf in stock_files:
            vc = VideoFileClip(sf).without_audio()
            if vc.duration < per_clip_dur:
                vc = vc.fx(afx.loop, duration=per_clip_dur)
            else:
                vc = vc.subclip(0, per_clip_dur)
            vc = vc.resize(width=card_w)
            if vc.h > card_h:
                vc = vc.crop(y_center=vc.h / 2, height=card_h)
            else:
                vc = vc.resize(height=card_h)
            sub_clips.append(vc)

        pip_video = concatenate_videoclips(sub_clips, method="compose").set_duration(total_duration)

        # কার্ডের জন্য সাদা বর্ডার ফ্রেম
        frame_img = Image.new("RGBA", (card_w + 16, card_h + 16), (255, 255, 255, 235))
        frame_path = os.path.join(WORK_DIR, "pip_frame.png")
        frame_img.save(frame_path)

        card_x = (WIDTH - card_w) // 2
        card_y = 1040
        layers.append(
            ImageClip(frame_path)
            .set_duration(total_duration)
            .set_position((card_x - 8, card_y - 8))
        )
        layers.append(
            pip_video.set_position((card_x, card_y))
        )

    # ৬. নিচের দিকে স্পষ্ট বাংলা সাবটাইটেল বক্স
    for idx, item in enumerate(timings):
        sub_img = create_overlay_image(
            item["text"],
            width=960,
            height=240,
            font_size=46,
            bg_rgba=(10, 15, 28, 225),
            text_color=(255, 255, 255),
            border_color=(56, 189, 248, 180)
        )
        sub_path = os.path.join(WORK_DIR, f"sub_{idx}.png")
        sub_img.save(sub_path)
        dur = max(0.2, item["end"] - item["start"])
        layers.append(
            ImageClip(sub_path)
            .set_start(item["start"])
            .set_duration(dur)
            .set_position(((WIDTH - 960) // 2, 1450))
        )

    # ৭. শেষ ৫ সেকেন্ডে কল-টু-অ্যাকশন (CTA) ব্যানার
    if total_duration > 5.0:
        cta_img = create_overlay_image(
            "সম্পূর্ণ তথ্য ও আবেদন লিংক পেতে Google-এ সার্চ করুন: homebdinfo.com",
            width=960,
            height=140,
            font_size=36,
            bg_rgba=(220, 38, 38, 240),
            text_color=(255, 255, 255),
            border_color=(255, 255, 255, 220)
        )
        cta_path = os.path.join(WORK_DIR, "cta_banner.png")
        cta_img.save(cta_path)
        layers.append(
            ImageClip(cta_path)
            .set_start(total_duration - 5.0)
            .set_duration(5.0)
            .set_position(((WIDTH - 960) // 2, 1715))
        )

    # ৮. ব্যাকগ্রাউন্ড মিউজিক (১২% ভলিউম) + ভয়েসওভার মিক্সিং
    audio_tracks = [voice_clip]
    if os.path.exists("bgm.mp3"):
        bgm = AudioFileClip("bgm.mp3").volumex(0.12)
        if bgm.duration < total_duration:
            bgm = bgm.fx(afx.audio_loop, duration=total_duration)
        else:
            bgm = bgm.subclip(0, total_duration)
        audio_tracks.append(bgm)

    final_audio = CompositeAudioClip(audio_tracks)
    final_video = CompositeVideoClip(layers, size=(WIDTH, HEIGHT)).set_audio(final_audio).set_duration(total_duration)

    output_mp4 = os.path.join(WORK_DIR, f"short_row_{row_num}.mp4")
    final_video.write_videofile(
        output_mp4,
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        threads=4,
        preset="medium",
        logger=None
    )
    return output_mp4


def upload_to_github_release(video_path, row_num):
    tag_name = f"shorts-row-{row_num}-{int(time.time())}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    rel_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    payload = {
        "tag_name": tag_name,
        "name": f"Bangla Short Row {row_num}",
        "body": "Automated YouTube Short generated via GitHub Actions.",
        "draft": False,
        "prerelease": False
    }
    res = requests.post(rel_url, headers=headers, json=payload, timeout=30)
    res.raise_for_status()
    rel_data = res.json()
    release_id = str(rel_data["id"])
    upload_url = rel_data["upload_url"].split("{")[0]

    file_name = os.path.basename(video_path)
    with open(video_path, "rb") as f:
        up_headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Content-Type": "video/mp4",
        }
        up_res = requests.post(
            f"{upload_url}?name={file_name}",
            headers=up_headers,
            data=f,
            timeout=120
        )
        up_res.raise_for_status()
        asset_data = up_res.json()

    return release_id, asset_data["browser_download_url"]


def send_telegram_approval(video_path, row_num, title, post_url):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    caption = (
        f"🎬 <b>নতুন Bangla Short প্রস্তুত!</b>\n\n"
        f"📌 <b>শিরোনাম:</b> {title}\n"
        f"🔗 <b>পোস্ট:</b> {post_url}\n"
        f"📊 <b>Sheet Row:</b> {row_num}"
    )
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"APPROVE_{row_num}"},
                {"text": "❌ Reject", "callback_data": f"REJECT_{row_num}"}
            ]
        ]
    }
    with open(video_path, "rb") as vf:
        res = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(reply_markup)
            },
            files={"video": vf},
            timeout=120
        )
        res.raise_for_status()
        result = res.json()["result"]
        return result["video"]["file_id"], str(result["message_id"])


def main():
    sheet = connect_google_sheet()
    col_map = get_header_map(sheet)
    rows = sheet.get_all_values()

    target_row = None
    row_data = {}
    for idx in range(1, len(rows)):
        status_val = rows[idx][col_map["Status"] - 1].strip()
        if status_val == "SCRIPT_READY":
            target_row = idx + 1
            for h, c_idx in col_map.items():
                row_data[h] = rows[idx][c_idx - 1] if c_idx - 1 < len(rows[idx]) else ""
            break

    if not target_row:
        print("কোনো SCRIPT_READY রো পাওয়া যায়নি।")
        return

    try:
        sheet.update_cell(target_row, col_map["Status"], "PROCESSING")
        sheet.update_cell(target_row, col_map["Video Status"], "GENERATING")

        video_path = build_video(row_data, target_row)
        rel_id, download_url = upload_to_github_release(video_path, target_row)
        tg_file_id, tg_msg_id = send_telegram_approval(
            video_path,
            target_row,
            row_data.get("Shorts Title (YT)", ""),
            row_data.get("Post URL", "")
        )

        sheet.update_cell(target_row, col_map["Drive File ID"], rel_id)
        sheet.update_cell(target_row, col_map["Drive File URL"], download_url)
        sheet.update_cell(target_row, col_map["Telegram Video File ID"], tg_file_id)
        sheet.update_cell(target_row, col_map["Telegram Message ID"], tg_msg_id)
        sheet.update_cell(target_row, col_map["Video Status"], "COMPLETED")
        sheet.update_cell(target_row, col_map["Status"], "PENDING_APPROVAL")
        print(f"Row {target_row} সফলভাবে প্রসেস হয়েছে।")

    except Exception as e:
        err_msg = str(e)[:900]
        sheet.update_cell(target_row, col_map["Video Status"], "FAILED")
        sheet.update_cell(target_row, col_map["Last Error"], err_msg)
        raise


if __name__ == "__main__":
    main()
