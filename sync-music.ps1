# sync-music.ps1 -- send new music from your PC to the NAS and file it into the library.
#
# There is no exclude list to edit:
#   * every folder under your music folder (MusicSource in dewey.config.psd1) is a playlist folder, UNLESS it is in sync-music.ignore.txt
#   * a file counts as "new" when the NAS has not already filed it (the NAS keeps a map of everything it has seen,
#     out\incoming-map.tsv), so new folders AND new files in an old folder are found automatically
#
# WORKFLOW
#   1. Drop a new playlist folder (or new songs in a folder you synced before) into your music folder
#   2. Run   .\sync-music.ps1
#      - it lists what is new and asks before it copies anything
#      - the new files are copied to \\NAS\Media\_incoming (nothing in the library is touched by the copy)
#      - the NAS then runs "place": each song is filed under Artist\Album (YYYY)\NN - Title, songs you already
#        have (or that arrive twice) are not added again, and the playlist .m3u is built from where each song ended up
#   3. Rescan Navidrome (or wait for its scan)
#
#   .\sync-music.ps1 -ShowOnly              list what is new, copy nothing
#   .\sync-music.ps1 -Yes                   no questions (playlists that already exist are left alone)
#   .\sync-music.ps1 -SkipDryRun            still asks before copying (and about existing playlists), but files the songs without the
#                                           dry run and the "File these now?" question. For music you already know is good to add.
#   .\sync-music.ps1 -NoPlace               copy to _incoming only; run  .\dewey\dewey.ps1 place  yourself later
#   .\sync-music.ps1 -Only "Folder name"    look at / send just this folder (even if it is in the ignore list); good for a first test
#   .\sync-music.ps1 -Ignore "A","B"        never sync these folders again (added to sync-music.ignore.txt), then exit
#
# Songs with no artist/title tags cannot be filed: they stay in \\NAS\Media\_incoming and are listed at the end.
# Fix the tags on the PC copy (for example with Mp3tag) and run this script again; they are sent again automatically.

param(
    [string]$Source = '',     # your music folder on this PC          (MusicSource in dewey.config.psd1)
    [string]$Nas = '',        # the media share, e.g. \\nas\Media     (MediaShare in dewey.config.psd1)
    [string]$MapFile = '',    # <Share>\out\incoming-map.tsv on the NAS (derived from Share in dewey.config.psd1)
    [string]$IgnoreFile = (Join-Path $PSScriptRoot 'sync-music.ignore.txt'),
    [Alias('Dedupe')]
    [string]$Dewey = $(if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'dewey.ps1')) { Join-Path $PSScriptRoot 'dewey.ps1' } else { Join-Path $PSScriptRoot 'dewey\dewey.ps1' }),
    [string[]]$Ignore,
    [string[]]$Only,
    [switch]$ShowOnly,
    [switch]$NoPlace,
    [switch]$SkipDryRun,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'

# settings: parameters win, then dewey.config.psd1 next to this script (copy dewey.config.example.psd1 and edit it)
$cfg = @{}
$cfgFile = Join-Path $PSScriptRoot 'dewey.config.psd1'
if (Test-Path -LiteralPath $cfgFile) { $cfg = Import-PowerShellDataFile -LiteralPath $cfgFile }
if (-not $Source -and $cfg.MusicSource) { $Source = [string]$cfg.MusicSource }
if (-not $Nas -and $cfg.MediaShare) { $Nas = [string]$cfg.MediaShare }
if (-not $MapFile -and $cfg.Share) { $MapFile = Join-Path ([string]$cfg.Share) 'out\incoming-map.tsv' }
$missingCfg = @()
if (-not $Source) { $missingCfg += 'MusicSource' }
if (-not $Nas) { $missingCfg += 'MediaShare' }
if (-not $MapFile) { $missingCfg += 'Share' }
if ($missingCfg.Count -gt 0) {
    Write-Host "Missing setting(s): $($missingCfg -join ', ')" -ForegroundColor Red
    Write-Host "Copy dewey.config.example.psd1 to dewey.config.psd1 (next to this script), fill it in, and run this again." -ForegroundColor Yellow
    exit 1
}
$audioExt = @('.flac', '.mp3', '.m4a', '.mp4', '.ogg', '.oga', '.opus', '.wav', '.wma', '.aac', '.ape', '.wv', '.aif', '.aiff')

