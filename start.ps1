# VC Scouting Intelligence System — Start Script (Windows)
# Run this to start the full stack
# Usage: .\start.ps1

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  VC Scouting Intelligence System" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ── Check Docker ──────────────────────────────────────────────────────────────
Write-Host "[1/5] Checking Docker..." -ForegroundColor Yellow
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "      ERROR: Docker not found. Install Docker Desktop first." -ForegroundColor Red
    exit 1
}

# ── Start Infrastructure ──────────────────────────────────────────────────────
Write-Host "[2/5] Starting PostgreSQL + Qdrant via Docker Compose..." -ForegroundColor Yellow
docker-compose up -d
if ($LASTEXITCODE -ne 0) {
    Write-Host "      ERROR: docker-compose failed." -ForegroundColor Red
    exit 1
}
Write-Host "      Waiting 8 seconds for services to be ready..." -ForegroundColor DarkGray
Start-Sleep -Seconds 8

# ── Setup Database ────────────────────────────────────────────────────────────
Write-Host "[3/5] Initializing database tables..." -ForegroundColor Yellow
python scripts/setup_db.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "      ERROR: Database setup failed. Check PostgreSQL + Qdrant are running." -ForegroundColor Red
    exit 1
}

# ── Check Ollama ──────────────────────────────────────────────────────────────
Write-Host "[4/5] Checking Ollama..." -ForegroundColor Yellow
try {
    $ollamaResp = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 5
    Write-Host "      Ollama is running." -ForegroundColor Green
} catch {
    Write-Host "      WARNING: Ollama not responding at localhost:11434" -ForegroundColor Red
    Write-Host "      Start Ollama and ensure these models are pulled:" -ForegroundColor Yellow
    Write-Host "        ollama pull qwen3:14b" -ForegroundColor White
    Write-Host "        ollama pull nomic-embed-text" -ForegroundColor White
}

# ── Start API Server ──────────────────────────────────────────────────────────
Write-Host "[5/5] Starting FastAPI server..." -ForegroundColor Yellow
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  API running at:   http://localhost:8000" -ForegroundColor Green
Write-Host "  API docs at:      http://localhost:8000/docs" -ForegroundColor Green
Write-Host "  Press Ctrl+C to stop" -ForegroundColor DarkGray
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""

python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
