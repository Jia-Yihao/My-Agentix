param(
  [string]$OutputDir = "backups",
  [switch]$SkipMissing
)
$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$dir = Join-Path $OutputDir $stamp
New-Item -ItemType Directory -Force -Path $dir | Out-Null

function Invoke-DbDump($name, $command, $arguments) {
  $target = Join-Path $dir $name
  $exe = Get-Command $command -ErrorAction SilentlyContinue
  if (-not $exe) {
    if ($SkipMissing) { Write-Warning "$command not found; skipped $name"; return }
    throw "$command not found. Install the database client or use -SkipMissing."
  }
  & $exe.Source @arguments > $target
  if ($LASTEXITCODE -ne 0) { throw "$command failed ($LASTEXITCODE)" }
  Write-Host "Created $target"
}

$pgArgs = @("-Fc")
if ($env:PGHOST) { $pgArgs += @("-h", $env:PGHOST) }
if ($env:PGPORT) { $pgArgs += @("-p", $env:PGPORT) }
if ($env:PGUSER) { $pgArgs += @("-U", $env:PGUSER) }
$pgDatabase = if ($env:PGDATABASE) { $env:PGDATABASE } else { "vector_db" }
$pgArgs += @("-f", (Join-Path $dir "postgres.dump"), $pgDatabase)
$pg = Get-Command pg_dump -ErrorAction SilentlyContinue
if ($pg) { & $pg.Source @pgArgs } elseif (-not $SkipMissing) { throw "pg_dump not found" }

$mysql = Get-Command mysqldump -ErrorAction SilentlyContinue
if ($mysql) {
  $mysqlDatabase = if ($env:MYSQL_DATABASE) { $env:MYSQL_DATABASE } else { "agentix" }
  & $mysql.Source "--single-transaction" "--routines" "--triggers" $mysqlDatabase > (Join-Path $dir "mysql.sql")
} elseif (-not $SkipMissing) { throw "mysqldump not found" }

$neo = Get-Command neo4j-admin -ErrorAction SilentlyContinue
if ($neo) {
  $neoDatabase = if ($env:NEO4J_DATABASE) { $env:NEO4J_DATABASE } else { "neo4j" }
  & $neo.Source database dump $neoDatabase "--to-path=$dir"
} elseif (-not $SkipMissing) { throw "neo4j-admin not found" }

$redis = Get-Command redis-cli -ErrorAction SilentlyContinue
if ($redis) { & $redis.Source --rdb (Join-Path $dir "redis.rdb") } elseif (-not $SkipMissing) { throw "redis-cli not found" }

Get-ChildItem $dir | Get-FileHash -Algorithm SHA256 | Export-Csv (Join-Path $dir "SHA256SUMS.csv") -NoTypeInformation
Write-Host "Backup complete: $dir"
