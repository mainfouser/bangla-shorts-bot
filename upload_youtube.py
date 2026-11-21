
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

MAX_ERROR_LENGTH = 1000
DOWNLOAD_CHUNK_SIZE = 8 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024


# =========================================================
# ENVIRONMENT VALIDATION
# =========================================================

REQUIRED_ENV = [
    "GOOGLE_CREDENTIALS",
    "SHEET_ID",
    "YT_REFRESH_TOKEN",
    "YT_CLIENT_ID",
    "YT_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID"
]

for key in REQUIRED_ENV:
    if not os.environ.get(key):
        raise ValueError(
            f"GitHub Secret / Environment Variable পাওয়া যায়নি: {key}"
        )


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

spreadsheet = client.open_by_key(
    os.environ["SHEET_ID"]
)

sheet = spreadsheet.worksheet(SHEET_NAME)


# =========================================================
# SHEET HEADER MAPPING
# =========================================================

headers = sheet.row_values(1)

COL = {
    str(name).strip(): index + 1
    for index, name in enumerate(headers)
    if str(name).strip()
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

    if field not in COL:
        raise ValueError(
            f"Sheet Header পাওয়া যায়নি: {field}"
        )

    sheet.update_cell(
        row_number,
        COL[field],
        value
    )


def update_fields(row_number, data):

    for field, value in data.items():
        update_field(
            row_number,
            field,
            value
        )


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

yt_creds.refresh(Request())

youtube = build(
    "youtube",
    "v3",
    credentials=yt_creds,
    cache_discovery=False
)


# =========================================================
# GOOGLE DRIVE DOWNLOAD
# =========================================================

def download_from_drive(file_id, output_path):

    print("Google Drive থেকে ভিডিও ডাউনলোড শুরু...")

    request = drive_service.files().get_media(
        fileId=file_id
    )

    with open(output_path, "wb") as file_handle:

        downloader = MediaIoBaseDownload(
            file_handle,
            request,
            chunksize=DOWNLOAD_CHUNK_SIZE
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

    file_size = os.path.getsize(output_path)

    if file_size <= 0:
        raise RuntimeError(
            "ডাউনলোড করা ভিডিও ফাইল খালি।"
        )

    print(
        f"Drive Download সম্পন্ন: {file_size} bytes"
    )

    return output_path


# =========================================================
# YOUTUBE UPLOAD
# =========================================================

def upload_to_youtube(video_path, title, description):

    print("YouTube Upload শুরু...")

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
        chunksize=UPLOAD_CHUNK_SIZE,
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

        # Already published: Never upload again
        if youtube_status == "PUBLISHED" or youtube_id:
            continue

        # Uncertain previous upload: Manual verification required
        if youtube_status == "UPLOADING":
            continue

        # Only approved videos
        if status != "APPROVED":
            continue

        if approval != "APPROVED":
            continue

        # Only blank or FAILED status
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

    # Mark UPLOADING before starting
    update_fields(row_number, {
        "YouTube Status": "UPLOADING",
        "Last Error": ""
    })

    print(
        f"YouTube Upload শুরু: Sheet Row {row_number}"
    )

    # Download video from Drive
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

    # Upload video
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

    # IMPORTANT:
    # Save YouTube success BEFORE Telegram notification
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

    # Telegram notification is independent
    try:

        notify_telegram(
            "🎉 তোমার Shorts সফলভাবে YouTube-এ Publish হয়েছে!\n\n"
            + youtube_url
        )

        print(
            "Telegram Notification সফল।"
        )

    except Exception as notification_error:

        print(
            "Telegram Notification Failed:",
            str(notification_error)
        )

        # Do NOT change YouTube Status to FAILED


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

        error_text = str(error)[:MAX_ERROR_LENGTH]

        print(
            "YOUTUBE UPLOAD ERROR:",
            error_text
        )

        traceback.print_exc()

        if row_number:

            try:

                # If YouTube already has an ID, never mark FAILED
                current_item = sheet.row_values(row_number)

                video_id_col = COL["YouTube Video ID"] - 1
                status_col = COL["YouTube Status"] - 1

                current_video_id = (
                    current_item[video_id_col]
                    if len(current_item) > video_id_col
                    else ""
                )

                current_status = (
                    current_item[status_col]
                    if len(current_item) > status_col
                    else ""
                )

                if current_video_id or current_status == "PUBLISHED":

                    print(
                        "YouTube ইতিমধ্যে PUBLISHED। "
                        "Status পরিবর্তন করা হয়নি।"
                    )

                else:

                    # Keep UPLOADING if the result may be uncertain.
                    # Only errors before an upload request can be retried safely.
                    #
                    # This conservative policy leaves uncertain uploads
                    # for manual verification.
                    print(
                        "Upload ফলাফল অনিশ্চিত হতে পারে। "
                        "Duplicate ঠেকাতে Status স্বয়ংক্রিয়ভাবে পরিবর্তন করা হয়নি।"
                    )

                    update_fields(row_number, {
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

                print(
                    "Temporary video file deleted."
                )

            except Exception as cleanup_error:

                print(
                    "Temporary File Cleanup Error:",
                    str(cleanup_error)
                )


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    main()
