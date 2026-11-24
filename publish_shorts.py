import os
import re
import json
import time
import requests
import gspread
from datetime import datetime, timezone
from google.oauth2.service_account import Credentials as ServiceCredentials
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SHEET_NAME = "Shorts_Queue"
SHEET_ID = os.environ.get("SHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDENTIALS")
YOUTUBE_OAUTH_TOKEN = os.environ.get("YOUTUBE_OAUTH_TOKEN")
FB_PAGE_ID = os.environ.get("FB_PAGE_ID")
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN")
FB_APP_ID = os.environ.get("FB_APP_ID")
FB_APP_SECRET = os.environ.get("FB_APP_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "mainfouser/bangla-shorts-bot")


def get_sheet():
    if not GOOGLE_CREDS_JSON or not SHEET_ID:
        raise ValueError("GOOGLE_CREDENTIALS বা SHEET_ID পাওয়া যায়নি।")
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceCredentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)


def get_header_map(sheet):
    headers = sheet.row_values(1)
    return {header.strip(): idx + 1 for idx, header in enumerate(headers)}


def get_col_val(row, header_map, col_name, default=""):
    idx = header_map.get(col_name, 0) - 1
    return row[idx].strip() if 0 <= idx < len(row) else default


def update_cells(sheet, row_num, header_map, updates):
    cells_to_update = []
    for col_name, value in updates.items():
        if col_name in header_map:
            col_idx = header_map[col_name]
            cells_to_update.append(gspread.Cell(row=row_num, col=col_idx, value=str(value)))
    if cells_to_update:
        sheet.update_cells(cells_to_update)


def generate_seo_metadata(raw_title, script_text, post_url):
    default_meta = {
        "yt_title": f"{raw_title[:85]} #Shorts",
        "yt_description": f"{raw_title}\n\nবিস্তারিত পড়ুন ও সরাসরি লিংক: {post_url}\n\n#Shorts #BanglaShorts #HomeBDInfo",
        "yt_tags": ["Bangla Shorts", "Bangladesh", "Home BD Info", "Bangla Tech", "Tutorial"],
        "fb_caption": f"{raw_title}\n\nবিস্তারিত গাইড ও লিংক প্রথম কমেন্টে দেওয়া আছে 👇\n\n#BanglaReels #Bangladesh #HomeBDInfo #InfoVideo"
    }

    if not GEMINI_API_KEY:
        return default_meta

    prompt = (
        "তুমি বাংলাদেশের একজন দক্ষ YouTube Shorts ও Facebook Reels SEO বিশেষজ্ঞ।\n"
        f"ভিডিওর মূল শিরোনাম: {raw_title}\n"
        f"ভয়েসওভার স্ক্রিপ্ট: {script_text}\n\n"
        "অ্যালগরিদম-বান্ধব ও সর্বোচ্চ রিচ পাওয়ার উপযোগী SEO মেটাডেটা তৈরি করো যাতে কোনো স্প্যামিং না হয়:\n"
        "১. yt_title: কৌতূহল-জাগানিয়া ক্লিকযোগ্য বাংলা টাইটেল (সর্বোচ্চ ৮০ অক্ষর) এবং শেষে #Shorts যুক্ত করবে।\n"
        "২. yt_summary: ভিডিওর বিষয়বস্তু নিয়ে ২-৩ লাইনের আকর্ষণীয় বাংলা সারসংক্ষেপ (কোনো লিংক বা হ্যাশট্যাগ ছাড়া)।\n"
        "৩. yt_hashtags: ঠিক ৪টি প্রাসঙ্গিক হ্যাশট্যাগ (যেমন: #Shorts #BanglaShorts ...)।\n"
        "৪. yt_tags: সার্চ র‍্যাংকিংয়ের জন্য ১২টি প্রাসঙ্গিক বাংলা ও ইংরেজি মিক্সড ট্যাগ (লিস্ট আকারে)।\n"
        "৫. fb_caption: ফেসবুক রিলসের জন্য আকর্ষণীয় হুকসহ সংক্ষিপ্ত বাংলা ক্যাপশন এবং শেষে ৩-৪টি হ্যাশট্যাগ। (সতর্কতা: fb_caption-এ কোনো ওয়েবসাইট লিংক দেবে না, শুধু লিখবে বিস্তারিত লিংক প্রথম কমেন্টে দেওয়া আছে)।\n\n"
        "শুধুমাত্র Valid JSON রিটার্ন করবে, কোনো Markdown কোড ব্লক দেবে না:\n"
        '{"yt_title": "...", "yt_summary": "...", "yt_hashtags": "...", "yt_tags": ["..."], "fb_caption": "..."}'
    )

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4}
        }
        res = requests.post(url, json=payload, timeout=30)
        res.raise_for_status()
        res_json = res.json()
        text = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)

        yt_title = data.get("yt_title", default_meta["yt_title"])[:98]
        if "#Shorts" not in yt_title and "#shorts" not in yt_title:
            yt_title = f"{yt_title[:88]} #Shorts"

        yt_summary = data.get("yt_summary", raw_title)
        yt_hashtags = data.get("yt_hashtags", "#Shorts #BanglaShorts #HomeBDInfo")
        yt_description = (
            f"{yt_summary}\n\n"
            f"🔗 বিস্তারিত গাইড ও লিংক:\n{post_url}\n\n"
            f"{yt_hashtags}"
        )

        return {
            "yt_title": yt_title,
            "yt_description": yt_description,
            "yt_tags": data.get("yt_tags", default_meta["yt_tags"])[:15],
            "fb_caption": data.get("fb_caption", default_meta["fb_caption"])
        }
    except Exception as e:
        print(f"SEO Generation fallback ব্যবহৃত হচ্ছে: {e}")
        return default_meta


