<#
.SYNOPSIS
    安装 VoiceHub SAPI 引擎：让系统朗读走 VoiceHub 的 /api/v1/tts。

.DESCRIPTION
    做的事：
      1. 用系统自带的 .NET Framework 编译器编出 VoiceHub.Sapi.dll；
      2. 把 DLL 与 bridge.json 放到 %LOCALAPPDATA%\voicehub\sapi\；
      3. 注册 COM 类（mscoree.dll + 程序集）；
      4. 注册 SAPI 语音令牌（默认令牌名 VoiceHub_TTS，显示名「VoiceHub TTS」）；
      5. 可选：把默认音色改成它（-SetDefault）。

    语音令牌只能装在 HKLM（系统只读机器级的音色令牌），所以默认走机器范围，
    脚本会自己弹 UAC 提权。-Scope User 只装 COM 类与配置，装不了音色令牌。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -SetDefault -VoiceId 9df4751995cb9964
#>
[CmdletBinding()]
param(
    [ValidateSet('User', 'Machine')]
    [string]$Scope = 'Machine',

    [string]$Endpoint = 'https://127.0.0.1:17865',
    [string]$Engine = 'auto',
    [string]$VoiceId = '',
    [string]$TokenId = 'VoiceHub_TTS',
    [string]$VoiceName = 'VoiceHub TTS',
    [string]$FallbackVoice = 'Microsoft Huihui Desktop',
    [int]$TimeoutSeconds = 30,
    [string]$Language = '804',

    [switch]$SetDefault,
    [switch]$NoBuild
)

$ErrorActionPreference = 'Stop'

$ClassId = '{6F3C0A5E-1C7B-4D2E-9E4B-6A0D8F35C211}'
$FactoryClassId = '{2B9D7E64-0F3A-4C58-9D1E-7A6C5B8E2F40}'
$ClassName = 'VoiceHub.Sapi.ClassFactory'
$AssemblyName = 'VoiceHub.Sapi, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null'
$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Join-Path $env:LOCALAPPDATA 'voicehub\sapi'
$DllPath = Join-Path $InstallDir 'VoiceHub.Sapi.dll'
$StubX64 = Join-Path $InstallDir 'VoiceHubSapiStub.x64.dll'
$StubX86 = Join-Path $InstallDir 'VoiceHubSapiStub.x86.dll'
$ConfigPath = Join-Path $InstallDir 'bridge.json'

if ($Scope -eq 'Machine') {
    $Hive = [Microsoft.Win32.RegistryHive]::LocalMachine
    $TokenRoot = 'SOFTWARE\Microsoft\Speech\Voices\Tokens'
    $ClassRoot = 'SOFTWARE\Classes\CLSID'
}
else {
    $Hive = [Microsoft.Win32.RegistryHive]::CurrentUser
    $TokenRoot = 'Software\Microsoft\Speech\Voices\Tokens'
    $ClassRoot = 'Software\Classes\CLSID'
}

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Info([string]$Text) { Write-Host "    $Text" }

