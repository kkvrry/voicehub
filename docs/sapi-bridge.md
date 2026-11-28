# SAPI 桥接：让系统朗读走 VoiceHub

Windows 的朗读接口是 SAPI 5（`sapi.dll`）。本机装一个 SAPI 语音引擎之后，
凡是走 SAPI 的程序——阅读 App、Word、资源管理器、无障碍朗读——念出来的声音就来自
VoiceHub 的 `/api/v1/tts`，引擎选择仍然是项目自己的「自动」路由。

项目接口不可用时，改用系统自带音色接着念，不出哑音。

## 组成

| 文件 | 作用 |
|---|---|
| `packaging/sapi_bridge/VoiceHub.Sapi.cs` | 引擎本体（C#）：实现 `ISpTTSEngine`、`ISpObjectWithToken`、`ISpNotifySource`，向项目接口要音频 |
| `packaging/sapi_bridge/sapi_stub.rs` | 原生桩（Rust）：只导出 `DllGetClassObject`，把 SAPI 的加载请求转给托管的类工厂 |
| `packaging/sapi_bridge/install.ps1` | 编译、注册语音令牌与 COM 类、写配置 |
| `packaging/sapi_bridge/uninstall.ps1` | 删注册项、把默认音色换回原来的那一个 |

装好的东西在 `%LOCALAPPDATA%\voicehub\sapi\`：

- `VoiceHub.Sapi.dll`：引擎程序集
- `VoiceHubSapiStub.x64.dll` / `VoiceHubSapiStub.x86.dll`：原生桩（32 位与 64 位各一份）
- `bridge.json`：接口地址、引擎、音色、超时、回落音色
- `bridge.log`：每次朗读的记录

## 为什么要原生桩

SAPI 不走 COM 的 `CoCreateInstance` 来造引擎：它自己加载引擎 DLL，再找
`DllGetClassObject`。托管程序集没有这个导出，所以注册表里音色令牌指向的
InprocServer32 必须是原生 DLL。原生桩只做一件事——把请求转给托管的类工厂，
真正的合成逻辑仍在 C# 里。

注册关系：

```
语音令牌 VoiceHub_TTS (HKLM\...\Speech\Voices\Tokens)
  └ CLSID {6F3C0A5E-…}  →  VoiceHubSapiStub.x64.dll   （原生桩）
                             └ VoiceHub.Sapi.dll        （托管类工厂 → 引擎）
```

## 音色令牌

令牌注册在 `HKLM\SOFTWARE\Microsoft\Speech\Voices\Tokens\VoiceHub_TTS`，
32 位视图（`Wow6432Node`）与 64 位视图各一份，系统只认机器级的令牌。

令牌上要有 `CLSID`、`LangDataPath`、`VoicePath` 与 `Attributes`（`Name`、
`Language`、`Gender`、`Age`、`Vendor`、`SpLexicon`…）。少 `LangDataPath` 或
`SpLexicon` 时 SAPI 会报「找不到音色」（`SPERR_VOICE_NOT_FOUND`）。

`LangDataPath` 与 `VoicePath` 借用系统自带中文音色的那两份现成文件：
`install.ps1` 从 `TTS_MS_ZH-CN_HUIHUI_11.0` 令牌里读出来照抄。

## 格式协商

SAPI 先问引擎要什么格式（`GetOutputFormat`），再按谈好的格式送文本（`Speak`）。
两条约定：

- 目标格式 ID 为空指针或全零 GUID，都按「没有指定」处理，回自己默认的
  22.05kHz / 16bit / 单声道，并把这个 ID 写回去。写回去的必须是
  `SPDFID_WaveFormatEx`（`c31adbae-527f-4ff5-a230-f62bb61ff70c`）。
- 引擎要实现 `ISpNotifySource`。缺了它，SAPI 造出引擎、问完格式就不再往下走。

项目接口给的是 16bit PCM WAV（采样率由引擎定，CosyVoice3 是 24kHz）。引擎按
SAPI 要的采样率与声道数重采样后再写进输出流。

## 回落

项目接口连不上、超时或返回的不是 PCM WAV 时，引擎改用系统音色。回落不走音频设备：
SAPI 此刻正占着设备，再开一路会返回 `SPERR_DEVICE_BUSY`。做法是让系统音色渲染到
SAPI 自己的内存流，读回来写进当前输出流。

回落音色按 `bridge.json` 的 `fallback_voice` 选（默认 Microsoft Huihui Desktop）；
那个音色不在时取任意一个不是自己的音色。

## 配置

`%LOCALAPPDATA%\voicehub\sapi\bridge.json`：

| 字段 | 含义 |
|---|---|
| `endpoint` | 项目接口地址，如 `https://127.0.0.1:17865` |
| `engine` | 引擎选择，`auto` 表示由项目自己路由 |
| `voice_id` | 音色 id（`GET /api/v1/voices` 里的 `id`） |
| `format` | 请求的音频格式，目前用 `wav` |
| `timeout_seconds` | 单次合成上限 |
| `fallback_voice` | 回落用的系统音色名 |
| `voice_name` | 音色在系统里显示的名字 |
| `language` | 语言 ID，中文是 `804` |
| `previous_default_voice` | 接管前系统原默认音色令牌 ID，取消默认或卸载时优先恢复 |

