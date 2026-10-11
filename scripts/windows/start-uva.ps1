<#
.SYNOPSIS
    Waits for Docker Desktop to be ready and starts the UVA stack.

.DESCRIPTION
    Meant to run at Windows logon (see register-autostart.ps1). Polls
    `docker info` until the engine answers, then runs `docker compose up -d`
    in the repository directory. Output goes to %LOCALAPPDATA%\uva\start-uva.log.
#>
param(
    [string]$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [int]$TimeoutSeconds = 600,
    [int]$PollSeconds = 5
)

$ErrorActionPreference = "Stop"

$logDir = Join-Path $env:LOCALAPPDATA "uva"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "start-uva.log"

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $logFile -Value $line -Encoding utf8
    Write-Output $line
}

Write-Log "Aguardando o Docker ficar pronto (timeout ${TimeoutSeconds}s)..."
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ($true) {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) { break }
    if ((Get-Date) -gt $deadline) {
        Write-Log "ERRO: Docker nao respondeu dentro do timeout. Abortando."
        exit 1
    }
    Start-Sleep -Seconds $PollSeconds
}

Write-Log "Docker pronto. Subindo a UVA em $RepoDir"
Push-Location $RepoDir
try {
    docker compose up -d *>> $logFile
    if ($LASTEXITCODE -ne 0) {
        Write-Log "ERRO: docker compose up falhou (codigo $LASTEXITCODE)."
        exit $LASTEXITCODE
    }
    Write-Log "UVA iniciada."
}
finally {
    Pop-Location
}
