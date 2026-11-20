# Build script for VHS-Sat-Lite (Windows, onedir)
# Usage: .\build.ps1

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

# Версия берётся из config.py (about.version), чтобы zip и приложение совпадали.
$version = "0.0.0"
$configText = Get-Content "config.py" -Raw -Encoding UTF8
if ($configText -match '"about"\s*:\s*\{[\s\S]*?"version"\s*:\s*"([^"]+)"') {
    $version = $Matches[1]
}
Write-Host "=== Version: $version ===" -ForegroundColor Cyan

$Py = "python"
if (Test-Path ".venv\Scripts\python.exe") { $Py = ".venv\Scripts\python.exe" }

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
$buildLog = Join-Path $ProjectRoot "build.log"
$buildErr = Join-Path $ProjectRoot "build.err.log"
$proc = Start-Process -FilePath $Py -ArgumentList @("-m","PyInstaller","--noconfirm","VHS-Sat-Lite.spec") -NoNewWindow -Wait -PassThru -RedirectStandardOutput $buildLog -RedirectStandardError $buildErr
if ($proc.ExitCode -ne 0) {
    Write-Host "--- last PyInstaller output ---" -ForegroundColor Red
    Get-Content $buildLog -Tail 30 -ErrorAction SilentlyContinue | Write-Host
    Get-Content $buildErr -Tail 30 -ErrorAction SilentlyContinue | Write-Host
    Write-Error "PyInstaller failed with exit code $($proc.ExitCode)"
    exit 1
}
Write-Host "  PyInstaller OK"

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

Write-Host "=== APK ===" -ForegroundColor Cyan
if ($env:VHS_INCLUDE_APK -eq "1") {
    $apks = Get-ChildItem "downloads\*.apk" -ErrorAction SilentlyContinue
    foreach ($apk in $apks) {
        Copy-Item $apk.FullName "$DistDir\downloads\$($apk.Name)" -Force
        Write-Host ("  copied {0} ({1} MB) - ТОЛЬКО для локальной проверки" -f $apk.Name,
                     ([math]::Round($apk.Length / 1MB, 1)))
    }
} else {
    Write-Host "  APK не включён: release-APK должен быть подписан отдельно и лежать в релизе." -ForegroundColor Yellow
    Write-Host "  Для локальной проверки: `$env:VHS_INCLUDE_APK=1; .\build.ps1"
}

Write-Host "=== Copying docs and config example ===" -ForegroundColor Cyan
foreach ($doc in @("README.md", "CHANGELOG.md", "config.example.yaml")) {
    if (Test-Path $doc) {
        Copy-Item $doc "$DistDir\$doc" -Force
        Write-Host "  $doc"
    }
}

Write-Host "=== Zipping result ===" -ForegroundColor Cyan
$zipName = "dist\VHS-Sat-Lite-$version.zip"
Compress-Archive -Path "$DistDir\*" -DestinationPath $zipName -Force
$zipMb = [math]::Round((Get-Item $zipName).Length / 1MB, 1)
$hash = (Get-FileHash $zipName -Algorithm SHA256).Hash.ToLower()
"$hash  $(Split-Path $zipName -Leaf)" | Set-Content "$zipName.sha256" -Encoding ASCII
Write-Host ("  {0} ({1} MB)" -f $zipName, $zipMb) -ForegroundColor Green
Write-Host ("  SHA-256: {0}" -f $hash)
Write-Host ("  Checksum: {0}.sha256" -f $zipName)

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green
Write-Host "Folder: $DistDir"
Write-Host "Zip:    $zipName"
Write-Host ""
Write-Host "To test: run $DistDir\VHS-Sat-Lite.exe"
Write-Host "Admin:   http://localhost:8300"
Write-Host "Viewer:  http://localhost:8301"