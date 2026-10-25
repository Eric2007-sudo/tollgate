<#
  install.ps1 — 把 tollgate 装到 agent 的技能目录

  用法：
    .\install.ps1                      # 装到 OpenCode 全局（默认）
    .\install.ps1 -Target agents       # 装到 ~/.agents/skills/
    .\install.ps1 -Target project -ProjectPath "D:\proj"
    .\install.ps1 -Target opencode -Force
#>

[CmdletBinding()]
param(
    [ValidateSet("opencode", "agents", "project")]
    [string]$Target = "opencode",

    [string]$ProjectPath = ".",

    [switch]$Force
)

$ErrorActionPreference = "Stop"

# 让子进程（python）的 UTF-8 输出在 cp936 控制台里也能正确显示
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$src = $PSScriptRoot

function Resolve-Dest {
    param($Target, $ProjectPath)
    switch ($Target) {
        "opencode" { return Join-Path $env:USERPROFILE ".config\opencode\skills\tollgate" }
        "agents"   { return Join-Path $env:USERPROFILE ".agents\skills\tollgate" }
        "project"  {
            $p = (Resolve-Path $ProjectPath).Path
            return Join-Path $p ".opencode\skills\tollgate"
        }
    }
}

$dest = Resolve-Dest -Target $Target -ProjectPath $ProjectPath

Write-Host ""
Write-Host "tollgate 安装" -ForegroundColor Cyan
Write-Host "  源:   $src"
Write-Host "  目标: $dest"
Write-Host ""

if ((Test-Path $dest) -and ($src -ne $dest)) {
    if (-not $Force) {
        Write-Host "目标已存在。加 -Force 覆盖。" -ForegroundColor Yellow
        exit 1
    }
    Remove-Item -Recurse -Force $dest
}

if ($src -ne $dest) {
    New-Item -ItemType Directory -Path $dest -Force | Out-Null

    # 只复制运行所需：SKILL.md + tg.py + orch.py + ui/ + references/
    foreach ($f in @("SKILL.md", "tg.py", "orch.py")) {
        $s = Join-Path $src $f
        if (Test-Path $s) { Copy-Item $s -Destination $dest -Force }
    }
    foreach ($sub in @("references", "ui")) {
        $s = Join-Path $src $sub
        if (Test-Path $s) { Copy-Item $s -Destination $dest -Recurse -Force }
    }
}

# 清掉可能被带过去的字节码
foreach ($d in @(Get-ChildItem -Path $dest -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue)) {
    Remove-Item -Recurse -Force $d.FullName
}

Write-Host "已复制。" -ForegroundColor Green

# ---------------------------------------------------------------- 自检
$tg = Join-Path $dest "tg.py"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
}

if ($python) {
    Write-Host ""
    Write-Host "自检：$($python.Source) $tg price" -ForegroundColor Cyan
    # 先把输出收全再截断：直接 |> Select-Object -First 8 会提前掐断管道，
    # 让 python 被终止、$LASTEXITCODE 变成非 0，自检误报失败。
    $lines = & $python.Source $tg price 2>&1
    $code = $LASTEXITCODE
    $lines | Select-Object -First 8
    if ($code -ne 0) {
        Write-Host "CLI 自检失败（exit=$code）。" -ForegroundColor Red
        exit 1
    }
    Write-Host ""
    Write-Host "可用。调用前缀：" -ForegroundColor Green
    Write-Host "  python `"$tg`""
    Write-Host "  python `"$tg`" serve --port 7717    # 本地工作台"
    Write-Host "  python `"$(Join-Path $dest 'orch.py')`"" -ForegroundColor Green
    Write-Host "  python `"$(Join-Path $dest 'orch.py')`" fan 2    # 多角色编排（不调模型）"
} else {
    Write-Host "未找到 python。技能文件已就位，但 CLI 需要 Python 3.8+。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "下一步：新开一个 agent 会话，说「用 tollgate 规划这个任务」。" -ForegroundColor Cyan
Write-Host ""
