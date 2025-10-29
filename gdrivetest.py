from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from concurrent.futures import ThreadPoolExecutor
import os

def get_drive():
    gauth = GoogleAuth()
    gauth.LoadCredentialsFile("credentials.json")
    if gauth.credentials is None:
        gauth.LocalWebserverAuth()
        gauth.SaveCredentialsFile("credentials.json")
    elif gauth.access_token_expired:
        gauth.Refresh()
        gauth.SaveCredentialsFile("credentials.json")
    else:
        gauth.Authorize()
    return GoogleDrive(gauth)

def upload_folder_to_drive(folder_path, max_workers=4):
    drive = get_drive()
    folder_name = os.path.basename(folder_path)
    folder_drive = drive.CreateFile({'title': folder_name, 'mimeType': 'application/vnd.google-apps.folder'})
    folder_drive.Upload()

    success = 0
    failed = 0

    def upload_file(local_path):
        nonlocal success, failed
        try:
            file_drive = drive.CreateFile({'title': os.path.basename(local_path), 'parents': [{'id': folder_drive['id']}]})
            file_drive.SetContentFile(local_path)
            file_drive.Upload()
            print(f"✅ Uploaded: {os.path.basename(local_path)}")
            success += 1
        except Exception as e:
            print(f"❌ Failed: {os.path.basename(local_path)} | {e}")
            failed += 1

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for root, _, files in os.walk(folder_path):
            for f in files:
                executor.submit(upload_file, os.path.join(root, f))

    print(f"\n✅ Upload complete: {success} files succeeded, ❌ {failed} failed.")
    print(f"📂 Folder link: https://drive.google.com/drive/folders/{folder_drive['id']}")

# Example:
# upload_folder_to_drive("/home/gostlimoss62/Documents/test_upload")


if __name__ == "__main__":
    #src = input("Enter local folder to upload: ").strip()
    src = "/home/gostlimoss62/Documents/1A_projects/file_transfer/Folder_A/"
    if os.path.isdir(src):
        upload_folder_to_drive(src)
    else:
        print("❌ Invalid folder path")
