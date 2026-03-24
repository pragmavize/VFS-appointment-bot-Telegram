#!/bin/bash
# VISABot + Dashboard + Watchdog başlatıcı

cd /home/rootuser/visabotreal

echo "🔧 Mevcut processler durduruluyor..."
pkill -f visa_watch_onefile.py 2>/dev/null
pkill -f watchdog.py 2>/dev/null
pkill -f dashboard.py 2>/dev/null
sleep 2

echo "🤖 Bot başlatılıyor..."
nohup python3 visa_watch_onefile.py >> bot.log 2>&1 &
BOT_PID=$!
echo "   Bot PID: $BOT_PID"
sleep 2

echo "📊 Dashboard başlatılıyor (port 8585)..."
nohup python3 dashboard.py >> dashboard.log 2>&1 &
DASH_PID=$!
echo "   Dashboard PID: $DASH_PID"
sleep 1

echo "👁 Watchdog başlatılıyor..."
nohup python3 watchdog.py >> watchdog.log 2>&1 &
WD_PID=$!
echo "   Watchdog PID: $WD_PID"

echo ""
echo "✅ Tüm servisler başlatıldı!"
echo "   Dashboard: http://$(hostname -I | awk '{print $1}'):8585"
echo ""
echo "Loglar:"
echo "   Bot:       tail -f /home/rootuser/visabotreal/bot.log"
echo "   Dashboard: tail -f /home/rootuser/visabotreal/dashboard.log"
echo "   Watchdog:  tail -f /home/rootuser/visabotreal/watchdog.log"
