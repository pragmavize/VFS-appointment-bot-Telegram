#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# VISABot — Render Başlatma Scripti
#
#   1. Bot   (visa_watch_onefile.py)  → arka plan
#   2. Dashboard (dashboard.py)       → ön plan, Render $PORT'una bağlanır
#
# sites.json ve proxies.txt git'te tutulmaz (hassas veri). Render env
# değişkenlerinden okunur; boş bırakılırsa dosya oluşturulmaz.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="/app"
cd "$APP_DIR"

# ── Konfigürasyon dosyalarını env'den diske yaz ───────────────────────────────
if [ -n "${SITES_JSON:-}" ]; then
    printf '%s' "$SITES_JSON" > sites.json
    echo "✅ sites.json yazıldı ($(wc -c < sites.json) bayt)"
else
    echo "⚠️  SITES_JSON boş — sites.json oluşturulmadı (bot açılmaz)."
fi

if [ -n "${PROXIES_TXT:-}" ]; then
    printf '%s' "$PROXIES_TXT" > proxies.txt
    echo "✅ proxies.txt yazıldı ($(wc -l < proxies.txt) satır)"
else
    echo "ℹ️  PROXIES_TXT boş — proxy'siz çalışılacak."
fi

mkdir -p data/cookies

# ── Bot (arka plan) ──────────────────────────────────────────────────────────
echo "🤖 Bot başlatılıyor..."
nohup python3 visa_watch_onefile.py >> bot.log 2>&1 &
echo "   Bot PID: $!"

# ── Dashboard (ön plan — Render $PORT'a bağlanır) ────────────────────────────
export PORT="${PORT:-8585}"
echo "📊 Dashboard başlatılıyor → http://0.0.0.0:${PORT}"
exec python3 dashboard.py