function Norm([string]$s) { return $s.Replace([string][char]0x2019, "'").Trim().ToLowerInvariant() }
function CleanName([string]$s) { return (($s -replace '[\\/:*?"<>|]', '_').Trim().Trim('.')) }

if (-not (Test-Path -LiteralPath $IgnoreFile)) {
    $header = @(
        '# Folders under your music folder that sync-music.ps1 never sends to the NAS (one folder name per line, # = comment).',
        '# Put folders here that you do not want synced, for example playlists you manage somewhere else.',
        '# A folder that is NOT listed here is synced. Add one with:  .\sync-music.ps1 -Ignore "Folder name"'
    )
    [System.IO.File]::WriteAllLines($IgnoreFile, $header, [System.Text.UTF8Encoding]::new($true))
    Write-Host "Created $IgnoreFile (empty: every folder under $Source counts as new until you add it)." -ForegroundColor Cyan
}

if ($Ignore -and $Ignore.Count -gt 0) {
    $existing = @(Get-Content -LiteralPath $IgnoreFile -Encoding UTF8)
    $have = @{}
    foreach ($l in $existing) { $have[(Norm $l)] = $true }
    $add = @()
    foreach ($n in $Ignore) { if (-not $have.ContainsKey((Norm $n))) { $add += $n } }
    if ($add.Count -gt 0) {
        [System.IO.File]::AppendAllText($IgnoreFile, ((($add -join "`r`n")) + "`r`n"), [System.Text.UTF8Encoding]::new($false))
    }
    Write-Host "Ignoring from now on: $($Ignore -join ', ')" -ForegroundColor Green
    return
}

if (-not (Test-Path -LiteralPath $Source)) { Write-Host "ERROR: source folder not found: $Source" -ForegroundColor Red; exit 1 }
if (-not (Test-Path -LiteralPath $Nas)) { Write-Host "ERROR: NAS unreachable at $Nas. Aborting." -ForegroundColor Red; exit 1 }
$landing = Join-Path $Nas '_incoming'
$plDir = Join-Path $Nas 'Music\Playlists'

$ignored = @{}
foreach ($line in (Get-Content -LiteralPath $IgnoreFile -Encoding UTF8)) {
    $l = $line.Trim()
    if ($l -and -not $l.StartsWith('#')) { $ignored[(Norm $l)] = $true }
}

# if the NAS folder cannot be seen at all, stop: a missing map would make every song on the PC look new
$mapDir = Split-Path -Parent $MapFile
if (-not (Test-Path -LiteralPath $mapDir)) {
    Write-Host "Can't reach $mapDir on the NAS, so there is no way to tell which songs were already filed." -ForegroundColor Red
    Write-Host "Check that the NAS is on and the share is reachable, or pass -MapFile with the right path." -ForegroundColor Yellow
    exit 1
}

# everything the NAS has already filed: "<folder>/<path inside the folder>"
$done = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
if (Test-Path -LiteralPath $MapFile) {
    foreach ($line in [System.IO.File]::ReadAllLines($MapFile, [System.Text.Encoding]::UTF8)) {
        $k = ($line -split "`t")[0]
        if ($k) { [void]$done.Add($k) }
    }
    Write-Host "The NAS has already filed $($done.Count) files from earlier syncs." -ForegroundColor DarkCyan
} else {
    Write-Host "No incoming-map.tsv on the NAS yet: every file in a folder that is not ignored counts as new." -ForegroundColor Yellow
}

