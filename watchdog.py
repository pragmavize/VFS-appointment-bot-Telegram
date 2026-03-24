#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VISABot Watchdog — Bot çökerse otomatik yeniden başlat + Telegram bildirimi.
"""

import os
import sys
os.environ["PYTHONUNBUFFERED"] = "1"
import time
import subprocess
import logging
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR   = Path(__file__).resolve().parent
LOG_PATH   = BASE_DIR / "bot.log"
BOT_SCRIPT = BASE_DIR / "visa_watch_onefile.py"
PID_FILE   = BASE_DIR / "data" / "bot.pid"

load_dotenv(BASE_DIR / ".env")
TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = int(os.getenv("WATCHDOG_NOTIFY_ID", "5816716319"))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
# Her log satırı hemen flush edilsin
for h in logging.root.handlers:
    h.flush = lambda: sys.stdout.flush()

log = logging.getLogger("watchdog")

CHECK_INTERVAL   = 30    # saniye — kaç saniyede bir bot kontrolü
CRASH_COOLDOWN   = 15    # saniye — crash sonrası yeniden başlatmadan önce bekleme
MAX_RESTARTS_1H  = 5     # 1 saatte maksimum kaç kez yeniden başlat
DAILY_REPORT_H   = 9     # Her gün saat kaçta özet rapor gönderilsin (0-23)

_restart_times: list[float] = []
_last_daily_report: float = 0.0
_bot_proc: subprocess.Popen | None = None


# ──────────────────────────────────────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────────────────────────────────────

def tg_send(text: str) -> bool:
    """Telegram'a mesaj gönder."""
    if not TOKEN:
        return False
    try:
        import urllib.request
        import urllib.parse
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":    OWNER_ID,
            "text":       text,
            "parse_mode": "Markdown",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        log.warning(f"Telegram gönderilemedi: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Bot yönetimi
# ──────────────────────────────────────────────────────────────────────────────

def bot_pid() -> int | None:
    """Çalışan bot PID'ini döndürür."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "visa_watch_onefile.py"], text=True
        ).strip()
        pids = [int(p) for p in out.split() if p.isdigit()]
        # watchdog'un kendi PID'ini hariç tut
        me = os.getpid()
        pids = [p for p in pids if p != me]
        return pids[0] if pids else None
    except Exception:
        return None


def start_bot() -> subprocess.Popen:
    log_file = open(LOG_PATH, "a")
    proc = subprocess.Popen(
        [sys.executable, str(BOT_SCRIPT)],
        cwd=str(BASE_DIR),
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(proc.pid))
    log.info(f"Bot başlatıldı (PID: {proc.pid})")
    return proc


def last_errors(n: int = 5) -> str:
    """Log dosyasından son N hata satırını döndürür."""
    try:
        lines = LOG_PATH.read_text(errors="replace").splitlines()
        errs = [l for l in reversed(lines) if "] ERROR:" in l or "] WARNING:" in l][:n]
        return "\n".join(reversed(errs)) if errs else "Hata yok."
    except Exception:
        return "-"


def stats_line() -> str:
    """optimizer.py'den kısa istatistik çeker."""
    try:
        sys.path.insert(0, str(BASE_DIR))
        from optimizer import stats_summary
        return stats_summary(24)
    except Exception:
        return "-"


# ──────────────────────────────────────────────────────────────────────────────
# Raporlama
# ──────────────────────────────────────────────────────────────────────────────

def send_crash_and_restart(pid_was: int | None, restart_count: int, new_pid: int | None) -> None:
    errs = last_errors(5)
    restart_info = f"✅ Yeniden başlatıldı (PID: `{new_pid}`)" if new_pid else "❌ Yeniden başlatma başarısız!"
    msg = (
        f"🚨 *VISABot Çöktü — Otomatik Yeniden Başlatıldı*\n\n"
        f"Yeniden başlatma no: *{restart_count}*\n"
        f"{restart_info}\n\n"
        f"*🔴 Bot Hata Mesajı:*\n```\n{errs[:600]}\n```"
    )
    tg_send(msg)


def send_daily_report() -> None:
    st = stats_line()
    pid = bot_pid()
    status = f"✅ Çalışıyor (PID: `{pid}`)" if pid else "❌ Durdurulmuş"
    msg = (
        f"📋 *Günlük VISABot Raporu*\n\n"
        f"🤖 Bot durumu: {status}\n\n"
        f"{st}"
    )
    tg_send(msg)


def send_limit_alert(count: int) -> None:
    tg_send(
        f"⚠️ *VISABot 1 saatte {count} kez çöktü!*\n"
        f"Otomatik yeniden başlatma durduruldu.\n"
        f"Dashboard'dan veya SSH ile manuel başlatın."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Ana döngü
# ──────────────────────────────────────────────────────────────────────────────

def main():
    global _last_daily_report

    log.info("Watchdog başlatılıyor...")
    sys.stdout.flush()

    # Bot çalışmıyorsa hemen başlat
    log.info("Bot PID kontrol ediliyor...")
    sys.stdout.flush()
    pid = bot_pid()
    log.info(f"Bot PID: {pid}")
    sys.stdout.flush()
    if not pid:
        log.info("Bot bulunamadı, başlatılıyor...")
        proc = start_bot()
        time.sleep(3)
        pid = bot_pid()

    log.info("Watchdog hazır, döngü başlıyor.")
    sys.stdout.flush()

    restart_count = 0

    while True:
        time.sleep(CHECK_INTERVAL)

        # ── Günlük rapor ──
        now = time.localtime()
        if now.tm_hour == DAILY_REPORT_H:
            since_last = time.time() - _last_daily_report
            if since_last > 3600 * 20:  # günde en fazla 1
                send_daily_report()
                _last_daily_report = time.time()

        # ── Bot kontrolü ──
        pid = bot_pid()
        if pid:
            continue  # İyi, çalışıyor

        # Bot çökmüş
        log.warning("Bot çökmüş!")

        # Son 1 saatte kaç kez çöktü?
        now_ts = time.time()
        _restart_times.append(now_ts)
        # 1 saatten eski girişleri temizle
        recent = [t for t in _restart_times if now_ts - t < 3600]
        _restart_times[:] = recent

        if len(_restart_times) > MAX_RESTARTS_1H:
            log.error(f"1 saatte {len(_restart_times)} crash — otomatik restart durduruldu!")
            send_limit_alert(len(_restart_times))
            # 30 dakika bekle sonra tekrar dene
            time.sleep(1800)
            _restart_times.clear()
            continue

        log.info(f"{CRASH_COOLDOWN}s bekleniyor sonra yeniden başlatılıyor...")
        time.sleep(CRASH_COOLDOWN)

        # Yeniden başlat (çift instance önlemi)
        if bot_pid():
            log.warning("Bot zaten çalışıyor, yeniden başlatma atlandı.")
            continue
        proc = start_bot()
        restart_count += 1
        time.sleep(3)
        new_pid = bot_pid()

        # Crash + restart bildirimini tek mesajda gönder
        send_crash_and_restart(None, restart_count, new_pid)

        if new_pid:
            log.info(f"Bot yeniden başlatıldı: PID={new_pid}")
        else:
            log.error("Yeniden başlatma başarısız!")


if __name__ == "__main__":
    main()
