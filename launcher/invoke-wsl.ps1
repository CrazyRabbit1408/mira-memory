param(
    [string]$Mode = "start-detached"
)

$ErrorActionPreference = "Stop"

# The project is intentionally kept inside the WSL filesystem.
# Passing arguments as PowerShell values avoids cmd.exe quote parsing.
$command = "exec bash ~/mira-memory-v1/launcher/run-mira.sh $Mode"
& wsl.exe --exec bash -lc $command
$result = $LASTEXITCODE
exit $result