# Installe le bot comme tâche planifiée : lancé à l'ouverture de TA session Windows,
# relancé automatiquement s'il plante. Pas besoin de droits administrateur.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1
#
# Le bot doit tourner dans ta session (pas en service Windows) : il a besoin du bureau
# pour lire Apple Music (SMTC) et piloter son interface (UI Automation).

$ErrorActionPreference = 'Stop'
$TaskName = 'RadioCreepBot'
$Root = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'

if (-not (Test-Path $Pythonw)) { throw "Introuvable : $Pythonw. Crée d'abord le venv (voir README, étape 3)." }
if (-not (Test-Path (Join-Path $Root '.env'))) { throw "Fichier .env manquant dans $Root (voir README, étape 4)." }

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument '-m bot' -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = 'PT45S'  # laisse le temps à Voicemeeter, Chrome et Apple Music de démarrer
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Bot Discord jukebox de la radio Feature Creepers' -Force | Out-Null

Write-Host "Tâche '$TaskName' installée. Elle démarrera à ta prochaine ouverture de session."
Write-Host "Pour la lancer tout de suite :  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Logs : $Root\logs\bot.log"
