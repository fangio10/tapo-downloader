from PIL import Image, ImageTk, ImageDraw, ImageFont
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont
import tkinter.messagebox as messagebox
import re
import json
import os
import threading
from threading import Timer
import time
import logging
import sys
import socket
from datetime import datetime
import shlex
import argparse

if getattr(sys, 'frozen', False) and sys.platform.startswith('linux'):
    _lib_candidates = (
        '/usr/lib64/libvlc.so.5',                       # Fedora/RHEL
        '/usr/lib/x86_64-linux-gnu/libvlc.so.5',        # Debian/Ubuntu amd64
        '/usr/lib/i386-linux-gnu/libvlc.so.5',          # Debian/Ubuntu i386
        '/usr/lib/aarch64-linux-gnu/libvlc.so.5',       # Debian/Ubuntu arm64
        '/usr/local/lib/libvlc.so.5',
    )
    _plugin_candidates = (
        '/usr/lib64/vlc/plugins',
        '/usr/lib/x86_64-linux-gnu/vlc/plugins',
        '/usr/lib/i386-linux-gnu/vlc/plugins',
        '/usr/lib/aarch64-linux-gnu/vlc/plugins',
        '/usr/lib/vlc/plugins',
        '/usr/local/lib/vlc/plugins',
    )
    for _candidate in _lib_candidates:
        if os.path.isfile(_candidate):
            os.environ.setdefault('PYTHON_VLC_LIB_PATH', _candidate)
            break
    for _candidate in _plugin_candidates:
        if os.path.isdir(_candidate):
            os.environ.setdefault('VLC_PLUGIN_PATH', _candidate)
            break

import vlc
import ctypes

def debounce(wait):
    # Decorator to debounce a function.
    def decorator(fn):
        def debounced(*args, **kwargs):
            def call_it():
                fn(*args, **kwargs)
            if hasattr(debounced, '_timer'):
                debounced._timer.cancel()
            debounced._timer = Timer(wait, call_it)
            debounced._timer.start()
        return debounced
    return decorator

