import os
import re
import json
import time
import random
import asyncio
import textwrap
import subprocess
import requests
import gspread
from google.oauth2.service_account import Credentials
from PIL import Image, ImageDraw, ImageFont
import edge_tts
from playwright.sync_api import sync_playwright

# ==============================================================================
# ১. ভয়েস কনফিগারেশন (সহজেই ভয়েস, গতি ও সুর পরিবর্তন করতে পারেন)
# ==============================================================================
# জনপ্রিয় বাংলা ভয়েস অপশনসমূহ:
# - "bn-BD-PradeepNeural"   (বাংলাদেশি স্মার্ট ও গম্ভীর পুরুষ কণ্ঠ - রেকমেন্ডেড)
# - "bn-BD-NabanitaNeural"  (বাংলাদেশি নারী কণ্ঠ)
# - "bn-IN-BashkarNeural"   (ভারতীয় বাংলা পুরুষ কণ্ঠ)
# - "bn-IN-TanishaaNeural"  (ভারতীয় বাংলা নারী কণ্ঠ)

VOICE_NAME = "bn-BD-PradeepNeural"

# ==============================================================================
# ২. ওয়েবসাইট ও ডিজাইন কনফিগারেশন
# ==============================================================================
SITE_DOMAIN = "homebdinfo.com"
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 30
BGM_VOLUME = 0.12  # ১২% ব্যাকগ্রাউন্ড মিউজিক ভলিউম
FONT_PATH = "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf"
FALLBACK_FONT_PATH = "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"

# গুগল শিট সেটিংস
SHEET_NAME = "Shorts_Queue"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]


def get_google_sheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    sheet_id = os.environ.get("SHEET_ID")
    if not creds_json or not sheet_id:
        raise ValueError("GOOGLE_CREDENTIALS বা SHEET_ID সিক্রেট পাওয়া যায়নি।")

    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id).worksheet(SHEET_NAME)


def get_font(size):
    for path in [FONT_PATH, FALLBACK_FONT_PATH]:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


# ==============================================================================
# ৩. মানুষের মতো ন্যাচারাল ও এক্সপ্রেসিভ ভয়েস জেনারেশন (FFmpeg ভিত্তিক, pydub ছাড়া)
# ==============================================================================
def split_sentences_for_tts(script_text):
    clean = re.sub(r"\s+", " ", script_text).strip()
    parts = [p.strip() for p in re.split(r"(?<=[।!?\.])\s+", clean) if p.strip()]
    return parts


async def synthesize_segment(text, out_mp3, rate, pitch):
    communicate = edge_tts.Communicate(
        text=text,
        voice=VOICE_NAME,
        rate=rate,
        pitch=pitch
    )
    await communicate.save(out_mp3)


