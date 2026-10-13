# GUI layer appended to the existing standalone download engine.
import queue
import subprocess
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# TCGdex 未公布硬性请求或并发限额；PTCG 值为本机实测择优预设。
# 2026-10-13：同样样本比较16/32/64，查询64、下载64吞吐最高。
# 详见 tcgdex-benchmark-results/report.md；图片实测使用120秒socket超时。
# https://tcgdex.dev/faq#are-there-rate-limits
GAME_PRESETS = {
    "万智牌": (DEFAULT_WORKERS, DEFAULT_DOWNLOAD_WORKERS),
    "宝可梦TCG（中文）": (64, 64),
    "宝可梦TCG（英文）": (64, 64),
    "游戏王（简中）": (DEFAULT_WORKERS, DEFAULT_DOWNLOAD_WORKERS),
    "游戏王（日文）": (DEFAULT_WORKERS, DEFAULT_DOWNLOAD_WORKERS),
    "游戏王（TCG）": (DEFAULT_WORKERS, DEFAULT_DOWNLOAD_WORKERS),
}
GAME_CODES = dict(zip(GAME_PRESETS, ("mtg", "ptcg-zh", "ptcg-en", "ygo-zh", "ygo-ja", "ygo-tcg")))
GAME_PICTURE_DIRECTORIES = {
    "万智牌": "mtg-pics",
    "宝可梦TCG（中文）": "ptcg-pics/zh-tw",
    "宝可梦TCG（英文）": "ptcg-pics/en",
}
PTCG_LANGUAGES = {"ptcg-zh": "zh-tw", "ptcg-en": "en"}
FIXED_LANGUAGE_LABELS = {
    "ptcg-zh": "繁体中文（zh-tw，固定，不回退）",
    "ptcg-en": "英文（en，固定，不回退）",
    "ygo-zh": "简体中文（占位）",
    "ygo-ja": "日文（占位）",
    "ygo-tcg": "TCG（占位）",
}

LANG_PRESETS = {
    "简体 → 繁体 → 英文（默认）": "zhs,zht,en",
    "繁体 → 简体 → 英文": "zht,zhs,en",
    "英文 → 简体 → 繁体": "en,zhs,zht",
    "简体 → 英文": "zhs,en",
    "繁体 → 英文": "zht,en",
}


def program_directory():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def create_task_directory(output_root):
    """按任务启动的本地时间创建目录，原子创建防止多窗口同秒冲突。"""
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%y%m%d-%H%M%S")
    sequence = 0
    while True:
        name = stamp if sequence == 0 else "%s_%02d" % (stamp, sequence)
        candidate = output_root / name
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            sequence += 1


def engine_command(deck, output, workers, paper, langs, pdf, marks, dry_run, force,
                   download_workers=DEFAULT_DOWNLOAD_WORKERS, game='mtg'):
    if game not in ('mtg', 'ptcg-zh', 'ptcg-en'):
        raise ValueError("该游戏尚未接入下载接口。")
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--engine"]
    else:
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe" and executable.with_name("python.exe").exists():
            executable = executable.with_name("python.exe")
        command = [str(executable), "-u", str(Path(__file__).resolve()), "--engine"]
    pics = Path(output).parent / ("mtg-pics" if game == 'mtg' else "ptcg-pics")
    if game in PTCG_LANGUAGES:
        pics /= PTCG_LANGUAGES[game]
    command += [str(deck), "--out", str(output), "--pics", str(pics),
                "--game", game, "--workers", str(workers),
                "--download-workers", str(download_workers), "--paper", paper]
    if game == 'mtg':
        command += ["--langs", langs]
    if not pdf:
        command.append("--no-pdf")
    if not marks:
        command.append("--no-cut-marks")
    if dry_run:
        command.append("--dry-run")
    if force:
        command.append("--force")
    return command


