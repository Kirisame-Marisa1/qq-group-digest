# run_selfloader.ps1 -- ONE injection of the STANDALONE loader (no NapCat runtime).
#
# Key design properties:
#   1. The injector is OUR OWN copy under injector\boot\ (NapCatWinBootMain.exe +
#      NapCatWinBootHook.dll + loadNapCat.js + qqnt.json = ~57 KB), NOT napcat-pkg.
#      napcat-pkg (455 MB of NapCat shell runtime) is no longer needed at all.
#   2. NAPCAT_MAIN_PATH points at our selfloader.mjs -> no OneBot adapter, no packet
#      backend, no WebUI, no plugin system, no listening ports.
#   3. No "-q <uin>" on the command line; the account arrives via QQ_QUICK_UIN
#      (read automatically from config.json when not passed via -Uin).
#   4. Start-Process -ArgumentList does NOT auto-quote; arguments are explicitly quoted.
#   5. The injector never exits by design, so it is started DETACHED and killed
#      explicitly during teardown, followed by a 6-file SHA256 check and normal QQ restart.
#
# ASCII ONLY: PowerShell 5.1 reads .ps1 as GBK and non-ASCII breaks the parser.

param(
  [string]$Stage = '',
  [int]$WaitSeconds = 0,
  [string]$QQDir = '',
  [string]$Uin = ''
)

$ErrorActionPreference = 'Continue'

$inj    = $PSScriptRoot
$boot   = Join-Path $inj 'boot'
$outDir = Join-Path $inj 'out'
$stage  = if ($Stage) { $Stage } else { Join-Path $inj 'attempt-selfloader' }
$probe  = Join-Path $outDir 'probe-selfloader.jsonl'
$plan   = Join-Path $outDir 'fill-plan.json'

