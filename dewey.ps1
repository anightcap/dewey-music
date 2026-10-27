# dewey.ps1 -- run Dewey (the music library tool) on the NAS from your PC.
#
#   .\dewey.ps1 scan                                   read-only scan, opens the report
#   .\dewey.ps1 apply --artist "Name"            DRY RUN for one artist
#   .\dewey.ps1 apply --artist "Name" --execute  quarantine the losers
#   .\dewey.ps1 apply --exact-only --execute           only the byte-for-byte-same-song groups
#   .\dewey.ps1 playlistfix                            DRY RUN: playlists that block a removal, and what would change
#   .\dewey.ps1 playlistfix --execute                  point those playlists at the kept copies (backed up first)
#   .\dewey.ps1 playlistfix --restore /out/playlist-backups/<run> --execute
#   .\dewey.ps1 reorg                                  read-only: plan the Artist/Album (YYYY)/NN - Title layout, opens the report
#   .\dewey.ps1 reorg-apply --limit 50                 DRY RUN: the first moves from that plan
#   .\dewey.ps1 reorg-apply --limit 50 --execute       move them (navidrome.db is backed up first, undo is possible)
#   .\dewey.ps1 beets-remap                           DRY RUN: repoint the Beets library.db at the reorganised files
#   .\dewey.ps1 beets-remap --execute                 do it (library.db is backed up first; no music file is touched)
#   .\dewey.ps1 place                                DRY RUN: file new music from the NAS _incoming folder into the library
#   .\dewey.ps1 place --execute                      do it (duplicates go to the quarantine; nothing is deleted)
#   .\dewey.ps1 compare "<A rel path>" "<B rel path>"   read-only: same audio (same version) or a real clean/explicit pair?
#   .\dewey.ps1 nd-missing                            read-only: explain Navidrome's 'Missing Files' list
#   .\dewey.ps1 tagaudit                              read-only: files/albums missing tags, cover art or lyrics
#   .\dewey.ps1 undo --manifest /media/_dedupe-quarantine/<run>/manifest.json --execute
#   .\dewey.ps1 undo --manifest /media/_reorg/<run>/manifest.json --execute      (undo a reorg run)
#   .\dewey.ps1 undo --manifest /media/_dedupe-quarantine/place-<run>/manifest.json --execute   (put a place run's set-aside files back in _incoming)
#   .\dewey.ps1 purge --older-than 30 --execute        permanently delete old quarantine runs
#   .\dewey.ps1 deploy                                 just copy the files to the NAS
#   .\dewey.ps1 web [up|stop|logs|status]              the read-only web console on the NAS (port 8099); 'web' alone = start / restart it
#   .\dewey.ps1 runner [up|stop|logs|status|secret|newsecret]   the runner: lets the web console do a few REAL runs, only while armed with an authenticator-app code
#
# playlistfix logs in to Navidrome with a navidrome.cred file that lives ONLY on the NAS (never copied from here).
# SETTINGS: copy dewey.config.example.psd1 to dewey.config.psd1 (next to this script) and fill it in: NAS address, user, folders.
# That file stays on your PC (it is git-ignored). Any setting can also be given as a parameter, e.g. -NasHost 192.168.1.50.
#
# Keep this file, dewey.py, Dockerfile, dewey.sh, rules.txt (and webui.py, runner.py, web\) together in one folder.
# Every run pushes them to the NAS share first, so rules.txt edits take effect immediately.

