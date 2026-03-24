#!/bin/bash
# VISABot — Tüm Servisleri Başlatıcı
#
# Başlatılanlar:
#   1. Bot          (visa_watch_onefile.py) → bot.log
#   2. Dashboard    (dashboard.py)          → dashboard.log  [port 8585]
#   3. Watchdog     (watchdog.py)           → watchdog.log
#
# SOCKS5 Proxy Notu:
#   proxies.txt içinde socks5 protokollü proxy varsa bot, ./gost binary'si
#   üzerinden otomatik yerel tünel açar (Firefox/Camoufox auth-SOCKS5
#   desteklemediği için). gost binary'nin bu dizinde mevcut olması gerekir.
#   İndirme: https://github.com/go-gost/gost/releases
#   Kurulum:  chmod +x gost

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Renkler ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

echo -e "${BLUE}══════════════════════════════════════${NC}"
echo -e "${BLUE}  VISABot — Başlatıcı${NC}"
echo -e "${BLUE}══════════════════════════════════════${NC}"

# ── Eski süreçleri durdur ────────────────────────────────────────────────────
echo -e "\n${YELLOW}🔧 Mevcut süreçler durduruluyor...${NC}"
pkill -f visa_watch_onefile.py 2>/dev/null && echo "   Bot durduruldu" || true
pkill -f watchdog.py           2>/dev/null && echo "   Watchdog durduruldu" || true
pkill -f dashboard.py          2>/dev/null && echo "   Dashboard durduruldu" || true
pkill -f "gost"                2>/dev/null && echo "   GOST tünelleri durduruldu" || true
sleep 2

# ── GOST kontrol (SOCKS5 için) ───────────────────────────────────────────────
if [ -f "./gost" ]; then
    echo -e "\n${GREEN}✅ GOST binary mevcut — SOCKS5 proxy desteği aktif${NC}"
    chmod +x ./gost
else
    echo -e "\n${YELLOW}⚠️  GOST binary bulunamadı (./gost)${NC}"
    echo -e "   SOCKS5 proxy'ler çalışmaz. Sadece HTTP proxy kullanılacak."
    echo -e "   İndirmek için: https://github.com/go-gost/gost/releases"
fi

# ── data/ klasörü ────────────────────────────────────────────────────────────
mkdir -p data/cookies

# ── Bot ──────────────────────────────────────────────────────────────────────
echo -e "\n${GREEN}🤖 Bot başlatılıyor...${NC}"
nohup python3 visa_watch_onefile.py >> bot.log 2>&1 &
BOT_PID=$!
echo "   PID: $BOT_PID"
sleep 2

# Bot gerçekten başladı mı?
if ! kill -0 "$BOT_PID" 2>/dev/null; then
    echo -e "${RED}❌ Bot başlatılamadı! Son log:${NC}"
    tail -5 bot.log
    exit 1
fi

# ── Dashboard ────────────────────────────────────────────────────────────────
echo -e "\n${GREEN}📊 Dashboard başlatılıyor (port 8585)...${NC}"
nohup python3 dashboard.py >> dashboard.log 2>&1 &
DASH_PID=$!
echo "   PID: $DASH_PID"
sleep 1

# ── Watchdog ─────────────────────────────────────────────────────────────────
echo -e "\n${GREEN}👁  Watchdog başlatılıyor...${NC}"
nohup python3 watchdog.py >> watchdog.log 2>&1 &
WD_PID=$!
echo "   PID: $WD_PID"

# ── PID'leri kaydet ──────────────────────────────────────────────────────────
echo "$BOT_PID"  > data/bot.pid
echo "$DASH_PID" > data/dashboard.pid
echo "$WD_PID"   > data/watchdog.pid

# ── Özet ─────────────────────────────────────────────────────────────────────
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo -e "\n${BLUE}══════════════════════════════════════${NC}"
echo -e "${GREEN}✅ Tüm servisler başlatıldı!${NC}"
echo -e "${BLUE}══════════════════════════════════════${NC}"
echo -e "   🤖 Bot PID       : $BOT_PID"
echo -e "   📊 Dashboard     : http://${HOST_IP}:8585"
echo -e "   👁  Watchdog PID  : $WD_PID"
echo ""
echo -e "Loglar:"
echo -e "   ${YELLOW}tail -f ${SCRIPT_DIR}/bot.log${NC}"
echo -e "   ${YELLOW}tail -f ${SCRIPT_DIR}/dashboard.log${NC}"
echo -e "   ${YELLOW}tail -f ${SCRIPT_DIR}/watchdog.log${NC}"
echo ""
echo -e "Durdurmak için:"
echo -e "   ${YELLOW}pkill -f visa_watch_onefile.py; pkill -f dashboard.py; pkill -f watchdog.py; pkill -f gost${NC}"
echo ""
