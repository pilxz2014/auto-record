# Android 自动化脚本编辑器

通过 **adb** 自动化操作 Android 设备：定位点击、清空输入框、输入账号密码（支持中文）、自动切换下一个输入框等。编辑好的脚本可**导出为独立 shell 脚本**，脱离编辑器直接运行。

## 启动

```bash
cd D:\test
pip install flask        # 唯一依赖
python server.py
```

浏览器打开 **http://127.0.0.1:5000**

## 界面说明

所有动作都是通过**步骤编辑弹窗**统一创建/编辑：点底部「添加步骤」、步骤卡片上的「＋在此后插入」、✏️ 编辑，或**点截图 / 点控件蓝框**都会弹出同一个窗口，在里面选动作类型、填名称备注、填内容（坐标或目标控件会自动带入）。

- **左侧**：步骤列表（每步下方有「＋在此后插入」可**任意位置插入**；▶ 单步试跑、✏️ 编辑、⬆⬇ 排序、⧉ 复制、🗑 删除）
- **右侧截图面板**：
  - **📍 坐标模式**：点击截图 → 弹出编辑窗，坐标已自动换算好（点控件也可在坐标模式用）
  - **🎯 控件模式**：截图上叠加控件蓝框 / 下方可搜索控件列表，点击 → 弹出编辑窗，resource-id/文字已自动带入
- **顶栏**：设备选择、IME 状态灯（ADBKeyboard）、保存/打开脚本、导出 .sh、运行整个脚本
- **🖥 scrcpy**：打开设备实时镜像，运行时可实时观察

## 支持的步骤类型

| 步骤 | 说明 | 底层命令 |
|---|---|---|
| 点击 (坐标) | 固定坐标点击 | `input tap x y` |
| 点击 (控件) | 按 resource-id 或文字定位 | `uiautomator dump` → bounds 中心点 |
| **点击并清空** | 聚焦目标输入框 + 清空，一步到位 | tap + Ctrl+A/DEL |
| **点击并输入** | 聚焦 + 清空 + 输入文本，填账号密码最常用 | tap + 清空 + input |
| 输入文本 | 中英文自动切换方式 | 中文走 ADBKeyboard，英文走 `input text` |
| 清空输入框 | 清空当前聚焦的输入框 | Ctrl+A + DEL（降级 MOVE_END+DEL） |
| 按键 | TAB 切到下一个输入框等 | `input keyevent` |
| 滑动 / 等待 / 启动App / 返回 / Home | | `input swipe` / `motionevent` / sleep / `am start` 等 |

> **滑动轨迹录制**：在「📍 坐标模式」下，**按住截图直接拖动**即可实时记录整条轨迹和拖动速度，
> 松手自动生成一条「滑动」步骤（存储所有轨迹点 + 总时长）。运行时用 `input motionevent`
> 按记录的点和速度还原（Android 12+），不支持时自动回退为首尾单段滑动。导出的 .sh 用 `swipe_traj` 同样还原。

> **自动收起键盘**：脚本属性里有「收起键盘」勾选框（默认开）。输入完成后自动发 BACK
> 收起软键盘（ADBKeyboard 或自带输入法都管），避免键盘挡住下一步点击。后端执行与导出的 .sh
> （`HIDE_KEYBOARD=1`）都有效；取消勾选则不收键盘。

> **小技巧**：截图面板上方有「点这里 / 点这里并清空 / 在这里输入」动作选择器。
> 想「清空账号框后输入」，选「👆 点这里并清空」点账号框即可生成一条组合步骤；
> 想直接填某个框，选「⌨️ 在这里输入」点该框，会弹出输入框让你填内容，自动生成「聚焦+清空+输入」。

## 中文输入（ADBKeyboard，一次性安装）

`adb input text` 原生不支持中文，需安装 ADBKeyboard 输入法：

1. 下载 APK：https://github.com/senzhk/ADBKeyBoard/releases/latest/download/ADBKeyboard.apk
2. 放入 `adbkeyboard/ADBKeyboard.apk`
3. 点击编辑器顶栏红色的 **IME ✗** → 自动安装并启用，变绿即可

手动命令（等效）：

```bash
adb install -r adbkeyboard/ADBKeyboard.apk
adb shell ime enable com.android.adbkeyboard/.AdbIME
adb shell ime set com.android.adbkeyboard/.AdbIME
```

> 不装 ADBKeyboard 也能用，只是输入步骤仅支持英文/数字。

## 导出的 shell 脚本

导出文件在 `exports/<脚本名>.sh`，自包含（内嵌全部工具函数），Git Bash / Linux / macOS 通用：

```bash
cd D:\test
./exports/登录脚本.sh
```

脚本开头可改 `SERIAL`（设备序列号）和 `STEP_DELAY`（步骤间隔秒数）。

## 已保存的脚本

`scripts/` 目录下是 JSON 格式（编辑器内部格式），可随时重新打开编辑再导出。

## 注意事项

- **敏感信息**：脚本 JSON 和导出的 .sh 里明文包含账号密码，**勿提交到公开仓库**
- `uiautomator dump` 在游戏、自绘界面（如部分 Flutter 页面）拿不到控件树，这类场景只能用坐标模式
- 多设备连接时，先在顶栏下拉框选中目标设备
- scrcpy 路径默认为 `D:\Program Files\scrcpy-win64-v3.3.4\scrcpy.exe`，或放在 PATH 中
