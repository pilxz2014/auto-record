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


def resolve_device(req_device=None):
    """确定目标设备：显式指定 > 请求参数 > 唯一在线设备。"""
    if req_device:
        return req_device
    devices = [d for d in get_devices() if d["status"] == "device"]
    if len(devices) == 1:
        return devices[0]["serial"]
    return None  # 多设备或无设备时由 adb 自己处理（单命令无 -s）


# ---------------------------------------------------------------------------
# 截图 / 控件树
# ---------------------------------------------------------------------------

def take_screenshot(device):
    return adb(device, "exec-out", "screencap", "-p", binary=True, timeout=30)


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
            "bounds": bounds,
        })
    return nodes


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


def resolve_tap_xy(device, step):
    """解析点击目标坐标：优先用 x/y，否则按控件定位。
    支持：resource_id + index(第N个匹配)、加 text 过滤(id+文字)、文字精确/包含匹配。
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
            b = n["bounds"]
            return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        k += 1
    return None


def resolve_tap_xy_retry(device, step, retries):
    """带重试的坐标解析：坐标型直接返回；控件型连续尝试若干次并在控制树刷新后重查。
    retries: 尝试次数（≥1），每次间隔 1 秒。找不到返回 None。"""
    if step.get("x") is not None and step.get("y") is not None:
        return int(step["x"]), int(step["y"])
    retries = max(1, int(retries or 0))
    for i in range(retries):
        xy = resolve_tap_xy(device, step)
        if xy:
            return xy
        if i + 1 < retries:
            time.sleep(1)  # 等界面刷新后重试
    return None


def do_clear(device):
    """清空当前焦点输入框。
    1) 已装 ADBKeyboard → 用其 ADB_CLEAR_TEXT 广播（整段清空，含中文可靠）
    2) 否则降级：MOVE_END 后连续 DEL（每次删一个字符，循环多次）"""
    if "adbkeyboard" in adb(device, "shell", "pm", "list", "packages", "adbkeyboard"):
        adb(device, "shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT", check=True)
    else:
        adb(device, "shell", "input", "keyevent", "123", check=True)  # MOVE_END
        for _ in range(200):  # 上限 200 次 DEL；设备掉线时 check=True 会立刻抛错中止
            adb(device, "shell", "input", "keyevent", "67", check=True)


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


def do_input(device, text):
    """输入文本：含非 ASCII(中文) 走 ADBKeyboard 广播，否则原生 input text。"""
    if has_non_ascii(text):
        adb(device, "shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT",
            "--es", "msg", text, check=True)
    else:
        adb(device, "shell", "input", "text", sh_escape_input(text), check=True)


def run_step(device, step, hide_input_keyboard=True, default_retry=3):
    """执行单条步骤，返回 dict(action, ok, detail, ms)。
    hide_input_keyboard: 输入类步骤结束后是否自动收起软键盘。
    default_retry: 控件没找到时的默认重试次数(下拉一次控件树等1秒)，单步可用 retry 覆盖。"""
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
                do_input(device, text)
                hid = hide_keyboard(device) if hide_input_keyboard else False
                detail = f"input {text}" + (" ·收起键盘" if hid else "")
        elif action == "clear_text":
            do_clear(device)
            detail = "clear_text"
        elif action in ("tap_clear", "tap_input"):
            # 组合步骤：先聚焦目标(坐标或控件)，再清空、可选输入
            xy = resolve_tap_xy_retry(device, step, retries)
            if not xy:
                ok = False
                detail = f"未找到控件(试了{retries}次): {step.get('resource_id') or step.get('text')}"
            else:
                adb(device, "shell", "input", "tap", str(xy[0]), str(xy[1]), check=True)
                do_clear(device)
                target = step.get("resource_id") or step.get("text") or f"{xy[0]},{xy[1]}"
                if action == "tap_input":
                    text = str(step.get("input", ""))  # 待输入文本
                    if not text:
                        ok, detail = False, "tap_input 文本为空"
                    else:
                        do_input(device, text)
                        hid = hide_keyboard(device) if hide_input_keyboard else False
                        detail = f"聚焦{target} → 清空 → input {text}" + (" ·收起键盘" if hid else "")
                else:
                    detail = f"聚焦{target} → 清空"
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


ADBK_NEEDING = {"input", "tap_input", "tap_clear", "clear_text"}


def _needs_adbk(step):
    return step.get("action") in ADBK_NEEDING


def run_script(device, script):
    """顺序执行脚本，返回逐步日志。设备找不到时立即停止，不跑任何步骤。
    含中文/清空输入时：运行期间切到 ADBKeyboard，结束(含出错/停止)自动切回原输入法。"""
    if not device_online(device):
        return [{"index": 0, "action": "", "ok": False,
                 "detail": f"设备未连接（{device or '未指定'}），已停止", "ms": 0, "stopped": True, "note": ""}]
    needs_adbk = any(_needs_adbk(s) for s in script.get("steps", []))
    pre_ime = get_ime(device)
    if needs_adbk and pre_ime and pre_ime != ADBKEYBOARD_IME:
        remember_ime(pre_ime)  # 本次运行时是你的输入法 → 自动记为“原输入法”
    run_id = str(time.time())
    _run_flags[run_id] = False
    delay = max(0, int(script.get("step_delay_ms", 300))) / 1000
    hide_kbd = bool(script.get("hide_keyboard_after_input", True))
    default_retry = int(script.get("control_retry") or 3)
    logs = []
    try:
        if needs_adbk:
            set_ime(device, ADBKEYBOARD_IME)  # 启动脚本时启用 ADBKeyboard
        for i, step in enumerate(script.get("steps", [])):
            if _run_flags.get(run_id):
                logs.append({"index": i, "action": step.get("action"), "ok": False,
                             "detail": "用户停止", "ms": 0, "stopped": True})
                break
            r = run_step(device, step, hide_input_keyboard=hide_kbd,
                         default_retry=default_retry)
            r["index"] = i
            r["note"] = step.get("note", "")
            logs.append(r)
            if not r["ok"]:
                break
            if i < len(script["steps"]) - 1:
                local = step.get("delay")   # 局部间隔(ms)，局部优先生效
                if local is not None:
                    if local > 0:
                        time.sleep(local / 1000)
                elif step.get("action") != "wait":
                    time.sleep(delay)       # 全局间隔
    finally:
        _run_flags.pop(run_id, None)
        if needs_adbk:
            restore_ime(device, pre_ime)  # 结束/出错/停止后切回原输入法
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

input_text() {  # 自动判断: 含非 ASCII 字符走 ADBKeyboard, 否则原生 input text
  local s
  case "$1" in
    *[![:ascii:]]*)
      adb_cmd shell am broadcast -a ADB_INPUT_TEXT --es msg "$1" >/dev/null ;;
    *)
      s=$(printf '%s' "$1" | sed 's/ /%s/g; s/["'"'"'\\<>();&|*$`]/\\&/g')
      adb_cmd shell input text "$s" ;;
  esac
  sleep_step
  [ "${HIDE_KEYBOARD:-1}" = 1 ] && hide_keyboard
}

clear_text() {  # 已装 ADBKeyboard → 广播整段清空; 否则 MOVE_END + 连续 DEL
  if adb_cmd shell pm list packages adbkeyboard 2>/dev/null | grep -qi adbkeyboard; then
    adb_cmd shell am broadcast -a ADB_CLEAR_TEXT >/dev/null
  else
    adb_cmd shell input keyevent 123
    local i; for i in $(seq 1 100); do adb_cmd shell input keyevent 67 || break; done
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


def export_shell(script):
    """把脚本 JSON 编译为自包含 bash 脚本。"""
    name = script.get("name", "未命名")
    serial = script.get("device", "")
    delay = max(0, int(script.get("step_delay_ms", 300))) / 1000
    escript_retry = max(0, int(script.get("control_retry") or 3))
    lines = [
        "#!/usr/bin/env bash",
        f"# 自动化脚本: {name}",
        "# 由 Android 自动化脚本编辑器导出 (Git Bash / Linux / macOS 通用)",
        f"# 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "command -v adb >/dev/null || { echo '错误: adb 不在 PATH 中' >&2; exit 1; }",
        "# 关闭 Git Bash/MSYS 自动路径转换: 否则 /sdcard 等设备路径会被转成 Windows 路径",
        "export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'",
        "",
        f"SERIAL={sh_quote(serial)}   # 设备序列号, 留空则用默认设备",
        f"STEP_DELAY={delay}",
        "HIDE_KEYBOARD=1  # 输入后自动收起软键盘(0 关闭, 键盘不挡操作)",
        f"TAP_RETRY={int(escript_retry)}  # 控件没找到时的重试次数(每次等1秒)",
        "",
        "set -e  # 任一步 adb 失败(含设备掉线)即停止",
        "",
        SH_HELPERS,
        "",
        "# 启动前检查设备是否连接(找不到就停止)",
        "adb_cmd get-state >/dev/null 2>&1 || { echo \"错误: 设备未连接 (${SERIAL:-默认设备})\" >&2; exit 1; }",
        "",
        "# 中文/清空输入需要 ADBKeyboard 输入法: 运行前启用, 结束(含出错/中断)自动切回原输入法",
        "ORIG_IME=$(adb_cmd shell settings get secure default_input_method 2>/dev/null | tr -d '\\r')",
        'restore_ime() { case "$ORIG_IME" in ""|"com.android.adbkeyboard/.AdbIME") return 0;; esac; adb_cmd shell ime set "$ORIG_IME" >/dev/null 2>&1; }',
        "trap restore_ime EXIT",
        "adb_cmd shell ime set com.android.adbkeyboard/.AdbIME >/dev/null 2>&1",
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
            lines.append(f"clear_text{comment}")
        elif a == "tap_input":
            lines.append(focus_call(step))
            lines.append("clear_text")
            lines.append(f"input_text {sh_quote(step.get('input', ''))}{comment}")
        elif a == "input":
            lines.append(f"input_text {sh_quote(step.get('text', ''))}{comment}")
        elif a == "clear_text":
            lines.append(f"clear_text{comment}")
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
    device = request.args.get("device") or resolve_device()
    png = take_screenshot(device)
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
    device = request.args.get("device") or resolve_device()
    return jsonify(uidump(device))


@app.get("/api/display_info")
def api_display_info():
    """返回当前显示几何：宽高(横竖屏双击实际)、导航栏/状态栏高度与可见性。
    用于坐标模式下修正虚拟按键(导航栏)隐藏造成的内容区高度变化/底部坐标偏移。"""
    device = request.args.get("device") or resolve_device()
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
    device = body.get("device") or resolve_device()
    if not device_online(device):
        return jsonify({"action": body.get("step", {}).get("action", "?"), "ok": False,
                        "detail": f"设备未连接（{device or '未指定'}），已停止", "ms": 0})
    retry = body.get("retry")
    step = body.get("step", {})
    pre_ime = get_ime(device)
    try:
        if _needs_adbk(step):
            set_ime(device, ADBKEYBOARD_IME)
        result = run_step(device, step, default_retry=retry or 3)
    finally:
        if _needs_adbk(step):
            restore_ime(device, pre_ime)
    result["screenshot_after"] = True  # 前端据此刷新截图
    return jsonify(result)


@app.post("/api/run_script")
def api_run_script():
    body = request.get_json(force=True)
    device = body.get("device") or resolve_device()
    script = body.get("script", {})
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
    device = request.args.get("device") or resolve_device()
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
    device = (request.get_json(silent=True) or {}).get("device") or resolve_device()
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
    device = body.get("device") or resolve_device()
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


if __name__ == "__main__":
    print("=" * 50)
    print("  Android 自动化脚本编辑器")
    print("  打开浏览器访问: http://127.0.0.1:5000")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5000, debug=False)
