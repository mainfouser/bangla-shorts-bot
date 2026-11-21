
import os
import json
import requests
import gspread
import traceback

from datetime import datetime, timezone

from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.auth.transport.requests import Request

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload


# =========================================================
# CONFIGURATION
# =========================================================

SHEET_NAME = "Shorts_Queue"
VIDEO_FILE = "yt_upload.mp4"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload"
]


# =========================================================
# GOOGLE SHEET CONNECTION
# =========================================================

creds_dict = json.loads(
    os.environ["GOOGLE_CREDENTIALS"]
)

service_creds = ServiceAccountCredentials.from_service_account_info(
    creds_dict,
    scopes=SCOPES
)

client = gspread.authorize(service_creds)

sheet = client.open_by_key(
    os.environ["SHEET_ID"]
).worksheet(SHEET_NAME)


# =========================================================
# SHEET HEADER MAPPING
# =========================================================

headers = sheet.row_values(1)

COL = {
    name: index + 1
    for index, name in enumerate(headers)
}

REQUIRED_HEADERS = [
    "Post URL",
    "Shorts Title (YT)",
    "Status",
    "Approval Status",
    "Drive File ID",
    "YouTube Status",
    "YouTube Video ID",
    "YouTube URL",
    "YouTube Published At",
    "Last Error"
]

missing_headers = [
    name for name in REQUIRED_HEADERS
    if name not in COL
]

if missing_headers:
    raise ValueError(
        "Google Sheet-এ প্রয়োজনীয় Header পাওয়া যায়নি: "
        + ", ".join(missing_headers)
    )


def update_field(row_number, field, value):
    sheet.update_cell(
        row_number,
        COL[field],
        value
    )


def update_fields(row_number, data):
    for field, value in data.items():
        update_field(row_number, field, value)


# =========================================================
# GOOGLE DRIVE CONNECTION
# =========================================================

drive_service = build(
    "drive",
    "v3",
    credentials=service_creds,
    cache_discovery=False
)


# =========================================================
# YOUTUBE OAUTH CONNECTION
# =========================================================

yt_creds = OAuthCredentials(
    token=None,
    refresh_token=os.environ["YT_REFRESH_TOKEN"],
    token_uri="https://oauth2.googleapis.com/token",
    client_id=os.environ["YT_CLIENT_ID"],
    client_secret=os.environ["YT_CLIENT_SECRET"],
    scopes=YOUTUBE_SCOPES
)

# Refresh access token
yt_creds.refresh(Request())

youtube = build(
    "youtube",
    "v3",
    credentials=yt_creds
)


# =========================================================
# DOWNLOAD VIDEO FROM GOOGLE DRIVE
# =========================================================

def download_from_drive(file_id, output_path):

    request = drive_service.files().get_media(
        fileId=file_id
    )

    with open(output_path, "wb") as file_handle:

        downloader = MediaIoBaseDownload(
            file_handle,
            request,
            chunksize=8 * 1024 * 1024
        )

        done = False

        while not done:

            status, done = downloader.next_chunk()

            if status:
                progress = int(
                    status.progress() * 100
                )

                print(
                    f"Drive Download: {progress}%"
                )

    if not os.path.exists(output_path):
        raise RuntimeError(
            "Google Drive থেকে ভিডিও ডাউনলোড হয়নি।"
        )

    if os.path.getsize(output_path) == 0:
        raise RuntimeError(
            "ডাউনলোড করা ভিডিও ফাইল খালি।"
        )

    return output_path


# =========================================================
# YOUTUBE VIDEO UPLOAD
# =========================================================

def upload_to_youtube(video_path, title, description):

    body = {
        "snippet": {
            "title": title[:100],
            "description": (
                description
                + "\n\n#Shorts #BanglaShorts"
            ),
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": "public"
        }
    }

    media = MediaFileUpload(
        video_path,
        chunksize=8 * 1024 * 1024,
        resumable=True,
        mimetype="video/mp4"
    )

    upload_request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None

    while response is None:

        status, response = upload_request.next_chunk()

        if status:
            progress = int(
                status.progress() * 100
            )

            print(
                f"YouTube Upload: {progress}%"
            )

    if not response or not response.get("id"):
        raise RuntimeError(
            "YouTube Video ID পাওয়া যায়নি।"
        )

    return response["id"]


