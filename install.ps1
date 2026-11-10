[CmdletBinding()]
param([string]$UserProfilePath = $env:USERPROFILE)
$ErrorActionPreference = 'Stop'
$candidate = Join-Path $PSScriptRoot 'skill'
$sourcePath = if (Test-Path -LiteralPath (Join-Path $candidate 'SKILL.md')) { (Resolve-Path -LiteralPath $candidate).Path } else { (Resolve-Path -LiteralPath $PSScriptRoot).Path }
$profilePath = [IO.Path]::GetFullPath($UserProfilePath)
$canonicalPath = Join-Path $profilePath '.agents\skills\work-time-calendar'
$claudePath = Join-Path $profilePath '.claude\skills\work-time-calendar'
$openCodePath = Join-Path $profilePath '.config\opencode\skills\work-time-calendar'

# Inspect every destination before mutations; do not replace another skill or link.
foreach ($targetPath in @($canonicalPath, $claudePath, $openCodePath)) {
    if (Test-Path -LiteralPath $targetPath) {
        $targetItem = Get-Item -LiteralPath $targetPath -Force
        if ($targetItem.LinkType) {
            if ([IO.Path]::GetFullPath([string]$targetItem.Target).TrimEnd('\') -ne $canonicalPath.TrimEnd('\')) {
                throw "Existing link points elsewhere: $targetPath"
            }
        } elseif ($targetPath -ne $canonicalPath) {
            throw "Existing non-link skill must be reviewed before installation: $targetPath"
        } else {
            $existingSkill = Join-Path $targetPath 'SKILL.md'
            if (-not (Test-Path -LiteralPath $existingSkill) -or
                -not ((Get-Content -LiteralPath $existingSkill -Raw) -match '(?m)^name: work-time-calendar\s*$')) {
                throw "Canonical destination is not this skill: $targetPath"
            }
        }
    }
}
New-Item -ItemType Directory -Path $canonicalPath -Force | Out-Null
$copied = 0
foreach ($sourceFile in Get-ChildItem -LiteralPath $sourcePath -File -Recurse) {
    if ($sourceFile.FullName -match '[\\/]__pycache__[\\/]' -or $sourceFile.Extension -eq '.pyc') { continue }
    $relativeCheck = [IO.Path]::GetRelativePath($sourcePath, $sourceFile.FullName)
    if ($sourcePath -eq $PSScriptRoot -and $relativeCheck -notmatch '^(SKILL\.md|agents[\\/]|references[\\/]|scripts[\\/])') { continue }
    $relativePath = [IO.Path]::GetRelativePath($sourcePath, $sourceFile.FullName)
    $destinationPath = Join-Path $canonicalPath $relativePath
    New-Item -ItemType Directory -Path (Split-Path -Parent $destinationPath) -Force | Out-Null
    Copy-Item -LiteralPath $sourceFile.FullName -Destination $destinationPath -Force
    if ((Get-FileHash -LiteralPath $sourceFile.FullName).Hash -ne (Get-FileHash -LiteralPath $destinationPath).Hash) {
        throw "Hash mismatch: $relativePath"
    }
    $copied++
}
foreach ($aliasPath in @($claudePath,$openCodePath)) {
    if (-not (Test-Path -LiteralPath $aliasPath)) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $aliasPath) -Force | Out-Null
        New-Item -ItemType Junction -Path $aliasPath -Target $canonicalPath | Out-Null
    }
}
$skillHashes = @($canonicalPath,$claudePath,$openCodePath) | ForEach-Object {
    $skillPath = Join-Path $_ 'SKILL.md'
    [pscustomobject]@{ Path = $skillPath; SHA256 = (Get-FileHash -LiteralPath $skillPath).Hash }
}
if (@($skillHashes.SHA256 | Select-Object -Unique).Count -ne 1) { throw 'Agent skill hashes differ' }
[pscustomobject]@{ FilesVerified = $copied; Canonical = $canonicalPath; AgentPaths = $skillHashes } | ConvertTo-Json -Depth 4
