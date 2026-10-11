# Generate one per-group report for every group with activity in the window.
#
# Why per-group instead of one big report: a single multi-group report has to cap
# --max-msgs (default 150, silently truncated), while per-group runs have no realistic
# cap, so nothing is dropped. Pair with report_parity.py to prove nothing was dropped.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\make_reports.ps1
#   powershell -ExecutionPolicy Bypass -File tools\make_reports.ps1 -Since '2026-10-01 00:00' -OutDir D:\QQChatCache\output\oct2026
#
# ASCII ONLY: PowerShell 5.1 reads .ps1 as GBK and non-ASCII breaks the parser.

param(
  [string]$Since   = '2026-10-01 00:00',
  [string]$OutDir  = 'D:\QQChatCache\output\oct2026-final',
  [int]$MinMsgs    = 1,
  [int]$MaxChars   = 8000000
)

$ErrorActionPreference = 'Continue'
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = 'python' }
$app = Split-Path -Parent $PSScriptRoot
$scriptsDir = Join-Path $app 'scripts'

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$codeList = @'
import sqlite3, datetime, sys, os
sys.path.insert(0, sys.argv[3])
from ntqq_core import INDEX_DB
since = int(datetime.datetime.strptime(sys.argv[1], "%Y-%m-%d %H:%M").timestamp())
con = sqlite3.connect("file:%s?mode=ro" % INDEX_DB.replace("\\", "/"), uri=True)
rows = con.execute("select group_code from messages where ts>=? group by group_code having count(*)>=? order by count(*) desc",
                   (since, int(sys.argv[2]))).fetchall()
print(",".join(str(r[0]) for r in rows))
'@ | & $py -X utf8 - $Since $MinMsgs $scriptsDir

$codes = $codeList.Trim().Split(',')
if (-not $codes -or $codes[0] -eq '') { Write-Output 'no groups in window - abort'; exit 1 }
Write-Output ('generating {0} per-group reports into {1} ...' -f $codes.Count, $OutDir)

Push-Location $app
$t0 = Get-Date
$i = 0
foreach ($c in $codes) {
  $i++
  & $py -X utf8 'scripts\ntqq_report.py' --group $c --since $Since --max-msgs 100000 --max-chars $MaxChars `
      --out (Join-Path $OutDir ($c + '.md')) *> $null
}
Pop-Location

$files = Get-ChildItem $OutDir -File
Write-Output ('done: {0} reports, {1:N1} MB, elapsed {2:N1}s' -f `
  $files.Count, (($files | Measure-Object Length -Sum).Sum / 1MB), ((Get-Date) - $t0).TotalSeconds)
Write-Output ''
Write-Output 'now prove nothing was dropped:'
Write-Output ('  {0} -X utf8 "{1}" --dir "{2}" --since "{3}"' -f $py, (Join-Path $PSScriptRoot 'report_parity.py'), $OutDir, $Since)