Write-Host "Looking for new music under $Source ..." -ForegroundColor Cyan
$plan = New-Object System.Collections.Generic.List[object]
$skippedFolders = 0
$onlySet = @{}
foreach ($o in @($Only)) { if ($o) { $onlySet[(Norm $o)] = $true } }
foreach ($d in (Get-ChildItem -LiteralPath $Source -Directory | Sort-Object Name)) {
    if ($onlySet.Count -gt 0) { if (-not $onlySet.ContainsKey((Norm $d.Name))) { continue } }
    elseif ($ignored.ContainsKey((Norm $d.Name))) { $skippedFolders++; continue }
    $base = $d.FullName.TrimEnd('\')
    $files = @(Get-ChildItem -LiteralPath $d.FullName -Recurse -File -ErrorAction SilentlyContinue |
               Where-Object { $audioExt -contains $_.Extension.ToLowerInvariant() })
    if ($files.Count -eq 0) { continue }
    $new = New-Object System.Collections.Generic.List[object]
    $bytes = 0
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($base.Length).TrimStart('\').Replace('\', '/')
        if (-not $done.Contains($d.Name + '/' + $rel)) {
            $new.Add([pscustomobject]@{ File = $f; Rel = $rel })
            $bytes += $f.Length
        }
    }
    if ($new.Count -gt 0) {
        $m3u = Join-Path $plDir ((CleanName $d.Name) + '.m3u')
        $plan.Add([pscustomobject]@{ Folder = $d; Total = $files.Count; New = $new; Bytes = $bytes; PlaylistExists = (Test-Path -LiteralPath $m3u) })
    }
}
Write-Host "($skippedFolders folder(s) skipped because they are in sync-music.ignore.txt)" -ForegroundColor DarkGray

if ($plan.Count -eq 0) {
    Write-Host "`nNothing new to send." -ForegroundColor Green
    # a place run that was stopped (connection dropped, window closed) after the songs moved but before the playlists were written
    $pending = Join-Path (Split-Path -Parent $MapFile) 'place-pending.json'
    if (Test-Path -LiteralPath $pending) {
        Write-Host "An earlier place run was stopped before it finished: its playlist(s) have not been written yet." -ForegroundColor Yellow
        if (-not $NoPlace -and (Test-Path -LiteralPath $Dewey)) {
            $fin = $(if ($Yes) { 'y' } else { Read-Host "Finish it now? (Y/n)" })
            if ($fin -notmatch '^[Nn]') { & $Dewey place --execute }
        } else {
            Write-Host "Finish it with:  $Dewey place --execute" -ForegroundColor Yellow
        }
    }
    $left = @()
    if (Test-Path -LiteralPath $landing) {
        $left = @(Get-ChildItem -LiteralPath $landing -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $audioExt -contains $_.Extension.ToLowerInvariant() })
    }
    if ($left.Count -gt 0) {
        Write-Host "$($left.Count) file(s) are still waiting in $landing (not filed, usually because they have no artist/title tags)." -ForegroundColor Yellow
    }
    return
}

Write-Host ""
$plan | ForEach-Object {
    [pscustomobject]@{
        Folder   = $_.Folder.Name
        Files    = $_.Total
        New      = $_.New.Count
        MB       = [math]::Round($_.Bytes / 1MB, 0)
        Playlist = $(if ($_.PlaylistExists) { 'exists in Navidrome' } else { 'will be created' })
    }
} | Format-Table -AutoSize

if ($ShowOnly) { Write-Host "-ShowOnly: nothing was copied." -ForegroundColor Cyan; return }

