# Refill finish-up: rebuild index -> import filled copy -> verify coverage -> purge copy.
#
# Run AFTER run_selfloader.ps1:
#   powershell -ExecutionPolicy Bypass -File tools\refill_finish.ps1
#   powershell -ExecutionPolicy Bypass -File tools\refill_finish.ps1 -Window 90d -Days 90
#
# Fixed steps:
#   1. rebuild the index with on-demand decryption (messages fetched by the loader were
#      written into nt_msg.db by QQ itself, so the index must be rebuilt to see them)
#   2. import filled-messages.jsonl (covers any messages QQ chose not to flush to nt_msg.db)
#   3. verify coverage (acceptance metric A: "backend gap length" must be 0 for 100%)
#   4. purge the fetched-message copy once verified as a subset of the index
#
# ASCII ONLY: PowerShell 5.1 reads .ps1 as GBK and non-ASCII breaks the parser.

param(
  [string]$Window = '30d',
  [int]$Days = 11
)

$ErrorActionPreference = 'Continue'
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = 'python' }
$app = Split-Path -Parent $PSScriptRoot
$inj = Join-Path $app 'injector'
if (-not (Test-Path $inj)) {
  $inj = Join-Path (Split-Path -Parent $app) 'injector'
}

Write-Output ('[1/4] rebuilding index (window {0}, on-demand decryption) ...' -f $Window)
Push-Location $app
$t0 = Get-Date
& $py -X utf8 'scripts\ntqq_build.py' --window $Window --source stream 2>&1 | Select-Object -Last 12
$buildExit = $LASTEXITCODE
Pop-Location
Write-Output ('      done, exit={0}, elapsed {1:N1}s' -f $buildExit, ((Get-Date) - $t0).TotalSeconds)

Write-Output ''
Write-Output '[2/4] merging fetched-message copy (filled-messages.jsonl) into index.db ...'
& $py -X utf8 (Join-Path $PSScriptRoot 'import_filled.py')

Write-Output ''
Write-Output ('[3/4] coverage acceptance (last {0} days, four metrics) ...' -f $Days)
& $py -X utf8 (Join-Path $PSScriptRoot 'coverage_check.py') --days $Days --top 25
$covExit = $LASTEXITCODE

Write-Output ''
Write-Output '[4/4] purging the fetched-message copy (verified subset of index) ...'
& $py -X utf8 (Join-Path $PSScriptRoot 'purge_filled.py')
$purgeExit = $LASTEXITCODE
if ($purgeExit -ne 0) {
  Write-Output '      !! purge refused - the copy holds data the index does not have.'
  Write-Output ('         run: "{0}" -X utf8 "{1}"   then run this script again' -f $py, (Join-Path $PSScriptRoot 'import_filled.py'))
}

Write-Output ''
if ($covExit -eq 0) {
  Write-Output 'PASS - ready to generate reports:'
  Write-Output ('  powershell -ExecutionPolicy Bypass -File "{0}" -Since "<window start>"' -f (Join-Path $PSScriptRoot 'make_reports.ps1'))
} else {
  Write-Output 'FAIL - iron rule 0 says refill again:'
  Write-Output ('  "{0}" -X utf8 "{1}" --days {2} --top-groups 75 --min-gap 50 --merge-gap 200' -f $py, (Join-Path $PSScriptRoot 'gap_plan.py'), $Days)
  Write-Output ('  powershell -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $inj 'run_selfloader.ps1'))
  Write-Output '  then run this script once more'
}
