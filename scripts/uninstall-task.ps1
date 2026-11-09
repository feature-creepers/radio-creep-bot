# Arrête et supprime la tâche planifiée du bot.
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall-task.ps1
$TaskName = 'RadioCreepBot'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) { Write-Host "Aucune tâche '$TaskName'."; return }
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Tâche '$TaskName' supprimée."
