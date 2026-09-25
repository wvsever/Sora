#Requires -Version 7
<#
.SYNOPSIS
    End-to-end SORA-DS pipeline: generate a CSV CPPBank book, run Vera (bcal_cli) to derive IFRS 9 / IRB
    risk parameters, convert them to Sora's sim_risk_parameter format, and optionally map + run Sora and
    package a tests/data-style archive.

.DESCRIPTION
    Steps (each timed and logged to <OutRoot>/pipeline.log, skippable with -SkipGenerate / -SkipVera):

      1. generate   python -m cppbankrawaccgen --format csv ...           -> <OutRoot>/book
      2. vera       bcal_cli.exe --book-dir <book> --consolidated ...     -> <OutRoot>/vera/risk_parameters.csv
      3. convert    sora-tools vera-params <risk_parameters.csv>          -> <OutRoot>/params/sim_risk_parameter.csv
      4. validate   sanity-checks the conversion report (rows_out > 0, PAR-010 pre-check counts)
      5. map/run    (-RunSora)  sora-tools map ...  then  sora run ... --parameters ...
      6. package    (-Package)  tests/data-style 7z archive of the book, excluding wire/ (and, by default,
                    the GL tables: journal_line, journal_entry, gl_balance - tools/extract_testdata.py's
                    convention), plus a manifest of what went in.

    A pipeline_manifest.json is always written: generator/Vera/Sora git SHAs, every command line run, seed,
    scale, timings and row/file counts - so "what produced this dataset" is answerable without re-deriving it.

    Every native command's exit code is checked; the script stops on the first failure ($ErrorActionPreference
    = 'Stop'). No step is ever backgrounded: this is meant to be run in the foreground and waited on.

.PARAMETER GeneratorRepo
    Path to the cppbankrawaccgen checkout (a worktree, never the shared checkout).

.PARAMETER VeraRepo
    Path to the baselcalculator checkout. Supplies --param-dir and --schema-sql, and (unless -VeraCli is
    given) the default bcal_cli.exe location under it.

.PARAMETER VeraCli
    Explicit path to bcal_cli.exe, overriding the default under -VeraRepo. -VeraRepo is still required (for
    --param-dir/--schema-sql) unless -SkipVera is given.

.PARAMETER SoraRepo
    Path to the Sora checkout. Defaults to two levels up from this script (tools/..), i.e. the repo this
    script ships in.

.PARAMETER OutRoot
    Output root. Created if missing. Holds book/, vera/, params/, sim/, run/, pipeline.log,
    pipeline_manifest.json and (with -Package) the archive.

