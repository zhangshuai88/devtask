"""Android 工程本地研发任务的图形界面。

双击 devtask-gui.cmd 或 `python devtask_gui.py [工程目录]` 启动。
具体执行逻辑全部复用 devtask.py 中的同名函数，界面只负责编排与展示。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, str(Path(__file__).resolve().parent))

import devtask  # noqa: E402

CONFIG_PATH = devtask.TOOL_DIR / "devtask.config.json"
# 「功能说明」弹窗的内容：分组 → (界面上的名称, 作用)。新增控件时在这里补一行即可
HELP_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("顶部", [
        ("选择工程…", "切换目标 Android 工程并写回 devtask.config.json，下次启动沿用；任务或日志流运行中不能切换。"),
        ("功能说明", "查看当前窗口里每个按钮、选项和状态的含义，也就是本窗口。"),
    ]),
    ("环境", [
        ("设备", "当前连接的车机列表（取 adb devices）；多台设备时在这里选一台作为操作目标，每 5 秒自动刷新。"),
        ("刷新", "立即重新读取 adb devices，不用等 5 秒的自动刷新。"),
        ("产物", "当前构建类型（debug / release）的 APK 文件名、大小与上次生成时间；还没编译过时显示「尚未构建」。"),
        ("已安装", "车机上已安装的本应用 versionName，用来和刚构建的版本对照；未安装时显示「未安装」。"),
    ]),
    ("选项", [
        ("构建类型 debug", "编译 debug 包：可调试、不混淆，日常开发用这个。"),
        ("构建类型 release", "编译 release 包：R8 混淆并签名；「提交前门禁」也按这里选的类型运行。"),
        ("安装前先卸载（清除旧数据）", "勾选后先卸载旧包再安装，应用数据、登录态与缓存都会被清空。"),
        ("安装后自动启动 HomeActivity", "编译安装完成后自动拉起主界面；取消勾选则只安装不启动。"),
    ]),
    ("操作", [
        ("编译 + 安装 + 启动", "编译所选构建类型，安装到当前选中的设备并按上面的选项启动主界面。"),
        ("只编译", "只编译 APK 输出到构建目录，不连接设备；没有车机时也能验证代码能否编译通过。"),
        ("提交前门禁", "编译后运行 koverVerify 与 detekt，检查覆盖率要求和代码规范；提交前先过这一步。"),
        ("卸载应用", "从当前设备卸载本应用，会清除其全部数据，执行前会再确认一次。"),
        ("查看应用日志", "实时显示本应用进程的 logcat 并按配置的 TAG 过滤；应用中按钮变为「停止日志」，应用未启动时需要先启动应用。"),
        ("抓取全部日志", "从点击时刻起抓取整台设备的全部日志写入文件，不过滤进程和 TAG；应用中按钮变为「停止抓取」，停止后自动打开日志目录。"),
        ("打开 APK 目录", "在资源管理器中打开当前构建类型 APK 所在的目录；还没构建过则打开工程根目录。"),
        ("打开日志目录", "在资源管理器中打开全量日志的输出目录（本工具目录下的 build/logs）。"),
        ("中止当前任务", "仅在编译/安装任务运行时出现：强制结束当前任务及其派生的 gradle 进程，不影响看板本身。"),
        ("清空", "清空输出区内容；不影响正在运行的任务和已写入文件的日志。"),
    ]),
]
POLL_INTERVAL_MS = 300
DEVICE_REFRESH_MS = 5000
INSTALLED_REFRESH_MS = 2000
LOG_MAX_CHARS = 2_000_000
MAX_EVENTS_PER_DRAIN = 2000
# 打包后没有 devtask.py 可执行：复用同一个 exe，用 --cli 前缀转入命令行模式
DEVTASK_CMD = [sys.executable, "--cli"] if devtask.FROZEN else [sys.executable, "-u", str(Path(devtask.__file__))]
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_VERSION_NAME_RE = re.compile(r"versionName=(\S+)")


class DevtaskApp(tk.Tk):
    def __init__(self, project: Path | None = None) -> None:
        super().__init__()
        if project is not None:
            devtask.set_project(project)
        self.title(f"devtask · {devtask.PROJECT_ROOT.name}")
        # 小屏笔记本上按可用高度收缩，避免窗口超出屏幕
        width, height = 1240, 820
        height = min(height, self.winfo_screenheight() - 110)
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2 - 40)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(1000, 640)

        self.events: queue.Queue[tuple] = queue.Queue()
        self.running = False
        self.devices: list[str] = []
        self._preferred_serial: str | None = None
        self.logcat_thread: threading.Thread | None = None
        self.stream_stop = threading.Event()
        self.grab_thread: threading.Thread | None = None
        self.grab_path: Path | None = None
        self.grab_lines = 0
        self._grab_file = None
        self._log_chars = 0
        self._last_trim = 0.0
        self._last_grab_sample = 0.0
        self.stopping = False
        self._installed_seq = 0
        self._installed_text = ""
        self.status = ""
        self._help_window: tk.Toplevel | None = None

        self._init_style()
        self._build_layout()
        self.after(POLL_INTERVAL_MS, self._drain_events)
        self.refresh_all()

    # ---------- 界面搭建 ----------

    def _init_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(10, 6))
        style.configure("Action.TButton", padding=(10, 6))

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(12, 10, 12, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(2, weight=1)
        ttk.Label(header, text="devtask", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.title_var = tk.StringVar(value=str(devtask.PROJECT_ROOT))
        ttk.Label(header, textvariable=self.title_var, style="Hint.TLabel").grid(
            row=0, column=1, sticky="w", padx=(10, 6)
        )
        pick_btn = ttk.Button(header, text="选择工程…", command=self.on_pick_project)
        pick_btn.grid(row=0, column=2, sticky="w")
        ttk.Button(header, text="功能说明", command=self.on_show_help).grid(row=0, column=3, sticky="e")

        body = ttk.Frame(self, padding=(12, 0, 12, 12))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        side = ttk.Frame(body, width=380)
        side.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        side.grid_propagate(False)
        self._build_status_card(side, 0)
        self._build_options_card(side, 1)
        self._build_actions_card(side, 2)

        self._build_log_panel(body, 1)

    # ---------- 功能说明 ----------

    def on_show_help(self) -> None:
        if self._help_window is not None and self._help_window.winfo_exists():
            # 已经打开就提到前面，避免叠出多个相同窗口
            self._help_window.deiconify()
            self._help_window.lift()
            self._help_window.focus_force()
            return
        window = tk.Toplevel(self)
        self._help_window = window
        window.title("devtask · 功能说明")
        window.transient(self)
        width, height = 900, 660
        x = self.winfo_rootx() + (self.winfo_width() - width) // 2
        y = self.winfo_rooty() + (self.winfo_height() - height) // 2 - 20
        window.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")

        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(
            frame,
            text="窗口中每个按钮、选项与状态的含义；动作类按钮都在任务执行完成后给出结果，详情见右侧输出区。",
            style="Hint.TLabel",
            wraplength=840,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        tree = ttk.Treeview(
            frame, columns=("section", "name", "desc"), show="headings", selectmode="none"
        )
        tree.heading("section", text="分组")
        tree.heading("name", text="名称")
        tree.heading("desc", text="作用")
        tree.column("section", width=56, stretch=False, anchor="w")
        tree.column("name", width=200, stretch=False, anchor="w")
        tree.column("desc", width=560, anchor="w")
        for section, rows in HELP_SECTIONS:
            tree.insert("", "end", values=(section, rows[0][0], rows[0][1]), tags=("section",))
            for name, desc in rows[1:]:
                tree.insert("", "end", values=("", name, desc))
        tree.tag_configure("section", font=("Microsoft YaHei UI", 9, "bold"), background="#f0f0f0")
        tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)

        ttk.Button(frame, text="关闭", command=window.destroy).grid(
            row=2, column=0, columnspan=2, sticky="e", pady=(8, 0)
        )
        window.bind("<Escape>", lambda _e: window.destroy())
        window.protocol("WM_DELETE_WINDOW", window.destroy)

    def on_pick_project(self) -> None:
        if self.running or self.logcat_thread or self.grab_thread:
            messagebox.showinfo("请先停止", "有任务或日志流正在进行，结束后再切换工程。")
            return
        chosen = filedialog.askdirectory(title="选择 Android 工程根目录", initialdir=str(devtask.PROJECT_ROOT))
        if not chosen:
            return
        try:
            devtask.set_project(chosen, quiet=True)
        except devtask.TaskError as e:
            messagebox.showerror("工程无效", str(e))
            return
        try:
            CONFIG_PATH.write_text(
                json.dumps({"project": str(devtask.PROJECT_ROOT)}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            self.append_log(f"[devtask] 保存配置失败（本次仍生效）：{e}\n", "error")
        self.title(f"devtask · {devtask.PROJECT_ROOT.name}")
        self.title_var.set(str(devtask.PROJECT_ROOT))
        self._installed_text = ""
        self.refresh_all()

    def _build_status_card(self, parent: ttk.Frame, row: int) -> None:
        card = ttk.LabelFrame(parent, text="环境", padding=10)
        card.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text="设备").grid(row=0, column=0, sticky="w")
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(card, textvariable=self.device_var, state="readonly", width=24)
        self.device_combo.grid(row=0, column=1, sticky="ew", padx=(8, 6))
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)
        ttk.Button(card, text="刷新", width=6, command=self.refresh_devices).grid(row=0, column=2)

        ttk.Label(card, text="产物").grid(row=1, column=0, sticky="nw", pady=(8, 0))
        self.apk_var = tk.StringVar(value="尚未构建")
        ttk.Label(card, textvariable=self.apk_var, style="Hint.TLabel", wraplength=210, justify="left").grid(
            row=1, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        ttk.Label(card, text="已安装").grid(row=2, column=0, sticky="nw", pady=(8, 0))
        self.installed_var = tk.StringVar(value="—")
        ttk.Label(card, textvariable=self.installed_var, style="Hint.TLabel", wraplength=210, justify="left").grid(
            row=2, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(8, 0)
        )

    def _build_options_card(self, parent: ttk.Frame, row: int) -> None:
        card = ttk.LabelFrame(parent, text="选项", padding=10)
        card.grid(row=row, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(card, text="构建类型").grid(row=0, column=0, sticky="w")
        self.build_type_var = tk.StringVar(value="debug")
        types = ttk.Frame(card)
        types.grid(row=0, column=1, sticky="w", padx=(8, 0))
        for name in ("debug", "release"):
            ttk.Radiobutton(types, text=name, value=name, variable=self.build_type_var).pack(side="left", padx=(0, 12))

        self.fresh_var = tk.BooleanVar()
        self.launch_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(card, text="安装前先卸载（清除旧数据）", variable=self.fresh_var).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        ttk.Checkbutton(card, text="安装后自动启动 HomeActivity", variable=self.launch_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

    def _build_actions_card(self, parent: ttk.Frame, row: int) -> None:
        card = ttk.LabelFrame(parent, text="操作", padding=10)
        card.grid(row=row, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)

        self.action_buttons: list[ttk.Button] = []
        layout = [
            (0, 0, "编译 + 安装 + 启动", self.on_apk, "Primary.TButton"),
            (0, 1, "只编译", self.on_build, "Action.TButton"),
            (1, 0, "提交前门禁", self.on_check, "Action.TButton"),
            (1, 1, "卸载应用", self.on_uninstall, "Action.TButton"),
        ]
        for r, c, text, command, style in layout:
            btn = ttk.Button(card, text=text, command=command, style=style)
            btn.grid(row=r, column=c, sticky="ew", padx=(0 if c == 0 else 4, 0), pady=(0 if r == 0 else 6, 0))
            self.action_buttons.append(btn)

        tail = ttk.Frame(card)
        tail.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        tail.columnconfigure(0, weight=1)
        tail.columnconfigure(1, weight=1)
        self.logcat_button = ttk.Button(tail, text="查看应用日志", command=self.on_logcat, style="Action.TButton")
        self.logcat_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.grab_button = ttk.Button(tail, text="抓取全部日志", command=self.on_grab, style="Action.TButton")
        self.grab_button.grid(row=0, column=1, sticky="ew")

        tail2 = ttk.Frame(card)
        tail2.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        tail2.columnconfigure(0, weight=1)
        tail2.columnconfigure(1, weight=1)
        self.open_dir_button = ttk.Button(
            tail2, text="打开 APK 目录", command=self.on_open_apk_dir, style="Action.TButton"
        )
        self.open_dir_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.open_log_dir_button = ttk.Button(
            tail2, text="打开日志目录", command=self.on_open_log_dir, style="Action.TButton"
        )
        self.open_log_dir_button.grid(row=0, column=1, sticky="ew")

        self.stop_button = ttk.Button(card, text="中止当前任务", command=self.on_stop)
        # 仅在任务运行时显示，空闲时不占位置
        self.stop_button.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.stop_button.grid_remove()

    def _build_log_panel(self, parent: ttk.Frame, column: int) -> None:
        # 用 LabelFrame 标题展示当前状态（"输出 · 编译中"），不再另设状态栏
        self.log_panel = ttk.LabelFrame(parent, text="输出", padding=6)
        # 侧栏占第 0 列，日志区必须落在第 1 列，否则两者叠在同一格把侧栏盖住
        self.log_panel.grid(row=0, column=column, sticky="nsew")
        self.log_panel.columnconfigure(0, weight=1)
        self.log_panel.rowconfigure(0, weight=1)
        panel = self.log_panel

        self.log_text = ScrolledText(
            panel,
            # 长行（堆栈、主题配置 dump）必须折行，否则只能横向滚动、看不到行尾
            wrap="char",
            font=("Consolas", 9),
            background="#1e1e1e",
            foreground="#d4d4d4",
            insertbackground="#d4d4d4",
            borderwidth=0,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("command", foreground="#569cd6")
        self.log_text.tag_configure("success", foreground="#4ec9b0")
        self.log_text.tag_configure("error", foreground="#f48771")
        self.log_text.tag_configure("info", foreground="#dcdcaa")
        self.log_text.configure(state="disabled")

        toolbar = ttk.Frame(panel)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(toolbar, text="清空", command=self.clear_log).pack(side="left")

    def _set_status(self, text: str) -> None:
        self.status = text
        self.log_panel.configure(text=f"输出 · {text}" if text else "输出")

    # ---------- 日志 ----------

    def append_log(self, text: str, tag: str | None = None) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text, tag or ())
        # 自动折行后行数不再是衡量体积的好指标，按字符总量裁剪并限频，避免全量日志拖死界面
        self._log_chars += len(text)
        now = time.monotonic()
        if self._log_chars > LOG_MAX_CHARS and now - self._last_trim > 1.0:
            self._last_trim = now
            self._trim_log()
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _trim_log(self) -> None:
        keep = "".join(self.log_text.get("1.0", "end-1c")[-LOG_MAX_CHARS // 2:])
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", keep)
        self._log_chars = len(keep)

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._log_chars = 0

    # ---------- 环境状态 ----------

    def selected_serial(self) -> str | None:
        text = self.device_var.get().strip()
        return text if text in self.devices else None

    def _on_device_selected(self, _event) -> None:
        self._preferred_serial = self.selected_serial()
        self.refresh_installed()

    def refresh_all(self) -> None:
        self.refresh_devices()
        self.refresh_apk()
        self.refresh_installed()
        self.schedule_device_refresh()
        self.schedule_installed_refresh()

    def schedule_device_refresh(self) -> None:
        self.after(DEVICE_REFRESH_MS, self._auto_refresh_devices)

    def _auto_refresh_devices(self) -> None:
        if not self.running:
            self.refresh_devices()
        self.schedule_device_refresh()

    def schedule_installed_refresh(self) -> None:
        self.after(INSTALLED_REFRESH_MS, self._auto_refresh_installed)

    def _auto_refresh_installed(self) -> None:
        # 安装结果与设备连接无关，独立轮询；查询只耗时几十毫秒，结果变化时才更新界面
        if not self.running:
            self.refresh_installed(background=True)
        self.schedule_installed_refresh()

    def refresh_devices(self) -> None:
        def work() -> None:
            try:
                devices = devtask.connected_devices(devtask.find_adb())
            except devtask.TaskError:
                devices = []
            self.events.put(("devices", devices))

        threading.Thread(target=work, daemon=True).start()

    def _apply_devices(self, devices: list[str]) -> None:
        self.devices = devices
        if not devices:
            self.device_combo["values"] = []
            self.device_var.set("未连接（adb devices 为空）")
            self.device_combo.configure(state="disabled")
            return
        self.device_combo.configure(state="readonly")
        self.device_combo["values"] = devices
        if self.device_var.get() not in devices:
            preferred = self._preferred_serial
            self.device_var.set(preferred if preferred in devices else devices[0])

    def refresh_apk(self) -> None:
        apk = devtask.apk_path(self.build_type_var.get())
        if apk.exists():
            st = apk.stat()
            size_mb = st.st_size / 1024 / 1024
            mtime = time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime))
            self.apk_var.set(f"{apk.name} · {size_mb:.1f} MB · {mtime}")
        else:
            self.apk_var.set("尚未构建")

    def refresh_installed(self, background: bool = False) -> None:
        """查询车机上已安装的版本。

        `background=True` 用于定时轮询：界面文字不变，仅在结果变化时更新，
        否则每轮都会短暂显示"查询中"，看上去不停闪烁。
        """
        serial = self.selected_serial()
        if not serial:
            self._installed_text = "—"
            self.installed_var.set("—")
            return
        if not background:
            self._installed_text = "查询中…"
            self.installed_var.set("查询中…")
        self._installed_seq += 1
        seq = self._installed_seq

        def work() -> None:
            try:
                adb = devtask.find_adb()
                _, out = devtask.capture([adb, "-s", serial, "shell", "dumpsys", "package", devtask.APPLICATION_ID])
            except devtask.TaskError as e:
                out = f"error: {e}"
            version = _VERSION_NAME_RE.search(out)
            text = f"versionName {version.group(1)}" if version else "未安装"
            self.events.put(("installed", (seq, text)))

        threading.Thread(target=work, daemon=True).start()

    # ---------- 任务执行 ----------

    def _start_task(self, cmd: list[str], label: str) -> None:
        if self.running:
            messagebox.showinfo("任务进行中", "请等待当前任务结束或点击「中止当前任务」。")
            return
        self.running = True
        self.log_text_label = label
        self._set_status(f"{label}中")
        self.append_log(f"\n===== {label} @ {time.strftime('%H:%M:%S')} =====\n", "info")
        self.append_log("$ " + " ".join(cmd) + "\n", "command")

        for btn in self.action_buttons:
            btn.configure(state="disabled")
        self.stop_button.grid()

        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(devtask.PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                # 新建进程组使「中止」能只结束任务；NO_WINDOW 防止 pythonw 下弹出控制台
                creationflags=CREATE_NEW_PROCESS_GROUP | devtask.NO_WINDOW,
            )
        except OSError as e:
            self._finish_task(1, label, note=f"无法启动进程：{e}")
            return
        self.proc = proc
        threading.Thread(target=self._pump_output, args=(proc,), daemon=True).start()

    def _pump_output(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        pending = b""
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            pending += chunk
            # gradle 进度行以 \r 刷新同一行，按 \r 也切分才能实时看到
            while True:
                idx = min([i for i in (pending.find(b"\n"), pending.find(b"\r")) if i >= 0], default=-1)
                if idx < 0:
                    break
                self.events.put(("line", pending[:idx].decode("utf-8", errors="replace") + "\n"))
                pending = pending[idx + 1:]
        if pending:
            self.events.put(("line", pending.decode("utf-8", errors="replace") + "\n"))
        proc.stdout.close()
        self.events.put(("finished", proc.wait()))

    def _finish_task(self, code: int, label: str, note: str | None = None) -> None:
        self.running = False
        self.stop_button.grid_remove()
        for btn in self.action_buttons:
            btn.configure(state="normal")
        if code == 0:
            status = f"{label} · 完成"
            self.append_log(f"===== {label} 成功 =====\n", "success")
        elif code == 130:
            status = f"{label} · 已中止"
            self.append_log(f"===== {label} 已中止 =====\n", "error")
        else:
            status = f"{label} · 失败（退出码 {code}）"
            suffix = f"：{note}" if note else ""
            self.append_log(f"===== {label} 失败（退出码 {code}）{suffix} =====\n", "error")
        if self.logcat_thread:
            status = f"{status} · 日志监听中"
        self._set_status(status)
        self.refresh_apk()
        # 安装后版本可能刚变化，后台刷新（不显示"查询中"）
        self.refresh_installed(background=True)

    def _drain_events(self) -> None:
        try:
            # 全量抓取时事件可能远超界面处理速度，限量处理并把未处理完的留到下一轮
            for _ in range(MAX_EVENTS_PER_DRAIN):
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "line":
                    self.append_log(event[1])
                elif kind == "finished":
                    self._finish_task(event[1], self.log_text_label)
                elif kind == "devices":
                    self._apply_devices(event[1])
                elif kind == "installed":
                    seq, text = event[1]
                    # 慢查询可能晚于后来的请求返回，丢弃过期结果避免版本号来回跳
                    if seq == self._installed_seq and text != self._installed_text:
                        self._installed_text = text
                        self.installed_var.set(text)
                elif kind == "logcat":
                    self.append_log(event[1])
                elif kind == "logcat_stopped":
                    # 停止中即为预期结束；否则是 adb 断开，同样需要恢复界面
                    self._finalize_stream("logcat")
                elif kind == "logcat_error":
                    self.append_log(event[1], "error")
                elif kind == "grab_progress":
                    count, sample = event[1]
                    self._set_status(f"全量日志抓取中 · {count} 行")
                    if time.monotonic() - self._last_grab_sample > 1:
                        self._last_grab_sample = time.monotonic()
                        self.append_log(sample)
                elif kind == "grab_stopped":
                    self._finalize_stream("grab")
                elif kind == "grab_error":
                    self.append_log(event[1], "error")
        except queue.Empty:
            pass
        self.after(POLL_INTERVAL_MS, self._drain_events)

    # ---------- 按钮回调 ----------

    def _require_device(self) -> str | None:
        serial = self.selected_serial()
        if not serial:
            messagebox.showwarning("未连接设备", "请先连接车机并在 adb devices 中显示为 device，然后点「刷新」。")
        return serial

    def on_apk(self) -> None:
        serial = self._require_device()
        if not serial:
            return
        cmd = [
            *DEVTASK_CMD,
            "apk",
            "--build-type",
            self.build_type_var.get(),
            "--device",
            serial,
        ]
        if self.fresh_var.get():
            cmd.append("--fresh")
        if not self.launch_var.get():
            cmd.append("--no-launch")
        self._start_task(cmd, "编译 + 安装 + 启动")

    def on_build(self) -> None:
        cmd = [*DEVTASK_CMD, "build", "--build-type", self.build_type_var.get()]
        self._start_task(cmd, "只编译")

    def on_check(self) -> None:
        cmd = [*DEVTASK_CMD, "check", "--build-type", self.build_type_var.get()]
        self._start_task(cmd, "提交前门禁")

    def on_uninstall(self) -> None:
        serial = self._require_device()
        if not serial:
            return
        if not messagebox.askyesno("确认卸载", f"从 {serial} 卸载 {devtask.APPLICATION_ID}？"):
            return
        cmd = [*DEVTASK_CMD, "uninstall", "--device", serial]
        self._start_task(cmd, "卸载应用")

    def on_logcat(self) -> None:
        if self.grab_thread:
            self._stop_grab()
            return
        if self.logcat_thread:
            self._stop_stream()
            return
        serial = self._require_device()
        if not serial:
            return
        try:
            adb = devtask.find_adb()
            pid = devtask.app_pid(adb, serial)
        except devtask.TaskError as e:
            messagebox.showerror("日志", str(e))
            return
        # 没有 pid 就没法只看本应用，引导用户先启动应用
        if not pid:
            messagebox.showwarning("应用未运行", "应用进程未运行，请先编译安装并启动应用，再查看日志。")
            return

        self.append_log(f"\n===== 应用日志 · {serial} =====\n", "info")
        self.logcat_thread = self._start_stream(
            "logcat", serial, [devtask.DEFAULT_LOG_TAG], pid, "日志监听中", tail=200
        )
        if self.logcat_thread is None:
            return
        self.logcat_button.configure(text="停止日志")

    def on_grab(self) -> None:
        if self.logcat_thread:
            self._stop_stream()
            return
        if self.grab_thread:
            self._stop_grab()
            return
        serial = self._require_device()
        if not serial:
            return
        try:
            adb = devtask.find_adb()
        except devtask.TaskError as e:
            messagebox.showerror("日志", str(e))
            return

        self.grab_path = devtask.log_output_path()
        self.grab_path.parent.mkdir(parents=True, exist_ok=True)
        self.grab_lines = 0
        self.append_log(f"\n===== 全量日志抓取 · {serial} =====\n", "info")
        self.append_log(f"保存到 {self.grab_path}\n", "info")
        self.grab_button.configure(text="停止抓取")
        self.grab_thread = self._start_stream("grab", serial, None, None, "全量日志抓取中")
        if self.grab_thread is None:
            self.grab_button.configure(text="抓取全部日志")

    def _start_stream(
        self, kind: str, serial: str, tags: list[str] | None, pid: str | None, hint: str, tail: int | None = None
    ) -> threading.Thread | None:
        """日志流（单进程过滤 / 全量抓取）共用一个后台线程 + 停止开关。"""
        try:
            adb = devtask.find_adb()
        except devtask.TaskError as e:
            messagebox.showerror("日志", str(e))
            return None
        self.stream_stop = threading.Event()
        self.stream_proc = None
        self.stopping = False
        self._set_status(hint)
        if kind == "grab":
            self._grab_file = open(self.grab_path, "w", encoding="utf-8", newline="\n") if self.grab_path else None

        def work() -> None:
            def on_line(line: str) -> None:
                if self.stream_stop.is_set():
                    raise devtask.StopStreaming
                if kind == "grab" and self.grab_path:
                    self._grab_write(line)
                else:
                    self.events.put((kind, line))

            try:
                devtask.stream_logcat(
                    adb,
                    serial,
                    tags,
                    pid,
                    on_line,
                    since_now=kind == "grab",
                    tail=tail,
                    on_start=self._set_stream_proc,
                    watch_app=kind == "logcat",
                    on_line_stopped=self.stream_stop.is_set,
                )
            except Exception as e:  # adb 被拔出等异常，提示后回到空闲
                self.events.put((f"{kind}_error", f"[devtask] 日志中断：{e}\n"))
            finally:
                if not self.stream_stop.is_set():
                    self.events.put((f"{kind}_stopped",))

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread

    def _set_stream_proc(self, proc: subprocess.Popen) -> None:
        self.stream_proc = proc

    def _grab_write(self, line: str) -> None:
        # 全量日志每秒可达数千行，写文件而不进 UI 队列；界面只展示统计与抽样
        f = self._grab_file
        if f is None:
            return
        f.write(line)
        f.flush()
        self.grab_lines += 1
        if self.grab_lines % 500 == 0:
            self.events.put(("grab_progress", (self.grab_lines, line)))

    def _stop_stream(self) -> None:
        self._request_stop("logcat")

    def _stop_grab(self) -> None:
        self._request_stop("grab")

    def _request_stop(self, kind: str) -> None:
        if self.stopping:
            return
        self.stopping = True
        self._set_status("正在停止…")
        thread = self.logcat_thread if kind == "logcat" else self.grab_thread
        proc = self.stream_proc

        def work() -> None:
            # 先置标志让读取循环跳出，再兜底结束进程；两者都在后台做，避免界面卡顿
            self.stream_stop.set()
            if thread:
                thread.join(timeout=3)
            if proc is not None:
                devtask.terminate_tree(proc)
            # 结束事件以这里为准：工作线程可能因读端被关闭而走不到 finally
            self.events.put((f"{kind}_stopped",))

        threading.Thread(target=work, daemon=True).start()

    def _finalize_stream(self, kind: str) -> None:
        # 在线程自行退出后调用（不可 join：线程可能正阻塞在事件队列上，join 会死锁）
        self.stopping = False
        if kind == "logcat":
            self.logcat_thread = None
            self.logcat_button.configure(text="查看应用日志")
            self._set_status("日志已停止")
            self.append_log("===== 应用日志已停止 =====\n", "info")
            return
        self.grab_thread = None
        if self._grab_file:
            self._grab_file.close()
            self._grab_file = None
        self.grab_button.configure(text="抓取全部日志")
        self._set_status("日志已停止")
        self.append_log(f"===== 抓取结束：{self.grab_path}（{self.grab_lines} 行）=====\n", "success")
        # 抓取结束即打开所在文件夹，省去再点「打开日志目录」；资源管理器不可用不影响日志落盘
        if self.grab_path and self.grab_path.exists():
            try:
                os.startfile(str(self.grab_path.parent))
            except OSError as e:
                self.append_log(f"[devtask] 打开日志目录失败：{e}\n", "error")

    def on_open_apk_dir(self) -> None:
        apk = devtask.apk_path(self.build_type_var.get())
        target = apk.parent if apk.parent.exists() else devtask.PROJECT_ROOT
        os.startfile(str(target))

    def on_open_log_dir(self) -> None:
        directory = devtask.log_output_path().parent
        directory.mkdir(parents=True, exist_ok=True)
        os.startfile(str(directory))

    def on_stop(self) -> None:
        proc = getattr(self, "proc", None)
        if proc and proc.poll() is None:
            # 子进程已用 CREATE_NEW_PROCESS_GROUP 建组，/T 只波及该组内的 gradle 派生子进程
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=devtask.NO_WINDOW,
            )
            self._set_status("中止中…")


def run_embedded() -> None:
    """打包后的入口：双击 exe 直接进 GUI，不带任何命令行参数。"""
    def report_exception(exc_type, exc, tb) -> None:
        messagebox.showerror("devtask 出错", "".join(traceback.format_exception(exc_type, exc, tb)))

    try:
        devtask.set_project(None, quiet=True)
        app = DevtaskApp()
    except devtask.TaskError as e:
        messagebox.showerror("devtask", str(e))
        return
    app.report_callback_exception = report_exception
    try:
        app.mainloop()
    except Exception:
        report_exception(*sys.exc_info())


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="devtask-gui", description="devtask 图形看板")
    parser.add_argument("project", nargs="?", help="Android 工程根目录（默认取 devtask.config.json 的 project）")
    project = parser.parse_args().project

    # pythonw 下没有控制台，工程错误必须用弹窗告知，否则窗口直接退出
    try:
        devtask.set_project(project, quiet=True)
        app = DevtaskApp()
    except devtask.TaskError as e:
        messagebox.showerror("devtask", str(e))
        return

    def report_exception(exc_type, exc, tb) -> None:
        # 用 pythonw 启动时没有控制台，异常必须落到消息框，否则窗口一闪而过
        messagebox.showerror("devtask 出错", "".join(traceback.format_exception(exc_type, exc, tb)))

    app.report_callback_exception = report_exception
    try:
        app.mainloop()
    except Exception:
        report_exception(*sys.exc_info())


if __name__ == "__main__":
    main()
