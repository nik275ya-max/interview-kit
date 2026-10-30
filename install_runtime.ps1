$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$temp = Join-Path $env:TEMP "interviewkit-runtime"
$bin = Join-Path $root "bin"
$models = Join-Path $root "models"

New-Item -ItemType Directory -Force -Path $temp, $bin, $models | Out-Null

$ffmpeg = Join-Path $bin "ffmpeg.exe"
if (!(Test-Path $ffmpeg)) {
    $zip = Join-Path $temp "ffmpeg.zip"
    $unpack = Join-Path $temp "ffmpeg"
    Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip
    Remove-Item $unpack -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive $zip $unpack -Force
    $downloaded = Get-ChildItem $unpack -Recurse -Filter ffmpeg.exe | Select-Object -First 1
    if (!$downloaded) { throw "ffmpeg.exe not found in archive" }
    Copy-Item $downloaded.FullName $ffmpeg
}

$whisper = Join-Path $bin "whisper\Release\whisper-cli.exe"
if (!(Test-Path $whisper)) {
    $zip = Join-Path $temp "whisper-bin-x64.zip"
    $destination = Join-Path $bin "whisper"
    Invoke-WebRequest "https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-x64.zip" -OutFile $zip
    Remove-Item $destination -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive $zip $destination -Force
}

$model = Join-Path $models "ggml-small-q5_1.bin"
if (!(Test-Path $model)) {
    Invoke-WebRequest "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small-q5_1.bin?download=true" -OutFile $model
}

Write-Host "InterviewKit runtime installed." -ForegroundColor Green
