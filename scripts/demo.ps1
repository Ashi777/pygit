<#
.SYNOPSIS
    Automated PyGit full-workflow demo for terminal recording.

.DESCRIPTION
    Run this script while your screen-recording tool is active.
    Every command is printed to the terminal before it executes so
    the viewer can follow along.  Total runtime: ~60-70 seconds.

    Recommended recording tools (Windows)
    -----------------------------------------------
    ScreenToGif   https://www.screentogif.com/
        1. Download and open ScreenToGif
        2. Recorder -> Window capture -> select this terminal
        3. Click Record
        4. Run:  .\scripts\demo.ps1
        5. Click Stop, trim the ends, Export -> Gif
           (check "Optimize" for a smaller file)
        6. Save as demo.gif in the repo root
        7. Suggested GIF settings:
              Quality  : 90
              Max size : 1024 x 600 (scale down if needed)
              Max delay: 1000 ms / frame

    Terminalizer  (CLI, requires Node.js)
        npm install -g terminalizer
        terminalizer record demo --skip-sharing
        # In the recorded shell, type:  .\scripts\demo.ps1  <Enter>
        # When done, press Ctrl+D to stop recording
        terminalizer render demo -o demo.gif
        Move-Item demo.gif ..\demo.gif    # move to repo root

    Best terminal settings for recording
    -----------------------------------------------
    Window size : 110 columns x 32 rows (or larger)
    Font        : Cascadia Code / Consolas, 14 pt
    Theme       : GitHub Dark / One Dark / Any dark theme
    Zoom level  : 100%

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
$DEMO_DIR  = "C:\tmp\pygit-demo"
$PAUSE_CMD = 0.8     # pause after each command
$PAUSE_LON = 1.8     # longer pause for the viewer to read longer output
$PAUSE_SEC = 0.4     # pause before a section banner

# UTF-8 encoder WITHOUT BOM -- works on all PowerShell versions (5.x and 7+)
$UTF8 = [System.Text.UTF8Encoding]::new($false)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-File([string]$Path, [string]$Content) {
    [System.IO.File]::WriteAllText(
        (Join-Path (Get-Location).Path $Path),
        $Content,
        $UTF8
    )
}

function Show-Banner([string]$Text) {
    $line = "-" * 58
    Write-Host ""
    Write-Host "  $line" -ForegroundColor DarkGray
    Write-Host "  $Text"  -ForegroundColor Yellow
    Write-Host "  $line" -ForegroundColor DarkGray
    Write-Host ""
    Start-Sleep -Seconds $PAUSE_SEC
}

# Print a fake prompt + command text, then execute the scriptblock.
function Run([string]$Display, [scriptblock]$Block) {
    Write-Host ""
    Write-Host "  $>" -NoNewline -ForegroundColor Green
    Write-Host " $Display" -ForegroundColor White
    Start-Sleep -Milliseconds 280
    & $Block
    Start-Sleep -Seconds $PAUSE_CMD
}

# ---------------------------------------------------------------------------
# Clean slate
# ---------------------------------------------------------------------------
Clear-Host
Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "  PyGit  --  Git implementation from scratch" -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host ""
Start-Sleep -Seconds 1.2

if (Test-Path $DEMO_DIR) { Remove-Item -Recurse -Force $DEMO_DIR }
New-Item -ItemType Directory -Force -Path $DEMO_DIR | Out-Null
Set-Location $DEMO_DIR

# ---------------------------------------------------------------------------
# 1/5  init + first commit
# ---------------------------------------------------------------------------
Show-Banner "1 / 5  |  init  +  first commit"

Run "pygit init ." {
    pygit init .
    # Add minimal user config (pygit reads identity from .git/config)
    $cfgLine = "`n[user]`n    name = Demo`n    email = demo@pygit.dev"
    Add-Content (Join-Path ".git" "config") $cfgLine
}

Write-File "README.md" "# PyGit Demo`n"
Write-File "main.py"   "print('hello from pygit')`n"

Run "pygit add ."      { pygit add . }
Run "pygit status"     { pygit status }
Run "pygit commit -m 'initial commit'" {
    pygit commit -m "initial commit"
}

# ---------------------------------------------------------------------------
# 2/5  branch
# ---------------------------------------------------------------------------
Show-Banner "2 / 5  |  branch"

Run "pygit branch feature" { pygit branch feature }
Run "pygit branch"         { pygit branch }

# ---------------------------------------------------------------------------
# 3/5  switch + diff + commit
# ---------------------------------------------------------------------------
Show-Banner "3 / 5  |  switch  +  diff  +  commit"

Run "pygit switch feature" { pygit switch feature }

# Modify main.py to produce a meaningful unified diff
Write-File "main.py" @"
print('hello from pygit')

def greet(name):
    '''Return a personalised greeting.'''
    return f'Hello, {name}!'

if __name__ == '__main__':
    print(greet('world'))
"@

Start-Sleep -Seconds 0.5
Run "pygit diff"  { pygit diff }
Start-Sleep -Seconds $PAUSE_LON

Run "pygit add main.py" { pygit add main.py }
Run "pygit commit -m 'add greet function'" {
    pygit commit -m "add greet function"
}

# ---------------------------------------------------------------------------
# 4/5  switch + merge
# ---------------------------------------------------------------------------
Show-Banner "4 / 5  |  switch  +  merge"

Run "pygit switch main" { pygit switch main }
Start-Sleep -Seconds $PAUSE_LON

Run "pygit log --graph" { pygit log --graph }
Start-Sleep -Seconds $PAUSE_LON

Run "pygit merge feature" { pygit merge feature }
Start-Sleep -Seconds $PAUSE_LON

Run "pygit log --graph" { pygit log --graph }

# ---------------------------------------------------------------------------
# 5/5  tag
# ---------------------------------------------------------------------------
Show-Banner "5 / 5  |  tag"

Run "pygit tag v1.0" { pygit tag v1.0 }
Run "pygit tag"      { pygit tag }

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "  =============================================" -ForegroundColor Green
Write-Host "  Done!  316 tests  |  zero external deps" -ForegroundColor Green
Write-Host "  =============================================" -ForegroundColor Green
Write-Host ""
Start-Sleep -Seconds 2