class CardDownloaderGUI:
    def __init__(self, root, base_dir=None):
        self.root = root
        self.base_dir = Path(base_dir) if base_dir else program_directory()
        self.output_root = self.base_dir / "卡图输出"
        self.output_dir = self.output_root
        self.events = queue.Queue()
        self.process = None
        self.process_lock = threading.Lock()
        self.running = False
        self.closing = False
        self.cancel_requested = threading.Event()
        self.summary = None
        self.token_summary = None
        self.download_total = 0
        self.download_finished = 0
        self.last_pdf_allowed = False
        self.previous_pdf_stamp = None
        self.poll_id = None
        self.edit_controls = []

        root.title("Scryfall 卡图下载器")
        root.geometry("1080x820")
        root.minsize(920, 740)
        root.configure(bg="#f3f5f8")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._style()
        self.game = tk.StringVar(value="万智牌")
        self.network_policy = tk.StringVar()
        self.game.trace_add("write", self._on_game_changed)
        self._on_game_changed()
        self.paper = tk.StringVar(value="A4 · 竖向 · 每页 9 张")
        self.language = tk.StringVar(value=next(iter(LANG_PRESETS)))
        self.pdf = tk.BooleanVar(value=True)
        self.cut_marks = tk.BooleanVar(value=True)
        self.dry_run = tk.BooleanVar(value=False)
        self.force = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="就绪：粘贴牌表，或导入 TXT 文件。")
        self.count = tk.StringVar(value="0 种牌 · 0 张")
        self.detail = tk.StringVar(value="自动处理网络请求，受限时等待并重试。")
        self.output_label = tk.StringVar(value=str(self.output_root / "yymmdd-hhmmss"))
        self.pics_label = tk.StringVar(value=str(self.picture_directory()))
        self._build()
        self.poll_id = root.after(80, self.poll_events)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f5f8")
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10), background="#f3f5f8", foreground="#243248")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Subtitle.TLabel", font=("Microsoft YaHei UI", 9), foreground="#65748a")
        style.configure("Card.TLabel", background="white")
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 11, "bold"), background="white")
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 7))
        style.configure("Primary.TButton", background="#2563eb", foreground="white", font=("Microsoft YaHei UI", 11, "bold"))
        style.map("Primary.TButton", background=[("active", "#1d4ed8"), ("disabled", "#b9c7df")], foreground=[("disabled", "#f3f5f8")])
        style.configure("TCheckbutton", font=("Microsoft YaHei UI", 10), background="white")
        style.configure("TCombobox", padding=5, font=("Microsoft YaHei UI", 10))
        style.configure("Horizontal.TProgressbar", background="#2563eb", troughcolor="#e3e9f2", borderwidth=0)

    def _editable(self, widget, normal="normal"):
        self.edit_controls.append((widget, normal))
        return widget

    def _build(self):
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Scryfall 卡图下载器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="粘贴中英文牌表，下载高清卡图，生成可打印卡册。", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 14))
        content = ttk.Frame(outer)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2, minsize=340)
        content.rowconfigure(0, weight=1)
        left = ttk.Frame(content, style="Card.TFrame", padding=16)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        right_shell = ttk.Frame(content, style="Card.TFrame")
        right_shell.grid(row=0, column=1, sticky="nsew")
        actions = ttk.Frame(right_shell, style="Card.TFrame", padding=(16, 8))
        actions.pack(side="bottom", fill="x")
        self.options_canvas = tk.Canvas(right_shell, bg="white", highlightthickness=0, width=340)
        options_scroll = ttk.Scrollbar(right_shell, command=self.options_canvas.yview)
        self.options_canvas.configure(yscrollcommand=options_scroll.set)
        options_scroll.pack(side="right", fill="y")
        self.options_canvas.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(self.options_canvas, style="Card.TFrame", padding=16)
        options_window = self.options_canvas.create_window((0, 0), window=right, anchor="nw")
        right.bind("<Configure>", lambda event: self.options_canvas.configure(scrollregion=self.options_canvas.bbox("all")))
        self.options_canvas.bind("<Configure>", lambda event: self.options_canvas.itemconfigure(options_window, width=event.width))
        def scroll_options(event):
            widget = event.widget
            if isinstance(widget, (ttk.Combobox, ttk.Spinbox)):
                return
            while widget is not None:
                if widget is right or widget is self.options_canvas:
                    self.options_canvas.yview_scroll(-int(event.delta / 120), "units")
                    return "break"
                widget = getattr(widget, "master", None)
        self.root.bind_all("<MouseWheel>", scroll_options, add="+")
        ttk.Label(left, text="牌表粘贴框", style="Section.TLabel").pack(anchor="w")
        tools = ttk.Frame(left, style="Card.TFrame")
        tools.pack(fill="x", pady=(10, 10))
        for label, action in [("导入 TXT", self.import_deck), ("粘贴", self.paste_deck), ("示例", self.example), ("清空", self.clear_deck)]:
            self._editable(ttk.Button(tools, text=label, command=action)).pack(side="left", padx=(0, 5))
        text_frame = ttk.Frame(left, style="Card.TFrame")
        text_frame.pack(fill="both", expand=True)
        self.deck_text = tk.Text(text_frame, wrap="word", undo=True, font=("Microsoft YaHei UI", 11),
                                 bg="#f7f9fc", fg="#243248", insertbackground="#2563eb", relief="flat", padx=12, pady=10, width=45, height=13)
        self._editable(self.deck_text)
        scrollbar = ttk.Scrollbar(text_frame, command=self.deck_text.yview)
        self.deck_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.deck_text.pack(side="left", fill="both", expand=True)
        self.deck_text.bind("<<Modified>>", self._on_modified)
        ttk.Label(left, textvariable=self.count, style="Card.TLabel").pack(anchor="w", pady=(8, 3))
        ttk.Label(left, text="MTG：数量 卡名，可加系列代码 编号锁定版本。\nPTCG：数量 卡名 系列代码 编号（必填）。\n游戏王占位支持纯名称行；数量用于 PDF 排版。", style="Card.TLabel", font=("Microsoft YaHei UI", 9)).pack(anchor="w")

        ttk.Label(right, text="卡牌游戏", style="Section.TLabel").pack(anchor="w")
        self.game_box = self._editable(ttk.Combobox(
            right, textvariable=self.game, values=tuple(GAME_PRESETS), state="readonly"), "readonly")
        self.game_box.pack(fill="x", pady=(4, 6))
        ttk.Label(right, textvariable=self.network_policy, style="Card.TLabel",
                  wraplength=300, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 14))
        ttk.Label(right, text="下载与排版", style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Label(right, text="PDF 纸张", style="Card.TLabel").pack(anchor="w")
        self.paper_box = self._editable(ttk.Combobox(right, textvariable=self.paper, state="readonly",
                              values=("A4 · 竖向 · 每页 9 张", "A3 · 横向 · 每页 18 张")), "readonly")
        self.paper_box.pack(fill="x", pady=(4, 10))
        ttk.Label(right, text="语言 / MTG 优先顺序", style="Card.TLabel").pack(anchor="w")
        self.language_box = self._editable(ttk.Combobox(right, textvariable=self.language, values=tuple(LANG_PRESETS)))
        self.language_box.pack(fill="x", pady=(4, 3))
        ttk.Label(right, text="MTG 按语言顺序选图；PTCG 语种固定，不回退。\n已有缓存直接复用；换版本时勾选“重新选图”。", style="Card.TLabel", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 10))
        ttk.Label(right, text="自动处理网络请求，受限时等待并重试。", style="Card.TLabel", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 10))
        self.pdf_check = self._editable(ttk.Checkbutton(right, text="生成可打印 PDF 卡册", variable=self.pdf, command=self.sync_options))
        self.pdf_check.pack(anchor="w", pady=2)
        self.mark_check = self._editable(ttk.Checkbutton(right, text="显示裁切框", variable=self.cut_marks))
        self.mark_check.pack(anchor="w", pady=2)
        self.dry_check = self._editable(ttk.Checkbutton(right, text="仅查询选图（不下载、不生成 PDF）", variable=self.dry_run, command=self.sync_options))
        self.dry_check.pack(anchor="w", pady=2)
        self.force_check = self._editable(ttk.Checkbutton(right, text="重新选图并下载（忽略图片缓存）", variable=self.force))
        self.force_check.pack(anchor="w", pady=2)
        ttk.Separator(right).pack(fill="x", pady=10)
        ttk.Label(right, text="输出位置 · 按任务时间戳隔离", style="Card.TLabel", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        self.path_display = ttk.Label(right, textvariable=self.output_label, style="Card.TLabel", wraplength=320, font=("Microsoft YaHei UI", 9), foreground="#65748a")
        self.path_display.pack(fill="x", pady=(4, 8))
        ttk.Label(right, text="图片缓存 · 重复牌名跳过查询和下载", style="Card.TLabel", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        ttk.Label(right, textvariable=self.pics_label, style="Card.TLabel", wraplength=320, font=("Microsoft YaHei UI", 9), foreground="#65748a").pack(fill="x", pady=(4, 8))
        self.start_button = ttk.Button(actions, text="开始下载", style="Primary.TButton", command=self.start)
        self.start_button.pack(fill="x", pady=(0, 5))
        action_row = ttk.Frame(actions, style="Card.TFrame")
        action_row.pack(fill="x")
        self.cancel_button = ttk.Button(action_row, text="停止任务", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(action_row, text="打开输出文件夹", command=self.open_output).pack(side="left", fill="x", expand=True)
        ttk.Button(actions, text="打开当前游戏卡图", command=self.open_pics).pack(fill="x", pady=(5, 0))

        progress_frame = ttk.Frame(outer)
        progress_frame.pack(fill="x", pady=(14, 8))
        ttk.Label(progress_frame, textvariable=self.status).pack(anchor="w")
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=6)
        ttk.Label(progress_frame, textvariable=self.detail, style="Subtitle.TLabel").pack(anchor="w")
        log_bar = ttk.Frame(outer)
        log_bar.pack(fill="x")
        ttk.Label(log_bar, text="运行日志").pack(side="left")
        self.pdf_button = ttk.Button(log_bar, text="打开 PDF", command=self.open_pdf, state="disabled")
        self.pdf_button.pack(side="right", padx=(5, 0))
        ttk.Button(log_bar, text="复制日志", command=self.copy_log).pack(side="right")
        log_frame = ttk.Frame(outer)
        log_frame.pack(fill="both", pady=(5, 0))
        self.log_text = tk.Text(log_frame, state="disabled", height=8, wrap="word", font=("Consolas", 9),
                                bg="#172338", fg="#d7e3f5", relief="flat", padx=10, pady=8)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        self.sync_options()

    def game_code(self):
        try:
            return GAME_CODES[self.game.get()]
        except KeyError:
            raise ValueError("请选择列表中的卡牌游戏。") from None

    def deck_rows(self, text):
        game = self.game_code()
        if game in PTCG_LANGUAGES:
            return parse_ptcg_decklist(text)
        if game == 'mtg':
            return parse_decklist(text)
        # 游戏王支持纯名称行；不能套用 MTG/PTCG 的数字行过滤。
        rows = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "//", ";")):
                continue
            match = re.fullmatch(r"(\d+)\s+(.+)", line)
            rows.append((int(match.group(1)), match.group(2)) if match else (1, line))
        return rows

    def update_count(self):
        try:
            rows = self.deck_rows(self.deck_text.get("1.0", "end-1c"))
            self.count.set("%d 种牌 · %d 张" % (len({tuple(row[1:]) for row in rows}), sum(row[0] for row in rows)))
        except ValueError as exc:
            self.count.set("牌表格式错误：%s" % exc)

    def _on_modified(self, event=None):
        if self.deck_text.edit_modified():
            self.update_count()
            self.deck_text.edit_modified(False)

    def set_deck(self, text):
        self.deck_text.delete("1.0", "end")
        self.deck_text.insert("1.0", text)
        self._on_modified()

    def import_deck(self):
        path = filedialog.askopenfilename(parent=self.root, title="导入牌表", filetypes=[("牌表文本", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("gb18030")
            self.set_deck(text)
        except Exception as exc:
            messagebox.showerror("无法导入牌表", str(exc), parent=self.root)

    def paste_deck(self):
        try:
            text = self.root.clipboard_get()
            self.deck_text.insert("insert", text)
            self._on_modified()
        except tk.TclError:
            self.status.set("剪贴板中没有可粘贴的文字。")

    def example(self):
        examples = {
            'mtg': "# 自动行与指定版可混用\n4 Lightning Bolt\n1 Lightning Bolt M11 146\n1 Lightning Bolt (M11) 146\n4 Counterspell\n8 Forest\n1 碎骨巨人\n",
            'ptcg-zh': "寶可夢\n2 爆炸頭水牛 SV8a 133\n1 傑尼龜 SV2a 007\n訓練家\n1 招式學習器 演進 SV8a 160\n",
            'ptcg-en': "Pokémon\n2 Shaymin DRI 10\n1 Froslass PR-SV 117\n1 Tatsugiri PR-SV 118\n",
            'ygo-zh': "青眼白龙\n黑魔术师\n死者苏生\n",
            'ygo-ja': "青眼の白龍\nブラック・マジシャン\n死者蘇生\n",
            'ygo-tcg': "Blue-Eyes White Dragon\nDark Magician\nMonster Reborn\n",
        }
        self.set_deck(examples[self.game_code()])

    def clear_deck(self):
        self.set_deck("")

    def sync_options(self):
        if self.running:
            return
        self.sync_language()
        self.pdf_check.configure(state="disabled" if self.dry_run.get() else "normal")
        enabled = self.pdf.get() and not self.dry_run.get()
        self.paper_box.configure(state="readonly" if enabled else "disabled")
        self.mark_check.configure(state="normal" if enabled else "disabled")
        self.force_check.configure(state="disabled" if self.dry_run.get() else "normal")
        self.start_button.configure(text="开始查询" if self.dry_run.get() else "开始下载")

    def set_running(self, running):
        self.running = running
        for widget, normal in self.edit_controls:
            widget.configure(state="disabled" if running else normal)
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        if not running:
            self.sync_options()

    def picture_directory(self):
        game = self.game_code()
        if game.startswith('ygo-'):
            raise ValueError("游戏王尚未接入，暂无图片目录。")
        return self.output_root / GAME_PICTURE_DIRECTORIES[self.game.get()]

    def concurrency(self):
        self.game_code()
        return GAME_PRESETS[self.game.get()]

    def sync_language(self):
        game = self.game_code()
        if game == 'mtg':
            if self.language.get() in FIXED_LANGUAGE_LABELS.values():
                self.language.set(getattr(self, '_mtg_language', next(iter(LANG_PRESETS))))
            self.language_box.configure(state="disabled" if self.running else "normal")
        else:
            if self.language.get() not in FIXED_LANGUAGE_LABELS.values():
                self._mtg_language = self.language.get()
            self.language.set(FIXED_LANGUAGE_LABELS[game])
            self.language_box.configure(state="disabled")

    def _on_game_changed(self, *args):
        if self.game.get() not in GAME_PRESETS:
            self.network_policy.set("请选择列表中的卡牌游戏。")
            return
        game = self.game_code()
        if hasattr(self, "pics_label"):
            self.pics_label.set("尚未接入，暂无图片目录" if game.startswith('ygo-') else str(self.picture_directory()))
        query_workers, download_workers = self.concurrency()
        text = "查询并发 %d · 下载并发 %d" % (query_workers, download_workers)
        if game in PTCG_LANGUAGES:
            text += "\n本机实测择优，TCGdex 未公布硬性限额。"
        elif game.startswith('ygo-'):
            text = "游戏王占位：下载接口尚未接入；可识别纯名称牌表。"
        self.network_policy.set(text)
        if hasattr(self, 'language_box'):
            self.sync_language()
        if hasattr(self, 'deck_text'):
            self.update_count()

    def configuration(self):
        text = self.deck_text.get("1.0", "end-1c").strip()
        game = self.game_code()
        if game.startswith('ygo-'):
            raise ValueError("游戏王下载接口尚未接入。")
        rows = self.deck_rows(text)
        if not rows:
            raise ValueError("请先粘贴或导入牌表，每行填写一种牌。")
        if any(row[0] <= 0 for row in rows):
            raise ValueError("牌表里的数量必须大于 0。")
        workers, _ = self.concurrency()
        if game in PTCG_LANGUAGES:
            codes = [PTCG_LANGUAGES[game]]
        else:
            lang_text = self.language.get().strip()
            langs = LANG_PRESETS.get(lang_text, lang_text).replace("，", ",")
            codes = [c.strip().lower() for c in langs.split(",") if c.strip()]
            if not codes or any(not re.fullmatch(r"[a-z]{2,3}", c) for c in codes):
                raise ValueError("请选择语言顺序，或输入语言代码，例如 zhs,zht,en。")
        if not self.paper.get().startswith(("A3", "A4")):
            raise ValueError("请选择 A3 或 A4 纸张。")
        return text, workers, self.paper.get()[:2], ",".join(dict.fromkeys(codes))

    def start(self):
        if self.running:
            return
        try:
            text, workers, paper, langs = self.configuration()
            _, download_workers = self.concurrency()
            task_dir = create_task_directory(self.output_root)
            deck_path = task_dir / "本次牌表.txt"
            deck_path.write_text(text + "\n", encoding="utf-8")
            command = engine_command(deck_path, task_dir, workers, paper, langs,
                                     self.pdf.get(), self.cut_marks.get(), self.dry_run.get(), self.force.get(),
                                     download_workers=download_workers, game=self.game_code())
            self.output_dir = task_dir
            self.output_label.set(str(task_dir))
        except Exception as exc:
            messagebox.showerror("无法开始", str(exc), parent=self.root)
            return
        self.cancel_requested.clear()
        self.summary = None
        self.token_summary = None
        self.download_total = self.download_finished = 0
        self.last_pdf_allowed = self.pdf.get() and not self.dry_run.get()
        pdf_path = self.output_dir / "cards.pdf"
        self.previous_pdf_stamp = (pdf_path.stat().st_mtime_ns, pdf_path.stat().st_size) if pdf_path.exists() else None
        self.pdf_button.configure(state="disabled")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.progress.stop()
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self.status.set("正在读取本地图片缓存并处理新牌名…")
        self.detail.set("自动处理网络请求，受限时等待并重试。")
        self.set_running(True)
        self.append_log("程序目录：%s\n输出目录：%s\n" % (self.base_dir, self.output_dir))
        threading.Thread(target=self.run_process, args=(command,), daemon=True).start()

    def run_process(self, command):
        """只负责进程和队列，所有 Tk 操作都在主线程进行。"""
        process = None
        code = 1
        try:
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUTF8"] = "1"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with self.process_lock:
                if self.cancel_requested.is_set() or self.closing:
                    self.events.put(("finished", -1))
                    return
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, cwd=str(self.base_dir), env=environment,
                                           creationflags=flags, text=True, encoding="utf-8", errors="replace", bufsize=1)
                self.process = process
            for line in process.stdout:
                self.events.put(("log", line))
            code = process.wait()
        except Exception as exc:
            self.events.put(("log", "启动或运行失败：%s\n" % exc))
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
                if process.stdout is not None:
                    process.stdout.close()
            with self.process_lock:
                self.process = None
        self.events.put(("finished", code))

    def append_log(self, text):
        # PDF 进度使用回车刷新；GUI 显示为普通日志行。
        text = text.replace("\r", "\n")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def interpret_log(self, text):
        token_summary = re.search(r"^Token 完成：成功 (\d+) / 失败 (\d+) / 关联检查失败 (\d+)\s*$", text.strip())
        if token_summary:
            self.token_summary = tuple(map(int, token_summary.groups()))
            self.status.set("Token 处理完成。")
            self.detail.set("Token 成功 %d · 失败 %d · 关联检查失败 %d" % self.token_summary)
            return
        if text.lstrip().startswith("[Token]"):
            self.status.set(text.strip())
            self.detail.set("正在自动处理关联 Token，网络请求受限时等待并重试。")
            return
        cached = re.search(r"\[缓存 (\d+)/(\d+)\] (.*)", text)
        if cached:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(1, int(cached.group(2))), value=int(cached.group(1)))
            self.status.set("本地缓存 %s/%s：%s" % cached.groups())
        if "所有牌名已在本地图片缓存中" in text:
            self.status.set("全部命中本地缓存，跳过 API 查询。")
        query = re.search(r"\[查询 (\d+)/(\d+)\] (.*)", text)
        query_begin = re.search(r"开始查询：(\d+) 种牌，查询并发 (\d+)", text)
        query_progress = re.search(r"查询进度 (\d+)/(\d+) (.*)", text)
        begin = re.search(r"开始下载：(\d+) 张图片，初始并发 (\d+)", text)
        download = re.search(r"下载完成 \[(\d+)/(\d+)\]", text)
        progress = re.search(r"下载进度 (\d+)/(\d+) (.*)", text)
        reduced = re.search(r"自动降并发：(\d+) -> (\d+)", text)
        summary = re.search(r"^完成：成功 (\d+) / 失败 (\d+)\s*$", text.strip())
        if query_begin:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(1, int(query_begin.group(1))), value=0)
            self.status.set("正在并行查询牌表并选择卡图…")
            self.detail.set("%s 种牌 · 自动处理网络请求，受限时等待并重试。" % query_begin.group(1))
        if query:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(1, int(query.group(2))), value=int(query.group(1)))
            self.status.set("查询 %s/%s：%s" % query.groups())
        if query_progress:
            self.progress.configure(value=int(query_progress.group(1)))
            self.detail.set(text.strip())
        if begin:
            self.download_total = int(begin.group(1))
            self.download_finished = 0
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(1, self.download_total), value=0)
            self.status.set("正在并行下载卡图…")
            self.detail.set("图片 %s 张 · 自动处理网络请求，受限时等待并重试。" % begin.group(1))
        if download:
            self.download_finished = int(download.group(1))
            self.progress.configure(value=self.download_finished)
            self.status.set("下载 %s/%s" % download.groups())
        elif "下载失败：" in text:
            self.download_finished += 1
            self.progress.configure(value=self.download_finished)
        if progress:
            self.download_finished = int(progress.group(1))
            self.progress.configure(value=self.download_finished)
            self.detail.set(text.strip())
        if reduced:
            self.detail.set("下载请求受限，等待后自动重试。")
        if "API 受限 HTTP" in text:
            self.status.set("查询请求受限，所有查询等待后自动重试…")
            self.detail.set(text.strip())
        elif "受限 HTTP" in text:
            self.status.set("服务器限制下载，正在等待后重试…")
        if "排版卡册:" in text:
            self.status.set("图片下载完成，正在生成 PDF 卡册…")
            self.progress.stop()
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        if summary:
            self.summary = tuple(map(int, summary.groups()))

    def poll_events(self):
        if self.closing:
            return
        for _ in range(300):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(value)
                self.interpret_log(value)
            elif kind == "finished":
                self.finish(value)
        self.poll_id = self.root.after(80, self.poll_events)

    def finish(self, code):
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100)
        if self.cancel_requested.is_set():
            self.status.set("任务已停止，已下载的图片保留。")
            self.detail.set("图片保留在 %s；记录保留在 %s；下次优先复用缓存。" % (self.picture_directory().name, self.output_dir.name))
        elif code == 0 or self.summary is not None or self.token_summary is not None:
            self.progress.configure(value=100)
            partial = code != 0 or (self.summary is not None and self.summary[1] > 0) or (self.token_summary is not None and any(self.token_summary[1:]))
            self.status.set("任务完成，部分卡牌失败。" if partial else ("查询完成。" if self.dry_run.get() else "任务完成。"))
            statistics = []
            if self.summary is not None:
                statistics.append("主卡成功 %d · 失败 %d" % self.summary)
            if self.token_summary is not None:
                statistics.append("Token 成功 %d · 失败 %d · 关联检查失败 %d" % self.token_summary)
            statistics.append("请查看日志和清单。" if partial else "输出已保存到程序同目录。")
            self.detail.set("；".join(statistics))
        else:
            self.status.set("运行失败，请查看下方日志。")
            self.detail.set("输出目录：%s" % self.output_dir)
        self.set_running(False)
        pdf_path = self.output_dir / "cards.pdf"
        pdf_stamp = (pdf_path.stat().st_mtime_ns, pdf_path.stat().st_size) if pdf_path.exists() else None
        pdf_ready = self.last_pdf_allowed and not self.cancel_requested.is_set() and pdf_stamp is not None and pdf_stamp != self.previous_pdf_stamp and code in (0, 1) and self.summary is not None
        self.pdf_button.configure(state="normal" if pdf_ready else "disabled")
        try:
            (self.output_dir / "运行日志.txt").write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8")
        except OSError as exc:
            self.append_log("无法保存日志：%s\n" % exc)

    def cancel(self):
        if not self.running:
            return
        self.cancel_requested.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("正在停止任务…")
        with self.process_lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()

    def open_output(self):
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._open_path(self.output_dir)
        except Exception as exc:
            messagebox.showerror("无法打开输出目录", str(exc), parent=self.root)

    def open_pics(self):
        try:
            path = self.picture_directory()
            path.mkdir(parents=True, exist_ok=True)
            self._open_path(path)
        except Exception as exc:
            messagebox.showerror("无法打开图片缓存", str(exc), parent=self.root)

    def open_pdf(self):
        path = self.output_dir / "cards.pdf"
        if path.exists():
            try:
                self._open_path(path)
            except Exception as exc:
                messagebox.showerror("无法打开 PDF", str(exc), parent=self.root)

    def _open_path(self, path):
        if os.name == "nt":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get("1.0", "end-1c"))
        self.status.set("日志已复制到剪贴板。")

    def close(self):
        self.closing = True
        self.cancel_requested.set()
        with self.process_lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
        if self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
        self.root.destroy()


def launch_gui():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    CardDownloaderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--engine":
        try:
            sys.exit(main(sys.argv[2:]))
        except KeyboardInterrupt:
            print("\n任务已停止。", file=sys.stderr, flush=True)
            sys.exit(130)
        except Exception as exc:
            print("运行失败：%s" % exc, file=sys.stderr, flush=True)
            sys.exit(1)
    else:
        launch_gui()