环境变量 `VOICEHUB_SAPI_CONFIG` 指向别的配置文件（调试用）。

## 设置页入口

在界面「设置 → 通用设置 → 系统朗读（SAPI）」中可直接完成以下操作（对应本机接口 `/api/v1/sapi`、`/api/v1/sapi/install`、`/api/v1/sapi/uninstall`，仅本机来源可调用）：

- **安装 / 重新安装 / 卸载**：调用 `install.ps1` / `uninstall.ps1`（写 `HKLM` 时触发 UAC 提权）。
- **朗读音色**：从音色库中选择音色，引擎固定按「自动」路由；保存后原子写入 `bridge.json`，无需管理员权限，下次朗读生效。
- **设为系统默认音色**：直接写当前用户注册表 `HKCU\SOFTWARE\Microsoft\Speech\Voices\DefaultTokenId`，无需提权；开启前记录原默认音色以便还原。

接口是本机自签证书，引擎只对回环地址放行该证书，且自己开一条 TLS 连接：
不改宿主进程的全局设置，宿主 App 的其他 HTTPS 请求不受影响。

## 安装与卸载

```bash
# 机器范围（默认；要写 HKLM，脚本会自己弹 UAC）
powershell -ExecutionPolicy Bypass -File packaging/sapi_bridge/install.ps1

# 指定音色；把默认音色也换成它
powershell -ExecutionPolicy Bypass -File packaging/sapi_bridge/install.ps1 -VoiceId <id> -SetDefault

# 卸载
powershell -ExecutionPolicy Bypass -File packaging/sapi_bridge/uninstall.ps1
```

编译用系统自带的 .NET Framework 编译器（`csc.exe`）与 `rustc`；两者都是本机现成的，
不引新依赖。桩要同时编 32 位与 64 位。

`-SetDefault` 会把系统默认音色改成 VoiceHub：所有走 SAPI 的程序都改用项目的声音，
不想要就别加这个参数。卸载脚本会把默认音色换回原来的那一个。

## 自检

```bash
# 音色在不在系统里
powershell -NoProfile -Command "& { $a=[Reflection.Assembly]::LoadFrom(\"$env:LOCALAPPDATA\voicehub\sapi\VoiceHub.Sapi.dll\"); $b=$a.GetType('VoiceHub.Sapi.Bridge'); $b::Voices().Keys }"

# 让它读一句，再看日志
tail "%LOCALAPPDATA%\voicehub\sapi\bridge.log"
```

日志里出现「项目接口合成完成」就是走了项目接口；出现「已回落系统音色」就是回落了。

`sapi.cpl`（控制面板 → 语音识别 → 文本转语音）里能看到 VoiceHub TTS 这个音色。

## 已知边界

- 默认音色不会自动改，要改得显式 `-SetDefault`。
- 回落时声音走调用方那条流，不外放到别的设备。
- 引擎在宿主进程内同步执行：合成期间那个 App 会卡住（网络往返的上限由
  `timeout_seconds` 兜着）。
- 只覆盖 SAPI 5。走 Windows 11 新朗读接口（OneCore / `SpeechRecognizer` 一类）
  的程序不走这条道。
