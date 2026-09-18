# -*- coding: utf-8 -*-
"""Android 自动化脚本编辑器 - 后端服务

启动: python server.py  → 浏览器打开 http://127.0.0.1:5000
依赖: flask
工具: adb (需在 PATH), scrcpy (可选, 路径可在页面配置)
"""
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from flask import Flask, jsonify, request, send_from_directory, Response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")
APK_DIR = os.path.join(BASE_DIR, "adbkeyboard")
YADB_DIR = os.path.join(BASE_DIR, "yadb")
YADB_BIN = os.path.join(YADB_DIR, "yadb")           # 本地 YADB 二进制(免装 App, push 到设备用)
YADB_DEVICE_PATH = "/data/local/tmp/yadb"
YADB_MAIN = "com.ysbing.yadb.Main"
STATIC_DIR = os.path.join(BASE_DIR, "static")

for d in (SCRIPTS_DIR, EXPORTS_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__, static_folder=None)

SCRCPY_DEFAULT = r"D:\Program Files\scrcpy-win64-v3.3.4\scrcpy.exe"
ADBKEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
ADBKEYBOARD_URL = "https://github.com/senzhk/ADBKeyBoard/releases/latest/download/ADBKeyboard.apk"
_orig_ime = ""  # 首次启用 ADBKeyboard 时记录的原始输入法，脚本结束自动切回

# ---------------------------------------------------------------------------
# adb 封装
# ---------------------------------------------------------------------------

class AdbError(Exception):
    """adb 命令执行失败（如设备未找到/离线、命令报错）。"""


def adb(device, *args, binary=False, timeout=30, check=False):
    """执行 adb 命令。device 为空则用默认设备。数组传参，不经 shell。
    check=True 时 returncode!=0 抛 AdbError（用于真正执行动作的命令，便于“设备找不到就停止”）。"""
    cmd = ["adb"]
    if device:
        cmd += ["-s", device]
    cmd += list(args)
    if binary:
        return subprocess.run(cmd, capture_output=True, timeout=timeout).stdout
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = r.stdout.decode("utf-8", errors="replace")
    if check and r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        raise AdbError(err or f"adb 命令失败 (exit {r.returncode})")
    return out


ORIG_IME_FILE = os.path.join(APK_DIR, "orig_ime.txt")


def _load_orig_ime():
    global _orig_ime
    try:
        if os.path.isfile(ORIG_IME_FILE):
            _orig_ime = open(ORIG_IME_FILE, encoding="utf-8").read().strip()
    except Exception:
        pass


def _save_orig_ime():
    try:
        with open(ORIG_IME_FILE, "w", encoding="utf-8") as f:
            f.write(_orig_ime)
    except Exception:
        pass


def remember_ime(ime):
    """记住用户的原始输入法(非 ADBKeyboard)，供脚本结束切回。持久化到文件。"""
    global _orig_ime
    if ime and ime != ADBKEYBOARD_IME:
        _orig_ime = ime
        _save_orig_ime()


_load_orig_ime()  # 启动时读回已记录的原始输入法


def get_ime(device):
    """当前输入法。"""
    try:
        return adb(device, "shell", "settings", "get", "secure",
                   "default_input_method").strip()
    except Exception:
        return ""


def set_ime(device, ime):
    if ime:
        try:
            adb(device, "shell", "ime", "set", ime, check=True)
        except Exception:
            pass


def restore_ime(device, pre_ime):
    """切回原始输入法：优先用首次启用 ADBKeyboard 时记录的，否则用本次运行前的。"""
    target = _orig_ime or pre_ime or ""
    if target and target != ADBKEYBOARD_IME:
        set_ime(device, target)


