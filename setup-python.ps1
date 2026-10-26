<#
.SYNOPSIS
  一键安装 Python（给 dsh-tide-memory 用）。

.DESCRIPTION
  自动做这些事，全程无需人工干预：
    1. 检查是否已经装了可用的 Python（有就直接退出，什么都不改）
    2. 从 python.org 下载官方安装包（3.12 x64）
    3. **仅当前用户安装**（InstallAllUsers=0），装到默认位置，不碰系统目录
    4. 带上 PrependPath=1，装完命令行能直接 python
    5. 校验：装完再跑一次 python --version，成功才报成功

  不做什么：
    · 不要管理员权限（HKCU 注册表足够）
    · 不改系统环境变量（只改当前用户 PATH）
    · 不装 pip 之外的额外包（引擎只用标准库）
    · 不删任何东西

.EXAMPLE
  .\setup-python.ps1                 # 静默安装（默认）
  .\setup-python.ps1 -Interactive    # 显示安装界面
  .\setup-python.ps1 -CheckOnly      # 只检测，不安装
#>
[CmdletBinding()]
param(
  # 显示安装程序界面（默认静默）
  [switch]$Interactive,
  # 只检测状态，不下载不安装
  [switch]$CheckOnly,
  # 指定 Python 版本（默认 3.12.8）
  [string]$Version = '3.12.8'
)

$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------------- #
# 检测：复用与插件相同的查找顺序
# --------------------------------------------------------------------------- #
function Find-Python {
  # 1. 显式指定
  if ($env:DSH_PYTHON -and (Test-Path -LiteralPath $env:DSH_PYTHON)) {
    return @{ Path = $env:DSH_PYTHON; Source = 'DSH_PYTHON 环境变量' }
  }

  # 2. DSH 自带运行时
  $dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }
  $candidates = @(
    (Join-Path $dshHome 'dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'),
    (Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe')
  )
  foreach ($c in $candidates) {
    if (Test-Path -LiteralPath $c) {
      return @{ Path = $c; Source = '本机已安装' }
    }
  }

  # 3. PATH
  $onPath = Get-Command python -ErrorAction SilentlyContinue
  if ($onPath -and $onPath.Source) {
    return @{ Path = $onPath.Source; Source = 'PATH' }
  }

  return $null
}

function Test-PythonOK {
  param([string]$Exe)
  try {
    $out = & $Exe --version 2>&1
    if ($out -match 'Python 3\.(\d+)') {
      $minor = [int]$Matches[1]
      return @{ OK = ($minor -ge 10); Version = $out.Trim(); Minor = $minor }
    }
  } catch {
    # 忽略，下面返回失败
  }
  return @{ OK = $false; Version = ''; Minor = 0 }
}

# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
Write-Host ''
Write-Host '  dsh-tide-memory · Python 环境安装' -ForegroundColor Cyan
Write-Host '  ─────────────────────────────────' -ForegroundColor DarkGray
Write-Host ''

$found = Find-Python
if ($found) {
  $check = Test-PythonOK -Exe $found.Path
  if ($check.OK) {
    Write-Host "  ✅ 已找到可用的 Python" -ForegroundColor Green
    Write-Host "     版本：$($check.Version)" -ForegroundColor DarkGray
    Write-Host "     位置：$($found.Path)" -ForegroundColor DarkGray
    Write-Host "     来源：$($found.Source)" -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '  不需要安装，记忆引擎可以直接用。' -ForegroundColor Green
    Write-Host ''
    exit 0
  }
  Write-Host "  ⚠️  找到 Python 但版本太老（需要 3.10+）" -ForegroundColor Yellow
  Write-Host "     $($found.Path) → $($check.Version)" -ForegroundColor DarkGray
  Write-Host ''
}

if ($CheckOnly) {
  Write-Host '  ❌ 没有找到可用的 Python（3.10+）' -ForegroundColor Red
  Write-Host ''
  exit 1
}

# --------------------------------------------------------------------------- #
# 下载安装
# --------------------------------------------------------------------------- #
$arch = if ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { 'win32' }
$file = "python-$Version-$arch.exe"
$url = "https://www.python.org/ftp/python/$Version/$file"
$tmp = Join-Path $env:TEMP $file

Write-Host "  没有找到可用的 Python，开始安装 $Version ($arch)" -ForegroundColor Yellow
Write-Host "  下载地址：$url" -ForegroundColor DarkGray
Write-Host ''

if (Test-Path -LiteralPath $tmp) {
  Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}

Write-Host '  [1/3] 下载中...' -ForegroundColor Cyan
try {
  $ProgressPreference = 'SilentlyContinue'
  Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing -TimeoutSec 600
} catch {
  Write-Host "  ❌ 下载失败：$($_.Exception.Message)" -ForegroundColor Red
  Write-Host ''
  Write-Host '  可能是网络问题。你可以手动下载安装：' -ForegroundColor Yellow
  Write-Host "    $url" -ForegroundColor White
  Write-Host '  安装时勾选「Add python.exe to PATH」即可。' -ForegroundColor DarkGray
  Write-Host ''
  exit 1
}
$sizeMB = [math]::Round((Get-Item -LiteralPath $tmp).Length / 1MB, 1)
Write-Host "       完成（$sizeMB MB）" -ForegroundColor Green