def download_video(video_url, output_path="temp_publish.mp4"):
    with requests.get(video_url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    return output_path


def download_thumbnail_from_release(video_url, row_num, output_path="temp_thumb.jpg"):
    """GitHub Release থেকে একই রো-এর থাম্বনেইল ইমেজ (.jpg) ডাউনলোড করে"""
    if not video_url:
        return None

    thumb_url = re.sub(r"bangla_short_row_\d+\.mp4$", f"thumb_row_{row_num}.jpg", video_url)
    if thumb_url == video_url:
        base_url = video_url.rsplit("/", 1)[0]
        thumb_url = f"{base_url}/thumb_row_{row_num}.jpg"

    try:
        res = requests.get(thumb_url, timeout=45)
        if res.status_code == 200 and len(res.content) > 1024:
            with open(output_path, "wb") as f:
                f.write(res.content)
            print(f"থাম্বনেইল ডাউনলোড সফল: {thumb_url}")
            return output_path
        else:
            print(f"রিলিজে থাম্বনেইল পাওয়া যায়নি (HTTP {res.status_code})")
    except Exception as e:
        print(f"থাম্বনেইল ডাউনলোড সতর্কতা: {e}")

    return None


def set_facebook_reel_thumbnail(video_id, thumb_path):
    """Facebook Reels-এর কভার ফটো হিসেবে থাম্বনেইল আপলোড করে"""
    if not thumb_path or not os.path.exists(thumb_path):
        return False

    thumb_endpoint = f"https://graph.facebook.com/v21.0/{video_id}/thumbnails"
    try:
        with open(thumb_path, "rb") as tf:
            res = requests.post(
                thumb_endpoint,
                data={
                    "access_token": FB_PAGE_ACCESS_TOKEN,
                    "is_preferred": "true"
                },
                files={
                    "source": ("thumbnail.jpg", tf, "image/jpeg")
                },
                timeout=60
            )
        if res.status_code == 200:
            print("Facebook Reels থাম্বনেইল সফলভাবে সেট হয়েছে।")
            return True
        else:
            print(f"Facebook থাম্বনেইল আপলোড সতর্কতা: {res.text}")
    except Exception as e:
        print(f"Facebook থাম্বনেইল সেট করতে সমস্যা: {e}")

    return False


def remove_auto_captions(youtube, video_id):
    """ইউটিউব কোনো অটো-জেনারেটেড ক্যাপশন ট্র্যাক তৈরি করলে তা মুছে ফেলে"""
    try:
        captions_resp = youtube.captions().list(part="snippet", videoId=video_id).execute()
        for item in captions_resp.get("items", []):
            snippet = item.get("snippet", {})
            if snippet.get("trackKind") == "asr":
                youtube.captions().delete(id=item["id"]).execute()
                print(f"অটো-জেনারেটেড ক্যাপশন মুছে ফেলা হয়েছে: {item['id']}")
    except Exception as e:
        print(f"অটো-ক্যাপশন চেক সতর্কতা: {e}")


def sanitize_yt_tags(tags):
    """ইউটিউবের নিয়ম অনুযায়ী ট্যাগ পরিষ্কার করে এবং ৫০০ ক্যারেক্টারের নিচে রাখে"""
    clean_tags = []
    total_chars = 0
    for tag in tags:
        t = re.sub(r'[<>"\']', '', str(tag)).strip()
        if not t:
            continue
        if total_chars + len(t) + 2 > 400:
            break
        clean_tags.append(t)
        total_chars += len(t) + 2
    return clean_tags


def upload_to_youtube(video_path, seo_meta):
    if not YOUTUBE_OAUTH_TOKEN:
        raise ValueError("YOUTUBE_OAUTH_TOKEN পাওয়া যায়নি।")

    token_info = json.loads(YOUTUBE_OAUTH_TOKEN)
    creds = UserCredentials.from_authorized_user_info(token_info)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    youtube = build("youtube", "v3", credentials=creds)

    clean_title = re.sub(r'[<>]', '', seo_meta["yt_title"]).strip()[:95]
    if not clean_title:
        clean_title = "Bangla Info Video #Shorts"

    clean_desc = re.sub(r'[<>]', '', seo_meta["yt_description"]).strip()[:4900]
    clean_tags = sanitize_yt_tags(seo_meta.get("yt_tags", []))

    body = {
        "snippet": {
            "title": clean_title,
            "description": clean_desc,
            "tags": clean_tags,
            "categoryId": "27"
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()

    yt_video_id = response["id"]
    yt_url = f"https://youtube.com/shorts/{yt_video_id}"
    print(f"YouTube Shorts আপলোড সফল: {yt_url}")

    time.sleep(5)
    remove_auto_captions(youtube, yt_video_id)

    return yt_video_id, yt_url


def upload_to_facebook_reels(video_path, seo_meta, post_url, thumb_path=None):
    if not FB_PAGE_ID or not FB_PAGE_ACCESS_TOKEN:
        raise ValueError("FB_PAGE_ID বা FB_PAGE_ACCESS_TOKEN পাওয়া যায়নি।")

    graph_base = f"https://graph.facebook.com/v21.0/{FB_PAGE_ID}/video_reels"

    init_res = requests.post(
        graph_base,
        data={
            "upload_phase": "start",
            "access_token": FB_PAGE_ACCESS_TOKEN
        },
        timeout=30
    )
    init_res.raise_for_status()
    init_data = init_res.json()
    video_id = init_data["video_id"]
    upload_url = init_data["upload_url"]

    file_size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        up_res = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {FB_PAGE_ACCESS_TOKEN}",
                "offset": "0",
                "file_size": str(file_size)
            },
            data=f,
            timeout=180
        )
        up_res.raise_for_status()

    finish_res = requests.post(
        graph_base,
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": seo_meta["fb_caption"],
            "access_token": FB_PAGE_ACCESS_TOKEN
        },
        timeout=60
    )
    finish_res.raise_for_status()
    fb_url = f"https://www.facebook.com/reel/{video_id}"
    print(f"Facebook Reels আপলোড সফল: {fb_url}")

    if post_url or thumb_path:
        print("ফেসবুক রিলস প্রসেসিং শেষ হওয়ার জন্য অপেক্ষা করা হচ্ছে...")
        is_ready = False
        for attempt in range(18):
            time.sleep(5)
            try:
                status_res = requests.get(
                    f"https://graph.facebook.com/v21.0/{video_id}",
                    params={
                        "fields": "status",
                        "access_token": FB_PAGE_ACCESS_TOKEN
                    },
                    timeout=20
                )
                if status_res.status_code == 200:
                    v_status = status_res.json().get("status", {}).get("video_status")
                    print(f"চেক {attempt + 1}: ভিডিও স্ট্যাটাস = {v_status}")
                    if v_status == "ready":
                        is_ready = True
                        break
            except Exception as se:
                print(f"স্ট্যাটাস চেক সতর্কতা: {se}")

        if is_ready:
            if thumb_path:
                set_facebook_reel_thumbnail(video_id, thumb_path)

            if post_url:
                try:
                    comment_url = f"https://graph.facebook.com/v21.0/{video_id}/comments"
                    comment_msg = f"বিস্তারিত গাইড ও সরাসরি আবেদন লিংক এখানে দেখুন 👉 {post_url}"
                    c_res = requests.post(
                        comment_url,
                        data={
                            "message": comment_msg,
                            "access_token": FB_PAGE_ACCESS_TOKEN
                        },
                        timeout=30
                    )
                    if c_res.status_code == 200:
                        print("Facebook First Comment সফলভাবে যুক্ত হয়েছে।")
                    else:
                        print(f"First comment সতর্কতা: {c_res.text}")
                except Exception as ce:
                    print(f"First comment যোগ করতে সমস্যা: {ce}")
        else:
            print("ভিডিও প্রসেসিংয়ে বেশি সময় লাগায় থাম্বনেইল/কমেন্ট করা সম্ভব হয়নি।")

    return str(video_id), fb_url


