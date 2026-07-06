$ErrorActionPreference = "Stop"
$hookName = [IO.Path]::GetFileNameWithoutExtension($MyInvocation.MyCommand.Name)
$projectDir = $env:AGENT_PACK_PROJECT_DIR
if (-not $projectDir) { $projectDir = $env:CLAUDE_PROJECT_DIR }
if (-not $projectDir) { $projectDir = (& git rev-parse --show-toplevel 2>$null).Trim() }
& (Join-Path $projectDir ".agents/hooks/run-hook.ps1") $hookName "claude"
exit $LASTEXITCODE
