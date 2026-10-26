# -*- coding: utf-8 -*-
"""
opencode go 订阅额度悬浮小组件
================================

- 纯 Python 标准库 tkinter 实现，无第三方依赖，兼容 Python 3.10+
- 无边框、置顶、可拖动的小窗，圆角白卡片 + 灰色描边观感
- 三行额度（Rolling / Weekly / Monthly）：标题 + 绿色百分比徽标 + Resets in + 灰底绿条进度
- 60 秒自动轮询（可在 config.json 中修改），后台线程请求，主线程更新界面
- 左键拖动移动，单击刷新，右键菜单：刷新 / 置顶 / 隐藏窗口 / 贴边自动收缩 / 小窗模式 / 透明度 / 退出
- 透明度可调（100%~50%），写回 config.json 的 opacity 字段持久化
- 右侧 8px 隐形抓柄可左右拖改宽度（280~640），双击抓柄恢复默认，写回 width 字段
- 小窗模式（mode=compact）：紧凑横条，三等分段显示 5h / 周 / 月 的**剩余**额度
  （上行百分比 + 档位名，下行细横条柱状图），固定 300×45；
  右键菜单可开关，写回 config.json 的 mode 字段（默认 full）
- 拖动边界：窗口永远拖不出虚拟屏（多显示器用 GetSystemMetrics 的
  SM_XVIRTUALSCREEN 系列判定，左/上到边即停，右/下也不许越界）
- 系统托盘（通知区域）常驻：ctypes Shell_NotifyIcon + 系统默认图标 LoadIcon(IDI_APPLICATION)，
  不占任务栏；左键单击 / 双击切换显示隐藏，右键菜单：显示/隐藏 · 立即刷新 · 退出；
  每次轮询后更新 tooltip（64 字内，不重建图标），失败时显示错误简称
- 贴边自动收缩：拖到屏幕边缘（8px 内松手）吸附，判定顺序 **上 -> 左 -> 右**：
  左右吸附成约 36px 宽的圆环条（显示 Rolling 剩余额度），上边吸附成贴顶悬浮小条
  （内容为小窗模式的三段横条）；悬停滑出完整面板，离开 800ms 后自动收回；拖离边缘恢复正常尺寸；
  右键菜单可开关，写回 config.json 的 edge_dock 字段（默认 true）

启动方式：双击 widget.pyw 或运行 opencode-widget.bat
"""

import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

# 高分屏适配：必须在创建 Tk 窗口之前启用 DPI 感知，否则界面会被系统拉伸发虚
try:  # pragma: no cover - 仅 Windows 生效
    import ctypes
    from ctypes import wintypes

    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    ctypes = None
    wintypes = None

import tkinter as tk
from tkinter import font as tkfont

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "widget.log")
API_URL = "https://opencode.ai/zen/go/v1/usage"
HTTP_TIMEOUT = 10  # 单次请求超时（秒）；网络类失败会自动重试一次
HTTP_RETRIES = 2   # 网络类失败的总尝试次数
REFRESH_WATCHDOG = 45  # 单轮刷新最长时间，超过则强制复位（防 getaddrinfo 卡死）
MIN_INTERVAL = 10  # 最小刷新间隔（秒）

# 认证文件候选位置（opencode 默认 ~/.local/share/opencode/auth.json）
AUTH_CANDIDATES = [
    os.path.expanduser(r"~\.local\share\opencode\auth.json"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "opencode", "auth.json"),
]

# 颜色（截图观感）
C_BG = "#ffffff"          # 卡片底色
C_BORDER = "#dcdcdc"      # 卡片描边
C_TEXT = "#1d1d1f"        # 标题
C_SUB = "#8a8a8a"         # 灰色辅助字
C_HINT = "#a6a6a6"        # 状态栏提示字
C_GREEN = "#2e9e5b"       # 进度条填充
C_BADGE_BG = "#e6f6ec"    # 徽标浅绿底
C_BADGE_FG = "#1e7a45"    # 徽标深绿字
C_TRACK = "#e2e2e2"       # 进度条底槽
C_ERR = "#d64545"         # 错误红字
C_ERR_BG = "#fdecea"      # 错误徽标底
C_DOT_BUSY = "#f0b429"    # 更新中状态点

TRANSPARENT_COLOR = "#ff00ff"  # 用于圆角外侧透明（Windows -transparentcolor）

# 逻辑尺寸（96 DPI 下的像素），实际绘制时乘以缩放比例
W = 400              # 默认宽度
W_MIN = 280          # 可拖到的最小宽度
W_MAX = 640          # 可拖到的最大宽度
GRIP_W = 8           # 右侧隐形抓柄宽度
R = 12              # 圆角半径
PAD = 14            # 左右内边距
PAD_TOP = 13        # 顶部内边距
ROW_H = 34          # 每行高度
ROW_GAP = 15        # 行间距
HEAD_H = 19         # 每行首行（标题+徽标+重置时间）高度
BAR_Y = 24          # 进度条在行内的纵坐标
BAR_H = 9           # 进度条高度
STATUS_Y = PAD_TOP + ROW_H * 3 + ROW_GAP * 2 + 9
STATUS_H = 15
H = STATUS_Y + STATUS_H + 13
INNER_W = W - PAD * 2

# 贴边自动收缩（圆环条）
EDGE_W = 36            # 收缩后露出的圆环条宽度（逻辑像素）
EDGE_H = 44            # 收缩后圆环条高度
EDGE_THRESHOLD = 8     # 判定「贴边」的吸附阈值（逻辑像素）
EDGE_LEAVE_MS = 800    # 鼠标离开面板后自动收回的延时（毫秒）

# 小窗模式（横向一排三档剩余额度：文字行 + 细横条柱状图）
MODE_FULL = "full"        # 标准模式：三行 + 状态栏
MODE_COMPACT = "compact"  # 小窗模式：三等分段「百分比 + 档位 + 横条」
COMPACT_W = 300           # 小窗模式宽度（逻辑像素，固定 290~320 区间）
COMPACT_H = 45            # 小窗模式高度（逻辑像素，固定 42~48 区间）
COMPACT_LABELS = ("5h", "周", "月")   # 与托盘 tooltip 的三个档位一致
COMPACT_SEP = "#e6e6e6"   # 段间 1px 浅灰竖分隔线
COMPACT_SEP_PAD = 8       # 分隔线上下留白
COMPACT_PAD = 9           # 段内左右内边距（横条按段宽自适应）
COMPACT_TEXT_CY = 17      # 第一行（百分比 + 档位）的垂直中心
COMPACT_TEXT_GAP = 6      # 百分比与档位名之间的间距
COMPACT_BAR_Y = 30        # 第二行横条顶部
COMPACT_BAR_H = 5         # 横条高度

# 透明度（越界自动钳制到 0.5~1.0）
OPACITY_STEPS = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5)
OPACITY_MIN = 0.5
OPACITY_MAX = 1.0

APP_TITLE = "opencode-go usage"   # 窗口标题

# 行定义
ROWS = (
    ("rolling", "Rolling usage"),
    ("weekly", "Weekly usage"),
    ("monthly", "Monthly usage"),
)

