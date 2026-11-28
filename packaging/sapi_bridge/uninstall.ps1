<#
.SYNOPSIS
    卸载 VoiceHub SAPI 引擎。

.DESCRIPTION
    删掉语音令牌与 COM 类注册，并把默认音色换回原来的那一个。
    程序集与配置文件留在 %LOCALAPPDATA%\voicehub\sapi\，重装时直接复用。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File uninstall.ps1
#>
[CmdletBinding()]
param(
    [ValidateSet('User', 'Machine')]
    [string]$Scope = 'Machine',

    [string]$TokenId = 'VoiceHub_TTS',
    [switch]$RemoveFiles
)

$ErrorActionPreference = 'Stop'

$ClassId = '{6F3C0A5E-1C7B-4D2E-9E4B-6A0D8F35C211}'
$FactoryClassId = '{2B9D7E64-0F3A-4C58-9D1E-7A6C5B8E2F40}'
$InstallDir = Join-Path $env:LOCALAPPDATA 'voicehub\sapi'
$DllPath = Join-Path $InstallDir 'VoiceHub.Sapi.dll'

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

function Remove-RegistryKey {
    param([Microsoft.Win32.RegistryView]$View, [string]$Path)
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($Hive, $View)
    try {
        $base.DeleteSubKeyTree($Path, $false)
    }
    finally { $base.Close() }
}

if ($Scope -eq 'Machine') {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Step '需要管理员权限，重新以管理员身份运行'
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $MyInvocation.MyCommand.Path,
            '-Scope', $Scope, '-TokenId', $TokenId)
        if ($RemoveFiles) { $arguments += '-RemoveFiles' }
        $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        exit $proc.ExitCode
    }
}

Write-Step '恢复默认音色'
if (Test-Path $DllPath) {
    try {
        $assembly = [System.Reflection.Assembly]::LoadFrom($DllPath)
        $bridge = $assembly.GetType('VoiceHub.Sapi.Bridge')
        $current = $bridge::DefaultVoiceId()
        if ($current -like "*$TokenId") {
            $fallback = ''
            $configPath = Join-Path $InstallDir 'bridge.json'
            if (Test-Path $configPath) {
                try {
                    $cfg = Get-Content -Raw -Encoding UTF8 $configPath | ConvertFrom-Json
                    if ($cfg.previous_default_voice -and ($cfg.previous_default_voice -notlike "*$TokenId")) {
                        $fallback = [string]$cfg.previous_default_voice
                    }
                }
                catch { }
            }
            if (-not $fallback) {
                $voices = $bridge::Voices()
                $huihui = 'HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_ZH-CN_HUIHUI_11.0'
                if ($voices.ContainsKey($huihui)) {
                    $fallback = $huihui
                }
                else {
                    $fallback = ($voices.Keys | Where-Object { $_ -notlike "*$TokenId" } | Select-Object -First 1)
                }
            }
            if ($fallback) {
                Write-Info "当前默认是 $TokenId，换回 $fallback → $($bridge::SetDefaultVoiceId($fallback))"
            }
            else {
                Write-Info '找不到其它系统音色可恢复，保留现状。'
            }
        }
        else {
            Write-Info "默认音色没动过：$current"
        }
    }
    catch {
        Write-Info "读默认音色失败：$($_.Exception.Message)"
    }
}

Write-Step '删注册表项'
foreach ($view in @([Microsoft.Win32.RegistryView]::Registry32, [Microsoft.Win32.RegistryView]::Registry64)) {
    Remove-RegistryKey -View $view -Path "$TokenRoot\$TokenId"
    Remove-RegistryKey -View $view -Path "$ClassRoot\$ClassId"
    Remove-RegistryKey -View $view -Path "$ClassRoot\$FactoryClassId"
}
Write-Info "令牌 $TokenId 与 COM 类 $ClassId / $FactoryClassId 已删除"

if ($RemoveFiles -and (Test-Path $InstallDir)) {
    Write-Step '清文件'
    Remove-Item -Recurse -Force $InstallDir
    Write-Info $InstallDir
}
else {
    Write-Info "文件留在 $InstallDir（要删就加 -RemoveFiles）"
}

Write-Step '卸完了'
