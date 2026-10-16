# Pobiera pełną wersję ffmpeg dla Windows (BtbN, wariant GPL - zawiera libvmaf z CAMBI)
# i kopiuje ffmpeg.exe oraz ffprobe.exe do podfolderu .\ffmpeg obok programu.
#
# Uruchomienie: prawy klik -> "Uruchom za pomocą programu PowerShell"
# albo w konsoli:  powershell -ExecutionPolicy Bypass -File .\get_ffmpeg.ps1

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $here "ffmpeg"
$url  = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
$tmp  = Join-Path $env:TEMP ("scev_ffmpeg_" + [guid]::NewGuid().ToString("N"))

New-Item -ItemType Directory -Force -Path $tmp  | Out-Null
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$zip = Join-Path $tmp "ffmpeg.zip"

Write-Host "Pobieranie ffmpeg (ok. 150-200 MB)..."
Write-Host $url
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing

Write-Host "Rozpakowywanie..."
Expand-Archive -Path $zip -DestinationPath $tmp -Force
$bin = Get-ChildItem -Path $tmp -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
if (-not $bin) { throw "Nie znaleziono ffmpeg.exe w archiwum." }
Copy-Item (Join-Path $bin.DirectoryName "ffmpeg.exe")  $dest -Force
Copy-Item (Join-Path $bin.DirectoryName "ffprobe.exe") $dest -Force
Remove-Item $tmp -Recurse -Force

$filters = & (Join-Path $dest "ffmpeg.exe") -hide_banner -filters 2>$null
if ($filters -match "libvmaf") {
    Write-Host "OK: ffmpeg z libvmaf (CAMBI dostepne) w $dest" -ForegroundColor Green
} else {
    Write-Host "Uwaga: ten build nie ma libvmaf - CAMBI nie bedzie liczone." -ForegroundColor Yellow
}
Read-Host "Gotowe. Enter, aby zamknac"
