import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from pathlib import Path
import hashlib
import shutil
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

CHUNK_SIZE = 1024 * 1024  # 1 MB
LARGE_FILE_THRESHOLD = 10 * 1024 * 1024  # 10 MB
WORKERS = min(8, max(2, os.cpu_count() or 4))


def get_file_hash(path, block_size=65536):
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block_size), b""):
            md5.update(chunk)
    return md5.hexdigest()

def get_drive():
    client_secrets_file = 'client_secrets.json'
    credentials_file = 'credentials.json'

    gauth = GoogleAuth()

    try:
        gauth.LoadCredentialsFile(credentials_file)
    except Exception as e:
        print(f"Warning: Could not load credentials file ({e}). Starting fresh authentication.")
        if os.path.exists(credentials_file):
            try:
                os.remove(credentials_file)
                print("Removed old/corrupted credentials file.")
            except OSError as e:
                print(f"Error removing credentials file: {e}")
        gauth.LoadCredentialsFile(credentials_file)

    if gauth.credentials is None:
        gauth.LocalWebserverAuth()
    elif gauth.access_token_expired:
        gauth.Refresh()
    else:
        gauth.Authorize()

    gauth.SaveCredentialsFile(credentials_file)
    return GoogleDrive(gauth)

class LocalTransfer(threading.Thread):
    def __init__(self, src, dst, progress_queue, stop_event, workers, tab_id):
        super().__init__(daemon=True)
        self.src = Path(src).resolve()
        self.dst = Path(dst).resolve()
        self.progress_queue = progress_queue
        self.stop_event = stop_event
        self.workers = workers
        self.tab_id = tab_id

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
            return True
        try:
            return get_file_hash(src_path) != get_file_hash(dst_path)
        except Exception:
            return True

    def copy_file_with_progress(self, src_file: Path, dst_file: Path, totalsize: int, current_size: list, lock: Lock):
        if self.stop_event.is_set():
            return ("cancelled", f"Cancelled: {src_file}")
        try:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            file_size = src_file.stat().st_size
            if file_size >= LARGE_FILE_THRESHOLD:
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
                                "phase": "downloading", "percent": pct, "name": src_file.name, "tab_id": self.tab_id
                            })
                try:
                    shutil.copystat(src_file, dst_file)
                except Exception:
                    pass
                return ("updated", f"Updated: {src_file}")
            else:
                shutil.copy2(src_file, dst_file)
                with lock:
                    current_size[0] += file_size
                    pct = int(current_size[0] / totalsize * 100)
                    self.progress_queue.put({
                        "phase": "downloading", "percent": pct, "name": src_file.name, "tab_id": self.tab_id
                    })
                return ("updated", f"Updated: {src_file}")
        except Exception as e:
            return ("error", f"Error copying {src_file}: {e}")

    def process_file(self, src_file: Path, dst_file: Path, totalsize: int, current_size: list, lock: Lock):
        if self.stop_event.is_set():
            return ("cancelled", f"Cancelled: {src_file}")
        try:
            if LocalTransfer.needs_update(src_file, dst_file):
                return self.copy_file_with_progress(src_file, dst_file, totalsize, current_size, lock)
            else:
                with lock:
                    pct = int(current_size[0] / totalsize * 100) if totalsize else 100
                    self.progress_queue.put({
                        "phase": "downloading", "percent": pct, "name": f"(Skipped) {src_file.name}", "tab_id": self.tab_id
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
                self.progress_queue.put({"phase": "error", "message": f"Source does not exist: {self.src}", "tab_id": self.tab_id})
                return
            if self.src.is_file():
                results = self.sync_file()
            elif self.src.is_dir():
                results = self.sync_folder()
            else:
                self.progress_queue.put({"phase": "error", "message": f"Invalid source path: {self.src}", "tab_id": self.tab_id})
                return
            if self.stop_event.is_set():
                self.progress_queue.put({"phase": "cancelled", "tab_id": self.tab_id})
            else:
                if any(r[0] == "error" for r in results):
                    self.progress_queue.put({"phase": "error", "message": "Some files failed; check console", "tab_id": self.tab_id})
                else:
                    self.progress_queue.put({"phase": "done", "tab_id": self.tab_id})
        except Exception as e:
            self.progress_queue.put({"phase": "error", "message": str(e), "tab_id": self.tab_id})

class GDriveTransfer(threading.Thread):
    def __init__(self, src, progress_queue, stop_event, workers, tab_id, gdrive_service):
        super().__init__(daemon=True)
        self.src = Path(src).resolve()
        self.progress_queue = progress_queue
        self.stop_event = stop_event
        self.workers = workers
        self.tab_id = tab_id
        self.gdrive_service = gdrive_service
        self.folder_link = ""

    def run(self):
        if not self.src.is_dir():
            self.progress_queue.put({"phase": "error", "message": "Source must be a folder for Google Drive upload.", "tab_id": self.tab_id})
            return

        try:
            self.progress_queue.put({"phase": "status", "message": "Finding destination folder on Drive...", "tab_id": self.tab_id})
            folder_name = self.src.name
            gdrive_folder = None
            query = f"title='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            file_list = self.gdrive_service.ListFile({'q': query}).GetList()

            if file_list:
                gdrive_folder = file_list[0]
                self.progress_queue.put({"phase": "status", "message": f"Found existing folder: '{folder_name}'", "tab_id": self.tab_id})
            else:
                self.progress_queue.put({"phase": "status", "message": f"Creating new folder: '{folder_name}'...", "tab_id": self.tab_id})
                folder_metadata = {'title': folder_name, 'mimeType': 'application/vnd.google-apps.folder'}
                gdrive_folder = self.gdrive_service.CreateFile(folder_metadata)
                gdrive_folder.Upload()

            self.folder_link = f"https://drive.google.com/drive/folders/{gdrive_folder['id']}"

            self.progress_queue.put({"phase": "status", "message": "Checking for existing files...", "tab_id": self.tab_id})
            remote_files = {}
            query = f"'{gdrive_folder['id']}' in parents and trashed=false"
            file_list = self.gdrive_service.ListFile({'q': query}).GetList()
            for file in file_list:
                if 'md5Checksum' in file:
                    remote_files[file['title']] = {
                        'id': file['id'],
                        'size': int(file['fileSize']),
                        'md5': file['md5Checksum']
                    }

            local_files_to_check = []
            for root, _, files in os.walk(self.src):
                for f in files:
                    local_files_to_check.append(Path(root) / f)

            total_files = len(local_files_to_check)
            if total_files == 0:
                self.progress_queue.put({"phase": "done", "message": "No files to upload.", "tab_id": self.tab_id})
                return

            uploaded_count = 0
            skipped_count = 0
            lock = Lock()

            def check_and_upload_file(local_path):
                nonlocal uploaded_count, skipped_count
                if self.stop_event.is_set():
                    return

                try:
                    file_name = local_path.name
                    local_size = local_path.stat().st_size
                    local_md5 = get_file_hash(local_path)

                    if file_name in remote_files:
                        remote_file_info = remote_files[file_name]
                        if local_size == remote_file_info['size'] and local_md5 == remote_file_info['md5']:
                            with lock:
                                skipped_count += 1
                                pct = int(((uploaded_count + skipped_count) / total_files) * 100)
                                self.progress_queue.put({
                                    "phase": "downloading", "percent": pct, "name": f"(Skipped) {file_name}", "tab_id": self.tab_id
                                })
                            return

                    gfile = self.gdrive_service.CreateFile({
                        'title': file_name,
                        'parents': [{'id': gdrive_folder['id']}]
                    })
                    gfile.SetContentFile(str(local_path))
                    gfile.Upload()

                    with lock:
                        uploaded_count += 1
                        pct = int(((uploaded_count + skipped_count) / total_files) * 100)
                        self.progress_queue.put({
                            "phase": "downloading", "percent": pct, "name": file_name, "tab_id": self.tab_id
                        })
                except Exception as e:
                    self.progress_queue.put({"phase": "error", "message": f"Failed to process {local_path.name}: {e}", "tab_id": self.tab_id})

            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = [executor.submit(check_and_upload_file, path) for path in local_files_to_check]
                for future in futures:
                    future.result()

            if self.stop_event.is_set():
                self.progress_queue.put({"phase": "cancelled", "tab_id": self.tab_id})
            else:
                final_message = f"Upload Complete! {uploaded_count} uploaded, {skipped_count} skipped. Folder: {self.folder_link}"
                self.progress_queue.put({"phase": "done", "message": final_message, "tab_id": self.tab_id})

        except Exception as e:
            self.progress_queue.put({"phase": "error", "message": f"An error occurred: {e}", "tab_id": self.tab_id})


class TabState:
    def __init__(self, name):
        self.name = name
        self.src = tk.StringVar(value=str(Path("/home/gostlimoss62/Documents/1A_projects/file_transfer/Folder_A") or Path.cwd()))
        self.dst = tk.StringVar(value=str(Path("/home/gostlimoss62/Documents/1A_projects/file_transfer/Folder_B") or Path.cwd()))
        self.status_label = None
        self.progress_bar = None
        self.start_btn = None
        self.stop_btn = None
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.gdrive_service = None
        self.login_btn = None

class App:
    def __init__(self, root):
        self.root = root
        root.title("File Transferer")
        root.resizable(False, False)

        self.progress_queue = queue.Queue()
        self.tab_states = {}

        self.tab_sizes = {
            "Local Transfer": "450x208",
            "GDrive Transfer": "450x184"
        }

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill='both', expand=True)

        self.create_tab1("Local Transfer")
        self.create_tab2("GDrive Transfer")

        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)

        initial_size_str = self.tab_sizes["Local Transfer"]
        width, height = map(int, initial_size_str.split('x'))
        self.center_window(width, height)

        self.root.after(200, self._process_queue)

    def center_window(self, width, height):
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width / 2) - (width / 2)
        y = (screen_height / 2) - (height / 2)
        self.root.geometry(f'{width}x{height}+{int(x)}+{int(y)}')
        self.window_x = int(x)
        self.window_y = int(y)

    def on_tab_changed(self, event):
        current_tab_frame = self.notebook.select()
        tab_name = self.notebook.tab(current_tab_frame, "text")
        new_size = self.tab_sizes.get(tab_name)
        if new_size:
            new_geometry = f"{new_size}+{self.window_x}+{self.window_y}"
            self.root.geometry(new_geometry)

    def create_tab1(self, tab_name):
        frame = ttk.Frame(self.notebook, padding=0)
        self.notebook.add(frame, text=tab_name)

        state = TabState(tab_name)
        self.tab_states[tab_name] = state

        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, weight=0)

        # Source folder select
        ttk.Label(frame, text="Entry folder:").grid(row=0, column=0, sticky='w')
        ttk.Button(frame, text="Browse...", command=lambda: self.browse_src(state)).grid(row=0, column=2, sticky='we')
        src_entry = ttk.Entry(frame, textvariable=state.src)
        src_entry.grid(row=1, column=0, columnspan=3, sticky='we')

        # Output folder select
        ttk.Label(frame, text="Output folder:").grid(row=2, column=0, sticky='w')
        ttk.Button(frame, text="Browse...", command=lambda: self.browse_dst(state)).grid(row=2, column=2, sticky='we')
        dst_entry = ttk.Entry(frame, textvariable=state.dst)
        dst_entry.grid(row=3, column=0, columnspan=3, sticky='we')

        # Status progress
        status_frame = ttk.Frame(frame)
        status_frame.grid(row=4, column=0, columnspan=3, sticky='we')
        status_frame.columnconfigure(0, weight=1)

        state.status_label = ttk.Label(status_frame, text="Idle")
        state.status_label.grid(row=0, column=0, sticky='w')

        # Progressbar
        state.progress_bar = ttk.Progressbar(frame, orient='horizontal', mode='determinate')
        state.progress_bar.grid(row=5, column=0, columnspan=3, sticky='we')

        # Start cancel buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=6, column=0, columnspan=3, sticky='we')
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)

        state.start_btn = ttk.Button(btn_frame, text="Start Transfer", command=lambda: self.start_transfer(state))
        state.start_btn.grid(row=0, column=0, sticky='we')
        state.stop_btn = ttk.Button(btn_frame, text="Cancel", command=lambda: self.cancel_transfer(state), state='disabled')
        state.stop_btn.grid(row=0, column=1, sticky='we')

    def create_tab2(self, tab_name):
        frame = ttk.Frame(self.notebook, padding=0)
        self.notebook.add(frame, text=tab_name)

        state = TabState(tab_name)
        self.tab_states[tab_name] = state

        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, weight=0)

        # Source folder select
        ttk.Label(frame, text="Upload folder:").grid(row=0, column=0, sticky='w')
        ttk.Button(frame, text="Browse...", command=lambda: self.browse_src(state)).grid(row=0, column=2, sticky='we')
        src_entry = ttk.Entry(frame, textvariable=state.src)
        src_entry.grid(row=1, column=0, columnspan=3, sticky='we')

        # Login Button
        state.login_btn = ttk.Button(frame, text="Google Login", command=lambda: self.Glogin(state))
        state.login_btn.grid(row=2, column=0, columnspan=3, sticky='we')

        # Status progress
        status_frame = ttk.Frame(frame)
        status_frame.grid(row=3, column=0, columnspan=3, sticky='we')
        status_frame.columnconfigure(0, weight=1)

        state.status_label = ttk.Label(status_frame, text="Please log in to Google Drive.")
        state.status_label.grid(row=0, column=0, sticky='w')

        # Progress bar
        state.progress_bar = ttk.Progressbar(frame, orient='horizontal', mode='determinate')
        state.progress_bar.grid(row=4, column=0, columnspan=3, sticky='we')

        # Start cancel buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=5, column=0, columnspan=3, sticky='we')
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)

        state.start_btn = ttk.Button(btn_frame, text="Start Transfer", command=lambda: self.start_transfer(state), state='disabled')
        state.start_btn.grid(row=0, column=0, sticky='we')
        state.stop_btn = ttk.Button(btn_frame, text="Cancel", command=lambda: self.cancel_transfer(state), state='disabled')
        state.stop_btn.grid(row=0, column=1, sticky='we')

    def _perform_login(self, state: TabState):
        try:
            gdrive_service = get_drive()
            state.gdrive_service = gdrive_service
            self.progress_queue.put({"phase": "login_success", "tab_id": state.name})
        except Exception as e:
            self.progress_queue.put({"phase": "login_error", "message": str(e), "tab_id": state.name})

    def Glogin(self, state: TabState):
        state.login_btn.config(text="Logging in...", state='disabled')
        state.status_label.config(text="Opening browser for authentication...")
        self.root.update_idletasks()
        login_thread = threading.Thread(target=self._perform_login, args=(state,), daemon=True)
        login_thread.start()

    def browse_src(self, state: TabState):
        choice = messagebox.askyesno("Select Source", "Select a folder?\n(No for a file)", parent=self.root)
        if choice:
            d = filedialog.askdirectory(initialdir=state.src.get())
            if d:
                state.src.set(d)
        else:
            f = filedialog.askopenfilename(initialdir=state.src.get())
            if f:
                state.src.set(f)

    def browse_dst(self, state: TabState):
        d = filedialog.askdirectory(initialdir=state.dst.get())
        if d:
            state.dst.set(d)

    def start_transfer(self, state: TabState):
        src = state.src.get().strip()
        if not src or not Path(src).exists():
            messagebox.showwarning("Missing Path", "Please select a valid source folder.", parent=self.root)
            return

        state.progress_bar['value'] = 0
        state.status_label.config(text="Starting...")
        state.start_btn.config(state='disabled')
        state.stop_btn.config(state='normal')
        state.stop_event.clear()

        if state.name == "GDrive Transfer":
            if not state.gdrive_service:
                messagebox.showerror("Not Logged In", "Please log in to Google Drive first.", parent=self.root)
                state.start_btn.config(state='normal')
                state.stop_btn.config(state='disabled')
                return
            state.worker_thread = GDriveTransfer(src, self.progress_queue, state.stop_event, WORKERS, state.name, state.gdrive_service)
        else:
            dst = state.dst.get().strip()
            if not dst:
                messagebox.showwarning("Missing Path", "Please select a destination.", parent=self.root)
                state.start_btn.config(state='normal')
                state.stop_btn.config(state='disabled')
                return
            state.worker_thread = LocalTransfer(src, dst, self.progress_queue, state.stop_event, WORKERS, state.name)

        state.worker_thread.start()

    def cancel_transfer(self, state: TabState):
        if not state.worker_thread:
            return
        state.stop_event.set()
        state.status_label.config(text="Cancelling...")
        state.stop_btn.config(state='disabled')

    def _process_queue(self):
        try:
            while True:
                msg = self.progress_queue.get_nowait()
                tab_id = msg.get('tab_id')
                if not tab_id or tab_id not in self.tab_states:
                    continue

                state = self.tab_states[tab_id]
                phase = msg.get('phase')

                if phase == 'downloading':
                    pct = msg.get('percent', 0)
                    name = msg.get('name', '')
                    state.progress_bar['value'] = pct
                    state.status_label.config(text=f"Uploading ({pct}%): {name}")
                elif phase == 'done':
                    message = msg.get('message', 'Finished')
                    state.status_label.config(text=message)
                    state.progress_bar['value'] = 100
                    state.start_btn.config(state='normal')
                    state.stop_btn.config(state='disabled')
                    state.worker_thread = None
                elif phase == 'status':
                    message = msg.get('message', 'Idle')
                    state.status_label.config(text=message)
                elif phase == 'cancelled':
                    state.status_label.config(text="Cancelled by user")
                    state.start_btn.config(state='normal')
                    state.stop_btn.config(state='disabled')
                    state.worker_thread = None
                elif phase == 'login_success':
                    state.status_label.config(text="Successfully logged in!")
                    state.login_btn.config(text="Logged In", state='disabled')
                    state.start_btn.config(state='normal')
                elif phase == 'login_error':
                    msgtxt = msg.get('message', 'Unknown login error')
                    state.status_label.config(text=f"Login Failed: {msgtxt}")
                    state.login_btn.config(text="Google Login", state='normal')
                    messagebox.showerror("Login Error", f"Failed to log in to Google Drive:\n{msgtxt}", parent=self.root)
                elif phase == 'error':
                    msgtxt = msg.get('message', 'Unknown error')
                    state.status_label.config(text=f"Error: {msgtxt}")
                    messagebox.showerror("Transfer Error", f"An error occurred in '{state.name}':\n{msgtxt}", parent=self.root)
                    state.start_btn.config(state='normal')
                    state.stop_btn.config(state='disabled')
                    state.worker_thread = None
        except queue.Empty:
            pass
        finally:
            is_any_worker_active = any(s.worker_thread and s.worker_thread.is_alive() for s in self.tab_states.values())
            delay = 50 if is_any_worker_active else 1000
            self.root.after(delay, self._process_queue)

def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
