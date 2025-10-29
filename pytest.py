import os
import shutil
import hashlib
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

def file_hash(path, block_size=65536): #Return MD5 hash of a file (used to detect changes).
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block_size), b""):
            md5.update(chunk)
    return md5.hexdigest()

def needs_update(src, dst): #Check if dst needs to be updated from src.
    if not os.path.exists(dst):
        return True
    if os.path.getsize(src) != os.path.getsize(dst):
        return True
    if int(os.path.getmtime(src)) > int(os.path.getmtime(dst)):
        return True
    return file_hash(src) != file_hash(dst)

def copy_file_with_progress(src, dst, position, chunk_size=1024*1024): #Copy file with aprogress bar
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        total_size = os.path.getsize(src)

        with open(src, "rb") as fsrc, open(dst, "wb") as fdst, tqdm(
            total=total_size,
            unit="B",
            unit_scale=True,
            desc=os.path.basename(src),
            position=position,
            leave=False,
        ) as pbar:
            while True:
                chunk = fsrc.read(chunk_size)
                if not chunk:
                    break
                fdst.write(chunk)
                pbar.update(len(chunk))

        shutil.copystat(src, dst)  # preserve metadata
        return ("updated", f"Updated: {os.path.basename(src)}")
    except Exception as e:
        return ("error", f"Error {src}: {e}")

def process_file(src, dst, position): #Process one file with progress bar.
    if needs_update(src, dst):
        return copy_file_with_progress(src, dst, position)
    else:
        return ("skipped", f"Skipped: {os.path.basename(src)}")

def sync_file(src_file, dst_folder): #Sync a single file directly into destination root.
    dst_file = os.path.join(dst_folder, os.path.basename(src_file))
    result, msg = process_file(src_file, dst_file, 0)
    print(f"\n--- Sync Summary for {src_file} ---")
    print(msg)

def sync_folder(src_folder, dst_folder, workers=4): #Sync a folder with parallel byte-level progress bars.
    dst_folder = os.path.join(dst_folder, os.path.basename(src_folder))
    os.makedirs(dst_folder, exist_ok=True)

    tasks = []
    for root, _, files in os.walk(src_folder):
        rel_path = os.path.relpath(root, src_folder)
        dst_root = os.path.join(dst_folder, rel_path)
        for f in files:
            src_file = os.path.join(root, f)
            dst_file = os.path.join(dst_root, f)
            tasks.append((src_file, dst_file))

    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_file, s, d, i): (s, d)
            for i, (s, d) in enumerate(tasks)
        }
        for f in ThreadPoolExecutor(futures):
            results.append(f.result())

    # Count stats
    updated = sum(1 for r in results if r[0] == "updated")
    skipped = sum(1 for r in results if r[0] == "skipped")
    errors = sum(1 for r in results if r[0] == "error")

    print(f"\n--- Sync Summary for {src_folder} ---")
    print(f"{updated} updated, {skipped} skipped, {errors} errors")

def sync_multiple(src_paths, dst_folder, workers=4): #Sync multiple files/folders with parallel progress bars
    for src in src_paths:
        if os.path.isfile(src):
            sync_file(src, dst_folder)  # ✅ goes to HDD root
        elif os.path.isdir(src):
            sync_folder(src, dst_folder, workers=workers)  # ✅ keeps folder
        else:
            print(f"⚠️ Skipped invalid path: {src}")

if __name__ == "__main__":
    print("Enter the source files or folders you want to sync.")
    print("Enter one per line, and press Enter on an empty line when done.\n")

    src_paths = []
    while True:
        path = input("Source path: ").strip()
        if not path:
            break
        if os.path.exists(path):
            src_paths.append(path)
        else:
            print("⚠️ Path does not exist, try again.")

    if not src_paths:
        print("No valid sources selected. Exiting.")
        exit()

    dst = input("\nEnter destination folder (HDD path): ").strip()
    if not os.path.exists(dst):
        os.makedirs(dst, exist_ok=True)

    sync_multiple(src_paths, dst, workers=4)
