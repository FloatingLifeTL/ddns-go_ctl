<#
.SYNOPSIS
为发布产物生成或校验 SHA256SUMS.txt 校验清单。

.DESCRIPTION
清单为 GNU coreutils 兼容文本，可直接被 sha256sum --check 使用：
- 每行一个产物：<64 位小写十六进制><两个空格><文件名>
- 只记录文件名，不带目录前缀，因此校验需在清单所在目录执行
- 换行固定 LF，文件不含 BOM，末尾保留一个换行

三种模式互斥：
- 默认（生成）：为目标产物写入 SHA256SUMS.txt，已有清单被覆盖
- -Verify：按清单重算并逐行比对，任何不一致都以非零退出码结束
- -CheckFormat：只检查清单自身格式，不读取产物内容

清单记录的是文件当前名称，因此重命名到最终发布名必须在生成之前完成。

.PARAMETER Path
生成模式：产物目录或单个产物文件路径。
校验与格式模式：SHA256SUMS.txt 清单文件路径，或其所在目录。

.PARAMETER Verify
按清单校验产物完整性。

.PARAMETER CheckFormat
只校验清单格式。

.EXAMPLE
将 v<version> 替换为实际版本目录名：

./scripts/checksums.ps1 _Release-Assets-Backups/v<version>_
./scripts/checksums.ps1 -Verify _Release-Assets-Backups/v<version>_
./scripts/checksums.ps1 -CheckFormat _Release-Assets-Backups/v<version>_

.NOTES
支持平台：Windows。
PowerShell 运行版本基线：
- 最低版本：Windows PowerShell 5.1。
- 已验证版本：Windows 上的 PowerShell 7.6.5（pwsh）。
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true, Position = 0)]
  [string]$Path,
  [switch]$Verify,
  [switch]$CheckFormat
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$manifestName = 'SHA256SUMS.txt'
$linePattern = '^([0-9a-f]{64})  (\S[^\r]*)$'

if ($Verify -and $CheckFormat) {
  throw 'Choose at most one mode: -Verify or -CheckFormat.'
}

# 目录输入统一补上清单文件名，单文件输入按清单自身处理。
function Resolve-ManifestPath {
  param([string]$InputPath, [switch]$Required)

  if (Test-Path -LiteralPath $InputPath -PathType Container) {
    $candidate = Join-Path $InputPath $manifestName
  } else {
    $candidate = $InputPath
  }

  if ($Required -and -not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
    throw "Manifest not found: $candidate"
  }

  return $candidate
}

# 读取并校验清单格式，返回字节与条目行；格式问题直接抛出。
function Read-Manifest {
  param([string]$ManifestPath)

  $fullPath = (Resolve-Path -LiteralPath $ManifestPath).Path
  $bytes = [System.IO.File]::ReadAllBytes($fullPath)

  if ($bytes.Length -lt 3) {
    throw "Manifest is empty: $fullPath"
  }
  if ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    throw "Manifest must not contain a UTF-8 BOM: $fullPath"
  }
  if ($bytes -contains 13) {
    throw "Manifest must use LF line endings, CR found: $fullPath"
  }
  if ($bytes[$bytes.Length - 1] -ne 10) {
    throw "Manifest must end with a newline: $fullPath"
  }

  $text = [System.Text.Encoding]::UTF8.GetString($bytes)
  $lines = @($text -split "`n")
  # 末尾换行会切出一个空元素，属于正常结构，去掉后再检查。
  if ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq '') {
    $lines = @($lines[0..($lines.Count - 2)])
  }
  if ($lines.Count -eq 0) {
    throw "Manifest has no entries: $fullPath"
  }

  $index = 0
  foreach ($line in $lines) {
    $index++
    $match = [regex]::Match($line, $linePattern)
    if (-not $match.Success) {
      throw "Manifest line $index is not coreutils format: $line"
    }
    $name = $match.Groups[2].Value
    if ($name -eq $manifestName) {
      throw "Manifest line $index lists the manifest itself."
    }
    if ($name -match '[\\/]') {
      throw "Manifest line $index must use a bare filename without a path: $line"
    }
  }

  return @{ Path = $fullPath; Lines = $lines }
}

# 生成模式：目录输入取其中除既有清单外的全部文件作为产物。
function New-Manifest {
  param([string]$TargetPath)

  if (-not (Test-Path -LiteralPath $TargetPath)) {
    throw "Path not found: $TargetPath"
  }
  if ((Split-Path -Leaf $TargetPath) -eq $manifestName) {
    throw "Pass the asset directory or asset file, not the manifest itself."
  }

  if (Test-Path -LiteralPath $TargetPath -PathType Container) {
    $assets = @(Get-ChildItem -LiteralPath $TargetPath -File -Force |
      Where-Object { $_.Name -ne $manifestName } | Sort-Object Name)
    if ($assets.Count -eq 0) {
      throw "No assets found under: $TargetPath"
    }
  } else {
    $assets = @(Get-Item -LiteralPath $TargetPath)
  }

  $entries = @($assets | ForEach-Object {
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    '{0}  {1}' -f $hash.ToLower(), $_.Name
  })

  $manifestPath = Join-Path $assets[0].DirectoryName $manifestName
  [System.IO.File]::WriteAllText(
    $manifestPath,
    (($entries -join "`n") + "`n"),
    (New-Object System.Text.UTF8Encoding($false)))

  # 生成后立即自检格式，避免写出 sha256sum 读不了的清单。
  $null = Read-Manifest -ManifestPath $manifestPath
  Write-Output "Wrote $manifestName ($($entries.Count) entries)"
  $entries | ForEach-Object { Write-Output $_ }
}

# 校验模式：以清单所在目录为基准逐行重算比对。
function Test-Manifest {
  param([string]$ManifestPath)

  $manifest = Read-Manifest -ManifestPath $ManifestPath
  $baseDir = Split-Path -Parent $manifest.Path
  $failures = @()

  foreach ($line in $manifest.Lines) {
    $match = [regex]::Match($line, $linePattern)
    $expected = $match.Groups[1].Value
    $name = $match.Groups[2].Value
    $assetPath = Join-Path $baseDir $name

    if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf)) {
      $failures += "$name : MISSING"
      continue
    }

    $actual = (Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLower()
    if ($actual -eq $expected) {
      Write-Output "$name : OK"
    } else {
      $failures += "$name : MISMATCH expected $expected actual $actual"
    }
  }

  if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Output $_ }
    throw "Checksum verification failed ($($failures.Count) of $($manifest.Lines.Count))."
  }

  Write-Output "Checksum verification: OK ($($manifest.Lines.Count) entries)"
}

if ($CheckFormat) {
  $manifest = Read-Manifest -ManifestPath (Resolve-ManifestPath -InputPath $Path -Required)
  Write-Output "Manifest format: OK ($($manifest.Lines.Count) entries)"
} elseif ($Verify) {
  Test-Manifest -ManifestPath (Resolve-ManifestPath -InputPath $Path -Required)
} else {
  # 生成模式接收目录或产物文件本身，不能先补成清单路径，否则会把旧清单当成产物。
  New-Manifest -TargetPath $Path
}