def get_audio_duration(file_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def create_silence_mp3(duration_sec, out_mp3):
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "anullsrc=r=24000:cl=mono",
        "-t", f"{duration_sec:.3f}",
        "-q:a", "9", "-acodec", "libmp3lame",
        out_mp3
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def generate_natural_voiceover(script_text, final_audio_path, work_dir):
    sentences = split_sentences_for_tts(script_text)
    if not sentences:
        raise ValueError("স্ক্রিপ্ট খালি পাওয়া গেছে।")

    concat_list_path = os.path.join(work_dir, "audio_concat.txt")
    segment_timings = []
    current_time = 0.0
    total = len(sentences)

    with open(concat_list_path, "w", encoding="utf-8") as list_file:
        for idx, sentence in enumerate(sentences):
            seg_mp3 = os.path.join(work_dir, f"seg_{idx}.mp3")

            # বাক্যের ধরন অনুযায়ী কণ্ঠের ওঠানামা (Dynamic Intonation)
            if idx == 0:
                # প্রথম বাক্য (Hook): একটু প্রাণবন্ত ও আকর্ষণীয়
                rate, pitch, pause = "+13%", "-1Hz", 0.35
            elif sentence.endswith("?"):
                # প্রশ্নবোধক বাক্য: সুর একটু উপরে উঠবে
                rate, pitch, pause = "+10%", "+4Hz", 0.40
            elif idx == total - 1:
                # শেষ বাক্য (CTA): গম্ভীর ও স্পষ্ট আহ্বান
                rate, pitch, pause = "+5%", "-5Hz", 0.30
            else:
                # সাধারণ তথ্যমূলক বাক্য
                rate, pitch, pause = "+8%", "-3Hz", 0.28

            asyncio.run(synthesize_segment(sentence, seg_mp3, rate, pitch))
            dur = get_audio_duration(seg_mp3)

            start_t = current_time
            end_t = current_time + dur
            segment_timings.append({
                "text": sentence,
                "start": start_t,
                "end": end_t
            })
            current_time = end_t

            list_file.write(f"file '{os.path.abspath(seg_mp3)}'\n")

            if idx < total - 1:
                sil_mp3 = os.path.join(work_dir, f"sil_{idx}.mp3")
                create_silence_mp3(pause, sil_mp3)
                list_file.write(f"file '{os.path.abspath(sil_mp3)}'\n")
                current_time += pause

    # সব অডিও ও বিরতি FFmpeg দিয়ে একসাথে জোড়া লাগানো
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", concat_list_path,
        "-ar", "44100", "-ac", "2", "-b:a", "192k",
        final_audio_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return current_time, segment_timings


# ==============================================================================
# ৪. মোবাইল ভিউতে ওয়েবসাইটের বড় ও স্পষ্ট স্ক্রিনশট গ্রহণ (Playwright)
# ==============================================================================
def capture_mobile_site_screenshot(post_url, output_png):
    target_url = post_url if post_url and post_url.startswith("http") else f"https://www.{SITE_DOMAIN}"

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
        try:
            page.goto(target_url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(2000)
            # লেখা আরও বড় ও পড়ার উপযোগী করতে সামান্য জুম ও পপআপ হাইড
            page.add_style_tag(content="""
                body { zoom: 1.12 !important; }
                .cookie-choices-info, #cookieChoiceInfo, .adsbygoogle { display: none !important; }
            """)
            page.wait_for_timeout(1000)
            page.screenshot(path=output_png, full_page=True)
        except Exception as e:
            print(f"স্ক্রিনশট নিতে সমস্যা হয়েছে ({e}), ফলব্যাক কার্ড তৈরি হচ্ছে...")
            img = Image.new("RGB", (1080, 2600), (245, 248, 252))
            draw = ImageDraw.Draw(img)
            font_lg = get_font(60)
            draw.text((120, 400), SITE_DOMAIN, fill=(26, 115, 232), font=font_lg)
            img.save(output_png)
        finally:
            browser.close()

    # স্ক্রিনশটকে ঠিক 1080px প্রস্থে রিসাইজ করা (উচ্চতা অনুপাত অনুযায়ী বাড়বে যাতে স্ক্রল করা যায়)
    img = Image.open(output_png).convert("RGB")
    w, h = img.size
    new_h = int((VIDEO_WIDTH / w) * h)
    if new_h < VIDEO_HEIGHT + 400:
        new_h = VIDEO_HEIGHT + 400
    img = img.resize((VIDEO_WIDTH, new_h), Image.Resampling.LANCZOS)
    img.save(output_png)
    return new_h


# ==============================================================================
# ৫. Pixabay থেকে স্টক ভিডিও ডাউনলোড
# ==============================================================================
def download_pixabay_clips(keywords_str, work_dir):
    api_key = os.environ.get("PIXABAY_API_KEY")
    if not api_key:
        return []

    keywords = [k.strip() for k in str(keywords_str).split(",") if k.strip()]
    if not keywords:
        keywords = ["technology", "bangladesh", "education"]

    downloaded = []
    for idx, kw in enumerate(keywords[:3]):
        try:
            url = (
                f"https://pixabay.com/api/videos/?key={api_key}"
                f"&q={requests.utils.quote(kw)}&per_page=5"
            )
            resp = requests.get(url, timeout=20).json()
            hits = resp.get("hits", [])
            if not hits:
                continue

            chosen = random.choice(hits[:3])
            videos = chosen.get("videos", {})
            vid_info = videos.get("medium") or videos.get("small") or videos.get("tiny")
            if vid_info and vid_info.get("url"):
                clip_path = os.path.join(work_dir, f"clip_{idx}.mp4")
                v_data = requests.get(vid_info["url"], timeout=40).content
                with open(clip_path, "wb") as f:
                    f.write(v_data)
                downloaded.append(clip_path)
        except Exception as e:
            print(f"Pixabay ডাউনলোড সতর্কতা ({kw}): {e}")

    return downloaded


# ==============================================================================
# ৬. বাংলা সাবটাইটেল ও ওভারলে ইমেজ জেনারেশন (PIL দিয়ে নিখুঁত বাংলা রেন্ডারিং)
# ==============================================================================
def create_top_banner_png(out_png):
    img = Image.new("RGBA", (VIDEO_WIDTH, 160), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # উপরে প্রমোশনাল পিল ব্যাজ
    draw.rounded_rectangle([140, 35, 940, 115], radius=40, fill=(20, 30, 48, 230), outline=(56, 189, 248, 255), width=3)
    font = get_font(36)
    text = f"বিস্তারিত তথ্য ও আবেদন: {SITE_DOMAIN}"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((VIDEO_WIDTH - tw) // 2, 52), text, fill=(255, 255, 255, 255), font=font)
    img.save(out_png)


def create_cta_banner_png(out_png):
    img = Image.new("RGBA", (VIDEO_WIDTH, 260), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([80, 20, 1000, 230], radius=30, fill=(15, 23, 42, 242), outline=(250, 204, 21, 255), width=4)
    font_title = get_font(40)
    font_url = get_font(48)

    line1 = "সম্পূর্ণ তথ্য ও অনলাইন লিংক পেতে"
    line2 = f"Google-এ সার্চ করুন: {SITE_DOMAIN}"

    b1 = draw.textbbox((0, 0), line1, font=font_title)
    draw.text(((VIDEO_WIDTH - (b1[2] - b1[0])) // 2, 48), line1, fill=(255, 255, 255, 255), font=font_title)

    b2 = draw.textbbox((0, 0), line2, font=font_url)
    draw.text(((VIDEO_WIDTH - (b2[2] - b2[0])) // 2, 125), line2, fill=(250, 204, 21, 255), font=font_url)
    img.save(out_png)


def render_subtitle_frame(text, out_png):
    img = Image.new("RGBA", (VIDEO_WIDTH, 340), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = get_font(44)

    wrapped = textwrap.fill(text, width=32)
    lines = wrapped.split("\n")[:3]
    final_text = "\n".join(lines)

    bbox = draw.multiline_textbbox((0, 0), final_text, font=font, spacing=14, align="center")
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    pad_x, pad_y = 44, 28
    box_w = min(VIDEO_WIDTH - 80, tw + pad_x * 2)
    box_h = th + pad_y * 2
    x0 = (VIDEO_WIDTH - box_w) // 2
    y0 = (340 - box_h) // 2

    draw.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=24, fill=(0, 0, 0, 215))
    draw.multiline_text(
        (VIDEO_WIDTH // 2, y0 + pad_y),
        final_text,
        fill=(255, 255, 255, 255),
        font=font,
        spacing=14,
        align="center",
        anchor="ma"
    )
    img.save(out_png)


# ==============================================================================
# ৭. চূড়ান্ত ভিডিও কম্পোজিটিং (FFmpeg)
# ==============================================================================
def build_final_video(
    screenshot_png,
    screenshot_height,
    clips,
    voice_mp3,
    total_duration,
    timings,
    output_mp4,
    work_dir
):
    top_banner = os.path.join(work_dir, "top_banner.png")
    cta_banner = os.path.join(work_dir, "cta_banner.png")
    create_top_banner_png(top_banner)
    create_cta_banner_png(cta_banner)

    # ব্যাকগ্রাউন্ড স্ক্রলিং ভিডিও তৈরি (হালকা ডিম ওভারলেসহ যাতে সাইটের লেখা স্পষ্ট দেখা যায়)
    scroll_dist = max(0, screenshot_height - VIDEO_HEIGHT)
    bg_video = os.path.join(work_dir, "bg_scroll.mp4")

    cmd_bg = [
        "ffmpeg", "-y", "-loop", "1", "-i", screenshot_png,
        "-vf", (
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:0:'min({scroll_dist}, t*({scroll_dist}/{max(1.0, total_duration)}))',"
            f"drawbox=x=0:y=0:w={VIDEO_WIDTH}:h={VIDEO_HEIGHT}:color=black@0.18:t=fill"
        ),
        "-t", f"{total_duration:.2f}",
        "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        bg_video
    ]
    subprocess.run(cmd_bg, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # স্টক ক্লিপ থাকলে মাঝখানের নিচের অংশে কার্ড হিসেবে বসানো (680x380)
    base_video = bg_video
    if clips:
        concat_vid_txt = os.path.join(work_dir, "vids.txt")
        with open(concat_vid_txt, "w") as vf:
            # লুপ করে পুরো সময় কভার করা
            for _ in range(10):
                for c in clips:
                    vf.write(f"file '{os.path.abspath(c)}'\n")

        pip_raw = os.path.join(work_dir, "pip_raw.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_vid_txt,
            "-t", f"{total_duration:.2f}",
            "-vf", "scale=680:380:force_original_aspect_ratio=increase,crop=680:380,pad=692:392:6:6:white",
            "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            pip_raw
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        pip_merged = os.path.join(work_dir, "pip_merged.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-i", bg_video, "-i", pip_raw,
            "-filter_complex", "[0:v][1:v]overlay=(W-w)/2:880",
            "-t", f"{total_duration:.2f}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            pip_merged
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        base_video = pip_merged

    # সাবটাইটেল ইমেজগুলো ওভারলে করা
    sub_inputs = []
    filter_chains = []
    last_label = "[0:v]"
    input_idx = 1

    # টপ ব্যানার
    sub_inputs.extend(["-i", top_banner])
    filter_chains.append(f"{last_label}[{input_idx}:v]overlay=0:35[v{input_idx}]")
    last_label = f"[v{input_idx}]"
    input_idx += 1

    # লোগো (যদি রিপোজিটরিতে থাকে)
    if os.path.exists("logo.png"):
        sub_inputs.extend(["-i", "logo.png"])
        filter_chains.append(f"[{input_idx}:v]scale=110:-1[logo];{last_label}[logo]overlay=W-w-35:175[v{input_idx}]")
        last_label = f"[v{input_idx}]"
        input_idx += 1

    # প্রতিটি বাক্যের সাবটাইটেল টাইমিং অনুযায়ী বসানো
    for i, item in enumerate(timings):
        sub_png = os.path.join(work_dir, f"sub_{i}.png")
        render_subtitle_frame(item["text"], sub_png)
        sub_inputs.extend(["-i", sub_png])
        st = item["start"]
        et = item["end"]
        filter_chains.append(
            f"{last_label}[{input_idx}:v]overlay=0:1320:enable='between(t,{st:.2f},{et:.2f})'[v{input_idx}]"
        )
        last_label = f"[v{input_idx}]"
        input_idx += 1

    # শেষ ৫ সেকেন্ডে CTA ব্যানার
    cta_start = max(0.0, total_duration - 5.0)
    sub_inputs.extend(["-i", cta_banner])
    filter_chains.append(
        f"{last_label}[{input_idx}:v]overlay=0:1630:enable='gte(t,{cta_start:.2f})'[vout]"
    )

    video_no_audio = os.path.join(work_dir, "video_no_audio.mp4")
    cmd_overlay = [
        "ffmpeg", "-y", "-i", base_video,
        *sub_inputs,
        "-filter_complex", ";".join(filter_chains),
        "-map", "[vout]",
        "-t", f"{total_duration:.2f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        video_no_audio
    ]
    subprocess.run(cmd_overlay, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # অডিও ও ব্যাকগ্রাউন্ড মিউজিক (bgm.mp3) মিক্সিং
    if os.path.exists("bgm.mp3"):
        cmd_final = [
            "ffmpeg", "-y",
            "-i", video_no_audio,
            "-i", voice_mp3,
            "-stream_loop", "-1", "-i", "bgm.mp3",
            "-filter_complex", (
                f"[1:a]volume=1.0[voice];"
                f"[2:a]volume={BGM_VOLUME}[bgm];"
                f"[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            ),
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", output_mp4
        ]
    else:
        cmd_final = [
            "ffmpeg", "-y",
            "-i", video_no_audio,
            "-i", voice_mp3,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", output_mp4
        ]

    subprocess.run(cmd_final, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


# ==============================================================================
# ৮. GitHub Release আপলোড ও Telegram নোটিফিকেশন
# ==============================================================================
def upload_to_github_release(video_path, tag_name, title):
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot")
    if not token:
        raise ValueError("GITHUB_TOKEN পাওয়া যায়নি।")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }

    rel_resp = requests.post(
        f"https://api.github.com/repos/{repo}/releases",
        headers=headers,
        json={
            "tag_name": tag_name,
            "name": title[:100],
            "body": "Automated Bangla YouTube Short",
            "draft": False,
            "prerelease": False
        },
        timeout=30
    )
    rel_data = rel_resp.json()
    release_id = str(rel_data.get("id", ""))
    upload_url = rel_data.get("upload_url", "").split("{")[0]
    if not upload_url:
        raise RuntimeError(f"GitHub Release তৈরি ব্যর্থ: {rel_data}")

    file_name = os.path.basename(video_path)
    with open(video_path, "rb") as vf:
        up_resp = requests.post(
            f"{upload_url}?name={file_name}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "video/mp4"
            },
            data=vf.read(),
            timeout=120
        )
    up_data = up_resp.json()
    download_url = up_data.get("browser_download_url", "")
    return release_id, download_url


def send_telegram_approval(video_path, row_num, shorts_title, post_url):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return "", ""

    caption = (
        f"🎬 <b>নতুন শর্টস ভিডিও প্রস্তুত!</b>\n\n"
        f"📌 <b>শিরোনাম:</b> {shorts_title}\n"
        f"🔗 <b>পোস্ট:</b> {post_url}\n"
        f"📊 <b>Sheet Row:</b> {row_num}"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ APPROVE", "callback_data": f"APPROVE_{row_num}"},
            {"text": "❌ REJECT", "callback_data": f"REJECT_{row_num}"}
        ]]
    }

    with open(video_path, "rb") as vf:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendVideo",
            data={
                "chat_id": chat_id,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(keyboard)
            },
            files={"video": vf},
            timeout=120
        ).json()

    if resp.get("ok"):
        msg = resp["result"]
        return msg.get("video", {}).get("file_id", ""), str(msg.get("message_id", ""))
    return "", ""


# ==============================================================================
# ৯. মেইন প্রসেসিং লুপ
# ==============================================================================
def main():
    sheet = get_google_sheet()
    records = sheet.get_all_records()
    headers = sheet.row_values(1)

    def col_idx(name):
        return headers.index(name) + 1

    target_row = None
    target_data = None

    for idx, row in enumerate(records, start=2):
        if str(row.get("Status", "")).strip() == "SCRIPT_READY":
            target_row = idx
            target_data = row
            break

    if not target_row:
        print("SCRIPT_READY স্ট্যাটাসের কোনো রো পাওয়া যায়নি।")
        return

    print(f"Row {target_row} প্রসেস শুরু হচ্ছে...")
    sheet.update_cell(target_row, col_idx("Status"), "PROCESSING")
    sheet.update_cell(target_row, col_idx("Video Status"), "GENERATING")

    work_dir = f"work_row_{target_row}"
    os.makedirs(work_dir, exist_ok=True)

    try:
        shorts_title = str(target_data.get("Shorts Title (YT)", "Bangla Shorts")).strip()
        script_text = str(target_data.get("Bangla Voiceover Script", "")).strip()
        keywords = str(target_data.get("Image Search Keywords (English)", "")).strip()
        post_url = str(target_data.get("Post URL", "")).strip()

        voice_mp3 = os.path.join(work_dir, "voice.mp3")
        total_dur, timings = generate_natural_voiceover(script_text, voice_mp3, work_dir)

        screenshot_png = os.path.join(work_dir, "site_mobile.png")
        sc_height = capture_mobile_site_screenshot(post_url, screenshot_png)

        clips = download_pixabay_clips(keywords, work_dir)

        final_mp4 = os.path.join(work_dir, f"short_row_{target_row}.mp4")
        build_final_video(
            screenshot_png, sc_height, clips,
            voice_mp3, total_dur, timings,
            final_mp4, work_dir
        )

        tag_name = f"short-r{target_row}-{int(time.time())}"
        rel_id, dl_url = upload_to_github_release(final_mp4, tag_name, shorts_title)

        tg_vid_id, tg_msg_id = send_telegram_approval(final_mp4, target_row, shorts_title, post_url)

        sheet.update_cell(target_row, col_idx("Drive File ID"), rel_id)
        sheet.update_cell(target_row, col_idx("Drive File URL"), dl_url)
        sheet.update_cell(target_row, col_idx("Telegram Video File ID"), tg_vid_id)
        sheet.update_cell(target_row, col_idx("Telegram Message ID"), tg_msg_id)
        sheet.update_cell(target_row, col_idx("Video Status"), "COMPLETED")
        sheet.update_cell(target_row, col_idx("Status"), "PENDING_APPROVAL")
        sheet.update_cell(target_row, col_idx("Last Error"), "")

        print(f"Row {target_row} সফলভাবে সম্পন্ন হয়েছে!")

    except Exception as e:
        err_msg = str(e)[:900]
        print(f"এরর হয়েছে: {err_msg}")
        sheet.update_cell(target_row, col_idx("Video Status"), "FAILED")
        sheet.update_cell(target_row, col_idx("Status"), "ERROR")
        sheet.update_cell(target_row, col_idx("Last Error"), err_msg)
        raise


if __name__ == "__main__":
    main()
