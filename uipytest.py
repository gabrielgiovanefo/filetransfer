import os, shutil, queue, threading, hashlib
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from pathlib import Path

CHUNK_SIZE = 1024 * 1024  # 1 MB
workers = min(8, max(2, os.cpu_count() or 4))

class transfer(threading.Thread):
    def __init__(self, src, dst, progress_queue, stop_event, workers):
        super().__init__(daemon=True)
        self.src = Path(src).resolve()
        self.dst = Path(dst).resolve()
        self.progress_queue = progress_queue
        self.stop_event = stop_event
        self.workers = workers

    @staticmethod
    def file_hash(path, block_size=65536):
        md5 = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(block_size), b""):
                md5.update(chunk)
        return md5.hexdigest()

    @staticmethod
    def needs_update(src_path: Path, dst_path: Path) -> bool:
        if not dst_path.exists():
            return True
        try:
            if src_path.stat().st_size != dst_path.stat().st_size:
                return True
            if int(src_path.stat().st_mtime) > int(dst_path.stat().st_mtime):
                return True
        except Exception:
            pass
        try:
            return transfer.file_hash(src_path) != transfer.file_hash(dst_path)
        except Exception:
            return True

    def copy_file_with_progress(self, src_file: Path, dst_file: Path,totalsize: int, current_size: list, lock: Lock):
        if self.stop_event.is_set():
            return ("cancelled", f"Cancelled: {src_file}")

        try:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            src_size = src_file.stat().st_size
            with src_file.open("rb") as fsrc, dst_file.open("wb") as fdst:
                while True:
                    if self.stop_event.is_set():
                        return ("cancelled", f"Cancelled: {src_file}")
                    chunk = fsrc.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fdst.write(chunk)

                    with lock:
                        current_size[0] += len(chunk)
                        pct = int(current_size[0] / totalsize * 100)
                        self.progress_queue.put({
                            "phase": "downloading",
                            "percent": pct,
                            "name": src_file.name
                        })

            try:
                shutil.copystat(src_file, dst_file)
            except Exception:
                pass

            return ("updated", f"Updated: {src_file}")
        except Exception as e:
            return ("error", f"Error copying {src_file}: {e}")

    def process_file(self, src_file: Path, dst_file: Path, totalsize: int,current_size: list, lock: Lock):
        if self.stop_event.is_set():
            return ("cancelled", f"Cancelled: {src_file}")

        try:
            if transfer.needs_update(src_file, dst_file):
                return self.copy_file_with_progress(src_file, dst_file, totalsize, current_size, lock)
            else:
                with lock:
                    pct = int(current_size[0] / totalsize * 100) if totalsize else 100
                    self.progress_queue.put({
                        "phase": "downloading",
                        "percent": pct,
                        "name": src_file.name
                    })
                return ("skipped", f"Skipped: {src_file}")
        except Exception as e:
            return ("error", f"Error processing {src_file}: {e}")

    def sync_folder(self):
        dst_root = self.dst.joinpath(self.src.name)
        dst_root.mkdir(parents=True, exist_ok=True)

        tasks = []
        totalsize = 0
        for root, _, files in os.walk(self.src):
            rel = os.path.relpath(root, self.src)
            for f in files:
                s = Path(root) / f
                d = dst_root.joinpath(rel) / f
                tasks.append((s, d))
                try:
                    totalsize += s.stat().st_size
                except Exception:
                    pass

        if totalsize == 0 and tasks:
            totalsize = 1

        lock = Lock()
        current_size = [0]
        results = []

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(self.process_file, s, d, totalsize, current_size, lock) for s, d in tasks]
            for fut in futures:
                try:
                    res = fut.result()
                except Exception as e:
                    res = ("error", f"Worker exception: {e}")
                results.append(res)
                if self.stop_event.is_set():
                    break

        updated = sum(1 for r in results if r[0] == "updated")
        skipped = sum(1 for r in results if r[0] == "skipped")
        errors = sum(1 for r in results if r[0] == "error")
        cancelled = sum(1 for r in results if r[0] == "cancelled")

        print(f"\n--- Sync Summary for {self.src} ---")
        print(f"{updated} updated, {skipped} skipped, {errors} errors, {cancelled} cancelled")

        return results

    def sync_file(self):
        dst_file = self.dst.joinpath(self.src.name)
        try:
            totalsize = self.src.stat().st_size
        except Exception:
            totalsize = 1
        lock = Lock()
        current_size = [0]
        res = self.process_file(self.src, dst_file, totalsize, current_size, lock)
        return [res]

    def run(self):
        try:
            if not self.src.exists():
                self.progress_queue.put({"phase": "error", "message": f"Source does not exist: {self.src}"})
                return

            if self.src.is_file():
                results = self.sync_file()
            elif self.src.is_dir():
                results = self.sync_folder()
            else:
                self.progress_queue.put({"phase": "error", "message": f"Invalid source path: {self.src}"})
                return

            if self.stop_event.is_set():
                self.progress_queue.put({"phase": "cancelled"})
            else:
                if any(r[0] == "error" for r in results):
                    self.progress_queue.put({"phase": "error", "message": "Some files failed; check console"})
                else:
                    self.progress_queue.put({"phase": "done"})
        except Exception as e:
            self.progress_queue.put({"phase": "error", "message": str(e)})


