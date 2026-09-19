[CmdletBinding()]
param(
    [string]$SourceRoot = '',
    [string]$TargetRoot = 'D:\AlgorithmWeb',
    [switch]$Preview,
    [switch]$IncludeData,
    [switch]$SkipHash,
    [switch]$RemoveBackupOnSuccess
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-NormalizedPath {
    param([Parameter(Mandatory)][string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($fullPath)
    if ($fullPath.TrimEnd('\', '/') -eq $root.TrimEnd('\', '/')) {
        return $root
    }
    return $fullPath.TrimEnd('\', '/')
}

function Assert-SafeDirectory {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Label
    )

    $fullPath = Get-NormalizedPath $Path
    $root = [System.IO.Path]::GetPathRoot($fullPath).TrimEnd('\', '/')
    if ([string]::IsNullOrWhiteSpace($fullPath) -or $fullPath -eq $root) {
        throw "$Label cannot be a drive root."
    }
    return $fullPath
}

function Assert-UnderParent {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$ParentPath,
        [Parameter(Mandatory)][string]$Label
    )

    $fullPath = Get-NormalizedPath $Path
    $fullParent = Get-NormalizedPath $ParentPath
    $prefix = if ($fullParent.EndsWith('\') -or $fullParent.EndsWith('/')) {
        $fullParent
    } else {
        $fullParent + '\'
    }
    if (-not $fullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must be under parent: $fullParent"
    }
    return $fullPath
}

function Get-RelativePath {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Path
    )

    $rootPath = Get-NormalizedPath $Root
    if (-not ($rootPath.EndsWith('\') -or $rootPath.EndsWith('/'))) {
        $rootPath += '\'
    }
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $rootUri = New-Object System.Uri($rootPath)
    $pathUri = New-Object System.Uri($pathFull)
    return [System.Uri]::UnescapeDataString(
        $rootUri.MakeRelativeUri($pathUri).ToString()
    ).Replace('/', '\')
}

function Test-ExcludedRelativePath {
    param(
        [Parameter(Mandatory)][string]$RelativePath,
        [Parameter(Mandatory)][bool]$IncludeRuntimeData
    )

    $parts = @(
        $RelativePath.Split([char[]]@('\', '/'), [System.StringSplitOptions]::RemoveEmptyEntries)
    )
    $name = if ($parts.Count) { $parts[-1] } else { '' }
    $alwaysExcludedDirectories = @(
        '.git', '.agents', '.codex', '.vscode', '.idea',
        '.cache', '.paddlex', '.portal_tmp', '.portal_recycle',
        '.runtime-cache', '.tmp_cargo_home', '__pycache__',
        '.pytest_cache', '.mypy_cache', 'target', 'venv', '.venv',
        'chromium_user_data', 'development'
    )
    if ($parts | Where-Object { $alwaysExcludedDirectories -contains $_ }) {
        return $true
    }
    if ($name -in @('.gitignore', 'AGENTS.md', 'Thumbs.db', '.DS_Store')) {
        return $true
    }
    if ($IncludeRuntimeData) {
        return $false
    }

    $runtimeDataDirectories = @(
        'portal_outputs', 'portal_inputs', 'images', 'output', 'Work-txt'
    )
    if ($parts | Where-Object { $runtimeDataDirectories -contains $_ }) {
        return $true
    }
    for ($index = 0; $index -lt $parts.Count - 1; $index++) {
        if ($parts[$index] -eq 'sources' -and $parts[$index + 1] -eq 'temp') {
            return $true
        }
        if ($parts[$index] -eq 'date_review' -and $parts[$index + 1] -eq '0Work') {
            return $true
        }
    }
    if ($name -eq 'todo.db' -or $name -like '*.sqlite3' -or $name -like '*.log') {
        return $true
    }
    return $false
}

function Get-ReleaseFiles {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][bool]$IncludeRuntimeData
    )

    $stack = New-Object System.Collections.Stack
    $stack.Push($Root)
    while ($stack.Count -gt 0) {
        $directory = [string]$stack.Pop()
        foreach ($entry in Get-ChildItem -LiteralPath $directory -Force) {
            $relative = Get-RelativePath $Root $entry.FullName
            if (Test-ExcludedRelativePath $relative $IncludeRuntimeData) {
                continue
            }
            if ($entry.PSIsContainer) {
                if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                    Write-Warning "Skipping directory link: $relative"
                    continue
                }
                $stack.Push($entry.FullName)
                continue
            }
            [PSCustomObject]@{
                FullName = $entry.FullName
                RelativePath = $relative
                Length = [int64]$entry.Length
            }
        }
    }
}

function Copy-ReleaseFile {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )

    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Rewrite-EmbeddedSourcePaths {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$OldRoot,
        [Parameter(Mandatory)][string]$NewRoot
    )

    $extensions = @(
        '.bat', '.cmd', '.ps1', '.rs', '.py', '.toml', '.yaml', '.yml',
        '.json', '.js', '.css', '.html', '.conf', '.ini'
    )
    $oldBackslash = $OldRoot.Replace('/', '\')
    $oldForwardSlash = $oldBackslash.Replace('\', '/')
    $newBackslash = $NewRoot.Replace('/', '\')
    $newForwardSlash = $newBackslash.Replace('\', '/')
    $replacements = @(
        @($oldBackslash, $newBackslash),
        @($oldForwardSlash, $newForwardSlash)
    )

    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force) {
        if ($extensions -notcontains $file.Extension.ToLowerInvariant()) {
            continue
        }
        $content = [System.IO.File]::ReadAllText($file.FullName)
        $updated = $content
        foreach ($replacement in $replacements) {
            $updated = $updated.Replace($replacement[0], $replacement[1])
        }
        if ($updated -ne $content) {
            [System.IO.File]::WriteAllText(
                $file.FullName,
                $updated,
                [System.Text.UTF8Encoding]::new($false)
            )
            Write-Host "Rewrote embedded path: $((Get-RelativePath $Root $file.FullName))"
        }
    }
}

function Get-Manifest {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][bool]$SkipChecksums
    )

    $manifest = @(
        foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force) {
            $relative = Get-RelativePath $Root $file.FullName
            $hash = if ($SkipChecksums) {
                ''
            } else {
                (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
            }
            [PSCustomObject]@{
                path = $relative.Replace('\', '/')
                length = [int64]$file.Length
                sha256 = $hash
            }
        }
    )
    return $manifest
}

function Test-Manifest {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][object[]]$Manifest,
        [Parameter(Mandatory)][bool]$SkipChecksums
    )

    foreach ($item in $Manifest) {
        $path = Join-Path $Root ($item.path.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Publish validation failed, missing file: $($item.path)"
        }
        $file = Get-Item -LiteralPath $path -Force
        if ([int64]$file.Length -ne [int64]$item.length) {
            throw "Publish validation failed, size mismatch: $($item.path)"
        }
        if (-not $SkipChecksums) {
            $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
            if ($actual -ne $item.sha256) {
                throw "Publish validation failed, SHA256 mismatch: $($item.path)"
            }
        }
    }
}

if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $scriptPath = if (-not [string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        $PSScriptRoot
    } else {
        Split-Path -Parent $PSCommandPath
    }
    $SourceRoot = Split-Path -Parent $scriptPath
}
$source = Get-NormalizedPath (Resolve-Path -LiteralPath $SourceRoot).Path
$target = Assert-SafeDirectory $TargetRoot 'target directory'
$source = Assert-SafeDirectory $source 'source directory'
if ([System.StringComparer]::OrdinalIgnoreCase.Equals($source, $target)) {
    throw 'Source and target directories cannot be the same.'
}
$targetParentRaw = Split-Path -Parent $target
if (-not [System.IO.Directory]::Exists($targetParentRaw)) {
    throw "Target parent directory does not exist: $targetParentRaw"
}
$targetParent = Get-NormalizedPath $targetParentRaw
Assert-UnderParent -Path $target -ParentPath $targetParent -Label 'target directory' | Out-Null

$targetExists = Test-Path -LiteralPath $target
if ($targetExists -and -not (Test-Path -LiteralPath $target -PathType Container)) {
    throw "Publish target is not a directory: $target"
}

$includeRuntimeData = $IncludeData.IsPresent
$skipChecksums = $SkipHash.IsPresent
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stage = Join-Path $targetParent ".AlgorithmWeb.stage.$timestamp"
$backup = Join-Path $targetParent "AlgorithmWeb.backup.$timestamp"
Assert-UnderParent -Path $stage -ParentPath $targetParent -Label 'stage directory' | Out-Null
Assert-UnderParent -Path $backup -ParentPath $targetParent -Label 'backup directory' | Out-Null
$targetHasEntries = $targetExists -and (@(Get-ChildItem -LiteralPath $target -Force).Count -gt 0)
$targetModified = $false
$backupCreated = $false

$versionPath = Join-Path $source 'Document\VERSION'
$version = if (Test-Path -LiteralPath $versionPath -PathType Leaf) {
    (Get-Content -LiteralPath $versionPath -Encoding utf8 | Select-Object -First 1).Trim()
} else {
    'unknown'
}
$files = @(Get-ReleaseFiles $source $includeRuntimeData)
$totalBytes = [int64](($files | Measure-Object -Property Length -Sum).Sum)
$totalGiB = [math]::Round($totalBytes / 1GB, 2)

Write-Host "Source: $source"
Write-Host "Target: $target"
Write-Host "Version: $version"
Write-Host "Mode: $(if ($includeRuntimeData) { 'initial release with runtime data' } else { 'code/config/model update; keep target runtime data' })"
Write-Host "Files: $($files.Count); estimated size: $totalBytes bytes (${totalGiB} GiB)"
if ($skipChecksums) {
    Write-Warning 'Using -SkipHash: only file presence and size will be validated.'
} else {
    Write-Host 'Validation: SHA256'
}

if ($Preview) {
    Write-Host 'Preview mode: no stage directory, no copy, no target changes.'
    exit 0
}

try {
    if (Test-Path -LiteralPath $stage) {
        throw "Stage directory already exists, refusing to overwrite: $stage"
    }
    if (Test-Path -LiteralPath $backup) {
        throw "Backup directory already exists, refusing to overwrite: $backup"
    }
    New-Item -ItemType Directory -Path $stage -Force | Out-Null

    foreach ($file in $files) {
        $destination = Join-Path $stage $file.RelativePath
        Copy-ReleaseFile $file.FullName $destination
    }
    Rewrite-EmbeddedSourcePaths $stage $source $target
    $manifest = @(Get-Manifest $stage $skipChecksums)

    if ($targetHasEntries) {
        Write-Host "Creating pre-release backup: $backup"
        Copy-Item -LiteralPath $target -Destination $backup -Recurse -Force
        $backupCreated = $true
    }
    if (-not (Test-Path -LiteralPath $target -PathType Container)) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }

    foreach ($item in $manifest) {
        $sourceFile = Join-Path $stage ($item.path.Replace('/', '\'))
        $targetFile = Join-Path $target ($item.path.Replace('/', '\'))
        Copy-ReleaseFile $sourceFile $targetFile
        $targetModified = $true
    }
    Test-Manifest $target $manifest $skipChecksums

    $releaseMarker = [ordered]@{
        schema = 1
        version = $version
        published_at = (Get-Date).ToString('o')
        include_data = $includeRuntimeData
        checksum = if ($skipChecksums) { 'length-only' } else { 'sha256' }
        file_count = $manifest.Count
        files = @($manifest)
    }
    $markerPath = Join-Path $target '.algorithm-web-release.json'
    $releaseMarker | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $markerPath -Encoding utf8

    if ($RemoveBackupOnSuccess -and $backupCreated) {
        Assert-SafeDirectory $backup 'backup directory' | Out-Null
        Remove-Item -LiteralPath $backup -Recurse -Force
        $backupCreated = $false
    }
    Write-Host "Publish succeeded: $target"
    if ($backupCreated) {
        Write-Host "Rollback backup retained at: $backup"
    }
}
catch {
    Write-Error "Publish failed: $($_.Exception.Message)"
    if ($targetModified) {
        try {
            Assert-SafeDirectory $target 'rollback target directory' | Out-Null
            if (Test-Path -LiteralPath $target) {
                Remove-Item -LiteralPath $target -Recurse -Force
            }
            if ($backupCreated) {
                Assert-SafeDirectory $backup 'rollback backup directory' | Out-Null
                Copy-Item -LiteralPath $backup -Destination $target -Recurse -Force
                Write-Host "Rolled back to pre-release target: $target"
            } else {
                New-Item -ItemType Directory -Path $target -Force | Out-Null
                Write-Host "Cleaned incomplete publish target: $target"
            }
        }
        catch {
            Write-Error "Automatic rollback failed: $($_.Exception.Message)"
        }
    } else {
        Write-Host 'Target was not modified; rollback is not needed.'
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $stage) {
        Assert-SafeDirectory $stage 'stage directory' | Out-Null
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
