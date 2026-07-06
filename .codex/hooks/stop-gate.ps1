$ErrorActionPreference = "Stop"
$hookName = [IO.Path]::GetFileNameWithoutExtension($MyInvocation.MyCommand.Name)
$projectDir = $env:AGENT_PACK_PROJECT_DIR
if (-not $projectDir) { $projectDir = $env:CODEX_PROJECT_DIR }
if (-not $projectDir) {
  try { $projectDir = (& git rev-parse --show-toplevel 2>$null).Trim() } catch { $projectDir = "" }
}
if (-not $projectDir) { $projectDir = (Get-Location).Path }
& (Join-Path $projectDir ".agents/hooks/run-hook.ps1") $hookName "codex"
exit $LASTEXITCODE
