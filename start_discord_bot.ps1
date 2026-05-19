# VC Scouting Intelligence System — Discord Bot Start Script
# Run this in a SEPARATE terminal AFTER start.ps1 is running
# Usage: .\start_discord_bot.ps1

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host "  SCOUT Discord Bot" -ForegroundColor Magenta
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host ""

# Check API is reachable
Write-Host "Checking API server..." -ForegroundColor Yellow
try {
    $health = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5
    Write-Host "  API is up. Startups in DB: $($health.startups_in_db)" -ForegroundColor Green
} catch {
    Write-Host "  WARNING: API not responding. Make sure start.ps1 is running first." -ForegroundColor Red
}

# Check DISCORD_BOT_TOKEN
if (-not (Test-Path ".env")) {
    Write-Host "ERROR: .env file not found. Copy .env.example to .env and fill in your token." -ForegroundColor Red
    exit 1
}

$envContent = Get-Content .env
$tokenLine  = $envContent | Where-Object { $_ -match "^DISCORD_BOT_TOKEN=" }
if ($tokenLine -eq "DISCORD_BOT_TOKEN=your_discord_bot_token_here" -or -not $tokenLine) {
    Write-Host "ERROR: DISCORD_BOT_TOKEN not set in .env" -ForegroundColor Red
    Write-Host "  1. Go to https://discord.com/developers/applications" -ForegroundColor Yellow
    Write-Host "  2. Create a bot and copy its token" -ForegroundColor Yellow
    Write-Host "  3. Paste into .env: DISCORD_BOT_TOKEN=your_token" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "Starting SCOUT Discord Bot..." -ForegroundColor Magenta
Write-Host "Available slash commands:" -ForegroundColor DarkGray
Write-Host "  /scout   — search startups" -ForegroundColor White
Write-Host "  /match   — investor-startup matchmaking" -ForegroundColor White
Write-Host "  /sector  — sector intelligence report" -ForegroundColor White
Write-Host "  /add     — manually add a startup" -ForegroundColor White
Write-Host "  /ingest  — trigger data ingestion" -ForegroundColor White
Write-Host "  /status  — system health check" -ForegroundColor White
Write-Host ""
Write-Host "Press Ctrl+C to stop" -ForegroundColor DarkGray
Write-Host "============================================================" -ForegroundColor Magenta
Write-Host ""

python discord_bot/bot.py
