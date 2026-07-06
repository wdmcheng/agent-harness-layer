param(
  [Parameter(Mandatory = $true, Position = 0)]
  [string] $HookName,

  [Parameter(Position = 1)]
  [string] $AgentName = $env:AGENT_PACK_AGENT
)

$ErrorActionPreference = "Stop"

$projectDir = $env:AGENT_PACK_PROJECT_DIR
if (-not $projectDir) { $projectDir = $env:CLAUDE_PROJECT_DIR }
if (-not $projectDir) { $projectDir = $env:CODEX_PROJECT_DIR }
if (-not $projectDir) {
  try {
    $projectDir = (& git rev-parse --show-toplevel 2>$null).Trim()
  } catch {
    $projectDir = ""
  }
}
if (-not $projectDir) { $projectDir = (Get-Location).Path }

$runner = Join-Path $projectDir ".agents/hooks/agent_pack_hook.py"
if (-not (Test-Path -LiteralPath $runner)) {
  Write-Error "Error: Agent Pack hook runner not found: $runner"
  exit 1
}

$candidates = @(
  @{ Exe = "python"; Args = @() },
  @{ Exe = "py"; Args = @("-3") },
  @{ Exe = "python3"; Args = @() }
)

foreach ($candidate in $candidates) {
  $exe = Get-Command $candidate.Exe -ErrorAction SilentlyContinue
  if ($exe) {
    & $candidate.Exe @($candidate.Args) $runner $HookName $AgentName
    $code = $LASTEXITCODE
    if ($code -ne 9009) { exit $code }
  }
}

Write-Error "Error: Python 3 is required to run Agent Pack hooks."
exit 1
