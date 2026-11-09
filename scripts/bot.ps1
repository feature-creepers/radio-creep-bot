# Raccourcis de gestion du bot.   Usage :  .\scripts\bot.ps1 <commande>
#
#   status     état de la tâche et du processus, dernières lignes du log
#   start      démarre le bot (tâche planifiée)
#   stop       arrête le bot
#   restart    redémarre (après une modification du .env ou du code)
#   logs       affiche les 40 dernières lignes du log
#   follow     suit le log en direct (Ctrl+C pour quitter)
#   check      diagnostic Discord : token, serveur, salons, rôle, permissions (n'envoie rien)
#   selfcheck  diagnostic local : Apple Music, bibliothèque, playlists (ne modifie rien)
#   console    lance le bot dans cette console (arrête d'abord la tâche ; Ctrl+C pour quitter)
#   install    installe le lancement automatique à l'ouverture de session
#   uninstall  supprime le lancement automatique

param([Parameter(Position = 0)][string]$Command = 'status')

$ErrorActionPreference = 'Stop'
$TaskName = 'RadioCreepBot'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Log = Join-Path $Root 'logs\bot.log'
Set-Location $Root

# Le python du venv est un lanceur qui démarre le vrai Python : 2 processus pour 1 bot.
# -All renvoie les deux (pour tout arrêter) ; sans -All, seulement le vrai bot (pour compter).
function Get-BotProcess([switch]$All) {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -match '-m bot(\s|$)' -and ($All -or $_.ExecutablePath -notlike '*\.venv\*') }
}

function Stop-Bot {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-BotProcess -All | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Show-Status {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) { Write-Host 'Tâche planifiée : non installée (.\scripts\bot.ps1 install)' -ForegroundColor Yellow }
    else {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "Tâche planifiée : $($task.State)  (dernier lancement : $($info.LastRunTime))"
    }
    $procs = @(Get-BotProcess)
    if ($procs.Count -eq 0) { Write-Host 'Processus du bot : aucun' -ForegroundColor Yellow }
    elseif ($procs.Count -gt 1) { Write-Host "Processus du bot : $($procs.Count) copies ! (annonces en double) → .\scripts\bot.ps1 restart" -ForegroundColor Red }
    else { Write-Host "Processus du bot : en cours (PID $($procs[0].ProcessId), depuis $($procs[0].CreationDate))" -ForegroundColor Green }
    if (Test-Path $Log) { Write-Host "`n--- derniers logs ---"; Get-Content $Log -Tail 10 -Encoding UTF8 }
}

switch ($Command.ToLower()) {
    'status' { Show-Status }
    'start' { Start-ScheduledTask -TaskName $TaskName; Start-Sleep 3; Show-Status }
    'stop' { Stop-Bot; Write-Host 'Bot arrêté.' }
    'restart' {
        Stop-Bot
        Start-Sleep 2
        Start-ScheduledTask -TaskName $TaskName; Start-Sleep 5; Show-Status
    }
    'logs' { Get-Content $Log -Tail 40 -Encoding UTF8 }
    'follow' { Get-Content $Log -Tail 20 -Wait -Encoding UTF8 }
    'check' { & $Python -m bot.discordcheck }
    'selfcheck' { & $Python -m bot.selfcheck }
    'console' { Stop-Bot; & $Python -m bot }
    'install' { & (Join-Path $PSScriptRoot 'install-task.ps1') }
    'uninstall' { & (Join-Path $PSScriptRoot 'uninstall-task.ps1') }
    default { Get-Content $PSCommandPath -TotalCount 14 -Encoding UTF8 }
}
