import os
import re
import json
import time
import asyncio
import requests
import gspread
from google.oauth2.service_account import Credentials
import edge_tts
from pydub import AudioSegment
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright
from moviepy.editor import (
    VideoFileClip,
    ImageClip,
    AudioFileClip,
    CompositeVideoClip,
    CompositeAudioClip,
    concatenate_videoclips,
    afx
)

# ==============================================================================
# ভয়েস কনফিগারেশন (সহজেই নিচের নাম পরিবর্তন করে ভয়েস বদলাতে পারবেন)
# ==============================================================================
# জনপ্রিয় বাংলা ভয়েস অপশন:
# 1. "bn-BD-PradeepNeural"   -> বাংলাদেশি স্মার্ট ও গম্ভীর পুরুষ কণ্ঠ (Recommended)
# 2. "bn-IN-BashkarNeural"   -> ভারতীয় বাংলা পুরুষ কণ্ঠ
# 3. "bn-BD-NabanitaNeural"  -> বাংলাদেশি নারী কণ্ঠ
# 4. "bn-IN-TanishaaNeural"  -> ভারতীয় বাংলা নারী কণ্ঠ

VOICE_NAME = "bn-BD-PradeepNeural"
# ==============================================================================

GITHUB_REPO = "mainfouser/bangla-shorts-bot"
SHEET_NAME = "Shorts_Queue"
WIDTH = 1080
HEIGHT = 1920
FPS = 30

TEMP_DIR = "temp_build"
os.makedirs(TEMP_DIR, exist_ok=True)


def get_google_sheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    sheet_id = os.environ.get("SHEET_ID")
    if not creds_json or not sheet_id:
        raise ValueError("GOOGLE_CREDENTIALS অথবা SHEET_ID পাওয়া যায়নি।")

    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id).worksheet(SHEET_NAME)


def split_sentences_natural(script_text):
    cleaned = re.sub(r"\s+", " ", script_text.strip())
    parts = [p.strip() for p in re.split(r"(?<=[।!?\.])\s+", cleaned) if p.strip()]
    return parts


async def generate_dynamic_voiceover(sentences, output_audio_path):
    combined_audio = AudioSegment.empty()
    timings = []
    current_ms = 0

    total = len(sentences)
    for idx, sentence in enumerate(sentences):
        seg_file = os.path.join(TEMP_DIR, f"voice_seg_{idx}.mp3")

        # বাক্যের ধরন অনুযায়ী মানুষের মতো কণ্ঠের ওঠানামা (Prosody) নির্ধারণ
        if idx == 0:
            # প্রথম বাক্য (Hook): একটু উচ্ছ্বসিত ও দ্রুত
            rate = "+13%"
            pitch = "-1Hz"
            pause_ms = 320
        elif "?" in sentence:
            # প্রশ্নবোধক বাক্য: সুর একটু উপরে উঠবে
            rate = "+9%"
            pitch = "+4Hz"
            pause_ms = 380
        elif idx == total - 1:
            # শেষ বাক্য (CTA): গুরুত্ব বোঝাতে একটু ধীর ও ভরাট
            rate = "+5%"
            pitch = "-5Hz"
            pause_ms = 200
        else:
            # সাধারণ বাক্য: স্বাভাবিক ও স্পষ্ট
            rate = "+8%"
            pitch = "-4Hz"
            pause_ms = 260

        communicate = edge_tts.Communicate(
            text=sentence,
            voice=VOICE_NAME,
            rate=rate,
            pitch=pitch
        )
        await communicate.save(seg_file)

        seg_audio = AudioSegment.from_mp3(seg_file)
        pause_audio = AudioSegment.silent(duration=pause_ms)

        start_sec = current_ms / 1000.0
        combined_audio += seg_audio + pause_audio
        current_ms += len(seg_audio) + pause_ms
        end_sec = current_ms / 1000.0

        timings.append({
            "text": sentence,
            "start": start_sec,
            "end": end_sec
        })

    combined_audio.export(output_audio_path, format="mp3")
    return timings, (current_ms / 1000.0)