class App:
    def __init__(self, root):
        self.root = root
        root.title("File Transferer(chunk)")
        root.geometry("395x210")
        

        self.progress_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.out_src = tk.StringVar(value=str(Path.cwd()))
        self.out_dst = tk.StringVar(value=str(Path.cwd()))

        style = ttk.Style()
        style.theme_use("classic")

        frm = ttk.Frame(root, padding=0)
        frm.pack(fill='both', expand=True)

        #Source folder select
        ttk.Label(frm, text="Entry folder:").grid(row=1, column=0, sticky='w')
        ttk.Button(frm, text="Browse...", command=self.browse_src).grid(row=1, column=1, sticky='we')
        self.out_entry = ttk.Entry(frm, textvariable=self.out_src)
        self.out_entry.grid(row=2, column=0, columnspan=3, sticky='we')
        
        #output folder select
        ttk.Label(frm, text="Output folder:").grid(row=3, column=0, sticky='w')
        ttk.Button(frm, text="Browse...", command=self.browse_dst).grid(row=3, column=1, sticky='we')
        self.out_entry = ttk.Entry(frm, textvariable=self.out_dst)
        self.out_entry.grid(row=4, column=0, columnspan=3, sticky='we')
        
        #progress bar
        self.progress = ttk.Progressbar(frm, orient='horizontal', length=395, mode='determinate')
        self.progress.grid(row=6, column=0, columnspan=2, sticky='we')
        self.status_label = ttk.Label(frm, text="Idle")
        self.status_label.grid(row=5, column=0, columnspan=3, sticky='w')

        #start cancel buttons
        self.start_btn = ttk.Button(frm, text="Start Transfer", command=self.start_transfer)
        self.start_btn.grid(row=7, column=0, sticky='we')
        self.stop_btn = ttk.Button(frm, text="Cancel", command=self.cancel_transfer, state='disabled')
        self.stop_btn.grid(row=7, column=1, sticky='we')

        self.root.after(200, self._process_queue)

    def browse_src(self):
        f = filedialog.askopenfilename(initialdir=str(Path.cwd()))
        if f:
            self.out_src.set(f)
            print(f)
            return
        d = filedialog.askdirectory(initialdir=str(Path.cwd()))
        if d:
            print(d)
            self.out_src.set(d)

    def browse_dst(self):
        d = filedialog.askdirectory(initialdir=self.out_dst.get())
        if d:
            self.out_dst.set(d)

    def start_transfer(self):
        src = self.out_src.get().strip()
        dst = self.out_dst.get().strip()
        if not src or not dst:
            messagebox.showwarning("Missing Folder", "Please select a folder.")
            return
        
        self.progress['value'] = 0
        self.status_label.config(text="Starting...")

        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.stop_event.clear()
        self.worker = transfer(src, dst, self.progress_queue, self.stop_event)
        self.worker.start()

    def cancel_transfer(self):
        if not self.worker:
            return
        self.stop_event.set()
        self.status_label.config(text="Cancelling...")
        self.stop_btn.config(state='disabled')

    def _process_queue(self):
        try:
            while True:
                msg = self.progress_queue.get_nowait()
                phase = msg.get('phase')

                if phase == 'downloading':
                    pct = msg.get('percent', 0)
                    name = msg.get('name', '')
                    self.progress['value'] = pct
                    self.status_label.config(text=f"Copying({pct}%): {name}")

                elif phase == 'done':
                    self.status_label.config(text="Finished")
                    self.progress['value'] = 100
                    self.start_btn.config(state='normal')
                    self.stop_btn.config(state='disabled')
                    self.worker = None

                elif phase == 'error':
                    msgtxt = msg.get('message', 'Unknown error')
                    self.status_label.config(text=f"Error: {msgtxt}")
                    messagebox.showerror("Transfer error", f"An error occurred:\n{msgtxt}")
                    self.start_btn.config(state='normal')
                    self.stop_btn.config(state='disabled')
                    self.worker = None

        except queue.Empty:
            pass
        finally:
            delay = 150 if self.worker and self.worker.is_alive() else 1000
            self.root.after(delay, self._process_queue)


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()
if __name__ == "__main__":
    main()
