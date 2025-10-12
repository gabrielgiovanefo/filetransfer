import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm
from threading import Lock

def copy_file(s, d, pbar, lock):
    os.makedirs(os.path.dirname(d), exist_ok=True)
    shutil.copy2(s, d)
    size = os.path.getsize(s)
    with lock:
        pbar.update(size)

def foldercopy(src, dst):
    totalsize = 0
    size = os.path.getsize(src)#stores source folder size
    dst_folder = os.path.join(dst, os.path.basename(src)) #stores distribution folder path

    #adding all source folder file paths into files=[]
    files = []
    for root, _, fs in os.walk(src):
        rel = os.path.relpath(root, src)
        for f in fs:
            s = os.path.join(root, f)
            d = os.path.join(dst_folder, rel, f)
            files.append((s, d))
            totalsize += size

    #passing each path through copy_file while threading 
    lock = Lock()
    with tqdm(total=totalsize, unit="B",unit_scale=True, mininterval=0.0) as pbar:
        with ThreadPoolExecutor() as thread:
            thread.map(lambda x: copy_file(*x, pbar, lock), files)

if __name__ == "__main__":
    src = input("Source folder: ").strip()
    #dst = input("Destination folder: ").strip()
    #src = "C:\AAAtest"
    dst = "C:\BBBtest"

    foldercopy(src, dst)