.PARAMETER HistoryMonths
    Forwarded to the generator as --history-months only when given (a new generator option this wave; the
    generator's own default is used when this is omitted - never assume what that default is).

.PARAMETER SkipGenerate
    Skip step 1. <OutRoot>/book must already exist and be a valid CSV book.

.PARAMETER SkipVera
    Skip step 2. <OutRoot>/vera/risk_parameters.csv must already exist.

.PARAMETER Package
    Run step 6: build <OutRoot>/dataset_<ReportingDate>.7z in tests/data/20260630.7z's own layout.

.PARAMETER IncludeGlTables
    With -Package, also include the GL tables (journal_line, journal_entry, gl_balance - about 70% of the
    volume). Excluded by default, matching tools/extract_testdata.py's own default.

.PARAMETER RunSora
    Run step 5: sora-tools map, then sora run --parameters <converted file>. Requires -Scenario and -SoraExe.

.PARAMETER Scenario
    Scenario YAML for -RunSora (e.g. tests/scenarios/test_eba2025.yaml, relative to -SoraRepo or absolute).

.PARAMETER SoraExe
    Path to the built sora C++ engine binary, required by -RunSora (this script never builds it - "lanes
    write code, the integrator builds").

.PARAMETER Threads
    Forwarded as --threads to sora-tools map (the only step in this pipeline with a documented thread-count
    flag; bcal_cli's own concurrency is whatever its own default is - NOT VERIFIED here, out of this repo).

.PARAMETER CpuAffinityPercent
    Caps every spawned process to this percentage of logical processors (e.g. 25 for office hours), by
    setting Process.ProcessorAffinity. Omit to run unrestricted.

.PARAMETER DryRun
    Print every command that would run (with its working directory) and do nothing else. The smoke test
    for this script (tools/Test-NewSoraDataset.Smoke.ps1) uses this.

.EXAMPLE
    pwsh tools/New-SoraDataset.ps1 `
      -GeneratorRepo F:/Temp/sora_ds/gwt_STG -VeraRepo F:/Temp/sora_ds/wt_EXP -SoraRepo F:/Temp/sora_ds/swt_PIPE `
      -OutRoot F:/CPPBank/sora/20260630 -Seed 27 -Scale 1.0 -ReportingDate 2026-06-30 `
      -Package -CpuAffinityPercent 25

.EXAMPLE
    # Dry run: print the commands without executing them.
    pwsh tools/New-SoraDataset.ps1 -GeneratorRepo C:\gen -VeraRepo C:\vera -OutRoot C:\out -DryRun

.EXAMPLE
    # Convert an already-generated book/Vera output, then map and run Sora against a scenario.
    pwsh tools/New-SoraDataset.ps1 -GeneratorRepo C:\gen -VeraRepo C:\vera -OutRoot C:\out `
      -SkipGenerate -SkipVera -RunSora -Scenario tests/scenarios/test_eba2025.yaml `
      -SoraExe C:\out\build\release\sora.exe
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $GeneratorRepo,
    [string] $VeraRepo,
    [string] $VeraCli,
    [string] $SoraRepo = (Split-Path -Parent $PSScriptRoot),
    [Parameter(Mandatory)] [string] $OutRoot,

    [int] $Seed = 27,
    [double] $Scale = 1.0,
    [string] $ReportingDate = '2026-06-30',
    [string] $Country = 'BE',
    [string] $Label,
    [Nullable[int]] $HistoryMonths,

    [switch] $SkipGenerate,
    [switch] $SkipVera,
    [switch] $Package,
    [switch] $IncludeGlTables,
    [switch] $RunSora,
    [string] $Scenario,
    [string] $SoraExe,

    [Nullable[int]] $Threads,
    [Nullable[int]] $CpuAffinityPercent,

    [string] $PythonExe = 'C:/Program Files/Python312/python',
    [string] $SoraToolsExe = 'sora-tools',
    [string] $SevenZipExe = '7z',

    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ---------------------------------------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------------------------------------

if (-not (Test-Path $OutRoot)) { New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null }
$OutRoot = (Resolve-Path $OutRoot).Path

$BookDir = Join-Path $OutRoot 'book'
$VeraOutDir = Join-Path $OutRoot 'vera'
$ParamsDir = Join-Path $OutRoot 'params'
$SimDir = Join-Path $OutRoot 'sim'
$RunOutDir = Join-Path $OutRoot 'run'
$LogPath = Join-Path $OutRoot 'pipeline.log'
$ManifestPath = Join-Path $OutRoot 'pipeline_manifest.json'

if (-not $Label) { $Label = "SORA-PIPE-$($ReportingDate)-seed$Seed" }

$script:StepResults = [System.Collections.Generic.List[object]]::new()
$script:PipelineStartedAt = Get-Date

function Write-Log {
    param([string] $Message)
    $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $Message
    Write-Host $line
    Add-Content -LiteralPath $LogPath -Value $line
}

function Get-GitSha {
    param([string] $RepoPath)
    if (-not $RepoPath -or -not (Test-Path $RepoPath)) { return $null }
    try {
        $sha = (& git -C $RepoPath rev-parse HEAD 2>$null)
        if ($LASTEXITCODE -eq 0 -and $sha) { return $sha.Trim() }
    } catch { }
    return $null
}

function Get-AffinityMask {
    param([int] $Percent)
    $total = [Environment]::ProcessorCount
    $count = [Math]::Max(1, [Math]::Floor($total * $Percent / 100.0))
    [int64] $mask = 0
    for ($i = 0; $i -lt $count; $i++) { $mask = $mask -bor ([int64]1 -shl $i) }
    return [IntPtr]$mask
}

function Resolve-ExePath {
    # A literal path without an extension (e.g. the generator's own "python", no ".exe") is not resolved by
    # CreateProcess the way a bare command name typed at a prompt is - only PATH lookup does that. Try the
    # literal path first, then the same path with ".exe", and only then give up and let the native call fail
    # with Windows' own "not found" error.
    param([string] $Path)
    if (Test-Path -LiteralPath $Path -PathType Leaf) { return $Path }
    if (Test-Path -LiteralPath "$Path.exe" -PathType Leaf) { return "$Path.exe" }
    return $Path
}

function Get-LineCount {
    # Streams the file (never loads it whole) - the reference book runs to millions of rows in some tables.
    param([string] $Path)
    if (-not (Test-Path $Path)) { return $null }
    $count = 0
    $reader = [System.IO.File]::OpenText($Path)
    try { while ($null -ne $reader.ReadLine()) { $count++ } } finally { $reader.Close() }
    return $count
}

# Builds an argument list with an explicit generic List<string> and .Add() calls, never a bare
# `if (...) { @(...) } else { $null }` expression fed to foreach - a single-element array there unrolls to
# a scalar and a foreach over $null silently runs zero times (reference_powershell_null_array_unroll_skips_loops).
function New-ArgList {
    return [System.Collections.Generic.List[string]]::new()
}

function Invoke-Step {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [string] $FilePath,
        [Parameter(Mandatory)] [System.Collections.Generic.List[string]] $ArgumentList,
        [Parameter(Mandatory)] [string] $WorkingDirectory
    )
    $argArray = $ArgumentList.ToArray()
    $cmdText = "$FilePath " + ($argArray -join ' ')
    Write-Log "STEP $Name : $cmdText  (cwd: $WorkingDirectory)"

    if ($DryRun) {
        Write-Log "  [DRYRUN] not executed"
        $result = [pscustomobject]@{ Name = $Name; Command = $cmdText; WorkingDirectory = $WorkingDirectory; ExitCode = 0; ElapsedSeconds = 0.0 }
        $script:StepResults.Add($result)
        return $result
    }

    $stepLog = Join-Path $OutRoot ("step_{0}.log" -f ($Name -replace '[^A-Za-z0-9_-]', '_'))
    $sw = [System.Diagnostics.Stopwatch]::StartNew()

    if ($CpuAffinityPercent) {
        $proc = Start-Process -FilePath $FilePath -ArgumentList $argArray -WorkingDirectory $WorkingDirectory `
            -NoNewWindow -PassThru `
            -RedirectStandardOutput "$stepLog.out" -RedirectStandardError "$stepLog.err"
        try {
            $proc.ProcessorAffinity = Get-AffinityMask -Percent $CpuAffinityPercent
        } catch {
            Write-Log "  WARN: could not set CPU affinity ($CpuAffinityPercent%): $_"
        }
        $proc.WaitForExit()
        $exitCode = $proc.ExitCode
        Get-Content "$stepLog.out" -ErrorAction SilentlyContinue | Add-Content -LiteralPath $stepLog
        Get-Content "$stepLog.err" -ErrorAction SilentlyContinue | Add-Content -LiteralPath $stepLog
    } else {
        Push-Location $WorkingDirectory
        try {
            & $FilePath @argArray 2>&1 | Tee-Object -FilePath $stepLog
            $exitCode = $LASTEXITCODE
        } finally {
            Pop-Location
        }
    }

    $sw.Stop()
    $elapsed = [Math]::Round($sw.Elapsed.TotalSeconds, 1)
    Write-Log "  exit=$exitCode elapsed=${elapsed}s log=$stepLog"

    $result = [pscustomobject]@{ Name = $Name; Command = $cmdText; WorkingDirectory = $WorkingDirectory; ExitCode = $exitCode; ElapsedSeconds = $elapsed; Log = $stepLog }
    $script:StepResults.Add($result)

    if ($exitCode -ne 0) {
        throw "Step '$Name' failed with exit code $exitCode. Command: $cmdText  (see $stepLog)"
    }
    return $result
}

function Write-Manifest {
    param([hashtable] $Extra)
    $manifest = [ordered]@{
        label = $Label
        reportingDate = $ReportingDate
        country = $Country
        seed = $Seed
        scale = $Scale
        historyMonths = $HistoryMonths
        startedAt = $script:PipelineStartedAt.ToString('o')
        finishedAt = (Get-Date).ToString('o')
        totalElapsedSeconds = [Math]::Round(((Get-Date) - $script:PipelineStartedAt).TotalSeconds, 1)
        cpuAffinityPercent = $CpuAffinityPercent
        dryRun = [bool]$DryRun
        repos = [ordered]@{
            generator = [ordered]@{ path = $GeneratorRepo; sha = (Get-GitSha $GeneratorRepo) }
            vera = [ordered]@{ path = $VeraRepo; sha = (Get-GitSha $VeraRepo) }
            sora = [ordered]@{ path = $SoraRepo; sha = (Get-GitSha $SoraRepo) }
        }
        steps = @($script:StepResults)
    }
    foreach ($key in $Extra.Keys) { $manifest[$key] = $Extra[$key] }
    ($manifest | ConvertTo-Json -Depth 10) | Set-Content -LiteralPath $ManifestPath
    Write-Log "Manifest written: $ManifestPath"
}

# ---------------------------------------------------------------------------------------------------------
# Validate inputs up front (fail closed, never a silent skip). The logical checks (is a required parameter
# combination even present) apply in -DryRun too, since a dry run that omits a required parameter should
# say so; only the filesystem existence checks are skipped, since a dry run's whole point is to work without
# the referenced trees necessarily existing yet.
# ---------------------------------------------------------------------------------------------------------

if (-not $SkipVera -and -not $VeraRepo) { throw "VeraRepo is required unless -SkipVera is given (it supplies --param-dir and --schema-sql, even when -VeraCli overrides the binary path)" }
if ($RunSora -and -not $Scenario) { throw "-RunSora requires -Scenario" }
if ($RunSora -and -not $SoraExe) { throw "-RunSora requires -SoraExe (this script never builds it - lanes write code, the integrator builds)" }

if (-not $DryRun) {
    if (-not (Test-Path $GeneratorRepo)) { throw "GeneratorRepo not found: $GeneratorRepo" }
    if ($VeraRepo -and -not (Test-Path $VeraRepo)) { throw "VeraRepo not found: $VeraRepo" }
    if (-not (Test-Path $SoraRepo)) { throw "SoraRepo not found: $SoraRepo" }
    if ($RunSora -and -not (Test-Path $SoraExe)) { throw "SoraExe not found: $SoraExe" }
    $PythonExe = Resolve-ExePath -Path $PythonExe
}

Write-Log "=== New-SoraDataset : label=$Label reportingDate=$ReportingDate seed=$Seed scale=$Scale outRoot=$OutRoot dryRun=$([bool]$DryRun) ==="

# ---------------------------------------------------------------------------------------------------------
# Step 1: generate the CSV book
# ---------------------------------------------------------------------------------------------------------

if (-not $SkipGenerate) {
    $genArgs = New-ArgList
    $genArgs.Add('-m'); $genArgs.Add('cppbankrawaccgen')
    $genArgs.Add('--everything')
    $genArgs.Add('--business'); $genArgs.Add('all')
    $genArgs.Add('--reporting-date'); $genArgs.Add($ReportingDate)
    $genArgs.Add('--country'); $genArgs.Add($Country)
    $genArgs.Add('--seed'); $genArgs.Add([string]$Seed)
    $genArgs.Add('--scale'); $genArgs.Add(('{0:0.0###}' -f $Scale))   # invariant "1.0", not double.ToString()'s bare "1"
    $genArgs.Add('--label'); $genArgs.Add($Label)
    $genArgs.Add('--with-group-entities')
    $genArgs.Add('--with-group-portfolios')
    $genArgs.Add('--check-identities')
    $genArgs.Add('--validate-output')
    $genArgs.Add('--overwrite-out')
    $genArgs.Add('--format'); $genArgs.Add('csv')
    $genArgs.Add('--out'); $genArgs.Add($BookDir)
    if ($null -ne $HistoryMonths) { $genArgs.Add('--history-months'); $genArgs.Add([string]$HistoryMonths) }

    Invoke-Step -Name 'generate' -FilePath $PythonExe -ArgumentList $genArgs -WorkingDirectory $GeneratorRepo | Out-Null
} else {
    Write-Log "SKIP generate (-SkipGenerate) - expecting an existing book at $BookDir"
    if (-not $DryRun -and -not (Test-Path $BookDir)) { throw "-SkipGenerate given but $BookDir does not exist" }
}

$bookFileCount = $null
if (-not $DryRun -and (Test-Path $BookDir)) {
    try { $bookFileCount = (Get-ChildItem -Path $BookDir -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count } catch { }
}

# ---------------------------------------------------------------------------------------------------------
# Step 2: run Vera (bcal_cli, DB-free)
# ---------------------------------------------------------------------------------------------------------

$riskParamsCsv = Join-Path $VeraOutDir 'risk_parameters.csv'

if (-not $SkipVera) {
    $veraCliPath = if ($VeraCli) { $VeraCli } else { Join-Path $VeraRepo 'engine/build/msvc-x64-release/app/bcal_cli/bcal_cli.exe' }
    if (-not $DryRun -and -not (Test-Path $veraCliPath)) { throw "bcal_cli.exe not found: $veraCliPath (pass -VeraCli to override)" }

    $veraArgs = New-ArgList
    $veraArgs.Add('--param-set'); $veraArgs.Add('EU_CRR3_2025-01-01')
    $veraArgs.Add('--param-dir'); $veraArgs.Add((Join-Path $VeraRepo 'reference_data/parameter_sets'))
    $veraArgs.Add('--schema-sql'); $veraArgs.Add((Join-Path $VeraRepo 'sql/schema/40_staging.sql'))
    $veraArgs.Add('--book-dir'); $veraArgs.Add($BookDir)
    $veraArgs.Add('--reporting-date'); $veraArgs.Add($ReportingDate)
    $veraArgs.Add('--out-dir'); $veraArgs.Add($VeraOutDir)
    $veraArgs.Add('--consolidated')

    Invoke-Step -Name 'vera' -FilePath $veraCliPath -ArgumentList $veraArgs -WorkingDirectory $VeraRepo | Out-Null
} else {
    Write-Log "SKIP vera (-SkipVera) - expecting an existing $riskParamsCsv"
    if (-not $DryRun -and -not (Test-Path $riskParamsCsv)) { throw "-SkipVera given but $riskParamsCsv does not exist" }
}

$veraRowCount = if (-not $DryRun) { Get-LineCount -Path $riskParamsCsv } else { $null }
if ($null -ne $veraRowCount) { $veraRowCount = $veraRowCount - 1 }  # header line

# ---------------------------------------------------------------------------------------------------------
# Step 3: convert (sora-tools vera-params)
# ---------------------------------------------------------------------------------------------------------

$simRiskParamCsv = Join-Path $ParamsDir 'sim_risk_parameter.csv'
$extrasCsv = Join-Path $ParamsDir 'vera_extras.csv'
$convertReport = Join-Path $ParamsDir 'vera_params_report.json'

$convertArgs = New-ArgList
$convertArgs.Add('vera-params')
$convertArgs.Add($riskParamsCsv)
$convertArgs.Add('-o'); $convertArgs.Add($simRiskParamCsv)
$convertArgs.Add('--extras'); $convertArgs.Add($extrasCsv)
$convertArgs.Add('--report'); $convertArgs.Add($convertReport)

Invoke-Step -Name 'convert' -FilePath $SoraToolsExe -ArgumentList $convertArgs -WorkingDirectory $SoraRepo | Out-Null

# ---------------------------------------------------------------------------------------------------------
# Step 4: validate the conversion (fail closed - a converter that silently produced nothing is a defect)
# ---------------------------------------------------------------------------------------------------------

$convertStats = $null
if (-not $DryRun) {
    if (-not (Test-Path $convertReport)) { throw "convert step did not write a report: $convertReport" }
    $convertStats = Get-Content -LiteralPath $convertReport -Raw | ConvertFrom-Json
    Write-Log ("VALIDATE convert: rows_in={0} rows_out={1} rows_dropped_all_empty={2} rows_dropped_no_contract_id={3}" -f `
        $convertStats.rows_in, $convertStats.rows_out, $convertStats.rows_dropped_all_empty, $convertStats.rows_dropped_no_contract_id)
    if ($convertStats.rows_out -le 0) {
        throw "convert step produced 0 rows of sim_risk_parameter - check $riskParamsCsv and $convertReport"
    }
    $rangeTotal = 0
    if ($convertStats.range_violations) {
        foreach ($prop in $convertStats.range_violations.PSObject.Properties) { $rangeTotal += [int]$prop.Value }
    }
    if ($rangeTotal -gt 0) {
        Write-Log "VALIDATE convert: $rangeTotal PAR-010 pre-check value(s) dropped to empty (never clamped) - see $convertReport"
    }
} else {
    Write-Log "STEP validate : [DRYRUN] would read $convertReport and require rows_out > 0"
}

# ---------------------------------------------------------------------------------------------------------
# Step 5 (optional): map + run Sora
# ---------------------------------------------------------------------------------------------------------

$soraRunSummary = $null
if ($RunSora) {
    $mapArgs = New-ArgList
    $mapArgs.Add('map'); $mapArgs.Add('mappings/cppbank')
    $mapArgs.Add('--export'); $mapArgs.Add($BookDir)
    $mapArgs.Add('-o'); $mapArgs.Add($SimDir)
    $mapArgs.Add('--validate')
    if ($null -ne $Threads) { $mapArgs.Add('--threads'); $mapArgs.Add([string]$Threads) }
    Invoke-Step -Name 'map' -FilePath $SoraToolsExe -ArgumentList $mapArgs -WorkingDirectory $SoraRepo | Out-Null

    $runArgs = New-ArgList
    $runArgs.Add('run'); $runArgs.Add($SimDir)
    $runArgs.Add('--scenario'); $runArgs.Add($Scenario)
    $runArgs.Add('-o'); $runArgs.Add($RunOutDir)
    $runArgs.Add('--parameters'); $runArgs.Add($simRiskParamCsv)
    Invoke-Step -Name 'run' -FilePath $SoraExe -ArgumentList $runArgs -WorkingDirectory $SoraRepo | Out-Null

    $soraRunSummary = [ordered]@{ simDir = $SimDir; runOutDir = $RunOutDir; scenario = $Scenario; parameters = $simRiskParamCsv }
} else {
    Write-Log "SKIP map/run (-RunSora not given)"
}

# ---------------------------------------------------------------------------------------------------------
# Step 6 (optional): package a tests/data-style archive
# ---------------------------------------------------------------------------------------------------------

$packageSummary = $null
if ($Package) {
    $stagingDir = Join-Path $OutRoot 'package_staging'
    $archivePath = Join-Path $OutRoot "dataset_$ReportingDate.7z"

    if ($DryRun) {
        $robocopyPreview = "robocopy `"$BookDir`" `"$stagingDir`" /E /XD wire" + $(if (-not $IncludeGlTables) { ' journal_line journal_entry gl_balance' } else { '' })
        Write-Log "STEP package : [DRYRUN] $robocopyPreview ; then $SevenZipExe a `"$archivePath`" (from $stagingDir)"
    } else {
        if (Test-Path $stagingDir) { Remove-Item -LiteralPath $stagingDir -Recurse -Force }
        New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null

        $excludeDirs = New-ArgList
        $excludeDirs.Add('wire')
        if (-not $IncludeGlTables) { $excludeDirs.Add('journal_line'); $excludeDirs.Add('journal_entry'); $excludeDirs.Add('gl_balance') }

        # robocopy's own exit codes 0-7 are all success (bit flags for "files copied" etc.); >= 8 is a real
        # failure. It does not participate in $LASTEXITCODE the way a normal console app does, so it is
        # checked on its own terms here rather than through Invoke-Step's uniform exit-code contract.
        $robocopyArgs = @($BookDir, $stagingDir, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/XD') + $excludeDirs.ToArray()
        Write-Log "STEP package-copy : robocopy $($robocopyArgs -join ' ')"
        & robocopy @robocopyArgs | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy failed while staging the package (exit $LASTEXITCODE)" }

        if (Test-Path $archivePath) { Remove-Item -LiteralPath $archivePath -Force }
        Write-Log "STEP package-archive : $SevenZipExe a `"$archivePath`" * (cwd: $stagingDir)"
        Push-Location $stagingDir
        try {
            & $SevenZipExe a $archivePath * | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "7z failed while building $archivePath (exit $LASTEXITCODE)" }
        } finally {
            Pop-Location
        }

        $archiveSize = (Get-Item -LiteralPath $archivePath).Length
        $stagedFileCount = (Get-ChildItem -Path $stagingDir -Recurse -File | Measure-Object).Count
        $packageSummary = [ordered]@{
            path = $archivePath
            sizeBytes = $archiveSize
            stagedFileCount = $stagedFileCount
            excludedGlTables = -not [bool]$IncludeGlTables
            excludedDirs = @($excludeDirs.ToArray())
        }
        Write-Log ("Package: $archivePath ({0:N1} MB, $stagedFileCount files, GL tables excluded={1})" -f ($archiveSize / 1MB), (-not $IncludeGlTables))
        Remove-Item -LiteralPath $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Log "SKIP package (-Package not given)"
}

# ---------------------------------------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------------------------------------

Write-Manifest -Extra @{
    rowCounts = [ordered]@{
        bookFiles = $bookFileCount
        veraRiskParameterRows = $veraRowCount
        convert = $convertStats
    }
    runSora = $soraRunSummary
    package = $packageSummary
}

Write-Log "=== New-SoraDataset : done ==="
