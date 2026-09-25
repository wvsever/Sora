#Requires -Version 7
<#
.SYNOPSIS
    Pester-free smoke test for New-SoraDataset.ps1: runs it with -DryRun and asserts the printed commands
    and the manifest look right, without touching the generator, Vera, or Sora themselves.

    Per the SORA-DS lane contract, lanes write code and tests only - the integrator runs this (and the real
    pipeline). It is written here, not executed by the authoring lane.

.EXAMPLE
    pwsh tools/Test-NewSoraDataset.Smoke.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$failures = [System.Collections.Generic.List[string]]::new()

function Assert {
    param([bool] $Condition, [string] $Message)
    if (-not $Condition) { $failures.Add($Message) }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'New-SoraDataset.ps1'
$outRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("sora_dryrun_{0}" -f ([guid]::NewGuid().ToString('N').Substring(0, 8)))

# --- Scenario 1: default dry run (generate + vera + convert, no map/run/package) --------------------------

& $script -GeneratorRepo 'C:/fake/gen' -VeraRepo 'C:/fake/vera' -SoraRepo $repoRoot -OutRoot $outRoot `
    -Seed 27 -Scale 1.0 -ReportingDate '2026-06-30' -DryRun *>&1 | Tee-Object -Variable dryRunOutput | Out-Null
$text = $dryRunOutput -join "`n"

Assert ($text -match '--format csv') "generate command should request --format csv"
Assert ($text -match '--everything') "generate command should carry --everything"
Assert ($text -match '--seed 27') "generate command should carry the given seed"
Assert ($text -match '--scale 1(\.0)?') "generate command should carry the given scale"
Assert ($text -notmatch '--history-months') "no -HistoryMonths given -> --history-months must not appear"
Assert ($text -match '--param-set EU_CRR3_2025-01-01') "vera command should carry the fixed param set"
Assert ($text -match '--consolidated') "vera command should carry --consolidated"
Assert ($text -match 'vera-params') "convert command should invoke sora-tools vera-params"
Assert ($text -match '\[DRYRUN\] not executed') "dry run must not execute any native step"
Assert ($text -notmatch 'STEP map ') "map must not run without -RunSora"
Assert ($text -notmatch 'STEP run ') "sora run must not run without -RunSora"

$manifestPath = Join-Path $outRoot 'pipeline_manifest.json'
Assert (Test-Path $manifestPath) "dry run must still write pipeline_manifest.json"
if (Test-Path $manifestPath) {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    Assert ($manifest.dryRun -eq $true) "manifest.dryRun should be true"
    Assert ($null -ne $manifest.steps -and $manifest.steps.Count -ge 3) "manifest should record at least the generate/vera/convert steps"
    Assert ($manifest.seed -eq 27) "manifest should record the seed"
}

# --- Scenario 2: -HistoryMonths is forwarded only when given -----------------------------------------------

$outRoot2 = "$outRoot`_hist"
& $script -GeneratorRepo 'C:/fake/gen' -VeraRepo 'C:/fake/vera' -SoraRepo $repoRoot -OutRoot $outRoot2 `
    -HistoryMonths 18 -DryRun *>&1 | Tee-Object -Variable histOutput | Out-Null
$histText = $histOutput -join "`n"
Assert ($histText -match '--history-months 18') "-HistoryMonths 18 must appear as --history-months 18"

# --- Scenario 3: -RunSora requires -Scenario and -SoraExe (fails closed, not a silent skip) ----------------

$outRoot3 = "$outRoot`_runsora_missing_args"
$threw = $false
try {
    & $script -GeneratorRepo 'C:/fake/gen' -VeraRepo 'C:/fake/vera' -SoraRepo $repoRoot -OutRoot $outRoot3 `
        -RunSora -DryRun 2>$null | Out-Null
} catch {
    $threw = $true
}
Assert $threw "-RunSora without -Scenario/-SoraExe must throw, never silently proceed"

# --- Scenario 4: -RunSora with both given prints map + run --------------------------------------------------

$outRoot4 = "$outRoot`_runsora_ok"
& $script -GeneratorRepo 'C:/fake/gen' -VeraRepo 'C:/fake/vera' -SoraRepo $repoRoot -OutRoot $outRoot4 `
    -RunSora -Scenario 'tests/scenarios/test_eba2025.yaml' -SoraExe 'C:/fake/sora.exe' -Threads 4 -DryRun *>&1 |
    Tee-Object -Variable runSoraOutput | Out-Null
$runSoraText = $runSoraOutput -join "`n"
Assert ($runSoraText -match 'STEP map ') "map: step should print with -RunSora"
Assert ($runSoraText -match '--threads 4') "-Threads should be forwarded to sora-tools map"
Assert ($runSoraText -match 'STEP run ') "run step should print with -RunSora"
Assert ($runSoraText -match '--parameters') "run command should pass --parameters"

# --- Scenario 5: -SkipVera without -VeraRepo is accepted; -SkipVera is honoured -----------------------------

$outRoot5 = "$outRoot`_skipvera"
& $script -GeneratorRepo 'C:/fake/gen' -SoraRepo $repoRoot -OutRoot $outRoot5 -SkipVera -SkipGenerate -DryRun *>&1 |
    Tee-Object -Variable skipVeraOutput | Out-Null
$skipVeraText = $skipVeraOutput -join "`n"
Assert ($skipVeraText -match 'SKIP vera') "SKIP vera line should print with -SkipVera"
Assert ($skipVeraText -match 'SKIP generate') "SKIP generate line should print with -SkipGenerate"

# --- Cleanup -------------------------------------------------------------------------------------------

foreach ($d in @($outRoot, $outRoot2, $outRoot3, $outRoot4, $outRoot5)) {
    Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction SilentlyContinue
}

# --- Report ----------------------------------------------------------------------------------------------

if ($failures.Count -gt 0) {
    Write-Host "FAILED ($($failures.Count)):" -ForegroundColor Red
    foreach ($f in $failures) { Write-Host "  - $f" -ForegroundColor Red }
    exit 1
}
Write-Host "PASSED: all New-SoraDataset.ps1 -DryRun smoke assertions held." -ForegroundColor Green
exit 0