# =========================================================
# TELEGRAM NOTIFICATION
# =========================================================

def notify_telegram(message):

    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    telegram_url = (
        f"https://api.telegram.org/"
        f"bot{bot_token}/sendMessage"
    )

    response = requests.post(
        telegram_url,
        json={
            "chat_id": chat_id,
            "text": message
        },
        timeout=30
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            "Telegram Notification Error: "
            + str(result)
        )


# =========================================================
# FIND APPROVED VIDEO
# =========================================================

def select_approved_row():

    records = sheet.get_all_records()

    for index, item in enumerate(records):

        row_number = index + 2

        status = str(
            item.get("Status", "")
        ).strip()

        approval = str(
            item.get("Approval Status", "")
        ).strip()

        youtube_status = str(
            item.get("YouTube Status", "")
        ).strip()

        drive_file_id = str(
            item.get("Drive File ID", "")
        ).strip()

        youtube_id = str(
            item.get("YouTube Video ID", "")
        ).strip()

        # Already published: Skip
        if youtube_status == "PUBLISHED" or youtube_id:
            continue

        # Avoid duplicate upload after uncertain result
        if youtube_status == "UPLOADING":
            continue

        # Only approved videos
        if status != "APPROVED":
            continue

        if approval != "APPROVED":
            continue

        # Only empty or failed status can be processed
        if youtube_status not in ("", "FAILED"):
            continue

        if not drive_file_id:
            continue

        return row_number, item

    return None, None


# =========================================================
# PROCESS VIDEO
# =========================================================

def process_video(row_number, item):

    title = str(
        item.get("Shorts Title (YT)", "")
    ).strip()

    post_url = str(
        item.get("Post URL", "")
    ).strip()

    drive_file_id = str(
        item.get("Drive File ID", "")
    ).strip()

    if not title:
        raise ValueError(
            "YouTube Title খালি।"
        )

    if not drive_file_id:
        raise ValueError(
            "Drive File ID পাওয়া যায়নি।"
        )

    # Mark uploading before API request
    update_fields(row_number, {
        "YouTube Status": "UPLOADING",
        "Last Error": ""
    })

    print(
        f"YouTube Upload শুরু: Row {row_number}"
    )

    # Download from Drive
    download_from_drive(
        drive_file_id,
        VIDEO_FILE
    )

    description = (
        "বিস্তারিত পড়ুন:\n"
        + post_url
        + "\n\n"
        "Home BD Info - বাংলাদেশের প্রয়োজনীয় তথ্য ও গাইড।"
    )

    # Upload to YouTube
    youtube_id = upload_to_youtube(
        VIDEO_FILE,
        title,
        description
    )

    youtube_url = (
        "https://www.youtube.com/shorts/"
        + youtube_id
    )

    published_at = datetime.now(
        timezone.utc
    ).isoformat()

    # Update Google Sheet
    update_fields(row_number, {
        "YouTube Status": "PUBLISHED",
        "YouTube Video ID": youtube_id,
        "YouTube URL": youtube_url,
        "YouTube Published At": published_at,
        "Last Error": ""
    })

    print(
        "YouTube Upload সফল:",
        youtube_url
    )

    # Telegram notification
    notify_telegram(
        "🎉 তোমার Shorts সফলভাবে YouTube-এ Publish হয়েছে!\n\n"
        + youtube_url
    )


# =========================================================
# MAIN
# =========================================================

def main():

    row_number = None

    try:

        row_number, item = select_approved_row()

        if row_number is None:

            print(
                "নতুন Approved ভিডিও পাওয়া যায়নি।"
            )

            return

        process_video(
            row_number,
            item
        )

    except Exception as error:

        error_text = str(error)[:1000]

        print(
            "YOUTUBE UPLOAD ERROR:",
            error_text
        )

        traceback.print_exc()

        if row_number:

            try:

                update_fields(row_number, {
                    "YouTube Status": "FAILED",
                    "Last Error": error_text
                })

            except Exception as sheet_error:

                print(
                    "Google Sheet Update Error:",
                    str(sheet_error)
                )

        raise

    finally:

        if os.path.exists(VIDEO_FILE):

            try:
                os.remove(VIDEO_FILE)
            except Exception:
                pass


if __name__ == "__main__":
    main()
