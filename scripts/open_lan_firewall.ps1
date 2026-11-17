# Adds or refreshes the inbound firewall rule that lets other machines on the
# LAN reach VoiceHub. Requires elevation; the script re-launches itself with a
# UAC prompt when it is not already elevated.
#
#   powershell -ExecutionPolicy Bypass -File scripts\open_lan_firewall.ps1 -Port 17865
#
# Windows only. The rule is scoped to the Private profile: public networks stay
# closed, because VoiceHub has no authentication.

param(
    [int]$Port = 17865,
    [string]$Name = "VoiceHub"
)

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-Port", $Port,
        "-Name", "`"$Name`""
    )
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $arguments
    exit 0
}

Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule `
    -DisplayName $Name `
    -Direction Inbound `
    -Protocol TCP `
    -LocalPort $Port `
    -Action Allow `
    -Profile Private `
    -Description "VoiceHub (no authentication; private networks only)" | Out-Null

Write-Host "Firewall rule '$Name' allows inbound TCP $Port on the Private profile."
