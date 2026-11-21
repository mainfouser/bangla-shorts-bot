import os
import io
import json
import requests
import gspread
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# 1. URL যাতে না কাটে সেজন্য ভেঙে Scope তৈরি করা হলো
creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
base_auth = "https://" + "://googleapis.com" + "/auth/"
scopes = [
    base_auth + "spreadsheets",
    base_auth + "drive"
]
creds = ServiceAccountCredentials.from_service_account_info(creds_dict, scopes=scopes)
client = gspread.authorize(creds)
sheet = client.open_by_key(os.environ["SHEET_ID"]).worksheet("Shorts_Queue")
drive_service = build("drive", "v3", credentials=creds)

# 2. YouTube API কানেকশন
token_url = "https://" + "://googleapis.com" + "/token"
yt_creds = Credentials(
    token=None,
    refresh_token=os.environ["YT_REFRESH_TOKEN"],
    token_uri=token_url,
    client_id=os.environ["YT_CLIENT_ID"],
    client_secret=os.environ["YT_CLIENT_SECRET"]
)
youtube = build("youtube", "v3", credentials=yt_creds)

def download_from_drive(file_id, output_path):
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.FileIO(output_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()

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
    tg_url = "https://" + "api.telegram.org" + f"/bot{bot_token}/sendMessage"
    requests.post(tg_url, json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text})

def main():
    rows = sheet.get_all_values()
    for i in range(1, len(rows)):
        row = rows[i]
        if len(row) <= 8:
            continue
            
        status = row
        drive_file_id = row
        
        if status == "APPROVED" and drive_file_id and not drive_file_id.startswith("http"):
            row_num = i + 1
            title = row
            post_url = row
            
            print(f"YouTube-এ আপলোড হচ্ছে (Row {row_num}): {title}")
            download_from_drive(drive_file_id, "yt_upload.mp4")
            yt_id = upload_to_youtube("yt_upload.mp4", title, f"বিস্তারিত পড়ুন: {post_url}")
            
            if yt_id:
                yt_link = "https://" + "://youtube.com" + yt_id
                sheet.update_cell(row_num, 8, "PUBLISHED")
                sheet.update_cell(row_num, 9, yt_link)
                drive_service.files().delete(fileId=drive_file_id).execute()
                notify_telegram(f"🎉 অভিনন্দন! আপনার শর্টসটি YouTube-এ পাবলিশ হয়েছে:\n{yt_link}")
                
        elif status == "REJECTED" and drive_file_id and not drive_file_id.startswith("http") and drive_file_id != "DELETED_FROM_DRIVE":
            drive_service.files().delete(fileId=drive_file_id).execute()
            sheet.update_cell(i + 1, 9, "DELETED_FROM_DRIVE")

if __name__ == "__main__":
    main()
