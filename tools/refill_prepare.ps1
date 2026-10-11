# Refill round preparation: rebuild index -> build the FULL gap table -> print next step.
#
# Pair with refill_finish.ps1:
#   refill_prepare.ps1   (before a fill round: make sure the table covers ALL gaps)
#   run_selfloader.ps1   (the one-shot standalone injection itself)
#   refill_finish.ps1    (after: rebuild + import + acceptance check + purge)
#
# Why this exists: the fill table must be generated from a FRESH index. If you generate it
# before rebuilding, it still lists gaps that the previous round already filled, and every
# such entry costs a wasted request (each request is visible to the server).
#
# ASCII ONLY: PowerShell 5.1 reads .ps1 as GBK and non-ASCII breaks the parser.

param(
  [string]$Window = '30d',
  [int]$Days = 11,
  [int]$MergeGap = 600,
  [switch]$SkipBuild
)

$ErrorActionPreference = 'Continue'
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = 'python' }
$app = Split-Path -Parent $PSScriptRoot
$inj = Join-Path $app 'injector'
if (-not (Test-Path $inj)) {
  $inj = Join-Path (Split-Path -Parent $app) 'injector'
}

if (-not $SkipBuild) {
  Write-Output ('[1/3] rebuilding index (window {0}, on-demand decryption) ...' -f $Window)
  Push-Location $app
  $t0 = Get-Date
  & $py -X utf8 'scripts\ntqq_build.py' --window $Window --source stream 2>&1 | Select-Object -Last 8
  Write-Output ('      done, exit={0}, elapsed {1:N1}s' -f $LASTEXITCODE, ((Get-Date) - $t0).TotalSeconds)
  Pop-Location
} else {
  Write-Output '[1/3] skipped index rebuild'
}

Write-Output ''
Write-Output '[2/3] building the FULL gap table (all gaps, merged into scan segments) ...'
& $py -X utf8 (Join-Path $PSScriptRoot 'gap_plan.py') --days $Days --top-groups 75 --min-rows 1 --min-gap 5 --merge-gap $MergeGap 2>&1 |
  Select-Object -Last 6 | ForEach-Object { '      ' + ([string]$_).Trim() }

Write-Output ''
Write-Output '[3/3] next step'
Write-Output '      run_selfloader.ps1 -- it kills QQ, injects, fills, cleans up and restarts QQ'
Write-Output ('        powershell -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $inj 'run_selfloader.ps1'))
Write-Output '      then:'
Write-Output ('        powershell -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $PSScriptRoot 'refill_finish.ps1'))
