<#
.SYNOPSIS
  Copy the newest production backup pair to this machine, and check it arrived intact.

.DESCRIPTION
  The server already dumps the database and archives uploads once a day
  (scripts/backup.sh, the `backup` service). Both land in a Docker volume on the
  SAME disk as the database they protect — so they survive a bad migration or a
  dropped table, and not a dead disk. This script is the off-site half: it pulls
  the newest pair down here.

  Nothing is deleted on the server. Local copies older than -KeepDays are pruned,
  so the folder does not grow without limit; the server keeps its own 7 days
  independently.

  Integrity is checked after the copy, not assumed. A truncated transfer produces
  a file that looks fine in Explorer and fails at 3am six months from now, which
  is the only moment anybody will care.

.PARAMETER Destination
  Where to keep the copies. Default: C:\Users\<you>\Backups\ContentEngine

.EXAMPLE
  pwsh scripts/pull-prod-backup.ps1
  pwsh scripts/pull-prod-backup.ps1 -KeepDays 30
#>
[CmdletBinding()]
param(
  [string] $ServerHost = '167.233.156.202',
  [string] $User       = 'root',
  [string] $KeyPath    = "$HOME\.ssh\hetzner_key",
  [string] $RemoteDir  = '/var/lib/docker/volumes/instacontentengine_backups/_data',
  [string] $Destination = "$HOME\Backups\ContentEngine",
  [int]    $KeepDays   = 30
)

$ErrorActionPreference = 'Stop'

function Fail($message) { Write-Error $message; exit 1 }

if (-not (Test-Path $KeyPath)) { Fail "SSH key not found: $KeyPath" }
if (-not (Test-Path $Destination)) { New-Item -ItemType Directory -Path $Destination -Force | Out-Null }

$target = "$User@$ServerHost"
$sshArgs = @('-i', $KeyPath, '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=20')

# The newest pair, chosen on the server: the timestamps in the two filenames are
# written by the same loop iteration, so picking the newest of each independently
# could straddle two runs and pair a database with the wrong media.
Write-Host 'Asking the server which backup is newest...'
$stamp = (& ssh @sshArgs $target "ls -1 $RemoteDir/insta_*.sql.gz 2>/dev/null | sort | tail -1 | sed -E 's#.*/insta_(.*)\.sql\.gz#\1#'").Trim()
if ($LASTEXITCODE -ne 0) { Fail 'Could not reach the server.' }
if (-not $stamp)          { Fail "No dumps found in $RemoteDir on the server." }

$files = @("insta_$stamp.sql.gz", "uploads_$stamp.tgz")
Write-Host "Newest backup: $stamp"

foreach ($name in $files) {
  $local = Join-Path $Destination $name
  if (Test-Path $local) { Write-Host "  already here: $name"; continue }
  Write-Host "  copying $name ..."
  & scp -i $KeyPath -o StrictHostKeyChecking=no "${target}:$RemoteDir/$name" $local
  if ($LASTEXITCODE -ne 0) { Fail "Copy failed: $name" }
}

# Verify what landed, rather than trusting that it did.
#
# Both files are gzip streams — one wraps SQL, the other a tar — so both are
# checked the same way: decompress the whole thing and throw the bytes away. A
# gzip member ends with a CRC and a length, and .NET's GZipStream validates both,
# so a transfer that stopped halfway fails here rather than at 3am six months
# from now. `tar -tzf` was wrong for the .sql.gz: it is not an archive.
function Test-GzipIntact {
  param([string] $Path)
  $in = $null; $gz = $null
  try {
    $in = [System.IO.File]::OpenRead($Path)
    $gz = New-Object System.IO.Compression.GZipStream($in, [System.IO.Compression.CompressionMode]::Decompress)
    $buffer = New-Object byte[] 1048576
    $bytes = 0L
    while (($read = $gz.Read($buffer, 0, $buffer.Length)) -gt 0) { $bytes += $read }
    return $bytes
  } finally {
    if ($gz) { $gz.Dispose() }
    if ($in) { $in.Dispose() }
  }
}

Write-Host 'Checking the copies...'
$bad = @()
foreach ($name in $files) {
  $local = Join-Path $Destination $name
  if (-not (Test-Path $local)) { $bad += "$name (missing)"; continue }
  $size = (Get-Item $local).Length
  try {
    $raw = Test-GzipIntact -Path $local
    if ($raw -le 0) { $bad += "$name (empty)" }
    else { Write-Host ("  ok: {0}  ({1:N1} MB on disk, {2:N1} MB inside)" -f $name, ($size / 1MB), ($raw / 1MB)) }
  } catch {
    $bad += "$name (corrupt or truncated)"
  }
}
if ($bad.Count) { Fail ("Damaged in transfer: " + ($bad -join ', ')) }

# Prune old local copies. The server prunes its own on BACKUP_KEEP_DAYS; this is
# a separate, longer window, which is the point of keeping a second copy.
$cutoff = (Get-Date).AddDays(-$KeepDays)
Get-ChildItem $Destination -File |
  Where-Object { $_.Name -match '^(insta_|uploads_)' -and $_.LastWriteTime -lt $cutoff } |
  ForEach-Object { Write-Host "  pruning $($_.Name)"; Remove-Item $_.FullName -Force }

$kept = @(Get-ChildItem $Destination -File | Where-Object { $_.Name -match '^(insta_|uploads_)' })
$total = ($kept | Measure-Object -Property Length -Sum).Sum
Write-Host ''
Write-Host ("Done. {0} files in {1} ({2:N1} MB)." -f $kept.Count, $Destination, ($total / 1MB))