FONT_TITLE = ("Segoe UI", 10, "bold")
FONT_BADGE = ("Segoe UI", 8, "bold")
FONT_RIGHT = ("Segoe UI", 9)
FONT_STATUS = ("Microsoft YaHei UI", 8)
FONT_MENU = ("Microsoft YaHei UI", 9)
# 圆环条里的剩余百分比：按内径自适应降级，保证 “100%” 也不会撑出圆环
FONT_RING = ("Segoe UI", 7, "bold")
FONT_RING_SM = ("Segoe UI", 6, "bold")
FONT_RING_XS = ("Segoe UI", 5, "bold")
# 小窗模式第一行：百分比（深色 10pt 半粗）+ 档位名（灰 9pt）
FONT_COMPACT_NUM = ("Segoe UI", 10, "bold")
FONT_COMPACT_TAG = ("Microsoft YaHei UI", 9)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def log(msg):
    """把意外异常写入同目录 widget.log（pythonw 启动时没有控制台可看）。"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def load_json(path):
    """读取 JSON 文件，失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def clamp_number(value, low, high, default):
    """把 value 钳制到 [low, high]；不是数字（含 NaN）时返回 default。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


def load_config():
    """读取同目录 config.json：token、刷新间隔、透明度、宽度、贴边收缩（越界自动钳制）。"""
    cfg = load_json(CONFIG_PATH) or {}
    try:
        interval = int(cfg.get("interval_sec", 60))
    except (TypeError, ValueError):
        interval = 60
    interval = max(MIN_INTERVAL, min(interval, 86400))
    token = str(cfg.get("token") or "").strip()
    opacity = clamp_number(cfg.get("opacity", 1.0), OPACITY_MIN, OPACITY_MAX, 1.0)
    width = int(round(clamp_number(cfg.get("width", W), W_MIN, W_MAX, float(W))))
    edge_raw = cfg.get("edge_dock", True)
    if isinstance(edge_raw, str):
        edge_dock = edge_raw.strip().lower() not in ("0", "false", "no", "off", "")
    else:
        edge_dock = bool(edge_raw)
    mode = str(cfg.get("mode") or MODE_FULL).strip().lower()
    if mode != MODE_COMPACT:
        mode = MODE_FULL          # 老配置没有 mode，或值非法，一律按标准模式
    return {
        "token": token,
        "interval": interval,
        "opacity": opacity,
        "width": width,
        "edge_dock": edge_dock,
        "mode": mode,
    }


def save_config(updates):
    """把 updates 合并写回 config.json（保留 token / interval_sec 等已有字段）。"""
    try:
        cfg = load_json(CONFIG_PATH)
        if not isinstance(cfg, dict):
            cfg = {}
        cfg.update(updates)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return True
    except Exception as exc:
        log("写入 config.json 失败：%s" % exc)
        return False


def resolve_token():
    """
    解析 token，优先级：
    1) 环境变量 OPENCODE_TOKEN
    2) 同目录 config.json 的 token 字段
    3) opencode 认证文件里 "opencode-go" 项的 key
    返回 (token, 来源描述)；都没有则返回 ("", "")。
    """
    token = os.environ.get("OPENCODE_TOKEN", "").strip()
    if token:
        return token, "环境变量 OPENCODE_TOKEN"

    cfg = load_config()
    if cfg["token"]:
        return cfg["token"], "config.json"

    paths = [p for p in AUTH_CANDIDATES if p and os.path.isfile(p)]
    env_auth = os.environ.get("OPENCODE_AUTH_PATH", "").strip()
    if env_auth:
        paths.insert(0, env_auth)
    for path in paths:
        data = load_json(path)
        if not data:
            continue
        entry = data.get("opencode-go")
        if isinstance(entry, dict):
            token = str(entry.get("key") or "").strip()
        elif isinstance(entry, str):
            token = entry.strip()
        if token:
            return token, path
    return "", ""


class UsageError(Exception):
    """带「详细说明」和「行内短文案」的业务异常。"""

    def __init__(self, detail, short):
        super().__init__(detail)
        self.detail = detail
        self.short = short


def _fetch_once(token):
    """单次请求额度接口，返回 usage 字典；失败抛 UsageError。"""
    req = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": "Bearer %s" % token,
            "Accept": "application/json",
            "User-Agent": "opencode-go-widget/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code == 401:
            raise UsageError("401 key 无效，请检查 token", "401 key 无效")
        if code == 403:
            raise UsageError("403 未订阅 opencode go", "403 未订阅")
        raise UsageError("接口返回 HTTP %d" % code, "HTTP %d" % code)
    except socket.timeout:
        raise UsageError("网络超时（%d 秒无响应）" % HTTP_TIMEOUT, "网络超时")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None) or exc
        raise UsageError("网络失败：%s" % reason, "网络失败")
    except OSError as exc:
        raise UsageError("网络失败：%s" % exc, "网络失败")

    try:
        data = json.loads(raw)
    except ValueError:
        raise UsageError("返回数据不是合法 JSON", "数据异常")
    if not isinstance(data, dict):
        raise UsageError("返回数据结构异常", "数据异常")
    usage = data.get("usage")
    if not isinstance(usage, dict):
        raise UsageError("返回数据缺少 usage 字段", "数据异常")
    return usage


def fetch_usage(token):
    """请求额度接口；网络类失败自动重试，避免一次握手抖动就把界面打成红色。"""
    last = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            return _fetch_once(token)
        except UsageError as exc:
            last = exc
            retryable = exc.short in ("网络失败", "网络超时") or exc.short.startswith("HTTP 5")
            if not retryable or attempt >= HTTP_RETRIES:
                raise
            log("第 %d 次请求失败（%s），%.1f 秒后重试" % (attempt, exc.short, 0.8 * attempt))
            time.sleep(0.8 * attempt)
    raise last


def format_resets(value):
    """把 ISO8601 时间换算成 'Resets in 3d 4h / 3h 4m / 12m'。"""
    if not value:
        return "—"
    try:
        text = str(value).strip().replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        total = int((moment - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return "—"
    if total <= 0:
        return "Resets in 0m"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return "Resets in %dd %dh" % (days, hours)
    if hours:
        return "Resets in %dh %dm" % (hours, minutes)
    return "Resets in %dm" % minutes


def parse_rows(usage):
    """把接口返回整理成三行展示数据。"""
    result = []
    for key, _title in ROWS:
        item = usage.get(key)
        if not isinstance(item, dict):
            result.append({"ok": False, "percent": 0, "resets": "", "error": "无数据"})
            continue
        status = str(item.get("status") or "").lower()
        try:
            percent = int(round(float(item.get("percent"))))
        except (TypeError, ValueError):
            result.append({"ok": False, "percent": 0, "resets": "", "error": "无数据"})
            continue
        percent = max(0, min(100, percent))
        if status == "rate-limited":
            percent = 100  # 限流按 100% 显示
        result.append(
            {
                "ok": True,
                "percent": percent,
                "resets": format_resets(item.get("resetsAt")),
                "error": "",
            }
        )
    return result


def resets_cn(resets):
    """把 'Resets in 2h 27m' 换成 tooltip 用的 '2h27m后重置'。"""
    if not resets or resets == "—":
        return "—"
    text = str(resets).replace("Resets in ", "").replace(" ", "")
    if not text or text == "—":
        return "—"
    return text + "后重置"


# ---------------------------------------------------------------------------
# 系统托盘图标（ctypes Shell_NotifyIcon，纯标准库，不新增图标文件）
# ---------------------------------------------------------------------------

TRAY_AVAILABLE = bool(ctypes is not None and wintypes is not None and hasattr(ctypes, "windll"))

WM_APP = 0x8000
WM_TRAYCALL = WM_APP + 1          # Shell_NotifyIcon 回调到托盘窗口的消息
WM_TRAYCMD = WM_APP + 2           # Tk 主线程 -> 托盘线程的命令消息
TRAY_CMD_TIP = 1                  # 重新写入 tooltip（NIM_MODIFY）
TRAY_CMD_QUIT = 2                 # 删除图标并结束消息循环
TRAY_TIMER_CLICK = 1              # 左键单击 / 双击去抖定时器 ID
TRAY_ICON_ID = 1
TRAY_CLASS_PREFIX = "OpenCodeGoWidgetTray-"

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x10
IDI_APPLICATION = 32512           # LoadIcon(NULL, IDI_APPLICATION) 系统默认图标
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_TIMER = 0x0113
WM_DESTROY = 0x0002
WM_NULL = 0x0000
NIN_SELECT, NIN_KEYSELECT = 0x0400, 0x0401
# 原生托盘菜单：显示/隐藏 · 立即刷新 · 退出
TRAY_CMD_TOGGLE, TRAY_CMD_REFRESH, TRAY_CMD_QUIT = 1001, 1002, 1003
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
MF_BYCOMMAND = 0x0000
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
TIP_MAX = 64                      # szTip 可读字数上限
CLICK_DELAY_MS = 350              # 单击判定延迟，用来和双击区分
ERROR_CLASS_ALREADY_EXISTS = 1410

_user32 = None
_shell32 = None
_k32 = None
_NID = None
_MSG = None
_WC = None
_WNDPROC = None

if TRAY_AVAILABLE:  # pragma: no cover - 仅 Windows 生效
    _user32 = ctypes.windll.user32
    _shell32 = ctypes.windll.shell32
    _k32 = ctypes.windll.kernel32

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]

    _WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),     # wintypes 没有 HCURSOR，句柄布局一致
            ("hbrBrush", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", POINT),
            ("lPrivate", wintypes.DWORD),
        ]

    _NID, _MSG, _WC = NOTIFYICONDATAW, MSG, WNDCLASSW

    # 统一设置一次原型，避免 64 位下句柄被默认 c_int 截断
    _shell32.Shell_NotifyIconW.argtypes = [wintypes.UINT, ctypes.POINTER(NOTIFYICONDATAW)]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.WORD
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
    ]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t
    _user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    _user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    _user32.GetCursorPos.restype = wintypes.BOOL
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.GetParent.argtypes = [wintypes.HWND]
    _user32.GetParent.restype = wintypes.HWND
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    # 原生托盘菜单（TrackPopupMenu）
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR
    ]
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
    ]
    _user32.TrackPopupMenu.restype = wintypes.UINT
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.GetMenuStringW.argtypes = [
        wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT
    ]


def cursor_pos():
    """当前鼠标屏幕坐标（虚拟屏坐标系，和 winfo_x 同一坐标系）。"""
    if _user32 is not None:
        pt = POINT()
        if _user32.GetCursorPos(ctypes.byref(pt)):
            return pt.x, pt.y
    return None


class TrayIcon:
    """
    通知区域（系统托盘）图标。

    - 独立后台线程：注册窗口类 -> 建一个不可见的 Win32 窗口 -> 跑 GetMessage 消息循环，
      因此完全不占任务栏，也不再需要任何 Tk 辅助 Toplevel；
    - 图标用系统默认的 LoadIcon(IDI_APPLICATION)，不新增 .ico 文件；
    - 主线程更新 tooltip 只 PostMessage 一个命令，由托盘线程 NIM_MODIFY 写 szTip，
      既不删除也不重建图标，所以不会闪烁；
    - 托盘线程收到鼠标事件后只往 queue 里丢消息，真正的 Tk 操作都在主线程做；
    - 右键菜单用原生 TrackPopupMenu（MSDN 托盘菜单标准写法）：Tk 菜单在根窗口
      withdraw 之后弹出会非确定性卡死，原生菜单不受 Tk 窗口状态影响。
    """

    def __init__(self, tip=""):
        self._lock = threading.Lock()
        self._tip = str(tip or "")[:TIP_MAX]
        self._added_tip = None
        self._hwnd = None
        self._hicon = None
        self._cb = None
        self._added = False
        self._closing = False
        self._click_armed = False
        self._suppress_up = False
        self._hidden = False           # 小窗当前是否隐藏（决定菜单第一项文案）
        self._menu_open = False
        self.queue = queue.Queue()     # 托盘线程 -> Tk 主线程："toggle" / ("cmd", id)
        self.ready = False
        self._thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)
        self._thread.start()

    # -- 主线程接口 ---------------------------------------------------------
    def set_hidden(self, hidden):
        """同步小窗可见状态，供托盘右键菜单的「显示/隐藏」文案使用。"""
        with self._lock:
            self._hidden = bool(hidden)
    def set_tip(self, text):
        """更新悬停提示（≤64 字）。只走 NIM_MODIFY，不删除重建图标。"""
        text = str(text or "")
        if len(text) > TIP_MAX:
            text = text[: TIP_MAX - 1] + "…"
        if _user32 is None:
            return
        with self._lock:
            if text == self._tip:
                return
            self._tip = text
            hwnd, added = self._hwnd, self._added
        if hwnd and added:
            _user32.PostMessageW(hwnd, WM_TRAYCMD, TRAY_CMD_TIP, 0)

    def remove(self):
        """删除托盘图标并结束消息循环（可重复调用）。"""
        if _user32 is None:
            return
        with self._lock:
            if self._closing:
                return
            self._closing = True
            hwnd = self._hwnd
        if hwnd:
            _user32.PostMessageW(hwnd, WM_TRAYCMD, TRAY_CMD_QUIT, 0)

    def join(self, timeout=1.0):
        self._thread.join(timeout)

    # -- 托盘线程 -----------------------------------------------------------
    def _run(self):
        try:
            self._thread_id = _k32.GetCurrentThreadId()   # 便于排查（枚举该线程窗口）
            hwnd = self._create_window()
        except Exception:
            log("创建托盘窗口失败：\n" + traceback.format_exc())
            return
        if not hwnd:
            return
        self._add_icon(hwnd)
        with self._lock:
            closing = self._closing
        if closing:
            _user32.DestroyWindow(hwnd)      # 同步触发 WM_DESTROY -> PostQuitMessage
        msg = _MSG()
        while True:
            try:
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            except Exception:
                log("托盘消息循环异常：\n" + traceback.format_exc())
                break
            if ret <= 0:                     # 0 = WM_QUIT，-1 = 出错
                break
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        self._delete_icon(hwnd)
        with self._lock:
            self._hwnd = None
            self._added = False
            self.ready = False

    def _create_window(self):
        hinst = _k32.GetModuleHandleW(None)
        self._hicon = _user32.LoadIconW(None, IDI_APPLICATION)
        # 回调对象必须长期持有引用，否则会被 GC，消息一来就崩
        self._cb = _WNDPROC(self._wndproc)
        wc = _WC()
        wc.style = 0
        wc.lpfnWndProc = self._cb
        wc.hInstance = hinst
        wc.hIcon = self._hicon
        # 类名带线程 ID：多个实例各用各的窗口类，回调不会串
        class_name = "%s%d" % (TRAY_CLASS_PREFIX, _k32.GetCurrentThreadId())
        wc.lpszClassName = class_name
        if not _user32.RegisterClassW(ctypes.byref(wc)):
            err = _k32.GetLastError()
            if err != ERROR_CLASS_ALREADY_EXISTS:
                raise OSError(err, "RegisterClassW 失败")
        hwnd = _user32.CreateWindowExW(
            0, class_name, "opencode-go tray", 0,
            0, 0, 0, 0, None, None, hinst, None,
        )
        if not hwnd:
            raise OSError(_k32.GetLastError(), "CreateWindowExW 失败")
        with self._lock:
            self._hwnd = hwnd
        return hwnd

    def _nid(self, hwnd, flags):
        nid = _NID()
        nid.cbSize = ctypes.sizeof(_NID)
        nid.hWnd = hwnd
        nid.uID = TRAY_ICON_ID
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAYCALL
        nid.hIcon = self._hicon
        return nid

    def _add_icon(self, hwnd):
        with self._lock:
            tip = self._tip
        nid = self._nid(hwnd, NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        nid.szTip = tip
        ok = bool(_shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        if not ok:
            # 上次异常退出可能留下同 ID 图标：先删再加
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            ok = bool(_shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        if not ok:
            log("托盘图标创建失败（错误 %d）" % _k32.GetLastError())
            return False
        with self._lock:
            self._added = True
            self._added_tip = tip
        self.ready = True
        log("托盘图标已创建")
        with self._lock:
            stale = self._tip != self._added_tip
        if stale:                            # 添加期间主线程又改了 tooltip
            self._modify_tip(hwnd)
        return True

    def _modify_tip(self, hwnd):
        with self._lock:
            tip = self._tip
        nid = self._nid(hwnd, NIF_TIP | NIF_SHOWTIP)
        nid.szTip = tip
        if not _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)):
            log("更新托盘 tooltip 失败（错误 %d）" % _k32.GetLastError())

    def _delete_icon(self, hwnd):
        with self._lock:
            if not self._added:
                return
            self._added = False
        nid = self._nid(hwnd, NIF_MESSAGE | NIF_ICON | NIF_TIP)
        _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        log("托盘图标已移除")

    def _wndproc(self, hwnd, msg, wParam, lParam):
        if msg == WM_TRAYCALL:
            try:
                self._on_tray_event(int(lParam))
            except Exception:
                log("托盘事件处理失败：\n" + traceback.format_exc())
            return 0
        if msg == WM_TRAYCMD:
            cmd = int(wParam)
            if cmd == TRAY_CMD_TIP:
                self._modify_tip(hwnd)
            elif cmd == TRAY_CMD_QUIT:
                self._delete_icon(hwnd)
                _user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_TIMER and int(wParam) == TRAY_TIMER_CLICK:
            _user32.KillTimer(hwnd, TRAY_TIMER_CLICK)
            self._click_armed = False
            self.queue.put("toggle")
            return 0
        if msg == WM_DESTROY:
            self._delete_icon(hwnd)
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wParam, lParam)

    def _build_hmenu(self):
        """
        构造原生弹出菜单，返回 HMENU（调用方负责 DestroyMenu）。
        第一项文案随小窗当前是显示还是隐藏切换。
        """
        hmenu = _user32.CreatePopupMenu()
        if not hmenu:
            return None
        with self._lock:
            hidden = self._hidden
        label = "显示窗口" if hidden else "隐藏窗口"
        _user32.AppendMenuW(hmenu, MF_STRING, TRAY_CMD_TOGGLE, label)
        _user32.AppendMenuW(hmenu, MF_STRING, TRAY_CMD_REFRESH, "立即刷新")
        _user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        _user32.AppendMenuW(hmenu, MF_STRING, TRAY_CMD_QUIT, "退出")
        return hmenu

    def _menu_label(self, hmenu, cmd_id):
        """读回某个菜单项的文字（自测用，也方便排查编码问题）。"""
        buf = ctypes.create_unicode_buffer(64)
        _user32.GetMenuStringW(hmenu, cmd_id, buf, 64, MF_BYCOMMAND)
        return buf.value

    def _popup_menu(self, x, y):
        """在托盘线程里同步弹出原生菜单，返回命中的命令 id（0 = 未选择）。"""
        hmenu = self._build_hmenu()
        if not hmenu:
            return 0
        try:
            # 托盘菜单标准动作：先抢前台（否则点外部可能收不起来），收起后补 WM_NULL
            _user32.SetForegroundWindow(self._hwnd)
            cmd = _user32.TrackPopupMenu(
                hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD, int(x), int(y), 0, self._hwnd, None
            )
            _user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            _user32.DestroyMenu(hmenu)
        return int(cmd or 0)

    def _on_tray_event(self, code):
        """托盘鼠标事件（lParam）：右键弹菜单，单击/双击切换显示隐藏。"""
        if code in (WM_RBUTTONUP, WM_CONTEXTMENU):
            if self._menu_open:
                return                    # 菜单已弹出，忽略重复右键
            self._menu_open = True
            try:
                pos = cursor_pos() or (0, 0)
                cmd = self._popup_menu(pos[0], pos[1])
            finally:
                self._menu_open = False
            if cmd:
                self.queue.put(("cmd", cmd))
            return
        if code in (WM_LBUTTONDBLCLK,):
            if self._click_armed:
                _user32.KillTimer(self._hwnd, TRAY_TIMER_CLICK)
                self._click_armed = False
            self._suppress_up = True      # 双击的第二次 LBUTTONUP 不再起定时器
            self.queue.put("toggle")
            return
        if code in (WM_LBUTTONUP, NIN_SELECT, NIN_KEYSELECT):
            if self._suppress_up:
                self._suppress_up = False
                return
            if not self._click_armed:
                _user32.SetTimer(self._hwnd, TRAY_TIMER_CLICK, CLICK_DELAY_MS, None)
                self._click_armed = True


# ---------------------------------------------------------------------------
# 绘制辅助
# ---------------------------------------------------------------------------

def round_rect(canvas, x1, y1, x2, y2, radius, **kw):
    """在 Canvas 上画圆角矩形（smooth 多边形近似）。"""
    r = max(0, min(radius, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    if r <= 0:
        return canvas.create_rectangle(x1, y1, x2, y2, **kw)
    points = [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kw)


def round_rect_top_flat(canvas, x1, y1, x2, y2, radius, **kw):
    """
    上边两个角为直角、下边两个角为圆角的形状。
    贴顶吸附时用：顶边与屏幕边缘齐平，只保留底部圆角，看起来才像「吸附」而不是浮着。
    """
    r = max(0, min(radius, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    if r <= 0:
        return canvas.create_rectangle(x1, y1, x2, y2, **kw)
    points = [
        x1, y1, x1, y1,            # 左上：重复点把样条锚成直角
        x2, y1, x2, y1,            # 右上：同上
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,                # 右下圆角
        x1 + r, y2,
        x1, y2,                    # 左下圆角
        x1, y2 - r,
        x1, y1, x1, y1,            # 收口
    ]
    return canvas.create_polygon(points, smooth=True, **kw)


# ---------------------------------------------------------------------------
# 单行组件
# ---------------------------------------------------------------------------

class UsageRow:
    """一行额度：标题 + 百分比徽标 + Resets in + 灰底绿条进度。"""

    def __init__(self, parent, s, title, fonts, width):
        self.s = s
        self.state = "busy"  # busy / ok / error
        self.percent = 0
        self.badge_text = "…"
        self.width = width  # 当前行宽（逻辑像素）

        self.frame = tk.Frame(parent, bg=C_BG)
        self.frame.place(x=0, y=0, width=s(width), height=s(ROW_H))

        # 首行：标题 + 徽标（左），Resets in（右）
        head = tk.Frame(self.frame, bg=C_BG, height=s(HEAD_H))
        head.place(x=0, y=0, width=s(width), height=s(HEAD_H))
        self.head = head

        self.title_lbl = tk.Label(
            head, text=title, font=fonts["title"], bg=C_BG, fg=C_TEXT, anchor="w"
        )
        self.title_lbl.pack(side="left")

        self.badge = tk.Canvas(
            head,
            width=s(58),
            height=s(16),
            bg=C_BG,
            highlightthickness=0,
            bd=0,
        )
        self.badge.pack(side="left", padx=(s(8), 0))

        self.right_lbl = tk.Label(
            head, text="—", font=fonts["right"], bg=C_BG, fg=C_SUB, anchor="e"
        )
        self.right_lbl.pack(side="right")

        # 次行：进度条
        self.bar = tk.Canvas(
            self.frame,
            height=s(BAR_H),
            bg=C_BG,
            highlightthickness=0,
            bd=0,
        )
        self.bar.place(x=0, y=s(BAR_Y), relwidth=1, height=s(BAR_H))
        self.bar.bind("<Configure>", self._draw_bar)

    # -- 徽标 ---------------------------------------------------------------
    def _draw_badge(self):
        canvas = self.badge
        canvas.delete("all")
        text = self.badge_text
        width = int(_FONTS["badge"].measure(text)) + self.s(14)
        canvas.configure(width=width)
        if self.state == "error":
            pill_fill, text_fill = C_ERR_BG, C_ERR
        elif self.state == "busy":
            pill_fill, text_fill = "#f1f2f3", C_SUB
        else:
            pill_fill, text_fill = C_BADGE_BG, C_BADGE_FG
        round_rect(
            canvas, 0.5, 0.5, max(width - 1, 2), self.s(16) - 0.5, self.s(8),
            fill=pill_fill, outline="",
        )
        canvas.create_text(
            width / 2.0, self.s(8), text=text,
            font=_FONTS["badge"], fill=text_fill, anchor="center",
        )

    # -- 进度条 -------------------------------------------------------------
    def _draw_bar(self, _event=None):
        canvas = self.bar
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 2 or height <= 2:
            return
        radius = height / 2.0
        track_color = C_ERR_BG if self.state == "error" else C_TRACK
        round_rect(canvas, 0, 0, width, height, radius, fill=track_color, outline="")
        if self.state == "ok" and self.percent > 0:
            fill = max(int(width * self.percent / 100.0), int(height))
            fill = min(fill, width)
            round_rect(canvas, 0, 0, fill, height, radius, fill=C_GREEN, outline="")

    # -- 对外 ---------------------------------------------------------------
    def set_ok(self, percent, resets):
        self.state = "ok"
        self.percent = percent
        self.badge_text = "%d%% used" % percent
        self.title_lbl.configure(fg=C_TEXT)
        self.right_lbl.configure(text=resets, fg=C_SUB)
        self._draw_badge()
        self._draw_bar()

    def set_error(self, message):
        self.state = "error"
        self.percent = 0
        self.badge_text = "—"
        self.title_lbl.configure(fg=C_ERR)
        self.right_lbl.configure(text=message or "数据异常", fg=C_ERR)
        self._draw_badge()
        self._draw_bar()

    def set_busy(self, message="加载中…"):
        """中性占位状态（灰色，不是错误红）。"""
        self.state = "busy"
        self.percent = 0
        self.badge_text = "…"
        self.title_lbl.configure(fg=C_SUB)
        self.right_lbl.configure(text=message, fg=C_SUB)
        self._draw_badge()
        self._draw_bar()

    def set_width(self, width):
        """窗口宽度变化时重排行宽；进度条靠 Configure 事件自动拉伸。"""
        self.width = width
        self.frame.place(width=self.s(width))
        self.head.place(width=self.s(width))
        self._draw_bar()


# 字体对象（全局共享，创建一次）
_FONTS = {}


def fonts_build(root):
    _FONTS["title"] = tkfont.Font(root, family=FONT_TITLE[0], size=FONT_TITLE[1], weight="bold")
    _FONTS["badge"] = tkfont.Font(root, family=FONT_BADGE[0], size=FONT_BADGE[1], weight="bold")
    _FONTS["right"] = tkfont.Font(root, family=FONT_RIGHT[0], size=FONT_RIGHT[1])
    _FONTS["status"] = tkfont.Font(root, family=FONT_STATUS[0], size=FONT_STATUS[1])
    _FONTS["menu"] = tkfont.Font(root, family=FONT_MENU[0], size=FONT_MENU[1])
    _FONTS["ring"] = tkfont.Font(root, family=FONT_RING[0], size=FONT_RING[1], weight="bold")
    _FONTS["ring_sm"] = tkfont.Font(root, family=FONT_RING_SM[0], size=FONT_RING_SM[1], weight="bold")
    _FONTS["ring_xs"] = tkfont.Font(root, family=FONT_RING_XS[0], size=FONT_RING_XS[1], weight="bold")
    _FONTS["cnum"] = tkfont.Font(
        root, family=FONT_COMPACT_NUM[0], size=FONT_COMPACT_NUM[1], weight="bold"
    )
    _FONTS["ctag"] = tkfont.Font(root, family=FONT_COMPACT_TAG[0], size=FONT_COMPACT_TAG[1])


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class Widget:
    DRAG_THRESHOLD = 4  # 位移小于该像素视为「单击」

    def __init__(self, root):
        self.root = root
        self.config = load_config()
        self.interval = self.config["interval"]
        self.opacity = self.config["opacity"]
        self.width = self.config["width"]
        self.busy = False
        self.busy_since = 0.0      # 本轮请求开始时间（看门狗用）
        self.topmost = True
        self.hidden = False
        self.last_error = ""
        self.results = queue.Queue()
        self.drag_origin = None
        self.win_origin = None
        self.moved = False
        # 显示模式：full = 三行完整面板，compact = 三段横条紧凑小窗
        self.mode = self.config["mode"]
        self.compact = self.mode == MODE_COMPACT
        # 宽度抓柄状态
        self.resize_origin = None
        self.press_width = self.width
        self.grip_press = False
        self.grip_cursor = ""
        # 贴边收缩状态
        self.edge_dock = self.config["edge_dock"]
        self.docked = None              # None / "left" / "right" / "top"
        self.hover_expanded = False     # 收缩后因悬停展开中
        self.hover_armed = True         # 指针离开收缩条后才允许再次展开
        self.leave_deadline = None      # 离开面板后自动收回的截止时刻
        # 托盘状态
        self.tray = None
        self.ring_state = "busy"        # busy / ok / error（左右圆环条）
        self.ring_used = 0

        # 高分屏缩放比例（96 DPI 为 1.0）
        try:
            dpi = float(root.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        self.scale = dpi / 96.0
        self.s = lambda v: int(round(v * self.scale))

        fonts_build(root)
        self._build_window()
        self._build_background()
        self._build_rows()
        self._build_status()
        self._build_compact()
        self._build_menu()
        self._bind_pointer(root)
        self._build_ring()
        self._relayout()              # 按启动时的模式决定初始形态（标准 / 小窗）
        self.tray = self._create_tray()   # 启动即驻留系统托盘

    # -- 窗口 ---------------------------------------------------------------
    def _build_window(self):
        root = self.root
        root.title(APP_TITLE)
        root.overrideredirect(True)
        self.transparent_ok = True
        try:
            root.attributes("-transparentcolor", TRANSPARENT_COLOR)
        except tk.TclError:
            self.transparent_ok = False
        # 透明度：与 -transparentcolor 互不影响（圆角镂空照常生效）
        try:
            root.attributes("-alpha", self.opacity)
        except tk.TclError:
            log("当前环境不支持 -alpha 透明度")
        root.attributes("-topmost", self.topmost)
        root.configure(bg=TRANSPARENT_COLOR if self.transparent_ok else C_BORDER)

        width, height = self._panel_px_size()
        vx0, vy0, vw, vh = self._virtual_screen()
        x = vx0 + vw - width - int(28 * self.scale)
        y = vy0 + int(vh * 0.10)
        x, y = self._clamp_pos(x, y, width, height)
        root.geometry("%dx%d+%d+%d" % (width, height, x, y))

    def _build_background(self):
        canvas = tk.Canvas(
            self.root,
            bg=TRANSPARENT_COLOR if self.transparent_ok else C_BORDER,
            highlightthickness=0,
            bd=0,
            width=self.s(self.width),
            height=self.s(H),
        )
        canvas.place(x=0, y=0, width=self.s(self.width), height=self.s(H))
        # 右侧 8px 隐形抓柄：只绑事件不画任何东西，光标提示可拖宽。
        # 跨控件移入时 Tk 只发 <Enter>，同控件内移动才发 <Motion>，两者都要判
        canvas.bind("<Motion>", self._on_grip_motion, add="+")
        canvas.bind("<Enter>", self._on_grip_motion, add="+")
        canvas.bind("<Leave>", self._on_grip_leave, add="+")
        canvas.bind("<Double-Button-1>", self._on_grip_double, add="+")
        self.bg_canvas = canvas
        self._draw_background()

    def _draw_background(self):
        """按当前宽度重绘圆角卡片。"""
        canvas = self.bg_canvas
        canvas.delete("all")
        round_rect(
            canvas,
            1, 1, self.s(self.width) - 1, self.s(H) - 1, self.s(R),
            fill=C_BG,
            outline=C_BORDER,
            width=max(1, self.s(1)),
        )

    def _build_rows(self):
        inner = self.width - PAD * 2
        holder = tk.Frame(self.root, bg=C_BG)
        holder.place(
            x=self.s(PAD),
            y=self.s(PAD_TOP),
            width=self.s(inner),
            height=self.s(ROW_H * 3 + ROW_GAP * 2),
        )
        self.row_holder = holder
        self.rows = []
        for index, (_key, title) in enumerate(ROWS):
            row = UsageRow(holder, self.s, title, _FONTS, inner)
            y = index * (self.s(ROW_H) + self.s(ROW_GAP))
            row.frame.place(x=0, y=y, width=self.s(inner), height=self.s(ROW_H))
            row.set_busy()
            self.rows.append(row)

    def _build_status(self):
        s = self.s
        frame = tk.Frame(self.root, bg=C_BG)
        frame.place(x=s(PAD), y=s(STATUS_Y), width=s(self.width - PAD * 2), height=s(STATUS_H))
        self.status_frame = frame

        self.dot = tk.Canvas(frame, width=s(9), height=s(9), bg=C_BG, highlightthickness=0, bd=0)
        self.dot.pack(side="left")
        self._draw_dot(C_DOT_BUSY)

        self.status_lbl = tk.Label(
            frame, text="正在加载…", font=_FONTS["status"], bg=C_BG, fg=C_SUB, anchor="w"
        )
        self.status_lbl.pack(side="left", padx=(s(6), 0))

        self.hint_lbl = tk.Label(
            frame, text="拖动 / 点击刷新 · 右键菜单", font=_FONTS["status"], bg=C_BG, fg=C_HINT, anchor="e"
        )
        self.hint_lbl.pack(side="right")

    def _draw_dot(self, color):
        c = self.dot
        c.delete("all")
        pad = self.s(1)
        c.create_oval(pad, pad, self.s(9) - pad, self.s(9) - pad, fill=color, outline="")

    def _build_compact(self):
        """小窗模式 / 顶部小条：三段「百分比 + 档位 + 横条」的画布（卡片背景自己画）。"""
        s = self.s
        canvas = tk.Canvas(
            self.root,
            width=s(COMPACT_W),
            height=s(COMPACT_H),
            bg=TRANSPARENT_COLOR if self.transparent_ok else C_BORDER,
            highlightthickness=0,
            bd=0,
        )
        canvas.place(x=0, y=0, width=s(COMPACT_W), height=s(COMPACT_H))
        canvas.place_forget()
        # place / 模式切换都会改变尺寸，交给 Configure 重绘，保证拿到真实宽高
        canvas.bind("<Configure>", self._draw_compact, add="+")
        self.compact_canvas = canvas
        self._draw_compact()

    def _fit_ring_font(self, text, inner_px):
        """按可用内径挑选字号，保证 “100%” 也不会撑出圆环。"""
        font = _FONTS["ring_xs"]
        for name in ("ring", "ring_sm", "ring_xs"):
            font = _FONTS[name]
            if font.measure(text) <= inner_px:
                break
        return font

    def _draw_compact(self, _event=None):
        """
        小窗模式 / 顶部小条：三等分段横向排列，段间 1px 浅灰竖分隔线。

        每段两行：
          上行  「剩余百分比 + 档位名」，如 `98% 5h`（百分比深色 10pt 半粗，档位灰 9pt）
          下行  一根细横条，长度 = 该档**剩余**百分比（100% 填满、0% 为空）
        错误档：整段文字变红、横条变红（浅红底）。
        """
        canvas = self.compact_canvas
        canvas.delete("all")
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 16 or h < 16:
            return
        s = self.s
        # 贴顶吸附时顶部两角画成直角，与屏幕边缘严丝合缝
        top_flat = self.docked == "top" and not self.hover_expanded
        shape = round_rect_top_flat if top_flat else round_rect
        shape(
            canvas, 0.5, 0.5, w - 1.0, h - 1.0, s(R),
            fill=C_BG, outline=C_BORDER, width=max(1, s(1)),
        )
        seg_w = w / 3.0
        pad = s(COMPACT_PAD)
        gap = s(COMPACT_TEXT_GAP)
        text_y = s(COMPACT_TEXT_CY)
        bar_y = s(COMPACT_BAR_Y)
        bar_h = max(3, s(COMPACT_BAR_H))
        # 段间 1px 浅灰竖分隔线（上下留白，不顶到卡片边）
        sep_top = s(COMPACT_SEP_PAD)
        sep_bot = h - s(COMPACT_SEP_PAD)
        for i in (1, 2):
            x = seg_w * i
            canvas.create_line(x, sep_top, x, sep_bot, fill=COMPACT_SEP, width=1)

        for i, tag in enumerate(COMPACT_LABELS):
            x0 = seg_w * i
            cx = x0 + seg_w / 2.0
            row = self.rows[i] if i < len(self.rows) else None
            state = row.state if row is not None else "busy"
            used = int(row.percent) if row is not None else 0
            remaining = max(0, min(100, 100 - used))
            if state == "ok":
                num = "%d%%" % remaining
                num_color = C_TEXT
                tag_color = C_SUB
                track, fill = C_TRACK, C_GREEN
            elif state == "error":
                num = "—"
                num_color = tag_color = C_ERR
                track, fill = C_ERR_BG, C_ERR      # 浅红底 + 红条
                remaining = 100
            else:
                num = "—"
                num_color = tag_color = C_SUB
                track, fill = C_TRACK, None
                remaining = 0

            # 上行：百分比 + 档位名，整组在段内居中
            w_num = _FONTS["cnum"].measure(num)
            w_tag = _FONTS["ctag"].measure(tag)
            left = cx - (w_num + gap + w_tag) / 2.0
            canvas.create_text(
                left + w_num / 2.0, text_y, text=num,
                font=_FONTS["cnum"], fill=num_color, anchor="center",
            )
            canvas.create_text(
                left + w_num + gap + w_tag / 2.0, text_y, text=tag,
                font=_FONTS["ctag"], fill=tag_color, anchor="center",
            )

            # 下行：细横条，长度 = 剩余百分比（100% 填满、0% 为空）
            bx0, bx1 = x0 + pad, x0 + seg_w - pad
            bw = bx1 - bx0
            if bw < 4:
                continue
            round_rect(
                canvas, bx0, bar_y, bx1, bar_y + bar_h, bar_h / 2.0,
                fill=track, outline="",
            )
            if fill is not None and remaining > 0:
                fw = bw * remaining / 100.0
                fw = min(bw, max(fw, float(bar_h)))   # 有剩余就至少露出一个圆头
                round_rect(
                    canvas, bx0, bar_y, bx0 + fw, bar_y + bar_h, bar_h / 2.0,
                    fill=fill, outline="",
                )

    def _build_menu(self):
        menu = tk.Menu(
            self.root,
            tearoff=0,
            font=_FONTS["menu"],
            bg="#ffffff",
            fg="#333333",
            activebackground=C_BADGE_BG,
            activeforeground=C_BADGE_FG,
            bd=1,
            relief="solid",
        )
        menu.add_command(label="立即刷新", command=self.refresh)
        menu.add_command(label="取消置顶", command=self.toggle_topmost)   # 索引 1
        menu.add_command(label="隐藏窗口", command=self.hide_window)     # 索引 2

        # 贴边自动收缩开关（索引 3），勾选状态写回 config.json 的 edge_dock
        self.edge_var = tk.BooleanVar(value=self.edge_dock)
        menu.add_checkbutton(
            label="贴边自动收缩",
            variable=self.edge_var,
            command=self.toggle_edge_dock,
        )

        # 小窗模式开关（索引 4），勾选状态写回 config.json 的 mode
        self.mode_var = tk.BooleanVar(value=self.compact)
        menu.add_checkbutton(
            label="小窗模式",
            variable=self.mode_var,
            command=self.toggle_compact,
        )

        # 透明度级联子菜单：勾选当前值
        submenu = tk.Menu(
            menu,
            tearoff=0,
            font=_FONTS["menu"],
            bg="#ffffff",
            fg="#333333",
            activebackground=C_BADGE_BG,
            activeforeground=C_BADGE_FG,
            bd=1,
            relief="solid",
        )
        self.opacity_vars = []
        for step in OPACITY_STEPS:
            var = tk.BooleanVar(value=abs(step - self.opacity) < 0.005)
            submenu.add_checkbutton(
                label="%d%%" % round(step * 100),
                variable=var,
                command=lambda value=step: self.set_opacity(value),
            )
            self.opacity_vars.append(var)
        menu.add_cascade(label="透明度", menu=submenu)
        self.opacity_menu = submenu

        menu.add_separator()
        menu.add_command(label="退出", command=self.quit)
        self.menu = menu

    # -- 指针交互 -----------------------------------------------------------
    def _bind_pointer(self, widget):
        """
        指针事件只在根窗口上绑定一次。

        Tk 会把 toplevel（根窗口）的绑定标签自动附加到所有子孙控件后面，
        如果再递归绑到每个子控件，同一个事件会触发两遍回调（抓柄松开时会
        多触发一次刷新、右键菜单会重复弹出）。
        """
        widget.bind("<ButtonPress-1>", self._on_press, add="+")
        widget.bind("<B1-Motion>", self._on_drag, add="+")
        widget.bind("<ButtonRelease-1>", self._on_release, add="+")
        widget.bind("<Button-3>", self._on_menu, add="+")
        widget.bind("<Button-2>", self._on_menu, add="+")

    def _in_grip(self, event):
        """事件是否落在窗口右侧约 8px 的隐形抓柄上。"""
        if self.compact:
            return False   # 小窗模式固定宽度，没有抓柄
        if self.docked is not None:
            return False   # 收缩态没有抓柄
        try:
            rel_x = event.x_root - self.root.winfo_x()
            width = self.root.winfo_width()
        except tk.TclError:
            return False
        if width <= 2:
            return False
        return rel_x >= width - max(self.s(GRIP_W), 8)

    def _on_grip_motion(self, event):
        """鼠标在抓柄上悬停时变成横向箭头光标。"""
        limit = self.bg_canvas.winfo_width() - max(self.s(GRIP_W), 8)
        cursor = "sb_h_double_arrow" if event.x >= limit else ""
        if cursor != self.grip_cursor:
            self.grip_cursor = cursor
            self.bg_canvas.configure(cursor=cursor)
            if os.environ.get("OPENCODE_WIDGET_DEBUG") == "1":
                log("grip cursor -> %r (x=%d limit=%d)" % (cursor, event.x, limit))

    def _on_grip_leave(self, _event):
        if self.grip_cursor:
            self.grip_cursor = ""
            self.bg_canvas.configure(cursor="")
            if os.environ.get("OPENCODE_WIDGET_DEBUG") == "1":
                log("grip cursor -> '' (leave)")

    def _on_grip_double(self, event):
        """双击抓柄恢复默认宽度。"""
        if not self._in_grip(event):
            return
        if self.width != W:
            self._set_width(W)
            save_config({"width": int(self.width)})

    def _on_press(self, event):
        if self._in_grip(event):
            # 抓柄按下：进入「改宽」模式，不移动窗口
            self.grip_press = True
            self.resize_origin = event.x_root
            self.press_width = self.width
            self.drag_origin = None
            self.win_origin = None
            self.moved = False
            return
        self.grip_press = False
        self.resize_origin = None
        self.drag_origin = (event.x_root, event.y_root)
        self.win_origin = (self.root.winfo_x(), self.root.winfo_y())
        self.moved = False
        if os.environ.get("OPENCODE_WIDGET_DEBUG") == "1":
            log("press root=%s,%s win=%s" % (event.x_root, event.y_root, self.win_origin))

    def _on_drag(self, event):
        if self.resize_origin is not None:
            dx = event.x_root - self.resize_origin
            self._set_width(self.press_width + int(round(dx / self.scale)))
            return
        if not self.drag_origin or not self.win_origin:
            return
        dx = event.x_root - self.drag_origin[0]
        dy = event.y_root - self.drag_origin[1]
        if not self.moved and abs(dx) + abs(dy) <= self.s(self.DRAG_THRESHOLD):
            return
        if not self.moved and os.environ.get("OPENCODE_WIDGET_DEBUG") == "1":
            log("drag start dx=%d dy=%d scale=%.2f" % (dx, dy, self.scale))
        self.moved = True
        if self.docked is not None and not self.hover_expanded:
            # 按住收缩条拖动：先滑出面板，再从当前位置继续拖，避免跳位
            self._expand_dock()
            self.drag_origin = (event.x_root, event.y_root)
            self.win_origin = (self.root.winfo_x(), self.root.winfo_y())
            dx = dy = 0
        x = self.win_origin[0] + dx
        y = self.win_origin[1] + dy
        # 拖动边界：不许拖出虚拟屏（左/上到边即停，右/下也不越界）
        px_w = self.root.winfo_width() or self.s(self.width)
        px_h = self.root.winfo_height() or self.s(H)
        x, y = self._clamp_pos(x, y, px_w, px_h)
        self.root.geometry("+%d+%d" % (x, y))

    def _on_release(self, _event):
        if self.grip_press:
            # 松开抓柄：仅在宽度确实变了时写回 config.json
            changed = self.width != self.press_width
            self.grip_press = False
            self.resize_origin = None
            self.drag_origin = None
            self.win_origin = None
            if changed:
                save_config({"width": int(self.width)})
            return
        if os.environ.get("OPENCODE_WIDGET_DEBUG") == "1":
            log("release moved=%s win=%s,%s" % (self.moved, self.root.winfo_x(), self.root.winfo_y()))
        if not self.moved:
            self.refresh()
        else:
            self._maybe_dock()      # 拖动松手：贴边吸附 / 拖离恢复
        self.drag_origin = None
        self.win_origin = None

    def _on_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # -- 交互动作 -----------------------------------------------------------
    def toggle_topmost(self):
        self.topmost = not self.topmost
        self.root.attributes("-topmost", self.topmost)
        self.menu.entryconfig(1, label="取消置顶" if self.topmost else "恢复置顶")

    def set_opacity(self, value):
        """调整整窗透明度（0.5~1.0），实时生效并写回 config.json。"""
        opacity = round(clamp_number(value, OPACITY_MIN, OPACITY_MAX, 1.0), 2)
        self.opacity = opacity
        try:
            self.root.attributes("-alpha", opacity)
        except tk.TclError:
            log("设置透明度失败：%.2f" % opacity)
        for step, var in zip(OPACITY_STEPS, self.opacity_vars):
            var.set(abs(step - opacity) < 0.005)
        save_config({"opacity": opacity})

    def _set_width(self, logical_width):
        """按逻辑像素调整窗口宽度（高度不变），并重排内容。"""
        if self.compact:
            return                      # 小窗模式固定宽度（300），改宽请切回标准模式
        if self.docked is not None and not self.hover_expanded:
            return                      # 收缩态（圆环条 / 顶部小条）不改宽度
        width = int(round(clamp_number(logical_width, W_MIN, W_MAX, float(W))))
        if width == self.width:
            return
        self.width = width
        px_width = self.s(width)
        y = self.root.winfo_y()
        vx0, _vy0, vw, vh = self._virtual_screen()
        # 右侧留出抓柄余量，超出虚拟屏时把窗口往左挪，保证抓柄始终够得着
        max_x = vx0 + vw - px_width - self.s(GRIP_W + 8)
        x = min(self.root.winfo_x(), max(vx0, max_x))
        if self.docked == "right":
            x = vx0 + vw - px_width     # 悬停展开时改宽也要贴住右边缘
        elif self.docked == "left":
            x = vx0
        elif self.docked == "top":
            # 顶部小条水平保持原中心，始终贴住上边缘
            cx = self.root.winfo_x() + (self.root.winfo_width() or px_width) // 2
            x = cx - px_width // 2
            y = _vy0
        x, y = self._clamp_pos(x, y, px_width, self.root.winfo_height() or self.s(H))
        self.root.geometry("%dx%d+%d+%d" % (px_width, self.root.winfo_height() or self.s(H), x, y))
        self._relayout()

    def _relayout(self):
        """
        按当前状态重排，四种形态：
        1. 未吸附 + 标准模式  -> 完整三行面板
        2. 未吸附 + 小窗模式  -> 三段横条紧凑小窗
        3. 左右吸附           -> 36px 圆环条（悬停展开为 1/2 的面板）
        4. 上边吸附           -> 顶部小条（三段横条，悬停展开为 1/2 的面板）
        """
        if self.docked is not None and not self.hover_expanded:
            if self.docked == "top":
                self._layout_topbar()
            else:
                self._layout_dock()
        else:
            self._layout_panel()

    def _hide_all_layers(self):
        """先全部收起，再按需要显示的层逐个摆放。"""
        for widget in (self.bg_canvas, self.row_holder, self.status_frame,
                       self.ring_canvas, self.compact_canvas):
            widget.place_forget()

    def _layout_panel(self):
        """展开态布局：标准 = 卡片 + 三行 + 状态栏；小窗 = 三段横条紧凑小窗。"""
        s = self.s
        self._hide_all_layers()
        if self.compact:
            self.compact_canvas.place(x=0, y=0, width=s(COMPACT_W), height=s(COMPACT_H))
            self.root.update_idletasks()
            self._draw_compact()
            return
        inner = self.width - PAD * 2
        self.bg_canvas.place(x=0, y=0, width=s(self.width), height=s(H))
        self.row_holder.place(
            x=s(PAD),
            y=s(PAD_TOP),
            width=s(inner),
            height=s(ROW_H * 3 + ROW_GAP * 2),
        )
        for index, row in enumerate(self.rows):
            y = index * (s(ROW_H) + s(ROW_GAP))
            row.frame.place(x=0, y=y, width=s(inner), height=s(ROW_H))
            row.set_width(inner)
        self.status_frame.place(x=s(PAD), y=s(STATUS_Y), width=s(inner), height=s(STATUS_H))
        self._draw_background()

    def _layout_dock(self):
        """左右吸附的收缩态：只留下 36px 宽的圆环条。"""
        s = self.s
        self._hide_all_layers()
        self.ring_canvas.place(x=0, y=0, width=s(EDGE_W), height=s(EDGE_H))
        self.root.update_idletasks()      # 让圆环立刻拿到真实宽高并完成绘制
        self._draw_ring()

    def _layout_topbar(self):
        """上边吸附的收缩态：贴顶的悬浮小条，内容是三段横条（顶部平角）。"""
        s = self.s
        self._hide_all_layers()
        self.compact_canvas.place(x=0, y=0, width=s(COMPACT_W), height=s(COMPACT_H))
        self.root.update_idletasks()
        self._draw_compact()

    # -- 显示 / 隐藏（配合托盘图标） ---------------------------------------
    def hide_window(self):
        """收起窗口，只留托盘图标；后台轮询继续跑，数据不停。"""
        if self.hidden:
            return
        # 菜单命令执行期间菜单可能还没来得及收起，先显式收起，避免残留
        try:
            self.menu.unpost()
        except tk.TclError:
            pass
        self.hidden = True
        try:
            self.root.withdraw()
        except tk.TclError:
            log("隐藏窗口失败：\n" + traceback.format_exc())
        if self.tray is not None:
            self.tray.set_hidden(True)

    def show_window(self, focus=True):
        """从托盘展开：还原窗口 + 恢复置顶（收缩态则还原成圆环条）。"""
        if self.hidden:
            self.hidden = False
            try:
                self.root.deiconify()
            except tk.TclError:
                log("展开窗口失败：\n" + traceback.format_exc())
        if self.tray is not None:
            self.tray.set_hidden(False)
        self._raise_window()
        if focus:
            try:
                self.root.focus_force()
            except tk.TclError:
                pass

    def toggle_visibility(self, *_args):
        if self.hidden:
            self.show_window(focus=True)
        else:
            self.hide_window()

    def _raise_window(self):
        """重新断言置顶状态，避免被其它窗口盖住。"""
        try:
            # 先把挂起的移动 / 缩放落地：否则 -topmost 的窗口重建会用旧坐标
            # 把刚设置好的贴边位置又拉回去（实测 geometry + topmost 必现）
            self.root.update_idletasks()
            self.root.attributes("-topmost", False)
            self.root.attributes("-topmost", self.topmost)
            self.root.lift()
        except tk.TclError:
            pass

    # -- 贴边自动收缩 -------------------------------------------------------
    def toggle_edge_dock(self):
        """右键菜单里的「贴边自动收缩」开关，写回 config.json 的 edge_dock。"""
        self.edge_dock = not self.edge_dock
        self.edge_var.set(self.edge_dock)
        save_config({"edge_dock": bool(self.edge_dock)})
        if not self.edge_dock and self.docked is not None:
            self._undock()

    # -- 显示模式（标准 / 小窗） ---------------------------------------------
    def toggle_compact(self):
        """右键菜单里的「小窗模式」开关，写回 config.json 的 mode。"""
        self.compact = not self.compact
        self.mode = MODE_COMPACT if self.compact else MODE_FULL
        self.mode_var.set(self.compact)
        save_config({"mode": self.mode})
        self._switch_mode()
        if os.environ.get("OPENCODE_WIDGET_DEBUG") == "1":
            log("mode -> %s" % self.mode)

    def _switch_mode(self):
        """
        标准 <-> 小窗的布局重排：
        - 收缩态（左右圆环条 / 顶部小条）只换内容，几何不动；
        - 展开态按新尺寸重设几何，以窗口中心为锚点，保证不跳出屏幕。
        """
        s = self.s
        if self.docked is not None and not self.hover_expanded:
            self._relayout()
            return
        cur_w = self.root.winfo_width() or s(COMPACT_W)
        cur_h = self.root.winfo_height() or s(COMPACT_H)
        cx = self.root.winfo_x() + cur_w // 2
        cy = self.root.winfo_y() + cur_h // 2
        px_w, px_h = self._panel_px_size()
        if self.docked is not None:
            x, y = self._dock_anchor(self.docked, px_w, px_h, cx, cy)
        else:
            x, y = self._clamp_pos(cx - px_w // 2, cy - px_h // 2, px_w, px_h)
        self.root.geometry("%dx%d+%d+%d" % (px_w, px_h, x, y))
        self._relayout()
        self._raise_window()

    # -- 屏幕坐标 / 拖动边界 -------------------------------------------------
    def _virtual_screen(self):
        """
        虚拟屏（多显示器合并后的桌面）范围，返回 (x0, y0, width, height)。

        用 GetSystemMetrics(SM_XVIRTUALSCREEN 等) 取真实值——副屏在主屏左侧 /
        上方时原点是负数，所以不能用主屏尺寸或 0 硬限制。
        """
        if _user32 is not None:
            try:
                x0 = int(_user32.GetSystemMetrics(76))   # SM_XVIRTUALSCREEN
                y0 = int(_user32.GetSystemMetrics(77))   # SM_YVIRTUALSCREEN
                w = int(_user32.GetSystemMetrics(78))    # SM_CXVIRTUALSCREEN
                h = int(_user32.GetSystemMetrics(79))    # SM_CYVIRTUALSCREEN
                if w > 0 and h > 0:
                    return x0, y0, w, h
            except Exception:
                pass
        try:                                              # 兜底：Tk 虚拟根窗口
            x0 = int(self.root.winfo_vrootx())
            y0 = int(self.root.winfo_vrooty())
            w = int(self.root.winfo_vrootwidth())
            h = int(self.root.winfo_vrootheight())
        except Exception:
            x0 = y0 = 0
            w = int(self.root.winfo_screenwidth())
            h = int(self.root.winfo_screenheight())
        if w <= 0 or h <= 0:
            x0 = y0 = 0
            w = int(self.root.winfo_screenwidth())
            h = int(self.root.winfo_screenheight())
        return x0, y0, w, h

    def _screen_edges(self):
        """虚拟屏左右边界 (left, right)。"""
        x0, _y0, vw, _vh = self._virtual_screen()
        return x0, x0 + vw

    def _clamp_pos(self, x, y, px_w, px_h):
        """
        把窗口位置限制在虚拟屏内：左边 / 上边不越界（到边缘即停），
        右边 / 下边同样不许超出——拖到哪一边都会被挡住。
        """
        vx0, vy0, vw, vh = self._virtual_screen()
        x = int(round(x))
        y = int(round(y))
        if px_w < vw:
            x = max(vx0, min(x, vx0 + vw - px_w))
        else:
            x = vx0
        if px_h < vh:
            y = max(vy0, min(y, vy0 + vh - px_h))
        else:
            y = vy0
        return x, y

    def _clamp_y(self, center_y, px_h):
        """按窗口中心的 y 把窗口钳制在虚拟屏内。"""
        vx0, vy0, vw, vh = self._virtual_screen()
        top = int(center_y - px_h // 2)
        return max(vy0, min(top, vy0 + vh - px_h))

    # -- 各形态的窗口尺寸（逻辑 -> 物理像素） --------------------------------
    def _panel_px_size(self):
        """展开态（完整面板 / 小窗模式）的窗口像素尺寸。"""
        if self.compact:
            return self.s(COMPACT_W), self.s(COMPACT_H)
        return self.s(self.width), self.s(H)

    def _dock_px_size(self, side):
        """收缩态的窗口像素尺寸：左右 = 圆环条，上边 = 顶部小条。"""
        if side == "top":
            return self.s(COMPACT_W), self.s(COMPACT_H)
        return self.s(EDGE_W), self.s(EDGE_H)

    def _dock_anchor(self, side, px_w, px_h, center_x, center_y):
        """
        计算吸附后的窗口坐标。
        left/right 用屏幕边缘锚定；top 贴住虚拟屏上边缘、水平方向保持原中心。
        """
        vx0, vy0, vw, vh = self._virtual_screen()
        if side == "left":
            x = vx0
        elif side == "right":
            x = vx0 + vw - px_w
        else:   # top：水平保持原中心，不跳到别处
            x, _ = self._clamp_pos(center_x - px_w // 2, vy0, px_w, px_h)
        if side == "top":
            y = vy0
        else:
            y = self._clamp_y(center_y, px_h)
        return x, y

    def _maybe_dock(self):
        """
        拖动松手时的吸附判定，顺序 **上 -> 左 -> 右（上边优先）**。
        命中边缘则收缩；已吸附但被拖离则恢复正常尺寸。
        """
        if not self.edge_dock:
            if self.docked is not None:
                self._undock()
            return
        x0, x1 = self._screen_edges()
        _vx0, vy0, _vw, _vh = self._virtual_screen()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        w = self.root.winfo_width()
        thr = max(self.s(EDGE_THRESHOLD), 4)
        if -thr <= (y - vy0) <= thr:          # 上边优先
            self._dock("top")
            return
        if -thr <= (x - x0) <= thr:           # 左边
            self._dock("left")
            return
        if -thr <= (x1 - (x + w)) <= thr:     # 右边
            self._dock("right")
            return
        if self.docked is not None:
            self._undock()

    def _dock(self, side):
        """吸附到屏幕边缘并收缩：左右 = 36px 圆环条，上边 = 顶部悬浮小条。"""
        if self.docked == side and not self.hover_expanded:
            return
        cur_w = self.root.winfo_width() or self.s(self.width)
        cur_h = self.root.winfo_height() or self.s(H)
        cx = self.root.winfo_x() + cur_w // 2
        cy = self.root.winfo_y() + cur_h // 2
        px_w, px_h = self._dock_px_size(side)
        x, y = self._dock_anchor(side, px_w, px_h, cx, cy)
        self.docked = side
        self.hover_expanded = False
        self.hover_armed = False       # 松手时指针还在收缩条上，别立刻弹开
        self.leave_deadline = None
        self._relayout()
        self.root.geometry("%dx%d+%d+%d" % (px_w, px_h, x, y))
        self._raise_window()
        if os.environ.get("OPENCODE_WIDGET_DEBUG") == "1":
            log("dock %s -> %d,%d %dx%d" % (side, x, y, px_w, px_h))

    def _expand_dock(self):
        """悬停收缩条：面板从边缘滑出（贴边锚点不变），上边展开成当前模式的完整面板。"""
        if self.docked is None or self.hover_expanded:
            return
        px_w, px_h = self._panel_px_size()
        cur_w = self.root.winfo_width() or px_w
        cur_h = self.root.winfo_height() or px_h
        cx = self.root.winfo_x() + cur_w // 2
        cy = self.root.winfo_y() + cur_h // 2
        x, y = self._dock_anchor(self.docked, px_w, px_h, cx, cy)
        self.hover_expanded = True
        self.leave_deadline = None
        self._relayout()
        self.root.geometry("%dx%d+%d+%d" % (px_w, px_h, x, y))
        self._raise_window()
        if os.environ.get("OPENCODE_WIDGET_DEBUG") == "1":
            log("expand dock %s -> %d,%d %dx%d" % (self.docked, x, y, px_w, px_h))

    def _collapse_dock(self):
        """鼠标离开 800ms 后（或程序性收回）把面板收回边缘。"""
        if self.docked is None or not self.hover_expanded:
            return
        side = self.docked
        px_w, px_h = self._dock_px_size(side)
        cur_w = self.root.winfo_width() or px_w
        cur_h = self.root.winfo_height() or px_h
        cx = self.root.winfo_x() + cur_w // 2
        cy = self.root.winfo_y() + cur_h // 2
        x, y = self._dock_anchor(side, px_w, px_h, cx, cy)
        self.hover_expanded = False
        self.hover_armed = False
        self.leave_deadline = None
        self._relayout()
        self.root.geometry("%dx%d+%d+%d" % (px_w, px_h, x, y))
        if os.environ.get("OPENCODE_WIDGET_DEBUG") == "1":
            log("collapse dock %s -> %d,%d %dx%d" % (side, x, y, px_w, px_h))

    def _undock(self):
        """拖离边缘：恢复正常尺寸，窗口中心保持不动（刚好在鼠标下方）。"""
        if self.docked is None:
            return
        px_w, px_h = self._panel_px_size()
        cur_w = self.root.winfo_width() or px_w
        cur_h = self.root.winfo_height() or px_h
        cx = self.root.winfo_x() + cur_w // 2
        cy = self.root.winfo_y() + cur_h // 2
        x, y = self._clamp_pos(cx - px_w // 2, cy - px_h // 2, px_w, px_h)
        self.docked = None
        self.hover_expanded = False
        self.hover_armed = True
        self.leave_deadline = None
        self._relayout()
        self.root.geometry("%dx%d+%d+%d" % (px_w, px_h, x, y))
        self._raise_window()
        if os.environ.get("OPENCODE_WIDGET_DEBUG") == "1":
            log("undock -> %d,%d %dx%d" % (x, y, px_w, px_h))

    def _hover_poll(self):
        """贴边收缩的悬停状态机：200ms 轮询一次指针位置（不依赖 Enter/Leave）。"""
        try:
            if self.docked is not None and not self.hidden:
                self._check_hover()
        except tk.TclError:
            pass
        except Exception:
            log("悬停检测异常：\n" + traceback.format_exc())
        try:
            self.root.after(200, self._hover_poll)
        except tk.TclError:
            pass

    def _check_hover(self):
        pos = self._pointer_pos()
        inside = False
        if pos is not None:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            inside = x <= pos[0] < x + w and y <= pos[1] < y + h
        if self.hover_expanded:
            if inside:
                self.leave_deadline = None
            else:
                now = time.monotonic()
                if self.leave_deadline is None:
                    self.leave_deadline = now + EDGE_LEAVE_MS / 1000.0
                elif now >= self.leave_deadline:
                    self._collapse_dock()
        elif inside and self.hover_armed:
            self._expand_dock()
        elif not inside:
            self.hover_armed = True     # 指针离开过圆环条，才允许再次展开

    def _pointer_pos(self):
        pos = cursor_pos()
        if pos is not None:
            return pos
        try:                            # 没有 ctypes 时退回 Tk（坐标同为虚拟屏坐标）
            x = self.root.winfo_pointerx()
            y = self.root.winfo_pointery()
            if x >= 0 and y >= 0:
                return x, y
        except tk.TclError:
            pass
        return None

    def quit(self):
        # 先删托盘图标、结束托盘消息线程，再退 Tk 主循环
        if self.tray is not None:
            try:
                self.tray.remove()
                self.tray.join(1.0)
            except Exception as exc:
                log("移除托盘图标失败：%s" % exc)
        try:
            # 先退出主循环，避免窗口销毁后还派发 after 回调
            self.root.quit()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass  # 已销毁（菜单退出与重复退出可能先后各来一次）

    # -- 贴边圆环条 ---------------------------------------------------------
    def _build_ring(self):
        """收缩态露出的圆环条（默认 place_forget，_layout_dock 时才显示）。"""
        s = self.s
        canvas = tk.Canvas(
            self.root,
            width=s(EDGE_W),
            height=s(EDGE_H),
            bg=TRANSPARENT_COLOR if self.transparent_ok else C_BORDER,
            highlightthickness=0,
            bd=0,
        )
        canvas.place(x=0, y=0, width=s(EDGE_W), height=s(EDGE_H))
        canvas.place_forget()
        # place / 收缩展开都会改变尺寸，交给 Configure 重绘，保证拿到真实宽高
        canvas.bind("<Configure>", self._draw_ring, add="+")
        self.ring_canvas = canvas
        self._draw_ring()

    def _draw_ring(self, _event=None):
        """
        圆环进度条 = Rolling（5 小时）剩余额度，剩余 = 100 - 已用，环上文字如 “92%”。
        出错时整环变红；数据没到之前是灰色占位。
        """
        canvas = self.ring_canvas
        canvas.delete("all")
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 8 or h < 8:
            return
        s = self.s
        # 收缩后依然是同一张圆角白卡片，只是变窄了
        round_rect(
            canvas, 0.5, 0.5, w - 1.0, h - 1.0, s(R),
            fill=C_BG, outline=C_BORDER, width=max(1, s(1)),
        )
        stroke = max(3, s(4))
        r = min(w, h) / 2.0 - stroke / 2.0 - s(3)
        if r < 4:
            return
        cx, cy = w / 2.0, h / 2.0
        box = (cx - r, cy - r, cx + r, cy + r)
        track = C_ERR_BG if self.ring_state == "error" else C_TRACK
        canvas.create_oval(box, outline=track, width=stroke)
        if self.ring_state == "ok":
            remaining = max(0, min(100, 100 - self.ring_used))
            text = "%d%%" % remaining
            color = C_ERR if remaining == 0 else C_TEXT
            if remaining > 0:
                canvas.create_arc(
                    box, start=90, extent=-360.0 * remaining / 100.0,
                    style="arc", outline=C_GREEN, width=stroke,
                )
            else:
                # 100% 用完：没有绿色额度可画，整环按错误红提示
                canvas.create_arc(
                    box, start=90, extent=-359.9, style="arc", outline=C_ERR, width=stroke
                )
        elif self.ring_state == "error":
            text, color = "—", C_ERR
            canvas.create_arc(
                box, start=90, extent=-359.9, style="arc", outline=C_ERR, width=stroke
            )
        else:
            text, color = "…", C_SUB
        canvas.create_text(
            cx, cy, text=text, font=self._fit_ring_font(text, 2 * r - stroke - s(2)),
            fill=color,
        )

    # -- 系统托盘 -----------------------------------------------------------
    def _create_tray(self):
        """启动即驻留系统托盘；创建失败只记日志，小组件本体照常可用。"""
        if not TRAY_AVAILABLE:
            log("当前环境不支持系统托盘（需要 Windows + ctypes）")
            return None
        try:
            return TrayIcon("opencode-go 额度加载中…")
        except Exception:
            log("创建托盘图标失败：\n" + traceback.format_exc())
            return None

    def _drain_tray(self):
        """托盘线程只往队列丢消息，这里在 Tk 主线程里统一处理。"""
        if self.tray is None:
            return
        try:
            while True:
                item = self.tray.queue.get_nowait()
                if item == "toggle":
                    self.toggle_visibility()
                elif isinstance(item, tuple) and item[0] == "cmd":
                    self._on_tray_command(int(item[1]))
        except queue.Empty:
            pass
        except tk.TclError:
            pass

    def _on_tray_command(self, cmd):
        """托盘右键菜单（原生 TrackPopupMenu）返回的命令。"""
        if cmd == TRAY_CMD_TOGGLE:
            self.toggle_visibility()
        elif cmd == TRAY_CMD_REFRESH:
            self.refresh()
        elif cmd == TRAY_CMD_QUIT:
            self.quit()

    def _ensure_no_taskbar(self):
        """本体永远不进任务栏：overrideredirect + WS_EX_TOOLWINDOW，托盘才是入口。"""
        if _user32 is None:
            return
        try:
            wid = self.root.winfo_id()
            hwnd = _user32.GetAncestor(wid, 2) or _user32.GetParent(wid) or wid
            GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x00000080, 0x00040000
            ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            _user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            )
            _user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020,   # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
            )
        except Exception as exc:
            log("设置任务栏样式失败：%s" % exc)

    # -- 数据刷新 -----------------------------------------------------------
    def refresh(self, *_args):
        if self.busy:
            return
        self.busy = True
        self.busy_since = time.monotonic()
        self._draw_dot(C_DOT_BUSY)
        self.status_lbl.configure(text="正在更新…", fg=C_SUB)
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        payload = {"rows": None, "short": "", "detail": ""}
        try:
            token, source = resolve_token()
            if not token:
                raise UsageError(
                    "未找到 token：请设置 OPENCODE_TOKEN、config.json 或 opencode 登录",
                    "未配置 token",
                )
            usage = fetch_usage(token)
            payload = {"rows": parse_rows(usage), "short": "", "detail": "", "source": source}
        except UsageError as exc:
            payload = {"rows": None, "short": exc.short, "detail": exc.detail}
        except Exception:
            log(traceback.format_exc())
            payload = {"rows": None, "short": "内部错误", "detail": "内部错误，详见 widget.log"}
        self.results.put(payload)

    def _poll_results(self):
        try:
            self._drain_tray()      # 托盘的显示/隐藏、菜单、刷新请求
        except Exception:
            log("托盘消息处理异常：\n" + traceback.format_exc())
        try:
            while True:
                payload = self.results.get_nowait()
                try:
                    self._apply(payload)
                except Exception:
                    log("界面刷新异常：\n" + traceback.format_exc())
                    self.busy = False
        except queue.Empty:
            pass
        # 看门狗：DNS 解析或 TLS 握手挂住时（socket 超时管不到 getaddrinfo），
        # 不能让 busy 永远为 True，否则之后所有刷新都会被跳过、界面停在旧错误上。
        if self.busy and time.monotonic() - self.busy_since > REFRESH_WATCHDOG:
            log("请求超过 %d 秒未返回，强制复位等待状态" % REFRESH_WATCHDOG)
            self.busy = False
            for row in self.rows:
                row.set_error("请求超时")
            self._draw_dot(C_ERR)
            self.status_lbl.configure(text="更新失败 · 请求超时，稍后自动重试", fg=C_ERR)
            self.ring_state = "error"
            self._draw_ring()
            self._draw_compact()
        self.root.after(150, self._poll_results)

    def _tooltip_text(self, payload):
        """托盘悬停速览文案，例如：5h已用8%·2h25m后重置 | 周8% | 月9%。"""
        if payload.get("rows") is None:
            return payload.get("short") or "数据异常"
        parts = []
        for index, (label, data) in enumerate(zip(("5h", "周", "月"), payload["rows"])):
            if not data.get("ok"):
                parts.append("%s—" % label)
            elif index == 0:
                parts.append(
                    "%s已用%d%%·%s" % (label, data["percent"], resets_cn(data.get("resets")))
                )
            else:
                parts.append("%s%d%%" % (label, data["percent"]))
        return " | ".join(parts)

    def _apply(self, payload):
        stamp = datetime.now().strftime("%H:%M:%S")
        if payload.get("rows") is None:
            short = payload.get("short") or "数据异常"
            for row in self.rows:
                row.set_error(short)
            self._draw_dot(C_ERR)
            if short != self.last_error:
                log(payload.get("detail") or short)
                self.last_error = short
            self.status_lbl.configure(text="更新失败 %s · %s" % (stamp, short), fg=C_ERR)
            self.ring_state = "error"
        else:
            self.last_error = ""
            for row, data in zip(self.rows, payload["rows"]):
                if data["ok"]:
                    row.set_ok(data["percent"], data["resets"])
                else:
                    row.set_error(data["error"])
            all_ok = all(data["ok"] for data in payload["rows"])
            self._draw_dot(C_GREEN if all_ok else C_ERR)
            self.status_lbl.configure(text="更新于 %s" % stamp, fg=C_SUB)
            rolling = payload["rows"][0]
            if rolling["ok"]:
                self.ring_state = "ok"
                self.ring_used = rolling["percent"]
            else:
                self.ring_state = "error"
        self._draw_ring()           # 左右圆环条立刻跟上最新数据 / 错误态
        self._draw_compact()        # 小窗模式 / 顶部小条的三段横条同样刷新
        if self.tray is not None:
            try:
                self.tray.set_tip(self._tooltip_text(payload))   # 只改 szTip，不重建图标
            except Exception:
                log("托盘提示更新失败：\n" + traceback.format_exc())
        self.busy = False

    def _auto_refresh(self):
        try:
            self.refresh()
        except Exception:
            log("自动刷新异常：\n" + traceback.format_exc())
        try:
            self.root.after(self.interval * 1000, self._auto_refresh)
        except tk.TclError:
            pass

    # -- 启动 ---------------------------------------------------------------
    def start(self):
        self.root.after(150, self._poll_results)
        self.root.after(200, self._auto_refresh)
        self.root.after(300, self._hover_poll)          # 贴边收缩的悬停状态机
        self.root.after(350, self._ensure_no_taskbar)   # 本体绝不进任务栏


def main():
    root = tk.Tk()
    try:
        app = Widget(root)
    except Exception:
        log("初始化失败：\n" + traceback.format_exc())
        try:
            from tkinter import messagebox

            messagebox.showerror("opencode-go 额度小组件", "启动失败，详见 widget.log")
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        return 1
    app.start()
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
