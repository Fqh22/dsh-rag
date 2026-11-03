# 安装/卸载 DSH-RAG 归档守护的登录自启项（不创建计划任务）
# 用法:
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Uninstall

param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$root    = Split-Path -Parent $PSScriptRoot
$src     = Join-Path $PSScriptRoot 'dsh-rag-archive.vbs'
$startup = [Environment]::GetFolderPath('Startup')
$dst     = Join-Path $startup 'DSH-RAG-Archive.vbs'

if ($Uninstall) {
    if (Test-Path $dst) { Remove-Item $dst -Force; Write-Host "已移除: $dst" }
    else { Write-Host "未安装，无需移除。" }
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like '*dsh-rag*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "已停止进程 $($_.ProcessId)" }
    return
}

if (-not (Test-Path $src)) { throw "找不到 $src" }
$vbs = Get-Content $src -Raw
$vbs = $vbs.Replace('__PROJECT_DIR__', $root)
Set-Content -Path $dst -Value $vbs -Encoding ASCII
Write-Host "已安装登录自启: $dst"

# 立即启动一次，不用等下次登录
$py = Join-Path $root '.venv\Scripts\pythonw.exe'
$script = Join-Path $root 'scripts\watch_archive.py'
Start-Process -FilePath $py -ArgumentList $script -WorkingDirectory $root -WindowStyle Hidden
Write-Host "归档守护已在后台启动。日志: $(Join-Path $root 'logs\archive.log')"
Write-Host "卸载: powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"
