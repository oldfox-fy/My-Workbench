#Requires -RunAsAdministrator
<#
.SYNOPSIS
Prepends local Python 3.11 to the Windows system PATH so it beats OSGeo4W.
Run: Right-click → "Run with PowerShell" (as Administrator), or:
     powershell -ExecutionPolicy Bypass -File "scripts\set-local-python-path.ps1"
#>

$ErrorActionPreference = "Stop"

$localPyDir   = "C:\Users\Ocean\AppData\Local\Programs\Python\Python311"
$localPyScr   = "C:\Users\Ocean\AppData\Local\Programs\Python\Python311\Scripts"

$machinePath = [Environment]::GetEnvironmentVariable('PATH', 'Machine')

if (-not $machinePath) {
    Write-Host "[ERROR] Could not read system PATH" -ForegroundColor Red
    exit 1
}

$entries = $machinePath -split ';' | Where-Object { $_ -ne '' }

# Remove existing local-Python entries from system PATH (dedup)
$entries = $entries | Where-Object {
    $_.TrimEnd('\') -ne $localPyDir.TrimEnd('\') -and
    $_.TrimEnd('\') -ne $localPyScr.TrimEnd('\')
}

# Prepend local Python at the very front
$newPath = @($localPyDir, $localPyScr) + $entries
$newPathStr = $newPath -join ';'

[Environment]::SetEnvironmentVariable('PATH', $newPathStr, 'Machine')

# Also clean up user PATH so there's no duplicate
$userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
if ($userPath) {
    $userEntries = $userPath -split ';' | Where-Object { $_ -ne '' }
    $userEntries = $userEntries | Where-Object {
        $_.TrimEnd('\') -ne $localPyDir.TrimEnd('\') -and
        $_.TrimEnd('\') -ne $localPyScr.TrimEnd('\')
    }
    $newUserPath = $userEntries -join ';'
    [Environment]::SetEnvironmentVariable('PATH', $newUserPath, 'User')
}

Write-Host "[OK] System PATH updated — local Python 3.11 is now first." -ForegroundColor Green
Write-Host "      $localPyDir" -ForegroundColor Cyan
Write-Host "      $localPyScr" -ForegroundColor Cyan
Write-Host ""
Write-Host "Restart your terminal for the changes to take effect." -ForegroundColor Yellow