# 校验是官方签名 —— 这是硬门：验不过就不装
Write-Host '  [2/3] 校验安装包签名...' -ForegroundColor Cyan
$sigOK = $false
try {
  $sig = Get-AuthenticodeSignature -LiteralPath $tmp
  if ($sig.Status -ne 'Valid') {
    Write-Host "  ❌ 签名无效（状态：$($sig.Status)），拒绝安装" -ForegroundColor Red
    Write-Host '     下载的安装包可能被篡改，或来源不是 python.org。' -ForegroundColor Yellow
    Write-Host "     文件保留在：$tmp" -ForegroundColor DarkGray
    Write-Host '     请手动从 https://www.python.org/downloads/ 下载安装。' -ForegroundColor Yellow
    Write-Host ''
    exit 1
  }
  $subject = $sig.SignerCertificate.Subject
  if ($subject -notmatch 'Python Software Foundation') {
    Write-Host "  ❌ 签发者不是 Python Software Foundation：$subject" -ForegroundColor Red
    Write-Host '     拒绝安装。请手动从 https://www.python.org/downloads/ 下载。' -ForegroundColor Yellow
    Write-Host ''
    exit 1
  }
  Write-Host '       ✅ 签名有效（Python Software Foundation）' -ForegroundColor Green
  $sigOK = $true
} catch {
  Write-Host "  ❌ 无法校验签名：$($_.Exception.Message)" -ForegroundColor Red
  Write-Host '     为了安全默认不安装。' -ForegroundColor Yellow
  Write-Host ''
  Write-Host '     如果你确认网络可信、想跳过校验，可以手动下载安装：' -ForegroundColor DarkGray
  Write-Host "       $url" -ForegroundColor White
  Write-Host '     安装时勾选「Add python.exe to PATH」。' -ForegroundColor DarkGray
  Write-Host ''
  Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
  exit 1
}

if (-not $sigOK) {
  Write-Host '  ❌ 签名未通过，中止安装' -ForegroundColor Red
  Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
  exit 1
}

Write-Host '  [3/3] 安装中（仅当前用户，无需管理员权限）...' -ForegroundColor Cyan
$installArgs = @(
  '/quiet'
  'InstallAllUsers=0'          # 仅当前用户，不碰系统目录
  'PrependPath=1'              # 加进当前用户 PATH
  'Include_launcher=1'
  'Include_test=0'             # 不装测试套件，省空间
  'Include_doc=0'              # 不装文档
  'AssociateFiles=0'
  'Shortcuts=0'                # 不建开始菜单项
)
if ($Interactive) {
  $installArgs = @('/passive', 'InstallAllUsers=0', 'PrependPath=1', 'Include_test=0')
}

$proc = Start-Process -FilePath $tmp -ArgumentList $installArgs -Wait -PassThru
if ($proc.ExitCode -ne 0) {
  Write-Host "  ❌ 安装程序返回错误码 $($proc.ExitCode)" -ForegroundColor Red
  Write-Host '     可以手动运行安装包看看具体提示：' -ForegroundColor Yellow
  Write-Host "       $tmp" -ForegroundColor White
  Write-Host ''
  exit 1
}
Write-Host '       安装完成' -ForegroundColor Green
Write-Host ''

# --------------------------------------------------------------------------- #
# 校验安装结果
# --------------------------------------------------------------------------- #
Start-Sleep -Seconds 2
$after = Find-Python
if ($after) {
  $check2 = Test-PythonOK -Exe $after.Path
  if ($check2.OK) {
    Write-Host "  ✅ 安装成功" -ForegroundColor Green
    Write-Host "     版本：$($check2.Version)" -ForegroundColor DarkGray
    Write-Host "     位置：$($after.Path)" -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '  现在回到 DSH 点一下「重新检测」，记忆引擎就能用了。' -ForegroundColor Cyan
    Write-Host ''
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    exit 0
  }
}

# 刚装完 PATH 可能还没刷新，用已知默认位置兜底查一次
$defaultPath = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$($Version.Split('.')[0..1] -join '')\python.exe"
if (Test-Path -LiteralPath $defaultPath) {
  $check3 = Test-PythonOK -Exe $defaultPath
  if ($check3.OK) {
    Write-Host "  ✅ 安装成功（$($check3.Version)）" -ForegroundColor Green
    Write-Host "     位置：$defaultPath" -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '  现在回到 DSH 点一下「重新检测」，记忆引擎就能用了。' -ForegroundColor Cyan
    Write-Host ''
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    exit 0
  }
}

Write-Host '  ⚠️  安装程序跑完了，但没检测到 Python' -ForegroundColor Yellow
Write-Host '     通常是 PATH 还没刷新。请关掉 DSH 重开一次，或在新的终端窗口里试 python --version。' -ForegroundColor DarkGray
Write-Host ''
exit 1
