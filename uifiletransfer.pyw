import os
import shutil
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from pathlib import Path

class transfer(threading.Thread):
    def __init__(self, src, dst, progress_queue, stop_event):
        super().__init__(daemon=True)
        self.src = Path(src).resolve()
        self.dst = Path(dst).resolve()
        self.progress_queue = progress_queue
        self.stop_event = stop_event

    def copy_file(self, s, d, lock, totalsize, current_size):
        if self.stop_event.is_set():
            return
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(s, d)
        size = os.path.getsize(s)

        with lock:
            current_size[0] += size
            pct = int(current_size[0] / totalsize * 100)
            self.progress_queue.put({
                "phase": "downloading",
                "percent": pct,
                "name": os.path.basename(s)
            })

    def run(self):
        if self.src.is_file():
            totalsize = os.path.getsize(self.src)
            lock = Lock()
            current_size = [0]
            dst_file = os.path.join(self.dst, self.src.name)

            self.copy_file(str(self.src), dst_file, lock, totalsize, current_size)
            self.progress_queue.put({"phase": "done"})
            return
        totalsize = 0
        dst_folder = os.path.join(self.dst, os.path.basename(self.src))
        files = []
        for root, _, fs in os.walk(self.src):
            rel = os.path.relpath(root, self.src)
            for f in fs:
                s = os.path.join(root, f)
                d = os.path.join(dst_folder, rel, f)
                files.append((s, d))
                totalsize += os.path.getsize(s)

        if totalsize == 0:
            self.progress_queue.put({"phase": "error", "message": "Source folder is empty"})
            return

        lock = Lock()
        current_size = [0]

        with ThreadPoolExecutor() as executor:
            futures = []
            for s, d in files:
                futures.append(executor.submit(self.copy_file, s, d, lock, totalsize, current_size))
            for f in futures:
                f.result()

        self.progress_queue.put({"phase": "done"})

class App:
    def __init__(self, root):
        self.root = root
        root.title("File Transferer")
        root.geometry("400x170")
        root.resizable(False, False)

        self.progress_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.out_src = tk.StringVar(value=str(Path.cwd()))
        self.out_dst = tk.StringVar(value=str(Path.cwd()))

        style = ttk.Style()
        style.theme_use("xpnative")

        frm = ttk.Frame(root, padding=0, style="Custom.TFrame")
        frm.pack(fill='both', expand=True)

        #Source folder select
        ttk.Label(frm, text="Entry folder:", style="Custom.TLabel").grid(row=1, column=0, sticky='w')
        ttk.Button(frm, text="Browse...", command=self.browse_src, style="Custom.TButton").grid(row=1, column=1, pady=0, sticky='we')
        self.out_entry = ttk.Entry(frm, textvariable=self.out_src)
        self.out_entry.grid(row=2, column=0, columnspan=3, sticky='we')
        
        #output folder select
        ttk.Label(frm, text="Output folder:", style="Custom.TLabel").grid(row=3, column=0, sticky='w')
        ttk.Button(frm, text="Browse...", command=self.browse_dst, style="Custom.TButton").grid(row=3, column=1, pady=0, sticky='we')
        self.out_entry = ttk.Entry(frm, textvariable=self.out_dst)
        self.out_entry.grid(row=4, column=0, columnspan=3, sticky='we')
        
        #progress bar
        self.progress = ttk.Progressbar(frm, orient='horizontal', length=395, mode='determinate')
        self.progress.grid(row=6, column=0, columnspan=2, sticky='we', pady=(0, 0))
        self.status_label = ttk.Label(frm, text="Idle")
        self.status_label.grid(row=5, column=0, columnspan=3, sticky='w', pady=(0, 0))

        #start cancel buttons
        self.start_btn = ttk.Button(frm, text="Start Transfer", command=self.start_transfer, style="Custom.TButton")
        self.start_btn.grid(row=8, column=0, pady=0, sticky='we')
        self.stop_btn = ttk.Button(frm, text="Cancel", command=self.cancel_transfer, style="Custom.TButton", state='disabled')
        self.stop_btn.grid(row=8, column=1, pady=0, sticky='we')

        self.root.after(200, self._process_queue)

    def browse_src(self):
        f = filedialog.askopenfilename(initialdir=str(Path.cwd()))
        if f:
            self.out_src.set(f)
            return
        d = filedialog.askdirectory(initialdir=str(Path.cwd()))
        if d:
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
            delay = 50 if self.worker and self.worker.is_alive() else 1000
            self.root.after(delay, self._process_queue)


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
