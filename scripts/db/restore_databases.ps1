param([Parameter(Mandatory=$true)][string]$BackupDir, [switch]$SkipMissing)
$ErrorActionPreference = "Stop"
if (-not (Test-Path $BackupDir)) { throw "Backup directory not found: $BackupDir" }
function Need($name) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if (-not $cmd -and -not $SkipMissing) { throw "$name not found" }
  return $cmd
}
$pg = Need "pg_restore"; if ($pg -and (Test-Path (Join-Path $BackupDir "postgres.dump"))) {
  $pgDatabase = if ($env:PGDATABASE) { $env:PGDATABASE } else { "vector_db" }
  & $pg.Source "--clean" "--if-exists" "--no-owner" "-d" $pgDatabase (Join-Path $BackupDir "postgres.dump")
}
$mysql = Need "mysql"; if ($mysql -and (Test-Path (Join-Path $BackupDir "mysql.sql"))) {
  $mysqlDatabase = if ($env:MYSQL_DATABASE) { $env:MYSQL_DATABASE } else { "agentix" }
  Get-Content (Join-Path $BackupDir "mysql.sql") -Raw | & $mysql.Source $mysqlDatabase
}
$neo = Need "neo4j-admin"; $neoFile = Get-ChildItem $BackupDir -Filter "*.dump" | Select-Object -First 1
if ($neo -and $neoFile) {
  $neoDatabase = if ($env:NEO4J_DATABASE) { $env:NEO4J_DATABASE } else { "neo4j" }
  & $neo.Source database load $neoDatabase "--from-path=$BackupDir" "--overwrite-destination=true"
}
$redis = Need "redis-cli"; if ($redis -and (Test-Path (Join-Path $BackupDir "redis.rdb"))) {
  Write-Warning "redis-cli cannot restore an RDB into a running server; copy redis.rdb to the Redis dir and restart Redis."
}
Write-Host "Restore complete (verify each service before accepting traffic)."