def device_online(device):
    """设备是否在线。"""
    try:
        if not device:
            return len(get_devices()) > 0
        r = subprocess.run(["adb", "-s", device, "get-state"],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def get_devices():
    """返回 [{serial, model, status}]"""
    out = adb(None, "devices", "-l")
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial, status = parts[0], parts[1]
        model = ""
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p[6:]
        devices.append({"serial": serial, "model": model, "status": status})
    return devices


def online_devices():
    """当前在线(status=device)的设备列表。"""
    try:
        return [d for d in get_devices() if d["status"] == "device"]
    except Exception:
        return []


def resolve_device(req_device=None):
    """确定目标设备：请求指定的设备在线则用它；设备已掉线(如脚本里记录的旧序列号)
    且当前只有一台在线设备时，自动回退到这台，避免截图空白/操作报"设备未连接"。
    返回 None 表示无法确定（无在线设备，或多台在线但未指定/指定的已离线）。"""
    serials = [d["serial"] for d in online_devices()]
    if req_device and req_device in serials:
        return req_device
    if len(serials) == 1:
        return serials[0]          # 只有一台在线 → 直接用（含指定设备已离线的情况）
    if serials:
        return None                # 多台在线但没指定/指定的不在线，交给上层报错
    return None                    # 无在线设备


def device_err_msg(req_dev=""):
    """设备不可用时的统一提示：说清是请求的设备离线还是压根没设备。"""
    online = [d["serial"] for d in online_devices()]
    if req_dev and req_dev not in online:
        msg = f"设备 {req_dev} 已离线"
        return msg + (f"，当前在线: {', '.join(online)}" if online else "，且没有其他在线设备")
    if len(online) > 1:
        return f"多台设备在线({', '.join(online)})，请在顶栏选择要操作哪台"
    return "无在线设备，请连接设备后重试（adb devices 确认）"


# ---------------------------------------------------------------------------
# 截图 / 控件树
# ---------------------------------------------------------------------------

def take_screenshot(device):
    png = adb(device, "exec-out", "screencap", "-p", binary=True, timeout=30)
    if not png:
        raise AdbError(f"截图返回为空，设备可能已断开（{device or '默认设备'}）")
    return png


def parse_bounds(b):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return [x1, y1, x2, y2]


def uidump(device):
    """uiautomator dump 并解析为扁平节点列表。"""
    adb(device, "shell", "uiautomator", "dump", "/sdcard/window_dump.xml", timeout=20)
    xml_bytes = adb(device, "exec-out", "cat", "/sdcard/window_dump.xml", binary=True, timeout=15)
    try:
        root = ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return []

    nodes = []
    for el in root.iter("node"):
        rid = el.get("resource-id", "")
        text = el.get("text", "") or el.get("content-desc", "")
        clickable = el.get("clickable") == "true"
        bounds = parse_bounds(el.get("bounds", ""))
        # 过滤无意义节点：无 id 无文本不可点击，或面积为 0
        if not bounds or (bounds[2] - bounds[0] <= 0 or bounds[3] - bounds[1] <= 0):
            continue
        if not rid and not text and not clickable:
            continue
        nodes.append({
            "resource_id": rid,
            "text": text,
            "class": el.get("class", ""),
            "clickable": clickable,
            "focused": el.get("focused") == "true",
            "password": el.get("password") == "true",
            "bounds": bounds,
        })
    return nodes


def read_field(device, rid=""):
    """读取输入框当前文本(用于校验"清空/输入是否真的生效")。
    rid 为空时取当前有焦点的 EditText(拿不到焦点则取第一个 EditText)。
    返回 (rid, text, is_password)；取不到返回 (rid, None, False)。
    注意1: 空输入框在多数 ROM 上 text 会返回 hint 文案(如"请先输入学号"), 校验时要当成"空"。
    注意2: 密码框只能读到掩码(且末位常为刚输入的明文), 所以密码框只能按字符数校验。"""
    try:
        nodes = uidump(device)
    except Exception:
        return rid, None, False
    edits = [n for n in nodes if "EditText" in (n.get("class") or "")]
    if rid:
        for n in edits:
            if n["resource_id"] == rid:
                return n["resource_id"], n["text"], bool(n.get("password"))
        return rid, None, False
    for n in edits:
        if n.get("focused"):
            return n["resource_id"], n["text"], bool(n.get("password"))
    if edits:
        return edits[0]["resource_id"], edits[0]["text"], bool(edits[0].get("password"))
    return "", None, False


def is_edit_text(device, rid):
    """rid 是不是一个文本输入框(决定要不要做清空/输入校验)。"""
    try:
        return any("EditText" in (n.get("class") or "") and n["resource_id"] == rid
                   for n in uidump(device))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 步骤执行引擎（run_step / run_script 共用）
# ---------------------------------------------------------------------------

KEY_MAP = {
    "TAB": "61", "ENTER": "66", "BACK": "4", "HOME": "3",
    "DEL": "67", "DELETE": "67", "ESC": "111", "SPACE": "62",
    "UP": "19", "DOWN": "20", "LEFT": "21", "RIGHT": "22",
    "MENU": "82", "APP_SWITCH": "187", "VOL_UP": "24", "VOL_DOWN": "25",
}


def has_non_ascii(s):
    return any(ord(c) > 127 for c in s)


def sh_escape_input(text):
    """为 adb shell input text 转义：空格→%s，引号/反斜杠/特殊符号加反斜杠。"""
    out = text.replace(" ", "%s")
    for ch in ('"', "'", "\\", "<", ">", "(", ")", ";", "&", "|", "*", "$", "`"):
        out = out.replace(ch, "\\" + ch)
    return out


def downsample(path, maxn):
    """抽稀轨迹点，最多保留 maxn 个（保持首尾）。"""
    if len(path) <= maxn:
        return path
    step = (len(path) - 1) / (maxn - 1)
    return [path[round(i * step)] for i in range(maxn)]


def replay_swipe(device, path, duration_ms):
    """按记录的整条轨迹和速度回放滑动。
    用若干段 input swipe 在【一次 adb shell 会话】里拼接：input swipe 自带时长、
    单次进程即可完成一段直线，设备开销小(~30ms/段)且无需 Android12 的 motionevent，
    从而总时长≈录制时长(还原真实滑动速度)。"""
    dur = max(300, int(duration_ms))
    n_seg = min(14, max(3, dur // 90))   # 段数按时长自适应：短滑少段、长滑多段
    pts = downsample(path, n_seg + 1)
    n = len(pts)
    if n < 2:
        return False
    seg = max(20, int(dur / (n - 1)))   # 每段时长，总和≈录制时长
    parts = [f"input swipe {pts[i-1][0]} {pts[i-1][1]} {pts[i][0]} {pts[i][1]} {seg}"
             for i in range(1, n)]
    try:
        adb(device, "shell", " ; ".join(parts), check=True)
        return True
    except AdbError:
        return False


def resolve_tap_xy(device, step, info=None):
    """解析点击目标坐标：优先用 x/y，否则按控件定位。
    支持：resource_id + index(第N个匹配)、加 text 过滤(id+文字)、文字精确/包含匹配。
    info: 传入 dict 时, 会把命中控件的信息写进去(text/资源id), 供清空时估算按键数。
    返回 (x, y)；找不到控件时返回 None。"""
    if step.get("x") is not None and step.get("y") is not None:
        return int(step["x"]), int(step["y"])
    rid = step.get("resource_id") or ""
    text = step.get("text") or ""
    match = (str(step.get("text_match") or "exact")).lower()
    idx = int(step.get("index") or 0)
    if idx < 0:
        idx = 0
    k = 0
    for n in uidump(device):
        if rid and n["resource_id"] != rid:
            continue
        if not rid and not text:
            continue  # 无 id 又无文字，无法按控件定位（编辑器会回落为该控件中心坐标）
        if text:
            t = n["text"]
            if match == "contains":
                if text not in t:
                    continue
            elif t != text:
                continue
        if k == idx:
            if info is not None:
                info["text"] = n["text"]
                info["resource_id"] = n["resource_id"]
            b = n["bounds"]
            return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        k += 1
    return None


def resolve_tap_xy_retry(device, step, retries, info=None):
    """带重试的坐标解析：坐标型直接返回；控件型连续尝试若干次并在控制树刷新后重查。
    retries: 尝试次数（≥1），每次间隔 1 秒。找不到返回 None。"""
    if step.get("x") is not None and step.get("y") is not None:
        return int(step["x"]), int(step["y"])
    retries = max(1, int(retries or 0))
    for i in range(retries):
        xy = resolve_tap_xy(device, step, info)
        if xy:
            return xy
        if i + 1 < retries:
            time.sleep(1)  # 等界面刷新后重试
    return None


def adbkeyboard_installed(device):
    """设备上是否已安装 ADBKeyboard。"""
    try:
        return "adbkeyboard" in adb(device, "shell", "pm", "list", "packages", "adbkeyboard")
    except Exception:
        return False


def _dev_sh_quote(s):
    """设备端 shell 单引号转义。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def yadb_on_device(device):
    """设备 /data/local/tmp 下是否已有 yadb。"""
    try:
        adb(device, "shell", f"ls {YADB_DEVICE_PATH}", check=True)
        return True
    except Exception:
        return False


def yadb_ensure(device):
    """确保设备上能跑 yadb: 已有直接用, 没有则把项目里的 yadb 推上去。
    全程不装 App(只放一个文件到 /data/local/tmp), 限制安装应用的设备也能用。"""
    if yadb_on_device(device):
        return True
    if not os.path.isfile(YADB_BIN):
        return False
    try:
        adb(device, "push", YADB_BIN, YADB_DEVICE_PATH, check=True, timeout=60)
        return True
    except Exception:
        return False


def yadb_available(device):
    """YADB 可用 = 设备上已有, 或本地有 yadb 二进制可推送。"""
    return os.path.isfile(YADB_BIN) or yadb_on_device(device)


def yadb_run(device, args, timeout=60):
    """执行一条 YADB 子命令, 返回 stdout(成功与否看返回值里的关键字)。"""
    return adb(device, "shell",
               f"app_process -Djava.class.path={YADB_DEVICE_PATH} /data/local/tmp "
               f"{YADB_MAIN} {args}", check=True, timeout=timeout)


def yadb_input(device, text):
    """YADB 输入任意文本(含中文/符号): 内部实现是「写剪贴板 + 注入 Ctrl+V 粘贴」。
    免装 App, 但依赖系统允许注入 Ctrl 组合键 —— 实测华为 HarmonyOS 会拒绝(返回
    成功但什么都没输进去), 所以只当兜底, 并优先用 ADBKeyboard。
    返回 True 只代表剪贴板写入成功且已发起粘贴, 不代表目标控件真的收到了文本。"""
    if not yadb_ensure(device):
        return False
    try:
        out = yadb_run(device, f"-keyboard {_dev_sh_quote(text)}")
    except Exception:
        return False
    return "Copy text:true" in out


CLEAR_DEL_MAX = 200   # 一次清空最多发多少个 DEL(单个 adb 调用批量发, 全发完约 2 秒)

# 会写/删文本的步骤: 这些步骤执行期间需要接管输入法(否则拼音输入法会吞按键)
TEXT_STEP_ACTIONS = ("input", "tap_input", "tap_clear", "clear_text")

# 各输入/清空方式在日志里的显示名(排查"输错了/没输入"时一眼看出走的哪条路)
INPUT_METHOD_LABEL = {
    "yadb": "粘贴(YADB)",
    "adbkeyboard": "ADBKeyboard 广播",
    "input": "按键注入",
    "input_ime_on": "按键注入(输入法没关·可能被转成中文)",
}
CLEAR_METHOD_LABEL = {
    "skip": "无需清空(框内已空)",
    "keyevent": "按键清空",
    "keyevent+": "按键清空·补发",
    "adbkeyboard": "ADBKeyboard 广播",
}


def verify_tag(v):
    """校验结果在日志里的后缀: True=已确认生效, False=确认没生效, None=读不到控件无法确认。"""
    if v is True:
        return ""
    if v is False:
        return " ·未生效"
    return " ·未确认(读不到控件)"


# ---------------------------------------------------------------------------
# 输入法托管: 输入期间临时关掉输入法
#
# 为什么需要: 拼音类输入法(讯飞/搜狗/百度)会把 `input text` 注入的字母按键当成拼音
# 组词, 于是 "20260001" 进框变成 "啊安…"、数字还会被候选缓冲吞掉 —— 这就是
# "输英文数字结果成了中文/丢字"的根因。密码框(inputType=textPassword)不参与组词,
# 所以之前时好时坏。
# 关掉输入法后, 按键直接进控件, 原样落字(实测华为 HarmonyOS 15 有效)。
# 中文没法走按键注入(KeyCharacterMap 没有对应键值), 仍需 ADBKeyboard 或 YADB。
# ---------------------------------------------------------------------------

IME_OFF_FILE = os.path.join(APK_DIR, "ime_off.json")   # 记录"运行期间被临时关闭的输入法", 供崩溃后恢复


def list_enabled_imes(device):
    """当前已启用的输入法 id 列表。"""
    try:
        out = adb(device, "shell", "ime", "list", "-s")
    except Exception:
        return []
    return [l.strip() for l in out.replace("\r", "").splitlines() if "/" in l]


def _save_ime_off(state):
    try:
        with open(IME_OFF_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass


def _load_ime_off():
    try:
        if os.path.isfile(IME_OFF_FILE):
            return json.load(open(IME_OFF_FILE, encoding="utf-8"))
    except Exception:
        pass
    return None


def _clear_ime_off():
    try:
        if os.path.isfile(IME_OFF_FILE):
            os.remove(IME_OFF_FILE)
    except Exception:
        pass


def ime_off_begin(device):
    """临时关闭全部输入法(输入期间不被拼音输入法干扰)。
    成功返回 True; 关不掉(个别 ROM 不允许)返回 False, 并保持原状。
    关闭记录写进文件, 服务崩溃/被杀后下次启动或下次运行前会自动恢复。"""
    imes = list_enabled_imes(device)
    if not imes:
        return False
    recover_ime_if_needed(device)   # 清掉可能残留的旧记录(上次崩溃留下的), 避免重复关闭
    _save_ime_off({"device": device, "imes": imes, "default": get_ime(device)})
    for i in imes:
        adb(device, "shell", "ime", "disable", i, check=False)
    if list_enabled_imes(device):     # 还有输入法活着 → 关不掉, 恢复原状
        ime_off_end(device)
        return False
    return True


def ime_off_end(device):
    """把上次临时关闭的输入法恢复回来。"""
    st = _load_ime_off()
    if not st:
        return False
    for i in st.get("imes") or []:
        adb(device, "shell", "ime", "enable", i, check=False)
    dft = st.get("default") or ""
    if dft and dft not in ("null", "none"):
        adb(device, "shell", "ime", "set", dft, check=False)
    _clear_ime_off()
    return True


def recover_ime_if_needed(device):
    """上次运行没跑完(崩溃/强杀)导致输入法被关着 → 自动恢复, 免得设备连键盘都没有。"""
    st = _load_ime_off()
    if not st:
        return False
    if st.get("device") and device and st["device"] != device:
        return False
    return ime_off_end(device)


def input_capability(device):
    """决定本次运行的输入方式。
    'adbk': 装了 ADBKeyboard —— 一律用它的广播(commitText 整段提交, 中英文都稳)
    'off' : 没装 ADBKeyboard —— 输入期间临时关掉输入法, 让按键原样落字
    ''    : 步骤里不用写/删文本, 不用管
    返回 (mode, 说明)。"""
    if adbkeyboard_installed(device):
        return "adbk", "使用 ADBKeyboard 广播输入"
    if not yadb_available(device):
        return "off", "未装 ADBKeyboard 且 YADB 不可用: 输入期间临时关掉输入法(中文会失败)"
    return "off", "未装 ADBKeyboard: 输入期间临时关掉输入法(拼音输入法会吞按键)"


def press_clear(device, n):
    """MOVE_END + 批量 DEL：所有按键在【一次】adb 调用里发完。
    这是同步操作 —— 命令返回时按键已注入并处理完，不像广播那样"喊完就走"。
    旧实现是"循环 N 次、每次一条 adb 命令"(每次 ~0.15s → 清一次等 30 秒), 这是
    "点执行后卡很久"的主因。"""
    adb(device, "shell", "input", "keyevent", *(["123"] + ["67"] * max(1, int(n))), check=True)


def broadcast_clear(device):
    """ADBKeyboard 的整段清空广播。异步：不保证 am broadcast 返回时已经清完。"""
    adb(device, "shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT", check=True)


def do_clear(device, use_adbk=False, dels=None, rid="", before=None, verify=True):
    """清空输入框，返回 (方式, 校验结果True/False/None)。

    为什么不再首选 ADBKeyboard 广播：`am broadcast` 是 fire-and-forget，返回只代表
    "喊了一声"，输入法还没处理；紧接着的输入会和它抢顺序 —— 实测表现就是
    "清空没生效(内容一直往后累加)"或"刚输进去的字又被清掉"。
    按键清空是同步的，返回即完成，所以改为主用；广播只作兜底。

    before: 调用方已知的框内文本(省一次控件树 dump)；None 表示自己读。
    verify: 是否回读控件文本确认真的清干净了。"""
    if before is not None and before == "":
        return "skip", True
    n = CLEAR_DEL_MAX if not dels else max(4, min(int(dels), CLEAR_DEL_MAX))
    press_clear(device, n)
    if not verify:
        return "keyevent", None
    _r, after, _pw = read_field(device, rid)
    if after is None or after == "" or after != before:
        # 读不到控件 / 已空 / 内容变了(变成 hint 或变短) → 清空生效
        return "keyevent", True
    press_clear(device, CLEAR_DEL_MAX)          # 原地没动 → 补发一轮
    _r, after2, _pw = read_field(device, rid)
    if after2 is None or after2 == "" or after2 != before:
        return "keyevent+", True
    if use_adbk and adbkeyboard_installed(device):
        broadcast_clear(device)
        time.sleep(0.4)
        _r, after3, _pw = read_field(device, rid)
        if after3 is None or after3 == "" or after3 != before:
            return "adbkeyboard", True
    # 两轮按键 + 广播都没让内容变化：框里本来就没内容(显示的是 hint)，或按键被输入法吞了
    return "keyevent", None


def do_input(device, text, mode="", ime_off=False, rid="", verify=False):
    """输入文本，返回 (方式, 校验结果True/False/None)。

    路线(同步优先, 同步的返回即已落字；异步的必须回读校验):
      1) ASCII → 原生 input text。此前保证输入法不吞按键：装了 ADBKeyboard 就切到它
         (它只是转发广播、不组词)，没装则输入期间临时关掉输入法。
      2) 中文 → ADBKeyboard 广播(输入法通道整段提交) > YADB 粘贴(免装 App)。
         两者都是异步，所以强制回读校验，没进去就重发一次。
    verify: 强制回读校验(异步方式默认就会校验)。"""
    method = ""
    if not has_non_ascii(text):
        adb(device, "shell", "input", "text", sh_escape_input(text), check=True)
        method = "input" if (ime_off or mode == "adbk") else "input_ime_on"
    elif mode == "adbk" and adbkeyboard_installed(device):
        adb(device, "shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT",
            "--es", "msg", text, check=True)
        method = "adbkeyboard"
    elif yadb_input(device, text):
        method = "yadb"
    elif adbkeyboard_installed(device):     # 万一 ADBKeyboard 正好是当前输入法
        adb(device, "shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT",
            "--es", "msg", text, check=True)
        method = "adbkeyboard"
    else:
        raise AdbError(
            "中文输入失败: 该设备未装 ADBKeyboard, 且 YADB 不可用。"
            "解决办法: 用编辑器顶栏的 IME 按钮一键安装 ADBKeyboard(仅 17KB 的输入法), "
            "或手动 adb install -r adbkeyboard/ADBKeyboard.apk")

    async_method = method in ("adbkeyboard", "yadb")
    ok = None
    if verify or async_method:
        _r, after, is_pwd = read_field(device, rid)
        ok = text_landed(after, text, is_pwd)
        if ok is False and async_method:
            time.sleep(0.35)            # 等上一发异步消息落定再重发, 避免又抢顺序
            if method == "adbkeyboard":
                adb(device, "shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT",
                    "--es", "msg", text, check=True)
            else:
                yadb_input(device, text)
            time.sleep(0.35)
            _r, after2, is_pwd2 = read_field(device, rid)
            if text_landed(after2, text, is_pwd2):
                return method + "+重发", True
    return method, ok


def text_landed(after, text, is_pwd=False):
    """目标文本是否真的进了控件。after=None 表示读不到控件(不阻塞, 返回 None)。
    密码框读不到明文, 只能按字符数判断(掩码位数=输入位数)。"""
    if after is None:
        return None
    if is_pwd:
        return after != "" and len(after) == len(text)
    if after == text:
        return True
    return bool(text) and text in after


def input_ok(before, after, text, is_pwd=False):
    """清空+输入后的综合校验：既确认写进去了，也确认旧内容没残留(清空失败的典型症状)。"""
    if after is None:
        return None
    if not is_pwd and before and before not in ("", after) and before in after:
        return False        # 旧内容还在 → 清空没生效
    return text_landed(after, text, is_pwd)


def hide_keyboard(device):
    """若软键盘当前可见(任意输入法)则发 BACK 收起；不可见则不动(避免误导航)。
    返回是否实际收起。"""
    try:
        out = adb(device, "shell", "dumpsys", "input_method")
        shown = re.search(r"mInputShown=(true|1)", out)
        if shown:
            pass
        else:
            vis = re.search(r"mImeWindowVis=0x([0-9a-fA-F]+)", out)
            if not vis or int(vis.group(1), 16) == 0:
                return False  # 键盘未显示
        adb(device, "shell", "input", "keyevent", "4")  # BACK 收起键盘
        return True
    except Exception:
        return False


def run_step(device, step, hide_input_keyboard=True, default_retry=3,
             input_mode="", ime_off=False):
    """执行单条步骤，返回 dict(action, ok, detail, ms)。
    hide_input_keyboard: 输入类步骤结束后是否自动收起软键盘。
    default_retry: 控件没找到时的默认重试次数(下拉一次控件树等1秒)，单步可用 retry 覆盖。
    input_mode: 'adbk'=已切到 ADBKeyboard(用它广播输入); ''=按需自动选择
    ime_off: 输入期间输入法是否已被临时关闭(影响 input text 是否会被拼音输入法干扰)"""
    action = step.get("action")
    retries = step.get("retry") or default_retry
    retries = max(1, int(retries or 0))
    t0 = time.time()
    detail = ""
    ok = True
    try:
        if action == "tap":
            xy = resolve_tap_xy_retry(device, step, retries)
            if not xy:
                ok, detail = False, f"未找到控件(试了{retries}次): {step.get('resource_id') or step.get('text')}"
            else:
                adb(device, "shell", "input", "tap", str(xy[0]), str(xy[1]), check=True)
                detail = f"tap {xy[0]},{xy[1]}"
        elif action == "tap_id":
            xy = resolve_tap_xy_retry(device, step, retries)
            if not xy:
                ok = False
                detail = f"未找到控件(试了{retries}次): {step.get('resource_id') or step.get('text')}"
            else:
                adb(device, "shell", "input", "tap", str(xy[0]), str(xy[1]), check=True)
                rid = step.get("resource_id") or step.get("text") or ""
                detail = f"tap_id {rid} → {xy[0]},{xy[1]}"
        elif action == "input":
            text = str(step.get("text", ""))
            if not text:
                ok = False
                detail = "input 文本为空"
            else:
                m, v = do_input(device, text, mode=input_mode, ime_off=ime_off)
                hid = hide_keyboard(device) if hide_input_keyboard else False
                if v is False:
                    ok = False
                    detail = (f"input {text} 没落进输入框(方式:{INPUT_METHOD_LABEL.get(m, m)})"
                              f"—— 多为输入法把按键吞了, 换成「点击并输入」步骤或安装 ADBKeyboard 更稳")
                else:
                    detail = (f"input {text} ·{INPUT_METHOD_LABEL.get(m, m)}{verify_tag(v)}"
                              + (" ·收起键盘" if hid else ""))
        elif action == "clear_text":
            frid, fb, _fpw = read_field(device, "")
            cm, cv = do_clear(device, use_adbk=(input_mode == "adbk"), rid=frid, before=fb)
            if cv is False:
                ok = False
                detail = "clear_text 没生效(框内内容没变化): 按键被输入法吞了或该控件不是输入框"
            else:
                detail = f"clear_text ·{CLEAR_METHOD_LABEL.get(cm, cm)}{verify_tag(cv)}"
        elif action in ("tap_clear", "tap_input"):
            # 组合步骤：先聚焦目标(坐标或控件)，再清空、可选输入；完成后回读校验，没生效自动重试
            info = {}
            xy = resolve_tap_xy_retry(device, step, retries, info)
            if not xy:
                ok = False
                detail = f"未找到控件(试了{retries}次): {step.get('resource_id') or step.get('text')}"
            else:
                target = step.get("resource_id") or step.get("text") or f"{xy[0]},{xy[1]}"
                rid = info.get("resource_id") or ""
                cur = info.get("text") or ""
                text = str(step.get("input", "")) if action == "tap_input" else ""
                if action == "tap_input" and not text:
                    ok, detail = False, "tap_input 文本为空"
                else:
                    cm = m = ""
                    v, after, attempt = None, None, 1
                    for attempt in (1, 2):
                        adb(device, "shell", "input", "tap", str(xy[0]), str(xy[1]), check=True)
                        if attempt == 1:
                            cm, _ = do_clear(device, use_adbk=(input_mode == "adbk"),
                                             dels=(len(cur) + 4) if cur else None,
                                             rid=rid, before=cur, verify=False)
                        else:
                            # 重试: 清空做彻底(按键拉满 + 广播兜底)并留出间隔, 消掉异步竞争
                            press_clear(device, CLEAR_DEL_MAX)
                            if input_mode == "adbk" and adbkeyboard_installed(device):
                                try:
                                    broadcast_clear(device)
                                except Exception:
                                    pass
                            time.sleep(0.4)
                            cm = "keyevent+"
                        if text:
                            m, _ = do_input(device, text, mode=input_mode, ime_off=ime_off,
                                            rid=rid, verify=False)
                        _r, after, is_pwd = read_field(device, rid)
                        if text:
                            v = input_ok(cur, after, text, is_pwd)
                        else:
                            v = (None if after is None
                                 else (after == "" or after != cur))
                        if v is not False:
                            break
                        time.sleep(0.3)
                    hid = hide_keyboard(device) if (hide_input_keyboard and text) else False
                    if v is False:
                        ok = False
                        detail = (f"聚焦{target} 后的清空/输入没生效(框内现在是 {after!r}); "
                                  f"若控件不是标准输入框或输入法拦截了按键, 可改用 ADBKeyboard")
                    elif text:
                        detail = (f"聚焦{target} → 清空[{CLEAR_METHOD_LABEL.get(cm, cm)}] → "
                                  f"input {text} ·{INPUT_METHOD_LABEL.get(m, m)}{verify_tag(v)}"
                                  + (" ·重试1次" if attempt == 2 else "")
                                  + (" ·收起键盘" if hid else ""))
                    else:
                        detail = (f"聚焦{target} → 清空[{CLEAR_METHOD_LABEL.get(cm, cm)}]{verify_tag(v)}"
                                  + (" ·重试1次" if attempt == 2 else ""))
        elif action == "key":
            key = str(step.get("key", "BACK")).upper()
            code = KEY_MAP.get(key, key if key.isdigit() else "4")
            adb(device, "shell", "input", "keyevent", code, check=True)
            detail = f"key {key}({code})"
        elif action == "swipe":
            path = step.get("path")
            if isinstance(path, list) and len(path) >= 2:
                dur = max(100, int(step.get("duration", 400)))
                if replay_swipe(device, path, dur):
                    detail = f"swipe 轨迹({len(path)}点,{dur}ms)"
                else:
                    # 不支持 motionevent → 回退为首尾单段
                    (a1, b1), (c1, d1) = path[0], path[-1]
                    adb(device, "shell", "input", "swipe",
                        str(a1), str(b1), str(c1), str(d1), str(dur), check=True)
                    detail = f"swipe 近似({a1},{b1})→({c1},{d1})"
            else:
                adb(device, "shell", "input", "swipe",
                    str(int(step["x1"])), str(int(step["y1"])),
                    str(int(step["x2"])), str(int(step["y2"])),
                    str(int(step.get("duration", 300))), check=True)
                detail = "swipe"
        elif action == "wait":
            ms = max(0, int(step.get("ms", 500)))
            time.sleep(ms / 1000)
            detail = f"wait {ms}ms"
        elif action == "start_app":
            pkg = step.get("package", "")
            act = step.get("activity", "")
            if act:
                adb(device, "shell", "am", "start", "-n", f"{pkg}/{act}", check=True)
            else:
                # 用 am start 按包名找启动 Activity(不 monkey: 部分 ROM 上 monkey 会卡死)
                adb(device, "shell", "am", "start", "-a", "android.intent.action.MAIN",
                    "-c", "android.intent.category.LAUNCHER", "-p", pkg, check=True)
            time.sleep(1)
            detail = f"start_app {pkg}"
        elif action == "back":
            adb(device, "shell", "input", "keyevent", "4", check=True)
            detail = "back"
        elif action == "home":
            adb(device, "shell", "input", "keyevent", "3", check=True)
            detail = "home"
        else:
            ok = False
            detail = f"未知步骤类型: {action}"
    except AdbError as e:
        ok = False
        detail = f"adb 失败:{str(e)}"
    except subprocess.TimeoutExpired:
        ok = False
        detail = "adb 命令超时"
    except KeyError as e:
        ok = False
        detail = f"缺少参数: {e}"
    return {"action": action or "?", "ok": ok, "detail": detail,
            "ms": int((time.time() - t0) * 1000)}


# 运行中断标志（按停止按钮置位）
_run_flags = {}


def _script_needs_cn(script):
    """脚本是否包含中文(非 ASCII)输入步骤 —— 只有这种情况才需要 ADBKeyboard/YADB。"""
    for s in script.get("steps", []):
        a = s.get("action")
        if a == "input":
            t = s.get("text") or ""
        elif a == "tap_input":
            t = s.get("input") or ""
        else:
            continue
        if has_non_ascii(t):
            return True
    return False


def _script_needs_input(script):
    """脚本里有没有会写/删文本的步骤(input / tap_input / tap_clear / clear_text)。"""
    return any(s.get("action") in TEXT_STEP_ACTIONS for s in script.get("steps", []))


def run_script(device, script):
    """顺序执行脚本，返回逐步日志。设备找不到时立即停止，不跑任何步骤。
    文本输入期间会接管输入法: 装了 ADBKeyboard 就切到它(整段提交, 中英文都稳);
    没装则【临时关掉输入法】, 让按键原样落字(拼音输入法会把字母当拼音组词, 这就是
    "输英文数字结果成了中文/丢字"的根因)。结束(含出错/停止)一定恢复用户原输入法。"""
    if not device_online(device):
        return [{"index": 0, "action": "", "ok": False,
                 "detail": device_err_msg(device or ""), "ms": 0, "stopped": True, "note": ""}]
    recover_ime_if_needed(device)      # 上次崩溃遗留的"输入法被临时关掉"先恢复
    steps = script.get("steps", [])
    needs_input = _script_needs_input(script)
    needs_cn = _script_needs_cn(script)
    mode = ""
    use_adbk = False
    if needs_input:
        mode, _why = input_capability(device)
        use_adbk = (mode == "adbk")
        if needs_cn and not use_adbk and not yadb_available(device):
            return [{"index": 0, "action": "", "ok": False,
                     "detail": "脚本含中文输入, 设备未装 ADBKeyboard 且 YADB 不可用。解决办法二选一: "
                               "1) 用编辑器顶栏 IME 按钮一键安装 ADBKeyboard ; "
                               "2) 下载 YADB(免装应用) 放到项目 yadb/yadb: https://github.com/ysbing/YADB/releases",
                     "ms": 0, "stopped": True, "note": ""}]
    pre_ime = get_ime(device) if use_adbk else ""
    if use_adbk and pre_ime and pre_ime != ADBKEYBOARD_IME:
        remember_ime(pre_ime)  # 本次运行时是你的输入法 → 自动记为“原输入法”
    run_id = str(time.time())
    _run_flags[run_id] = False
    delay = max(0, int(script.get("step_delay_ms", 300))) / 1000
    hide_kbd = bool(script.get("hide_keyboard_after_input", True))
    default_retry = int(script.get("control_retry") or 3)
    ime_off = False
    logs = []
    try:
        if use_adbk:
            set_ime(device, ADBKEYBOARD_IME)   # 启动脚本时启用 ADBKeyboard
        elif mode == "off":
            ime_off = ime_off_begin(device)    # 输入期间临时关掉输入法(结束恢复)
        for i, step in enumerate(steps):
            if _run_flags.get(run_id):
                logs.append({"index": i, "action": step.get("action"), "ok": False,
                             "detail": "用户停止", "ms": 0, "stopped": True})
                break
            r = run_step(device, step, hide_input_keyboard=hide_kbd,
                         default_retry=default_retry,
                         input_mode=mode, ime_off=ime_off)
            r["index"] = i
            r["note"] = step.get("note", "")
            logs.append(r)
            if not r["ok"]:
                break
            if i < len(steps) - 1:
                local = step.get("delay")   # 局部间隔(ms)，局部优先生效
                if local is not None:
                    if local > 0:
                        time.sleep(local / 1000)
                elif step.get("action") != "wait":
                    time.sleep(delay)       # 全局间隔
    finally:
        _run_flags.pop(run_id, None)
        if use_adbk:
            restore_ime(device, pre_ime)  # 结束/出错/停止后切回原输入法
        if ime_off:
            ime_off_end(device)           # 把临时关掉的输入法恢复回来
    return logs


# ---------------------------------------------------------------------------
# .sh 导出
# ---------------------------------------------------------------------------

SH_HELPERS = r'''# ---------- 工具函数 ----------
adb_cmd() { if [ -n "$SERIAL" ]; then adb -s "$SERIAL" "$@"; else adb "$@"; fi; }
sleep_step() { sleep "$STEP_DELAY"; }

hide_keyboard() {  # 软键盘可见时发 BACK 收起, 避免遮挡下一步; 不可见则不动
  if adb_cmd shell dumpsys input_method 2>/dev/null | grep -qE 'mInputShown=(true|1)|mImeWindowVis=0x[1-9a-fA-F]'; then
    adb_cmd shell input keyevent 4
  fi
  sleep_step
}

tap()        { adb_cmd shell input tap "$1" "$2"; sleep_step; }
key()        { adb_cmd shell input keyevent "$1"; sleep_step; }
back()       { adb_cmd shell input keyevent 4; sleep_step; }
home()       { adb_cmd shell input keyevent 3; sleep_step; }
swipe()      { adb_cmd shell input swipe "$1" "$2" "$3" "$4" "${5:-300}"; sleep_step; }
swipe_traj() {  # $1="x,y x,y ..." 轨迹点; $2=总时长ms; 多段 input swipe 在单会话拼接(快, 全Android可用)
  local pts dur n seg prev p parts
  dur="${2:-400}"; pts="$1"
  n=0; for _p in ${pts}; do n=$((n+1)); done
  [ "$n" -lt 2 ] && return
  seg=$(( (dur > 49 ? dur : 300) / (n - 1) )); [ "$seg" -lt 20 ] && seg=20
  parts=""; prev=""
  for p in ${pts}; do
    if [ -n "$prev" ]; then
      parts="$parts; input swipe ${prev%%,*} ${prev#*,} ${p%%,*} ${p#*,} $seg"
    fi
    prev="$p"
  done
  parts="${parts#; }"
  adb_cmd shell "$parts"
  sleep_step
}
wait_ms()    { sleep "$(awk "BEGIN{print $1/1000}")"; }

yadb_ensure() {  # 设备上能跑 yadb 即可(不装 App): 已有直接用, 否则从脚本同目录推送
  adb_cmd shell ls /data/local/tmp/yadb >/dev/null 2>&1 && return 0
  local f found=""
  for f in "$SCRIPT_DIR/yadb" "$SCRIPT_DIR/yadb.jar"; do [ -f "$f" ] && { found="$f"; break; }; done
  [ -n "$found" ] || return 1
  adb_cmd push "$found" /data/local/tmp/yadb >/dev/null 2>&1
}

yadb_cmd() { adb_cmd shell "app_process -Djava.class.path=/data/local/tmp/yadb /data/local/tmp com.ysbing.yadb.Main $1"; }

yadb_input() {  # 写剪贴板 + 注入 Ctrl+V 粘贴, 并回读界面确认真的进控件了
  yadb_ensure || return 1
  local q out; q=$(printf '%s' "$1" | sed "s/'/'\\\\''/g")
  out=$(yadb_cmd "-keyboard '$q'" 2>/dev/null) || return 1
  printf '%s' "$out" | grep -q 'Copy text:true' || return 1   # 剪贴板没写成就没粘贴
  # 部分 ROM(实测华为 HarmonyOS)剪贴板能写, 但拒绝注入 Ctrl+V: 必须回读确认
  adb_cmd shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
  adb_cmd shell cat /sdcard/ui.xml 2>/dev/null | tr -d '\r' | grep -qF -- "$1"
}

ui_field() {  # 读输入框文本做校验: $1=resource-id(空=第一个 EditText); 输出 "文本<TAB>是否密码框(1/0)"
  local xml line t p
  adb_cmd shell uiautomator dump /sdcard/ui_check.xml >/dev/null 2>&1 || return 1
  xml=$(adb_cmd shell cat /sdcard/ui_check.xml 2>/dev/null | tr -d '\r' | tr '>' '\n') || return 1
  if [ -n "${1:-}" ]; then
    line=$(printf '%s\n' "$xml" | grep -F "resource-id=\"$1\"" | head -n 1)
  else
    line=$(printf '%s\n' "$xml" | grep -F 'class="android.widget.EditText"' | head -n 1)
  fi
  [ -n "$line" ] || return 1
  t=$(printf '%s' "$line" | sed -n 's/.* text="\([^"]*\)".*/\1/p')
  p=0; case "$line" in *'password="true"'*) p=1 ;; esac
  printf '%s\t%s' "$t" "$p"
}

field_ok() {  # 回读校验"输入到底进控件没有": $1=期望文本 $2=resource-id(可空)。0=进了, 1=没进
  local f t p
  f=$(ui_field "${2:-}" || true)
  [ -n "$f" ] || return 0          # 读不到控件(不是输入框/界面已变) → 不阻塞
  t="${f%%$'\t'*}"; p="${f##*$'\t'}"
  if [ "$p" = 1 ]; then
    [ -n "$t" ]                    # 密码框只能看到掩码 → 非空即认为写进去了
  else
    printf '%s' "$t" | grep -qF -- "$1"
  fi
}

press_clear() {  # $1=DEL 个数(默认200): MOVE_END + 批量 DEL, 所有键在【一次】adb 调用里发完
  local n="${1:-200}" keys="123" i=0
  while [ "$i" -lt "$n" ]; do keys="$keys 67"; i=$((i+1)); done
  adb_cmd shell input keyevent $keys
}

input_raw() {  # 只输入(不含等待/收键盘): ASCII 走同步按键; 中文走 ADBKeyboard 广播 > YADB 粘贴
  local s
  if printf '%s' "$1" | LC_ALL=C grep -q '[^ -~]'; then
    if [ "${USE_ADBK_INPUT:-0}" = 1 ]; then
      adb_cmd shell am broadcast -a ADB_INPUT_TEXT --es msg "$1" >/dev/null
    elif yadb_input "$1"; then
      :
    else
      echo '[input] 中文输入失败: YADB 粘贴没进控件(部分 ROM 拒绝注入 Ctrl+V)' >&2
      echo '  解决办法: 安装 ADBKeyboard 输入法(17KB), 用输入法通道直接提交文本' >&2
      echo '    下载: https://github.com/senzhk/ADBKeyBoard (或用编辑器顶栏 IME 按钮一键安装)' >&2
      echo '    安装: adb install -r adbkeyboard.apk' >&2
      return 1
    fi
    return 0
  fi
  # 纯 ASCII: 输入法已被接管(切到 ADBKeyboard 或临时关掉), 按键原样落字
  s=$(printf '%s' "$1" | sed 's/ /%s/g; s/["'"'"'\\<>();&|*$`]/\\&/g')
  adb_cmd shell input text "$s"
}

fill_field() {  # $1=resource-id(可空) $2=文本: 清空 + 输入 + 回读校验, 没生效自动重试一次
  local rid="${1:-}" txt="$2" try=0
  while :; do
    press_clear 200
    if [ "$try" -gt 0 ]; then
      # 重试: 补发按键 + 广播兜底并留出间隔, 消掉"广播异步抢顺序"这类竞争
      if [ "${USE_ADBK_INPUT:-0}" = 1 ]; then
        adb_cmd shell am broadcast -a ADB_CLEAR_TEXT >/dev/null 2>&1
        sleep 0.4
      fi
      sleep 0.3
    fi
    input_raw "$txt"
    if field_ok "$txt" "$rid"; then
      [ "$try" -gt 0 ] && echo "[输入] 首次没生效, 重试后成功: ${rid:-焦点输入框}"
      break
    fi
    try=$((try+1))
    if [ "$try" -gt "${FILL_RETRY:-1}" ]; then
      echo "[输入] 警告: 输入后回读校验没通过(${rid:-焦点输入框}) —— 该控件可能不是标准输入框, 或输入法拦下了按键" >&2
      break
    fi
  done
  sleep_step
  if [ "${HIDE_KEYBOARD:-1}" = 1 ]; then hide_keyboard; fi
}

input_text() {  # $1=文本 $2=resource-id(可空): 输入 + 回读校验(异步方式没进去就重发一次)
  input_raw "$1" || return 1
  if [ "${USE_ADBK_INPUT:-0}" = 1 ] && ! field_ok "$1" "${2:-}"; then
    sleep 0.35
    input_raw "$1"
  fi
  sleep_step
  if [ "${HIDE_KEYBOARD:-1}" = 1 ]; then hide_keyboard; fi
}

clear_text() {  # $1=resource-id(可空): 同步按键清空 + 回读校验(不干净就补发, 再不干净用广播兜底)
  # 为什么不再首选 ADBKeyboard 广播: am broadcast 是"喊完就走", 返回时输入法还没处理,
  # 紧接着的输入会和它抢顺序 —— 表现就是"清空没生效(内容一直往后累加)"或"刚输进去的字又被清掉"。
  local rid="${1:-}" before after
  before=$(ui_field "$rid" || true); before="${before%%$'\t'*}"
  press_clear 200
  if [ -n "$before" ]; then
    after=$(ui_field "$rid" || true); after="${after%%$'\t'*}"
    if [ "$after" = "$before" ]; then
      press_clear 200
      if [ "${USE_ADBK_INPUT:-0}" = 1 ]; then
        adb_cmd shell am broadcast -a ADB_CLEAR_TEXT >/dev/null 2>&1
        sleep 0.4
      fi
      after=$(ui_field "$rid" || true); after="${after%%$'\t'*}"
      [ "$after" = "$before" ] && echo "[清空] 警告: 清空似乎没生效(框内内容没变化)" >&2
    fi
  fi
  sleep_step
}

tap_id() {  # $1=resource-id; $2=文字过滤(可空); $3=第几个匹配(0基,默认0); $4=重试次数; $5=match(exact|contains)
  local xml b i x1 y1 x2 y2 cx cy retries occurred m pat
  retries="${4:-$TAP_RETRY}"; retries=$(( retries > 0 ? retries : 1 )); i=0
  if [ -n "$1" ]; then
    pat="resource-id=\"$1\""
  elif [ "${5:-exact}" = "contains" ]; then
    pat="text=\"[^\"]*$2[^\"]*\""
  else
    pat="text=\"$2\""
  fi
  while :; do
    adb_cmd shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
    xml=$(adb_cmd shell cat /sdcard/ui.xml | tr -d '\r')   # 去掉 adb shell 的 \r，防解析错位
    b=""; occurred=0
    for m in $(printf '%s' "$xml" | grep -o "${pat}[^>]*bounds=\"\[[0-9]*,[0-9]*\]\[[0-9]*,[0-9]*\]\"" | grep -o '\[[0-9]*,[0-9]*\]\[[0-9]*,[0-9]*\]'); do
      [ "$occurred" = "${3:-0}" ] && { b="$m"; break; }
      occurred=$((occurred+1))
    done
    [ -n "$b" ] && break
    i=$(( i + 1 ))
    if [ "$i" -ge "$retries" ]; then
      echo "[tap_id] 未找到控件(试了${retries}次): ${1:-$2} #${3:-0}" >&2
      return 1
    fi
    sleep 1   # 等界面刷新后重试
  done
  b="${b//\[/ }"; b="${b//\]/ }"; b="${b//,/ }"           # [0,0][1920,1122] -> 0 0 1920 1122
  read -r x1 y1 x2 y2 <<< "$b"
  cx=$(( (10#$x1 + 10#$x2) / 2 )); cy=$(( (10#$y1 + 10#$y2) / 2 ))   # 10# 强制十进制防八进制误判
  adb_cmd shell input tap "$cx" "$cy"; sleep_step
}

start_app() {  # $1=包名, $2=activity(可选)
  if [ -n "${2:-}" ]; then
    adb_cmd shell am start -n "$1/$2"
  else
    adb_cmd shell am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -p "$1" 2>/dev/null
  fi
  sleep 1
}
# ---------- 工具函数结束 ----------'''


def sh_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


# ---------- 设备选择: 第1个参数 > SERIAL 环境变量 > 自动检测 ----------
# 导出的脚本不绑定设备: 1台自动选, 多台交互选择, 0台给出排查提示
SH_DEVICE_SELECT = r'''# ---------- 设备选择: 第1个参数 > SERIAL 环境变量 > 自动检测 ----------
[ -n "${1:-}" ] && SERIAL="$1"
if [ -z "$SERIAL" ]; then
  DEVICES=$(adb devices | awk 'NR>1 && $2=="device" {print $1}')
  N=$(printf '%s\n' "$DEVICES" | grep -c . || true)
  if [ "$N" -eq 0 ]; then
    echo '错误: 未检测到在线设备, 请检查:' >&2
    echo '  1. 手机已通过 USB 连接, 且已打开"USB 调试"(开发者选项)' >&2
    echo '  2. 手机上"允许 USB 调试"授权弹窗要点允许' >&2
    echo '  3. 仍失败可执行: adb kill-server && adb start-server 后重试' >&2
    exit 1
  elif [ "$N" -eq 1 ]; then
    SERIAL=$(printf '%s\n' "$DEVICES" | head -n 1)
    echo "已自动选择设备: $SERIAL"
  else
    echo "检测到 $N 台在线设备:"
    i=1
    for d in $DEVICES; do
      MODEL=$(adb -s "$d" shell getprop ro.product.model 2>/dev/null | tr -d '\r')
      echo "  $i) $d ${MODEL:+[$MODEL]}"
      i=$((i+1))
    done
    if [ ! -t 0 ]; then
      echo "错误: 当前是非交互终端, 无法交互选择设备" >&2
      echo "  请指定设备后运行: $0 <序列号>   或   SERIAL=<序列号> $0" >&2
      exit 1
    fi
    printf '请选择设备序号 [1-%s]: ' "$N"
    read -r CHOICE
    case "$CHOICE" in
      ''|*[!0-9]*) echo "错误: 无效的序号: $CHOICE" >&2; exit 1 ;;
    esac
    if [ "$CHOICE" -lt 1 ] || [ "$CHOICE" -gt "$N" ]; then
      echo "错误: 序号超出范围: $CHOICE" >&2
      exit 1
    fi
    SERIAL=$(printf '%s\n' "$DEVICES" | sed -n "${CHOICE}p")
    echo "已选择设备: $SERIAL"
  fi
fi'''

# ---------- 中文输入能力检查(仅脚本含中文时): ADBKeyboard 或 YADB 至少其一 ----------
SH_ADBK_CHECK = r'''# ---------- 文本输入能力接管 + 检查 ----------
# 为什么: 拼音类输入法(讯飞/搜狗/百度)会把 `input text` 注入的字母按键当成拼音组词,
# 于是 "20260001" 进框变成 "啊安…"、数字还会被候选缓冲吞掉 —— 这就是"输英文数字结果
# 成了中文/丢字"的根因(密码框不参与组词, 所以以前时好时坏)。
# 对策: 装了 ADBKeyboard 就切到它(整段提交, 中英文都稳); 没装则【临时关掉输入法】,
# 让按键原样落字。两者都会在脚本结束时(含出错/中断)自动恢复。
INPUT_MODE=""
IME_LIST=""; IME_DEFAULT=""; USE_ADBK_INPUT=0
if [ "$NEED_TEXT_INPUT" = 1 ]; then
  if adb_cmd shell pm list packages 2>/dev/null | grep -qi adbkeyboard; then
    INPUT_MODE="adbkeyboard"; USE_ADBK_INPUT=1
    IME_DEFAULT=$(adb_cmd shell settings get secure default_input_method 2>/dev/null | tr -d '\r')
    ime_restore() { case "$IME_DEFAULT" in ""|"com.android.adbkeyboard/.AdbIME") return 0;; esac; adb_cmd shell ime set "$IME_DEFAULT" >/dev/null 2>&1; }
    trap ime_restore EXIT
    trap 'exit 130' INT TERM
    adb_cmd shell ime enable com.android.adbkeyboard/.AdbIME >/dev/null 2>&1
    adb_cmd shell ime set com.android.adbkeyboard/.AdbIME >/dev/null 2>&1
    echo "[输入] 已切到 ADBKeyboard 输入(脚本结束自动切回原输入法)"
  else
    IME_LIST=$(adb_cmd shell ime list -s 2>/dev/null | tr -d '\r' | grep / || true)
    IME_DEFAULT=$(adb_cmd shell settings get secure default_input_method 2>/dev/null | tr -d '\r')
    if [ -n "$IME_LIST" ]; then
      ime_restore() {
        for _i in $IME_LIST; do adb_cmd shell ime enable "$_i" >/dev/null 2>&1; done
        case "$IME_DEFAULT" in ""|null) : ;; *) adb_cmd shell ime set "$IME_DEFAULT" >/dev/null 2>&1 ;; esac
      }
      trap ime_restore EXIT          # 正常结束/出错都恢复输入法
      trap 'exit 130' INT TERM       # Ctrl+C 也走 EXIT 恢复(否则设备会一直没输入法)
      for _i in $IME_LIST; do adb_cmd shell ime disable "$_i" >/dev/null 2>&1; done
      if [ -z "$(adb_cmd shell ime list -s 2>/dev/null | tr -d '\r' | grep / || true)" ]; then
        INPUT_MODE="keyevent"   # 输入法已临时关掉 -> 按键原样落字
        echo "[输入] 未装 ADBKeyboard, 已在输入期间临时关闭输入法(脚本结束自动恢复)"
      else
        ime_restore; IME_LIST=""
        echo "[输入] 警告: 该设备不允许关闭输入法, 英文数字可能被拼音输入法转成汉字" >&2
      fi
    else
      echo "[输入] 警告: 设备上没有已启用的输入法, 输入效果可能异常" >&2
    fi
  fi
  if [ "$NEED_CN_INPUT" = 1 ] && [ "$USE_ADBK_INPUT" = 0 ] && ! yadb_ensure; then
    echo '错误: 本脚本含中文输入, 但设备未装 ADBKeyboard 且 YADB 不可用' >&2
    echo '  解决办法1: 安装 ADBKeyboard 输入法(17KB): adb install -r adbkeyboard.apk' >&2
    echo '    下载: https://github.com/senzhk/ADBKeyBoard' >&2
    echo '  解决办法2(免装应用): 下载 yadb 文件放到本脚本同目录' >&2
    echo '    下载: https://github.com/ysbing/YADB/releases' >&2
    exit 1
  fi
fi'''


def export_shell(script):
    """把脚本 JSON 编译为自包含 bash 脚本(不绑定设备, 自动检测/选择设备)。"""
    name = script.get("name", "未命名")
    delay = max(0, int(script.get("step_delay_ms", 300))) / 1000
    escript_retry = max(0, int(script.get("control_retry") or 3))
    # 扫描步骤: 有没有写/删文本的步骤(要接管输入法) / 有没有中文输入
    needs_cn = False
    needs_input = False
    for step in script.get("steps", []):
        a = step.get("action")
        if a in TEXT_STEP_ACTIONS:
            needs_input = True
        if a == "input":
            t = step.get("text") or ""
        elif a == "tap_input":
            t = step.get("input") or ""
        else:
            continue
        if any(ord(c) > 127 for c in t):
            needs_cn = True
    lines = [
        "#!/usr/bin/env bash",
        f"# 自动化脚本: {name}",
        "# 由 Android 自动化脚本编辑器导出 (Git Bash / Linux / macOS 通用)",
        "# 用法: ./本脚本.sh [设备序列号]   或   SERIAL=序列号 ./本脚本.sh",
        "#       不指定设备则自动检测: 1台直接用, 多台交互选择, 没有则报错提示",
        f"# 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "# ---------- 依赖检查: 缺什么直接提示怎么装 ----------",
        "command -v adb >/dev/null 2>&1 || {",
        "  echo '错误: 缺少 adb 命令' >&2",
        "  echo '  需要安装 Android platform-tools, 并把其目录加入 PATH 后重试' >&2",
        "  echo '  下载: https://developer.android.com/tools/releases/platform-tools' >&2",
        "  exit 1",
        "}",
        "for _c in dirname awk grep sed tr; do",
        "  command -v \"$_c\" >/dev/null 2>&1 || { echo \"错误: 缺少 $_c 命令 (Git Bash / macOS 自带; Linux 可用 sudo apt install coreutils gawk 安装)\" >&2; exit 1; }",
        "done",
        "# 关闭 Git Bash/MSYS 自动路径转换: 否则 /sdcard 等设备路径会被转成 Windows 路径",
        "export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'",
        "SCRIPT_DIR=$(cd \"$(dirname \"$0\")\" && pwd)   # 脚本所在目录(找同目录 yadb 用)",
        "",
        "SERIAL=''   # 设备序列号: 留空自动检测; 也可用环境变量 SERIAL 或第1个参数指定",
        f"STEP_DELAY={delay}",
        "HIDE_KEYBOARD=1  # 输入后自动收起软键盘(0 关闭, 键盘不挡操作)",
        f"TAP_RETRY={int(escript_retry)}  # 控件没找到时的重试次数(每次等1秒)",
        "FILL_RETRY=1  # 清空+输入后回读校验没通过时, 自动重试的次数",
        f"NEED_CN_INPUT={1 if needs_cn else 0}  # 1=脚本含中文输入(需 ADBKeyboard 或免安装的 YADB)",
        f"NEED_TEXT_INPUT={1 if needs_input else 0}  # 1=脚本有文本输入 → 输入期间会接管输入法",
        "",
        "set -e  # 任一步 adb 失败(含设备掉线)即停止",
        "",
        SH_HELPERS,
        "",
        SH_DEVICE_SELECT,
        "",
        SH_ADBK_CHECK,
        "",
        "# ========== 步骤序列 ==========",
    ]
    def focus_call(step):
        """返回聚焦目标的 shell 调用行（坐标 tap 或控件 tap_id）。
        控件型：$1=rid, $2=文字过滤, $3=第N个匹配, $4=重试, $5=contains。"""
        if step.get("x") is not None and step.get("y") is not None:
            return f"tap {int(step['x'])} {int(step['y'])}"
        rid = step.get("resource_id") or ""
        text = step.get("text") or ""
        idx = int(step.get("index") or 0)
        extra = f" {int(step['retry'])}" if step.get("retry") is not None else ""
        if (step.get("text_match") or "exact") == "contains":
            extra += " contains"
        return f"tap_id {sh_quote(rid)} {sh_quote(text)} {idx}{extra}"

    for step in script.get("steps", []):
        a = step.get("action")
        note = step.get("note", "")
        comment = f"  # {note}" if note else ""
        locald = step.get("delay")   # 每步可单独设置间隔(ms)，局部优先
        if locald is not None:
            lines.append(f"STEP_DELAY={max(0, locald)/1000:.3f}   # 本步骤局部间隔")
        if a in ("tap", "tap_id"):
            lines.append(focus_call(step) + comment)
        elif a == "tap_clear":
            lines.append(focus_call(step))
            lines.append(f"clear_text {sh_quote(step.get('resource_id') or '')}{comment}")
        elif a == "tap_input":
            lines.append(focus_call(step))
            lines.append(f"fill_field {sh_quote(step.get('resource_id') or '')} "
                         f"{sh_quote(step.get('input', ''))}{comment}")
        elif a == "input":
            lines.append(f"input_text {sh_quote(step.get('text', ''))} "
                         f"{sh_quote(step.get('resource_id') or '')}{comment}")
        elif a == "clear_text":
            lines.append(f"clear_text {sh_quote(step.get('resource_id') or '')}{comment}")
        elif a == "key":
            key = str(step.get("key", "BACK")).upper()
            lines.append(f"key {KEY_MAP.get(key, key)}{comment}")
        elif a == "swipe":
            path = step.get("path")
            dur = int(step.get("duration", 300))
            if isinstance(path, list) and len(path) >= 2:
                n_seg = min(14, max(3, dur // 90))
                traj = " ".join(f"{int(x)},{int(y)}" for x, y in downsample(path, n_seg + 1))
                lines.append(f"swipe_traj '{traj}' {dur}{comment}")
            else:
                lines.append(f"swipe {int(step['x1'])} {int(step['y1'])} "
                             f"{int(step['x2'])} {int(step['y2'])} {dur}{comment}")
        elif a == "wait":
            lines.append(f"wait_ms {int(step.get('ms', 500))}{comment}")
        elif a == "start_app":
            act = step.get("activity") or ""
            if act:
                lines.append(f"start_app {sh_quote(step.get('package', ''))} {sh_quote(act)}{comment}")
            else:
                lines.append(f"start_app {sh_quote(step.get('package', ''))}{comment}")
        elif a == "back":
            lines.append(f"back{comment}")
        elif a == "home":
            lines.append(f"home{comment}")
        if locald is not None:
            lines.append(f"STEP_DELAY={delay:.3f}   # 还原全局间隔")
    lines.append("")
    lines.append('echo "脚本执行完成"')
    return "\n".join(lines)


def safe_filename(name):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "script"


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

def nav_bar_height(device, display_h):
    """读取系统导航栏高度：截图常包含这段非内容区(虚拟键)，裁剪后可让截图=App内容区。"""
    try:
        out = adb(device, "shell", "dumpsys", "window")
        m = re.search(r"ITYPE_NAVIGATION_BAR frame=\[0,(\d+)\]\[(\d+),(\d+)\]", out)
        if m and display_h:
            return max(0, display_h - int(m.group(1)))
    except Exception:
        pass
    return 0


def crop_nav_bottom(png, nav_h):
    """裁掉截图底部导航栏高度的非内容区，使截图高度=App可交互内容区高度。"""
    if not nav_h or nav_h <= 0:
        return png
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(png))
        w, h = im.size
        if nav_h >= h:
            return png
        buf = io.BytesIO()
        im.crop((0, 0, w, h - nav_h)).save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return png


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/static/<path:path>")
def static_files(path):
    return send_from_directory(STATIC_DIR, path)


@app.get("/api/devices")
def api_devices():
    return jsonify(get_devices())


@app.get("/api/screenshot")
def api_screenshot():
    dev_arg = request.args.get("device") or ""
    device = resolve_device(dev_arg)
    if not device:
        # 明确报错(前端会提示)，而不是返回空图片导致截图区一片空白
        return jsonify({"error": "无可用的在线设备"
                        + (f"（{dev_arg} 已离线）" if dev_arg else "，请先连接设备")}), 409
    try:
        png = take_screenshot(device)
    except Exception as e:
        return jsonify({"error": f"截图失败: {e}"}), 502
    # 裁掉底部导航栏(虚拟键)高度，让截图=App内容区，坐标/蒙层才和真实可点击区对齐
    try:
        import struct as _st
        _, h = _st.unpack(">II", png[:24][16:24])
        nav_h = nav_bar_height(device, h)
        if nav_h:
            png = crop_nav_bottom(png, nav_h)
    except Exception:
        pass
    return Response(png, mimetype="image/png")


@app.get("/api/uidump")
def api_uidump():
    device = resolve_device(request.args.get("device") or "")
    if not device:
        return jsonify({"error": "无可用的在线设备，无法获取控件树"}), 409
    return jsonify(uidump(device))


@app.get("/api/display_info")
def api_display_info():
    """返回当前显示几何：宽高(横竖屏双击实际)、导航栏/状态栏高度与可见性。
    用于坐标模式下修正虚拟按键(导航栏)隐藏造成的内容区高度变化/底部坐标偏移。"""
    device = resolve_device(request.args.get("device") or "")
    w = h = nav_h = status_h = 0
    nav_vis = None
    try:
        png = take_screenshot(device)
        import struct as _st
        w, h = _st.unpack(">II", png[:24][16:24])
    except Exception:
        pass
    try:
        out = adb(device, "shell", "dumpsys", "window")
        nf = re.search(r"ITYPE_NAVIGATION_BAR frame=\[0,(\d+)\]\[(\d+),(\d+)\]", out)
        if nf and h:
            nav_h = h - int(nf.group(1))
        nv = re.search(r"ITYPE_NAVIGATION_BAR frame=\[0,\d+\]\[(\d+),(\d+)\] visible=(\S+)", out)
        if nv:
            nav_vis = nv.group(3) == "true"
        sf = re.search(r"ITYPE_STATUS_BAR frame=\[0,0\]\[(\d+),(\d+)\]", out)
        if sf:
            status_h = int(sf.group(2))
    except Exception:
        pass
    return jsonify({"width": w, "height": h, "nav_height": nav_h,
                    "status_height": status_h, "nav_visible": nav_vis})


@app.post("/api/run_step")
def api_run_step():
    body = request.get_json(force=True)
    req_dev = body.get("device") or ""
    device = resolve_device(req_dev)
    if not device_online(device):
        return jsonify({"action": body.get("step", {}).get("action", "?"), "ok": False,
                        "detail": device_err_msg(req_dev), "ms": 0})
    retry = body.get("retry")
    step = body.get("step", {})
    recover_ime_if_needed(device)   # 上次崩溃遗留的"输入法被临时关掉"先恢复
    # 写/删文本的步骤: 装了 ADBKeyboard 就切到它; 否则整个步骤期间临时关掉输入法
    # (拼音输入法会把注入的字母当拼音组词, 导致"输英文数字变成中文/丢字"; 清空也会被吞)
    mode = ""
    if step.get("action") in TEXT_STEP_ACTIONS:
        mode, _why = input_capability(device)
    use_adbk = (mode == "adbk")
    pre_ime = get_ime(device) if use_adbk else ""
    ime_off = False
    try:
        if use_adbk:
            remember_ime(pre_ime)
            set_ime(device, ADBKEYBOARD_IME)
        elif mode == "off":
            ime_off = ime_off_begin(device)
        result = run_step(device, step, default_retry=retry or 3,
                          input_mode=mode, ime_off=ime_off)
    finally:
        if use_adbk:
            restore_ime(device, pre_ime)
        if ime_off:
            ime_off_end(device)
    result["screenshot_after"] = True  # 前端据此刷新截图
    return jsonify(result)


@app.post("/api/run_script")
def api_run_script():
    body = request.get_json(force=True)
    device = resolve_device(body.get("device") or "")
    script = body.get("script", {})
    if not device_online(device):
        return jsonify({"logs": [{"index": 0, "action": "", "ok": False, "ms": 0, "stopped": True,
                                  "note": "", "detail": device_err_msg(body.get("device") or "")}]})
    logs = run_script(device, script)
    return jsonify({"logs": logs})


@app.post("/api/stop")
def api_stop():
    # 尽力而为: 丢弃标志位仅在同进程内生效, 前端还会杀请求
    return jsonify({"ok": True})


@app.post("/api/export")
def api_export():
    script = request.get_json(force=True)
    sh_text = export_shell(script)
    fname = safe_filename(script.get("name", "script")) + ".sh"
    path = os.path.join(EXPORTS_DIR, fname)
    # UTF-8 无 BOM, Unix 换行
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(sh_text)
    return jsonify({"ok": True, "filename": fname, "path": path, "content": sh_text})


@app.get("/api/scripts")
def api_list_scripts():
    files = sorted(f for f in os.listdir(SCRIPTS_DIR) if f.endswith(".json"))
    names = []
    for f in files:
        try:
            with open(os.path.join(SCRIPTS_DIR, f), encoding="utf-8") as fp:
                data = json.load(fp)
            names.append({"file": f, "name": data.get("name", f[:-5]),
                          "steps": len(data.get("steps", []))})
        except (json.JSONDecodeError, OSError):
            continue
    return jsonify(names)


@app.get("/api/scripts/<path:fname>")
def api_get_script(fname):
    path = os.path.join(SCRIPTS_DIR, os.path.basename(fname))
    if not os.path.isfile(path):
        return jsonify({"error": "脚本不存在"}), 404
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.post("/api/scripts")
def api_save_script():
    script = request.get_json(force=True)
    fname = safe_filename(script.get("name", "script")) + ".json"
    with open(os.path.join(SCRIPTS_DIR, fname), "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "file": fname})


@app.get("/api/adbkeyboard/status")
def api_adbkeyboard_status():
    device = resolve_device(request.args.get("device") or "")
    if not device:
        return jsonify({"installed": False, "enabled": False, "apk_ready": False,
                        "url": ADBKEYBOARD_URL})
    imes = adb(device, "shell", "ime", "list", "-s")
    installed = ADBKEYBOARD_IME in imes
    current = adb(device, "shell", "settings", "get", "secure", "default_input_method").strip()
    enabled = installed and ADBKEYBOARD_IME in current
    apk_ready = os.path.isfile(os.path.join(APK_DIR, "ADBKeyboard.apk"))
    return jsonify({"installed": installed, "enabled": installed,
                    "apk_ready": apk_ready, "url": ADBKEYBOARD_URL})


@app.post("/api/adbkeyboard/setup")
def api_adbkeyboard_setup():
    device = resolve_device((request.get_json(silent=True) or {}).get("device") or "")
    apk = os.path.join(APK_DIR, "ADBKeyboard.apk")
    if not os.path.isfile(apk):
        return jsonify({"ok": False, "error": "APK 不存在",
                        "hint": f"请下载 {ADBKEYBOARD_URL} 放入 {APK_DIR} 后重试"}), 400
    out = adb(None, "install", "-r", apk, timeout=120) if not device else \
        adb(device, "install", "-r", apk, timeout=120)
    if "Success" not in out:
        return jsonify({"ok": False, "error": f"安装失败: {out.strip()}"}), 500
    remember_ime(get_ime(device))  # 记录切走之前的输入法，供脚本结束自动切回
    adb(device, "shell", "ime", "enable", ADBKEYBOARD_IME)
    adb(device, "shell", "ime", "set", ADBKEYBOARD_IME)
    return jsonify({"ok": True, "message": "ADBKeyboard 已安装并设为当前输入法"})


@app.post("/api/scrcpy/start")
def api_scrcpy_start():
    body = request.get_json(silent=True) or {}
    device = resolve_device(body.get("device") or "")
    path = body.get("path") or SCRCPY_DEFAULT
    if not os.path.isfile(path):
        # 尝试 PATH 中的 scrcpy
        import shutil
        path = shutil.which("scrcpy") or path
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": f"找不到 scrcpy: {path}"}), 400
    cmd = [path]
    if device:
        cmd += ["-s", device]
    subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0)
    return jsonify({"ok": True})


def _recover_imes_on_start():
    """启动时: 若上次运行崩溃/被强杀导致设备的输入法被临时关着, 立刻恢复回来
    (否则设备会连键盘都没有, 用户会以为输入法坏了)。"""
    try:
        st = _load_ime_off()
        if not st:
            return
        serial = st.get("device") or ""
        try:
            online = [d["serial"] for d in get_devices() if d["status"] == "device"]
        except Exception:
            online = []
        if serial and serial in online:
            ime_off_end(serial)
            print(f"[输入法] 已恢复 {serial} 上被临时关闭的输入法: {st.get('imes')}")
        else:
            print(f"[输入法] 上次运行遗留了输入法关闭记录(设备 {serial or '未知'} 不在线), "
                  f"该设备下次连上并运行时会自动恢复")
    except Exception as e:      # 任何异常都不能影响服务启动
        print(f"[输入法] 启动检查跳过: {e}")


if __name__ == "__main__":
    _recover_imes_on_start()
    print("=" * 50)
    print("  Android 自动化脚本编辑器")
    print("  打开浏览器访问: http://127.0.0.1:5000")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5000, debug=False)