if (-not $Yes) {
    Write-Host "Folders you never want synced can be added with  .\sync-music.ps1 -Ignore `"Folder name`"" -ForegroundColor DarkGray
    $answer = Read-Host "Copy these new files to the NAS now? (Y/n)"
    if ($answer -match '^[Nn]') { Write-Host "Cancelled. Nothing was copied." -ForegroundColor Cyan; return }
}

if (-not (Test-Path -LiteralPath $landing)) { New-Item -ItemType Directory -Path $landing -Force | Out-Null }

foreach ($p in $plan) {
    $name = $p.Folder.Name
    $dest = Join-Path $landing $name
    Write-Host "`nCopying '$name' ($($p.New.Count) new file(s)) ..." -ForegroundColor Cyan
    if ($p.New.Count -eq $p.Total) {
        # a whole new folder: robocopy it (copy only, never moves or deletes anything on the PC)
        robocopy $p.Folder.FullName $dest /E /MT:8 /R:2 /W:2 /NP /NFL /NDL /NJH /XJ /XF Thumbs.db desktop.ini | Out-Null
        if ($LASTEXITCODE -ge 8) { Write-Host "  robocopy reported errors for '$name' (exit code $LASTEXITCODE)" -ForegroundColor Red }
    } else {
        # an old folder with new songs in it: only the new ones
        foreach ($n in $p.New) {
            $target = Join-Path $dest ($n.Rel.Replace('/', '\'))
            [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target))
            [System.IO.File]::Copy($n.File.FullName, $target, $true)
        }
    }
}
Write-Host "`nCopied. The new files are in $landing (the library itself is untouched so far)." -ForegroundColor Green

if ($NoPlace) {
    Write-Host "-NoPlace: run  $Dewey place  when you are ready to file them." -ForegroundColor Cyan
    return
}
if (-not (Test-Path -LiteralPath $Dewey)) {
    Write-Host "Can't find $Dewey - run  .\dewey\dewey.ps1 place  yourself." -ForegroundColor Red
    exit 1
}

# playlists that already exist in Navidrome: appending changes the .m3u, which Navidrome re-reads
# (it would overwrite edits made inside Navidrome), so the default is to leave them alone
$update = @()
if (-not $Yes) {
    foreach ($p in $plan) {
        if ($p.PlaylistExists) {
            $a = Read-Host "Playlist '$($p.Folder.Name)' already exists. Append its $($p.New.Count) new track(s)? (y/N)"
            if ($a -match '^[Yy]') { $update += $p.Folder.Name }
        }
    }
    if ($SkipDryRun) {
        Write-Host "`n-SkipDryRun: filing the songs now without a dry run (duplicates are still set aside in the quarantine, nothing is deleted)." -ForegroundColor Cyan
    } else {
        Write-Host "`nNow the NAS will check what it already has. Dry run first (enter the NAS passwords when asked):" -ForegroundColor Cyan
        & $Dewey place
        if ($LASTEXITCODE -ne 0) { Write-Host "The dry run failed - nothing was filed." -ForegroundColor Red; exit 1 }
        $go = Read-Host "`nFile these now? (Y/n)"
        if ($go -match '^[Nn]') {
            Write-Host "Not filed. The files stay in $landing; run  .\sync-music.ps1  again or  $Dewey place --execute  later." -ForegroundColor Cyan
            return
        }
    }
}
if ($update.Count -gt 0) {
    & $Dewey place --execute --m3u-update ($update -join '|')
} else {
    & $Dewey place --execute
}
if ($LASTEXITCODE -ne 0) { Write-Host "place reported an error - see the output above." -ForegroundColor Red; exit 1 }

$left = @(Get-ChildItem -LiteralPath $landing -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $audioExt -contains $_.Extension.ToLowerInvariant() })
if ($left.Count -gt 0) {
    Write-Host "`n$($left.Count) file(s) could not be filed and are still in $landing :" -ForegroundColor Yellow
    $left | Select-Object -First 25 | ForEach-Object { Write-Host "   $($_.FullName.Substring($landing.Length + 1))" -ForegroundColor Yellow }
    Write-Host "Usually they have no artist/title tags. Fix the tags on the PC copy and run this script again." -ForegroundColor Yellow
}
Write-Host "`nDone. Rescan Navidrome (or wait for its scan). A scheduled Beets import (if you run one) will pick up the new files." -ForegroundColor Green
