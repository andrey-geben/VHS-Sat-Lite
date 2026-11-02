# Build script for VHS-Sat-Lite (Windows, onedir)
# Usage: .\build.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "=== Cleaning previous build ===" -ForegroundColor Cyan
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist")  { Remove-Item -Recurse -Force "dist" }

Write-Host "=== Checking FFmpeg in bin\ ===" -ForegroundColor Cyan
if (-not (Test-Path "bin\ffmpeg.exe")) {
    Write-Error "bin\ffmpeg.exe not found. Download FFmpeg from https://www.gyan.dev/ffmpeg/builds/ and put ffmpeg.exe + ffprobe.exe into bin\"
    exit 1
}
if (-not (Test-Path "bin\ffprobe.exe")) {
    Write-Error "bin\ffprobe.exe not found."
    exit 1
}

Write-Host "=== Running PyInstaller ===" -ForegroundColor Cyan
pyinstaller --noconfirm VHS-Sat-Lite.spec
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed with exit code $LASTEXITCODE"
    exit 1
}

$DistDir = "dist\VHS-Sat-Lite"

Write-Host "=== Copying FFmpeg into dist\bin\ ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path "$DistDir\bin" | Out-Null
Copy-Item "bin\ffmpeg.exe"  "$DistDir\bin\ffmpeg.exe"  -Force
Copy-Item "bin\ffprobe.exe" "$DistDir\bin\ffprobe.exe" -Force

Write-Host "=== Creating data/, logs/, downloads/ ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path "$DistDir\data"      | Out-Null
New-Item -ItemType Directory -Force -Path "$DistDir\data\hls"  | Out-Null
New-Item -ItemType Directory -Force -Path "$DistDir\data\pip"  | Out-Null
New-Item -ItemType Directory -Force -Path "$DistDir\logs"      | Out-Null
New-Item -ItemType Directory -Force -Path "$DistDir\downloads" | Out-Null

Write-Host "=== Copying APK from downloads\ (if any) ===" -ForegroundColor Cyan
$apks = Get-ChildItem "downloads\*.apk" -ErrorAction SilentlyContinue
if ($apks) {
    foreach ($apk in $apks) {
        Copy-Item $apk.FullName "$DistDir\downloads\$($apk.Name)" -Force
        $apkMb = [math]::Round($apk.Length / 1MB, 1)
        Write-Host ("  copied {0} ({1} MB)" -f $apk.Name, $apkMb)
    }
} else {
    Write-Host "  no APK in downloads\ - /download page will show instructions only"
}

Write-Host "=== Copying config.example.yaml (if any) ===" -ForegroundColor Cyan
if (Test-Path "config.example.yaml") {
    Copy-Item "config.example.yaml" "$DistDir\config.example.yaml" -Force
}

Write-Host "=== Zipping result ===" -ForegroundColor Cyan
$version = Get-Date -Format "yyyyMMdd"
$zipName = "dist\VHS-Sat-Lite-$version.zip"
Compress-Archive -Path "$DistDir\*" -DestinationPath $zipName -Force
$zipMb = [math]::Round((Get-Item $zipName).Length / 1MB, 1)
Write-Host ("  {0} ({1} MB)" -f $zipName, $zipMb) -ForegroundColor Green

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green
Write-Host "Folder: $DistDir"
Write-Host "Zip:    $zipName"
Write-Host ""
Write-Host "To test: run $DistDir\VHS-Sat-Lite.exe"
Write-Host "Admin:   http://localhost:8300"
Write-Host "Viewer:  http://localhost:8301"