def capture_mobile_website_screenshot(post_url, output_img_path):
    target_url = post_url if (post_url and post_url.startswith("http")) else "https://www.homebdinfo.com"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                device_scale_factor=2.8,
                is_mobile=True,
                has_touch=True,
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Mobile Safari/537.36"
                )
            )
            page = context.new_page()
            page.goto(target_url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(2000)

            # লেখাগুলো আরও বড় ও স্পষ্ট দেখানোর জন্য হালকা জুম এবং পপআপ লুকানো
            page.add_style_tag(content="""
                body { zoom: 1.12 !important; }
                .cookie-choices-info, #cookieChoiceInfo, .adsbygoogle { display: none !important; }
            """)
            page.wait_for_timeout(800)
            page.screenshot(path=output_img_path, full_page=True)
            browser.close()
            return True
    except Exception as e:
        print(f"Screenshot warning: {e}")
        # ফলব্যাক ইমেজ তৈরি
        img = Image.new("RGB", (WIDTH, HEIGHT * 2), color=(245, 247, 250))
        draw = ImageDraw.Draw(img)
        font = get_bangla_font(48)
        draw.text((120, 400), "বিস্তারিত তথ্য জানতে ভিজিট করুন:\nwww.homebdinfo.com", fill=(20, 40, 80), font=font)
        img.save(output_img_path)
        return False


def download_pixabay_videos(keywords_str, target_duration):
    api_key = os.environ.get("PIXABAY_API_KEY")
    if not api_key:
        return []

    keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]
    if not keywords:
        keywords = ["technology", "information", "smartphone"]

    downloaded_files = []
    for idx, kw in enumerate(keywords[:3]):
        try:
            url = (
                f"https://pixabay.com/api/videos/?key={api_key}"
                f"&q={requests.utils.quote(kw)}&per_page=5&safesearch=true"
            )
            resp = requests.get(url, timeout=20).json()
            hits = resp.get("hits", [])
            if not hits:
                continue

            best_hit = hits[0]
            videos = best_hit.get("videos", {})
            video_url = (
                videos.get("medium", {}).get("url")
                or videos.get("small", {}).get("url")
            )
            if not video_url:
                continue

            out_path = os.path.join(TEMP_DIR, f"stock_{idx}.mp4")
            v_data = requests.get(video_url, timeout=30).content
            with open(out_path, "wb") as f:
                f.write(v_data)
            downloaded_files.append(out_path)
        except Exception as e:
            print(f"Pixabay download warning ({kw}): {e}")

    return downloaded_files


def get_bangla_font(size):
    font_candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    ]
    for path in font_candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def wrap_text_pixels(text, font, max_width, draw):
    words = text.split(" ")
    lines = []
    current_line = ""
    for word in words:
        test_line = f"{current_line} {word}".strip()
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)
    return lines