# Auto-read config.json from repo root or ..\app\config.json
$cfgPath = Join-Path (Split-Path -Parent $inj) 'config.json'
if (-not (Test-Path $cfgPath)) {
  $altCfg = Join-Path (Join-Path (Split-Path -Parent $inj) 'app') 'config.json'
  if (Test-Path $altCfg) { $cfgPath = $altCfg }
}
$cfgObj = $null
if (Test-Path $cfgPath) {
  try { $cfgObj = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
}

if (-not $QQDir) {
  if ($cfgObj -and $cfgObj.qq_exe) {
    $QQDir = Split-Path -Parent ($cfgObj.qq_exe -replace '/', '\')
  } elseif (Test-Path 'D:\QQNT\QQ.exe') {
    $QQDir = 'D:\QQNT'
  } else {
    $QQDir = 'C:\Program Files\Tencent\QQNT'
  }
}
if (-not $Uin -and $cfgObj -and $cfgObj.account) {
  $Uin = [string]$cfgObj.account
}

# Derive WaitSeconds from fill-plan.json budget_ms + 90s if not explicitly set
if ($WaitSeconds -le 0) {
  $WaitSeconds = 300
  if (Test-Path $plan) {
    try {
      $planObj = Get-Content $plan -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($planObj.budget_ms -and [int]$planObj.budget_ms -gt 0) {
        $WaitSeconds = [int][math]::Ceiling(([double]$planObj.budget_ms / 1000.0) + 90)
      }
    } catch {}
  }
}

$qq  = Join-Path $QQDir 'QQ.exe'
New-Item -ItemType Directory -Force -Path $stage | Out-Null
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$log = Join-Path $stage 'inject-stdout.log'

$verDir = (Get-ChildItem (Join-Path $QQDir 'versions') -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1).Name
if (-not $verDir) { $verDir = '9.9.36-53644' }

function HashMap {
  param([string[]]$List)
  $o = @{}
  foreach ($f in $List) {
    $p = Join-Path $QQDir $f
    if (Test-Path $p) { $o[$f] = (Get-FileHash $p -Algorithm SHA256).Hash } else { $o[$f] = 'MISSING' }
  }
  return $o
}
$files = @(
  'QQ.exe',
  ("versions\{0}\QQNT.dll" -f $verDir),
  ("versions\{0}\resources\app\package.json" -f $verDir),
  ("versions\{0}\resources\app\wrapper.node" -f $verDir),
  ("versions\{0}\resources\app\major.node" -f $verDir),
  ("versions\{0}\resources\app\dbghelp.dll" -f $verDir)
)
$before = HashMap -List $files
$before.GetEnumerator() | Sort-Object Name | ForEach-Object { '{0}  {1}' -f $_.Value, $_.Key } |
  Set-Content (Join-Path $stage 'qq-hashes-BEFORE.txt')

# ---------- 0. stop QQ, clear stale injectors ----------
Write-Output '[step] killing QQ ...'
taskkill /f /im QQ.exe 2>&1 | Out-Null
Start-Sleep -Seconds 5
Write-Output ('[step] QQ processes after kill = {0}' -f (@(Get-Process -Name QQ -ErrorAction SilentlyContinue)).Count)
$stale = @(Get-Process -Name NapCatWinBootMain -ErrorAction SilentlyContinue)
if ($stale.Count) {
  Write-Output ('[step] killing {0} stale injector process(es)' -f $stale.Count)
  $stale | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 3
}
Remove-Item $log -Force -ErrorAction SilentlyContinue
Remove-Item $probe -Force -ErrorAction SilentlyContinue

# ---------- 1. environment: our boot files, our loader, account via env ----------
$env:NAPCAT_PATCH_PACKAGE         = Join-Path $boot 'qqnt.json'
$env:NAPCAT_LOAD_PATH             = Join-Path $boot 'loadNapCat.js'
$env:NAPCAT_INJECT_PATH           = Join-Path $boot 'NapCatWinBootHook.dll'
$env:NAPCAT_LAUNCHER_PATH         = Join-Path $boot 'NapCatWinBootMain.exe'
$env:NAPCAT_MAIN_PATH             = Join-Path $inj 'selfloader.mjs'
$env:NAPCAT_DISABLE_MULTI_PROCESS = '1'
$env:QQ_QUICK_UIN                 = $Uin
$env:QQPROBE_OUT                  = $outDir
$env:QQ_DIR                       = $QQDir
Remove-Item Env:ELECTRON_ENABLE_LOGGING -ErrorAction SilentlyContinue
Remove-Item Env:ELECTRON_ENABLE_STACK_DUMPING -ErrorAction SilentlyContinue

# ---------- 2. launch (detached, quoted args) ----------
$launcher = Join-Path $boot 'NapCatWinBootMain.exe'
$hook     = Join-Path $boot 'NapCatWinBootHook.dll'
Set-Location $boot
Write-Output '[step] launching standalone loader (no -q, account via QQ_QUICK_UIN) ...'
$injector = Start-Process -FilePath $launcher -ArgumentList @(('"{0}"' -f $qq), ('"{0}"' -f $hook)) `
  -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError ($log + '.err')
Write-Output ('[step] injector pid = {0} (detached; never exits by design)' -f $injector.Id)
Start-Sleep -Seconds 6
foreach ($pat in @('argv\[0\]', 'argv\[1\]', 'Boot Command')) {
  Select-String -Path $log -Pattern $pat -Encoding UTF8 -ErrorAction SilentlyContinue |
    Select-Object -First 1 | ForEach-Object { Write-Output ('[step] ' + $_.Line.Trim()) }
}

# ---------- 3. wait for the loader ----------
Write-Output ('[step] waiting up to {0}s for the loader ...' -f $WaitSeconds)
$deadline = (Get-Date).AddSeconds($WaitSeconds)
$seen = ''
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 3
  if (Test-Path $probe) {
    $txt = Get-Content $probe -Raw -Encoding UTF8
    if ($txt -match '"event":"done"' -or $txt -match '"event":"fatal"') { $seen = 'done'; break }
  }
  if ((@(Get-Process -Name QQ -ErrorAction SilentlyContinue)).Count -eq 0) { $seen = 'qq-died'; break }
}
Write-Output ('[step] loader wait finished: {0}' -f $seen)
if (Test-Path $probe) {
  Copy-Item $probe (Join-Path $stage 'probe-selfloader.jsonl') -Force
  Write-Output ('[step] loader lines = {0}' -f (@(Get-Content $probe).Count))
} else {
  Write-Output '[step] !! no probe output was written'
}

# ---------- 4. teardown ----------
Write-Output '[step] tearing down ...'
Get-Process -Name NapCatWinBootMain -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
taskkill /f /im QQ.exe 2>&1 | Out-Null
Start-Sleep -Seconds 6
# QQ may create its own guild db inside the working directory (we Set-Location to boot).
Get-ChildItem $boot -Filter 'guild*.db*' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Write-Output ('[step] QQ processes after teardown = {0}' -f (@(Get-Process -Name QQ -ErrorAction SilentlyContinue)).Count)

$after = HashMap -List $files
$after.GetEnumerator() | Sort-Object Name | ForEach-Object { '{0}  {1}' -f $_.Value, $_.Key } |
  Set-Content (Join-Path $stage 'qq-hashes-AFTER.txt')
$changed = @(); foreach ($k in $files) { if ($before[$k] -ne $after[$k]) { $changed += $k } }
if ($changed.Count) { Write-Output ('[step] !! QQ files CHANGED: ' + ($changed -join '; ')) }
else                { Write-Output '[step] QQ file integrity: unchanged (all hashes identical)' }

foreach ($port in 3000, 6099) {
  $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  if ($c) { Write-Output ('[step] !! port {0} still listening (pid {1})' -f $port, $c.OwningProcess) }
  else    { Write-Output ('[step] port {0} free' -f $port) }
}

Write-Output '[step] starting QQ normally (official state restored)'
Start-Process $qq
Start-Sleep -Seconds 18
Get-CimInstance Win32_Process -Filter "name='QQ.exe'" | Where-Object { $_.CommandLine -notmatch '--type=' } |
  Select-Object ProcessId, CommandLine | Out-File (Join-Path $stage 'qq-pids-after.txt')
Write-Output '[step] done'