class tapoStreamer:
    DEFAULT_VLC_PARAMS = [
        "--avcodec-hw=any",
        "--network-caching=3000",
        "--deinterlace=auto"
    ]

    @classmethod
    def parse_vlcparams(cls, raw, default=None):
        default = default if default is not None else cls.DEFAULT_VLC_PARAMS
        try:
            if isinstance(raw, (list, tuple)):
                params = list(raw)
            else:
                raw_str = (raw or "").strip()
                params = shlex.split(raw_str) if raw_str else []
            valid_params = [p for p in params if p.startswith('--')]
            return valid_params or default
        except Exception as e:
            logging.error(f"Failed to parse VLC parameters '{raw}': {e}", exc_info=True)
            return default

    MIN_WIDTH = 1340
    MIN_HEIGHT = 720
    FONT_CANDIDATES = ["Verdana", "Tahoma", "Arial", "Segoe UI", "DejaVu Sans", "Liberation Sans", "Noto Sans"]
    FONT_FALLBACK = "Helvetica"  # Always resolvable as a Tk built-in alias, even if nothing above matches

    # Maps raw filename detection-type tokens (case-insensitive) to a
    # canonical id. Several raw tokens can collapse onto the same canonical
    # id - this includes legacy tokens used before the downloader switched
    # to alarm_type-code-based naming, so older files keep working.
    DETECTION_TYPE_ALIASES = {
        "motion": "motion",
        "person": "person",
        "person_detection": "person",
        "pet": "pet",
        "pet_detection": "pet",
        "vehicle": "vehicle",
        "vehicle_detection": "vehicle",
        "baby_cry": "baby_cry",
        "baby_crying": "baby_cry",
        "bark": "bark",
        "meow": "meow",
        "line_crossing": "line_crossing",
        "area_intrusion": "area_intrusion",
        "tamper": "tamper",
        "tampering": "tamper",
        "glass_break": "glass_break",
        "smoke": "smoke",
        "face": "face",
        "loitering": "loitering",
        "package_delivered": "package_delivered",
        "package_picked_up": "package_picked_up",
    }

    # Canonical id -> friendly display label, in a sensible default order.
    DETECTION_TYPE_LABELS = {
        "motion": "Motion",
        "person": "Person",
        "pet": "Pet",
        "vehicle": "Vehicle",
        "face": "Face",
        "line_crossing": "Line Crossing",
        "area_intrusion": "Area Intrusion",
        "loitering": "Loitering",
        "package_delivered": "Package Delivered",
        "package_picked_up": "Package Picked Up",
        "tamper": "Tamper",
        "baby_cry": "Baby Crying",
        "bark": "Bark",
        "meow": "Meow",
        "glass_break": "Glass Break",
        "smoke": "Smoke",
    }

    ALL_TYPES_LABEL = "All Types"

    @classmethod
    def normalize_detection_type(cls, raw):
        """Map a raw filename token to a canonical detection-type id, or
        None if it isn't a recognized detection type (keeps things forward
        compatible with unrecognized future tags by falling back to
        treating them as their own canonical id rather than dropping them)."""
        if not raw:
            return None
        key = raw.strip().lower()
        if key in cls.DETECTION_TYPE_ALIASES:
            return cls.DETECTION_TYPE_ALIASES[key]
        if key:
            # Unknown tag - keep it visible/filterable rather than silently
            # discarding it, using the raw token itself as the canonical id.
            return key
        return None

    @classmethod
    def detection_type_label(cls, canonical_id):
        if canonical_id in cls.DETECTION_TYPE_LABELS:
            return cls.DETECTION_TYPE_LABELS[canonical_id]
        # Unknown canonical id - prettify it for display (e.g. "foo_bar" -> "Foo Bar")
        return canonical_id.replace("_", " ").title()

    def check_decoder_availability(self):
        # Best-effort check for a hardware-capable ffmpeg h264 decoder.
        if not sys.platform.startswith("linux"):
            return
        try:
            import subprocess
            result = subprocess.run(
                ['ffmpeg', '-decoders'],
                capture_output=True, text=True, timeout=5
            )
            output = result.stdout
            h264_lines = [l for l in output.splitlines() if 'h264' in l.lower()]
            has_hw_capable = any(
                ' h264 ' in l or l.strip().split()[-1] == 'h264'
                for l in h264_lines if 'libopenh264' not in l and 'v4l2m2m' not in l
            )
            only_openh264 = h264_lines and all('libopenh264' in l or 'v4l2m2m' in l for l in h264_lines)
            if only_openh264 or not has_hw_capable:
                logging.warning(
                    "ffmpeg appears to only provide software h264 decoding "
                    "(libopenh264) - hardware acceleration will not be available. "
                    "On Fedora/RHEL, install ffmpeg from RPM Fusion for a "
                    "hardware-capable build (h264 with vaapi/cuda/qsv support)."
                )
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _setup_logging(self, debug_mode):
        logging.getLogger().handlers = []

        if not debug_mode:
            null_handler = logging.NullHandler()
            logging.getLogger().addHandler(null_handler)
            logging.getLogger().setLevel(logging.CRITICAL + 1)
            return

        # INFO rather than DEBUG: the app itself only ever logs at
        # info/warning/error, so this loses none of its own output, but it
        # blocks debug-level chatter from third-party libraries (onvif,
        # urllib3, PIL, etc.) that would otherwise flood the log file.
        log_level = logging.INFO
        log_dir = os.path.dirname(self.config_file)
        log_file = os.path.join(log_dir, "tapo-streamer.log")

        logging.basicConfig(
            filename=log_file,
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )

        zeep_logger = logging.getLogger('zeep')
        zeep_logger.propagate = False 

        logging.getLogger('zeep.xsd.types.simple').setLevel(logging.WARNING)

        logging.info("Logging initialized with level INFO")

    def __init__(self, root):
        # Parse command-line arguments
        parser = argparse.ArgumentParser(description="Tapo Streamer Application")
        parser.add_argument('--debug', action='store_true', help="Enable debug logging")
        args = parser.parse_args()

        # --- Configuration Setup ---
        self.root = root
        self.root.title("Tapo Streamer")
        self.root.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)

        # Set up configuration directory
        if sys.platform.startswith("linux"):
            config_dir = os.path.join(os.path.expanduser("~"), ".tapo-streamer")
        else:
            config_dir = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "TapoStreamer")
        os.makedirs(config_dir, exist_ok=True)
        self.config_file = os.path.join(config_dir, "config.json")
        self.watch_progress_file = os.path.join(config_dir, "watch_progress.json")

        # Initialize debug mode and logging
        self.debug_mode = args.debug
        self.speed_cycle = [1.0, 2.0, 4.0, 8.0]
        self._init_font_choices()
        self.load_config()
        if not args.debug and hasattr(self, 'config_debug'):
            self.debug_mode = self.config_debug
        self._setup_logging(self.debug_mode)
        self.check_decoder_availability()

        # --- Application Setup ---
        if getattr(sys, 'frozen', False):
            self.base_path = sys._MEIPASS
            if sys.platform.startswith('win'):
                os.environ['VLC_PLUGIN_PATH'] = os.path.join(self.base_path, 'vlc', 'plugins')
            # On Linux, VLC_PLUGIN_PATH is set at module-load time above,
            # pointing at the system VLC plugin directory.
        else:
            self.base_path = os.path.dirname(os.path.abspath(__file__))

        icon_path = os.path.join(self.base_path, "cam.png")
        try:
            img = Image.open(icon_path)
            img_titlebar = img.resize((64, 64), Image.LANCZOS)
            icon = ImageTk.PhotoImage(img_titlebar)
            self.root.iconphoto(True, icon)
        except Exception as e:
            logging.error(f"Error loading icon: {e}", exc_info=True)

        # --- Initialize Core State ---
        self.root.configure(bg="#222222")
        self.apply_theme()
        self.root.minsize(1340, 720)
        self.root.protocol("WM_DELETE_WINDOW", self.cleanup)
        self.running = True
        self.is_fullscreen = False
        self.fullscreen_index = None
        self.help_overlay = None
        self.vlc_instances = [None] * 4
        self.stream_initializing = [False] * 4
        self.stream_init_lock = threading.Lock()
        self.stream_cleanup_events = [threading.Event() for _ in range(4)]
        self.archive_entry_locks = [threading.Lock() for _ in range(4)]
        self.archive_transitioning = [False] * 4
        self.media_players = [None] * 4
        self.streams = [""] * 4
        self.panels = [None] * 4
        self.labels = [None] * 4
        self.drop_timestamps = [[] for _ in range(4)]
        self.fullscreen_buttons = [None] * 4
        self.exit_fullscreen_button = None
        self.exit_fullscreen_image = None

        self.onvif_cams = {}
        self.ptz_moving = False
        self.ptz_busy = False
        self.ptz_buttons_disabled = False
        self.ptz_lock = threading.Lock()
        self.ptz_click_counts = [0] * 4

        self.is_archive_mode = [False] * 4 
        self.archive_mode_button = None
        self.archive_mode_image = None
        self.current_archive_path = [None] * 4
        self.playback_speeds = [1.0] * 4
        self.is_paused = [False] * 4
        self.video_ended = [False] * 4
        self.pagination_state = [{} for _ in range(4)]

        self.config_button = None
        self.ptz_buttons = []
        self.ptz_images = []
        self.archive_buttons = [None] * 4
        self.archive_canvas = [None] * 4
        self.back_buttons = [None] * 4
        self.exit_buttons = [None] * 4
        self.pause_buttons = [None] * 4
        self.speed_buttons = [None] * 4
        self.replay_buttons = [None] * 4
        self.rewind_buttons = [None] * 4
        self.audio_buttons = [None] * 4
        self.archive_audio_muted = [True] * 4
        self.event_mode = False

        # Hover polling state for clip-playback controls. On Windows the
        # embedded VLC HWND swallows Tk <Enter>/<Leave> events, so cursor
        # position is polled to know when to show/hide the clip control
        # strip for a given quadrant. This is display-only - no click
        # emulation is done here; all actions (fullscreen, exit, back,
        # events) use real Tk buttons/bindings, which already work
        # correctly on both platforms.
        self._hover_poll_ids = [None] * 4
        self._clip_controls_visible = [False] * 4

        self.event_clip_queues = [[] for _ in range(4)]
        self.event_active_cams = set()
        self.event_done_cams = set()
        self.event_overlay = None
        self.current_playing_event = None
        self._pending_event_afters = []
        self.events_button = None
        self.events_button_image = None
        self.event_back_button = None
        self.event_back_button_image = None
        self.watch_progress = {index: {} for index in range(4)}
        self.watch_progress_dirty = False
        self.visited_folders = {index: set() for index in range(4)}

        # Sleep mode: stop live streams after the app has been unfocused
        # or minimized for sleep_mode_minutes, and restart them when the
        # app is brought back to focus. 0 minutes disables the feature.
        self._sleep_timer_id = None
        self._is_asleep = False
        self._sleep_stopped_indices = []
        self._app_focused = True
        self._app_minimized = False

        self.panel_sizes = [(0, 0)] * 4
        self.target_dims = [(0, 0)] * 4
        self.frame_shapes = [(0, 0)] * 4
  
        # Initialize frame count tracking
        self.last_dropped_frames = {}  # Last cumulative dropped frames per stream
        self.last_displayed_frames = {}  # Last cumulative displayed frames per stream
        for i in range(len(self.ips)):
            self.last_dropped_frames[i] = 0
            self.last_displayed_frames[i] = 0

        # Pre-render and cache all icons
        self.icon_cache = {
            "up": self.create_icon("up"),
            "down": self.create_icon("down"),
            "left": self.create_icon("left"),
            "right": self.create_icon("right"),
            "minimize": self.create_icon("minimize"),
            "config": self.create_icon("config"),
            "disk": self.create_icon("disk"),
            "fullscreen": self.create_icon("fullscreen"),
            "pause": self.create_icon("pause"),
            "play": self.create_icon("play"),
            "speed": self.create_icon("speed"),
            "replay": self.create_icon("replay"),
            "rewind": self.create_icon("rewind"),
            "exit": self.create_icon("exit"),
            "resize": self.create_icon("resize"),
            "back": self.create_icon("back"),
            "folder": self.create_icon("folder", opacity=1.0),
            "folder_clicked": self.create_icon("folder", opacity=0.6),
            "archive": self.create_icon("archive", opacity=1.0),
            "events": self.create_icon("events"),
            "list": self.create_icon("list"),
            "delete": self.create_icon("delete"),
            "audio_on": self.create_icon("audio_on"),
            "audio_off": self.create_icon("audio_off"),
        }
        # Red variants of the mode-toggle icons, swapped in while Archive /
        # Events mode is active so the button visually indicates current mode.
        self.icon_cache["disk_active"] = self.recolor_icon_active(self.icon_cache["disk"])
        self.icon_cache["events_active"] = self.recolor_icon_active(self.icon_cache["events"])
        # Dimmed variants, swapped in while the button is disabled (e.g.
        # live streams reinitializing) so the button visibly reads as
        # inactive/locked rather than looking indistinguishable from its
        # normal enabled state - plain Tk disabled-state rendering for
        # image buttons is inconsistent across platforms/themes.
        self.icon_cache["disk_disabled"] = self.dim_icon(self.icon_cache["disk"])
        self.icon_cache["events_disabled"] = self.dim_icon(self.icon_cache["events"])

        self.day_folder_icon_cache = {}
        self.thumbnail_cache = {}
        self.thumbnail_cache_order = []
        self.thumbnail_cache_max = 200

        self.load_watch_progress()
        self.init_ui()
        self.update_streams()
        self.root.after(0, lambda: threading.Thread(target=self.start_streams, daemon=True).start())

    THEME_PALETTES = {
        "dark": {
            "bg": "#222222", "bg_alt": "#2a2a2a", "fg": "#ffffff",
            "fg_dim": "#aaaaaa", "accent": "#e62117",
            "field_bg": "#333333", "border": "#444444",
        },
        "light": {
            "bg": "#f0f0f0", "bg_alt": "#e2e2e2", "fg": "#1a1a1a",
            "fg_dim": "#666666", "accent": "#c8261a",
            "field_bg": "#ffffff", "border": "#bbbbbb",
        },
    }

    def detect_system_theme(self):
        """Best-effort detection of the OS-wide light/dark preference.
        Falls back to 'dark' if it can't be determined."""
        try:
            if sys.platform.startswith("win"):
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
                )
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "light" if value else "dark"
            elif sys.platform.startswith("linux"):
                import subprocess
                try:
                    result = subprocess.run(
                        ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                        capture_output=True, text=True, timeout=2
                    )
                    if result.returncode == 0 and "light" in result.stdout.lower():
                        return "light"
                    if result.returncode == 0 and "dark" in result.stdout.lower():
                        return "dark"
                except Exception:
                    pass
                try:
                    result = subprocess.run(
                        ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
                        capture_output=True, text=True, timeout=2
                    )
                    if result.returncode == 0 and "dark" in result.stdout.lower():
                        return "dark"
                except Exception:
                    pass
        except Exception as e:
            logging.warning(f"Could not detect system theme, defaulting to dark: {e}")
        return "dark"

    def apply_theme(self):
        """Apply the active theme (dark/light/system) to ttk and plain Tk
        widgets, so the app looks the same on Windows 11 as it does on
        Linux instead of relying on the native/system ttk theme.

        Note: this only re-themes UI chrome (dialogs, config panel,
        archive browser, buttons/text). Camera and clip video panels are
        intentionally left black regardless of theme, since that's the
        right backdrop for video either way."""
        mode = self.ui_theme
        if mode == "system":
            mode = self.detect_system_theme()
        palette = self.THEME_PALETTES.get(mode, self.THEME_PALETTES["dark"])
        self.active_theme_colors = palette

        try:
            bg = palette["bg"]
            bg_alt = palette["bg_alt"]
            fg = palette["fg"]
            fg_dim = palette["fg_dim"]
            accent = palette["accent"]
            field_bg = palette["field_bg"]
            border = palette["border"]

            style = ttk.Style(self.root)
            style.theme_use("clam")

            style.configure(".", background=bg, foreground=fg,
                             fieldbackground=field_bg, bordercolor=border,
                             darkcolor=bg, lightcolor=bg_alt,
                             troughcolor=bg_alt, focuscolor=accent)

            style.configure("TFrame", background=bg)
            style.configure("TLabel", background=bg, foreground=fg)
            style.configure("TSeparator", background=border)

            style.configure("TButton", background=bg_alt, foreground=fg,
                             bordercolor=border, focusthickness=1)
            style.map("TButton",
                      background=[("active", border), ("disabled", bg)],
                      foreground=[("disabled", fg_dim)])

            style.configure("TCheckbutton", background=bg, foreground=fg)
            style.map("TCheckbutton",
                      background=[("active", bg)],
                      foreground=[("disabled", fg_dim)])

            style.configure("TEntry", fieldbackground=field_bg, foreground=fg,
                             insertcolor=fg, bordercolor=border)

            style.configure("TCombobox", fieldbackground=field_bg, background=bg_alt,
                             foreground=fg, arrowcolor=fg, bordercolor=border)
            style.map("TCombobox",
                      fieldbackground=[("readonly", field_bg)],
                      foreground=[("disabled", fg_dim)])
            self.root.option_add("*TCombobox*Listbox.background", field_bg)
            self.root.option_add("*TCombobox*Listbox.foreground", fg)
            self.root.option_add("*TCombobox*Listbox.selectBackground", accent)

            style.configure("TNotebook", background=bg, bordercolor=border)
            style.configure("TNotebook.Tab", background=bg_alt, foreground=fg_dim,
                             padding=(12, 6))
            style.map("TNotebook.Tab",
                      background=[("selected", bg)],
                      foreground=[("selected", fg)])

            style.configure("Vertical.TScrollbar", background=bg_alt,
                             troughcolor=bg, arrowcolor=fg, bordercolor=border)
            style.configure("Horizontal.TScrollbar", background=bg_alt,
                             troughcolor=bg, arrowcolor=fg, bordercolor=border)

            # Toplevel dialogs (e.g. the config window) don't pick up the
            # root's bg automatically - apply the theme default for any
            # new Toplevel/Frame/Label/Entry/Text created without one.
            for widget_class in ("Toplevel", "Frame", "Label", "Text"):
                self.root.option_add(f"*{widget_class}.background", bg)
                self.root.option_add(f"*{widget_class}.foreground", fg)
            self.root.option_add("*Entry.background", field_bg)
            self.root.option_add("*Entry.foreground", fg)
            self.root.option_add("*Entry.insertBackground", fg)
            self.root.option_add("*Text.background", field_bg)
            self.root.option_add("*Text.foreground", fg)
            self.root.option_add("*Text.insertBackground", fg)
            self.root.option_add("*Button.background", bg_alt)
            self.root.option_add("*Button.foreground", fg)
            self.root.option_add("*Button.activeBackground", border)

            self.root.configure(bg=bg)
        except Exception as e:
            logging.warning(f"Failed to apply theme: {e}")

    def _init_font_choices(self):
        # Build the list of fonts offered in Options > General > Font.
        try:
            installed = {name.lower() for name in tkfont.families(self.root)}
        except Exception as e:
            logging.warning(f"Could not query installed fonts, using fallback: {e}")
            installed = set()

        available = [name for name in self.FONT_CANDIDATES if name.lower() in installed]
        if not available:
            available = [self.FONT_FALLBACK]
        available = available[:5]  # Keep the dropdown to a handful of choices

        self.font_choices = [{"label": name, "family": name, "weight": "normal"} for name in available]

        bold_base = available[0]
        self.font_choices.append({"label": f"{bold_base} Bold", "family": bold_base, "weight": "bold"})

        self.font_choice_labels = [choice["label"] for choice in self.font_choices]

    def app_font(self, size, style=None):
        # Return a Tk font tuple using the user's selected UI font.
        choice = next(
            (c for c in self.font_choices if c["label"] == self.ui_font),
            self.font_choices[0]
        )
        weight = style if style is not None else choice["weight"]
        return (choice["family"], size, weight)

    def load_config(self):
        # Initialize default configuration
        self.username = ""
        self.password = ""
        self.archive_dir = ""
        self.ips = ["", "", "", ""]
        self.hq_enabled = [True] * 4
        self.audio_enabled = [True] * 4
        self.ptz_supported = [False] * 4
        self.config_debug = False
        self.vlcparams = self.DEFAULT_VLC_PARAMS
        self.ptz_resolution = 3
        self.saved_window_size = "1340x720"
        self.enable_fullscreen_buttons = False
        self.default_playback_speed = 1.0
        # New stream reliability settings
        self.enable_retries = True
        self.max_retry_attempts = 5
        self.initial_backoff_delay = 2.0
        self.enable_quality_downgrade = True
        self.drop_threshold = 8
        self.drop_window = 30.0
        self.downgrade_cooldown = 120.0
        self.enable_auto_revert_hq = False
        self.stability_period = 300.0
        self.no_frame_timeout = 15.0
        self.ui_font = self.font_choice_labels[0]
        self.resume_playback = True
        self.motion_triggered_events = False
        self.event_overlap_window_mins = 1
        self.exclusive_archive_audio = True
        self.controls_position = "top-left"
        self.default_event_filter = "all"
        self.ui_theme = "dark"
        self.sleep_mode_minutes = 0

        # Load from config file if it exists
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    config = json.load(f)
                self.username = config.get("username", self.username)
                self.password = config.get("password", self.password)
                self.archive_dir = config.get("archive_dir", self.archive_dir)
                self.ips = config.get("ips", self.ips)
                self.hq_enabled = [bool(config.get("hq_enabled", self.hq_enabled)[i]) for i in range(4)]
                self.audio_enabled = config.get("audio_enabled", self.audio_enabled)
                self.ptz_supported = config.get("ptz_supported", self.ptz_supported)
                self.config_debug = config.get("debug", self.config_debug)
                self.vlcparams = self.parse_vlcparams(config.get("vlcparams", self.vlcparams), default=self.vlcparams)
                self.ptz_resolution = config.get("ptz_resolution", self.ptz_resolution)
                self.saved_window_size = config.get("saved_window_size", self.saved_window_size)
                self.enable_fullscreen_buttons = config.get("enable_fullscreen_buttons", self.enable_fullscreen_buttons)
                self.default_playback_speed = config.get("default_playback_speed", self.default_playback_speed)
                # Load new stream reliability settings
                self.enable_retries = config.get("enable_retries", self.enable_retries)
                self.max_retry_attempts = config.get("max_retry_attempts", self.max_retry_attempts)
                self.initial_backoff_delay = config.get("initial_backoff_delay", self.initial_backoff_delay)
                self.enable_quality_downgrade = config.get("enable_quality_downgrade", self.enable_quality_downgrade)
                self.drop_threshold = config.get("drop_threshold", self.drop_threshold)
                self.drop_window = config.get("drop_window", self.drop_window)
                self.downgrade_cooldown = config.get("downgrade_cooldown", self.downgrade_cooldown)
                self.enable_auto_revert_hq = config.get("enable_auto_revert_hq", self.enable_auto_revert_hq)
                self.stability_period = config.get("stability_period", self.stability_period)
                raw_no_frame = config.get("no_frame_timeout", self.no_frame_timeout)
                self.no_frame_timeout = float(raw_no_frame) if raw_no_frame > 5 else 15.0
                raw_font = config.get("ui_font", config.get("archive_font", self.ui_font))
                matched_font = next(
                    (label for label in self.font_choice_labels if label.lower() == str(raw_font).lower()),
                    None
                )
                self.ui_font = matched_font if matched_font else self.font_choice_labels[0]
                self.resume_playback = bool(config.get("resume_playback", self.resume_playback))
                self.motion_triggered_events = bool(config.get("motion_triggered_events", self.motion_triggered_events))
                self.event_overlap_window_mins = int(config.get("event_overlap_window_mins", self.event_overlap_window_mins))
                if self.event_overlap_window_mins not in (1, 2, 3, 5):
                    self.event_overlap_window_mins = 1
                self.exclusive_archive_audio = bool(config.get("exclusive_archive_audio", self.exclusive_archive_audio))
                raw_cp = config.get("controls_position", self.controls_position)
                self.controls_position = raw_cp if raw_cp in self.CONTROL_POSITIONS else "top-left"
                self.default_event_filter = str(config.get("default_event_filter", self.default_event_filter) or "all").lower()
                self.ui_theme = str(config.get("ui_theme", self.ui_theme) or "dark").lower()
                if self.ui_theme not in ("dark", "light", "system"):
                    logging.warning(f"Invalid ui_theme: {self.ui_theme}, using default 'dark'")
                    self.ui_theme = "dark"
                try:
                    self.sleep_mode_minutes = int(config.get("sleep_mode_minutes", self.sleep_mode_minutes))
                    if self.sleep_mode_minutes < 0:
                        logging.warning(f"Invalid sleep_mode_minutes: {self.sleep_mode_minutes}, using default 0")
                        self.sleep_mode_minutes = 0
                except (ValueError, TypeError):
                    logging.warning(f"Invalid sleep_mode_minutes input, using default 0")
                    self.sleep_mode_minutes = 0

                # Validate settings
                try:
                    if self.saved_window_size != "fullscreen":
                        width, height = map(int, self.saved_window_size.split("x"))
                        if width < self.MIN_WIDTH or height < self.MIN_HEIGHT:
                            logging.warning(f"Invalid saved_window_size: {self.saved_window_size}, using default 1340x720")
                            self.saved_window_size = "1340x720"
                except (ValueError, TypeError):
                    logging.warning(f"Invalid saved_window_size: {self.saved_window_size}, using default 1340x720")
                    self.saved_window_size = "1340x720"

                if not isinstance(self.ptz_resolution, int) or self.ptz_resolution < 1 or self.ptz_resolution > 5:
                    self.ptz_resolution = 3

                try:
                    self.default_playback_speed = float(self.default_playback_speed)
                    if self.default_playback_speed not in self.speed_cycle:
                        logging.warning(f"Invalid default_playback_speed: {self.default_playback_speed}, using default 1.0")
                        self.default_playback_speed = 1.0
                except (ValueError, TypeError):
                    logging.warning(f"Invalid default_playback_speed: {self.default_playback_speed}, using default 1.0")
                    self.default_playback_speed = 1.0

                if self.max_retry_attempts < 1:
                    logging.warning(f"Invalid max_retry_attempts: {self.max_retry_attempts}, using default 3")
                    self.max_retry_attempts = 5
                if self.initial_backoff_delay <= 0:
                    logging.warning(f"Invalid initial_backoff_delay: {self.initial_backoff_delay}, using default 1.0")
                    self.initial_backoff_delay = 2.0
                if self.drop_threshold < 1:
                    logging.warning(f"Invalid drop_threshold: {self.drop_threshold}, using default 10")
                    self.drop_threshold = 8
                if self.drop_window <= 0:
                    logging.warning(f"Invalid drop_window: {self.drop_window}, using default 5.0")
                    self.drop_window = 30.0
                if self.downgrade_cooldown < 10:
                    logging.warning(f"Invalid downgrade_cooldown: {self.downgrade_cooldown}, using default 30.0")
                    self.downgrade_cooldown = 120.0
                if self.stability_period < 10:
                    logging.warning(f"Invalid stability_period: {self.stability_period}, using default 30.0")
                    self.stability_period = 300.0

            except json.JSONDecodeError as e:
                logging.error(f"Failed to parse config file {self.config_file}: {e}. Using default settings.")
                self.save_config()
            except PermissionError as e:
                logging.error(f"Permission denied accessing config file {self.config_file}: {e}. Using default settings.")
            except Exception as e:
                logging.error(f"Unexpected error loading config file {self.config_file}: {e}", exc_info=True)
                self.save_config()
        else:
            logging.info(f"Config file {self.config_file} does not exist. Creating with default settings.")
            self.save_config()

        # Windows: real Tk buttons (fullscreen icon, exit-fullscreen,
        # per-clip exit, events) are the workaround for the embedded VLC
        # HWND swallowing native Tk mouse events - always force this on
        # rather than relying on GetAsyncKeyState-based click polling to
        # fake the missing clicks. Overrides any saved config value.
        if sys.platform.startswith('win'):
            self.enable_fullscreen_buttons = True

    def save_config(self):
        config = {
            "username": self.username,
            "password": self.password,
            "archive_dir": self.archive_dir,
            "vlcparams": self.vlcparams,
            "ips": self.ips,
            "hq_enabled": self.hq_enabled,
            "audio_enabled": self.audio_enabled,
            "ptz_supported": self.ptz_supported,
            "debug": self.config_debug,
            "ptz_resolution": self.ptz_resolution,
            "saved_window_size": self.saved_window_size,
            "enable_fullscreen_buttons": self.enable_fullscreen_buttons,
            "default_playback_speed": self.default_playback_speed,
            "enable_retries": self.enable_retries,
            "max_retry_attempts": self.max_retry_attempts,
            "initial_backoff_delay": self.initial_backoff_delay,
            "enable_quality_downgrade": self.enable_quality_downgrade,
            "drop_threshold": self.drop_threshold,
            "drop_window": self.drop_window,
            "downgrade_cooldown": self.downgrade_cooldown,
            "enable_auto_revert_hq": self.enable_auto_revert_hq,
            "stability_period": self.stability_period,
            "no_frame_timeout": self.no_frame_timeout,
            "ui_font": self.ui_font,
            "resume_playback": self.resume_playback,
            "motion_triggered_events": self.motion_triggered_events,
            "event_overlap_window_mins": self.event_overlap_window_mins,
            "exclusive_archive_audio": self.exclusive_archive_audio,
            "controls_position": self.controls_position,
            "default_event_filter": self.default_event_filter,
            "ui_theme": self.ui_theme,
            "sleep_mode_minutes": self.sleep_mode_minutes,
        }
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, "w") as f:
                json.dump(config, f, indent=4)
        except PermissionError as e:
            logging.error(f"Permission denied saving config to {self.config_file}: {e}")
            messagebox.showerror("Error", f"Failed to save configuration due to permission issues: {e}")
        except Exception as e:
            logging.error(f"Failed to save config to {self.config_file}: {e}", exc_info=True)
            messagebox.showerror("Error", f"Failed to save configuration: {e}")

    def load_watch_progress(self):
        #Load per-video watch progress from disk into self.watch_progress.
        if not os.path.exists(self.watch_progress_file):
            return
        try:
            with open(self.watch_progress_file, "r") as f:
                data = json.load(f)
            for index in range(4):
                entries = data.get(str(index), {})
                if isinstance(entries, dict):
                    cleaned = {}
                    for path, info in entries.items():
                        try:
                            position = float(info.get("position", 0))
                            duration = float(info.get("duration", 0))
                            if duration > 0 and position >= 0:
                                cleaned[path] = {"position": position, "duration": duration}
                        except (TypeError, ValueError, AttributeError):
                            continue
                    self.watch_progress[index] = cleaned
        except Exception as e:
            logging.warning(f"Failed to load watch progress from {self.watch_progress_file}: {e}")

    def save_watch_progress(self):
        # Persist self.watch_progress to disk.
        try:
            data = {str(index): self.watch_progress[index] for index in range(4)}
            os.makedirs(os.path.dirname(self.watch_progress_file), exist_ok=True)
            with open(self.watch_progress_file, "w") as f:
                json.dump(data, f, indent=2)
            self.watch_progress_dirty = False
        except Exception as e:
            logging.warning(f"Failed to save watch progress to {self.watch_progress_file}: {e}")

    def show_config_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Configuration")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        # Create a Notebook (tabbed interface)
        notebook = ttk.Notebook(dialog)
        notebook.pack(pady=10, padx=10, fill="both", expand=True)

        # Connection Tab
        connection_frame = ttk.Frame(notebook)
        notebook.add(connection_frame, text="Connection")
        connection_frame.columnconfigure(1, weight=1)

        # General Tab
        core_frame = ttk.Frame(notebook)
        notebook.add(core_frame, text="General")
        core_frame.columnconfigure(1, weight=1)

        # Advanced Tab
        advanced_frame = ttk.Frame(notebook)
        notebook.add(advanced_frame, text="Advanced")
        advanced_frame.columnconfigure(1, weight=1)

        # Shared grid options
        LBL  = dict(sticky="w",  padx=(12, 6), pady=4)
        WIDE = dict(sticky="we", padx=(0,  12), pady=4)
        SPAN = dict(sticky="w",  padx=(12, 12), pady=4, columnspan=2)

        def add_section_header(frame, text, row):
            """Draw a bold section header + separator at `row`, return the next free row."""
            tk.Label(frame, text=text, font=self.app_font(10, "bold")).grid(
                row=row, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=(10, 2)
            )
            row += 1
            ttk.Separator(frame, orient="horizontal").grid(
                row=row, column=0, columnspan=2, sticky="we", padx=12, pady=(0, 4)
            )
            return row + 1

        # --- Connection Tab ---
        conn_row = 0

        conn_row = add_section_header(connection_frame, "Connection", conn_row)

        # Username
        tk.Label(connection_frame, text="Username:", font=self.app_font(10)).grid(row=conn_row, column=0, **LBL)
        username_entry = tk.Entry(connection_frame, width=32)
        username_entry.insert(0, self.username)
        username_entry.grid(row=conn_row, column=1, **WIDE)
        conn_row += 1

        # Password
        tk.Label(connection_frame, text="Password:", font=self.app_font(10)).grid(row=conn_row, column=0, **LBL)
        password_entry = tk.Entry(connection_frame, width=32)
        password_entry.insert(0, self.password)
        password_entry.grid(row=conn_row, column=1, **WIDE)
        conn_row += 1

        # Video Path
        tk.Label(connection_frame, text="Video Path:", font=self.app_font(10)).grid(row=conn_row, column=0, **LBL)
        archive_entry = tk.Entry(connection_frame, width=32)
        archive_entry.insert(0, self.archive_dir)
        archive_entry.grid(row=conn_row, column=1, **WIDE)
        conn_row += 1

        conn_row = add_section_header(connection_frame, "Cameras", conn_row)

        # Camera IPs and settings
        ip_entries = []
        hq_checkboxes = []
        audio_checkboxes = []
        ptz_checkboxes = []
        for i in range(4):
            tk.Label(connection_frame, text=f"Cam {i+1} IP:", font=self.app_font(10)).grid(row=conn_row, column=0, **LBL)

            cam_frame = ttk.Frame(connection_frame)
            cam_frame.grid(row=conn_row, column=1, sticky="we", padx=(0, 12), pady=4)

            ip_entry = tk.Entry(cam_frame, width=16)
            ip_entry.insert(0, self.ips[i])
            ip_entry.pack(side="left")
            ip_entries.append(ip_entry)

            hq_var = tk.BooleanVar(value=self.hq_enabled[i])
            ttk.Checkbutton(cam_frame, text="HQ", variable=hq_var).pack(side="left", padx=(8, 0))
            hq_checkboxes.append(hq_var)

            audio_var = tk.BooleanVar(value=self.audio_enabled[i])
            ttk.Checkbutton(cam_frame, text="Audio", variable=audio_var).pack(side="left", padx=(8, 0))
            audio_checkboxes.append(audio_var)

            ptz_var = tk.BooleanVar(value=self.ptz_supported[i])
            ttk.Checkbutton(cam_frame, text="PTZ", variable=ptz_var).pack(side="left", padx=(8, 0))
            ptz_checkboxes.append(ptz_var)

            conn_row += 1

        # --- General Tab ---
        row = 0

        row = add_section_header(core_frame, "Appearance", row)

        THEME_LABELS = {"dark": "Dark", "light": "Light", "system": "Match System"}
        THEME_VALUES_BY_LABEL = {v: k for k, v in THEME_LABELS.items()}
        tk.Label(core_frame, text="Theme:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        theme_var = tk.StringVar(value=THEME_LABELS.get(self.ui_theme, "Dark"))
        ttk.Combobox(
            core_frame, textvariable=theme_var, values=list(THEME_LABELS.values()),
            state="readonly", width=16
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(core_frame, text="Font:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        font_var = tk.StringVar(value=self.ui_font)
        ttk.Combobox(
            core_frame, textvariable=font_var, values=self.font_choice_labels, state="readonly", width=16
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(core_frame, text="Player Controls:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        controls_position_var = tk.StringVar(value=self.controls_position)
        ttk.Combobox(
            core_frame, textvariable=controls_position_var,
            values=self.CONTROL_POSITIONS, state="readonly", width=16
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        fullscreen_buttons_var = tk.BooleanVar(value=self.enable_fullscreen_buttons)
        fullscreen_buttons_cb = ttk.Checkbutton(
            core_frame, text="Show Stream Buttons", variable=fullscreen_buttons_var
        )
        fullscreen_buttons_cb.grid(row=row, column=0, **SPAN)
        if sys.platform.startswith('win'):
            fullscreen_buttons_var.set(True)
            fullscreen_buttons_cb.configure(state="disabled")
            tk.Label(
                core_frame, text="Required on Windows",
                font=self.app_font(9), fg="#888888"
            ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        save_window_size_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(core_frame, text="Save Window Size", variable=save_window_size_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        row = add_section_header(core_frame, "Playback & Display", row)

        tk.Label(core_frame, text="PTZ Travel:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        ptz_resolution_var = tk.IntVar(value=self.ptz_resolution)
        ttk.Combobox(
            core_frame, textvariable=ptz_resolution_var, values=[1, 2, 3, 4, 5], state="readonly", width=6
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(core_frame, text="Playback Speed:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        playback_speed_var = tk.DoubleVar(value=self.default_playback_speed)
        ttk.Combobox(
            core_frame, textvariable=playback_speed_var, values=self.speed_cycle, state="readonly", width=6
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        resume_playback_var = tk.BooleanVar(value=self.resume_playback)
        ttk.Checkbutton(
            core_frame, text="Resume Archive Clips From Last Position", variable=resume_playback_var
        ).grid(row=row, column=0, **SPAN)
        row += 1

        exclusive_audio_var = tk.BooleanVar(value=self.exclusive_archive_audio)
        ttk.Checkbutton(
            core_frame, text="Exclusive Archive Audio (unmuting one clip mutes others)",
            variable=exclusive_audio_var
        ).grid(row=row, column=0, **SPAN)
        row += 1

        tk.Label(core_frame, text="Sleep Mode (min):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        sleep_mode_entry = tk.Entry(core_frame, width=10)
        sleep_mode_entry.insert(0, str(self.sleep_mode_minutes))
        sleep_mode_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        tk.Label(
            core_frame, text="Stop live streams when unfocused/minimized this long. 0 = disabled",
            font=self.app_font(9), fg="#888888"
        ).grid(row=row + 1, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=(0, 4))
        row += 2

        row = add_section_header(core_frame, "Events", row)

        motion_events_var = tk.BooleanVar(value=self.motion_triggered_events)
        ttk.Checkbutton(core_frame, text="Motion Triggered Events", variable=motion_events_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        tk.Label(core_frame, text="Event Overlap Window:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        event_overlap_var = tk.IntVar(value=self.event_overlap_window_mins)
        overlap_combo = ttk.Combobox(
            core_frame, textvariable=event_overlap_var, values=[1, 2, 3, 5], state="readonly", width=6
        )
        overlap_combo.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        tk.Label(core_frame, text="min", font=self.app_font(10)).grid(
            row=row, column=1, sticky="w", padx=(62, 0), pady=4
        )
        row += 1

        def _update_overlap_state(*_):
            overlap_combo.config(state="readonly" if motion_events_var.get() else "disabled")
        motion_events_var.trace_add("write", _update_overlap_state)
        _update_overlap_state()

        tk.Label(core_frame, text="Default Event Filter:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        default_filter_labels = [self.ALL_TYPES_LABEL] + [
            self.detection_type_label(cid) for cid in self.DETECTION_TYPE_LABELS
        ]
        current_default_label = (
            self.ALL_TYPES_LABEL if self.default_event_filter == "all"
            else self.detection_type_label(self.default_event_filter)
        )
        if current_default_label not in default_filter_labels:
            default_filter_labels.append(current_default_label)
        default_filter_var = tk.StringVar(value=current_default_label)
        ttk.Combobox(
            core_frame, textvariable=default_filter_var, values=default_filter_labels,
            state="readonly", width=16
        ).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        def _clear_events_cache():
            events_dir = self._events_dir()
            removed = 0
            errors = 0
            if os.path.isdir(events_dir):
                for root_dir, dirs, files in os.walk(events_dir):
                    for fname in files:
                        if fname.endswith(".json"):
                            try:
                                os.remove(os.path.join(root_dir, fname))
                                removed += 1
                            except Exception as e:
                                logging.warning(f"Could not remove events cache file: {e}")
                                errors += 1
            if errors:
                messagebox.showwarning(
                    "Events Cache",
                    f"Removed {removed} file(s), {errors} could not be deleted.",
                    parent=dialog
                )
            else:
                messagebox.showinfo(
                    "Events Cache",
                    f"Cleared {removed} cached event file(s)." if removed else "No cached event files found.",
                    parent=dialog
                )

        cache_row = tk.Frame(core_frame)
        cache_row.grid(row=row, column=0, columnspan=2, sticky="w", padx=(12, 12), pady=4)
        tk.Button(
            cache_row, text="Clear Events Cache", font=self.app_font(10),
            command=_clear_events_cache
        ).pack(side="left")
        tk.Label(
            cache_row, text="Forces a fresh scan next time Events are opened",
            font=self.app_font(9), fg="#888888"
        ).pack(side="left", padx=(10, 0))
        row += 1

        # --- Advanced Tab ---
        row = 0

        row = add_section_header(advanced_frame, "Stream Reliability", row)

        enable_retries_var = tk.BooleanVar(value=self.enable_retries)
        ttk.Checkbutton(advanced_frame, text="Enable Automatic Retries", variable=enable_retries_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        tk.Label(advanced_frame, text="Max Retry Attempts:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        max_retry_attempts_entry = tk.Entry(advanced_frame, width=10)
        max_retry_attempts_entry.insert(0, str(self.max_retry_attempts))
        max_retry_attempts_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Initial Backoff Delay (s):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        initial_backoff_delay_entry = tk.Entry(advanced_frame, width=10)
        initial_backoff_delay_entry.insert(0, str(self.initial_backoff_delay))
        initial_backoff_delay_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        row = add_section_header(advanced_frame, "Quality Downgrading", row)

        enable_quality_downgrade_var = tk.BooleanVar(value=self.enable_quality_downgrade)
        ttk.Checkbutton(advanced_frame, text="Enable Quality Downgrading", variable=enable_quality_downgrade_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        enable_auto_revert_hq_var = tk.BooleanVar(value=self.enable_auto_revert_hq)
        ttk.Checkbutton(advanced_frame, text="Enable Auto-Revert to HQ", variable=enable_auto_revert_hq_var).grid(
            row=row, column=0, **SPAN
        )
        row += 1

        tk.Label(advanced_frame, text="Frame Drop Threshold:", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        drop_threshold_entry = tk.Entry(advanced_frame, width=10)
        drop_threshold_entry.insert(0, str(self.drop_threshold))
        drop_threshold_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Frame Drop Window (s):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        drop_window_entry = tk.Entry(advanced_frame, width=10)
        drop_window_entry.insert(0, str(self.drop_window))
        drop_window_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Downgrade Cooldown (s):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        downgrade_cooldown_entry = tk.Entry(advanced_frame, width=10)
        downgrade_cooldown_entry.insert(0, str(self.downgrade_cooldown))
        downgrade_cooldown_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="Stability Period (s):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        stability_period_entry = tk.Entry(advanced_frame, width=10)
        stability_period_entry.insert(0, str(self.stability_period))
        stability_period_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        tk.Label(advanced_frame, text="No-Frame Timeout (s):", font=self.app_font(10)).grid(row=row, column=0, **LBL)
        no_frame_timeout_entry = tk.Entry(advanced_frame, width=10)
        no_frame_timeout_entry.insert(0, str(self.no_frame_timeout))
        no_frame_timeout_entry.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
        row += 1

        row = add_section_header(advanced_frame, "VLC Options", row)

        vlc_params = tk.Text(advanced_frame, width=45, height=3)
        vlc_params.insert("1.0", ' '.join(self.vlcparams or self.DEFAULT_VLC_PARAMS))
        vlc_params.grid(row=row, column=0, columnspan=2, sticky="we", padx=(12, 12), pady=4)
        row += 1

        ttk.Separator(advanced_frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="we", padx=12, pady=6
        )
        row += 1

        debug_var = tk.BooleanVar(value=self.config_debug)
        ttk.Checkbutton(advanced_frame, text="Enable Debug Logging", variable=debug_var).grid(
            row=row, column=0, **SPAN
        )

        # Save and Cancel Buttons
        button_frame = ttk.Frame(dialog)
        button_frame.pack(pady=10)

        tk.Button(
            button_frame, text="Save", width=10, font=self.app_font(10),
            command=lambda: self.save_streams(
                username_entry, password_entry, ip_entries,
                hq_checkboxes, audio_checkboxes, ptz_checkboxes,
                fullscreen_buttons_var, debug_var, archive_entry, vlc_params,
                ptz_resolution_var, save_window_size_var, dialog,
                enable_retries_var, max_retry_attempts_entry, initial_backoff_delay_entry,
                enable_quality_downgrade_var, drop_threshold_entry, drop_window_entry,
                downgrade_cooldown_entry, enable_auto_revert_hq_var, stability_period_entry,
                playback_speed_var, font_var, no_frame_timeout_entry, resume_playback_var,
                motion_events_var, event_overlap_var, exclusive_audio_var, default_filter_var,
                theme_var, controls_position_var, sleep_mode_entry
            )
        ).pack(side="left", padx=5)

        tk.Button(
            button_frame, text="Cancel", width=10, font=self.app_font(10),
            command=dialog.destroy
        ).pack(side="left", padx=5)

        dialog.update_idletasks()

    def save_streams(self, username_entry, password_entry, ip_entries, hq_checkboxes, audio_checkboxes, ptz_checkboxes, fullscreen_buttons_var, debug_var, archive_entry, vlc_params, ptz_resolution_var, save_window_size_var, dialog, enable_retries_var, max_retry_attempts_entry, initial_backoff_delay_entry, enable_quality_downgrade_var, drop_threshold_entry, drop_window_entry, downgrade_cooldown_entry, enable_auto_revert_hq_var, stability_period_entry, playback_speed_var, font_var=None, no_frame_timeout_entry=None, resume_playback_var=None, motion_events_var=None, event_overlap_var=None, exclusive_audio_var=None, default_filter_var=None, theme_var=None, controls_position_var=None, sleep_mode_entry=None):
        old_fullscreen_buttons = self.enable_fullscreen_buttons
        # Snapshot which streams were actually live (connected, not just
        # configured) before we touch any config, so saving doesn't force a
        # reconnect on cameras that weren't playing (e.g. disabled, in
        # archive mode, or in a failed/not-yet-connected state).
        was_playing = [
            bool(self.media_players[i]) and not self.is_archive_mode[i]
            for i in range(4)
        ]
        self.username = username_entry.get().strip()
        self.password = password_entry.get().strip()
        self.archive_dir = archive_entry.get().strip()
        self.ips = [e.get().strip() for e in ip_entries]
        self.hq_enabled = [v.get() for v in hq_checkboxes]
        self.audio_enabled = [v.get() for v in audio_checkboxes]
        self.ptz_supported = [v.get() for v in ptz_checkboxes]
        self.enable_fullscreen_buttons = fullscreen_buttons_var.get()
        self.config_debug = debug_var.get()
        if font_var is not None:
            chosen = font_var.get()
            self.ui_font = chosen if chosen in self.font_choice_labels else self.font_choice_labels[0]
        if theme_var is not None:
            theme_labels = {"Dark": "dark", "Light": "light", "Match System": "system"}
            self.ui_theme = theme_labels.get(theme_var.get(), "dark")
        if resume_playback_var is not None:
            self.resume_playback = resume_playback_var.get()
        if motion_events_var is not None:
            self.motion_triggered_events = motion_events_var.get()
        if event_overlap_var is not None:
            v = int(event_overlap_var.get())
            self.event_overlap_window_mins = v if v in (1, 2, 3, 5) else 1
        if exclusive_audio_var is not None:
            self.exclusive_archive_audio = exclusive_audio_var.get()
        if controls_position_var is not None:
            v = controls_position_var.get()
            self.controls_position = v if v in self.CONTROL_POSITIONS else "top-left"
        if sleep_mode_entry is not None:
            try:
                self.sleep_mode_minutes = int(sleep_mode_entry.get().strip())
                if self.sleep_mode_minutes < 0:
                    logging.warning(f"Invalid sleep_mode_minutes: {self.sleep_mode_minutes}, using default 0")
                    self.sleep_mode_minutes = 0
            except ValueError:
                logging.warning(f"Invalid sleep_mode_minutes input, using default 0")
                self.sleep_mode_minutes = 0
        if default_filter_var is not None:
            chosen_label = default_filter_var.get()
            if chosen_label == self.ALL_TYPES_LABEL:
                self.default_event_filter = "all"
            else:
                label_to_id = {self.detection_type_label(cid): cid for cid in self.DETECTION_TYPE_LABELS}
                self.default_event_filter = label_to_id.get(chosen_label, "all")
    
        # Save default playback speed
        try:
            self.default_playback_speed = float(playback_speed_var.get())
            if self.default_playback_speed not in self.speed_cycle:
                logging.warning(f"Invalid default_playback_speed: {self.default_playback_speed}, using default 1.0")
                self.default_playback_speed = 1.0
        except (ValueError, TypeError):
            logging.warning(f"Invalid default_playback_speed input, using default 1.0")
            self.default_playback_speed = 1.0

        # Save new stream reliability settings
        self.enable_retries = enable_retries_var.get()
        try:
            self.max_retry_attempts = int(max_retry_attempts_entry.get().strip())
            if self.max_retry_attempts < 1:
                logging.warning(f"Invalid max_retry_attempts: {self.max_retry_attempts}, using default 5")
                self.max_retry_attempts = 5
        except ValueError:
            logging.warning(f"Invalid max_retry_attempts input, using default 5")
            self.max_retry_attempts = 5
        try:
            self.initial_backoff_delay = float(initial_backoff_delay_entry.get().strip())
            if self.initial_backoff_delay <= 0:
                logging.warning(f"Invalid initial_backoff_delay: {self.initial_backoff_delay}, using default 2.0")
                self.initial_backoff_delay = 2.0
        except ValueError:
            logging.warning(f"Invalid initial_backoff_delay input, using default 2.0")
            self.initial_backoff_delay = 2.0
        self.enable_quality_downgrade = enable_quality_downgrade_var.get()
        try:
            self.drop_threshold = int(drop_threshold_entry.get().strip())
            if self.drop_threshold < 1:
                logging.warning(f"Invalid drop_threshold: {self.drop_threshold}, using default 8")
                self.drop_threshold = 8
        except ValueError:
            logging.warning(f"Invalid drop_threshold input, using default 8")
            self.drop_threshold = 8
        try:
            self.drop_window = float(drop_window_entry.get().strip())
            if self.drop_window <= 0:
                logging.warning(f"Invalid drop_window: {self.drop_window}, using default 30.0")
                self.drop_window = 30.0
        except ValueError:
            logging.warning(f"Invalid drop_window input, using default 30.0")
            self.drop_window = 30.0
        try:
            self.downgrade_cooldown = float(downgrade_cooldown_entry.get().strip())
            if self.downgrade_cooldown < 10:
                logging.warning(f"Invalid downgrade_cooldown: {self.downgrade_cooldown}, using default 120.0")
                self.downgrade_cooldown = 120.0
        except ValueError:
            logging.warning(f"Invalid downgrade_cooldown input, using default 120.0")
            self.downgrade_cooldown = 120.0
        self.enable_auto_revert_hq = enable_auto_revert_hq_var.get()
        try:
            self.stability_period = float(stability_period_entry.get().strip())
            if self.stability_period < 10:
                logging.warning(f"Invalid stability_period: {self.stability_period}, using default 300.0")
                self.stability_period = 300.0
        except ValueError:
            logging.warning(f"Invalid stability_period input, using default 300.0")
            self.stability_period = 300.0
        try:
            self.no_frame_timeout = float(no_frame_timeout_entry.get().strip())
            if self.no_frame_timeout < 5:
                logging.warning(f"Invalid no_frame_timeout: {self.no_frame_timeout}, using default 15.0")
                self.no_frame_timeout = 15.0
        except ValueError:
            logging.warning(f"Invalid no_frame_timeout input, using default 15.0")
            self.no_frame_timeout = 15.0

        try:
            ptz_resolution = ptz_resolution_var.get()
            if not isinstance(ptz_resolution, int) or ptz_resolution < 1 or ptz_resolution > 5:
                logging.warning(f"Invalid PTZ resolution: {ptz_resolution}, using default 3")
                ptz_resolution = 3
            self.ptz_resolution = ptz_resolution
        except Exception as e:
            logging.error(f"Failed to parse PTZ resolution: {e}, using default 3")
            self.ptz_resolution = 3

        # Handle VLC params from Text widget
        raw_params = vlc_params.get("1.0", "end-1c").strip()
        self.vlcparams = self.parse_vlcparams(raw_params)

        if save_window_size_var.get():
            if self.root.attributes("-fullscreen"):
                self.saved_window_size = "fullscreen"
            else:
                width = self.root.winfo_width()
                height = self.root.winfo_height()
                if width >= self.MIN_WIDTH and height >= self.MIN_HEIGHT:
                    self.saved_window_size = f"{width}x{height}"
                else:
                    logging.warning(f"Current window size {width}x{height} is below minsize {self.MIN_WIDTH}x{self.MIN_HEIGHT}, saving default")
                    self.saved_window_size = "1340x720"
         
        if self.debug_mode != self.config_debug:
            self.debug_mode = self.config_debug
            self._setup_logging(self.debug_mode)

        self.onvif_cams = {}
        self.ptz_click_counts = [0] * 4
        self.drop_timestamps = [[] for _ in range(4)]
        self.update_streams()
        self.save_config()
        self.apply_theme()

        # Update label bindings and rebuild config panel
        self.update_label_bindings()
        self.build_config_panel()
        self._rearm_sleep_mode_timer()

        dialog.destroy()
        threading.Thread(target=self.restart_previously_playing_streams, args=(was_playing,), daemon=True).start()

    def check_network_connectivity(self, ip_input):
        """
        Check if the camera at the given IP (or IP:port) is reachable on its RTSP port.
        
        Supports formats like:
        - 192.168.1.100
        - 192.168.1.100:8554
        - 192.168.1.100:8554/cam1/stream1   ← cleans /cam1/stream1 part
        """
        try:
            # Split on colon, but only the first one (in case username:password@host:port)
            if ':' in ip_input:
                parts = ip_input.split(':', 1)  # split only on first colon
                host = parts[0].strip()
                port_str = parts[1].strip()

                # Remove any trailing path (/cam1/stream1 etc.)
                if '/' in port_str:
                    port_str = port_str.split('/', 1)[0].strip()

                # Try to convert to integer port
                try:
                    port = int(port_str)
                    if not (1 <= port <= 65535):
                        port = 554  # invalid port → fallback
                        logging.warning(f"Invalid port value '{port_str}' for {host} → using default 554")
                except ValueError:
                    port = 554  # not a number → fallback
                    logging.warning(f"Non-numeric port '{port_str}' for {host} → using default 554")
            else:
                host = ip_input.strip()
                port = 554

            # Now perform the actual connection check
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            result = sock.connect_ex((host, port))
            sock.close()

            if result == 0:
                return True
            else:
                logging.warning(f"Network check: Camera at {host}:{port} is not reachable (error code: {result})")
                return False

        except Exception as e:
            logging.warning(f"Network check failed for '{ip_input}': {e}")
            return False

    def debounce_layout_update(self):
        """Debounce layout updates to prevent excessive calls."""
        if hasattr(self, '_layout_debounce_id'):
            self.root.after_cancel(self._layout_debounce_id)
        self._layout_debounce_id = self.root.after(100, self.update_layout)

    def update_stream(self, index: int) -> None:
        if not 0 <= index <= 3:
            return
        
        # Ensure self.streams is initialized with enough slots
        if len(self.streams) < 4:
            self.streams.extend([""] * (4 - len(self.streams)))
        
        ip = self.ips[index]
        hq = self.hq_enabled[index]
        
        # Generate the stream URL
        if ip and self.username and self.password:
            stream = f"rtsp://{self.username}:{self.password}@{ip}/stream{'2' if not hq else '1'}"
            # Check if stream is unique (not already in other indices)
            seen_urls = {s for i, s in enumerate(self.streams) if s and i != index}
            if stream in seen_urls:
                stream = ""
        else:
            stream = ""
        
        # Update the specific index
        self.streams[index] = stream
        logging.info(f"Updated stream at index {index}: {stream}")

    def update_streams(self):
        self.streams = []
        seen_urls = set()
        for ip, hq in zip(self.ips, self.hq_enabled):
            if ip and self.username and self.password:
                stream = f"rtsp://{self.username}:{self.password}@{ip}/stream{'2' if not hq else '1'}"
                if stream in seen_urls:
                    stream = ""
                else:
                    seen_urls.add(stream)
            else:
                stream = ""
            self.streams.append(stream)
        logging.info(f"Updated streams: {self.streams}")

    def create_icon(self, icon_type, opacity=1.0):
        size = (40, 40) if icon_type in ["config", "back", "left", "right", "up", "down", "fullscreen", "minimize", "play", "resize"] else (100, 100) if icon_type in ["folder", "archive", "back"] else (40, 40)
        img = Image.new("RGBA", size, (0, 0, 0, 255))
        draw = ImageDraw.Draw(img)
        
        # Helper function to adjust color opacity
        def adjust_color(color, opacity):
            if color == "white":
                return (255, 255, 255, int(255 * opacity))
            elif color == "black":
                return (0, 0, 0, int(255 * opacity))
            return color

        if icon_type == "config":
            draw.rectangle((18, 22, 22, 34), fill="white")
            draw.rectangle((12, 10, 28, 20), fill="white")
        elif icon_type == "fullscreen":
            draw.rectangle((8, 8, 32, 32), outline="white", width=2)
            draw.line((10, 10, 13, 10), fill="white", width=2)
            draw.line((10, 10, 10, 13), fill="white", width=2)
            draw.line((30, 30, 27, 30), fill="white", width=2)
            draw.line((30, 30, 30, 27), fill="white", width=2)
        elif icon_type == "minimize":
            draw.rectangle((8, 8, 32, 32), outline="white", width=2)
            draw.line((8, 20, 32, 20), fill="white", width=2)
            draw.line((20, 8, 20, 32), fill="white", width=2)
        elif icon_type == "pause":
            draw.rectangle((12, 8, 18, 32), fill="white")
            draw.rectangle((22, 8, 28, 32), fill="white")
        elif icon_type == "speed":
            draw.polygon([(10, 8), (20, 20), (10, 32)], fill="white")
            draw.polygon([(20, 8), (30, 20), (20, 32)], fill="white")
        elif icon_type == "replay":
            draw.arc((10, 10, 30, 30), start=45, end=315, fill="white", width=3)
            draw.polygon([(28, 12), (32, 16), (28, 20)], fill="white")
        elif icon_type == "rewind":
            draw.polygon([(30, 8), (20, 20), (30, 32)], fill="white")
            draw.polygon([(20, 8), (10, 20), (20, 32)], fill="white")
        elif icon_type == "exit":
            draw.line((12, 12, 28, 28), fill="white", width=3)
            draw.line((12, 28, 28, 12), fill="white", width=3)
        elif icon_type == "resize":
            draw.rectangle((10, 10, 30, 30), outline="white", width=2)
            draw.line((10, 10, 8, 8), fill="white", width=2)
            draw.line((10, 10, 12, 8), fill="white", width=2)
            draw.line((30, 30, 32, 32), fill="white", width=2)
            draw.line((30, 30, 28, 32), fill="white", width=2)
            draw.line((30, 10, 32, 8), fill="white", width=2)
            draw.line((30, 10, 28, 8), fill="white", width=2)
            draw.line((10, 30, 8, 32), fill="white", width=2)
            draw.line((10, 30, 12, 32), fill="white", width=2)
        elif icon_type == "folder":
            draw.rectangle((20, 30, 80, 80), fill=adjust_color("white", opacity), outline=adjust_color("white", opacity), width=3)
            draw.polygon([(20, 30), (30, 20), (40, 20), (40, 30)], fill=adjust_color("white", opacity), outline=adjust_color("white", opacity), width=3)
            draw.line((20, 30, 40, 50), fill=adjust_color("black", opacity), width=2)
        elif icon_type == "archive":
            draw.rectangle((20, 20, 80, 80), outline=adjust_color("white", opacity), width=2)
            draw.polygon([(35, 25), (35, 75), (75, 50)], fill=adjust_color("white", opacity), outline=adjust_color("white", opacity), width=3)
        elif icon_type == "play":
            draw.polygon([(12, 8), (32, 20), (12, 32)], outline="white", width=2, fill="white")
        elif icon_type == "disk":
            draw.rectangle([(8, 8), (32, 32)], outline="white", width=2, fill="black")
            draw.rectangle([(12, 10), (28, 18)], outline="white", width=1, fill="white")
            draw.rectangle([(16, 24), (24, 30)], outline="white", width=1, fill="white")      
        elif icon_type == "back":
            draw.polygon([(30, 10), (15, 20), (30, 30)], fill="white")
        elif icon_type == "events":
            # Calendar/lightning bolt: a small rectangle with a ⚡ inside
            draw.rectangle([(9, 11), (31, 31)], outline="white", width=2)
            draw.line([(9, 17), (31, 17)], fill="white", width=2)
            draw.line([(14, 9), (14, 14)], fill="white", width=2)
            draw.line([(26, 9), (26, 14)], fill="white", width=2)
            # lightning bolt inside calendar body
            draw.polygon([(22, 19), (18, 25), (21, 25), (18, 31), (24, 23), (21, 23)], fill="white")
        elif icon_type == "list":
            # Justified-list glyph: a bullet dot + horizontal rule on each row
            for row_y in (11, 20, 29):
                draw.ellipse([(8, row_y - 2), (12, row_y + 2)], fill="white")
                draw.line([(17, row_y), (32, row_y)], fill="white", width=3)
        elif icon_type == "delete":
            # Trash can
            draw.rectangle([(13, 14), (27, 31)], outline="white", width=2)
            draw.line([(10, 14), (30, 14)], fill="white", width=2)
            draw.line([(17, 11), (23, 11)], fill="white", width=2)
            draw.line([(17, 18), (17, 28)], fill="white", width=1)
            draw.line([(20, 18), (20, 28)], fill="white", width=1)
            draw.line([(23, 18), (23, 28)], fill="white", width=1)
        elif icon_type == "audio_on":
            # Speaker with sound waves
            draw.polygon([(10, 14), (10, 26), (16, 26), (22, 32), (22, 8), (16, 14)], fill="white")
            draw.arc([(23, 13), (31, 27)], start=300, end=60, fill="white", width=2)
            draw.arc([(25, 10), (35, 30)], start=300, end=60, fill="white", width=2)
        elif icon_type == "audio_off":
            # Speaker with X (muted)
            draw.polygon([(10, 14), (10, 26), (16, 26), (22, 32), (22, 8), (16, 14)], fill="white")
            draw.line([(25, 14), (33, 26)], fill="white", width=2)
            draw.line([(33, 14), (25, 26)], fill="white", width=2)
        elif icon_type == "left":
            draw.polygon([(30, 10), (15, 20), (30, 30)], fill="white")
        elif icon_type == "right":
            draw.polygon([(10, 10), (25, 20), (10, 30)], fill="white")
        elif icon_type == "up":
            draw.polygon([(10, 30), (20, 15), (30, 30)], fill="white")
        elif icon_type == "down":
            draw.polygon([(10, 10), (20, 25), (30, 10)], fill="white")
        return ImageTk.PhotoImage(img)

    ACTIVE_MODE_COLOR = "#e62117"

    def recolor_icon_active(self, photo_image):
        """Return a red-tinted variant of a cached white-on-black icon
        PhotoImage, used to indicate a mode button (Archive/Events) is
        currently active. Recolors white pixels to ACTIVE_MODE_COLOR while
        preserving alpha and leaving black/other pixels untouched."""
        img = ImageTk.getimage(photo_image).convert("RGBA")
        r, g, b = tuple(int(self.ACTIVE_MODE_COLOR[i:i + 2], 16) for i in (1, 3, 5))
        pixels = img.load()
        for y in range(img.height):
            for x in range(img.width):
                pr, pg, pb, pa = pixels[x, y]
                if pr > 200 and pg > 200 and pb > 200:
                    pixels[x, y] = (r, g, b, pa)
        return ImageTk.PhotoImage(img)

    def dim_icon(self, photo_image, opacity=0.55):
        """Return a faded variant of a cached icon PhotoImage, used to show
        a mode button (Archive/Events) as inactive/locked while disabled.
        Scales alpha only, leaving color untouched, so it reads as dimmed
        rather than recolored - this is used instead of relying on Tk's
        native disabled-image rendering, which is inconsistent across
        platforms/themes for plain (non-ttk) image buttons."""
        img = ImageTk.getimage(photo_image).convert("RGBA")
        pixels = img.load()
        for y in range(img.height):
            for x in range(img.width):
                pr, pg, pb, pa = pixels[x, y]
                pixels[x, y] = (pr, pg, pb, int(pa * opacity))
        return ImageTk.PhotoImage(img)

    def get_day_folder_icon(self, day_abbrev, is_clicked):
        #Return a (cached) folder icon with the weekday abbreviation.
        cache_key = (day_abbrev, is_clicked)
        cached = self.day_folder_icon_cache.get(cache_key)
        if cached is not None:
            return cached

        opacity = 0.6 if is_clicked else 1.0
        size = (100, 100)
        img = Image.new("RGBA", size, (0, 0, 0, 255))
        draw = ImageDraw.Draw(img)

        def adjust_color(color, opacity):
            if color == "white":
                return (255, 255, 255, int(255 * opacity))
            elif color == "black":
                return (0, 0, 0, int(255 * opacity))
            return color

        draw.rectangle((20, 30, 80, 80), fill=adjust_color("white", opacity),
                        outline=adjust_color("white", opacity), width=3)
        draw.polygon([(20, 30), (30, 20), (40, 20), (40, 30)],
                      fill=adjust_color("white", opacity), outline=adjust_color("white", opacity), width=3)

        font = None
        for font_path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
            "arialbd.ttf",
        ):
            try:
                font = ImageFont.truetype(font_path, 24)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        text = day_abbrev
        text_color = adjust_color("black", opacity)
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
        except Exception:
            text_w, text_h = draw.textsize(text, font=font)

        text_x = 20 + (60 - text_w) // 2
        text_y = 30 + (50 - text_h) // 2 - 2
        draw.text((text_x, text_y), text, fill=text_color, font=font)

        photo = ImageTk.PhotoImage(img)
        self.day_folder_icon_cache[cache_key] = photo
        return photo

    def bind_stream_label(self, index):
        # Bind the label for a given stream based on its state.
        try:
            # Unbind any existing click event
            self.labels[index].unbind("<Button-1>")

            # Check if stream is in failed state
            if self.labels[index].cget("text") == "Stream Failed, click to reconnect":
                # Bind retry action for failed streams
                self.labels[index].bind("<Button-1>", lambda event, idx=index: self.retry_stream_connection(idx))
            else:
                if self.event_mode:
                    return
                if not self.enable_fullscreen_buttons and self.streams[index]:
                    self.labels[index].bind("<Button-1>", lambda event, idx=index: self.handle_stream_click(idx))
        except Exception as e:
            logging.error(f"Stream {index}: Failed to bind stream label: {e}")

    def update_label_bindings(self):
        for i in range(4):
            self.bind_stream_label(i)

    def _apply_fullscreen_audio(self):
        """Unmute the audio-enabled, non-archive stream currently shown in
        fullscreen and mute all other audio-enabled, non-archive streams."""
        for i in range(4):
            if not self.streams[i] or not self.audio_enabled[i]:
                continue
            if self.is_archive_mode[i]:
                continue
            self.set_audio_state(i, mute=(i != self.fullscreen_index))

    def enter_fullscreen(self):
        if self.is_fullscreen:
            return
        for i in range(4):
            if self.streams[i]:
                self.is_fullscreen = True
                self.fullscreen_index = i
                self._apply_fullscreen_audio()
                self.build_config_panel()
                logging.info(f"Entered fullscreen mode for stream {i}")
                break

    def _return_to_event_listing(self):
        """Force-finish any currently playing event clips and return to the
        event listing overlay, without leaving event mode entirely. Shared
        by the right-click/Down-arrow exit_fullscreen path and the explicit
        'back to list' button (needed on Windows when 4 cams are streaming,
        since right-click over the embedded VLC surface may not be caught)."""
        playing = self.event_active_cams - self.event_done_cams
        if playing:
            # Cancel any pending delayed clip launches before stopping
            # players so no new playback starts after cleanup.
            for _after_id in self._pending_event_afters:
                try:
                    self.root.after_cancel(_after_id)
                except Exception:
                    pass
            self._pending_event_afters.clear()
            # Force-finish every cam that still has an active player.
            for i in list(playing):
                self.event_clip_queues[i] = []   # clear queue so no next-clip is started
                self.event_done_cams.add(i)
                self.cleanup_stream(i)
                for widget in self.labels[i].winfo_children():
                    widget.destroy()
                self._reset_clip_buttons(i)
                self.is_archive_mode[i] = False
                self._set_event_blank_label(i)

            # All cams are now done — mark event played and re-show overlay
            if self.current_playing_event:
                self.current_playing_event["played"] = True
                self._save_events_json(
                    getattr(self, "_event_date_for_save", None),
                    getattr(self, "_event_list_for_save", [])
                )
                try:
                    if self._event_played_label and self._event_played_label.winfo_exists():
                        self._event_played_label.configure(text="✓")
                except Exception:
                    pass

            # If a single-cam event entered fullscreen, drop back to grid
            # before re-showing the overlay so it centres over all panels.
            if getattr(self, '_event_entered_fullscreen', False):
                self.is_fullscreen = False
                self.fullscreen_index = -1
                self._event_entered_fullscreen = False
                self.update_layout()
                self.build_config_panel()
            else:
                # Multi-cam events stay in grid - still need to refresh so
                # the back-to-listing button (only shown while a clip is
                # actively playing) is hidden again now that we've stopped.
                self.build_config_panel()

            if self.event_overlay and self.event_overlay.winfo_exists():
                ow, oh = getattr(self, "_event_overlay_size", (820, 500))
                self.event_overlay.place(relx=0.5, rely=0.5, anchor="center", width=ow, height=oh)
                self.event_overlay.lift()
        else:
            # Overlay is already showing, no clips running — exit to live
            self._exit_event_mode()

    def exit_fullscreen(self, event=None):

        # Event mode intercept
        if self.event_mode:
            self._return_to_event_listing()
            return
        if self.is_fullscreen:
            idx = self.fullscreen_index

            if idx is not None and idx >= 0 and self.is_archive_mode[idx]:
                self.go_back(idx)
                return

            self.is_fullscreen = False
            self.fullscreen_index = -1

            for i in range(4):
                if self.audio_enabled[i]:
                    self.set_audio_state(i, mute=True)

            self.build_config_panel()
        else:
            any_archive_mode = False
            for i in range(4):
                if self.is_archive_mode[i]:
                    any_archive_mode = True
                    self.toggle_archive_mode(i)

            if any_archive_mode:
                self.build_config_panel()

    def build_config_panel(self):
        try:
            # Initialize config panel if not exists
            if not self.config_panel:
                self.config_panel = tk.Frame(self.root, bg="#222222", width=60)

            # Initialize buttons if not exists
            if not self.ptz_buttons:
                self.ptz_buttons = []
                self.ptz_images = []
                for direction in ["up", "down", "left", "right"]:
                    img = self.icon_cache[direction]
                    button = tk.Button(
                        self.config_panel, image=img, bg="#222222", bd=0, cursor="hand2",
                        command=lambda d=direction: self.start_ptz_move(d)
                    )
                    button.bind("<ButtonRelease-1>", lambda event, d=direction: self.stop_ptz_move(d))
                    self.ptz_buttons.append(button)
                    self.ptz_images.append(img)

            if not self.exit_fullscreen_button:
                self.exit_fullscreen_image = self.icon_cache["minimize"]
                self.exit_fullscreen_button = tk.Button(
                    self.config_panel, image=self.exit_fullscreen_image, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.exit_fullscreen, cursor="hand2"
                )

            if not self.config_button:
                self.config_img = self.icon_cache["config"]
                self.config_button = tk.Button(
                    self.config_panel, image=self.config_img, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.show_config_dialog, cursor="hand2"
                )

            if not self.archive_mode_button:
                self.archive_mode_image = self.icon_cache["disk"]
                self.archive_mode_button = tk.Button(
                    self.config_panel, image=self.archive_mode_image, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.toggle_all_archive_mode, cursor="hand2"
                )

            if not self.events_button:
                self.events_button_image = self.icon_cache["events"]
                self.events_button = tk.Button(
                    self.config_panel, image=self.events_button_image, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self.toggle_event_mode, cursor="hand2"
                )

            if not self.event_back_button:
                self.event_back_button_image = self.icon_cache["list"]
                self.event_back_button = tk.Button(
                    self.config_panel, image=self.event_back_button_image, bg="#222222", bd=0,
                    activebackground="#222222", relief="flat",
                    command=self._return_to_event_listing, cursor="hand2"
                )

            for i in range(4):
                if not self.fullscreen_buttons[i]:
                    img = self.icon_cache["fullscreen"]
                    self.fullscreen_buttons[i] = tk.Button(
                        self.panels[i], image=img, bg="black", bd=0, cursor="hand2",
                        command=lambda idx=i: self.handle_stream_click(idx),
                        state="disabled" if not self.enable_fullscreen_buttons else "normal"
                    )

            if self.is_fullscreen and self.fullscreen_index is not None:
                if not self.archive_buttons[self.fullscreen_index]:
                    img = self.icon_cache["disk"]
                    self.archive_buttons[self.fullscreen_index] = tk.Button(
                        self.config_panel, image=img, bg="#222222", bd=0, cursor="hand2",
                        command=lambda idx=self.fullscreen_index: self.toggle_archive_mode(idx)
                    )

            # Forget all buttons before re-packing
            for button in self.ptz_buttons + [self.exit_fullscreen_button, self.config_button, self.archive_mode_button] + \
                          ([self.events_button] if self.events_button else []) + \
                          ([self.event_back_button] if self.event_back_button else []) + \
                          [b for b in self.archive_buttons if b] + [b for b in self.fullscreen_buttons if b]:
                button.pack_forget()
                if button in self.fullscreen_buttons:
                    button.place_forget()

            # Update PTZ button states
            ptz_enabled = (self.is_fullscreen and self.fullscreen_index is not None and
                           self.ptz_supported[self.fullscreen_index] and not self.is_archive_mode[self.fullscreen_index])
            for button in self.ptz_buttons:
                button.config(state="normal" if ptz_enabled else "disabled")

            # Pack buttons based on state
            if self.is_fullscreen and self.fullscreen_index is not None and not self.event_mode:
                if (self.archive_dir and self.streams[self.fullscreen_index]):
                    self.archive_buttons[self.fullscreen_index].configure(
                        image=self.icon_cache["disk_active"] if self.is_archive_mode[self.fullscreen_index] else self.icon_cache["disk"]
                    )
                    self.archive_buttons[self.fullscreen_index].pack(pady=5, padx=10)
                if ptz_enabled:
                    for button in self.ptz_buttons:
                        button.pack(pady=5, padx=10)
                # The grid/exit-fullscreen button is redundant in fullscreen
                # archive mode - clicking the archive button already exits
                # archive mode (and drops back to grid), so only show it
                # for live fullscreen.
                if not self.is_archive_mode[self.fullscreen_index]:
                    self.exit_fullscreen_button.pack(pady=5, padx=10)
                # Offer the Events button in fullscreen too - both archive
                # and live - so the user can jump straight to Events without
                # backing out to grid first. Same button/behaviour as grid mode.
                if self.motion_triggered_events and self.archive_dir:
                    any_initializing = any(self.stream_initializing)
                    self.events_button.configure(
                        state="disabled" if any_initializing else "normal",
                        image=(self.icon_cache["events_active"] if self.event_mode
                               else self.icon_cache["events_disabled"] if any_initializing
                               else self.icon_cache["events"])
                    )
                    self.events_button.pack(pady=5, padx=10)
                self.config_button.pack(pady=5, padx=10)
            elif self.event_mode:
                # Event playback (single-cam events auto-enter fullscreen,
                # multi-cam events stay in grid) always shows the same nav:
                # Archive toggle, Events toggle, back-to-listing, and Config.
                # No PTZ or exit-fullscreen/grid button, regardless of
                # fullscreen state.
                any_initializing = any(self.stream_initializing)
                any_archive_mode = any(self.is_archive_mode[i] for i in range(4))
                if self.archive_dir:
                    self.archive_mode_button.configure(
                        state="disabled" if any_initializing else "normal",
                        image=(self.icon_cache["disk_active"] if any_archive_mode
                               else self.icon_cache["disk_disabled"] if any_initializing
                               else self.icon_cache["disk"])
                    )
                    self.archive_mode_button.pack(pady=5, padx=10)
                if self.motion_triggered_events and self.archive_dir:
                    self.events_button.configure(
                        state="disabled" if any_initializing else "normal",
                        image=self.icon_cache["events_active"]
                    )
                    self.events_button.pack(pady=5, padx=10)
                # Only relevant while a clip is actively playing - the
                # listing overlay itself is already visible otherwise.
                if self.event_active_cams - self.event_done_cams:
                    self.event_back_button.pack(pady=5, padx=10)
                self.config_button.pack(pady=10, padx=10)
            else:
                any_initializing = any(self.stream_initializing)
                any_archive_mode = any(self.is_archive_mode[i] for i in range(4))
                # Pack archive mode button only in grid mode if archive_dir is valid.
                if self.archive_dir:
                    self.archive_mode_button.configure(
                        state="disabled" if any_initializing else "normal",
                        image=(self.icon_cache["disk_active"] if any_archive_mode
                               else self.icon_cache["disk_disabled"] if any_initializing
                               else self.icon_cache["disk"])
                    )
                    self.archive_mode_button.pack(pady=5, padx=10)
                # Pack events button in grid mode if motion_triggered_events is on
                if self.motion_triggered_events and self.archive_dir:
                    self.events_button.configure(
                        state="disabled" if any_initializing else "normal",
                        image=(self.icon_cache["events_active"] if self.event_mode
                               else self.icon_cache["events_disabled"] if any_initializing
                               else self.icon_cache["events"])
                    )
                    self.events_button.pack(pady=5, padx=10)
                self.config_button.pack(pady=10, padx=10)
                # Place fullscreen buttons in grid mode. Fullscreen buttons
                # are never shown in event mode: single-cam events auto-enter
                # fullscreen, multi-cam events don't support it.
                for i in range(4):
                    if (self.enable_fullscreen_buttons and self.ips[i] and not self.is_archive_mode[i]
                            and not self.event_mode):
                        self.fullscreen_buttons[i].configure(state="normal")
                        self.fullscreen_buttons[i].place(relx=1.0, rely=1.0, x=-35, y=-35, anchor="se")
                        self.fullscreen_buttons[i].lift()
                    elif self.fullscreen_buttons[i]:
                        self.fullscreen_buttons[i].place_forget()

        except Exception:
            pass

    
    def iterate_streams(self, direction):
        if not self.is_fullscreen:
            return
        if self.fullscreen_index is None:
            return

        enabled_streams = [i for i in range(4) if self.streams[i]]
        if not enabled_streams:
            return

        try:
            current_pos = enabled_streams.index(self.fullscreen_index)
        except ValueError:
            return

        new_pos = (current_pos + direction) % len(enabled_streams)
        new_index = enabled_streams[new_pos]
        self.fullscreen_index = new_index

        for i in range(4):
            if not self.streams[i] or not self.audio_enabled[i]:
                continue
            if i == self.fullscreen_index and not self.is_archive_mode[i]:
                self.set_audio_state(i, mute=False)
            else:
                self.set_audio_state(i, mute=True)

        self.build_config_panel()
        self.debounce_layout_update()


    def init_ui(self):
        # Initialize the user interface, applying saved window size and centering.

        self.config_panel = tk.Frame(self.root, bg="#222222", width=60)
        self.config_panel.pack(side="right", fill="y")
        
        self.grid_frame = tk.Frame(self.root, bg="#222222")
        self.grid_frame.pack(fill="both", expand=True)

        initial_width, initial_height = 960, 540
        for i in range(4):
            panel = tk.Frame(self.grid_frame, bg="black")
            self.panels[i] = panel
            label = tk.Label(panel, bg="black", text="Disabled", fg="white")
            self.labels[i] = label
            label.pack(fill="both", expand=True)
            x = 0 if i in (0, 2) else initial_width + 5
            y = 0 if i in (0, 1) else initial_height + 5
            panel.place(x=x, y=y, width=initial_width, height=initial_height)
            self.panel_sizes[i] = (initial_width, initial_height)
          
            self.archive_buttons[i] = None  # Created in build_config_panel
            self.archive_canvas[i] = tk.Canvas(panel, bg="#222222", highlightthickness=0)
          
            self.fullscreen_buttons[i] = None  # Initialized later

        self.update_label_bindings()

        self.apply_window_size(self.saved_window_size)

        # Key bindings
        def handle_fullscreen_toggle(event):
            is_fullscreen = not self.root.attributes("-fullscreen")
            self.root.attributes("-fullscreen", is_fullscreen)

        self.root.bind("<Alt-Return>", handle_fullscreen_toggle)
        self.root.bind("<Shift_L>", handle_fullscreen_toggle)
        self.root.bind("<Up>", lambda e: self.enter_fullscreen())
        self.root.bind("<Down>", lambda e: self.exit_fullscreen())
        self.root.bind("<Button-3>", lambda e: self.exit_fullscreen())
        self.root.bind("<Left>", lambda e: self.iterate_streams(-1))
        self.root.bind("<Right>", lambda e: self.iterate_streams(1))
        self.root.bind("<Configure>", lambda e: self.debounce_layout_update())

        # Archive view navigation: Page Up/Down change page, Backspace
        # goes back to the parent folder, when in fullscreen archive mode.
        self.root.bind("<Prior>", lambda e: self.archive_change_page_shortcut(-1))   # Page Up
        self.root.bind("<Next>", lambda e: self.archive_change_page_shortcut(1))     # Page Down
        self.root.bind("<BackSpace>", lambda e: self.archive_go_back_shortcut())

        # Sleep mode: track focus and minimize/restore transitions on the
        # root window. Bound on root (not individual widgets) so it fires
        # regardless of which child currently has focus, on both platforms.
        self.root.bind("<FocusIn>", self._on_app_focus_in)
        self.root.bind("<FocusOut>", self._on_app_focus_out)
        self.root.bind("<Unmap>", self._on_app_unmap)
        self.root.bind("<Map>", self._on_app_map)

        # Initialize buttons via build_config_panel
        self.build_config_panel()

    # --- Sleep mode -----------------------------------------------------
    #
    # Tracks whether the app window currently has focus and/or is
    # minimized. Only counts as "unfocused" when both FocusOut has fired
    # AND the window isn't in the foreground - Unmap/Map (minimize/
    # restore) are tracked separately since a minimized window doesn't
    # reliably emit FocusOut/FocusIn on all platforms/window managers.
    # Either condition (unfocused or minimized) starts the countdown;
    # both must clear (focused again AND not minimized) to cancel it.

    def _on_app_focus_out(self, event=None):
        # Only the root window's own focus matters - child widgets
        # (dialogs, entries) losing focus to each other shouldn't trigger
        # this. Tkinter fires FocusOut/FocusIn on the toplevel too, so
        # filter to events targeting root itself.
        if event is not None and event.widget is not self.root:
            return
        self._app_focused = False
        self._maybe_start_sleep_timer()

    def _on_app_focus_in(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self._app_focused = True
        self._maybe_cancel_sleep_timer_and_wake()

    def _on_app_unmap(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self._app_minimized = True
        self._maybe_start_sleep_timer()

    def _on_app_map(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self._app_minimized = False
        self._maybe_cancel_sleep_timer_and_wake()

    def _maybe_start_sleep_timer(self):
        """Arm the sleep countdown if the app is now unfocused/minimized,
        sleep mode is enabled, and a timer isn't already running."""
        if self.sleep_mode_minutes <= 0:
            return
        if self._is_asleep or self._sleep_timer_id is not None:
            return
        if self._app_focused and not self._app_minimized:
            return
        delay_ms = int(self.sleep_mode_minutes * 60 * 1000)
        logging.info(f"Sleep mode: app unfocused/minimized, sleeping in {self.sleep_mode_minutes} min if it stays that way")
        self._sleep_timer_id = self.root.after(delay_ms, self._enter_sleep_mode)

    def _maybe_cancel_sleep_timer_and_wake(self):
        """Called when the app regains focus or is restored from minimize.
        Cancels any pending countdown, and if already asleep, wakes the
        streams back up."""
        # Still unfocused/minimized on the other axis - don't wake yet.
        if not self._app_focused or self._app_minimized:
            return
        if self._sleep_timer_id is not None:
            try:
                self.root.after_cancel(self._sleep_timer_id)
            except Exception:
                pass
            self._sleep_timer_id = None
            logging.info("Sleep mode: app refocused before timeout, countdown cancelled")
        if self._is_asleep:
            self._wake_from_sleep_mode()

    def _rearm_sleep_mode_timer(self):
        """Called after config is saved (sleep_mode_minutes may have
        changed). Cancels any pending timer and re-evaluates from the
        current focus/minimize state, so raising the value while already
        unfocused starts a fresh countdown, and setting it to 0 disables
        the feature immediately."""
        if self._sleep_timer_id is not None:
            try:
                self.root.after_cancel(self._sleep_timer_id)
            except Exception:
                pass
            self._sleep_timer_id = None
        if self.sleep_mode_minutes <= 0:
            # Disabled - if we were already asleep, wake immediately so
            # streams aren't left stopped indefinitely.
            if self._is_asleep:
                self._wake_from_sleep_mode()
            return
        self._maybe_start_sleep_timer()

    def _enter_sleep_mode(self):
        """Stop all currently-live (non-archive, non-event-mode) streams
        and remember which ones were running, so they can be restarted on
        wake. Archive browsing/playback and event mode are left alone -
        sleep mode only concerns the always-on live view."""
        self._sleep_timer_id = None

        # Double-check we're still actually unfocused/minimized - guards
        # against a race where focus returned just as the timer fired.
        if self._app_focused and not self._app_minimized:
            return
        if self._is_asleep:
            return

        if self.event_mode:
            logging.info("Sleep mode: skipping, event mode is active")
            return

        stopped = []
        for i in range(4):
            if self.is_archive_mode[i]:
                continue
            if not self.ips[i] or not self.streams[i]:
                continue
            if not self.media_players[i] and not self.stream_initializing[i]:
                continue
            stopped.append(i)

        if not stopped:
            logging.info("Sleep mode: no active live streams to stop")
            self._is_asleep = True
            self._sleep_stopped_indices = []
            return

        logging.info(f"Sleep mode: stopping live streams {stopped} after {self.sleep_mode_minutes} min unfocused/minimized")
        self._is_asleep = True
        self._sleep_stopped_indices = stopped

        for i in stopped:
            try:
                self.cleanup_stream(i)
            except Exception as e:
                logging.error(f"Sleep mode: error stopping stream {i}: {e}")
            self.update_stream_label(i, "Sleeping")
            if self.fullscreen_buttons[i]:
                self.root.after(0, lambda idx=i: self.fullscreen_buttons[idx].place_forget())

    def _wake_from_sleep_mode(self):
        """Restart streams that sleep mode stopped."""
        if not self._is_asleep:
            return
        self._is_asleep = False
        to_restart = self._sleep_stopped_indices
        self._sleep_stopped_indices = []

        if not to_restart:
            return

        logging.info(f"Sleep mode: waking, restarting streams {to_restart}")

        self.root.after(0, self._disable_stream_action_buttons)

        def _restart():
            threads = [
                threading.Thread(target=self.try_init_stream_with_retries, args=(i,), daemon=True)
                for i in to_restart if not self.is_archive_mode[i]
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.root.after(0, self.update_layout)
            self.root.after(0, self._reenable_stream_action_buttons)

        threading.Thread(target=_restart, daemon=True).start()

    def set_audio_state(self, index, mute=True):
        if not self.audio_enabled[index]:
            return

        # Check if stream is initializing
        with self.stream_init_lock:
            if self.stream_initializing[index]:
                return

        if self.media_players[index]:
            try:
                state = self.media_players[index].get_state()
                if state in (vlc.State.Error, vlc.State.Ended, vlc.State.Stopped):
                    return
                self.media_players[index].audio_set_mute(mute)
            except Exception as e:
                logging.error(f"Stream {index}: Failed to set python-vlc audio state: {e}")

    def update_stream_label(self, index, text, fg="white", image=""):
        """Update the stream label in a thread-safe manner."""
        try:
            self.root.after(0, lambda: self.labels[index].configure(image=image, text=text, fg=fg))
        except Exception as e:
            logging.error(f"Stream {index}: Failed to update label: {e}")

    def _set_event_blank_label(self, index):
        """Blank out a cam's label while in event mode (not currently
        playing a clip). Shows 'Disabled' - matching the normal disabled
        stream label's color - for cams with no configured IP/stream, or
        'Cam N' in the dimmed event-mode color otherwise, so a disabled
        cam doesn't come back from event mode mislabeled as just inactive."""
        if not self.ips[index] or not self.streams[index]:
            self.labels[index].configure(image="", text="Disabled", fg="white", bg="black")
        else:
            self.labels[index].configure(image="", text=f"Cam {index + 1}", fg="#888888", bg="black")

    def try_init_stream_with_retries(self, index):
        # Attempt to initialize a stream with retries, managing all label updates.
        with self.stream_init_lock:
            if self.stream_initializing[index]:
                logging.warning(f"Stream {index}: Already initializing, skipping retry")
                return False
            self.stream_initializing[index] = True

        try:
            if not self.ips[index] or not self.streams[index]:
                self.update_stream_label(index, "Disabled")
                if self.fullscreen_buttons[index]:
                    self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget())
                logging.info(f"Stream {index}: Disabled (no IP or URL)")
                return False

            max_attempts = self.max_retry_attempts if self.enable_retries else 1
            backoff_delay = self.initial_backoff_delay
            max_backoff = 30.0

            for attempt in range(max_attempts):
                # Check if we have been asked to abort (e.g. user switched to
                # archive mode while we were retrying or sleeping in backoff).
                if self.stream_cleanup_events[index].is_set():
                    logging.info(f"Stream {index}: Abort signal received, stopping init")
                    return False

                # On the final retry attempt, drop to LQ for this session only
                if attempt == max_attempts - 1 and self.enable_quality_downgrade and self.hq_enabled[index]:
                    logging.info(f"Stream {index}: Final retry, switching to low quality for this session")
                    self.hq_enabled[index] = False
                    self.update_stream(index)
                    self.update_stream_label(index, "Final attempt, trying Low Quality...")
                elif attempt > 0:
                    self.update_stream_label(index, f"Retrying... (Attempt {attempt+1}/{max_attempts})")
                else:
                    self.update_stream_label(index, "Loading...")

                logging.info(f"Stream {index}: Attempt {attempt+1}/{max_attempts}")

                if not self.check_network_connectivity(self.ips[index]):
                    logging.warning(f"Stream {index}: Network check failed")
                    if attempt == max_attempts - 1:
                        self.update_stream_label(index, "Network Unreachable")
                        if self.fullscreen_buttons[index]:
                            self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget())
                        logging.error(f"Stream {index}: Network unreachable after all attempts")
                        return False
                    time.sleep(backoff_delay)
                    backoff_delay = min(backoff_delay * 2, max_backoff)
                    continue

                self.cleanup_stream(index)
                if self.init_stream(index):
                    logging.info(f"Stream {index}: Initialized successfully")
                    self.set_audio_state(index, mute=True)
                    self.root.after(0, lambda idx=index: self.bind_stream_label(idx))
                    return True

                if self.stream_cleanup_events[index].is_set():
                    logging.info(f"Stream {index}: Init failed due to abort signal, yielding cleanup to signaller")
                    return False

                if attempt == max_attempts - 1:
                    self.cleanup_stream(index)
                    self.update_stream_label(index, "Stream Failed, click to reconnect")
                    self.bind_retry_connection(index)
                    if self.fullscreen_buttons[index]:
                        self.root.after(0, lambda: self.fullscreen_buttons[index].place_forget())
                    logging.error(f"Stream {index}: All attempts failed")
                    return False

                logging.info(f"Stream {index}: Attempt {attempt+1} failed, retrying in {backoff_delay:.2f}s")
                self.stream_cleanup_events[index].wait(timeout=backoff_delay)
                backoff_delay = min(backoff_delay * 2, max_backoff)

            return False
        except Exception as e:
            logging.error(f"Stream {index}: Unexpected error during retry: {e}")
            return False
        finally:
            with self.stream_init_lock:
                self.stream_initializing[index] = False

    def build_vlc_instance_args(self, extra_args=None):
        """Build the common libvlc instance argument list, with optional
        per-call extra args (e.g. archive-specific caching flags). """
        args = [
            '--no-video-title-show',
            '--rtsp-tcp',
            '--no-skip-frames',
            '--network-caching=2000',
            '--no-plugins-cache',
        ]
        if extra_args:
            args.extend(extra_args)
        args.extend(self.vlcparams)
        if sys.platform.startswith('win'):
            args.append('--aout=directsound')
        else:
            args.extend(['--aout=pulse', '--vout=gl'])
        if self.debug_mode:
            # verbose=1 (warnings+errors) instead of 2 (full debug) - level 2
            # floods the log with per-frame demux/codec chatter from libvlc
            # itself, which drowns out the app's own debug logging. Anything
            # actually useful (stream errors, RTSP issues) still comes
            # through at level 1; _vlc_log_handler also filters to
            # WARNING+ regardless of this setting.
            args.append('--verbose=1')
        return args

    def _vlc_log_handler(self, data, level, ctx, fmt, args):
        # LIBVLC levels: 0=DEBUG, 1=NOTICE, 2=WARNING, 3=ERROR. Only forward
        # WARNING+ to our log - DEBUG/NOTICE from libvlc is per-frame demux
        # and codec chatter that drowns out the app's own debug logging,
        # and this filter applies regardless of the --verbose level passed
        # to the instance.
        if level < 2:
            return
        try:
            buf = ctypes.create_string_buffer(2048)
            libc = ctypes.CDLL(None)
            libc.vsnprintf(buf, ctypes.c_size_t(len(buf)), fmt, args)
            message = buf.value.decode(errors="replace")
        except Exception:
            message = "<unformattable libvlc log message>"

        level_map = {
            2: logging.WARNING,  # LIBVLC_WARNING
            3: logging.ERROR,    # LIBVLC_ERROR
        }
        logging.log(level_map.get(level, logging.WARNING), f"libvlc: {message}")

    def attach_vlc_logging(self, instance):
        """Attach the libvlc log callback to an instance, if debug logging
        is enabled. Safe to call even if libvlc_vprintf/log_set are
        unavailable on this python-vlc version."""
        if not self.debug_mode:
            return
        try:
            log_cb = vlc.LogCb(self._vlc_log_handler)
            # Keep a reference so it isn't garbage-collected while in use
            self._vlc_log_cb = log_cb
            instance.log_set(log_cb, None)
        except Exception:
            pass

    def init_stream(self, index):
        """Initialize a stream using Python-VLC (libvlc auto-selects hardware decode if available)."""
        logging.info(f"Stream {index}: Initializing stream")
        try:
            xid = self.labels[index].winfo_id()
        except Exception as e:
            logging.error(f"Stream {index}: Failed to get window ID: {e}")
            return False

        timeout = 8
        start_wait = time.time()
        check_interval = 0.5
        required_frames = 5
        frame_times = []

        try:
            instance = vlc.Instance(self.build_vlc_instance_args(
                ['--live-caching=2000']
            ))
            if not instance:
                raise RuntimeError("Failed to create VLC instance")
            self.attach_vlc_logging(instance)
            self.vlc_instances[index] = instance
            player = instance.media_player_new()
            if not player:
                raise RuntimeError("Failed to create VLC media player")
            self.media_players[index] = player
            media = instance.media_new(self.streams[index])
            player.set_media(media)
            player.set_xwindow(xid) if sys.platform.startswith("linux") else player.set_hwnd(xid)

            if player.play() == -1:
                raise RuntimeError("Failed to start VLC player")

            try:
                player.audio_set_mute(True)
            except Exception:
                pass

            while time.time() - start_wait < timeout:
                if self.stream_cleanup_events[index].is_set():
                    logging.info(f"Stream {index}: Abort signal during frame wait, stopping init")
                    try:
                        if player.get_state() not in (vlc.State.Stopped, vlc.State.Ended, vlc.State.Error):
                            player.stop()
                        player.release()
                    except Exception as e:
                        logging.warning(f"Stream {index}: Error releasing player during abort: {e}")
                    self.media_players[index] = None
                    # Each stream now owns its instance outright, so we must
                    # also release it here - nothing else will.
                    try:
                        instance.release()
                    except Exception as e:
                        logging.warning(f"Stream {index}: Error releasing VLC instance during abort: {e}")
                    if self.vlc_instances[index] is instance:
                        self.vlc_instances[index] = None
                    return False
                stats = vlc.MediaStats()
                if player.get_media().get_stats(stats):
                    current_displayed = stats.displayed_pictures
                    new_frames = current_displayed - self.last_displayed_frames[index]
                    self.last_displayed_frames[index] = current_displayed
                    if new_frames > 0:
                        frame_times.append((time.time(), new_frames))
                        frame_times = [(t, f) for t, f in frame_times if time.time() - t < self.drop_window]
                        recent_frames = sum(f for _, f in frame_times)
                        if recent_frames >= required_frames:
                            for _ in range(5):
                                time.sleep(0.5)
                                width, height = player.video_get_size(0) or (0, 0)
                                if width > 0 and height > 0:
                                    self.frame_shapes[index] = (width, height)
                                    break
                            player.video_set_scale(0)
                            self.last_dropped_frames[index] = stats.lost_pictures
                            threading.Thread(target=self.monitor_stream, args=(index, player), daemon=True).start()
                            return True
                if player.get_state() in (vlc.State.Error, vlc.State.Ended):
                    raise RuntimeError("Stream encountered error or ended")
                time.sleep(check_interval)
            logging.warning(f"Stream {index}: No frames detected within {timeout}s")
            return False
        except Exception as e:
            logging.error(f"Stream {index}: Python-VLC initialization failed: {e}")
            return False

    def cleanup_stream(self, index):
        """Clean up stream resources."""
        logging.info(f"Stream {index}: Cleaning up")
        self.stream_cleanup_events[index].set()
        self._stop_hover_poll(index)

        try:
            # Stop media player
            if self.media_players[index]:
                try:
                    if self.media_players[index].get_state() not in (vlc.State.Stopped, vlc.State.Ended, vlc.State.Error):
                        self.media_players[index].stop()
                    self.media_players[index].release()
                except Exception as e:
                    logging.error(f"Stream {index}: Error releasing media player: {e}")
                self.media_players[index] = None

            # Release this stream's own VLC instance. Each stream owns its
            # instance exclusively (see vlc_instances in __init__), so there
            # is no cross-stream "still in use" check needed - releasing it
            # here can never affect another stream's player.
            if self.vlc_instances[index]:
                try:
                    self.vlc_instances[index].release()
                except Exception as e:
                    logging.error(f"Stream {index}: Error releasing VLC instance: {e}")
                self.vlc_instances[index] = None

            # Reset stream state
            self.frame_shapes[index] = (0, 0)
            self.drop_timestamps[index] = []
            self.last_dropped_frames[index] = 0
            self.last_displayed_frames[index] = 0

            logging.info(f"Stream {index}: Cleanup completed")
        except Exception as e:
            logging.error(f"Stream {index}: Cleanup failed: {e}")
        finally:
            self.stream_cleanup_events[index].clear()

    def _retry_stream_connection_thread(self, index):
        """Thread function to retry stream connection and restore bindings."""
        try:
            # Attempt to reinitialize the stream
            success = self.try_init_stream_with_retries(index)
            if success:
                logging.info(f"Stream {index}: Retry successful, restoring bindings")
                # Restore default label bindings (including fullscreen if enabled)
                self.root.after(0, lambda: self.bind_stream_label(index))
                # Update layout to ensure stream is displayed
                self.root.after(0, self.update_layout)
            else:
                logging.warning(f"Stream {index}: Retry failed, keeping retry binding")
                # Ensure retry binding remains
                self.root.after(0, lambda: self.bind_retry_connection(index))
        except Exception as e:
            logging.error(f"Stream {index}: Error during retry: {e}")
            # Restore retry binding on error
            self.root.after(0, lambda: self.bind_retry_connection(index))

    def retry_stream_connection(self, index):
        """Retry the stream connection and restore bindings if successful."""
        logging.info(f"Stream {index}: Retrying connection due to label click")
        # Start retry in a separate thread to avoid blocking the UI
        threading.Thread(target=self._retry_stream_connection_thread, args=(index,), daemon=True).start()

    def bind_retry_connection(self, index):
        """Bind the label to retry the stream connection."""
        try:
            # Unbind any existing click event
            self.labels[index].unbind("<Button-1>")
            # Bind retry action
            self.labels[index].bind("<Button-1>", lambda event, idx=index: self.retry_stream_connection(idx))
        except Exception as e:
            logging.error(f"Stream {index}: Failed to bind retry connection: {e}")

    def monitor_stream(self, index, player):
        #Monitor a live stream for frame drops and state changes.
        logging.info(f"Monitoring stream {index}")

        last_check = time.time()
        last_stream_switch = 0        # tracks when we last switched quality
        last_stable_time = time.time()
        last_frame_time = time.time()
        no_frame_timeout = self.no_frame_timeout

        while self.running and self.media_players[index]:
            # Wait for cleanup event or poll timeout
            if self.stream_cleanup_events[index].wait(timeout=1.0):
                logging.info(f"Stream {index}: Cleanup event set, stopping monitoring")
                break

            try:
                current_time = time.time()
                dropped_frames = 0
                displayed_frames = 0

                if player is None:
                    logging.error(f"Stream {index}: No player, exiting monitor")
                    break

                state = player.get_state()
                if state in (vlc.State.Ended, vlc.State.Error):
                    logging.error(f"Stream {index} stopped: {state}")
                    self.cleanup_stream(index)
                    self.update_stream_label(index, "Stream Failed, click to reconnect")
                    self.bind_retry_connection(index)
                    break

                if current_time - last_check >= 1.0:
                    try:
                        stats = vlc.MediaStats()
                        media_obj = player.get_media()
                        if media_obj.get_stats(stats):
                            current_displayed = stats.displayed_pictures
                            displayed_frames = current_displayed - self.last_displayed_frames[index]
                            self.last_displayed_frames[index] = current_displayed

                            current_dropped = stats.lost_pictures
                            dropped_frames = current_dropped - self.last_dropped_frames[index]
                            self.last_dropped_frames[index] = current_dropped

                            # Guard against counter resets producing negative deltas
                            displayed_frames = max(0, displayed_frames)
                            dropped_frames = max(0, dropped_frames)

                            if dropped_frames > 0:
                                self.drop_timestamps[index].append(current_time)

                            if displayed_frames > 0:
                                last_frame_time = current_time
                        else:
                            # Stats unavailable this tick — record as a drop event but
                            # do NOT advance last_frame_time so the no-frame timeout
                            # still fires if the stream is genuinely stalled.
                            self.drop_timestamps[index].append(current_time)
                    except Exception as e:
                        logging.warning(f"Stream {index}: Error fetching VLC stats: {e}")
                        self.drop_timestamps[index].append(current_time)

                    last_check = current_time

                # Prune drop window
                self.drop_timestamps[index] = [
                    t for t in self.drop_timestamps[index]
                    if current_time - t < self.drop_window
                ]

                # No-frame timeout
                if current_time - last_frame_time > no_frame_timeout:
                    logging.error(f"Stream {index}: No frames for {no_frame_timeout}s, marking failed")
                    self.cleanup_stream(index)
                    self.update_stream_label(index, "Stream Failed, click to reconnect")
                    self.bind_retry_connection(index)
                    break

                # Quality downgrade (session-only — no save_config)
                if (self.enable_quality_downgrade
                        and self.hq_enabled[index]
                        and len(self.drop_timestamps[index]) >= self.drop_threshold):
                    if current_time - last_stream_switch < self.downgrade_cooldown:
                        logging.warning(f"Stream {index}: Downgrade throttled by cooldown")
                        self.update_stream_label(index, "Waiting: Stream Unstable")
                        continue
                    logging.warning(f"Stream {index}: Excessive drops, downgrading to LQ for this session")
                    self.update_stream_label(index, "Switching to Low Quality...")
                    self.hq_enabled[index] = False
                    self.update_stream(index)
                    last_stream_switch = current_time
                    self.drop_timestamps[index].clear()
                    last_stable_time = current_time
                    self.try_init_stream_with_retries(index)
                    return

                # Auto-revert to HQ
                if (self.enable_auto_revert_hq
                        and not self.hq_enabled[index]
                        and current_time - last_stream_switch >= self.downgrade_cooldown):
                    if (current_time - last_stable_time >= self.stability_period
                            and len(self.drop_timestamps[index]) == 0):
                        logging.info(f"Stream {index}: Stable for {self.stability_period}s, reverting to HQ")
                        self.update_stream_label(index, "Reverting to High Quality...")
                        self.hq_enabled[index] = True
                        self.update_stream(index)
                        last_stream_switch = current_time
                        self.drop_timestamps[index].clear()
                        self.try_init_stream_with_retries(index)
                        return

                # Reset stability clock whenever a drop is recorded
                if self.drop_timestamps[index]:
                    last_stable_time = current_time

            except Exception as e:
                logging.error(f"Stream {index}: Monitoring error: {e}")
                self.update_stream_label(index, "Stream Failed")
                break

        logging.info(f"Stream {index} monitoring stopped")

    def _disable_stream_action_buttons(self, indices=None):
        # Disable the archive-mode and events buttons on the main thread,
        # while live streams reinitialize (guards against clicking either
        # button and racing the new player, which can segfault). Shown
        # dimmed/inactive unless that mode is actually still active - it
        # never should be on this path, since this is only reached after
        # exiting to live, but the check is kept for correctness.
        #
        # archive_mode_button and events_button both act on every stream at
        # once (toggle_all_archive_mode / toggle_event_mode loop all 4
        # indices), so they must stay disabled for the full duration
        # regardless of which indices are reinitializing.
        any_archive_mode = any(self.is_archive_mode[i] for i in range(4))
        if self.archive_mode_button:
            self.archive_mode_button.configure(
                state="disabled",
                image=self.icon_cache["disk_active"] if any_archive_mode else self.icon_cache["disk_disabled"]
            )
        if self.events_button:
            self.events_button.configure(
                state="disabled",
                image=self.icon_cache["events_active"] if self.event_mode else self.icon_cache["events_disabled"]
            )
        # The fullscreen-specific archive button (self.archive_buttons[i]) is
        # a separate widget from archive_mode_button above, and is only
        # otherwise refreshed by build_config_panel() - which doesn't run
        # synchronously on the archive-exit path. Without this, it would
        # keep showing its last image (red) for the whole lock duration.
        #
        # Unlike the two buttons above, toggle_archive_mode(idx) only ever
        # touches its own index, so only the indices actually reinitializing
        # need disabling here - a slow camera on index 2 has no bearing on
        # whether it's safe to click the fullscreen archive button for
        # index 0. Defaults to all 4 for callers that don't know/care which
        # indices are affected (e.g. a blanket disable before scanning).
        target_indices = range(4) if indices is None else indices
        for i in target_indices:
            btn = self.archive_buttons[i]
            if btn:
                btn.configure(
                    state="disabled",
                    image=self.icon_cache["disk_active"] if self.is_archive_mode[i] else self.icon_cache["disk_disabled"]
                )

    def _reenable_stream_action_buttons(self, indices=None):
        # archive_mode_button and events_button act on every stream at once,
        # so they're only safe to reenable once nothing anywhere is still
        # initializing - a caller finishing its own subset of streams
        # doesn't mean some other in-flight init (from a different call
        # site) isn't still racing against a fresh player elsewhere.
        any_initializing = any(self.stream_initializing)
        any_archive_mode = any(self.is_archive_mode[i] for i in range(4))
        if not any_initializing:
            if self.archive_mode_button and self.archive_dir:
                self.archive_mode_button.configure(
                    state="normal",
                    image=self.icon_cache["disk_active"] if any_archive_mode else self.icon_cache["disk"]
                )
            if self.events_button and self.motion_triggered_events and self.archive_dir:
                self.events_button.configure(
                    state="normal",
                    image=self.icon_cache["events_active"] if self.event_mode else self.icon_cache["events"]
                )
        target_indices = range(4) if indices is None else indices
        for i in target_indices:
            # Same reasoning for the per-panel button: only safe to reenable
            # index i's button once index i itself is no longer initializing.
            if self.stream_initializing[i]:
                continue
            btn = self.archive_buttons[i]
            if btn:
                btn.configure(
                    state="normal",
                    image=self.icon_cache["disk_active"] if self.is_archive_mode[i] else self.icon_cache["disk"]
                )

    def start_streams(self):

        self.root.after(0, self._disable_stream_action_buttons)

        threads = []
        for i in range(4):
            # Skip stream if it's in archive mode
            if self.is_archive_mode[i]:
                continue
            if self.ips[i]:
                thread = threading.Thread(target=self.try_init_stream_with_retries, args=(i,), daemon=True)
                threads.append(thread)
                thread.start()
        for thread in threads:
            thread.join()
        for i in range(4):
            # Skip updating target dims for streams in archive mode
            if self.is_archive_mode[i]:
                continue
            if self.media_players[i]:
                self.root.after(0, lambda idx=i: self.update_target_dims(idx))

        # All inits done — re-enable buttons that are appropriate for the
        # current config.
        self.root.after(0, self._reenable_stream_action_buttons)

    def restart_previously_playing_streams(self, was_playing):
        """Like start_streams, but only reinitializes streams that were
        actually connected and playing before the config dialog was saved.
        Used by save_streams so that saving config doesn't force a
        reconnect on cameras that weren't live (disabled, archive mode,
        never configured, or already in a failed state) - only ones that
        were genuinely interrupted by the save get restarted."""

        self.root.after(0, self._disable_stream_action_buttons)

        threads = []
        for i in range(4):
            # Skip stream if it's in archive mode
            if self.is_archive_mode[i]:
                continue
            # Only restart streams that were live before the save, and that
            # still have a valid IP/URL after the new config was applied.
            if was_playing[i] and self.ips[i] and self.streams[i]:
                thread = threading.Thread(target=self.try_init_stream_with_retries, args=(i,), daemon=True)
                threads.append(thread)
                thread.start()
        for thread in threads:
            thread.join()
        for i in range(4):
            # Skip updating target dims for streams in archive mode
            if self.is_archive_mode[i]:
                continue
            if self.media_players[i]:
                self.root.after(0, lambda idx=i: self.update_target_dims(idx))

        # All inits done — re-enable buttons that are appropriate for the
        # current config.
        self.root.after(0, self._reenable_stream_action_buttons)

    def update_layout(self):
        """Update the layout of panels based on current state."""
        # State dictionary
        current_state = {
            'is_fullscreen': self.is_fullscreen,
            'is_fullscreen_index': self.fullscreen_index,
            'window_size': (self.grid_frame.winfo_width(), self.grid_frame.winfo_height())
        }

        # Skip update if state hasn't changed
        if hasattr(self, 'last_layout_state') and self.last_layout_state == current_state:
            return

        # Check if window size changed
        size_changed = not hasattr(self, 'last_layout_state') or \
                       self.last_layout_state['window_size'] != current_state['window_size']

        self.last_layout_state = current_state

        # Update current window size
        x_offset = 60
        self.grid_frame.place(x=0, y=0, width=-x_offset, relwidth=1.0, relheight=1.0)
        w, h = self.grid_frame.winfo_width(), self.grid_frame.winfo_height()
        if w <= 10 or h <= 10:
            w, h = 1920, 1080  # Fallback dimensions

        # Debounced rendering for archive views if size changed
        if size_changed:
            self._debounced_render_archive_views()

        if self.is_fullscreen and self.fullscreen_index is not None:
            for i in range(4):
                if i == self.fullscreen_index:
                    self.panels[i].place(x=0, y=0, width=w, height=h)
                    self.panel_sizes[i] = (w, h)
                    self.update_target_dims(i)
                    if not self.is_archive_mode[i] and self.media_players[i]:
                        hwnd = self.labels[i].winfo_id()
                        self.media_players[i].set_hwnd(hwnd) if sys.platform.startswith("win") else self.media_players[i].set_xwindow(hwnd)
                else:
                    self.panels[i].place_forget()
                    if self.fullscreen_buttons[i]:
                        self.fullscreen_buttons[i].place_forget()
        else:
            ww, hh = w // 2, h // 2
            self.panel_sizes = [(ww, hh)] * 4
            self.panels[0].place(x=0, y=0, width=ww, height=hh)
            self.panels[1].place(x=ww, y=0, width=ww, height=hh)
            self.panels[2].place(x=0, y=hh, width=ww, height=hh)
            self.panels[3].place(x=ww, y=hh, width=ww, height=hh)
            for i in range(4):
                self.update_target_dims(i)

    @debounce(0.2)  # 200ms debounce
    def _debounced_render_archive_views(self):
        """Debounced rendering of archive views for panels in archive mode."""
        for i in range(4):
            if self.is_archive_mode[i] and self.current_archive_path[i]:
                self.render_archive_view(i)

    def update_target_dims(self, index):
        w, h = self.panel_sizes[index]
        if w <= 10 or h <= 10:
            self.target_dims[index] = (0, 0)
            return
        frame_w, frame_h = self.frame_shapes[index]
        if frame_w <= 0 or frame_h <= 0:
            self.target_dims[index] = (0, 0)
            return
        self.target_dims[index] = (w, h)

    def handle_stream_click(self, index):
        if not self.streams[index] and not self.is_archive_mode[index]:
            return

        if self.is_fullscreen and self.fullscreen_index == index:
            self.exit_fullscreen()
        else:
            self.is_fullscreen = True  # Set fullscreen state directly
            self.fullscreen_index = index
            self._apply_fullscreen_audio()

            self.build_config_panel()
        
        logging.info(f"{'Entered' if self.is_fullscreen else 'Exited'} fullscreen mode for stream {index} via click")


    def toggle_all_archive_mode(self):
        """Toggle archive mode for all active streams based on current state.

        Mode-button semantics (mirrors the Events button):
          - If Events mode is currently active, close it first, then enter
            Archive mode (switching between modes).
          - If Archive mode is currently active, close it (same as right-click).
          - Otherwise, enter Archive mode.
        """
        if not self.archive_dir:
            return

        if self.event_mode:
            # Switching from Events -> Archive: close the event overlay and
            # coordinated playback first, then fall through to enter archive.
            # Don't restart live streams here - we're about to put eligible
            # cams straight into archive mode, and any ineligible cams are
            # stopped explicitly below.
            self._exit_event_mode(restart_streams=False)

        # Check if any stream is in archive mode
        any_archive_mode = any(self.is_archive_mode[i] for i in range(4))

        if any_archive_mode:
            for i in range(4):
                if self.is_archive_mode[i] and not self.archive_transitioning[i]:
                    # rebuild_ui=True: let each stream's own exit-archive flow
                    # rebuild the config panel once its button lock lifts, so
                    # the archive icon stays red for the full duration of the
                    # restart (consistent with the Events button's timing)
                    # rather than flipping white immediately here.
                    self.toggle_archive_mode(i, rebuild_ui=True)
                    logging.info(f"Stream {i}: Exited archive mode via global toggle")
            return

        eligible = [i for i in range(4) if self.streams[i] and not self.is_archive_mode[i]]
        if not eligible:
            # Nothing to put into archive mode. If we just exited event mode
            # without restarting streams, restart them now so we don't leave
            # every cam stopped with no mode active.
            cams_to_restart = [i for i in range(4) if self.ips[i] and self.streams[i] and not self.media_players[i]]
            if cams_to_restart:
                threading.Thread(
                    target=lambda: [self.try_init_stream_with_retries(i) for i in cams_to_restart],
                    daemon=True
                ).start()
            return

        # Explicitly stop every other live stream (mirrors Events mode's
        # unconditional stop-all-cams behaviour on entry) so no live feed
        # keeps rendering/decoding behind the archive browser view.
        for i in range(4):
            if i not in eligible and not self.is_archive_mode[i] and self.media_players[i]:
                self.cleanup_stream(i)

        for i in eligible:
            if not self.archive_transitioning[i]:
                self.toggle_archive_mode(i, rebuild_ui=False)
                logging.info(f"Stream {i}: Entered archive mode via global toggle")
        self.build_config_panel()

    def toggle_archive_mode(self, index, rebuild_ui=True, restart_stream=True):
        if not self.archive_dir:
            return

        if self.archive_transitioning[index]:
            return
        self.archive_transitioning[index] = True

        if self.stream_initializing[index]:
            logging.info(f"Stream {index}: Init in progress, signalling abort for archive toggle")
            self.stream_cleanup_events[index].set()

        self.is_archive_mode[index] = not self.is_archive_mode[index]
        logging.info(f"Stream {index}: Archive mode {'enabled' if self.is_archive_mode[index] else 'disabled'}")

        if self.is_archive_mode[index]:
            self.labels[index].pack_forget()
            self.archive_canvas[index].pack(fill="both", expand=True)
            self.archive_canvas[index].delete("all")

            if self.archive_buttons[index]:
                self.archive_buttons[index].config(state="normal")
            if rebuild_ui:
                self.build_config_panel()

            if hasattr(self, "nav_buttons") and index < len(self.nav_buttons):
                for btn in self.nav_buttons[index].values():
                    btn.place_forget()
            if self.back_buttons[index]:
                self.back_buttons[index].place_forget()
            panel_width, panel_height = self.panel_sizes[index]
            self.archive_canvas[index].create_text(
                panel_width // 2, panel_height // 2,
                text="Loading...", fill="white", font=self.app_font(-16)
            )
            threading.Thread(target=self._enter_archive_mode_thread, args=(index,), daemon=True).start()
        else:
            with self.archive_entry_locks[index]:
                self.cleanup_archive_mode(index)

            if restart_stream:
                # Disable both action buttons while this stream re-initializes so
                # the user can't click archive/events and race against the new
                # player. The two global buttons (archive_mode_button/
                # events_button) still cover all 4 indices since they act on
                # every stream, but the per-panel archive button only needs
                # to cover this one index.
                self._disable_stream_action_buttons(indices=[index])

                # Start stream initialization in a separate thread. The
                # archive icon(s) are dimmed for the duration of the lock
                # by _disable_stream_action_buttons and only return to
                # white/active once _reenable_stream_action_buttons runs.
                def _init_and_reenable():
                    self.try_init_stream_with_retries(index)
                    def _on_done():
                        self._reenable_stream_action_buttons(indices=[index])
                        if rebuild_ui:
                            self.build_config_panel()
                    self.root.after(0, _on_done)

                threading.Thread(target=_init_and_reenable, args=(), daemon=True).start()
            else:
                logging.info(f"Stream {index}: Archive mode exited without restarting live stream (mode switch)")
                if rebuild_ui:
                    self.build_config_panel()

            # Exit is fully synchronous from here, so the transition is
            # over now - clear the flag so the next toggle is accepted.
            self.archive_transitioning[index] = False

    def _enter_archive_mode_thread(self, index):
        with self.archive_entry_locks[index]:
            self._enter_archive_mode_thread_locked(index)

    def _enter_archive_mode_thread_locked(self, index):
        """Actual body of _enter_archive_mode_thread, called with
        archive_entry_locks[index] already held."""
      
        deadline = time.time() + 10.0
        while self.stream_initializing[index] and time.time() < deadline:
            time.sleep(0.05)
        if self.stream_initializing[index]:
            logging.warning(f"Stream {index}: Init did not stop within timeout, proceeding anyway")
        self.cleanup_stream(index)

        root_path = os.path.normpath(os.path.join(self.archive_dir, f"cam{index+1}"))
        try:
            exists = os.path.isdir(root_path)
        except Exception as e:
            logging.warning(f"Stream {index}: Error accessing archive directory {root_path}: {e}")
            exists = False

        def finish():
            try:
                if not self.is_archive_mode[index]:
                    # User toggled back out while we were waiting
                    return
                if not exists:
                    self.archive_canvas[index].delete("all")
                    panel_width, panel_height = self.panel_sizes[index]
                    self.archive_canvas[index].create_text(
                        panel_width // 2, panel_height // 2,
                        text="Archive directory not found", fill="white", font=self.app_font(-16)
                    )
                    return
                self.pagination_state[index] = {root_path: 0}
                self.current_archive_path[index] = root_path
                self.render_archive_view(index)
            finally:
                # The entry transition is fully resolved now (canvas shows
                # either the browser or an error state) - accept new toggles.
                self.archive_transitioning[index] = False

        self.root.after(0, finish)


    def get_cached_thumbnail(self, thumbnail_path, width, height):
        try:
            mtime = os.path.getmtime(thumbnail_path)
        except OSError:
            mtime = None
        key = (thumbnail_path, width, height, mtime)

        cached = self.thumbnail_cache.get(key)
        if cached is not None:
            return cached

        with Image.open(thumbnail_path) as img:
            img = img.resize((width, height), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img)

        self.thumbnail_cache[key] = photo
        self.thumbnail_cache_order.append(key)
        if len(self.thumbnail_cache_order) > self.thumbnail_cache_max:
            oldest = self.thumbnail_cache_order.pop(0)
            self.thumbnail_cache.pop(oldest, None)

        return photo

    def draw_progress_bar(self, index, x, y, width, height, progress):
        BORDER_INSET = 1
        bar_height = 4
        bar_x = x + BORDER_INSET
        bar_width = width - (BORDER_INSET * 2)
        bar_bottom = y + height - BORDER_INSET
        bar_y = bar_bottom - bar_height

        duration = progress.get("duration", 0)
        if not duration or duration <= 0:
            return
        fraction = max(0.0, min(1.0, progress.get("position", 0) / duration))

        # Track (unwatched portion background)
        self.archive_canvas[index].create_rectangle(
            bar_x, bar_y, bar_x + bar_width, bar_bottom,
            fill="#3d3d3d", outline=""
        )
        # Filled (watched) portion
        if fraction > 0:
            self.archive_canvas[index].create_rectangle(
                bar_x, bar_y, bar_x + (bar_width * fraction), bar_bottom,
                fill="#e62117", outline=""
            )

    def render_archive_view(self, index):
        # Hide nav buttons before clearing so they never appear on a
        # loading/transitioning canvas.
        if hasattr(self, "nav_buttons") and index < len(self.nav_buttons):
            if "prev" in self.nav_buttons[index]:
                self.nav_buttons[index]["prev"].place_forget()
            if "next" in self.nav_buttons[index]:
                self.nav_buttons[index]["next"].place_forget()

        # Clear canvas
        self.archive_canvas[index].delete("all")

        # Initialize pagination state for the current path if not set
        path = os.path.normpath(self.current_archive_path[index])
        if path not in self.pagination_state[index]:
            self.pagination_state[index][path] = 0

        # Calculate layout parameters
        panel_width, panel_height = self.panel_sizes[index]
        icon_size = 100  # Default icon size (width in pixels)
        text_height = 30  # Approximate height for text labels
        item_width = icon_size + 20  # Icon + horizontal padding
        item_height = icon_size + text_height + 20  # Icon + text + vertical padding (default)
        margin_x, margin_y = 20, 60  # Left/top margins (top includes space for buttons and location text)
        
        # Check if path is a day folder (YYYY-MM-DD) and adjust icon size if thumbnails exist
        is_day_folder = re.match(r".*\d{4}-\d{2}-\d{2}$", path)
        thumbnail_width = None
        thumbnail_height = None
        use_thumbnails = False
        if is_day_folder:
            try:
                # Check for thumbnails in the 'thumbnails' subdirectory
                thumbnails_dir = os.path.join(path, "thumbnails")
                if os.path.isdir(thumbnails_dir):
                    items = os.listdir(path)
                    mp4_files = [item for item in items if item.endswith(".mp4")]
                    if mp4_files:
                        # Get the width and height of the first thumbnail (assume all are the same)
                        for mp4_file in mp4_files:
                            base_name = os.path.splitext(mp4_file)[0]
                            thumbnail_path = os.path.join(thumbnails_dir, f"{base_name}.jpg")
                            if os.path.exists(thumbnail_path):
                                with Image.open(thumbnail_path) as img:
                                    original_width, original_height = img.size
                                break
                        else:
                            original_width, original_height = None, None
                        if original_width and original_height:
                            # --- Modified: Set thumbnail height to 120px in grid mode ---
                            if not self.is_fullscreen:
                                thumbnail_height = 120  # Use 120px height in grid mode
                                # Calculate width to maintain aspect ratio
                                aspect_ratio = original_width / original_height
                                thumbnail_width = int(thumbnail_height * aspect_ratio)
                            else:
                                # In fullscreen mode, use original dimensions or scale appropriately
                                thumbnail_width, thumbnail_height = original_width, original_height
                            icon_size = thumbnail_width
                            item_width = icon_size + 20
                            item_height = thumbnail_height + text_height + 20  # Use actual thumbnail height
                            use_thumbnails = True
            except Exception as e:
                logging.warning(f"Stream {index}: Failed to check thumbnails in {path}: {e}")

        # Calculate number of columns and rows
        max_columns = (panel_width - 2 * margin_x) // item_width
        max_rows = (panel_height - margin_y - 20) // item_height  # 20 for bottom padding
        items_per_page = max_columns * max_rows
        if items_per_page < 1:
            items_per_page = 1  # Ensure at least one item per page
        self.items_per_page = items_per_page

        # Back button
        if not self.back_buttons[index]:
            back_img = self.icon_cache["back"]
            self.back_buttons[index] = tk.Button(
                self.archive_canvas[index],
                image=back_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.go_back(index)
            )
            self.back_buttons[index].image = back_img
        self.back_buttons[index].place(x=31, y=5)

        # List and sort items
        if not os.path.isdir(path):
            logging.error(f"Stream {index}: Path {path} is not a directory")
            return

        try:
            items = os.listdir(path)
            # Exclude 'thumbnails' directory in day folders
            if is_day_folder:
                items = [item for item in items if item != "thumbnails"]
        except Exception as e:
            logging.error(f"Stream {index}: Failed to list directory {path}: {e}")
            return
            
        # Sort function for both folders and videos
        def get_sort_key(item):
            try:
                full_path = os.path.join(path, item)
                if os.path.isdir(full_path):
                    # Folders (in root path) are sorted descending
                    return datetime.strptime(item, "%Y-%m-%d")
                elif item.endswith(".mp4"):
                    # Files (in folder path) are sorted ascending.
                    # Supports both old (HH-MM) and new (HH-MM-SS) filename formats.
                    match = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}(?:-\d{2})?)_(\d+m-\d+s)\.mp4$", item)
                    if not match:
                        logging.warning(f"Stream {index}: Invalid video format for {item}")
                        return datetime.min
                    date_str, time_str, _ = match.groups()
                    date_time = f"{date_str} {time_str.replace('-', ':')}"
                    fmt = "%Y-%m-%d %H:%M:%S" if time_str.count('-') == 2 else "%Y-%m-%d %H:%M"
                    return datetime.strptime(date_time, fmt)
                return datetime.min
            except Exception as e:
                logging.warning(f"Stream {index}: Failed to parse item {item}: {e}")
                return datetime.min

        # Determine sort order based on whether path is the root (folders) or subfolder (files)
        folder_path = os.path.normpath(os.path.join(self.archive_dir, f"cam{index+1}"))
        is_folder_path = os.path.normpath(path) == folder_path
        sorted_items = sorted(items, key=get_sort_key, reverse=is_folder_path)

        # Pagination logic
        total_items = len(sorted_items)
        total_pages = (total_items + items_per_page - 1) // items_per_page
        current_page = max(0, min(self.pagination_state[index][path], total_pages - 1))
        self.pagination_state[index][path] = current_page
        start_idx = current_page * items_per_page
        end_idx = min(start_idx + items_per_page, total_items)
        page_items = sorted_items[start_idx:end_idx]

        # Set left-aligned x-coordinate for items
        start_x = margin_x  # Fixed starting x-position (left margin)
        x, y = start_x, margin_y
        column = 0

        # Display location
        cam_index = path.find("/cam")
        if cam_index != -1:
            location = path[cam_index:].replace("/", " / ")
            location = re.sub(r'(?i)\bcam(\d+)\b', lambda m: 'CAM ' + m.group(1), location).upper()
            self.archive_canvas[index].create_text(
                80, 25, anchor="w", text=f"{location}", fill="white", font=self.app_font(-17)
            )

        # Render items for the current page
        page_images = []
        for item in page_items:
            full_path = os.path.join(path, item)
            is_visited = os.path.normpath(full_path) in self.visited_folders[index]
            progress = self.watch_progress[index].get(full_path)

            if os.path.isdir(full_path):
                # Render folder icon. For day folders (YYYY-MM-DD)
                day_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", item)
                if day_match:
                    try:
                        folder_date = datetime.strptime(item, "%Y-%m-%d")
                        day_abbrev = folder_date.strftime("%a")  # Mon, Tue, ...
                        folder_img = self.get_day_folder_icon(day_abbrev, is_visited)
                    except Exception:
                        folder_img = self.icon_cache["folder_clicked" if is_visited else "folder"]
                else:
                    folder_img = self.icon_cache["folder_clicked" if is_visited else "folder"]

                folder_id = self.archive_canvas[index].create_image(x + item_width // 2, y + icon_size // 2, image=folder_img)
                if day_match:
                    text_id = self.archive_canvas[index].create_text(
                        x + item_width // 2, y + icon_size + 10, text=item, fill="white", font=self.app_font(-17), anchor="n"
                    )
                else:
                    text_id = self.archive_canvas[index].create_text(
                        x + item_width // 2, y + icon_size + 10, text=item[:10], fill="white", font=self.app_font(-17), anchor="n"
                    )

                # Bind click event
                for id_ in (folder_id, text_id):
                    self.archive_canvas[index].tag_bind(
                        id_, "<Button-1>",
                        lambda e, p=full_path: self.open_folder(index, p)
                    )
                    # Bind hover events for hand2 cursor
                    self.archive_canvas[index].tag_bind(
                        id_, "<Enter>",
                        lambda e: self.archive_canvas[index].config(cursor="hand2")
                    )
                    self.archive_canvas[index].tag_bind(
                        id_, "<Leave>",
                        lambda e: self.archive_canvas[index].config(cursor="")
                    )

                page_images.append(folder_img)

            elif item.endswith(".mp4"):
                # Render video file (thumbnail or icon)
                if use_thumbnails:
                    # Load thumbnail from thumbnails directory
                    base_name = os.path.splitext(item)[0]
                    thumbnail_path = os.path.join(path, "thumbnails", f"{base_name}.jpg")
                    if os.path.exists(thumbnail_path):
                        try:
                            video_img = self.get_cached_thumbnail(thumbnail_path, thumbnail_width, thumbnail_height)
                            video_id = self.archive_canvas[index].create_image(
                                x + item_width // 2, y + thumbnail_height // 2, image=video_img
                            )
                            page_images.append(video_img)
                            thumb_x = x + (item_width - thumbnail_width) // 2
                            border_id = self.archive_canvas[index].create_rectangle(
                                thumb_x, y, thumb_x + thumbnail_width, y + thumbnail_height,
                                outline='#ffffff', width=2
                            )
                            if progress:
                                self.draw_progress_bar(
                                    index, thumb_x, y, thumbnail_width, thumbnail_height, progress
                                )
                            # Bind click and hover events on the thumbnail image itself
                            self.archive_canvas[index].tag_bind(
                                video_id, "<Button-1>",
                                lambda e, p=full_path: self.play_archive_video(index, p)
                            )
                            self.archive_canvas[index].tag_bind(
                                video_id, "<Enter>",
                                lambda e: self.archive_canvas[index].config(cursor="hand2")
                            )
                            self.archive_canvas[index].tag_bind(
                                video_id, "<Leave>",
                                lambda e: self.archive_canvas[index].config(cursor="")
                            )
                        except Exception as e:
                            logging.warning(f"Stream {index}: Failed to load thumbnail {thumbnail_path}: {e}")
                            # Fallback to cached icon
                            video_img = self.icon_cache["archive"]
                            video_id = self.archive_canvas[index].create_image(
                                x + item_width // 2, y + icon_size // 2, image=video_img
                            )
                            page_images.append(video_img)
                            if progress:
                                icon_x = x + (item_width - icon_size) // 2
                                self.draw_progress_bar(
                                    index, icon_x, y, icon_size, icon_size, progress
                                )
                    else:
                        # Fallback to cached icon
                        video_img = self.icon_cache["archive"]
                        video_id = self.archive_canvas[index].create_image(
                            x + item_width // 2, y + icon_size // 2, image=video_img
                        )
                        page_images.append(video_img)
                        if progress:
                            icon_x = x + (item_width - icon_size) // 2
                            self.draw_progress_bar(
                                index, icon_x, y, icon_size, icon_size, progress
                            )
                else:
                    # Render cached video icon
                    video_img = self.icon_cache["archive"]
                    video_id = self.archive_canvas[index].create_image(
                        x + item_width // 2, y + icon_size // 2, image=video_img
                    )
                    page_images.append(video_img)
                    if progress:
                        icon_x = x + (item_width - icon_size) // 2
                        self.draw_progress_bar(
                            index, icon_x, y, icon_size, icon_size, progress
                        )

                # Label shows HH:MM (and clip duration) for both old HH-MM
                # and new HH-MM-SS filename formats.
                time_parts = item.split('_')[1].split('-')[:2]
                time_label = ':'.join(time_parts)
                dur_label  = item.split('_')[2].split('.')[0].replace('-', '')
                label = f"{time_label} {dur_label}"
                text_id = self.archive_canvas[index].create_text(
                    x + item_width // 2, y + (thumbnail_height if use_thumbnails else icon_size) + 10, text=label, fill="white", font=self.app_font(-17), anchor="n"
                )

                # Bind click and hover events for video image and text
                for id_ in (video_id, text_id):
                    self.archive_canvas[index].tag_bind(
                        id_, "<Button-1>",
                        lambda e, p=full_path: self.play_archive_video(index, p)
                    )
                    self.archive_canvas[index].tag_bind(
                        id_, "<Enter>",
                        lambda e: self.archive_canvas[index].config(cursor="hand2")
                    )
                    self.archive_canvas[index].tag_bind(
                        id_, "<Leave>",
                        lambda e: self.archive_canvas[index].config(cursor="")
                    )
            
            column += 1
            x += item_width
            if column >= max_columns:
                x = start_x
                y += item_height
                column = 0

        # Keep references to all PhotoImages for this page so they aren't
        # garbage-collected, in a single assignment rather than rebuilding
        # the list on every item.
        self.archive_canvas[index].images = page_images

        # Navigation buttons (Previous/Next) and pagination text
        if not hasattr(self, 'nav_buttons'):
            self.nav_buttons = [{} for _ in range(len(self.panel_sizes))]

        if total_pages > 1:
            last_column_x = 5 + margin_x + (max_columns - 1) * item_width + item_width // 2

            # Previous button
            if "prev" not in self.nav_buttons[index]:
                prev_img = self.icon_cache["left"]
                self.nav_buttons[index]["prev"] = tk.Button(
                    self.archive_canvas[index],
                    image=prev_img,
                    bg="#222222",
                    bd=0,
                    cursor="hand2",
                    command=lambda: self.change_page(index, -1)
                )
                self.nav_buttons[index]["prev"].image = prev_img
            self.nav_buttons[index]["prev"].place(x=last_column_x - 40, y=5)
            self.nav_buttons[index]["prev"].config(state="normal" if current_page > 0 else "disabled")

            # Next button
            if "next" not in self.nav_buttons[index]:
                next_img = self.icon_cache["right"]
                self.nav_buttons[index]["next"] = tk.Button(
                    self.archive_canvas[index],
                    image=next_img,
                    bg="#222222",
                    bd=0,
                    cursor="hand2",
                    command=lambda: self.change_page(index, 1)
                )
                self.nav_buttons[index]["next"].image = next_img
            self.nav_buttons[index]["next"].place(x=last_column_x, y=5)
            self.nav_buttons[index]["next"].config(state="normal" if current_page < total_pages - 1 else "disabled")

            # Pagination text
            pagination_x = last_column_x - 100 
            pagination_y = 30
            self.archive_canvas[index].create_text(
                pagination_x,
                25,
                text=f"PAGE {current_page + 1}/{total_pages}",
                fill="white",
                font=self.app_font(-17),
                anchor="center"
            )
        else:
            # No pagination needed, ensure pagination text is not rendered
            pass
    
    def change_page(self, index, delta):
        """Update the current page for the current path and re-render the view."""
        path = os.path.normpath(self.current_archive_path[index])
        self.pagination_state[index][path] = self.pagination_state[index].get(path, 0) + delta
        self.render_archive_view(index)

    def _fullscreen_archive_index(self):
        """Return the stream index if currently in fullscreen archive
        mode, otherwise None. Used by keyboard shortcuts so Page Up/Down
        and Backspace only act on the visible archive browser."""
        if self.is_fullscreen and self.fullscreen_index is not None:
            idx = self.fullscreen_index
            if self.is_archive_mode[idx]:
                return idx
        return None

    def archive_change_page_shortcut(self, delta):
        """Page Up/Down handler: change archive page for the fullscreen
        stream, if it's currently showing the archive browser (i.e. not
        actively playing a clip)."""
        idx = self._fullscreen_archive_index()
        if idx is None:
            return
        if self.current_archive_path[idx] and self.media_players[idx] is None:
            self.change_page(idx, delta)

    def archive_go_back_shortcut(self):
        """Backspace handler: go back one level in the archive browser
        (or stop playback and return to the browser) for the fullscreen
        stream, if currently in archive mode."""
        idx = self._fullscreen_archive_index()
        if idx is None:
            return
        self.go_back(idx)

    def open_folder(self, index, path):
        self.visited_folders[index].add(os.path.normpath(path))
        self.current_archive_path[index] = path
        path = os.path.normpath(path)
        if path not in self.pagination_state[index]:
            self.pagination_state[index][path] = 0
        self.render_archive_view(index)

    def _reset_clip_buttons(self, index):
        """Null out per-stream clip-playback button refs and reset playback flags."""
        self._stop_hover_poll(index)
        self.exit_buttons[index]   = None
        self.pause_buttons[index]  = None
        self.speed_buttons[index]  = None
        self.replay_buttons[index] = None
        self.rewind_buttons[index] = None
        self.audio_buttons[index]  = None
        self.video_ended[index]    = False

    def _clip_control_widgets(self, index):
        return [
            self.exit_buttons[index],
            self.rewind_buttons[index],
            self.pause_buttons[index],
            self.speed_buttons[index],
            self.replay_buttons[index],
            self.audio_buttons[index],
        ]

    CONTROL_POSITIONS = [
        "top-left", "top-center", "top-right",
        "center",
        "bottom-left", "bottom-center", "bottom-right",
    ]

    def _clip_control_positions(self):
        """Return a list of place() kwargs dicts for each of the 6 clip-control
        buttons, computed from self.controls_position."""
        btn_w, btn_h = 40, 40
        n_buttons = 6
        strip_w = n_buttons * btn_w   # 240 px
        margin = 10
        pos = getattr(self, 'controls_position', 'top-left')

        result = []
        for bi in range(n_buttons):
            dx = bi * btn_w   # each button's offset within the strip

            if pos == "top-left":
                kw = dict(relx=0.0, rely=0.0,
                          x=margin + dx, y=margin, anchor="nw")
            elif pos == "top-center":
                kw = dict(relx=0.5, rely=0.0,
                          x=-strip_w // 2 + dx, y=margin, anchor="nw")
            elif pos == "top-right":
                kw = dict(relx=1.0, rely=0.0,
                          x=-(margin + strip_w) + dx, y=margin, anchor="nw")
            elif pos == "center":
                kw = dict(relx=0.5, rely=0.5,
                          x=-strip_w // 2 + dx, y=-btn_h // 2, anchor="nw")
            elif pos == "bottom-left":
                kw = dict(relx=0.0, rely=1.0,
                          x=margin + dx, y=-(margin + btn_h), anchor="nw")
            elif pos == "bottom-center":
                kw = dict(relx=0.5, rely=1.0,
                          x=-strip_w // 2 + dx, y=-(margin + btn_h), anchor="nw")
            elif pos == "bottom-right":
                kw = dict(relx=1.0, rely=1.0,
                          x=-(margin + strip_w) + dx, y=-(margin + btn_h), anchor="nw")
            else:   # fallback
                kw = dict(relx=0.0, rely=0.0,
                          x=margin + dx, y=margin, anchor="nw")
            result.append(kw)
        return result

    def _set_clip_controls_visible(self, index, visible):
        """Show/hide the clip-playback control buttons for a stream. Cheap
        no-op if already in the requested state."""
        if self._clip_controls_visible[index] == visible:
            return
        self._clip_controls_visible[index] = visible
        widgets = self._clip_control_widgets(index)
        if visible:
            for widget, kw in zip(widgets, self._clip_control_positions()):
                if widget:
                    widget.place(**kw)
        else:
            for widget in widgets:
                if widget:
                    widget.place_forget()

    def _stop_hover_poll(self, index):
        """Cancel the hover poll loop for a stream, if running."""
        poll_id = self._hover_poll_ids[index]
        if poll_id:
            try:
                self.root.after_cancel(poll_id)
            except Exception:
                pass
            self._hover_poll_ids[index] = None
        self._clip_controls_visible[index] = False

    def _start_hover_poll(self, index):
        """Poll pointer position to drive clip-control visibility for a
        clip-playback quadrant. Display-only: no click emulation happens
        here. On Windows the embedded VLC HWND swallows Tk <Enter>/<Leave>
        events, so cursor position has to be polled to know when to show or
        hide the control strip; on Linux native Tk hover events would work
        fine, but the same poll is used on both platforms for simplicity
        since it's just visibility, not action.

        All actual clip actions (exit/back, pause, speed, replay, rewind,
        audio) are real Tk buttons in the control strip this shows/hides -
        no click polling or emulation is needed for them on either OS."""
        self._stop_hover_poll(index)

        def poll():
            if not self.running or not self.media_players[index]:
                self._set_clip_controls_visible(index, False)
                self._hover_poll_ids[index] = None
                return

            try:
                px, py = self.root.winfo_pointerxy()
                label = self.labels[index]
                x0 = label.winfo_rootx()
                y0 = label.winfo_rooty()
                inside = (x0 <= px <= x0 + label.winfo_width() and
                          y0 <= py <= y0 + label.winfo_height())
            except Exception:
                inside = False

            self._set_clip_controls_visible(index, inside)

            interval = 60 if sys.platform.startswith('win') else 150
            self._hover_poll_ids[index] = self.root.after(interval, poll)

        poll()

    def go_back(self, index):
        # In event mode the exit button should follow the clip queue rather
        # than navigating the archive folder tree or tearing down the whole
        # session.
        if self.event_mode:
            self._on_event_clip_ended(index)
            return

        if self.watch_progress_dirty:
            self.save_watch_progress()

        # Stop video playback and clean up VLC resources
        self.cleanup_stream(index)

        # Destroy all children of self.labels[index]
        for widget in self.labels[index].winfo_children():
            widget.destroy()

        # Reset button refs and playback state
        self._reset_clip_buttons(index)
        self.playback_speeds[index] = 1.0
        self.is_paused[index] = False

        # Handle navigation
        if not self.current_archive_path[index]:
            logging.warning(f"Stream {index}: No current archive path, exiting archive mode")
            self.toggle_archive_mode(index)
            return

        # Normalize paths to handle Linux/Windows separators
        current_path = os.path.normpath(self.current_archive_path[index])
        archive_dir = os.path.normpath(self.archive_dir)
        archive_root = os.path.normpath(os.path.join(archive_dir, f"cam{index+1}"))

        # If already at the root folder view, exit archive mode
        if current_path == archive_root and not current_path.endswith(".mp4"):
            logging.info(f"Stream {index}: At archive root {current_path}, exiting archive mode")
            self.toggle_archive_mode(index)
            return

        # Determine parent path
        if current_path.endswith(".mp4"):
            parent_path = os.path.dirname(current_path)
        else:
            parent_path = os.path.dirname(current_path)

        # Reset pagination for video listing view (subfolder) when navigating up
        if current_path != archive_root and not current_path.endswith(".mp4"):
            self.pagination_state[index][current_path] = 0
            logging.info(f"Stream {index}: Reset pagination for video listing view {current_path} to page 1")

        # Check if parent_path is still within or equal to archive_dir
        if not os.path.commonprefix([parent_path, archive_dir]) == archive_dir or parent_path == archive_dir:
            # Reached or exceeded archive_dir, exit archive mode
            logging.info(f"Stream {index}: Reached archive_dir boundary, exiting archive mode")
            self.toggle_archive_mode(index)
            return
        else:
            # Update to parent directory
            self.current_archive_path[index] = parent_path
            parent_path = os.path.normpath(parent_path)
            if parent_path not in self.pagination_state[index]:
                self.pagination_state[index][parent_path] = 0
            logging.info(f"Stream {index}: Navigated back to {self.current_archive_path[index]}")

        # Restore archive view
        self.is_archive_mode[index] = True
        self.labels[index].pack_forget()
        self.archive_canvas[index].pack(fill="both", expand=True)
        self.render_archive_view(index)

    def _events_dir(self):
        # Return the base directory where per-day event JSON files are stored.
        config_dir = os.path.dirname(self.config_file)
        return os.path.join(config_dir, "events")

    def _events_path(self, date):
        # Return the full path for a given date's event JSON file.

        return os.path.join(self._events_dir(), str(date.year), date.strftime("%Y%m%d") + ".json")

    def _scan_events_for_date(self, date):
        # Scan archive clips for date and cluster them into events.
        from datetime import timedelta

        date_str = date.strftime("%Y-%m-%d")
        min_duration_s = 10          # Ignore clips shorter than this
        gap_limit = timedelta(minutes=self.event_overlap_window_mins)

        # --- Collect all parseable clips across all cams ---
        # Trailing _<detectiontype> is optional, for backwards compatibility
        # with clips recorded before detection-type tagging was added.
        clip_re = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}(?:-\d{2})?)_(\d+)m-(\d+)s(?:_([A-Za-z0-9]+(?:_[A-Za-z0-9]+)*))?\.mp4$")
        all_clips = []  # (start_dt, end_dt, cam_index, abs_path, detection_type)

        for cam_idx in range(4):
            if not self.ips[cam_idx]:
                continue
            day_folder = os.path.join(self.archive_dir, f"cam{cam_idx + 1}", date_str)
            if not os.path.isdir(day_folder):
                continue
            try:
                entries = os.listdir(day_folder)
            except OSError as e:
                logging.warning(f"Events scan: cannot list {day_folder}: {e}")
                continue
            for fname in entries:
                m = clip_re.match(fname)
                if not m:
                    continue
                d_str, t_str, mins_str, secs_str, raw_type = m.groups()
                try:
                    start_dt = datetime.strptime(f"{d_str} {t_str.replace('-', ':')}", "%Y-%m-%d %H:%M:%S" if t_str.count('-') == 2 else "%Y-%m-%d %H:%M")
                    duration_s = int(mins_str) * 60 + int(secs_str)
                except (ValueError, TypeError):
                    continue
                if duration_s < min_duration_s:
                    continue
                end_dt = start_dt + timedelta(seconds=duration_s)
                detection_type = self.normalize_detection_type(raw_type)
                all_clips.append((start_dt, end_dt, cam_idx, os.path.join(day_folder, fname), detection_type))

        if not all_clips:
            return []

        all_clips.sort(key=lambda c: c[0])

        # --- Cluster into events using gap-based merge ---
        events = []
        current_cluster = []     # list of clip tuples in this event
        window_end = None        # furthest end time seen so far in this cluster

        def _finalise_cluster(cluster):
            """Convert a raw clip cluster into the event dict schema."""
            cams_data = {}
            for cam_i in range(4):
                cams_data[str(cam_i + 1)] = {"enabled": False, "clips": []}

            e_start = cluster[0][0]
            e_end   = cluster[0][1]
            detection_types = set()
            for s_dt, e_dt, ci, path, detection_type in cluster:
                if s_dt < e_start:
                    e_start = s_dt
                if e_dt > e_end:
                    e_end = e_dt
                cams_data[str(ci + 1)]["clips"].append({
                    "path":           path,
                    "clip_start":     s_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "clip_end":       e_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "detection_type": detection_type,
                })
                cams_data[str(ci + 1)]["enabled"] = True
                if detection_type:
                    detection_types.add(detection_type)

            # Sort clips within each cam by start time
            for cd in cams_data.values():
                cd["clips"].sort(key=lambda c: c["clip_start"])

            return {
                "id":               e_start.strftime("%Y%m%d_%H%M%S"),
                "start":            e_start.strftime("%Y-%m-%dT%H:%M:%S"),
                "end":              e_end.strftime("%Y-%m-%dT%H:%M:%S"),
                "played":           False,
                "cams":             cams_data,
                "detection_types":  sorted(detection_types),
            }

        for clip in all_clips:
            s_dt, e_dt, ci, path, detection_type = clip
            if window_end is None or s_dt > window_end + gap_limit:
                # New event cluster
                if current_cluster:
                    events.append(_finalise_cluster(current_cluster))
                current_cluster = [clip]
                window_end = e_dt
            else:
                current_cluster.append(clip)
                if e_dt > window_end:
                    window_end = e_dt

        if current_cluster:
            events.append(_finalise_cluster(current_cluster))

        return events

    def _load_or_scan_events(self, date):
        # Return the events list for date, using cached JSON for past days.
        from datetime import date as _date
        json_path = self._events_path(date)
        today = _date.today()
        is_today = (date.year == today.year and date.month == today.month and date.day == today.day)

        if not is_today and os.path.exists(json_path):
            try:
                with open(json_path) as f:
                    data = json.load(f)
                return data.get("events", [])
            except Exception as e:
                logging.warning(f"Events: failed to read cache {json_path}: {e}")

        # Scan from archive
        events = self._scan_events_for_date(date)
        self._save_events_json(date, events)
        return events

    def _save_events_json(self, date, events):
        """Persist events list for date to its JSON file."""
        json_path = self._events_path(date)
        try:
            os.makedirs(os.path.dirname(json_path), exist_ok=True)
            payload = {
                "date":       date.strftime("%Y-%m-%d"),
                "scanned_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "events":     events,
            }
            with open(json_path, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logging.warning(f"Events: failed to write cache {json_path}: {e}")

    def _event_detection_types(self, event):
        """Return the sorted list of canonical detection-type ids present in
        an event, deriving them from clips if the cached JSON predates the
        detection_types field."""
        if "detection_types" in event:
            return event["detection_types"]
        types = set()
        for cam_data in event.get("cams", {}).values():
            for clip in cam_data.get("clips", []):
                dt = clip.get("detection_type")
                if dt:
                    types.add(dt)
        result = sorted(types)
        event["detection_types"] = result  # cache on the dict for this session
        return result

    def toggle_event_mode(self):
        """Toggle between event-mode (overlay + coordinated playback) and live.

        Mode-button semantics (mirrors the Archive button):
          - If Events mode is currently active, close it (same as right-click).
          - If Archive mode is currently active, close it first, then open
            Events (switching between modes).
          - Otherwise, open Events mode.
        """
        if self.event_mode:
            self._exit_event_mode()
            return

        if any(self.is_archive_mode[i] for i in range(4)):
            for i in range(4):
                if self.is_archive_mode[i] and not self.archive_transitioning[i]:
                    self.toggle_archive_mode(i, rebuild_ui=False, restart_stream=False)
                    logging.info(f"Stream {i}: Exited archive mode to switch to Events")

        # Drop back to grid before opening the overlay, whether we got here
        # from fullscreen archive or fullscreen live - the event listing
        # centres over the grid area and multi-cam events need all four
        # panels available to render into.
        if self.is_fullscreen:
            self.is_fullscreen = False
            self.fullscreen_index = -1
            self.update_layout()

        self._open_event_overlay()

    def _exit_event_mode(self, restart_streams=True):
        # Tear down event mode and return all quadrants to live streams.
        self.event_mode = False

        for _after_id in self._pending_event_afters:
            try:
                self.root.after_cancel(_after_id)
            except Exception:
                pass
        self._pending_event_afters.clear()

        # If a single-cam event pushed us into fullscreen, return to grid view.
        if getattr(self, '_event_entered_fullscreen', False):
            self.is_fullscreen = False
            self.fullscreen_index = -1
            self._event_entered_fullscreen = False

        # Clean up any active archive-mode playback (event clips in progress).
        for i in list(self.event_active_cams):
            if self.is_archive_mode[i]:
                self.cleanup_archive_mode(i)

        self.event_active_cams  = set()
        self.event_done_cams    = set()
        self.event_clip_queues  = [[] for _ in range(4)]
        self.current_playing_event = None

        # Destroy the overlay
        if self.event_overlay and self.event_overlay.winfo_exists():
            self.event_overlay.destroy()
        self.event_overlay = None

        # Reflect the state flips above (event_mode off, fullscreen reset)
        # immediately - grid view and the Events/Archive icons must not
        # wait for the stream restart lock to finish, or the UI stays
        # showing a single fullscreen cam and a lingering Events icon for
        # the whole reinit duration, which reads as broken/jarring.
        self.update_layout()
        self.build_config_panel()

        if not restart_streams:
            # Caller (e.g. switching straight to Archive mode) will handle
            # its own stream state — don't spin the live streams back up
            # just to tear them down again a moment later.
            logging.info("Event mode exited without restarting live streams (mode switch)")
            self.update_label_bindings()
            return

        # All live streams were stopped on entry — restart every configured cam.
        cams_to_restart = [i for i in range(4) if self.ips[i] and self.streams[i]]
        if cams_to_restart:
            # Disable both action buttons immediately.
            self.root.after(0, self._disable_stream_action_buttons)

            def _restart_all():
                threads = [
                    threading.Thread(
                        target=self.try_init_stream_with_retries,
                        args=(i,),
                        daemon=True
                    )
                    for i in cams_to_restart
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                # All inits done — re-enable buttons and refresh the panel.
                def _on_done():
                    self._reenable_stream_action_buttons()
                    self.build_config_panel()
                self.root.after(0, _on_done)

            threading.Thread(target=_restart_all, daemon=True).start()
        else:
            self.build_config_panel()

        self.update_label_bindings()

    def _open_event_overlay(self, date=None):
        """Build and show the event listing overlay for date (default: today)."""
        from datetime import date as _date, timedelta as _td

        if date is None:
            date = _date.today()

        self.event_mode = True
        self.build_config_panel()   # re-pack to reflect active state

        for i in range(4):
            if self.is_archive_mode[i]:
                self.cleanup_archive_mode(i)
            else:
                self.cleanup_stream(i)
            self._set_event_blank_label(i)

        # Clear label click bindings now that event_mode is True — prevents
        # a click on a blank quadrant from triggering fullscreen-zoom.
        self.update_label_bindings()

        # Load / scan events for this date
        events = self._load_or_scan_events(date)

        # --- Build the panel as a Frame inside the grid area ---
        if self.event_overlay and self.event_overlay.winfo_exists():
            self.event_overlay.destroy()

        overlay = tk.Frame(
            self.grid_frame, bg="#1a1a1a",
            highlightbackground="#444444", highlightthickness=1
        )
        self.event_overlay = overlay

        # Size and centre over the grid area
        self.root.update_idletasks()
        gw = self.grid_frame.winfo_width()
        gh = self.grid_frame.winfo_height()
        ow = min(820, max(500, gw - 40))
        oh = min(500, max(300, gh - 40))

        # Store the sizing args so _start_event_playback and _on_event_clip_ended
        # can re-show the panel without having access to the local ow/oh closure.
        self._event_overlay_size = (ow, oh)

        def _place_overlay():
            overlay.place(relx=0.5, rely=0.5, anchor="center", width=ow, height=oh)
            overlay.lift()

        # ---- Header bar ----
        hdr = tk.Frame(overlay, bg="#111111")
        hdr.pack(fill="x")

        tk.Label(hdr, text="Events", bg="#111111", fg="white",
                 font=self.app_font(13, "bold")).pack(side="left", padx=12, pady=8)

        # Day navigation
        nav_frame = tk.Frame(hdr, bg="#111111")
        nav_frame.pack(side="left", expand=True)

        prev_btn = tk.Button(nav_frame, text="◀", bg="#111111", fg="white", bd=0,
                             activebackground="#333333", cursor="hand2",
                             font=self.app_font(11))
        prev_btn.pack(side="left", padx=4)

        day_label_var = tk.StringVar(value=date.strftime("%A, %d %b %Y"))
        tk.Label(nav_frame, textvariable=day_label_var, bg="#111111", fg="#cccccc",
                 font=self.app_font(11), width=22).pack(side="left")

        next_btn = tk.Button(nav_frame, text="▶", bg="#111111", fg="white", bd=0,
                             activebackground="#333333", cursor="hand2",
                             font=self.app_font(11))
        next_btn.pack(side="left", padx=4)

        close_btn = tk.Button(hdr, text="✕", bg="#111111", fg="#aaaaaa", bd=0,
                              activebackground="#333333", cursor="hand2",
                              font=self.app_font(11),
                              command=self._exit_event_mode)
        close_btn.pack(side="right", padx=10, pady=6)

        ttk.Separator(overlay, orient="horizontal").pack(fill="x")

        # ---- Filter bar ----
        filter_bar = tk.Frame(overlay, bg="#1a1a1a")
        filter_bar.pack(fill="x")

        tk.Label(filter_bar, text="Filter:", bg="#1a1a1a", fg="#888888",
                 font=self.app_font(9)).pack(side="left", padx=(12, 6), pady=4)

        filter_var = tk.StringVar(value=self.ALL_TYPES_LABEL)
        filter_combo = ttk.Combobox(
            filter_bar, textvariable=filter_var, state="readonly", width=18
        )
        filter_combo.pack(side="left", pady=4)

        def _label_for_type(canonical_id):
            return self.detection_type_label(canonical_id)

        def _refresh_filter_options(evs, preserve_selection=False):
            """Rebuild the filter dropdown's options from the detection
            types actually present in evs. By default applies the
            configured default (falling back to All Types if not present);
            when preserve_selection is True, keeps the current choice if
            it's still a valid option (e.g. after deleting an event)."""
            present_ids = sorted({t for ev in evs for t in self._event_detection_types(ev)})
            options = [self.ALL_TYPES_LABEL] + [_label_for_type(cid) for cid in present_ids]
            filter_combo.configure(values=options)

            if preserve_selection and filter_var.get() in options:
                return present_ids

            if self.default_event_filter != "all" and self.default_event_filter in present_ids:
                desired = _label_for_type(self.default_event_filter)
            else:
                desired = self.ALL_TYPES_LABEL
            filter_var.set(desired)
            return present_ids

        def _filtered_events():
            chosen = filter_var.get()
            if chosen == self.ALL_TYPES_LABEL:
                return state["events"]
            label_to_id = {_label_for_type(cid): cid for cid in
                           {t for ev in state["events"] for t in self._event_detection_types(ev)}}
            chosen_id = label_to_id.get(chosen)
            if not chosen_id:
                return state["events"]
            return [ev for ev in state["events"] if chosen_id in self._event_detection_types(ev)]

        ttk.Separator(overlay, orient="horizontal").pack(fill="x")

        # ---- Column headers ----
        cols_frame = tk.Frame(overlay, bg="#2a2a2a")
        cols_frame.pack(fill="x")
        COL_WIDTHS = [9, 9, 31, 5, 5, 4, 5, 7]  # action, time, label, 1, 2, 3, 4, watched
        COL_HEADS  = [" ", "Time", "Label", "1", "2", "3", "4", "Watched"]
        for w, h in zip(COL_WIDTHS, COL_HEADS):
            tk.Label(cols_frame, text=h, bg="#2a2a2a", fg="#888888",
                     font=self.app_font(9, "bold"), width=w).pack(side="left", pady=3)

        ttk.Separator(overlay, orient="horizontal").pack(fill="x")

        # ---- Scrollable event rows ----
        list_outer = tk.Frame(overlay, bg="#1a1a1a")
        list_outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_outer, bg="#1a1a1a", highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        rows_frame = tk.Frame(canvas, bg="#1a1a1a")
        canvas_win = canvas.create_window((0, 0), window=rows_frame, anchor="nw")

        def _on_rows_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(canvas_win, width=canvas.winfo_width())
        rows_frame.bind("<Configure>", _on_rows_configure)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(canvas_win, width=e.width))

        def _on_scroll_linux(event):
            canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

        def _on_scroll_win(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        def _bind_scroll(widget):
            widget.bind("<Button-4>",    _on_scroll_linux)
            widget.bind("<Button-5>",    _on_scroll_linux)
            widget.bind("<MouseWheel>",  _on_scroll_win)
            for child in widget.winfo_children():
                _bind_scroll(child)

        def _unbind_scroll(widget):
            for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
                try:
                    widget.unbind(seq)
                except Exception:
                    pass
            for child in widget.winfo_children():
                _unbind_scroll(child)

        # Bind canvas and list_outer so scrolling works over empty space too
        _bind_scroll(canvas)
        _bind_scroll(list_outer)

        # State held across day navigation refreshes
        state = {"date": date, "events": events}

        def _render_rows(evs):
            _unbind_scroll(rows_frame)
            for w in rows_frame.winfo_children():
                w.destroy()

            if not evs:
                tk.Label(rows_frame, text="No events found for this day.",
                         bg="#1a1a1a", fg="#666666",
                         font=self.app_font(10, "italic")).pack(pady=20)
                return

            rows_frame.unbind("<Configure>")

            play_img   = self.icon_cache["play"]
            delete_img = self.icon_cache["delete"]

            for ev_idx, ev in enumerate(evs):
                row_bg = "#1e1e1e" if ev_idx % 2 == 0 else "#232323"
                row_f = tk.Frame(rows_frame, bg=row_bg)
                row_f.pack(fill="x", pady=1)

                pb = tk.Label(row_f, image=play_img, bg=row_bg, cursor="hand2")
                pb.pack(side="left", padx=(6, 2), pady=4)

                db = tk.Label(row_f, image=delete_img, bg=row_bg, cursor="hand2")
                db.pack(side="left", padx=(2, 8), pady=4)

                # Time range label
                try:
                    s = datetime.strptime(ev["start"], "%Y-%m-%dT%H:%M:%S")
                    e = datetime.strptime(ev["end"],   "%Y-%m-%dT%H:%M:%S")
                    time_txt = f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}"
                except Exception:
                    time_txt = ev.get("start", "?")[:16]

                tk.Label(row_f, text=time_txt, bg=row_bg, fg="white",
                         font=self.app_font(10), width=16, anchor="w").pack(side="left")

                # Editable event label
                label_var = tk.StringVar(value=ev.get("label", ""))
                label_entry = tk.Entry(
                    row_f, textvariable=label_var, width=16,
                    bg="#2a2a2a", fg="white", insertbackground="white",
                    relief="flat", highlightthickness=1,
                    highlightbackground="#444444", highlightcolor="#666666",
                    font=self.app_font(10)
                )
                label_entry.pack(side="left", padx=(4, 8), ipady=2)

                def _save_label(event_ref=ev, v=label_var):
                    event_ref["label"] = v.get().strip()
                    self._save_events_json(state["date"], state["events"])

                label_entry.bind("<FocusOut>", lambda e, fn=_save_label: fn())
                label_entry.bind("<Return>",   lambda e, fn=_save_label: fn())

                # Per-cam checkboxes
                cam_vars = {}
                for ci in range(1, 5):
                    cam_key  = str(ci)
                    cam_data = ev["cams"].get(cam_key, {"enabled": False, "clips": []})
                    has_clip = bool(cam_data.get("clips"))
                    var = tk.BooleanVar(value=cam_data.get("enabled", False))
                    cam_vars[cam_key] = var

                    cb = ttk.Checkbutton(row_f, variable=var,
                                         state="normal" if has_clip else "disabled")
                    cb.pack(side="left", padx=8)

                    def _on_toggle(ck=cam_key, v=var, event_ref=ev):
                        event_ref["cams"][ck]["enabled"] = v.get()
                        self._save_events_json(state["date"], state["events"])

                    var.trace_add("write", lambda *_, cb=_on_toggle: cb())

                # Played indicator
                played_txt = "✓" if ev.get("played") else ""
                played_lbl = tk.Label(row_f, text=played_txt, bg=row_bg,
                                      fg="#4a9d4a", font=self.app_font(11, "bold"),
                                      width=3)
                played_lbl.pack(side="left", padx=4)

                # Wire play/delete buttons (closures over ev, ev_idx, played_lbl)
                def _play(event_ref=ev, p_lbl=played_lbl):
                    enabled_cams = [
                        int(ck) - 1
                        for ck, cd in event_ref["cams"].items()
                        if cd.get("enabled") and cd.get("clips")
                    ]
                    if not enabled_cams:
                        messagebox.showwarning(
                            "No Cameras Selected",
                            "Enable at least one camera checkbox before playing.",
                            parent=self.root
                        )
                        return
                    overlay.place_forget()
                    self._start_event_playback(event_ref, p_lbl, state["date"], state["events"])

                def _delete(ev_ref=ev, idx=ev_idx):
                    if messagebox.askyesno(
                        "Delete Event",
                        "Remove this event?",
                        parent=self.root
                    ):
                        state["events"].pop(idx)
                        self._save_events_json(state["date"], state["events"])
                        _refresh_filter_options(state["events"], preserve_selection=True)
                        _render_rows(_filtered_events())

                # Click and hover bindings for the Label-based icons.
                HOVER_BG = "#2e2e2e"
                for lbl, fn in ((pb, _play), (db, _delete)):
                    lbl.bind("<Button-1>",  lambda e, f=fn:     f())
                    lbl.bind("<Enter>",     lambda e, l=lbl:    l.configure(bg=HOVER_BG))
                    lbl.bind("<Leave>",     lambda e, l=lbl, b=row_bg: l.configure(bg=b))

                ttk.Separator(rows_frame, orient="horizontal").pack(fill="x")

            rows_frame.bind("<Configure>", _on_rows_configure)
            rows_frame.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(canvas_win, width=canvas.winfo_width())
            _bind_scroll(rows_frame)

        _refresh_filter_options(events)
        _render_rows(_filtered_events())

        filter_combo.bind("<<ComboboxSelected>>", lambda e: _render_rows(_filtered_events()))

        # ---- Day navigation wiring ----
        def _navigate(delta):
            from datetime import timedelta as _td2
            new_date = state["date"] + _td2(days=delta)
            if new_date > _date.today():
                return  # Can't navigate to the future
            state["date"]   = new_date
            state["events"] = self._load_or_scan_events(new_date)
            day_label_var.set(new_date.strftime("%A, %d %b %Y"))
            next_btn.configure(state="normal" if new_date < _date.today() else "disabled")
            _refresh_filter_options(state["events"])
            _render_rows(_filtered_events())

        prev_btn.configure(command=lambda: _navigate(-1))
        next_btn.configure(command=lambda: _navigate(+1))
        next_btn.configure(state="disabled" if date >= _date.today() else "normal")

        # Show the panel
        _place_overlay()

    def _transfer_archive_audio(self, index):
        # Give audio exclusively to index (archive or event clip playback) -
        # mutes every other currently-unmuted archive/event stream first,
        # matching "most recently started clip gets audio" behaviour.

        if not self.exclusive_archive_audio:
            return
        for i in range(4):
            if i == index:
                continue
            if not self.archive_audio_muted[i]:
                self.archive_audio_muted[i] = True
                if self.media_players[i]:
                    try:
                        self.media_players[i].audio_set_mute(True)
                    except Exception:
                        pass
                if self.audio_buttons[i]:
                    try:
                        self.audio_buttons[i].configure(image=self.icon_cache["audio_off"])
                    except Exception:
                        pass
        self.archive_audio_muted[index] = False

    def _start_event_playback(self, event, played_label_widget, date, events_list):
        # Kick off coordinated playback for event across all enabled cams.

        self.current_playing_event = event
        self.event_clip_queues  = [[] for _ in range(4)]
        self.event_active_cams  = set()
        self.event_done_cams    = set()

        # Parse the event's global start time for delay calculations.
        try:
            event_start_dt = datetime.strptime(event["start"], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            event_start_dt = None

        cam_launches = []  # list of (ci, first_path, delay_ms)

        for cam_key, cam_data in event["cams"].items():
            if not cam_data.get("enabled"):
                continue
            clips = cam_data.get("clips", [])
            if not clips:
                continue
            ci = int(cam_key) - 1

            queue = []
            for clip_idx, clip in enumerate(clips):
                if clip_idx == 0:
                    gap_ms = 0
                else:
                    try:
                        prev_end_dt  = datetime.strptime(clips[clip_idx - 1]["clip_end"],   "%Y-%m-%dT%H:%M:%S")
                        this_start_dt = datetime.strptime(clip["clip_start"],               "%Y-%m-%dT%H:%M:%S")
                        gap_s  = max(0.0, (this_start_dt - prev_end_dt).total_seconds())
                        gap_ms = int(gap_s * 1000)
                    except Exception:
                        gap_ms = 0
                queue.append((clip["path"], gap_ms))
            self.event_clip_queues[ci] = queue
            self.event_active_cams.add(ci)

            speed = max(1.0, self.default_playback_speed)
            if event_start_dt is not None:
                try:
                    clip_start_dt = datetime.strptime(clips[0]["clip_start"], "%Y-%m-%dT%H:%M:%S")
                    delay_s = max(0.0, (clip_start_dt - event_start_dt).total_seconds())
                    delay_ms = int(delay_s * 1000 / speed)
                except Exception:
                    delay_ms = 0
            else:
                delay_ms = 0

            first_path, _ = self.event_clip_queues[ci].pop(0)
            cam_launches.append((ci, first_path, delay_ms))

        if not self.event_active_cams:
            # Nothing to play — re-show overlay immediately
            if self.event_overlay and self.event_overlay.winfo_exists():
                ow, oh = getattr(self, "_event_overlay_size", (820, 500))
                self.event_overlay.place(relx=0.5, rely=0.5, anchor="center", width=ow, height=oh)
                self.event_overlay.lift()
            return

        # Store refs for the completion callback
        self._event_played_label   = played_label_widget
        self._event_date_for_save  = date
        self._event_list_for_save  = events_list

        self._event_entered_fullscreen = False
        if len(self.event_active_cams) == 1:
            ci_solo = next(iter(self.event_active_cams))
            if not self.is_fullscreen:
                self.is_fullscreen = True
                self.fullscreen_index = ci_solo
                self._event_entered_fullscreen = True
                self.update_layout()
                self.build_config_panel()
        else:
            # Multi-cam events stay in grid - refresh now so the
            # back-to-listing button appears as soon as playback starts.
            self.build_config_panel()

        for ci, _, _ in cam_launches:
            if not self.is_archive_mode[ci]:
                self.is_archive_mode[ci] = True
                self.archive_canvas[ci].pack_forget()
                self.labels[ci].pack(fill="both", expand=True)

        for ci, _, _ in cam_launches:
            self.archive_audio_muted[ci] = True

        for ci, first_path, delay_ms in cam_launches:
            if delay_ms == 0:
                self.play_archive_video(ci, first_path)
            else:
                logging.info(f"Cam {ci + 1}: delaying event playback start by {delay_ms}ms")
                _after_id = self.root.after(
                    delay_ms,
                    lambda c=ci, p=first_path: self.play_archive_video(c, p)
                )
                self._pending_event_afters.append(_after_id)
              
    def _on_event_clip_ended(self, index):
        # Called (on main thread via root.after) when a clip finishes in event mode.
        if not self.event_mode:
            return  # User exited event mode early — nothing to do

        if self.event_clip_queues[index]:

            next_path, gap_ms = self.event_clip_queues[index].pop(0)

            self.cleanup_stream(index)
            for widget in self.labels[index].winfo_children():
                widget.destroy()
            self._reset_clip_buttons(index)

            speed = max(1.0, self.playback_speeds[index])
            adjusted_gap_ms = int(gap_ms / speed)
            if adjusted_gap_ms > 0:
                self._set_event_blank_label(index)
                _after_id = self.root.after(
                    adjusted_gap_ms,
                    lambda i=index, p=next_path: self.play_archive_video(i, p)
                )
                self._pending_event_afters.append(_after_id)
            else:
                self.play_archive_video(index, next_path)
        else:
            # This cam's clips are all done — black it out
            self.cleanup_stream(index)
            for widget in self.labels[index].winfo_children():
                widget.destroy()
            self._reset_clip_buttons(index)
            self.is_archive_mode[index] = False
            self._set_event_blank_label(index)
            self.event_done_cams.add(index)

            if self.event_done_cams >= self.event_active_cams:
                # All cams finished — mark played and restore overlay
                if self.current_playing_event:
                    self.current_playing_event["played"] = True
                    self._save_events_json(
                        self._event_date_for_save,
                        self._event_list_for_save
                    )
                    # Update the ✓ label in the overlay row if it still exists
                    try:
                        if self._event_played_label and self._event_played_label.winfo_exists():
                            self._event_played_label.configure(text="✓")
                    except Exception:
                        pass

                if self.event_overlay and self.event_overlay.winfo_exists():
                    # If a single-cam event entered fullscreen, drop back to
                    # grid before re-showing the overlay.
                    if getattr(self, '_event_entered_fullscreen', False):
                        self.is_fullscreen = False
                        self.fullscreen_index = -1
                        self._event_entered_fullscreen = False
                        self.update_layout()
                        self.build_config_panel()
                    else:
                        # Multi-cam events stay in grid - still need to
                        # refresh so the back-to-listing button (only shown
                        # while a clip is actively playing) is hidden again.
                        self.build_config_panel()
                    ow, oh = getattr(self, "_event_overlay_size", (820, 500))
                    self.event_overlay.place(relx=0.5, rely=0.5, anchor="center", width=ow, height=oh)
                    self.event_overlay.lift()

    def play_archive_video(self, index, video_path):
        self.is_archive_mode[index] = True
        self.archive_canvas[index].pack_forget()
        self.labels[index].pack(fill="both", expand=True)

        self.update_stream_label(index, "Loading...")

        # Clean up any existing VLC frame
        for widget in self.labels[index].winfo_children():
            if isinstance(widget, tk.Frame):
                widget.destroy()

        # Create a Frame for VLC rendering
        try:
            vlc_frame = tk.Frame(self.labels[index], bg="")
            vlc_frame.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0, anchor="nw")
            vlc_frame.configure(highlightthickness=0)
        except Exception as e:
            logging.error(f"Stream {index}: Failed to create VLC frame: {e}")
            self.labels[index].configure(image="", text="Frame Creation Failed", fg="white")
            return

        # Player control buttons
        try:
            exit_img = self.icon_cache["exit"]
            self.exit_buttons[index] = tk.Button(
                self.labels[index],
                image=exit_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.go_back(index)
            )
            self.exit_buttons[index].image = exit_img

            pause_img = self.icon_cache["pause"]
            self.pause_buttons[index] = tk.Button(
                self.labels[index],
                image=pause_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.toggle_pause(index)
            )
            self.pause_buttons[index].image = pause_img

            speed_img = self.icon_cache["speed"]
            self.speed_buttons[index] = tk.Button(
                self.labels[index],
                image=speed_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.cycle_speed(index)
            )
            self.speed_buttons[index].image = speed_img

            replay_img = self.icon_cache["replay"]
            self.replay_buttons[index] = tk.Button(
                self.labels[index],
                image=replay_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.replay_video(index)
            )
            self.replay_buttons[index].image = replay_img

            rewind_img = self.icon_cache["rewind"]
            self.rewind_buttons[index] = tk.Button(
                self.labels[index],
                image=rewind_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda: self.rewind_video(index)
            )
            self.rewind_buttons[index].image = rewind_img

            # Audio toggle button. Starting this clip transfers audio to it
            # exclusively (muting any other currently-playing archive/event
            # clip) - same "most recently started clip gets audio" behaviour
            # in both archive browsing and event playback.
            self._transfer_archive_audio(index)
            audio_img = self.icon_cache["audio_on"]
            self.audio_buttons[index] = tk.Button(
                self.labels[index],
                image=audio_img,
                bg="#222222",
                bd=0,
                cursor="hand2",
                command=lambda idx=index: self.toggle_archive_audio(idx)
            )
            self.audio_buttons[index].image = audio_img

            self.current_archive_path[index] = video_path
            # Buttons start hidden; the hover-poll loop will place/forget them
            # as the cursor enters/leaves the quadrant.
            self.labels[index].update_idletasks()
            logging.info(f"Stream {index}: Buttons created for video {video_path}")
        except Exception as e:
            logging.error(f"Stream {index}: Failed to create or place buttons: {e}")
            self.labels[index].configure(image="", text="Button Creation Failed", fg="white")
            vlc_frame.destroy()
            return

        # Reset playback state
        self.playback_speeds[index] = 1.0
        self.is_paused[index] = False
        self.video_ended[index] = False

        # Clean up previous VLC instances/processes
        self.cleanup_stream(index)

        # Start video playback
        try:
            xid = vlc_frame.winfo_id()
            instance = vlc.Instance(self.build_vlc_instance_args(
                ['--no-drop-late-frames']
            ))
            if instance is None:
                logging.error(f"Stream {index}: Failed to create VLC instance for archive video")
                self.labels[index].configure(image="", text="VLC Initialization Failed", fg="white")
                vlc_frame.destroy()
                return
            self.attach_vlc_logging(instance)
            self.vlc_instances[index] = instance
            player = instance.media_player_new()
            if player is None:
                logging.error(f"Stream {index}: Failed to create VLC media player for archive video")
                self.labels[index].configure(image="", text="VLC Player Creation Failed", fg="white")
                instance.release()
                self.vlc_instances[index] = None
                vlc_frame.destroy()
                return
            self.media_players[index] = player
            media = instance.media_new(video_path)
            player.set_media(media)
            player.set_hwnd(xid) if sys.platform.startswith("win") else player.set_xwindow(xid)
            event_manager = player.event_manager()
            playing_event = threading.Event()
            self.playback_speeds[index] = self.default_playback_speed

            def on_playing():
                playing_event.set()

            event_manager.event_attach(vlc.EventType.MediaPlayerPlaying, lambda e: on_playing())

            if player.play() == -1:
                logging.error(f"Stream {index}: Failed to start VLC player for archive video")
                self.labels[index].configure(image="", text="VLC Playback Failed", fg="white")
                player.release()
                instance.release()
                self.media_players[index] = None
                self.vlc_instances[index] = None
                vlc_frame.destroy()
                return

            timeout = 5.0
            start_time = time.time()
            while time.time() - start_time < timeout:
                if playing_event.is_set():
                    self.set_audio_state(index, mute=self.archive_audio_muted[index])
                    self.media_players[index].set_rate(self.default_playback_speed)
                    # Resume from saved position only in archive browse mode.
                    # Event playback always starts from the beginning of each
                    # clip so consecutive clips play in full regardless of
                    # whether they were partially watched in archive mode.
                    if self.resume_playback and not self.event_mode:
                        saved = self.watch_progress[index].get(video_path)
                        if saved and saved.get("duration", 0) > 0:
                            resume_at = saved.get("position", 0)
                            if 0 < resume_at < saved["duration"] - 3:
                                try:
                                    self.media_players[index].set_time(int(resume_at * 1000))
                                    logging.info(f"Stream {index}: Resumed playback at {resume_at:.1f}s")
                                except Exception as e:
                                    logging.warning(f"Stream {index}: Failed to seek to saved position: {e}")
                    threading.Thread(target=self.monitor_vlc_playback, args=(index,), daemon=True).start()
                    self.root.after(0, lambda i=index: self._start_hover_poll(i))
                    logging.info(f"Stream {index}: Started python-vlc playback for archive video")
                    break
                time.sleep(0.1)
            else:
                logging.error(f"Stream {index}: Archive video failed to start within {timeout}s")
                self.labels[index].configure(image="", text="Playback Timeout", fg="white")
                player.release()
                instance.release()
                self.media_players[index] = None
                self.vlc_instances[index] = None
                vlc_frame.destroy()
                return
        except Exception as e:
            logging.error(f"Stream {index}: Failed to start archive video playback: {e}")
            self.labels[index].configure(image="", text="Playback Failed", fg="white")
            vlc_frame.destroy()
            self.cleanup_stream(index)


    def toggle_pause(self, index):
        self.is_paused[index] = not self.is_paused[index]
        self.playback_speeds[index] = 1.0

        new_icon = self.icon_cache["play" if self.is_paused[index] else "pause"]
        self.pause_buttons[index].configure(image=new_icon)
        self.pause_buttons[index].image = new_icon

        if self.media_players[index]:
            try:
                self.media_players[index].pause()
                self.media_players[index].set_rate(1.0)
                logging.info(f"Stream {index} {'paused' if self.is_paused[index] else 'resumed'} at 1x speed")
            except Exception as e:
                logging.error(f"Error toggling pause for stream {index}: {e}")

    def toggle_archive_audio(self, index):
        # Toggle mute for an archive/event clip.
        currently_muted = self.archive_audio_muted[index]
        new_muted = not currently_muted

        if not new_muted and self.exclusive_archive_audio:
            # Unmuting this stream — mute every other archive stream first.
            for i in range(4):
                if i != index and not self.archive_audio_muted[i]:
                    self.archive_audio_muted[i] = True
                    if self.media_players[i]:
                        try:
                            self.media_players[i].audio_set_mute(True)
                        except Exception:
                            pass
                    if self.audio_buttons[i]:
                        try:
                            self.audio_buttons[i].configure(image=self.icon_cache["audio_off"])
                        except Exception:
                            pass

        self.archive_audio_muted[index] = new_muted
        if self.media_players[index]:
            try:
                self.media_players[index].audio_set_mute(new_muted)
            except Exception as e:
                logging.error(f"Stream {index}: Failed to set archive audio mute: {e}")

        if self.audio_buttons[index]:
            icon = self.icon_cache["audio_off" if new_muted else "audio_on"]
            self.audio_buttons[index].configure(image=icon)

        logging.info(f"Stream {index}: Archive audio {'muted' if new_muted else 'unmuted'}")

    def rewind_video(self, index):
        if not self.current_archive_path[index]:
            logging.warning(f"Stream {index}: No video path set for rewind")
            return

        self.playback_speeds[index] = 1.0

        if self.media_players[index] and not self.video_ended[index]:
            try:
                current_time = self.media_players[index].get_time()
                new_time = max(0, current_time - 10000)
                self.media_players[index].set_time(new_time)
                self.media_players[index].set_rate(1.0)
                if self.is_paused[index]:
                    self.media_players[index].play()
                    self.is_paused[index] = False
                    new_icon = self.icon_cache["pause"]
                self.pause_buttons[index].configure(image=new_icon)
                self.pause_buttons[index].image = new_icon
                logging.info(f"Stream {index}: Rewound video by 10 seconds to {new_time/1000:.1f}s at 1x speed")
            except Exception as e:
                logging.error(f"Error rewinding video for stream {index}: {e}")
        else:
            self.play_archive_video(index, self.current_archive_path[index])
            logging.info(f"Stream {index}: Video ended, restarted for rewind at 1x speed")

    def replay_video(self, index):
        if not self.current_archive_path[index]:
            logging.warning(f"Stream {index}: No video path set for replay")
            return

        self.playback_speeds[index] = 1.0

        if self.media_players[index] and not self.video_ended[index]:
            try:
                self.media_players[index].set_time(0)
                self.media_players[index].set_rate(1.0)
                if self.is_paused[index]:
                    self.media_players[index].play()
                    self.is_paused[index] = False
                    new_icon = self.icon_cache["pause"]
                self.pause_buttons[index].configure(image=new_icon)
                self.pause_buttons[index].image = new_icon
                logging.info(f"Stream {index}: Replayed video at 1x speed")
            except Exception as e:
                logging.error(f"Error replaying video for stream {index}: {e}")
        else:
            self.play_archive_video(index, self.current_archive_path[index])
            logging.info(f"Stream {index}: Restarted video playback at 1x speed")

    def cycle_speed(self, index):
        current_speed = self.playback_speeds[index]
        speed_cycle = [1.0, 2.0, 4.0, 8.0]
        next_speed = speed_cycle[(speed_cycle.index(current_speed) + 1) % len(speed_cycle)]
        self.playback_speeds[index] = next_speed

        if self.media_players[index]:
            try:
                self.media_players[index].set_rate(next_speed)
                logging.info(f"Stream {index} playback speed set to x{next_speed}")
            except Exception as e:
                logging.error(f"Error setting playback speed for stream {index}: {e}")

    def monitor_vlc_playback(self, index):
        video_path = self.current_archive_path[index]
        while self.running and not self.video_ended[index]:
            try:
                if self.media_players[index]:
                    state = self.media_players[index].get_state()
                    if state == vlc.State.Ended:
                        logging.info(f"Stream {index}: python-vlc playback ended")
                        self.video_ended[index] = True
                        # In event mode hand off to the event coordinator instead
                        # of the normal go_back/archive-navigation path.
                        if self.event_mode:
                            self.root.after(0, self._on_event_clip_ended, index)
                            break
             
                        existing = self.watch_progress[index].get(video_path, {})
                        duration = existing.get("duration", 0)
                        if duration > 0:
                            self.watch_progress[index][video_path] = {
                                "position": duration,
                                "duration": duration,
                            }
                            self.watch_progress_dirty = True
                        break
             
                    position_ms = self.media_players[index].get_time()
                    duration_ms = self.media_players[index].get_length()
                    if position_ms is not None and position_ms > 0 and duration_ms and duration_ms > 0:
                        self.watch_progress[index][video_path] = {
                            "position": position_ms / 1000.0,
                            "duration": duration_ms / 1000.0,
                        }
                        self.watch_progress_dirty = True
                time.sleep(1.0)
            except Exception as e:
                logging.error(f"Error monitoring playback for stream {index}: {e}")
                self.video_ended[index] = True  # Mark as ended to exit loop
                break

    def get_onvif_camera(self, ip):
        if ip in self.onvif_cams:
            return self.onvif_cams[ip]
        try:
            from onvif import ONVIFCamera
            cam = ONVIFCamera(ip, 2020, self.username, self.password)
            media = cam.create_media_service()
            ptz = cam.create_ptz_service()
            profiles = media.GetProfiles()
            if not profiles:
                return None
            token = profiles[0].token
            self.onvif_cams[ip] = {
                "cam": cam,
                "ptz": ptz,
                "media": media,
                "token": token
            }
            return self.onvif_cams[ip]
        except Exception:
            return None

    def start_ptz_move(self, direction):
        if not self.is_fullscreen or self.fullscreen_index is None or not self.streams[self.fullscreen_index]:
            return
        ip = self.ips[self.fullscreen_index]
        if not ip or self.ptz_busy:
            return
        
        with self.ptz_lock:
            if self.ptz_buttons_disabled:
                return
            self.ptz_busy = True
            self.ptz_moving = True
            self.disable_ptz_buttons()
            
        if direction in ["left", "right"]:
            self.ptz_click_counts[self.fullscreen_index] += 1
        
        logging.info(f"Starting PTZ move: direction={direction}, ip={ip}")
        threading.Thread(target=self.ptz_move_loop, args=(direction, ip), daemon=True).start()

    def stop_ptz_move(self, direction):
        if not self.is_fullscreen or self.fullscreen_index is None:
            return
        self.ptz_moving = False

    def disable_ptz_buttons(self):
        """Disable all PTZ buttons on the main thread."""
        if not self.ptz_buttons_disabled:
            self.ptz_buttons_disabled = True
            self.root.after(0, lambda: [
                button.config(state="disabled") for button in self.ptz_buttons
            ])

    def enable_ptz_buttons(self):
        """Enable all PTZ buttons on the main thread."""
        if self.ptz_buttons_disabled:
            self.ptz_buttons_disabled = False
            self.root.after(0, lambda: [
                button.config(state="normal") for button in self.ptz_buttons
            ])

    def ptz_move_loop(self, direction, ip):
        try:
            with self.ptz_lock:
                # Send initial PTZ command
                self.send_ptz_command(ip, direction)
                logging.info(f"Sent PTZ command: direction={direction}, ip={ip}")
                
                # Disable buttons
                self.disable_ptz_buttons()
                
                # Wait for a fixed duration to match original movement amount
                movement_duration = 0.5  # Fixed duration for movement
                start_time = time.time()
                time.sleep(movement_duration)
                
                # Send stop command (or pulse_stop for left/right)
                if direction in ["left", "right"]:
                    self.send_ptz_command(ip, "pulse_stop")
                else:
                    self.send_ptz_command(ip, "stop")
                
                # Get ONVIF camera for status polling
                cam_info = self.get_onvif_camera(ip)
                if not cam_info:
                    logging.error(f"Failed to get ONVIF camera for ip={ip}")
                    # Re-enable buttons to avoid being stuck
                    self.ptz_moving = False
                    self.ptz_busy = False
                    self.enable_ptz_buttons()
                    return
                
                ptz = cam_info["ptz"]
                token = cam_info["token"]
                
                # Poll to confirm PTZ is idle before re-enabling buttons
                max_polling_time = 2.0  # Max time to wait for IDLE status
                polling_interval = 0.2  # Interval between status checks
                poll_start = time.time()
                while time.time() - poll_start < max_polling_time:
                    try:
                        status = ptz.GetStatus({"ProfileToken": token})
                        move_status = status.MoveStatus.PanTilt if hasattr(status.MoveStatus, 'PanTilt') else "UNKNOWN"
                        if move_status == "IDLE":
                            logging.info(f"Confirmed PTZ idle for ip={ip} after {time.time() - start_time:.2f} seconds")
                            break
                        time.sleep(polling_interval)
                    except Exception as e:
                        logging.error(f"Error checking PTZ status for ip={ip}: {e}")
                        break  # Exit polling on error to avoid hanging
                
                logging.info(f"PTZ movement completed for ip={ip}, direction={direction}, total duration={time.time() - start_time:.2f} seconds")
                
        except Exception as e:
            logging.error(f"Error in PTZ move loop for ip={ip}, direction={direction}: {e}", exc_info=True)
        finally:
            self.ptz_moving = False
            self.ptz_busy = False
            self.enable_ptz_buttons()

    def send_ptz_command(self, ip, command):
        cam_info = self.get_onvif_camera(ip)
        if not cam_info:
            logging.error(f"Cannot send PTZ command: No ONVIF camera for ip={ip}")
            return
        try:
            ptz = cam_info["ptz"]
            token = cam_info["token"]
            if command == "stop":
                ptz.Stop({"ProfileToken": token})
            elif command == "pulse_stop":
                request = ptz.create_type("ContinuousMove")
                request.ProfileToken = token
                y_velocity = 0.001 if self.ptz_click_counts[self.fullscreen_index] % 2 == 1 else -0.001
                request.Velocity = {"PanTilt": {"x": 0, "y": y_velocity}, "Zoom": {"x": 0}}
                ptz.ContinuousMove(request)
                time.sleep(0.1)
                ptz.Stop({"ProfileToken": token})
            else:
                request = ptz.create_type("ContinuousMove")
                request.ProfileToken = token
                velocity = {"PanTilt": {"x": 0, "y": 0}, "Zoom": {"x": 0}}
                # Speed scaling: speed = 0.025 * (4 ^ (resolution - 1)), capped at 1.0
                base_speed = 0.025  # Anchors resolution 1 at 0.025
                speed = min(1.0, base_speed * (4 ** (self.ptz_resolution - 1)))
                if command == "left":
                    velocity["PanTilt"]["x"] = -speed
                elif command == "right":
                    velocity["PanTilt"]["x"] = speed
                elif command == "up":
                    velocity["PanTilt"]["y"] = speed
                elif command == "down":
                    velocity["PanTilt"]["y"] = -speed
                else:
                    logging.warning(f"Invalid PTZ command: {command}")
                    return
                request.Velocity = velocity
                ptz.ContinuousMove(request)
                logging.info(f"Sent PTZ {command} command to ip={ip}, velocity={velocity}, resolution={self.ptz_resolution}, speed={speed:.4f}")
        except Exception as e:
            logging.error(f"Failed to send PTZ command {command} to ip={ip}: {e}", exc_info=True)


    def apply_window_size(self, size):
        logging.info(f"Applying window size: {size}")
        try:
            if size.lower() == "fullscreen":
                self.root.attributes("-fullscreen", True)
                self.root.update_idletasks()
            else:
                # Parse width and height from size string (e.g., '1340x720')
                try:
                    width, height = map(int, size.split("x"))
                except (ValueError, AttributeError) as e:
                    logging.warning(f"Invalid saved_window_size: {size}, using default {self.MIN_WIDTH}x{self.MIN_HEIGHT}")
                    width, height = self.MIN_WIDTH, self.MIN_HEIGHT

                # Clamp size to valid bounds
                width, height = self.clamp_size(width, height)

                # Ensure window is not maximized
                self.force_unmaximize(width, height)

                # Set window geometry and center it
                self.root.attributes("-fullscreen", False)
                self.root.geometry(f"{width}x{height}")
                self.center_window(width, height)
        except Exception as e:
            logging.error(f"Failed to apply window size {size}: {e}", exc_info=True)
            # Fallback to default size
            width, height = self.MIN_WIDTH, self.MIN_HEIGHT
            self.root.attributes("-fullscreen", False)
            self.force_unmaximize(width, height)
            self.root.geometry(f"{width}x{height}")
            self.center_window(width, height)

    def center_window(self, width, height):
        try:
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            taskbar_height = self.get_taskbar_height()

            if taskbar_height > 0:
                # Center in available space excluding taskbar (Windows)
                available_height = screen_height - taskbar_height
                x = (screen_width - width) // 2
                y = (available_height - height) // 2
                if y < 0:
                    y = 0
            else:
                # Use full screen height (Linux or Windows detection failure)
                x = (screen_width - width) // 2
                y = (screen_height - height) // 2

            # Ensure non-negative coordinates
            x = max(0, x)
            y = max(0, y)

            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self.root.update_idletasks()
        except Exception as e:
            logging.error(f"Failed to center window: {e}", exc_info=True)
            # Fallback to default positioning
            self.root.geometry(f"{width}x{height}+0+0")
            self.root.update_idletasks()

    def clamp_size(self, width, height):
        screen_width = self.root.winfo_screenwidth() - 50  # Margin for panels/docks
        screen_height = self.root.winfo_screenheight() - 100
        return (max(self.MIN_WIDTH, min(width, screen_width)),
                max(self.MIN_HEIGHT, min(height, screen_height)))

    def force_unmaximize(self, width, height):
        try:
            self.root.wm_state("normal")
            self.root.update_idletasks()
            for attempt in range(3):
                if self.root.wm_state() != "zoomed":
                    return True
                self.root.wm_state("normal")
                self.root.update_idletasks()
                time.sleep(0.1)
            return False
        except Exception as e:
            logging.error(f"Error forcing unmaximize: {e}", exc_info=True)
            return False
    
    def get_taskbar_height(self):
        """Detect taskbar height for Windows; return 0 for Linux."""
        if sys.platform.startswith("win"):
            try:
                # Query work area using SystemParametersInfo
                class RECT(ctypes.Structure):
                    _fields_ = [("left", ctypes.c_long),
                                ("top", ctypes.c_long),
                                ("right", ctypes.c_long),
                                ("bottom", ctypes.c_long)]
                
                rect = RECT()
                SPI_GETWORKAREA = 0x0030
                ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
                screen_height = self.root.winfo_screenheight()
                work_area_height = rect.bottom - rect.top
                taskbar_height = screen_height - work_area_height
                if taskbar_height > 0 and taskbar_height < screen_height // 2:  # Sanity check
                    return taskbar_height
            except Exception:
                pass
        return 0

    def cleanup_archive_mode(self, index):
        # Clean up archive mode state and UI for the specified stream index.
        try:
            if self.media_players[index]:
                self.cleanup_stream(index)

            # Destroy all child widgets in self.labels[index]
            for widget in self.labels[index].winfo_children():
                widget.destroy()
            logging.info(f"Stream {index}: Destroyed all child widgets in label")

            # Reset archive UI elements
            if self.archive_canvas[index]:
                self.archive_canvas[index].pack_forget()
            if self.back_buttons[index]:
                self.back_buttons[index].place_forget()

            # Reset player control button refs
            self._reset_clip_buttons(index)

            # Reset archive state
            self.is_archive_mode[index] = False
            self.current_archive_path[index] = None
            self.playback_speeds[index] = 1.0
            self.is_paused[index] = False
            self.video_ended[index] = False
            self.pagination_state[index] = {}

            # Restore live view label
            self.labels[index].pack(fill="both", expand=True)

        except Exception as e:
            logging.error(f"Stream {index}: Failed to clean up archive mode: {e}")

    def cleanup_config_panel(self):
        try:
            if self.config_panel:
                self.config_panel.destroy()
                self.config_panel = None
            if self.ptz_buttons:
                for button in self.ptz_buttons:
                    button.destroy()
                self.ptz_buttons = []
                self.ptz_images = []
            if self.exit_fullscreen_button:
                self.exit_fullscreen_button.destroy()
                self.exit_fullscreen_button = None
                self.exit_fullscreen_image = None
            if self.config_button:
                self.config_button.destroy()
                self.config_button = None
                self.config_img = None
            if self.archive_mode_button:
                self.archive_mode_button.destroy()
                self.archive_mode_button = None
                self.archive_mode_image = None
            # Destroy archive buttons
            for i in range(4):
                if self.archive_buttons[i]:
                    self.archive_buttons[i].destroy()
                    self.archive_buttons[i] = None
            # Destroy fullscreen buttons
            for i in range(4):
                if self.fullscreen_buttons[i]:
                    self.fullscreen_buttons[i].destroy()
                    self.fullscreen_buttons[i] = None
        except Exception:
            pass



    def cleanup(self):
        self.enable_ptz_buttons()

        # Cancel any pending sleep-mode timer.
        if self._sleep_timer_id is not None:
            try:
                self.root.after_cancel(self._sleep_timer_id)
            except Exception:
                pass
            self._sleep_timer_id = None

        # Signal all background threads to stop before touching any VLC object.
        self.running = False
        for i in range(4):
            self.stream_cleanup_events[i].set()

        # Safety-net flush in case the app is closed mid-video.
        if self.watch_progress_dirty:
            self.save_watch_progress()

        # Destroy the help overlay (pure Tk, no VLC involvement).
        if self.help_overlay is not None:
            try:
                self.help_overlay.destroy()
            except Exception:
                pass
            self.help_overlay = None

        # Cancel any pending event after() callbacks.
        for _after_id in getattr(self, "_pending_event_afters", []):
            try:
                self.root.after_cancel(_after_id)
            except Exception:
                pass
        self._pending_event_afters = []

        # VLC teardown on a background thread
        try:
            self.root.withdraw()
        except Exception:
            pass

        def _vlc_teardown():
            for i in range(4):
                try:
                    self.cleanup_stream(i)
                except Exception as e:
                    logging.error(f"Error during shutdown of stream {i}: {e}")

            self.onvif_cams.clear()

            # Hand off Tk widget destruction back to the main thread.
            self.root.after(0, _tk_teardown)

        def _tk_teardown():
            for i in range(4):
                try:
                    if self.archive_canvas[i]:
                        self.archive_canvas[i].destroy()
                    if self.back_buttons[i]:
                        self.back_buttons[i].destroy()
                    if self.exit_buttons[i]:
                        self.exit_buttons[i].destroy()
                    if self.pause_buttons[i]:
                        self.pause_buttons[i].destroy()
                    if self.speed_buttons[i]:
                        self.speed_buttons[i].destroy()
                    if self.replay_buttons[i]:
                        self.replay_buttons[i].destroy()
                    if self.rewind_buttons[i]:
                        self.rewind_buttons[i].destroy()
                    if self.audio_buttons[i]:
                        self.audio_buttons[i].destroy()
                    self.archive_canvas[i] = None
                    self.back_buttons[i] = None
                    self._reset_clip_buttons(i)
                except Exception as e:
                    logging.error(f"Error cleaning up UI for stream {i}: {e}")

            self.cleanup_config_panel()

            try:
                self.root.destroy()
                logging.info("Shutdown completed")
            except Exception as e:
                logging.error(f"Error destroying Tkinter root: {e}")

        import threading as _threading
        _threading.Thread(target=_vlc_teardown, daemon=True).start()

if __name__ == "__main__":
    root = tk.Tk()
    app = tapoStreamer(root)
    root.mainloop()