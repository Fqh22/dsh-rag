# dsh-rag 一键接入脚本（Windows）
#
# 做三件事，全部幂等、改动前备份：
#   1. 把 MCP server 写进 DSH profile 的 cordis.patch.yml（必须包在 - insert: 里）
#   2. 把面板插件挂进 profile 的 package.json（dependencies + dsh.profile.bundles）
#   3. 用 DSH 自带的 pnpm 安装，生成 node_modules 链接
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Profile web
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -DryRun
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -SkipPanel   # 只要 MCP
#
# 回滚：脚本会把改动前的文件存成 *.bak-<时间戳>；也可以用 -Uninstall 撤掉写入的行。

param(
    [string]$Profile = "desktop",
    [string]$ProfilesRoot = (Join-Path $env:USERPROFILE ".dsh\profiles"),
    [switch]$SkipPanel,
    [switch]$DryRun,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$profileDir = Join-Path $ProfilesRoot $Profile
$patch = Join-Path $profileDir 'cordis.patch.yml'
$pkgPath = Join-Path $profileDir 'package.json'

function Write-Step($msg) { Write-Host "· $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "! $msg" -ForegroundColor Yellow }

function Backup($path) {
    if (-not (Test-Path $path)) { return }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $bak = "$path.bak-$stamp"
    Copy-Item $path $bak -Force
    Write-Step "已备份 $([IO.Path]::GetFileName($path)) → $([IO.Path]::GetFileName($bak))"
}

function WriteUtf8NoBom($path, $text) {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $text, $enc)
}

function Show-Plan() {
    Write-Host ""
    Write-Host "dsh-rag 接入计划（$Profile profile）" -ForegroundColor Green
    Write-Host "  仓库：  $root"
    Write-Host "  profile：$profileDir"
    Write-Host "  MCP：   $(if ($Uninstall) { '移除' } else { '写入 cordis.patch.yml' })"
    Write-Host "  插件：  $(if ($Uninstall -or $SkipPanel) { '跳过' } else { '写入 package.json 并 pnpm install' })"
    Write-Host ""
}

Show-Plan
if ($DryRun) { Write-Warn "DryRun：只显示计划，没有改动任何文件"; return }

if (-not (Test-Path $profileDir)) {
    throw "找不到 profile 目录：$profileDir`n先用 DSH 打开一次（会自动创建 profile），或确认 -Profile 参数。"
}

# ---------------------------------------------------------------- MCP 配置
$insertBlock = @"

# dsh-rag：本地项目记忆库（MCP stdio）。由 scripts/install.ps1 写入；重复执行不会重复添加。
# 回滚：scripts/install.ps1 -Uninstall，或用同目录 *.bak-* 覆盖。
- insert:
    - id: dsh-rag
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: rag
        transport: stdio
        command: '$($root)\.venv\Scripts\python.exe'
        args: ['$($root)\src\server.py']
        cwd: '$($root)\src'
        env:
          PYTHONIOENCODING: utf-8
          PYTHONUTF8: '1'
        toolCallTimeoutMs: 120000
        failOnStartupError: false
"@

if (Test-Path $patch) {
    $yaml = [System.IO.File]::ReadAllText($patch)
} else {
    $yaml = "# dsh profile patch layer`n[]`n"
}

$hasMcp = $yaml -match '(?m)^\s*-?\s*id:\s*dsh-rag\s*$'

if ($Uninstall) {
    if (-not $hasMcp) {
        Write-Step "cordis.patch.yml 里没有 dsh-rag，跳过"
    } else {
        Backup $patch
        # 只删我们自己写的那一段：从注释行到 failOnStartupError 行
        $patterns = @(
            '(?ms)\r?\n# dsh-rag：.*?failOnStartupError:\s*false\s*\r?\n?',
            '(?ms)\r?\n# dsh-rag：本地项目记忆库.*?(?=\r?\n-|\r?\n\[|\z)'
        )
        $new = $yaml
        foreach ($p in $patterns) { $new = [regex]::Replace($new, $p, "`n") }
        if ($new -eq $yaml) {
            Write-Warn "没能精确定位到写入段落，请手动删除 cordis.patch.yml 里的 dsh-rag 段"
        } else {
            WriteUtf8NoBom $patch $new
            Write-Step "已从 cordis.patch.yml 移除 dsh-rag"
        }
    }
} elseif ($hasMcp) {
    Write-Step "cordis.patch.yml 里已有 dsh-rag，跳过（要更新路径请先 -Uninstall）"
} else {
    Backup $patch
    $new = $yaml.TrimEnd() + "`n" + $insertBlock + "`n"
    WriteUtf8NoBom $patch $new
    Write-Step "已写入 MCP 配置（- insert: 包裹，避免被当成覆盖行静默忽略）"
}

# ---------------------------------------------------------------- 面板插件
if ($Uninstall) {
    if (Test-Path $pkgPath) {
        try {
            $pkg = Get-Content -Raw -Encoding utf8 $pkgPath | ConvertFrom-Json
            if ($pkg.dependencies.PSObject.Properties.Name -contains 'dsh-rag-panel') {
                Backup $pkgPath
                $pkg.dependencies.PSObject.Properties.Remove('dsh-rag-panel')
                if ($pkg.dsh.profile.bundles) {
                    $pkg.dsh.profile.bundles = @($pkg.dsh.profile.bundles | Where-Object { $_ -ne 'dsh-rag-panel' })
                }
                $json = $pkg | ConvertTo-Json -Depth 12
                WriteUtf8NoBom $pkgPath ($json + "`n")
                Write-Step "已从 profile package.json 移除 dsh-rag-panel"
            }
        } catch {
            Write-Warn "package.json 处理失败：$($_.Exception.Message)"
        }
    }
    Write-Host ""
    Write-Host "卸载完成。重启 DSH 生效。" -ForegroundColor Green
    return
}

if (-not $SkipPanel) {
    if (-not (Test-Path $pkgPath)) {
        Write-Warn "profile 里没有 package.json，跳过插件"
    } else {
        $pkg = Get-Content -Raw -Encoding utf8 $pkgPath | ConvertFrom-Json
        $changed = $false

        if (-not $pkg.dependencies) {
            $pkg | Add-Member -NotePropertyName dependencies -NotePropertyValue ([pscustomobject]@{}) -Force
        }
        $linkSpec = "link:$($root)\plugin"
        if ($pkg.dependencies.PSObject.Properties.Name -notcontains 'dsh-rag-panel') {
            $changed = $true
        }
        $pkg.dependencies | Add-Member -NotePropertyName 'dsh-rag-panel' -NotePropertyValue $linkSpec -Force

        if (-not $pkg.dsh) {
            $pkg | Add-Member -NotePropertyName dsh -NotePropertyValue ([pscustomobject]@{ profile = [pscustomobject]@{ bundles = @() } }) -Force
        }
        if (-not $pkg.dsh.profile) {
            $pkg.dsh | Add-Member -NotePropertyName profile -NotePropertyValue ([pscustomobject]@{ bundles = @() }) -Force
        }
        $bundles = @($pkg.dsh.profile.bundles)
        if ($bundles -notcontains 'dsh-rag-panel') {
            $pkg.dsh.profile.bundles = @($bundles + 'dsh-rag-panel')
            $changed = $true
        }

        if ($changed) {
            Backup $pkgPath
            $json = $pkg | ConvertTo-Json -Depth 12
            WriteUtf8NoBom $pkgPath ($json + "`n")
            Write-Step "已把 dsh-rag-panel 写入 profile package.json"
        } else {
            Write-Step "profile 里已配置 dsh-rag-panel，跳过"
        }

        # 用 DSH 自带的 pnpm 生成链接
        $pnpm = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\pnpm\bin\pnpm.mjs'
        $node = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe'
        if ((Test-Path $pnpm) -and (Test-Path $node)) {
            Write-Step "安装依赖（pnpm install，可能需要几十秒）…"
            Push-Location $profileDir
            try {
                & $node $pnpm install --silent
                if ($LASTEXITCODE -ne 0) { Write-Warn "pnpm install 返回 $LASTEXITCODE，插件可能没链上" }
                else { Write-Step "依赖已安装" }
            } catch {
                Write-Warn "pnpm install 失败：$($_.Exception.Message)"
            } finally {
                Pop-Location
            }
        } else {
            Write-Warn "没找到 DSH 自带的 pnpm/node，请手动在 $profileDir 执行 pnpm install"
        }
    }
}

# ---------------------------------------------------------------- 自检
Write-Host ""
Write-Step "自检"
$py = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path $py) {
    & $py (Join-Path $root 'src\cli.py') state 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Step "Python 侧可运行（cli.py state 正常）" }
    else { Write-Warn "cli.py state 返回 $LASTEXITCODE，检查 .venv 是否装好依赖" }
} else {
    Write-Warn "没找到 .venv，先跑：python -m venv .venv 并 pip install -r requirements.txt"
}

Write-Host ""
Write-Host "完成。重启 DSH 后生效。" -ForegroundColor Green
Write-Host "验证：工具表里出现 mcp__rag__*；界面上出现右下角记忆面板按钮。" -ForegroundColor Green
