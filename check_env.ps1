$ErrorActionPreference = "Stop"

$REQUIRED_PYTHON_MAJOR = 3
$REQUIRED_PYTHON_MINOR = 11
$PACKAGES = @("pywin32", "Pillow", "pynput")

function Write-Step($msg) {
    Write-Host ""
    Write-Host ">>> $msg" -ForegroundColor Cyan
}

function Write-OK($msg) {
    Write-Host "    [OK] $msg" -ForegroundColor Green
}

function Write-Warn($msg) {
    Write-Host "    [WARN] $msg" -ForegroundColor Yellow
}

function Write-Fail($msg) {
    Write-Host ""
    Write-Host "    [ERROR] $msg" -ForegroundColor Red
}

Write-Step "Locating Python installation..."

$pythonExe = $null
$candidates = @("python", "python3", "py")

foreach ($candidate in $candidates) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) {
        # The Windows Store stub exits immediately with no output — skip it
        $testOutput = & $found.Source "--version" 2>&1
        if ($testOutput -match "Python\s+\d+\.\d+") {
            $pythonExe = $found.Source
            break
        }
    }
}

if (-not $pythonExe) {
    Write-Fail "No Python installation was found on this system."
    Write-Host ""
    Write-Host "    Python $REQUIRED_PYTHON_MAJOR.$REQUIRED_PYTHON_MINOR or newer is required." -ForegroundColor White
    Write-Host "    Download it from: https://www.python.org/downloads/" -ForegroundColor White
    Write-Host "    Make sure to tick 'Add Python to PATH' during installation." -ForegroundColor White
    exit 1
}

Write-Step "Checking Python version..."

$versionOutput = & $pythonExe "--version" 2>&1   # e.g. "Python 3.12.1"
if ($versionOutput -match "Python\s+(\d+)\.(\d+)\.(\d+)") {
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    $patch = [int]$Matches[3]
} else {
    Write-Fail "Could not parse Python version from: $versionOutput"
    exit 1
}

$versionStr = "$major.$minor.$patch"
Write-Host "    Found: Python $versionStr  (at $pythonExe)" -ForegroundColor White

if ($major -lt $REQUIRED_PYTHON_MAJOR -or
    ($major -eq $REQUIRED_PYTHON_MAJOR -and $minor -lt $REQUIRED_PYTHON_MINOR)) {

    Write-Fail "Python $versionStr is too old."
    Write-Host ""
    Write-Host "    This bot requires Python $REQUIRED_PYTHON_MAJOR.$REQUIRED_PYTHON_MINOR or newer." -ForegroundColor White
    Write-Host "    Download the latest version from: https://www.python.org/downloads/" -ForegroundColor White
    Write-Host "    Make sure to tick 'Add Python to PATH' during installation." -ForegroundColor White
    Write-Host "    You can have multiple Python versions installed side by side." -ForegroundColor White
    exit 1
}

Write-OK "Python $versionStr meets the requirement (>= $REQUIRED_PYTHON_MAJOR.$REQUIRED_PYTHON_MINOR)."

Write-Step "Checking pip..."

$pipOutput = & $pythonExe "-m" "pip" "--version" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warn "pip not found, attempting to bootstrap it..."
    & $pythonExe "-m" "ensurepip" "--upgrade" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Could not bootstrap pip. Please install it manually:"
        Write-Host "    https://pip.pypa.io/en/stable/installation/" -ForegroundColor White
        exit 1
    }
}

Write-OK "pip is available."

Write-Step "Upgrading pip to latest..."
$upgradeOut = & $pythonExe "-m" "pip" "install" "--upgrade" "pip" 2>&1
if ($LASTEXITCODE -eq 0) {
    # Extract the version line from pip's output for display
    $pipVer = ($upgradeOut | Select-String "Successfully installed pip|already up-to-date|Requirement already") | Select-Object -First 1
    Write-OK $(if ($pipVer) { $pipVer.Line.Trim() } else { "pip upgraded." })
} else {
    Write-Warn "pip upgrade failed (non-fatal, continuing with current version)."
}

Write-Step "Installing / updating required packages..."

$anyFailed = $false

foreach ($pkg in $PACKAGES) {
    Write-Host "    Installing $pkg..." -NoNewline -ForegroundColor White
    $out = & $pythonExe "-m" "pip" "install" "--upgrade" $pkg 2>&1
    if ($LASTEXITCODE -eq 0) {
        $summary = ($out | Select-String "Successfully installed|already satisfied|Requirement already") | Select-Object -First 1
        $label = if ($summary) { $summary.Line.Trim() } else { "done." }
        Write-Host " OK" -ForegroundColor Green
        Write-Host "        $label" -ForegroundColor DarkGray
    } else {
        Write-Host " FAILED" -ForegroundColor Red
        Write-Host ($out -join "`n") -ForegroundColor DarkGray
        $anyFailed = $true
    }
}

if ($anyFailed) {
    Write-Fail "One or more packages failed to install."
    Write-Host ""
    Write-Host "    Common fixes:" -ForegroundColor White
    Write-Host "      - Run launch.bat as Administrator (right-click -> Run as administrator)" -ForegroundColor White
    Write-Host "      - Check your internet connection" -ForegroundColor White
    Write-Host "      - If behind a proxy, configure pip: https://pip.pypa.io/en/stable/topics/https-certificates/" -ForegroundColor White
    exit 1
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  All checks passed. Ready to launch." -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

exit 0