param(
    [Parameter(Position = 0)]
    [ValidateSet('scan', 'apply', 'playlistfix', 'reorg', 'reorg-apply', 'beets-remap', 'nd-missing', 'tagaudit', 'compare', 'place', 'undo', 'purge', 'web', 'runner', 'deploy')]
    [string]$Command = 'scan',

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest,

    [string]$NasHost = '',   # NAS address (name or IP)
    [string]$NasUser = '',   # a NAS account that may use ssh and sudo
    [string]$RemoteDir = '', # Dewey's folder on the NAS, e.g. /volume1/docker/dewey
    [string]$Share = '',     # the same folder as seen from this PC, e.g. \\nas\docker\dewey
    [string]$NdData = '',    # folder on the NAS that holds navidrome.db, only if auto-detect fails
    [string]$Media = '',     # folder on the NAS that contains Music/, default /volume1/Media
    [string]$BeetsDir = '',  # folder on the NAS that holds the Beets library.db (only needed for 'beets-remap')
    [string]$TimeZone = '',  # e.g. America/New_York: used when the image is built; run names and logs use it
    [string]$Label = '',     # name shown in your authenticator app for the runner's secret
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

# settings: a parameter wins, then dewey.config.psd1 next to this script
$cfg = @{}
$cfgFile = Join-Path $here 'dewey.config.psd1'
if (Test-Path -LiteralPath $cfgFile) { $cfg = Import-PowerShellDataFile -LiteralPath $cfgFile }
foreach ($k in 'NasHost', 'NasUser', 'RemoteDir', 'Share', 'NdData', 'Media', 'BeetsDir', 'TimeZone', 'Label') {
    if (-not $PSBoundParameters.ContainsKey($k) -and $cfg.ContainsKey($k) -and $cfg[$k]) { Set-Variable -Name $k -Value ([string]$cfg[$k]) }
}
$need = @('NasHost', 'NasUser', 'RemoteDir', 'Share') | Where-Object { -not (Get-Variable -Name $_ -ValueOnly) }
if ($need) {
    Write-Host "Missing setting(s): $($need -join ', ')" -ForegroundColor Red
    Write-Host "Copy dewey.config.example.psd1 to dewey.config.psd1 (next to this script), fill it in, and run this again." -ForegroundColor Yellow
    exit 1
}

function Push-Files {
    if (-not (Test-Path $Share)) {
        try { New-Item -ItemType Directory -Path $Share -Force | Out-Null } catch {}
    }
    if (-not (Test-Path $Share)) {
        Write-Host "Can't reach $Share" -ForegroundColor Red
        Write-Host "Copy dewey.py, Dockerfile, dewey.sh and rules.txt to $RemoteDir on the NAS by hand," -ForegroundColor Yellow
        Write-Host "or pass -Share with the right UNC path." -ForegroundColor Yellow
        exit 1
    }
    # rules.txt is yours (git-ignored); the first time, start it from the example
    $rulesFile = Join-Path $here 'rules.txt'
    $rulesExample = Join-Path $here 'rules.example.txt'
    if (-not (Test-Path $rulesFile) -and (Test-Path $rulesExample)) {
        Copy-Item $rulesExample $rulesFile
        Write-Host "Created rules.txt from rules.example.txt - edit it to taste, then re-run 'scan'." -ForegroundColor Cyan
    }
    foreach ($f in 'dewey.py', 'Dockerfile', 'dewey.sh', 'rules.txt', '.dockerignore') {
        $src = Join-Path $here $f
        if ($f -eq '.dockerignore' -and -not (Test-Path $src)) { continue }
        if (-not (Test-Path $src)) { Write-Host "Missing $src" -ForegroundColor Red; exit 1 }
        # LF line endings -- bash and the Dockerfile break on CRLF
        $text = [System.IO.File]::ReadAllText($src) -replace "`r`n", "`n"
        [System.IO.File]::WriteAllText((Join-Path $Share $f), $text, [System.Text.UTF8Encoding]::new($false))
    }
    # the web console (optional: only if webui.py and the web folder are next to this script)
    $webpy = Join-Path $here 'webui.py'
    $webdir = Join-Path $here 'web'
    $needWeb = @($webpy, (Join-Path $webdir 'index.html'), (Join-Path $webdir 'app.js'), (Join-Path $webdir 'style.css'))
    $missingWeb = @($needWeb | Where-Object { -not (Test-Path $_) })
    $webAction = 'up'
    if ($Rest -and $Rest.Count -gt 0) { $webAction = $Rest[0] }
    if ($missingWeb.Count -gt 0 -and ($Command -eq 'deploy' -or ($Command -eq 'web' -and $webAction -eq 'up'))) {
        Write-Host "The web console files are missing next to dewey.ps1 ($here):" -ForegroundColor Red
        $missingWeb | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
        Write-Host "Copy webui.py and the whole web\ folder (index.html, app.js, style.css) next to dewey.ps1 ($here), then run this again." -ForegroundColor Yellow
        exit 1
    }
    # the runner (optional; only needed for real runs from the web console)
    $runnerpy = Join-Path $here 'runner.py'
    if (Test-Path $runnerpy) {
        $text = [System.IO.File]::ReadAllText($runnerpy) -replace "`r`n", "`n"
        [System.IO.File]::WriteAllText((Join-Path $Share 'runner.py'), $text, [System.Text.UTF8Encoding]::new($false))
    } elseif ($Command -eq 'runner') {
        Write-Host "runner.py is missing next to dewey.ps1 ($here). Copy it next to dewey.ps1, then run this again." -ForegroundColor Red
        exit 1
    }
    if ($missingWeb.Count -eq 0) {
        $text = [System.IO.File]::ReadAllText($webpy) -replace "`r`n", "`n"
        [System.IO.File]::WriteAllText((Join-Path $Share 'webui.py'), $text, [System.Text.UTF8Encoding]::new($false))
        New-Item -ItemType Directory -Path (Join-Path $Share 'web') -Force | Out-Null
        foreach ($wf in Get-ChildItem $webdir -File) {
            $text = [System.IO.File]::ReadAllText($wf.FullName) -replace "`r`n", "`n"
            [System.IO.File]::WriteAllText((Join-Path (Join-Path $Share 'web') $wf.Name), $text, [System.Text.UTF8Encoding]::new($false))
        }
    }
}

Push-Files
if ($Command -eq 'deploy') { Write-Host "Files copied to $Share" -ForegroundColor Green; return }

# single-quote every argument for the remote shell
$quoted = ''
if ($Rest -and $Rest.Count -gt 0) {
    $quoted = (@($Rest) | Where-Object { $null -ne $_ -and $_ -ne '' } | ForEach-Object { "'" + ($_ -replace "'", "'\''") + "'" }) -join ' '
}
$envVars = ''
if ($NdData) { $envVars += " MD_NDDATA='$NdData'" }
if ($Media)  { $envVars += " MD_MEDIA='$Media'" }
if ($BeetsDir) { $envVars += " MD_BEETS='$BeetsDir'" }
if ($TimeZone) { $envVars += " MD_TZ='$TimeZone'" }
if ($Label)    { $envVars += " MD_LABEL='$Label'" }
$remote = "sudo env$envVars bash $RemoteDir/dewey.sh $Command $quoted"

if ($Rest -contains '--execute') {
    if ($Command -eq 'playlistfix') {
        Write-Host "This run will CHANGE Navidrome playlists (backed up first) and may rewrite .m3u files." -ForegroundColor Yellow
    } elseif ($Command -eq 'beets-remap') {
        Write-Host "This run will EDIT the Beets library.db (backed up first); no music file is touched. Make sure the Beets task is not running." -ForegroundColor Yellow
    } elseif ($Command -eq 'place') {
        Write-Host "This run will MOVE the new files from _incoming into the library and write playlists (duplicates are set aside in the quarantine, nothing is deleted)." -ForegroundColor Yellow
    } elseif ($Command -eq 'reorg-apply') {
        Write-Host "This run will MOVE and RENAME files in the library (tags are never changed, navidrome.db is backed up first, the run can be undone)." -ForegroundColor Yellow
    } else {
        Write-Host "This run will CHANGE the library (files go to quarantine, nothing is deleted)." -ForegroundColor Yellow
    }
}

& ssh -t "$NasUser@$NasHost" $remote
$rc = $LASTEXITCODE

# pull the report back so it can be opened locally
$out = Join-Path $here 'out'
if (Test-Path "$Share\out") {
    New-Item -ItemType Directory -Path $out -Force | Out-Null
    foreach ($f in 'report.html', 'dupes.csv', 'plan.json', 'inventory.json', 'unmatched.csv', 'filehealth.csv', 'redirects.tsv', 'scan.log',
                'reorg-plan.json', 'reorg-plan.csv', 'reorg-report.html', 'reorg-moves.tsv', 'beets-remap.csv', 'nd-missing.csv', 'tagaudit.csv', 'tagaudit-albums.csv', 'place-plan.csv', 'incoming-map.tsv') {
        if (Test-Path "$Share\out\$f") { Copy-Item "$Share\out\$f" $out -Force }
    }
    if (Test-Path "$Share\out\playlist-backups") {
        New-Item -ItemType Directory -Path "$out\playlist-backups" -Force | Out-Null
        Copy-Item "$Share\out\playlist-backups\*" "$out\playlist-backups" -Recurse -Force
    }
    # only this run's log comes back; the NAS keeps the full history, and copying all of it every time would undo any clean-up done here
    if ($Command -ne 'scan' -and (Test-Path "$Share\out\logs")) {
        $newest = Get-ChildItem "$Share\out\logs" -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($newest -and $newest.LastWriteTime -gt (Get-Date).AddMinutes(-10)) {
            New-Item -ItemType Directory -Path "$out\logs" -Force | Out-Null
            Copy-Item $newest.FullName "$out\logs" -Force
        }
    }
    if ($Command -eq 'scan' -and $rc -eq 0 -and -not $NoOpen -and (Test-Path "$out\report.html")) {
        Write-Host "Report: $out\report.html" -ForegroundColor Green
        Start-Process "$out\report.html"
    }
    if ($Command -eq 'reorg' -and $rc -eq 0 -and -not $NoOpen -and (Test-Path "$out\reorg-report.html")) {
        Write-Host "Reorg plan: $out\reorg-report.html   (every change, one per line: $out\reorg-plan.csv)" -ForegroundColor Green
        Start-Process "$out\reorg-report.html"
    }
}
exit $rc
