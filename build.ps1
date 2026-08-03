[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Resolve-Tool([string]$Name) {
    $command = Get-Command "$Name.exe" -ErrorAction SilentlyContinue
    if (-not $command) {
        return $null
    }
    $item = Get-Item -LiteralPath $command.Source -Force
    if ($item.LinkType -and $item.Target) {
        $target = if ([System.IO.Path]::IsPathRooted($item.Target)) {
            $item.Target
        } else {
            Join-Path $item.DirectoryName $item.Target
        }
        return (Resolve-Path -LiteralPath $target).Path
    }
    return $item.FullName
}

$ffmpeg = Resolve-Tool "ffmpeg"
if (-not $ffmpeg) {
    throw "FFmpeg was not found. Install FFmpeg or place ffmpeg.exe on PATH before building."
}
$ffprobe = Resolve-Tool "ffprobe"
if (-not $ffprobe) {
    $candidate = Join-Path (Split-Path -Parent $ffmpeg) "ffprobe.exe"
    if (Test-Path -LiteralPath $candidate) {
        $ffprobe = (Resolve-Path -LiteralPath $candidate).Path
    }
}
if (-not $ffprobe) {
    throw "ffprobe.exe was not found beside FFmpeg."
}

$binaryArguments = [System.Collections.Generic.List[string]]::new()
$seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($binary in @($ffmpeg, $ffprobe)) {
    if ($seen.Add($binary)) {
        $binaryArguments.Add("--add-binary")
        $binaryArguments.Add("$binary;.")
    }
}
$ffmpegBin = Split-Path -Parent $ffmpeg
foreach ($dll in Get-ChildItem -LiteralPath $ffmpegBin -Filter "*.dll" -File -ErrorAction SilentlyContinue) {
    if ($seen.Add($dll.FullName)) {
        $binaryArguments.Add("--add-binary")
        $binaryArguments.Add("$($dll.FullName);.")
    }
}

$pyinstallerArguments = @(
    "--noconfirm", "--clean", "--onefile", "--windowed", "--name", "VideoCrush",
    "video_compressor.py"
)
$pyinstallerArguments += $binaryArguments
& pyinstaller @pyinstallerArguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
Write-Host "Built bundled-FFmpeg artifact: $(Resolve-Path dist\VideoCrush.exe)"
