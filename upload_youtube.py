import os
import json
import base64
import requests
import gspread
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# 1. Google Sheet কানেকশন
creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
scopes = [
    base64.b64decode("aHR0cHM6Ly93d3cuZ29vZ2xlYXBpcy5jb20vYXV0aC9zcHJlYWRzaGVldHM=").decode("utf-8"),
    base64.b64decode("aHR0cHM6Ly93d3cuZ29vZ2xlYXBpcy5jb20vYXV0aC9kcml2ZQ==").decode("utf-8")
]
creds = ServiceAccountCredentials.from_service_account_info(creds_dict, scopes=scopes)
client = gspread.authorize(creds)
sheet = client.open_by_key(os.environ["SHEET_ID"]).worksheet("Shorts_Queue")

# 2. YouTube API কানেকশন
token_url = base64.b64decode("aHR0cHM6Ly9vYXV0aDIuZ29vZ2xlYXBpcy5jb20vdG9rZW4=").decode("utf-8")
yt_creds = Credentials(
    token=None,
    refresh_token=os.environ["YT_REFRESH_TOKEN"],
    token_uri=token_url,
    client_id=os.environ["YT_CLIENT_ID"],
    client_secret=os.environ["YT_CLIENT_SECRET"]
)
youtube = build("youtube", "v3", credentials=yt_creds)

# 3. টেলিগ্রাম থেকে ভিডিও ডাউনলোড
def download_from_telegram(file_id, output_path):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_host = base64.b64decode("aHR0cHM6Ly9hcGkudGVsZWdyYW0ub3Jn").decode("utf-8")
    
    # ফাইলের পাথ বের করা
    file_info = requests.get(f"{tg_host}/bot{bot_token}/getFile?file_id={file_id}").json()
    file_path = file_info["result"]["file_path"]
    
    # ভিডিও ডাউনলোড করা
    download_url = f"{tg_host}/file/bot{bot_token}/{file_path}"
    with open(output_path, "wb") as f:
        f.write(requests.get(download_url).content)

def upload_to_youtube(video_path, title, description):
    body = {
        "snippet": {
            "title": title[:100],
            "description": description + "\n\n#Shorts #BanglaShorts",
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    return response.get("id")

def notify_telegram(text):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_host = base64.b64decode("aHR0cHM6Ly9hcGkudGVsZWdyYW0ub3Jn").decode("utf-8")
    tg_url = f"{tg_host}/bot{bot_token}/sendMessage"
    requests.post(tg_url, json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text})

def main():
    records = sheet.get_all_records()
    for idx, item in enumerate(records):
        row_num = idx + 2
        status = str(item.get("Status", "")).strip()
        tg_file_id = str(item.get("Video Drive/Cloud URL", "")).strip()
        
        if status == "APPROVED" and tg_file_id and not tg_file_id.startswith("http"):
            title = str(item.get("Shorts Title (YT)", ""))
            post_url = str(item.get("Post URL", ""))
            
            print(f"YouTube-এ আপলোড হচ্ছে (Row {row_num}): {title}")
            download_from_telegram(tg_file_id, "yt_upload.mp4")
            yt_id = upload_to_youtube("yt_upload.mp4", title, f"বিস্তারিত পড়ুন: {post_url}")
            
            if yt_id:
                yt_host = base64.b64decode("aHR0cHM6Ly95b3V0dWJlLmNvbS9zaG9ydHMv").decode("utf-8")
                yt_link = yt_host + yt_id
                sheet.update_cell(row_num, 8, "PUBLISHED")
                sheet.update_cell(row_num, 9, yt_link)
                notify_telegram(f"🎉 অভিনন্দন! আপনার শর্টসটি YouTube-এ পাবলিশ হয়েছে:\n{yt_link}")

if __name__ == "__main__":
    main()