def create_subtitle_image(text, out_path):
    img = Image.new("RGBA", (WIDTH, 340), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = get_bangla_font(46)

    lines = wrap_text_pixels(text, font, WIDTH - 140, draw)
    lines = lines[:3]

    line_heights = []
    max_line_w = 0
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        max_line_w = max(max_line_w, w)
        line_heights.append(h + 14)

    total_text_h = sum(line_heights)
    box_w = min(WIDTH - 80, max_line_w + 80)
    box_h = total_text_h + 44
    box_x1 = (WIDTH - box_w) // 2
    box_y1 = (340 - box_h) // 2
    box_x2 = box_x1 + box_w
    box_y2 = box_y1 + box_h

    draw.rounded_rectangle(
        [box_x1, box_y1, box_x2, box_y2],
        radius=26,
        fill=(10, 15, 28, 225),
        outline=(255, 200, 50, 255),
        width=3
    )

    curr_y = box_y1 + 20
    for idx, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        x = (WIDTH - w) // 2
        draw.text((x, curr_y), line, font=font, fill=(255, 255, 255, 255))
        curr_y += line_heights[idx]

    img.save(out_path)


def create_top_banner_image(out_path):
    img = Image.new("RGBA", (WIDTH, 130), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = get_bangla_font(36)

    draw.rounded_rectangle(
        [45, 28, 760, 102],
        radius=20,
        fill=(15, 23, 42, 230),
        outline=(56, 189, 248, 255),
        width=3
    )
    draw.text(
        (72, 44),
        "বিস্তারিত পড়ুন: homebdinfo.com",
        font=font,
        fill=(255, 255, 255, 255)
    )
    img.save(out_path)


def create_cta_overlay_image(out_path):
    img = Image.new("RGBA", (WIDTH, 240), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_title = get_bangla_font(40)
    font_sub = get_bangla_font(44)

    draw.rounded_rectangle(
        [60, 15, WIDTH - 60, 220],
        radius=28,
        fill=(185, 28, 28, 240),
        outline=(255, 255, 255, 255),
        width=4
    )
    t1 = "সম্পূর্ণ তথ্য ও আবেদন লিংক পেতে"
    t2 = "Google-এ সার্চ করুন: homebdinfo.com"

    b1 = draw.textbbox((0, 0), t1, font=font_title)
    b2 = draw.textbbox((0, 0), t2, font=font_sub)

    draw.text(((WIDTH - (b1[2] - b1[0])) // 2, 42), t1, font=font_title, fill=(255, 240, 200, 255))
    draw.text(((WIDTH - (b2[2] - b2[0])) // 2, 112), t2, font=font_sub, fill=(255, 255, 255, 255))
    img.save(out_path)


def build_video(screenshot_path, stock_paths, timings, voice_path, total_duration, output_video_path):
    layers = []

    # ১. ব্যাকগ্রাউন্ড: মোবাইল ওয়েবসাইট স্ক্রিনশট (বড় লেখা ও ধীরে ধীরে স্ক্রলিং)
    pil_bg = Image.open(screenshot_path).convert("RGB")
    scale_ratio = WIDTH / float(pil_bg.width)
    new_h = int(pil_bg.height * scale_ratio)
    if new_h < HEIGHT + 300:
        new_h = HEIGHT + 400
    pil_bg = pil_bg.resize((WIDTH, new_h), Image.Resampling.LANCZOS)
    resized_bg_path = os.path.join(TEMP_DIR, "bg_mobile_scaled.png")
    pil_bg.save(resized_bg_path)

    max_scroll = min(new_h - HEIGHT, 2200)

    def scroll_pos(t):
        progress = min(1.0, t / max(1.0, total_duration))
        y = -int(progress * max_scroll)
        return (0, y)

    bg_clip = (
        ImageClip(resized_bg_path)
        .set_duration(total_duration)
        .set_position(scroll_pos)
    )
    layers.append(bg_clip)

    # লেখা স্পষ্ট রাখার জন্য খুব হালকা ডার্ক টিন্ট (মাত্র ২৫%)
    tint_img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 65))
    tint_path = os.path.join(TEMP_DIR, "tint.png")
    tint_img.save(tint_path)
    layers.append(ImageClip(tint_path).set_duration(total_duration))

    # ২. ফ্রেম করা স্টক ফুটেজ (নিচের মাঝামাঝি ছোট কার্ডে, যাতে উপরের লেখা স্পষ্ট দেখা যায়)
    if stock_paths:
        stock_clips = []
        per_clip_dur = total_duration / len(stock_paths)
        card_w, card_h = 680, 380
        for sp in stock_paths:
            try:
                vc = VideoFileClip(sp).without_audio()
                if vc.duration < per_clip_dur:
                    vc = vc.fx(afx.loop, duration=per_clip_dur)
                else:
                    vc = vc.subclip(0, per_clip_dur)
                vc = vc.resize(height=card_h)
                if vc.w > card_w:
                    x_center = vc.w / 2
                    vc = vc.crop(x1=x_center - card_w / 2, x2=x_center + card_w / 2)
                else:
                    vc = vc.resize((card_w, card_h))
                stock_clips.append(vc)
            except Exception as e:
                print(f"Stock clip error: {e}")

        if stock_clips:
            combined_stock = concatenate_videoclips(stock_clips, method="compose").set_duration(total_duration)
            border_img = Image.new("RGBA", (card_w + 16, card_h + 16), (255, 255, 255, 235))
            border_path = os.path.join(TEMP_DIR, "card_border.png")
            border_img.save(border_path)

            card_x = (WIDTH - card_w) // 2
            card_y = 1040

            layers.append(
                ImageClip(border_path)
                .set_duration(total_duration)
                .set_position((card_x - 8, card_y - 8))
            )
            layers.append(
                combined_stock.set_position((card_x, card_y))
            )

    # ৩. উপরের ব্র্যান্ডিং ব্যানার
    banner_path = os.path.join(TEMP_DIR, "top_banner.png")
    create_top_banner_image(banner_path)
    layers.append(ImageClip(banner_path).set_duration(total_duration).set_position((0, 35)))

    # ৪. উপরের ডান কোণায় লোগো ওয়াটারমার্ক (যদি রিপোজিটরিতে logo.png থাকে)
    if os.path.exists("logo.png"):
        logo_clip = (
            ImageClip("logo.png")
            .resize(width=150)
            .set_duration(total_duration)
            .set_position((WIDTH - 195, 45))
        )
        layers.append(logo_clip)

    # ৫. ডায়নামিক বাংলা সাবটাইটেল
    for idx, item in enumerate(timings):
        sub_path = os.path.join(TEMP_DIR, f"sub_{idx}.png")
        create_subtitle_image(item["text"], sub_path)
        dur = max(0.3, item["end"] - item["start"])
        sub_clip = (
            ImageClip(sub_path)
            .set_start(item["start"])
            .set_duration(dur)
            .set_position((0, 1460))
        )
        layers.append(sub_clip)

    # ৬. শেষ ৫ সেকেন্ডে কল-টু-অ্যাকশন (CTA) ওভারলে
    if total_duration > 6:
        cta_path = os.path.join(TEMP_DIR, "cta_overlay.png")
        create_cta_overlay_image(cta_path)
        cta_clip = (
            ImageClip(cta_path)
            .set_start(total_duration - 5.0)
            .set_duration(5.0)
            .set_position((0, 760))
        )
        layers.append(cta_clip)

    final_video = CompositeVideoClip(layers, size=(WIDTH, HEIGHT)).set_duration(total_duration)

    # ৭. অডিও ও ১২% ভলিউমে ব্যাকগ্রাউন্ড মিউজিক
    voice_audio = AudioFileClip(voice_path)
    if os.path.exists("bgm.mp3"):
        bgm = AudioFileClip("bgm.mp3")
        if bgm.duration < total_duration:
            bgm = bgm.fx(afx.audio_loop, duration=total_duration)
        else:
            bgm = bgm.subclip(0, total_duration)
        bgm = bgm.volumex(0.12)
        final_audio = CompositeAudioClip([voice_audio, bgm])
    else:
        final_audio = voice_audio

    final_video = final_video.set_audio(final_audio)
    final_video.write_videofile(
        output_video_path,
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        threads=4,
        preset="medium"
    )


def upload_to_github_release(video_path, title_slug, row_num):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN পাওয়া যায়নি।")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    tag_name = f"shorts-row-{row_num}-{int(time.time())}"
    release_payload = {
        "tag_name": tag_name,
        "name": f"Shorts Video Row {row_num}",
        "body": f"Auto-generated Bangla Short: {title_slug}",
        "draft": False,
        "prerelease": False
    }

    rel_resp = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases",
        headers=headers,
        json=release_payload,
        timeout=30
    )
    rel_resp.raise_for_status()
    rel_data = rel_resp.json()

    release_id = str(rel_data["id"])
    upload_url_template = rel_data["upload_url"].split("{")[0]
    asset_name = os.path.basename(video_path)

    upload_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "video/mp4"
    }
    with open(video_path, "rb") as f:
        up_resp = requests.post(
            f"{upload_url_template}?name={asset_name}",
            headers=upload_headers,
            data=f,
            timeout=120
        )
    up_resp.raise_for_status()
    download_url = up_resp.json()["browser_download_url"]
    return release_id, download_url


def send_telegram_approval(video_path, title, post_url, row_num):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN অথবা TELEGRAM_CHAT_ID পাওয়া যায়নি।")

    caption = (
        f"🎬 <b>নতুন Bangla Short প্রস্তুত!</b>\n\n"
        f"📌 <b>শিরোনাম:</b> {title}\n"
        f"🔗 <b>পোস্ট লিংক:</b> {post_url}\n"
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
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendVideo",
            data={
                "chat_id": chat_id,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(reply_markup)
            },
            files={"video": vf},
            timeout=120
        )
    resp.raise_for_status()
    res_json = resp.json()
    msg_id = str(res_json["result"]["message_id"])
    file_id = str(res_json["result"]["video"]["file_id"])
    return file_id, msg_id


def main():
    sheet = get_google_sheet()
    headers = [h.strip() for h in sheet.row_values(1)]
    col_map = {name: idx + 1 for idx, name in enumerate(headers)}

    all_rows = sheet.get_all_values()
    target_row_idx = None
    target_row_data = None

    for i in range(1, len(all_rows)):
        row = all_rows[i]
        status_val = row[col_map["Status"] - 1].strip() if len(row) >= col_map["Status"] else ""
        if status_val == "SCRIPT_READY":
            target_row_idx = i + 1
            target_row_data = row
            break

    if not target_row_idx:
        print("SCRIPT_READY স্ট্যাটাসের কোনো রো পাওয়া যায়নি।")
        return

    def get_col(col_name):
        idx = col_map.get(col_name, 0) - 1
        return target_row_data[idx].strip() if (0 <= idx < len(target_row_data)) else ""

    title = get_col("Shorts Title (YT)") or get_col("Post Title") or "Bangla Info Short"
    script = get_col("Bangla Voiceover Script")
    keywords = get_col("Image Search Keywords (English)")
    post_url = get_col("Post URL")

    if not script:
        sheet.update_cell(target_row_idx, col_map["Last Error"], "Bangla Voiceover Script খালি।")
        return

    try:
        sheet.update_cell(target_row_idx, col_map["Status"], "PROCESSING")
        sheet.update_cell(target_row_idx, col_map["Video Status"], "GENERATING")

        sentences = split_sentences_natural(script)
        voice_path = os.path.join(TEMP_DIR, "final_voice.mp3")
        timings, total_duration = asyncio.run(generate_dynamic_voiceover(sentences, voice_path))

        screenshot_path = os.path.join(TEMP_DIR, "site_mobile.png")
        capture_mobile_website_screenshot(post_url, screenshot_path)

        stock_paths = download_pixabay_videos(keywords, total_duration)

        output_video = os.path.join(TEMP_DIR, f"short_row_{target_row_idx}.mp4")
        build_video(screenshot_path, stock_paths, timings, voice_path, total_duration, output_video)

        release_id, download_url = upload_to_github_release(output_video, title, target_row_idx)
        tg_file_id, tg_msg_id = send_telegram_approval(output_video, title, post_url, target_row_idx)

        sheet.update_cell(target_row_idx, col_map["Drive File ID"], release_id)
        sheet.update_cell(target_row_idx, col_map["Drive File URL"], download_url)
        sheet.update_cell(target_row_idx, col_map["Telegram Video File ID"], tg_file_id)
        sheet.update_cell(target_row_idx, col_map["Telegram Message ID"], tg_msg_id)
        sheet.update_cell(target_row_idx, col_map["Video Status"], "COMPLETED")
        sheet.update_cell(target_row_idx, col_map["Status"], "PENDING_APPROVAL")
        sheet.update_cell(target_row_idx, col_map["Last Error"], "")

        print(f"সফলভাবে Row {target_row_idx}-এর ভিডিও তৈরি ও টেলিগ্রামে পাঠানো হয়েছে।")

    except Exception as e:
        err_msg = str(e)[:900]
        print(f"Error processing row {target_row_idx}: {err_msg}")
        sheet.update_cell(target_row_idx, col_map["Video Status"], "FAILED")
        sheet.update_cell(target_row_idx, col_map["Last Error"], err_msg)
        raise


if __name__ == "__main__":
    main()