def delete_github_release(release_id):
    if not GITHUB_TOKEN or not release_id:
        return False
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}"
    res = requests.delete(url, headers=headers, timeout=30)
    return res.status_code == 204


def send_telegram_notice(message_text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            res = requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message_text,
                    "parse_mode": "HTML"
                },
                timeout=25
            )
            if res.status_code == 200:
                return
        except Exception as e:
            print(f"Telegram notice চেষ্টায় সাময়িক সমস্যা ({attempt + 1}/3): {e}")
            time.sleep(3)
    print("Telegram নোটিফিকেশন পাঠানো সম্ভব হয়নি, তবে ভিডিও পাবলিশ সফল হয়েছে।")


def check_fb_token_expiry():
    if not all([FB_PAGE_ACCESS_TOKEN, FB_APP_ID, FB_APP_SECRET]):
        return

    app_access_token = f"{FB_APP_ID}|{FB_APP_SECRET}"
    url = "https://graph.facebook.com/v21.0/debug_token"

    try:
        res = requests.get(
            url,
            params={
                "input_token": FB_PAGE_ACCESS_TOKEN,
                "access_token": app_access_token
            },
            timeout=15
        )
        if res.status_code == 200:
            data = res.json().get("data", {})
            data_expires_at = data.get("data_access_expires_at", 0)

            if data_expires_at > 0:
                days_left = (data_expires_at - int(time.time())) // 86400
                print(f"Facebook Data Access মেয়াদ বাকি আছে: {days_left} দিন")

                if days_left <= 7:
                    send_telegram_notice(
                        f"⚠️ <b>Facebook Token সতর্কতা!</b>\n\n"
                        f"আপনার Facebook Data Access-এর মেয়াদ আর মাত্র <b>{days_left} দিন</b> বাকি আছে।\n"
                        f"বট সচল রাখতে নিচের লিংকে গিয়ে অ্যাক্সেস রিনিউ (Renew) করুন:\n"
                        f"🔗 https://www.facebook.com/settings?tab=business_tools"
                    )
    except Exception as e:
        print(f"Facebook Token মেয়াদ চেক সতর্কতা: {e}")