function Get-CscPath {
    foreach ($candidate in @(
            (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
            (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'))) {
        if (Test-Path $candidate) { return $candidate }
    }
    throw '找不到 .NET Framework 编译器（csc.exe）。'
}

function Build-Engine {
    $source = Join-Path $SourceDir 'VoiceHub.Sapi.cs'
    if (-not (Test-Path $source)) { throw "找不到源码：$source" }
    Write-Step '编译引擎'
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $csc = Get-CscPath
    & $csc /nologo /codepage:65001 /target:library /platform:anycpu /optimize+ `
        "/out:$DllPath" /reference:System.Web.Extensions.dll $source
    if ($LASTEXITCODE -ne 0) { throw '编译失败。' }
    Write-Info "已生成 $DllPath"
}

function Build-Stub {
    # SAPI 自己加载引擎 DLL 并找 DllGetClassObject，托管程序集没有这个导出，
    # 所以要一个极小的原生 DLL 顶上。
    $source = Join-Path $SourceDir 'sapi_stub.rs'
    if (-not (Test-Path $source)) { throw "找不到源码：$source" }
    Write-Step '编译原生桩'
    if (-not (Get-Command rustc -ErrorAction SilentlyContinue)) {
        throw '找不到 rustc：SAPI 要原生 DLL，得用它编译桩。'
    }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $build = Join-Path $env:TEMP 'voicehub-sapi-build'
    New-Item -ItemType Directory -Force -Path $build | Out-Null
    & rustc --crate-type cdylib -O --target x86_64-pc-windows-msvc `
        -o (Join-Path $build 'stub64.dll') $source
    if ($LASTEXITCODE -ne 0) { throw '编译 64 位桩失败。' }
    & rustc --crate-type cdylib -O --target i686-pc-windows-msvc `
        -o (Join-Path $build 'stub32.dll') $source
    if ($LASTEXITCODE -ne 0) { throw '编译 32 位桩失败。' }
    Copy-Item (Join-Path $build 'stub64.dll') $StubX64 -Force
    Copy-Item (Join-Path $build 'stub32.dll') $StubX86 -Force
    Remove-Item $build -Recurse -Force
    Write-Info $StubX64
    Write-Info $StubX86
}

function Write-BridgeConfig {
    param([string]$PreviousDefaultVoice = '')
    Write-Step '写配置'
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $prev = $PreviousDefaultVoice
    if (-not $prev -and (Test-Path $ConfigPath)) {
        try {
            $old = Get-Content -Raw -Encoding UTF8 $ConfigPath | ConvertFrom-Json
            if ($old.previous_default_voice) { $prev = [string]$old.previous_default_voice }
        }
        catch { }
    }
    $config = [ordered]@{
        endpoint               = $Endpoint
        engine                 = $Engine
        voice_id               = $VoiceId
        format                 = 'wav'
        timeout_seconds        = $TimeoutSeconds
        fallback_voice         = $FallbackVoice
        voice_name             = $VoiceName
        language               = $Language
    }
    if ($prev) { $config['previous_default_voice'] = $prev }
    $json = $config | ConvertTo-Json
    [System.IO.File]::WriteAllText($ConfigPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Info $ConfigPath
}

function Set-RegistryValue {
    param(
        [Microsoft.Win32.RegistryView]$View,
        [string]$Path,
        [string]$Name,
        [object]$Value,
        [Microsoft.Win32.RegistryValueKind]$Kind = [Microsoft.Win32.RegistryValueKind]::String
    )
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($Hive, $View)
    try {
        $key = $base.CreateSubKey($Path)
        if ($null -eq $key) { throw "建不了注册表项：$Path" }
        try { $key.SetValue($Name, $Value, $Kind) } finally { $key.Close() }
    }
    finally { $base.Close() }
}

function Remove-RegistryValue {
    param([Microsoft.Win32.RegistryView]$View, [string]$Path, [string]$Name)
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($Hive, $View)
    try {
        $key = $base.OpenSubKey($Path, $true)
        if ($key) {
            try { $key.DeleteValue($Name, $false) } finally { $key.Close() }
        }
    }
    finally { $base.Close() }
}

function Register-ComClass {
    Write-Step '注册 COM 类'
    foreach ($view in @([Microsoft.Win32.RegistryView]::Registry32, [Microsoft.Win32.RegistryView]::Registry64)) {
        # 音色引擎指向原生桩：SAPI 加载的就是它。
        $stub = if ($view -eq [Microsoft.Win32.RegistryView]::Registry64) { $StubX64 } else { $StubX86 }
        $inproc = "$ClassRoot\$ClassId\InprocServer32"
        Set-RegistryValue -View $view -Path $inproc -Name '' -Value $stub
        Set-RegistryValue -View $view -Path $inproc -Name 'ThreadingModel' -Value 'Both'
        # 这里不能留 .NET 的登记：留着会绕过原生桩。
        foreach ($name in @('Assembly', 'Class', 'CodeBase', 'RuntimeVersion')) {
            Remove-RegistryValue -View $view -Path $inproc -Name $name
        }
        # 托管类工厂：桩通过 COM 找到它，再由它造出引擎。
        $factory = "$ClassRoot\$FactoryClassId\InprocServer32"
        Set-RegistryValue -View $view -Path $factory -Name '' -Value 'mscoree.dll'
        Set-RegistryValue -View $view -Path $factory -Name 'Assembly' -Value $AssemblyName
        Set-RegistryValue -View $view -Path $factory -Name 'Class' -Value $ClassName
        Set-RegistryValue -View $view -Path $factory -Name 'CodeBase' -Value ('file:///' + ($DllPath -replace '\\', '/'))
        Set-RegistryValue -View $view -Path $factory -Name 'RuntimeVersion' -Value 'v4.0.30319'
        Set-RegistryValue -View $view -Path $factory -Name 'ThreadingModel' -Value 'Both'
    }
    Write-Info "$ClassId → $StubX64 / $StubX86"
    Write-Info "$FactoryClassId → $DllPath"
}

function Get-SystemVoicePaths {
    # 借用系统自带中文音色的语言数据路径：SAPI 认这两个属性，缺了会说「找不到音色」。
    $data = '%windir%\Speech_OneCore\Engines\TTS\zh-CN\MSTTSLocZhCN.dat'
    $voice = '%windir%\Speech_OneCore\Engines\TTS\zh-CN\M2052Huihui'
    try {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
            [Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry64)
        try {
            $token = $base.OpenSubKey('SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_ZH-CN_HUIHUI_11.0')
            if ($token) {
                try {
                    $value = $token.GetValue('LangDataPath')
                    if ($value) { $data = [string]$value }
                    $value = $token.GetValue('VoicePath')
                    if ($value) { $voice = [string]$value }
                }
                finally { $token.Close() }
            }
        }
        finally { $base.Close() }
    }
    catch {
        Write-Info "读系统音色路径失败，用默认：$($_.Exception.Message)"
    }
    return @{ LangDataPath = $data; VoicePath = $voice }
}

function Register-VoiceToken {
    Write-Step '注册语音令牌'
    $paths = Get-SystemVoicePaths
    foreach ($view in @([Microsoft.Win32.RegistryView]::Registry32, [Microsoft.Win32.RegistryView]::Registry64)) {
        $token = "$TokenRoot\$TokenId"
        $display = "$VoiceName - VoiceHub"
        Set-RegistryValue -View $view -Path $token -Name '' -Value $display
        Set-RegistryValue -View $view -Path $token -Name 'CLSID' -Value $ClassId
        Set-RegistryValue -View $view -Path $token -Name $Language -Value $display
        Set-RegistryValue -View $view -Path $token -Name 'LangDataPath' -Value $paths.LangDataPath `
            -Kind ([Microsoft.Win32.RegistryValueKind]::ExpandString)
        Set-RegistryValue -View $view -Path $token -Name 'VoicePath' -Value $paths.VoicePath `
            -Kind ([Microsoft.Win32.RegistryValueKind]::ExpandString)
        Set-RegistryValue -View $view -Path "$token\Attributes" -Name 'Name' -Value $VoiceName
        Set-RegistryValue -View $view -Path "$token\Attributes" -Name 'Language' -Value $Language
        Set-RegistryValue -View $view -Path "$token\Attributes" -Name 'Gender' -Value 'Female'
        Set-RegistryValue -View $view -Path "$token\Attributes" -Name 'Age' -Value 'Adult'
        Set-RegistryValue -View $view -Path "$token\Attributes" -Name 'Vendor' -Value 'VoiceHub'
        Set-RegistryValue -View $view -Path "$token\Attributes" -Name 'SharedPronunciation' -Value ''
        Set-RegistryValue -View $view -Path "$token\Attributes" -Name 'SpLexicon' `
            -Value '{0655E396-25D0-11D3-9C26-00C04F8EF87C}'
        Set-RegistryValue -View $view -Path "$token\Attributes" -Name 'Version' -Value '1.0'
        Set-RegistryValue -View $view -Path "$token\Attributes" -Name 'DataVersion' -Value '1.0'
    }
    Write-Info "令牌名：$TokenId（显示名 $VoiceName）"
}

function Resolve-VoiceId {
    Write-Step '挑一个可用音色'
    try {
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
        [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
        $engines = Invoke-RestMethod -Uri "$Endpoint/api/v1/engines" -TimeoutSec 10
        $live = @($engines | Where-Object { $_.enabled -and $_.available } | ForEach-Object { $_.key })
        if ($live.Count -eq 0) {
            Write-Info '没有在跑的引擎，音色留空由使用者指定。'
            return ''
        }
        $voices = Invoke-RestMethod -Uri "$Endpoint/api/v1/voices?source=local&limit=200" -TimeoutSec 20
        foreach ($voice in $voices.data) {
            if ($voice.engines | Where-Object { $live -contains $_ }) {
                Write-Info "音色 $($voice.name)（$($voice.id)）"
                return $voice.id
            }
        }
    }
    catch {
        Write-Info "问不到项目接口：$($_.Exception.Message)"
    }
    return ''
}

function Get-Bridge {
    $assembly = [System.Reflection.Assembly]::LoadFrom($DllPath)
    return $assembly.GetType('VoiceHub.Sapi.Bridge')
}

function Set-DefaultVoice {
    Write-Step '改默认音色'
    $bridge = Get-Bridge
    $tokenPath = "HKEY_LOCAL_MACHINE\$TokenRoot\$TokenId"
    $previous = $bridge::DefaultVoiceId()
    Write-Info "原默认音色：$previous"
    if ($previous -and ($previous -notlike "*$TokenId")) {
        Write-BridgeConfig -PreviousDefaultVoice $previous
    }
    Write-Info "设置 $tokenPath → $($bridge::SetDefaultVoiceId($tokenPath))"
}

function Test-Installed {
    Write-Step '自检'
    $bridge = Get-Bridge
    $voices = $bridge::Voices()
    $found = $voices.Keys | Where-Object { $_ -like "*$TokenId" }
    if ($found) {
        Write-Info "音色已出现在系统音色列表：$($found -join ', ')"
    }
    else {
        Write-Info "系统音色列表里还没有 $TokenId；机器范围安装后才会出现。"
    }
    Write-Info "当前默认音色：$($bridge::DefaultVoiceId())"
}

if ($Scope -eq 'Machine') {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Step '需要管理员权限，重新以管理员身份运行'
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $MyInvocation.MyCommand.Path)
        foreach ($pair in $PSBoundParameters.GetEnumerator()) {
            if ($pair.Key -in @('Verbose', 'Debug', 'ErrorAction', 'WarningAction', 'InformationAction',
                    'ProgressAction', 'ErrorVariable', 'WarningVariable', 'InformationVariable',
                    'OutVariable', 'OutBuffer', 'PipelineVariable')) { continue }
            if ($pair.Value -is [switch]) {
                if ($pair.Value) { $arguments += "-$($pair.Key)" }
            }
            else {
                $arguments += @("-$($pair.Key)", "$($pair.Value)")
            }
        }
        $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        exit $proc.ExitCode
    }
}

if (-not $NoBuild) { Build-Engine; Build-Stub }
Write-BridgeConfig
Register-ComClass
if ($Scope -eq 'Machine') {
    Register-VoiceToken
}
else {
    Write-Step '用户范围'
    Write-Info '系统只认机器级音色令牌，用户范围只装 COM 类与配置。'
}

if (-not $VoiceId) {
    $resolved = Resolve-VoiceId
    if ($resolved) {
        $VoiceId = $resolved
        Write-BridgeConfig
    }
}

if ($SetDefault) { Set-DefaultVoice }
Test-Installed

Write-Step '装好了'
Write-Info "音色名：$VoiceName"
Write-Info "接口：$Endpoint/api/v1/tts（引擎 $Engine）"
Write-Info "日志：$(Join-Path $env:LOCALAPPDATA 'voicehub\sapi\bridge.log')"
Write-Info '自检：sapi.cpl 里看音色列表；让任意 SAPI 程序读一句，再看日志有没有「项目接口合成完成」。'