def is_scheduled_time_due(scheduled_str):
    if not scheduled_str or not scheduled_str.strip():
        return False
    try:
        clean_str = scheduled_str.strip().replace(" UTC", "")
        scheduled_dt = datetime.strptime(clean_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        return now_utc >= scheduled_dt
    except Exception as e:
        print(f"Scheduled time parse error ({scheduled_str}): {e}")
        return False


def process_approved_row(sheet, row_num, row_data, header_map):
    raw_title = get_col_val(row_data, header_map, "Shorts Title (YT)")
    script_text = get_col_val(row_data, header_map, "Bangla Voiceover Script")
    post_url = get_col_val(row_data, header_map, "Post URL", "https://www.homebdinfo.com")
    release_id = get_col_val(row_data, header_map, "Drive File ID")
    video_url = get_col_val(row_data, header_map, "Drive File URL")

    existing_yt_status = get_col_val(row_data, header_map, "YouTube Status")
    existing_yt_id = get_col_val(row_data, header_map, "YouTube Video ID")
    existing_yt_url = get_col_val(row_data, header_map, "YouTube URL")
    existing_yt_time = get_col_val(row_data, header_map, "YouTube Published At")

    existing_fb_status = get_col_val(row_data, header_map, "Facebook Status")
    existing_fb_id = get_col_val(row_data, header_map, "Facebook Video ID")
    existing_fb_url = get_col_val(row_data, header_map, "Facebook URL")
    existing_fb_time = get_col_val(row_data, header_map, "Facebook Published At")

    print(f"পাবলিশিং শুরু হচ্ছে: Row {row_num} - {raw_title}")

    if not video_url or not video_url.startswith("http"):
        raise ValueError("Drive File URL খালি বা সঠিক নয়! ভিডিওটি আগে জেনারেট হতে হবে।")

    update_cells(sheet, row_num, header_map, {"Status": "PUBLISHING"})

    video_path = "temp_publish.mp4"
    thumb_path = None

    try:
        download_video(video_url, video_path)
        thumb_path = download_thumbnail_from_release(video_url, row_num, "temp_thumb.jpg")
        seo_meta = generate_seo_metadata(raw_title, script_text, post_url)

        # YouTube Upload (ডুপ্লিকেট চেকসহ)
        if existing_yt_status == "PUBLISHED" and existing_yt_url:
            print(f"YouTube ইতিমধ্যেই পাবলিশ করা আছে, স্কিপ করা হচ্ছে: {existing_yt_url}")
            yt_id, yt_url, yt_time_str = existing_yt_id, existing_yt_url, existing_yt_time
        else:
            yt_id, yt_url = upload_to_youtube(video_path, seo_meta)
            yt_time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            update_cells(sheet, row_num, header_map, {
                "YouTube Status": "PUBLISHED",
                "YouTube Video ID": yt_id,
                "YouTube URL": yt_url,
                "YouTube Published At": yt_time_str
            })

        # Facebook Upload (ডুপ্লিকেট চেকসহ)
        if existing_fb_status == "PUBLISHED" and existing_fb_url:
            print(f"Facebook ইতিমধ্যেই পাবলিশ করা আছে, স্কিপ করা হচ্ছে: {existing_fb_url}")
            fb_id, fb_url, fb_time_str = existing_fb_id, existing_fb_url, existing_fb_time
        else:
            fb_id, fb_url = upload_to_facebook_reels(video_path, seo_meta, post_url, thumb_path)
            fb_time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            update_cells(sheet, row_num, header_map, {
                "Facebook Status": "PUBLISHED",
                "Facebook Video ID": fb_id,
                "Facebook URL": fb_url,
                "Facebook Published At": fb_time_str
            })

        deleted = delete_github_release(release_id)

        update_cells(sheet, row_num, header_map, {
            "Status": "PUBLISHED",
            "Cleanup Status": "DELETED" if deleted else "PENDING",
            "Last Error": ""
        })

        send_telegram_notice(
            f"✅ <b>ভিডিও সফলভাবে পাবলিশ হয়েছে!</b>\n\n"
            f"📌 <b>Title:</b> {seo_meta['yt_title']}\n"
            f"▶️ <b>YouTube:</b> {yt_url}\n"
            f"📘 <b>Facebook Reels:</b> {fb_url}\n"
            f"🗑️ <b>Temp Storage:</b> {'Cleaned' if deleted else 'Pending'}"
        )
        print(f"Row {row_num} সম্পূর্ণভাবে পাবলিশ ও ক্লিনআপ হয়েছে!")

    finally:
        for tmp_f in (video_path, thumb_path):
            if tmp_f and os.path.exists(tmp_f):
                try:
                    os.remove(tmp_f)
                except Exception:
                    pass


def main():
    check_fb_token_expiry()

    sheet = get_sheet()
    header_map = get_header_map(sheet)
    all_rows = sheet.get_all_values()

    for idx in range(1, len(all_rows)):
        row = all_rows[idx]
        status = get_col_val(row, header_map, "Status")
        scheduled_val = get_col_val(row, header_map, "Cleanup After")

        should_publish = False
        if status == "APPROVED":
            should_publish = True
        elif status == "SCHEDULED" and is_scheduled_time_due(scheduled_val):
            print(f"Row {idx + 1}-এর নির্ধারিত শিডিউল সময় ({scheduled_val}) পূর্ণ হয়েছে!")
            should_publish = True

        if should_publish:
            row_num = idx + 1
            try:
                process_approved_row(sheet, row_num, row, header_map)
            except Exception as e:
                err = str(e)[:1000]
                print(f"Publish Error (Row {row_num}): {err}")
                update_cells(sheet, row_num, header_map, {
                    "Status": "PUBLISH_ERROR",
                    "Last Error": err
                })
                raise
            return

    print("পাবলিশ করার মতো কোনো APPROVED বা সময়পূর্ণ SCHEDULED রো পাওয়া যায়নি।")


if __name__ == "__main__":
    main()
